"""Federated KB ingestion: selective pull from pueo-kb GitHub repo."""

from __future__ import annotations

import base64
import json
import re
import subprocess  # nosec B404 — fixed gh commands; repo validated before use
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from interfaces import KnowledgeStoreClientProtocol

_SAFE_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_STATE_FILE = "kb_sync_state.json"


class KbIngestError(Exception):
    pass


@dataclass
class ManifestEntry:
    id: str
    type: str  # "runbook" | "gap"
    path: str
    sha256: str
    tags: list[str] = field(default_factory=list)
    integrations: list[str] = field(default_factory=lambda: ["all"])
    ha_version_min: Optional[str] = None
    ha_version_max: Optional[str] = None
    quality_score: float = 0.0
    added_at: str = ""


def _validate_repo(repo: str) -> None:
    if not repo or not _SAFE_REPO.match(repo):
        raise KbIngestError(f"Invalid PUEO_KB_REPO: {repo!r}. Must be 'owner/repo'.")


def _run_gh(args: list[str], timeout: int = 60) -> str:
    result = subprocess.run(  # nosec B603 — cmd is always a hardcoded list
        ["gh"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    combined = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise KbIngestError(f"gh command failed: {combined}")
    return result.stdout.strip()


def _decode_gh_content(raw_json: str) -> str:
    """Decode base64-encoded content from a GitHub API contents response."""
    data = json.loads(raw_json)
    if data.get("encoding") == "base64":
        return base64.b64decode(data["content"].replace("\n", "")).decode("utf-8")
    return data.get("content", "")


def fetch_manifest(repo: str) -> list[ManifestEntry]:
    """Download MANIFEST.json from repo, return parsed entries."""
    _validate_repo(repo)
    raw = _run_gh(["api", f"repos/{repo}/contents/MANIFEST.json"], timeout=30)
    content = _decode_gh_content(raw)
    entries_raw = json.loads(content)
    if not isinstance(entries_raw, list):
        raise KbIngestError("MANIFEST.json must be a JSON array.")
    entries = []
    for item in entries_raw:
        if not isinstance(item, dict):
            continue
        entries.append(
            ManifestEntry(
                id=str(item.get("id", "")),
                type=str(item.get("type", "runbook")),
                path=str(item.get("path", "")),
                sha256=str(item.get("sha256", "")),
                tags=list(item.get("tags") or []),
                integrations=list(item.get("integrations") or ["all"]),
                ha_version_min=item.get("ha_version_min"),
                ha_version_max=item.get("ha_version_max"),
                quality_score=float(item.get("quality_score") or 0.0),
                added_at=str(item.get("added_at") or ""),
            )
        )
    return entries


def select_relevant_entries(
    entries: list[ManifestEntry],
    integration_profile: list[str],
    ingested_sha256s: set[str],
) -> list[ManifestEntry]:
    """Filter entries relevant to this installation.

    Keeps entries where integrations == ["all"] or intersects with
    integration_profile, and whose sha256 has not been ingested yet.
    """
    profile_set = {s.lower() for s in integration_profile}
    relevant = []
    for entry in entries:
        if entry.sha256 and entry.sha256 in ingested_sha256s:
            continue
        integrations = [i.lower() for i in entry.integrations]
        if "all" in integrations or bool(set(integrations) & profile_set):
            relevant.append(entry)
    return relevant


def _fetch_file_content(repo: str, path: str) -> str:
    """Fetch a file's text content from the repo via gh api."""
    raw = _run_gh(["api", f"repos/{repo}/contents/{path}"], timeout=30)
    return _decode_gh_content(raw)


def _collection_for_type(entry_type: str) -> str:
    return "strategies"


def download_and_embed(
    entries: list[ManifestEntry],
    repo: str,
    knowledge_store: "KnowledgeStoreClientProtocol",
) -> tuple[int, set[str]]:
    """Download selected entries and upsert into ChromaDB.

    Returns (count_embedded, new_sha256s_ingested).
    """
    embedded = 0
    new_sha256s: set[str] = set()
    for entry in entries:
        try:
            content = _fetch_file_content(repo, entry.path)
        except Exception:  # nosec B112 — skip inaccessible files, continue loop
            continue
        text = content.strip()
        if not text:
            continue
        chunk_id = f"kb_{entry.id}"
        collection = _collection_for_type(entry.type)
        metadata: dict = {
            "source": "pueo_kb",
            "kb_id": entry.id,
            "kb_type": entry.type,
            "sha256": entry.sha256,
            "tags": ",".join(entry.tags),
            "collection": collection,
        }
        try:
            knowledge_store.upsert(
                collection,
                ids=[chunk_id],
                documents=[text],
                metadatas=[metadata],
            )
            embedded += 1
            if entry.sha256:
                new_sha256s.add(entry.sha256)
        except Exception:  # nosec B110 — skip embedding failures
            pass
    return embedded, new_sha256s


def load_sync_state(cache_dir: str) -> dict:
    state_file = Path(cache_dir) / _STATE_FILE
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_sync_state(cache_dir: str, state: dict) -> None:
    path = Path(cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    (path / _STATE_FILE).write_text(json.dumps(state, indent=2), encoding="utf-8")


def run_kb_sync(
    repo: str,
    cache_dir: str,
    knowledge_store: "KnowledgeStoreClientProtocol",
    integration_profile: Optional[list[str]] = None,
) -> int:
    """Pull manifest, select relevant new entries, download and embed.

    Tracks ingested sha256s in cache_dir/kb_sync_state.json so repeated
    runs are idempotent. Returns count of newly embedded entries.
    """
    _validate_repo(repo)
    state = load_sync_state(cache_dir)
    ingested_sha256s: set[str] = set(state.get("ingested_sha256s", []))

    entries = fetch_manifest(repo)
    relevant = select_relevant_entries(
        entries,
        integration_profile or [],
        ingested_sha256s,
    )
    if not relevant:
        return 0

    count, new_sha256s = download_and_embed(relevant, repo, knowledge_store)
    ingested_sha256s |= new_sha256s
    state["ingested_sha256s"] = sorted(ingested_sha256s)
    save_sync_state(cache_dir, state)
    return count
