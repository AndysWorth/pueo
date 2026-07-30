"""macOS launchd service management for the Pueo supervisor daemon."""

import subprocess
import sys
from pathlib import Path

PLIST_LABEL = "com.pueo.agent"
PLIST_TARGET = Path.home() / "Library/LaunchAgents/com.pueo.agent.plist"
TEMPLATE_PATH = Path(__file__).parent.parent / "deploy/pueo.launchd.plist.template"


def render_plist(pueo_dir: str, python_path: str) -> str:
    """Return the plist template with {{ PUEO_DIR }} and {{ PYTHON_PATH }} substituted."""
    content = TEMPLATE_PATH.read_text()
    content = content.replace("{{ PUEO_DIR }}", pueo_dir)
    content = content.replace("{{ PYTHON_PATH }}", python_path)
    return content


def service_status() -> dict:
    """Return {"loaded": bool, "running": bool, "pid": int | None}.

    On non-macOS hosts returns loaded=False with an "error" key — callers should
    handle this gracefully rather than raising.
    """
    if sys.platform != "darwin":
        return {"loaded": False, "running": False, "pid": None, "error": "macOS only"}

    try:
        result = subprocess.run(
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

    if pueo_dir is None:
        pueo_dir = str(TEMPLATE_PATH.parent.parent.resolve())
    if python_path is None:
        python_path = sys.executable

    rendered = render_plist(pueo_dir, python_path)
    PLIST_TARGET.parent.mkdir(parents=True, exist_ok=True)
    PLIST_TARGET.write_text(rendered)

    subprocess.run(
        ["launchctl", "load", "-w", str(PLIST_TARGET)],
        check=True,
    )


def restart_service() -> None:
    """Stop the service; KeepAlive causes launchd to restart it automatically."""
    if sys.platform != "darwin":  # pragma: no cover
        raise RuntimeError("launchd service management is macOS only")
    subprocess.run(["launchctl", "stop", PLIST_LABEL], check=True)


def uninstall_service() -> None:
    """Unload the launchd service and remove the plist file."""
    if sys.platform != "darwin":  # pragma: no cover
        raise RuntimeError("launchd service management is macOS only")
    if PLIST_TARGET.exists():
        subprocess.run(
            ["launchctl", "unload", "-w", str(PLIST_TARGET)],
            check=False,
        )
        PLIST_TARGET.unlink()
