"""Tests for utils/ha/ha_sidebar.py — pure helpers and install_ha_sidebar orchestrator."""

from __future__ import annotations

import asyncio
import base64
import struct
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.ha.ha_sidebar import (
    add_extra_module_url,
    build_iconset_js,
    decode_png_rgba,
    detect_lan_ip,
    icon_path_from_rgba,
    install_ha_sidebar,
    png_from_nav_svg,
    webpage_dashboard_payload,
)


# ---------------------------------------------------------------------------
# PNG builder helpers for crafted test cases
# ---------------------------------------------------------------------------


def _chunk(ctype: bytes, data: bytes) -> bytes:
    c = ctype + data
    return (
        struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    )


def _ihdr(width: int, height: int, bit_depth: int = 8, color_type: int = 6) -> bytes:
    return _chunk(
        b"IHDR",
        struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0),
    )


def _idat(rows: list[bytearray], width: int) -> bytes:
    raw = b""
    for row in rows:
        raw += bytes([0]) + bytes(row)  # filter 0 (None)
    return _chunk(b"IDAT", zlib.compress(raw))


def _idat_with_filter(filter_byte: int, row_data: bytearray) -> bytes:
    raw = bytes([filter_byte]) + bytes(row_data)
    return _chunk(b"IDAT", zlib.compress(raw))


_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_IEND = _chunk(b"IEND", b"")


def _build_png(width: int, height: int, rows: list[bytearray]) -> bytes:
    return _PNG_SIG + _ihdr(width, height) + _idat(rows, width) + _IEND


def _1x1_rgba(r: int, g: int, b: int, a: int) -> bytes:
    row = bytearray([r, g, b, a])
    return _build_png(1, 1, [row])


# ---------------------------------------------------------------------------
# png_from_nav_svg
# ---------------------------------------------------------------------------


NAV_32_SVG = (
    Path(__file__).parent.parent / "web" / "static" / "nav_32.svg"
).read_text()


class TestPngFromNavSvg:
    def test_extracts_png_bytes(self):
        data = png_from_nav_svg(NAV_32_SVG)
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(data) > 100

    def test_missing_href_raises(self):
        with pytest.raises(ValueError, match="No base64 PNG"):
            png_from_nav_svg("<svg></svg>")


# ---------------------------------------------------------------------------
# decode_png_rgba
# ---------------------------------------------------------------------------


class TestDecodePngRgba:
    def test_real_nav_svg_dimensions(self):
        png = png_from_nav_svg(NAV_32_SVG)
        rows = decode_png_rgba(png)
        assert len(rows) == 32
        assert all(len(row) == 32 * 4 for row in rows)

    def test_real_nav_svg_has_opaque_dark_pixel(self):
        png = png_from_nav_svg(NAV_32_SVG)
        rows = decode_png_rgba(png)
        found = False
        for row in rows:
            for i in range(32):
                r, g, b, a = row[i * 4], row[i * 4 + 1], row[i * 4 + 2], row[i * 4 + 3]
                if a >= 128 and (0.299 * r + 0.587 * g + 0.114 * b) <= 180:
                    found = True
                    break
            if found:
                break
        assert found, "Expected at least one opaque dark pixel in nav_32.svg"

    def test_filter_0_none(self):
        # filter 0: bytes pass through unchanged
        row = bytearray([10, 20, 30, 255])
        png = _1x1_rgba(10, 20, 30, 255)
        rows = decode_png_rgba(png)
        assert rows[0] == row

    def test_filter_1_sub(self):
        # 2x1 RGBA: row=[100,100,100,255, 10,10,10,255] with filter 1 (Sub)
        # raw stored: [110, 110, 110, 255+255&FF=254…] no, let's just store what filter 1 decodes to:
        # decoded[x] = raw[x] + decoded[x-4] for x >= 4
        # Store raw = [0,0,0,255, 10,10,10,0] → decoded = [0,0,0,255, 10,10,10,255]
        raw_row = bytearray([0, 0, 0, 255, 10, 10, 10, 0])
        encoded_row = bytearray(raw_row)  # with filter 1, sub:
        # actually let's just build the raw bytes directly without encoding
        # decode: row[x] = raw[x] if x<4 else (raw[x] + row[x-4]) & 0xFF
        # If raw_row = [50, 60, 70, 200, 5, 6, 7, 55], then:
        # decoded = [50, 60, 70, 200, 55, 66, 77, 255]
        w = 2
        raw_row2 = bytearray([50, 60, 70, 200, 5, 6, 7, 55])
        expected = bytearray([50, 60, 70, 200, 55, 66, 77, 255])

        raw = bytes([1]) + bytes(raw_row2)
        png = _PNG_SIG + _ihdr(w, 1) + _chunk(b"IDAT", zlib.compress(raw)) + _IEND
        rows = decode_png_rgba(png)
        assert rows[0] == expected

    def test_filter_2_up(self):
        # Two rows with filter 2 (Up)
        # row1 raw = [10, 20, 30, 255], prev=[0,0,0,0] → decoded=[10,20,30,255]
        # row2 raw = [5, 5, 5, 0], filter 2 → decoded=[15,25,35,255]
        raw = bytes([0]) + bytes([10, 20, 30, 255]) + bytes([2]) + bytes([5, 5, 5, 0])
        png = _PNG_SIG + _ihdr(1, 2) + _chunk(b"IDAT", zlib.compress(raw)) + _IEND
        rows = decode_png_rgba(png)
        assert rows[0] == bytearray([10, 20, 30, 255])
        assert rows[1] == bytearray([15, 25, 35, 255])

    def test_filter_3_average(self):
        # 1x1, filter 3 (Average), prev row = 0, a = 0 → decoded = raw[0] + 0
        raw = bytes([3]) + bytes([80, 100, 120, 200])
        png = _PNG_SIG + _ihdr(1, 1) + _chunk(b"IDAT", zlib.compress(raw)) + _IEND
        rows = decode_png_rgba(png)
        assert rows[0] == bytearray([80, 100, 120, 200])

    def test_filter_4_paeth(self):
        # 1x1, filter 4 (Paeth), all neighbours 0 → decoded = raw byte
        raw = bytes([4]) + bytes([42, 55, 60, 210])
        png = _PNG_SIG + _ihdr(1, 1) + _chunk(b"IDAT", zlib.compress(raw)) + _IEND
        rows = decode_png_rgba(png)
        assert rows[0] == bytearray([42, 55, 60, 210])

    def test_unknown_filter_raises(self):
        raw = bytes([7]) + bytes([10, 20, 30, 255])
        png = _PNG_SIG + _ihdr(1, 1) + _chunk(b"IDAT", zlib.compress(raw)) + _IEND
        with pytest.raises(ValueError, match="Unknown PNG filter"):
            decode_png_rgba(png)

    def test_non_rgba_color_type_raises(self):
        # color type 2 = RGB
        ihdr = _chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0),
        )
        raw = bytes([0]) + bytes([10, 20, 30])
        idat = _chunk(b"IDAT", zlib.compress(raw))
        png = _PNG_SIG + ihdr + idat + _IEND
        with pytest.raises(ValueError, match="color type"):
            decode_png_rgba(png)

    def test_non_8bit_raises(self):
        ihdr = _chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", 1, 1, 16, 6, 0, 0, 0),
        )
        raw = bytes([0]) + bytes([0, 10, 0, 20, 0, 30, 0, 255])
        idat = _chunk(b"IDAT", zlib.compress(raw))
        png = _PNG_SIG + ihdr + idat + _IEND
        with pytest.raises(ValueError, match="bit depth"):
            decode_png_rgba(png)

    def test_interlaced_raises(self):
        ihdr = _chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 1),
        )
        raw = bytes([0]) + bytes([10, 20, 30, 255])
        idat = _chunk(b"IDAT", zlib.compress(raw))
        png = _PNG_SIG + ihdr + idat + _IEND
        with pytest.raises(ValueError, match="Interlaced"):
            decode_png_rgba(png)

    def test_bad_signature_raises(self):
        with pytest.raises(ValueError, match="Not a valid PNG"):
            decode_png_rgba(b"not a png")


# ---------------------------------------------------------------------------
# icon_path_from_rgba
# ---------------------------------------------------------------------------


class TestIconPathFromRgba:
    def _dark_row(self, n: int = 32) -> bytearray:
        return bytearray([0, 0, 0, 255] * n)

    def _transparent_row(self, n: int = 32) -> bytearray:
        return bytearray([0, 0, 0, 0] * n)

    def test_nonempty_path_from_real_svg(self):
        png = png_from_nav_svg(NAV_32_SVG)
        rows = decode_png_rgba(png)
        path = icon_path_from_rgba(rows)
        assert path, "Path should be non-empty"

    def test_path_coords_within_bounds(self):
        png = png_from_nav_svg(NAV_32_SVG)
        rows = decode_png_rgba(png)
        path = icon_path_from_rgba(rows)
        import re

        numbers = [int(n) for n in re.findall(r"\d+", path)]
        assert all(0 <= n <= 32 for n in numbers)

    def test_deterministic(self):
        png = png_from_nav_svg(NAV_32_SVG)
        rows = decode_png_rgba(png)
        assert icon_path_from_rgba(rows) == icon_path_from_rgba(rows)

    def test_all_transparent_raises(self):
        rows = [self._transparent_row() for _ in range(4)]
        with pytest.raises(ValueError, match="No opaque dark"):
            icon_path_from_rgba(rows)

    def test_run_encoding_single_row(self):
        # A 4-pixel row: [dark, dark, transparent, dark]
        row = bytearray([0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255, 0, 0, 0, 0, 255])
        path = icon_path_from_rgba([row])
        # Expect two runs: x=0 len=2 and x=3 len=1
        assert "M0 0h2v1h-2Z" in path
        assert "M3 0h1v1h-1Z" in path

    def test_light_pixel_excluded(self):
        # A bright pixel (lum > 180) should not appear in path
        row = bytearray([220, 220, 220, 255])  # luminance ≈ 220 > 180
        with pytest.raises(ValueError):
            icon_path_from_rgba([row])


# ---------------------------------------------------------------------------
# build_iconset_js
# ---------------------------------------------------------------------------


class TestBuildIconsetJs:
    def test_contains_pueo_key(self):
        js = build_iconset_js("M0 0h1v1h-1Z")
        assert '"pueo"' in js

    def test_contains_path(self):
        path_d = "M0 0h1v1h-1Z"
        js = build_iconset_js(path_d)
        assert path_d in js

    def test_default_viewbox(self):
        js = build_iconset_js("M0 0h1v1h-1Z")
        assert "0 0 32 32" in js

    def test_custom_viewbox(self):
        js = build_iconset_js("M0 0h1v1h-1Z", viewbox="0 0 24 24")
        assert "0 0 24 24" in js

    def test_path_is_json_escaped(self):
        # Path with characters that need JSON escaping
        path_with_quote = 'M0 "test" 0'
        js = build_iconset_js(path_with_quote)
        # JSON.dumps wraps in quotes and escapes internal quotes
        import json

        assert json.dumps(path_with_quote) in js

    def test_registers_customIconsets(self):
        js = build_iconset_js("M0 0h1v1h-1Z")
        assert "window.customIconsets" in js


# ---------------------------------------------------------------------------
# add_extra_module_url
# ---------------------------------------------------------------------------


class TestAddExtraModuleUrl:
    URL = "/local/pueo/pueo-icons.js"

    def test_no_frontend_appends_block(self):
        config = "homeassistant:\n  name: Home\n"
        result = add_extra_module_url(config, self.URL)
        assert result is not None
        assert "frontend:" in result
        assert "extra_module_url:" in result
        assert self.URL in result

    def test_frontend_no_emu_inserts_key(self):
        config = "homeassistant:\n  name: Home\n\nfrontend:\n  themes: {}\n"
        result = add_extra_module_url(config, self.URL)
        assert result is not None
        assert "extra_module_url:" in result
        assert self.URL in result
        # Confirm themes still present
        assert "themes: {}" in result

    def test_frontend_with_emu_appends_item(self):
        config = (
            "homeassistant:\n  name: Home\n\n"
            "frontend:\n"
            "  extra_module_url:\n"
            "    - /local/other.js\n"
        )
        result = add_extra_module_url(config, self.URL)
        assert result is not None
        assert self.URL in result
        assert "/local/other.js" in result

    def test_url_already_present_is_idempotent(self):
        config = "frontend:\n" "  extra_module_url:\n" f"    - {self.URL}\n"
        result = add_extra_module_url(config, self.URL)
        assert result == config

    def test_frontend_include_returns_none(self):
        config = "frontend: !include frontend.yaml\n"
        assert add_extra_module_url(config, self.URL) is None

    def test_frontend_inline_value_returns_none(self):
        config = "frontend: {themes: {}}\n"
        assert add_extra_module_url(config, self.URL) is None

    def test_result_still_contains_url_after_multiple_calls(self):
        config = "homeassistant:\n  name: Home\n"
        r1 = add_extra_module_url(config, self.URL)
        assert r1 is not None
        r2 = add_extra_module_url(r1, self.URL)
        assert r2 == r1  # idempotent

    def test_new_item_after_existing_items(self):
        config = (
            "frontend:\n"
            "  extra_module_url:\n"
            "    - /local/a.js\n"
            "    - /local/b.js\n"
        )
        result = add_extra_module_url(config, self.URL)
        assert result is not None
        lines = result.splitlines()
        idx_a = next(i for i, l in enumerate(lines) if "/local/a.js" in l)
        idx_b = next(i for i, l in enumerate(lines) if "/local/b.js" in l)
        idx_new = next(i for i, l in enumerate(lines) if self.URL in l)
        assert idx_new > idx_b > idx_a


# ---------------------------------------------------------------------------
# detect_lan_ip
# ---------------------------------------------------------------------------


class TestDetectLanIp:
    def test_returns_en0_ip_when_available(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="192.168.1.100\n")
            result = detect_lan_ip()
        assert result == "192.168.1.100"

    def test_falls_back_to_en1(self):
        def side_effect(args, **kwargs):
            m = MagicMock()
            if args == ["ipconfig", "getifaddr", "en0"]:
                m.stdout = ""
            else:
                m.stdout = "192.168.1.200\n"
            return m

        with patch("subprocess.run", side_effect=side_effect):
            result = detect_lan_ip()
        assert result == "192.168.1.200"

    def test_falls_back_to_socket(self):
        with patch("subprocess.run", side_effect=Exception("no ipconfig")):
            with patch("socket.socket") as mock_socket_cls:
                mock_sock = MagicMock()
                mock_sock.__enter__ = lambda s: s
                mock_sock.__exit__ = MagicMock(return_value=False)
                mock_sock.getsockname.return_value = ("10.0.0.5", 0)
                mock_socket_cls.return_value = mock_sock
                result = detect_lan_ip()
        assert result == "10.0.0.5"

    def test_returns_none_when_all_fail(self):
        with patch("subprocess.run", side_effect=Exception):
            with patch("socket.socket", side_effect=Exception):
                result = detect_lan_ip()
        assert result is None


# ---------------------------------------------------------------------------
# webpage_dashboard_payload
# ---------------------------------------------------------------------------


class TestWebpageDashboardPayload:
    def test_default_shape(self):
        p = webpage_dashboard_payload("http://192.168.1.1:8099")
        assert p["title"] == "Pueo"
        assert p["icon"] == "pueo:owl"
        assert p["url_path"] == "pueo-dashboard"
        assert p["url"] == "http://192.168.1.1:8099"
        assert p["show_in_sidebar"] is True

    def test_url_path_contains_hyphen(self):
        p = webpage_dashboard_payload("http://192.168.1.1:8099")
        assert "-" in p["url_path"]

    def test_custom_title_and_icon(self):
        p = webpage_dashboard_payload(
            "http://192.168.1.1:8099", title="My Pueo", icon="mdi:owl"
        )
        assert p["title"] == "My Pueo"
        assert p["icon"] == "mdi:owl"

    def test_type_is_iframe(self):
        p = webpage_dashboard_payload("http://192.168.1.1:8099")
        assert p["type"] == "iframe"


# ---------------------------------------------------------------------------
# install_ha_sidebar orchestrator
# ---------------------------------------------------------------------------


# Minimal valid SVG that embeds a 2×2 RGBA PNG with one dark pixel.
def _make_minimal_svg() -> str:
    """Return an SVG string that embeds a 2×2 RGBA PNG decodable by png_from_nav_svg."""
    # Build a 2×2 RGBA PNG with pixel (0,0) dark-opaque, rest transparent.
    width, height = 2, 2

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        return (
            struct.pack(">I", len(data))
            + c
            + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        )

    ihdr = _chunk(
        b"IHDR",
        struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0),
    )
    rows = [bytearray([0, 0, 0, 255, 0, 0, 0, 0])]  # row0: dark opaque, transparent
    rows.append(bytearray([0, 0, 0, 0, 0, 0, 0, 0]))  # row1: all transparent
    raw = b""
    for row in rows:
        raw += bytes([0]) + bytes(row)
    idat = _chunk(b"IDAT", zlib.compress(raw))
    iend = _chunk(b"IEND", b"")
    png_bytes = b"\x89PNG\r\n\x1a\n" + ihdr + idat + iend
    b64 = base64.b64encode(png_bytes).decode()
    return f'<svg><image xlink:href="data:image/png;base64,{b64}"/></svg>'


import json as _json

_REGISTRY_PATH = "/config/.storage/lovelace_dashboards"
_DASH_CONFIG_PATH = "/config/.storage/lovelace.pueo-dashboard"


def _make_fake_ssh(
    config_text: str,
    check_exit_code: int = 0,
    storage_files: dict | None = None,
):
    from utils.ha.ssh_client import FakeSSHClient

    files: dict = {"/config/configuration.yaml": config_text}
    if storage_files:
        files.update(storage_files)
    return FakeSSHClient(
        file_contents=files,
        command_results={
            "mkdir": (0, "", ""),
            "ha core check": (
                check_exit_code,
                "",
                "check failed" if check_exit_code else "",
            ),
        },
    )


_PATCH_BACKUP = "agents.ha_agent_advanced.execute_remote_backup"
_PATCH_RECORD = "agents.ha_agent_advanced.record_backup_slug"
_PATCH_RESTART = "agents.ha_update_manager.execute_ha_core_restart"


class TestInstallHaSidebar:
    """Tests for the install_ha_sidebar async orchestrator."""

    def _patch_agents(self, backup_slug="abc123"):
        mock_backup = AsyncMock(return_value=backup_slug)
        mock_record = MagicMock()
        mock_restart = AsyncMock(return_value=True)
        return mock_backup, mock_record, mock_restart

    def _run_install(
        self,
        ssh,
        notifier=None,
        url="http://1.2.3.4:8080",
        mock_backup=None,
        mock_record=None,
        mock_restart=None,
        svg_path=None,
    ):
        mb = mock_backup or AsyncMock(return_value="abc123")
        mr = mock_record or MagicMock()
        mres = mock_restart or AsyncMock(return_value=True)
        with (
            patch(_PATCH_BACKUP, mb),
            patch(_PATCH_RECORD, mr),
            patch(_PATCH_RESTART, mres),
        ):
            asyncio.run(
                install_ha_sidebar(
                    ssh, url, notifier=notifier or MagicMock(), svg_path=svg_path
                )
            )
        return mb, mr, mres

    def _svg_path(self, tmp_path: Path) -> Path:
        svg = tmp_path / "nav_32.svg"
        svg.write_text(_make_minimal_svg())
        return svg

    def test_backup_before_config_write(self, tmp_path):
        """Backup must precede the config write when configuration.yaml changes."""
        config_text = "homeassistant:\n  name: Test\n"
        ssh = _make_fake_ssh(config_text)
        call_order = []

        async def fake_backup(ssh_client=None):
            call_order.append("backup")
            return "slug1"

        async def fake_write(path, content):
            call_order.append(f"write:{path}")

        ssh.write_file = fake_write  # type: ignore[method-assign]

        with (
            patch(_PATCH_BACKUP, fake_backup),
            patch(_PATCH_RECORD, MagicMock()),
            patch(_PATCH_RESTART, AsyncMock(return_value=True)),
        ):
            asyncio.run(
                install_ha_sidebar(
                    ssh,
                    "http://1.2.3.4:8080",
                    notifier=MagicMock(),
                    svg_path=self._svg_path(tmp_path),
                )
            )

        config_write_idx = next(
            (
                i
                for i, c in enumerate(call_order)
                if c == "write:/config/configuration.yaml"
            ),
            None,
        )
        backup_idx = call_order.index("backup")
        assert backup_idx < config_write_idx

    def test_failed_check_restores_original(self, tmp_path):
        """When ha core check fails the original config is restored and restart is not called."""
        config_text = "homeassistant:\n  name: Test\n"
        ssh = _make_fake_ssh(config_text, check_exit_code=1)
        mock_restart = AsyncMock(return_value=True)

        with pytest.raises(RuntimeError, match="ha core check failed"):
            with (
                patch(_PATCH_BACKUP, AsyncMock(return_value="s")),
                patch(_PATCH_RECORD, MagicMock()),
                patch(_PATCH_RESTART, mock_restart),
            ):
                asyncio.run(
                    install_ha_sidebar(
                        ssh,
                        "http://1.2.3.4:8080",
                        notifier=MagicMock(),
                        svg_path=self._svg_path(tmp_path),
                    )
                )

        assert ssh.written_files.get("/config/configuration.yaml") == config_text
        mock_restart.assert_not_called()

    def test_unchanged_config_skips_backup_and_restart(self, tmp_path):
        """If config and storage are already up to date, no backup or restart should happen."""
        config_text = (
            "frontend:\n  extra_module_url:\n    - /local/pueo/pueo-icons.js\n"
        )
        url = "http://1.2.3.4:8080"
        registry = {
            "version": 1,
            "minor_version": 1,
            "key": "lovelace_dashboards",
            "data": {
                "items": [
                    {
                        "id": "lovelace_pueo_dashboard",
                        "url_path": "pueo-dashboard",
                        "title": "Pueo",
                        "icon": "pueo:owl",
                        "show_in_sidebar": True,
                        "require_admin": False,
                        "mode": "storage",
                    }
                ]
            },
        }
        dash_cfg = {
            "version": 1,
            "minor_version": 1,
            "key": "lovelace.pueo-dashboard",
            "data": {"config": {"strategy": {"type": "iframe", "url": url}}},
        }
        ssh = _make_fake_ssh(
            config_text,
            storage_files={
                _REGISTRY_PATH: _json.dumps(registry),
                _DASH_CONFIG_PATH: _json.dumps(dash_cfg),
            },
        )
        mb, mr, mres = self._patch_agents()

        self._run_install(
            ssh,
            url=url,
            mock_backup=mb,
            mock_record=mr,
            mock_restart=mres,
            svg_path=self._svg_path(tmp_path),
        )

        mb.assert_not_called()
        mr.assert_not_called()
        mres.assert_not_called()

    def test_none_case_skips_config_but_creates_dashboard(self, tmp_path, capsys):
        """When add_extra_module_url returns None, config write is skipped but storage is written."""
        config_text = "frontend: !include frontend.yaml\n"
        ssh = _make_fake_ssh(config_text)
        mb = AsyncMock(return_value="s")
        mr = MagicMock()
        mres = AsyncMock(return_value=True)

        self._run_install(
            ssh,
            mock_backup=mb,
            mock_record=mr,
            mock_restart=mres,
            svg_path=self._svg_path(tmp_path),
        )

        mb.assert_not_called()
        captured = capsys.readouterr()
        assert "manually" in captured.out.lower() or "⚠" in captured.out
        assert _REGISTRY_PATH in ssh.written_files
        assert _DASH_CONFIG_PATH in ssh.written_files

    def test_existing_dashboard_updated_not_duplicated(self, tmp_path):
        """When the registry already has the dashboard it is updated, not duplicated."""
        config_text = "homeassistant:\n  name: Test\n"
        existing_registry = {
            "version": 1,
            "minor_version": 1,
            "key": "lovelace_dashboards",
            "data": {
                "items": [
                    {
                        "id": "lovelace_pueo_dashboard",
                        "url_path": "pueo-dashboard",
                        "title": "Old Title",
                        "icon": "mdi:owl",
                        "show_in_sidebar": True,
                        "require_admin": False,
                        "mode": "storage",
                    }
                ]
            },
        }
        ssh = _make_fake_ssh(
            config_text,
            storage_files={_REGISTRY_PATH: _json.dumps(existing_registry)},
        )

        self._run_install(ssh, svg_path=self._svg_path(tmp_path))

        # Registry should be written with updated entry
        assert _REGISTRY_PATH in ssh.written_files
        written = _json.loads(ssh.written_files[_REGISTRY_PATH])
        items = written["data"]["items"]
        assert len(items) == 1
        assert items[0]["title"] == "Pueo"
        assert items[0]["icon"] == "pueo:owl"

    def test_iconset_js_written_to_remote(self, tmp_path):
        """The JS iconset file should be written to /config/www/pueo/pueo-icons.js."""
        config_text = "homeassistant:\n  name: Test\n"
        ssh = _make_fake_ssh(config_text)

        self._run_install(ssh, svg_path=self._svg_path(tmp_path))

        assert "/config/www/pueo/pueo-icons.js" in ssh.written_files
        assert "customIconsets" in ssh.written_files["/config/www/pueo/pueo-icons.js"]

    def test_dashboard_config_saved_with_iframe_url(self, tmp_path):
        """Dashboard config file must contain the iframe strategy and the dashboard URL."""
        config_text = "homeassistant:\n  name: Test\n"
        ssh = _make_fake_ssh(config_text)
        url = "http://5.6.7.8:8080"

        self._run_install(ssh, url=url, svg_path=self._svg_path(tmp_path))

        assert _DASH_CONFIG_PATH in ssh.written_files
        saved = _json.loads(ssh.written_files[_DASH_CONFIG_PATH])
        cfg = saved["data"]["config"]
        assert cfg["strategy"]["type"] == "iframe"
        assert cfg["strategy"]["url"] == url


# ---------------------------------------------------------------------------
# main.py --mode ha-sidebar dispatch
# ---------------------------------------------------------------------------


class TestMainHaSidebarMode:
    def test_ha_sidebar_mode_dispatches(self, monkeypatch, tmp_path):
        import sys
        import main as main_module

        config = tmp_path / "config.yaml"
        config.write_text("")

        ran = []

        async def fake_run_ha_sidebar(args):
            ran.append(args.mode)

        monkeypatch.setattr(main_module, "_run_ha_sidebar", fake_run_ha_sidebar)
        monkeypatch.setattr(
            sys,
            "argv",
            ["main.py", "--config", str(config), "--mode", "ha-sidebar"],
        )
        main_module.main()
        assert ran == ["ha-sidebar"]
