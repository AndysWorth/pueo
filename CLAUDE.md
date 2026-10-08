# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Pueo** is a local, privacy-first agentic AI system that monitors and self-heals a Home Assistant (HA) instance. It runs on macOS Apple Silicon or in Docker. The LLM inference engine is configurable (`LLM_PROVIDER`): local Ollama only (default, no WAN for inference), Anthropic Claude API, both (Ollama for autonomous cycles + Claude available for approved escalation), or any OpenAI-compatible server (LM Studio, mlx-lm, vLLM, llama-swap — `openai_compat`). All HA communication goes over SSH/SFTP. Transparency is a first-class design goal: users should be able to see what Pueo has done (event timeline, repair episodes) and what it is currently thinking (live tool-call trace in Chat).

Source code lives in `pueo/`.

## Project Type: Team/Library

This is a multi-person project. The following procedure variations from `~/.claude/CLAUDE.md` are active:

- **Code review:** At least one approval required before merging; the author cannot merge their own PR.
- **Dependency changes:** Changes to `requirements*.txt` warrant a second reviewer; call out transitive dependency risk in the PR description.
- **Breaking changes:** Add a deprecation warning for at least one version, bump the semver major version, and include a migration guide in the changelog.
- **Branch lifespan:** Rebase onto `main` daily for branches open more than one day.
- **Branch strategy:** Feature branches off `main` (no `develop` branch); hotfix branches off the relevant release tag for production bugs.
- **Merge strategy:** Squash merge to keep `main` history clean.
- **Migrations:** Test against a real local copy of `ha_agent_state.db` and flag migrations explicitly in the PR; no staging environment exists.
- **Rollback:** Document the rollback plan (revert commit + migration version) in the PR description for migrations and production config writes.

## Commands

Run from the `pueo/` directory:

```bash
pip install -r requirements-dev.txt  # includes runtime deps + dev/test tooling

# Primary entry point (macOS)
pueo                          # start supervisor (all loops + dashboard)
python main.py                # same, without the background/PID wrapper
pueo-py                       # pyproject entry point (main:main), no wrapper

# Docker equivalents
docker compose up -d           # start supervisor in Docker
docker compose logs -f pueo    # follow live log
docker exec pueo-agent python main.py --mode rag-refresh   # RAG refresh in Docker

# Individual agents (for debugging; live under agents/)
python agents/ha_agent_core.py            # Read-only: SSH fetch + Ollama diagnosis
python agents/ha_agent_advanced.py        # + SQLite memory + backup triggering
python agents/ha_agent_sandbox_engine.py  # Full: sandbox-test-then-atomic-swap repair
python agents/ha_log_monitor.py           # Continuous: live SSH log tail + AI triage

# Tests
pytest
pytest tests/test_config.py::TestConfigDefaults::test_loads_values_from_yaml  # single test example

# Code quality (CI enforces all of these)
black --check .
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
mypy --ignore-missing-imports .
bandit -r . -x ./tests,./.venv
```

`setup.sh` installs `pueo` as a bash script in `$PATH` (venv activation, PID wrapper). The `pyproject.toml` entry point is `pueo-py` (runs `main:main` directly, no wrapper). Use `pueo` for normal operation; use `python main.py` or `pueo-py` for direct debugging.

## Architecture

### Primary runtime architecture

- **`main.py`** — single entry point with 24 `--mode` values. Default: `supervisor`. All agent module imports are deferred inside mode branches (not top-level) so `PUEO_CONFIG` is set before any module imports `config.py`.
- **`LoopSupervisor`** (`utils/agent/supervisor.py`) — manages all background tasks with exception catching and exponential-backoff restart (2s → 5-min cap). The dashboard ASGI app runs alongside all supervisor tasks.
- **`AgentLoop`** (`utils/agent/agent_loop.py`) — the universal reasoning engine. Used by all 7 agent pipelines and by the Chat endpoint. All significant Pueo decisions go through an `AgentLoop` session.

For a compact orientation map (file responsibilities, utils directory, interfaces, quick commands), see `docs/for-agents.md`.

### Agent files (`agents/`)

| Agent | Responsibility | Supervisor task(s) | `--mode` |
|---|---|---|---|
| `ha_agent_core.py` | Config fetch + LLM diagnosis (read-only) | — | `diagnose` |
| `ha_agent_advanced.py` | Diagnosis + SQLite memory (`ha_agent_state.db`) + backup triggering | — | `advanced` |
| `ha_agent_sandbox_engine.py` | Full repair: sandbox-test-then-atomic-swap | repair pipeline (triggered by log monitor) | `repair` |
| `ha_log_monitor.py` | Live SSH log tail with AI triage; also houses `poll_for_updates`, `poll_for_notifications`, `poll_for_repairs` | `ha_log_monitor`, `ha_log_monitor_supervisor`, `repair_poll`, `notification_poll`, `update_check` | `monitor` |
| `ha_lovelace_monitor.py` | Dashboard entity health + benign suppression | `lovelace_poll` | — |
| `ha_notification_manager.py` | Persistent notification triage and IP enrichment | `notification_poll` (for processing) | `notifications` |
| `ha_update_manager.py` | Update detection, breaking-change analysis, orchestration | `update_check` | `update-check` |

Agent scripts in `agents/` are runnable directly for debugging; under `supervisor` mode, all 7 run as coordinated background tasks managed by `LoopSupervisor`.

**Log monitor detail:** runs `ha core logs --follow` over SSH to stream live HA logs from the supervisor journal (modern HA does not reliably write to `/config/home-assistant.log`). Two-layer triage: fast regex pre-filter (`CRITICAL_LOG_PATTERN`) then Ollama `LogEvaluation` with `confidence_score > 0.7` threshold. High-confidence actionable errors trigger the repair pipeline. Reconnects automatically on stream failure.

**Repair pipeline (`ha_agent_sandbox_engine.py`):** When Ollama returns `is_valid=False` with a `recommended_fix_yaml`:
1. Run `validate_proposed_fix()` — abort if the proposed YAML removes critical keys or is suspiciously large
2. `AutonomyGate.require_approval()` — if CRITICAL severity or current autonomy level requires human approval, notify and wait
3. Trigger HA backup (mandatory)
4. Write proposed fix to `/config/.agent_sandbox/configuration.yaml` over SFTP
5. Temporarily swap it into `/config/configuration.yaml`, run `ha core check`, immediately revert (always, via `finally`)
6. Only if the sandbox check passes: atomically write to production and call `ha core restart`

## Key Patterns

@docs/key-patterns.md

## Configuration

`config.py` loads `config.yaml` at import time and exposes all settings as typed module-level constants with fallback defaults. Agent scripts are run-able directly without a `config.yaml` (defaults kick in); `main.py` is needed to point at a non-default config path. When no `config.yaml` is present, path defaults resolve to platform directories via `paths.get_dirs()` (e.g. `~/Library/Application Support/Pueo/ha_agent_state.db` on macOS). Pass `--config /path/to/config.yaml` or set `PUEO_CONFIG` to override the config file location.

`config.yaml` is gitignored. `config.yaml.default` is the committed reference template. Run `setup.sh` to generate `config.yaml` interactively.

## Deployment

`setup.sh` supports three deployment modes — `macos`, `docker`, or `both` — chosen interactively at setup time. Both macOS and Docker are equally supported; neither is second-class. For Docker, `setup.sh` generates `docker-compose.yml` with the SSH key volume mount (`<host-key-path>:/root/.ssh/id_ed25519:ro`) and writes `config/config.yaml` (the bind-mount source). Run `./setup.sh` and choose the mode; no manual editing of `docker-compose.yml` is needed. `docker-compose.yml.example` is the committed reference template (with placeholders); `docker-compose.yml` is gitignored.

`setup.sh --clean` removes all state (DB, caches, launchd plists, CLI symlink, config.yaml). `setup.sh --reset` does the same but preserves `config.yaml` for a clean reinstall without re-answering questions.

`Dockerfile` + `docker-compose.yml` use `network_mode: host` for ARP/raw socket access. The container uses five volumes: `/config` (bind-mount of `./config/`, read-only — place `config.yaml` here), `/data` (`pueo-data` named volume — backups, ChromaDB), `/state` (`pueo-state` named volume — SQLite DB, HITL cards, tools), `/cache` (`pueo-cache` named volume — scraped knowledge), `/logs` (`pueo-logs` named volume — log files). The `Dockerfile` sets `PUEO_CONFIG_DIR=/config` and equivalent `PUEO_*` env vars; `paths.py` picks these up automatically so no code path references `/app`. `main.py` is the unified entry point; default mode is `supervisor` (same as running `python main.py` directly).

## Testing

When adding or modifying any feature, add corresponding tests in the same session — do not defer them. Tests live in `tests/`, grouped by subsystem. Three tiers:

| Tier | Location | Needs | When to run |
|---|---|---|---|
| Unit | `tests/` | Nothing | Every push (CI enforced) |
| Seam | `tests/integration/` (non-ollama) | Nothing | Locally, on demand |
| Eval | `tests/integration/test_evals.py` | Ollama running | Locally, on demand |

**Rules:**
- Every new Pydantic schema gets three tests: valid construction, invalid/missing fields, JSON round-trip
- Every new `config.py` key gets a test in `TestConfigDefaults` using the `isolated_config` fixture
- Every new pure-logic function (path derivation, regex, threshold comparison) gets a test
- SSH and Ollama calls are never mocked in the unit suite — those are integration/eval concerns
- Use `/project:write-tests <target>` to generate tests for a specific function or module

See `tests/CLAUDE.md` for full commands and the three-tier structure.

The Stop hook (`/.claude/hooks/stop.sh`) will remind you at session end if Python files were modified without touching `tests/`.

## CI

`.github/workflows/test.yml` runs on Python 3.12, 3.13, 3.14 against `main`. Gates: `black`, `flake8` (errors only), `mypy`, `bandit`, `pytest --cov --cov-fail-under=90 --ignore=tests/integration`.

`tests/integration/` is never run on GitHub — it contains seam tests (cross-module state flows) and eval tests (real Ollama inference) that require local services.

## Development Procedure

Every code change follows this procedure in order. Never commit directly to `main`.

### Before writing any code
1. `git checkout main && git pull` — start from a clean base
2. `git remote prune origin` — remove stale remote-tracking refs
3. `git branch --merged | grep -v '^\*\|main' | xargs git branch -d 2>/dev/null` — prune merged local branches
4. **Every change needs a GitHub issue.** Check that an issue exists for this work. If not, create one with `gh issue create` before touching any files. No issue = no branch. Reference the issue number in the branch name and in every commit message.
5. **Plan non-trivial changes first.** Trivial = a few files within the same module; implement directly. Non-trivial = crosses module boundaries or touches many files; agree on the approach before touching any files.
6. `git checkout -b feat/<issue-number>-<slug>` — branch created before the first edit, named with the issue number. If a change was already made on `main` without branching, do this retroactively — uncommitted changes carry over.

### During coding
7. **Write/update tests in the same session** — not deferred. Do not commit logic changes without corresponding test changes.
8. **Update all related files** and report explicitly when done:
   - Config key added → `config.py`, `config.yaml.default`, and `setup.sh`
   - Architecture change → add/update a decision record in `docs/decisions/`
   - Public interface changed → update this file if the pattern is documented here
   - Dependency added/changed → update `requirements.txt` or `requirements-dev.txt`
9. **Migrations and schema changes** — flag separately from code changes. Test against a real local copy of `ha_agent_state.db`. Document the rollback path (which migration version to revert to) before proceeding.
10. **Security review** — invoke `/security-review` when the change meaningfully touches SSH transport, external HTTP calls, credential handling, or production file writes.

### Before committing
11. `git diff --staged` — self-review the diff; catch noise, debug artifacts, unintended changes
12. **Exercise the change** — run the app and manually verify the affected behavior. Tests confirm code correctness; only running it confirms the feature works. Use `/run` to launch the app. If the change touches the dashboard, SSH flows, or LLM interactions, test those paths directly. If it truly cannot be exercised locally (e.g. requires live HA state that isn't available), say so explicitly rather than skipping silently.
13. Commit atomically — one logical concern per commit; message explains *why*, not *what*; include `Closes #N` or `Refs #N`.

### Before opening a PR
14. Run the full CI gate locally — all must pass:
    ```bash
    black --check .
    flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
    mypy --ignore-missing-imports .
    bandit -r . -x ./tests,./.venv
    pytest --cov --cov-fail-under=90 --ignore=tests/integration
    ```
15. **Rollback planning** — for migrations or config writes to production, note the rollback path in the PR description (revert commit + migration version).
16. CI passing = done, open the PR. `gh pr create` — description focuses on *why*, not *what*; reference the issue (`Closes #N`); include rollback note if step 15 applies.

### After merge
17. Repeat steps 1–3 to clean up.

## Roadmap

@docs/roadmap-summary.md

## MCP Servers

`.mcp.json` configures a Home Assistant MCP server for use during development, giving Claude Code direct access to live HA state and entities. Requires `mcp-homeassistant` installed (`uvx mcp-homeassistant`) and `HA_TOKEN` set in the environment. See `.mcp.json` for the full config shape.

## Knowledge Base

`pueo-kb` ([AndysWorth/pueo-kb](https://github.com/AndysWorth/pueo-kb)) is the
federated runbook library. Pueo pulls from it at every RAG refresh
(`--mode rag-refresh`) via `utils/knowledge/kb_ingester.py` and contributes
validated and candidate runbooks plus open gaps back via
`utils/knowledge/kb_contributor.py`. Approach text, titles, and trigger patterns
are anonymized before contribution. Seed runbooks in `prompts/seed_*.md` are the
human-curated baseline.

Runbook lifecycle (ADR 038): seed → candidate (distilled post-session) → validated
(auto after `RUNBOOK_VALIDATE_MIN_SUCCESSES` successes, default 3) → seed (human
promotes). Gaps move to `knowledge_gaps` (never retrieved). Manage via the
`/runbooks` dashboard tab (`development_mode: true` required).

Federation (ADR 039): contributed runbooks carry a `kb_id` (`rb_` + sha256 of
signature). The pueo-kb CI (`build-manifest.yml`) aggregates evidence files from
all instances and sets each entry's `state` in `MANIFEST.json`. Ingester authority:
pueo-kb validated/seed → 0.70 `[COMMUNITY RUNBOOK]`; pueo-kb candidate → 0.45
`[COMMUNITY CANDIDATE]`; flagged entries are skipped. The ingester re-embeds an
entry when its `state` changes (ingest key encodes `id:sha256:state`).

Configured via `PUEO_KB_REPO` in `config.yaml` (default `"AndysWorth/pueo-kb"`).

## Work Tracking

New bugs, enhancements, and feature work are tracked in [GitHub Issues](https://github.com/AndysWorth/pueo/issues). Use labels `bug`, `enhancement`, `security`, `ux`, or `discussion`.

When starting work on an issue, reference it in the branch name (`feat/<slug>`) and in commit messages (`Closes #N`). Complex features that need upfront design get a spec in `docs/plan/` — link it from the issue before implementing.

The initial build-out phases (items 1–STOR-6) are archived in `docs/implementation-plan.md` as a historical record.

## Design Decisions

Rationale for key architectural choices is in `docs/decisions/`:

@docs/decisions/000-index.md
