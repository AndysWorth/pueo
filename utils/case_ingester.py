"""Ingest merged community repair episodes from pueo-cases into ChromaDB (item 81)."""

from __future__ import annotations

import base64
import json
import re
import subprocess  # nosec B404 — fixed gh commands; repo path validated before use
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import yaml

if TYPE_CHECKING:
    from interfaces import KnowledgeStoreClientProtocol

_SAFE_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_STATE_FILE = "state.json"


class CaseIngestError(Exception):
    pass


def _validate_repo(repo: str) -> None:
    if not repo or not _SAFE_REPO.match(repo):
        raise CaseIngestError(
            f"Invalid FEDERATED_CASES_REPO: {repo!r}. "
            "Must be 'owner/repo' (e.g. 'myorg/pueo-cases')."
        )


def _run_gh(args: list[str], timeout: int = 60) -> str:
    result = (
        subprocess.run(  # nosec B603 — cmd is always a hardcoded list, never user-built
            ["gh"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    )
    combined = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise CaseIngestError(f"gh command failed: {combined}")
    return result.stdout.strip()


def _parse_iso(dt_str: str) -> float:
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00")).timestamp()


def _list_merged_prs(repo: str, since_ts: Optional[float] = None) -> list[dict]:
    """Return merged PRs from repo that were merged after since_ts."""
    raw = _run_gh(
        [
            "api",
            f"repos/{repo}/pulls",
            "--field",
            "state=closed",
            "--field",
            "sort=updated",
            "--field",
            "direction=desc",
            "--field",
            "per_page=100",
            "--paginate",
        ],
        timeout=90,
    )
    try:
        prs = json.loads(raw)
    except json.JSONDecodeError:
        return []

    result = []
    for pr in prs:
        merged_at = pr.get("merged_at")
        if not merged_at:
            continue
        merged_ts = _parse_iso(merged_at)
        if since_ts is not None and merged_ts <= since_ts:
            continue
        result.append({"number": pr["number"], "merged_ts": merged_ts})
    return result


def _get_pr_episode_files(repo: str, pr_number: int) -> list[str]:
    """Return filenames of episode YAML files changed in this PR."""
    raw = _run_gh(["api", f"repos/{repo}/pulls/{pr_number}/files"], timeout=30)
    try:
        files = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [
        f["filename"]
        for f in files
        if isinstance(f, dict)
        and f.get("filename", "").startswith("episodes/")
        and f.get("filename", "").endswith(".yaml")
    ]


def _fetch_file_content(repo: str, filename: str) -> str:
    """Fetch a file's text content from the repo via gh api."""
    raw = _run_gh(["api", f"repos/{repo}/contents/{filename}"], timeout=30)
    data = json.loads(raw)
    if data.get("encoding") == "base64":
        # GitHub wraps long base64 lines with newlines — strip them
        return base64.b64decode(data["content"].replace("\n", "")).decode("utf-8")
    return data.get("content", "")


def _chunk_episode(
    record: dict, pr_number: int, ingest_date: str
) -> Optional[tuple[str, str, dict]]:
    """Build (chunk_id, text, metadata) from one episode record.

    Returns None if there is not enough content to embed.
    """
    episode_id = str(record.get("id", ""))
    trigger = str(record.get("trigger", "unknown"))
    symptoms: list = record.get("symptoms") or []
    hypothesis_chain: list = record.get("hypothesis_chain") or []
    fix_applied: str = record.get("fix_applied") or ""
    description: str = record.get("description") or ""

    parts: list[str] = []
    if description:
        parts.append(f"Description: {description}")
    parts.append(f"Trigger: {trigger}")
    if symptoms:
        parts.append("Symptoms:\n" + "\n".join(f"- {s}" for s in symptoms))
    if hypothesis_chain:
        parts.append("Hypothesis:\n" + "\n".join(f"- {h}" for h in hypothesis_chain))
    if fix_applied:
        parts.append(f"Fix applied:\n{fix_applied}")

    text = "\n\n".join(parts).strip()
    if not text:
        return None

    id_slug = episode_id[:12] if episode_id else "unknown"
    chunk_id = f"community_pr{pr_number}_{id_slug}"
    metadata: dict = {
        "source_pr": str(pr_number),
        "ingest_date": ingest_date,
        "trigger_type": trigger,
        "collection": "community_cases",
    }
    return chunk_id, text, metadata


def load_ingest_state(cache_dir: str) -> dict:
    state_file = Path(cache_dir) / _STATE_FILE
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_ingest_state(cache_dir: str, state: dict) -> None:
    path = Path(cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    (path / _STATE_FILE).write_text(json.dumps(state, indent=2), encoding="utf-8")


def ingest_community_cases(
    repo: str,
    cache_dir: str,
    knowledge_store: "KnowledgeStoreClientProtocol",
) -> int:
    """Pull merged PRs from repo, embed episode YAML files, upsert into community_cases.

    Tracks last-ingest timestamp in cache_dir/state.json so repeated runs are idempotent.
    Returns the total number of episode records ingested.
    """
    _validate_repo(repo)

    state = load_ingest_state(cache_dir)
    since_ts: Optional[float] = state.get("last_ingest_ts")

    prs = _list_merged_prs(repo, since_ts)
    if not prs:
        return 0

    ingest_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    max_merged_ts = since_ts or 0.0
    total = 0

    for pr in prs:
        pr_number: int = pr["number"]
        max_merged_ts = max(max_merged_ts, pr["merged_ts"])

        try:
            filenames = _get_pr_episode_files(repo, pr_number)
        except CaseIngestError:
            continue

        for filename in filenames:
            try:
                content = _fetch_file_content(repo, filename)
                records = yaml.safe_load(content)
                if not isinstance(records, list):
                    records = [records]
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    result = _chunk_episode(record, pr_number, ingest_date)
                    if result is None:
                        continue
                    chunk_id, text, metadata = result
                    knowledge_store.upsert(
                        "community_cases",
                        ids=[chunk_id],
                        documents=[text],
                        metadatas=[metadata],
                    )
                    total += 1
            except Exception:  # nosec B110 — skip malformed/inaccessible files
                pass

    state["last_ingest_ts"] = max_merged_ts
    save_ingest_state(cache_dir, state)

    return total
