"""Pueo HA dependency manifest: drift against the code, and term derivation."""

import re
from pathlib import Path

import pytest

from utils.ha.pueo_dependencies import (
    PUEO_HA_DEPENDENCIES,
    dependency_key,
    dependency_term_index,
    dependency_terms,
)
from utils.knowledge.ha_release_report import (
    PueoDependencyHit,
    find_pueo_dependency_hits,
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


# The HACS part of the manifest as it was before #1127 (git show e39983e^), when
# Pueo still called hacs/* and knew nothing about the Marketplace.
_PRE_1127_MANIFEST = {
    "websocket_commands": [
        {
            "cmd": "hacs/repositories/list",
            "feature": "HACS integration discovery and companion provisioning",
        },
        {
            "cmd": "hacs/repositories/add",
            "feature": "HACS integration install (companion provisioning)",
        },
        {
            "cmd": "hacs/repository/download",
            "feature": "HACS integration download (companion provisioning)",
        },
        {
            "cmd": "repairs/list_issues",
            "feature": "repair issue triage and host-health investigation",
        },
    ],
    "repair_id_patterns": [
        {
            "pattern": "hacs/restart_required_*",
            "feature": "HACS restart-required repair detection in log monitor",
        },
    ],
}

# Cut from the Home Assistant 2026.11 release blog post (plain text, as cached).
_BLOG_2026_11 = """\
## Welcome to the Marketplace

For years, the community has built custom integrations , dashboard cards, themes, and templates for Home Assistant. People found them through HACS, the Home Assistant Community Store. With this release, HACS becomes part of Home Assistant as the new Marketplace .

### Thank you, HACS

HACS made this possible. @ludeeus founded it and donated it to the Open Home Foundation about a year and a half ago, which is what made bringing it into Home Assistant possible.

If you use HACS today, there is nothing you need to do. Home Assistant migrates it when it starts after the update. Everything you installed stays installed and keeps receiving updates.

The HACS installer stops at Home Assistant 2026.11, so a separate HACS installation is no longer possible on this version. The old HACS won\u2019t get new releases either: the Marketplace ships with every Home Assistant update.

## Exclude items from a target

Sometimes you want an automation to run everywhere except one room. Repairs to old automations are not needed.
"""


class TestDependencyTermIndex:
    def test_entries_sharing_a_term_are_grouped(self):
        index = dependency_term_index(PUEO_HA_DEPENDENCIES)
        sections = {section for section, _ in index["hacs"]}
        assert {"websocket_commands", "integrations"} <= sections
        assert all(term == term.lower() for term in index)

    def test_blank_terms_are_skipped(self):
        index = dependency_term_index(
            {"integrations": [{"domain": "x", "terms": [" "]}]}
        )
        assert index == {}


class TestPueoDependencyHit:
    def test_valid(self):
        hit = PueoDependencyHit(
            dependency="hacs", kind="integrations", source="blog:2026.11", snippet="s"
        )
        assert hit.dependency == "hacs"

    def test_missing_field(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            PueoDependencyHit(dependency="hacs", kind="integrations", source="b")

    def test_json_round_trip(self):
        hit = PueoDependencyHit(
            dependency="hacs", kind="integrations", source="blog:2026.11", snippet="s"
        )
        assert PueoDependencyHit.model_validate_json(hit.model_dump_json()) == hit


class TestFindPueoDependencyHits:
    def test_old_manifest_finds_the_hacs_removal_in_blog_prose(self):
        """Acceptance (#1233): what the 2026.11.0 analysis missed is now found."""
        hits = find_pueo_dependency_hits(
            [("blog:2026.11", _BLOG_2026_11)], _PRE_1127_MANIFEST
        )
        hacs = [h for h in hits if h.dependency == "hacs"]
        assert any("no longer possible" in h.snippet for h in hacs)
        assert all(h.source == "blog:2026.11" for h in hacs)
        assert all(h.kind == "websocket_commands" for h in hacs)

    def test_generic_word_needs_a_change_verb(self):
        # "hacs" comes only from WS commands here, so prose that merely names
        # HACS is not a hit; neither is "Repairs ... are not needed".
        hits = find_pueo_dependency_hits(
            [("blog:2026.11", _BLOG_2026_11)], _PRE_1127_MANIFEST
        )
        assert {h.dependency for h in hits} == {"hacs"}
        assert not any("made this possible" in h.snippet for h in hits)
        assert not any(h.dependency == "repairs" for h in hits)

    def test_integration_name_matches_without_a_verb(self):
        manifest = {"integrations": [{"domain": "hacs", "terms": ["HACS"]}]}
        hits = find_pueo_dependency_hits([("blog:2026.11", _BLOG_2026_11)], manifest)
        assert any("made this possible" in h.snippet for h in hits)
        assert all(h.kind == "integrations" for h in hits)
        # Passages that say something changed come first.
        assert "migrates" in hits[0].snippet or "no longer" in hits[0].snippet

    def test_headings_are_not_hits(self):
        manifest = {"integrations": [{"domain": "hacs", "terms": ["HACS"]}]}
        hits = find_pueo_dependency_hits([("b", "### Thank you, HACS\n")], manifest)
        assert hits == []

    def test_full_command_and_path_match_anywhere(self):
        manifest = {
            "ssh_commands": [{"cmd": "ha apps info <slug>", "feature": "f"}],
            "rest_paths": [{"path": "GET /api/states/<entity_id>", "feature": "f"}],
        }
        text = (
            "- The `ha apps info` output gained a field\n"
            "- http://host:8123/api/states now paginates\n"
            "- Unrelated /api/statesman line\n"
        )
        hits = find_pueo_dependency_hits([("changelog:2026.11", text)], manifest)
        assert [(h.dependency, h.kind) for h in hits] == [
            ("/api/states", "rest_paths"),
            ("ha apps info", "ssh_commands"),
        ]

    def test_term_is_matched_whole(self):
        manifest = {"websocket_commands": [{"cmd": "repairs/list_issues"}]}
        text = (
            "- Remove the prerepairs helper\n"
            "- Removed repairsman\n"
            "- Rename repairs/list_issues to repairs/list\n"
        )
        hits = find_pueo_dependency_hits([("c", text)], manifest)
        assert [h.snippet for h in hits] == [
            "- Rename repairs/list_issues to repairs/list"
        ]

    def test_cap_keeps_verb_lines_and_dedups_across_sources(self):
        manifest = {"integrations": [{"domain": "spook", "terms": ["Spook"]}]}
        plain = "".join(f"- Spook note {i}\n" for i in range(10))
        text = plain + "- Spook service removed\n"
        hits = find_pueo_dependency_hits([("a", text), ("b", text)], manifest)
        assert len(hits) == 6
        assert hits[0].snippet == "- Spook service removed"
        assert {h.source for h in hits} == {"a"}

    def test_long_passage_is_cut_around_the_match(self):
        manifest = {"integrations": [{"domain": "spook", "terms": ["Spook"]}]}
        text = "x" * 2000 + " Spook was removed " + "y" * 2000
        (hit,) = find_pueo_dependency_hits([("a", text)], manifest)
        assert len(hit.snippet) <= 600
        assert "Spook was removed" in hit.snippet

    def test_no_texts_no_hits(self):
        assert find_pueo_dependency_hits([], PUEO_HA_DEPENDENCIES) == []
