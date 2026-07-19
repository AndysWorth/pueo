#!/usr/bin/env python3
"""Layer 2 — diagnose + SQLite state memory + pre-repair backup triggering."""

import hashlib
import sqlite3
import time
import uuid
from typing import Any, Optional
from pydantic import BaseModel, Field

from config import (
    HA_HOST,
    HA_USER,
    SSH_KEY_PATH,
    CONFIG_REMOTE_PATH,
    OLLAMA_MODEL,
    DB_PATH,
    SSH_RETRY_ATTEMPTS,
    SSH_RETRY_BASE_DELAY,
    MAX_PROMPT_TOKENS,
)
from interfaces import LLMClientProtocol, SSHClientProtocol
from utils.context import estimate_tokens, truncate_to_budget
from utils.logging import (
    get_logger,
    get_correlation_id,
    setup_logging,
    set_correlation_id,
)
from utils.ollama_client import OllamaClient
from utils.prompts import load_prompt
from utils.retry import async_retry
from utils.ssh_client import AsyncSSHClient

log = get_logger("ha_agent_advanced")

_SSH_RETRY: dict[str, Any] = dict(
    max_attempts=SSH_RETRY_ATTEMPTS,
    base_delay=SSH_RETRY_BASE_DELAY,
    exceptions=(OSError,),
)


# ==========================================
# DATA SHAPE DEFINITIONS
# ==========================================
class DiagnosticsReport(BaseModel):
    is_valid: bool = Field(
        description="True if the YAML config has no structural or deprecated flaws."
    )
    severity: str = Field(
        description="Severity classification: 'NONE', 'LOW', 'MEDIUM', 'CRITICAL'"
    )
    identified_issues: list[str] = Field(
        description="List of specific flaws, deprecated formats, or risks found."
    )
    recommended_fix_yaml: Optional[str] = Field(
        None, description="Corrected YAML block snippet if applicable."
    )


# ==========================================
# LOCAL MEMORY LAYER (SQLite)
# ==========================================
def _migrate_v1(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS state_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            config_hash TEXT,
            is_valid INTEGER,
            issues_found TEXT,
            action_taken TEXT
        )
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS backup_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            backup_slug TEXT,
            status TEXT
        )
    """
    )


def _migrate_v2(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        "ALTER TABLE state_history ADD COLUMN correlation_id TEXT DEFAULT ''"
    )


_MIGRATIONS: list[tuple[int, object]] = [
    (1, _migrate_v1),
    (2, _migrate_v2),
]


def init_local_database() -> None:
    """Run any pending schema migrations against the local SQLite database."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        row = cursor.execute("SELECT version FROM schema_version").fetchone()
        current: int = row[0] if row else 0
        for version, migration in _MIGRATIONS:  # type: ignore[assignment]
            if version > current:
                migration(cursor)  # type: ignore[operator]
                if current == 0:
                    cursor.execute("INSERT INTO schema_version VALUES (?)", (version,))
                else:
                    cursor.execute("UPDATE schema_version SET version = ?", (version,))
                current = version
        conn.commit()


def record_state_memory(
    config_hash: str, is_valid: bool, issues: list, action: str
) -> None:
    """Saves telemetry to prevent the agent from cycling into identical failures."""
    cid = get_correlation_id()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO state_history"
            " (timestamp, config_hash, is_valid, issues_found, action_taken, correlation_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                int(time.time()),
                config_hash,
                int(is_valid),
                ", ".join(issues),
                action,
                cid,
            ),
        )
        conn.commit()


def record_backup_slug(slug: str) -> None:
    """Registers an active backup point locally before executing a repair strategy."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO backup_registry (timestamp, backup_slug, status) VALUES (?, ?, ?)",
            (int(time.time()), slug, "ACTIVE"),
        )
        conn.commit()


# ==========================================
# REMOTE INFRASTRUCTURE & BACKUP TOOLS
# ==========================================
@async_retry(**_SSH_RETRY)
async def fetch_remote_config(
    ssh_client: Optional[SSHClientProtocol] = None,
) -> tuple[str, str]:
    """Reads remote config into memory and generates a lookup hash."""
    client = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
    try:
        content = await client.read_file(CONFIG_REMOTE_PATH)
        config_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        log.info("config_fetched", host=HA_HOST, hash_prefix=config_hash[:12])
        return content, config_hash
    except Exception as e:
        log.error("ssh_fetch_failed", host=HA_HOST, error=str(e))
        raise


def _extract_backup_slug(output: str) -> str:
    for line in output.split("\n"):
        if "slug:" in line.lower():
            return line.split(":")[-1].strip()
    return "unknown_slug"


@async_retry(**_SSH_RETRY)
async def execute_remote_backup(
    ssh_client: Optional[SSHClientProtocol] = None,
) -> str:
    """Triggers Home Assistant's native backup CLI engine over SSH. Returns backup identifier slug."""
    log.info("backup_trigger_start")
    client = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
    try:
        exit_code, stdout, stderr = await client.run(
            'ha backup new --name "Agent_PreFix_Snapshot"', check=True
        )
        slug = _extract_backup_slug(stdout.strip())
        log.info("backup_created", slug=slug)
        return slug
    except Exception as e:
        log.critical("backup_failed", error=str(e))
        log.critical("backup_aborted_no_writes_will_proceed")
        raise


@async_retry(**_SSH_RETRY)
async def execute_remote_preflight_check(
    ssh_client: Optional[SSHClientProtocol] = None,
) -> tuple[int, str, str]:
    """Executes Home Assistant's config verification parser via CLI."""
    client = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
    return await client.run("ha core check", check=False)


# ==========================================
# OLLAMA INFERENCE LAYER
# ==========================================
@async_retry(
    max_attempts=SSH_RETRY_ATTEMPTS,
    base_delay=SSH_RETRY_BASE_DELAY,
    exceptions=(ConnectionRefusedError,),
)
async def analyze_config_locally(
    yaml_content: str,
    llm_client: Optional[LLMClientProtocol] = None,
) -> DiagnosticsReport:
    """Runs zero-latency local analysis via macOS Ollama environment."""
    client = llm_client or OllamaClient()

    system_prompt = load_prompt("diagnose_config")
    user_prefix = "Analyze this configuration data:\n\n```yaml\n"
    user_suffix = "\n```"
    overhead = estimate_tokens(system_prompt) + estimate_tokens(
        user_prefix + user_suffix
    )
    content_budget = MAX_PROMPT_TOKENS - overhead
    original_tokens = estimate_tokens(yaml_content)
    if original_tokens > content_budget:
        yaml_content = truncate_to_budget(yaml_content, content_budget, "smart")
        log.warning(
            "content_truncated",
            original_tokens=original_tokens,
            truncated_tokens=estimate_tokens(yaml_content),
        )
    user_prompt = f"{user_prefix}{yaml_content}{user_suffix}"

    try:
        log.info("ollama_analyze_start", model=OLLAMA_MODEL)
        response = await client.chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            options={"temperature": 0.0},
            format=DiagnosticsReport.model_json_schema(),
        )
        return DiagnosticsReport.model_validate_json(response["message"]["content"])
    except Exception as e:
        log.error("ollama_inference_failed", error=str(e))
        raise


# ==========================================
# TRANSACTUAL STATE ORCHESTRATION
# ==========================================
async def main(
    ssh_client: Optional[SSHClientProtocol] = None,
    llm_client: Optional[LLMClientProtocol] = None,
) -> None:
    setup_logging()
    set_correlation_id(str(uuid.uuid4()))
    init_local_database()

    log.info("ssh_connect_start", host=HA_HOST)
    yaml_content, config_hash = await fetch_remote_config(ssh_client=ssh_client)

    report = await analyze_config_locally(yaml_content, llm_client=llm_client)
    log.info(
        "diagnostics_complete",
        is_valid=report.is_valid,
        severity=report.severity,
        issues=report.identified_issues,
    )

    if not report.is_valid:
        log.warning("issue_flagged", severity=report.severity)

        backup_slug = await execute_remote_backup(ssh_client=ssh_client)
        record_backup_slug(backup_slug)

        record_state_memory(
            config_hash,
            report.is_valid,
            report.identified_issues,
            action=f"Created backup {backup_slug}; Preparing patch.",
        )
        log.info("state_committed", backup_slug=backup_slug)
    else:
        log.info("config_valid")
        record_state_memory(
            config_hash,
            report.is_valid,
            ["None"],
            action="System Verified Clear - No Action.",
        )

        log.info("preflight_check_start")
        exit_code, stdout, stderr = await execute_remote_preflight_check(
            ssh_client=ssh_client
        )
        if exit_code == 0:
            log.info("preflight_check_passed")
        else:
            log.warning("preflight_check_discrepancy", output=stderr or stdout)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
