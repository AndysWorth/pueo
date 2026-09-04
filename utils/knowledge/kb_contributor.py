"""Federated KB contribution: submit reviewed runbooks/cases/gaps to pueo-kb."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess  # nosec B404 — fixed gh/git commands; repo validated before use
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

_SAFE_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class KbContributeError(Exception):
    pass


@dataclass
class ContributionFile:
    filename: str
    content: str
    item_id: str
    item_type: str  # "runbook" | "case" | "gap"


def _validate_repo(repo: str) -> None:
    if not repo or not _SAFE_REPO.match(repo):
        raise KbContributeError(
            f"Invalid PUEO_KB_REPO: {repo!r}. Must be 'owner/repo'."
        )


def _run(cmd: list[str], cwd: Optional[str] = None, timeout: int = 60) -> str:
    result = subprocess.run(  # nosec B603 — cmd is always a hardcoded list
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise KbContributeError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n{output}"
        )
    return output


def _runbook_to_markdown(runbook: dict) -> str:
    """Serialize a runbook dict to markdown with YAML frontmatter."""
    frontmatter = {
        k: runbook[k]
        for k in ("id", "title", "trigger_pattern", "contributed_at")
        if k in runbook
    }
    if "tags" in runbook:
        frontmatter["tags"] = runbook["tags"]
    if "integrations" in runbook:
        frontmatter["integrations"] = runbook["integrations"]
    fm = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)
    approach = runbook.get("approach", "")
    return f"---\n{fm}---\n\n{approach}\n"


def prepare_contribution_batch(
    reviewed_runbooks: list[dict],
    ready_cases: Optional[list[dict]] = None,
    gap_reports: Optional[list[dict]] = None,
) -> list[ContributionFile]:
    """Assemble anonymized contribution files ready for submission.

    Each dict in reviewed_runbooks must have at least 'id' and 'approach' keys.
    Returns one ContributionFile per item.
    """
    files: list[ContributionFile] = []

    for rb in reviewed_runbooks:
        rb_id = str(rb.get("id", "unknown"))
        slug = re.sub(r"[^A-Za-z0-9_-]", "_", rb_id)[:48]
        filename = f"runbooks/{slug}.md"
        content = _runbook_to_markdown(rb)
        files.append(
            ContributionFile(
                filename=filename,
                content=content,
                item_id=rb_id,
                item_type="runbook",
            )
        )

    for case in ready_cases or []:
        case_id = str(case.get("id", "unknown"))
        slug = re.sub(r"[^A-Za-z0-9_-]", "_", case_id)[:48]
        filename = f"cases/{slug}.yaml"
        content = yaml.dump(
            case, default_flow_style=False, sort_keys=False, allow_unicode=True
        )
        files.append(
            ContributionFile(
                filename=filename,
                content=content,
                item_id=case_id,
                item_type="case",
            )
        )

    for gap in gap_reports or []:
        gap_id = str(gap.get("id", "unknown"))
        slug = re.sub(r"[^A-Za-z0-9_-]", "_", gap_id)[:48]
        filename = f"gaps/{slug}.yaml"
        content = yaml.dump(
            gap, default_flow_style=False, sort_keys=False, allow_unicode=True
        )
        files.append(
            ContributionFile(
                filename=filename,
                content=content,
                item_id=gap_id,
                item_type="gap",
            )
        )

    return files


async def submit_batch(
    batch: list[ContributionFile],
    repo: str,
    branch_prefix: str = "contribute",
) -> str:
    """Submit batch to pueo-kb repo via PR. Returns PR URL."""
    _validate_repo(repo)
    if not batch:
        raise KbContributeError("No files in contribution batch.")
    return await asyncio.to_thread(_submit_blocking, batch, repo, branch_prefix)


def _submit_blocking(
    batch: list[ContributionFile],
    repo: str,
    branch_prefix: str,
) -> str:
    from datetime import datetime, timezone

    tmpdir = tempfile.mkdtemp(prefix="pueo-kb-")
    try:
        _run(["gh", "repo", "clone", repo, tmpdir, "--", "--depth=1"], timeout=90)

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        branch = f"{branch_prefix}/{ts}"
        _run(["git", "checkout", "-b", branch], cwd=tmpdir)

        for cfile in batch:
            target = Path(tmpdir) / cfile.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(cfile.content, encoding="utf-8")
            _run(["git", "add", str(target)], cwd=tmpdir)

        type_counts: dict[str, int] = {}
        for cfile in batch:
            type_counts[cfile.item_type] = type_counts.get(cfile.item_type, 0) + 1
        parts = [f"{v} {k}(s)" for k, v in sorted(type_counts.items())]
        summary = ", ".join(parts)

        _run(
            ["git", "commit", "-m", f"Contribute {summary}"],
            cwd=tmpdir,
        )
        _run(["git", "push", "origin", branch], cwd=tmpdir, timeout=90)

        body_lines = [
            "Automated contribution from a Pueo instance.",
            "",
            f"Contains: {summary}",
            "",
            "Files:",
        ]
        body_lines += [f"- {f.filename}" for f in batch]

        pr_url = _run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                repo,
                "--base",
                "main",
                "--head",
                branch,
                "--title",
                f"Pueo contribution: {summary}",
                "--body",
                "\n".join(body_lines),
            ],
            cwd=tmpdir,
        )
        return pr_url.strip()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
