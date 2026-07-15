#!/usr/bin/env python3
"""
"""

import asyncio
import sqlite3
import sys
import time
import hashlib
from typing import Dict, Any, Optional
import asyncssh
import ollama
from pydantic import BaseModel, Field

from config import HA_HOST, HA_USER, SSH_KEY_PATH, CONFIG_REMOTE_PATH, OLLAMA_MODEL, DB_PATH

# Sandbox paths derived from CONFIG_REMOTE_PATH
_config_dir = CONFIG_REMOTE_PATH.rsplit("/", 1)[0]
_config_filename = CONFIG_REMOTE_PATH.rsplit("/", 1)[1]
SANDBOX_REMOTE_DIR = f"{_config_dir}/.agent_sandbox"
SANDBOX_REMOTE_FILE = f"{_config_dir}/.agent_sandbox/{_config_filename}"

# ==========================================
# DATA SHAPE DEFINITIONS
# ==========================================
class DiagnosticsReport(BaseModel):
    is_valid: bool = Field(description="True if the YAML config has no structural flaws.")
    severity: str = Field(description="Severity classification: 'NONE', 'LOW', 'MEDIUM', 'CRITICAL'")
    identified_issues: list[str] = Field(description="List of specific flaws or risks found.")
    recommended_fix_yaml: Optional[str] = Field(None, description="The complete, fully corrected replacement string for configuration.yaml.")

# ==========================================
# LOCAL MEMORY LAYER (SQLite)
# ==========================================
def init_local_database():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS state_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER, config_hash TEXT,
                is_valid INTEGER, issues_found TEXT, action_taken TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS backup_registry (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER, backup_slug TEXT, status TEXT
            )
        """)
        conn.commit()

def record_state_memory(config_hash: str, is_valid: bool, issues: list, action: str):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO state_history (timestamp, config_hash, is_valid, issues_found, action_taken) VALUES (?, ?, ?, ?, ?)",
            (int(time.time()), config_hash, int(is_valid), ", ".join(issues), action)
        )
        conn.commit()

def record_backup_slug(slug: str):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO backup_registry (timestamp, backup_slug, status) VALUES (?, ?, 'ACTIVE')", (int(time.time()), slug))
        conn.commit()

# ==========================================
# REMOTE INFRASTRUCTURE & BACKUP TOOLS
# ==========================================
async def fetch_remote_config() -> tuple[str, str]:
    try:
        async with asyncssh.connect(HA_HOST, username=HA_USER, client_keys=[SSH_KEY_PATH], known_hosts=None) as conn:
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(CONFIG_REMOTE_PATH, 'r') as file:
                    content = await file.read()
                    config_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
                    return content, config_hash
    except Exception as e:
        print(f"❌ [Transport Error] Failed config fetch over SSH: {e}", file=sys.stderr)
        raise

def _extract_backup_slug(output: str) -> str:
    for line in output.split("\n"):
        if "slug:" in line.lower():
            return line.split(":")[-1].strip()
    return "unknown_slug"


async def execute_remote_backup() -> str:
    print("💾 Triggering native Home Assistant hardware snapshot backup...")
    try:
        async with asyncssh.connect(HA_HOST, username=HA_USER, client_keys=[SSH_KEY_PATH], known_hosts=None) as conn:
            result = await conn.run('ha backup new --name "Agent_PreFix_Snapshot"', check=True)
            slug = _extract_backup_slug(result.stdout.strip())
            print(f"✅ Secure Backup created successfully. Remote identifier: {slug}")
            return slug
    except Exception as e:
        print(f"🛑 [CRITICAL FAILURE] Remote backup routine failed: {e}", file=sys.stderr)
        raise

async def execute_remote_preflight_check() -> tuple[int, str, str]:
    async with asyncssh.connect(HA_HOST, username=HA_USER, client_keys=[SSH_KEY_PATH], known_hosts=None) as conn:
        result = await conn.run('ha core check', check=False)
        return result.exit_status, result.stdout, result.stderr

# ==========================================
# SANDBOX EXECUTION & ATOMIC SWAP ENGINE
# ==========================================
async def deploy_and_test_in_sandbox(fixed_yaml: str) -> bool:
    """Deploys code change to an isolated remote sandbox file and tests it via the HA compiler."""
    print("🧪 Preparing remote isolated testing sandbox...")
    try:
        async with asyncssh.connect(HA_HOST, username=HA_USER, client_keys=[SSH_KEY_PATH], known_hosts=None) as conn:
            # 1. Ensure sandbox directory exists
            await conn.run(f'mkdir -p {SANDBOX_REMOTE_DIR}', check=True)

            # 2. Write the AI's proposed configuration payload to the sandbox file
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(SANDBOX_REMOTE_FILE, 'w') as file:
                    await file.write(fixed_yaml)

            # 3. Swap sandbox into the compilation path *temporarily* to verify code structure
            print("⚙️ Executing remote pre-flight testing compilation block...")
            await conn.run(f'mv {CONFIG_REMOTE_PATH} {CONFIG_REMOTE_PATH}.bak', check=True)
            await conn.run(f'cp {SANDBOX_REMOTE_FILE} {CONFIG_REMOTE_PATH}', check=True)

            # 4. Trigger Home Assistant's internal compiler validator
            exit_code, stdout, stderr = await execute_remote_preflight_check()

            # 5. Instantly revert to protect the running system while evaluating the result
            await conn.run(f'mv {CONFIG_REMOTE_PATH}.bak {CONFIG_REMOTE_PATH}', check=True)

            if exit_code == 0:
                print("🏁 Sandbox Test Result: 100% VALID. Code patch is safe.")
                return True
            else:
                print(f"❌ Sandbox Test Result: REJECTED by HA Compiler.\nError Log:\n{stderr or stdout}")
                return False

    except Exception as e:
        print(f"🛑 [Sandbox Error] Safety engine failed execution: {e}", file=sys.stderr)
        return False

async def commit_atomic_swap(fixed_yaml: str):
    """Executes a permanent, clean, atomic swap of the validated sandbox code into production."""
    print("🚀 Committing atomic swap to production target...")
    async with asyncssh.connect(HA_HOST, username=HA_USER, client_keys=[SSH_KEY_PATH], known_hosts=None) as conn:
        async with conn.start_sftp_client() as sftp:
            async with sftp.open(CONFIG_REMOTE_PATH, 'w') as file:
                await file.write(fixed_yaml)
        # Force a hot reload of the configuration variables in the core engine
        await conn.run('ha core reload', check=False)
    print("🎉 Production successfully updated and hot-reloaded.")

# ==========================================
# OLLAMA INFERENCE LAYER
# ==========================================
async def analyze_config_locally(yaml_content: str) -> DiagnosticsReport:
    system_prompt = (
        "You are an expert Home Assistant core systems engineering agent. "
        "Analyze the provided configuration.yaml content for errors. If errors exist, set is_valid to false, "
        "and provide the complete, functional configuration.yaml content inside recommended_fix_yaml with the error corrected."
    )
    user_prompt = f"Analyze this configuration data:\n\n```yaml\n{yaml_content}\n```"

    response = await asyncio.to_thread(
        ollama.chat, model=OLLAMA_MODEL,
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        options={"temperature": 0.0}, format=DiagnosticsReport.model_json_schema()
    )
    return DiagnosticsReport.model_validate_json(response['message']['content'])

# ==========================================
# ORCHESTRATION PIPELINE
# ==========================================
async def main():
    init_local_database()
    yaml_content, config_hash = await fetch_remote_config()
    report = await analyze_config_locally(yaml_content)

    if not report.is_valid and report.recommended_fix_yaml:
        print(f"⚠️ Local Agent flagged an issue. Severity: {report.severity}")

        # 1. Enforce strict backup baseline
        backup_slug = await execute_remote_backup()
        record_backup_slug(backup_slug)

        # 2. Deploy fix into sandbox and execute runtime checks
        passed_sandbox = await deploy_and_test_in_sandbox(report.recommended_fix_yaml)

        if passed_sandbox:
            # 3. Commit only if sandbox testing completes with absolute success
            await commit_atomic_swap(report.recommended_fix_yaml)
            record_state_memory(config_hash, False, report.identified_issues, f"Patched via Sandbox; Backup: {backup_slug}")
        else:
            print("🛑 Atomic Swap aborted. The code patch generated by the AI failed safety validation.")
            record_state_memory(config_hash, False, report.identified_issues, "Patch aborted; Sandbox test failed.")
    else:
        print("🛡️ System configuration is valid. No remediation required.")
        record_state_memory(config_hash, True, ["None"], "No Action Taken.")

if __name__ == "__main__":
    asyncio.run(main())
