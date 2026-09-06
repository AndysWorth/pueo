"""Tests for utils/knowledge/kb_ingester.py."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from utils.knowledge.kb_ingester import (
    KbIngestError,
    ManifestEntry,
    _collection_for_type,
    download_and_embed,
    fetch_manifest,
    load_sync_state,
    run_kb_sync,
    save_sync_state,
    select_relevant_entries,
)


# ── ManifestEntry dataclass ───────────────────────────────────────────────────


class TestManifestEntry:
    def test_defaults(self):
        e = ManifestEntry(id="x", type="runbook", path="runbooks/x.md", sha256="abc")
        assert e.integrations == ["all"]
        assert e.tags == []
        assert e.quality_score == 0.0
        assert e.ha_version_min is None

    def test_custom_fields(self):
        e = ManifestEntry(
            id="y",
            type="gap",
            path="gaps/y.yaml",
            sha256="def",
            integrations=["zha", "mqtt"],
            tags=["tag1"],
            quality_score=0.85,
        )
        assert e.integrations == ["zha", "mqtt"]
        assert e.quality_score == 0.85


# ── _validate_repo ────────────────────────────────────────────────────────────


class TestValidateRepo:
    def test_valid_repo(self):
        from utils.knowledge.kb_ingester import _validate_repo

        _validate_repo("owner/pueo-kb")  # should not raise

    def test_empty_repo_raises(self):
        from utils.knowledge.kb_ingester import _validate_repo

        with pytest.raises(KbIngestError):
            _validate_repo("")

    def test_invalid_format_raises(self):
        from utils.knowledge.kb_ingester import _validate_repo

        with pytest.raises(KbIngestError):
            _validate_repo("not-a-valid/repo/format/extra")

    def test_slash_only_raises(self):
        from utils.knowledge.kb_ingester import _validate_repo

        with pytest.raises(KbIngestError):
            _validate_repo("/")


# ── fetch_manifest ────────────────────────────────────────────────────────────


SAMPLE_MANIFEST = [
    {
        "id": "runbook-ha-config-error-v1",
        "type": "runbook",
        "path": "runbooks/ha_config_error.md",
        "sha256": "abc123",
        "tags": ["ha_config", "yaml_error"],
        "integrations": ["all"],
        "quality_score": 0.90,
        "added_at": "2026-09-01",
    },
    {
        "id": "case-zha-pairing-001",
        "type": "case",
        "path": "cases/2026-09/zha_001.yaml",
        "sha256": "def456",
        "tags": ["zha"],
        "integrations": ["zha"],
        "quality_score": 0.75,
    },
]


def _make_gh_response(content: str) -> str:
    import base64

    encoded = base64.b64encode(content.encode()).decode()
    return json.dumps({"encoding": "base64", "content": encoded})


class TestFetchManifest:
    def test_parses_entries(self):
        raw = _make_gh_response(json.dumps(SAMPLE_MANIFEST))
        with patch("utils.knowledge.kb_ingester._run_gh", return_value=raw):
            entries = fetch_manifest("owner/pueo-kb")
        assert len(entries) == 2
        assert entries[0].id == "runbook-ha-config-error-v1"
        assert entries[0].type == "runbook"
        assert entries[0].sha256 == "abc123"
        assert entries[0].integrations == ["all"]
        assert entries[1].id == "case-zha-pairing-001"
        assert entries[1].integrations == ["zha"]

    def test_invalid_repo_raises(self):
        with pytest.raises(KbIngestError):
            fetch_manifest("")

    def test_non_list_manifest_raises(self):
        raw = _make_gh_response(json.dumps({"not": "a list"}))
        with patch("utils.knowledge.kb_ingester._run_gh", return_value=raw):
            with pytest.raises(KbIngestError):
                fetch_manifest("owner/pueo-kb")

    def test_gh_failure_raises(self):
        with patch(
            "utils.knowledge.kb_ingester._run_gh",
            side_effect=KbIngestError("gh failed"),
        ):
            with pytest.raises(KbIngestError):
                fetch_manifest("owner/pueo-kb")

    def test_skips_non_dict_items(self):
        raw = _make_gh_response(json.dumps([SAMPLE_MANIFEST[0], "not_a_dict"]))
        with patch("utils.knowledge.kb_ingester._run_gh", return_value=raw):
            entries = fetch_manifest("owner/pueo-kb")
        assert len(entries) == 1


# ── select_relevant_entries ───────────────────────────────────────────────────


def _entry(**kwargs: Any) -> ManifestEntry:
    defaults: dict[str, Any] = dict(id="x", type="runbook", path="p.md", sha256="s1")
    defaults.update(kwargs)
    return ManifestEntry(**defaults)


class TestSelectRelevantEntries:
    def test_all_integrations_kept(self):
        e = _entry(integrations=["all"], sha256="s1")
        result = select_relevant_entries([e], ["zha"], set())
        assert result == [e]

    def test_matching_integration_kept(self):
        e = _entry(integrations=["zha", "mqtt"], sha256="s2")
        result = select_relevant_entries([e], ["zha"], set())
        assert result == [e]

    def test_non_matching_integration_excluded(self):
        e = _entry(integrations=["zha"], sha256="s3")
        result = select_relevant_entries([e], ["mqtt"], set())
        assert result == []

    def test_already_ingested_excluded(self):
        e = _entry(integrations=["all"], sha256="s4")
        result = select_relevant_entries([e], [], {"s4"})
        assert result == []

    def test_empty_sha256_not_excluded_by_state(self):
        e = _entry(integrations=["all"], sha256="")
        result = select_relevant_entries([e], [], {"s5"})
        assert result == [e]

    def test_case_insensitive_integration_match(self):
        e = _entry(integrations=["ZHA"], sha256="s6")
        result = select_relevant_entries([e], ["zha"], set())
        assert result == [e]

    def test_empty_profile_with_all_integrations(self):
        e = _entry(integrations=["all"], sha256="s7")
        result = select_relevant_entries([e], [], set())
        assert result == [e]

    def test_empty_profile_with_specific_integration_excluded(self):
        e = _entry(integrations=["zha"], sha256="s8")
        result = select_relevant_entries([e], [], set())
        assert result == []


# ── _collection_for_type ──────────────────────────────────────────────────────


class TestCollectionForType:
    def test_runbook_maps_to_strategies(self):
        assert _collection_for_type("runbook") == "strategies"

    def test_gap_maps_to_strategies(self):
        assert _collection_for_type("gap") == "strategies"

    def test_unknown_maps_to_strategies(self):
        assert _collection_for_type("unknown") == "strategies"


# ── download_and_embed ────────────────────────────────────────────────────────


class TestDownloadAndEmbed:
    def _fake_store(self):
        store = MagicMock()
        store.upsert = MagicMock()
        return store

    def test_embeds_valid_entries(self):
        entries = [_entry(id="rb1", sha256="h1", type="runbook", integrations=["all"])]
        store = self._fake_store()
        raw = _make_gh_response("# Runbook content here")
        with patch("utils.knowledge.kb_ingester._run_gh", return_value=raw):
            count, sha256s = download_and_embed(entries, "owner/pueo-kb", store)
        assert count == 1
        assert "h1" in sha256s
        store.upsert.assert_called_once()
        call_kwargs = store.upsert.call_args
        assert call_kwargs[0][0] == "strategies"

    def test_skips_empty_content(self):
        entries = [_entry(id="rb2", sha256="h2", type="runbook", integrations=["all"])]
        store = self._fake_store()
        raw = _make_gh_response("   ")  # whitespace only
        with patch("utils.knowledge.kb_ingester._run_gh", return_value=raw):
            count, sha256s = download_and_embed(entries, "owner/pueo-kb", store)
        assert count == 0
        assert sha256s == set()

    def test_skips_on_fetch_failure(self):
        entries = [_entry(id="rb3", sha256="h3", type="runbook", integrations=["all"])]
        store = self._fake_store()
        with patch(
            "utils.knowledge.kb_ingester._run_gh",
            side_effect=KbIngestError("network"),
        ):
            count, sha256s = download_and_embed(entries, "owner/pueo-kb", store)
        assert count == 0

    def test_skips_on_upsert_failure(self):
        entries = [_entry(id="rb4", sha256="h4", type="runbook", integrations=["all"])]
        store = self._fake_store()
        store.upsert.side_effect = RuntimeError("chroma error")
        raw = _make_gh_response("some content")
        with patch("utils.knowledge.kb_ingester._run_gh", return_value=raw):
            count, sha256s = download_and_embed(entries, "owner/pueo-kb", store)
        assert count == 0

    def test_gap_goes_to_strategies(self):
        entries = [_entry(id="g1", sha256="h5", type="gap", integrations=["all"])]
        store = self._fake_store()
        raw = _make_gh_response("gap content")
        with patch("utils.knowledge.kb_ingester._run_gh", return_value=raw):
            download_and_embed(entries, "owner/pueo-kb", store)
        call_args = store.upsert.call_args
        assert call_args[0][0] == "strategies"


# ── load/save sync state ──────────────────────────────────────────────────────


class TestSyncState:
    def test_load_empty_returns_empty_dict(self, tmp_path):
        state = load_sync_state(str(tmp_path))
        assert state == {}

    def test_save_and_load_roundtrip(self, tmp_path):
        state = {"ingested_sha256s": ["abc", "def"]}
        save_sync_state(str(tmp_path), state)
        loaded = load_sync_state(str(tmp_path))
        assert loaded == state

    def test_load_corrupt_file_returns_empty(self, tmp_path):
        (tmp_path / "kb_sync_state.json").write_text("not json")
        state = load_sync_state(str(tmp_path))
        assert state == {}

    def test_save_creates_directory(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c"
        save_sync_state(str(nested), {"x": 1})
        assert (nested / "kb_sync_state.json").exists()


# ── run_kb_sync ───────────────────────────────────────────────────────────────


class TestRunKbSync:
    def test_invalid_repo_raises(self, tmp_path):
        store = MagicMock()
        with pytest.raises(KbIngestError):
            run_kb_sync("", str(tmp_path), store)

    def test_no_relevant_entries_returns_zero(self, tmp_path):
        store = MagicMock()
        manifest = [_entry(integrations=["zha"], sha256="s1")]
        raw = _make_gh_response(
            json.dumps(
                [
                    {
                        "id": "x",
                        "type": "runbook",
                        "path": "p.md",
                        "sha256": "s1",
                        "integrations": ["zha"],
                        "tags": [],
                        "quality_score": 0.5,
                    }
                ]
            )
        )
        with patch("utils.knowledge.kb_ingester._run_gh", return_value=raw):
            # integration_profile is empty so zha-only entry is filtered out
            count = run_kb_sync(
                "owner/kb", str(tmp_path), store, integration_profile=[]
            )
        assert count == 0

    def test_already_ingested_skipped(self, tmp_path):
        store = MagicMock()
        save_sync_state(str(tmp_path), {"ingested_sha256s": ["s2"]})
        manifest_raw = _make_gh_response(
            json.dumps(
                [
                    {
                        "id": "rb",
                        "type": "runbook",
                        "path": "p.md",
                        "sha256": "s2",
                        "integrations": ["all"],
                        "tags": [],
                        "quality_score": 0.9,
                    }
                ]
            )
        )
        with patch("utils.knowledge.kb_ingester._run_gh", return_value=manifest_raw):
            count = run_kb_sync(
                "owner/kb", str(tmp_path), store, integration_profile=[]
            )
        assert count == 0
        store.upsert.assert_not_called()

    def test_successful_sync_saves_state(self, tmp_path):
        store = MagicMock()
        manifest_raw = _make_gh_response(
            json.dumps(
                [
                    {
                        "id": "rb",
                        "type": "runbook",
                        "path": "p.md",
                        "sha256": "s3",
                        "integrations": ["all"],
                        "tags": [],
                        "quality_score": 0.9,
                    }
                ]
            )
        )
        file_raw = _make_gh_response("# Runbook body")
        call_count = {"n": 0}

        def _gh(args, timeout=60):
            call_count["n"] += 1
            return manifest_raw if call_count["n"] == 1 else file_raw

        with patch("utils.knowledge.kb_ingester._run_gh", side_effect=_gh):
            count = run_kb_sync(
                "owner/kb", str(tmp_path), store, integration_profile=[]
            )
        assert count == 1
        state = load_sync_state(str(tmp_path))
        assert "s3" in state["ingested_sha256s"]
