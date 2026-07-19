#!/usr/bin/env python3
"""Layer 3 — full repair pipeline: content validation, HITL gate, backup, sandbox test, atomic swap."""

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
    NOTIFIER,
    NOTIFY_URL,
    NOTIFY_WATCH_DIR,
    HITL_ALWAYS,
    AUTONOMY_LEVEL,
    HITL_TIMEOUT_MINUTES,
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
from utils.autonomy import AutonomyGate, RiskLevel
from utils.notify import NotifierProtocol, get_notifier
from utils.yaml_validator import validate_proposed_fix

log = get_logger("ha_agent_sandbox_engine")

_SSH_RETRY: dict[str, Any] = dict(
    max_attempts=SSH_RETRY_ATTEMPTS,
    base_delay=SSH_RETRY_BASE_DELAY,
    exceptions=(OSError,),
)

# Sandbox paths derived from CONFIG_REMOTE_PATH
_config_dir = CONFIG_REMOTE_PATH.rsplit("/", 1)[0]
_config_filename = CONFIG_REMOTE_PATH.rsplit("/", 1)[1]
SANDBOX_REMOTE_DIR = f"{_config_dir}/.agent_sandbox"
SANDBOX_REMOTE_FILE = f"{_config_dir}/.agent_sandbox/{_config_filename}"


# ==========================================
# DATA SHAPE DEFINITIONS
# ==========================================
class DiagnosticsReport(BaseModel):
    is_valid: bool = Field(
        description="True if the YAML config has no structural flaws."
    )
    severity: str = Field(
        description="Severity classification: 'NONE', 'LOW', 'MEDIUM', 'CRITICAL'"
    )
    identified_issues: list[str] = Field(
        description="List of specific flaws or risks found."
    )
    recommended_fix_yaml: Optional[str] = Field(
        None,
        description="The complete, fully corrected replacement string for configuration.yaml.",
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
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO backup_registry (timestamp, backup_slug, status) VALUES (?, ?, 'ACTIVE')",
            (int(time.time()), slug),
        )
        conn.commit()


# ==========================================
# REMOTE INFRASTRUCTURE & BACKUP TOOLS
# ==========================================
@async_retry(**_SSH_RETRY)
async def fetch_remote_config(
    ssh_client: Optional[SSHClientProtocol] = None,
) -> tuple[str, str]:
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
        raise


@async_retry(**_SSH_RETRY)
async def execute_remote_preflight_check(
    ssh_client: Optional[SSHClientProtocol] = None,
) -> tuple[int, str, str]:
    client = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
    return await client.run("ha core check", check=False)


# ==========================================
# SANDBOX EXECUTION & ATOMIC SWAP ENGINE
# ==========================================
async def deploy_and_test_in_sandbox(
    fixed_yaml: str,
    ssh_client: Optional[SSHClientProtocol] = None,
) -> bool:
    """Deploys code change to an isolated remote sandbox file and tests it via the HA compiler."""
    client = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
    log.info("sandbox_deploy_start")
    try:
        await client.run(f"mkdir -p {SANDBOX_REMOTE_DIR}", check=True)
        await client.write_file(SANDBOX_REMOTE_FILE, fixed_yaml)

        log.info("sandbox_preflight_start")
        await client.run(
            f"mv {CONFIG_REMOTE_PATH} {CONFIG_REMOTE_PATH}.bak", check=True
        )
        # Restore the original config unconditionally — whether the check
        # passes, fails, or the SSH connection drops mid-call.
        try:
            await client.run(
                f"cp {SANDBOX_REMOTE_FILE} {CONFIG_REMOTE_PATH}", check=True
            )
            exit_code, stdout, stderr = await execute_remote_preflight_check(
                ssh_client=client
            )
        finally:
            await client.run(
                f"mv {CONFIG_REMOTE_PATH}.bak {CONFIG_REMOTE_PATH}", check=True
            )

        if exit_code == 0:
            log.info("sandbox_test_passed")
            return True
        else:
            log.error("sandbox_test_failed", output=stderr or stdout)
            return False

    except Exception as e:
        log.error("sandbox_engine_failed", error=str(e))
        return False


async def commit_atomic_swap(
    fixed_yaml: str,
    ssh_client: Optional[SSHClientProtocol] = None,
) -> None:
    """Executes a permanent, clean, atomic swap of the validated sandbox code into production."""
    client = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
    log.info("atomic_swap_start")
    await client.write_file(CONFIG_REMOTE_PATH, fixed_yaml)
    await client.run("ha core reload", check=False)
    log.info("atomic_swap_complete")


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
    client = llm_client or OllamaClient()

    system_prompt = load_prompt("diagnose_config_repair")
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


# ==========================================
# HITL GATE
# ==========================================
def requires_hitl(report: DiagnosticsReport, hitl_always: bool = False) -> bool:
    """Returns True when the repair requires human approval before proceeding."""
    if hitl_always:
        return True
    if report.severity == "CRITICAL":
        return True
    joined = " ".join(report.identified_issues).lower()
    return any(kw in joined for kw in ("hacs", "database"))


# ==========================================
# ORCHESTRATION PIPELINE
# ==========================================
async def main(
    ssh_client: Optional[SSHClientProtocol] = None,
    llm_client: Optional[LLMClientProtocol] = None,
    notifier: Optional[NotifierProtocol] = None,
    gate: Optional[AutonomyGate] = None,
) -> None:
    setup_logging()
    if not get_correlation_id():
        set_correlation_id(str(uuid.uuid4()))
    init_local_database()

    _notifier: NotifierProtocol = notifier or get_notifier(
        NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR
    )
    _gate: AutonomyGate = gate or AutonomyGate(AUTONOMY_LEVEL, HITL_TIMEOUT_MINUTES)

    yaml_content, config_hash = await fetch_remote_config(ssh_client=ssh_client)
    report = await analyze_config_locally(yaml_content, llm_client=llm_client)

    if not report.is_valid and report.recommended_fix_yaml:
        log.warning("issue_flagged", severity=report.severity)

        # 0. Validate YAML content before touching the remote system
        validation = validate_proposed_fix(yaml_content, report.recommended_fix_yaml)
        if not validation.is_safe:
            log.error(
                "proposed_fix_rejected",
                reasons=validation.reasons,
            )
            record_state_memory(
                config_hash,
                False,
                report.identified_issues,
                f"Fix rejected by content validator: {'; '.join(validation.reasons)}",
            )
            return

        # 0b. Autonomy gate — request approval before any production config write
        risk = RiskLevel.CRITICAL if report.severity == "CRITICAL" else RiskLevel.HIGH
        nid = get_correlation_id() or str(uuid.uuid4())
        log.info("autonomy_gate_check", severity=report.severity, risk=risk.name)
        approved = await _gate.require_approval(
            subject=f"Pueo HITL: {report.severity} repair requires approval",
            body="\n".join(report.identified_issues),
            payload={
                "notification_id": nid,
                "severity": report.severity,
                "issues": report.identified_issues,
                "correlation_id": get_correlation_id(),
            },
            notifier=_notifier,
            risk=risk,
        )
        if not approved:
            log.warning("autonomy_gate_rejected", notification_id=nid)
            record_state_memory(
                config_hash,
                False,
                report.identified_issues,
                "Repair rejected via autonomy gate.",
            )
            return
        log.info("autonomy_gate_approved", notification_id=nid)

        # 1. Enforce strict backup baseline
        backup_slug = await execute_remote_backup(ssh_client=ssh_client)
        record_backup_slug(backup_slug)

        # 2. Deploy fix into sandbox and execute runtime checks
        passed_sandbox = await deploy_and_test_in_sandbox(
            report.recommended_fix_yaml, ssh_client=ssh_client
        )

        if passed_sandbox:
            # 3. Commit only if sandbox testing completes with absolute success
            await commit_atomic_swap(report.recommended_fix_yaml, ssh_client=ssh_client)
            record_state_memory(
                config_hash,
                False,
                report.identified_issues,
                f"Patched via Sandbox; Backup: {backup_slug}",
            )
        else:
            log.warning("atomic_swap_aborted")
            record_state_memory(
                config_hash,
                False,
                report.identified_issues,
                "Patch aborted; Sandbox test failed.",
            )
    else:
        log.info("config_valid")
        record_state_memory(config_hash, True, ["None"], "No Action Taken.")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
