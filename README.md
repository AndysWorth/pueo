# <img src="web/static/nav_32.png" alt="pueo" height="32" valign="middle"> pueo

[![CI](https://github.com/AndysWorth/pueo/actions/workflows/test.yml/badge.svg)](https://github.com/AndysWorth/pueo/actions/workflows/test.yml)
[![codecov](https://codecov.io/gh/AndysWorth/pueo/graph/badge.svg)](https://codecov.io/gh/AndysWorth/pueo)

A vigilant, self-healing agentic AI system designed to monitor, maintain, and repair Home Assistant instances. 

`pueo` runs entirely on-device — all inference is local via Ollama, with zero cloud API calls during active monitoring or repair cycles.

---

## 🌺 Naming & Cultural Attribution

This project is named **Pueo** (the endemic Hawaiian short-eared owl, pronounced *poo-eh-oh*). 

In Hawaiian culture, the pueo is traditionally revered as an **ʻaumākua**—an ancestral guardian spirit that watches over, guides, and protects a home and its family. Furthermore, the word *pueo* historically links to the **ʻaho pueo**, the main structural cross-beams that physically hold a traditional house together.

### Why this name?
We chose this name with deep humility and respect for the Hawaiian language (`ʻōlelo Hawaiʻi`) and culture. This AI agent's architecture directly mirrors the protective, vigilant, and self-healing traits of the pueo. It serves as a persistent digital guardian, ensuring your home's automation infrastructure remains stable and resilient.

### Commitment to Non-Commercialization
In alignment with the spirit of open-source and out of respect for Native Hawaiian traditional knowledge principles, **this software is 100% free, non-commercial, and open-source**. 
* The maintainers strictly prohibit the commercialization, packaging, or corporate trademarking of this repository under the name "Pueo".
* To learn more about the biological preservation of this endangered endemic bird, please visit the [Honolulu Zoo Society Pueo Profile](https://honoluluzoo.org).

---

## 🚀 Core Features

*   **Vigilant Monitoring:** Streams live HA logs via `ha core logs --follow` over SSH and triages entries with a local AI model.
*   **Automated Diagnostics:** Fetches and analyses `configuration.yaml` for syntax errors, deprecated keys, and missing required blocks.
*   **Self-Healing Actions:** Sandbox-tests proposed fixes before writing to production; always creates a native HA backup snapshot first.
*   **Active Dashboard:** Approves and executes HITL repair actions in-browser; real-time loop health, event timeline, resource gauges, and configuration editor served at `http://127.0.0.1:8099`.
*   **Privacy-First:** All inference runs on a local Ollama instance — zero cloud API calls during active monitoring or repair cycles.

---

## 🛠️ Quick Start

### 1. Prerequisites
*   Home Assistant OS with the **Terminal & SSH** App installed (`Settings → Apps → Terminal & SSH`).
    Set `port: 22`, add your public key under `authorized_keys`, and start the App.
*   [Ollama](https://ollama.com) installed and running locally (macOS Apple Silicon recommended).
*   Python 3.14 available — either via [Homebrew](https://brew.sh) (`brew install python@3.14`) or [pyenv](https://github.com/pyenv/pyenv).

> **Passphrase-protected SSH keys:** Pueo uses `asyncssh` and cannot prompt for a passphrase interactively. Add your key to the macOS keychain once before running Pueo:
> ```bash
> ssh-add --apple-use-keychain ~/.ssh/id_ed25519
> ```
> `setup.sh` will remind you if the agent is not active or the key is not loaded.

### 2. Installation & Configuration
Clone the repository and run the setup script:
```bash
git clone https://github.com/AndysWorth/pueo
cd pueo
./setup.sh
```

`setup.sh` is idempotent — safe to re-run at any time. It will:
- Locate Python 3.14 (Homebrew or pyenv) and create a `.venv`
- Check that Ollama is installed and running, and pull `qwen2.5-coder:7b` if missing
- Generate an SSH key if none exists and show instructions for adding it to the Terminal & SSH App
- Check that the SSH agent is running and the key is loaded
- Prompt for your HA hostname, SSH settings, and agent preferences, then write `config.yaml`
- Connect to HA over SSH, detect the HA version, and warn if the log file is missing
- Run `./setup.sh --clean` to wipe all generated files and start from scratch

A reference template for `config.yaml` is available in `config.yaml.default`.

### 3. Running the Agent

#### Supervisor (recommended — starts everything)
```bash
source .venv/bin/activate
python main.py
```

This is the default mode. It starts all monitoring loops (HA log tail, resource polling,
update checks, notification polling, NetAlertX) and the HITL dashboard in a single
supervised process. The dashboard is available at `http://127.0.0.1:8099`. Crashed loops
restart automatically with exponential backoff.

`setup.sh` can install Pueo as a macOS launchd service (auto-start at login, auto-restart
on crash) — choose the option when prompted, or run `python main.py --mode install-service`
afterwards.

#### Individual modes
```bash
# Daemons (single-loop, no dashboard)
python main.py --mode monitor             # live SSH log tail with AI triage
python main.py --mode dashboard           # HITL web dashboard only (passive — no loops)

# One-shot diagnostics
python main.py --mode diagnose            # config fetch and analysis
python main.py --mode advanced            # diagnose + SQLite memory + backup triggering
python main.py --mode repair              # full sandbox-test-then-atomic-swap repair cycle
python main.py --mode netalertx-diagnose  # NetAlertX health check and optional heal
python main.py --mode update-check        # check for available HA Core/OS/add-on updates
python main.py --mode notifications       # triage HA persistent notifications
python main.py --mode backup-status       # print backup inventory table
python main.py --mode audit               # self-diagnostics gap report (saved to audits/)

# Setup and maintenance
python main.py --mode netalertx-setup     # install and configure NetAlertX on HA
python main.py --mode netalertx           # monitor NetAlertX logs continuously (daemon)
python main.py --mode rag-refresh         # embed cached HA release notes + HACS changelogs
python main.py --mode install-service     # install as macOS launchd service
```

Pass `--config /path/to/config.yaml` if your config file is not in the project directory.

---

## Claude Code configuration

This repo includes a `.claude/settings.json` that configures Claude Code's permission system for Pueo development. It auto-approves a set of shell commands so Claude doesn't prompt for confirmation on routine diagnostic operations:

| Command pattern | Why |
|---|---|
| `ssh -i *` | SSH into HA over the configured key |
| `curl -s *` / `curl --silent *` | NetAlertX and HA REST API calls |
| `ping -c *` | Connectivity checks |
| `nc -z *` | Port reachability checks |
| `nmap *` | Network scanning during NetAlertX diagnostics |

**If you clone this repo and use Claude Code**, be aware that these commands will run without a confirmation prompt. `nmap` in particular is a network scanner — only appropriate on networks you own or have explicit permission to scan. Review `.claude/settings.json` and remove any entries you're not comfortable auto-approving before starting a Claude Code session.

---

## Testing

Tests are organized in three tiers. Run from the `pueo/` directory with the virtualenv active.

### Tier 1 — Unit tests (no external services)

```bash
pytest --cov=./ --cov-fail-under=90 --ignore=tests/integration
```

Uses `FakeSSHClient` and `FakeLLMClient` throughout — no SSH connection or Ollama required. This is the same command GitHub CI runs on every push and PR.

### Tier 2 — Seam tests (no external services)

```bash
pytest tests/integration/ -m "not live_ha and not ollama" -v
```

Verifies cross-module state flows (e.g. manager functions write SQLite → dashboard reads it). No external services needed.

### Tier 3 — Eval tests (requires local Ollama)

Each scenario in `evals/scenarios/` runs through real Ollama inference. Start Ollama first (`ollama serve`), then:

```bash
pytest tests/integration/ -m ollama -v
```

Tests are skipped automatically if Ollama is not reachable. Expect a full run to take several minutes.

For the scored report with baseline delta comparison, use the standalone harness:

```bash
python evals/run_evals.py
python evals/run_evals.py --scenario 01    # single scenario by name fragment
python evals/run_evals.py --save-baseline  # overwrite baseline.json
```

To run seam tests and evals together:

```bash
pytest tests/integration/ -m "not live_ha" -v
```

### Live HA smoke tests (requires a real HA instance)

```bash
HA_HOST=homeassistant.local pytest tests/integration/ -m live_ha -v
```

Skipped unless `HA_HOST` is set. Read-only SSH commands against a real HA instance.

### GitHub CI

| Workflow | Trigger | What runs |
|---|---|---|
| `test.yml` | Every push and PR to `main` | Tier 1 unit tests on Python 3.12, 3.13, 3.14; black, flake8, mypy, bandit, Codecov upload |
| `evals.yml` | Manual (`workflow_dispatch` only) | Tier 3 evals via `evals/run_evals.py`; never blocks merges |

Tier 2 and Tier 3 tests are never run automatically on GitHub — run them locally on demand.

---

## 📄 License

Distributed under the **GNU Lesser General Public License v3.0 (LGPL-3.0)**. Downstream modifications must remain entirely free and open-source. Commercial corporate branding or exclusive trademark enforcement of this code under the name "Pueo" is strictly prohibited under our cultural attribution guidelines.
