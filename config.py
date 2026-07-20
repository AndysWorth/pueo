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
HA_KNOWN_VERSION: str = _ha.get("known_version", "")
HA_USER: str = _ha.get("user", "root")
SSH_KEY_PATH: str = os.path.expanduser(_ha.get("ssh_key_path", "~/.ssh/id_ed25519"))
HA_API_TOKEN: str = _ha.get("api_token", "")
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
HITL_ALWAYS: bool = bool(_agent.get("hitl_always", False))
DASHBOARD_PORT: int = int(_agent.get("dashboard_port", 8080))

# NetAlertX integration
_nax = _cfg.get("netalertx", {})

NETALERTX_ENABLED: bool = bool(_nax.get("enabled", False))
NETALERTX_DEPLOYMENT: str = _nax.get("deployment", "auto")
NETALERTX_HOST: str = _nax.get("host", _ha.get("host", "homeassistant.local"))
NETALERTX_API_PORT: int = int(_nax.get("api_port", 20212))
NETALERTX_API_TOKEN: str = _nax.get("api_token", "")
NETALERTX_SSH_HOST: str = _nax.get("ssh_host", _ha.get("host", "homeassistant.local"))
NETALERTX_SSH_USER: str = _nax.get("ssh_user", _ha.get("user", "root"))
NETALERTX_SSH_KEY_PATH: str = os.path.expanduser(
    _nax.get("ssh_key_path", _ha.get("ssh_key_path", "~/.ssh/id_ed25519"))
)
NETALERTX_ADDON_REPOSITORY_URL: str = _nax.get(
    "addon_repository_url", "https://github.com/jokob-sk/NetAlertX"
)
NETALERTX_ADDON_SLUG: str = _nax.get("addon_slug", "")
NETALERTX_SCAN_INTERFACE: str = _nax.get("scan_interface", "")
NETALERTX_AUTO_GENERATED_NAME_PATTERNS: list[str] = _nax.get(
    "auto_generated_name_patterns",
    ["^unknown-", "^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"],
)
NETALERTX_MAX_SCAN_AGE_MINUTES: int = int(_nax.get("max_scan_age_minutes", 20))
NETALERTX_MQTT_SUBSCRIBE: bool = bool(_nax.get("mqtt_subscribe", True))
NETALERTX_LOG_CONTAINER_NAME: str = _nax.get("log_container_name", "netalertx")
NETALERTX_MAX_DB_HISTORY_ROWS: int = int(_nax.get("max_db_history_rows", 100000))

# Autonomy control
_netalertx_mode = _nax.get("mode", "")
_NETALERTX_MODE_MAP: dict[str, int] = {"diagnose": 1, "auto_fix": 3, "autonomous": 4}
_autonomy_raw = _agent.get("autonomy_level", None)
if _netalertx_mode in _NETALERTX_MODE_MAP and _autonomy_raw is None:
    import logging as _logging

    _logging.warning(
        "config: netalertx.mode is deprecated; migrate to agent.autonomy_level"
    )
    _autonomy_raw = _NETALERTX_MODE_MAP[_netalertx_mode]
AUTONOMY_LEVEL: int = int(_autonomy_raw if _autonomy_raw is not None else 2)
