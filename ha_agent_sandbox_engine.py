#!/usr/bin/env python3
"""Layer 3 — full repair pipeline: content validation, HITL gate, backup, sandbox test, atomic swap."""

import hashlib
import sqlite3
import time
import uuid
from typing import Optional

from config import (
    HA_HOST,
    HA_USER,
    SSH_KEY_PATH,
    CONFIG_REMOTE_PATH,
    OLLAMA_MODEL,
    OLLAMA_ENDPOINT,
    DB_PATH,
    SSH_RETRY_ATTEMPTS,
    SSH_RETRY_BASE_DELAY,
    MAX_PROMPT_TOKENS,
    NOTIFIER,
    NOTIFY_URL,
    NOTIFY_WATCH_DIR,
    HITL_ALWAYS,
    AUTONOMY_LEVEL,
    CHROMADB_PATH,
    RAG_EMBED_MODEL,
)
from interfaces import (
    KnowledgeStoreClientProtocol,
    LLMClientProtocol,
    SSHClientProtocol,
)
from utils.context import estimate_tokens, truncate_to_budget
from utils.llm_trace import LLMTrace
from utils.logging import (
    get_logger,
    get_correlation_id,
    setup_logging,
    set_correlation_id,
)
from utils.ollama_client import OllamaClient
from utils.prompts import load_prompt
from ha_agent_core import DiagnosticsReport
from utils.retry import async_retry, SSH_RETRY_KWARGS
from utils.ssh_client import AsyncSSHClient
from utils.autonomy import AutonomyGate, RiskLevel
from utils.notify import NotifierProtocol, get_notifier
from utils.yaml_validator import validate_proposed_fix
from ha_agent_advanced import (
    offload_backup_to_local,
    enforce_ha_retention,
    purge_local_backups,
)

log = get_logger("ha_agent_sandbox_engine")

# Sandbox paths derived from CONFIG_REMOTE_PATH
_config_dir = CONFIG_REMOTE_PATH.rsplit("/", 1)[0]
_config_filename = CONFIG_REMOTE_PATH.rsplit("/", 1)[1]
SANDBOX_REMOTE_DIR = f"{_config_dir}/.agent_sandbox"
SANDBOX_REMOTE_FILE = f"{_config_dir}/.agent_sandbox/{_config_filename}"


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


def _migrate_v6(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_history (
            notification_id TEXT PRIMARY KEY,
            first_seen_at   REAL,
            last_seen_at    REAL,
            category        TEXT,
            severity        TEXT,
            hitl_sent_at    REAL,
            dismissed_at    REAL,
            dismissed_by    TEXT
        )
        """
    )


def _migrate_v7(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS timeline_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL    NOT NULL,
            level       TEXT    NOT NULL,
            source      TEXT    NOT NULL,
            message     TEXT    NOT NULL,
            detail_json TEXT
        )
        """
    )


_MIGRATIONS: list[tuple[int, object]] = [
    (1, _migrate_v1),
    (2, _migrate_v2),
    (3, _migrate_v3),
    (4, _migrate_v4),
    (5, _migrate_v5),
    (6, _migrate_v6),
    (7, _migrate_v7),
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
            "INSERT INTO backup_registry (timestamp, backup_slug, status, size_bytes, location)"
            " VALUES (?, ?, 'ACTIVE', 0, 'ha')",
            (int(time.time()), slug),
        )
        conn.commit()


# ==========================================
# REMOTE INFRASTRUCTURE & BACKUP TOOLS
# ==========================================
@async_retry(**SSH_RETRY_KWARGS)
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


@async_retry(**SSH_RETRY_KWARGS)
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


@async_retry(**SSH_RETRY_KWARGS)
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
    await client.run("ha core restart", check=False)
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
) -> tuple[DiagnosticsReport, LLMTrace]:
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
    raw_output = response["message"]["content"]
    trace = LLMTrace(
        model=OLLAMA_MODEL,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        raw_response=raw_output,
    )
    return DiagnosticsReport.model_validate_json(raw_output), trace


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
    knowledge_store: Optional[KnowledgeStoreClientProtocol] = None,
) -> None:
    setup_logging()
    if not get_correlation_id():
        set_correlation_id(str(uuid.uuid4()))
    init_local_database()

    _ssh = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
    _notifier: NotifierProtocol = notifier or get_notifier(
        NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR
    )
    _gate: AutonomyGate = gate or AutonomyGate(AUTONOMY_LEVEL)
    _llm = llm_client or OllamaClient()

    yaml_content, config_hash = await fetch_remote_config(ssh_client=_ssh)

    from utils.agent_loop import AgentLoop
    from utils.tool_executor import ToolExecutor
    from utils.tool_registry import build_ha_tool_registry

    _knowledge_store: Optional[KnowledgeStoreClientProtocol]
    if knowledge_store is not None:
        _knowledge_store = knowledge_store
    else:
        from utils.knowledge_store import ChromaKnowledgeStore  # pragma: no cover

        _knowledge_store = ChromaKnowledgeStore(  # pragma: no cover
            path=CHROMADB_PATH,
            embed_model=RAG_EMBED_MODEL,
            ollama_endpoint=OLLAMA_ENDPOINT,
        )
    executor = ToolExecutor(
        ha_ssh_client=_ssh,
        gate=_gate,
        notifier=_notifier,
        knowledge_store=_knowledge_store,
    )
    registry = build_ha_tool_registry()
    loop = AgentLoop(
        llm_client=_llm,
        tool_executor=executor,
        tool_registry=registry,
    )

    initial_context = (
        "Analyze the following Home Assistant configuration.yaml for issues. "
        "If you find problems, investigate further and apply a fix. "
        "If the config looks correct, call finish_repair with action_taken='no_fix_needed'.\n\n"
        f"Current configuration.yaml:\n```yaml\n{yaml_content}\n```"
    )
    result = await loop.run(initial_context)

    action = {
        "success": "Repaired via agent loop",
        "exhausted": "Agent loop exhausted budget without resolution",
        "timeout": "Agent loop timed out",
        "fix_failed": "Agent loop attempted fix but sandbox test failed",
    }.get(result.outcome, result.outcome)

    issues = (
        [result.episode_stub.get("summary", "")]
        if result.episode_stub
        else ["No summary available"]
    )
    is_healthy = (
        result.outcome == "success"
        and result.episode_stub
        and (result.episode_stub.get("action_taken") in ("fixed", "no_fix_needed"))
    )
    record_state_memory(
        config_hash,
        bool(is_healthy),
        issues,
        action,
    )


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
