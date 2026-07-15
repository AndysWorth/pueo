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

echo -e "\n🦉  ${BOLD}Pueo Setup${NC}"
echo "════════════════════════════════════════"

# ── 1. Python ───────────────────────────────────────────────────────────────────
hdr "1. Python"

REQUIRED_PYTHON="3.14"

if ! command -v pyenv &>/dev/null; then
    fail "pyenv not found. Install it from https://github.com/pyenv/pyenv then re-run."
    exit 1
fi
ok "pyenv $(pyenv --version | awk '{print $2}')"

# Install Python 3.14 if no 3.14.x is present
INSTALLED_VERSION=$(pyenv versions --bare | grep "^${REQUIRED_PYTHON}\." | sort -V | tail -1 || true)
if [[ -z "$INSTALLED_VERSION" ]]; then
    info "Python ${REQUIRED_PYTHON} not found — installing via pyenv (this may take a few minutes)..."
    pyenv install "${REQUIRED_PYTHON}"
    INSTALLED_VERSION=$(pyenv versions --bare | grep "^${REQUIRED_PYTHON}\." | sort -V | tail -1)
fi
ok "Python ${INSTALLED_VERSION}"

PYTHON_BIN="$(pyenv prefix "$INSTALLED_VERSION")/bin/python"

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
        echo "  In HA: Settings → Add-ons → SSH & Web Terminal"
        echo "         → Configuration → Authorized Keys"
        echo
        read -rp "  Press Enter once the key is added to HA to continue..."
    else
        warn "Skipping key generation — SSH features will not work without a key."
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
    ask "SSH private key path"              "$DEFAULT_SSH_KEY"              HA_SSH_KEY
    ask "config.yaml path on HA host"      "/config/configuration.yaml"    HA_CONFIG_PATH
    ask "home-assistant.log path on HA"    "/config/home-assistant.log"    HA_LOG_PATH
    ask "Ollama model"                      "$DEFAULT_MODEL"                OLLAMA_MODEL
    ask "Local SQLite database path"        "ha_agent_state.db"             DB_PATH
    ask "Log confidence threshold (0–1)"    "0.7"                           LOG_THRESHOLD
    ask "Self-healing enabled"              "true"                          SELF_HEALING

    cat > config.yaml <<EOF
home_assistant:
  host: "${HA_HOST}"
  user: "${HA_USER}"
  ssh_key_path: "${HA_SSH_KEY}"
  config_path: "${HA_CONFIG_PATH}"
  log_path: "${HA_LOG_PATH}"

ollama:
  model: "${OLLAMA_MODEL}"
  endpoint: "http://localhost:11434"

agent:
  db_path: "${DB_PATH}"
  log_confidence_threshold: ${LOG_THRESHOLD}
  self_healing_enabled: ${SELF_HEALING}
EOF
    ok "config.yaml written"

    # Verify SSH connectivity
    echo
    info "Testing SSH connection to ${HA_HOST}..."
    if ssh -i "${HA_SSH_KEY}" -o ConnectTimeout=5 -o BatchMode=yes \
           -o StrictHostKeyChecking=no "${HA_USER}@${HA_HOST}" "echo ok" &>/dev/null; then
        ok "SSH connection to ${HA_HOST} successful"
    else
        warn "SSH connection failed — check that ${HA_HOST} is reachable and the key is authorized."
        warn "Test manually: ssh -i ${HA_SSH_KEY} ${HA_USER}@${HA_HOST}"
    fi
fi

# ── Done ─────────────────────────────────────────────────────────────────────────
echo
echo -e "${GREEN}${BOLD}✔  Pueo is ready.${NC}"
echo
echo "  Activate environment : source .venv/bin/activate"
echo "  Live log monitor     : python main.py --mode monitor"
echo "  One-shot diagnostics : python main.py --mode diagnose"
echo "  Full repair pipeline : python main.py --mode repair"
echo
