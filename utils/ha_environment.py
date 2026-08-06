"""HA environment profile — captures installed integrations, versions, config keys (item 90)."""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from interfaces import HAWebSocketClientProtocol, SSHClientProtocol

_VERSION_RE = re.compile(r"version:\s*(\S+)")
_SUPERVISOR_RE = re.compile(r"supervisor:\s*(\S+)")


@dataclass
class HAEnvironmentProfile:
    ha_version: str = ""
    os_version: str = ""
    supervisor_version: str = ""
    installed_integrations: list[str] = field(default_factory=list)
    hacs_integrations: list[str] = field(default_factory=list)
    config_yaml_top_keys: list[str] = field(default_factory=list)
    config_entries: list[dict] = field(default_factory=list)
    last_updated: float = 0.0


async def build_environment_profile(
    ssh_client: "SSHClientProtocol",
    ws_client: "HAWebSocketClientProtocol",
    ha_token: str,
    ha_url: str,
    config_remote_path: str,
    *,
    _discover_integrations: Optional[Callable] = None,
    _discover_hacs: Optional[Callable] = None,
) -> HAEnvironmentProfile:
    """Assemble an HAEnvironmentProfile from all available sources.

    Every sub-call is wrapped in try/except so failures produce empty values
    rather than aborting the whole profile build.
    """
    from utils.ha_docs_scraper import discover_installed_integrations
    from utils.hacs_scraper import discover_hacs_integrations

    if _discover_integrations is None:
        _discover_integrations = discover_installed_integrations  # pragma: no cover
    if _discover_hacs is None:
        _discover_hacs = discover_hacs_integrations  # pragma: no cover

    profile = HAEnvironmentProfile(last_updated=time.time())

    # 1. ha_version from `ha core info`
    try:
        _, stdout, _ = await ssh_client.run("ha core info")
        m = _VERSION_RE.search(stdout)
        if m:
            profile.ha_version = m.group(1)
    except Exception:
        pass

    # 2. os_version + supervisor_version from `ha os info`
    try:
        _, stdout, _ = await ssh_client.run("ha os info")
        m = _VERSION_RE.search(stdout)
        if m:
            profile.os_version = m.group(1)
        m = _SUPERVISOR_RE.search(stdout)
        if m:
            profile.supervisor_version = m.group(1)
    except Exception:
        pass

    # 3. installed_integrations via REST /api/config
    try:
        profile.installed_integrations = _discover_integrations(ha_url, ha_token)
    except Exception:
        pass

    # 4. hacs_integrations — extract slugs from (slug, repo) pairs
    try:
        pairs = _discover_hacs(ha_url, ha_token)
        profile.hacs_integrations = [slug for slug, _ in pairs]
    except Exception:
        pass

    # 5. config_yaml_top_keys from remote configuration.yaml
    try:
        import yaml  # type: ignore[import-untyped]

        content = await ssh_client.read_file(config_remote_path)
        doc = yaml.safe_load(content)
        if isinstance(doc, dict):
            profile.config_yaml_top_keys = list(doc.keys())
    except Exception:
        pass

    # 6. config_entries via WebSocket config/config_entries/all
    try:
        profile.config_entries = await ws_client.get_config_entries()
    except Exception:
        pass

    return profile


def save_environment_profile(profile: HAEnvironmentProfile, db_path: str) -> None:
    """Persist the profile as a single JSON row in SQLite (INSERT OR REPLACE)."""
    profile_json = json.dumps(dataclasses.asdict(profile))
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ha_environment_profile "
            "(id, profile_json, last_updated) VALUES (1, ?, ?)",
            (profile_json, profile.last_updated),
        )


def load_environment_profile(db_path: str) -> Optional[HAEnvironmentProfile]:
    """Load the persisted profile from SQLite. Returns None if not yet saved."""
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT profile_json FROM ha_environment_profile WHERE id = 1"
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    data = json.loads(row[0])
    return HAEnvironmentProfile(**data)
