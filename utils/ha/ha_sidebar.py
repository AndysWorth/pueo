"""Helpers for installing the Pueo owl icon and Webpage dashboard in HA.

Pure helpers (png_from_nav_svg, decode_png_rgba, icon_path_from_rgba,
build_iconset_js, add_extra_module_url, detect_lan_ip,
webpage_dashboard_payload) are synchronous with no external dependencies.

install_ha_sidebar() is the async orchestrator that requires SSH + WS clients.
"""

from __future__ import annotations

import base64
import json
import re
import socket
import struct
import subprocess  # nosec B404
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from interfaces import SSHClientProtocol


_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def png_from_nav_svg(svg_text: str) -> bytes:
    """Extract the embedded base64 PNG from nav_32.svg's xlink:href attribute."""
    m = re.search(r'xlink:href="data:image/png;base64,([^"]+)"', svg_text)
    if not m:
        raise ValueError("No base64 PNG found in SVG xlink:href")
    return base64.b64decode(m.group(1))


def decode_png_rgba(png: bytes) -> list[bytearray]:
    """Decode an 8-bit RGBA non-interlaced PNG to a list of rows (each row is a bytearray of R,G,B,A bytes).

    Raises ValueError for any unsupported format.
    """
    if not png.startswith(_PNG_SIG):
        raise ValueError("Not a valid PNG file")

    # Parse chunks
    pos = 8
    chunks: dict[bytes, list[bytes]] = {}
    while pos < len(png):
        length = struct.unpack(">I", png[pos : pos + 4])[0]
        ctype = png[pos + 4 : pos + 8]
        data = png[pos + 8 : pos + 8 + length]
        pos += 12 + length
        chunks.setdefault(ctype, []).append(data)

    ihdr = chunks.get(b"IHDR", [None])[0]
    if ihdr is None:
        raise ValueError("Missing IHDR chunk")

    width, height = struct.unpack(">II", ihdr[:8])
    bit_depth, color_type, _, _, interlace = struct.unpack("BBBBB", ihdr[8:13])

    if bit_depth != 8:
        raise ValueError(f"Unsupported bit depth: {bit_depth}")
    if color_type != 6:
        raise ValueError(f"Unsupported color type: {color_type} (expected 6 = RGBA)")
    if interlace != 0:
        raise ValueError("Interlaced PNGs are not supported")

    raw = zlib.decompress(b"".join(chunks.get(b"IDAT", [])))

    bpp = 4  # bytes per pixel for RGBA
    stride = width * bpp
    rows: list[bytearray] = []
    prev = bytearray(stride)

    for y in range(height):
        row_start = y * (stride + 1)
        filt = raw[row_start]
        row = bytearray(raw[row_start + 1 : row_start + 1 + stride])

        if filt == 0:
            pass
        elif filt == 1:
            for x in range(bpp, stride):
                row[x] = (row[x] + row[x - bpp]) & 0xFF
        elif filt == 2:
            for x in range(stride):
                row[x] = (row[x] + prev[x]) & 0xFF
        elif filt == 3:
            for x in range(stride):
                a = row[x - bpp] if x >= bpp else 0
                row[x] = (row[x] + (a + prev[x]) // 2) & 0xFF
        elif filt == 4:
            for x in range(stride):
                a = row[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                row[x] = (row[x] + pr) & 0xFF
        else:
            raise ValueError(f"Unknown PNG filter type: {filt}")

        rows.append(row)
        prev = row

    return rows


def icon_path_from_rgba(
    rows: list[bytearray],
    alpha_min: int = 128,
    lum_max: int = 180,
) -> str:
    """Build a filled SVG path from RGBA pixel rows by run-length encoding dark opaque pixels."""
    parts: list[str] = []
    for y, row in enumerate(rows):
        width = len(row) // 4
        x = 0
        while x < width:
            r, g, b, a = row[x * 4], row[x * 4 + 1], row[x * 4 + 2], row[x * 4 + 3]
            if a >= alpha_min and (0.299 * r + 0.587 * g + 0.114 * b) <= lum_max:
                run_start = x
                while x < width:
                    r2, g2, b2, a2 = (
                        row[x * 4],
                        row[x * 4 + 1],
                        row[x * 4 + 2],
                        row[x * 4 + 3],
                    )
                    if (
                        a2 >= alpha_min
                        and (0.299 * r2 + 0.587 * g2 + 0.114 * b2) <= lum_max
                    ):
                        x += 1
                    else:
                        break
                run_len = x - run_start
                parts.append(f"M{run_start} {y}h{run_len}v1h-{run_len}Z")
            else:
                x += 1
    if not parts:
        raise ValueError("No opaque dark pixels found in icon")
    return " ".join(parts)


def build_iconset_js(path_d: str, viewbox: str = "0 0 32 32") -> str:
    """Build the JS module that registers window.customIconsets['pueo']."""
    escaped_path = json.dumps(path_d)
    escaped_viewbox = json.dumps(viewbox)
    return (
        "window.customIconsets = window.customIconsets || {};\n"
        'window.customIconsets["pueo"] = async (_name) => ({\n'
        f"  path: {escaped_path},\n"
        f"  viewBox: {escaped_viewbox},\n"
        "});\n"
    )


def add_extra_module_url(config_text: str, url: str) -> Optional[str]:
    """Add url to frontend.extra_module_url in HA configuration.yaml text.

    Returns updated text, or the same text if the URL is already present,
    or None if the frontend: block cannot be safely text-edited.
    """
    if url in config_text:
        return config_text

    lines = config_text.splitlines(keepends=True)

    # Locate the frontend: key
    frontend_line_idx: Optional[int] = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r"^frontend\s*:", stripped):
            rest = stripped[stripped.index(":") + 1 :].strip()
            if rest and not rest.startswith("#"):
                # frontend: !include or frontend: {…} — cannot edit
                return None
            frontend_line_idx = i
            break

    item_line = f"  - {url}\n"

    if frontend_line_idx is None:
        # Append a new frontend block
        if not config_text.endswith("\n"):
            config_text += "\n"
        config_text += f"\nfrontend:\n  extra_module_url:\n{item_line}"
        return config_text

    # Walk the frontend block (indented lines after frontend:)
    emu_line_idx: Optional[int] = None
    block_end = len(lines)
    for i in range(frontend_line_idx + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(lines[i]) - len(lines[i].lstrip())
        if indent == 0:
            # Back to top-level — end of frontend block
            block_end = i
            break
        if re.match(r"^\s*extra_module_url\s*:", lines[i]):
            emu_line_idx = i
            break

    if emu_line_idx is not None:
        # Insert a new list item after any existing items
        insert_at = emu_line_idx + 1
        while insert_at < block_end and re.match(r"^\s*-\s+", lines[insert_at]):
            insert_at += 1
        lines.insert(insert_at, item_line)
        return "".join(lines)

    # No extra_module_url key — insert it at block_end (before next top-level key)
    new_key = f"  extra_module_url:\n{item_line}"
    lines.insert(block_end, new_key)
    return "".join(lines)


def detect_lan_ip() -> Optional[str]:
    """Return the LAN IP of this machine, or None if it cannot be determined."""
    for iface in ("en0", "en1"):
        try:
            result = subprocess.run(  # nosec B603, B607
                ["ipconfig", "getifaddr", iface],
                capture_output=True,
                text=True,
                timeout=2,
            )
            ip = result.stdout.strip()
            if ip:
                return ip
        except Exception:  # nosec B110
            pass

    # UDP socket fallback — works on Linux too
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:  # nosec B110
        return None


def webpage_dashboard_payload(
    url: str,
    title: str = "Pueo",
    icon: str = "pueo:owl",
    url_path: str = "pueo-dashboard",
) -> dict:
    """Return the payload dict for creating a Webpage (iframe) Lovelace dashboard."""
    return {
        "title": title,
        "icon": icon,
        "url_path": url_path,
        "show_in_sidebar": True,
        "require_admin": False,
        "mode": "storage",
        "type": "iframe",
        "url": url,
    }


_CONFIG_REMOTE_PATH = "/config/configuration.yaml"
_ICONSET_REMOTE_PATH = "/config/www/pueo/pueo-icons.js"
_ICONSET_MODULE_URL = "/local/pueo/pueo-icons.js"
_DASHBOARD_URL_PATH = "pueo-dashboard"
_DASHBOARD_REGISTRY_REMOTE_PATH = "/config/.storage/lovelace_dashboards"
_DASHBOARD_CONFIG_REMOTE_PATH = "/config/.storage/lovelace.pueo-dashboard"
_DASHBOARD_ID = "lovelace_pueo_dashboard"


async def install_ha_sidebar(
    ssh: "SSHClientProtocol",
    dashboard_url: str,
    *,
    notifier: Any,
    svg_path: Optional[Path] = None,
) -> None:
    """Install the Pueo owl iconset and Webpage dashboard in HA.

    Steps:
    1. Derive the JS iconset from nav_32.svg and SFTP-write it to /config/www/pueo/.
    2. Edit /config/configuration.yaml to add frontend.extra_module_url.
       - If the URL is already present: skip backup/restart.
       - If add_extra_module_url returns None: print manual instructions; still
         proceed to create the dashboard.
       - Otherwise: backup → write → ha core check → (restore on failure) → restart.
    3. Write .storage/lovelace_dashboards and .storage/lovelace.pueo-dashboard.
       - If storage was changed and no restart was triggered in step 2: restart.
    """
    from agents.ha_agent_advanced import execute_remote_backup, record_backup_slug
    from agents.ha_update_manager import execute_ha_core_restart

    if svg_path is None:
        svg_path = Path(__file__).parent.parent.parent / "web" / "static" / "nav_32.svg"

    # 1. Build and upload the iconset JS.
    svg_text = svg_path.read_text()
    png_bytes = png_from_nav_svg(svg_text)
    rows = decode_png_rgba(png_bytes)
    path_d = icon_path_from_rgba(rows)
    js_content = build_iconset_js(path_d)

    await ssh.run("mkdir -p /config/www/pueo")
    await ssh.write_file(_ICONSET_REMOTE_PATH, js_content)
    print(f"✓  Iconset written to {_ICONSET_REMOTE_PATH}")

    # 2. Edit configuration.yaml.
    _config_restart_triggered = False
    config_text = await ssh.read_file(_CONFIG_REMOTE_PATH)
    new_config = add_extra_module_url(config_text, _ICONSET_MODULE_URL)

    if new_config is None:
        print(
            "\n⚠  Could not auto-edit configuration.yaml (frontend: block uses an unsupported form).\n"
            "   Add this manually, then restart HA Core:\n\n"
            "   frontend:\n"
            "     extra_module_url:\n"
            f"       - {_ICONSET_MODULE_URL}\n"
        )
    elif new_config == config_text:
        print(
            "✓  configuration.yaml already contains the module URL — no changes needed."
        )
    else:
        # Safety invariant: backup before any production write.
        backup_slug = await execute_remote_backup(ssh_client=ssh)
        record_backup_slug(backup_slug)

        # Write, sandbox-check, restore on failure.
        try:
            await ssh.write_file(_CONFIG_REMOTE_PATH, new_config)
            exit_code, _, stderr = await ssh.run("ha core check")
            if exit_code != 0:
                await ssh.write_file(_CONFIG_REMOTE_PATH, config_text)
                raise RuntimeError(
                    f"ha core check failed after adding extra_module_url "
                    f"(exit {exit_code}); original config restored.\n{stderr}"
                )
        except Exception:
            # Ensure restore on any unexpected error too.
            try:
                await ssh.write_file(_CONFIG_REMOTE_PATH, config_text)
            except Exception:  # nosec B110
                pass
            raise

        print("✓  configuration.yaml updated with extra_module_url.")
        await execute_ha_core_restart(
            ssh_client=ssh, notifier=notifier, skip_backup=True
        )
        _config_restart_triggered = True

    # 3. Write .storage/lovelace_dashboards and .storage/lovelace.pueo-dashboard.
    _storage_changed = False

    try:
        registry_text = await ssh.read_file(_DASHBOARD_REGISTRY_REMOTE_PATH)
        registry: dict = json.loads(registry_text)
    except (FileNotFoundError, json.JSONDecodeError):
        registry = {
            "version": 1,
            "minor_version": 1,
            "key": "lovelace_dashboards",
            "data": {"items": []},
        }

    items: list[dict] = registry.setdefault("data", {}).setdefault("items", [])
    existing_idx = next(
        (i for i, d in enumerate(items) if d.get("url_path") == _DASHBOARD_URL_PATH),
        None,
    )

    payload = webpage_dashboard_payload(dashboard_url)
    registry_entry: dict = {
        "id": _DASHBOARD_ID,
        "url_path": payload["url_path"],
        "title": payload["title"],
        "icon": payload["icon"],
        "show_in_sidebar": payload["show_in_sidebar"],
        "require_admin": payload["require_admin"],
        "mode": payload["mode"],
    }

    if existing_idx is None:
        items.append(registry_entry)
        _storage_changed = True
        print("✓  Webpage dashboard added to registry.")
    elif items[existing_idx] != registry_entry:
        items[existing_idx] = registry_entry
        _storage_changed = True
        print(f"✓  Webpage dashboard updated in registry.")
    else:
        print("✓  Webpage dashboard already up to date in registry.")

    dashboard_config_file: dict = {
        "version": 1,
        "minor_version": 1,
        "key": "lovelace.pueo-dashboard",
        "data": {"config": {"strategy": {"type": "iframe", "url": dashboard_url}}},
    }

    try:
        existing_cfg_text = await ssh.read_file(_DASHBOARD_CONFIG_REMOTE_PATH)
        existing_cfg: Any = json.loads(existing_cfg_text)
    except (FileNotFoundError, json.JSONDecodeError):
        existing_cfg = None

    if existing_cfg != dashboard_config_file:
        await ssh.write_file(
            _DASHBOARD_CONFIG_REMOTE_PATH,
            json.dumps(dashboard_config_file, indent=2),
        )
        _storage_changed = True
        print(f"✓  Dashboard config saved (url={dashboard_url}).")

    if _storage_changed:
        await ssh.write_file(
            _DASHBOARD_REGISTRY_REMOTE_PATH, json.dumps(registry, indent=2)
        )

    if _storage_changed and not _config_restart_triggered:
        await execute_ha_core_restart(
            ssh_client=ssh, notifier=notifier, skip_backup=True
        )
