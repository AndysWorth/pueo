#!/usr/bin/env python3
""" """

import asyncio
import sys
from typing import Dict, Any, Optional
import asyncssh
import ollama
from pydantic import BaseModel, Field

from config import HA_HOST, HA_USER, SSH_KEY_PATH, CONFIG_REMOTE_PATH, OLLAMA_MODEL


# ==========================================
# DATA SHAPE DEFINITIONS (Pydantic validation)
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
# LOCAL AGENTIC TOOL LAYER
# ==========================================
async def fetch_remote_config() -> str:
    """Connects via SSH and reads the configuration file atomically into memory."""
    try:
        async with asyncssh.connect(
            HA_HOST,
            username=HA_USER,
            client_keys=[SSH_KEY_PATH],
            known_hosts=None,  # In production, map this to your known_hosts file
        ) as conn:
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(CONFIG_REMOTE_PATH, "r") as file:
                    content = await file.read()
                    return content
    except Exception as e:
        print(
            f"❌ [Transport Error] Failed to fetch remote config over SSH: {e}",
            file=sys.stderr,
        )
        raise


async def execute_remote_preflight_check() -> tuple[int, str, str]:
    """Executes Home Assistant's native verification engine via CLI over SSH."""
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
    """Uses your local M-series Mac Ollama instance to analyze the config file."""

    system_prompt = (
        "You are an expert Home Assistant core systems engineering agent. "
        "Analyze the provided configuration.yaml content for syntax issues, format breaking changes, or logical risks. "
        "You must respond strictly with a valid JSON object matching the requested schema."
    )

    user_prompt = f"Analyze this configuration data:\n\n```yaml\n{yaml_content}\n```"

    try:
        # Run local inference asynchronously via thread pool to keep the loop unblocked
        response = await asyncio.to_thread(
            ollama.chat,
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            options={"temperature": 0.0},  # Keep reasoning deterministic
            format=DiagnosticsReport.model_json_schema(),  # Force structured JSON output
        )

        raw_output = response["message"]["content"]
        # Parse and return structured Pydantic data
        return DiagnosticsReport.model_validate_json(raw_output)

    except Exception as e:
        print(f"❌ [Inference Error] Local Ollama parsing failed: {e}", file=sys.stderr)
        raise


# ==========================================
# ORCHESTRATION PIPELINE
# ==========================================
async def main():
    print(f"📡 Connecting to Home Assistant at {HA_HOST}...")
    yaml_content = await fetch_remote_config()
    print("✅ Configuration securely downloaded into isolated memory.")

    print(f"🧠 Routing to local Ollama runtime ({OLLAMA_MODEL}). Evaluating safety...")
    report = await analyze_config_locally(yaml_content)

    print("\n==========================================")
    print("📋 LOCAL AI DIAGNOSTICS REPORT")
    print("==========================================")
    print(f"Status Valid: {report.is_valid}")
    print(f"Risk Rating:  {report.severity}")
    print(f"Issues Found: {report.identified_issues}")
    if report.recommended_fix_yaml:
        print(f"\nProposed Fix Snippet:\n{report.recommended_fix_yaml}")
    print("==========================================\n")

    print("🛡️ Running remote pre-flight CLI verification...")
    exit_code, stdout, stderr = await execute_remote_preflight_check()
    if exit_code == 0:
        print("✅ Remote HA core engine reports: Configuration is valid.")
    else:
        print(f"❌ Remote HA core engine reports FAILURE:\n{stderr or stdout}")


if __name__ == "__main__":
    asyncio.run(main())
