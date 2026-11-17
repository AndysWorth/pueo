"""Pueo HA dependency manifest: drift against the code, and term derivation."""

import re
from pathlib import Path

import pytest

from utils.ha.pueo_dependencies import (
    PUEO_HA_DEPENDENCIES,
    dependency_key,
    dependency_terms,
)

_ROOT = Path(__file__).resolve().parent.parent
_SCAN_DIRS = ("utils", "agents", "netalertx", "web")

_WS_CALL_RE = re.compile(r'_call\(\s*"([a-z_]+(?:/[a-z_]+)*)"')
_WS_TYPE_RE = re.compile(r'"type":\s*"([a-z_]+(?:/[a-z_]+)+)"')
_WS_CONST_RE = re.compile(r'_TYPE\s*=\s*"([a-z_]+(?:/[a-z_]+)+)"')
_REST_URL_RE = re.compile(r'f"\{(self\._base_url|base|ha_url)\}(/[^"?]*)')
_REST_HELPER_RE = re.compile(
    r'rest\.(?:get_raw|get_text|post|delete)\(\s*f?"(/api/[^"?]*)'
)


def _sources() -> list[tuple[Path, str]]:
    out = []
    for d in _SCAN_DIRS:
        for path in sorted((_ROOT / d).rglob("*.py")):
            if path.name == "pueo_dependencies.py":
                continue
            out.append((path, path.read_text(encoding="utf-8")))
    return out


def _ws_commands_in_code() -> dict[str, str]:
    found: dict[str, str] = {}
    for path, text in _sources():
        for rx in (_WS_CALL_RE, _WS_TYPE_RE, _WS_CONST_RE):
            for cmd in rx.findall(text):
                found.setdefault(cmd, str(path.relative_to(_ROOT)))
    return found


def _norm_path(path: str) -> str:
    path = re.sub(r"\{[^}]*\}|<[^>]*>", "{}", path)
    return path.rstrip("/")


def _rest_paths_in_code() -> dict[str, str]:
    found: dict[str, str] = {}
    for path, text in _sources():
        rel = str(path.relative_to(_ROOT))
        for prefix, tail in _REST_URL_RE.findall(text):
            if prefix == "base" and tail.startswith("{"):
                continue  # generic get_raw/post helper: f"{base}{path}"
            full = ("/api" + tail) if prefix == "self._base_url" else tail
            if full.startswith("/api/"):
                found.setdefault(_norm_path(full), rel)
        for tail in _REST_HELPER_RE.findall(text):
            found.setdefault(_norm_path(tail), rel)
    return found


def _manifest_rest_paths() -> set[str]:
    return {
        _norm_path(re.sub(r"^[A-Z]+\s+", "", e["path"]))
        for e in PUEO_HA_DEPENDENCIES["rest_paths"]
    }


class TestManifestDrift:
    def test_scanner_finds_known_call_sites(self):
        ws = _ws_commands_in_code()
        assert "repairs/list_issues" in ws
        assert "marketplace/repositories/list" in ws
        assert "trace/get" in ws  # multi-line _call(
        assert "persistent_notification/subscribe" in ws  # module constant
        rest = _rest_paths_in_code()
        assert "/api/states/{}" in rest
        assert "/api/repairs/issues/fix" in rest

    def test_every_ws_command_in_code_is_listed(self):
        listed = {e["cmd"] for e in PUEO_HA_DEPENDENCIES["websocket_commands"]}
        missing = {
            cmd: where
            for cmd, where in _ws_commands_in_code().items()
            if cmd not in listed
        }
        assert not missing, (
            "WebSocket commands called but not in PUEO_HA_DEPENDENCIES "
            f"(utils/ha/pueo_dependencies.py): {missing}"
        )

    def test_every_listed_ws_command_is_still_called(self):
        in_code = _ws_commands_in_code()
        all_text = "\n".join(text for _, text in _sources())
        stale = [
            e["cmd"]
            for e in PUEO_HA_DEPENDENCIES["websocket_commands"]
            if e["cmd"] not in in_code and f'"{e["cmd"]}"' not in all_text
        ]
        assert not stale, f"Manifest lists WebSocket commands no code calls: {stale}"

    def test_every_rest_path_in_code_is_listed(self):
        listed = _manifest_rest_paths()
        missing = {
            path: where
            for path, where in _rest_paths_in_code().items()
            if path not in listed
        }
        assert not missing, (
            "REST paths called but not in PUEO_HA_DEPENDENCIES "
            f"(utils/ha/pueo_dependencies.py): {missing}"
        )

    def test_keys_are_unique_within_a_section(self):
        for section, entries in PUEO_HA_DEPENDENCIES.items():
            keys = [dependency_key(e) for e in entries]
            assert len(keys) == len(set(keys)), f"duplicate key in {section}"

    def test_reexport_is_the_same_object(self):
        from agents import ha_update_manager

        assert ha_update_manager.PUEO_HA_DEPENDENCIES is PUEO_HA_DEPENDENCIES


class TestDependencyTerms:
    @pytest.mark.parametrize(
        "section,entry,expected",
        [
            ("integrations", {"domain": "hacs", "terms": ["HACS"]}, ["HACS"]),
            ("integrations", {"domain": "ai_agent_ha"}, ["ai_agent_ha"]),
            ("websocket_commands", {"cmd": "repairs/list_issues"}, ["repairs"]),
            (
                "websocket_commands",
                {"cmd": "config/entity_registry/list"},
                ["entity_registry"],
            ),
            ("websocket_commands", {"cmd": "config"}, ["config"]),
            ("ssh_commands", {"cmd": "ha apps info <slug>"}, ["ha apps info"]),
            ("ssh_commands", {"cmd": "ha core info --raw-json"}, ["ha core info"]),
            (
                "rest_paths",
                {"path": "POST /api/services/<domain>/<service>"},
                ["/api/services"],
            ),
            ("rest_paths", {"path": "POST /api/template"}, ["/api/template"]),
            (
                "repair_id_patterns",
                {"pattern": "hacs/restart_required_*"},
                ["hacs/restart_required"],
            ),
            ("ssh_file_paths", {"path": "/backup/"}, ["/backup/"]),
        ],
    )
    def test_terms(self, section, entry, expected):
        assert dependency_terms(section, entry) == expected

    def test_explicit_terms_are_copied(self):
        entry = {"domain": "hacs", "terms": ["HACS"]}
        terms = dependency_terms("integrations", entry)
        terms.append("x")
        assert entry["terms"] == ["HACS"]

    def test_every_entry_yields_a_non_empty_term(self):
        for section, entries in PUEO_HA_DEPENDENCIES.items():
            for entry in entries:
                terms = dependency_terms(section, entry)
                assert terms and all(t.strip() for t in terms), (section, entry)

    def test_dependency_key_fallbacks(self):
        assert dependency_key({"cmd": "a"}) == "a"
        assert dependency_key({"path": "b"}) == "b"
        assert dependency_key({"domain": "c"}) == "c"
        assert dependency_key({"pattern": "d"}) == "d"
        assert dependency_key({}) == ""
