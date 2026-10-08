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
    _ingest_key,
    download_and_embed,
    fetch_manifest,
    load_kb_manifest_ids,
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
        assert e.state == "seed"
        assert e.community_stats == {}

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

    def test_state_and_community_stats(self):
        e = ManifestEntry(
            id="x",
            type="runbook",
            path="p.md",
            sha256="s",
            state="validated",
            community_stats={"successes": 5, "failures": 0, "instances": 3},
        )
        assert e.state == "validated"
        assert e.community_stats == {"successes": 5, "failures": 0, "instances": 3}

    def test_source_seed_defaults_to_empty(self):
        e = ManifestEntry(id="x", type="runbook", path="p.md", sha256="s")
        assert e.source_seed == ""

    def test_source_seed_custom(self):
        e = ManifestEntry(
            id="x",
            type="runbook",
            path="p.md",
            sha256="s",
            source_seed="seed_update_analysis.md",
        )
        assert e.source_seed == "seed_update_analysis.md"


# ── _ingest_key ───────────────────────────────────────────────────────────────


class TestIngestKey:
    def test_key_format(self):
        e = ManifestEntry(
            id="rb1", type="runbook", path="p.md", sha256="abc", state="candidate"
        )
        assert _ingest_key(e) == "rb1:abc:candidate"

    def test_different_states_give_different_keys(self):
        e1 = ManifestEntry(
            id="rb1", type="runbook", path="p.md", sha256="abc", state="candidate"
        )
        e2 = ManifestEntry(
            id="rb1", type="runbook", path="p.md", sha256="abc", state="validated"
        )
        assert _ingest_key(e1) != _ingest_key(e2)


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
        "state": "validated",
        "community_stats": {"successes": 4, "failures": 0, "instances": 2},
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
        "state": "candidate",
        "community_stats": {"successes": 1, "failures": 0, "instances": 1},
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
        assert entries[0].state == "validated"
        assert entries[0].community_stats == {
            "successes": 4,
            "failures": 0,
            "instances": 2,
        }
        assert entries[1].id == "case-zha-pairing-001"
        assert entries[1].integrations == ["zha"]
        assert entries[1].state == "candidate"

    def test_missing_state_defaults_to_seed(self):
        manifest = [
            {
                "id": "rb",
                "type": "runbook",
                "path": "p.md",
                "sha256": "s",
                "integrations": ["all"],
            }
        ]
        raw = _make_gh_response(json.dumps(manifest))
        with patch("utils.knowledge.kb_ingester._run_gh", return_value=raw):
            entries = fetch_manifest("owner/pueo-kb")
        assert entries[0].state == "seed"
        assert entries[0].community_stats == {
            "successes": 0,
            "failures": 0,
            "instances": 0,
        }

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
        key = _ingest_key(e)
        result = select_relevant_entries([e], [], {key})
        assert result == []

    def test_same_sha256_different_state_not_excluded(self):
        """Same sha256 but different state → re-ingest (state changed)."""
        e = _entry(integrations=["all"], sha256="s4", state="validated")
        old_key = f"x:s4:candidate"  # key from old state
        result = select_relevant_entries([e], [], {old_key})
        assert result == [e]

    def test_empty_sha256_not_excluded_by_state(self):
        e = _entry(integrations=["all"], sha256="")
        result = select_relevant_entries([e], [], {"x::seed"})
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

    def test_flagged_entries_skipped(self):
        """Flagged entries are never ingested regardless of integration match."""
        e = _entry(integrations=["all"], sha256="s_flag", state="flagged")
        result = select_relevant_entries([e], [], set())
        assert result == []

    def test_source_seed_mirror_excluded(self):
        """Entry with source_seed matching a local seed filename is skipped."""
        e = _entry(
            integrations=["all"],
            sha256="s9",
            source_seed="seed_update_analysis.md",
        )
        seeds = frozenset({"seed_update_analysis.md"})
        result = select_relevant_entries([e], [], set(), local_seed_filenames=seeds)
        assert result == []

    def test_source_seed_not_in_local_seeds_kept(self):
        e = _entry(integrations=["all"], sha256="s10", source_seed="some_other_file.md")
        seeds = frozenset({"seed_update_analysis.md"})
        result = select_relevant_entries([e], [], set(), local_seed_filenames=seeds)
        assert result == [e]

    def test_slug_mirror_excluded_without_source_seed(self):
        """Entry with id matching a local seed filename stem is skipped via slug fallback."""
        e = _entry(id="seed_update_analysis", integrations=["all"], sha256="s11")
        seeds = frozenset({"seed_update_analysis.md"})
        result = select_relevant_entries([e], [], set(), local_seed_filenames=seeds)
        assert result == []

    def test_no_seed_filter_without_local_seeds(self):
        """Without local_seed_filenames, source_seed is ignored."""
        e = _entry(
            integrations=["all"], sha256="s12", source_seed="seed_update_analysis.md"
        )
        result = select_relevant_entries([e], [], set())
        assert result == [e]


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
            count, new_keys = download_and_embed(entries, "owner/pueo-kb", store)
        assert count == 1
        assert any("h1" in k for k in new_keys)
        store.upsert.assert_called_once()
        call_kwargs = store.upsert.call_args
        assert call_kwargs[0][0] == "strategies"

    def test_kb_state_in_metadata(self):
        entries = [
            _entry(
                id="rb_cand",
                sha256="hc",
                type="runbook",
                integrations=["all"],
                state="candidate",
            )
        ]
        store = self._fake_store()
        raw = _make_gh_response("candidate runbook body")
        with patch("utils.knowledge.kb_ingester._run_gh", return_value=raw):
            download_and_embed(entries, "owner/pueo-kb", store)
        metadata = store.upsert.call_args.kwargs["metadatas"][0]
        assert metadata["kb_state"] == "candidate"

    def test_community_stats_in_metadata_when_present(self):
        entry = ManifestEntry(
            id="rb_stats",
            type="runbook",
            path="p.md",
            sha256="hs",
            state="validated",
            community_stats={"successes": 5, "failures": 0, "instances": 3},
            integrations=["all"],
        )
        store = self._fake_store()
        raw = _make_gh_response("validated runbook body")
        with patch("utils.knowledge.kb_ingester._run_gh", return_value=raw):
            download_and_embed([entry], "owner/pueo-kb", store)
        metadata = store.upsert.call_args.kwargs["metadatas"][0]
        assert metadata["community_successes"] == 5
        assert metadata["community_failures"] == 0
        assert metadata["community_instances"] == 3

    def test_community_stats_omitted_when_empty(self):
        entry = _entry(
            id="rb_nostats", sha256="hns", type="runbook", integrations=["all"]
        )
        store = self._fake_store()
        raw = _make_gh_response("some runbook body")
        with patch("utils.knowledge.kb_ingester._run_gh", return_value=raw):
            download_and_embed([entry], "owner/pueo-kb", store)
        metadata = store.upsert.call_args.kwargs["metadatas"][0]
        assert "community_successes" not in metadata

    def test_skips_empty_content(self):
        entries = [_entry(id="rb2", sha256="h2", type="runbook", integrations=["all"])]
        store = self._fake_store()
        raw = _make_gh_response("   ")  # whitespace only
        with patch("utils.knowledge.kb_ingester._run_gh", return_value=raw):
            count, new_keys = download_and_embed(entries, "owner/pueo-kb", store)
        assert count == 0
        assert new_keys == set()

    def test_skips_on_fetch_failure(self):
        entries = [_entry(id="rb3", sha256="h3", type="runbook", integrations=["all"])]
        store = self._fake_store()
        with patch(
            "utils.knowledge.kb_ingester._run_gh",
            side_effect=KbIngestError("network"),
        ):
            count, _ = download_and_embed(entries, "owner/pueo-kb", store)
        assert count == 0

    def test_skips_on_upsert_failure(self):
        entries = [_entry(id="rb4", sha256="h4", type="runbook", integrations=["all"])]
        store = self._fake_store()
        store.upsert.side_effect = RuntimeError("chroma error")
        raw = _make_gh_response("some content")
        with patch("utils.knowledge.kb_ingester._run_gh", return_value=raw):
            count, _ = download_and_embed(entries, "owner/pueo-kb", store)
        assert count == 0

    def test_gap_goes_to_strategies(self):
        entries = [_entry(id="g1", sha256="h5", type="gap", integrations=["all"])]
        store = self._fake_store()
        raw = _make_gh_response("gap content")
        with patch("utils.knowledge.kb_ingester._run_gh", return_value=raw):
            download_and_embed(entries, "owner/pueo-kb", store)
        call_args = store.upsert.call_args
        assert call_args[0][0] == "strategies"

    def test_ha_version_propagated_to_metadata(self):
        entry = _entry(
            id="ver1",
            sha256="hv1",
            type="runbook",
            integrations=["all"],
            ha_version_min="2026.1",
            ha_version_max="2026.9",
        )
        store = self._fake_store()
        raw = _make_gh_response("version-gated runbook")
        with patch("utils.knowledge.kb_ingester._run_gh", return_value=raw):
            download_and_embed([entry], "owner/pueo-kb", store)
        call_kwargs = store.upsert.call_args
        metadata = call_kwargs.kwargs["metadatas"][0]
        assert metadata["ha_version_min"] == "2026.1"
        assert metadata["ha_version_max"] == "2026.9"

    def test_ha_version_omitted_when_none(self):
        entry = _entry(id="ver2", sha256="hv2", type="runbook", integrations=["all"])
        store = self._fake_store()
        raw = _make_gh_response("no version runbook")
        with patch("utils.knowledge.kb_ingester._run_gh", return_value=raw):
            download_and_embed([entry], "owner/pueo-kb", store)
        call_kwargs = store.upsert.call_args
        metadata = call_kwargs.kwargs["metadatas"][0]
        assert "ha_version_min" not in metadata
        assert "ha_version_max" not in metadata

    def test_ingest_key_in_returned_keys(self):
        """Returned keys encode id:sha256:state for state-change detection."""
        entry = _entry(
            id="rb_key",
            sha256="hkey",
            type="runbook",
            integrations=["all"],
            state="validated",
        )
        store = self._fake_store()
        raw = _make_gh_response("runbook body")
        with patch("utils.knowledge.kb_ingester._run_gh", return_value=raw):
            _, new_keys = download_and_embed([entry], "owner/pueo-kb", store)
        assert "rb_key:hkey:validated" in new_keys


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


# ── load_kb_manifest_ids ──────────────────────────────────────────────────────


class TestLoadKbManifestIds:
    def test_empty_cache_returns_empty_set(self, tmp_path):
        assert load_kb_manifest_ids(str(tmp_path)) == set()

    def test_returns_saved_ids(self, tmp_path):
        save_sync_state(str(tmp_path), {"all_manifest_ids": ["a", "b"]})
        ids = load_kb_manifest_ids(str(tmp_path))
        assert ids == {"a", "b"}

    def test_missing_key_returns_empty_set(self, tmp_path):
        save_sync_state(str(tmp_path), {"ingested_sha256s": ["x"]})
        assert load_kb_manifest_ids(str(tmp_path)) == set()


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
        # Pre-save the key in the new format
        save_sync_state(str(tmp_path), {"ingested_keys": ["rb:s2:seed"]})
        manifest_raw = _make_gh_response(
            json.dumps(
                [
                    {
                        "id": "rb",
                        "type": "runbook",
                        "path": "p.md",
                        "sha256": "s2",
                        "state": "seed",
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

    def test_state_change_triggers_reingest(self, tmp_path):
        """An entry already ingested as candidate is re-ingested when it becomes validated."""
        store = MagicMock()
        save_sync_state(str(tmp_path), {"ingested_keys": ["rb:s_promo:candidate"]})
        manifest_raw = _make_gh_response(
            json.dumps(
                [
                    {
                        "id": "rb",
                        "type": "runbook",
                        "path": "p.md",
                        "sha256": "s_promo",
                        "state": "validated",  # promoted
                        "integrations": ["all"],
                        "tags": [],
                        "quality_score": 0.9,
                    }
                ]
            )
        )
        file_raw = _make_gh_response("# Runbook body")
        call_count: dict[str, int] = {"n": 0}

        def _gh(args: Any, timeout: int = 60) -> str:
            call_count["n"] += 1
            return manifest_raw if call_count["n"] == 1 else file_raw

        with patch("utils.knowledge.kb_ingester._run_gh", side_effect=_gh):
            count = run_kb_sync(
                "owner/kb", str(tmp_path), store, integration_profile=[]
            )
        assert count == 1

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
                        "state": "seed",
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
        assert "rb:s3:seed" in state["ingested_keys"]

    def test_saves_all_manifest_ids_to_state(self, tmp_path):
        """run_kb_sync saves all manifest entry ids (not just newly ingested) to state."""
        store = MagicMock()
        manifest_raw = _make_gh_response(
            json.dumps(
                [
                    {
                        "id": "rb1",
                        "type": "runbook",
                        "path": "p.md",
                        "sha256": "s10",
                        "integrations": ["all"],
                        "tags": [],
                        "quality_score": 0.9,
                    },
                    {
                        "id": "rb2",
                        "type": "runbook",
                        "path": "q.md",
                        "sha256": "s11",
                        "integrations": ["zha"],  # filtered by empty profile
                        "tags": [],
                        "quality_score": 0.9,
                    },
                ]
            )
        )
        file_raw = _make_gh_response("# Body")
        call_count: dict[str, int] = {"n": 0}

        def _gh(args: Any, timeout: int = 60) -> str:
            call_count["n"] += 1
            return manifest_raw if call_count["n"] == 1 else file_raw

        with patch("utils.knowledge.kb_ingester._run_gh", side_effect=_gh):
            run_kb_sync("owner/kb", str(tmp_path), store, integration_profile=[])

        state = load_sync_state(str(tmp_path))
        # Both ids should be saved even though rb2 was filtered out
        assert set(state["all_manifest_ids"]) == {"rb1", "rb2"}

    def test_local_seed_mirror_skipped(self, tmp_path):
        """Entry with source_seed matching a local seed is not embedded."""
        store = MagicMock()
        manifest_raw = _make_gh_response(
            json.dumps(
                [
                    {
                        "id": "seed_update_analysis",
                        "type": "runbook",
                        "path": "runbooks/seed_update_analysis.md",
                        "sha256": "sM",
                        "integrations": ["all"],
                        "tags": [],
                        "quality_score": 0.9,
                        "source_seed": "seed_update_analysis.md",
                    }
                ]
            )
        )
        with patch("utils.knowledge.kb_ingester._run_gh", return_value=manifest_raw):
            count = run_kb_sync(
                "owner/kb",
                str(tmp_path),
                store,
                integration_profile=[],
                local_seed_filenames=frozenset({"seed_update_analysis.md"}),
            )
        assert count == 0
        store.upsert.assert_not_called()
