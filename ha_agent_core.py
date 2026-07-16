#!/usr/bin/env python3
"""Layer 1 — read-only SSH fetch and local Ollama config diagnostics."""

import uuid
from typing import Any, Optional
from pydantic import BaseModel, Field

from config import (
    HA_HOST,
    HA_USER,
    SSH_KEY_PATH,
    CONFIG_REMOTE_PATH,
    OLLAMA_MODEL,
    SSH_RETRY_ATTEMPTS,
    SSH_RETRY_BASE_DELAY,
    MAX_PROMPT_TOKENS,
    HA_KNOWN_VERSION,
)
from interfaces import LLMClientProtocol, SSHClientProtocol
from utils.context import estimate_tokens, truncate_to_budget
from utils.logging import get_logger, setup_logging, set_correlation_id
from utils.ollama_client import OllamaClient
from utils.prompts import load_prompt
from utils.retry import async_retry
from utils.ssh_client import AsyncSSHClient

log = get_logger("ha_agent_core")

_SSH_RETRY: dict[str, Any] = dict(
    max_attempts=SSH_RETRY_ATTEMPTS,
    base_delay=SSH_RETRY_BASE_DELAY,
    exceptions=(OSError,),
)


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
@async_retry(**_SSH_RETRY)
async def fetch_remote_config(
    ssh_client: Optional[SSHClientProtocol] = None,
) -> str:
    """Connects via SSH and reads the configuration file atomically into memory."""
    client = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
    try:
        content = await client.read_file(CONFIG_REMOTE_PATH)
        log.info("config_fetched", host=HA_HOST)
        return content
    except Exception as e:
        log.error("ssh_fetch_failed", host=HA_HOST, error=str(e))
        raise


@async_retry(**_SSH_RETRY)
async def execute_remote_preflight_check(
    ssh_client: Optional[SSHClientProtocol] = None,
) -> tuple[int, str, str]:
    """Executes Home Assistant's native verification engine via CLI over SSH."""
    client = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
    return await client.run("ha core check", check=False)


async def check_ha_version(
    ssh_client: Optional[SSHClientProtocol] = None,
) -> None:
    """Fetches the live HA version and warns if it differs from the version recorded at setup."""
    if not HA_KNOWN_VERSION:
        return
    client = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
    try:
        _, stdout, _ = await client.run("ha core info")
        for line in stdout.splitlines():
            if line.startswith("version:"):
                live_version = line.split(":", 1)[1].strip()
                if live_version != HA_KNOWN_VERSION:
                    log.warning(
                        "ha_version_changed",
                        known_version=HA_KNOWN_VERSION,
                        live_version=live_version,
                    )
                else:
                    log.info("ha_version_ok", version=live_version)
                return
    except Exception as e:
        log.warning("ha_version_check_failed", error=str(e))


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
    """Uses your local M-series Mac Ollama instance to analyze the config file."""
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
        raw_output = response["message"]["content"]
        return DiagnosticsReport.model_validate_json(raw_output)
    except Exception as e:
        log.error("ollama_inference_failed", error=str(e))
        raise


# ==========================================
# ORCHESTRATION PIPELINE
# ==========================================
async def main(
    ssh_client: Optional[SSHClientProtocol] = None,
    llm_client: Optional[LLMClientProtocol] = None,
) -> None:
    setup_logging()
    set_correlation_id(str(uuid.uuid4()))

    log.info("ssh_connect_start", host=HA_HOST)
    await check_ha_version(ssh_client=ssh_client)
    yaml_content = await fetch_remote_config(ssh_client=ssh_client)

    report = await analyze_config_locally(yaml_content, llm_client=llm_client)

    log.info(
        "diagnostics_complete",
        is_valid=report.is_valid,
        severity=report.severity,
        issues=report.identified_issues,
        fix_provided=report.recommended_fix_yaml is not None,
    )

    log.info("preflight_check_start")
    exit_code, stdout, stderr = await execute_remote_preflight_check(
        ssh_client=ssh_client
    )
    if exit_code == 0:
        log.info("preflight_check_passed")
    else:
        log.error("preflight_check_failed", output=stderr or stdout)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
