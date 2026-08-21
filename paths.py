"""Centralized path abstraction for Pueo.

Resolves platform-appropriate base directories using platformdirs and
PUEO_* environment variable overrides.  Call get_dirs() once at startup;
pass the resulting PueoDirectories to anything that needs a base path.
"""

import os
from dataclasses import dataclass
from pathlib import Path

import platformdirs

_APP_NAME = "Pueo"
_APP_AUTHOR = "Pueo"


@dataclass(frozen=True)
class PueoDirectories:
    """Resolved base directories for all Pueo runtime state and resources."""

    config_dir: Path  # config.yaml
    data_dir: Path  # backups/, archives/, chromadb/
    state_dir: Path  # ha_agent_state.db, hitl/, tools/, audits/
    cache_dir: Path  # .cache/* subcategories
    log_dir: Path  # pueo.log, pueo-stderr.log
    runtime_dir: Path  # transient files (future: PID file)
    resources_dir: Path  # prompts/, web/static/, web/templates/ — immutable

    def create_all(self) -> None:
        """Create all mutable directories (mkdir -p). Does not touch resources_dir."""
        for field_name in (
            "config_dir",
            "data_dir",
            "state_dir",
            "cache_dir",
            "log_dir",
            "runtime_dir",
        ):
            getattr(self, field_name).mkdir(parents=True, exist_ok=True)

    def __str__(self) -> str:
        lines = [
            f"  config_dir:    {self.config_dir}",
            f"  data_dir:      {self.data_dir}",
            f"  state_dir:     {self.state_dir}",
            f"  cache_dir:     {self.cache_dir}",
            f"  log_dir:       {self.log_dir}",
            f"  runtime_dir:   {self.runtime_dir}",
            f"  resources_dir: {self.resources_dir}",
        ]
        return "\n".join(lines)


def get_dirs() -> PueoDirectories:
    """Resolve platform-appropriate directories.

    Precedence (highest wins):
    1. PUEO_* environment variables
    2. platformdirs platform defaults

    resources_dir is always the directory containing this file (the repo root)
    and is not overridable via env var — it is immutable application source.
    """
    pd = platformdirs.PlatformDirs(_APP_NAME, _APP_AUTHOR, ensure_exists=False)

    def _resolve(env_var: str, default: Path) -> Path:
        val = os.environ.get(env_var)
        return Path(val) if val else default

    return PueoDirectories(
        config_dir=_resolve("PUEO_CONFIG_DIR", Path(pd.user_config_dir)),
        data_dir=_resolve("PUEO_DATA_DIR", Path(pd.user_data_dir)),
        state_dir=_resolve("PUEO_STATE_DIR", Path(pd.user_state_dir)),
        cache_dir=_resolve("PUEO_CACHE_DIR", Path(pd.user_cache_dir)),
        log_dir=_resolve("PUEO_LOG_DIR", Path(pd.user_log_dir)),
        runtime_dir=_resolve("PUEO_RUNTIME_DIR", Path(pd.user_runtime_dir)),
        resources_dir=Path(__file__).parent,
    )
