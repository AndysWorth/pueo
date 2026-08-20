"""Submit anonymized repair episodes to the community pueo-cases GitHub repo (item 80)."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess  # nosec B404 — fixed gh/git commands; repo path validated before use
import tempfile
from pathlib import Path
from typing import Optional


_SAFE_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class CaseSubmitError(Exception):
    pass


def _validate_repo(repo: str) -> None:
    if not repo or not _SAFE_REPO.match(repo):
        raise CaseSubmitError(
            f"Invalid FEDERATED_CASES_REPO: {repo!r}. "
            "Must be 'owner/repo' (e.g. 'myorg/pueo-cases')."
        )


def _run(cmd: list[str], cwd: Optional[str] = None, timeout: int = 60) -> str:
    """Run a shell command synchronously and return combined stdout+stderr."""
    result = (
        subprocess.run(  # nosec B603 — cmd is always a hardcoded list, never user-built
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
        )
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise CaseSubmitError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n{output}"
        )
    return output


async def submit_episode(
    episode_id: str,
    yaml_content: str,
    cases_repo: str,
    pr_title: str,
    pr_body: str,
) -> str:
    """
    Clone cases_repo, commit episode YAML on a new branch, and open a PR.

    Returns the PR URL on success.  Raises CaseSubmitError on any failure.
    The caller is responsible for writing the PR URL back to the DB.

    Requires: gh CLI authenticated; cases_repo write access (or fork already cloned).
    """
    _validate_repo(cases_repo)

    return await asyncio.to_thread(
        _submit_blocking, episode_id, yaml_content, cases_repo, pr_title, pr_body
    )


def _submit_blocking(
    episode_id: str,
    yaml_content: str,
    cases_repo: str,
    pr_title: str,
    pr_body: str,
) -> str:
    tmpdir = tempfile.mkdtemp(prefix="pueo-cases-")
    try:
        # Clone the target repo (shallow, main branch only)
        _run(["gh", "repo", "clone", cases_repo, tmpdir, "--", "--depth=1"], timeout=90)

        branch = f"submit/{episode_id[:8]}"
        _run(["git", "checkout", "-b", branch], cwd=tmpdir)

        # Write episode YAML into episodes/ subdir
        episodes_dir = Path(tmpdir) / "episodes"
        episodes_dir.mkdir(exist_ok=True)
        episode_file = episodes_dir / f"{episode_id}.yaml"
        episode_file.write_text(yaml_content, encoding="utf-8")

        _run(["git", "add", str(episode_file)], cwd=tmpdir)
        _run(
            [
                "git",
                "commit",
                "-m",
                f"Add repair episode {episode_id[:8]}",
            ],
            cwd=tmpdir,
        )
        _run(["git", "push", "origin", branch], cwd=tmpdir, timeout=90)

        pr_url = _run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                cases_repo,
                "--base",
                "main",
                "--head",
                branch,
                "--title",
                pr_title,
                "--body",
                pr_body,
            ],
            cwd=tmpdir,
        )
        # gh pr create prints the URL on stdout
        return pr_url.strip()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
