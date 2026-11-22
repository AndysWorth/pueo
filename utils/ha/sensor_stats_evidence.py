"""Build a deterministic evidence block for sensor statistics repair groups.

Called by ``_run_sensor_stats_investigation`` to pre-inject structured facts
so the model does not need extra tool calls to discover them.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from interfaces import HARestClientProtocol, HAWebSocketClientProtocol
    from utils.ha.ha_rest_client import HARepairIssue

# Statistics-domain translation_keys that this module handles.
STATISTICS_TRANSLATION_KEYS: frozenset[str] = frozenset(
    {
        "state_class_removed",
        "units_changed",
        "mean_type_changed",
        "entity_no_longer_recorded",
        "entity_not_recorded",
        "unsupported_state_class",
    }
)


def sensor_stats_group_fp(member_fps: list[str]) -> str:
    """Stable sha256 over sorted member fingerprints (first 16 chars)."""
    raw = json.dumps(sorted(member_fps)).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


async def build_sensor_stats_evidence(
    group_key: str,
    issues: "list[HARepairIssue]",
    ws_client: "HAWebSocketClientProtocol",
    rest_client: Optional["HARestClientProtocol"] = None,
) -> str:
    """Build a human-readable evidence block for a sensor stats issue group.

    Fetches current entity attributes, statistics metadata, device registry
    info, and the last statistics row date.  Returns a plain-text string
    suitable for pre-injection as ``initial_context``.
    """
    statistic_ids = [
        issue.data.get("translation_placeholders", {}).get("statistic_id", "")
        for issue in issues
    ]
    statistic_ids = [s for s in statistic_ids if s]

    # Fetch entity registry and device registry in parallel (best-effort).
    try:
        entity_reg = await ws_client.get_entity_registry()
    except Exception:
        entity_reg = []

    try:
        device_reg = await ws_client.get_device_registry()
    except Exception:
        device_reg = []

    # Build lookup maps.
    entity_by_id = {e.get("entity_id", ""): e for e in entity_reg}
    device_by_id = {d.get("id", ""): d for d in device_reg}

    # Fetch statistics metadata for the statistic_ids.
    stats_meta: dict[str, dict] = {}
    if statistic_ids:
        try:
            stats_meta = await ws_client.get_statistics_metadata(statistic_ids)
        except Exception:
            stats_meta = {}

    # Fetch last stats row: one day bucket, last 90 days.
    last_stats_dates: dict[str, str] = {}
    if statistic_ids:
        try:
            now = datetime.now(timezone.utc)
            start = now.replace(
                year=now.year - 1 if now.month == 1 else now.year,
                month=12 if now.month == 1 else now.month - 1,
                day=1,
            )
            # 90 days back is simpler
            import datetime as _dt

            start_dt = datetime.now(timezone.utc) - _dt.timedelta(days=90)
            start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            stat_data = await ws_client.get_statistics(
                statistic_ids, start_iso, None, "day", ["sum", "mean"]
            )
            for sid, buckets in stat_data.items():
                if buckets:
                    last_bucket = buckets[-1]
                    ts_ms = last_bucket.get("start", 0)
                    if ts_ms:
                        last_stats_dates[sid] = datetime.fromtimestamp(
                            ts_ms / 1000, tz=timezone.utc
                        ).strftime("%Y-%m-%d")
        except Exception:  # nosec B110
            pass

    # Fetch current states from REST.
    states: dict[str, dict] = {}
    if rest_client and statistic_ids:
        for sid in statistic_ids:
            try:
                state = await rest_client.get_state(sid)
                states[sid] = state
            except Exception:  # nosec B110
                pass

    lines = [f"Sensor statistics repair group: {group_key}"]
    lines.append(f"Issue count: {len(issues)}")
    lines.append("")

    for issue in issues:
        placeholders = issue.data.get("translation_placeholders", {})
        sid = placeholders.get("statistic_id", issue.issue_id)
        tkey = issue.translation_key or "(unknown)"

        lines.append(f"Issue: {tkey} — {issue.issue_key}")
        lines.append(f"  statistic_id: {sid}")

        # Current entity state.
        state = states.get(sid, {})
        attrs = state.get("attributes", {})
        cur_unit = attrs.get("unit_of_measurement", "(none)")
        cur_state_class = attrs.get("state_class", "(none)")
        cur_device_class = attrs.get("device_class", "(none)")
        lines.append(f"  current unit_of_measurement: {cur_unit}")
        lines.append(f"  current state_class: {cur_state_class}")
        lines.append(f"  current device_class: {cur_device_class}")

        # Statistics metadata.
        meta = stats_meta.get(sid, {})
        if meta:
            meta_unit = meta.get("unit_of_measurement", "(none)")
            has_sum = meta.get("has_sum", False)
            has_mean = meta.get("has_mean", False)
            lines.append(f"  statistics metadata unit: {meta_unit}")
            lines.append(f"  statistics has_sum: {has_sum}, has_mean: {has_mean}")
        else:
            lines.append("  statistics metadata: not found")

        # Last stats row.
        last_date = last_stats_dates.get(sid)
        if last_date:
            lines.append(f"  last statistics row: {last_date}")
        else:
            lines.append("  last statistics row: none in last 90 days")

        # Entity registry info.
        ent = entity_by_id.get(sid, {})
        if ent:
            platform = ent.get("platform", "(unknown)")
            created_at = ent.get("created_at", "(unknown)")
            lines.append(f"  platform: {platform}")
            lines.append(f"  entity created_at: {created_at}")

            # Device info.
            device_id = ent.get("device_id")
            if device_id:
                dev = device_by_id.get(device_id, {})
                if dev:
                    dev_name = dev.get("name_by_user") or dev.get("name", "(unknown)")
                    dev_model = dev.get("model", "(unknown)")
                    dev_sw = dev.get("sw_version", "(unknown)")
                    dev_created = dev.get("created_at", "(unknown)")
                    lines.append(f"  device: {dev_name} (model: {dev_model})")
                    lines.append(f"  device sw_version: {dev_sw}")
                    lines.append(f"  device created_at: {dev_created}")

        # Issue-level placeholders.
        for k, v in placeholders.items():
            if k != "statistic_id":
                lines.append(f"  placeholder {k}: {v}")

        # Issue created date.
        issue_created = issue.data.get("created", "")
        if issue_created:
            lines.append(f"  issue created: {issue_created}")

        lines.append("")

    return "\n".join(lines)


def device_id_for_issue(
    issue: "HARepairIssue",
    entity_registry: list[dict],
) -> Optional[str]:
    """Return the device_id for the entity linked to this issue, or None."""
    placeholders = issue.data.get("translation_placeholders", {})
    sid = placeholders.get("statistic_id", "")
    if not sid:
        return None
    for ent in entity_registry:
        if ent.get("entity_id") == sid:
            return ent.get("device_id") or None
    return None
