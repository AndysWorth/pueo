"""Guard the auto-loaded Claude Code memory files against the 40k-char limit.

Claude Code warns every session when a memory file (CLAUDE.md or a file it
imports with ``@path``) exceeds 40,000 chars. The guard sits below that so the
warning never reappears (#1109).
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LIMIT = 38_000
_IMPORT_RE = re.compile(r"^@(\S+)$", re.MULTILINE)


def _imports() -> list[str]:
    return _IMPORT_RE.findall((REPO / "CLAUDE.md").read_text(encoding="utf-8"))


def _memory_files() -> list[str]:
    return ["CLAUDE.md", "tests/CLAUDE.md", *_imports()]


class TestMemoryFileSize:
    @pytest.mark.parametrize("rel_path", _memory_files())
    def test_memory_file_under_limit(self, rel_path: str) -> None:
        path = REPO / rel_path
        assert path.is_file(), f"{rel_path} is imported but missing"
        size = len(path.read_text(encoding="utf-8"))
        assert size <= LIMIT, (
            f"{rel_path} is {size:,} chars (limit {LIMIT:,}). It loads into "
            "every Claude Code session: move how-it-works detail into the ADR "
            "or a docs/<subsystem>.md and leave a one-line pointer."
        )

    def test_import_regex_finds_key_patterns(self) -> None:
        assert "docs/key-patterns.md" in _imports()
