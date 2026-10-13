"""Host-level health indicators fetched via SSH: swap, eMMC wear, uptime, Supervisor resolution."""

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from interfaces import SSHClientProtocol
from utils.core.logging import get_logger

if TYPE_CHECKING:
    from utils.disk.resource import ResourceStatus

log = get_logger("host_health")


@dataclass
class ResolutionInfo:
    unsupported: list = field(default_factory=list)  # list[str]
    unhealthy: list = field(default_factory=list)  # list[str]
    issues: list = field(default_factory=list)  # list[dict]
    suggestions: list = field(default_factory=list)  # list[dict]


@dataclass
class HostHealth:
    swap_total_mb: float
    swap_used_mb: float
    disk_life_used_pct: Optional[float]  # None when the board has no eMMC
    boot_ts: Optional[float]  # seconds since epoch; None on parse failure
    uptime_seconds: Optional[float]  # None when boot_ts unavailable
    resolution: Optional[ResolutionInfo]
    fetched_at: float
    errors: list = field(default_factory=list)  # list[str] — which sections failed


def _parse_swap(meminfo: str) -> tuple[float, float]:
    """Return (swap_used_mb, swap_total_mb) parsed from /proc/meminfo lines."""
    swap_total_kb = 0
    swap_free_kb = 0
    for line in meminfo.splitlines():
        if line.startswith("SwapTotal:"):
            try:
                swap_total_kb = int(line.split()[1])
            except (IndexError, ValueError):
                pass
        elif line.startswith("SwapFree:"):
            try:
                swap_free_kb = int(line.split()[1])
            except (IndexError, ValueError):
                pass
    swap_total_mb = swap_total_kb / 1024.0
    swap_used_mb = max(0.0, (swap_total_kb - swap_free_kb) / 1024.0)
    return swap_used_mb, swap_total_mb


def _parse_host_info_json(raw: str) -> tuple[Optional[float], Optional[float]]:
    """Return (disk_life_used_pct, boot_ts_seconds) from 'ha host info --raw-json' output."""
    disk_life: Optional[float] = None
    boot_ts: Optional[float] = None
    try:
        obj = json.loads(raw.strip())
        data = obj.get("data", obj)

        raw_life = data.get("disk_life_time")
        if raw_life is not None:
            try:
                disk_life = float(raw_life)
            except (TypeError, ValueError):
                log.warning("host_health_disk_life_parse_error", raw=str(raw_life)[:80])

        # boot_timestamp is microseconds since epoch
        raw_boot = data.get("boot_timestamp")
        if raw_boot is not None:
            try:
                boot_ts = float(raw_boot) / 1_000_000.0
            except (TypeError, ValueError):
                log.warning("host_health_boot_ts_parse_error", raw=str(raw_boot)[:80])

    except Exception as exc:
        log.warning("host_health_host_info_parse_failed", error=str(exc)[:200])
    return disk_life, boot_ts


def _parse_resolution_json(raw: str) -> Optional[ResolutionInfo]:
    """Return ResolutionInfo from 'ha resolution info --raw-json' output, or None on failure."""
    try:
        obj = json.loads(raw.strip())
        data = obj.get("data", obj)
        return ResolutionInfo(
            unsupported=data.get("unsupported") or [],
            unhealthy=data.get("unhealthy") or [],
            issues=data.get("issues") or [],
            suggestions=data.get("suggestions") or [],
        )
    except Exception as exc:
        log.warning("host_health_resolution_parse_failed", error=str(exc)[:200])
        return None


async def fetch_host_health(ssh: SSHClientProtocol) -> HostHealth:
    """Fetch host health indicators in one batched SSH call."""
    from utils.disk.resource import _parse_meminfo

    parts = [
        "echo '@@host'; ha host info --raw-json 2>/dev/null",
        "echo '@@res'; ha resolution info --raw-json 2>/dev/null",
        "echo '@@mem'; cat /proc/meminfo",
    ]
    _, batch_out, _ = await ssh.run("; ".join(parts), check=False)

    # Split output by @@ markers
    sections: dict[str, str] = {}
    current_key: Optional[str] = None
    current_lines: list[str] = []
    for line in batch_out.splitlines():
        if line.startswith("@@"):
            if current_key is not None:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = line[2:]
            current_lines = []
        else:
            current_lines.append(line)
    if current_key is not None:
        sections[current_key] = "\n".join(current_lines).strip()

    errors: list[str] = []

    # Parse swap from /proc/meminfo
    meminfo_raw = sections.get("mem", "")
    if meminfo_raw:
        swap_used_mb, swap_total_mb = _parse_swap(meminfo_raw)
    else:
        swap_used_mb, swap_total_mb = 0.0, 0.0
        errors.append("mem")

    # Parse host info
    host_raw = sections.get("host", "")
    if host_raw:
        disk_life_used_pct, boot_ts = _parse_host_info_json(host_raw)
    else:
        disk_life_used_pct, boot_ts = None, None
        errors.append("host")

    # Compute uptime
    uptime_seconds: Optional[float] = None
    if boot_ts is not None:
        uptime_seconds = max(0.0, time.time() - boot_ts)

    # Parse resolution info
    res_raw = sections.get("res", "")
    if res_raw:
        resolution = _parse_resolution_json(res_raw)
        if resolution is None:
            errors.append("res")
    else:
        resolution = None
        errors.append("res")

    return HostHealth(
        swap_total_mb=round(swap_total_mb, 1),
        swap_used_mb=round(swap_used_mb, 1),
        disk_life_used_pct=disk_life_used_pct,
        boot_ts=boot_ts,
        uptime_seconds=round(uptime_seconds, 0) if uptime_seconds is not None else None,
        resolution=resolution,
        fetched_at=time.time(),
        errors=errors,
    )


# Module-global cache — updated by ResourcePoller after each successful fetch.
_last_host_health: Optional[HostHealth] = None


def update_host_health(health: HostHealth) -> None:
    """Store the latest host health so the dashboard can read it without an SSH call."""
    global _last_host_health
    _last_host_health = health


def get_host_health() -> Optional[HostHealth]:
    """Return the last cached host health, or None if not yet fetched."""
    return _last_host_health


# ---------------------------------------------------------------------------
# Host-health finding detection
# ---------------------------------------------------------------------------


@dataclass
class HostHealthFinding:
    """A detected host-health condition that may warrant an LLM investigation."""

    key: str  # unique dedup key within a poll, e.g. "swap_pressure"
    kind: str  # finding type: "swap_pressure", "memory_low", "disk_wear", etc.
    severity: str  # "WARN", "HIGH", "CRITICAL"
    evidence: dict = field(default_factory=dict)


def evaluate_host_health(
    health: HostHealth,
    status: "ResourceStatus",
    last_boot_ts: Optional[float],
    pueo_reboot_recent: bool,
    known_repair_uuids: "set[str]",
    swap_warn_pct: float,
    disk_life_warn_pct: float,
    mem_warn_mb: float,
) -> "list[HostHealthFinding]":
    """Pure function: derive the list of current host-health findings.

    All threshold comparisons happen here; no I/O.  The caller supplies
    the in-memory state (cached health + status) and the DB-derived
    context (last_boot_ts, pueo_reboot_recent, known_repair_uuids).
    """
    findings: list[HostHealthFinding] = []

    # memory_low — mirrors the existing mem_warn flag
    if status.mem_warn:
        findings.append(
            HostHealthFinding(
                key="memory_low",
                kind="memory_low",
                severity="WARN",
                evidence={
                    "mem_available_mb": round(status.mem_available_mb),
                    "mem_total_mb": round(status.mem_total_mb),
                    "threshold_mb": mem_warn_mb,
                },
            )
        )

    # swap_pressure — high swap AND low available memory (both conditions required)
    if health.swap_total_mb > 0:
        swap_pct = 100.0 * health.swap_used_mb / health.swap_total_mb
        if swap_pct >= swap_warn_pct and status.mem_available_mb < 2.0 * mem_warn_mb:
            findings.append(
                HostHealthFinding(
                    key="swap_pressure",
                    kind="swap_pressure",
                    severity="WARN",
                    evidence={
                        "swap_used_mb": round(health.swap_used_mb, 1),
                        "swap_total_mb": round(health.swap_total_mb, 1),
                        "swap_pct": round(swap_pct, 1),
                        "mem_available_mb": round(status.mem_available_mb),
                        "threshold_swap_pct": swap_warn_pct,
                    },
                )
            )

    # disk_wear — eMMC life approaching end of life
    if (
        health.disk_life_used_pct is not None
        and health.disk_life_used_pct >= disk_life_warn_pct
    ):
        severity = "CRITICAL" if health.disk_life_used_pct >= 90.0 else "WARN"
        findings.append(
            HostHealthFinding(
                key="disk_wear",
                kind="disk_wear",
                severity=severity,
                evidence={
                    "disk_life_used_pct": health.disk_life_used_pct,
                    "threshold_pct": disk_life_warn_pct,
                },
            )
        )

    # unexpected_reboot — boot_ts changed vs stored value without a Pueo-initiated restart
    if (
        health.boot_ts is not None
        and last_boot_ts is not None
        and abs(health.boot_ts - last_boot_ts) > 30.0  # 30 s tolerance for clock drift
        and not pueo_reboot_recent
    ):
        findings.append(
            HostHealthFinding(
                key="unexpected_reboot",
                kind="unexpected_reboot",
                severity="WARN",
                evidence={
                    "boot_ts": health.boot_ts,
                    "prev_boot_ts": last_boot_ts,
                    "uptime_seconds": health.uptime_seconds,
                },
            )
        )

    # supervisor_unhealthy / supervisor_unsupported
    if health.resolution is not None:
        for reason in health.resolution.unhealthy:
            reason_key = str(reason)[:64]
            findings.append(
                HostHealthFinding(
                    key=f"supervisor_unhealthy:{reason_key}",
                    kind="supervisor_unhealthy",
                    severity="HIGH",
                    evidence={"reason": reason_key},
                )
            )
        for reason in health.resolution.unsupported:
            reason_key = str(reason)[:64]
            findings.append(
                HostHealthFinding(
                    key=f"supervisor_unsupported:{reason_key}",
                    kind="supervisor_unsupported",
                    severity="WARN",
                    evidence={"reason": reason_key},
                )
            )

        # resolution_issue — skip any UUID already mirrored as a hassio HA Repair
        for issue in health.resolution.issues:
            uuid = str(issue.get("uuid", ""))
            if not uuid:
                continue
            if uuid in known_repair_uuids:
                continue
            issue_type = str(issue.get("type", ""))
            findings.append(
                HostHealthFinding(
                    key=f"resolution_issue:{uuid}",
                    kind="resolution_issue",
                    severity="WARN",
                    evidence={
                        "uuid": uuid,
                        "type": issue_type,
                        "context": issue.get("context"),
                        "reference": issue.get("reference"),
                    },
                )
            )

    return findings
