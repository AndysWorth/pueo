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

Disarm: the user can answer a ``device_offline`` card with "I turned these off".
Disarmed devices (one ``user_prefs`` row each, ``device_availability:disarmed:<id>``)
never alert until they are next seen online, which re-arms them.  A restart does
not re-arm them, because unknown is not online.

Post-restart sweep: findings in the first polls after the settle window are merged
into one ``OfflineFinding`` (``ha_start_ts`` set) so a restart that exposes devices
the user already switched off yields one investigation, not one per integration.

Recurrence: per-device episode history is stored in the snapshot JSON alongside
DeviceState fields (no DB migration needed).  After DEVICE_RECURRENCE_COUNT
self-recoveries within DEVICE_RECURRENCE_WINDOW_DAYS, Pueo sends one
``device_recurring`` card instead of launching another investigation.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
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
    card_key: Optional[str] = None  # suppression key of the card for this episode


@dataclass
class OfflineFinding:
    """One group of offline devices sharing a config entry."""

    config_entry_id: str
    domain: str
    devices: list[dict]  # [{"name": …, "device_id": …, "offline_since": <ts>}]
    ha_start_ts: Optional[float] = None  # set on a merged post-restart sweep


@dataclass
class AvailabilityResult:
    """Output of evaluate_device_availability()."""

    new_offline: list[OfflineFinding]
    recovered: list[OfflineFinding]  # devices whose alert is now resolved
    snapshot: dict  # updated snapshot blob (device_id → DeviceState as dict)
    skipped: Optional[str] = None  # "incomplete_states" | "settling" when skipped
    state_coverage: float = 1.0  # fraction of registry entities with a state row
    rearmed: list[str] = field(default_factory=list)  # disarmed devices seen online


# Entity ids listed per device in the investigation context.
_MAX_ENTITY_IDS_PER_DEVICE = 5

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
    disarmed: frozenset[str] = frozenset(),
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
    disarmed:
        Device ids the user said they switched off.  They never alert; seeing
        one online re-arms it (listed in ``rearmed``) without a recovery.

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
    rearmed: list[str] = []

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
            card_key=prior_entry.get("card_key"),
        )
        current[device_id] = ds

        status = _device_status([state_by_id.get(e["entity_id"]) for e in entities])

        if status == "online":
            if device_id in disarmed:
                # Switched back on by the user: re-arm, not a recovery.
                rearmed.append(device_id)
            elif ds.alerted:
                recovered_by_entry.setdefault(primary_entry_id, []).append(
                    {
                        "name": name,
                        "device_id": device_id,
                        "domain": domain,
                        "offline_since": ds.offline_since,
                        "investigated": ds.investigated,
                        "card_key": ds.card_key,
                    }
                )
            ds.last_online_ts = now
            ds.alerted = False
            ds.offline_since = None
            ds.investigated = False
            ds.card_key = None
        elif status == "offline":
            if device_id in disarmed:
                # Off on purpose — close any open episode and stay quiet.
                ds.alerted = False
                ds.offline_since = None
                ds.investigated = False
                ds.card_key = None
                continue
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
                        "config_entry_id": primary_entry_id,
                        "offline_since": ds.offline_since,
                        "entity_ids": [e["entity_id"] for e in entities][
                            :_MAX_ENTITY_IDS_PER_DEVICE
                        ],
                    }
                )
                ds.alerted = True
                ds.card_key = f"device_offline:{primary_entry_id}"
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
            "card_key": ds.card_key,
        }
        for device_id, ds in current.items()
    }

    return AvailabilityResult(
        new_offline=new_offline_findings,
        recovered=recovered_findings,
        snapshot=snapshot,
        rearmed=rearmed,
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
# Post-restart sweep (pure — no I/O)
# ------------------------------------------------------------------

POST_RESTART_DOMAIN = "post_restart"
# Findings this soon after the settle window ends count as restart-exposed.
_POST_RESTART_SWEEP_WINDOW_S = 600


def in_post_restart_sweep(
    now: float,
    ha_start_ts: Optional[float],
    settle_s: float,
    window_s: float = _POST_RESTART_SWEEP_WINDOW_S,
) -> bool:
    """True during the first polls after the post-restart settle window."""
    if ha_start_ts is None:
        return False
    settled_at = ha_start_ts + settle_s
    return settled_at <= now < settled_at + window_s


def merge_post_restart_findings(
    findings: list[OfflineFinding], ha_start_ts: float
) -> Optional[OfflineFinding]:
    """Merge per-entry findings into one post-restart sweep finding."""
    devices = [d for f in findings for d in f.devices]
    if not devices:
        return None
    return OfflineFinding(
        config_entry_id=f"{POST_RESTART_DOMAIN}:{int(ha_start_ts)}",
        domain=POST_RESTART_DOMAIN,
        devices=devices,
        ha_start_ts=ha_start_ts,
    )


# ------------------------------------------------------------------
# Disarm ("I turned these off") — one user_prefs row per device
# ------------------------------------------------------------------

_DISARM_PREFIX = "device_availability:disarmed:"


def load_disarmed(conn: sqlite3.Connection) -> frozenset[str]:
    """Return the ids of devices the user said they switched off."""
    try:
        rows = conn.execute(
            "SELECT key FROM user_prefs WHERE key LIKE ?", (_DISARM_PREFIX + "%",)
        ).fetchall()
        return frozenset(r[0][len(_DISARM_PREFIX) :] for r in rows)
    except Exception as e:  # nosec B110
        log.warning("device_disarmed_load_failed", error=str(e))
    return frozenset()


def rearm_devices(conn: sqlite3.Connection, device_ids: list[str]) -> None:
    """Delete the disarm rows of devices seen online again."""
    if not device_ids:
        return
    try:
        conn.executemany(
            "DELETE FROM user_prefs WHERE key = ?",
            [(_DISARM_PREFIX + d,) for d in device_ids],
        )
        conn.commit()
        log.info("device_rearmed", device_ids=device_ids)
    except Exception as e:  # nosec B110
        log.warning("device_rearm_failed", error=str(e))


def _intentionally_off_fact_key(domain: str) -> str:
    return (
        "fact_"
        + hashlib.sha256(f"{domain}:intentionally_off".encode()).hexdigest()[:12]
    )


_OFF_FACT_PREFIX = (
    "User-confirmed: these {domain} devices are sometimes switched off on purpose: "
)


def disarm_devices(db_path: str, devices: list[dict], nid: str) -> list[str]:
    """Disarm *devices* and record one user-verified instance fact per domain.

    Writes the disarm rows (written row by row, so a concurrent poll can't lose
    them) and merges the device names into the domain's "switched off on
    purpose" fact.  Returns the disarmed device ids.
    """
    now = time.time()
    ids = [d["device_id"] for d in devices if d.get("device_id")]
    names_by_domain: dict[str, list[str]] = {}
    for d in devices:
        dom = d.get("domain") or ""
        if dom and d.get("name"):
            names_by_domain.setdefault(dom, []).append(d["name"])
    try:
        with sqlite3.connect(db_path) as conn:
            conn.executemany(
                "INSERT INTO user_prefs (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [
                    (
                        _DISARM_PREFIX + d["device_id"],
                        json.dumps({"ts": now, "nid": nid}),
                    )
                    for d in devices
                    if d.get("device_id")
                ],
            )
            for dom, names in names_by_domain.items():
                key = _intentionally_off_fact_key(dom)
                signature = f"device:offline:{dom}"
                prefix = _OFF_FACT_PREFIX.format(domain=dom)
                row = conn.execute(
                    "SELECT content FROM agent_memory WHERE key = ? AND signature = ?",
                    (key, signature),
                ).fetchone()
                known: list[str] = []
                if row and str(row[0]).startswith(prefix):
                    known = [
                        n for n in row[0][len(prefix) :].rstrip(".").split(", ") if n
                    ]
                merged = known + [n for n in names if n not in known]
                conn.execute(
                    "DELETE FROM agent_memory WHERE key = ? AND signature = ?",
                    (key, signature),
                )
                conn.execute(
                    "INSERT INTO agent_memory (key, content, source, ts, signature)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        key,
                        prefix + ", ".join(merged) + ".",
                        f"user:{nid}",
                        now,
                        signature,
                    ),
                )
            conn.commit()
        log.info("device_disarmed", device_ids=ids, nid=nid)
    except Exception as e:  # nosec B110
        log.warning("device_disarm_failed", nid=nid, error=str(e))
        return []
    return ids


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
    # Per-episode data only: sorting offline and recovered lists separately mispairs
    # them.  The fact counts episodes; it states no cause (#993).
    complete = [e for e in recent if e.get("offline_at") and e.get("recovered_at")]
    head = f"{device_name} ({domain}) was offline {count}× in {window_days} days"
    if not complete:
        return f"{head}, coming back on its own each time."
    durations = sorted(e["recovered_at"] - e["offline_at"] for e in complete)
    typical = int(statistics.median(durations) / 60)
    return (
        f"{head} (typical duration {typical} min, "
        f"{_usual_start([e['offline_at'] for e in complete])})."
    )


def _usual_start(offline_times: list[float]) -> str:
    """Describe when episodes start: 'usually starting HH:MM' or 'at varying times'.

    The median local start time counts as usual when at least half the starts fall
    within an hour of it (wrapping around midnight).
    """
    import datetime

    minutes = sorted(
        datetime.datetime.fromtimestamp(t).hour * 60
        + datetime.datetime.fromtimestamp(t).minute
        for t in offline_times
    )
    mid = minutes[(len(minutes) - 1) // 2]
    if len(minutes) == 1:
        return f"starting {mid // 60:02d}:{mid % 60:02d}"
    near = sum(1 for m in minutes if min(abs(m - mid), 1440 - abs(m - mid)) <= 60)
    if near * 2 >= len(minutes):
        return f"usually starting {mid // 60:02d}:{mid % 60:02d}"
    return "starting at varying times"


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
    recurrence_count: "int | None" = None,
    recurrence_window_days: "int | None" = None,
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
        recurrence_count=recurrence_count,
        recurrence_window_days=recurrence_window_days,
    )


# ------------------------------------------------------------------
# Poll loop
# ------------------------------------------------------------------


def _post_restart_sweep(
    findings: list[OfflineFinding], db_path: str, ha_start_ts: float
) -> list[OfflineFinding]:
    """Drop suppressed/backed-off entries, then merge the rest into one sweep."""
    from utils.hitl.hitl_tracker import should_send_card

    eligible: list[OfflineFinding] = []
    with sqlite3.connect(db_path) as conn:
        for f in findings:
            if should_send_card(
                conn, f"device_offline:{f.config_entry_id}"
            ) and should_send_card(conn, f"device_offline_backoff:{f.config_entry_id}"):
                eligible.append(f)
            else:
                log.info(
                    "device_offline_sweep_entry_suppressed",
                    config_entry_id=f.config_entry_id,
                )
    sweep = merge_post_restart_findings(eligible, ha_start_ts)
    if sweep is None:
        return []
    log.info(
        "device_offline_post_restart_sweep",
        ha_start_ts=ha_start_ts,
        entries=[f.config_entry_id for f in eligible],
        devices=len(sweep.devices),
    )
    return [sweep]


async def check_device_predictions(
    db_path: str,
    now: float,
    recovered: dict[str, bool],
    still_offline: set[str],
    knowledge_store: object = None,
    llm_client: object = None,
) -> int:
    """Judge pending session predictions against what the poll just saw (ADR 050).

    *recovered* maps the card_key of each episode that just recovered to whether
    its card was actioned; *still_offline* lists card_keys of episodes still
    alerted.  Sessions are matched by ``card_id`` (the card's stable nid).
    Returns the number of sessions verified.
    """
    import asyncio

    from utils.hitl.hitl_tracker import stable_nid
    from utils.knowledge.session_verification import (
        judge_prediction,
        load_pending_predictions,
        resolve_prediction,
    )

    if not recovered and not still_offline:
        return 0
    by_nid: dict[str, tuple[str, bool]] = {
        stable_nid(k): (k, False) for k in still_offline
    }
    by_nid.update({stable_nid(k): (k, True) for k in recovered})
    resolved = 0
    for pending in await asyncio.to_thread(load_pending_predictions, db_path):
        hit = by_nid.get(pending.card_id)
        if hit is None or pending.prediction is None:
            continue
        key, is_recovered = hit
        verdict = judge_prediction(
            pending.prediction,
            pending.created_ts,
            now,
            recovered_at=now if is_recovered else None,
            actioned=pending.actioned or recovered.get(key, False),
            reloaded=pending.reloaded,
        )
        if verdict is None:
            continue
        await resolve_prediction(db_path, pending, verdict, knowledge_store, llm_client)
        resolved += 1
    return resolved


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
                disarmed = load_disarmed(conn)

            grace_s = _cfg.DEVICE_AVAILABILITY_GRACE_MINUTES * 60
            settle_s = _cfg.DEVICE_AVAILABILITY_RESTART_SETTLE_MINUTES * 60
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
                settle_s=settle_s,
                disarmed=disarmed,
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
                    rearm_devices(conn, result.rearmed)

            findings = result.new_offline
            if (
                findings
                and ha_start_ts is not None
                and in_post_restart_sweep(now, ha_start_ts, settle_s)
            ):
                findings = _post_restart_sweep(findings, _db, ha_start_ts)

            # Process newly offline devices.
            for finding in findings:
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
                # A post-restart sweep is investigated as one group.
                for _dev in finding.devices if finding.ha_start_ts is None else []:
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

                        # Also submit one investigation to identify the root cause.
                        _rf = finding
                        _rd = _db
                        _rn = _notifier
                        _rl = llm_client
                        _rk = knowledge_store
                        _rnax = netalertx_api_client
                        _rws = _ws
                        _rrest = ha_rest_client
                        _rcnt = _recurring_count
                        _rwin = _cfg.DEVICE_RECURRENCE_WINDOW_DAYS

                        async def _recurrence_coro(
                            _f: OfflineFinding = _rf,
                            _d: str = _rd,
                            _n: object = _rn,
                            _l: object = _rl,
                            _k: object = _rk,
                            _nax: object = _rnax,
                            _wsc: object = _rws,
                            _rest: object = _rrest,
                            _cnt: int = _rcnt,
                            _win: int = _rwin,
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
                                recurrence_count=_cnt,
                                recurrence_window_days=_win,
                            )

                        _wq_r = get_work_queue_or_none()
                        if _wq_r is not None:
                            await _wq_r.submit(
                                WorkItem(
                                    priority=PRIORITY_NORMAL,
                                    activity_type="device_availability",
                                    description=(
                                        f"Device recurrence investigation:"
                                        f" {finding.domain} ({_recurring_count}×)"
                                    ),
                                    dedup_key=f"device_offline:{finding.config_entry_id}",
                                    suppress_while_running=frozenset(),
                                    coro_factory=_recurrence_coro,
                                )
                            )
                        else:
                            await _recurrence_coro()

                        # Mark as investigated so this offline period is not counted
                        # as a self-recovery when the device recovers.
                        _rec_snap = result.snapshot.get(_recurring_device["device_id"])
                        if _rec_snap is not None:
                            _rec_snap["investigated"] = True
                            _rec_snap["card_key"] = _rec_card_key
                        with sqlite3.connect(_db) as conn:
                            save_snapshot(conn, result.snapshot)
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
                        _snap_dev["card_key"] = card_key
                with sqlite3.connect(_db) as conn:
                    save_snapshot(conn, result.snapshot)

            # Process recoveries.
            _recovered_keys: dict[str, bool] = {}
            for finding in result.recovered:
                # A swept device's card is the sweep card, not its entry's.
                card_keys = sorted(
                    {
                        d.get("card_key") or f"device_offline:{finding.config_entry_id}"
                        for d in finding.devices
                    }
                )

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
                    _actioned = any(was_card_actioned(conn, k) for k in card_keys)
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
                    for _k in card_keys:
                        mark_card_resolved(conn, _k)
                for _k in card_keys:
                    _recovered_keys[_k] = _recovered_keys.get(_k, False) or _actioned

                log.info(
                    "device_recovered",
                    config_entry_id=finding.config_entry_id,
                    domain=finding.domain,
                    devices=[d["name"] for d in finding.devices],
                    duration_minutes=duration_minutes,
                    self_recovered=_any_self_recovered,
                )

            # Prediction checks — states inside the settle window prove nothing.
            if result.skipped is None:
                _still_offline = {
                    str(s["card_key"])
                    for s in result.snapshot.values()
                    if s.get("alerted") and s.get("card_key")
                }
                # A card recovers only when none of its devices is still offline.
                await check_device_predictions(
                    _db,
                    now,
                    {
                        k: v
                        for k, v in _recovered_keys.items()
                        if k not in _still_offline
                    },
                    _still_offline,
                    knowledge_store,
                    llm_client,
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
