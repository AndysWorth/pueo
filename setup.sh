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
ask_secret() {
    # ask_secret "Prompt" varname  — input is not echoed
    local answer
    read -rsp "  $1: " answer; echo
    printf -v "$2" '%s' "${answer}"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
PUEO_DIR="$SCRIPT_DIR"

# ── --help / --clean flags ───────────────────────────────────────────────────────
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    echo -e "\nUsage: ./setup.sh [--clean]"
    echo
    echo "  (no flags)   Interactive setup: choose native/docker/both mode,"
    echo "               install dependencies, configure Pueo, and set up"
    echo "               infrastructure. Safe to re-run at any time."
    echo
    echo "  --clean      Remove .venv and all Pueo platform-directory state"
    echo "               (DB, HITL cards, caches, logs) before running setup."
    echo "               Use this to start completely from scratch."
    echo
    echo "  -h, --help   Show this help message."
    exit 0
fi

if [[ "${1:-}" == "--clean" ]]; then
    echo -e "\n${YELLOW}⚠  Clean mode — this will remove all Pueo state:${NC}"
    echo "  .venv, config.yaml, and platform-directory state (DB, HITL cards,"
    echo "  caches, backups, ChromaDB, logs)."
    echo
    read -rp "  Continue? [y/N]: " clean_confirm
    if [[ ! "${clean_confirm:-N}" =~ ^[Yy] ]]; then
        info "Clean cancelled."
        exit 0
    fi
    rm -rf .venv
    rm -f config.yaml
    # Remove legacy repo-root artifacts from pre-platform-dir layout
    rm -f ha_agent_state.db pueo.log
    rm -rf hitl/ .cache/ backups/ chromadb/ archives/
    # Remove platform dirs if they exist (derive defaults without venv)
    _STATE="${PUEO_STATE_DIR:-${HOME}/Library/Application Support/Pueo}"
    _DATA="${PUEO_DATA_DIR:-${HOME}/Library/Application Support/Pueo}"
    _CACHE="${PUEO_CACHE_DIR:-${HOME}/Library/Caches/Pueo}"
    _LOGS="${PUEO_LOG_DIR:-${HOME}/Library/Logs/Pueo}"
    rm -rf "$_STATE" "$_DATA" "$_CACHE" "$_LOGS"
    ok "Removed .venv, config.yaml, and all Pueo state directories"
fi

echo -e "\n🦉  ${BOLD}Pueo Setup${NC}"
echo "════════════════════════════════════════"

# ── 0. Deployment Mode ──────────────────────────────────────────────────────────
hdr "0. Deployment Mode"

echo "  How will you run Pueo?"
echo "    1) native  — macOS (launchd, ~/Library/* dirs)"
echo "    2) docker  — Docker container (docker-compose)"
echo "    3) both    — native + Docker side-by-side"
echo
ask "Deployment mode [1/2/3]" "1" _DEPLOY_MODE_NUM
case "${_DEPLOY_MODE_NUM}" in
    2) DEPLOY_MODE="docker" ;;
    3) DEPLOY_MODE="both" ;;
    *) DEPLOY_MODE="native" ;;
esac
ok "Deployment mode: ${DEPLOY_MODE}"

# ── 1. Python ───────────────────────────────────────────────────────────────────
hdr "1. Python"

REQUIRED_PYTHON="3.14"

# Docker-only: skip venv; derive Docker config destination
if [[ "$DEPLOY_MODE" == "docker" ]]; then
    info "Docker-only mode — skipping venv creation (not needed inside the container)."
    DOCKER_CONFIG_DIR="${PUEO_DIR}/config"
    NATIVE_CONFIG_DIR=""
    PUEO_CONFIG_DIR="$DOCKER_CONFIG_DIR"
    PUEO_STATE_DIR=""
    PUEO_DATA_DIR=""
    PUEO_CACHE_DIR=""
    PUEO_LOG_DIR=""
else
    # native or both: create venv and resolve platform dirs
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

    # Resolve platform-appropriate directories now that platformdirs is installed
    PUEO_CONFIG_DIR=$(.venv/bin/python -c "import sys; sys.path.insert(0,'$SCRIPT_DIR'); import paths; d=paths.get_dirs(); print(d.config_dir)" 2>/dev/null || echo "${HOME}/.config/pueo")
    PUEO_STATE_DIR=$(.venv/bin/python -c "import sys; sys.path.insert(0,'$SCRIPT_DIR'); import paths; d=paths.get_dirs(); print(d.state_dir)" 2>/dev/null || echo "${HOME}/Library/Application Support/Pueo")
    PUEO_DATA_DIR=$(.venv/bin/python -c "import sys; sys.path.insert(0,'$SCRIPT_DIR'); import paths; d=paths.get_dirs(); print(d.data_dir)" 2>/dev/null || echo "${HOME}/Library/Application Support/Pueo")
    PUEO_CACHE_DIR=$(.venv/bin/python -c "import sys; sys.path.insert(0,'$SCRIPT_DIR'); import paths; d=paths.get_dirs(); print(d.cache_dir)" 2>/dev/null || echo "${HOME}/Library/Caches/Pueo")
    PUEO_LOG_DIR=$(.venv/bin/python -c "import sys; sys.path.insert(0,'$SCRIPT_DIR'); import paths; d=paths.get_dirs(); print(d.log_dir)" 2>/dev/null || echo "${HOME}/Library/Logs/Pueo")

    NATIVE_CONFIG_DIR="$PUEO_CONFIG_DIR"
    DOCKER_CONFIG_DIR="${PUEO_DIR}/config"
fi

# ── 2. Ollama ───────────────────────────────────────────────────────────────────
hdr "2. Ollama"

if [[ "$DEPLOY_MODE" == "docker" ]]; then
    info "Docker-only mode — skipping local Ollama install check."
    info "Ollama must run on the host; its endpoint will be configured in the next section."
    OLLAMA_ENDPOINT_DEFAULT="http://host.docker.internal:11434"
    ask "Ollama endpoint (Docker host sees it as)" "$OLLAMA_ENDPOINT_DEFAULT" OLLAMA_ENDPOINT_FOR_CONFIG
    CONFIGURED_MODEL="qwen2.5-coder:7b"
    DEFAULT_MODEL="$CONFIGURED_MODEL"
    RAG_EMBED_MODEL="nomic-embed-text"
else
    OLLAMA_ENDPOINT_FOR_CONFIG="http://localhost:11434"

    if ! command -v ollama &>/dev/null; then
        fail "ollama CLI not found. Install from https://ollama.com then re-run."
        exit 1
    fi
    ok "ollama found"

    # Check if Ollama is responding; try to start it if not
    if ! ollama list &>/dev/null 2>&1; then
        warn "Ollama is not running — attempting to start..."
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

    # Detect hardware and recommend the best model for this machine
    RAM_BYTES=$(sysctl -n hw.memsize 2>/dev/null || \
        awk '/MemTotal/ {print $2 * 1024}' /proc/meminfo 2>/dev/null || echo "0")
    RAM_GB=$(echo "$RAM_BYTES" | awk '{printf "%d", $1/1073741824}')
    CHIP=$(system_profiler SPHardwareDataType 2>/dev/null | \
        awk -F'Chip: ' 'NF>1{print $2; exit}' | xargs 2>/dev/null || \
        awk -F': ' '/model name/{print $2; exit}' /proc/cpuinfo 2>/dev/null || echo "Unknown")
    info "Hardware: ${CHIP} — ${RAM_GB} GB RAM"

    if   [ "${RAM_GB}" -ge 48 ]; then RECOMMENDED_MODEL="qwen2.5-coder:32b"
    elif [ "${RAM_GB}" -ge 20 ]; then RECOMMENDED_MODEL="qwen2.5-coder:14b"
    elif [ "${RAM_GB}" -ge 10 ]; then RECOMMENDED_MODEL="qwen2.5-coder:7b"
    else                               RECOMMENDED_MODEL="qwen2.5-coder:7b"
    fi
    info "Recommended model for your hardware: ${RECOMMENDED_MODEL}"

    # Read model from existing config if present, else use hardware recommendation
    DEFAULT_MODEL="${RECOMMENDED_MODEL}"
    # Extract model from the `ollama:` section only — avoid matching `cloud.model`
    _extract_ollama_model() {
        awk '/^ollama:/{in_ollama=1} in_ollama && /^[^ ]/{if(!/^ollama:/)in_ollama=0} in_ollama && /^ *model:/{gsub(/[" ]/,"",$2); print $2; exit}' "$1"
    }
    if [[ -f "${NATIVE_CONFIG_DIR}/config.yaml" ]]; then
        CONFIGURED_MODEL=$(_extract_ollama_model "${NATIVE_CONFIG_DIR}/config.yaml")
        [[ -z "$CONFIGURED_MODEL" ]] && CONFIGURED_MODEL="$DEFAULT_MODEL"
    elif [[ -f "config.yaml" ]]; then
        CONFIGURED_MODEL=$(_extract_ollama_model "config.yaml")
        [[ -z "$CONFIGURED_MODEL" ]] && CONFIGURED_MODEL="$DEFAULT_MODEL"
    else
        CONFIGURED_MODEL="$DEFAULT_MODEL"
    fi

    if ollama show "${CONFIGURED_MODEL}" &>/dev/null; then
        ok "Model ${CONFIGURED_MODEL} is available"
    else
        info "Pulling model ${CONFIGURED_MODEL} (this may take several minutes)..."
        ollama pull "$CONFIGURED_MODEL"
        ok "Model ${CONFIGURED_MODEL} ready"
    fi

    RAG_EMBED_MODEL="nomic-embed-text"
    if ollama show "${RAG_EMBED_MODEL}" &>/dev/null; then
        ok "Embedding model ${RAG_EMBED_MODEL} is available"
    else
        info "Pulling embedding model ${RAG_EMBED_MODEL} (required for RAG knowledge base)..."
        ollama pull "$RAG_EMBED_MODEL"
        ok "Embedding model ${RAG_EMBED_MODEL} ready"
    fi
fi

# ── 2.5. LLM Provider ───────────────────────────────────────────────────────────
hdr "2.5. LLM Provider"

echo "  Choose how Pueo runs LLM inference:"
echo "    local  — Ollama only (default, no WAN; privacy-first)"
echo "    cloud  — Anthropic Claude API as primary (requires ANTHROPIC_API_KEY)"
echo "    both   — Ollama for autonomous cycles + Claude available for approved escalation"
echo
ask "LLM provider (local/cloud/both)" "local" LLM_PROVIDER
CLOUD_MODEL_VAL="claude-sonnet-5"

if [[ "$LLM_PROVIDER" == "cloud" || "$LLM_PROVIDER" == "both" ]]; then
    ask "Claude model" "claude-sonnet-5" CLOUD_MODEL_VAL
    echo
    if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
        warn "ANTHROPIC_API_KEY is not set in the current environment."
        warn "Pueo will fail to start until it is exported."
        if [[ "$DEPLOY_MODE" == "docker" || "$DEPLOY_MODE" == "both" ]]; then
            warn "For Docker: set it in the environment: section of docker-compose.yml"
            warn "  or supply a .env file alongside docker-compose.yml."
        fi
        warn "For native: add this line to ~/.zshenv and reload your shell:"
        warn "  export ANTHROPIC_API_KEY=<your-key>"
    else
        ok "ANTHROPIC_API_KEY is set"
    fi
    if [[ "$LLM_PROVIDER" == "cloud" && "$DEPLOY_MODE" != "docker" ]]; then
        info "Ollama inference model pull skipped (cloud mode — not needed for inference)."
        info "nomic-embed-text was already pulled above for RAG embeddings."
    fi
else
    ok "Using local Ollama inference (no cloud API required)"
fi

if [[ "$DEPLOY_MODE" != "docker" ]]; then
    echo
    echo "  Pueo can automatically select the best Ollama model for your hardware"
    echo "  each time it starts. Useful as you add or remove larger models over time."
    ask "Auto-select best model at startup? (true/false)" "false" OLLAMA_MODEL_AUTO
else
    OLLAMA_MODEL_AUTO="false"
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

if [[ "$DEPLOY_MODE" != "docker" ]]; then
    # ── SSH agent ────────────────────────────────────────────────────────────────
    echo
    info "Checking SSH agent..."
    if [[ -z "${SSH_AUTH_SOCK:-}" ]]; then
        warn "SSH_AUTH_SOCK is not set — the SSH agent may not be running."
        warn "Pueo uses asyncssh, which cannot prompt for a key passphrase."
        warn "If your key has a passphrase, add it to the macOS keychain:"
        warn "  ssh-add --apple-use-keychain ${DEFAULT_SSH_KEY}"
        warn "Then re-run this script, or run Pueo from a shell where the agent is active."
    else
        if ssh-add -l 2>/dev/null | grep -q "${DEFAULT_SSH_KEY}"; then
            ok "SSH agent running and key is loaded"
        else
            warn "SSH agent is running but ${DEFAULT_SSH_KEY} is not loaded."
            warn "If the key has a passphrase, add it with:"
            warn "  ssh-add --apple-use-keychain ${DEFAULT_SSH_KEY}"
        fi
    fi
fi

# ── 4. Configuration ────────────────────────────────────────────────────────────
hdr "4. Configuration"

# Determine config destinations
if [[ "$DEPLOY_MODE" == "native" ]]; then
    CONFIG_DEST_NATIVE="${NATIVE_CONFIG_DIR}/config.yaml"
    CONFIG_DEST_DOCKER=""
elif [[ "$DEPLOY_MODE" == "docker" ]]; then
    CONFIG_DEST_NATIVE=""
    CONFIG_DEST_DOCKER="${DOCKER_CONFIG_DIR}/config.yaml"
else
    # both
    CONFIG_DEST_NATIVE="${NATIVE_CONFIG_DIR}/config.yaml"
    CONFIG_DEST_DOCKER="${DOCKER_CONFIG_DIR}/config.yaml"
fi

WRITE_CONFIG=false
PRIMARY_CONFIG="${CONFIG_DEST_NATIVE:-$CONFIG_DEST_DOCKER}"
if [[ -f "$PRIMARY_CONFIG" ]]; then
    ok "config.yaml already exists at ${PRIMARY_CONFIG}"
    read -rp "  Reconfigure? [y/N]: " reconf
    [[ "${reconf:-N}" =~ ^[Yy] ]] && WRITE_CONFIG=true
else
    WRITE_CONFIG=true
fi

# Defaults that may already be set in docker-only path (no Ollama section ran)
CONFIGURED_MODEL="${CONFIGURED_MODEL:-qwen2.5-coder:7b}"

# Initialize unbound variables before the NAX prompts to avoid set -u failures
NAX_DOCKER_CONFIG_PATH=""
NAX_API_TOKEN=""
HA_KNOWN_VERSION=""

if $WRITE_CONFIG; then
    echo "  Press Enter to accept each default."
    echo

    ask "Home Assistant hostname or IP"    "homeassistant.local"          HA_HOST
    ask "SSH username"                      "root"                          HA_USER
    ask "SSH private key path"             "$DEFAULT_SSH_KEY"              HA_SSH_KEY
    echo "  (Create at: HA Profile → Security → Long-Lived Access Tokens)"
    ask_secret "HA long-lived access token (hidden)" HA_API_TOKEN
    echo
    echo "  ── HA polling features (require api_token) ──"
    echo "  When an api_token is set, Pueo can poll for HA updates and surface"
    echo "  persistent HA notifications as approval cards on the dashboard."
    echo "  Set the update check interval to 0 to disable update checking."
    ask "Update check interval (hours, 0 = disabled)"  "6"  HA_UPDATE_CHECK_INTERVAL_HOURS
    echo
    ask "config.yaml path on HA host"      "/config/configuration.yaml"    HA_CONFIG_PATH
    if [[ "$DEPLOY_MODE" != "docker" ]]; then
        if [[ "$CONFIGURED_MODEL" != "${DEFAULT_MODEL:-$CONFIGURED_MODEL}" ]]; then
            info "Hardware recommendation: ${DEFAULT_MODEL} (press Enter to keep current, or type the new model name)"
        fi
    fi
    ask "Ollama model"                      "$CONFIGURED_MODEL"             OLLAMA_MODEL
    if ! [[ "$OLLAMA_MODEL" =~ ^[a-zA-Z0-9./:_-]+$ ]]; then
        warn "Model name '${OLLAMA_MODEL}' looks invalid. Using default: ${CONFIGURED_MODEL}"
        OLLAMA_MODEL="$CONFIGURED_MODEL"
    fi
    if [[ "$DEPLOY_MODE" != "docker" && "$OLLAMA_MODEL" != "$CONFIGURED_MODEL" ]]; then
        if ! ollama show "${OLLAMA_MODEL}" &>/dev/null; then
            warn "Model ${OLLAMA_MODEL} is not installed locally."
            read -rp "  Pull it now? [Y/n]: " pull_new_model
            if [[ "${pull_new_model:-Y}" =~ ^[Yy] ]]; then
                info "Pulling ${OLLAMA_MODEL} (this may take several minutes)..."
                ollama pull "${OLLAMA_MODEL}"
                ok "Model ${OLLAMA_MODEL} ready"
            else
                warn "Skipped. Run 'ollama pull ${OLLAMA_MODEL}' before starting Pueo."
            fi
        fi
    fi

    if [[ -n "${PUEO_STATE_DIR:-}" ]]; then
        DB_PATH_DEFAULT="${PUEO_STATE_DIR}/ha_agent_state.db"
    else
        DB_PATH_DEFAULT="/state/ha_agent_state.db"
    fi
    ask "Local SQLite database path"        "$DB_PATH_DEFAULT"  DB_PATH
    ask "Log confidence threshold (0–1)"    "0.7"               LOG_THRESHOLD
    ask "Self-healing enabled"              "true"              SELF_HEALING

    echo
    echo "  ── Approval notifications ──"
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
    ask "Autonomy level (1=report-only 2=suggest 3=guided 4=autonomous)"  "2"  AUTONOMY_LEVEL
    ask "Dashboard port"  "8080"  DASHBOARD_PORT
    echo
    echo "  Development mode unlocks advanced surfaces: Runbook Review dashboard tab,"
    echo "  community knowledge-base submission, cloud escalation approval cards, code"
    echo "  proposals, and the debug logging toggle. Disable for appliance / production use."
    ask "Enable development mode? (true/false)"  "false"  DEVELOPMENT_MODE
    echo
    echo "  Chat tool registration allows the conversational agent to write and register"
    echo "  new Python tools at runtime. Each tool requires sandbox CI validation and"
    echo "  explicit approval before it is loaded, but the agent can still generate"
    echo "  arbitrary code. Leave disabled unless you understand the risk."
    ask "Allow chat agent to register new tools? (true/false)"  "false"  CHAT_ALLOW_TOOL_REGISTRATION
    echo ""
    echo "  Diagnostic WAN access lets Pueo verify external API availability during"
    echo "  investigations (e.g., confirming a service outage has resolved)."
    echo "  Uses HTTP GET only; private/loopback addresses are always blocked."
    ask "Allow diagnostic WAN fetch (fetch_url tool)? (true/false)"  "true"  ALLOW_DIAGNOSTIC_WAN
    ask "Notifier type (file/ntfy/webhook)"  "file"                          NOTIFIER_TYPE

    NOTIFY_URL=""
    if [[ -n "${PUEO_STATE_DIR:-}" ]]; then
        NOTIFY_WATCH_DIR_DEFAULT="${PUEO_STATE_DIR}/hitl"
    else
        NOTIFY_WATCH_DIR_DEFAULT="/state/hitl"
    fi
    NOTIFY_WATCH_DIR="$NOTIFY_WATCH_DIR_DEFAULT"

    if [[ "$NOTIFIER_TYPE" == "ntfy" ]]; then
        echo
        echo "  ntfy topic URL format: https://ntfy.sh/<your-topic>"
        echo "  Pick a unique topic name — anyone who knows it can see your alerts."
        echo "  For self-hosted ntfy use: https://ntfy.example.com/<topic>"
        ask "ntfy topic URL"  "https://ntfy.sh/pueo-$(openssl rand -hex 8)"  NOTIFY_URL
        ask "Approval watch directory"  "$NOTIFY_WATCH_DIR_DEFAULT"  NOTIFY_WATCH_DIR
        echo
        echo "  To approve a pending repair (from this machine or via SSH):"
        echo "    touch ${NOTIFY_WATCH_DIR}/<notification-id>.approved"
        echo "  To reject:"
        echo "    touch ${NOTIFY_WATCH_DIR}/<notification-id>.rejected"
    elif [[ "$NOTIFIER_TYPE" == "webhook" ]]; then
        ask "Webhook URL"  ""  NOTIFY_URL
    else
        ask "Approval watch directory"  "$NOTIFY_WATCH_DIR_DEFAULT"  NOTIFY_WATCH_DIR
        NOTIFIER_TYPE="file"
    fi

    # ── SSH connectivity, HA version, and log file check ─────────────────────────
    echo
    info "Testing SSH connection to ${HA_HOST}..."
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

    # Deploy target: HA add-on (default) or separate Docker machine
    NAX_DEPLOY_TARGET="ha"
    NAX_DOCKER_HOST=""
    NAX_DOCKER_SSH_USER=""
    NAX_DOCKER_SSH_KEY_PATH=""
    if $NAX_SETUP_DESIRED; then
        echo
        echo "  ── NetAlertX deploy target ─────────────────────────────────────"
        echo "  NetAlertX can be installed as a Home Assistant add-on (default)"
        echo "  or on a separate machine running Docker (recommended if HA disk"
        echo "  space is limited — the add-on database grows with your network)."
        echo
        read -rp "  Install on separate Docker machine instead of HA? [y/N]: " nax_docker_ans
        if [[ "${nax_docker_ans:-N}" =~ ^[Yy] ]]; then
            NAX_DEPLOY_TARGET="docker"
            echo
            ask "Docker host IP or hostname" "" NAX_DOCKER_HOST
            ask "SSH user on Docker host" "$(whoami)" NAX_DOCKER_SSH_USER
            ask "SSH key path for Docker host (blank = same as HA key)" "" NAX_DOCKER_SSH_KEY_PATH
            echo
            echo "  NetAlertX config files will be written to this directory on the Docker host."
            echo "  The directory MUST be writable by the SSH user."
            ask "NetAlertX config path on Docker host" "/opt/netalertx/config" NAX_DOCKER_CONFIG_PATH
            if [[ -n "$NAX_DOCKER_HOST" ]]; then
                _DOCKER_SSH="ssh -i ${NAX_DOCKER_SSH_KEY_PATH:-$HA_SSH_KEY} \
                    -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
                    ${NAX_DOCKER_SSH_USER:-$(whoami)}@${NAX_DOCKER_HOST}"
                if $_DOCKER_SSH "echo ok" &>/dev/null 2>&1; then
                    ok "SSH to Docker host (${NAX_DOCKER_HOST}) succeeded"
                    docker_avail_gb=$({ $_DOCKER_SSH "df -k /opt 2>/dev/null || df -k /" 2>/dev/null || true; } \
                        | awk 'NR==2{printf "%d", $4/1048576}')
                    if [[ -n "$docker_avail_gb" && "$docker_avail_gb" -ge 5 ]]; then
                        ok "Docker host disk: ${docker_avail_gb} GB free (≥ 5 GB required)"
                    elif [[ -n "$docker_avail_gb" ]]; then
                        warn "Docker host disk: only ${docker_avail_gb} GB free (5 GB recommended)"
                        warn "NetAlertX install may fail. Free space before running."
                    fi
                else
                    warn "SSH to Docker host (${NAX_DOCKER_HOST}) failed — check credentials."
                    warn "You can edit config.yaml later and run: pueo --mode netalertx-docker-setup"
                fi
            fi
        fi

        echo
        echo "  NOTE: The NetAlertX API token can only be generated AFTER NetAlertX is"
        echo "  installed. If this is your first run, press Enter to skip."
        echo "  Once NetAlertX is running, find it at:"
        echo "    NetAlertX web UI → Settings → Main Settings → API Key"
        echo "  Then re-run setup.sh or edit config.yaml directly."
        echo
        ask_secret "NetAlertX API token, blank = set after first install (hidden)" NAX_API_TOKEN
    fi

    # ── Mosquitto MQTT broker ─────────────────────────────────────────────────
    echo
    echo "  ── MQTT (Mosquitto broker) ─────────────────────────────────────"
    MQTT_USER=""
    MQTT_PASSWORD=""
    _SSH_OK=false
    if $_SSH "echo ok" &>/dev/null; then
        _SSH_OK=true
        mosquitto_state=$($_SSH "ha apps info core_mosquitto 2>/dev/null | grep -E '^\s*state:' | awk '{print \$2}'" 2>/dev/null || echo "")
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

    # ── Write native config ──────────────────────────────────────────────────────
    _write_config() {
        local dest="$1"
        local ollama_endpoint="$2"
        mkdir -p "$(dirname "$dest")"
        cat > "$dest" <<EOF
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
  model_auto: ${OLLAMA_MODEL_AUTO}
  endpoint: "${ollama_endpoint}"

llm:
  provider: "${LLM_PROVIDER}"

cloud:
  model: "${CLOUD_MODEL_VAL}"
  max_cost_per_incident_usd: 0.50
  max_daily_spend_usd: 5.00
  # ANTHROPIC_API_KEY must be exported in the environment — never written here

netalertx:
  enabled: ${NAX_SETUP_DESIRED}
  setup_desired: ${NAX_SETUP_DESIRED}
  api_token: "${NAX_API_TOKEN}"
  deploy_target: "${NAX_DEPLOY_TARGET}"
  docker_host: "${NAX_DOCKER_HOST}"
  docker_ssh_user: "${NAX_DOCKER_SSH_USER}"
  docker_ssh_key_path: "${NAX_DOCKER_SSH_KEY_PATH}"
  docker_config_path: "${NAX_DOCKER_CONFIG_PATH}"
  docker_image: "ghcr.io/netalertx/netalertx:latest"
  docker_min_disk_gb: 5.0
  # Advanced tuning — edit config.yaml directly to override these defaults:
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
  autonomy_level: ${AUTONOMY_LEVEL}
  escalation_preference: "hitl"     # hitl | cloud | cloud_then_hitl — where to route when agent is stuck
  dashboard_port: ${DASHBOARD_PORT}
  timeline_page_size: 25             # Number of events shown per page on the Timeline tab
  development_mode: ${DEVELOPMENT_MODE}
  chat_allow_tool_registration: ${CHAT_ALLOW_TOOL_REGISTRATION}
  allow_diagnostic_wan: ${ALLOW_DIAGNOSTIC_WAN}
  ha_profile_refresh_hours: 24        # How often to rebuild the HA environment profile (integrations, versions)
  notifier: "${NOTIFIER_TYPE}"
  notify_url: "${NOTIFY_URL}"
  notify_watch_dir: "${NOTIFY_WATCH_DIR}"
  # Advanced tuning — edit config.yaml directly to override these defaults:
  # agent_max_tool_result_tokens: 4000  # Guardrail: truncate tool outputs exceeding this token count
  # ssh_retry_attempts: 3
  # ssh_retry_base_delay: 2.0
  # debounce_window_seconds: 30
  # repair_cooldown_seconds: 300
  # max_repairs_per_hour: 10
  # log_triage_cooldown_hours: 4  # Min hours between approval cards for same recurring log error
  # rejection_cooldown_hours: 24  # Hours a card is suppressed after rejection (doubles on repeat rejections)
  # known_issue_reminder_days: 7  # Days before a Known Issue generates a reminder card
  # log_level: INFO
  # log_file: pueo.log
  # max_prompt_tokens: 7000
  # resource_poll_interval_seconds: 300
  # ha_disk_warn_gb: 5.0
  # ha_disk_critical_gb: 3.0  # keep above 1.0 (HA Supervisor hard-blocks backups below 1 GB); 3.0 leaves a 2 GB write buffer
  # ha_mem_warn_mb: 256
  # backup_offload_enabled: true
  # backup_local_dir: ""     # default: data_dir/backups
  # backup_retain_on_ha: 2
  # backup_retain_local_days: 30
  # disk_recovery_auto_enabled: true
  # disk_recovery_recorder_keep_days: 30
  # disk_recovery_journal_max_mb: 200
  update_check_interval_hours: ${HA_UPDATE_CHECK_INTERVAL_HOURS}
  notification_poll_interval_minutes: 5
  ha_repair_poll_interval_minutes: 5
  lovelace_check_interval_minutes: 30
  # update_notify_on_available: true
EOF
    }

    if [[ "$DEPLOY_MODE" == "native" || "$DEPLOY_MODE" == "both" ]]; then
        _write_config "$CONFIG_DEST_NATIVE" "http://localhost:11434"
        ok "Native config written: ${CONFIG_DEST_NATIVE}"
    fi
    if [[ "$DEPLOY_MODE" == "docker" || "$DEPLOY_MODE" == "both" ]]; then
        _write_config "$CONFIG_DEST_DOCKER" "${OLLAMA_ENDPOINT_FOR_CONFIG}"
        ok "Docker config written: ${CONFIG_DEST_DOCKER}"
    fi
fi

# ── 4.5. docker-compose.yml generation ─────────────────────────────────────────
if [[ "$DEPLOY_MODE" == "docker" || "$DEPLOY_MODE" == "both" ]]; then
    hdr "4.5. Docker Compose"

    mkdir -p "${PUEO_DIR}/config"

    # Resolve SSH key path for the volume mount
    _KEY_PATH="${HA_SSH_KEY:-$DEFAULT_SSH_KEY}"

    # Build the ANTHROPIC_API_KEY environment block
    if [[ "$LLM_PROVIDER" == "cloud" || "$LLM_PROVIDER" == "both" ]]; then
        _ANTHROPIC_ENV="      # ANTHROPIC_API_KEY is required for cloud/both LLM mode:
      # - ANTHROPIC_API_KEY=\${ANTHROPIC_API_KEY}"
    else
        _ANTHROPIC_ENV="      # ANTHROPIC_API_KEY is required only when LLM_PROVIDER=cloud or both.
      # Uncomment and set if you use cloud escalation:
      # - ANTHROPIC_API_KEY=\${ANTHROPIC_API_KEY}"
    fi

    cat > "${PUEO_DIR}/docker-compose.yml" <<EOF
services:
  pueo:
    build: .
    container_name: pueo-agent
    restart: unless-stopped
    network_mode: "host"
    volumes:
      - ./config:/config:ro
      - ${_KEY_PATH}:/root/.ssh/id_ed25519:ro
      - pueo-data:/data
      - pueo-state:/state
      - pueo-cache:/cache
      - pueo-logs:/logs
    environment:
      - TZ=${TZ:-America/New_York}
${_ANTHROPIC_ENV}

volumes:
  pueo-data:
  pueo-state:
  pueo-cache:
  pueo-logs:
EOF
    ok "docker-compose.yml written (SSH key mount: ${_KEY_PATH})"
    info "config.yaml is at ${CONFIG_DEST_DOCKER}"
    info "Start with: docker compose up -d"
fi

# ── 5. NetAlertX ──────────────────────────────────────────────────────────────────
hdr "5. NetAlertX"

if [[ "${NAX_SETUP_DESIRED:-false}" == "true" ]]; then
    ok "NetAlertX will be installed and configured automatically when Pueo starts."
    info "Approve the cards that appear on the dashboard to proceed through each setup step."
else
    info "NetAlertX setup is disabled."
    info "To enable: set 'netalertx.setup_desired: true' in config.yaml and restart Pueo."
fi

# ── 6. launchd service ───────────────────────────────────────────────────────────
hdr "6. launchd Service"

if [[ "$DEPLOY_MODE" == "docker" ]]; then
    info "Docker mode — skipping launchd service install."
    info "Restart policy is handled by Docker (restart: unless-stopped)."
else
    PLIST_LABEL="com.pueo.agent"
    PLIST_TARGET="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

    if launchctl list "$PLIST_LABEL" &>/dev/null 2>&1; then
        ok "Pueo launchd service is already installed and loaded"
    else
        echo
        read -rp "  Install Pueo as a launchd service (auto-start at login)? [Y/n]: " install_svc
        if [[ "${install_svc:-Y}" =~ ^[Yy] ]]; then
            PYTHON_PATH="${PUEO_DIR}/.venv/bin/python"
            mkdir -p "$PUEO_LOG_DIR"
            sed -e "s|{{ PUEO_DIR }}|${PUEO_DIR}|g" \
                -e "s|{{ PYTHON_PATH }}|${PYTHON_PATH}|g" \
                -e "s|{{ PUEO_CONFIG_DIR }}|${PUEO_CONFIG_DIR}|g" \
                -e "s|{{ PUEO_DATA_DIR }}|${PUEO_DATA_DIR}|g" \
                -e "s|{{ PUEO_STATE_DIR }}|${PUEO_STATE_DIR}|g" \
                -e "s|{{ PUEO_CACHE_DIR }}|${PUEO_CACHE_DIR}|g" \
                -e "s|{{ PUEO_LOG_DIR }}|${PUEO_LOG_DIR}|g" \
                deploy/pueo.launchd.plist.template > "$PLIST_TARGET"
            launchctl load -w "$PLIST_TARGET"
            ok "Pueo service installed and started: ${PLIST_LABEL}"
            info "Pueo will start automatically at login and restart on crash."
            info "Dashboard → http://127.0.0.1:8080"
        else
            info "Skipped — start manually: pueo"
        fi
    fi
fi

# ── 7. RAG refresh launchd job ───────────────────────────────────────────────────
hdr "7. RAG Knowledge-Base Refresh"

if [[ "$DEPLOY_MODE" == "docker" ]]; then
    info "Docker mode — skipping launchd RAG refresh job."
    info "To refresh the knowledge base in Docker:"
    info "  docker exec pueo-agent python main.py --mode rag-refresh"
else
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
        echo "  rag_ha_docs_cache_dir, ha_source_cache_dir, ha_concepts_cache_dir, case_ingest_cache_dir,"
        echo "  rag_refresh_interval_hours (default 168, i.e. weekly), pueo_kb_repo, kb_sync_interval_hours,"
        echo "  kb_sync_cache_dir — see config.yaml.default for details."
        echo
        read -rp "  Install the weekly RAG refresh job? [Y/n]: " install_rag
        if [[ "${install_rag:-Y}" =~ ^[Yy] ]]; then
            PYTHON_PATH="${PUEO_DIR}/.venv/bin/python"
            mkdir -p "$PUEO_LOG_DIR"
            sed -e "s|{{ PUEO_DIR }}|${PUEO_DIR}|g" \
                -e "s|{{ PYTHON_PATH }}|${PYTHON_PATH}|g" \
                -e "s|{{ PUEO_CONFIG_DIR }}|${PUEO_CONFIG_DIR}|g" \
                -e "s|{{ PUEO_DATA_DIR }}|${PUEO_DATA_DIR}|g" \
                -e "s|{{ PUEO_STATE_DIR }}|${PUEO_STATE_DIR}|g" \
                -e "s|{{ PUEO_CACHE_DIR }}|${PUEO_CACHE_DIR}|g" \
                -e "s|{{ PUEO_LOG_DIR }}|${PUEO_LOG_DIR}|g" \
                deploy/pueo-rag-refresh.plist.template > "$RAG_PLIST_TARGET"
            launchctl load -w "$RAG_PLIST_TARGET"
            ok "RAG refresh job installed: ${RAG_PLIST_LABEL} (runs Sundays at 03:00)"
            info "Run immediately: launchctl start ${RAG_PLIST_LABEL}"
        else
            info "Skipped — run manually: pueo --mode rag-refresh"
        fi
    fi
fi

# ── 8. pueo command ──────────────────────────────────────────────────────────────
hdr "8. pueo Command"

if [[ "$DEPLOY_MODE" == "docker" ]]; then
    info "Docker mode — skipping pueo symlink install."
else
    PUEO_BIN="$SCRIPT_DIR/bin/pueo"
    PUEO_LINK="/usr/local/bin/pueo"

    chmod +x "$PUEO_BIN"
    if ln -sf "$PUEO_BIN" "$PUEO_LINK" 2>/dev/null; then
        ok "pueo command installed at $PUEO_LINK"
    else
        warn "Could not write to /usr/local/bin (try: sudo ln -sf \"$PUEO_BIN\" $PUEO_LINK)"
        info "Or add $SCRIPT_DIR/bin to your PATH manually"
    fi
fi

# ── Done ─────────────────────────────────────────────────────────────────────────
echo
echo -e "${GREEN}${BOLD}✔  Pueo is ready.${NC}"
echo

if [[ "$DEPLOY_MODE" == "native" || "$DEPLOY_MODE" == "both" ]]; then
    echo "  ── Native ─────────────────────────────────────────────────────"
    echo "  Start Pueo           : pueo"
    echo "  Live log             : tail -f ${PUEO_LOG_DIR}/pueo.log"
    echo "  Dashboard            : http://127.0.0.1:8080"
    echo
    echo "  Other modes:"
    echo "    pueo --mode monitor"
    echo "    pueo --mode diagnose"
    echo "    pueo --mode dashboard"
    echo
    echo "  NetAlertX install    : pueo --mode netalertx-setup"
    echo "  NetAlertX diagnose   : pueo --mode netalertx-diagnose"
    echo
fi
if [[ "$DEPLOY_MODE" == "docker" || "$DEPLOY_MODE" == "both" ]]; then
    echo "  ── Docker ─────────────────────────────────────────────────────"
    echo "  Start Pueo           : docker compose up -d"
    echo "  Live log             : docker compose logs -f pueo"
    echo "  Dashboard            : http://127.0.0.1:8080"
    echo
    echo "  One-shot modes:"
    echo "    docker exec pueo-agent python main.py --mode diagnose"
    echo "    docker exec pueo-agent python main.py --mode rag-refresh"
    echo "    docker exec pueo-agent python main.py --mode update-check"
    echo
fi
