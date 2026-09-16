"""Seed the 'strategies' ChromaDB collection from Pueo's prompt files.

Each prompt file that contains a playbook or investigation methodology is
embedded as a runbook document. This ensures query_knowledge surfaces
relevant runbooks alongside breaking-change release notes and community cases.

Called once per RAG refresh cycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from interfaces import KnowledgeStoreClientProtocol
    from utils.ha.ha_environment import HAEnvironmentProfile

# Prompt files to seed as runbook documents.
# Each entry is (prompt_filename, title, trigger_pattern).
_SEED_PROMPTS: list[tuple[str, str, str]] = [
    (
        "diagnose_netalertx.md",
        "NetAlertX health diagnosis",
        "NetAlertX not scanning, MQTT errors, device detection failures",
    ),
    (
        "diagnose_installer.md",
        "NetAlertX installer diagnosis",
        "NetAlertX installation failure, docker pull error, port conflict",
    ),
    (
        "triage_repair_issue.md",
        "HA repair issue triage",
        "HA repair card, persistent issue, component failure flagged by HA",
    ),
    (
        "investigation.md",
        "General investigation methodology",
        "Unknown HA failure, general diagnostic investigation",
    ),
    (
        "diagnose_config.md",
        "HA configuration diagnosis",
        "Invalid HA configuration, yaml error, config check failure",
    ),
    (
        "seed_integration_error.md",
        "Integration or entity error investigation",
        "integration failing, sensor unavailable, entity error, connection error, API outage",
    ),
    (
        "seed_security_notification.md",
        "Security notification investigation",
        "failed login notification, suspicious device, unknown IP, http_login alert",
    ),
    (
        "seed_disk_space.md",
        "HA disk space investigation",
        "disk space low, HA disk usage, backups taking too much space, recorder DB large",
    ),
    (
        "seed_pueo_log.md",
        "Pueo log investigation",
        "errors in Pueo itself, stream resets, loop crashes, agent loop failures",
    ),
    (
        "seed_config_error.md",
        "HA configuration error investigation",
        "HA config invalid, yaml error, ha core check failing, configuration.yaml problem",
    ),
    (
        "seed_log_analysis.md",
        "Time-range log analysis",
        "analyze log lines from time range, what happened in the log, log analysis HH:MM, sparkline click, log window",
    ),
    (
        "seed_lovelace_config.md",
        "Lovelace unregistered entity investigation",
        "lovelace entity not in registry, unregistered entity, dashboard entity has state but no unique_id, sub-platform entity, not-loaded config entry",
    ),
    (
        "seed_repair_issue.md",
        "HA repair issue investigation",
        "HA repair panel issue, config_entry_reauth, reboot_required, restart_required, integration disabled, breaking change deprecation",
    ),
    (
        "seed_notification_analysis.md",
        "HA notification investigation",
        "HA persistent notification, http_login, ip-ban, invalid_config, integration disabled, failed login, unknown device",
    ),
    (
        "seed_update_analysis.md",
        "HA update breaking-change analysis",
        "HA update available, breaking changes, deprecated config key, OS update, CLI change, HACS incompatibility, release notes unavailable",
    ),
    (
        "seed_supervisor_cli.md",
        "HA Supervisor CLI reference",
        "run_ha_command, ha apps, ha core, ha os, ha backups, ha supervisor, ha network, restart HA, list backups, check config, CLI command",
    ),
]


def seed_strategies(
    store: "KnowledgeStoreClientProtocol",
    db_path: str = "",
) -> int:
    """Embed seed runbook documents into the 'strategies' collection.

    Uses the prompt file name as the document ID so repeated calls are
    idempotent (upsert semantics). Also writes seed entries to the
    agent_strategies SQLite table (INSERT OR IGNORE) so they appear in
    the dashboard Runbook Review tab. Returns the number of documents upserted.
    """
    import sqlite3

    prompts_dir = Path(__file__).parent.parent.parent / "prompts"
    n = 0
    for filename, title, trigger_pattern in _SEED_PROMPTS:
        path = prompts_dir / filename
        if not path.exists():
            continue
        content = path.read_text("utf-8")
        text = f"# {title}\n\nTrigger: {trigger_pattern}\n\n{content}"
        doc_id = f"seed:{filename}"
        try:
            store.upsert(
                collection="strategies",
                ids=[doc_id],
                documents=[text],
                metadatas=[
                    {
                        "source": "seed_prompt",
                        "filename": filename,
                        "title": title,
                        "trigger_pattern": trigger_pattern,
                    }
                ],
            )
            n += 1
        except Exception:  # nosec B110
            pass
        if db_path:
            try:
                with sqlite3.connect(db_path) as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO agent_strategies"
                        " (id, title, trigger_pattern, approach, runbook_state, created_at)"
                        " VALUES (?, ?, ?, ?, 'seed', datetime('now'))",
                        (doc_id, title, trigger_pattern, content),
                    )
                    conn.commit()
            except Exception:  # nosec B110
                pass
    return n


def seed_home_profile(
    store: "KnowledgeStoreClientProtocol",
    env_profile: "Optional[HAEnvironmentProfile]" = None,
    db_path: str = "",
) -> int:
    """Generate and upsert the HA instance home profile into the strategies collection.

    Produces a single document describing the installed HA instance: versions,
    integrations, HACS components, and top-level config keys.  The document is
    stored with ``runbook_type=instance_profile`` and overwritten on each call
    (upsert semantics).  Returns 1 on success, 0 on failure.
    """
    import sqlite3

    prompts_dir = Path(__file__).parent.parent.parent / "prompts"
    template_path = prompts_dir / "seed_home_profile.md"
    preamble = template_path.read_text("utf-8") if template_path.exists() else ""

    lines: list[str] = [preamble.strip(), ""]
    if env_profile is not None:
        lines.append("## Current Installation")
        lines.append("")
        if env_profile.ha_version:
            lines.append(f"- **HA Core version:** {env_profile.ha_version}")
        if env_profile.os_version:
            lines.append(f"- **HA OS version:** {env_profile.os_version}")
        if env_profile.supervisor_version:
            lines.append(f"- **Supervisor version:** {env_profile.supervisor_version}")
        lines.append("")
        if env_profile.installed_integrations:
            count = len(env_profile.installed_integrations)
            domains = ", ".join(sorted(env_profile.installed_integrations))
            lines.append(f"## Installed Integrations ({count})")
            lines.append("")
            lines.append(domains)
            lines.append("")
        if env_profile.hacs_integrations:
            count = len(env_profile.hacs_integrations)
            slugs = ", ".join(sorted(env_profile.hacs_integrations))
            lines.append(f"## HACS Custom Components ({count})")
            lines.append("")
            lines.append(slugs)
            lines.append("")
        if env_profile.config_yaml_top_keys:
            keys = ", ".join(sorted(env_profile.config_yaml_top_keys))
            lines.append("## Top-Level configuration.yaml Keys")
            lines.append("")
            lines.append(keys)
            lines.append("")
    else:
        lines.append("*No live HA profile available at last RAG refresh.*")

    text = "\n".join(lines).strip()
    doc_id = "ha_instance_profile"
    title = "HA instance home profile"
    trigger = (
        "installed integrations, ha version, hacs, custom components, home profile"
    )
    try:
        store.upsert(
            collection="strategies",
            ids=[doc_id],
            documents=[text],
            metadatas=[
                {
                    "source": "home_profile",
                    "title": title,
                    "trigger_pattern": trigger,
                    "runbook_type": "instance_profile",
                }
            ],
        )
    except Exception:  # nosec B110
        return 0
    if db_path:
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO agent_strategies"
                    " (id, title, trigger_pattern, approach, runbook_state, created_at)"
                    " VALUES (?, ?, ?, ?, 'seed', datetime('now'))",
                    (doc_id, title, trigger, text),
                )
                conn.commit()
        except Exception:  # nosec B110
            pass
    return 1
