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

from utils.core.logging import get_logger

log = get_logger("ha_environment")

_VERSION_RE = re.compile(r"version:\s*(\S+)")


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
    fetch_errors: dict = field(default_factory=dict)


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
    from utils.knowledge.ha_docs_scraper import discover_installed_integrations
    from utils.knowledge.hacs_scraper import discover_hacs_integrations

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
    except Exception as e:  # nosec B110
        log.warning("ha_profile_field_failed", field="ha_version", exc=str(e))
        profile.fetch_errors["ha_version"] = str(e)

    # 2. os_version from `ha os info`
    try:
        _, stdout, _ = await ssh_client.run("ha os info")
        m = _VERSION_RE.search(stdout)
        if m:
            profile.os_version = m.group(1)
    except Exception as e:  # nosec B110
        log.warning("ha_profile_field_failed", field="os_version", exc=str(e))
        profile.fetch_errors["os_version"] = str(e)

    # 2b. supervisor_version from `ha supervisor info` (not present in `ha os info` output)
    try:
        _, stdout, _ = await ssh_client.run("ha supervisor info")
        m = _VERSION_RE.search(stdout)
        if m:
            profile.supervisor_version = m.group(1)
    except Exception as e:  # nosec B110
        log.warning("ha_profile_field_failed", field="supervisor_version", exc=str(e))
        profile.fetch_errors["supervisor_version"] = str(e)

    # 3. installed_integrations via REST /api/config
    try:
        profile.installed_integrations = _discover_integrations(ha_url, ha_token)
    except Exception as e:  # nosec B110
        log.warning(
            "ha_profile_field_failed", field="installed_integrations", exc=str(e)
        )
        profile.fetch_errors["installed_integrations"] = str(e)

    # 4. hacs_integrations — extract slugs from (slug, repo) pairs
    try:
        pairs = _discover_hacs(ha_url, ha_token)
        profile.hacs_integrations = [slug for slug, _ in pairs]
    except Exception as e:  # nosec B110
        log.warning("ha_profile_field_failed", field="hacs_integrations", exc=str(e))
        profile.fetch_errors["hacs_integrations"] = str(e)

    # 5. config_yaml_top_keys from remote configuration.yaml
    try:
        import yaml  # type: ignore[import-untyped]

        content = await ssh_client.read_file(config_remote_path)
        doc = yaml.safe_load(content)
        if isinstance(doc, dict):
            profile.config_yaml_top_keys = list(doc.keys())
    except Exception as e:  # nosec B110
        log.warning("ha_profile_field_failed", field="config_yaml_top_keys", exc=str(e))
        profile.fetch_errors["config_yaml_top_keys"] = str(e)

    # 6. config_entries via WebSocket config/config_entries/all
    try:
        profile.config_entries = await ws_client.get_config_entries()
    except Exception as e:  # nosec B110
        log.warning("ha_profile_field_failed", field="config_entries", exc=str(e))
        profile.fetch_errors["config_entries"] = str(e)

    return profile


def format_profile_summary(profile: Optional[HAEnvironmentProfile]) -> str:
    """Return a compact one-block summary of the HA environment profile.

    Large lists (installed_integrations, hacs_integrations, config_entries) are
    shown as counts only. A hint line tells the LLM how to retrieve full lists.
    """
    if profile is None:
        return "HA environment profile not yet available."
    keys_str = (
        ", ".join(profile.config_yaml_top_keys)
        if profile.config_yaml_top_keys
        else "(none)"
    )
    lines = [
        "HA environment:",
        f"  Core: {profile.ha_version or '?'}  |  OS: {profile.os_version or '?'}  |  Supervisor: {profile.supervisor_version or '?'}",
        f"  Config keys: {keys_str}",
        f"  Integrations: {len(profile.installed_integrations)} installed  |  {len(profile.hacs_integrations)} via HACS",
        f"  Config entries: {len(profile.config_entries)}",
        '  (use get_ha_profile field="installed_integrations"|"hacs_integrations"|"config_entries" for full lists)',
    ]
    return "\n".join(lines)


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
    known = {f.name for f in dataclasses.fields(HAEnvironmentProfile)}
    return HAEnvironmentProfile(**{k: v for k, v in data.items() if k in known})
