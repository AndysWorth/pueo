#!/usr/bin/env python3
""" """

import asyncio
import re
import sys
import time
import asyncssh
import ollama
from pydantic import BaseModel, Field

from config import (
    HA_HOST,
    HA_USER,
    SSH_KEY_PATH,
    LOG_REMOTE_PATH,
    OLLAMA_MODEL,
    CONFIDENCE_THRESHOLD,
)

# High-priority regex to capture structural components collapsing or syntax crashes
CRITICAL_LOG_PATTERN = re.compile(
    r"(ERROR|CRITICAL).*?(Component error|Failed to initialize|Traceback|Invalid config|Error doing job)",
    re.IGNORECASE,
)


# ==========================================
# DATA SHAPE DEFINITIONS
# ==========================================
class LogEvaluation(BaseModel):
    is_actionable: bool = Field(
        description="True if this log indicates a configuration or integration issue that a code patch can fix."
    )
    root_cause_summary: str = Field(
        description="Brief string summarizing exactly what failed (e.g., 'Malformed YAML in light integration')."
    )
    confidence_score: float = Field(
        description="Value between 0.0 and 1.0 evaluating certainty."
    )


# ==========================================
# LOCAL REAL-TIME LOG FILTERING ENGINE
# ==========================================
async def analyze_log_line_with_ai(log_line: str) -> LogEvaluation:
    """Uses local Ollama to quickly classify if an intercepted error is patchable."""
    system_prompt = (
        "You are an expert Home Assistant site reliability agent. "
        "Analyze this intercepted log error line. Determine if it is an infrastructure, "
        "integration setup, or configuration error that can be fixed by altering configuration files. "
        "Respond strictly in the requested JSON format."
    )
    user_prompt = f"Evaluate this error line:\n`{log_line}`"

    try:
        response = await asyncio.to_thread(
            ollama.chat,
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            options={"temperature": 0.0},
            format=LogEvaluation.model_json_schema(),
        )
        return LogEvaluation.model_validate_json(response["message"]["content"])
    except Exception as e:
        print(f"❌ [Log AI Error] Local classification failed: {e}", file=sys.stderr)
        return LogEvaluation(
            is_actionable=False,
            root_cause_summary="Inference crash",
            confidence_score=0.0,
        )


# ==========================================
# STREAMING SSH CONNECTION LAYER
# ==========================================
async def tail_remote_log_stream():
    """Establishes an unblocked SSH pipeline streaming live Home Assistant logs."""
    print(f"📡 Initializing live SSH log pipeline stream to {HA_HOST}...")

    # We use a Linux tail loop over SSH to stream updates continuously without pooling files
    tail_command = f"tail -F -n 10 {LOG_REMOTE_PATH}"

    try:
        async with asyncssh.connect(
            HA_HOST, username=HA_USER, client_keys=[SSH_KEY_PATH], known_hosts=None
        ) as conn:
            async with conn.create_process(tail_command) as process:
                print(
                    "👁️  System Listening. Monitoring for smart home infrastructure anomalies..."
                )

                # Continuously read incoming lines from the standard output stream
                async for line in process.stdout:
                    clean_line = line.strip()

                    # Layer 1: Hyper-fast Python Regex match (Zero local CPU overhead)
                    if CRITICAL_LOG_PATTERN.search(clean_line):
                        print(f"\n⚠️  [Intercepted Error Line]: {clean_line}")

                        # Layer 2: Targeted Local LLM context evaluation
                        print(
                            f"🧠 Consulting local {OLLAMA_MODEL} for triage classification..."
                        )
                        evaluation = await analyze_log_line_with_ai(clean_line)

                        print(
                            f"📋 AI Evaluation -> Actionable: {evaluation.is_actionable} | Cause: {evaluation.root_cause_summary}"
                        )

                        if (
                            evaluation.is_actionable
                            and evaluation.confidence_score > CONFIDENCE_THRESHOLD
                        ):
                            print("🚨 TRIGGERING AUTONOMOUS HEALING SEQUENCE 🚨")
                            # Trigger your Sandbox Engine module directly from here
                            await trigger_remediation_pipeline()
                            print("🏁 Returning to log monitoring loop...\n")

    except Exception as e:
        print(f"🛑 [Log Stream Disruption] Pipeline crashed: {e}", file=sys.stderr)
        print("🔄 Attempting automatic stream reconnection in 5 seconds...")
        await asyncio.sleep(5)
        await tail_remote_log_stream()


async def trigger_remediation_pipeline():
    """Invokes your previously built Sandbox & Swap Engine script securely."""
    try:
        # Import your previous main pipeline script block asynchronously
        import ha_agent_sandbox_engine

        await ha_agent_sandbox_engine.main()
    except Exception as e:
        print(
            f"❌ [Remediation Route Failed] Could not execute swap pipeline: {e}",
            file=sys.stderr,
        )


# ==========================================
# MAIN EXECUTION ENTRY
# ==========================================
async def main():
    await tail_remote_log_stream()


if __name__ == "__main__":
    asyncio.run(main())
