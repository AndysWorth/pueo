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
    echo "  --clean      Remove .venv, config.yaml, ha_agent_state.db, hitl/,"
    echo "               .cache/, backups/, chromadb/, and pueo.log before running"
    echo "               setup. Use this to start completely from scratch."
    echo
    echo "  -h, --help   Show this help message."
    exit 0
fi

if [[ "${1:-}" == "--clean" ]]; then
    echo -e "\n${YELLOW}⚠  Clean mode — removing generated files before setup${NC}"
    rm -rf .venv
    rm -f config.yaml ha_agent_state.db pueo.log
    rm -rf hitl/ .cache/ backups/ chromadb/
    ok "Removed .venv, config.yaml, ha_agent_state.db, hitl/, .cache/, backups/, chromadb/, pueo.log"
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

RAG_EMBED_MODEL="nomic-embed-text"
if ollama list | grep -q "^${RAG_EMBED_MODEL}"; then
    ok "Embedding model ${RAG_EMBED_MODEL} is available"
else
    info "Pulling embedding model ${RAG_EMBED_MODEL} (required for RAG knowledge base)..."
    ollama pull "$RAG_EMBED_MODEL"
    ok "Embedding model ${RAG_EMBED_MODEL} ready"
fi

# ── 2.5. LLM Provider ───────────────────────────────────────────────────────────
hdr "2.5. LLM Provider"

echo "  Choose how Pueo runs LLM inference:"
echo "    local  — Ollama only (default, no WAN; privacy-first)"
echo "    cloud  — Anthropic Claude API as primary (requires ANTHROPIC_API_KEY)"
echo "    both   — Ollama for autonomous cycles + Claude available for HITL escalation"
echo
ask "LLM provider (local/cloud/both)" "local" LLM_PROVIDER
CLOUD_MODEL_VAL="claude-sonnet-4-5"

if [[ "$LLM_PROVIDER" == "cloud" || "$LLM_PROVIDER" == "both" ]]; then
    ask "Claude model" "claude-sonnet-4-5" CLOUD_MODEL_VAL
    echo
    if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
        warn "ANTHROPIC_API_KEY is not set in the current environment."
        warn "Pueo will fail to start until it is exported."
        warn "Add this line to ~/.zshenv and reload your shell:"
        warn "  export ANTHROPIC_API_KEY=<your-key>"
    else
        ok "ANTHROPIC_API_KEY is set"
    fi
    if [[ "$LLM_PROVIDER" == "cloud" ]]; then
        info "Ollama inference model pull skipped (cloud mode — not needed for inference)."
        info "nomic-embed-text was already pulled above for RAG embeddings."
    fi
else
    ok "Using local Ollama inference (no cloud API required)"
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
    echo "  (Create at: HA Profile → Security → Long-Lived Access Tokens)"
    ask "HA long-lived access token"        ""                              HA_API_TOKEN
    echo
    echo "  ── HA polling features (require api_token) ──"
    echo "  When an api_token is set, Pueo can poll for HA updates and surface"
    echo "  persistent HA notifications as HITL cards on the dashboard."
    echo "  Set the update check interval to 0 to disable update checking."
    ask "Update check interval (hours, 0 = disabled)"  "6"  HA_UPDATE_CHECK_INTERVAL_HOURS
    echo
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
    echo
    echo "  Chat tool registration allows the conversational agent to write and register"
    echo "  new Python tools at runtime. Each tool requires sandbox CI validation and"
    echo "  explicit HITL approval before it is loaded, but the agent can still generate"
    echo "  arbitrary code. Leave disabled unless you understand the risk."
    ask "Allow chat agent to register new tools? (true/false)"  "false"  CHAT_ALLOW_TOOL_REGISTRATION
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
    echo "  NetAlertX is a network-presence monitor that Pueo can install"
    echo "  and manage automatically once the Pueo supervisor is running."
    echo "  If you say yes, Pueo will install and configure it on first start."
    echo "  You can change this later by editing netalertx.setup_desired in config.yaml."
    echo
    read -rp "  Set up NetAlertX automatically when Pueo runs? [Y/n]: " nax_setup_ans
    NAX_SETUP_DESIRED=false
    [[ "${nax_setup_ans:-Y}" =~ ^[Yy] ]] && NAX_SETUP_DESIRED=true
    echo
    echo "  NOTE: The NetAlertX API token can only be generated AFTER NetAlertX is"
    echo "  installed. If this is your first run, press Enter to skip."
    echo "  Once NetAlertX is running, find it at:"
    echo "    NetAlertX web UI → Settings → Main Settings → API Key"
    echo "  Then re-run setup.sh or edit config.yaml directly."
    echo
    ask "NetAlertX API token (blank = set after first install)"  ""  NAX_API_TOKEN

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
    echo "  use to connect."
    echo "  Step 1 — Create a dedicated HA user:"
    echo "    Settings → People → Users → Add User (enable 'Local access only')"
    echo "  Step 2 — Enable auth in the Mosquitto add-on:"
    echo "    Settings → Apps → Mosquitto broker → Configuration"
    echo "    Add the user to the 'logins:' list and save."
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
  api_port: 8123
  config_path: "${HA_CONFIG_PATH}"
  known_version: "${HA_KNOWN_VERSION}"

ollama:
  model: "${OLLAMA_MODEL}"
  endpoint: "http://localhost:11434"

llm:
  provider: "${LLM_PROVIDER}"

cloud:
  model: "${CLOUD_MODEL_VAL}"
  max_cost_per_incident_usd: 0.50
  max_daily_spend_usd: 5.00
  # ANTHROPIC_API_KEY must be exported in ~/.zshenv — never written here

netalertx:
  setup_desired: ${NAX_SETUP_DESIRED}
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
  timeline_page_size: 25             # Number of events shown per page on the Timeline tab
  chat_allow_tool_registration: ${CHAT_ALLOW_TOOL_REGISTRATION}
  ha_profile_refresh_hours: 24        # How often to rebuild the HA environment profile (integrations, versions)
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
  # resource_poll_interval_seconds: 300
  # ha_disk_warn_gb: 5.0
  # ha_disk_critical_gb: 2.0
  # ha_mem_warn_mb: 256
  # backup_offload_enabled: true
  # backup_local_dir: "./backups/"
  # backup_retain_on_ha: 2
  # backup_retain_local_days: 30
  update_check_interval_hours: ${HA_UPDATE_CHECK_INTERVAL_HOURS}
  notification_poll_interval_minutes: 5
  ha_repair_poll_interval_minutes: 5
  # update_notify_on_available: true
EOF
    ok "config.yaml written"
fi

# ── 5. NetAlertX ──────────────────────────────────────────────────────────────────
hdr "5. NetAlertX"

if [[ "$NAX_SETUP_DESIRED" == "true" ]]; then
    ok "NetAlertX will be installed and configured automatically when Pueo starts."
    info "Approve the HITL cards that appear on the dashboard to proceed through each setup step."
else
    info "NetAlertX setup is disabled."
    info "To enable: set 'netalertx.setup_desired: true' in config.yaml and restart Pueo."
fi

# ── 6. launchd service ───────────────────────────────────────────────────────────
hdr "6. launchd Service"

PLIST_LABEL="com.pueo.agent"
PLIST_TARGET="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

if launchctl list "$PLIST_LABEL" &>/dev/null 2>&1; then
    ok "Pueo launchd service is already installed and loaded"
else
    echo
    read -rp "  Install Pueo as a launchd service (auto-start at login)? [Y/n]: " install_svc
    if [[ "${install_svc:-Y}" =~ ^[Yy] ]]; then
        PUEO_DIR="$(pwd)"
        PYTHON_PATH="${PUEO_DIR}/.venv/bin/python"
        sed -e "s|{{ PUEO_DIR }}|${PUEO_DIR}|g" \
            -e "s|{{ PYTHON_PATH }}|${PYTHON_PATH}|g" \
            deploy/pueo.launchd.plist.template > "$PLIST_TARGET"
        launchctl load -w "$PLIST_TARGET"
        ok "Pueo service installed and started: ${PLIST_LABEL}"
        info "Pueo will start automatically at login and restart on crash."
        info "Dashboard → http://127.0.0.1:8080"
    else
        info "Skipped — start manually: python main.py"
    fi
fi

# ── 7. RAG refresh launchd job ───────────────────────────────────────────────────
hdr "7. RAG Knowledge-Base Refresh"

RAG_PLIST_LABEL="io.pueo.rag-refresh"
RAG_PLIST_TARGET="$HOME/Library/LaunchAgents/${RAG_PLIST_LABEL}.plist"

if launchctl list "$RAG_PLIST_LABEL" &>/dev/null 2>&1; then
    ok "RAG refresh launchd job is already installed"
else
    echo
    echo "  Pueo uses a local ChromaDB vector store (RAG) for HA knowledge: release"
    echo "  notes (last N versions), HACS integration changelogs (auto-discovered"
    echo "  from your HA instance), and HA integration documentation. A weekly"
    echo "  launchd job fetches and re-embeds this content every Sunday at 03:00."
    echo "  Optional config keys: rag_ha_versions_to_fetch, rag_hacs_cache_dir,"
    echo "  rag_ha_docs_cache_dir — see config.yaml.default for details."
    echo
    read -rp "  Install the weekly RAG refresh job? [Y/n]: " install_rag
    if [[ "${install_rag:-Y}" =~ ^[Yy] ]]; then
        PUEO_DIR="$(pwd)"
        sed -e "s|/path/to/pueo|${PUEO_DIR}|g" \
            -e "s|python3|${PUEO_DIR}/.venv/bin/python|g" \
            deploy/pueo-rag-refresh.plist > "$RAG_PLIST_TARGET"
        launchctl load -w "$RAG_PLIST_TARGET"
        ok "RAG refresh job installed: ${RAG_PLIST_LABEL} (runs Sundays at 03:00)"
        info "Run immediately: launchctl start ${RAG_PLIST_LABEL}"
    else
        info "Skipped — run manually: python main.py --mode rag-refresh"
    fi
fi

# ── 8. pueo command ──────────────────────────────────────────────────────────────
hdr "8. pueo Command"

PUEO_BIN="$SCRIPT_DIR/bin/pueo"
PUEO_LINK="/usr/local/bin/pueo"

chmod +x "$PUEO_BIN"
if ln -sf "$PUEO_BIN" "$PUEO_LINK" 2>/dev/null; then
    ok "pueo command installed at $PUEO_LINK"
else
    warn "Could not write to /usr/local/bin (try: sudo ln -sf \"$PUEO_BIN\" $PUEO_LINK)"
    info "Or add $SCRIPT_DIR/bin to your PATH manually"
fi

# ── Done ─────────────────────────────────────────────────────────────────────────
echo
echo -e "${GREEN}${BOLD}✔  Pueo is ready.${NC}"
echo
echo "  Start Pueo           : pueo"
echo "  Live log              : tail -f pueo.log"
echo
echo "  Other modes (in venv): source .venv/bin/activate"
echo "    python main.py --mode monitor"
echo "    python main.py --mode diagnose"
echo "    python main.py --mode dashboard"
echo
echo "  NetAlertX install    : python main.py --mode netalertx-setup"
echo "  NetAlertX diagnose   : python main.py --mode netalertx-diagnose"
echo
