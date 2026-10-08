"""Tests for utils/knowledge/kb_contributor.py."""

import asyncio
import hashlib
import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
import yaml

from utils.knowledge.kb_contributor import (
    ContributionFile,
    KbContributeError,
    _is_federatable,
    _runbook_to_markdown,
    _submit_blocking,
    build_evidence,
    compute_kb_id,
    prepare_contribution_batch,
    submit_batch,
)


# ── compute_kb_id ─────────────────────────────────────────────────────────────


class TestComputeKbId:
    def test_format(self):
        kb_id = compute_kb_id("repair:zha:device_offline")
        assert kb_id.startswith("rb_")
        assert len(kb_id) == 3 + 12

    def test_deterministic(self):
        sig = "log:homeassistant.core:servicenotfound"
        assert compute_kb_id(sig) == compute_kb_id(sig)

    def test_different_signatures_give_different_ids(self):
        assert compute_kb_id("repair:zha:x") != compute_kb_id("repair:zha:y")

    def test_matches_sha256_prefix(self):
        sig = "repair:zha:device_offline"
        expected = "rb_" + hashlib.sha256(sig.encode()).hexdigest()[:12]
        assert compute_kb_id(sig) == expected


# ── _is_federatable ───────────────────────────────────────────────────────────


class TestIsFederatable:
    def test_valid_signature(self):
        assert _is_federatable("repair:zha:device_offline") is True

    def test_chat_signature_excluded(self):
        assert _is_federatable("chat:unclassified") is False
        assert _is_federatable("chat:repair:zha:x") is False

    def test_none_excluded(self):
        assert _is_federatable(None) is False

    def test_empty_excluded(self):
        assert _is_federatable("") is False


# ── ContributionFile dataclass ────────────────────────────────────────────────


class TestContributionFile:
    def test_fields(self):
        cf = ContributionFile(
            filename="runbooks/test.md",
            content="# body",
            item_id="rb-1",
            item_type="runbook",
        )
        assert cf.filename == "runbooks/test.md"
        assert cf.item_type == "runbook"


# ── _validate_repo ────────────────────────────────────────────────────────────


class TestValidateRepoContributor:
    def test_valid(self):
        from utils.knowledge.kb_contributor import _validate_repo

        _validate_repo("owner/pueo-kb")  # should not raise

    def test_empty_raises(self):
        from utils.knowledge.kb_contributor import _validate_repo

        with pytest.raises(KbContributeError):
            _validate_repo("")

    def test_no_slash_raises(self):
        from utils.knowledge.kb_contributor import _validate_repo

        with pytest.raises(KbContributeError):
            _validate_repo("noslash")


# ── _runbook_to_markdown ──────────────────────────────────────────────────────


class TestRunbookToMarkdown:
    def test_produces_valid_frontmatter(self):
        rb = {
            "id": "rb-001",
            "title": "Test Runbook",
            "trigger_pattern": "error.*yaml",
            "signature": "repair:yaml:invalid",
            "runbook_state": "validated",
            "approach": "Check the YAML syntax.",
            "contributed_at": "2026-09-04T12:00:00",
        }
        md = _runbook_to_markdown(rb, "Check the YAML syntax.")
        assert md.startswith("---\n")
        assert "---\n\n" in md
        parts = md.split("---\n\n", 1)
        fm = yaml.safe_load(parts[0].lstrip("---\n"))
        # id is now the kb_id derived from signature
        assert fm["id"] == compute_kb_id("repair:yaml:invalid")
        assert fm["type"] == "runbook"
        assert fm["state"] == "validated"
        assert fm["signature"] == "repair:yaml:invalid"
        body = parts[1]
        assert "Check the YAML syntax." in body

    def test_approach_text_overrides_dict_approach(self):
        rb = {
            "id": "rb-002",
            "title": "T",
            "trigger_pattern": "p",
            "signature": "repair:config:syntax",
            "approach": "original text",
            "contributed_at": "2026-09-04",
        }
        md = _runbook_to_markdown(rb, "anonymized text")
        assert "anonymized text" in md
        assert "original text" not in md

    def test_includes_tags_and_integrations(self):
        rb = {
            "id": "rb-002",
            "title": "T",
            "trigger_pattern": "p",
            "signature": "repair:config:tags",
            "approach": "a",
            "tags": ["ha_config"],
            "integrations": ["zha"],
            "contributed_at": "2026-09-04",
        }
        md = _runbook_to_markdown(rb, "a")
        assert "ha_config" in md
        assert "zha" in md

    def test_missing_optional_keys_ok(self):
        rb = {"id": "rb-003", "approach": "fix it"}
        md = _runbook_to_markdown(rb, "fix it")
        assert "---" in md
        assert "fix it" in md

    def test_title_and_trigger_in_body_not_frontmatter(self):
        rb = {
            "id": "rb-004",
            "title": "Config YAML Error",
            "trigger_pattern": "yaml.*error",
            "signature": "repair:config:yaml",
            "runbook_state": "validated",
            "approach": "Check syntax.",
        }
        md = _runbook_to_markdown(rb, "Check syntax.")
        parts = md.split("---\n\n", 1)
        fm = yaml.safe_load(parts[0].lstrip("---\n"))
        # title and trigger_pattern are NOT in frontmatter
        assert "title" not in fm
        assert "trigger_pattern" not in fm
        # but they appear in the body
        assert "Config YAML Error" in parts[1]
        assert "yaml.*error" in parts[1]

    def test_candidate_state(self):
        rb = {
            "id": "rb-005",
            "signature": "repair:zha:device",
            "runbook_state": "candidate",
            "state": "candidate",
            "approach": "a",
        }
        md = _runbook_to_markdown(rb, "a")
        fm = yaml.safe_load(md.split("---\n\n", 1)[0].lstrip("---\n"))
        assert fm["state"] == "candidate"


# ── prepare_contribution_batch ────────────────────────────────────────────────


class TestPrepareContributionBatch:
    def _rb(
        self, sig: str = "repair:zha:device_offline", state: str = "validated"
    ) -> dict:
        return {
            "id": "rb-001",
            "title": "Test",
            "trigger_pattern": "zha.*offline",
            "approach": "a",
            "signature": sig,
            "runbook_state": state,
        }

    def test_runbook_creates_md_file(self):
        batch = prepare_contribution_batch([self._rb()])
        assert len(batch) == 1
        assert batch[0].item_type == "runbook"
        assert batch[0].filename.startswith("runbooks/rb_")
        assert batch[0].filename.endswith(".md")

    def test_filename_uses_kb_id(self):
        sig = "repair:zha:device_offline"
        batch = prepare_contribution_batch([self._rb(sig)])
        expected_kb_id = compute_kb_id(sig)
        assert batch[0].filename == f"runbooks/{expected_kb_id}.md"

    def test_chat_signature_skipped(self):
        rb = self._rb(sig="chat:unclassified")
        batch = prepare_contribution_batch([rb])
        assert batch == []

    def test_missing_signature_skipped(self):
        rb = {"id": "rb-x", "approach": "a"}
        batch = prepare_contribution_batch([rb])
        assert batch == []

    def test_candidates_included_with_state(self):
        candidate = self._rb(sig="repair:zigbee:missing", state="candidate")
        batch = prepare_contribution_batch([], candidates=[candidate])
        assert len(batch) == 1
        fm = yaml.safe_load(batch[0].content.split("---\n\n", 1)[0].lstrip("---\n"))
        assert fm["state"] == "candidate"

    def test_candidates_chat_sig_skipped(self):
        rb = self._rb(sig="chat:repair:x", state="candidate")
        batch = prepare_contribution_batch([], candidates=[rb])
        assert batch == []

    def test_gap_creates_yaml_file(self):
        gap = {"id": "gap-001", "description": "g"}
        batch = prepare_contribution_batch([], gap_reports=[gap])
        assert len(batch) == 1
        assert batch[0].item_type == "gap"
        assert batch[0].filename.startswith("gaps/")

    def test_mixed_batch(self):
        rb = self._rb()
        gap = {"id": "g-001"}
        candidate = self._rb(sig="repair:zha:join", state="candidate")
        batch = prepare_contribution_batch(
            [rb], gap_reports=[gap], candidates=[candidate]
        )
        types = {f.item_type for f in batch}
        assert types == {"runbook", "gap"}
        assert len(batch) == 3

    def test_empty_inputs_return_empty(self):
        batch = prepare_contribution_batch([])
        assert batch == []

    def test_gap_content_is_valid_yaml(self):
        gap = {"id": "g-002", "trigger": "ha_log", "symptoms": ["err1", "err2"]}
        batch = prepare_contribution_batch([], gap_reports=[gap])
        parsed = yaml.safe_load(batch[0].content)
        assert parsed["trigger"] == "ha_log"
        assert "err1" in parsed["symptoms"]

    def test_runbook_approach_anonymized(self):
        rb = dict(self._rb())
        rb["approach"] = "Check host 192.168.1.10 for issues."
        batch = prepare_contribution_batch([rb])
        content = batch[0].content
        assert "192.168.1.10" not in content
        assert "<host_" in content

    def test_gap_approach_anonymized(self):
        gap = {
            "id": "g-anon",
            "approach": "Tried connecting to 10.0.0.1 but failed.",
        }
        batch = prepare_contribution_batch([], gap_reports=[gap])
        parsed = yaml.safe_load(batch[0].content)
        assert "10.0.0.1" not in parsed.get("approach", "")
        assert "<host_" in parsed.get("approach", "")

    def test_title_and_trigger_anonymized_in_body(self):
        rb = dict(self._rb())
        rb["title"] = "Fix host 10.0.0.5 device offline"
        rb["trigger_pattern"] = "host 10.0.0.5 unavailable"
        batch = prepare_contribution_batch([rb])
        assert "10.0.0.5" not in batch[0].content


# ── build_evidence ────────────────────────────────────────────────────────────


class TestBuildEvidence:
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE runbook_usage ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "strategy_id TEXT NOT NULL, "
            "episode_id TEXT, "
            "signature TEXT, "
            "outcome TEXT NOT NULL, "
            "created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
            ")"
        )
        return conn

    def test_returns_none_when_no_rows(self):
        conn = self._conn()
        result = build_evidence(conn, "inst-1", "0.1.0")
        assert result is None

    def test_returns_none_when_only_chat_sigs(self):
        conn = self._conn()
        conn.execute(
            "INSERT INTO runbook_usage (strategy_id, signature, outcome) "
            "VALUES ('rb1', 'chat:unclassified', 'success')"
        )
        result = build_evidence(conn, "inst-1", "0.1.0")
        assert result is None

    def test_builds_evidence_file(self):
        conn = self._conn()
        sig = "repair:zha:device_offline"
        conn.execute(
            "INSERT INTO runbook_usage (strategy_id, signature, outcome, episode_id) "
            "VALUES ('rb1', ?, 'success', 'ep-1')",
            (sig,),
        )
        conn.execute(
            "INSERT INTO runbook_usage (strategy_id, signature, outcome, episode_id) "
            "VALUES ('rb1', ?, 'failure', 'ep-2')",
            (sig,),
        )
        result = build_evidence(conn, "inst-abc", "0.1.0")
        assert result is not None
        assert result.item_type == "evidence"
        assert result.filename == "evidence/inst-abc.json"
        payload = json.loads(result.content)
        assert payload["instance_id"] == "inst-abc"
        assert payload["pueo_version"] == "0.1.0"
        kb_id = compute_kb_id(sig)
        assert kb_id in payload["runbooks"]
        stats = payload["runbooks"][kb_id]
        assert stats["successes"] == 1
        assert stats["failures"] == 1
        assert stats["signature"] == sig
        assert "ep-1" in stats["episodes"]
        assert "ep-2" in stats["episodes"]

    def test_deduplicates_episode_ids(self):
        conn = self._conn()
        sig = "repair:zha:device_offline"
        for _ in range(3):
            conn.execute(
                "INSERT INTO runbook_usage (strategy_id, signature, outcome, episode_id) "
                "VALUES ('rb1', ?, 'success', 'ep-same')",
                (sig,),
            )
        result = build_evidence(conn, "inst-1", "0.1.0")
        payload = json.loads(result.content)
        kb_id = compute_kb_id(sig)
        assert payload["runbooks"][kb_id]["episodes"] == ["ep-same"]

    def test_sha256_from_runbook_files(self):
        conn = self._conn()
        sig = "repair:config:yaml"
        conn.execute(
            "INSERT INTO runbook_usage (strategy_id, signature, outcome) "
            "VALUES ('rb1', ?, 'success')",
            (sig,),
        )
        kb_id = compute_kb_id(sig)
        fake_content = "---\nid: rb_x\n---\n\nApproach text\n"
        expected_sha = hashlib.sha256(fake_content.encode()).hexdigest()
        runbook_files = [
            ContributionFile(
                filename=f"runbooks/{kb_id}.md",
                content=fake_content,
                item_id=kb_id,
                item_type="runbook",
            )
        ]
        result = build_evidence(conn, "inst-1", "0.1.0", runbook_files)
        payload = json.loads(result.content)
        assert payload["runbooks"][kb_id]["sha256"] == expected_sha

    def test_multiple_signatures_aggregated_separately(self):
        conn = self._conn()
        sig1 = "repair:zha:x"
        sig2 = "repair:config:y"
        conn.execute(
            "INSERT INTO runbook_usage (strategy_id, signature, outcome) "
            "VALUES ('rb1', ?, 'success')",
            (sig1,),
        )
        conn.execute(
            "INSERT INTO runbook_usage (strategy_id, signature, outcome) "
            "VALUES ('rb2', ?, 'failure')",
            (sig2,),
        )
        result = build_evidence(conn, "inst-1", "0.1.0")
        payload = json.loads(result.content)
        assert compute_kb_id(sig1) in payload["runbooks"]
        assert compute_kb_id(sig2) in payload["runbooks"]


# ── submit_batch ──────────────────────────────────────────────────────────────


class TestSubmitBatch:
    def test_empty_batch_raises(self):
        with pytest.raises(KbContributeError):
            asyncio.run(submit_batch([], "owner/pueo-kb"))

    def test_invalid_repo_raises(self):
        batch = [ContributionFile("f.md", "c", "id", "runbook")]
        with pytest.raises(KbContributeError):
            asyncio.run(submit_batch(batch, ""))

    def test_calls_submit_blocking(self):
        batch = [ContributionFile("runbooks/rb.md", "# content", "rb-1", "runbook")]
        with patch(
            "utils.knowledge.kb_contributor._submit_blocking",
            return_value="https://github.com/owner/pueo-kb/pull/1",
        ) as mock_submit:
            result = asyncio.run(submit_batch(batch, "owner/pueo-kb"))
        assert result == "https://github.com/owner/pueo-kb/pull/1"
        mock_submit.assert_called_once_with(batch, "owner/pueo-kb", "contribute")


# ── _submit_blocking ──────────────────────────────────────────────────────────


class TestSubmitBlocking:
    def _make_batch(self, count: int = 1) -> list[ContributionFile]:
        return [
            ContributionFile(
                f"runbooks/rb{i}.md", f"# content {i}", f"rb-{i}", "runbook"
            )
            for i in range(count)
        ]

    def test_clones_and_creates_pr(self, tmp_path):
        batch = self._make_batch(1)
        run_calls: list[list[str]] = []

        def fake_run(cmd, cwd=None, timeout=60):
            run_calls.append(cmd)
            if cmd[0] == "gh" and "pr" in cmd and "create" in cmd:
                return "https://github.com/owner/pueo-kb/pull/42"
            return ""

        with patch("utils.knowledge.kb_contributor._run", side_effect=fake_run):
            with patch("tempfile.mkdtemp", return_value=str(tmp_path)):
                with patch("shutil.rmtree"):
                    result = _submit_blocking(batch, "owner/pueo-kb", "contribute")

        assert result == "https://github.com/owner/pueo-kb/pull/42"
        clone_calls = [c for c in run_calls if "repo" in c and "clone" in c]
        assert len(clone_calls) == 1
        commit_calls = [c for c in run_calls if c[:2] == ["git", "commit"]]
        assert len(commit_calls) == 1
        pr_calls = [c for c in run_calls if "pr" in c and "create" in c]
        assert len(pr_calls) == 1

    def test_pr_body_lists_files(self, tmp_path):
        batch = [
            ContributionFile("runbooks/rb1.md", "# body", "rb-1", "runbook"),
            ContributionFile("cases/c1.yaml", "id: c1", "c-1", "case"),
        ]
        run_calls: list[list[str]] = []

        def fake_run(cmd, cwd=None, timeout=60):
            run_calls.append(cmd)
            if "create" in cmd:
                return "https://github.com/owner/pueo-kb/pull/99"
            return ""

        with patch("utils.knowledge.kb_contributor._run", side_effect=fake_run):
            with patch("tempfile.mkdtemp", return_value=str(tmp_path)):
                with patch("shutil.rmtree"):
                    _submit_blocking(batch, "owner/pueo-kb", "contribute")

        pr_call = next(c for c in run_calls if "create" in c)
        body_idx = pr_call.index("--body") + 1
        body = pr_call[body_idx]
        assert "runbooks/rb1.md" in body
        assert "cases/c1.yaml" in body

    def test_cleanup_on_failure(self, tmp_path):
        batch = self._make_batch(1)
        cleanup_called = {"n": 0}

        def fake_run(cmd, cwd=None, timeout=60):
            raise KbContributeError("clone failed")

        with patch("utils.knowledge.kb_contributor._run", side_effect=fake_run):
            with patch("tempfile.mkdtemp", return_value=str(tmp_path)):
                with patch(
                    "shutil.rmtree",
                    side_effect=lambda p, **kw: cleanup_called.__setitem__(
                        "n", cleanup_called["n"] + 1
                    ),
                ):
                    with pytest.raises(KbContributeError):
                        _submit_blocking(batch, "owner/pueo-kb", "contribute")

        assert cleanup_called["n"] == 1


# ── config keys ───────────────────────────────────────────────────────────────


class TestKbContributorConfigKeys:
    """Smoke tests that the new config keys exist and have expected defaults."""

    def test_pueo_kb_repo_default_empty(self):
        from config import PUEO_KB_REPO

        assert isinstance(PUEO_KB_REPO, str)

    def test_kb_sync_interval_hours_default(self):
        from config import KB_SYNC_INTERVAL_HOURS

        assert KB_SYNC_INTERVAL_HOURS == 168

    def test_kb_sync_cache_dir_is_str(self):
        from config import KB_SYNC_CACHE_DIR

        assert isinstance(KB_SYNC_CACHE_DIR, str)
        assert len(KB_SYNC_CACHE_DIR) > 0
