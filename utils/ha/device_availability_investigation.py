"""LLM-guided investigation for device-availability findings (offline devices)."""

import asyncio
import sqlite3
import time
from typing import Any, Optional

from utils.core.logging import get_logger
from utils.ha.device_availability import OfflineFinding, load_ha_start_ts

log = get_logger("device_availability_investigation")


def device_availability_knowledge_query(finding: OfflineFinding) -> str:
    """Targeted pre-inject query: domain plus 'device offline'."""
    if finding.ha_start_ts is not None:
        return "devices unavailable after Home Assistant restart update reboot"
    return f"device offline {finding.domain} unavailable".strip()


def _fmt_local(ts: float) -> str:
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(ts)))
    return f"{iso} local (epoch {int(ts)})"


def format_device_line(d: dict, with_integration: bool = False) -> str:
    """One context line per device: name, offline-since and its entity_ids."""
    since = d.get("offline_since")
    parts = [f"offline since {_fmt_local(since) if since is not None else 'unknown'}"]
    if d.get("entity_ids"):
        parts.append("entities: " + ", ".join(d["entity_ids"]))
    where = ""
    if with_integration:
        where = (
            f" [{d.get('domain') or 'unknown'}, entry {d.get('config_entry_id', '')}]"
        )
    return f"  - {d['name']}{where} ({'; '.join(parts)})"


# Timeline sources that explain a restart: Pueo-run updates and host reboots.
# Three entries — matches the "IN (?, ?, ?)" in restart_events().
_RESTART_EVENT_SOURCES = ("update_executor", "update_check", "resource")
_RESTART_LOOKBACK_S = 2 * 3600


def restart_events(db_path: str, ha_start_ts: float, limit: int = 6) -> list[str]:
    """Timeline lines from two hours before the HA start up to 15 min after it."""
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT ts, source, message FROM timeline_events"
                " WHERE ts BETWEEN ? AND ?"
                " AND source IN (?, ?, ?)"
                " ORDER BY ts DESC LIMIT ?",
                (
                    ha_start_ts - _RESTART_LOOKBACK_S,
                    ha_start_ts + 900,
                    *_RESTART_EVENT_SOURCES,
                    limit,
                ),
            ).fetchall()
    except Exception as exc:  # nosec B110
        log.debug("restart_events_fetch_failed", error=str(exc))
        return []
    return [
        f"  - {_fmt_local(ts)} [{src}] {str(msg)[:160]}"
        for ts, src, msg in reversed(rows)
    ]


def last_ha_start_line(db_path: str) -> str:
    """'Last HA start: …' context line, or '' when none is recorded.

    seed_device_offline.md asks the model whether the device went offline right
    after a restart, so per-integration sessions need the start time too (#993).
    """
    try:
        with sqlite3.connect(db_path) as conn:
            ts = load_ha_start_ts(conn)
    except Exception as exc:  # nosec B110
        log.debug("last_ha_start_fetch_failed", error=str(exc))
        return ""
    return f"Last HA start: {_fmt_local(ts)}\n" if ts is not None else ""


def build_post_restart_context(finding: OfflineFinding, db_path: str) -> str:
    """Initial context for a grouped post-restart sweep."""
    assert finding.ha_start_ts is not None  # nosec B101
    domains = sorted({d.get("domain") or "unknown" for d in finding.devices})
    events = restart_events(db_path, finding.ha_start_ts)
    event_block = (
        "\n".join(events)
        if events
        else "  (none recorded — the restart may have been manual, from the HA UI,"
        " or unplanned)"
    )
    device_lines = "\n".join(
        format_device_line(d, with_integration=True) for d in finding.devices
    )
    return (
        "Devices offline after a Home Assistant restart (post-restart sweep):\n"
        f"HA started: {_fmt_local(finding.ha_start_ts)}\n"
        f"Updates, reboots and host events around the restart:\n{event_block}\n"
        f"Integrations: {', '.join(domains)}\n"
        f"Offline devices ({len(finding.devices)}):\n{device_lines}\n"
        "Note: these devices were all found offline right after the restart. Devices"
        " that a restart exposes as unavailable were usually already switched off"
        " before it — HA just didn't show it until it reloaded. Investigate them as one"
        " group."
    )


async def build_presence_context(
    devices: list,  # list[dict] from OfflineFinding.devices
    netalertx_api_client: Optional[Any],  # Optional[NetAlertXAPIClient]
    ws_client: Optional[Any],  # Optional[HAWebSocketClientProtocol]
    ssh_client: Optional[Any] = None,  # Optional[SSHClientProtocol]
    db_path: Optional[str] = None,
) -> tuple:
    """Return (presence_text, unmapped_device_ids) for pre-injection into initial_context.

    presence_text is empty string when NetAlertX is not configured, has no token, or
    when any fetch fails.  unmapped_device_ids lists HA device IDs that could not be
    matched because they have no MAC/IP/host candidates.  Never raises.  Unmapped
    devices are reported as "unknown" rather than "absent" so the agent does not draw
    a wrong conclusion.
    """
    if netalertx_api_client is None or not getattr(
        netalertx_api_client, "has_token", False
    ):
        return "", []

    from utils.ha.network_identity import (
        candidates_for_device,
        load_config_entry_hosts,
        load_manual_links,
        match_netalertx,
    )

    try:
        reg_by_id: dict[str, dict] = {}
        if ws_client is not None:
            try:
                dev_reg = await ws_client.get_device_registry()
                for dev in dev_reg:
                    reg_by_id[dev["id"]] = dev
            except Exception:  # nosec B110
                pass

        host_by_entry: dict[str, str] = {}
        if ssh_client is not None:
            host_by_entry = await load_config_entry_hosts(ssh_client)

        manual_links: dict[str, str] = {}
        if db_path:
            import asyncio as _asyncio

            manual_links = await _asyncio.to_thread(load_manual_links, db_path)

        nax_devices = await netalertx_api_client.get_devices()

        lines: list = []
        unmapped_ids: list = []
        for dev in devices:
            device_id = dev.get("device_id", "")
            name = dev.get("name", device_id)
            registry_entry = reg_by_id.get(device_id, {})
            candidates = candidates_for_device(
                registry_entry, host_by_entry, manual_links
            )
            nax_dev, method = match_netalertx(candidates, nax_devices)

            if nax_dev is None:
                if method == "unmapped":
                    unmapped_ids.append(device_id)
                    lines.append(
                        f"  - {name}: unknown (no MAC/IP mapping for this device)"
                    )
                else:
                    # not_in_netalertx — we had candidates but none matched
                    best = next((v for k, v in candidates if k == "mac"), None) or next(
                        (v for _, v in candidates), "no identifier"
                    )
                    lines.append(f"  - {name}: not found in NetAlertX ({best})")
                continue

            present = bool(nax_dev.get("devPresentLastScan"))
            last_seen = (
                nax_dev.get("devLastSeen")
                or nax_dev.get("devLastConnection")
                or "unknown"
            )
            current_ip = nax_dev.get("devLastIP") or "unknown"
            nax_mac = (nax_dev.get("devMac") or "").lower()
            method_note = "" if method in ("mac", "manual") else f" matched by {method}"
            lines.append(
                f"  - {name}: {'present on LAN' if present else 'absent from LAN'}"
                f" (MAC {nax_mac}, IP {current_ip}, last_seen {last_seen}{method_note})"
            )

        if not lines:
            return "", unmapped_ids

        return "\n\nNetAlertX network presence:\n" + "\n".join(lines), unmapped_ids

    except Exception as exc:  # nosec B110
        log.debug("presence_context_fetch_failed", error=str(exc))
        return "", []


async def run_device_availability_investigation(
    finding: OfflineFinding,
    notifier: Any,  # NotifierProtocol
    db_path: str,
    llm_client: Optional[Any] = None,  # LLMClientProtocol
    knowledge_store: Optional[Any] = None,
    ha_rest_client: Optional[Any] = None,
    ssh_client: Optional[Any] = None,  # SSHClientProtocol
    ws_client: Optional[Any] = None,  # HAWebSocketClientProtocol
    netalertx_api_client: Optional[Any] = None,  # NetAlertXAPIClient
) -> None:
    """Run an AgentLoop to investigate an offline device and create a HITL card.

    Uses the real AutonomyGate(AUTONOMY_LEVEL) so gated fixes (reload_integration)
    route through the real gate and produce cards at lower autonomy levels.
    SSH reads use ReadOnlySSHClient (ADR 043); every SSH write raises.
    """
    import config as _cfg
    from utils.agent.agent_loop import AgentLoop
    from utils.agent.autonomy import AutonomyGate
    from utils.agent.tool_executor import ToolExecutor
    from utils.agent.tool_registry import build_device_availability_tool_registry
    from utils.core.prompts import load_prompt
    from utils.ha.read_only_ssh import ReadOnlySSHClient
    from utils.ha.ssh_client import AsyncSSHClient
    from utils.llm.llm_factory import make_llm_client

    llm = llm_client or make_llm_client()  # pragma: no cover
    gate = AutonomyGate(_cfg.AUTONOMY_LEVEL)
    _ssh_real = ssh_client or AsyncSSHClient(  # pragma: no cover
        _cfg.HA_HOST, _cfg.HA_USER, _cfg.SSH_KEY_PATH
    )

    executor = ToolExecutor(
        ha_ssh_client=ReadOnlySSHClient(_ssh_real),
        gate=gate,
        notifier=notifier,
        ha_rest_client=ha_rest_client,
        ha_ws_client=ws_client,
        knowledge_store=knowledge_store,
        db_path=db_path,
        netalertx_api_client=netalertx_api_client,
    )
    executor.set_device_availability_finding(finding)

    try:
        from utils.ha.ha_environment import load_environment_profile as _lep

        _prof = await asyncio.to_thread(_lep, db_path)
        if _prof is not None:
            executor.set_ha_profile(_prof)
    except Exception:  # nosec B110
        pass

    registry = build_device_availability_tool_registry()
    system_prompt = load_prompt("agent_loop_device_availability").format(
        terminal_tool="finish_device_availability"
    )

    from utils.knowledge.runbook_signature import (
        sig_device_availability,
        sig_device_post_restart,
    )

    if finding.ha_start_ts is not None:
        initial_context = build_post_restart_context(finding, db_path)
        signature = sig_device_post_restart()
        subject_key = finding.domain
        # Earlier per-integration sessions about the same devices count too.
        prior_keys = [subject_key] + sorted(
            {
                f"{d.get('config_entry_id', '')}:{d.get('domain', '')}"
                for d in finding.devices
            }
        )
    else:
        device_lines = "\n".join(format_device_line(d) for d in finding.devices)
        initial_context = (
            f"Device availability issue detected:\n"
            f"Integration: {finding.domain}\n"
            f"Config entry ID: {finding.config_entry_id}\n"
            f"{last_ha_start_line(db_path)}"
            f"Offline devices ({len(finding.devices)}):\n{device_lines}"
        )
        signature = sig_device_availability(finding.domain)
        subject_key = f"{finding.config_entry_id}:{finding.domain}"
        prior_keys = [subject_key]

    presence_block, _unmapped_ids = await build_presence_context(
        finding.devices,
        netalertx_api_client,
        ws_client,
        ssh_client=_ssh_real,
        db_path=db_path,
    )
    if presence_block:
        initial_context += presence_block
    if _unmapped_ids:
        executor.set_unmapped_device_ids(_unmapped_ids)

    from utils.agent.supervisor import (
        decrement_active_agent,
        increment_active_agent,
        make_activity_timeline_callback,
        publish_activity_done,
    )

    loop = AgentLoop(
        llm_client=llm,
        tool_executor=executor,
        tool_registry=registry,
        system_prompt=system_prompt,
        terminal_tool_name="finish_device_availability",
        trigger="device_availability_poll",
        activity_type="device_availability",
        db_path=db_path,
        knowledge_store=knowledge_store,
        timeline_callback=make_activity_timeline_callback(
            "device_availability",
            trigger=f"Device offline: {finding.domain}",
        ),
        capture_llm=True,
    )
    increment_active_agent()
    try:
        result = await loop.run(
            initial_context=initial_context,
            knowledge_query=device_availability_knowledge_query(finding),
            signature=signature,
            subject_key=subject_key,
            prior_subject_keys=prior_keys,
        )
        publish_activity_done("device_availability", result.outcome)
        if result.outcome != "success":
            import sqlite3 as _sqlite3

            from utils.hitl.hitl_tracker import mark_investigation_backoff

            with _sqlite3.connect(db_path) as _conn:
                mark_investigation_backoff(
                    _conn,
                    f"device_offline_backoff:{finding.config_entry_id}",
                )
            log.warning(
                "device_availability_investigation_backoff",
                outcome=result.outcome,
                config_entry_id=finding.config_entry_id,
                domain=finding.domain,
            )
    except Exception as exc:
        log.error(
            "device_availability_investigation_failed",
            config_entry_id=finding.config_entry_id,
            domain=finding.domain,
            error=str(exc),
        )
    finally:
        decrement_active_agent()
