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
            "  monitor   live SSH log tail with AI triage (default, daemon mode)\n"
            "  diagnose  one-shot config fetch and analysis\n"
            "  advanced  diagnose + SQLite memory + backup triggering\n"
            "  repair    full sandbox-test-then-atomic-swap repair cycle\n"
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
        import netalertx.installer

        asyncio.run(netalertx.installer.main())
    elif args.mode == "netalertx":
        import netalertx.log_monitor

        asyncio.run(netalertx.log_monitor.main())


if __name__ == "__main__":
    main()
