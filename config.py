#!/usr/bin/env python3
"""
Loads config.yaml and exposes settings as module-level constants.

Path resolution order:
  1. PUEO_CONFIG environment variable (set by main.py for non-default paths)
  2. config.yaml next to this file (default for scripts run directly)
"""

import os
from pathlib import Path

import yaml

_config_path = Path(
    os.environ.get("PUEO_CONFIG", Path(__file__).parent / "config.yaml")
)

_cfg: dict = {}
if _config_path.exists():
    with open(_config_path) as _f:
        _cfg = yaml.safe_load(_f) or {}

_ha = _cfg.get("home_assistant", {})
_ollama = _cfg.get("ollama", {})
_agent = _cfg.get("agent", {})

HA_HOST: str = _ha.get("host", "homeassistant.local")
HA_USER: str = _ha.get("user", "root")
SSH_KEY_PATH: str = os.path.expanduser(_ha.get("ssh_key_path", "~/.ssh/id_ed25519"))
CONFIG_REMOTE_PATH: str = _ha.get("config_path", "/config/configuration.yaml")
LOG_REMOTE_PATH: str = _ha.get("log_path", "/config/home-assistant.log")

OLLAMA_MODEL: str = _ollama.get("model", "qwen2.5-coder:7b")
OLLAMA_ENDPOINT: str = _ollama.get("endpoint", "http://localhost:11434")

DB_PATH: str = _agent.get("db_path", "ha_agent_state.db")
CONFIDENCE_THRESHOLD: float = float(_agent.get("log_confidence_threshold", 0.7))
SELF_HEALING_ENABLED: bool = bool(_agent.get("self_healing_enabled", True))
SSH_RETRY_ATTEMPTS: int = int(_agent.get("ssh_retry_attempts", 3))
SSH_RETRY_BASE_DELAY: float = float(_agent.get("ssh_retry_base_delay", 2.0))
DEBOUNCE_WINDOW_SECONDS: float = float(_agent.get("debounce_window_seconds", 30))
REPAIR_COOLDOWN_SECONDS: float = float(_agent.get("repair_cooldown_seconds", 300))
MAX_REPAIRS_PER_HOUR: int = int(_agent.get("max_repairs_per_hour", 10))
LOG_LEVEL: str = _agent.get("log_level", "INFO")
LOG_FILE: str = _agent.get("log_file", "pueo.log")
MAX_PROMPT_TOKENS: int = int(_agent.get("max_prompt_tokens", 7000))
NOTIFIER: str = _agent.get("notifier", "file")
NOTIFY_URL: str = _agent.get("notify_url", "")
NOTIFY_WATCH_DIR: str = _agent.get("notify_watch_dir", "hitl/")
