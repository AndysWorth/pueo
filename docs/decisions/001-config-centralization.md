# ADR 001 — Centralized configuration in config.py

## Status
Accepted

## Context
Each agent script originally contained a `# CONFIGURATION BOUNDARY` block with identical hardcoded constants (`HA_HOST`, `HA_USER`, `SSH_KEY_PATH`, etc.). Any change to a setting required editing four files, and the values in `config.yaml` (the intended runtime config) were ignored.

## Decision
All settings live in `config.py` as typed module-level constants with fallback defaults. Agent scripts import from it; they never declare their own constants. `config.yaml` is the source of runtime values; `config.py` loads it at import time.

## Consequences
- Adding a setting requires changes in exactly three places: `config.py`, `config.yaml.default`, and `setup.sh`.
- `config.py` loads at module import time, which means the `PUEO_CONFIG` env var must be set before any agent module is imported (see ADR 002 for how `main.py` handles this).
- Agent scripts remain directly runnable (`python ha_agent_core.py`) because `config.py` falls back to defaults when no `config.yaml` is present.
