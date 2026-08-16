# <img src="web/static/nav_32.png" alt="pueo" height="32" valign="middle"> pueo

[![CI](https://github.com/AndysWorth/pueo/actions/workflows/test.yml/badge.svg)](https://github.com/AndysWorth/pueo/actions/workflows/test.yml)
[![codecov](https://codecov.io/gh/AndysWorth/pueo/graph/badge.svg)](https://codecov.io/gh/AndysWorth/pueo)

A vigilant, self-healing agentic AI system designed to monitor, maintain, and repair Home Assistant instances. 

`pueo` runs entirely on-device — all inference is local via Ollama, with zero cloud API calls during active monitoring or repair cycles.

---

## 🚀 Core Features

*   **Vigilant Monitoring:** Streams live HA logs via `ha core logs --follow` over SSH and triages entries with a local AI model.
*   **Automated Diagnostics:** Fetches and analyses `configuration.yaml` for syntax errors, deprecated keys, and missing required blocks.
*   **Self-Healing Actions:** Sandbox-tests proposed fixes before writing to production; always creates a native HA backup snapshot first.
*   **Active Dashboard:** Review and approve pending repair actions in-browser; real-time loop health, event timeline, resource gauges, and configuration editor served at `http://127.0.0.1:8080`.
*   **Ask Pueo:** A Chat tab in the dashboard lets you talk directly to the agent — query live HA state, store persistent notes that survive restarts, and extend Pueo's capabilities by proposing new tools through a sandboxed code review flow.
*   **Local RAG Knowledge Base:** HA breaking-change release notes, HACS component changelogs, and installed integration docs are embedded locally via ChromaDB and `nomic-embed-text`. The agent queries this knowledge automatically during repair cycles — no internet access required.
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

`setup.sh` is idempotent — safe to re-run at any time. It installs dependencies, detects your hardware and recommends an Ollama model, generates an SSH key if needed, writes `config.yaml`, and optionally installs Pueo as a macOS launchd service. For a detailed walkthrough of every prompt — credentials to gather, LLM provider options, autonomy levels, notifier setup, and NetAlertX configuration — see **[docs/setup-guide.md](docs/setup-guide.md)**.

Run `./setup.sh --clean` to wipe all generated files and start from scratch. A reference template is in `config.yaml.default`.

### 3. Running the Agent

#### Starting Pueo
```bash
pueo
```

`setup.sh` installs a `pueo` command at `/usr/local/bin/pueo`. This is the recommended way
to start the supervisor — it resolves its own path (works from anywhere, no `cd` required),
checks for an already-running instance, and launches in the background so your terminal stays
free. Logs go to `pueo.log` in the project directory.

```bash
tail -f pueo.log   # follow the live log
```

If the symlink install failed (check `setup.sh` output), you can start manually:

```bash
cd /path/to/pueo
source .venv/bin/activate
python main.py  # equivalent to: pueo
```

#### What the supervisor starts

`pueo` (no flags) is the default supervisor mode. It starts all monitoring loops
(HA log tail, resource polling, update checks, notification polling, NetAlertX) and the
dashboard in a single supervised process. The dashboard is available at
`http://127.0.0.1:8080`. Crashed loops restart automatically with exponential backoff.

`setup.sh` can install Pueo as a macOS launchd service (auto-start at login, auto-restart
on crash) — choose the option when prompted, or run `pueo --mode install-service`
afterwards. Once installed, use `stop-service` / `start-service` / `restart-service` to
control the daemon without touching `launchctl` directly.

#### Individual modes
```bash
# Daemons (single-loop, no dashboard)
pueo --mode monitor             # live SSH log tail with AI triage
pueo --mode dashboard           # web dashboard only (passive — no loops)

# One-shot diagnostics
pueo --mode diagnose            # config fetch and analysis
pueo --mode advanced            # diagnose + SQLite memory + backup triggering
pueo --mode repair              # full sandbox-test-then-atomic-swap repair cycle
pueo --mode netalertx-diagnose  # NetAlertX health check and optional heal
pueo --mode update-check        # check for available HA Core/OS/add-on updates
pueo --mode notifications       # triage HA persistent notifications
pueo --mode backup-status       # print backup inventory table
pueo --mode audit               # self-diagnostics gap report (saved to audits/)

# Setup and maintenance
pueo --mode netalertx-setup     # install and configure NetAlertX on HA
pueo --mode netalertx           # monitor NetAlertX logs continuously (daemon)
pueo --mode rag-refresh         # refresh the local RAG knowledge base (see below)
pueo --mode install-service     # install as macOS launchd service
pueo --mode stop-service        # stop the launchd service (stays stopped until start-service)
pueo --mode start-service       # re-enable and start the launchd service
pueo --mode restart-service     # bounce the service; launchd KeepAlive restarts it immediately
```

> **One-shot modes and the dashboard:** The one-shot diagnostic modes (`diagnose`, `repair`, `update-check`, `notifications`, `netalertx-diagnose`, `backup-status`, `audit`) are designed to run while Pueo is already running normally. Any approval cards they generate are picked up and displayed by the already-running dashboard in real time. If Pueo is not running when you fire a one-shot mode, the cards are written to the watch directory but won't appear in the dashboard until Pueo starts.

Pass `--config /path/to/config.yaml` if your config file is not in the project directory:
```bash
pueo --mode diagnose --config /path/to/config.yaml
pueo start --config /path/to/config.yaml   # supervisor with custom config, daemonized
```

---

## 📚 Local RAG Knowledge Base

Pueo maintains a local vector database (ChromaDB) seeded with context the repair agent can
query during active sessions — no internet access needed during monitoring or repair cycles.

**What gets embedded:**
- **HA Core release notes** — the breaking-changes section from each version's GitHub release,
  so the agent can flag when a config key was deprecated or a service call was renamed
- **HACS component changelogs** — fetched for each HACS integration installed on your HA instance
- **HA integration docs** — official documentation pages for your active integrations, scraped
  from the Home Assistant docs site

**How the agent uses it:**
The `query_knowledge` tool is registered alongside the repair tools. During a fix cycle the
model decides when to call it — surfacing relevant context only when it judges it useful,
rather than prepending every prompt with the full knowledge base.

**Refreshing the knowledge base:**
`setup.sh` installs a weekly `launchd` job that runs `--mode rag-refresh` automatically.
To refresh on demand:

```bash
pueo --mode rag-refresh
```

Embedded data is stored in `chromadb/` in the project directory. The embeddings use
`nomic-embed-text` running locally via Ollama — zero WAN traffic after the initial scrape.

---

## 💬 Ask Pueo

The **Chat** tab in the dashboard (`http://127.0.0.1:8080/chat`) lets you talk directly
to the agent between incidents. It uses the same `AgentLoop` that drives reactive repair
sessions — same tool registry, same safety gates — with a conversational system prompt and
a `finish_chat` termination signal instead of `finish_repair`.

**What you can do:**
- **Query live HA state** — "What's the disk usage on HA right now?" triggers
  `run_ha_command` and returns a plain-English answer
- **Store persistent notes** — "Remember that the NAS is at 192.168.1.100" saves a memory
  entry that survives restarts and is recalled automatically in future sessions
- **Browse session history** — past conversations are listed in the left panel; click any to
  resume it

**Extending Pueo with new tools** (opt-in):
Set `CHAT_ALLOW_TOOL_REGISTRATION = true` in `config.yaml` to enable the code-sandbox flow.
With it enabled, you can ask Pueo to write a new tool, review the proposed code in an
approval card, and — once approved — have the tool registered and callable in the next
session. Tools are stored in `user_tools/` and loaded automatically on startup.

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

## 🌺 Naming & Cultural Attribution

This project is named after the endemic Hawaiian short-eared owl — a traditional guardian spirit and namesake of the structural beams that hold a Hawaiian home together. See [NAMING.md](NAMING.md) for the full cultural attribution and non-commercialization commitment.

---

## 📄 License

Distributed under the **GNU Lesser General Public License v3.0 (LGPL-3.0)**. Downstream modifications must remain entirely free and open-source. Commercial corporate branding or exclusive trademark enforcement of this code under the name "Pueo" is strictly prohibited under our cultural attribution guidelines.
