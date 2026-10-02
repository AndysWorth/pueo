"""Summarise device and integration context for update analysis prompts.

Reduces token cost by collapsing repeated device entries and providing a
compact paragraph describing the HA environment.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from utils.ha.ha_environment import HAEnvironmentProfile


def compress_device_list(devices: list[dict]) -> str:
    """Group devices by (manufacturer, model) and return collapsed one-liners.

    Each line: "N× Manufacturer Model", sorted by count descending.
    Returns empty string for an empty list.
    """
    if not devices:
        return ""
    counts: Counter = Counter()
    for d in devices:
        mfr = (d.get("manufacturer") or "").strip()
        model = (d.get("model") or "").strip()
        label = " ".join(filter(None, [mfr, model])) or "Unknown device"
        counts[label] += 1
    lines = [f"{count}× {label}" for label, count in counts.most_common()]
    return "\n".join(lines)


def device_context_summary(profile: Optional["HAEnvironmentProfile"]) -> str:
    """Return a concise summary of the HA environment for update analysis context.

    Uses integration and config-entry data already in the profile.
    Returns empty string when profile is None.
    """
    if profile is None:
        return ""
    parts: list[str] = []
    if profile.ha_version:
        parts.append(f"HA version: {profile.ha_version}")
    if profile.installed_integrations:
        n = len(profile.installed_integrations)
        sample = ", ".join(sorted(profile.installed_integrations)[:10])
        suffix = f" (and {n - 10} more)" if n > 10 else ""
        parts.append(f"Integrations ({n}): {sample}{suffix}")
    if profile.hacs_integrations:
        n = len(profile.hacs_integrations)
        sample = ", ".join(sorted(profile.hacs_integrations)[:5])
        suffix = f" (and {n - 5} more)" if n > 5 else ""
        parts.append(f"HACS ({n}): {sample}{suffix}")
    if profile.config_yaml_top_keys:
        parts.append(
            f"Config top-level keys: {', '.join(profile.config_yaml_top_keys)}"
        )
    if not parts:
        return ""
    return "Environment summary:\n" + "\n".join(parts)
