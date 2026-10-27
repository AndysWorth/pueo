"""NetAlertX API token reader — reads API_TOKEN from the add-on's app.db.

The HAOS SSH shell has no python3, so we SFTP-download the SQLite database
to a local temp directory and query it with the local sqlite3 module.
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from utils.core.logging import get_logger

if TYPE_CHECKING:
    from interfaces import SSHClientProtocol

log = get_logger("netalertx.token")


async def read_api_token(
    ssh_client: "SSHClientProtocol",
    slug: str,
    app_config_root: str = "",
) -> str:
    """Read API_TOKEN from the NetAlertX app.db Settings table via SFTP.

    Downloads app.db to a temp directory and queries it locally — the HAOS
    SSH shell has no python3 so remote querying is not available.

    Returns the token string or '' if not found or on any error.
    Never logs the token value.
    """
    if not slug:
        log.warning("netalertx_token_read_skipped", reason="no_addon_slug")
        return ""
    effective_root = app_config_root or "/app_configs"
    db_remote_path = f"{effective_root}/{slug}/db/app.db"

    with tempfile.TemporaryDirectory() as tmp_dir:
        local_db = os.path.join(tmp_dir, "app.db")
        try:
            await ssh_client.download_file(db_remote_path, local_db)
        except Exception as exc:
            log.warning(
                "netalertx_token_download_failed",
                remote_path=db_remote_path,
                exc_type=type(exc).__name__,
            )
            return ""

        try:
            token = await asyncio.to_thread(_query_token_from_db, local_db)
        except Exception as exc:
            log.warning(
                "netalertx_token_query_failed",
                exc_type=type(exc).__name__,
            )
            return ""

    if token:
        log.info("netalertx_token_read", source=db_remote_path, found=True)
    else:
        log.info("netalertx_token_read", source=db_remote_path, found=False)
    return token


def _query_token_from_db(local_db_path: str) -> str:
    """Synchronous helper: query Settings.API_TOKEN from a local SQLite file."""
    try:
        # immutable=1: NetAlertX's app.db is WAL-mode and we hold only the main
        # file (no -shm/-wal), which a plain mode=ro open cannot read (#977).
        with sqlite3.connect(f"file:{local_db_path}?immutable=1", uri=True) as conn:
            row = conn.execute(
                "SELECT setValue FROM Settings WHERE setKey='API_TOKEN'"
            ).fetchone()
        return (row[0] or "").strip() if row else ""
    except sqlite3.OperationalError:
        return ""


def persist_api_token(token: str) -> bool:
    """Write netalertx.api_token into config.yaml using a targeted replacement.

    Locates the netalertx: section and replaces the api_token line within it.
    Returns True when updated, False when config.yaml is absent or the key is
    not found under netalertx:.  Never logs the token value.
    """
    config_path = Path(
        os.environ.get(
            "PUEO_CONFIG",
            str(__import__("paths").get_dirs().config_dir / "config.yaml"),
        )
    )
    if not config_path.exists():
        return False

    text = config_path.read_text()
    lines = text.splitlines(keepends=True)
    result: list[str] = []
    in_netalertx = False
    replaced = False

    for line in lines:
        stripped = line.strip()

        # Enter the netalertx: section (top-level key, no leading spaces)
        if not line.startswith(" ") and stripped.startswith("netalertx:"):
            in_netalertx = True
            result.append(line)
            continue

        # Exit when we see another top-level non-comment key
        if (
            in_netalertx
            and not line.startswith(" ")
            and stripped
            and not stripped.startswith("#")
        ):
            in_netalertx = False

        if in_netalertx and not replaced and re.match(r"^\s+api_token\s*:", line):
            result.append(
                re.sub(r"(^\s*api_token\s*:\s*).*$", rf'\g<1>"{token}"', line)
            )
            replaced = True
        else:
            result.append(line)

    if not replaced:
        return False

    config_path.write_text("".join(result))
    return True


def on_token_confirmed(db_path: str, watch_dir: str | None = None) -> None:
    """Clear stale setup cards and promote OPERATIONAL_NO_API → FULLY_OPERATIONAL.

    Call whenever a valid NetAlertX API token has been confirmed working.
    Idempotent and never raises.
    """
    import uuid

    import config as _cfg
    from utils.hitl.hitl_tracker import resolve_card

    _watch: str = watch_dir or str(getattr(_cfg, "NOTIFY_WATCH_DIR", "hitl/"))

    for _nid in (
        "netalertx_api_token_required",
        "netalertx_setup_connection_failed",
        "netalertx_docker_setup_connection_failed",
    ):
        try:
            resolve_card(_watch, _nid)
        except Exception:  # nosec B110
            pass

    try:
        from netalertx.installer import _read_install_state, _write_install_state

        state, details = _read_install_state(db_path)
        if state == "OPERATIONAL_NO_API":
            _write_install_state(
                db_path, "FULLY_OPERATIONAL", details, str(uuid.uuid4())
            )
            log.info("netalertx_token_confirmed_promoted")
    except Exception:  # nosec B110
        pass

    log.info("netalertx_token_confirmed")
