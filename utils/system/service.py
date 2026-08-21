"""macOS launchd service management for the Pueo supervisor daemon."""

import subprocess  # nosec B404 — launchctl is a fixed macOS system binary, no user input
import sys
from pathlib import Path

from paths import PueoDirectories, get_dirs as _get_dirs

PLIST_LABEL = "com.pueo.agent"
PLIST_TARGET = Path.home() / "Library/LaunchAgents/com.pueo.agent.plist"
TEMPLATE_PATH = _get_dirs().resources_dir / "deploy/pueo.launchd.plist.template"


def render_plist(
    pueo_dir: str,
    python_path: str,
    dirs: PueoDirectories | None = None,
) -> str:
    """Return the plist template with all {{ }} placeholders substituted."""
    if dirs is None:
        dirs = _get_dirs()
    content = TEMPLATE_PATH.read_text()
    subs = {
        "{{ PUEO_DIR }}": pueo_dir,
        "{{ PYTHON_PATH }}": python_path,
        "{{ PUEO_CONFIG_DIR }}": str(dirs.config_dir),
        "{{ PUEO_DATA_DIR }}": str(dirs.data_dir),
        "{{ PUEO_STATE_DIR }}": str(dirs.state_dir),
        "{{ PUEO_CACHE_DIR }}": str(dirs.cache_dir),
        "{{ PUEO_LOG_DIR }}": str(dirs.log_dir),
    }
    for placeholder, value in subs.items():
        content = content.replace(placeholder, value)
    return content


def service_status() -> dict:
    """Return {"loaded": bool, "running": bool, "pid": int | None}.

    On non-macOS hosts returns loaded=False with an "error" key — callers should
    handle this gracefully rather than raising.
    """
    if sys.platform != "darwin":
        return {"loaded": False, "running": False, "pid": None, "error": "macOS only"}

    try:
        result = subprocess.run(  # nosec — launchctl is a fixed macOS system binary
            ["launchctl", "list", PLIST_LABEL],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {
            "loaded": False,
            "running": False,
            "pid": None,
            "error": "launchctl unavailable",
        }

    if result.returncode != 0:
        return {"loaded": False, "running": False, "pid": None}

    # launchctl list <label> prints a dict-style block; extract PID if present:
    # "PID" = 12345;
    pid = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith('"PID"'):
            parts = stripped.split("=", 1)
            if len(parts) == 2:
                try:
                    pid = int(parts[1].strip().rstrip(";").strip())
                except ValueError:
                    pass
            break

    return {"loaded": True, "running": pid is not None, "pid": pid}


def install_service(
    pueo_dir: str | None = None, python_path: str | None = None
) -> None:
    """Render the plist, write to ~/Library/LaunchAgents/, and load via launchctl."""
    if sys.platform != "darwin":  # pragma: no cover
        raise RuntimeError("launchd service management is macOS only")

    dirs = _get_dirs()
    dirs.log_dir.mkdir(parents=True, exist_ok=True)
    if pueo_dir is None:
        pueo_dir = str(dirs.resources_dir)
    if python_path is None:
        python_path = sys.executable

    rendered = render_plist(pueo_dir, python_path, dirs=dirs)
    PLIST_TARGET.parent.mkdir(parents=True, exist_ok=True)
    PLIST_TARGET.write_text(rendered)

    subprocess.run(  # nosec — launchctl is a fixed macOS system binary
        ["launchctl", "load", "-w", str(PLIST_TARGET)],
        check=True,
    )


def restart_service() -> None:
    """Stop the service; KeepAlive causes launchd to restart it automatically."""
    if sys.platform != "darwin":  # pragma: no cover
        raise RuntimeError("launchd service management is macOS only")
    subprocess.run(["launchctl", "stop", PLIST_LABEL], check=True)  # nosec


def stop_service() -> None:
    """Unload the service without removing the plist; suppresses KeepAlive restart."""
    if sys.platform != "darwin":  # pragma: no cover
        raise RuntimeError("launchd service management is macOS only")
    subprocess.run(  # nosec — launchctl is a fixed macOS system binary
        ["launchctl", "unload", "-w", str(PLIST_TARGET)],
        check=False,
    )


def start_service() -> None:
    """Load (re-enable) the service from the existing plist."""
    if sys.platform != "darwin":  # pragma: no cover
        raise RuntimeError("launchd service management is macOS only")
    subprocess.run(  # nosec — launchctl is a fixed macOS system binary
        ["launchctl", "load", "-w", str(PLIST_TARGET)],
        check=True,
    )


def uninstall_service() -> None:
    """Unload the launchd service and remove the plist file."""
    if sys.platform != "darwin":  # pragma: no cover
        raise RuntimeError("launchd service management is macOS only")
    if PLIST_TARGET.exists():
        subprocess.run(  # nosec — launchctl is a fixed macOS system binary
            ["launchctl", "unload", "-w", str(PLIST_TARGET)],
            check=False,
        )
        PLIST_TARGET.unlink()
