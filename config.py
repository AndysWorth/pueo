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
HA_API_PORT: int = int(_ha.get("api_port", 8123))
CONFIG_REMOTE_PATH: str = _ha.get("config_path", "/config/configuration.yaml")

OLLAMA_MODEL: str = _ollama.get("model", "qwen2.5-coder:7b")
OLLAMA_MODEL_AUTO: bool = bool(_ollama.get("model_auto", False))
OLLAMA_ENDPOINT: str = _ollama.get("endpoint", "http://localhost:11434")
RAG_EMBED_MODEL: str = _ollama.get("embed_model", "nomic-embed-text")

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
TIMELINE_PAGE_SIZE: int = int(_agent.get("timeline_page_size", 25))
RESOURCE_POLL_INTERVAL_SECONDS: float = float(
    _agent.get("resource_poll_interval_seconds", 300)
)
DISK_USAGE_POLL_INTERVAL_SECONDS: float = float(
    _agent.get("disk_usage_poll_interval_seconds", 300)
)
HA_DISK_WARN_GB: float = float(_agent.get("ha_disk_warn_gb", 5.0))
HA_DISK_CRITICAL_GB: float = float(_agent.get("ha_disk_critical_gb", 3.0))
HA_MEM_WARN_MB: float = float(_agent.get("ha_mem_warn_mb", 256.0))
BACKUP_OFFLOAD_ENABLED: bool = bool(_agent.get("backup_offload_enabled", True))
BACKUP_LOCAL_DIR: str = _agent.get("backup_local_dir", "./backups/")
BACKUP_RETAIN_ON_HA: int = int(_agent.get("backup_retain_on_ha", 2))
BACKUP_RETAIN_LOCAL_DAYS: int = int(_agent.get("backup_retain_local_days", 30))

# Disk recovery — safe auto-steps on disk critical
DISK_RECOVERY_AUTO_ENABLED: bool = bool(_agent.get("disk_recovery_auto_enabled", True))
DISK_RECOVERY_RECORDER_KEEP_DAYS: int = int(
    _agent.get("disk_recovery_recorder_keep_days", 30)
)
DISK_RECOVERY_JOURNAL_MAX_MB: int = int(_agent.get("disk_recovery_journal_max_mb", 200))

# Local archive — copy HA data to Pueo before deletion
PUEO_ARCHIVE_DIR: str = _agent.get("pueo_archive_dir", "./archives/")
ARCHIVE_HA_LOG_ENABLED: bool = bool(_agent.get("archive_ha_log_enabled", True))
ARCHIVE_JOURNAL_ENABLED: bool = bool(_agent.get("archive_journal_enabled", False))
PUEO_ARCHIVE_MAX_GB: float = float(_agent.get("pueo_archive_max_gb", 2.0))
PUEO_LOCAL_MAX_GB: float = float(_agent.get("pueo_local_max_gb", 20.0))

# HA Update Manager
HA_UPDATE_CHECK_INTERVAL_HOURS: float = float(
    _agent.get("update_check_interval_hours", 0.0)
)
HA_UPDATE_NOTIFY_ON_AVAILABLE: bool = bool(
    _agent.get("update_notify_on_available", True)
)
HA_UPDATE_RELEASE_NOTES_CACHE_DIR: str = _agent.get(
    "update_release_notes_cache_dir", ".cache/ha_release_notes/"
)

# HA Notification Intelligence
HA_NOTIFICATION_POLL_INTERVAL_MINUTES: float = float(
    _agent.get("notification_poll_interval_minutes", 5)
)

# HA Repairs polling
HA_REPAIR_POLL_INTERVAL_MINUTES: float = float(
    _agent.get("ha_repair_poll_interval_minutes", 5)
)

# Log triage deduplication — suppress repeat HITL cards for the same recurring error
LOG_TRIAGE_COOLDOWN_HOURS: int = int(_agent.get("log_triage_cooldown_hours", 4))

# RAG knowledge layer
CHROMADB_PATH: str = _agent.get("chromadb_path", "./chromadb/")
RAG_TOP_K: int = int(_agent.get("rag_top_k", 5))
RAG_HA_VERSIONS_TO_FETCH: int = int(_agent.get("rag_ha_versions_to_fetch", 12))
RAG_HACS_CACHE_DIR: str = _agent.get("rag_hacs_cache_dir", ".cache/hacs_changelogs/")
RAG_HA_DOCS_CACHE_DIR: str = _agent.get("rag_ha_docs_cache_dir", ".cache/ha_docs/")

# Tool-calling agent loop
AGENT_MAX_TOOL_CALLS: int = int(_agent.get("agent_max_tool_calls", 20))
AGENT_MAX_WALL_SECONDS: float = float(_agent.get("agent_max_wall_seconds", 120.0))

# HA environment profile
HA_PROFILE_REFRESH_HOURS: int = int(_agent.get("ha_profile_refresh_hours", 24))

# Federated case library — community repair episode sharing
FEDERATED_CASES_REPO: str = _agent.get("federated_cases_repo", "")

# LLM provider selection
_llm_cfg = _cfg.get("llm", {})
_cloud_cfg = _cfg.get("cloud", {})
LLM_PROVIDER: str = _llm_cfg.get("provider", "local")  # "local" | "cloud" | "both"
CLOUD_MODEL: str = _cloud_cfg.get("model", "claude-sonnet-5")
CLOUD_MAX_COST_PER_INCIDENT_USD: float = float(
    _cloud_cfg.get("max_cost_per_incident_usd", 0.50)
)
CLOUD_MAX_DAILY_SPEND_USD: float = float(_cloud_cfg.get("max_daily_spend_usd", 5.00))
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

# Startup guards — fire at import time so the process fails fast at launch
if LLM_PROVIDER in ("cloud", "both") and not ANTHROPIC_API_KEY:
    raise RuntimeError(
        "ANTHROPIC_API_KEY environment variable is not set. "
        f"LLM_PROVIDER={LLM_PROVIDER!r} requires it. "
        "Export it in ~/.zshenv: export ANTHROPIC_API_KEY=<your-key>"
    )

if _config_path.exists():
    _raw_yaml = _config_path.read_text()
    if "ANTHROPIC_API_KEY:" in _raw_yaml or "anthropic_api_key:" in _raw_yaml:
        raise RuntimeError(
            f"ANTHROPIC_API_KEY must not appear as a key in {_config_path}. "
            "Store it in the environment only (e.g. ~/.zshenv). "
            "Remove it from config.yaml and reload your shell."
        )

# Conversational agent
CHAT_MEMORY_TOP_K: int = int(_agent.get("chat_memory_top_k", 10))
CHAT_ALLOW_TOOL_REGISTRATION: bool = bool(
    _agent.get("chat_allow_tool_registration", False)
)
HA_NOTIFICATION_ENRICH_AUTH_FAILURES: bool = bool(
    _agent.get("notification_enrich_auth_failures", True)
)

# NetAlertX integration
_nax = _cfg.get("netalertx", {})

NETALERTX_SETUP_DESIRED: bool = bool(_nax.get("setup_desired", False))
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
    "addon_repository_url", "https://github.com/alexbelgium/hassio-addons"
)
NETALERTX_ADDON_SLUG: str = _nax.get("addon_slug", "")
NETALERTX_SCAN_INTERFACE: str = _nax.get("scan_interface", "")
NETALERTX_AUTO_GENERATED_NAME_PATTERNS: list[str] = _nax.get(
    "auto_generated_name_patterns",
    ["^unknown-", "^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"],
)
NETALERTX_MAX_SCAN_AGE_MINUTES: int = int(_nax.get("max_scan_age_minutes", 20))
NETALERTX_MQTT_SUBSCRIBE: bool = bool(_nax.get("mqtt_subscribe", True))
NETALERTX_MQTT_USER: str = _nax.get("mqtt_user", "")
NETALERTX_MQTT_PASSWORD: str = _nax.get("mqtt_password", "")
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
