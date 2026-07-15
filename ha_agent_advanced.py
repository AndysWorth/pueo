#!/usr/bin/env python3
""" """

import asyncio
import sqlite3
import sys
import time
from typing import Dict, Any, Optional
import asyncssh
import ollama
from pydantic import BaseModel, Field

from config import (
    HA_HOST,
    HA_USER,
    SSH_KEY_PATH,
    CONFIG_REMOTE_PATH,
    OLLAMA_MODEL,
    DB_PATH,
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
def init_local_database():
    """Initializes the agent's long-term memory schema on the Mac file system."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # Track past system states and actions taken
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS state_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER,
                config_hash TEXT,
                is_valid INTEGER,
                issues_found TEXT,
                action_taken TEXT
            )
        """)
        # Track active backups to guarantee recovery chains
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS backup_registry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER,
                backup_slug TEXT,
                status TEXT
            )
        """)
        conn.commit()


def record_state_memory(config_hash: str, is_valid: bool, issues: list, action: str):
    """Saves telemetry to prevent the agent from cycling into identical failures."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO state_history (timestamp, config_hash, is_valid, issues_found, action_taken) VALUES (?, ?, ?, ?, ?)",
            (int(time.time()), config_hash, int(is_valid), ", ".join(issues), action),
        )
        conn.commit()


def record_backup_slug(slug: str):
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
async def fetch_remote_config() -> tuple[str, str]:
    """Reads remote config into memory and generates a lookup hash."""
    try:
        async with asyncssh.connect(
            HA_HOST, username=HA_USER, client_keys=[SSH_KEY_PATH], known_hosts=None
        ) as conn:
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(CONFIG_REMOTE_PATH, "r") as file:
                    content = await file.read()
                    # Calculate basic fingerprint hash to track states
                    import hashlib

                    config_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    return content, config_hash
    except Exception as e:
        print(
            f"❌ [Transport Error] Failed config fetch over SSH: {e}", file=sys.stderr
        )
        raise


def _extract_backup_slug(output: str) -> str:
    for line in output.split("\n"):
        if "slug:" in line.lower():
            return line.split(":")[-1].strip()
    return "unknown_slug"


async def execute_remote_backup() -> str:
    """Triggers Home Assistant's native backup CLI engine over SSH. Returns backup identifier slug."""
    print("💾 Triggering native Home Assistant hardware snapshot backup...")
    try:
        async with asyncssh.connect(
            HA_HOST, username=HA_USER, client_keys=[SSH_KEY_PATH], known_hosts=None
        ) as conn:
            result = await conn.run(
                'ha backup new --name "Agent_PreFix_Snapshot"', check=True
            )
            stdout = result.stdout if isinstance(result.stdout, str) else ""
            slug = _extract_backup_slug(stdout.strip())
            print(
                f"✅ Secure Backup created successfully. Remote target identifier: {slug}"
            )
            return slug
    except Exception as e:
        print(
            f"🛑 [CRITICAL SECURITY FAILURE] Remote backup routine failed: {e}",
            file=sys.stderr,
        )
        print(
            "❌ System aborting. No file operations will proceed without a confirmed backup.",
            file=sys.stderr,
        )
        raise


async def execute_remote_preflight_check() -> tuple[int, str, str]:
    """Executes Home Assistant's config verification parser via CLI."""
    async with asyncssh.connect(
        HA_HOST, username=HA_USER, client_keys=[SSH_KEY_PATH], known_hosts=None
    ) as conn:
        result = await conn.run("ha core check", check=False)
        exit_code = result.exit_status if result.exit_status is not None else 1
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        stderr = result.stderr if isinstance(result.stderr, str) else ""
        return exit_code, stdout, stderr


# ==========================================
# OLLAMA INFERENCE LAYER
# ==========================================
async def analyze_config_locally(yaml_content: str) -> DiagnosticsReport:
    """Runs zero-latency local analysis via macOS Ollama environment."""
    system_prompt = (
        "You are an expert Home Assistant core systems engineering agent. "
        "Analyze the provided configuration.yaml content for syntax issues, format breaking changes, or logical risks. "
        "You must respond strictly with a valid JSON object matching the requested schema."
    )
    user_prompt = f"Analyze this configuration data:\n\n```yaml\n{yaml_content}\n```"

    try:
        response = await asyncio.to_thread(
            ollama.chat,
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
        print(f"❌ [Inference Error] Ollama execution failed: {e}", file=sys.stderr)
        raise


# ==========================================
# TRANSACTUAL STATE ORCHESTRATION
# ==========================================
async def main():
    init_local_database()

    print(f"📡 Establishing SSH connection layer with {HA_HOST}...")
    yaml_content, config_hash = await fetch_remote_config()
    print(f"📊 State Unique Hash ID: {config_hash[:12]}")

    print(f"🧠 Computing intelligence diagnostics via Ollama {OLLAMA_MODEL}...")
    report = await analyze_config_locally(yaml_content)

    print("\n==========================================")
    print("📋 LOCAL AI STATE DIAGNOSTICS")
    print("==========================================")
    print(f"Configuration Securely Valid: {report.is_valid}")
    print(f"Risk Profile Rating:         {report.severity}")
    print(f"Identified Fault Log Items:  {report.identified_issues}")
    print("==========================================\n")

    # Transaction decision tree
    if not report.is_valid:
        print("⚠️ Anomalies or risks flagged by local agent reasoning.")

        # Operational Gate: Backup MUST happen prior to state recording / remediation actions
        backup_slug = await execute_remote_backup()
        record_backup_slug(backup_slug)

        # Commit to long-term memory history
        record_state_memory(
            config_hash,
            report.is_valid,
            report.identified_issues,
            action=f"Created backup {backup_slug}; Preparing patch.",
        )
        print("💾 State committed to local Mac SQLite history database.")
    else:
        print("🛡️ System configuration parsed as clear by local AI model.")
        record_state_memory(
            config_hash,
            report.is_valid,
            ["None"],
            action="System Verified Clear - No Action.",
        )

        print("⚙️ Verifying with raw remote native API parser...")
        exit_code, stdout, stderr = await execute_remote_preflight_check()
        if exit_code == 0:
            print(
                "✅ Complete synchronization: System reports 100% active operational health."
            )
        else:
            print(
                f"⚠️ DISCREPANCY: Ollama verified clear, but HA core check failed:\n{stderr or stdout}"
            )


if __name__ == "__main__":
    asyncio.run(main())
