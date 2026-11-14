#!/usr/bin/env python3
"""HA Update Manager — detects available updates and surfaces them as approval cards."""

import asyncio
import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

import asyncssh
import httpx
from pydantic import BaseModel, Field

import config as _config
from config import (
    AUTONOMY_LEVEL,
    DB_PATH,
    HA_API_PORT,
    HA_API_TOKEN,
    HA_DISK_CRITICAL_GB,
    HA_DISK_WARN_GB,
    HA_HOST,
    HA_MEM_WARN_MB,
    HA_UPDATE_RELEASE_NOTES_CACHE_DIR,
    NOTIFIER,
    NOTIFY_URL,
    NOTIFY_WATCH_DIR,
    SSH_KEY_PATH,
    HA_USER,
)
from interfaces import HARestClientProtocol, LLMClientProtocol, SSHClientProtocol
from utils.agent.autonomy import RiskLevel
from utils.ha.ha_rest_client import HARestClient, UpdateStatus, get_update_status
from utils.core.logging import get_logger

if TYPE_CHECKING:
    from utils.agent.agent_loop import AgentLoopResult
    from utils.agent.autonomy import AutonomyGate, FakeAutonomyGate
    from utils.ha.ha_environment import HAEnvironmentProfile
    from utils.hitl.notify import NotifierProtocol
    from interfaces import KnowledgeStoreClientProtocol

log = get_logger("ha_update_manager")

# ── Auto-reboot flag ──────────────────────────────────────────────────────────
# Set after an OS or Supervisor update succeeds so the next `reboot_required`
# repair issue auto-executes the reboot without a second approval card (the
# pre-update backup already covers recovery). Stored as a monotonic set-time and
# expires after _POST_UPDATE_FLAG_TTL_SECONDS so a flag left over from an old
# update can never trigger an unrelated reboot/restart days later (#846).
_POST_UPDATE_FLAG_TTL_SECONDS = 30 * 60
_reboot_pending_after_update: Optional[float] = None

# Entities whose update is currently being executed by Pueo.  Used to suppress
# false "updated outside Pueo" timeline events while execute_update is in flight.
_entities_executing_update: set = set()

# Supervisor add-on slugs that provide Pueo's SSH transport.  When one of these
# is being updated, SSH-based loops (ha_log_monitor, netalertx) will lose their
# connection — the supervisor treats this as expected, not a crash.
_SSH_TRANSPORT_SLUGS: frozenset = frozenset({"core_ssh", "a0d7b954_ssh"})

# Set while a transport add-on update is executing so the supervisor can suppress
# ConnectionLost timeline errors during the update window.
_ssh_transport_update_in_progress: bool = False

# Matches the Supervisor icon URL to extract the add-on slug without an SSH query.
_HASSIO_ADDON_ICON_RE = re.compile(r"^/api/hassio/addons/([^/]+)/icon")


def is_entity_executing_update(entity_id: str) -> bool:
    """Return True while Pueo is actively running execute_update for this entity."""
    return entity_id in _entities_executing_update


def is_ssh_transport_update_in_progress() -> bool:
    """Return True while a known SSH-transport add-on update is executing."""
    return _ssh_transport_update_in_progress


async def _enqueue_distillation_for_subject(
    subject_key: str, verification: str
) -> None:
    """Read session_outcomes rows for subject_key and enqueue distillation for each.

    Only distills rows that still have a payload (not swept) and haven't been
    distilled yet — rows with distill_payload_json IS NULL or distilled_at set
    are silently skipped.
    """
    import sqlite3 as _sql
    import config as _cfg
    from utils.agent.supervisor import get_supervisor_instance
    from utils.knowledge.runbook_distiller import (
        enqueue_distillation_after_verification,
    )

    _sv = get_supervisor_instance()
    _ks = getattr(getattr(_sv, "_tool_executor", None), "_knowledge_store", None)
    if _ks is None:
        return
    try:
        with _sql.connect(_cfg.DB_PATH) as _conn:
            _rows = _conn.execute(
                "SELECT episode_id FROM session_outcomes"
                " WHERE subject_key = ? AND verification = ?"
                "   AND distill_payload_json IS NOT NULL AND distilled_at IS NULL",
                (subject_key, verification),
            ).fetchall()
        for (ep_id,) in _rows:
            if ep_id:
                await enqueue_distillation_after_verification(
                    episode_id=ep_id,
                    verification=verification,
                    correction=None,
                    db_path=_cfg.DB_PATH,
                    knowledge_store=_ks,
                    model=_cfg.OLLAMA_MODEL,
                )
    except Exception:  # nosec B110
        pass


def _flag_is_fresh(set_at: Optional[float]) -> bool:
    return (
        set_at is not None
        and (time.monotonic() - set_at) < _POST_UPDATE_FLAG_TTL_SECONDS
    )


def set_reboot_pending_after_update(val: bool) -> None:
    global _reboot_pending_after_update
    _reboot_pending_after_update = time.monotonic() if val else None


def is_reboot_pending_after_update() -> bool:
    return _flag_is_fresh(_reboot_pending_after_update)


async def _post_update_repair_scan(settle_seconds: int = 5) -> None:
    """Trigger repair_poll shortly after an OS/Supervisor update so the
    reboot_required repair issue surfaces quickly."""
    await asyncio.sleep(settle_seconds)
    try:
        from utils.agent.supervisor import get_supervisor_instance

        sv = get_supervisor_instance()
        if sv is not None:
            sv.wake("repair_poll")
            log.info("post_update_repair_scan_triggered")
    except Exception as exc:  # nosec B110 — best-effort; scheduled poll is fallback
        log.warning("post_update_repair_scan_failed", error=str(exc))


# ── Auto-restart flag ─────────────────────────────────────────────────────────
# Set after an integration/add-on update succeeds so the next `restart_required`
# repair issue auto-executes ha core restart without a second approval card (the
# pre-update backup already covers recovery). Same TTL as the reboot flag.
_restart_pending_after_update: Optional[float] = None


def set_restart_pending_after_update(val: bool) -> None:
    global _restart_pending_after_update
    _restart_pending_after_update = time.monotonic() if val else None


def is_restart_pending_after_update() -> bool:
    return _flag_is_fresh(_restart_pending_after_update)


# ── DB-persisted post-update pending flags ────────────────────────────────────
# Supplement the in-memory flags above with rows in the ``user_prefs`` table so
# the pending state survives a Pueo restart.  Key format:
# ``post_update_pending:<slug_or_id>``; value: JSON {"kind": "restart"|"reboot",
# "set_at": <unix timestamp>}.  The same 30-minute TTL applies, enforced at
# read time.  These are used by ``poll_for_repairs`` to short-circuit the LLM
# investigation for HACS-triggered ``restart_required`` issues when a matching
# pending flag exists.


def set_post_update_pending(
    slug_or_id: str, kind: str, db_path: Optional[str] = None
) -> None:
    """Write a post-update pending flag to user_prefs."""
    import json
    import sqlite3 as _sql

    _db = db_path or DB_PATH
    value = json.dumps({"kind": kind, "set_at": time.time()})
    try:
        with _sql.connect(_db) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_prefs (key, value) VALUES (?, ?)",
                (f"post_update_pending:{slug_or_id}", value),
            )
    except Exception:  # nosec B110
        pass


def get_post_update_pending(
    slug_or_id: str, db_path: Optional[str] = None
) -> Optional[str]:
    """Return the kind ("restart"|"reboot") if a fresh flag exists for this id, else None."""
    import json
    import sqlite3 as _sql

    _db = db_path or DB_PATH
    try:
        with _sql.connect(_db) as conn:
            row = conn.execute(
                "SELECT value FROM user_prefs WHERE key = ?",
                (f"post_update_pending:{slug_or_id}",),
            ).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        if time.time() - data.get("set_at", 0) < _POST_UPDATE_FLAG_TTL_SECONDS:
            return data.get("kind")
        return None
    except Exception:  # nosec B110
        return None


def clear_post_update_pending(slug_or_id: str, db_path: Optional[str] = None) -> None:
    """Remove the post-update pending flag for this id."""
    import sqlite3 as _sql

    _db = db_path or DB_PATH
    try:
        with _sql.connect(_db) as conn:
            conn.execute(
                "DELETE FROM user_prefs WHERE key = ?",
                (f"post_update_pending:{slug_or_id}",),
            )
    except Exception:  # nosec B110
        pass


def find_any_post_update_pending(
    kind: str, db_path: Optional[str] = None
) -> Optional[str]:
    """Return the first slug_or_id with a fresh pending flag of the given kind, or None."""
    import json
    import sqlite3 as _sql

    _db = db_path or DB_PATH
    now = time.time()
    try:
        with _sql.connect(_db) as conn:
            rows = conn.execute(
                "SELECT key, value FROM user_prefs"
                " WHERE key LIKE 'post_update_pending:%'",
            ).fetchall()
        for key, value in rows:
            try:
                data = json.loads(value)
            except Exception:  # nosec B112
                continue
            if data.get("kind") == kind and (
                now - data.get("set_at", 0) < _POST_UPDATE_FLAG_TTL_SECONDS
            ):
                return key.split(":", 1)[1]
        return None
    except Exception:  # nosec B110
        return None


# ── Loop pause/resume during reboot ──────────────────────────────────────────
_REBOOT_PAUSE_LOOPS = [
    "ha_log_monitor",
    "ha_log_monitor_supervisor",
    "resource_poll",
    "disk_usage_poll",
    "notification_poll",
    "repair_poll",
    "lovelace_poll",
]


def _pause_loops_for_reboot() -> None:
    """Pause monitoring loops that would produce noisy errors during a reboot."""
    from utils.agent.supervisor import get_supervisor_instance

    sv = get_supervisor_instance()
    if sv is None:
        return
    for name in _REBOOT_PAUSE_LOOPS:
        try:
            sv.pause(name)
        except Exception:  # nosec B110 — loop may not exist in all deployments
            pass
    log.info("reboot_loops_paused", loops=_REBOOT_PAUSE_LOOPS)


def _resume_loops_after_reboot() -> None:
    """Resume monitoring loops after HA comes back online."""
    from utils.agent.supervisor import get_supervisor_instance

    sv = get_supervisor_instance()
    if sv is None:
        return
    for name in _REBOOT_PAUSE_LOOPS:
        try:
            sv.resume(name)
        except Exception:  # nosec B110 — loop may not exist in all deployments
            pass
    log.info("reboot_loops_resumed", loops=_REBOOT_PAUSE_LOOPS)


# ── Activity widget step emission ─────────────────────────────────────────────
def _emit_step(status: str, activity: str = "update_execution") -> None:
    """Publish an agent_step SSE event so the activity widget shows progress."""
    try:
        from utils.agent.supervisor import publish_event

        publish_event(
            {
                "event_type": "agent_step",
                "tool": "update",
                "status": status,
                "activity": activity,
            }
        )
    except Exception:  # nosec B110 — best-effort; never block update execution
        pass


# SSH commands Pueo uses — kept for backwards compatibility; also included in PUEO_HA_DEPENDENCIES.
PUEO_SSH_COMMANDS = [
    "ha core check",
    "ha core restart",
    "ha core update",
    "ha core info --raw-json",
    "ha core logs",
    "ha apps list",
    "ha apps info <slug>",
    "ha backup new",
    "ha os update",
    "ha os info",
    "ha host info",
]

# Full dependency manifest — every HA API surface Pueo touches, with the feature that uses it.
# The LLM calls get_pueo_command_catalog() and cross-references this against release notes to
# detect renames, removals, or incompatible changes that would break Pueo.
PUEO_HA_DEPENDENCIES: dict[str, list[dict[str, str]]] = {
    "ssh_commands": [
        {"cmd": "ha core check", "feature": "sandbox config validation before repair"},
        {"cmd": "ha core restart", "feature": "post-repair Core restart"},
        {"cmd": "ha core update", "feature": "Core update execution"},
        {"cmd": "ha core info --raw-json", "feature": "version detection"},
        {"cmd": "ha core logs", "feature": "log tailing and triage"},
        {"cmd": "ha apps list", "feature": "add-on slug resolution"},
        {"cmd": "ha apps info <slug>", "feature": "add-on metadata and update state"},
        {"cmd": "ha backup new", "feature": "mandatory pre-repair backup"},
        {"cmd": "ha os update", "feature": "OS update execution"},
        {"cmd": "ha os info", "feature": "OS version detection"},
        {"cmd": "ha host info", "feature": "host hardware profile"},
        {
            "cmd": "ha resolution suggestion apply <uuid>",
            "feature": "host-health resolution",
        },
        {
            "cmd": "ha resolution issue dismiss <uuid>",
            "feature": "host-health issue dismiss",
        },
        {"cmd": "ha apps restart <slug>", "feature": "add-on restart"},
        {"cmd": "ha resolution healthcheck", "feature": "host-health check"},
    ],
    "websocket_commands": [
        {
            "cmd": "marketplace/repositories/list",
            "feature": "Marketplace integration discovery and companion provisioning (HA 2026.11+)",
            "note": "Falls back to hacs/repositories/list on older HA versions",
        },
        {
            "cmd": "marketplace/repositories/add",
            "feature": "Marketplace integration add (companion provisioning, HA 2026.11+)",
            "note": "Falls back to hacs/repositories/add on older HA versions",
        },
        {
            "cmd": "marketplace/repository/install",
            "feature": "Marketplace integration install (companion provisioning, HA 2026.11+)",
            "note": "Falls back to hacs/repository/download on older HA versions",
        },
        {
            "cmd": "hacs/repositories/list",
            "feature": "HACS integration discovery (HA <2026.11 fallback)",
        },
        {
            "cmd": "hacs/repositories/add",
            "feature": "HACS integration add (HA <2026.11 fallback)",
        },
        {
            "cmd": "hacs/repository/download",
            "feature": "HACS integration download (HA <2026.11 fallback)",
        },
        {
            "cmd": "repairs/list_issues",
            "feature": "repair issue triage and host-health investigation",
        },
        {
            "cmd": "repairs/ignore_issue",
            "feature": "repair issue ignore/dismiss",
        },
        {
            "cmd": "update/release_notes",
            "feature": "add-on release notes retrieval",
        },
        {
            "cmd": "persistent_notification/subscribe",
            "feature": "notification event streaming",
        },
        {
            "cmd": "persistent_notification/get",
            "feature": "persistent notification retrieval",
        },
        {
            "cmd": "config/config_entries/flow/init",
            "feature": "config flow initiation (companion provisioning, integration setup)",
        },
        {
            "cmd": "config/config_entries/flow/{flow_id}",
            "feature": "config flow step execution",
        },
        {
            "cmd": "config/config_entries/options/flow/init",
            "feature": "options flow initiation (companion provisioning)",
        },
        {
            "cmd": "lovelace/resources",
            "feature": "dashboard resource listing",
        },
        {
            "cmd": "validate_config",
            "feature": "automation YAML validation before authoring",
        },
    ],
    "rest_paths": [
        {
            "path": "POST /api/services/<domain>/<service>",
            "feature": "service call execution (call_service tool)",
        },
        {
            "path": "GET /api/states/<entity_id>",
            "feature": "entity state reads (get_entity_history, device availability)",
        },
        {
            "path": "GET /api/logbook",
            "feature": "logbook retrieval",
        },
        {
            "path": "POST /api/template",
            "feature": "template rendering (render_ha_template)",
        },
        {
            "path": "GET /api/repairs/issues/fix/<issue_id>",
            "feature": "repair flow initiation (drive_repair_flow)",
        },
        {
            "path": "POST /api/config/automation/config/<automation_id>",
            "feature": "automation create/update",
        },
        {
            "path": "DELETE /api/config/automation/config/<automation_id>",
            "feature": "automation delete (rollback)",
        },
        {
            "path": "POST http://supervisor/addons/<slug>/options",
            "feature": "add-on options update (companion provisioning)",
        },
    ],
    "integrations": [
        {
            "domain": "marketplace",
            "feature": "Marketplace integration discovery and companion provisioning (HA 2026.11+)",
            "note": "Built-in replacement for HACS; hacs/* WS commands are gone on 2026.11+",
        },
        {
            "domain": "hacs",
            "feature": "HACS integration discovery and companion provisioning (HA <2026.11)",
            "note": "Replaced by built-in Marketplace in HA 2026.11+; cannot be installed on 2026.11+",
        },
        {
            "domain": "spook",
            "feature": "Spook issue digest and companion provisioning",
            "note": "optional HACS companion",
        },
        {
            "domain": "ai_agent_ha",
            "feature": "AI Agent HA companion integration detection",
            "note": "optional HACS companion",
        },
    ],
    "repair_id_patterns": [
        {
            "pattern": "marketplace/restart_required_*",
            "feature": "Marketplace restart-required repair detection in log monitor (HA 2026.11+)",
        },
        {
            "pattern": "hacs/restart_required_*",
            "feature": "HACS restart-required repair detection in log monitor (HA <2026.11)",
        },
    ],
    "ssh_file_paths": [
        {
            "path": "/config/configuration.yaml",
            "feature": "config read, sandbox validation, atomic repair swap",
        },
        {
            "path": "/config/.agent_sandbox/",
            "feature": "sandbox config testing before production apply",
        },
        {
            "path": "/backup/",
            "feature": "backup file download and SHA-256 verification",
        },
    ],
}

_GITHUB_API_URL = (
    "https://api.github.com/repos/home-assistant/core/releases/tags/{version}"
)


def _enumerate_intermediate_core_versions(
    installed_version: str, target_version: str
) -> list[str]:
    """Enumerate HA Core milestone versions between installed (exclusive) and target (inclusive).

    Returns intermediate minor x.0 releases plus the target. If installed and target
    share the same minor (or differ only by patch), returns only the target.

    Examples::
        installed=2026.1.2, target=2026.3.1 → ["2026.2.0", "2026.3.1"]
        installed=2026.2.0, target=2026.3.1 → ["2026.3.1"]
        installed=2026.3.0, target=2026.3.1 → ["2026.3.1"]
    """

    def _parse_core(v: str) -> Optional[tuple[int, int]]:
        parts = v.split(".")
        if len(parts) >= 2 and len(parts[0]) == 4 and parts[0].startswith("20"):
            try:
                return (int(parts[0]), int(parts[1]))
            except ValueError:
                pass
        return None

    iv = _parse_core(installed_version)
    tv = _parse_core(target_version)
    if iv is None or tv is None:
        return [target_version]

    iy, im = iv
    ty, tm = tv
    versions: list[str] = []
    curr_y, curr_m = iy, im + 1
    while (curr_y, curr_m) < (ty, tm):
        versions.append(f"{curr_y}.{curr_m}.0")
        curr_m += 1
        if curr_m > 12:
            curr_m = 1
            curr_y += 1
    versions.append(target_version)
    return versions


# Match github.com HTML release URLs and rewrite them to the API form so the
# JSON body field (release notes) is directly accessible.
_GITHUB_RELEASE_HTML_RE = re.compile(
    r"https?://github\.com/([^/]+/[^/]+)/releases/(?:tag/)?([^/?\s]+)"
)

# Strip <ha-alert>…</ha-alert> blocks (including multi-line) to reveal stub WS notes.
_HA_ALERT_RE = re.compile(r"<ha-alert\b[^>]*>.*?</ha-alert>", re.DOTALL | re.IGNORECASE)

# A WS release-notes body is a stub when, after removing <ha-alert> blocks, the
# remaining text is under this many characters.
_WS_STUB_THRESHOLD = 200


def _is_stub_notes(notes: str) -> bool:
    """Return True when *notes* is a WS stub (only ha-alert boilerplate, no real content)."""
    stripped = _HA_ALERT_RE.sub("", notes).strip()
    return len(stripped) < _WS_STUB_THRESHOLD


def _is_alert_only(notes: str) -> bool:
    """Return True when *notes* has no content outside <ha-alert> blocks (#1029).

    Stricter than ``_is_stub_notes``: a short but real add-on changelog is not
    alert-only, while HA's OS-update "reboot required" notice is.
    """
    return not _HA_ALERT_RE.sub("", notes).strip()


def _rewrite_github_release_url(url: str) -> str:
    """Rewrite a github.com HTML release URL to its GitHub API counterpart."""
    m = _GITHUB_RELEASE_HTML_RE.match(url)
    if m:
        return f"https://api.github.com/repos/{m.group(1)}/releases/tags/{m.group(2)}"
    return url


async def _fetch_github_release_notes(version: str) -> str:  # pragma: no cover
    """Fetch release notes body from the GitHub API for a given HA Core version."""
    url = _GITHUB_API_URL.format(version=version)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            url,
            headers={"Accept": "application/vnd.github.v3+json"},
        )
        resp.raise_for_status()
        data = resp.json()
    return data.get("body") or "No release notes available."


async def _fetch_release_notes_from_url(url: str) -> str:  # pragma: no cover
    """Fetch release notes from an arbitrary URL (e.g. HACS or App release pages)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            url,
            headers={
                "Accept": "application/vnd.github.v3+json,application/json",
            },
        )
        if resp.status_code == 200:
            try:
                data = resp.json()
                # GitHub API release JSON — return the body field directly.
                if "tag_name" in data and "body" in data:
                    return data.get("body") or "No release notes available."
                return data.get("body") or data.get("description") or str(data)
            except Exception:  # nosec B110
                pass
        # Fall back to raw text for non-JSON responses (e.g. changelog pages).
        resp.raise_for_status()
        return resp.text


async def fetch_release_notes_cached(
    version: str,
    cache_dir: str,
    *,
    entity_id: Optional[str] = None,
    release_url: Optional[str] = None,
    ws_fetcher=None,  # injectable: async callable(entity_id) -> str | None
    _fetcher=None,  # injectable for tests (HA Core GitHub fetch)
    _blog_post_fetcher=None,  # injectable for tests: callable(url) -> bytes|str
) -> str:
    """Return cached release notes; fetch from GitHub API if not yet cached.

    For HA Core versions (YYYY.M.P), fetches from the home-assistant/core
    GitHub API.  For HACS / App / add-on updates (non-YYYY versions), the
    fetch order is:

    1. WS ``update/release_notes`` (when ``ws_fetcher`` and ``entity_id`` are
       provided) — most reliable for supervisor add-ons.
    2. ``release_url`` (HTTP fetch) — used when WS notes are absent.
    3. A concrete CHANGELOG URL derived from the entity's component name in the
       HA add-ons repo, suitable for the agent to fetch directly.

    Cache key for non-core: ``{entity_id}_{version}.txt`` to avoid collisions
    between different add-ons that share a version string (e.g. ``0.7.1``).
    Core cache key is unchanged: ``{version}.txt``.

    GA monthly (``.0``) Core releases set the GitHub body to a blog-link stub
    (<500 chars).  When detected, the stub's blog URL is extracted and the real
    article is fetched via ``fetch_blog_post`` and cached.  If the blog is
    unreachable, the beta PR-list fallback is returned with an ``INCOMPLETE:``
    prefix and is **not** cached under ``<version>.txt`` (so the next call
    re-attempts the blog fetch).
    """
    _is_ha_core = version[:4].isdigit() and version.startswith("20")

    # Non-core cache key includes the entity_id to avoid cross-add-on collisions.
    if _is_ha_core or entity_id is None:
        cache_path = Path(cache_dir) / f"{version}.txt"
    else:
        _safe_eid = entity_id.replace(".", "_").replace("/", "_")
        cache_path = Path(cache_dir) / f"{_safe_eid}_{version}.txt"

    if cache_path.exists():
        cached = cache_path.read_text()
        # A cached alert-only WS stub must not block the GitHub fallback: refetch
        # when a GitHub release URL could supply the real notes (#1029).
        if not (
            not _is_ha_core
            and _is_alert_only(cached)
            and release_url
            and _GITHUB_RELEASE_HTML_RE.match(release_url)
        ):
            return cached

    if not _is_ha_core:
        # 1. Try WS update/release_notes first — most reliable for supervisor add-ons.
        if ws_fetcher is not None and entity_id is not None:
            try:
                ws_notes = await ws_fetcher(entity_id)
                if ws_notes:
                    # If the WS body is a stub (only <ha-alert> boilerplate) and a
                    # GitHub release URL is available, try to fetch the real notes.
                    if _is_stub_notes(ws_notes) and release_url:
                        _allow_wan = getattr(_config, "ALLOW_DIAGNOSTIC_WAN", True)
                        if _allow_wan and _GITHUB_RELEASE_HTML_RE.match(release_url):
                            _api_url = _rewrite_github_release_url(release_url)
                            try:
                                gh_notes = await _fetch_release_notes_from_url(_api_url)
                                if not _is_alert_only(gh_notes):
                                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                                    cache_path.write_text(gh_notes)
                                    return gh_notes
                            except Exception:  # nosec B110
                                pass
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(ws_notes)
                    return ws_notes
            except Exception:  # nosec B110
                pass

        # 2. Try release_url HTTP fetch.  Rewrite HTML github.com release URLs to
        # the API form so the JSON body field is directly accessible.
        if release_url:
            _api_url = _rewrite_github_release_url(release_url)
            try:
                notes = await _fetch_release_notes_from_url(_api_url)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(notes)
                return notes
            except Exception:  # nosec B110
                pass

        # 3. Build a concrete CHANGELOG URL.  Use the owner/repo from release_url
        # when available; fall back to home-assistant/addons only when the release_url
        # itself comes from that repository (i.e. the entity is a real Supervisor
        # add-on from the official store, not a HACS integration).
        _changelog_url: Optional[str] = None
        if release_url:
            _m = _GITHUB_RELEASE_HTML_RE.match(release_url)
            if _m:
                _repo = _m.group(1)  # e.g. "basnijholt/spook"
                _changelog_url = (
                    f"https://raw.githubusercontent.com/{_repo}/main/CHANGELOG.md"
                )
        if _changelog_url is None and entity_id:
            # No release_url or non-GitHub URL: suggest the HA add-ons CHANGELOG
            # only for entities that appear to be official Supervisor add-ons.
            _eid_body = entity_id.removeprefix("update.")
            if _eid_body.endswith("_update"):
                _component = _eid_body[: -len("_update")]
            else:
                _component = _eid_body
            _changelog_url = (
                f"https://raw.githubusercontent.com/home-assistant/addons/master/"
                f"{_component}/CHANGELOG.md"
            )
        if _changelog_url is None:
            return "Release notes unavailable via WebSocket or release URL."
        return (
            f"Release notes unavailable via WebSocket or release URL. "
            f"Fetch the changelog directly: {_changelog_url}"
        )

    fetcher = _fetcher or _fetch_github_release_notes
    notes = await fetcher(version)
    if len(notes.strip()) < 500:
        # GA Core stub — extract the blog URL and fetch the real article
        from utils.knowledge.ha_blog_scraper import (
            extract_blog_url_from_stub,
            fetch_blog_post as _fetch_blog_post,
        )

        _blog_url = extract_blog_url_from_stub(notes)
        _blog_text: Optional[str] = None
        if _blog_url and getattr(_config, "ALLOW_DIAGNOSTIC_WAN", True):
            try:
                if _blog_post_fetcher is not None:
                    _blog_text = _fetch_blog_post(
                        _blog_url, _fetcher=_blog_post_fetcher
                    )
                else:
                    _blog_text = await asyncio.to_thread(_fetch_blog_post, _blog_url)
            except Exception as _blog_exc:  # nosec B110
                log.warning(
                    "release_notes_blog_fetch_failed",
                    url=_blog_url,
                    error=str(_blog_exc),
                )

        if _blog_text and len(_blog_text.strip()) >= 500:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(_blog_text)
            return _blog_text

        # Blog unavailable — beta PR-list as INCOMPLETE fallback (not cached)
        _beta_notes: Optional[str] = None
        parts = version.split(".")
        if len(parts) >= 2:
            minor_base = f"{parts[0]}.{parts[1]}.0"
            for beta_num in range(5, -1, -1):
                try:
                    candidate = await fetcher(f"{minor_base}b{beta_num}")
                    if len(candidate.strip()) >= 500:
                        _beta_notes = candidate
                        break
                except Exception:  # nosec B112
                    continue
        return f"INCOMPLETE: {_beta_notes or notes}"

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(notes)
    return notes


def _format_update_table(updates: list[UpdateStatus]) -> str:
    if not updates:
        return "No update entities found via REST API."

    header = f"{'Component':<30} {'Installed':<20} {'Latest':<20} {'Available':<10}"
    separator = "-" * len(header)
    rows = [header, separator]
    for u in updates:
        available = "YES" if u.update_available else "no"
        rows.append(
            f"{u.component:<30} {u.installed_version:<20} {u.latest_version:<20} {available:<10}"
        )
    return "\n".join(rows)


@dataclass
class UpdatePreflight:
    """Result of a pre-update preflight check."""

    disk_before_gb: float
    disk_after_gb: float
    backups_purged_count: int
    disk_ok: bool  # True when disk_after_gb >= HA_DISK_WARN_GB + 1.5
    supervisor_status: str  # "current" | "update_available" | "unknown"
    problems: list = field(default_factory=list)


# Extra disk headroom required beyond HA_DISK_WARN_GB before an update is considered safe.
_PREFLIGHT_DISK_BUFFER_GB = 1.5


async def run_update_preflight(
    ssh_client: Optional[SSHClientProtocol] = None,
    rest_client: Optional[HARestClientProtocol] = None,
    watch_dir: Optional[Path] = None,
) -> UpdatePreflight:
    """Pre-flight check before a Core or OS update approval card is sent.

    1. Read cached (or live) disk free on the HA host.
    2. If disk is below HA_DISK_WARN_GB + 1.5 GB, run enforce_ha_retention() to delete
       confirmed-offloaded backups from HA, then re-poll disk.
    3. Check whether the Supervisor has a pending update via REST or watch-dir scan.
    Returns a structured summary that is embedded in the update approval card body.
    """
    from .ha_agent_advanced import enforce_ha_retention
    from utils.disk.resource import get_resource_status, poll_host_resources
    from utils.ha.ssh_client import AsyncSSHClient

    disk_threshold = HA_DISK_WARN_GB + _PREFLIGHT_DISK_BUFFER_GB

    # --- Step 1: get current disk reading ---
    cached = get_resource_status()
    if cached is not None:
        disk_before_gb = cached.disk_free_gb
    else:
        ssh = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
        status = await poll_host_resources(
            ssh, HA_DISK_WARN_GB, HA_DISK_CRITICAL_GB, HA_MEM_WARN_MB
        )
        disk_before_gb = status.disk_free_gb

    problems: list[str] = []
    purged = 0
    disk_after_gb = disk_before_gb

    # --- Step 2: enforce retention if disk is low ---
    if disk_before_gb < disk_threshold:
        ssh = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
        purged = await enforce_ha_retention(ssh_client=ssh)
        if purged == 0:
            problems.append(
                "No confirmed-offloaded backups available to delete — "
                "free disk manually before updating"
            )
        # Re-poll after enforcement to get a fresh reading
        fresh = await poll_host_resources(
            ssh, HA_DISK_WARN_GB, HA_DISK_CRITICAL_GB, HA_MEM_WARN_MB
        )
        disk_after_gb = fresh.disk_free_gb
        if disk_after_gb < disk_threshold:
            problems.append(
                f"Disk still at {disk_after_gb:.1f} GB after cleanup — "
                "update may not have enough space"
            )

    disk_ok = disk_after_gb >= disk_threshold

    # --- Step 3: check Supervisor update status ---
    supervisor_status = "unknown"
    try:
        if rest_client is not None:
            updates = await get_update_status(rest_client)
            sup_updates = [
                u for u in updates if "supervisor" in u.entity_id and u.update_available
            ]
            if sup_updates:
                supervisor_status = "update_available"
                problems.append(
                    "Supervisor update available — approve Supervisor before Core"
                )
            else:
                supervisor_status = "current"
        else:
            # Fallback: scan watch-dir for a pending Supervisor update card
            _watch_dir = watch_dir or Path(NOTIFY_WATCH_DIR)
            pending = _pending_higher_priority_components("core", _watch_dir)
            if "supervisor" in pending:
                supervisor_status = "update_available"
                problems.append(
                    "Supervisor update pending — approve Supervisor before Core"
                )
            else:
                supervisor_status = "current"
    except Exception as exc:
        log.error("update_preflight_supervisor_check_failed", error=str(exc))
        supervisor_status = "unknown"

    log.info(
        "update_preflight_complete",
        disk_before_gb=round(disk_before_gb, 2),
        disk_after_gb=round(disk_after_gb, 2),
        backups_purged=purged,
        disk_ok=disk_ok,
        supervisor_status=supervisor_status,
        problems=problems,
    )
    return UpdatePreflight(
        disk_before_gb=disk_before_gb,
        disk_after_gb=disk_after_gb,
        backups_purged_count=purged,
        disk_ok=disk_ok,
        supervisor_status=supervisor_status,
        problems=problems,
    )


def _component_risk_level(component: str) -> RiskLevel:
    """Return the risk level for a component update.

    Core and OS updates always require approval (CRITICAL).
    Supervisor updates are HIGH risk.
    Add-on updates are MEDIUM risk and may auto-execute at autonomy level 4.
    """
    if component in ("core", "os"):
        return RiskLevel.CRITICAL
    if component == "supervisor":
        return RiskLevel.HIGH
    return RiskLevel.MEDIUM


async def request_update_approval(
    update: UpdateStatus,
    gate: "AutonomyGate | FakeAutonomyGate",
    notifier: "NotifierProtocol",
    disk_free_gb: Optional[float] = None,
    disk_warn: bool = False,
    disk_critical: bool = False,
    notification_id: Optional[str] = None,
    watch_dir: Optional[Path] = None,
    ssh_client: Optional[SSHClientProtocol] = None,
    rest_client: Optional[HARestClientProtocol] = None,
) -> bool:
    """Send a per-component update approval card and wait for a decision.

    Returns True if the update should proceed, False if rejected or deferred.
    Risk is always CRITICAL for core/os, HIGH for supervisor, MEDIUM for add-ons.
    For Core and OS updates, runs a pre-flight check (disk clearance + Supervisor status)
    and embeds the result in the card body.
    """
    risk = _component_risk_level(update.component)
    nid = notification_id or str(uuid.uuid4())

    subject = (
        f"Update available: {update.component} "
        f"{update.installed_version} → {update.latest_version}"
    )
    body_parts = [f"Component: {update.component}", f"Risk: {risk.name}"]
    if update.release_summary:
        body_parts.append(f"Summary: {update.release_summary}")

    _watch_dir = watch_dir or Path(NOTIFY_WATCH_DIR)
    pending_higher = _pending_higher_priority_components(update.component, _watch_dir)
    if pending_higher:
        body_parts.append(
            f"⚠️ Apply {', '.join(pending_higher)} update(s) first — "
            "correct order: OS → Supervisor → Core → Add-ons"
        )

    # Run pre-flight for Core and OS updates: disk clearance + Supervisor check.
    preflight: Optional[UpdatePreflight] = None
    if update.component in ("core", "os"):
        try:
            preflight = await run_update_preflight(
                ssh_client=ssh_client,
                rest_client=rest_client,
                watch_dir=_watch_dir,
            )
            if preflight.backups_purged_count > 0:
                body_parts.append(
                    f"Pre-flight: freed space by removing "
                    f"{preflight.backups_purged_count} old backup(s) from HA — "
                    f"disk now {preflight.disk_after_gb:.1f} GB"
                )
            if not preflight.disk_ok:
                body_parts.append(
                    f"⚠️ Disk exhaustion risk: HA Supervisor downloads ~2–3 GB during "
                    f"a Core update. With {preflight.disk_after_gb:.1f} GB free, disk "
                    "may reach 0 GB. Free space before approving."
                )
            if (
                preflight.supervisor_status == "update_available"
                and update.component == "core"
            ):
                body_parts.append(
                    "⚠️ Supervisor update available — approve Supervisor before Core"
                )
        except Exception as exc:
            log.error("update_preflight_failed", error=str(exc))

    body = "\n".join(body_parts)

    from utils.hitl.card_types import CARD_TYPE_UPDATE

    payload: dict = {
        "notification_id": nid,
        "card_type": CARD_TYPE_UPDATE,
        "component": update.component,
        "entity_id": update.entity_id,
        "installed_version": update.installed_version,
        "latest_version": update.latest_version,
        "release_url": update.release_url,
        "release_summary": update.release_summary,
        "risk": risk.name,
        "severity": risk.name,
        "breaking_changes": [],
        "affected_config_keys": [],
        "pueo_command_risks": [],
        "advisory": None,
        "safe_to_update": None,
        "disk_free_gb": disk_free_gb,
        "disk_warn": disk_warn,
        "disk_critical": disk_critical,
        "preflight_disk_before_gb": preflight.disk_before_gb if preflight else None,
        "preflight_disk_after_gb": preflight.disk_after_gb if preflight else None,
        "preflight_backups_purged": preflight.backups_purged_count if preflight else 0,
        "preflight_disk_ok": preflight.disk_ok if preflight else None,
        "preflight_supervisor_status": (
            preflight.supervisor_status if preflight else None
        ),
        "preflight_problems": preflight.problems if preflight else [],
        "disk_exhaustion_risk": (preflight is not None and not preflight.disk_ok),
    }

    log.info(
        "update_hitl_card_sent",
        component=update.component,
        version=update.latest_version,
        risk=risk.name,
        notification_id=nid,
    )
    return await gate.queue_for_approval(subject, body, payload, notifier, risk)


async def run_update_check(
    ha_rest_client: Optional[HARestClientProtocol] = None,
    ssh_client: Optional[SSHClientProtocol] = None,
    gate: Optional["AutonomyGate | FakeAutonomyGate"] = None,
    notifier: Optional["NotifierProtocol"] = None,
    knowledge_store: Optional[Any] = None,
) -> list[UpdateStatus]:
    """One-shot: print update status table and submit agentic analysis WorkItems."""
    if not ha_rest_client and not HA_API_TOKEN:
        log.error(
            "update_check_no_token",
            detail="Set home_assistant.api_token in config.yaml to use update-check.",
        )
        print(
            "Error: HA_API_TOKEN is not set. "
            "Add api_token under home_assistant in config.yaml."
        )
        return []

    rest: HARestClientProtocol = ha_rest_client or HARestClient(
        HA_HOST, HA_API_PORT, HA_API_TOKEN
    )

    try:
        updates = await get_update_status(rest)
    except Exception as exc:
        log.error("update_check_failed", error=str(exc))
        print(f"Error fetching update status: {exc}")
        return []

    print(_format_update_table(updates))

    available = [u for u in updates if u.update_available]
    if available:
        print(f"\n{len(available)} update(s) available.")
        for u in available:
            if u.release_url:
                print(f"  {u.component}: {u.release_url}")

    from utils.hitl.notify import get_notifier

    _notifier = notifier or get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)

    for update in available:
        await _run_update_analysis(
            update,
            ssh_client=ssh_client,
            notifier=_notifier,
            knowledge_store=knowledge_store,
            ha_rest_client=rest,
        )

    return updates


def classify_update_type(
    component: str,
    entity_id: str,
    profile: Optional["HAEnvironmentProfile"] = None,
) -> str:
    """Return the update type used in the session signature ``update:<type>:<slug>``.

    A component that matches a HACS repository slug or integration domain in the
    profile is ``hacs_integration`` so ``seed_update_hacs.md`` attaches (#945).
    """
    if component in ("core", "os", "supervisor"):
        return component
    if component.lower() == "hacs":
        return "hacs"
    if "firmware" in entity_id.lower():
        return "device"
    if profile is not None:
        hacs_names = {
            n.lower().replace("-", "_")
            for n in [
                *(profile.hacs_integrations or []),
                *(profile.hacs_unloaded_domains or []),
                *(v[1] for v in (profile.hacs_repo_map or {}).values() if v),
            ]
            if n
        }
        if component.lower().replace("-", "_") in hacs_names:
            return "hacs_integration"
    return "addon"


async def _resolve_addon_slug(
    component: str,
    ssh_client: Any,
    entity_picture: Optional[str] = None,
) -> Optional[str]:
    """Return the Supervisor slug for an add-on whose entity component is *component*.

    First tries to extract the slug directly from the update entity's
    ``entity_picture`` URL (``/api/hassio/addons/<slug>/icon``), which HA always
    sets and requires no SSH query.  Falls back to querying
    ``ha apps list --raw-json`` and matching by:

    - slug == component (exact)
    - slug.endswith("_" + component) (prefix, e.g. core_matter_server → matter_server)
    - url ending in "/<component>"

    Returns None on no match or SSH error.
    """
    if entity_picture:
        m = _HASSIO_ADDON_ICON_RE.match(entity_picture)
        if m:
            return m.group(1)
    try:
        _, stdout, _ = await ssh_client.run("ha apps list --raw-json", check=False)
        data = json.loads(stdout)
        addons = (
            data.get("data", {}).get("addons", []) if isinstance(data, dict) else []
        )
    except Exception:
        return None
    for app in addons:
        slug = str(app.get("slug") or "")
        if not slug:
            continue
        if slug == component:
            return slug
        if slug.endswith("_" + component):
            return slug
        url = str(app.get("url") or "")
        if url and url.rstrip("/").endswith("/" + component):
            return slug
    return None


# Per-BC sessions share the update entity as subject_key, so earlier analyses of
# the same pending update would be injected as "Previous investigations" and
# anchor the verdict to a stale conclusion (#1209).  Screening batches write
# rows under the same key with no conclusion, which is noise (#1211).
_BC_EXCLUDED_PRIOR_SESSION_TYPES = (
    "update_analysis",
    "bc_assessment",
    "change_screening",
)


def _bc_session_outcome(
    loop_outcome: str, bc_name: str, assessments: dict[str, dict]
) -> str:
    """Return the outcome of one BC assessment session.

    AgentLoop reports "success" whenever the terminal tool was called, even when
    finish_bc_assessment rejected the call (missing/invalid verdict).  A session
    that stored no verdict is "no_verdict", so the caller retries and then
    records the BC as not_assessed instead of treating it as judged.
    """
    if loop_outcome == "success" and bc_name not in assessments:
        return "no_verdict"
    return loop_outcome


async def _run_update_analysis(
    update: UpdateStatus,
    ssh_client: Optional[SSHClientProtocol] = None,
    notifier: Optional["NotifierProtocol"] = None,
    knowledge_store: Optional[Any] = None,
    ha_profile: Optional["HAEnvironmentProfile"] = None,
    ha_rest_client: Optional[HARestClientProtocol] = None,
) -> bool:
    """Run a single AgentLoop to analyse an update and create the HITL card via the terminal tool.

    Returns True when the analysis was accepted by the work queue (or when the work queue is
    absent), False when it was deduplicated (another analysis for this entity is already queued
    or running).  The caller should write the "update_available" timeline entry and log event
    only on True so that dedup drops do not produce duplicate entries.
    """
    from utils.agent.agent_loop import AgentLoop
    from utils.agent.supervisor import (
        decrement_active_agent,
        increment_active_agent,
        make_activity_timeline_callback,
    )
    from utils.agent.tool_executor import ToolExecutor
    from utils.agent.tool_registry import build_update_analysis_registry
    from utils.agent.work_queue import (
        PRIORITY_NORMAL,
        WorkItem,
        get_work_queue_or_none,
    )
    from utils.core.prompts import load_agent_prompt
    from utils.llm.llm_factory import make_llm_client
    from utils.hitl.notify import get_notifier

    terminal_tool = "finish_update_analysis"
    _notifier = notifier or get_notifier(NOTIFIER, NOTIFY_URL, NOTIFY_WATCH_DIR)

    async def _coro() -> None:
        from utils.ha.ssh_client import AsyncSSHClient
        from utils.agent.autonomy import AutonomyGate

        gate = AutonomyGate(AUTONOMY_LEVEL)
        _ssh = ssh_client or AsyncSSHClient(HA_HOST, HA_USER, SSH_KEY_PATH)
        executor = ToolExecutor(
            ha_ssh_client=_ssh,  # type: ignore[arg-type]
            gate=gate,  # type: ignore[arg-type]
            notifier=_notifier,
            ha_rest_client=ha_rest_client,
            knowledge_store=knowledge_store,
            db_path=DB_PATH,
        )
        executor.set_update_status(update)

        # Inject a WS client so the executor can call update/release_notes directly.
        _ws_client = None
        if HA_API_TOKEN:
            from utils.ha.ha_ws_client import HAWebSocketClient

            _ws_client = HAWebSocketClient(  # pragma: no cover
                HA_HOST, HA_API_PORT, HA_API_TOKEN
            )
            executor.set_ws_client(_ws_client)  # pragma: no cover

        # Profile fallback: use the persisted profile when none is passed in
        from utils.ha.ha_environment import load_environment_profile

        _profile = ha_profile or load_environment_profile(DB_PATH)
        if _profile is not None:
            executor.set_ha_profile(_profile)

        system_prompt = load_agent_prompt("update", terminal_tool)
        from utils.ha.context_sanitizer import sanitize_update_context
        from utils.ha.device_summarizer import device_context_summary

        _update_type_early = classify_update_type(
            update.component, update.entity_id, _profile
        )
        initial_context = (
            f"Available update: {update.component} "
            f"{update.installed_version} → {update.latest_version}"
        )
        # Expose the resolved Supervisor slug for add-on updates so the agent can
        # use it in `ha apps info <slug>` without guessing.  update.component is the
        # HA entity component name (e.g. "matter_server"), which may differ from the
        # actual slug (e.g. "core_matter_server"); resolve via `ha apps list --raw-json`.
        if _update_type_early == "addon":
            _resolved_slug = await _resolve_addon_slug(
                update.component,
                _ssh,
                entity_picture=getattr(update, "entity_picture", None),
            )
            if _resolved_slug:
                initial_context += f"\nSupervisor slug: {_resolved_slug}"
                if _resolved_slug in _SSH_TRANSPORT_SLUGS:
                    initial_context += (
                        "\nNote: This add-on provides Pueo's SSH transport to HA. "
                        "Pueo will lose SSH connectivity while the add-on restarts — "
                        "this is expected and not a failure."
                    )
        if update.release_url:
            initial_context += f"\nRelease URL: {update.release_url}"
        if update.release_summary:
            initial_context += f"\nSummary: {update.release_summary}"

        env_summary = device_context_summary(_profile)
        if env_summary:
            initial_context += f"\n\n{env_summary}"

        # Inform the agent when a HACS integration has no config entry so it can
        # set safe_to_update=True without requiring a backup (zero runtime impact).
        if _profile is not None and update.component in (
            _profile.hacs_unloaded_domains or []
        ):
            initial_context += (
                f"\nNote: {update.component} is downloaded via HACS but has no "
                "config entry — updating has no runtime effect until set up."
            )

        # Best-effort: trigger and read upgrade-advisor sensor (brianegge/ha-upgrade-advisor)
        _rest = ha_rest_client
        if _rest is not None:
            try:
                from utils.ha.upgrade_advisor import (
                    read_advisor_report,
                    request_advisor_analysis,
                )

                # Trigger analysis when the integration is installed, so the report
                # is fresh for this exact version.  Add-ons are skipped inside
                # request_advisor_analysis because upstream does not support them.
                _trigger_time: Optional[float] = None
                if _profile is not None and _profile.upgrade_advisor_installed:
                    _trigger_time = time.time()
                    await request_advisor_analysis(
                        _rest,
                        update.latest_version,
                        update.component,
                        ws_client=_ws_client,
                    )

                advisor = await read_advisor_report(
                    _rest,
                    update.latest_version,
                    ws_client=_ws_client,
                    component_type=update.component,
                    trigger_time=_trigger_time,
                )
                if advisor is not None:
                    lines = [
                        "Third-party upgrade-advisor report"
                        " (unverified — treat as advisory only):",
                        f"  Risk: {advisor.risk}",
                        f"  Breaking changes: {advisor.breaking_change_count}",
                    ]
                    if advisor.report:
                        lines.append(f"  Report:\n{advisor.report}")
                    initial_context = "\n".join(lines) + "\n\n" + initial_context
            except Exception:  # nosec B110
                pass

        initial_context = sanitize_update_context(initial_context)

        # ── Core: build report + run screening batches (S3) ──────────────
        # For Core updates only: build the structured release report (step 1),
        # then run sequential LLM screening batches (step 2) inside this same
        # WorkItem so the no-concurrent-LLM rule is satisfied.  The main loop
        # (step 3) then calls get_update_release_notes which returns a compact
        # digest from the report instead of re-fetching the blog.
        _rpt = None  # set by the Core path; used for slim-path digest injection below
        _is_core_update = _update_type_early == "core"
        if _is_core_update and _profile is not None and HA_API_TOKEN:
            from utils.knowledge.ha_release_report import (
                CoreReleaseReport as _CRR,
                ScreeningFlag,
                ScreeningHighlight,
                build_core_release_report,
                make_screening_batches,
            )
            from utils.agent.tool_registry import build_change_screening_registry
            from utils.knowledge.runbook_signature import sig_update as _sig_update
            from paths import get_dirs as _get_dirs
            import config as _cfg

            _installed = list(
                _profile.loaded_components or _profile.installed_integrations or []
            )
            _report_path = (
                _get_dirs().state_dir
                / "update_reports"
                / f"core_{update.latest_version}.json"
            )

            # Step 1: build report (deterministic)
            try:
                _rpt = await build_core_release_report(
                    update.latest_version,
                    _installed,
                    ws_client=_ws_client,
                    cache_dir=_cfg.HA_UPDATE_RELEASE_NOTES_CACHE_DIR,
                    state_dir=_get_dirs().state_dir,
                    installed_version=update.installed_version or "",
                )
                log.info(
                    "core_release_report_built",
                    version=update.latest_version,
                    completeness=_rpt.completeness,
                    bc_installed=len(_rpt.bc_installed),
                    bc_general=len(_rpt.bc_general),
                    changelog_domains=len(_rpt.changes_by_domain),
                )
            except Exception as _rpt_exc:
                log.warning(
                    "core_release_report_failed",
                    version=update.latest_version,
                    error=str(_rpt_exc),
                )
                _rpt = None  # type: ignore[assignment]

            # Step 2: screening batches (LLM, sequential, same WorkItem)
            if _rpt is not None:
                executor.clear_screening_state()
                _batches = make_screening_batches(_rpt)
                _all_complete = True
                _batches_total = 0
                _batches_done = 0
                for _batch_idx, _batch_text in enumerate(_batches):
                    if not _batch_text.strip():
                        continue
                    _batches_total += 1
                    _screen_wq = get_work_queue_or_none()
                    if _screen_wq is not None:
                        _screen_wq.set_running_detail(
                            f"screening batch {_batch_idx + 1} of {len(_batches)}"
                        )
                    _screen_t0 = time.monotonic()
                    log.info(
                        "screening_batch_started",
                        batch=_batch_idx + 1,
                        total=len(_batches),
                    )
                    _batch_outcome = "failed"
                    for _attempt in range(2):
                        _screen_t0 = time.monotonic()
                        try:
                            _screen_prompt = load_agent_prompt(
                                "change_screening", "finish_change_screening"
                            )
                            _screen_llm = make_llm_client()  # pragma: no cover
                            _screen_loop = AgentLoop(
                                llm_client=_screen_llm,
                                tool_executor=executor,
                                tool_registry=build_change_screening_registry(),
                                system_prompt=_screen_prompt,
                                terminal_tool_name="finish_change_screening",
                                trigger="update_poll",
                                activity_type="change_screening",
                                db_path=DB_PATH,
                                knowledge_store=knowledge_store,
                                capture_llm=False,
                                max_tool_calls=3,
                                distill=False,
                                think=False,
                                limit_review=False,
                                num_predict=500,
                            )
                            _screen_result = await _screen_loop.run(
                                (
                                    f"HA Core {update.latest_version} — batch"
                                    f" {_batch_idx + 1} of {len(_batches)}:"
                                    f"\n\n{_batch_text}"
                                ),
                                signature=_sig_update("core_screen", update.component),
                                subject_key=update.entity_id,
                            )
                            _screen_elapsed = round(time.monotonic() - _screen_t0, 1)
                            _batch_outcome = _screen_result.outcome
                            log.info(
                                "screening_batch_done",
                                batch=_batch_idx + 1,
                                total=len(_batches),
                                elapsed=_screen_elapsed,
                                outcome=_batch_outcome,
                                attempt=_attempt + 1,
                            )
                            if _batch_outcome == "success":
                                break
                            if _attempt == 0:
                                log.warning(
                                    "screening_batch_incomplete_retrying",
                                    batch=_batch_idx + 1,
                                    outcome=_batch_outcome,
                                )
                        except Exception as _se:
                            _screen_elapsed = round(time.monotonic() - _screen_t0, 1)
                            log.warning(
                                "screening_batch_failed",
                                batch=_batch_idx + 1,
                                attempt=_attempt + 1,
                                error=str(_se),
                            )
                            log.info(
                                "screening_batch_done",
                                batch=_batch_idx + 1,
                                total=len(_batches),
                                elapsed=_screen_elapsed,
                                outcome="failed",
                                attempt=_attempt + 1,
                            )
                    if _batch_outcome == "success":
                        _batches_done += 1
                    else:
                        _all_complete = False
                        log.warning(
                            "screening_batch_incomplete",
                            batch=_batch_idx + 1,
                            outcome=_batch_outcome,
                        )

                # Persist screening results to report
                _flags, _hls, _sc = executor.get_screening_results()
                _rpt.screening_flags = [
                    ScreeningFlag(**f) if isinstance(f, dict) else f for f in _flags
                ]
                _rpt.screening_highlights = [
                    ScreeningHighlight(**h) if isinstance(h, dict) else h for h in _hls
                ]
                _rpt.screening_complete = _all_complete
                _rpt.screening_batches_total = _batches_total
                _rpt.screening_batches_done = _batches_done
                # completeness stays the deterministic notes status: a stuck screening
                # batch must not make the card claim the notes were missing (#1148).
                try:
                    _report_dir = _get_dirs().state_dir / "update_reports"
                    _report_dir.mkdir(parents=True, exist_ok=True)
                    (_report_dir / f"core_{update.latest_version}.json").write_text(
                        _rpt.model_dump_json(indent=2), encoding="utf-8"
                    )
                    log.info(
                        "core_release_report_screening_persisted",
                        version=update.latest_version,
                        flags=len(_flags),
                        highlights=len(_hls),
                        complete=_all_complete,
                    )
                except Exception as _persist_exc:
                    log.warning(
                        "core_release_report_persist_failed",
                        error=str(_persist_exc),
                    )

                # Step 3 (new): BC evidence builder — gather entity/dashboard facts per BC
                try:
                    from utils.ha.update_bc_evidence import (
                        build_bc_evidence as _build_bc_ev,
                    )

                    _bc_ev, _flag_ev = await _build_bc_ev(
                        _rpt,
                        ws_client=_ws_client,
                        rest_client=ha_rest_client,
                        ssh_client=_ssh,
                        config_yaml_top_keys=(
                            _profile.config_yaml_top_keys if _profile else []
                        ),
                    )
                    if _bc_ev or _flag_ev:
                        _rpt.bc_evidence = _bc_ev
                        _rpt.flag_evidence = _flag_ev
                        _ev_path = (
                            _get_dirs().state_dir
                            / "update_reports"
                            / f"core_{update.latest_version}.json"
                        )
                        _ev_path.write_text(
                            _rpt.model_dump_json(indent=2), encoding="utf-8"
                        )
                        log.info(
                            "core_bc_evidence_built",
                            version=update.latest_version,
                            bcs=len(_bc_ev),
                            flags=len(_flag_ev),
                        )
                except Exception as _ev_exc:
                    log.warning("core_bc_evidence_failed", error=str(_ev_exc))

            # Step 4 (new): per-BC assessment pass
            if _rpt is not None and (list(_rpt.bc_installed) + list(_rpt.bc_general)):
                from utils.agent.tool_registry import build_bc_assessment_registry
                from utils.knowledge.runbook_signature import (
                    sig_update as _sig_update_bc,
                )

                executor.clear_bc_assessment_state()
                _all_bcs = list(_rpt.bc_installed) + list(_rpt.bc_general)

                # Skip assessment pass if the report already has complete verdicts
                # (e.g. a previous run stored them before the main analysis failed).
                # This avoids re-running all sessions on every 30-min retry (#1215).
                _existing_names = {
                    v["bc"]
                    for v in _rpt.bc_verdicts
                    if isinstance(v, dict) and "bc" in v
                }
                _all_bc_names = {e.name for e in _all_bcs}
                _recognized = {"action_needed", "no_action", "user_check"}
                _all_assessed = _all_bc_names.issubset(_existing_names) and all(
                    v.get("verdict") in _recognized
                    for v in _rpt.bc_verdicts
                    if isinstance(v, dict) and v.get("bc") in _all_bc_names
                )
                if _all_assessed:
                    log.info(
                        "bc_assessment_reusing_stored_verdicts",
                        count=len(_all_bc_names),
                    )
                    for _v in _rpt.bc_verdicts:
                        if isinstance(_v, dict) and _v.get("bc") in _all_bc_names:
                            executor._bc_assessments[_v["bc"]] = dict(_v)  # type: ignore[attr-defined]
                else:
                    for _bc_idx, _bc_entry in enumerate(_all_bcs):
                        _assess_wq = get_work_queue_or_none()
                        if _assess_wq is not None:
                            _assess_wq.set_running_detail(
                                f"assessing BC {_bc_idx + 1} of {len(_all_bcs)}"
                            )
                        executor._current_bc_name = _bc_entry.name  # type: ignore[attr-defined]
                        _ev_block = ""
                        if _rpt.bc_evidence.get(_bc_entry.name):
                            from utils.knowledge.ha_release_report import (
                                _evidence_lines,
                            )

                            _ev_lines = _evidence_lines(
                                _rpt.bc_evidence[_bc_entry.name]
                            )
                            if _ev_lines:
                                _ev_block = "\n\nEvidence:\n" + "\n".join(
                                    f"- {l}" for l in _ev_lines
                                )
                        _is_installed_bc = _bc_entry in _rpt.bc_installed
                        _install_note = ""
                        if not _is_installed_bc and _profile and _profile.os_version:
                            _install_note = "\n\nInstall type: HAOS (Home Assistant Operating System)"
                        _bc_context = (
                            f"HA Core {update.latest_version} breaking change assessment:\n\n"
                            f"**{_bc_entry.name}**"
                            f"{' (installed integration)' if _is_installed_bc else ' (general)'}"
                            f"{_install_note}"
                            f"\n\n{_bc_entry.body or '(no body)'}"
                            f"{_ev_block}"
                        )
                        _assess_outcome = "failed"
                        for _attempt in range(2):
                            _assess_t0 = time.monotonic()
                            try:
                                _assess_prompt = load_agent_prompt(
                                    "bc_assessment", "finish_bc_assessment"
                                )
                                _assess_llm = make_llm_client()  # pragma: no cover
                                _assess_loop = AgentLoop(
                                    llm_client=_assess_llm,
                                    tool_executor=executor,
                                    tool_registry=build_bc_assessment_registry(),
                                    system_prompt=_assess_prompt,
                                    terminal_tool_name="finish_bc_assessment",
                                    trigger="update_poll",
                                    activity_type="bc_assessment",
                                    db_path=DB_PATH,
                                    knowledge_store=knowledge_store,
                                    capture_llm=True,
                                    max_tool_calls=1,
                                    distill=False,
                                    think=False,
                                    limit_review=False,
                                    num_predict=500,
                                    exclude_prior_session_types=list(
                                        _BC_EXCLUDED_PRIOR_SESSION_TYPES
                                    ),
                                )
                                _assess_result = await _assess_loop.run(
                                    _bc_context,
                                    signature=_sig_update_bc(
                                        "core_bc",
                                        (
                                            _bc_entry.domains[0]
                                            if _bc_entry.domains
                                            else "core"
                                        ),
                                    ),
                                    subject_key=update.entity_id,
                                )
                                _assess_elapsed = round(
                                    time.monotonic() - _assess_t0, 1
                                )
                                _assess_outcome = _bc_session_outcome(
                                    _assess_result.outcome,
                                    _bc_entry.name,
                                    executor._bc_assessments,  # type: ignore[attr-defined]
                                )
                                log.info(
                                    "bc_assessment_done",
                                    bc=_bc_entry.name,
                                    elapsed=_assess_elapsed,
                                    outcome=_assess_outcome,
                                    attempt=_attempt + 1,
                                )
                                if _assess_outcome == "success":
                                    break
                                if _attempt == 0:
                                    log.warning(
                                        "bc_assessment_incomplete_retrying",
                                        bc=_bc_entry.name,
                                        outcome=_assess_outcome,
                                    )
                            except Exception as _ae:
                                _assess_elapsed = round(
                                    time.monotonic() - _assess_t0, 1
                                )
                                log.warning(
                                    "bc_assessment_failed",
                                    bc=_bc_entry.name,
                                    attempt=_attempt + 1,
                                    error=str(_ae),
                                )
                        if _assess_outcome != "success":
                            # Mark gap: verdict will be unknown + card prefix
                            executor._bc_assessment_gap = True  # type: ignore[attr-defined]
                            # Store unknown fallback so BC appears in the verdict table
                            executor._bc_assessments[_bc_entry.name] = {  # type: ignore[attr-defined]
                                "bc": _bc_entry.name,
                                "verdict": "not_assessed",
                                "reason": "assessment did not complete",
                            }
                            log.warning(
                                "bc_assessment_incomplete",
                                bc=_bc_entry.name,
                                outcome=_assess_outcome,
                            )

                # Persist verdicts to report JSON
                if executor._bc_assessments:  # type: ignore[attr-defined]
                    _rpt.bc_verdicts = list(
                        executor._bc_assessments.values()  # type: ignore[attr-defined]
                    )
                    try:
                        _report_dir = _get_dirs().state_dir / "update_reports"
                        _report_dir.mkdir(parents=True, exist_ok=True)
                        (_report_dir / f"core_{update.latest_version}.json").write_text(
                            _rpt.model_dump_json(indent=2), encoding="utf-8"
                        )
                        log.info(
                            "bc_assessment_verdicts_persisted",
                            version=update.latest_version,
                            count=len(executor._bc_assessments),  # type: ignore[attr-defined]
                        )
                    except Exception as _persist_exc:
                        log.warning(
                            "bc_assessment_persist_failed",
                            error=str(_persist_exc),
                        )

        # ── Slim path: when BC assessments ran, pre-inject the digest and use the
        # slim registry (no bc_verdicts in the tool schema) so the model writes only
        # summary/highlights.  Also exclude prior update_analysis sessions from
        # the Previous Investigations block to avoid anchoring to stale conclusions.
        _has_assessments = bool(getattr(executor, "_bc_assessments", None))
        from utils.agent.tool_registry import build_update_analysis_slim_registry

        if _has_assessments and _rpt is not None:
            from utils.knowledge.ha_release_report import format_core_release_digest

            _digest = format_core_release_digest(_rpt)
            if _digest:
                initial_context = (
                    initial_context
                    + "\n\n## Pre-computed BC assessment results\n\n"
                    + _digest
                )
                # The model now has the notes and may finish without calling
                # get_update_release_notes, so record them as obtained (#1211).
                executor.adopt_core_release_report(_rpt)

        _analyse_wq = get_work_queue_or_none()
        if _analyse_wq is not None:
            _analyse_wq.set_running_detail("analysing")
        llm_client = make_llm_client()  # pragma: no cover
        loop = AgentLoop(
            llm_client=llm_client,
            tool_executor=executor,
            tool_registry=(
                build_update_analysis_slim_registry()
                if _has_assessments
                else build_update_analysis_registry()
            ),
            system_prompt=system_prompt,
            terminal_tool_name=terminal_tool,
            trigger="update_poll",
            activity_type="update_analysis",
            db_path=DB_PATH,
            knowledge_store=knowledge_store,
            timeline_callback=make_activity_timeline_callback(
                "update_analysis",
                trigger=f"Update: {update.component} → {update.latest_version}",
            ),
            capture_llm=True,
            think=False,
            num_predict=1000,
            exclude_prior_session_types=(
                ["update_analysis"] if _has_assessments else []
            ),
        )
        _knowledge_query = (
            f"{update.component} "
            f"{update.installed_version} → {update.latest_version} "
            "update breaking changes"
        )
        from utils.knowledge.runbook_signature import sig_update

        _update_type = _update_type_early
        _result: "AgentLoopResult | None" = None
        increment_active_agent()
        try:
            _result = await loop.run(
                initial_context,
                knowledge_query=_knowledge_query,
                signature=sig_update(_update_type, update.component),
                subject_key=update.entity_id,
            )
        except Exception as _exc:
            # An unhandled exception from loop.run leaves no suppression or analyzed
            # key, so the next poll would immediately retry.  Write the same backoff
            # row that a stuck/failed outcome would produce to enforce a 30-minute
            # quiet period.
            import sqlite3 as _sqlite3
            from utils.hitl.hitl_tracker import mark_investigation_backoff

            _backoff_key = f"update_analysis_backoff:{update.entity_id}"
            try:
                with _sqlite3.connect(DB_PATH) as _conn:
                    mark_investigation_backoff(_conn, _backoff_key)
            except Exception:  # nosec B110
                pass
            log.error(
                "update_analysis_exception_backoff",
                component=update.component,
                error=str(_exc),
            )
            raise
        else:
            if _result.outcome != "success":
                import sqlite3 as _sqlite3
                from utils.hitl.hitl_tracker import mark_investigation_backoff

                _backoff_key = f"update_analysis_backoff:{update.entity_id}"
                with _sqlite3.connect(DB_PATH) as _conn:
                    mark_investigation_backoff(_conn, _backoff_key)
                log.warning(
                    "update_analysis_stuck_backoff",
                    outcome=_result.outcome,
                    component=update.component,
                )
            elif executor._pending_auto_apply:
                # Gate (tool_executor.py) already enforces the L4+high-confidence
                # requirement for core/OS/supervisor (ADR 035); trust it here.
                log.info(
                    "update_auto_apply_start",
                    component=update.component,
                    version=update.latest_version,
                )
                _entities_executing_update.add(update.entity_id)
                try:
                    _update_ok = await execute_update(update, _ssh, _notifier, gate)
                    from agents.ha_log_monitor import _update_mark_auto_applied
                    from utils.hitl.card_types import CARD_TYPE_UPDATE

                    suppression_key = f"update:{update.entity_id}"
                    await asyncio.to_thread(
                        _update_mark_auto_applied,
                        suppression_key,
                        CARD_TYPE_UPDATE,
                        f"Auto-applied: {update.component} {update.latest_version}",
                    )
                    # Verify the analysis session outcome by install result.
                    try:
                        from agents.ha_agent_advanced import resolve_session_outcome

                        _verif = "success" if _update_ok else "failure"
                        await asyncio.to_thread(
                            resolve_session_outcome,
                            update.entity_id,
                            _verif,
                            "system",
                        )
                        if _verif == "success":
                            await _enqueue_distillation_for_subject(
                                update.entity_id, "success"
                            )
                    except Exception:  # nosec B110
                        pass
                except Exception as _exc:
                    log.error(
                        "update_auto_apply_failed",
                        component=update.component,
                        error=str(_exc),
                    )
                    try:
                        from agents.ha_agent_advanced import resolve_session_outcome

                        await asyncio.to_thread(
                            resolve_session_outcome,
                            update.entity_id,
                            "failure",
                            "system",
                        )
                    except Exception:  # nosec B110
                        pass
                    await _notifier.send(
                        subject=f"Auto-update failed: {update.component} {update.latest_version}",
                        body=f"Error: {_exc}",
                        payload={},
                    )
                finally:
                    _entities_executing_update.discard(update.entity_id)
        finally:
            decrement_active_agent()
            try:
                from utils.agent.supervisor import publish_activity_done

                publish_activity_done(
                    "update_analysis",
                    _result.outcome if _result is not None else "failed",
                )
            except Exception:  # nosec B110 — best-effort SSE
                pass

    _wq = get_work_queue_or_none()
    if _wq is not None:
        return await _wq.submit(
            WorkItem(
                priority=PRIORITY_NORMAL,
                activity_type="update_analysis",
                description=f"Update analysis: {update.component} → {update.latest_version}",
                dedup_key=f"update_analysis:{update.entity_id}",
                suppress_while_running=frozenset(),
                coro_factory=_coro,
            )
        )
    else:
        await _coro()
        return True


# ── Pueo Self-Check (item 37) ─────────────────────────────────────────────────

# NetAlertX add-on slug used in self-check smoke test.
_NETALERTX_SLUG = "db21ed7f_netalertx_fa"
# Disk buffer beyond the warn threshold needed to run the backup smoke test.
_BACKUP_SMOKE_DISK_BUFFER_GB = 2.0


@dataclass
class PueoSelfCheckResult:
    """Results of Pueo's post-Core-update self-check."""

    core_check_ok: bool
    core_info_ok: bool
    apps_list_ok: bool
    netalertx_info_ok: bool
    backup_smoke_ok: Optional[bool]  # None = skipped (disk constrained)
    command_risks: list[str] = field(default_factory=list)

    @property
    def all_commands_ok(self) -> bool:
        return (
            self.core_check_ok
            and self.core_info_ok
            and self.apps_list_ok
            and self.netalertx_info_ok
        )


async def run_pueo_self_check(
    ssh_client: SSHClientProtocol,
    version: str,
    disk_free_gb: Optional[float] = None,
) -> PueoSelfCheckResult:
    """Run CLI smoke tests after a Core update."""
    # ── CLI smoke tests ────────────────────────────────────────────────────────
    core_check_ok = False
    try:
        ec, _, _ = await ssh_client.run("ha core check", check=False)
        core_check_ok = ec == 0
    except Exception as exc:
        log.warning("self_check_core_check_failed", error=str(exc))

    core_info_ok = False
    try:
        ec, stdout, _ = await ssh_client.run("ha core info --raw-json", check=False)
        core_info_ok = ec == 0 and bool(stdout.strip())
    except Exception as exc:
        log.warning("self_check_core_info_failed", error=str(exc))

    apps_list_ok = False
    try:
        ec, _, _ = await ssh_client.run("ha apps list", check=False)
        apps_list_ok = ec == 0
    except Exception as exc:
        log.warning("self_check_apps_list_failed", error=str(exc))

    netalertx_info_ok = False
    try:
        ec, _, _ = await ssh_client.run(f"ha apps info {_NETALERTX_SLUG}", check=False)
        netalertx_info_ok = ec == 0
    except Exception as exc:
        log.warning("self_check_netalertx_info_failed", error=str(exc))

    # ── Optional backup smoke test ─────────────────────────────────────────────
    backup_smoke_ok: Optional[bool] = None
    disk_ok = (
        disk_free_gb is not None
        and disk_free_gb > HA_DISK_WARN_GB + _BACKUP_SMOKE_DISK_BUFFER_GB
    )
    if disk_ok:
        try:
            from .ha_agent_advanced import _extract_backup_slug

            _, bk_out, _ = await ssh_client.run(
                'ha backup new --name "pueo_selfcheck_DELETE_ME"', check=False
            )
            slug = _extract_backup_slug(bk_out.strip())
            if slug:
                await ssh_client.run(f"ha backup remove {slug}", check=False)
                backup_smoke_ok = True
            else:
                backup_smoke_ok = False
        except Exception as exc:
            log.warning("self_check_backup_smoke_failed", error=str(exc))
            backup_smoke_ok = False

    command_risks: list[str] = []

    result = PueoSelfCheckResult(
        core_check_ok=core_check_ok,
        core_info_ok=core_info_ok,
        apps_list_ok=apps_list_ok,
        netalertx_info_ok=netalertx_info_ok,
        backup_smoke_ok=backup_smoke_ok,
        command_risks=command_risks,
    )
    log.info(
        "self_check_complete",
        version=version,
        all_commands_ok=result.all_commands_ok,
        command_risks=len(command_risks),
    )
    return result


# ── Update Execution (item 36) ────────────────────────────────────────────────

_CORE_UPDATE_TIMEOUT = 480  # seconds
_OS_UPDATE_TIMEOUT = 480
_ADDON_UPDATE_TIMEOUT = 180
_POLL_INTERVAL_CORE = 15
_POLL_INTERVAL_OS = 15
_POLL_INTERVAL_ADDON = 10


_DISK_CHECK_EVERY_N_POLLS = 4  # check disk every ~60 s during a Core update poll


async def _poll_core_version(
    target_version: str,
    ssh_client: SSHClientProtocol,
    timeout_seconds: int = _CORE_UPDATE_TIMEOUT,
    interval: int = _POLL_INTERVAL_CORE,
    _sleep: Optional[Callable] = None,
    _get_disk: Optional[Callable] = None,
) -> bool:
    """Poll `ha core info` every ``interval`` seconds until version matches target or timeout."""
    from utils.disk.resource import get_resource_status

    sleep_fn = _sleep or asyncio.sleep
    get_disk_fn = _get_disk or get_resource_status
    elapsed = 0
    poll_count = 0
    while elapsed < timeout_seconds:
        try:
            _, stdout, _ = await ssh_client.run("ha core info --raw-json", check=False)
            data = json.loads(stdout)
            info = data.get("data", data)
            if not isinstance(info.get("version"), str):
                log.error("ha_core_info_schema_unexpected", keys=list(info.keys()))
            current = info.get("version", "")
            update_available = info.get("update_available", True)
            if current == target_version or not update_available:
                return True
        except Exception as exc:
            log.warning("poll_core_version_error", error=str(exc))
        poll_count += 1
        if poll_count % _DISK_CHECK_EVERY_N_POLLS == 0:
            disk_status = get_disk_fn()
            if (
                disk_status is not None
                and disk_status.disk_free_gb < HA_DISK_CRITICAL_GB
            ):
                log.warning(
                    "core_update_disk_critical_during_poll",
                    disk_free_gb=round(disk_status.disk_free_gb, 2),
                    elapsed=elapsed,
                )
        await sleep_fn(interval)
        elapsed += interval
    return False


async def _poll_os_online(
    ha_host: str,
    ha_port: int,
    timeout_seconds: int = _OS_UPDATE_TIMEOUT,
    interval: int = _POLL_INTERVAL_OS,
    _sleep: Optional[Callable] = None,
    _connect: Optional[Callable] = None,
) -> bool:
    """Poll TCP connect to ha_host:ha_port until success or timeout."""
    sleep_fn = _sleep or asyncio.sleep
    connect_fn = _connect or asyncio.open_connection
    elapsed = 0
    while elapsed < timeout_seconds:
        try:
            reader, writer = await connect_fn(ha_host, ha_port)
            writer.close()
            await writer.wait_closed()
            return True
        except (
            Exception
        ):  # nosec B110 — intentional: any connection error means HA not yet up
            pass
        await sleep_fn(interval)
        elapsed += interval
    return False


async def _wait_for_ha_down(
    ha_host: str,
    ha_port: int,
    timeout_seconds: int = 60,
    _sleep: Optional[Callable] = None,
    _connect: Optional[Callable] = None,
) -> bool:
    """Poll TCP until connection is refused (HA has started shutting down)."""
    sleep_fn = _sleep or asyncio.sleep
    connect_fn = _connect or asyncio.open_connection
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        try:
            reader, writer = await connect_fn(ha_host, ha_port)
            writer.close()
            await writer.wait_closed()
        except (ConnectionRefusedError, OSError):
            return True
        await sleep_fn(2)
    return False


async def _poll_ha_api_ready(
    ha_host: str,
    ha_port: int,
    token: str,
    timeout_seconds: int = 120,
    interval: int = 5,
    _sleep: Optional[Callable] = None,
    _get: Optional[Callable] = None,
) -> bool:
    """Poll GET /api/ until HA HTTP API returns 200 or 401 (API layer is up)."""
    sleep_fn = _sleep or asyncio.sleep
    url = f"http://{ha_host}:{ha_port}/api/"
    headers = {"Authorization": f"Bearer {token}"}
    elapsed = 0
    while elapsed < timeout_seconds:
        try:
            if _get is not None:
                status = await _get(url, headers)
            else:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(url, headers=headers)
                    status = resp.status_code
            if status in (200, 401):
                return True
        except Exception:  # nosec B110 — connection errors expected during boot
            pass
        await sleep_fn(interval)
        elapsed += interval
    return False


async def _poll_addon_version(
    slug: str,
    target_version: str,
    ssh_client: SSHClientProtocol,
    timeout_seconds: int = _ADDON_UPDATE_TIMEOUT,
    interval: int = _POLL_INTERVAL_ADDON,
    _sleep: Optional[Callable] = None,
) -> bool:
    """Poll `ha apps info <slug>` until version matches and state=started or timeout."""
    sleep_fn = _sleep or asyncio.sleep
    elapsed = 0
    while elapsed < timeout_seconds:
        try:
            _, stdout, _ = await ssh_client.run(
                f"ha apps info {slug} --raw-json", check=False
            )
            data = json.loads(stdout)
            info = data.get("data", data)
            version = info.get("version", "")
            state = info.get("state", "")
            if version == target_version and state == "started":
                return True
        except Exception as exc:
            log.warning("poll_addon_version_error", slug=slug, error=str(exc))
        await sleep_fn(interval)
        elapsed += interval
    return False


async def _poll_addon_update_via_rest(
    entity_id: str,
    ha_rest_client: HARestClientProtocol,
    timeout_seconds: int = _ADDON_UPDATE_TIMEOUT,
    interval: int = _POLL_INTERVAL_ADDON,
    _sleep: Optional[Callable] = None,
) -> bool:
    """Poll the HA REST update entity until state flips to 'off' (update applied)."""
    sleep_fn = _sleep or asyncio.sleep
    elapsed = 0
    while elapsed < timeout_seconds:
        try:
            entity = await ha_rest_client.get_state(entity_id)
            state = entity.get("state", "on")
            in_progress = bool(entity.get("attributes", {}).get("in_progress", False))
            if state == "off" and not in_progress:
                log.info("poll_addon_rest_success", entity_id=entity_id)
                return True
        except Exception as exc:
            log.warning("poll_addon_rest_error", entity_id=entity_id, error=str(exc))
        await sleep_fn(interval)
        elapsed += interval
    return False


async def _check_hacs_install_via_ssh(
    ssh_client: "SSHClientProtocol",
) -> bool:
    """Return True if ha resolution info shows a hacs/restart_required issue.

    HACS downloads complete in seconds but leave a restart_required repair
    instead of flipping the update entity to "off". This is the reliable
    install-success signal when the REST poll times out.
    """
    try:
        _, stdout, _ = await ssh_client.run(
            "ha resolution info --raw-json", check=False
        )
        data = json.loads(stdout.strip())
        issues = data.get("data", data).get("issues") or []
        return any(
            issue.get("domain") in {"hacs", "marketplace"}
            and "restart_required" in (issue.get("issue_id") or "")
            for issue in issues
        )
    except Exception as exc:
        log.warning("hacs_install_ssh_check_failed", error=str(exc))
        return False


async def _send_post_update_card(
    update: UpdateStatus,
    notifier: "NotifierProtocol",
    success: bool,
    config_check_output: str,
    log_triage_summary: str,
    notification_id: Optional[str] = None,
    self_check: Optional["PueoSelfCheckResult"] = None,
    advisor_regressions: Optional[list] = None,
) -> None:
    """Send an informational result card after an update attempt."""
    nid = notification_id or str(uuid.uuid4())
    if update.component == "restart":
        subject = "HA Core restarted" if success else "Restart failed — check HA UI"
        outcome = "restarted" if success else "did not come back online"
    elif update.component == "reboot":
        subject = "HA host rebooted" if success else "Reboot failed — check HA UI"
        outcome = "rebooted" if success else "did not come back online"
    else:
        outcome = "succeeded" if success else "timed out — check HA UI"
        subject = f"Update {outcome}: {update.component} → {update.latest_version}"
    body_lines = [
        f"Component: {update.component}",
        f"Version: {update.installed_version} → {update.latest_version}",
        f"Outcome: {outcome}",
    ]
    if config_check_output:
        body_lines.append(f"Config check: {config_check_output[:200]}")
    if log_triage_summary:
        body_lines.append(f"Log triage: {log_triage_summary}")
    if self_check is not None:
        sc_status = "OK" if self_check.all_commands_ok else "DEGRADED"
        body_lines.append(f"Pueo self-check: {sc_status}")
        if self_check.command_risks:
            body_lines.append(
                f"Command risks: {'; '.join(self_check.command_risks[:3])}"
            )
    if advisor_regressions:
        body_lines.append(
            f"Upgrade advisor regressions ({len(advisor_regressions)}): "
            + "; ".join(advisor_regressions[:3])
        )
    body = "\n".join(body_lines)
    payload: dict = {
        "notification_id": nid,
        "type": "update_result",
        "component": update.component,
        "installed_version": update.installed_version,
        "latest_version": update.latest_version,
        "success": success,
        "config_check_output": config_check_output,
        "log_triage_summary": log_triage_summary,
    }
    if self_check is not None:
        payload["self_check"] = {
            "core_check_ok": self_check.core_check_ok,
            "core_info_ok": self_check.core_info_ok,
            "apps_list_ok": self_check.apps_list_ok,
            "netalertx_info_ok": self_check.netalertx_info_ok,
            "backup_smoke_ok": self_check.backup_smoke_ok,
            "command_risks": self_check.command_risks,
            "all_commands_ok": self_check.all_commands_ok,
        }
    if advisor_regressions is not None:
        payload["advisor_regressions"] = advisor_regressions
    log.info(
        "post_update_card_sent",
        component=update.component,
        version=update.latest_version,
        success=success,
        notification_id=nid,
    )
    await notifier.send(subject, body, payload)


async def _send_update_command(
    ssh_client: SSHClientProtocol, command: str, kind: str
) -> str:
    """Run an update command and return stderr; treat a lost connection as non-fatal.

    Core and OS updates restart HA, which drops the SSH session.  Treating that as
    an error would mask successful updates.  Instead we log and fall through so the
    caller's version-poll loop decides success from HA's actual state.
    Backup and offload calls must remain outside this helper (safety invariant).
    """
    try:
        _, _, stderr = await ssh_client.run(command, check=False)
        return stderr
    except (asyncssh.Error, OSError, asyncio.TimeoutError) as exc:
        log.warning(
            "update_command_connection_lost",
            kind=kind,
            error=str(exc),
        )
        return ""


async def execute_core_update(
    update: UpdateStatus,
    ssh_client: SSHClientProtocol,
    notifier: "NotifierProtocol",
    gate: "AutonomyGate | FakeAutonomyGate",
    llm_client: Optional[LLMClientProtocol] = None,
    _poll: Optional[Callable] = None,
    disk_free_gb: Optional[float] = None,
    cache_dir: Optional[str] = None,
    ha_rest_client: Optional[HARestClientProtocol] = None,
) -> bool:
    """Execute Core update: backup → ha core update → poll → post-check → result card."""
    from .ha_agent_advanced import (
        execute_remote_backup,
        offload_backup_to_local,
        record_backup_slug,
    )

    log.info("core_update_start", version=update.latest_version)
    _emit_step("Creating pre-update backup…")
    backup_slug = await execute_remote_backup(ssh_client=ssh_client)
    record_backup_slug(backup_slug)
    await offload_backup_to_local(backup_slug, ssh_client=ssh_client)
    log.info("core_update_backup_complete", slug=backup_slug)

    _emit_step("Backup ready — sending Core update command…")
    update_stderr = await _send_update_command(
        ssh_client, "ha core update --no-progress", "core"
    )
    if update_stderr:
        log.warning("core_update_stderr", stderr=update_stderr[:200])

    _emit_step("Waiting for Core update to apply…")
    poll_fn = _poll or _poll_core_version
    success = await poll_fn(
        update.latest_version, ssh_client, timeout_seconds=_CORE_UPDATE_TIMEOUT
    )
    _update_completed_at = time.time() if success else None

    config_check_output = ""
    log_triage_summary = ""
    self_check: Optional[PueoSelfCheckResult] = None
    advisor_regressions: Optional[list] = None
    if success:
        _emit_step("Running post-update config check…")
        try:
            _, cc_out, cc_err = await ssh_client.run("ha core check", check=False)
            config_check_output = (cc_out or cc_err or "").strip()
        except Exception as exc:
            log.warning("post_update_config_check_failed", error=str(exc))

        _emit_step("Running post-update health checks…")
        try:
            _, log_out, _ = await ssh_client.run(
                "ha core logs --lines 100", check=False
            )
            if log_out:
                from .ha_log_monitor import analyze_log_line_with_ai

                lines = log_out.splitlines()
                evaluation, _ = await analyze_log_line_with_ai(lines, llm_client)
                log_triage_summary = evaluation.root_cause_summary
                log.info(
                    "post_update_log_triage",
                    actionable=evaluation.is_actionable,
                    confidence=evaluation.confidence_score,
                )
        except Exception as exc:
            log.warning("post_update_log_triage_failed", error=str(exc))

        try:
            self_check = await run_pueo_self_check(
                ssh_client,
                version=update.latest_version,
                disk_free_gb=disk_free_gb,
            )
        except Exception as exc:
            log.warning("post_update_self_check_failed", error=str(exc))

        if ha_rest_client is not None:
            _emit_step("Checking upgrade advisor post-upgrade report…")
            try:
                from utils.ha.upgrade_advisor import read_post_upgrade_report

                post_report = await read_post_upgrade_report(
                    ha_rest_client,
                    update_completed_at=_update_completed_at,
                )
                if post_report is not None:
                    advisor_regressions = post_report.regressions
                    log.info(
                        "post_update_advisor_report",
                        post_upgrade_status=post_report.post_upgrade_status,
                        regression_count=len(post_report.regressions),
                    )
                    if post_report.regressions:
                        log.warning(
                            "post_update_advisor_regressions",
                            count=len(post_report.regressions),
                        )
            except Exception as exc:
                log.warning("post_update_advisor_check_failed", error=str(exc))

        _emit_step("Core update complete")
    else:
        _emit_step("Core update timed out — check HA UI")

    await _send_post_update_card(
        update,
        notifier,
        success,
        config_check_output,
        log_triage_summary,
        self_check=self_check,
        advisor_regressions=advisor_regressions,
    )

    if not success:
        log.warning(
            "core_update_timed_out",
            version=update.latest_version,
            detail="Update may still be in progress — check HA UI",
        )
    return success


async def execute_os_update(
    update: UpdateStatus,
    ssh_client: SSHClientProtocol,
    notifier: "NotifierProtocol",
    gate: "AutonomyGate | FakeAutonomyGate",
    ha_host: Optional[str] = None,
    ha_port: Optional[int] = None,
    _poll: Optional[Callable] = None,
    _api_poll: Optional[Callable] = None,
) -> bool:
    """Execute OS update: backup → ha os update → TCP poll → API readiness → result card."""
    from .ha_agent_advanced import (
        execute_remote_backup,
        offload_backup_to_local,
        record_backup_slug,
    )

    log.info("os_update_start", version=update.latest_version)
    _emit_step("Creating pre-update backup…")
    backup_slug = await execute_remote_backup(ssh_client=ssh_client)
    record_backup_slug(backup_slug)
    await offload_backup_to_local(backup_slug, ssh_client=ssh_client)
    log.info("os_update_backup_complete", slug=backup_slug)

    _emit_step("Backup ready — sending OS update command…")
    await _send_update_command(ssh_client, "ha os update --no-progress", "os")

    host = ha_host or HA_HOST
    port = ha_port or HA_API_PORT

    _pause_loops_for_reboot()
    success = False
    try:
        _emit_step("OS update in progress — waiting for HA…")
        poll_fn = _poll or _poll_os_online
        success = await poll_fn(host, port, timeout_seconds=_OS_UPDATE_TIMEOUT)

        if success:
            api_fn = _api_poll or _poll_ha_api_ready
            success = await api_fn(host, port, HA_API_TOKEN, timeout_seconds=120)
            if not success:
                log.warning(
                    "os_update_api_not_ready",
                    detail="TCP up but HTTP API timed out",
                )
    finally:
        _resume_loops_after_reboot()

    if success:
        _emit_step("HA is back online")
        set_reboot_pending_after_update(True)
        set_post_update_pending("os", "reboot")
        asyncio.create_task(_post_update_repair_scan())
        _os_reboot_note = (
            "HA requires a reboot to complete this update. "
            "Pueo will reboot automatically once the repair notification appears "
            "— no further approval needed."
        )
        await _send_post_update_card(update, notifier, True, "", _os_reboot_note)
    else:
        _emit_step("OS update timed out — check HA UI")
        await _send_post_update_card(update, notifier, False, "", "")
        log.warning(
            "os_update_timed_out",
            version=update.latest_version,
            detail="HA did not come back online — check HA UI",
        )
    return success


async def execute_addon_update(
    update: UpdateStatus,
    ssh_client: SSHClientProtocol,
    notifier: "NotifierProtocol",
    gate: "AutonomyGate | FakeAutonomyGate",
    _poll: Optional[Callable] = None,
    ha_rest_client: Optional[HARestClientProtocol] = None,
) -> bool:
    """Execute add-on update: backup → update.install REST service → poll → result card."""
    global _ssh_transport_update_in_progress
    from .ha_agent_advanced import (
        execute_remote_backup,
        offload_backup_to_local,
        record_backup_slug,
    )

    # Detect transport add-on via entity_picture before touching SSH.
    _ep = getattr(update, "entity_picture", None) or ""
    _ep_match = _HASSIO_ADDON_ICON_RE.match(_ep)
    _is_transport = bool(_ep_match and _ep_match.group(1) in _SSH_TRANSPORT_SLUGS)
    # HACS integrations have a brands.home-assistant.io entity_picture; Supervisor
    # add-ons use /api/hassio/addons/<slug>/icon.  Only HACS integrations trigger a
    # Core restart — regular add-ons restart themselves.
    _is_hacs = not _ep.startswith("/api/hassio/addons/")

    if _is_transport:
        _ssh_transport_update_in_progress = True

    try:
        log.info(
            "addon_update_start", slug=update.component, version=update.latest_version
        )

        def _tl(level: str, msg: str, extra: dict) -> None:  # pragma: no cover
            """Write a timeline event; best-effort, never raises."""
            try:
                from utils.core.timeline import write_timeline_event

                write_timeline_event(level, "update_check", msg, extra)
            except Exception:  # nosec B110
                pass

        backup_slug = await execute_remote_backup(ssh_client=ssh_client)
        record_backup_slug(backup_slug)
        await offload_backup_to_local(backup_slug, ssh_client=ssh_client)
        log.info("addon_update_backup_complete", slug=backup_slug)
        await asyncio.to_thread(
            _tl,
            "INFO",
            f"{update.component}: backup created ({backup_slug})",
            {
                "component": update.component,
                "version": update.latest_version,
                "backup_slug": backup_slug,
                "step": "backup_created",
            },
        )
        await asyncio.to_thread(
            _tl,
            "INFO",
            f"{update.component}: install started → {update.latest_version}",
            {
                "component": update.component,
                "version": update.latest_version,
                "step": "install_started",
            },
        )

        rest: HARestClientProtocol
        if ha_rest_client is not None:
            rest = ha_rest_client
        else:
            from utils.ha.ha_rest_client import HARestClient

            rest = HARestClient(HA_HOST, HA_API_PORT, HA_API_TOKEN, timeout=30.0)

        try:
            await rest.call_service(
                "update", "install", {"entity_id": update.entity_id}
            )
            log.info(
                "addon_update_install_service_called",
                slug=update.component,
                entity_id=update.entity_id,
            )
        except httpx.TimeoutException:
            # HA holds the connection open while the update runs; a timeout here is expected
            # behaviour, not a failure. The update may already be in progress — proceed to
            # the poll, which will determine the actual outcome.
            log.warning(
                "addon_update_service_timeout",
                slug=update.component,
                entity_id=update.entity_id,
                detail="service call timed out; update may be in progress — proceeding to poll",
            )
        except Exception as exc:
            log.warning(
                "addon_update_service_failed",
                slug=update.component,
                error=str(exc)[:200] or repr(exc),
            )
            await asyncio.to_thread(
                _tl,
                "WARNING",
                f"{update.component}: update service call failed",
                {
                    "component": update.component,
                    "version": update.latest_version,
                    "step": "install_result",
                    "success": False,
                },
            )
            await _send_post_update_card(update, notifier, False, "", "")
            return False

        if _poll is not None:
            success = await _poll(
                update.component,
                update.latest_version,
                ssh_client,
                timeout_seconds=_ADDON_UPDATE_TIMEOUT,
            )
        else:
            success = await _poll_addon_update_via_rest(
                update.entity_id,
                rest,
                timeout_seconds=_ADDON_UPDATE_TIMEOUT,
            )

        # HACS-specific fallback: a successful HACS download produces a
        # hacs/restart_required_* repair within seconds instead of flipping the
        # update entity to "off". The REST poll always times out in this case;
        # check ha resolution info over SSH as the true install-success signal.
        if not success and "hacs" in update.entity_id.lower() and _poll is None:
            success = await _check_hacs_install_via_ssh(ssh_client)
            if success:
                log.info(
                    "hacs_install_success_via_ssh_fallback",
                    slug=update.component,
                    version=update.latest_version,
                )

        if success:
            _restart_note = ""
            if _is_hacs:
                set_restart_pending_after_update(True)
                set_post_update_pending(update.component, "restart")
                asyncio.create_task(_post_update_repair_scan())
                log.info("addon_update_restart_pending_flag_set", slug=update.component)
                _restart_note = (
                    "HA may require a Core restart to complete this update. "
                    "If so, Pueo will restart automatically — no further approval needed."
                )
            await asyncio.to_thread(
                _tl,
                "INFO",
                f"{update.component}: updated to {update.latest_version} ✓",
                {
                    "component": update.component,
                    "version": update.latest_version,
                    "step": "install_result",
                    "success": True,
                },
            )
            await _send_post_update_card(update, notifier, True, "", _restart_note)
        else:
            await asyncio.to_thread(
                _tl,
                "WARNING",
                f"{update.component}: update timed out — check HA UI",
                {
                    "component": update.component,
                    "version": update.latest_version,
                    "step": "install_result",
                    "success": False,
                },
            )
            await _send_post_update_card(update, notifier, False, "", "")
            log.warning(
                "addon_update_timed_out",
                slug=update.component,
                version=update.latest_version,
                detail="Add-on may still be updating — check HA UI",
            )
        return success
    finally:
        if _is_transport:
            _ssh_transport_update_in_progress = False


_UPDATE_PRIORITY: dict[str, int] = {"os": 0, "supervisor": 1, "core": 2}


def _update_priority(component: str) -> int:
    return _UPDATE_PRIORITY.get(component, 3)


def _pending_higher_priority_components(component: str, watch_dir: Path) -> list[str]:
    """Return component names of PENDING update cards with higher priority than ``component``."""
    this_priority = _update_priority(component)
    higher: list[str] = []
    for json_path in watch_dir.glob("*.json"):
        stem = json_path.stem
        if any(
            (watch_dir / f"{stem}.{s}").exists()
            for s in ("approved", "rejected", "deferred", "in_progress")
        ):
            continue
        try:
            other_payload = (
                __import__("json").loads(json_path.read_text()).get("payload", {})
            )
        except Exception:  # nosec B112
            continue
        from utils.hitl.card_types import CARD_TYPE_UPDATE

        if other_payload.get("card_type") != CARD_TYPE_UPDATE:
            continue
        other_component = other_payload.get("component", "")
        if _update_priority(other_component) < this_priority:
            higher.append(other_component)
    higher.sort(key=_update_priority)
    return higher


async def execute_ha_reboot(
    ssh_client: SSHClientProtocol,
    notifier: "NotifierProtocol",
    ha_host: Optional[str] = None,
    ha_port: Optional[int] = None,
    _poll: Optional[Callable] = None,
    _api_poll: Optional[Callable] = None,
    _down_poll: Optional[Callable] = None,
    skip_backup: bool = False,
) -> bool:
    """Reboot HA host: (backup) → reboot → wait for down → TCP poll → API ready → card.

    When skip_backup=True the pre-reboot backup is skipped because a backup was
    already taken immediately before the triggering OS/Supervisor update.
    """
    if skip_backup:
        log.info("ha_reboot_start_skip_backup")
        _emit_step(
            "Using existing update backup — skipping redundant backup",
            activity="reboot_execution",
        )
    else:
        from .ha_agent_advanced import (
            execute_remote_backup,
            offload_backup_to_local,
            record_backup_slug,
        )

        log.info("ha_reboot_start")
        _emit_step("Creating pre-reboot backup…", activity="reboot_execution")
        backup_slug = await execute_remote_backup(ssh_client=ssh_client)
        record_backup_slug(backup_slug)
        await offload_backup_to_local(backup_slug, ssh_client=ssh_client)
        log.info("ha_reboot_backup_complete", slug=backup_slug)

    _emit_step("Sending reboot command to HA…", activity="reboot_execution")
    try:
        import config as _rcfg
        from utils.ha.device_availability import record_pueo_action

        await asyncio.to_thread(
            record_pueo_action, _rcfg.DB_PATH, "host_reboot", "ha host reboot"
        )
    except Exception:  # nosec B110
        pass
    await ssh_client.run("ha host reboot", check=False)

    host = ha_host or HA_HOST
    port = ha_port or HA_API_PORT

    _pause_loops_for_reboot()
    success = False
    try:
        # Wait 10 s for ha-supervisor to begin shutdown, then confirm TCP drops.
        _emit_step("Waiting for HA to shut down…", activity="reboot_execution")
        await asyncio.sleep(10)
        down_fn = _down_poll or _wait_for_ha_down
        await down_fn(host, port, timeout_seconds=60)

        # Wait for TCP to accept connections again.
        _emit_step("HA is down — waiting for restart…", activity="reboot_execution")
        poll_fn = _poll or _poll_os_online
        success = await poll_fn(host, port, timeout_seconds=_OS_UPDATE_TIMEOUT)

        # Confirm the HTTP API layer is actually ready (TCP open ≠ API ready).
        if success:
            _emit_step("Waiting for HA API to be ready…", activity="reboot_execution")
            api_fn = _api_poll or _poll_ha_api_ready
            success = await api_fn(host, port, HA_API_TOKEN, timeout_seconds=120)
            if not success:
                log.warning(
                    "ha_reboot_api_not_ready", detail="TCP up but HTTP API timed out"
                )
    finally:
        _resume_loops_after_reboot()

    reboot_update = UpdateStatus(
        component="reboot",
        entity_id="",
        installed_version="",
        latest_version="rebooted",
        update_available=False,
        release_url=None,
        release_summary=None,
        in_progress=False,
    )
    if success:
        _emit_step("HA is online", activity="reboot_execution")
    else:
        _emit_step(
            "HA did not come back online — check manually",
            activity="reboot_execution",
        )
    await _send_post_update_card(reboot_update, notifier, success, "", "")

    if not success:
        log.warning("ha_reboot_timed_out", detail="HA did not come back online")
    return success


async def execute_ha_core_restart(
    ssh_client: SSHClientProtocol,
    notifier: Optional["NotifierProtocol"],
    _api_poll: Optional[Callable] = None,
    _down_poll: Optional[Callable] = None,
    skip_backup: bool = False,
) -> bool:
    """Restart HA Core: (backup) → ha core restart → wait for down → API ready → card.

    When skip_backup=True the pre-restart backup is skipped because a backup was
    already taken immediately before the triggering integration update.
    """
    if skip_backup:
        log.info("ha_core_restart_start_skip_backup")
        _emit_step(
            "Using existing update backup — skipping redundant backup",
            activity="restart_execution",
        )
    else:
        from .ha_agent_advanced import (
            execute_remote_backup,
            offload_backup_to_local,
            record_backup_slug,
        )

        log.info("ha_core_restart_start")
        _emit_step("Creating pre-restart backup…", activity="restart_execution")
        backup_slug = await execute_remote_backup(ssh_client=ssh_client)
        record_backup_slug(backup_slug)
        await offload_backup_to_local(backup_slug, ssh_client=ssh_client)
        log.info("ha_core_restart_backup_complete", slug=backup_slug)

    _emit_step("Sending restart command to HA Core…", activity="restart_execution")
    try:
        import config as _rcfg
        from utils.ha.device_availability import record_pueo_action

        await asyncio.to_thread(
            record_pueo_action, _rcfg.DB_PATH, "core_restart", "ha core restart"
        )
    except Exception:  # nosec B110
        pass
    down_fn = _down_poll or _wait_for_ha_down

    # Pause loops and start the TCP down-watcher BEFORE the blocking SSH call.
    # `ha core restart` does not return until HA is back online, so anything
    # started after it always sees HA already up and misreports "never went down".
    _pause_loops_for_reboot()
    down_task: "asyncio.Task[bool]" = asyncio.create_task(
        down_fn(HA_HOST, HA_API_PORT, timeout_seconds=60)
    )

    success = False
    try:
        rc, _, _ = await ssh_client.run("ha core restart", check=False)
        # SSH exit 0 is direct evidence that HA went down and came back.
        ssh_exited_zero = rc == 0

        if not down_task.done():
            down_task.cancel()
            try:
                await down_task
            except asyncio.CancelledError:
                pass

        try:
            tcp_went_down = not down_task.cancelled() and bool(down_task.result())
        except Exception:
            tcp_went_down = False

        went_down = ssh_exited_zero or tcp_went_down

        _emit_step("Waiting for HA Core to restart…", activity="restart_execution")
        if not went_down:
            log.warning(
                "ha_core_restart_never_went_down",
                detail="ha core restart returned non-zero and TCP never dropped",
            )
        else:
            api_fn = _api_poll or _poll_ha_api_ready
            success = await api_fn(
                HA_HOST, HA_API_PORT, HA_API_TOKEN, timeout_seconds=120
            )
            if not success:
                log.warning(
                    "ha_core_restart_api_not_ready",
                    detail="HA Core did not respond within timeout",
                )
    finally:
        _resume_loops_after_reboot()

    restart_update = UpdateStatus(
        component="restart",
        entity_id="",
        installed_version="",
        latest_version="restarted",
        update_available=False,
        release_url=None,
        release_summary=None,
        in_progress=False,
    )
    if success:
        _emit_step("HA Core is online", activity="restart_execution")
    else:
        _emit_step(
            "HA Core did not come back online — check manually",
            activity="restart_execution",
        )
    if notifier is None:
        log.warning("ha_core_restart_no_notifier")
    else:
        await _send_post_update_card(restart_update, notifier, success, "", "")

    if not success:
        log.warning(
            "ha_core_restart_timed_out", detail="HA Core did not come back online"
        )
    return success


async def execute_update(
    update: UpdateStatus,
    ssh_client: SSHClientProtocol,
    notifier: "NotifierProtocol",
    gate: "AutonomyGate | FakeAutonomyGate",
    llm_client: Optional[LLMClientProtocol] = None,
    ha_rest_client: Optional[HARestClientProtocol] = None,
) -> bool:
    """Dispatch update execution by component type."""
    from .ha_agent_advanced import is_reboot_required_active

    if is_reboot_required_active():
        log.warning("update_blocked_reboot_required", component=update.component)
        await notifier.send(
            subject=f"Update blocked: {update.component}",
            body=(
                "HA requires a reboot before this update can proceed. "
                "Approve the reboot repair card first."
            ),
            payload={},
        )
        return False

    # Guard: prevent poll_for_updates from closing the card mid-execution.
    # Only register if the entity isn't already tracked (the auto-apply path manages its
    # own entry at a higher level and must not have it removed by our finally).
    _added_to_executing = update.entity_id not in _entities_executing_update
    if _added_to_executing:
        _entities_executing_update.add(update.entity_id)
    try:
        if update.component == "core":
            return await execute_core_update(
                update,
                ssh_client,
                notifier,
                gate,
                llm_client,
                ha_rest_client=ha_rest_client,
            )
        if update.component == "os":
            return await execute_os_update(update, ssh_client, notifier, gate)
        return await execute_addon_update(
            update, ssh_client, notifier, gate, ha_rest_client=ha_rest_client
        )
    finally:
        if _added_to_executing:
            _entities_executing_update.discard(update.entity_id)


async def reconcile_in_progress_updates(
    ssh_client: SSHClientProtocol,
    notifier: "NotifierProtocol",
    watch_dir: Optional[Path] = None,
) -> None:
    """On startup, resolve any *.in_progress update cards left from a previous run.

    For each in-progress Core update card, queries `ha core info` to determine
    whether the update succeeded. Sends a post-update result card and removes
    the marker so the card does not sit unresolved indefinitely.
    If SSH fails the marker is left in place and retried on next startup.
    """
    from utils.hitl.card_types import CARD_TYPE_UPDATE

    _watch_dir = watch_dir or Path(NOTIFY_WATCH_DIR)
    in_progress_files = list(_watch_dir.glob("*.in_progress"))
    if not in_progress_files:
        return

    for marker in in_progress_files:
        stem = marker.stem
        json_path = _watch_dir / f"{stem}.json"
        if not json_path.exists():
            log.warning("reconcile_update_json_missing", stem=stem)
            continue
        try:
            card_data = json.loads(json_path.read_text())
        except Exception as exc:
            log.warning("reconcile_update_json_parse_error", stem=stem, error=str(exc))
            continue

        payload = card_data.get("payload", {})
        if payload.get("card_type") != CARD_TYPE_UPDATE:
            continue
        component = payload.get("component", "")
        if component != "core":
            continue

        latest_version = payload.get("latest_version", "")
        installed_version = payload.get("installed_version", "")
        log.info(
            "reconcile_core_update_start",
            stem=stem,
            latest_version=latest_version,
        )

        try:
            _, stdout, _ = await ssh_client.run("ha core info --raw-json", check=False)
            data = json.loads(stdout)
            info = data.get("data", data)
            current_version = info.get("version", "")
        except Exception as exc:
            log.warning("reconcile_core_update_ssh_failed", stem=stem, error=str(exc))
            continue

        update = UpdateStatus(
            component="core",
            entity_id=payload.get("entity_id", ""),
            installed_version=installed_version,
            latest_version=latest_version,
            update_available=False,
            release_url=payload.get("release_url"),
            release_summary=payload.get("release_summary"),
            in_progress=False,
        )

        if current_version == latest_version:
            log.info(
                "core_update_reconciled_success",
                stem=stem,
                version=current_version,
            )
            await _send_post_update_card(update, notifier, True, "", "")
        else:
            log.warning(
                "core_update_reconciled_unknown",
                stem=stem,
                expected=latest_version,
                current=current_version,
            )
            await _send_post_update_card(
                update,
                notifier,
                False,
                f"Reconciliation: current version is {current_version or 'unknown'}",
                "",
            )

        marker.unlink(missing_ok=True)


def reconcile_stale_approved_cards(
    watch_dir: Optional[Path] = None,
) -> int:
    """Resolve suppression rows whose card file has an .approved sentinel but DB was not updated.

    Before mark_card_approved() was added to approve(), the /approve endpoint wrote the
    .approved sentinel file but never updated hitl_suppression. This left last_action=NULL,
    resolved_at=NULL, permanently blocking future cards for the same entity.
    Setting resolved_at lets the next poll treat the entity as a fresh occurrence.
    Returns the number of rows resolved.
    """
    from utils.hitl.hitl_tracker import mark_card_resolved, stable_nid

    _watch_dir = watch_dir or Path(NOTIFY_WATCH_DIR)
    resolved = 0
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT card_key FROM hitl_suppression
               WHERE card_key LIKE 'update:%'
                 AND send_count >= 1
                 AND last_action IS NULL
                 AND resolved_at IS NULL""",
        ).fetchall()
        for (card_key,) in rows:
            nid = stable_nid(card_key)
            approved_path = _watch_dir / f"{nid}.approved"
            if approved_path.exists():
                mark_card_resolved(conn, card_key)
                log.warning(
                    "reconcile_stale_approved_card",
                    card_key=card_key,
                    reason="approved_sentinel",
                )
                resolved += 1
    return resolved
