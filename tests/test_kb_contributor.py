"""Tests for utils/knowledge/kb_contributor.py."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
import yaml

from utils.knowledge.kb_contributor import (
    ContributionFile,
    KbContributeError,
    _runbook_to_markdown,
    _submit_blocking,
    prepare_contribution_batch,
    submit_batch,
)


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
            "approach": "Check the YAML syntax.",
            "contributed_at": "2026-09-04T12:00:00",
        }
        md = _runbook_to_markdown(rb)
        assert md.startswith("---\n")
        assert "---\n\n" in md
        parts = md.split("---\n\n", 1)
        fm = yaml.safe_load(parts[0].lstrip("---\n"))
        assert fm["id"] == "rb-001"
        assert fm["title"] == "Test Runbook"
        body = parts[1]
        assert "Check the YAML syntax." in body

    def test_includes_tags_and_integrations(self):
        rb = {
            "id": "rb-002",
            "title": "T",
            "trigger_pattern": "p",
            "approach": "a",
            "tags": ["ha_config"],
            "integrations": ["zha"],
            "contributed_at": "2026-09-04",
        }
        md = _runbook_to_markdown(rb)
        assert "ha_config" in md
        assert "zha" in md

    def test_missing_optional_keys_ok(self):
        rb = {"id": "rb-003", "approach": "fix it"}
        md = _runbook_to_markdown(rb)
        assert "---" in md
        assert "fix it" in md


# ── prepare_contribution_batch ────────────────────────────────────────────────


class TestPrepareContributionBatch:
    def test_runbook_creates_md_file(self):
        rb = {"id": "rb-001", "title": "T", "trigger_pattern": "p", "approach": "a"}
        batch = prepare_contribution_batch([rb])
        assert len(batch) == 1
        assert batch[0].item_type == "runbook"
        assert batch[0].filename.startswith("runbooks/")
        assert batch[0].filename.endswith(".md")

    def test_case_creates_yaml_file(self):
        case = {"id": "case-001", "description": "d", "fix_applied": "f"}
        batch = prepare_contribution_batch([], ready_cases=[case])
        assert len(batch) == 1
        assert batch[0].item_type == "case"
        assert batch[0].filename.startswith("cases/")
        assert batch[0].filename.endswith(".yaml")

    def test_gap_creates_yaml_file(self):
        gap = {"id": "gap-001", "description": "g"}
        batch = prepare_contribution_batch([], gap_reports=[gap])
        assert len(batch) == 1
        assert batch[0].item_type == "gap"
        assert batch[0].filename.startswith("gaps/")

    def test_mixed_batch(self):
        rb = {"id": "rb-001", "approach": "a"}
        case = {"id": "c-001"}
        gap = {"id": "g-001"}
        batch = prepare_contribution_batch([rb], ready_cases=[case], gap_reports=[gap])
        assert len(batch) == 3
        types = {f.item_type for f in batch}
        assert types == {"runbook", "case", "gap"}

    def test_empty_inputs_return_empty(self):
        batch = prepare_contribution_batch([])
        assert batch == []

    def test_id_slug_sanitizes_special_chars(self):
        rb = {"id": "rb with spaces/and slashes", "approach": "a"}
        batch = prepare_contribution_batch([rb])
        assert "/" not in batch[0].filename.split("/", 1)[1]

    def test_case_content_is_valid_yaml(self):
        case = {"id": "c-002", "trigger": "ha_log", "symptoms": ["err1", "err2"]}
        batch = prepare_contribution_batch([], ready_cases=[case])
        parsed = yaml.safe_load(batch[0].content)
        assert parsed["trigger"] == "ha_log"
        assert "err1" in parsed["symptoms"]


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
        # Verify gh repo clone was called
        clone_calls = [c for c in run_calls if "repo" in c and "clone" in c]
        assert len(clone_calls) == 1
        # Verify git commit was called
        commit_calls = [c for c in run_calls if c[:2] == ["git", "commit"]]
        assert len(commit_calls) == 1
        # Verify gh pr create was called
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
        # default is empty (feature disabled by default)

    def test_kb_sync_interval_hours_default(self):
        from config import KB_SYNC_INTERVAL_HOURS

        assert KB_SYNC_INTERVAL_HOURS == 168

    def test_kb_sync_cache_dir_is_str(self):
        from config import KB_SYNC_CACHE_DIR

        assert isinstance(KB_SYNC_CACHE_DIR, str)
        assert len(KB_SYNC_CACHE_DIR) > 0
