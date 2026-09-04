"""Seed the 'strategies' ChromaDB collection from Pueo's prompt files.

Each prompt file that contains a playbook or investigation methodology is
embedded as a runbook document. This ensures query_knowledge surfaces
relevant runbooks alongside breaking-change release notes and community cases.

Called once per RAG refresh cycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from interfaces import KnowledgeStoreClientProtocol

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
        "triage_dashboard_entity.md",
        "HA dashboard entity not found",
        "Lovelace entity-not-found error, dashboard card showing unavailable",
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
]


def seed_strategies(store: "KnowledgeStoreClientProtocol") -> int:
    """Embed seed runbook documents into the 'strategies' collection.

    Uses the prompt file name as the document ID so repeated calls are
    idempotent (upsert semantics). Returns the number of documents upserted.
    """
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
    return n
