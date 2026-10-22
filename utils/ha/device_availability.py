"""Device availability monitoring — pure evaluator + snapshot persistence.

A device is considered offline when every non-disabled entity linked to it has
state ``unavailable``.  Findings are grouped by config entry so that a cloud-side
outage affecting many devices in one integration produces a single investigation,
not one per device.

Arming rule: a device only alerts if Pueo has previously seen it online
(``last_online_ts`` present in the snapshot).  Devices that are already offline
on the first run (seasonal, unplugged, Christmas lights …) never alert until they
come back at least once.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

from utils.core.logging import get_logger

log = get_logger("device_availability")

# ------------------------------------------------------------------
# Data shapes
# ------------------------------------------------------------------

_REGISTRY_TTL_SECONDS = 1800  # 30 min registry cache


@dataclass
class DeviceState:
    """Current knowledge about one device, stored in the snapshot."""

    device_id: str
    name: str
    config_entry_id: str  # primary config entry
    domain: str  # integration domain (from config entry, e.g. "econet")
    last_online_ts: Optional[float]  # None = never seen online (unarmed)
    alerted: bool  # True while an offline card is open


@dataclass
class OfflineFinding:
    """One group of offline devices sharing a config entry."""

    config_entry_id: str
    domain: str
    devices: list[dict]  # [{"name": …, "device_id": …, "offline_since": <ts>}]


@dataclass
class AvailabilityResult:
    """Output of evaluate_device_availability()."""

    new_offline: list[OfflineFinding]
    recovered: list[OfflineFinding]  # devices whose alert is now resolved
    snapshot: dict  # updated snapshot blob (device_id → DeviceState as dict)


# ------------------------------------------------------------------
# Evaluator (pure — no I/O)
# ------------------------------------------------------------------


def evaluate_device_availability(
    states: list[dict],
    entity_registry: list[dict],
    device_registry: list[dict],
    config_entries: list[dict],
    prior: dict,  # previously persisted snapshot blob
    now: float,
    grace_s: float,
) -> AvailabilityResult:
    """Evaluate device availability from HA state snapshots.

    Parameters
    ----------
    states:
        Output of ``get_states()`` — all HA entity state dicts.
    entity_registry:
        Output of ``get_entity_registry()`` — all HA entity registry dicts.
    device_registry:
        Output of ``get_device_registry()`` — all HA device registry dicts.
    config_entries:
        Output of ``get_all_config_entries()`` — all HA config entry dicts.
    prior:
        The snapshot blob from the last run (device_id → DeviceState fields as
        dict).  Pass ``{}`` on the first run.
    now:
        Current Unix timestamp (``time.time()``).
    grace_s:
        Grace period in seconds.  A device must be offline for at least this
        long before it appears in ``new_offline``.

    Returns
    -------
    AvailabilityResult
        ``new_offline`` lists findings for armed devices newly past the grace
        period and not already alerted.  ``recovered`` lists findings for
        previously-alerted devices that are back online.  ``snapshot`` is the
        updated state blob to persist.
    """
    # Index states by entity_id for fast lookup
    state_by_id: dict[str, dict] = {s["entity_id"]: s for s in states}

    # Index config entries to get domain by entry_id
    domain_by_entry: dict[str, str] = {
        e["entry_id"]: e.get("domain", "") for e in config_entries
    }

    # Group entity registry rows by device_id, ignoring disabled entities
    entities_by_device: dict[str, list[dict]] = {}
    for ent in entity_registry:
        device_id = ent.get("device_id")
        if not device_id:
            continue
        if ent.get("disabled_by"):
            continue
        entities_by_device.setdefault(device_id, []).append(ent)

    # Build current DeviceState for every device that has entities
    current: dict[str, DeviceState] = {}
    for dev in device_registry:
        device_id = dev["id"]
        entities = entities_by_device.get(device_id, [])
        if not entities:
            continue

        # Pick the primary config entry (first non-empty)
        config_entry_ids: list[str] = dev.get("config_entries", [])
        primary_entry_id = config_entry_ids[0] if config_entry_ids else ""
        domain = domain_by_entry.get(primary_entry_id, "")

        name = dev.get("name_by_user") or dev.get("name") or device_id

        # Determine if the device is currently online (at least one entity is
        # not unavailable — and every entity has a state row)
        entity_states = [state_by_id.get(e["entity_id"]) for e in entities]
        # If any entity has no state yet, treat device as unknown / not offline
        if any(s is None for s in entity_states):
            all_unavailable = False
        else:
            all_unavailable = all(
                s["state"] == "unavailable" for s in entity_states  # type: ignore[index]
            )

        prior_entry = prior.get(device_id, {})
        last_online_ts: Optional[float] = prior_entry.get("last_online_ts")
        alerted: bool = bool(prior_entry.get("alerted", False))

        if not all_unavailable:
            # Device is online — update last_online_ts
            last_online_ts = now
            alerted = False  # recovery resets alerted flag

        current[device_id] = DeviceState(
            device_id=device_id,
            name=name,
            config_entry_id=primary_entry_id,
            domain=domain,
            last_online_ts=last_online_ts,
            alerted=alerted,
        )

    # Detect new offline and recovered states
    new_offline_by_entry: dict[str, list[dict]] = {}
    recovered_by_entry: dict[str, list[dict]] = {}

    for device_id, ds in current.items():
        prior_entry = prior.get(device_id, {})
        prior_alerted = bool(prior_entry.get("alerted", False))

        # Check if offline now (last_online_ts not updated to `now`)
        is_offline = ds.last_online_ts != now

        if is_offline and not ds.alerted:
            if ds.last_online_ts is None:
                # Never seen online — unarmed, skip
                continue
            # Find offline-since from entity last_changed timestamps
            offline_since = _compute_offline_since(
                entities_by_device.get(device_id, []), state_by_id
            )
            if offline_since is None:
                offline_since = ds.last_online_ts

            offline_duration = now - offline_since
            if offline_duration >= grace_s:
                entry_id = ds.config_entry_id
                new_offline_by_entry.setdefault(entry_id, []).append(
                    {
                        "name": ds.name,
                        "device_id": device_id,
                        "domain": ds.domain,
                        "offline_since": offline_since,
                    }
                )
                ds.alerted = True

        elif prior_alerted and not is_offline:
            # Was alerted, now online — recovery
            entry_id = ds.config_entry_id
            recovered_by_entry.setdefault(entry_id, []).append(
                {
                    "name": ds.name,
                    "device_id": device_id,
                    "domain": ds.domain,
                    "offline_since": prior_entry.get("offline_since"),
                }
            )

    # Build grouped findings
    new_offline_findings = [
        OfflineFinding(
            config_entry_id=entry_id,
            domain=devices[0]["domain"] if devices else "",
            devices=devices,
        )
        for entry_id, devices in new_offline_by_entry.items()
    ]
    recovered_findings = [
        OfflineFinding(
            config_entry_id=entry_id,
            domain=devices[0]["domain"] if devices else "",
            devices=devices,
        )
        for entry_id, devices in recovered_by_entry.items()
    ]

    # Build updated snapshot
    snapshot = {
        device_id: {
            "last_online_ts": ds.last_online_ts,
            "alerted": ds.alerted,
        }
        for device_id, ds in current.items()
    }

    return AvailabilityResult(
        new_offline=new_offline_findings,
        recovered=recovered_findings,
        snapshot=snapshot,
    )


def _compute_offline_since(
    entities: list[dict],
    state_by_id: dict[str, dict],
) -> Optional[float]:
    """Return the latest last_changed timestamp among all unavailable entities.

    This is the point when the device as a whole became unreachable.
    """
    import datetime

    latest: Optional[float] = None
    for ent in entities:
        state = state_by_id.get(ent["entity_id"])
        if state is None or state.get("state") != "unavailable":
            continue
        lc = state.get("last_changed")
        if lc is None:
            continue
        try:
            ts = (
                datetime.datetime.fromisoformat(lc.rstrip("Z"))
                .replace(tzinfo=datetime.timezone.utc)
                .timestamp()
            )
        except Exception:  # nosec B112
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


# ------------------------------------------------------------------
# Snapshot persistence (user_prefs JSON blob, no migration needed)
# ------------------------------------------------------------------

_SNAPSHOT_KEY = "device_availability:state"


def load_snapshot(conn: sqlite3.Connection) -> dict:
    """Load the device availability snapshot from user_prefs.  Returns {} if absent."""
    try:
        row = conn.execute(
            "SELECT value FROM user_prefs WHERE key = ?", (_SNAPSHOT_KEY,)
        ).fetchone()
        if row:
            return json.loads(row[0])
    except Exception as e:  # nosec B110
        log.warning("device_availability_snapshot_load_failed", error=str(e))
    return {}


def save_snapshot(conn: sqlite3.Connection, snapshot: dict) -> None:
    """Persist the device availability snapshot to user_prefs."""
    try:
        conn.execute(
            "INSERT INTO user_prefs (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_SNAPSHOT_KEY, json.dumps(snapshot)),
        )
        conn.commit()
    except Exception as e:  # nosec B110
        log.warning("device_availability_snapshot_save_failed", error=str(e))


# ------------------------------------------------------------------
# Investigation stub (replaced by the real implementation in S3)
# ------------------------------------------------------------------


async def _run_device_availability_investigation(
    finding: OfflineFinding,
    db_path: str,
    notifier: object,
    llm_client: object,
    knowledge_store: object,
) -> None:
    """Delegate to the investigation module."""
    from utils.ha.device_availability_investigation import (
        run_device_availability_investigation,
    )

    await run_device_availability_investigation(
        finding=finding,
        notifier=notifier,
        db_path=db_path,
        llm_client=llm_client,
        knowledge_store=knowledge_store,
    )


# ------------------------------------------------------------------
# Poll loop
# ------------------------------------------------------------------

_POLL_INTERVAL_SECONDS = 60
_REGISTRY_CACHE_SECONDS = _REGISTRY_TTL_SECONDS  # 1800 s


async def poll_device_availability(
    ws_client=None,  # Optional[HAWebSocketClientProtocol]
    db_path: Optional[str] = None,
    notifier=None,  # Optional[NotifierProtocol]
    llm_client=None,  # Optional[LLMClientProtocol]
    knowledge_store=None,
) -> None:
    """Periodically evaluate device availability and investigate armed devices going offline."""
    import asyncio

    import config as _cfg

    from utils.agent.work_queue import (
        PRIORITY_NORMAL,
        WorkItem,
        get_work_queue_or_none,
    )
    from utils.core.timeline import write_timeline_event
    from utils.ha.ha_ws_client import HAWebSocketClient
    from utils.hitl.card_types import CARD_TYPE_DEVICE_OFFLINE
    from utils.hitl.hitl_tracker import (
        mark_card_resolved,
        should_send_card,
    )
    from utils.hitl.notify import get_notifier

    _db: str = db_path or _cfg.DB_PATH
    _ws = ws_client or HAWebSocketClient(  # pragma: no cover
        _cfg.HA_HOST, _cfg.HA_API_PORT, _cfg.HA_API_TOKEN
    )
    _notifier = notifier or get_notifier(  # pragma: no cover
        _cfg.NOTIFIER, _cfg.NOTIFY_URL, _cfg.NOTIFY_WATCH_DIR
    )

    _registry_cache: Optional[dict] = None
    _registry_cache_ts: float = 0.0

    while True:
        try:
            now = time.time()

            # Refresh registries every 30 min; fetch states every poll.
            if (
                _registry_cache is None
                or (now - _registry_cache_ts) > _REGISTRY_CACHE_SECONDS
            ):
                entity_registry = await _ws.get_entity_registry()
                device_registry = await _ws.get_device_registry()
                config_entries = await _ws.get_all_config_entries()
                _registry_cache = {
                    "entity_registry": entity_registry,
                    "device_registry": device_registry,
                    "config_entries": config_entries,
                }
                _registry_cache_ts = now
            else:
                entity_registry = _registry_cache["entity_registry"]
                device_registry = _registry_cache["device_registry"]
                config_entries = _registry_cache["config_entries"]

            states = await _ws.get_states()

            with sqlite3.connect(_db) as conn:
                prior = load_snapshot(conn)

            grace_s = _cfg.DEVICE_AVAILABILITY_GRACE_MINUTES * 60
            result = evaluate_device_availability(
                states=states,
                entity_registry=entity_registry,
                device_registry=device_registry,
                config_entries=config_entries,
                prior=prior,
                now=now,
                grace_s=grace_s,
            )

            with sqlite3.connect(_db) as conn:
                save_snapshot(conn, result.snapshot)

            # Process newly offline devices.
            for finding in result.new_offline:
                card_key = f"device_offline:{finding.config_entry_id}"
                backoff_key = f"device_offline_backoff:{finding.config_entry_id}"

                with sqlite3.connect(_db) as conn:
                    if not should_send_card(conn, card_key):
                        log.info(
                            "device_offline_suppressed",
                            config_entry_id=finding.config_entry_id,
                        )
                        continue
                    if not should_send_card(conn, backoff_key):
                        log.info(
                            "device_offline_backoff",
                            config_entry_id=finding.config_entry_id,
                        )
                        continue

                # Build a human-readable summary for the timeline.
                names = [d["name"] for d in finding.devices]
                if len(names) == 1:
                    label = names[0]
                else:
                    label = f"{names[0]} (+{len(names) - 1} more)"

                oldest_offline_since = min(
                    d.get("offline_since") or now for d in finding.devices
                )
                offline_minutes = int((now - oldest_offline_since) / 60)
                tl_msg = (
                    f"Device offline {offline_minutes}m: {label} ({finding.domain})"
                )

                await asyncio.to_thread(
                    write_timeline_event,
                    "WARNING",
                    "device_availability_poll",
                    tl_msg,
                    {
                        "config_entry_id": finding.config_entry_id,
                        "domain": finding.domain,
                        "devices": finding.devices,
                    },
                )

                log.warning(
                    "device_offline_detected",
                    config_entry_id=finding.config_entry_id,
                    domain=finding.domain,
                    devices=[d["name"] for d in finding.devices],
                    offline_minutes=offline_minutes,
                )

                _finding = finding
                _db_ref = _db
                _notifier_ref = _notifier
                _llm_ref = llm_client
                _ks_ref = knowledge_store

                async def _investigate_coro(
                    _f: OfflineFinding = _finding,
                    _d: str = _db_ref,
                    _n: object = _notifier_ref,
                    _l: object = _llm_ref,
                    _k: object = _ks_ref,
                ) -> None:
                    await _run_device_availability_investigation(
                        finding=_f,
                        db_path=_d,
                        notifier=_n,
                        llm_client=_l,
                        knowledge_store=_k,
                    )

                _wq = get_work_queue_or_none()
                if _wq is not None:
                    await _wq.submit(
                        WorkItem(
                            priority=PRIORITY_NORMAL,
                            activity_type="device_availability",
                            description=f"Device offline investigation: {finding.domain}",
                            dedup_key=f"device_offline:{finding.config_entry_id}",
                            suppress_while_running=frozenset(),
                            coro_factory=_investigate_coro,
                        )
                    )
                else:
                    await _investigate_coro()

            # Process recoveries.
            for finding in result.recovered:
                card_key = f"device_offline:{finding.config_entry_id}"

                names = [d["name"] for d in finding.devices]
                if len(names) == 1:
                    label = names[0]
                else:
                    label = f"{names[0]} (+{len(names) - 1} more)"

                # Use oldest offline_since to compute total outage duration.
                oldest_offline_since = (
                    min(
                        (d.get("offline_since") or now)
                        for d in finding.devices
                        if d.get("offline_since") is not None
                    )
                    if any(d.get("offline_since") for d in finding.devices)
                    else now
                )
                duration_minutes = int((now - oldest_offline_since) / 60)

                tl_msg = f"Device back online after {duration_minutes}m: {label} ({finding.domain})"

                await asyncio.to_thread(
                    write_timeline_event,
                    "INFO",
                    "device_availability_poll",
                    tl_msg,
                    {
                        "config_entry_id": finding.config_entry_id,
                        "domain": finding.domain,
                        "devices": finding.devices,
                        "duration_minutes": duration_minutes,
                    },
                )

                with sqlite3.connect(_db) as conn:
                    mark_card_resolved(conn, card_key)

                log.info(
                    "device_recovered",
                    config_entry_id=finding.config_entry_id,
                    domain=finding.domain,
                    devices=[d["name"] for d in finding.devices],
                    duration_minutes=duration_minutes,
                )

        except Exception as exc:
            log.warning("device_availability_poll_failed", error=str(exc))

        _outcome = (
            f"{len(result.new_offline)} offline, {len(result.recovered)} recovered"
            if "result" in dir()
            else "poll_error"
        )
        try:
            from utils.agent.supervisor import get_supervisor_instance as _get_sv

            _sv = _get_sv()
            if _sv is not None:
                _sv.touch("device_availability_poll", outcome=_outcome)
        except Exception:  # nosec B110
            pass

        from utils.agent.supervisor import supervised_sleep as _sup_sleep_da

        await _sup_sleep_da("device_availability_poll", _POLL_INTERVAL_SECONDS)
