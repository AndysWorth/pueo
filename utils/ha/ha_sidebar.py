"""Pure helpers for installing the Pueo owl icon and Webpage dashboard in HA.

All functions are synchronous and have no external dependencies beyond stdlib.
No HA connectivity or SSH is needed in this module.
"""

from __future__ import annotations

import base64
import json
import re
import socket
import struct
import subprocess
import zlib
from typing import Optional


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
            result = subprocess.run(
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
