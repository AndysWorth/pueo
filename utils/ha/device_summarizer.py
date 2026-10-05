"""Summarise integration context for update analysis prompts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from utils.ha.ha_environment import HAEnvironmentProfile


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
