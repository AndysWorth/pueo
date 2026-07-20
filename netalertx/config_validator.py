"""Deterministic (non-LLM) configuration checks for NetAlertX + HA integration (item 17).

Returns list[ConfigIssue] — callers decide how to act on the findings.
"""

from __future__ import annotations

import re

import yaml
from pydantic import BaseModel

REQUIRED_APP_CONF_KEYS = [
    "MQTT_BROKER",
    "MQTT_PORT",
    "HA_URL",
    "HA_BEARER_TOKEN",
    "SCAN_SUBNETS",
    "TIMEZONE",
    "LOADED_PLUGINS",
]
REQUIRED_PLUGINS = ["MQTT", "ARPSCAN"]

# Webhook field names are camelCase since NetAlertX v26.4.6.
# Map from the old snake_case name → correct camelCase replacement.
_SNAKE_TO_CAMEL: dict[str, str] = {
    "eve_mac": "eveMac",
    "eve_ip": "eveIp",
    "eve_date_time": "eveDateTime",
    "eve_event_type": "eveEventType",
    "dev_vendor": "devVendor",
    "dev_comments": "devComments",
}


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


def validate_ha_config(config_yaml_text: str) -> list[ConfigIssue]:
    """Detect top-level 'mqtt:' key that blocks HA MQTT auto-discovery."""
    issues: list[ConfigIssue] = []
    try:
        parsed = yaml.safe_load(config_yaml_text) or {}
    except yaml.YAMLError as exc:
        return [
            ConfigIssue(
                field="configuration.yaml",
                message=f"Failed to parse YAML: {exc}",
                severity="HIGH",
            )
        ]

    if isinstance(parsed, dict) and "mqtt" in parsed:
        issues.append(
            ConfigIssue(
                field="mqtt",
                message=(
                    "Top-level 'mqtt:' key found in configuration.yaml — "
                    "this blocks MQTT auto-discovery on current HA. "
                    "Remove the key and configure MQTT via Settings → Devices & Services."
                ),
                severity="HIGH",
            )
        )
    return issues


def validate_webhook_automation(automation_yaml_text: str) -> list[ConfigIssue]:
    """Check that webhook payload field names are camelCase (required since v26.4.6)."""
    issues: list[ConfigIssue] = []
    for snake, camel in _SNAKE_TO_CAMEL.items():
        if re.search(rf"\b{re.escape(snake)}\b", automation_yaml_text):
            issues.append(
                ConfigIssue(
                    field=snake,
                    message=(
                        f"Webhook field '{snake}' must be camelCase '{camel}' "
                        "since NetAlertX v26.4.6"
                    ),
                    severity="MEDIUM",
                )
            )
    return issues
