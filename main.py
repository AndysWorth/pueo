#!/usr/bin/env python3
"""
Pueo entry point. Reads config.yaml and dispatches to the chosen agent mode.
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from interfaces import KnowledgeStoreClientProtocol


def run_rag_refresh(store: "KnowledgeStoreClientProtocol") -> None:
    import config
    from utils.knowledge.ha_blog_scraper import fetch_blog_release_notes
    from utils.knowledge.ha_docs_scraper import (
        discover_installed_integrations,
        embed_cached_integration_docs,
        fetch_integration_doc,
    )
    from utils.knowledge.ha_release_notes_scraper import (
        fetch_ha_release_notes,
        scrape_cached_release_notes,
    )
    from utils.knowledge.hacs_scraper import (
        discover_hacs_integrations,
        embed_cached_changelogs,
        fetch_hacs_changelog,
    )

    ha_url = f"http://{config.HA_HOST}:{config.HA_API_PORT}"
    ha_token = config.HA_API_TOKEN

    # ── 1. HA release notes ──────────────────────────────────────────────────
    print(
        f"[rag-refresh] Fetching HA release notes "
        f"(last {config.RAG_HA_VERSIONS_TO_FETCH} versions)…"
    )
    n_fetched_notes = fetch_ha_release_notes(
        config.HA_UPDATE_RELEASE_NOTES_CACHE_DIR, config.RAG_HA_VERSIONS_TO_FETCH
    )
    print(f"[rag-refresh]   → {n_fetched_notes} new version(s) downloaded")

    print("[rag-refresh] Fetching HA blog posts for stub releases…")
    n_blog = fetch_blog_release_notes(config.HA_UPDATE_RELEASE_NOTES_CACHE_DIR)
    print(f"[rag-refresh]   → {n_blog} stub(s) replaced with blog content")

    print("[rag-refresh] Embedding HA release notes…")
    ha_ids: set[str] = set()
    n_ha = scrape_cached_release_notes(
        config.HA_UPDATE_RELEASE_NOTES_CACHE_DIR, store, ha_ids
    )
    print(f"[rag-refresh]   → embedded {n_ha} version(s)")
    if ha_ids:
        store.prune("ha_release_notes", ha_ids)

    # ── 2. HACS changelogs ───────────────────────────────────────────────────
    print("[rag-refresh] Discovering HACS integrations from HA…")
    if ha_token:
        hacs_pairs = discover_hacs_integrations(ha_url, ha_token)
        slugs = [s for s, _ in hacs_pairs]
        slug_list = ", ".join(slugs) if slugs else "none found"
        print(f"[rag-refresh]   → {len(hacs_pairs)} integration(s): {slug_list}")
        for slug, repo in hacs_pairs:
            fetch_hacs_changelog(slug, repo, config.RAG_HACS_CACHE_DIR)
    else:
        print("[rag-refresh]   → skipped (no HA API token configured)")

    print("[rag-refresh] Embedding HACS changelogs…")
    hacs_ids: set[str] = set()
    n_hacs = embed_cached_changelogs(config.RAG_HACS_CACHE_DIR, store, hacs_ids)
    print(f"[rag-refresh]   → embedded {n_hacs} changelog(s)")
    if hacs_ids:
        store.prune("hacs_changelogs", hacs_ids)

    # ── 3. HA integration docs ───────────────────────────────────────────────
    print("[rag-refresh] Discovering installed HA integrations…")
    n_fetched_docs = n_cached_docs = n_missing_docs = 0
    if ha_token:
        domains = discover_installed_integrations(ha_url, ha_token)
        print(f"[rag-refresh]   → {len(domains)} domain(s) active")
        for domain in domains:
            result = fetch_integration_doc(domain, config.RAG_HA_DOCS_CACHE_DIR)
            if result == 1:
                n_fetched_docs += 1
            elif result == 0:
                n_cached_docs += 1
            else:
                n_missing_docs += 1
        print(
            f"[rag-refresh]   → {n_fetched_docs} new, "
            f"{n_cached_docs} cached, "
            f"{n_missing_docs} not on ha.io"
        )
    else:
        print("[rag-refresh]   → skipped (no HA API token configured)")

    print("[rag-refresh] Embedding HA integration docs…")
    docs_ids: set[str] = set()
    n_docs = embed_cached_integration_docs(
        config.RAG_HA_DOCS_CACHE_DIR, store, docs_ids
    )
    print(f"[rag-refresh]   → embedded {n_docs} doc(s)")
    if docs_ids:
        store.prune("ha_integration_docs", docs_ids)

    # ── 3.5. HA concept docs ────────────────────────────────────────────────
    print("[rag-refresh] Fetching HA concept docs (Lovelace, entities, automation…)…")
    from utils.knowledge.ha_concepts_scraper import (
        embed_cached_concept_docs,
        fetch_concept_docs,
    )

    n_fetched_concepts = fetch_concept_docs(config.HA_CONCEPTS_CACHE_DIR)
    print(f"[rag-refresh]   → {n_fetched_concepts} new concept doc(s) downloaded")

    print("[rag-refresh] Embedding HA concept docs…")
    concepts_ids: set[str] = set()
    n_concepts = embed_cached_concept_docs(
        config.HA_CONCEPTS_CACHE_DIR, store, concepts_ids
    )
    print(f"[rag-refresh]   → embedded {n_concepts} concept doc(s)")
    if concepts_ids:
        store.prune("ha_concepts", concepts_ids)

    # ── 4. Community cases ───────────────────────────────────────────────────
    n_cases = 0
    if config.FEDERATED_CASES_REPO:
        from utils.cases.case_ingester import CaseIngestError, ingest_community_cases

        print(
            f"[rag-refresh] Ingesting community cases from {config.FEDERATED_CASES_REPO}…"
        )
        community_scenarios_dir = str(
            Path(__file__).parent / "evals" / "scenarios" / "community"
        )
        try:
            n_cases = ingest_community_cases(
                config.FEDERATED_CASES_REPO,
                config.CASE_INGEST_CACHE_DIR,
                store,
                scenarios_dir=community_scenarios_dir,
            )
            print(f"[rag-refresh]   → {n_cases} new episode(s) ingested")
        except CaseIngestError as exc:
            print(f"[rag-refresh]   → case ingest failed: {exc}")
    else:
        print(
            "[rag-refresh] Community cases: skipped "
            "(set federated_cases_repo in config.yaml to enable)"
        )

    # ── 5. Strategy seeding ──────────────────────────────────────────────────
    print("[rag-refresh] Seeding strategy knowledge base from prompt files…")
    from utils.knowledge.strategy_seeder import seed_strategies

    n_strategies = seed_strategies(store)
    print(f"[rag-refresh]   → seeded {n_strategies} strategy document(s)")

    total = n_ha + n_hacs + n_docs + n_concepts + n_cases + n_strategies
    print(
        f"[rag-refresh] Done. Embedded content from {total} file(s) "
        f"across 6 collections."
    )


def _load_registered_tools(executor: "Any", db_path: str) -> None:
    """Load approved dynamic tools from registered_tools DB into the shared executor."""
    import importlib.util
    import sqlite3

    from utils.core.logging import get_logger

    log = get_logger("main")
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT name, code FROM registered_tools").fetchall()
    except Exception as exc:
        log.error("registered_tools_load_failed", error=str(exc))
        return

    from paths import get_dirs

    user_tools_dir = get_dirs().state_dir / "tools"
    for tool_name, _code in rows:
        tool_file = user_tools_dir / f"{tool_name}.py"
        if not tool_file.exists():
            continue
        try:
            module_name = f"pueo_tools.{tool_name}"
            spec = importlib.util.spec_from_file_location(module_name, tool_file)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            import sys

            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            fn = getattr(mod, "tool_implementation", None) or getattr(
                mod, tool_name, None
            )
            if fn is not None:
                executor.register_dynamic_tool(tool_name, fn)
                log.info("dynamic_tool_loaded", tool=tool_name)
        except Exception as exc:
            log.warning("dynamic_tool_load_failed", tool=tool_name, error=str(exc))


async def _embed_episodes_loop(db_path: str, knowledge_store: Any) -> None:
    """Embed unembedded successful repair episodes into ChromaDB every 10 minutes."""
    from utils.knowledge.knowledge_store import embed_local_episode
    from utils.repair.repair_episode import get_unembedded_successful_episodes

    while True:
        episodes = get_unembedded_successful_episodes(db_path)
        for ep in episodes:
            await embed_local_episode(ep, knowledge_store, db_path)
        await asyncio.sleep(600)


async def _rag_refresh_loop(knowledge_store: Any, interval_hours: int) -> None:
    """Periodically scrape and embed RAG content (release notes, HACS, HA docs).

    Runs immediately on startup when any bootstrap collection is empty so a fresh
    install populates ChromaDB without requiring a manual --mode rag-refresh invocation.
    Bootstrap collections are those run_rag_refresh always populates without a token
    (ha_release_notes, strategies). Token-gated collections (hacs_changelogs,
    ha_integration_docs) are excluded from the trigger — they may be legitimately empty
    and are covered by the weekly scheduled refresh.
    Subsequent runs fire every interval_hours (default 168 = weekly).
    """
    from utils.core.logging import get_logger as _gl
    from utils.core.timeline import write_timeline_event

    _log = _gl("main")

    # Bootstrap collections run_rag_refresh always populates (no token needed).
    # Token-gated collections are added when the token is available — on a fresh
    # install with a configured token they should not stay empty across restarts.
    import config as _cfg

    _BOOTSTRAP_COLLECTIONS: list[str] = ["ha_release_notes", "strategies"]
    if _cfg.HA_API_TOKEN:
        _BOOTSTRAP_COLLECTIONS += ["hacs_changelogs", "ha_integration_docs"]
    empty_bootstrap = [
        c for c in _BOOTSTRAP_COLLECTIONS if knowledge_store.collection_count(c) == 0
    ]
    if empty_bootstrap:
        _log.info(
            "rag_refresh_start", reason="collections_empty", empty=empty_bootstrap
        )
        write_timeline_event(
            "INFO",
            "rag_refresh",
            "RAG refresh started (bootstrap collections empty)",
            {"reason": "collections_empty", "empty": empty_bootstrap},
        )
        try:
            await asyncio.to_thread(run_rag_refresh, knowledge_store)
            _log.info("rag_refresh_done")
            write_timeline_event("INFO", "rag_refresh", "RAG refresh complete")
        except Exception as exc:  # pragma: no cover  # nosec B110
            _log.warning("rag_refresh_failed", error=str(exc))
            write_timeline_event(
                "WARN", "rag_refresh", f"RAG refresh failed: {exc}"
            )  # pragma: no cover

    _log.info("rag_refresh_loop_started", next_run_hours=interval_hours)
    write_timeline_event(
        "INFO",
        "rag_refresh",
        f"RAG refresh scheduled (next run in {interval_hours}h)",
    )

    while True:
        await asyncio.sleep(interval_hours * 3600)
        _log.info("rag_refresh_start", reason="scheduled")
        write_timeline_event(
            "INFO",
            "rag_refresh",
            "RAG refresh started (scheduled)",
            {"reason": "scheduled"},
        )
        try:
            await asyncio.to_thread(run_rag_refresh, knowledge_store)
            _log.info("rag_refresh_done")
            write_timeline_event("INFO", "rag_refresh", "RAG refresh complete")
        except Exception as exc:  # pragma: no cover  # nosec B110
            _log.warning("rag_refresh_failed", error=str(exc))
            write_timeline_event(
                "WARN", "rag_refresh", f"RAG refresh failed: {exc}"
            )  # pragma: no cover


async def _known_issues_poll_loop(
    db_path: str, reminder_days: int, notifier: Any
) -> None:
    """Hourly: fire one reminder card for each Known Issue older than reminder_days."""
    import sqlite3

    from utils.hitl.hitl_tracker import check_reminders_due, touch_reminder_sent
    from utils.core.logging import get_logger as _gl

    _log = _gl("main")
    while True:
        await asyncio.sleep(3600)
        try:
            with sqlite3.connect(db_path) as conn:
                due = check_reminders_due(conn, reminder_days)
            for issue in due:
                await notifier.send(
                    subject=f"Pueo: Known Issue reminder — {issue['description']}",
                    body=(
                        f"This issue has been suppressed for over {reminder_days} day(s).\n"
                        f"Type: {issue['card_type']}\n"
                        f"Rejected {issue['rejection_count']} time(s) before suppression.\n"
                        f"Resolve it from the Known Issues section in the approval queue."
                    ),
                    payload={
                        "card_type": "known_issue_reminder",
                        "card_key": issue["card_key"],
                        "original_card_type": issue["card_type"],
                        "description": issue["description"],
                    },
                )
                with sqlite3.connect(db_path) as conn:
                    touch_reminder_sent(conn, issue["card_key"])
        except Exception as exc:
            _log.warning("known_issues_poll_failed", error=str(exc))


async def supervisor_main(config_path: Path) -> None:
    """Start all monitoring loops and the dashboard in a single supervised asyncio process."""
    import config as cfg
    from agents import ha_agent_advanced
    import uvicorn
    from utils.hitl.notify import get_notifier
    from utils.disk.resource import ResourcePoller
    from utils.ha.ssh_client import AsyncSSHClient
    from utils.agent.supervisor import (
        LoopSupervisor,
        event_bus,
        set_supervisor_instance,
    )
    from web.dashboard import app as dashboard_app

    ha_agent_advanced.init_local_database()
    try:
        await ha_agent_advanced.reconcile_backup_inventory()
        await ha_agent_advanced.offload_pending_backups()
    except Exception as _e:  # nosec B110
        from utils.core.logging import get_logger as _gl

        _gl("main").warning("backup_startup_failed", error=str(_e))

    # Resolve any *.in_progress update cards left from a previous crashed run.
    try:
        from agents.ha_update_manager import reconcile_in_progress_updates
        from utils.hitl.notify import get_notifier as _get_notifier
        from utils.ha.ssh_client import AsyncSSHClient as _SSH

        _reconcile_ssh = _SSH(cfg.HA_HOST, cfg.HA_USER, cfg.SSH_KEY_PATH)
        _reconcile_notifier = _get_notifier(
            cfg.NOTIFIER, cfg.NOTIFY_URL, cfg.NOTIFY_WATCH_DIR
        )
        await reconcile_in_progress_updates(_reconcile_ssh, _reconcile_notifier)
    except Exception as _e:  # nosec B110
        from utils.core.logging import get_logger as _gl

        _gl("main").warning("update_reconcile_startup_failed", error=str(_e))

    # Detect local hardware and populate the model cache; optionally auto-select model
    try:
        from utils.disk.hardware import (
            detect_local_hardware,
            list_ollama_models,
            select_best_model,
        )
        import asyncio as _asyncio

        _profile = await _asyncio.to_thread(detect_local_hardware)
        _models = await _asyncio.to_thread(list_ollama_models)
        ha_agent_advanced.store_hardware_profile(
            chip=_profile.chip,
            arch=_profile.arch,
            ram_gb=_profile.ram_gb,
            cpu_cores=_profile.cpu_cores,
            detected_at=_profile.detected_at,
        )
        ha_agent_advanced.store_model_cache(
            [
                {
                    "name": m.name,
                    "size_gb": m.size_gb,
                    "has_tools": m.has_tools,
                    "context_length": m.context_length,
                    "last_seen_at": m.last_seen_at,
                }
                for m in _models
            ]
        )
        if cfg.OLLAMA_MODEL_AUTO:
            _best = await _asyncio.to_thread(select_best_model)
            if _best != cfg.OLLAMA_MODEL:
                from utils.core.logging import get_logger as _get_logger

                _get_logger("main").info(
                    "model_auto_switch",
                    previous=cfg.OLLAMA_MODEL,
                    selected=_best,
                )
                cfg.OLLAMA_MODEL = _best
    except Exception as exc:  # nosec B110 — hardware detection must not block startup
        from utils.core.logging import get_logger as _get_logger

        _get_logger("main").warning("model_auto_select_failed", error=str(exc))

    # Deferred after auto-select so ha_log_monitor.py captures the final OLLAMA_MODEL.
    from agents.ha_log_monitor import (
        poll_for_notifications,
        poll_for_repairs,
        poll_for_updates,
        tail_remote_log_stream,
    )

    notifier = get_notifier(cfg.NOTIFIER, cfg.NOTIFY_URL, cfg.NOTIFY_WATCH_DIR)
    supervisor = LoopSupervisor(bus=event_bus)
    set_supervisor_instance(supervisor)

    # Build shared ToolExecutor and attach to supervisor so the chat loop and
    # dashboard code_proposal handler share the same dynamic-tools registry.
    from utils.agent.autonomy import AutonomyGate
    from utils.agent.tool_executor import ToolExecutor

    knowledge_store = None
    try:
        from utils.knowledge.knowledge_store import ChromaKnowledgeStore
        from utils.core.logging import get_logger as _get_logger

        _ks_log = _get_logger("main")
        chroma_path = Path(cfg.CHROMADB_PATH)
        if chroma_path.exists():
            knowledge_store = ChromaKnowledgeStore(
                str(chroma_path), cfg.RAG_EMBED_MODEL, cfg.OLLAMA_ENDPOINT
            )
            _ks_log.info("knowledge_store_ready", path=str(chroma_path))
        else:
            _ks_log.warning(
                "knowledge_store_missing",
                path=str(chroma_path),
                hint="Run --mode rag-refresh to populate it",
            )
    except Exception as exc:  # pragma: no cover
        _get_logger("main").warning("knowledge_store_init_failed", error=str(exc))

    # Wire NAX clients at construction time so chat tools (INVESTIGATE_DEVICE,
    # RESTART_NETALERTX, etc.) receive the same clients as the automated pipeline.
    _nax_api = None
    _nax_ssh = None
    if cfg.NETALERTX_HOST:
        from netalertx.api_client import NetAlertXAPIClient

        _nax_api = NetAlertXAPIClient(
            f"http://{cfg.NETALERTX_HOST}:{cfg.NETALERTX_API_PORT}",
            cfg.NETALERTX_API_TOKEN,
        )
        _nax_ssh = AsyncSSHClient(
            cfg.NETALERTX_SSH_HOST, cfg.NETALERTX_SSH_USER, cfg.NETALERTX_SSH_KEY_PATH
        )

    _shared_executor = ToolExecutor(
        ha_ssh_client=AsyncSSHClient(cfg.HA_HOST, cfg.HA_USER, cfg.SSH_KEY_PATH),
        gate=AutonomyGate(cfg.AUTONOMY_LEVEL),
        notifier=notifier,
        db_path=cfg.DB_PATH,
        knowledge_store=knowledge_store,
        netalertx_api_client=_nax_api,
        nax_ssh_client=_nax_ssh,
        netalertx_container_name=cfg.NETALERTX_LOG_CONTAINER_NAME,
    )
    supervisor._tool_executor = _shared_executor
    _load_registered_tools(_shared_executor, cfg.DB_PATH)

    # Build the initial HA environment profile and register a periodic refresh loop.
    if cfg.HA_API_TOKEN:
        from utils.ha.ha_environment import (
            build_environment_profile,
            save_environment_profile,
        )
        from utils.ha.ha_ws_client import HAWebSocketClient

        _profile_ssh = AsyncSSHClient(cfg.HA_HOST, cfg.HA_USER, cfg.SSH_KEY_PATH)
        _profile_ws = HAWebSocketClient(cfg.HA_HOST, cfg.HA_API_PORT, cfg.HA_API_TOKEN)
        _shared_executor.set_ws_client(_profile_ws)

        async def _profile_refresh_loop() -> None:
            while True:
                try:
                    _p = await build_environment_profile(
                        ssh_client=_profile_ssh,
                        ws_client=_profile_ws,
                        ha_token=cfg.HA_API_TOKEN,
                        ha_url=f"http://{cfg.HA_HOST}:{cfg.HA_API_PORT}",
                        config_remote_path=cfg.CONFIG_REMOTE_PATH,
                    )
                    _shared_executor.set_ha_profile(_p)
                    save_environment_profile(_p, cfg.DB_PATH)
                except Exception as exc:  # pragma: no cover  # nosec B110
                    from utils.core.logging import get_logger as _gl

                    _gl("main").warning("ha_profile_refresh_failed", exc=str(exc))
                await asyncio.sleep(cfg.HA_PROFILE_REFRESH_HOURS * 3600)

        supervisor.start("profile_refresh", _profile_refresh_loop)

    # Periodic backup reconciliation and offload loop — keeps inventory in sync and
    # ensures new HA-side backups (including automatic daily ones) are pulled locally.
    async def _backup_reconcile_loop() -> None:
        from utils.core.logging import get_logger as _gl
        from utils.ha.ssh_client import AsyncSSHClient as _SSH

        _log = _gl("main")
        while True:
            await asyncio.sleep(
                30 * 60
            )  # 30-minute interval; startup reconcile+offload already ran
            ssh = _SSH(cfg.HA_HOST, cfg.HA_USER, cfg.SSH_KEY_PATH)
            try:
                await ha_agent_advanced.reconcile_backup_inventory(ssh_client=ssh)
            except Exception as e:  # pragma: no cover  # nosec B110
                _log.warning("backup_reconcile_loop_failed", error=str(e))
            try:
                await ha_agent_advanced.offload_pending_backups(ssh_client=ssh)
            except Exception as e:  # pragma: no cover  # nosec B110
                _log.warning("backup_offload_loop_failed", error=str(e))
            try:
                from pathlib import Path as _Path
                from utils.disk.archiver import enforce_archive_retention as _ear

                _ear(
                    _Path(cfg.PUEO_ARCHIVE_DIR),
                    int(cfg.PUEO_ARCHIVE_MAX_GB * 1_000_000_000),
                )
            except Exception as e:  # pragma: no cover  # nosec B110
                _log.warning("archive_retention_loop_failed", error=str(e))

    supervisor.start("backup_sync", _backup_reconcile_loop)

    # HA log monitor loop (SSH tail + AI triage)
    supervisor.start(
        "ha_log_monitor", lambda: tail_remote_log_stream(notifier=notifier)
    )

    # Resource polling loop — create a fresh poller on each supervisor restart.
    # Pass rest_client so the poller can call recorder.purge during disk-critical auto-recovery.
    _rest_client_for_poller = None
    if cfg.HA_API_TOKEN:
        from utils.ha.ha_rest_client import HARestClient

        _rest_client_for_poller = HARestClient(
            cfg.HA_HOST, cfg.HA_API_PORT, cfg.HA_API_TOKEN
        )

    from utils.llm.llm_factory import make_llm_client

    supervisor.start(
        "resource_poll",
        lambda: ResourcePoller(
            ssh_client=AsyncSSHClient(cfg.HA_HOST, cfg.HA_USER, cfg.SSH_KEY_PATH),
            notifier=notifier,
            interval_seconds=cfg.RESOURCE_POLL_INTERVAL_SECONDS,
            disk_warn_gb=cfg.HA_DISK_WARN_GB,
            disk_critical_gb=cfg.HA_DISK_CRITICAL_GB,
            mem_warn_mb=cfg.HA_MEM_WARN_MB,
            rest_client=_rest_client_for_poller,
            llm_client=make_llm_client(),
            knowledge_store=knowledge_store,
            db_path=cfg.DB_PATH,
        ).run(),
    )

    if knowledge_store is not None:
        supervisor.start(
            "embed_episodes",
            lambda: _embed_episodes_loop(cfg.DB_PATH, knowledge_store),
        )
        supervisor.start(
            "rag_refresh",
            lambda: _rag_refresh_loop(knowledge_store, cfg.RAG_REFRESH_INTERVAL_HOURS),
        )

    # Per-path disk usage polling loop
    from utils.disk.disk_usage import DiskUsagePoller

    supervisor.start(
        "disk_usage_poll",
        lambda: DiskUsagePoller(
            ssh_client=AsyncSSHClient(cfg.HA_HOST, cfg.HA_USER, cfg.SSH_KEY_PATH),
            interval_seconds=cfg.DISK_USAGE_POLL_INTERVAL_SECONDS,
        ).run(),
    )

    # Update check loop (only if interval > 0 and token is configured)
    if cfg.HA_UPDATE_CHECK_INTERVAL_HOURS > 0 and cfg.HA_API_TOKEN:
        supervisor.start(
            "update_check",
            lambda: poll_for_updates(
                notifier=notifier, knowledge_store=knowledge_store
            ),
        )

    # Notification polling loop (only if interval > 0 and token is configured)
    if cfg.HA_NOTIFICATION_POLL_INTERVAL_MINUTES > 0 and cfg.HA_API_TOKEN:
        supervisor.start(
            "notification_poll",
            lambda: poll_for_notifications(notifier=notifier),
        )

    # HA Repairs polling loop (only if interval > 0 and token is configured)
    if cfg.HA_REPAIR_POLL_INTERVAL_MINUTES > 0 and cfg.HA_API_TOKEN:
        supervisor.start(
            "repair_poll",
            lambda: poll_for_repairs(notifier=notifier),
        )

    # Lovelace dashboard entity health check (only if interval > 0 and token is configured)
    if cfg.HA_LOVELACE_CHECK_INTERVAL_MINUTES > 0 and cfg.HA_API_TOKEN:
        from agents.ha_lovelace_monitor import poll_for_dashboard_entity_issues

        supervisor.start(
            "lovelace_poll",
            lambda: poll_for_dashboard_entity_issues(notifier=notifier),
        )

    # Known Issues reminder loop — checks hourly for suppressed issues older than
    # KNOWN_ISSUE_REMINDER_DAYS and sends a one-shot reminder card for each.
    supervisor.start(
        "known_issues_poll",
        lambda: _known_issues_poll_loop(
            cfg.DB_PATH, cfg.KNOWN_ISSUE_REMINDER_DAYS, notifier
        ),
    )

    # NetAlertX log monitor (only if host is configured — non-empty means NetAlertX active)
    if cfg.NETALERTX_HOST:
        from netalertx.log_monitor import tail_netalertx_log_stream

        supervisor.start(
            "netalertx",
            lambda: tail_netalertx_log_stream(notifier=notifier),
        )

    # NetAlertX deferred setup: if the user requested setup and it isn't done yet,
    # run the installer as a supervised one-shot loop so approval cards reach the dashboard.
    # Route to the docker installer when deploy_target="docker"; HA installer otherwise.
    if cfg.NETALERTX_SETUP_DESIRED:
        import netalertx.installer as _nax_installer
        from utils.agent.autonomy import AutonomyGate

        _NETALERTX_DONE_STATES = {"FULLY_OPERATIONAL", "DOCKER_MACOS_UNSUPPORTED"}
        if _nax_installer.get_install_state(cfg.DB_PATH) not in _NETALERTX_DONE_STATES:
            _nax_gate = AutonomyGate(cfg.AUTONOMY_LEVEL)
            if cfg.NETALERTX_DEPLOY_TARGET == "docker":
                import netalertx.docker_installer as _nax_docker_installer

                supervisor.start(
                    "netalertx_setup",
                    lambda: _nax_docker_installer.main(
                        gate=_nax_gate, notifier=notifier
                    ),
                )
            else:
                supervisor.start(
                    "netalertx_setup",
                    lambda: _nax_installer.main(gate=_nax_gate, notifier=notifier),
                )

    # Register signal handlers for clean shutdown.
    # cancel_all() cancels asyncio tasks; server.should_exit stops uvicorn.
    # call_later forces an exit after 3 s in case SSH streams or Ollama
    # threads don't yield to the cancellation in time.
    loop = asyncio.get_running_loop()

    def _shutdown() -> None:
        # Mute uvicorn.error before cancellation so the expected CancelledErrors
        # from open SSE connections (listen_for_disconnect / lifespan) don't
        # flood the terminal.  They are normal shutdown noise, not real errors.
        logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
        supervisor.cancel_all()
        server.should_exit = True
        loop.call_later(3.0, sys.exit, 0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown)

    # Run uvicorn as an asyncio coroutine alongside the supervised loops
    uvi_config = uvicorn.Config(
        dashboard_app,
        host="127.0.0.1",
        port=cfg.DASHBOARD_PORT,
        log_level="warning",
    )
    server = uvicorn.Server(uvi_config)
    print(f"Pueo supervisor → dashboard at http://127.0.0.1:{cfg.DASHBOARD_PORT}")
    await server.serve()

    # serve() returned — cancel all loops on clean exit
    supervisor.cancel_all()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pueo — Home Assistant guardian agent\n\nRun with no arguments to start in supervisor mode (all loops + dashboard).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "commands (bin/pueo only — handled before Python starts):\n"
            "  start [--config FILE]   start supervisor in background; logs to pueo.log (default when no command given)\n"
            "  stop                    stop the running supervisor\n"
            "  status                  show whether Pueo is running, PID, and log path\n"
            "\n"
            "modes:\n"
            "  supervisor          all loops + dashboard in one process (default; same as no arguments)\n"
            "  monitor             live SSH log tail with AI triage (single-loop daemon)\n"
            "  diagnose            one-shot config fetch and analysis\n"
            "  advanced            diagnose + SQLite memory + backup triggering\n"
            "  repair              full sandbox-test-then-atomic-swap repair cycle\n"
            "  netalertx-setup        install and configure NetAlertX on HA\n"
            "  netalertx-uninstall    remove NetAlertX from HA and reset install state\n"
            "  netalertx-docker-setup install NetAlertX FA on a separate Docker host\n"
            "  netalertx-docker-uninstall remove NetAlertX Docker container and reset state\n"
            "  netalertx-switch       move NetAlertX between HA and Docker (reads deploy_target)\n"
            "  netalertx           monitor NetAlertX logs continuously\n"
            "  netalertx-diagnose  one-shot NetAlertX health check and optional heal\n"
            "  backup-status       print backup inventory table (slug, size, age, HA, Pueo)\n"
            "  update-check        one-shot update availability check (requires api_token)\n"
            "  notifications       one-shot: triage HA persistent notifications and send approval cards\n"
            "  rag-refresh         embed cached HA release notes and HACS changelogs into ChromaDB\n"
            "  dashboard           web dashboard for approving/rejecting pending actions\n"
            "  install-service     install Pueo as a macOS launchd service (auto-start at login)\n"
            "  start-service       load and enable the launchd service\n"
            "  stop-service        unload the launchd service (suppresses KeepAlive restart)\n"
            "  restart-service     stop the service; launchd KeepAlive restarts it immediately\n"
            "  audit               self-diagnostics: gap report saved to audits/\n"
            "  export-episodes     export anonymized repair episodes as YAML (use --since DATE)\n"
            "  migrate-paths       move existing data from checkout to platform directories\n"
            "                      (run after upgrading from a version before Phase 2)\n"
            "                      set PUEO_MIGRATE_DRY_RUN=1 to preview without moving\n"
        ),
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        metavar="FILE",
        help="path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--mode",
        choices=[
            "supervisor",
            "monitor",
            "diagnose",
            "advanced",
            "repair",
            "netalertx-setup",
            "netalertx-uninstall",
            "netalertx-docker-setup",
            "netalertx-docker-uninstall",
            "netalertx-switch",
            "netalertx",
            "netalertx-diagnose",
            "backup-status",
            "update-check",
            "notifications",
            "rag-refresh",
            "dashboard",
            "install-service",
            "start-service",
            "stop-service",
            "restart-service",
            "audit",
            "export-episodes",
            "migrate-paths",
        ],
        default="supervisor",
        help="agent mode (default: supervisor)",
    )
    parser.add_argument(
        "--since",
        metavar="DATE",
        default=None,
        help="ISO date (YYYY-MM-DD) — export episodes on or after this date (export-episodes mode)",
    )
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        sys.stderr.write(f"✘  Config file not found: {args.config}\n")
        sys.stderr.write("   Run ./setup.sh to create one.\n")
        sys.exit(1)

    # Must be set before importing agent modules so config.py picks up the right path
    os.environ["PUEO_CONFIG"] = str(config_path)

    from utils.core.logging import setup_logging

    setup_logging(
        console_text=(
            args.mode
            in (
                "netalertx-setup",
                "netalertx-uninstall",
                "netalertx-docker-setup",
                "netalertx-docker-uninstall",
                "netalertx-switch",
                "netalertx-diagnose",
            )
        )
    )

    if args.mode == "supervisor":
        asyncio.run(supervisor_main(config_path))
    elif args.mode == "monitor":
        from agents import ha_log_monitor

        asyncio.run(ha_log_monitor.main())
    elif args.mode == "diagnose":
        from agents import ha_agent_core

        asyncio.run(ha_agent_core.main())
    elif args.mode == "advanced":
        from agents import ha_agent_advanced

        asyncio.run(ha_agent_advanced.main())
    elif args.mode == "repair":
        from agents import ha_agent_sandbox_engine

        asyncio.run(ha_agent_sandbox_engine.main())
    elif args.mode == "netalertx-setup":
        from agents import ha_agent_advanced
        import netalertx.installer

        ha_agent_advanced.init_local_database()
        asyncio.run(netalertx.installer.main())
    elif args.mode == "netalertx-uninstall":
        from agents import ha_agent_advanced
        import netalertx.uninstaller

        ha_agent_advanced.init_local_database()
        asyncio.run(netalertx.uninstaller.main())
    elif args.mode == "netalertx-docker-setup":
        from agents import ha_agent_advanced
        import netalertx.docker_installer

        ha_agent_advanced.init_local_database()
        asyncio.run(netalertx.docker_installer.main())
    elif args.mode == "netalertx-docker-uninstall":
        from agents import ha_agent_advanced
        import netalertx.docker_uninstaller

        ha_agent_advanced.init_local_database()
        asyncio.run(netalertx.docker_uninstaller.main())
    elif args.mode == "netalertx-switch":
        from agents import ha_agent_advanced
        import netalertx.switch

        ha_agent_advanced.init_local_database()
        asyncio.run(netalertx.switch.main())
    elif args.mode == "netalertx":
        from agents import ha_agent_advanced
        import netalertx.log_monitor

        ha_agent_advanced.init_local_database()
        asyncio.run(netalertx.log_monitor.main())
    elif args.mode == "netalertx-diagnose":
        from agents import ha_agent_advanced
        import netalertx.one_shot_diagnose

        ha_agent_advanced.init_local_database()
        asyncio.run(netalertx.one_shot_diagnose.run_diagnose())
    elif args.mode == "backup-status":
        from agents import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.print_backup_status()
    elif args.mode == "update-check":
        from agents import ha_agent_advanced
        from agents import ha_update_manager

        ha_agent_advanced.init_local_database()
        asyncio.run(ha_update_manager.run_update_check())
    elif args.mode == "notifications":
        from agents import ha_agent_advanced
        from agents import ha_notification_manager

        ha_agent_advanced.init_local_database()
        asyncio.run(ha_notification_manager.run_notifications())
    elif args.mode == "rag-refresh":  # pragma: no cover
        import config
        from utils.knowledge.knowledge_store import ChromaKnowledgeStore

        store = ChromaKnowledgeStore(
            config.CHROMADB_PATH, config.RAG_EMBED_MODEL, config.OLLAMA_ENDPOINT
        )
        run_rag_refresh(store)
    elif args.mode == "dashboard":
        from web.dashboard import run_dashboard

        run_dashboard()
    elif args.mode == "install-service":  # pragma: no cover
        from utils.system.service import install_service

        install_service(
            pueo_dir=str(config_path.parent),
            python_path=sys.executable,
        )
        print("Pueo service installed → com.pueo.agent")
        print(f"Dashboard → http://127.0.0.1:{os.environ.get('DASHBOARD_PORT', 8080)}")
    elif args.mode == "start-service":  # pragma: no cover
        from utils.system.service import start_service

        start_service()
        print("Pueo service started → com.pueo.agent")
    elif args.mode == "stop-service":  # pragma: no cover
        from utils.system.service import stop_service

        stop_service()
        print("Pueo service stopped.")
    elif args.mode == "restart-service":  # pragma: no cover
        from utils.system.service import restart_service

        restart_service()
        print("Pueo service restarting (launchd KeepAlive will restart it).")
    elif args.mode == "audit":
        from agents import ha_agent_advanced
        from utils.system.audit import main_audit

        ha_agent_advanced.init_local_database()
        asyncio.run(main_audit())
    elif args.mode == "export-episodes":
        from agents import ha_agent_advanced
        from utils.repair.repair_episode import export_episodes_yaml, load_episodes

        ha_agent_advanced.init_local_database()
        since_ts: Optional[float] = None
        if args.since:
            from datetime import datetime

            try:
                since_ts = datetime.fromisoformat(args.since).timestamp()
            except ValueError:
                sys.stderr.write(f"✘  Invalid --since date: {args.since!r}\n")
                sys.stderr.write("   Expected ISO format, e.g. 2026-08-01\n")
                sys.exit(1)
        episodes = load_episodes(ha_agent_advanced.DB_PATH, since=since_ts)
        if not episodes:
            sys.stderr.write("No repair episodes found.\n")
            sys.exit(0)
        print(export_episodes_yaml(episodes), end="")
    elif args.mode == "migrate-paths":
        from utils.system.migrate_paths import run_migrate

        run_migrate(config_path)


if __name__ == "__main__":
    main()
