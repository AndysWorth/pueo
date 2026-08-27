# Pueo Setup Guide

Run `./setup.sh` from the `pueo/` directory. This guide walks through every prompt in the order setup.sh asks for it, so you can have everything ready without stopping mid-install.

```bash
git clone https://github.com/AndysWorth/pueo
cd pueo
./setup.sh
```

`setup.sh` is idempotent — safe to re-run at any time. If you want to start completely from scratch, run `./setup.sh --clean` first (removes `.venv`, `config.yaml`, and all platform-directory state: DB, HITL cards, caches, logs).

---

## Section 0 — Deployment Mode

**One prompt.**

```
How will you run Pueo?
  1) native  — macOS (launchd, ~/Library/* dirs)
  2) docker  — Docker container (docker-compose)
  3) both    — native + Docker side-by-side
[1/2/3, default: 1]:
```

| Mode | What setup.sh does |
|---|---|
| `native` | Creates `.venv`, installs Ollama model, writes `~/.config/pueo/config.yaml`, installs launchd service and RAG refresh job, symlinks `pueo` command |
| `docker` | Skips venv and launchd; writes `config/config.yaml` and generates `docker-compose.yml` with SSH key mount |
| `both` | Does everything: native config + infrastructure, plus Docker config and `docker-compose.yml` |

---

## Section 1 — Python

> **Docker mode: this section is skipped.** Python runs inside the container; you don't need a local venv.

**Automatic, no input needed (native/both).**

Setup detects Python 3.14 via Homebrew (`python3.14`) or pyenv, creates a `.venv`, and installs all dependencies from `requirements-dev.txt`. If Python 3.14 isn't found, setup exits with instructions.

**Prerequisites:**
- [Homebrew](https://brew.sh): `brew install python@3.14`
- Or [pyenv](https://github.com/pyenv/pyenv): setup installs `3.14` automatically if pyenv is present

---

## Section 2 — Ollama

> **Docker mode:** this section is skipped. Ollama must run on the host machine. Setup instead asks for the Ollama endpoint URL (default: `http://host.docker.internal:11434` for macOS Docker Desktop). No model pull is attempted.

**Automatic, then two prompts (native/both).**

Setup verifies that Ollama is installed and running, detects your hardware (chip and RAM), and recommends a model:

| RAM | Recommended model |
|---|---|
| ≥ 48 GB | `qwen2.5-coder:32b` |
| ≥ 20 GB | `qwen2.5-coder:14b` |
| < 20 GB | `qwen2.5-coder:7b` |

Setup pulls the recommended model plus `nomic-embed-text` (used for RAG embeddings) if they aren't already present. This can take several minutes on the first run.

**Prompt: Ollama model**
Press Enter to accept the hardware-matched default, or type a different model name. The chosen model is pulled immediately if not already installed.

**Prompt: Auto-select best model at startup?** (`true` / `false`, default: `false`)
When `true`, Pueo checks which `qwen2.5-coder` variants are installed at startup and picks the largest one that fits in your current RAM. Useful as you add or remove larger models over time.

Config keys: `ollama.model`, `ollama.model_auto`, `ollama.endpoint`

---

## Section 2.5 — LLM Provider

**Two prompts (more if cloud mode is chosen). All modes.**

**Prompt: LLM provider** (`local` / `cloud` / `both`, default: `local`)

| Choice  | Behaviour                                                                                                                   |
| ------- | --------------------------------------------------------------------------------------------------------------------------- |
| `local` | Ollama only — all inference stays on device; zero cloud API calls                                                           |
| `cloud` | Anthropic Claude as the primary model; all inference calls go to Anthropic                                                  |
| `both`  | Ollama for autonomous repair cycles; Claude available as an approved escalation when the local loop hits the tool-call cap or wall-clock timeout without finishing |

`both` mode preserves the local-first property during unattended repair — Claude is only invoked when a human approves a cloud-escalation card in the dashboard.

**If you choose `cloud` or `both`:**

**Prompt: Claude model** (default: `claude-sonnet-5`)
Press Enter to accept the default or type a different Anthropic model ID.

**ANTHROPIC_API_KEY** — Required for `cloud` and `both` modes. Pueo reads this exclusively from the environment — it is never written to `config.yaml`.

- **Native:** add to `~/.zshenv` and reload your shell:
  ```bash
  export ANTHROPIC_API_KEY=<your-key>
  ```
- **Docker:** set it in `docker-compose.yml`'s `environment:` section, or supply a `.env` file alongside `docker-compose.yml`:
  ```env
  ANTHROPIC_API_KEY=<your-key>
  ```

Setup warns you if the key is absent; Pueo will refuse to start in `cloud` or `both` mode until it is set.

Config keys: `llm.provider`, `cloud.model`

---

## Section 3 — SSH Key

**Automatic, with optional interactive steps. All modes.**

Setup checks for `~/.ssh/id_ed25519`. If it doesn't exist, it offers to generate one.

If you generate a key, setup prints the public key and gives instructions for adding it to HA:
1. In HA: `Settings → Apps → Terminal & SSH`
2. Paste the public key into the `authorized_keys` field
3. Set `port: 22` and click **Start**
4. Press Enter in setup to continue

**SSH agent check (native/both only)** — Pueo uses `asyncssh` and cannot prompt for a key passphrase interactively. If your key has a passphrase, add it to the macOS keychain once:

```bash
ssh-add --apple-use-keychain ~/.ssh/id_ed25519
```

**Docker note:** the SSH key is mounted read-only into the container at `/root/.ssh/id_ed25519`. setup.sh writes the correct volume mount into `docker-compose.yml` automatically based on the key path you specify.

---

## Section 4 — Configuration

**The main configuration prompts. Press Enter to accept each default. All modes.**

Config file destination:
- **native:** `~/.config/pueo/config.yaml`
- **docker:** `config/config.yaml` (the bind-mount source for `./config:/config:ro`)
- **both:** writes both; the Docker copy uses `host.docker.internal` as the Ollama endpoint

**Prompt: Home Assistant hostname or IP** (default: `homeassistant.local`)

Config key: `home_assistant.host`

---

**Prompt: SSH username** (default: `root`)

Config key: `home_assistant.user`

---

**Prompt: SSH private key path** (default: `~/.ssh/id_ed25519`)

Config key: `home_assistant.ssh_key_path`

---

**Prompt: HA long-lived access token**
Required for update monitoring and persistent notification polling. Leave blank to skip.

To create one: HA profile picture → **Security → Long-Lived Access Tokens → Create Token**.

Config key: `home_assistant.api_token`

---

**Prompt: Update check interval (hours, 0 = disabled)** (default: `6`)

Config key: `agent.update_check_interval_hours`

---

**Prompt: config.yaml path on HA host** (default: `/config/configuration.yaml`)

Config key: `home_assistant.config_path`

---

**Prompt: Ollama model** (default: hardware-matched recommendation)
Confirms or overrides the model chosen in Section 2.

Config key: `ollama.model`

---

**Prompt: Local SQLite database path**
- Native default: `~/Library/Application Support/Pueo/ha_agent_state.db`
- Docker default: `/state/ha_agent_state.db`

Config key: `agent.db_path`

---

**Prompt: Log confidence threshold (0–1)** (default: `0.7`)

Config key: `agent.log_confidence_threshold`

---

**Prompt: Self-healing enabled** (`true` / `false`, default: `true`)

Config key: `agent.self_healing_enabled`

---

**Prompt: Autonomy level** (`1`–`4`, default: `2`)

| Level | Name | Behaviour |
|---|---|---|
| `1` | Report only | Diagnoses and explains issues; never writes to HA |
| `2` | Suggest | Generates proposed fixes and sends them to the dashboard; you approve each one |
| `3` | Guided | Auto-executes LOW-severity fixes; approval for MEDIUM and CRITICAL |
| `4` | Autonomous | Auto-executes LOW and MEDIUM fixes; approval for CRITICAL only |

Config key: `agent.autonomy_level`

---

**Prompt: Dashboard port** (default: `8080`)

Config key: `agent.dashboard_port`

---

**Prompt: Allow chat agent to register new tools?** (`true` / `false`, default: `false`)

Config key: `agent.chat_allow_tool_registration`

---

**Prompt: Notifier type** (`file` / `ntfy` / `webhook`, default: `file`)

| Choice | Behaviour |
|---|---|
| `file` | Writes a JSON file to a local directory; approve by running `touch <hitl-dir>/<id>.approved` |
| `ntfy` | Sends a push notification to an ntfy topic |
| `webhook` | HTTP POST to any URL |

Config keys: `agent.notifier`, `agent.notify_url`, `agent.notify_watch_dir`

---

**SSH connectivity test — automatic.**
Setup connects to HA over SSH to verify credentials and detect the HA version.

---

**Prompt: Set up NetAlertX automatically when Pueo runs?** (`Y` / `n`)

**If yes — Prompt: Install on separate Docker machine instead of HA?** (`y` / `N`)

Config keys: `netalertx.setup_desired`, `netalertx.deploy_target`, `netalertx.docker_host`, `netalertx.docker_ssh_user`, `netalertx.docker_ssh_key_path`

---

**Prompt: NetAlertX API token** (blank = set after first install)

> **First-time setup: leave this blank.** Once NetAlertX is running: `NetAlertX web UI → Settings → Main Settings → API Key`. Then re-run `./setup.sh` or edit `config.yaml` directly.

Config key: `netalertx.api_token`

---

**MQTT credentials — two prompts.**

Config keys: `netalertx.mqtt_user`, `netalertx.mqtt_password`

---

## Section 4.5 — Docker Compose Setup

> **Native mode: this section is skipped.**

**Automatic (docker/both).**

After the config file is written, setup generates `docker-compose.yml` in the repo root with:
- SSH key volume mount: `- <your-key-path>:/root/.ssh/id_ed25519:ro`
- `restart: unless-stopped` restart policy
- `ANTHROPIC_API_KEY` comment block in the `environment:` section (uncommented if you chose `cloud` or `both` LLM mode)

**What to do next:**
```bash
docker compose up -d
docker compose logs -f pueo
```

The config file at `config/config.yaml` is already in place — no manual editing needed.

---

## Section 5 — NetAlertX

**Automatic, no input needed. All modes.**

Reports whether NetAlertX will be installed automatically on first start or is disabled.

---

## Section 6 — launchd Service

> **Docker mode: this section is skipped.** Restart policy is handled by Docker (`restart: unless-stopped`).

**One prompt (native/both).**

**Prompt: Install Pueo as a launchd service?** (`Y` / `n`)
When installed, Pueo starts automatically at login and restarts automatically on crash.

---

## Section 7 — RAG Knowledge-Base Refresh

> **Docker mode: this section is skipped.** Refresh the knowledge base manually:
> ```bash
> docker exec pueo-agent python main.py --mode rag-refresh
> ```
> Or add a cron job on the host to run it on a schedule.

**One prompt (native/both).**

**Prompt: Install weekly RAG refresh job?** (`Y` / `n`)
A weekly launchd job runs `--mode rag-refresh` every Sunday at 03:00.

---

## Section 8 — pueo Command

> **Docker mode: this section is skipped.**

**Automatic, no input needed (native/both).**

Setup symlinks `bin/pueo` to `/usr/local/bin/pueo`.

---

## Section 9 — Where Your Data Lives

### Native / both

| Directory | Contents |
|---|---|
| `~/.config/pueo/` | `config.yaml` |
| `~/Library/Application Support/Pueo/` | SQLite DB, HITL cards, backups, archives, ChromaDB, registered tools |
| `~/Library/Caches/Pueo/` | HA release notes, HACS changelogs, ha_source, case_ingest |
| `~/Library/Logs/Pueo/` | `pueo.log`, `pueo-stderr.log` |

Override any directory with environment variables: `PUEO_CONFIG_DIR`, `PUEO_DATA_DIR`, `PUEO_STATE_DIR`, `PUEO_CACHE_DIR`, `PUEO_LOG_DIR`.

### Docker

| Container path | Volume | Contents |
|---|---|---|
| `/config` | `./config` (bind, read-only) | `config.yaml` |
| `/data` | `pueo-data` (named) | backups, archives, ChromaDB |
| `/state` | `pueo-state` (named) | SQLite DB, HITL cards, registered tools |
| `/cache` | `pueo-cache` (named) | HA release notes, HACS changelogs, ha_source |
| `/logs` | `pueo-logs` (named) | `pueo.log`, `pueo-stderr.log` |

Named volumes persist across container recreation (`docker compose down` does not remove them; `docker compose down -v` does).

---

## After Setup

### Native / both

```bash
pueo                                      # start the supervisor (all loops + dashboard)
tail -f ~/Library/Logs/Pueo/pueo.log      # follow the live log
```

Dashboard: `http://127.0.0.1:8080`

```bash
pueo --mode diagnose
pueo --mode update-check
pueo --mode rag-refresh
pueo --mode diagnose --config /path/to/config.yaml
```

### Docker

```bash
docker compose up -d                      # start in the background
docker compose logs -f pueo               # follow the live log
```

Dashboard: `http://127.0.0.1:8080`

```bash
docker exec pueo-agent python main.py --mode diagnose
docker exec pueo-agent python main.py --mode rag-refresh
docker exec pueo-agent python main.py --mode update-check
```

See `python main.py --help` for the full mode list.
