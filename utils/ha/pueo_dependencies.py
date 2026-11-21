"""Every Home Assistant surface Pueo itself calls, with the Pueo feature that uses it.

Update analysis compares release notes against this manifest to find HA changes
that would break Pueo. ``tests/test_pueo_dependencies.py`` fails when the code
calls a WebSocket command or REST path that is not listed here.
"""

from __future__ import annotations

import re
from typing import Any

# SSH commands Pueo uses — kept for backwards compatibility; also in PUEO_HA_DEPENDENCIES.
PUEO_SSH_COMMANDS = [
    "ha core check",
    "ha core restart",
    "ha core update",
    "ha core info --raw-json",
    "ha core logs",
    "ha apps list",
    "ha apps info <slug>",
    "ha backup new",
    "ha os update",
    "ha os info",
    "ha host info",
]

# An entry may carry "terms": the words to look for in release notes. Without it,
# dependency_terms() derives them from the entry itself.
PUEO_HA_DEPENDENCIES: dict[str, list[dict[str, Any]]] = {
    "ssh_commands": [
        {"cmd": "ha core check", "feature": "sandbox config validation before repair"},
        {"cmd": "ha core restart", "feature": "post-repair Core restart"},
        {"cmd": "ha core update", "feature": "Core update execution"},
        {"cmd": "ha core info --raw-json", "feature": "version detection"},
        {"cmd": "ha core logs", "feature": "log tailing and triage"},
        {"cmd": "ha apps list", "feature": "add-on slug resolution"},
        {"cmd": "ha apps info <slug>", "feature": "add-on metadata and update state"},
        {"cmd": "ha backup new", "feature": "mandatory pre-repair backup"},
        {"cmd": "ha os update", "feature": "OS update execution"},
        {"cmd": "ha os info", "feature": "OS version detection"},
        {"cmd": "ha host info", "feature": "host hardware profile"},
        {
            "cmd": "ha resolution suggestion apply <uuid>",
            "feature": "host-health resolution",
        },
        {
            "cmd": "ha resolution issue dismiss <uuid>",
            "feature": "host-health issue dismiss",
        },
        {"cmd": "ha apps restart <slug>", "feature": "add-on restart"},
        {"cmd": "ha resolution healthcheck", "feature": "host-health check"},
    ],
    "websocket_commands": [
        {
            "cmd": "marketplace/info",
            "feature": "Marketplace warning-accepted precheck before HACS/Marketplace update install (HA 2026.11+)",
            "note": "Returns None on unknown_command (HA <2026.11); precheck skipped when None",
        },
        {
            "cmd": "marketplace/repositories/list",
            "feature": "Marketplace integration discovery and companion provisioning (HA 2026.11+)",
            "note": "Falls back to hacs/repositories/list on older HA versions",
        },
        {
            "cmd": "marketplace/repositories/add",
            "feature": "Marketplace integration add (companion provisioning, HA 2026.11+)",
            "note": "Falls back to hacs/repositories/add on older HA versions",
        },
        {
            "cmd": "marketplace/repository/install",
            "feature": "Marketplace integration install (companion provisioning, HA 2026.11+)",
            "note": "Falls back to hacs/repository/download on older HA versions",
        },
        {
            "cmd": "hacs/repositories/list",
            "feature": "HACS integration discovery (HA <2026.11 fallback)",
        },
        {
            "cmd": "hacs/repositories/add",
            "feature": "HACS integration add (HA <2026.11 fallback)",
        },
        {
            "cmd": "hacs/repository/download",
            "feature": "HACS integration download (HA <2026.11 fallback)",
        },
        {
            "cmd": "repairs/list_issues",
            "feature": "repair issue triage and host-health investigation",
        },
        {
            "cmd": "repairs/ignore_issue",
            "feature": "repair issue ignore/dismiss",
        },
        {
            "cmd": "update/release_notes",
            "feature": "add-on release notes retrieval",
            "terms": ["update/release_notes", "update entity release notes"],
        },
        {
            "cmd": "persistent_notification/subscribe",
            "feature": "notification event streaming",
        },
        {
            "cmd": "persistent_notification/get",
            "feature": "persistent notification retrieval",
        },
        {
            "cmd": "call_service",
            "feature": "persistent notification dismiss",
            "terms": ["call_service"],
        },
        {
            "cmd": "subscribe_events",
            "feature": "event subscriber (state changes, repairs, automation triggers)",
            "terms": ["subscribe_events"],
        },
        {
            "cmd": "get_states",
            "feature": "entity state snapshot (device availability, lovelace monitor)",
            "terms": ["get_states"],
        },
        {
            "cmd": "config/device_registry/list",
            "feature": "device registry (device availability, network presence)",
        },
        {
            "cmd": "config/entity_registry/list",
            "feature": "entity registry (device availability, Spook analysis)",
        },
        {
            "cmd": "config/area_registry/list",
            "feature": "area layout (get_area_layout tool)",
        },
        {
            "cmd": "config/floor_registry/list",
            "feature": "area layout (get_area_layout tool)",
        },
        {
            "cmd": "config/label_registry/list",
            "feature": "area layout (get_area_layout tool)",
        },
        {
            "cmd": "config_entries/flow/progress",
            "feature": "list in-progress config flows (integration setup)",
            "terms": ["config_entries/flow"],
        },
        {
            "cmd": "config_entries/disable",
            "feature": "enable or disable a config entry (integration setup)",
            "terms": ["config_entries/disable"],
        },
        {
            "cmd": "manifest/get",
            "feature": "check an integration is known to HA (companion provisioning)",
            "terms": ["manifest/get"],
        },
        {
            "cmd": "manifest/list",
            "feature": "list integration manifests (integration search)",
            "terms": ["manifest/list"],
        },
        {
            "cmd": "conversation/agent/list",
            "feature": "conversation agent lookup (upgrade advisor setup)",
            "terms": ["conversation/agent"],
        },
        {
            "cmd": "system_log/list",
            "feature": "system error log (get_system_error_log tool)",
        },
        {
            "cmd": "trace/list",
            "feature": "automation traces (get_automation_traces tool)",
            "terms": ["trace/list", "automation trace"],
        },
        {
            "cmd": "trace/get",
            "feature": "automation traces (get_automation_traces tool)",
            "terms": ["trace/get", "automation trace"],
        },
        {
            "cmd": "recorder/statistics_during_period",
            "feature": "long-term statistics (get_statistics tool)",
            "terms": ["statistics_during_period"],
        },
        {
            "cmd": "recorder/list_statistic_ids",
            "feature": "statistics metadata (sensor stats repair grouping)",
            "terms": ["list_statistic_ids"],
        },
        {
            "cmd": "lovelace/dashboards/list",
            "feature": "dashboard listing (lovelace monitor)",
        },
        {
            "cmd": "lovelace/config",
            "feature": "dashboard config read (lovelace monitor)",
        },
        {
            "cmd": "validate_config",
            "feature": "automation YAML validation before authoring",
            "terms": ["validate_config"],
        },
    ],
    "rest_paths": [
        {
            "path": "POST /api/services/<domain>/<service>",
            "feature": "service call execution (call_service tool)",
        },
        {
            "path": "GET /api/services",
            "feature": "service listing (search_integrations)",
        },
        {
            "path": "GET /api/states",
            "feature": "entity state listing (update detection, HACS fallback discovery)",
        },
        {
            "path": "GET /api/states/<entity_id>",
            "feature": "entity state reads (get_entity_history, device availability)",
        },
        {
            "path": "GET /api/history/period/<start>",
            "feature": "entity history (get_entity_history tool)",
        },
        {
            "path": "GET /api/logbook/<start>",
            "feature": "logbook retrieval",
        },
        {
            "path": "POST /api/template",
            "feature": "template rendering (render_ha_template)",
        },
        {
            "path": "GET /api/config",
            "feature": "installed integration and loaded component discovery",
        },
        {
            "path": "GET /api/diagnostics/config_entry/<entry_id>",
            "feature": "integration diagnostics (get_integration_diagnostics tool)",
        },
        {
            "path": "POST /api/config/config_entries/entry/<entry_id>/reload",
            "feature": "integration reload (reload_integration tool)",
        },
        {
            "path": "DELETE /api/config/config_entries/entry/<entry_id>",
            "feature": "config entry removal (integration setup re-add)",
        },
        {
            "path": "POST /api/config/config_entries/flow",
            "feature": "config flow initiation (companion provisioning, integration setup)",
        },
        {
            "path": "POST /api/config/config_entries/flow/<flow_id>",
            "feature": "config flow step execution and abort",
        },
        {
            "path": "POST /api/config/config_entries/options/flow",
            "feature": "options flow initiation (companion provisioning)",
        },
        {
            "path": "POST /api/config/config_entries/options/flow/<flow_id>",
            "feature": "options flow step execution",
        },
        {
            "path": "POST /api/repairs/issues/fix",
            "feature": "repair flow initiation (drive_repair_flow)",
        },
        {
            "path": "POST /api/repairs/issues/fix/<flow_id>",
            "feature": "repair flow step execution and abort",
        },
        {
            "path": "DELETE /api/repairs/issues/<domain>/<issue_id>",
            "feature": "repair issue dismiss",
        },
        {
            "path": "POST /api/config/automation/config/<automation_id>",
            "feature": "automation create/update",
        },
        {
            "path": "DELETE /api/config/automation/config/<automation_id>",
            "feature": "automation delete (rollback)",
        },
        {
            "path": "POST /api/lovelace/config",
            "feature": "dashboard config read and write (lovelace card fix)",
        },
        {
            "path": "POST http://supervisor/addons/<slug>/options",
            "feature": "add-on options update (NetAlertX installer)",
        },
    ],
    "integrations": [
        {
            "domain": "marketplace",
            "feature": "Marketplace integration discovery and companion provisioning (HA 2026.11+)",
            "note": "Built-in replacement for HACS; hacs/* WS commands are gone on 2026.11+",
            "terms": ["Marketplace"],
        },
        {
            "domain": "hacs",
            "feature": "HACS integration discovery and companion provisioning (HA <2026.11)",
            "note": "Replaced by built-in Marketplace in HA 2026.11+; cannot be installed on 2026.11+",
            "terms": ["HACS"],
        },
        {
            "domain": "spook",
            "feature": "Spook issue digest and companion provisioning",
            "note": "optional HACS companion",
            "terms": ["Spook"],
        },
        {
            "domain": "ai_agent_ha",
            "feature": "AI Agent HA companion integration detection",
            "note": "optional HACS companion",
        },
    ],
    "repair_id_patterns": [
        {
            "pattern": "marketplace/restart_required_*",
            "feature": "Marketplace restart-required repair detection in log monitor (HA 2026.11+)",
        },
        {
            "pattern": "hacs/restart_required_*",
            "feature": "HACS restart-required repair detection in log monitor (HA <2026.11)",
        },
    ],
    "ssh_file_paths": [
        {
            "path": "/config/configuration.yaml",
            "feature": "config read, sandbox validation, atomic repair swap",
        },
        {
            "path": "/config/.agent_sandbox/",
            "feature": "sandbox config testing before production apply",
        },
        {
            "path": "/backup/",
            "feature": "backup file download and SHA-256 verification",
        },
    ],
}

_PLACEHOLDER_RE = re.compile(r"\s*<[^>]*>")
_REST_METHOD_RE = re.compile(r"^[A-Z]+\s+")
# First path segments that say nothing about which HA feature a command belongs to.
_GENERIC_WS_SEGMENTS = frozenset({"config"})


def dependency_key(entry: dict[str, Any]) -> str:
    """The command, path, domain or pattern that identifies a manifest entry."""
    return str(
        entry.get("cmd")
        or entry.get("path")
        or entry.get("domain")
        or entry.get("pattern", "")
    )


def dependency_terms(section: str, entry: dict[str, Any]) -> list[str]:
    """Words to look for in release notes for one manifest entry."""
    explicit = entry.get("terms")
    if explicit:
        return list(explicit)
    key = dependency_key(entry)
    if section == "websocket_commands":
        segments = key.split("/")
        if segments[0] in _GENERIC_WS_SEGMENTS and len(segments) > 1:
            return [segments[1]]
        return [segments[0]]
    if section == "ssh_commands":
        return [_PLACEHOLDER_RE.sub("", key).split(" --")[0].strip()]
    if section == "rest_paths":
        path = _REST_METHOD_RE.sub("", key)
        return [path.split("<")[0].rstrip("/")]
    if section == "repair_id_patterns":
        return [key.split("*")[0].rstrip("_")]
    return [key]


def dependency_term_index(
    manifest: dict[str, list[dict[str, Any]]],
) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Map each lowercased search term to the ``(section, entry)`` pairs that carry it.

    Entries that share a term (``hacs/repositories/list`` and the ``hacs``
    integration) form one dependency: release notes are scanned once per term.
    """
    index: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for section, entries in manifest.items():
        for entry in entries:
            for term in dependency_terms(section, entry):
                term = term.strip().lower()
                if term:
                    index.setdefault(term, []).append((section, entry))
    return index
