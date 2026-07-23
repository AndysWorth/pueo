#!/usr/bin/env bash
# setup.sh — Pueo environment setup and configuration
# Idempotent: safe to run multiple times. Fixes common problems automatically.
set -euo pipefail

# ── Output helpers ──────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✔${NC}  $*"; }
info() { echo -e "${BLUE}→${NC}  $*"; }
warn() { echo -e "${YELLOW}⚠${NC}  $*"; }
fail() { echo -e "${RED}✘${NC}  $*" >&2; }
hdr()  { echo -e "\n${BOLD}$*${NC}\n────────────────────────────────────────"; }
ask()  {
    # ask "Prompt" "default" varname
    local answer
    read -rp "  $1 [${2}]: " answer
    printf -v "$3" '%s' "${answer:-$2}"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── --help / --clean flags ───────────────────────────────────────────────────────
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    echo -e "\nUsage: ./setup.sh [--clean]"
    echo
    echo "  (no flags)   Interactive setup: install dependencies, create .venv,"
    echo "               and write config.yaml. Safe to re-run at any time."
    echo
    echo "  --clean      Remove .venv, config.yaml, ha_agent_state.db, and hitl/"
    echo "               before running setup. Use this to start from scratch."
    echo
    echo "  -h, --help   Show this help message."
    exit 0
fi

if [[ "${1:-}" == "--clean" ]]; then
    echo -e "\n${YELLOW}⚠  Clean mode — removing generated files before setup${NC}"
    rm -rf .venv
    rm -f config.yaml ha_agent_state.db
    rm -rf hitl/
    ok "Removed .venv, config.yaml, ha_agent_state.db, hitl/"
fi

echo -e "\n🦉  ${BOLD}Pueo Setup${NC}"
echo "════════════════════════════════════════"

# ── 1. Python ───────────────────────────────────────────────────────────────────
hdr "1. Python"

REQUIRED_PYTHON="3.14"

if command -v pyenv &>/dev/null; then
    ok "pyenv $(pyenv --version | awk '{print $2}')"
else
    warn "pyenv not found — will use system Python if available"
fi

# Prefer a system python3.14 (e.g. Homebrew) before touching pyenv
if command -v python3.14 &>/dev/null; then
    PYTHON_BIN="$(command -v python3.14)"
    INSTALLED_VERSION="$(python3.14 --version 2>&1 | awk '{print $2}')"
    ok "Python ${INSTALLED_VERSION} (system)"
else
    # Fall back to pyenv — install if needed
    INSTALLED_VERSION=$(pyenv versions --bare | grep "^${REQUIRED_PYTHON}\." | sort -V | tail -1 || true)
    if [[ -z "$INSTALLED_VERSION" ]]; then
        info "Python ${REQUIRED_PYTHON} not found — installing via pyenv (this may take a few minutes)..."
        pyenv install "${REQUIRED_PYTHON}"
        INSTALLED_VERSION=$(pyenv versions --bare | grep "^${REQUIRED_PYTHON}\." | sort -V | tail -1)
    fi
    ok "Python ${INSTALLED_VERSION} (pyenv)"
    PYTHON_BIN="$(pyenv prefix "$INSTALLED_VERSION")/bin/python"
fi

# Create or verify .venv
if [[ -d ".venv" ]]; then
    VENV_VER=$(.venv/bin/python --version 2>&1 | awk '{print $2}' | cut -d. -f1,2)
    if [[ "$VENV_VER" == "$REQUIRED_PYTHON" ]]; then
        ok ".venv (Python ${VENV_VER})"
    else
        warn ".venv is Python ${VENV_VER}, need ${REQUIRED_PYTHON} — recreating..."
        rm -rf .venv
        "$PYTHON_BIN" -m venv .venv
        ok ".venv recreated (Python ${REQUIRED_PYTHON})"
    fi
else
    info "Creating .venv..."
    "$PYTHON_BIN" -m venv .venv
    ok ".venv created"
fi

# Install / sync dev dependencies
info "Syncing requirements-dev.txt..."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements-dev.txt
ok "Dependencies installed"

# ── 2. Ollama ───────────────────────────────────────────────────────────────────
hdr "2. Ollama"

if ! command -v ollama &>/dev/null; then
    fail "ollama CLI not found. Install from https://ollama.com then re-run."
    exit 1
fi
ok "ollama found"

# Check if Ollama is responding; try to start it if not
if ! ollama list &>/dev/null 2>&1; then
    warn "Ollama is not running — attempting to start..."
    # macOS: Ollama.app may not be open; try the CLI server
    nohup ollama serve &>/tmp/ollama-serve.log &
    sleep 4
    if ! ollama list &>/dev/null 2>&1; then
        fail "Could not start Ollama. Start it manually ('ollama serve' or open Ollama.app) then re-run."
        exit 1
    fi
    ok "Ollama started"
else
    ok "Ollama is running"
fi

# Read model from existing config if present, else use default
DEFAULT_MODEL="qwen2.5-coder:7b"
if [[ -f config.yaml ]]; then
    CONFIGURED_MODEL=$(grep "model:" config.yaml | head -1 | awk '{print $2}' | tr -d '"' || echo "$DEFAULT_MODEL")
else
    CONFIGURED_MODEL="$DEFAULT_MODEL"
fi

if ollama list | grep -q "^${CONFIGURED_MODEL}"; then
    ok "Model ${CONFIGURED_MODEL} is available"
else
    info "Pulling model ${CONFIGURED_MODEL} (this may take several minutes)..."
    ollama pull "$CONFIGURED_MODEL"
    ok "Model ${CONFIGURED_MODEL} ready"
fi

# ── 3. SSH Key ──────────────────────────────────────────────────────────────────
hdr "3. SSH Key"

DEFAULT_SSH_KEY="${HOME}/.ssh/id_ed25519"

if [[ -f "$DEFAULT_SSH_KEY" ]]; then
    ok "SSH key found: ${DEFAULT_SSH_KEY}"
else
    warn "No SSH key at ${DEFAULT_SSH_KEY}"
    read -rp "  Generate a new ed25519 key now? [Y/n]: " gen_key
    if [[ "${gen_key:-Y}" =~ ^[Yy] ]]; then
        mkdir -p "${HOME}/.ssh" && chmod 700 "${HOME}/.ssh"
        ssh-keygen -t ed25519 -f "$DEFAULT_SSH_KEY" -C "pueo-agent" -N ""
        ok "SSH key generated: ${DEFAULT_SSH_KEY}"
        echo
        echo "  ── Add this public key to Home Assistant ──────────────────────"
        cat "${DEFAULT_SSH_KEY}.pub"
        echo "  ───────────────────────────────────────────────────────────────"
        echo "  In HA: Settings → Apps → Terminal & SSH"
        echo "         → Configuration → authorized_keys"
        echo "         Paste the public key above, set port: 22, then Start."
        echo
        read -rp "  Press Enter once the key is added to HA to continue..."
    else
        warn "Skipping key generation — SSH features will not work without a key."
    fi
fi

# ── SSH agent ────────────────────────────────────────────────────────────────────
echo
info "Checking SSH agent..."
if [[ -z "${SSH_AUTH_SOCK:-}" ]]; then
    warn "SSH_AUTH_SOCK is not set — the SSH agent may not be running."
    warn "Pueo uses asyncssh, which cannot prompt for a key passphrase."
    warn "If your key has a passphrase, add it to the macOS keychain:"
    warn "  ssh-add --apple-use-keychain ${DEFAULT_SSH_KEY}"
    warn "Then re-run this script, or run Pueo from a shell where the agent is active."
else
    # Check whether the key is actually loaded
    if ssh-add -l 2>/dev/null | grep -q "${DEFAULT_SSH_KEY}"; then
        ok "SSH agent running and key is loaded"
    else
        warn "SSH agent is running but ${DEFAULT_SSH_KEY} is not loaded."
        warn "If the key has a passphrase, add it with:"
        warn "  ssh-add --apple-use-keychain ${DEFAULT_SSH_KEY}"
    fi
fi

# ── 4. Configuration ────────────────────────────────────────────────────────────
hdr "4. Configuration"

WRITE_CONFIG=false
if [[ -f "config.yaml" ]]; then
    ok "config.yaml already exists"
    read -rp "  Reconfigure? [y/N]: " reconf
    [[ "${reconf:-N}" =~ ^[Yy] ]] && WRITE_CONFIG=true
else
    WRITE_CONFIG=true
fi

if $WRITE_CONFIG; then
    echo "  Press Enter to accept each default."
    echo

    ask "Home Assistant hostname or IP"    "homeassistant.local"          HA_HOST
    ask "SSH username"                      "root"                          HA_USER
    ask "SSH private key path"             "$DEFAULT_SSH_KEY"              HA_SSH_KEY
    ask "HA long-lived access token"        ""                              HA_API_TOKEN
    ask "config.yaml path on HA host"      "/config/configuration.yaml"    HA_CONFIG_PATH
    ask "Ollama model"                      "$DEFAULT_MODEL"                OLLAMA_MODEL
    ask "Local SQLite database path"        "ha_agent_state.db"             DB_PATH
    ask "Log confidence threshold (0–1)"    "0.7"                           LOG_THRESHOLD
    ask "Self-healing enabled"              "true"                          SELF_HEALING

    echo
    echo "  ── HITL (human-in-the-loop) notifications ──"
    echo "  When Pueo encounters a CRITICAL issue it pauses and waits for your"
    echo "  approval before writing to Home Assistant. Choose how it notifies you."
    echo
    echo "  Options:"
    echo "    file    — writes a JSON file to a local directory; you approve by"
    echo "              touching <id>.approved in that directory (good for testing)"
    echo "    ntfy    — sends a push notification to ntfy.sh or a self-hosted"
    echo "              instance; you approve by touching the approval file via SSH"
    echo "    webhook — HTTP POST to any URL (e.g. an HA automation)"
    echo
    ask "Require human approval before every repair? (true/false)"  "false"  HITL_ALWAYS
    ask "Autonomy level (1=report-only 2=suggest 3=guided 4=autonomous)"  "2"  AUTONOMY_LEVEL
    ask "HITL dashboard port"  "8080"  DASHBOARD_PORT
    ask "Notifier type (file/ntfy/webhook)"  "file"                          NOTIFIER_TYPE

    NOTIFY_URL=""
    NOTIFY_WATCH_DIR="hitl/"

    if [[ "$NOTIFIER_TYPE" == "ntfy" ]]; then
        echo
        echo "  ntfy topic URL format: https://ntfy.sh/<your-topic>"
        echo "  Pick a unique topic name — anyone who knows it can see your alerts."
        echo "  For self-hosted ntfy use: https://ntfy.example.com/<topic>"
        ask "ntfy topic URL"  "https://ntfy.sh/pueo-$(openssl rand -hex 8)"  NOTIFY_URL
        ask "Approval watch directory"  "hitl/"  NOTIFY_WATCH_DIR
        echo
        echo "  To approve a pending repair (from this machine or via SSH):"
        echo "    touch hitl/<notification-id>.approved"
        echo "  To reject:"
        echo "    touch hitl/<notification-id>.rejected"
    elif [[ "$NOTIFIER_TYPE" == "webhook" ]]; then
        ask "Webhook URL"  ""  NOTIFY_URL
    else
        ask "Approval watch directory"  "hitl/"  NOTIFY_WATCH_DIR
        NOTIFIER_TYPE="file"
    fi

    # ── SSH connectivity, HA version, and log file check ─────────────────────────
    echo
    info "Testing SSH connection to ${HA_HOST}..."
    HA_KNOWN_VERSION=""
    _SSH="ssh -i ${HA_SSH_KEY} -o ConnectTimeout=5 -o BatchMode=yes -o StrictHostKeyChecking=no ${HA_USER}@${HA_HOST}"
    if $_SSH "echo ok" &>/dev/null; then
        ok "SSH connection to ${HA_HOST} successful"

        # Fetch HA version
        HA_KNOWN_VERSION=$($_SSH "ha core info 2>/dev/null | grep '^version:' | awk '{print \$2}'" 2>/dev/null || echo "")
        if [[ -n "$HA_KNOWN_VERSION" ]]; then
            ok "HA version: ${HA_KNOWN_VERSION}"
        else
            warn "Could not determine HA version — known_version will be empty in config.yaml"
        fi

    else
        warn "SSH connection failed — check that ${HA_HOST} is reachable and the key is authorized."
        warn "Test manually: ssh -i ${HA_SSH_KEY} ${HA_USER}@${HA_HOST}"
    fi

    echo
    echo "  ── NetAlertX ──────────────────────────────────────────────────"
    echo "  Provide an API token to enable REST API calls from Pueo."
    echo "  All other values default to your HA SSH settings."
    echo
    ask "NetAlertX API token"  ""  NAX_API_TOKEN

    # ── Mosquitto MQTT broker ─────────────────────────────────────────────────
    echo
    echo "  ── MQTT (Mosquitto broker) ─────────────────────────────────────"
    MQTT_USER=""
    MQTT_PASSWORD=""
    if $_SSH "echo ok" &>/dev/null; then
        mosquitto_state=$($_SSH "ha addons info core_mosquitto 2>/dev/null | grep -E '^\s*state:' | awk '{print \$2}'" 2>/dev/null || echo "")
        if [[ "$mosquitto_state" == "started" ]]; then
            ok "Mosquitto broker is running"
        else
            warn "Mosquitto does not appear to be running (state: ${mosquitto_state:-unknown})"
            warn "Install it from the HA App Store (search: Mosquitto broker), then re-run setup."
        fi
    fi
    echo
    echo "  If Mosquitto requires authentication, enter the credentials Pueo should"
    echo "  use to connect. Create a dedicated HA user at:"
    echo "    Settings → People → Users → Add User (enable 'Local access only')"
    echo "  Leave blank for anonymous (unauthenticated) access."
    echo
    read -rp "  MQTT username (blank = anonymous): " MQTT_USER
    if [[ -n "$MQTT_USER" ]]; then
        read -rsp "  MQTT password: " MQTT_PASSWORD
        echo
        ok "MQTT credentials recorded"
    else
        ok "MQTT anonymous access configured"
    fi

    cat > config.yaml <<EOF
home_assistant:
  host: "${HA_HOST}"
  user: "${HA_USER}"
  ssh_key_path: "${HA_SSH_KEY}"
  api_token: "${HA_API_TOKEN}"
  config_path: "${HA_CONFIG_PATH}"
  known_version: "${HA_KNOWN_VERSION}"

ollama:
  model: "${OLLAMA_MODEL}"
  endpoint: "http://localhost:11434"

netalertx:
  api_token: "${NAX_API_TOKEN}"
  # Advanced tuning — edit config.yaml directly to override these defaults:
  # deployment: auto          # auto | addon | docker
  # host: <same as home_assistant.host>
  # api_port: 20212
  # ssh_host: <same as home_assistant.host>
  # ssh_user: <same as home_assistant.user>
  # ssh_key_path: <same as home_assistant.ssh_key_path>
  # addon_repository_url: https://github.com/alexbelgium/hassio-addons
  # addon_slug: ""            # blank = auto-resolved from Supervisor store
  # scan_interface: ""        # blank = auto-detected from default route
  # auto_generated_name_patterns: ["^unknown-", "^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"]
  # max_scan_age_minutes: 20
  mqtt_user: "${MQTT_USER}"
  mqtt_password: "${MQTT_PASSWORD}"
  # log_container_name: netalertx
  # max_db_history_rows: 100000

agent:
  db_path: "${DB_PATH}"
  log_confidence_threshold: ${LOG_THRESHOLD}
  self_healing_enabled: ${SELF_HEALING}
  hitl_always: ${HITL_ALWAYS}
  autonomy_level: ${AUTONOMY_LEVEL}
  dashboard_port: ${DASHBOARD_PORT}
  notifier: "${NOTIFIER_TYPE}"
  notify_url: "${NOTIFY_URL}"
  notify_watch_dir: "${NOTIFY_WATCH_DIR}"
  # Advanced tuning — edit config.yaml directly to override these defaults:
  # ssh_retry_attempts: 3
  # ssh_retry_base_delay: 2.0
  # debounce_window_seconds: 30
  # repair_cooldown_seconds: 300
  # max_repairs_per_hour: 10
  # log_level: INFO
  # log_file: pueo.log
  # max_prompt_tokens: 7000
EOF
    ok "config.yaml written"
fi

# ── 5. NetAlertX ──────────────────────────────────────────────────────────────────
hdr "5. NetAlertX"

# Read db_path from config.yaml; fall back to default
DB_PATH=$(grep -E '^\s+db_path:' config.yaml 2>/dev/null | awk '{print $2}' | tr -d '"')
[[ -z "$DB_PATH" ]] && DB_PATH="ha_agent_state.db"

# Check installer state via Python sqlite3 (guaranteed available in .venv)
NAX_STATE=$(.venv/bin/python -c "
import sqlite3, os
db = '${DB_PATH}'
if not os.path.exists(db):
    print('NOT_INSTALLED')
else:
    try:
        with sqlite3.connect(db) as c:
            row = c.execute(
                'SELECT state FROM netalertx_install_state WHERE id=1'
            ).fetchone()
            print(row[0] if row else 'NOT_INSTALLED')
    except Exception:
        print('NOT_INSTALLED')
" 2>/dev/null || echo "NOT_INSTALLED")

if [[ "$NAX_STATE" == "FULLY_OPERATIONAL" ]]; then
    ok "NetAlertX is already fully set up"
else
    if [[ "$NAX_STATE" == "NOT_INSTALLED" ]]; then
        info "NetAlertX has not been set up yet."
    else
        info "NetAlertX installer is partially complete (state: ${NAX_STATE})."
    fi
    echo
    read -rp "  Run the NetAlertX installer now? [Y/n]: " run_nax
    if [[ "${run_nax:-Y}" =~ ^[Yy] ]]; then
        .venv/bin/python main.py --mode netalertx-setup
    else
        info "You can run it later: python main.py --mode netalertx-setup"
    fi
fi

# ── Done ─────────────────────────────────────────────────────────────────────────
echo
echo -e "${GREEN}${BOLD}✔  Pueo is ready.${NC}"
echo
echo "  Activate environment : source .venv/bin/activate"
echo "  Live log monitor     : python main.py --mode monitor"
echo "  One-shot diagnostics : python main.py --mode diagnose"
echo "  With memory layer    : python main.py --mode advanced"
echo "  Full repair pipeline : python main.py --mode repair"
echo "  HITL dashboard       : python main.py --mode dashboard"
echo
echo "  NetAlertX install    : python main.py --mode netalertx-setup"
echo "  NetAlertX monitor    : python main.py --mode netalertx"
echo "  NetAlertX diagnose   : python main.py --mode netalertx-diagnose"
echo
