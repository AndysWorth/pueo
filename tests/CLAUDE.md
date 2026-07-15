# Tests — Guidance for Claude Code

## Philosophy
Tests verify logic that can be exercised without external services. SSH connections, Ollama inference, and live HA calls are never made in tests — these are integration concerns tested manually against a real HA instance.

## What is tested
- `config.py` loading: correct values from YAML, fallback defaults when no file present, partial configs
- Pydantic schemas (`DiagnosticsReport`, `LogEvaluation`): valid construction, validation errors on bad input
- Sandbox path derivation: `SANDBOX_REMOTE_DIR` and `SANDBOX_REMOTE_FILE` stay in sync with `CONFIG_REMOTE_PATH`
- Log pattern matching: `CRITICAL_LOG_PATTERN` regex hits/misses against known HA log formats
- `CONFIDENCE_THRESHOLD` propagation from config into the monitor module

## Config isolation
`config.py` loads at import time using the `PUEO_CONFIG` env var. Tests that exercise config loading use the `isolated_config` fixture from `conftest.py`, which:
1. Points `PUEO_CONFIG` at a temp file
2. Calls `importlib.reload()` so the module re-reads the env var
3. Reloads all dependent agent modules after the test to prevent state leakage

## What is not tested here (yet)
- SSH transport layer (`asyncssh` calls) — needs mock or integration test harness
- Ollama inference — needs mock or integration test against local Ollama
- The full repair pipeline end-to-end — milestone 4 work
