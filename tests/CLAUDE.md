# Tests — Guidance for Claude Code

## Philosophy
Tests verify logic that can be exercised without external services. SSH connections, Ollama inference, and live HA calls are never made in tests — these are integration concerns tested manually against a real HA instance.

## File map

| File | Subsystem | What lives here |
|---|---|---|
| `test_config.py` | Config | Every `config.py` key: default value, YAML override, partial config. One test per key minimum. |
| `test_core_agent.py` | Core HA agent | Pydantic schemas (`DiagnosticsReport`, `LogEvaluation`), SQLite migration layers, `main.py` CLI, `check_ha_version`, full pipeline tests via fakes. |
| `test_utils.py` | Utilities | `utils/retry`, `utils/rate_limiter`, `utils/logging` formatters, `utils/context`, `utils/yaml_validator`, `FakeSSHClient`, `FakeLLMClient`. |
| `test_hitl.py` | HITL & autonomy | `utils/notify` notifiers, `requires_hitl`, HITL pipeline gate, `AutonomyGate` all four levels. |
| `test_netalertx.py` | NetAlertX | Detector, API client, SQLite migration, installer steps 1–8, device name sync, log/health monitoring, AI diagnosis, autonomy-gated healing, maintenance, version guard. |
| `test_dashboard.py` | Dashboard & evidence | FastAPI routes, HITL request model, `LLMTrace`, dashboard rich payloads, installer diagnostics. |

**Where to put new tests:**
- New `config.py` key → `test_config.py` (`TestConfigDefaults` or the relevant subsystem config class)
- New utility function in `utils/` → `test_utils.py`
- New NetAlertX feature → `test_netalertx.py`
- New dashboard route or evidence field → `test_dashboard.py`
- New HA agent behavior → `test_core_agent.py`

## Rules

- Every new Pydantic schema gets three tests: valid construction, invalid/missing fields, JSON round-trip.
- Every new `config.py` key gets a test in `test_config.py` using the `isolated_config` fixture.
- Every new pure-logic function (path derivation, regex, threshold comparison) gets a test.
- SSH and Ollama calls are never made in tests — use `FakeSSHClient` / `FakeLLMClient` from `conftest.py`.

## Shared fixtures (`conftest.py`)

| Fixture | What it provides |
|---|---|
| `pueo_dirs` | `PueoDirectories` backed by `tmp_path`; sets `PUEO_*` env vars so `paths.get_dirs()` and `config.py` defaults point to temp dirs — prevents writes to `~/Library/Application Support/Pueo/`. |
| `isolated_config` | Writable temp `config.yaml` path; includes `pueo_dirs` so path defaults are also isolated; reloads all agent modules after the test to prevent state leakage. |
| `fake_ssh_client` | `FakeSSHClient` pre-loaded with a minimal valid `configuration.yaml` and standard command results. |
| `fake_llm_client` | `FakeLLMClient` returning a valid `DiagnosticsReport(is_valid=True)` JSON response. |

Class-scoped fixtures (defined as `@pytest.fixture` methods inside a test class) stay with their class — do not move them to `conftest.py` unless they are used across multiple test files.

## Config isolation
`config.py` loads at import time using the `PUEO_CONFIG` env var. Tests that exercise config loading use the `isolated_config` fixture from `conftest.py`, which:
1. Points `PUEO_CONFIG` at a temp file
2. Calls `importlib.reload()` so the module re-reads the env var
3. Reloads all dependent agent modules after the test to prevent state leakage

## What is not tested here
- SSH transport layer (`asyncssh` calls) — needs integration test against real HA
- Live Ollama inference — needs integration test against local Ollama

## Three-tier structure

### Tier 1 — Unit tests (`tests/`) — runs on every CI push
```bash
pytest --cov=./ --cov-fail-under=90 --ignore=tests/integration
```
No external services. All SSH and Ollama calls use fakes. 90% coverage gate enforced.

### Tier 2 — Seam tests (`tests/integration/`, no Ollama) — run locally on demand
```bash
pytest tests/integration/ -m "not live_ha and not ollama" -v
```
Verify cross-module state flows (manager functions write SQLite → dashboard reads it) and
code paths that are 0% covered in the unit suite. No external services needed.

### Tier 3 — Eval tests (`tests/integration/test_evals.py`, real Ollama) — run locally on demand
```bash
pytest tests/integration/ -m ollama -v
```
Each scenario in `evals/scenarios/` runs through real Ollama inference with
`FakeToolExecutor` intercepting tool calls. Tests verify safety (apply_fix ≤ 1×) and
outcome accuracy. Each scenario takes 5–120 seconds — expect a full run to take several
minutes.

Skipped automatically when Ollama is not reachable. Start Ollama first: `ollama serve`.

**To run seam tests and evals together in one command:**
```bash
pytest tests/integration/ -m "not live_ha" -v
```

**For the full scored report with baseline delta comparison, use the standalone harness:**
```bash
python evals/run_evals.py
python evals/run_evals.py --scenario 01    # single scenario by name fragment
python evals/run_evals.py --save-baseline  # overwrite baseline.json
```

### Live-HA smoke tests (`tests/integration/test_live_ha.py`)
```bash
HA_HOST=homeassistant.local pytest tests/integration/ -m live_ha -v
```
Skipped unless `HA_HOST` is set. Read-only SSH commands against a real HA instance.

**Nothing in `tests/integration/` is ever run on GitHub CI.**
