# Pueo Setup Guide

Run `./setup.sh` from the `pueo/` directory. This guide walks through every prompt in the order setup.sh asks for it, so you can have everything ready without stopping mid-install.

```bash
git clone https://github.com/AndysWorth/pueo
cd pueo
./setup.sh
```

`setup.sh` is idempotent — safe to re-run at any time. If you want to start completely from scratch, run `./setup.sh --clean` first (removes `.venv`, `config.yaml`, and all platform-directory state: DB, HITL cards, caches, backups, ChromaDB, logs).

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
| `native` | Creates `.venv`, installs Ollama model, writes `~/Library/Application Support/Pueo/config.yaml`, installs launchd service and RAG refresh job, symlinks `pueo` command |
| `docker` | Skips venv and launchd; writes `config/config.yaml` and generates `docker-compose.yml` with SSH key mount |
| `both` | Does everything: native config + infrastructure, plus Docker config and `docker-compose.yml` |

---

## Section 1 — Python

> **Docker mode: this section is skipped.** Python runs inside the container; you don't need a local venv.

**Automatic, no input needed (native/both).**

Setup detects Python 3.14 via Homebrew (`python3.14`) or pyenv, creates a `.venv`, and installs all dependencies from `requirements-dev.txt`. If the existing `.venv` is the wrong Python version, it is recreated automatically. If Python 3.14 isn't found at all, setup exits with instructions.

**Prerequisites:**
- [Homebrew](https://brew.sh): `brew install python@3.14`
- Or [pyenv](https://github.com/pyenv/pyenv): setup installs `3.14` automatically if pyenv is present

---

## Section 2 — Ollama

> **Docker mode:** this section asks for the Ollama endpoint URL (default: `http://host.docker.internal:11434` for macOS Docker Desktop). Ollama must run on the host machine; no model pull is attempted.

**Automatic, no prompts (native/both).**

Setup verifies that Ollama is installed and running (starting it automatically if needed), detects your hardware (chip and RAM), and recommends a model:

| RAM | Recommended model |
|---|---|
| ≥ 48 GB | `qwen2.5-coder:32b` |
| 20–47 GB | `qwen2.5-coder:14b` |
| < 20 GB | `qwen2.5-coder:7b` |

If you have an existing `config.yaml`, setup reads your currently configured model from it and uses that as the default. Otherwise it uses the hardware recommendation.

Setup pulls the configured model plus `nomic-embed-text` (used for RAG embeddings) if they aren't already installed locally. This can take several minutes on the first run.

Config keys: `ollama.endpoint` (Docker mode only at this step)

---

## Section 2.5 — LLM Provider

**Two to four prompts depending on choices. All modes.**

**Prompt: LLM provider** (`local` / `cloud` / `both`, default: `local`)

| Choice  | Behaviour |
| ------- | --------- |
| `local` | Ollama only — all inference stays on device; zero cloud API calls |
| `cloud` | Anthropic Claude as the primary model; all inference calls go to Anthropic |
| `both`  | Ollama for autonomous repair cycles; Claude available as an approved escalation when the local loop hits the tool-call cap without finishing |

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

**Prompt: Auto-select best model at startup?** (`true` / `false`, default: `false`)

> **Docker mode: this prompt is skipped** — `model_auto` is set to `false` in the generated config.

When `true`, Pueo checks which `qwen2.5-coder` variants are installed at startup and picks the largest one that fits in your current RAM. Useful as you add or remove larger models over time.

Config keys: `llm.provider`, `cloud.model`, `ollama.model_auto`

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

Setup warns if the agent is not running or the key is not loaded.

**Docker note:** the SSH key is mounted read-only into the container at `/root/.ssh/id_ed25519`. setup.sh writes the correct volume mount into `docker-compose.yml` automatically based on the key path you specify.

---

## Section 4 — Configuration

**The main configuration prompts. Press Enter to accept each default. All modes.**

**If `config.yaml` already exists**, setup detects it and asks:
```
  Reconfigure? [y/N]:
```
Answer `y` to re-run all prompts. Answer `n` (or Enter) to skip the entire configuration section and proceed to the service install steps.

Config file destination:
- **native:** `~/Library/Application Support/Pueo/config.yaml`
- **docker:** `config/config.yaml` (the bind-mount source for `./config:/config:ro`)
- **both:** writes both; the Docker copy uses `host.docker.internal` as the Ollama endpoint

---

**Prompt: Home Assistant hostname or IP** (default: `homeassistant.local`)

Config key: `home_assistant.host`

---

**Prompt: SSH username** (default: `root`)

Config key: `home_assistant.user`

---

**Prompt: SSH private key path** (default: `~/.ssh/id_ed25519`)

Config key: `home_assistant.ssh_key_path`

---

**Prompt: HA long-lived access token** *(input hidden — not echoed to terminal)*

Required for update monitoring and persistent notification polling. Leave blank to skip (you can add it later by editing `config.yaml` or re-running setup).

To create one: HA profile picture → **Security → Long-Lived Access Tokens → Create Token**.

Config key: `home_assistant.api_token`

---

**Prompt: Update check interval (hours, 0 = disabled)** (default: `6`)

Only used when `api_token` is set. Controls how often Pueo polls for available HA Core, OS, and add-on updates.

Config key: `agent.update_check_interval_hours`

---

**Prompt: config.yaml path on HA host** (default: `/config/configuration.yaml`)

Config key: `home_assistant.config_path`

---

**Prompt: Ollama model** (default: hardware-matched recommendation or existing config value)

Confirms or overrides the model detected in Section 2. If you enter a model name that isn't installed, setup offers to pull it immediately.

Config key: `ollama.model`

---

**Prompt: Local SQLite database path**
- Native default: `~/Library/Application Support/Pueo/ha_agent_state.db`
- Docker default: `/state/ha_agent_state.db`

Config key: `agent.db_path`

---

**Prompt: Log confidence threshold (0–1)** (default: `0.7`)

The minimum confidence score an LLM log-triage result must reach before Pueo treats it as actionable. Lower values increase sensitivity; raise it to reduce false positives on noisy logs.

Config key: `agent.log_confidence_threshold`

---

**Prompt: Self-healing enabled** (`true` / `false`, default: `true`)

When `false`, Pueo diagnoses and reports issues but never writes to Home Assistant or triggers repairs.

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

When enabled, the conversational agent can write new Python tools at runtime. Each tool requires sandbox CI validation and explicit approval before loading. Leave disabled unless you understand the risk.

Config key: `agent.chat_allow_tool_registration`

---

**Prompt: Allow diagnostic WAN fetch?** (`true` / `false`, default: `true`)

Controls the `fetch_url` tool, which lets Pueo make read-only HTTP GET requests to external URLs during investigations — for example, to confirm that a cloud API outage has resolved. Private and loopback addresses are always blocked regardless of this setting.

Config key: `agent.allow_diagnostic_wan`

---

**Prompt: Notifier type** (`file` / `ntfy` / `webhook`, default: `file`)

| Choice | Behaviour |
|---|---|
| `file` | Writes a JSON card to a local directory; approve via the dashboard or by running `touch <hitl-dir>/<id>.approved` |
| `ntfy` | Sends a push notification to an ntfy topic |
| `webhook` | HTTP POST to any URL |

**If `ntfy`:**

**Prompt: ntfy topic URL** (default: a randomly generated `https://ntfy.sh/pueo-<hex>`)
Pick a unique topic name — anyone who knows it can see your alerts. For a self-hosted instance: `https://ntfy.example.com/<topic>`.

**Prompt: Approval watch directory** (default: platform state dir `/hitl`)
Path that Pueo watches for `.approved` / `.rejected` files. To approve from this machine or via SSH:
```bash
touch <watch-dir>/<notification-id>.approved
touch <watch-dir>/<notification-id>.rejected
```

**If `file`:**

**Prompt: Approval watch directory** (default: platform state dir `/hitl`)

**If `webhook`:**

**Prompt: Webhook URL** — HTTP POST target for outgoing notifications.

Config keys: `agent.notifier`, `agent.notify_url`, `agent.notify_watch_dir`

---

**SSH connectivity test — automatic.**

Setup connects to HA over SSH to verify credentials and detect the HA Core version. The detected version is written into `config.yaml` as `home_assistant.known_version`.

---

**Prompt: Set up NetAlertX automatically when Pueo runs?** (`Y` / `n`, default: `Y`)

NetAlertX is a network-presence monitor. When enabled, Pueo installs and configures it automatically the first time the supervisor starts. You can change this later: set `netalertx.setup_desired: true` in `config.yaml` and restart Pueo.

Config key: `netalertx.setup_desired`

---

**If NetAlertX setup is enabled — Prompt: Install on separate Docker machine instead of HA?** (`y` / `N`)

By default, NetAlertX runs as a Home Assistant add-on. If HA disk space is limited (the NetAlertX database grows with your network size), you can install it on a separate machine running Docker instead.

**If separate Docker machine:**

**Prompt: Docker host IP or hostname**

**Prompt: SSH user on Docker host** (default: current macOS user)

**Prompt: SSH key path for Docker host** (blank = same key as HA)

**Prompt: NetAlertX config path on Docker host** (default: `/opt/netalertx/config`)

Setup tests SSH connectivity to the Docker host and checks available disk space (5 GB recommended).

Config keys: `netalertx.deploy_target`, `netalertx.docker_host`, `netalertx.docker_ssh_user`, `netalertx.docker_ssh_key_path`, `netalertx.docker_config_path`

---

**If NetAlertX setup is enabled — Prompt: NetAlertX API token** *(input hidden — not echoed to terminal)*

> **First-time setup: leave this blank.** The token can only be generated after NetAlertX is running. Once it is: `NetAlertX web UI → Settings → Main Settings → API Key`. Then re-run `./setup.sh` or edit `config.yaml` directly.

Config key: `netalertx.api_token`

---

**MQTT (Mosquitto broker) — automatic check, then up to two prompts.**

Setup checks whether the Mosquitto broker add-on is running on HA (requires the SSH connection to succeed). If Mosquitto isn't found, setup warns and suggests installing it from the HA App Store.

**Prompt: MQTT username** (blank = anonymous access)

To set up a dedicated Mosquitto user:
1. `Settings → People → Users → Add User` (enable "Local access only")
2. `Settings → Apps → Mosquitto broker → Configuration` — add the user to the `logins:` list

**Prompt: MQTT password** *(input hidden — not echoed to terminal)*
Only asked when you provide a username.

Config keys: `netalertx.mqtt_user`, `netalertx.mqtt_password`

---

## Section 4.5 — Docker Compose Setup

> **Native mode: this section is skipped.**

**Automatic (docker/both).**

After the config file is written, setup generates `docker-compose.yml` in the repo root with:
- SSH key volume mount: `- <your-key-path>:/root/.ssh/id_ed25519:ro`
- `restart: unless-stopped` restart policy
- `TZ` environment variable (from your current shell's `$TZ`, defaulting to `America/New_York`)
- `ANTHROPIC_API_KEY` instructions in a comment block — uncomment and set the value if you chose `cloud` or `both` LLM mode

**What to do next:**
```bash
docker compose up -d
docker compose logs -f pueo
```

The config file at `config/config.yaml` is already in place — no manual editing needed.

---

## Section 5 — NetAlertX

**Automatic, no input needed. All modes.**

Reports whether NetAlertX will be installed automatically on first start or is disabled. No action required here.

---

## Section 6 — launchd Service

> **Docker mode: this section is skipped.** Restart policy is handled by Docker (`restart: unless-stopped`).

**One prompt (native/both).**

**Prompt: Install Pueo as a launchd service?** (`Y` / `n`)
When installed, Pueo starts automatically at login and restarts automatically on crash. If the service is already installed, this section is skipped.

To manage the service manually:
```bash
launchctl stop com.pueo.agent       # stop
launchctl start com.pueo.agent      # start
launchctl remove com.pueo.agent     # uninstall
```

---

## Section 7 — RAG Knowledge-Base Refresh

> **Docker mode: this section is skipped.** Refresh the knowledge base manually:
> ```bash
> docker exec pueo-agent python main.py --mode rag-refresh
> ```
> Or add a cron job on the host to run it on a schedule.

**One prompt (native/both).**

**Prompt: Install weekly RAG refresh job?** (`Y` / `n`)

A launchd job runs `--mode rag-refresh` every Sunday at 03:00. This fetches and re-embeds:
- HA Core release notes (breaking changes for the last N versions)
- HACS integration changelogs (auto-discovered from your HA instance)
- HA integration documentation and source files for installed integrations
- HA concepts and community cases

If the job is already installed, this section is skipped.

To trigger a refresh immediately after installing:
```bash
launchctl start io.pueo.rag-refresh
# or
pueo --mode rag-refresh
```

Optional config keys (edit `config.yaml` directly to set these):
`rag_ha_versions_to_fetch`, `rag_hacs_cache_dir`, `rag_ha_docs_cache_dir`, `ha_source_cache_dir`, `ha_concepts_cache_dir`, `case_ingest_cache_dir`, `rag_refresh_interval_hours` (default `168` — weekly)

---

## Section 8 — pueo Command

> **Docker mode: this section is skipped.**

**Automatic, no input needed (native/both).**

Setup symlinks `bin/pueo` to `/usr/local/bin/pueo` so `pueo` is available anywhere in your shell. If `/usr/local/bin` is not writable, setup prints the manual symlink command to run with `sudo`.

---

## Section 9 — Where Your Data Lives

### Native / both

On macOS, `platformdirs` maps all Pueo directories:

| Directory | Contents |
|---|---|
| `~/Library/Application Support/Pueo/` | `config.yaml`, SQLite DB, HITL cards, backups, archives, ChromaDB, registered tools |
| `~/Library/Caches/Pueo/` | HA release notes, HACS changelogs, ha_source, ha_concepts, case_ingest |
| `~/Library/Logs/Pueo/` | `pueo.log`, `pueo-stderr.log` |

> **Note:** config and state live in the same `Application Support/Pueo/` directory on macOS because `platformdirs` maps both to the same location. Override any directory with environment variables: `PUEO_CONFIG_DIR`, `PUEO_DATA_DIR`, `PUEO_STATE_DIR`, `PUEO_CACHE_DIR`, `PUEO_LOG_DIR`.

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
