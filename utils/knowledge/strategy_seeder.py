"""Seed the 'strategies' ChromaDB collection from Pueo's prompt files.

Each prompt file that contains a playbook or investigation methodology is
embedded as a runbook document. This ensures query_knowledge surfaces
relevant runbooks alongside breaking-change release notes and community cases.

Called once per RAG refresh cycle.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from interfaces import KnowledgeStoreClientProtocol
    from utils.ha.ha_environment import HAEnvironmentProfile


_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SIG_LINE_RE = re.compile(r"^signature:\s*(.+)$", re.MULTILINE)


def _parse_front_matter_signature(content: str) -> Optional[str]:
    """Return the ``signature:`` value from YAML front-matter, or None."""
    m = _FRONT_MATTER_RE.match(content)
    if not m:
        return None
    sm = _SIG_LINE_RE.search(m.group(1))
    return sm.group(1).strip() if sm else None


def _strip_front_matter(content: str) -> str:
    """Remove YAML front-matter block from content if present."""
    return _FRONT_MATTER_RE.sub("", content, count=1)


# Prompt files that used to be seeded but are other agents' system prompts, not
# runbooks. seed_strategies() purges their ids from Chroma and agent_strategies so
# they stop being retrieved (and credited) in unrelated sessions. run_rag_refresh
# also passes them to kb sync so pueo-kb copies are not re-ingested.
_RETIRED_SEED_PROMPTS: frozenset[str] = frozenset(
    {
        "diagnose_netalertx.md",
        "diagnose_installer.md",
        "triage_repair_issue.md",
        "investigation.md",
        "diagnose_config.md",
        # Folded into seed_notification_http_login.md (#945): two seeds prescribed
        # different procedures for the same symptom.
        "seed_security_notification.md",
    }
)

# Prompt files to seed as runbook documents.
# Each entry is (prompt_filename, title, trigger_pattern).
# Files with a YAML front-matter ``signature:`` line have their signature written into
# agent_strategies.signature so _fetch_signature_chunks can match them exactly.
_SEED_PROMPTS: list[tuple[str, str, str]] = [
    (
        "seed_integration_error.md",
        "Integration or entity error investigation",
        "integration failing, sensor unavailable, entity error, connection error, API outage",
    ),
    (
        "seed_disk_space.md",
        "HA disk space investigation",
        "disk space low, HA disk usage, backups taking too much space, recorder DB large",
    ),
    (
        "seed_pueo_log.md",
        "Pueo log investigation",
        "errors in Pueo itself, stream resets, loop crashes, agent loop failures",
    ),
    (
        "seed_config_error.md",
        "HA configuration error investigation",
        "HA config invalid, yaml error, ha core check failing, configuration.yaml problem",
    ),
    (
        "seed_log_analysis.md",
        "Time-range log analysis",
        "analyze log lines from time range, what happened in the log, log analysis HH:MM, sparkline click, log window",
    ),
    (
        "seed_lovelace_config.md",
        "Lovelace unregistered entity investigation",
        "lovelace entity not in registry, unregistered entity, dashboard entity has state but no unique_id, sub-platform entity, not-loaded config entry",
    ),
    (
        "seed_repair_issue.md",
        "HA repair issue investigation — method",
        "HA repair panel issue, unknown repair type, novel translation_key",
    ),
    (
        "seed_repair_restart_required.md",
        "restart_required repair — component restart needed",
        "restart_required repair issue, HACS restart_required, HA Core restart needed after update",
    ),
    (
        "seed_repair_reauth.md",
        "config_entry_reauth repair — integration re-authentication",
        "config_entry_reauth repair issue, OAuth token expired, API key revoked",
    ),
    (
        "seed_repair_reboot_required.md",
        "reboot_required repair — OS/Supervisor reboot needed",
        "reboot_required repair issue, OS update reboot, Supervisor reboot",
    ),
    (
        "seed_repair_integration_disabled.md",
        "integration_disabled repair — integration failed to load",
        "integration_disabled repair, setup_error repair, integration load failure",
    ),
    (
        "seed_repair_breaking_change.md",
        "breaking_change repair — deprecated configuration key",
        "breaking_change repair, deprecated config key, YAML migration required",
    ),
    (
        "seed_notification_analysis.md",
        "HA notification investigation — method",
        "HA persistent notification, invalid_config, integration disabled, failed login, unknown device",
    ),
    (
        "seed_notification_http_login.md",
        "http_login notification — failed login attempt",
        "http_login notification, ip-ban notification, failed login, suspicious login, unknown device login",
    ),
    (
        "seed_update_analysis.md",
        "HA update breaking-change analysis — method",
        "HA update available, breaking changes, deprecated config key, OS update, CLI change, release notes unavailable",
    ),
    (
        "seed_update_addon.md",
        "Add-on update analysis",
        "add-on update, app update, Mosquitto update, File editor update, addon patch, addon breaking change",
    ),
    (
        "seed_update_core.md",
        "HA Core update analysis",
        "Core update, Home Assistant update, HA core breaking change, core release notes, minor release update",
    ),
    (
        "seed_update_hacs.md",
        "HACS integration update analysis",
        "HACS update available, HACS integration update, custom integration update",
    ),
    (
        "seed_update_os.md",
        "HAOS OS update analysis",
        "HAOS update, Home Assistant OS update, OS update reboot, OS release notes, HAOS board notes, OS stub release notes",
    ),
    (
        "seed_update_supervisor.md",
        "Supervisor update analysis",
        "Supervisor update, Home Assistant Supervisor update, Supervisor restart, Supervisor API change, supervisor release notes",
    ),
    (
        "seed_supervisor_cli.md",
        "HA Supervisor CLI reference",
        "run_ha_command, ha apps, ha core, ha os, ha backups, ha supervisor, ha network, restart HA, list backups, check config, CLI command",
    ),
    (
        "seed_automation_failure.md",
        "Automation or script failure investigation",
        "automation didn't fire, automation not triggering, script failed, automation stopped, condition blocked, trigger not matching",
    ),
    (
        "seed_ai_agent_ha.md",
        "ai_agent_ha companion integration awareness",
        "ai_agent_ha installed, ai_agent_auto_ automation broken, phantom entity state, set_entity_state wrong value, LLM-created dashboard broken, automations.yaml.bak",
    ),
    (
        "seed_automation_authoring.md",
        "Automation authoring via propose_automation",
        "create automation, add automation, write automation, build automation, automate lights, automate when, turn on when, notify me when, schedule automation",
    ),
    (
        "seed_host_health.md",
        "Host health investigation — method",
        "swap pressure, memory low, eMMC disk wear, unexpected reboot, supervisor unhealthy, supervisor unsupported, resolution issue",
    ),
    (
        "seed_device_offline.md",
        "Device offline investigation",
        "device offline, device unavailable, econet unavailable, ecobee offline, MQTT disconnect, cloud push thread death, integration unavailable, TV offline, LIFX offline, Cast unavailable, media player unavailable",
    ),
    (
        "seed_device_post_restart.md",
        "Devices offline after a Home Assistant restart",
        "devices unavailable after restart, devices offline after update, many integrations offline at once, post-restart sweep, host reboot exposed devices, turned off devices",
    ),
    (
        "seed_host_rpi_firmware.md",
        "rpi_firmware_update_blocked — CM4 EEPROM read-only",
        "rpi_firmware_update_blocked, CM4 EEPROM update blocked, eeprom_update_unavailable",
    ),
    (
        "seed_host_no_backup.md",
        "no_current_backup — Supervisor requires full backup",
        "no_current_backup resolution issue, Supervisor backup requirement, full backup missing",
    ),
    (
        "seed_spook.md",
        "Spook repair digest — method",
        "Spook repair issues, dead_entity, missing_entity, Spook first load, many repairs at once",
    ),
    (
        "seed_netalertx.md",
        "NetAlertX investigation — method",
        "netalertx unreachable, netalertx API down, SCAN_SUBNETS drift, app.conf SyntaxError, NetAlertX restart, NetAlertX config, read_netalertx_conf, slug path, availability",
    ),
]

# Signature family (first signature segment) of seeds that have no ``signature:``
# but are written for one kind of session.  Mid-session query_knowledge hides them
# from other kinds (#993).  Unsigned seeds not listed here are general references
# shown to every session.
SEED_FAMILIES: dict[str, str] = {
    "seed_host_health.md": "host",
    "seed_disk_space.md": "host",
    "seed_lovelace_config.md": "lovelace",
    "seed_repair_issue.md": "repair",
    "seed_notification_analysis.md": "notification",
    "seed_update_analysis.md": "update",
    "seed_automation_authoring.md": "chat",
}


def strategy_family(metadata: dict, chunk_id: str = "") -> Optional[str]:
    """Return the signature family a strategies chunk belongs to, or None if general.

    Signed chunks (seeds and learned runbooks) use their signature's first segment;
    ``*`` there means any family, so it returns None.  Unsigned seeds use
    SEED_FAMILIES.
    """
    sig = str(metadata.get("signature") or "")
    if sig:
        family = sig.split(":", 1)[0]
        return None if family in ("", "*") else family
    filename = str(metadata.get("filename") or "")
    if not filename and chunk_id.startswith("seed:"):
        filename = chunk_id[len("seed:") :]
    return SEED_FAMILIES.get(filename)


# Tables whose ``signature`` column may hold a legacy signature.  Fixed list — the
# names are interpolated into SQL.
_SIGNATURE_TABLES: tuple[str, ...] = (
    "agent_strategies",
    "agent_memory",
    "runbook_usage",
    "session_outcomes",
)


def normalize_legacy_signatures(db_path: str) -> int:
    """Rewrite signatures listed in LEGACY_SIGNATURE_RENAMES to their current form.

    Idempotent; tables or columns that don't exist are skipped.  Returns rows changed.
    """
    from utils.knowledge.runbook_signature import LEGACY_SIGNATURE_RENAMES

    if not db_path:
        return 0
    changed = 0
    try:
        with sqlite3.connect(db_path) as conn:
            for table in _SIGNATURE_TABLES:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if "signature" not in cols:
                    continue
                for old, new in LEGACY_SIGNATURE_RENAMES.items():
                    cur = conn.execute(
                        f"UPDATE {table} SET signature=? WHERE signature=?",  # nosec B608
                        (new, old),
                    )
                    changed += cur.rowcount
            conn.commit()
    except Exception:  # nosec B110 — best-effort startup data fix
        pass
    return changed


def _stored_seed_approaches(db_path: str) -> dict[str, str]:
    """Return {seed id: approach} for seed rows already in agent_strategies."""
    if not db_path:
        return {}
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, approach FROM agent_strategies WHERE id LIKE 'seed:%'"
            ).fetchall()
        return {str(r[0]): str(r[1] or "") for r in rows}
    except Exception:  # nosec B110 — table may not exist yet
        return {}


def seed_strategies(
    store: "KnowledgeStoreClientProtocol",
    db_path: str = "",
    only_changed: bool = False,
) -> int:
    """Embed seed runbook documents into the 'strategies' collection.

    Uses the prompt file name as the document ID so repeated calls are
    idempotent (upsert semantics). Also writes seed entries to the
    agent_strategies SQLite table, refreshing approach/title/trigger_pattern
    on each run so stale text is updated. Returns the number of documents upserted.
    Retired seed ids (_RETIRED_SEED_PROMPTS) are purged from both stores.

    only_changed=True (startup sync) skips seeds whose file content matches the
    stored agent_strategies approach, so only new or edited seeds are re-embedded.
    """
    purge_retired_seeds(store, db_path)
    discard_trigger_keyed_candidates(store, db_path)
    stored = _stored_seed_approaches(db_path) if only_changed else {}
    prompts_dir = Path(__file__).parent.parent.parent / "prompts"
    n = 0
    for filename, title, trigger_pattern in _SEED_PROMPTS:
        path = prompts_dir / filename
        if not path.exists():
            continue
        content = path.read_text("utf-8")
        signature = _parse_front_matter_signature(content)
        body = _strip_front_matter(content)
        text = f"# {title}\n\nTrigger: {trigger_pattern}\n\n{body}"
        doc_id = f"seed:{filename}"
        if only_changed and stored.get(doc_id) == content:
            continue
        try:
            store.upsert(
                collection="strategies",
                ids=[doc_id],
                documents=[text],
                metadatas=[
                    {
                        "source": "seed_prompt",
                        "filename": filename,
                        "title": title,
                        "trigger_pattern": trigger_pattern,
                        **({"signature": signature} if signature else {}),
                    }
                ],
            )
            n += 1
        except Exception:  # nosec B110
            pass
        if db_path:
            try:
                with sqlite3.connect(db_path) as conn:
                    conn.execute(
                        "INSERT INTO agent_strategies"
                        " (id, title, trigger_pattern, approach, runbook_state,"
                        "  signature, created_at)"
                        " VALUES (?, ?, ?, ?, 'seed', ?, datetime('now'))"
                        " ON CONFLICT(id) DO UPDATE SET"
                        "  approach=excluded.approach,"
                        "  title=excluded.title,"
                        "  trigger_pattern=excluded.trigger_pattern,"
                        "  signature=excluded.signature",
                        (doc_id, title, trigger_pattern, content, signature),
                    )
                    conn.commit()
            except Exception:  # nosec B110
                pass
    return n


def purge_retired_seeds(
    store: "KnowledgeStoreClientProtocol",
    db_path: str = "",
) -> None:
    """Delete retired seed ids from the 'strategies' collection and agent_strategies.

    Idempotent; best-effort. runbook_usage rows are kept as history.
    """
    retired_ids = sorted(f"seed:{fn}" for fn in _RETIRED_SEED_PROMPTS)
    try:
        store.delete_ids("strategies", retired_ids)
    except Exception:  # nosec B110
        pass
    if db_path:
        try:
            with sqlite3.connect(db_path) as conn:
                conn.executemany(
                    "DELETE FROM agent_strategies WHERE id=?",
                    [(i,) for i in retired_ids],
                )
                conn.commit()
        except Exception:  # nosec B110
            pass


def discard_trigger_keyed_candidates(
    store: "KnowledgeStoreClientProtocol",
    db_path: str = "",
) -> int:
    """Mark candidates whose signature equals a loop trigger name as discarded.

    Signatures must contain at least one colon (e.g. ``repair:recorder:key``).
    Candidates whose ``signature`` has no colon were keyed by the trigger fallback
    (e.g. ``update_poll``), not a real symptom signature.  They cannot match any
    real session and should be discarded so they don't pollute exact-match lookups.

    Idempotent; best-effort.  Returns the count of rows discarded.
    """
    if not db_path:
        return 0
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id FROM agent_strategies"
                " WHERE runbook_state = 'candidate'"
                "   AND signature IS NOT NULL"
                "   AND signature NOT LIKE '%:%'",
            ).fetchall()
            ids = [r[0] for r in rows]
            if ids:
                conn.executemany(
                    "UPDATE agent_strategies SET runbook_state='discarded' WHERE id=?",
                    [(i,) for i in ids],
                )
                conn.commit()
        if ids:
            try:
                store.delete_ids("strategies", ids)
            except Exception:  # nosec B110
                pass
        return len(ids)
    except Exception:  # nosec B110
        return 0


def seed_home_profile(
    store: "KnowledgeStoreClientProtocol",
    env_profile: "Optional[HAEnvironmentProfile]" = None,
    db_path: str = "",
) -> int:
    """Generate and upsert the HA instance home profile into the strategies collection.

    Produces a single document describing the installed HA instance: versions,
    integrations, HACS components, and top-level config keys.  The document is
    stored with ``runbook_type=instance_profile`` and overwritten on each call
    (upsert semantics).  Returns 1 on success, 0 on failure.
    """
    import sqlite3

    prompts_dir = Path(__file__).parent.parent.parent / "prompts"
    template_path = prompts_dir / "seed_home_profile.md"
    preamble = template_path.read_text("utf-8") if template_path.exists() else ""

    lines: list[str] = [preamble.strip(), ""]
    if env_profile is not None:
        lines.append("## Current Installation")
        lines.append("")
        if env_profile.ha_version:
            lines.append(f"- **HA Core version:** {env_profile.ha_version}")
        if env_profile.os_version:
            lines.append(f"- **HA OS version:** {env_profile.os_version}")
        if env_profile.supervisor_version:
            lines.append(f"- **Supervisor version:** {env_profile.supervisor_version}")
        lines.append("")
        if env_profile.installed_integrations:
            count = len(env_profile.installed_integrations)
            domains = ", ".join(sorted(env_profile.installed_integrations))
            lines.append(f"## Installed Integrations ({count})")
            lines.append("")
            lines.append(domains)
            lines.append("")
        if env_profile.hacs_integrations:
            count = len(env_profile.hacs_integrations)
            slugs = ", ".join(sorted(env_profile.hacs_integrations))
            lines.append(f"## HACS Custom Components ({count})")
            lines.append("")
            lines.append(slugs)
            lines.append("")
        if env_profile.config_yaml_top_keys:
            keys = ", ".join(sorted(env_profile.config_yaml_top_keys))
            lines.append("## Top-Level configuration.yaml Keys")
            lines.append("")
            lines.append(keys)
            lines.append("")
    else:
        lines.append("*No live HA profile available at last RAG refresh.*")

    text = "\n".join(lines).strip()
    doc_id = "ha_instance_profile"
    title = "HA instance home profile"
    trigger = (
        "installed integrations, ha version, hacs, custom components, home profile"
    )
    try:
        store.upsert(
            collection="strategies",
            ids=[doc_id],
            documents=[text],
            metadatas=[
                {
                    "source": "home_profile",
                    "title": title,
                    "trigger_pattern": trigger,
                    "runbook_type": "instance_profile",
                }
            ],
        )
    except Exception:  # nosec B110
        return 0
    if db_path:
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO agent_strategies"
                    " (id, title, trigger_pattern, approach, runbook_state, created_at)"
                    " VALUES (?, ?, ?, ?, 'seed', datetime('now'))",
                    (doc_id, title, trigger, text),
                )
                conn.commit()
        except Exception:  # nosec B110
            pass
    return 1


def sync_strategy_to_chroma(
    strategy_id: str,
    db_path: str,
    store: "KnowledgeStoreClientProtocol",
) -> bool:
    """Upsert a strategy row's Chroma entry to reflect its current SQLite state.

    Reads the row from SQLite and upserts into the 'strategies' collection with
    updated 'runbook_type' metadata so labels and authority scores stay current.
    A signed row keeps its ``signature`` metadata, which family scoping reads.
    Returns True on success, False if the row was not found (caller should
    handle Chroma deletion separately if needed).
    """
    row = None
    if db_path:
        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT id, title, trigger_pattern, approach, runbook_state,"
                    " signature FROM agent_strategies WHERE id=?",
                    (strategy_id,),
                ).fetchone()
        except Exception:  # nosec B110
            pass
    if row is None:
        return False
    meta = {
        "source": "agent_strategies",
        "title": str(row["title"] or ""),
        "trigger_pattern": str(row["trigger_pattern"] or ""),
        "runbook_type": str(row["runbook_state"] or "candidate"),
    }
    if row["signature"]:
        meta["signature"] = str(row["signature"])
    try:
        store.upsert(
            collection="strategies",
            ids=[row["id"]],
            documents=[str(row["approach"] or "")],
            metadatas=[meta],
        )
        return True
    except Exception:  # nosec B110
        return False


def prune_strategies(
    store: "KnowledgeStoreClientProtocol",
    db_path: str,
    kb_manifest_ids: Optional[set[str]] = None,
) -> int:
    """Remove 'strategies' Chroma entries not backed by SQLite, local seeds, or the pueo-kb manifest.

    Returns the number of entries pruned.
    """
    keep: set[str] = set()

    # Local seed IDs (seed:{filename}) and the home profile
    for filename, _, _ in _SEED_PROMPTS:
        keep.add(f"seed:{filename}")
    keep.add("ha_instance_profile")

    # All current SQLite strategy IDs
    if db_path:
        try:
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute("SELECT id FROM agent_strategies").fetchall()
                keep.update(row[0] for row in rows)
        except Exception:  # nosec B110
            pass

    # All pueo-kb manifest IDs the caller already knows
    if kb_manifest_ids:
        for kb_id in kb_manifest_ids:
            keep.add(f"kb_{kb_id}")

    try:
        return store.prune("strategies", keep)
    except Exception:  # nosec B110
        return 0
