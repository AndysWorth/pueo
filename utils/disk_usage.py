"""Per-path disk usage breakdown for the HA host — fetched via SSH du/sqlite3 commands."""

import asyncio
import json
import pathlib
import sqlite3 as _sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

from interfaces import SSHClientProtocol
from utils.core.logging import get_logger

log = get_logger("disk_usage")


@dataclass
class DiskItem:
    path: str
    name: str
    size_bytes: int
    size_human: str
    is_empty: bool
    pct_of_section: float


@dataclass
class DiskSection:
    title: str
    items: list = field(default_factory=list)
    total_bytes: int = 0
    total_human: str = "0 B"
    is_empty: bool = True


@dataclass
class DiskBreakdown:
    sections: list = field(default_factory=list)
    disk_used_gb: float = 0.0
    disk_total_gb: float = 0.0
    disk_free_gb: float = 0.0
    disk_used_pct: float = 0.0
    fetched_at: float = 0.0
    db_tables: Optional[list] = None  # list[tuple[str, int]]
    custom_components: Optional[list] = None  # list[DiskItem] — None if absent/empty
    container_images_estimated_gb: Optional[float] = None  # total_used minus visible
    orphaned_addons: Optional[list] = (
        None  # list[OrphanedAddonDir] — None if none found
    )


def _parse_size_to_bytes(size_str: str) -> int:
    """Convert du -sh output like '94.5M', '4.0K', '1.7G' to bytes (1024-based, busybox format)."""
    size_str = size_str.strip()
    if not size_str or size_str == "0":
        return 0
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    upper = size_str.upper()
    for suffix, mult in multipliers.items():
        if upper.endswith(suffix):
            try:
                return int(float(size_str[:-1]) * mult)
            except ValueError:
                return 0
    try:
        return int(size_str)
    except ValueError:
        return 0


def _bytes_to_human(size_bytes: int) -> str:
    if size_bytes >= 1024**3:
        return f"{size_bytes / 1024**3:.1f} GB"
    if size_bytes >= 1024**2:
        return f"{size_bytes / 1024**2:.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def _parse_du_output(output: str) -> dict:
    """Parse 'du -sh' output into a {path: size_bytes} dict."""
    result: dict = {}
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        # du output: "<size>\t<path>" on busybox/Linux; fall back to whitespace split
        if "\t" in line:
            size_str, path = line.split("\t", 1)
        else:
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            size_str, path = parts
        result[path.strip()] = _parse_size_to_bytes(size_str.strip())
    return result


async def _fetch_addon_names(ssh_client: SSHClientProtocol) -> dict:
    """Return {slug: display_name} via 'ha apps list --raw-json'; empty dict on any error."""
    _, stdout, _ = await ssh_client.run("ha apps list --raw-json", check=False)
    try:
        data = json.loads(stdout)
        addons = data.get("data", {}).get("addons", [])
        return {a["slug"]: a["name"] for a in addons if "slug" in a and "name" in a}
    except Exception:
        return {}


async def _fetch_db_tables(ssh_client: SSHClientProtocol) -> Optional[list]:
    """SFTP-pull home-assistant_v2.db and query table sizes locally via Python sqlite3."""
    remote_db = "/homeassistant/home-assistant_v2.db"
    with tempfile.NamedTemporaryFile(
        suffix=".db", prefix="pueo_ha_", delete=False
    ) as f:
        tmp = f.name
    try:
        await ssh_client.download_file(remote_db, tmp)
        con = _sqlite3.connect(tmp)
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT name, SUM(pgsize) as sz FROM dbstat "
                "GROUP BY name ORDER BY sz DESC LIMIT 15;"
            )
            rows = [(r[0], r[1]) for r in cur.fetchall()]
        finally:
            con.close()
        return rows if rows else None
    except Exception:
        return None
    finally:
        try:
            pathlib.Path(tmp).unlink()
        except Exception:  # nosec B110
            pass


async def _fetch_custom_components(ssh_client: SSHClientProtocol) -> Optional[list]:
    """Return per-component DiskItem list from /homeassistant/custom_components/*."""
    _, out, _ = await ssh_client.run(
        "du -sh /homeassistant/custom_components/* 2>/dev/null", check=False
    )
    path_sizes = _parse_du_output(out)
    if not path_sizes:
        return None
    total = sum(path_sizes.values())
    items = []
    for path, size_bytes in sorted(path_sizes.items(), key=lambda kv: -kv[1]):
        name = path.rstrip("/").rsplit("/", 1)[-1]
        pct = round(100.0 * size_bytes / total, 1) if total else 0.0
        items.append(
            DiskItem(
                path=path,
                name=name,
                size_bytes=size_bytes,
                size_human=_bytes_to_human(size_bytes),
                is_empty=size_bytes <= 8192,
                pct_of_section=pct,
            )
        )
    return items if items else None


def _build_section(
    title: str,
    path_sizes: dict,
    addon_names: Optional[dict] = None,
) -> DiskSection:
    sorted_paths = sorted(path_sizes.items(), key=lambda kv: -kv[1])
    section_total = sum(s for _, s in sorted_paths)

    items = []
    for path, size_bytes in sorted_paths:
        basename = path.rstrip("/").rsplit("/", 1)[-1]
        if addon_names is not None and path.startswith("/addon_configs/"):
            display_name = addon_names.get(basename, basename)
        else:
            display_name = basename
        pct = round(100.0 * size_bytes / section_total, 1) if section_total else 0.0
        items.append(
            DiskItem(
                path=path,
                name=display_name,
                size_bytes=size_bytes,
                size_human=_bytes_to_human(size_bytes),
                is_empty=size_bytes <= 8192,
                pct_of_section=pct,
            )
        )

    return DiskSection(
        title=title,
        items=items,
        total_bytes=section_total,
        total_human=_bytes_to_human(section_total),
        is_empty=not items or all(item.is_empty for item in items),
    )


async def fetch_disk_breakdown(ssh_client: SSHClientProtocol) -> DiskBreakdown:
    """Fetch per-path disk usage from the HA host and return a structured breakdown."""
    import yaml as _yaml

    # 1. Overall disk stats from ha host info
    _, host_info_out, _ = await ssh_client.run("ha host info", check=False)
    try:
        host_data = _yaml.safe_load(host_info_out)
        disk_free = float(host_data["disk_free"])
        disk_total = float(host_data["disk_total"])
        disk_used = float(host_data["disk_used"])
    except Exception:
        disk_free, disk_total, disk_used = 0.0, 0.0, 0.0
    disk_used_pct = round(100.0 * disk_used / disk_total, 1) if disk_total else 0.0

    # 2. Per-path sizes — single SSH round-trip
    du_cmd = (
        "du -sh /homeassistant/* /backup/* /addon_configs/*"
        " /share /media /ssl 2>/dev/null"
    )
    _, du_out, _ = await ssh_client.run(du_cmd, check=False)
    path_sizes = _parse_du_output(du_out)

    # 3. Addon slug → friendly name mapping; custom components; DB tables;
    #    orphaned addon dirs — run in parallel
    from utils.disk_recovery import scan_orphaned_addon_dirs

    addon_names, custom_components, db_tables, orphaned_addons_raw = (
        await asyncio.gather(
            _fetch_addon_names(ssh_client),
            _fetch_custom_components(ssh_client),
            _fetch_db_tables(ssh_client),
            scan_orphaned_addon_dirs(ssh_client),
        )
    )
    orphaned_addons = orphaned_addons_raw if orphaned_addons_raw else None

    # 4. Build the four user-actionable sections
    sections = [
        _build_section(
            "HA Config & Database",
            {p: s for p, s in path_sizes.items() if p.startswith("/homeassistant/")},
        ),
        _build_section(
            "Backups",
            {p: s for p, s in path_sizes.items() if p.startswith("/backup/")},
        ),
        _build_section(
            "Addon Data",
            {p: s for p, s in path_sizes.items() if p.startswith("/addon_configs/")},
            addon_names=addon_names,
        ),
        _build_section(
            "Shared Storage",
            {p: s for p, s in path_sizes.items() if p in ("/share", "/media", "/ssl")},
        ),
    ]

    # 5. Estimate container images + system OS as the unaccounted remainder
    if disk_total > 0:
        visible_gb = sum(s.total_bytes for s in sections) / 1024**3
        container_images_estimated_gb: Optional[float] = max(
            0.0, round(disk_used - visible_gb, 2)
        )
    else:
        container_images_estimated_gb = None

    return DiskBreakdown(
        sections=sections,
        disk_used_gb=disk_used,
        disk_total_gb=disk_total,
        disk_free_gb=disk_free,
        disk_used_pct=disk_used_pct,
        fetched_at=time.time(),
        db_tables=db_tables,
        custom_components=custom_components,
        container_images_estimated_gb=container_images_estimated_gb,
        orphaned_addons=orphaned_addons,
    )


def fetch_top_consumers_flat(breakdown: DiskBreakdown, n: int = 6) -> list:
    """Return the top N non-empty DiskItems across all sections, sorted by size descending.

    Each entry is a plain dict: {name, section, size_bytes, size_human}.
    """
    all_items = []
    for section in breakdown.sections:
        for item in section.items:
            if not item.is_empty:
                all_items.append(
                    {
                        "name": item.name,
                        "section": section.title,
                        "size_bytes": item.size_bytes,
                        "size_human": item.size_human,
                    }
                )
    all_items.sort(key=lambda x: -x["size_bytes"])
    return all_items[:n]


async def fetch_addon_persistent_sizes(
    ssh_client: SSHClientProtocol,
    addon_name_map: dict,
) -> list:
    """Fetch per-add-on persistent data sizes from /mnt/data/supervisor/addons/*.

    Returns a list of dicts sorted by size descending:
      {slug, name, size_bytes, size_human}
    Returns [] on any error — treat as best-effort at CRITICAL alert time.
    """
    try:
        _, out, _ = await ssh_client.run(
            "du -sh /mnt/data/supervisor/addons/* 2>/dev/null || true",
            check=False,
        )
        path_sizes = _parse_du_output(out)
        if not path_sizes:
            return []
        results = []
        for path, size_bytes in sorted(path_sizes.items(), key=lambda kv: -kv[1]):
            slug = path.rstrip("/").rsplit("/", 1)[-1]
            name = addon_name_map.get(slug, slug)
            results.append(
                {
                    "slug": slug,
                    "name": name,
                    "size_bytes": size_bytes,
                    "size_human": _bytes_to_human(size_bytes),
                }
            )
        return results
    except Exception:
        return []


_last_disk_breakdown: Optional[DiskBreakdown] = None


def update_disk_breakdown(breakdown: DiskBreakdown) -> None:
    global _last_disk_breakdown
    _last_disk_breakdown = breakdown


def get_disk_breakdown() -> Optional[DiskBreakdown]:
    return _last_disk_breakdown


class DiskUsagePoller:
    """Polls HA per-path disk breakdown on a fixed interval."""

    def __init__(
        self,
        ssh_client: SSHClientProtocol,
        interval_seconds: float,
    ) -> None:
        self._ssh = ssh_client
        self._interval = interval_seconds

    async def run(self) -> None:
        while True:
            try:
                breakdown = await fetch_disk_breakdown(self._ssh)
                update_disk_breakdown(breakdown)
                try:
                    from utils.supervisor import publish_event

                    publish_event(
                        {
                            "event_type": "disk_usage",
                            "fetched_at": breakdown.fetched_at,
                            "disk_used_pct": breakdown.disk_used_pct,
                        }
                    )
                except Exception:  # nosec B110 — SSE publish is best-effort
                    pass
                log.info(
                    "disk_usage_poll",
                    disk_used_pct=breakdown.disk_used_pct,
                    sections=len(breakdown.sections),
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("disk_usage_poll_failed", error=str(e))
            await asyncio.sleep(self._interval)
