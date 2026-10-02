"""Service risk classification for the call_service tool.

classify_service_risk(domain, service) -> RiskLevel | None
    None  — blocked outright; agent must not call this service.
    LOW   — autonomy ≥ GUIDED auto-executes; safe, reversible operations.
    MEDIUM — autonomy AUTONOMOUS only, otherwise approval card.
    HIGH  — approval card always (except AUTONOMOUS level).
"""

from __future__ import annotations

import fnmatch

from utils.agent.autonomy import RiskLevel

# ---------------------------------------------------------------------------
# Blocked patterns — None return; never executed regardless of autonomy level
# ---------------------------------------------------------------------------
_BLOCKED: tuple[str, ...] = (
    "homeassistant.restart",
    "homeassistant.stop",
    "hassio.*",
    "backup.*",
    "recorder.purge",
    "recorder.purge_entities",
    "update.install",
    "shell_command.*",
    "python_script.*",
    "pyscript.*",
)

# ---------------------------------------------------------------------------
# LOW-risk domains — safe, reversible device/notify calls
# ---------------------------------------------------------------------------
_LOW_DOMAINS: frozenset[str] = frozenset(
    (
        "light",
        "switch",
        "fan",
        "cover",
        "media_player",
        "notify",
        "persistent_notification",
    )
)

_LOW_DOMAIN_PREFIXES: tuple[str, ...] = ("input_",)

# ---------------------------------------------------------------------------
# MEDIUM-risk patterns — orchestration/reload, not config writes
# ---------------------------------------------------------------------------
_MEDIUM_PATTERNS: tuple[str, ...] = (
    "automation.*",
    "script.*",
    "scene.*",
    "homeassistant.reload_*",
    "homeassistant.update_entity",
    "homeassistant.toggle",
    "homeassistant.turn_on",
    "homeassistant.turn_off",
    "group.*",
    "timer.*",
    "counter.*",
    "input_boolean.*",
    "input_number.*",
    "input_select.*",
    "input_text.*",
    "input_datetime.*",
    "input_button.*",
)


def classify_service_risk(domain: str, service: str) -> RiskLevel | None:
    """Return the risk level for a service call, or None if it is blocked."""
    call = f"{domain}.{service}"

    for pattern in _BLOCKED:
        if fnmatch.fnmatch(call, pattern):
            return None

    if domain in _LOW_DOMAINS:
        return RiskLevel.LOW

    for prefix in _LOW_DOMAIN_PREFIXES:
        if domain.startswith(prefix):
            return RiskLevel.LOW

    for pattern in _MEDIUM_PATTERNS:
        if fnmatch.fnmatch(call, pattern):
            return RiskLevel.MEDIUM

    return RiskLevel.HIGH
