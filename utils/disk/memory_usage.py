"""Per-container RAM breakdown for the HA host via 'ha * stats --raw-json' over SSH."""

import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from interfaces import SSHClientProtocol
from utils.core.logging import get_logger

log = get_logger("memory_usage")

_SLUG_RE = re.compile(r"^[a-z0-9_\-]+$")
_MARKER_RE = re.compile(r"@@([A-Za-z0-9_:.\-]+)\n?")

_SYSTEM_COMPONENTS = [
    ("sys", "core"),
    ("sys", "supervisor"),
    ("sys", "dns"),
    ("sys", "audio"),
    ("sys", "multicast"),
    ("sys", "observer"),
    ("sys", "cli"),
]

_SYSTEM_DISPLAY_NAMES = {
    "core": "HA Core",
    "supervisor": "Supervisor",
    "dns": "DNS",
    "audio": "Audio",
    "multicast": "Multicast",
    "observer": "Observer",
    "cli": "CLI",
}


@dataclass
class MemoryConsumer:
    kind: str  # "sys" | "app"
    id: str
    name: str
    used_mb: float
    limit_mb: Optional[float]
    pct_host: float


@dataclass
class MemoryBreakdown:
    total_mb: float
    available_mb: float
    consumers: list = field(default_factory=list)  # list[MemoryConsumer]
    other_mb: float = 0.0
    stopped_apps: list = field(default_factory=list)  # list[str] slugs
    fetched_at: float = 0.0
    errors: list = field(default_factory=list)  # list[str] component ids


async def _fetch_started_apps(
    ssh: SSHClientProtocol,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Return ([(slug, display_name),...], [stopped_slug,...]) from ha apps list."""
    _, stdout, _ = await ssh.run("ha apps list --raw-json", check=False)
    try:
        data = json.loads(stdout)
        addons = data.get("data", {}).get("addons", [])
    except Exception:
        return [], []
    started: list[tuple[str, str]] = []
    stopped: list[str] = []
    for a in addons:
        slug = a.get("slug", "")
        if not slug or not _SLUG_RE.match(slug):
            continue
        name = a.get("name", slug)
        if a.get("state") == "started":
            started.append((slug, name))
        else:
            stopped.append(slug)
    return started, stopped


def split_marker_sections(out: str) -> dict[str, str]:
    """Split @@-marker-delimited SSH output into a {key: content} dict.

    Uses a regex split so markers that are not preceded by a newline (because
    ``ha … --raw-json`` emits no trailing newline) are still found.
    """
    parts = _MARKER_RE.split(out)
    # parts[0]  — text before the first marker (discard)
    # then alternating: key, content, key, content, ...
    sections: dict[str, str] = {}
    i = 1
    while i < len(parts):
        key = parts[i]
        content = parts[i + 1].strip() if i + 1 < len(parts) else ""
        sections[key] = content
        i += 2
    return sections


def _parse_stats_json(raw: str) -> Optional[dict]:
    """Parse 'ha * stats --raw-json' output; return the inner data dict or None."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj.get("data", obj)
    except Exception:
        return None


def _stats_to_consumer(
    kind: str,
    component_id: str,
    name: str,
    data: dict,
    total_mb: float,
) -> Optional[MemoryConsumer]:
    """Convert a stats data dict to a MemoryConsumer; None if memory_usage is absent."""
    usage_bytes = data.get("memory_usage")
    if usage_bytes is None:
        return None
    used_mb = round(usage_bytes / (1024 * 1024), 1)
    limit_bytes = data.get("memory_limit")
    limit_mb = round(limit_bytes / (1024 * 1024), 1) if limit_bytes else None
    pct_host = round(100.0 * used_mb / total_mb, 1) if total_mb > 0 else 0.0
    return MemoryConsumer(
        kind=kind,
        id=component_id,
        name=name,
        used_mb=used_mb,
        limit_mb=limit_mb,
        pct_host=pct_host,
    )


def _parse_batch_output(
    batch_out: str,
    started_apps: list[tuple[str, str]],
    total_mb: float,
    expected_keys: "set[str] | None" = None,
) -> tuple[list[MemoryConsumer], list[str]]:
    """Parse the @@ marker-delimited stats output into consumers and error ids."""
    app_name_map = {slug: name for slug, name in started_apps}
    sections = split_marker_sections(batch_out)

    consumers: list[MemoryConsumer] = []
    errors: list[str] = []

    for key, raw in sections.items():
        if ":" not in key:
            continue
        kind, cid = key.split(":", 1)
        data = _parse_stats_json(raw)
        if data is None:
            if raw:
                log.warning("memory_stats_parse_error", component=cid, raw=raw[:200])
                errors.append(cid)
            continue
        name = (
            _SYSTEM_DISPLAY_NAMES.get(cid, cid)
            if kind == "sys"
            else app_name_map.get(cid, cid)
        )
        consumer = _stats_to_consumer(kind, cid, name, data, total_mb)
        if consumer is None:
            log.warning("memory_stats_missing_field", component=cid)
            errors.append(cid)
            continue
        consumers.append(consumer)

    # Report any requested component that produced no section at all
    if expected_keys:
        found = set(sections.keys())
        for key in expected_keys:
            if key not in found:
                _, cid = key.split(":", 1) if ":" in key else ("", key)
                errors.append(cid)

    return consumers, errors


async def fetch_memory_breakdown(ssh: SSHClientProtocol) -> MemoryBreakdown:
    """Fetch per-container RAM breakdown from HA over SSH in two round trips."""
    from utils.disk.resource import _parse_meminfo

    _, meminfo_out, _ = await ssh.run("cat /proc/meminfo", check=False)
    available_mb, total_mb = _parse_meminfo(meminfo_out)

    started_apps, stopped_apps = await _fetch_started_apps(ssh)

    parts: list[str] = []
    expected_keys: set[str] = set()
    for kind, cid in _SYSTEM_COMPONENTS:
        key = f"{kind}:{cid}"
        expected_keys.add(key)
        parts.append(f"echo; echo '@@{key}'; ha {cid} stats --raw-json 2>/dev/null")
    for slug, _ in started_apps:
        key = f"app:{slug}"
        expected_keys.add(key)
        parts.append(
            f"echo; echo '@@{key}'; ha apps stats {slug} --raw-json 2>/dev/null"
        )

    if not parts:
        return MemoryBreakdown(
            total_mb=round(total_mb, 1),
            available_mb=round(available_mb, 1),
            stopped_apps=stopped_apps,
            fetched_at=time.time(),
        )

    _, batch_out, _ = await ssh.run("; ".join(parts), check=False)
    consumers, errors = _parse_batch_output(
        batch_out, started_apps, total_mb, expected_keys
    )

    used_mb = total_mb - available_mb
    sum_containers = sum(c.used_mb for c in consumers)
    other_mb = max(0.0, round(used_mb - sum_containers, 1))

    return MemoryBreakdown(
        total_mb=round(total_mb, 1),
        available_mb=round(available_mb, 1),
        consumers=consumers,
        other_mb=other_mb,
        stopped_apps=stopped_apps,
        fetched_at=time.time(),
        errors=errors,
    )


# Module-global cache — updated by ResourcePoller after each successful fetch.
_last_memory_breakdown: Optional[MemoryBreakdown] = None


def update_memory_breakdown(breakdown: MemoryBreakdown) -> None:
    """Store the latest memory breakdown so the dashboard can read it without an SSH call."""
    global _last_memory_breakdown
    _last_memory_breakdown = breakdown


def get_memory_breakdown() -> Optional[MemoryBreakdown]:
    """Return the last cached memory breakdown, or None if not yet fetched."""
    return _last_memory_breakdown
