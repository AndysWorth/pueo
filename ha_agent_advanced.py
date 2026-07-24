#!/usr/bin/env python3
"""Layer 2 — diagnose + SQLite state memory + pre-repair backup triggering."""

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

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
    HA_DISK_CRITICAL_GB,
    BACKUP_OFFLOAD_ENABLED,
    BACKUP_LOCAL_DIR,
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
from ha_agent_core import DiagnosticsReport
from utils.resource import DiskCriticalError, check_disk_not_critical
from utils.retry import async_retry, SSH_RETRY_KWARGS
from utils.ssh_client import AsyncSSHClient

log = get_logger("ha_agent_advanced")


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


def _migrate_v3(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS netalertx_install_state (
            id INTEGER PRIMARY KEY,
            state TEXT,
            correlation_id TEXT,
            timestamp TEXT,
            details_json TEXT
        )
    """
    )


def _migrate_v4(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS netalertx_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE,
            value TEXT NOT NULL
        )
    """
    )


def _migrate_v5(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        "ALTER TABLE backup_registry ADD COLUMN size_bytes INTEGER NOT NULL DEFAULT 0"
    )
    cursor.execute(
        "ALTER TABLE backup_registry ADD COLUMN location TEXT NOT NULL DEFAULT 'ha'"
    )
    cursor.execute("ALTER TABLE backup_registry ADD COLUMN offloaded_at REAL")
    cursor.execute("ALTER TABLE backup_registry ADD COLUMN deleted_from_ha_at REAL")


_MIGRATIONS: list[tuple[int, object]] = [
    (1, _migrate_v1),
    (2, _migrate_v2),
    (3, _migrate_v3),
    (4, _migrate_v4),
    (5, _migrate_v5),
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
            "INSERT INTO backup_registry (timestamp, backup_slug, status, size_bytes, location)"
            " VALUES (?, ?, 'ACTIVE', 0, 'ha')",
            (int(time.time()), slug),
        )
        conn.commit()


def _parse_backup_list(output: str) -> list[dict]:
    """Parse JSON from `ha backups list --raw-json`. Returns list of {slug, size_bytes}."""
    try:
        data = json.loads(output)
        backups = data.get("data", {}).get("backups", [])
        return [
            {"slug": b["slug"], "size_bytes": b.get("size_bytes", 0)}
            for b in backups
            if "slug" in b
        ]
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return []


async def list_ha_backups(
    ssh_client: Optional[SSHClientProtocol] = None,
) -> list[dict]:
    """Run `ha backups list --raw-json` via SSH. Raises on SSH error."""
    client = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
    _, stdout, _ = await client.run("ha backups list --raw-json", check=False)
    return _parse_backup_list(stdout)


async def reconcile_backup_inventory(
    ssh_client: Optional[SSHClientProtocol] = None,
) -> None:
    """Compare HA backup list against SQLite. Insert HA-only slugs; warn on orphans."""
    try:
        ha_backups = await list_ha_backups(ssh_client=ssh_client)
    except Exception as e:
        log.warning("backup_reconcile_skipped", error=str(e))
        return

    ha_slugs = {b["slug"]: b["size_bytes"] for b in ha_backups}

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        db_slugs = {
            row[0]
            for row in cursor.execute(
                "SELECT backup_slug FROM backup_registry"
            ).fetchall()
        }

        for slug, size_bytes in ha_slugs.items():
            if slug not in db_slugs:
                log.info("backup_inventory_add", slug=slug, size_bytes=size_bytes)
                cursor.execute(
                    "INSERT INTO backup_registry"
                    " (timestamp, backup_slug, status, size_bytes, location)"
                    " VALUES (?, ?, 'ACTIVE', ?, 'ha')",
                    (int(time.time()), slug, size_bytes),
                )

        for slug in db_slugs:
            if slug not in ha_slugs:
                log.warning("backup_inventory_orphaned", slug=slug)

        conn.commit()


# ==========================================
# REMOTE INFRASTRUCTURE & BACKUP TOOLS
# ==========================================
@async_retry(**SSH_RETRY_KWARGS)
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


@async_retry(**SSH_RETRY_KWARGS)
async def execute_remote_backup(
    ssh_client: Optional[SSHClientProtocol] = None,
) -> str:
    """Triggers Home Assistant's native backup CLI engine over SSH. Returns backup identifier slug."""
    check_disk_not_critical(HA_DISK_CRITICAL_GB)
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


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


async def offload_backup_to_local(
    slug: str,
    ssh_client: Optional[SSHClientProtocol] = None,
) -> None:
    """SFTP-pull /backup/<slug>.tar to BACKUP_LOCAL_DIR, SHA-256 verify, update location."""
    if not BACKUP_OFFLOAD_ENABLED:
        return
    remote_path = f"/backup/{slug}.tar"
    local_dir = Path(BACKUP_LOCAL_DIR)
    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / f"{slug}.tar"
    client = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
    try:
        await client.download_file(remote_path, str(local_path))
        local_hash = _sha256_file(local_path)
        _, stdout, _ = await client.run(f"sha256sum {remote_path}", check=False)
        remote_hash = stdout.strip().split()[0] if stdout.strip() else None
        if remote_hash and remote_hash != local_hash:
            local_path.unlink(missing_ok=True)
            log.warning(
                "backup_offload_checksum_mismatch",
                slug=slug,
                local_hash=local_hash,
                remote_hash=remote_hash,
            )
            return
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE backup_registry SET location = 'both', offloaded_at = ?"
                " WHERE backup_slug = ?",
                (time.time(), slug),
            )
            conn.commit()
        log.info("backup_offloaded", slug=slug, local_path=str(local_path))
    except Exception as e:
        log.warning("backup_offload_failed", slug=slug, error=str(e))


@async_retry(**SSH_RETRY_KWARGS)
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
    await reconcile_backup_inventory(ssh_client=ssh_client)

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
        await offload_backup_to_local(backup_slug, ssh_client=ssh_client)

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
