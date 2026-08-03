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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from interfaces import KnowledgeStoreClientProtocol


def run_rag_refresh(store: "KnowledgeStoreClientProtocol") -> None:
    import config
    from utils.ha_docs_scraper import (
        discover_installed_integrations,
        embed_cached_integration_docs,
        fetch_integration_doc,
    )
    from utils.ha_release_notes_scraper import (
        fetch_ha_release_notes,
        scrape_cached_release_notes,
    )
    from utils.hacs_scraper import (
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

    total = n_ha + n_hacs + n_docs
    print(
        f"[rag-refresh] Done. Embedded content from {total} file(s) "
        f"across 3 collections."
    )


def _load_registered_tools(executor: "Any", db_path: str) -> None:
    """Load approved dynamic tools from registered_tools DB into the shared executor."""
    import importlib.util
    import sqlite3

    from utils.logging import get_logger

    log = get_logger("main")
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT name, code FROM registered_tools").fetchall()
    except Exception:
        return

    user_tools_dir = Path(__file__).parent / "user_tools"
    for tool_name, _code in rows:
        tool_file = user_tools_dir / f"{tool_name}.py"
        if not tool_file.exists():
            continue
        try:
            module_name = f"user_tools.{tool_name}"
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
                log.info("dynamic_tool_loaded", name=tool_name)
        except Exception as exc:
            log.warning("dynamic_tool_load_failed", name=tool_name, error=str(exc))


async def supervisor_main(config_path: Path) -> None:
    """Start all monitoring loops and the dashboard in a single supervised asyncio process."""
    import config as cfg
    import ha_agent_advanced
    import uvicorn
    from ha_log_monitor import (
        poll_for_notifications,
        poll_for_updates,
        tail_remote_log_stream,
    )
    from utils.notify import get_notifier
    from utils.resource import ResourcePoller
    from utils.ssh_client import AsyncSSHClient
    from utils.supervisor import LoopSupervisor, event_bus, set_supervisor_instance
    from web.dashboard import app as dashboard_app

    ha_agent_advanced.init_local_database()
    try:
        await ha_agent_advanced.reconcile_backup_inventory()
        await ha_agent_advanced.offload_pending_backups()
    except Exception:  # nosec B110 — reconcile/offload failure must not block startup
        pass

    notifier = get_notifier(cfg.NOTIFIER, cfg.NOTIFY_URL, cfg.NOTIFY_WATCH_DIR)
    supervisor = LoopSupervisor(bus=event_bus)
    set_supervisor_instance(supervisor)

    # Build shared ToolExecutor and attach to supervisor so the chat loop and
    # dashboard code_proposal handler share the same dynamic-tools registry.
    from utils.autonomy import AutonomyGate
    from utils.tool_executor import ToolExecutor

    _shared_executor = ToolExecutor(
        ha_ssh_client=AsyncSSHClient(cfg.HA_HOST, cfg.HA_USER, cfg.SSH_KEY_PATH),
        gate=AutonomyGate(cfg.AUTONOMY_LEVEL),
        notifier=notifier,
        db_path=cfg.DB_PATH,
    )
    supervisor._tool_executor = _shared_executor
    _load_registered_tools(_shared_executor, cfg.DB_PATH)

    # HA log monitor loop (SSH tail + AI triage)
    supervisor.start(
        "ha_log_monitor", lambda: tail_remote_log_stream(notifier=notifier)
    )

    # Resource polling loop — create a fresh poller on each supervisor restart
    supervisor.start(
        "resource_poll",
        lambda: ResourcePoller(
            ssh_client=AsyncSSHClient(cfg.HA_HOST, cfg.HA_USER, cfg.SSH_KEY_PATH),
            notifier=notifier,
            interval_seconds=cfg.RESOURCE_POLL_INTERVAL_SECONDS,
            disk_warn_gb=cfg.HA_DISK_WARN_GB,
            disk_critical_gb=cfg.HA_DISK_CRITICAL_GB,
            mem_warn_mb=cfg.HA_MEM_WARN_MB,
        ).run(),
    )

    # Update check loop (only if interval > 0 and token is configured)
    if cfg.HA_UPDATE_CHECK_INTERVAL_HOURS > 0 and cfg.HA_API_TOKEN:
        supervisor.start("update_check", lambda: poll_for_updates(notifier=notifier))

    # Notification polling loop (only if interval > 0 and token is configured)
    if cfg.HA_NOTIFICATION_POLL_INTERVAL_MINUTES > 0 and cfg.HA_API_TOKEN:
        supervisor.start(
            "notification_poll",
            lambda: poll_for_notifications(notifier=notifier),
        )

    # NetAlertX log monitor (only if host is configured — non-empty means NetAlertX active)
    if cfg.NETALERTX_HOST:
        from netalertx.log_monitor import tail_netalertx_log_stream

        supervisor.start(
            "netalertx",
            lambda: tail_netalertx_log_stream(notifier=notifier),
        )

    # NetAlertX deferred setup: if the user requested setup and it isn't done yet,
    # run the installer as a supervised one-shot loop so HITL cards reach the dashboard.
    if cfg.NETALERTX_SETUP_DESIRED:
        import netalertx.installer as _nax_installer
        from utils.autonomy import AutonomyGate

        if _nax_installer.get_install_state(cfg.DB_PATH) != "FULLY_OPERATIONAL":
            _nax_gate = AutonomyGate(cfg.AUTONOMY_LEVEL)
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
        description="Pueo — Home Assistant guardian agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "modes:\n"
            "  supervisor          all loops + dashboard in one process (default)\n"
            "  monitor             live SSH log tail with AI triage (single-loop daemon)\n"
            "  diagnose            one-shot config fetch and analysis\n"
            "  advanced            diagnose + SQLite memory + backup triggering\n"
            "  repair              full sandbox-test-then-atomic-swap repair cycle\n"
            "  netalertx-setup     install and configure NetAlertX on HA\n"
            "  netalertx           monitor NetAlertX logs continuously\n"
            "  netalertx-diagnose  one-shot NetAlertX health check and optional heal\n"
            "  backup-status       print backup inventory table (slug, size, age, HA, Pueo)\n"
            "  update-check        one-shot update availability check (requires api_token)\n"
            "  notifications       one-shot: triage HA persistent notifications and send HITL cards\n"
            "  rag-refresh         embed cached HA release notes and HACS changelogs into ChromaDB\n"
            "  dashboard           HITL web dashboard for approving/rejecting pending actions\n"
            "  install-service     install Pueo as a macOS launchd service (auto-start at login)\n"
            "  start-service       load and enable the launchd service\n"
            "  stop-service        unload the launchd service (suppresses KeepAlive restart)\n"
            "  restart-service     stop the service; launchd KeepAlive restarts it immediately\n"
            "  audit               self-diagnostics: gap report saved to audits/\n"
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
        ],
        default="supervisor",
        help="agent mode (default: supervisor)",
    )
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        sys.stderr.write(f"✘  Config file not found: {args.config}\n")
        sys.stderr.write("   Run ./setup.sh to create one.\n")
        sys.exit(1)

    # Must be set before importing agent modules so config.py picks up the right path
    os.environ["PUEO_CONFIG"] = str(config_path)

    from utils.logging import setup_logging

    setup_logging(console_text=(args.mode in ("netalertx-setup", "netalertx-diagnose")))

    if args.mode == "supervisor":
        asyncio.run(supervisor_main(config_path))
    elif args.mode == "monitor":
        import ha_log_monitor

        asyncio.run(ha_log_monitor.main())
    elif args.mode == "diagnose":
        import ha_agent_core

        asyncio.run(ha_agent_core.main())
    elif args.mode == "advanced":
        import ha_agent_advanced

        asyncio.run(ha_agent_advanced.main())
    elif args.mode == "repair":
        import ha_agent_sandbox_engine

        asyncio.run(ha_agent_sandbox_engine.main())
    elif args.mode == "netalertx-setup":
        import ha_agent_advanced
        import netalertx.installer

        ha_agent_advanced.init_local_database()
        asyncio.run(netalertx.installer.main())
    elif args.mode == "netalertx":
        import ha_agent_advanced
        import netalertx.log_monitor

        ha_agent_advanced.init_local_database()
        asyncio.run(netalertx.log_monitor.main())
    elif args.mode == "netalertx-diagnose":
        import ha_agent_advanced
        import netalertx.one_shot_diagnose

        ha_agent_advanced.init_local_database()
        asyncio.run(netalertx.one_shot_diagnose.run_diagnose())
    elif args.mode == "backup-status":
        import ha_agent_advanced

        ha_agent_advanced.init_local_database()
        ha_agent_advanced.print_backup_status()
    elif args.mode == "update-check":
        import ha_agent_advanced
        import ha_update_manager

        ha_agent_advanced.init_local_database()
        asyncio.run(ha_update_manager.run_update_check())
    elif args.mode == "notifications":
        import ha_agent_advanced
        import ha_notification_manager

        ha_agent_advanced.init_local_database()
        asyncio.run(ha_notification_manager.run_notifications())
    elif args.mode == "rag-refresh":  # pragma: no cover
        import config
        from utils.knowledge_store import ChromaKnowledgeStore

        store = ChromaKnowledgeStore(
            config.CHROMADB_PATH, config.RAG_EMBED_MODEL, config.OLLAMA_ENDPOINT
        )
        run_rag_refresh(store)
    elif args.mode == "dashboard":
        from web.dashboard import run_dashboard

        run_dashboard()
    elif args.mode == "install-service":  # pragma: no cover
        from utils.service import install_service

        install_service(
            pueo_dir=str(config_path.parent),
            python_path=sys.executable,
        )
        print("Pueo service installed → com.pueo.agent")
        print(f"Dashboard → http://127.0.0.1:{os.environ.get('DASHBOARD_PORT', 8080)}")
    elif args.mode == "start-service":  # pragma: no cover
        from utils.service import start_service

        start_service()
        print("Pueo service started → com.pueo.agent")
    elif args.mode == "stop-service":  # pragma: no cover
        from utils.service import stop_service

        stop_service()
        print("Pueo service stopped.")
    elif args.mode == "restart-service":  # pragma: no cover
        from utils.service import restart_service

        restart_service()
        print("Pueo service restarting (launchd KeepAlive will restart it).")
    elif args.mode == "audit":
        import ha_agent_advanced
        from utils.audit import main_audit

        ha_agent_advanced.init_local_database()
        asyncio.run(main_audit())


if __name__ == "__main__":
    main()
