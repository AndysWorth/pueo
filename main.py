#!/usr/bin/env python3
"""
Pueo entry point. Reads config.yaml and dispatches to the chosen agent mode.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pueo — Home Assistant guardian agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "modes:\n"
            "  monitor             live SSH log tail with AI triage (default, daemon mode)\n"
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
        ],
        default="monitor",
        help="agent mode (default: monitor)",
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

    if args.mode == "monitor":
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
        import ha_update_manager

        asyncio.run(ha_update_manager.run_update_check())
    elif args.mode == "notifications":
        import ha_agent_advanced
        import ha_notification_manager

        ha_agent_advanced.init_local_database()
        asyncio.run(ha_notification_manager.run_notifications())
    elif args.mode == "rag-refresh":
        import config
        from utils.ha_release_notes_scraper import scrape_cached_release_notes
        from utils.hacs_scraper import embed_cached_changelogs
        from utils.knowledge_store import ChromaKnowledgeStore

        store = ChromaKnowledgeStore(
            config.CHROMADB_PATH, config.RAG_EMBED_MODEL, config.OLLAMA_ENDPOINT
        )
        n_ha = scrape_cached_release_notes(
            config.HA_UPDATE_RELEASE_NOTES_CACHE_DIR, store
        )
        n_hacs = embed_cached_changelogs(".cache/hacs_changelogs/", store)
        print(
            f"rag-refresh: embedded {n_ha} HA release note file(s), {n_hacs} HACS changelog(s)"
        )
    elif args.mode == "dashboard":
        from web.dashboard import run_dashboard

        run_dashboard()


if __name__ == "__main__":
    main()
