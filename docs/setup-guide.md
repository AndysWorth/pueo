# Pueo Setup Guide

Run `./setup.sh` from the `pueo/` directory. This guide walks through every prompt in the order setup.sh asks for it, so you can have everything ready without stopping mid-install.

```bash
git clone https://github.com/AndysWorth/pueo
cd pueo
./setup.sh
```

`setup.sh` is idempotent — safe to re-run at any time. If you want to start completely from scratch, run `./setup.sh --clean` first (removes `.venv`, `config.yaml`, `ha_agent_state.db`, `hitl/`, `backups/`, `chromadb/`, `archives/`).

---

## Section 1 — Python

**Automatic, no input needed.**

Setup detects Python 3.14 via Homebrew (`python3.14`) or pyenv, creates a `.venv`, and installs all dependencies from `requirements-dev.txt`. If Python 3.14 isn't found, setup exits with instructions.

**Prerequisites:**
- [Homebrew](https://brew.sh): `brew install python@3.14`
- Or [pyenv](https://github.com/pyenv/pyenv): setup installs `3.14` automatically if pyenv is present

---

## Section 2 — Ollama

**Automatic, then two prompts.**

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

Config keys: `ollama.model`, `ollama.model_auto`

---

## Section 2.5 — LLM Provider

**Two prompts (more if cloud mode is chosen).**

**Prompt: LLM provider** (`local` / `cloud` / `both`, default: `local`)

| Choice  | Behaviour                                                                                                                   |
| ------- | --------------------------------------------------------------------------------------------------------------------------- |
| `local` | Ollama only — all inference stays on device; zero cloud API calls                                                           |
| `cloud` | Anthropic Claude as the primary model; all inference calls go to Anthropic                                                  |
| `both`  | Ollama for autonomous repair cycles; Claude available as a HITL-approved escalation when the local loop hits the tool-call cap or wall-clock timeout without finishing |

`both` mode preserves the local-first property during unattended repair — Claude is only invoked when a human approves a cloud-escalation card in the dashboard. A local loop that explicitly signals failure (e.g. the model determines no fix is possible) does *not* trigger escalation; only hitting the hard limits does.

**If you choose `cloud` or `both`:**

**Prompt: Claude model** (default: `claude-sonnet-5`)
Press Enter to accept the default or type a different Anthropic model ID.

**ANTHROPIC_API_KEY** — Required for `cloud` and `both` modes. Pueo reads this exclusively from the environment — it is never written to `config.yaml` (a plaintext file is not an appropriate store for a billable credential). Set it once in your shell environment:

```bash
# Add to ~/.zshenv and reload your shell
export ANTHROPIC_API_KEY=<your-key>
```

Setup warns you if the key is absent; Pueo will refuse to start in `cloud` or `both` mode until it is set.

Config keys: `llm.provider`, `cloud.model`

---

## Section 3 — SSH Key

**Automatic, with optional interactive steps.**

Setup checks for `~/.ssh/id_ed25519`. If it doesn't exist, it offers to generate one:

```
Generate a new ed25519 key now? [Y/n]:
```

If you generate a key, setup prints the public key and gives instructions for adding it to HA:
1. In HA: `Settings → Apps → Terminal & SSH`
2. Paste the public key into the `authorized_keys` field
3. Set `port: 22` and click **Start**
4. Press Enter in setup to continue

**SSH agent check** — Pueo uses `asyncssh` and cannot prompt for a key passphrase interactively. If your key has a passphrase, add it to the macOS keychain once:

```bash
ssh-add --apple-use-keychain ~/.ssh/id_ed25519
```

Setup warns you if the SSH agent is not running or the key is not loaded.

---

## Section 4 — Configuration

**The main configuration prompts. Press Enter to accept each default.**

**Prompt: Home Assistant hostname or IP** (default: `homeassistant.local`)
The hostname or IP address of your HA instance on your local network.

Config key: `home_assistant.host`

---

**Prompt: SSH username** (default: `root`)
The user Pueo connects as over SSH. For Home Assistant OS this is `root`.

Config key: `home_assistant.user`

---

**Prompt: SSH private key path** (default: `~/.ssh/id_ed25519`)
Path to the private key whose public half is in HA's `authorized_keys`.

Config key: `home_assistant.ssh_key_path`

---

**Prompt: HA long-lived access token**
Required for update monitoring and persistent notification polling. Leave blank to skip — you can set it later in `config.yaml`.

To create one:
1. In HA, click your **profile picture** (bottom-left)
2. Scroll to **Security → Long-Lived Access Tokens**
3. Click **Create Token**, name it `pueo`, copy the value

Config key: `home_assistant.api_token`

---

**Prompt: Update check interval (hours, 0 = disabled)** (default: `6`)
How often Pueo polls for available HA Core, OS, and add-on updates. Requires `api_token` to be set. Set to `0` to disable update checking.

Config key: `agent.update_check_interval_hours`

---

**Prompt: config.yaml path on HA host** (default: `/config/configuration.yaml`)
The remote path to your Home Assistant `configuration.yaml`. Change this only if you use a non-standard HA layout.

Config key: `home_assistant.config_path`

---

**Prompt: Ollama model** (default: hardware-matched recommendation)
Confirms or overrides the model chosen in Section 2. If you type a model that isn't installed, setup offers to pull it immediately.

Config key: `ollama.model`

---

**Prompt: Local SQLite database path** (default: `ha_agent_state.db`)
Where Pueo stores its state: repair history, backup registry, chat memory, episode records, and billing data. Keep the default unless you want the database somewhere specific.

Config key: `agent.db_path`

---

**Prompt: Log confidence threshold (0–1)** (default: `0.7`)
The minimum confidence score the AI model must assign to a log event before Pueo treats it as actionable. Lower values trigger more alerts; higher values mean only high-confidence events are acted on.

Config key: `agent.log_confidence_threshold`

---

**Prompt: Self-healing enabled** (`true` / `false`, default: `true`)
When `false`, Pueo monitors and diagnoses but never writes to HA — equivalent to a permanent read-only mode that overrides the autonomy level.

Config key: `agent.self_healing_enabled`

---

**Prompt: Require human approval before every repair?** (`true` / `false`, default: `false`)
When `true`, forces HITL approval for every repair action regardless of the autonomy level below.

Config key: `agent.hitl_always`

---

**Prompt: Autonomy level** (`1`–`4`, default: `2`)
Controls how much Pueo acts on its own versus waiting for approval:

| Level | Name | Behaviour |
|---|---|---|
| `1` | Report only | Diagnoses and explains issues; never writes to HA |
| `2` | Suggest | Generates proposed fixes and sends them to the dashboard; you approve each one |
| `3` | Guided | Auto-executes LOW-severity fixes; HITL approval for MEDIUM and CRITICAL |
| `4` | Autonomous | Auto-executes LOW and MEDIUM fixes; HITL approval for CRITICAL only |

Config key: `agent.autonomy_level`

---

**Prompt: HITL dashboard port** (default: `8080`)
The local port for the web dashboard. Change this if port 8080 is already in use on your machine.

Config key: `agent.dashboard_port`

---

**Prompt: Allow chat agent to register new tools?** (`true` / `false`, default: `false`)
When `true`, the conversational agent can write new Python tools at runtime — each goes through sandbox CI validation and an explicit HITL approval card before being loaded. Leave `false` unless you understand that the agent will be generating and executing arbitrary Python code.

Config key: `agent.chat_allow_tool_registration`

---

**Prompt: Notifier type** (`file` / `ntfy` / `webhook`, default: `file`)
How Pueo alerts you when it needs HITL approval:

| Choice | Behaviour |
|---|---|
| `file` | Writes a JSON file to a local directory; approve by running `touch hitl/<id>.approved` on this machine |
| `ntfy` | Sends a push notification to an ntfy topic; you still approve by touching the file (or via SSH) |
| `webhook` | HTTP POST to any URL — use this to trigger a Home Assistant automation |

**If you choose `ntfy`:** setup generates a unique topic URL (e.g. `https://ntfy.sh/pueo-<random8hex>`). Subscribe to that topic in the [ntfy app](https://ntfy.sh) on your phone. Anyone who knows the topic URL can see your alerts — treat it as a secret. Setup also asks for an approval watch directory (default: `hitl/`).

**If you choose `webhook`:** have your endpoint URL ready. Setup asks for it now.

Config keys: `agent.notifier`, `agent.notify_url`, `agent.notify_watch_dir`

---

**SSH connectivity test — automatic.**
Setup connects to HA over SSH to verify credentials and detect the HA version. If the connection fails, setup warns you and continues — you can fix the credentials in `config.yaml` later.

---

**Prompt: Set up NetAlertX automatically when Pueo runs?** (`Y` / `n`)
NetAlertX is a network-presence monitor that Pueo can install and manage as a HA App. If you say yes, Pueo installs it automatically on first start — you just approve the HITL cards that appear in the dashboard.

You can change this decision later: set `netalertx.setup_desired: true` in `config.yaml` and restart Pueo.

**If yes — Prompt: Install on separate Docker machine instead of HA?** (`y` / `N`)
By default NetAlertX installs as a HA App. If your HA disk space is limited (the NetAlertX database grows with your network), you can install it on a separate machine running Docker instead.

**If Docker:**
- **Prompt: Docker host IP or hostname** — the machine where Docker is running
- **Prompt: SSH user on Docker host** (default: your current username)
- **Prompt: SSH key path for Docker host** — leave blank to reuse the HA SSH key

Setup verifies SSH access to the Docker host and checks available disk space (5 GB minimum recommended).

Config keys: `netalertx.setup_desired`, `netalertx.deploy_target`, `netalertx.docker_host`, `netalertx.docker_ssh_user`, `netalertx.docker_ssh_key_path`

---

**Prompt: NetAlertX API token** (blank = set after first install)
Required for Pueo to communicate with NetAlertX's REST API.

> **First-time setup: leave this blank.** NetAlertX doesn't exist yet. Once it's installed and running, find the token at: `NetAlertX web UI (http://<ha-host>:20212) → Settings → Main Settings → API Key`. Then re-run `./setup.sh` or edit `config.yaml` directly.

Config key: `netalertx.api_token`

---

**MQTT credentials — two prompts.**
If your Mosquitto broker requires authentication, enter the credentials Pueo should use to connect.

**Prompt: MQTT username** (blank = anonymous access)
**Prompt: MQTT password** (only shown if a username is entered)

To set up a dedicated Mosquitto user:
1. **Create a HA user:** `Settings → People → Users → Add User` (enable "Local access only")
2. **Enable auth in Mosquitto:** `Settings → Apps → Mosquitto broker → Configuration` → add the user to the `logins:` list and save

Leave blank for anonymous (unauthenticated) access.

Config keys: `netalertx.mqtt_user`, `netalertx.mqtt_password`

---

## Section 5 — NetAlertX

**Automatic, no input needed.**

Reports whether NetAlertX will be installed automatically on first start (based on your answers in Section 4) or is disabled. No prompts.

---

## Section 6 — launchd Service

**One prompt.**

**Prompt: Install Pueo as a launchd service?** (`Y` / `n`)
When installed, Pueo starts automatically at login and restarts automatically on crash. The service writes a plist to `~/Library/LaunchAgents/com.pueo.agent.plist`.

If you skip this, start Pueo manually at any time with `pueo`. You can install the service later with `pueo --mode install-service`.

---

## Section 7 — RAG Knowledge-Base Refresh

**One prompt.**

**Prompt: Install weekly RAG refresh job?** (`Y` / `n`)
Pueo maintains a local ChromaDB vector database seeded with context the repair agent queries during fix cycles — HA breaking-change release notes, HACS component changelogs, and installed integration docs. A weekly launchd job keeps this database current by running every Sunday at 03:00.

If you skip, refresh manually at any time:
```bash
pueo --mode rag-refresh
```

---

## Section 8 — pueo Command

**Automatic, no input needed.**

Setup symlinks `bin/pueo` to `/usr/local/bin/pueo`. If that fails (permissions), setup shows the manual `ln -sf` command to run with `sudo`.

---

## After setup

```bash
pueo                  # start the supervisor (all loops + dashboard)
tail -f pueo.log      # follow the live log
```

Dashboard: `http://127.0.0.1:8080`

To run individual modes:
```bash
pueo --mode diagnose
pueo --mode update-check
pueo --mode rag-refresh
pueo --mode diagnose --config /path/to/config.yaml
```

See `pueo --help` (i.e., `python main.py --help`) for the full mode list.
