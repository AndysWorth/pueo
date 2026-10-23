"""LLM-guided investigation for device-availability findings (offline devices)."""

import asyncio
from typing import Any, Optional

from utils.core.logging import get_logger
from utils.ha.device_availability import OfflineFinding

log = get_logger("device_availability_investigation")


def device_availability_knowledge_query(finding: OfflineFinding) -> str:
    """Targeted pre-inject query: domain plus 'device offline'."""
    return f"device offline {finding.domain} unavailable".strip()


async def build_presence_context(
    devices: list,  # list[dict] from OfflineFinding.devices
    netalertx_api_client: Optional[Any],  # Optional[NetAlertXAPIClient]
    ws_client: Optional[Any],  # Optional[HAWebSocketClientProtocol]
) -> str:
    """Return a NetAlertX presence summary block for pre-injection into initial_context.

    Returns an empty string when NetAlertX is not configured, has no token, or
    when any fetch fails.  Never raises.
    """
    if netalertx_api_client is None or not getattr(
        netalertx_api_client, "has_token", False
    ):
        return ""

    try:
        mac_by_device_id: dict = {}
        if ws_client is not None:
            try:
                dev_reg = await ws_client.get_device_registry()
                for dev in dev_reg:
                    for conn_type, conn_val in dev.get("connections", []):
                        if conn_type == "mac":
                            mac_by_device_id[dev["id"]] = conn_val.lower()
                            break
            except Exception:  # nosec B110
                pass

        nax_devices = await netalertx_api_client.get_devices()
        mac_to_nax: dict = {(d.get("devMac") or "").lower(): d for d in nax_devices}

        lines: list = []
        for dev in devices:
            device_id = dev.get("device_id", "")
            name = dev.get("name", device_id)
            mac = mac_by_device_id.get(device_id)
            if mac is None:
                lines.append(f"  - {name}: not found in NetAlertX (no MAC mapping)")
                continue
            nax_dev = mac_to_nax.get(mac)
            if nax_dev is None:
                lines.append(f"  - {name}: not found in NetAlertX (MAC {mac})")
                continue
            present = bool(nax_dev.get("devPresentLastScan"))
            last_seen = (
                nax_dev.get("devLastSeen")
                or nax_dev.get("devLastConnection")
                or "unknown"
            )
            current_ip = nax_dev.get("devLastIP") or "unknown"
            lines.append(
                f"  - {name}: {'present on LAN' if present else 'absent from LAN'}"
                f" (MAC {mac}, IP {current_ip}, last_seen {last_seen})"
            )

        if not lines:
            return ""

        return "\n\nNetAlertX network presence:\n" + "\n".join(lines)

    except Exception as exc:  # nosec B110
        log.debug("presence_context_fetch_failed", error=str(exc))
        return ""


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

    def _fmt_offline_since(d: dict) -> str:
        ts = d.get("offline_since")
        if ts is None:
            return "unknown"
        import time as _time

        iso = _time.strftime("%Y-%m-%dT%H:%M:%S", _time.localtime(float(ts)))
        return f"{iso} local (epoch {int(ts)})"

    device_lines = "\n".join(
        f"  - {d['name']} (offline since {_fmt_offline_since(d)})"
        for d in finding.devices
    )
    initial_context = (
        f"Device availability issue detected:\n"
        f"Integration: {finding.domain}\n"
        f"Config entry ID: {finding.config_entry_id}\n"
        f"Offline devices ({len(finding.devices)}):\n{device_lines}"
    )

    presence_block = await build_presence_context(
        finding.devices, netalertx_api_client, ws_client
    )
    if presence_block:
        initial_context += presence_block

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
    from utils.knowledge.runbook_signature import sig_device_availability

    increment_active_agent()
    try:
        result = await loop.run(
            initial_context=initial_context,
            knowledge_query=device_availability_knowledge_query(finding),
            signature=sig_device_availability(finding.domain),
            subject_key=f"{finding.config_entry_id}:{finding.domain}",
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
