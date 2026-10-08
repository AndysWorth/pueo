"""Federated KB contribution: submit reviewed runbooks/cases/gaps to pueo-kb."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess  # nosec B404 — fixed gh/git commands; repo validated before use
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
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
    item_type: str  # "runbook" | "case" | "gap" | "evidence"


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


def compute_kb_id(signature: str) -> str:
    """'rb_' + first 12 hex chars of SHA-256(signature)."""
    return "rb_" + hashlib.sha256(signature.encode()).hexdigest()[:12]


def _is_federatable(signature: Optional[str]) -> bool:
    """True if signature is present and not a chat: signature."""
    return bool(signature) and not (signature or "").startswith("chat:")


def _anonymize_text(text: str) -> str:
    from utils.repair.anonymizer import Anonymizer

    return Anonymizer().text(text)


def _runbook_to_markdown(runbook: dict, approach_text: str) -> str:
    """Serialize a runbook to markdown with pueo-kb frontmatter format."""
    signature = runbook.get("signature") or runbook.get("runbook_signature")
    kb_id = compute_kb_id(signature) if signature else str(runbook.get("id", "unknown"))
    state = runbook.get("runbook_state") or runbook.get("state", "candidate")

    frontmatter: dict = {
        "id": kb_id,
        "type": "runbook",
        "state": state,
    }
    if signature:
        frontmatter["signature"] = signature
    if runbook.get("tags"):
        frontmatter["tags"] = runbook["tags"]
    if runbook.get("integrations"):
        frontmatter["integrations"] = runbook["integrations"]
    if runbook.get("ha_version_min"):
        frontmatter["ha_version_min"] = runbook["ha_version_min"]
    if runbook.get("contributed_at"):
        frontmatter["contributed_at"] = runbook["contributed_at"]

    # Anonymize title and trigger_pattern before including in the body
    title_raw = runbook.get("title", "")
    title_anon = _anonymize_text(title_raw) if title_raw else ""
    trigger_raw = runbook.get("trigger_pattern", "")
    trigger_anon = _anonymize_text(trigger_raw) if trigger_raw else ""

    fm = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)
    body = ""
    if title_anon:
        body += f"# {title_anon}\n\n"
    if trigger_anon:
        body += f"Trigger: {trigger_anon}\n\n"
    body += approach_text + "\n"

    return f"---\n{fm}---\n\n{body}"


def prepare_contribution_batch(
    reviewed_runbooks: list[dict],
    gap_reports: Optional[list[dict]] = None,
    candidates: Optional[list[dict]] = None,
) -> list[ContributionFile]:
    """Assemble anonymized contribution files ready for submission.

    reviewed_runbooks: validated/seed runbooks (each dict must have at least
        'id', 'approach', and ideally 'signature').
    candidates: candidate runbooks to federate (same shape; state forced to
        'candidate').
    gap_reports: open gap dicts.
    Returns one ContributionFile per item (runbooks + candidates by kb_id, gaps
    by local id). Runbooks/candidates whose signature is missing or starts with
    'chat:' are skipped.
    """
    files: list[ContributionFile] = []

    def _add_runbook(rb: dict, forced_state: Optional[str] = None) -> None:
        signature = rb.get("signature") or rb.get("runbook_signature")
        if not _is_federatable(signature):
            return
        kb_id = compute_kb_id(signature)  # type: ignore[arg-type]
        slug = kb_id[:48]
        filename = f"runbooks/{slug}.md"
        approach_raw = rb.get("approach", "")
        approach_anon = _anonymize_text(approach_raw)
        rb_copy = dict(rb)
        if forced_state:
            rb_copy["state"] = forced_state
        content = _runbook_to_markdown(rb_copy, approach_anon)
        files.append(
            ContributionFile(
                filename=filename,
                content=content,
                item_id=kb_id,
                item_type="runbook",
            )
        )

    for rb in reviewed_runbooks:
        _add_runbook(rb)

    for rb in candidates or []:
        _add_runbook(rb, forced_state="candidate")

    for gap in gap_reports or []:
        gap_id = str(gap.get("id", "unknown"))
        slug = re.sub(r"[^A-Za-z0-9_-]", "_", gap_id)[:48]
        filename = f"gaps/{slug}.yaml"
        anon_gap = {
            k: _anonymize_text(v) if isinstance(v, str) else v for k, v in gap.items()
        }
        content = yaml.dump(
            anon_gap, default_flow_style=False, sort_keys=False, allow_unicode=True
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


def build_evidence(
    conn: sqlite3.Connection,
    instance_id: str,
    pueo_version: str,
    runbook_files: Optional[list[ContributionFile]] = None,
) -> Optional[ContributionFile]:
    """Build the evidence/instance_id.json file from local runbook_usage rows.

    Aggregates successes/failures/episodes per kb_id (computed from signature).
    Skips usage rows with no signature or a chat: signature.
    Returns None if there are no eligible usage rows.
    runbook_files: if provided, the sha256 of each runbook file is used for the
        evidence stats (keyed by kb_id derived from the file's item_id).
    """
    rows = conn.execute(
        "SELECT strategy_id, signature, outcome, episode_id "
        "FROM runbook_usage "
        "WHERE signature IS NOT NULL"
    ).fetchall()

    sha256_map: dict[str, str] = {}
    if runbook_files:
        for cf in runbook_files:
            if cf.item_type == "runbook":
                sha256_map[cf.item_id] = hashlib.sha256(cf.content.encode()).hexdigest()

    stats: dict[str, dict] = {}
    for strategy_id, signature, outcome, episode_id in rows:
        if not _is_federatable(signature):
            continue
        kb_id = compute_kb_id(signature)
        if kb_id not in stats:
            stats[kb_id] = {
                "sha256": sha256_map.get(kb_id, ""),
                "signature": signature,
                "successes": 0,
                "failures": 0,
                "episodes": [],
            }
        if outcome == "success":
            stats[kb_id]["successes"] += 1
        else:
            stats[kb_id]["failures"] += 1
        if episode_id and episode_id not in stats[kb_id]["episodes"]:
            stats[kb_id]["episodes"].append(episode_id)

    if not stats:
        return None

    payload = {
        "instance_id": instance_id,
        "pueo_version": pueo_version,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "runbooks": stats,
    }
    content = json.dumps(payload, indent=2) + "\n"
    return ContributionFile(
        filename=f"evidence/{instance_id}.json",
        content=content,
        item_id=instance_id,
        item_type="evidence",
    )


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
