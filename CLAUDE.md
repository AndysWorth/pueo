# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Pueo** is a local, privacy-first agentic AI system that monitors and self-heals a Home Assistant (HA) instance. It runs on macOS Apple Silicon using a local Ollama inference engine — no cloud LLM calls during active repair cycles. All HA communication goes over SSH/SFTP.

Source code lives in `pueo/`.

## Commands

Run from the `pueo/` directory:

```bash
pip install -r requirements-dev.txt  # includes runtime deps + dev/test tooling

# Entry points
python ha_agent_core.py            # Read-only: SSH fetch + Ollama diagnosis
python ha_agent_advanced.py        # + SQLite memory + backup triggering
python ha_agent_sandbox_engine.py  # Full: sandbox-test-then-atomic-swap repair
python ha_log_monitor.py           # Continuous: live SSH log tail + AI triage

# Tests
pytest
pytest tests/test_core.py::TestConfigDefaults::test_loads_values_from_yaml  # single test example

# Code quality (CI enforces all of these)
black --check .
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
mypy --ignore-missing-imports .
bandit -r . -x ./tests
```

## Architecture

Four layered scripts, each building on the previous:

### Layer 1 — Sensing: `ha_agent_core.py`
Read-only. Fetches `/config/configuration.yaml` from HA over SSH/SFTP, runs it through local Ollama (`qwen2.5-coder:7b`) with structured JSON output enforced by the `DiagnosticsReport` Pydantic schema, then optionally cross-verifies by running `ha core check` on the remote host.

### Layer 2 — Memory: `ha_agent_advanced.py`
Extends core with a local SQLite database (`ha_agent_state.db`) persisting `state_history` and `backup_registry` tables. Before any remediation action, a native HA backup snapshot is triggered via `ha backup new` over SSH and its slug is recorded — hard safety gate, aborts on failure.

### Layer 3 — Reasoning + Acting: `ha_agent_sandbox_engine.py`
Full repair pipeline. When Ollama returns `is_valid=False` with a `recommended_fix_yaml`:
1. Trigger HA backup (mandatory)
2. Write proposed fix to `/config/.agent_sandbox/configuration.yaml` over SFTP
3. Temporarily swap it into `/config/configuration.yaml`, run `ha core check`, immediately revert
4. Only if the sandbox check passes: atomically write to production and call `ha core reload`

### Layer 4 — Continuous Monitoring: `ha_log_monitor.py`
Runs `tail -F` on `/config/home-assistant.log` via a persistent SSH process stream. Two-layer triage: fast regex pre-filter (`CRITICAL_LOG_PATTERN`) then Ollama `LogEvaluation` with `confidence_score > 0.7` threshold. High-confidence actionable errors trigger `ha_agent_sandbox_engine.main()`. Reconnects automatically on stream failure.

## Key Patterns

**Structured LLM output**: All Ollama calls use `format=PydanticModel.model_json_schema()` and `temperature=0.0` to force deterministic, parseable JSON. Always wrap in `asyncio.to_thread()` since `ollama.chat` is synchronous.

**Safety invariant**: No write operation proceeds without a confirmed HA backup slug. Ordering is always `execute_remote_backup()` → `record_backup_slug()` → remediation. Never bypass this chain.

**SSH connections**: Each function opens its own `asyncssh.connect()` context. `known_hosts=None` is intentional for local-network HA hosts — flag in any security review.

**Single config source**: `config.py` is the only place settings are defined. Agent scripts must import from it (`from config import ...`) and must never redeclare constants. Adding a new setting means adding it to `config.yaml.default`, `config.py`, and `setup.sh` — nowhere else.

**Config path resolution**: `config.py` loads at module import time. It checks the `PUEO_CONFIG` environment variable first, then falls back to `config.yaml` next to the script. `main.py` sets `PUEO_CONFIG` before importing any agent module so the right config file is used. Agent imports inside `main.py` must stay deferred (inside the `if args.mode` blocks) — moving them to the top of the file would break this.

**Sandbox path derivation**: `SANDBOX_REMOTE_DIR` and `SANDBOX_REMOTE_FILE` in `ha_agent_sandbox_engine.py` are derived from `CONFIG_REMOTE_PATH`, not independently hardcoded, so changing the config path in `config.yaml` automatically keeps the sandbox path in sync.

## Configuration

`config.py` loads `config.yaml` at import time and exposes all settings as typed module-level constants with fallback defaults. Agent scripts are run-able directly without a `config.yaml` (defaults kick in); `main.py` is needed to point at a non-default config path.

`config.yaml` is gitignored. `config.yaml.default` is the committed reference template. Run `setup.sh` to generate `config.yaml` interactively.

## Deployment

`Dockerfile` + `docker-compose.yml` use `network_mode: host` for ARP/raw socket access. The container expects `config.yaml` mounted read-only at `/app/config.yaml`. `main.py` is the unified entry point; it sets `PUEO_CONFIG`, then dispatches to the right agent module. Default mode is `monitor` (the live log daemon).

## Testing

When adding or modifying any feature, add corresponding tests in the same session — do not defer them. Tests live in `tests/test_core.py`, grouped by class matching their module.

**Rules:**
- Every new Pydantic schema gets three tests: valid construction, invalid/missing fields, JSON round-trip
- Every new `config.py` key gets a test in `TestConfigDefaults` using the `isolated_config` fixture
- Every new pure-logic function (path derivation, regex, threshold comparison) gets a test
- SSH and Ollama calls are never mocked in the unit suite — those are integration concerns; see `tests/CLAUDE.md`
- Use `/project:write-tests <target>` to generate tests for a specific function or module

The Stop hook (`/.claude/hooks/stop.sh`) will remind you at session end if Python files were modified without touching `tests/`.

## CI

`.github/workflows/test.yml` runs on Python 3.12, 3.13, 3.14 against `main`/`develop`. Gates: `black`, `flake8` (errors only), `mypy`, `bandit`, `pytest --cov`.

## Roadmap

@docs/roadmap.md

## MCP Servers

`.mcp.json` configures a Home Assistant MCP server for use during development, giving Claude Code direct access to live HA state and entities. Requires `mcp-homeassistant` installed (`uvx mcp-homeassistant`) and `HA_TOKEN` set in the environment. See `.mcp.json` for the full config shape.

## Implementation Plan

Ordered backlog of agentic engineering practices to implement. Pick up the next incomplete item at the start of a new session.

@docs/implementation-plan.md

## Design Decisions

Rationale for key architectural choices is in `docs/decisions/`:

@docs/decisions/001-config-centralization.md

@docs/decisions/002-safety-invariant.md

@docs/decisions/003-structured-llm-output.md
