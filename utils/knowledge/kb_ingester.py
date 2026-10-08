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
    state: str = "seed"  # seed | candidate | validated | flagged
    community_stats: dict = field(
        default_factory=dict
    )  # {successes, failures, instances}
    tags: list[str] = field(default_factory=list)
    integrations: list[str] = field(default_factory=lambda: ["all"])
    ha_version_min: Optional[str] = None
    ha_version_max: Optional[str] = None
    quality_score: float = 0.0
    added_at: str = ""
    source_seed: str = ""  # local seed filename this entry mirrors (if any)


def _ingest_key(entry: ManifestEntry) -> str:
    """Stable key for tracking whether an entry has been ingested at a given state."""
    return f"{entry.id}:{entry.sha256}:{entry.state}"


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
        raw_stats = item.get("community_stats") or {}
        entries.append(
            ManifestEntry(
                id=str(item.get("id", "")),
                type=str(item.get("type", "runbook")),
                path=str(item.get("path", "")),
                sha256=str(item.get("sha256", "")),
                state=str(item.get("state") or "seed"),
                community_stats={
                    "successes": int(raw_stats.get("successes") or 0),
                    "failures": int(raw_stats.get("failures") or 0),
                    "instances": int(raw_stats.get("instances") or 0),
                },
                tags=list(item.get("tags") or []),
                integrations=list(item.get("integrations") or ["all"]),
                ha_version_min=item.get("ha_version_min"),
                ha_version_max=item.get("ha_version_max"),
                quality_score=float(item.get("quality_score") or 0.0),
                added_at=str(item.get("added_at") or ""),
                source_seed=str(item.get("source_seed") or ""),
            )
        )
    return entries


def select_relevant_entries(
    entries: list[ManifestEntry],
    integration_profile: list[str],
    ingested_keys: set[str],
    local_seed_filenames: Optional[frozenset[str]] = None,
) -> list[ManifestEntry]:
    """Filter entries relevant to this installation.

    Keeps entries where integrations == ["all"] or intersects with
    integration_profile, and whose ingest key (id:sha256:state) has not been
    ingested yet.  Skips flagged entries and entries that mirror a local seed
    prompt (by source_seed match or id-slug match).
    """
    profile_set = {s.lower() for s in integration_profile}
    seeds = local_seed_filenames or frozenset()
    relevant = []
    for entry in entries:
        if entry.state == "flagged":
            continue
        if entry.sha256 and _ingest_key(entry) in ingested_keys:
            continue
        # Skip entries that duplicate a local seed prompt
        if entry.source_seed and entry.source_seed in seeds:
            continue
        # Fallback: match by id slug (e.g. entry.id == "seed_update_analysis" matches
        # local seed filename "seed_update_analysis.md")
        if not entry.source_seed and seeds:
            entry_slug = entry.id.replace("-", "_")
            if any(entry_slug == fn.removesuffix(".md") for fn in seeds):
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

    Returns (count_embedded, new_ingest_keys).  Each key is id:sha256:state so
    callers can detect future state promotions and re-ingest.
    """
    embedded = 0
    new_keys: set[str] = set()
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
            "kb_state": entry.state,
            "sha256": entry.sha256,
            "tags": ",".join(entry.tags),
            "collection": collection,
        }
        if entry.community_stats:
            metadata["community_successes"] = int(
                entry.community_stats.get("successes", 0)
            )
            metadata["community_failures"] = int(
                entry.community_stats.get("failures", 0)
            )
            metadata["community_instances"] = int(
                entry.community_stats.get("instances", 0)
            )
        if entry.ha_version_min:
            metadata["ha_version_min"] = entry.ha_version_min
        if entry.ha_version_max:
            metadata["ha_version_max"] = entry.ha_version_max
        try:
            knowledge_store.upsert(
                collection,
                ids=[chunk_id],
                documents=[text],
                metadatas=[metadata],
            )
            embedded += 1
            if entry.sha256:
                new_keys.add(_ingest_key(entry))
        except Exception:  # nosec B110 — skip embedding failures
            pass
    return embedded, new_keys


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


def load_kb_manifest_ids(cache_dir: str) -> set[str]:
    """Return the set of entry ids from the last successful kb sync (or empty set)."""
    state = load_sync_state(cache_dir)
    return set(state.get("all_manifest_ids", []))


def run_kb_sync(
    repo: str,
    cache_dir: str,
    knowledge_store: "KnowledgeStoreClientProtocol",
    integration_profile: Optional[list[str]] = None,
    local_seed_filenames: Optional[frozenset[str]] = None,
) -> int:
    """Pull manifest, select relevant new/updated entries, download and embed.

    Tracks ingest keys (id:sha256:state) in cache_dir/kb_sync_state.json.
    An entry is re-ingested when its state changes (e.g. candidate → validated),
    because the key encodes state.  Also saves all_manifest_ids so
    prune_strategies can exclude valid kb entries.
    Returns count of newly embedded entries.
    """
    _validate_repo(repo)
    state = load_sync_state(cache_dir)
    ingested_keys: set[str] = set(state.get("ingested_keys", []))

    entries = fetch_manifest(repo)
    all_manifest_ids = {e.id for e in entries}
    relevant = select_relevant_entries(
        entries,
        integration_profile or [],
        ingested_keys,
        local_seed_filenames,
    )
    if not relevant:
        state["all_manifest_ids"] = sorted(all_manifest_ids)
        save_sync_state(cache_dir, state)
        return 0

    count, new_keys = download_and_embed(relevant, repo, knowledge_store)
    ingested_keys |= new_keys
    state["ingested_keys"] = sorted(ingested_keys)
    state["all_manifest_ids"] = sorted(all_manifest_ids)
    save_sync_state(cache_dir, state)
    return count
