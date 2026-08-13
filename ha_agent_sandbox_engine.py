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
    LLM_PROVIDER,
    CLOUD_MODEL,
    CLOUD_MAX_COST_PER_INCIDENT_USD,
    CLOUD_MAX_DAILY_SPEND_USD,
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
from utils.llm_factory import make_llm_client
from utils.prompts import load_prompt
from ha_agent_core import DiagnosticsReport
from utils.retry import async_retry, SSH_RETRY_KWARGS
from utils.ssh_client import AsyncSSHClient
from utils.autonomy import AutonomyGate, RiskLevel
from utils.notify import NotifierProtocol, get_notifier
from utils.yaml_validator import validate_proposed_fix
from ha_agent_advanced import (
    execute_remote_backup,  # noqa: F401 — re-exported; callers may import from here
    record_backup_slug,  # noqa: F401 — re-exported; callers may import from here
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


def _migrate_v8(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_memory (
            id      INTEGER PRIMARY KEY,
            key     TEXT NOT NULL,
            content TEXT NOT NULL,
            source  TEXT NOT NULL,
            ts      REAL NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id         INTEGER PRIMARY KEY,
            created_at REAL NOT NULL,
            title      TEXT NOT NULL DEFAULT ''
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id              INTEGER PRIMARY KEY,
            session_id      INTEGER NOT NULL REFERENCES chat_sessions(id),
            role            TEXT NOT NULL,
            content         TEXT NOT NULL,
            tool_calls_json TEXT,
            ts              REAL NOT NULL
        )
        """
    )


def _migrate_v9(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS registered_tools (
            id              INTEGER PRIMARY KEY,
            name            TEXT NOT NULL UNIQUE,
            description     TEXT NOT NULL,
            parameters_json TEXT NOT NULL,
            code            TEXT NOT NULL,
            created_at      REAL NOT NULL
        )
        """
    )


def _migrate_v10(cursor: sqlite3.Cursor) -> None:
    cursor.execute("ALTER TABLE notification_history ADD COLUMN ha_created_at REAL")


def _migrate_v11(cursor: sqlite3.Cursor) -> None:
    cursor.execute("ALTER TABLE backup_registry ADD COLUMN name TEXT")


def _migrate_v12(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ha_repair_history (
            issue_key    TEXT PRIMARY KEY,
            first_seen_at REAL NOT NULL,
            hitl_sent_at  REAL,
            resolved_at   REAL,
            dismissed_at  REAL
        )
        """
    )


def _migrate_v13(cursor: sqlite3.Cursor) -> None:
    cursor.execute("ALTER TABLE ha_repair_history ADD COLUMN translation_key TEXT")


def _migrate_v14(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ha_environment_profile (
            id           INTEGER PRIMARY KEY,
            profile_json TEXT NOT NULL,
            last_updated REAL NOT NULL
        )
        """
    )


def _migrate_v15(cursor: sqlite3.Cursor) -> None:
    cursor.execute("ALTER TABLE backup_registry ADD COLUMN ha_created_at REAL")


def _migrate_v16(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS cloud_spend (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id   TEXT,
            timestamp     REAL NOT NULL,
            model         TEXT NOT NULL,
            input_tokens  INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cost_usd      REAL NOT NULL
        )
        """
    )


def _migrate_v17(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS repair_episodes (
            id                  TEXT PRIMARY KEY,
            timestamp           REAL NOT NULL,
            trigger             TEXT NOT NULL,
            symptoms            TEXT NOT NULL,
            tool_sequence       TEXT NOT NULL,
            hypothesis_chain    TEXT NOT NULL,
            fix_applied         TEXT,
            verification_result INTEGER NOT NULL,
            model_used          TEXT NOT NULL,
            escalated           INTEGER NOT NULL,
            duration_seconds    REAL NOT NULL
        )
        """
    )


def _migrate_v18(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS hardware_profile (
            id INTEGER PRIMARY KEY,
            chip TEXT NOT NULL DEFAULT 'Unknown',
            arch TEXT NOT NULL DEFAULT 'unknown',
            ram_gb REAL NOT NULL DEFAULT 0,
            cpu_cores INTEGER NOT NULL DEFAULT 0,
            detected_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ollama_model_cache (
            model_name TEXT PRIMARY KEY,
            size_gb REAL NOT NULL DEFAULT 0,
            has_tools INTEGER NOT NULL DEFAULT 0,
            context_length INTEGER NOT NULL DEFAULT 0,
            last_seen_at TEXT NOT NULL
        )
        """
    )


def _migrate_v19(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS log_triage_history (
            fingerprint   TEXT PRIMARY KEY,
            first_seen_at REAL NOT NULL,
            last_seen_at  REAL NOT NULL,
            hitl_sent_at  REAL,
            send_count    INTEGER NOT NULL DEFAULT 0
        )
        """
    )


def _migrate_v20(cursor: sqlite3.Cursor) -> None:
    cursor.execute("ALTER TABLE repair_episodes ADD COLUMN submitted_at REAL")
    cursor.execute("ALTER TABLE repair_episodes ADD COLUMN pr_url TEXT")


def _migrate_v21(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        "ALTER TABLE repair_episodes ADD COLUMN embedded_at REAL DEFAULT NULL"
    )


_MIGRATIONS: list[tuple[int, object]] = [
    (1, _migrate_v1),
    (2, _migrate_v2),
    (3, _migrate_v3),
    (4, _migrate_v4),
    (5, _migrate_v5),
    (6, _migrate_v6),
    (7, _migrate_v7),
    (8, _migrate_v8),
    (9, _migrate_v9),
    (10, _migrate_v10),
    (11, _migrate_v11),
    (12, _migrate_v12),
    (13, _migrate_v13),
    (14, _migrate_v14),
    (15, _migrate_v15),
    (16, _migrate_v16),
    (17, _migrate_v17),
    (18, _migrate_v18),
    (19, _migrate_v19),
    (20, _migrate_v20),
    (21, _migrate_v21),
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
    client = llm_client or make_llm_client()

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


_CODE_PROPOSAL_SYSTEM_PROMPT = """\
You are Pueo, performing autonomous capability-gap closure.

A previous repair loop could not fully address an issue because a required tool or capability
was missing. Your task is to propose a Python patch that adds the missing capability.

MANDATORY FLOW — follow exactly in this order:
1. Call read_source to examine the source files relevant to the missing capability
   (e.g. utils/tool_executor.py, utils/tool_registry.py).
2. Call propose_patch to stage the proposed change (complete new file content).
3. Call sandbox_code to run CI validation (black, flake8, mypy, pytest).
4. If CI passes, call open_pr to queue a HITL PR-approval card.
5. Call finish_repair when done:
   - action_taken='fixed' if open_pr was successfully queued
   - action_taken='fix_failed' if sandbox CI failed or no valid patch could be produced

Never call open_pr before sandbox_code passes.
"""


async def _run_code_proposal_loop(
    gap_description: str,
    ssh_client: "SSHClientProtocol",
    gate: "AutonomyGate",
    notifier: "NotifierProtocol",
    llm_client: "LLMClientProtocol",
    db_path: str = DB_PATH,
) -> None:
    """Run a code-proposal loop in response to a capability gap detected by finish_repair.

    Called automatically when a repair AgentLoop result has capability_gap=True.
    The loop reads relevant source, proposes a patch, validates it in the sandbox,
    and queues an open_pr HITL card — all without human input until the final approval.
    """
    from utils.agent_loop import AgentLoop
    from utils.tool_executor import ToolExecutor
    from utils.tool_registry import build_code_proposal_registry

    executor = ToolExecutor(
        ha_ssh_client=ssh_client,
        gate=gate,
        notifier=notifier,
        db_path=db_path,
    )
    registry = build_code_proposal_registry()
    proposal_loop = AgentLoop(
        llm_client=llm_client,
        tool_executor=executor,
        tool_registry=registry,
        system_prompt=_CODE_PROPOSAL_SYSTEM_PROMPT,
        trigger="gap_detection",
    )
    initial_context = (
        "The previous HA repair loop detected a capability gap:\n\n"
        f"{gap_description}\n\n"
        "Read the relevant source files, propose a patch that closes this gap, "
        "validate it with sandbox_code, and queue it for review with open_pr."
    )
    result = await proposal_loop.run(initial_context)
    log.info(
        "code_proposal_loop_complete",
        outcome=result.outcome,
        steps=len(result.steps),
        gap_description=gap_description[:100],
    )


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
    _llm = llm_client or make_llm_client()

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
        trigger="ha_log",
        db_path=DB_PATH,
    )

    initial_context = (
        "Analyze the following Home Assistant configuration.yaml for issues. "
        "If you find problems, investigate further and apply a fix. "
        "If the config looks correct, call finish_repair with action_taken='no_fix_needed'.\n\n"
        f"Current configuration.yaml:\n```yaml\n{yaml_content}\n```"
    )
    result = await loop.run(initial_context)

    # Autonomous gap detection (item 84): when the agent signalled a capability gap,
    # automatically start a code-proposal loop that drafts a patch, validates it in
    # sandbox CI, and queues an open_pr HITL card for human review.
    if result.capability_gap and result.gap_description:
        log.info(
            "gap_detection_triggered",
            gap_description=result.gap_description[:100],
        )
        await _run_code_proposal_loop(
            gap_description=result.gap_description,
            ssh_client=_ssh,
            gate=_gate,
            notifier=_notifier,
            llm_client=_llm,
        )

    # When running in "both" mode and the local loop is unable to resolve the
    # issue, gate a cloud escalation through AutonomyGate:
    # - Level 4 (AUTONOMOUS): auto-proceeds immediately, no HITL card.
    # - Level 2–3: sends a cloud_escalation HITL card and polls for approval.
    # - Level 1 (REPORT_ONLY): require_approval returns False, skip silently.
    if result.outcome in ("exhausted", "timeout") and LLM_PROVIDER == "both":
        from utils.agent_loop import _format_step_trace
        from utils.billing import BillingCapError, estimate_cost, get_daily_spend
        from utils.card_types import CARD_TYPE_CLOUD_ESCALATION

        nid = str(uuid.uuid4())
        step_summary = _format_step_trace(result.steps)
        daily_spent = get_daily_spend()
        est_cost = estimate_cost(CLOUD_MODEL, 2000, 512)

        approved = await _gate.require_approval(
            subject=f"Cloud LLM escalation: local loop {result.outcome}",
            body=(
                f"Local Ollama loop ran {len(result.steps)} tool call(s) "
                f"and {result.outcome}. "
                f"Escalate to {CLOUD_MODEL}? "
                f"Estimated cost: ${est_cost:.4f}. "
                f"Daily spend: ${daily_spent:.4f} / ${CLOUD_MAX_DAILY_SPEND_USD:.2f}."
            ),
            payload={
                "notification_id": nid,
                "card_type": CARD_TYPE_CLOUD_ESCALATION,
                "outcome": result.outcome,
                "step_count": len(result.steps),
                "step_summary": step_summary,
                "initial_context": initial_context,
                "cloud_model": CLOUD_MODEL,
                "estimated_cost_usd": round(est_cost, 4),
                "daily_spend_usd": round(daily_spent, 4),
                "daily_cap_usd": CLOUD_MAX_DAILY_SPEND_USD,
                "per_incident_cap_usd": CLOUD_MAX_COST_PER_INCIDENT_USD,
            },
            notifier=_notifier,
            risk=RiskLevel.HIGH,
        )
        log.info(
            "cloud_escalation_gate",
            outcome=result.outcome,
            approved=approved,
            steps=len(result.steps),
        )
        if approved:
            from utils.cloud_escalation import run_cloud_escalation

            try:
                result = await run_cloud_escalation(
                    initial_context=initial_context,
                    tool_registry=registry,
                    gate=_gate,
                    ha_ssh_client=_ssh,
                    notifier=_notifier,
                    incident_id=nid,
                )
            except BillingCapError as exc:
                log.error("cloud_escalation_billing_cap", error=str(exc))
                return
        else:
            return

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
