"""Device availability monitoring — pure evaluator + snapshot persistence.

A device is considered offline when every non-disabled entity linked to it has
state ``unavailable``.  Findings are grouped by config entry so that a cloud-side
outage affecting many devices in one integration produces a single investigation,
not one per device.

Arming rule: a device only alerts if Pueo has previously seen it online
(``last_online_ts`` present in the snapshot).  Devices that are already offline
on the first run (seasonal, unplugged, Christmas lights …) never alert until they
come back at least once.

Restart safety: a missing state row or an ``unknown`` state means the device's
status is *unknown* — never online — so an HA restart can't fake a recovery.
Evaluation is skipped entirely when the states payload is incomplete, or within
the settle window after an HA start (``user_prefs`` ``ha:last_start_ts``).
``offline_since`` is pinned when a device is first seen offline and kept until
it is seen online again, so restarts don't reset it.

Recurrence: per-device episode history is stored in the snapshot JSON alongside
DeviceState fields (no DB migration needed).  After DEVICE_RECURRENCE_COUNT
self-recoveries within DEVICE_RECURRENCE_WINDOW_DAYS, Pueo sends one
``device_recurring`` card instead of launching another investigation.
"""

from __future__ import annotations

import hashlib
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
    offline_since: Optional[float] = (
        None  # persisted until seen online again; drives recovery duration
    )
    investigated: bool = False  # an investigation ran during this episode


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
    skipped: Optional[str] = None  # "incomplete_states" | "settling" when skipped
    state_coverage: float = 1.0  # fraction of registry entities with a state row


# A device is online only if some entity reports a real state.
_NOT_REAL_STATES = frozenset({"unavailable", "unknown"})


def _device_status(entity_states: list[Optional[dict]]) -> str:
    """Return "online", "offline" or "unknown" for one device's entity states.

    Missing rows and ``unknown`` states (HA still starting) are not evidence
    either way, so a device is offline only when every entity has a state row
    and all of them are ``unavailable``.
    """
    if any(s is not None and s["state"] not in _NOT_REAL_STATES for s in entity_states):
        return "online"
    if all(s is not None and s["state"] == "unavailable" for s in entity_states):
        return "offline"
    return "unknown"


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
    min_state_coverage: float = 0.0,
    ha_start_ts: Optional[float] = None,
    settle_s: float = 0.0,
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
    min_state_coverage:
        Minimum fraction of registry entities that must have a state row.
        Below it the payload is treated as incomplete (HA starting up) and the
        evaluation is skipped.
    ha_start_ts:
        Unix timestamp of the last HA start, or ``None`` if unknown.
    settle_s:
        Settle window in seconds after ``ha_start_ts``.  No transitions are
        evaluated inside it.

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

    def _skipped(reason: str, coverage: float) -> AvailabilityResult:
        return AvailabilityResult(
            new_offline=[],
            recovered=[],
            snapshot=dict(prior),
            skipped=reason,
            state_coverage=coverage,
        )

    # Incomplete payload (HA still starting) — no conclusions either way.
    all_entity_ids = [
        e["entity_id"] for ents in entities_by_device.values() for e in ents
    ]
    coverage = (
        sum(1 for eid in all_entity_ids if eid in state_by_id) / len(all_entity_ids)
        if all_entity_ids
        else 1.0
    )
    if coverage < min_state_coverage:
        return _skipped("incomplete_states", coverage)

    # Settle window after an HA start — restored and half-loaded states lie.
    if ha_start_ts is not None and now < ha_start_ts + settle_s:
        return _skipped("settling", coverage)

    current: dict[str, DeviceState] = {}
    new_offline_by_entry: dict[str, list[dict]] = {}
    recovered_by_entry: dict[str, list[dict]] = {}

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

        prior_entry = prior.get(device_id, {})
        ds = DeviceState(
            device_id=device_id,
            name=name,
            config_entry_id=primary_entry_id,
            domain=domain,
            last_online_ts=prior_entry.get("last_online_ts"),
            alerted=bool(prior_entry.get("alerted", False)),
            offline_since=prior_entry.get("offline_since"),
            investigated=bool(prior_entry.get("investigated", False)),
        )
        current[device_id] = ds

        status = _device_status([state_by_id.get(e["entity_id"]) for e in entities])

        if status == "online":
            if ds.alerted:
                recovered_by_entry.setdefault(primary_entry_id, []).append(
                    {
                        "name": name,
                        "device_id": device_id,
                        "domain": domain,
                        "offline_since": ds.offline_since,
                        "investigated": ds.investigated,
                    }
                )
            ds.last_online_ts = now
            ds.alerted = False
            ds.offline_since = None
            ds.investigated = False
        elif status == "offline":
            if ds.alerted or ds.last_online_ts is None:
                # Already alerted, or never seen online (unarmed) — nothing new.
                continue
            # Pin offline_since on first sight; a restart resets last_changed.
            if ds.offline_since is None:
                ds.offline_since = (
                    _compute_offline_since(entities, state_by_id) or ds.last_online_ts
                )
            if now - ds.offline_since >= grace_s:
                new_offline_by_entry.setdefault(primary_entry_id, []).append(
                    {
                        "name": name,
                        "device_id": device_id,
                        "domain": domain,
                        "offline_since": ds.offline_since,
                    }
                )
                ds.alerted = True
        # status == "unknown": keep everything as it was.

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
            "offline_since": ds.offline_since,
            "investigated": ds.investigated,
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


_HA_START_KEY = "ha:last_start_ts"


def load_ha_start_ts(conn: sqlite3.Connection) -> Optional[float]:
    """Return the last recorded HA start timestamp, or None if absent."""
    try:
        row = conn.execute(
            "SELECT value FROM user_prefs WHERE key = ?", (_HA_START_KEY,)
        ).fetchone()
        if row:
            return float(row[0])
    except Exception as e:  # nosec B110
        log.warning("ha_start_ts_load_failed", error=str(e))
    return None


def record_ha_start(db_path: str, ts: float) -> None:
    """Record an HA start (WS reconnect or host reboot).  Never moves backwards."""
    try:
        with sqlite3.connect(db_path) as conn:
            prev = load_ha_start_ts(conn)
            if prev is not None and prev >= ts:
                return
            conn.execute(
                "INSERT INTO user_prefs (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_HA_START_KEY, str(ts)),
            )
            conn.commit()
        log.info("ha_start_recorded", ts=ts)
    except Exception as e:  # nosec B110
        log.warning("ha_start_record_failed", error=str(e))


# ------------------------------------------------------------------
# Recurrence tracking (pure — no I/O)
# ------------------------------------------------------------------

_RECURRENCE_KEY = "device_availability:episodes"
_EPISODE_RETENTION_DAYS = 30


def record_episode(
    episodes: dict,
    device_id: str,
    offline_at: float,
    recovered_at: float,
    self_recovered: bool,
    investigated: bool,
) -> dict:
    """Return updated episodes dict with the new episode appended.

    Keeps only episodes within _EPISODE_RETENTION_DAYS of recovered_at.
    episodes maps device_id → list of {offline_at, recovered_at,
    self_recovered, investigated}.
    """
    cutoff = recovered_at - _EPISODE_RETENTION_DAYS * 86400
    history = [
        e for e in episodes.get(device_id, []) if e.get("recovered_at", 0) >= cutoff
    ]
    history.append(
        {
            "offline_at": offline_at,
            "recovered_at": recovered_at,
            "self_recovered": self_recovered,
            "investigated": investigated,
        }
    )
    return {**episodes, device_id: history}


def count_self_recovered_in_window(
    episodes: dict,
    device_id: str,
    now: float,
    window_days: int,
) -> int:
    """Count self-recovered episodes for device_id within window_days of now."""
    cutoff = now - window_days * 86400
    return sum(
        1
        for e in episodes.get(device_id, [])
        if e.get("self_recovered") and e.get("recovered_at", 0) >= cutoff
    )


def build_recurrence_fact(
    device_name: str,
    domain: str,
    episodes: dict,
    device_id: str,
    now: float,
    window_days: int,
) -> str:
    """Return a human-readable fact string for agent_memory."""
    recent = [
        e
        for e in episodes.get(device_id, [])
        if e.get("self_recovered")
        and e.get("recovered_at", 0) >= now - window_days * 86400
    ]
    count = len(recent)
    if count == 0:
        return ""
    # Build time range description
    offline_times = [e["offline_at"] for e in recent if e.get("offline_at")]
    # Per-episode durations — sorting the two lists separately mispairs them.
    durations = [
        e["recovered_at"] - e["offline_at"]
        for e in recent
        if e.get("offline_at") and e.get("recovered_at")
    ]
    if offline_times and durations:
        import datetime

        def _hm(ts: float) -> str:
            return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")

        earliest = min(offline_times)
        latest = max(offline_times)
        if len(set(_hm(t) for t in offline_times)) == 1:
            time_range = f"around {_hm(offline_times[0])}"
        else:
            time_range = f"between {_hm(earliest)} and {_hm(latest)}"
        avg_duration = int(sum(durations) / len(durations) / 60)
        return (
            f"{device_name} ({domain}) went offline {count}× in {window_days} days,"
            f" {time_range}, recovering on its own (avg {avg_duration} min)."
        )
    return (
        f"{device_name} ({domain}) went offline {count}× in {window_days} days,"
        " self-recovering each time."
    )


def load_episodes(conn: sqlite3.Connection) -> dict:
    """Load the per-device episode history from user_prefs.  Returns {} if absent."""
    try:
        row = conn.execute(
            "SELECT value FROM user_prefs WHERE key = ?", (_RECURRENCE_KEY,)
        ).fetchone()
        if row:
            return json.loads(row[0])
    except Exception as e:  # nosec B110
        log.warning("device_episodes_load_failed", error=str(e))
    return {}


def save_episodes(conn: sqlite3.Connection, episodes: dict) -> None:
    """Persist per-device episode history to user_prefs."""
    try:
        conn.execute(
            "INSERT INTO user_prefs (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_RECURRENCE_KEY, json.dumps(episodes)),
        )
        conn.commit()
    except Exception as e:  # nosec B110
        log.warning("device_episodes_save_failed", error=str(e))


def upsert_recurrence_fact(
    db_path: str,
    device_id: str,
    fact: str,
    domain: str,
) -> None:
    """Write or update one agent_memory fact for a recurring device."""
    if not fact:
        return
    signature = f"device:offline:{domain}"
    key = "fact_" + hashlib.sha256(f"{device_id}:recurrence".encode()).hexdigest()[:12]
    now = time.time()
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "DELETE FROM agent_memory WHERE key = ? AND signature = ?",
                (key, signature),
            )
            conn.execute(
                "INSERT INTO agent_memory (key, content, source, ts, signature)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, fact, "observed", now, signature),
            )
            conn.commit()
    except Exception as e:  # nosec B110
        log.warning("upsert_recurrence_fact_failed", error=str(e))


# ------------------------------------------------------------------
# Investigation stub (replaced by the real implementation in S3)
# ------------------------------------------------------------------


async def _run_device_availability_investigation(
    finding: OfflineFinding,
    db_path: str,
    notifier: object,
    llm_client: object,
    knowledge_store: object,
    netalertx_api_client: object = None,
    ws_client: object = None,
    ha_rest_client: object = None,
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
        netalertx_api_client=netalertx_api_client,
        ws_client=ws_client,
        ha_rest_client=ha_rest_client,
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
    netalertx_api_client=None,  # Optional[NetAlertXAPIClient]
    ha_rest_client=None,  # Optional[HARestClientProtocol]
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
    from utils.hitl.card_types import (
        CARD_TYPE_DEVICE_OFFLINE,
        CARD_TYPE_DEVICE_RECURRING,
    )
    from utils.hitl.hitl_tracker import (
        mark_card_resolved,
        should_send_card,
        was_card_actioned,
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
                ha_start_ts = load_ha_start_ts(conn)

            grace_s = _cfg.DEVICE_AVAILABILITY_GRACE_MINUTES * 60
            result = evaluate_device_availability(
                states=states,
                entity_registry=entity_registry,
                device_registry=device_registry,
                config_entries=config_entries,
                prior=prior,
                now=now,
                grace_s=grace_s,
                min_state_coverage=_cfg.DEVICE_AVAILABILITY_MIN_STATE_COVERAGE,
                ha_start_ts=ha_start_ts,
                settle_s=_cfg.DEVICE_AVAILABILITY_RESTART_SETTLE_MINUTES * 60,
            )

            if result.skipped == "incomplete_states":
                log.info(
                    "device_availability_skipped_incomplete_states",
                    state_coverage=round(result.state_coverage, 3),
                )
            elif result.skipped == "settling":
                log.info(
                    "device_availability_skipped_settling",
                    ha_start_ts=ha_start_ts,
                )
            else:
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

                # Recurrence gate — check before submitting an investigation.
                with sqlite3.connect(_db) as conn:
                    episodes_blob = load_episodes(conn)
                _recurrence_triggered = False
                for _dev in finding.devices:
                    _dev_id = _dev["device_id"]
                    _self_count = count_self_recovered_in_window(
                        episodes_blob,
                        _dev_id,
                        now,
                        _cfg.DEVICE_RECURRENCE_WINDOW_DAYS,
                    )
                    if _self_count >= _cfg.DEVICE_RECURRENCE_COUNT:
                        _recurrence_triggered = True
                        _recurring_device = _dev
                        _recurring_count = _self_count
                        break

                if _recurrence_triggered:
                    _rec_card_key = f"device_recurring:{_recurring_device['device_id']}"
                    with sqlite3.connect(_db) as conn:
                        _can_send_recurring = should_send_card(conn, _rec_card_key)
                    if _can_send_recurring:
                        _rec_name = _recurring_device["name"]
                        _rec_msg = (
                            f"{_rec_name} ({finding.domain}) has gone offline and"
                            f" self-recovered {_recurring_count}× in"
                            f" {_cfg.DEVICE_RECURRENCE_WINDOW_DAYS} days."
                            " No investigation launched."
                        )
                        await asyncio.to_thread(
                            write_timeline_event,
                            "INFO",
                            "device_availability_poll",
                            _rec_msg,
                            {
                                "config_entry_id": finding.config_entry_id,
                                "domain": finding.domain,
                                "device_id": _recurring_device["device_id"],
                                "recurrence_count": _recurring_count,
                            },
                        )
                        log.info(
                            "device_recurrence_triggered",
                            device=_rec_name,
                            domain=finding.domain,
                            count=_recurring_count,
                        )
                        from utils.hitl.hitl_tracker import stable_nid

                        _rec_nid = stable_nid(_rec_card_key)
                        await _notifier.send(
                            subject=f"Pueo: recurring device — {_rec_name}",
                            body=_rec_msg,
                            payload={
                                "notification_id": _rec_nid,
                                "card_type": CARD_TYPE_DEVICE_RECURRING,
                                "suppression_key": _rec_card_key,
                                "device_id": _recurring_device["device_id"],
                                "device_name": _rec_name,
                                "domain": finding.domain,
                                "config_entry_id": finding.config_entry_id,
                                "recurrence_count": _recurring_count,
                                "window_days": _cfg.DEVICE_RECURRENCE_WINDOW_DAYS,
                            },
                        )
                    else:
                        log.info(
                            "device_recurrence_card_suppressed",
                            config_entry_id=finding.config_entry_id,
                        )
                    continue

                _finding = finding
                _db_ref = _db
                _notifier_ref = _notifier
                _llm_ref = llm_client
                _ks_ref = knowledge_store
                _nax_ref = netalertx_api_client
                _ws_ref = _ws
                _rest_ref = ha_rest_client

                async def _investigate_coro(
                    _f: OfflineFinding = _finding,
                    _d: str = _db_ref,
                    _n: object = _notifier_ref,
                    _l: object = _llm_ref,
                    _k: object = _ks_ref,
                    _nax: object = _nax_ref,
                    _wsc: object = _ws_ref,
                    _rest: object = _rest_ref,
                ) -> None:
                    await _run_device_availability_investigation(
                        finding=_f,
                        db_path=_d,
                        notifier=_n,
                        llm_client=_l,
                        knowledge_store=_k,
                        netalertx_api_client=_nax,
                        ws_client=_wsc,
                        ha_rest_client=_rest,
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

                # An investigated episode is never a self-recovery.
                for _dev in finding.devices:
                    _snap_dev = result.snapshot.get(_dev["device_id"])
                    if _snap_dev is not None:
                        _snap_dev["investigated"] = True
                with sqlite3.connect(_db) as conn:
                    save_snapshot(conn, result.snapshot)

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

                # Self-recovery: no human approved the offline card and no
                # investigation (which may have reloaded the integration) ran.
                with sqlite3.connect(_db) as conn:
                    _actioned = was_card_actioned(conn, card_key)
                    _episodes_blob = load_episodes(conn)

                _updated_episodes = _episodes_blob
                _any_self_recovered = False
                for _dev in finding.devices:
                    _dev_id = _dev["device_id"]
                    _self_recovered = not _actioned and not _dev.get("investigated")
                    _any_self_recovered = _any_self_recovered or _self_recovered
                    _offline_at = _dev.get("offline_since") or oldest_offline_since
                    _updated_episodes = record_episode(
                        _updated_episodes,
                        _dev_id,
                        offline_at=_offline_at,
                        recovered_at=now,
                        self_recovered=_self_recovered,
                        investigated=not _self_recovered,
                    )
                    if _self_recovered:
                        _fact = build_recurrence_fact(
                            _dev["name"],
                            finding.domain,
                            _updated_episodes,
                            _dev_id,
                            now,
                            _cfg.DEVICE_RECURRENCE_WINDOW_DAYS,
                        )
                        if _fact:
                            await asyncio.to_thread(
                                upsert_recurrence_fact,
                                _db,
                                _dev_id,
                                _fact,
                                finding.domain,
                            )

                with sqlite3.connect(_db) as conn:
                    save_episodes(conn, _updated_episodes)
                    mark_card_resolved(conn, card_key)

                log.info(
                    "device_recovered",
                    config_entry_id=finding.config_entry_id,
                    domain=finding.domain,
                    devices=[d["name"] for d in finding.devices],
                    duration_minutes=duration_minutes,
                    self_recovered=_any_self_recovered,
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
