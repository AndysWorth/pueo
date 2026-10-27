"""Deterministic (non-LLM) configuration checks for NetAlertX + HA integration (items 17, 19).

Returns list[ConfigIssue] — callers decide how to act on the findings.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

REQUIRED_APP_CONF_KEYS = [
    "SCAN_SUBNETS",
    "TIMEZONE",
    "LOADED_PLUGINS",
]
REQUIRED_PLUGINS = ["ARPSCAN"]


class ConfigIssue(BaseModel):
    field: str
    message: str
    severity: str  # LOW | MEDIUM | HIGH | CRITICAL | WARNING


def validate_app_conf(conf_text: str) -> list[ConfigIssue]:
    """Check app.conf for required keys and plugin presence."""
    issues: list[ConfigIssue] = []
    present: dict[str, str] = {}

    for line in conf_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        present[key.strip()] = value.strip()

    for key in REQUIRED_APP_CONF_KEYS:
        if key not in present or not present[key]:
            issues.append(
                ConfigIssue(
                    field=key,
                    message=f"Required key '{key}' is missing or empty in app.conf",
                    severity="HIGH",
                )
            )

    if "LOADED_PLUGINS" in present:
        plugins_val = present["LOADED_PLUGINS"]
        for plugin in REQUIRED_PLUGINS:
            if plugin not in plugins_val:
                issues.append(
                    ConfigIssue(
                        field="LOADED_PLUGINS",
                        message=f"Required plugin '{plugin}' not in LOADED_PLUGINS",
                        severity="HIGH",
                    )
                )

    return issues


# ── item 19: maintenance validators ─────────────────────────────────────────


def _normalize_mac(mac: str) -> str:
    raw = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(raw) != 12:
        return mac.upper()
    return ":".join(raw[i : i + 2].upper() for i in range(0, 12, 2))


def validate_scan_subnets(conf_text: str) -> list[ConfigIssue]:
    """Flag SCAN_SUBNETS entries that include internal Docker/Supervisor ranges.

    The Supervisor-internal subnet 172.16.0.0/12 (commonly 172.30.32.0/23)
    is added by some NetAlertX default configs and produces noise devices
    (HA containers) in the inventory.
    """
    import ipaddress

    issues: list[ConfigIssue] = []
    for line in conf_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() != "SCAN_SUBNETS":
            continue
        # Extract all CIDR-looking strings from the value
        for cidr_str in re.findall(r"(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})", value):
            try:
                net = ipaddress.ip_network(cidr_str, strict=False)
                if net.overlaps(ipaddress.ip_network("172.16.0.0/12")):
                    issues.append(
                        ConfigIssue(
                            field="SCAN_SUBNETS",
                            message=(
                                f"SCAN_SUBNETS contains {cidr_str} which overlaps the "
                                "RFC-1918 172.16.0.0/12 range used by Docker/Supervisor "
                                "internal bridges — remove it to avoid phantom devices "
                                "in the NetAlertX inventory."
                            ),
                            severity="MEDIUM",
                        )
                    )
            except ValueError:
                pass
    return issues


def validate_db_row_counts(
    metrics: dict[str, float],
    max_rows: int,
) -> list[ConfigIssue]:
    """Return WARNING ConfigIssues when Plugins_History or Events exceed max_rows."""
    issues: list[ConfigIssue] = []
    for metric_key in ("Plugins_History", "Events"):
        count = metrics.get(metric_key)
        if count is not None and count > max_rows:
            issues.append(
                ConfigIssue(
                    field=metric_key,
                    message=(
                        f"Table '{metric_key}' has {int(count):,} rows "
                        f"(threshold: {max_rows:,}). Run DBCLNP cleanup."
                    ),
                    severity="WARNING",
                )
            )
    return issues
