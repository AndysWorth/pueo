"""LLM-guided investigation for host-health findings (swap, eMMC wear, reboot, resolution)."""

import asyncio
import time
from typing import Any, Optional

_MEMORY_KINDS = frozenset({"memory_pressure", "memory_low"})

from utils.core.logging import get_logger
from utils.disk.host_health import HostHealthFinding

log = get_logger("host_health_investigation")


def load_recurrence_history(db_path: str, subject_key: str) -> dict:
    """Return recurrence history for the initial_context history block.

    Queries session_outcomes for the last completed host_health session with the
    given subject_key (= finding.key) and counts sessions in the last 7 days.
    """
    import sqlite3

    cutoff_iso = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7 * 86400)
    )
    result: dict = {}
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT conclusion_json, created_at FROM session_outcomes"
                " WHERE session_type = 'host_health' AND subject_key = ?"
                " ORDER BY rowid DESC LIMIT 1",
                (subject_key,),
            ).fetchone()
            if row:
                import json as _json

                try:
                    conc = _json.loads(row[0] or "{}")
                    result["last_root_cause"] = conc.get("root_cause", "")
                    result["last_summary"] = conc.get("summary", "")
                    result["last_created_at"] = row[1] or ""
                except Exception:  # nosec B110
                    pass
            count_row = conn.execute(
                "SELECT COUNT(*) FROM session_outcomes"
                " WHERE session_type = 'host_health' AND subject_key = ?"
                "   AND created_at >= ?",
                (subject_key, cutoff_iso),
            ).fetchone()
            result["times_in_7d"] = count_row[0] if count_row else 0
    except Exception:  # nosec B110
        pass
    return result


def host_health_knowledge_query(finding: HostHealthFinding) -> str:
    """Targeted pre-inject query: kind plus resolution issue type or reason."""
    detail = finding.evidence.get("type") or finding.evidence.get("reason") or ""
    return f"host health {finding.kind} {detail}".strip()


async def run_host_health_investigation(
    finding: HostHealthFinding,
    notifier: Any,  # NotifierProtocol
    db_path: str,
    llm_client: Optional[Any] = None,  # LLMClientProtocol
    knowledge_store: Optional[Any] = None,
    ha_rest_client: Optional[Any] = None,
    ssh_client: Optional[Any] = None,  # SSHClientProtocol
    ha_ws_client: Optional[Any] = None,  # HAWebSocketClientProtocol
    recurrence_history: Optional[dict] = None,
) -> None:
    """Run an AgentLoop to investigate a host-health finding and create a HITL card.

    Uses the real AutonomyGate(AUTONOMY_LEVEL) and a real AsyncSSHClient so
    gated fixes (ha resolution suggestion apply, ha apps restart) actually
    execute or become cards depending on the autonomy level — unlike repair-issue
    investigations, which use a FakeAutonomyGate.
    """
    import config as _cfg
    from utils.agent.agent_loop import AgentLoop
    from utils.agent.autonomy import AutonomyGate
    from utils.agent.tool_executor import ToolExecutor
    from utils.agent.tool_registry import build_host_health_tool_registry
    from utils.core.prompts import load_agent_prompt
    from utils.ha.ssh_client import AsyncSSHClient
    from utils.llm.llm_factory import make_llm_client

    llm = llm_client or make_llm_client()  # pragma: no cover
    gate = AutonomyGate(_cfg.AUTONOMY_LEVEL)
    _ssh = ssh_client or AsyncSSHClient(  # pragma: no cover
        _cfg.HA_HOST, _cfg.HA_USER, _cfg.SSH_KEY_PATH
    )

    executor = ToolExecutor(
        ha_ssh_client=_ssh,
        gate=gate,
        notifier=notifier,
        ha_rest_client=ha_rest_client,
        ha_ws_client=ha_ws_client,
        knowledge_store=knowledge_store,
        db_path=db_path,
    )
    executor.set_host_health_finding(finding)

    try:
        from utils.ha.ha_environment import load_environment_profile as _lep

        _prof = await asyncio.to_thread(_lep, db_path)
        if _prof is not None:
            executor.set_ha_profile(_prof)
    except Exception:  # nosec B110
        pass

    registry = build_host_health_tool_registry()
    system_prompt = load_agent_prompt("host", "finish_host_health")

    evidence_lines = "\n".join(
        f"  {k}: {v}" for k, v in finding.evidence.items() if v is not None
    )
    initial_context = (
        f"Host health issue detected:\n"
        f"Kind: {finding.kind}\n"
        f"Severity: {finding.severity}\n"
        f"Evidence:\n{evidence_lines}"
    )
    if recurrence_history and recurrence_history.get("times_in_7d", 0) > 0:
        _hist = recurrence_history
        _hist_lines = [
            f"History (last 7 days): seen {_hist['times_in_7d']}× as an investigation.",
        ]
        if _hist.get("last_created_at"):
            _hist_lines.append(f"Last investigated: {_hist['last_created_at']} UTC.")
        if _hist.get("last_root_cause"):
            _hist_lines.append(f"Previous root cause: {_hist['last_root_cause']}")
        if _hist.get("last_summary"):
            _hist_lines.append(f"Previous conclusion: {_hist['last_summary']}")
        initial_context = "\n".join(_hist_lines) + "\n\n" + initial_context

    # For memory kinds: append trend summary and cached memory breakdown.
    if finding.kind in _MEMORY_KINDS:
        try:
            from utils.core.context import truncate_to_budget
            from utils.disk.host_health import summarize_resource_trend
            from utils.disk.memory_usage import get_memory_breakdown
            from utils.disk.resource import load_resource_samples

            _samples = await asyncio.to_thread(load_resource_samples, db_path, 30.0)
            if _samples:
                _trend = summarize_resource_trend(_samples, time.time())
                _trend_lines = ["Trend (resource_samples, up to 30 d):"]
                for _w, _wdata in _trend.get("windows", {}).items():
                    _mem = _wdata.get("mem_available_mb", {})
                    _line = (
                        f"  {_w}: mem_available "
                        f"min={_mem.get('min_mb'):.0f} "
                        f"median={_mem.get('median_mb'):.0f} "
                        f"max={_mem.get('max_mb'):.0f} MB"
                        f" ({_mem.get('samples', 0)} samples)"
                    )
                    if "psi_some_max" in _wdata:
                        _line += f", PSI-some max={_wdata['psi_some_max']}"
                    if "oom_kill_range" in _wdata:
                        _line += f", OOM delta={_wdata['oom_kill_range']}"
                    _trend_lines.append(_line)
                if "slope_mb_per_day" in _trend:
                    _s = _trend["slope_mb_per_day"]
                    _dir = (
                        " (declining)"
                        if _s < -5
                        else (" (improving)" if _s > 5 else " (stable)")
                    )
                    _trend_lines.append(f"  Slope: {_s:+.1f} MB/day{_dir}")
                initial_context += "\n\n" + "\n".join(_trend_lines)

            _bd = get_memory_breakdown()
            if _bd is not None:
                _bd_lines = ["Memory breakdown (cached from last poll):"]
                _bd_lines.append(
                    f"  Total: {_bd.total_mb:.0f} MB, "
                    f"Available: {_bd.available_mb:.0f} MB"
                )
                for _c in _bd.consumers[:8]:
                    _bd_lines.append(f"  {_c.name}: {_c.used_mb:.0f} MB")
                if _bd.other_mb > 0:
                    _bd_lines.append(f"  other: {_bd.other_mb:.0f} MB")
                initial_context = truncate_to_budget(
                    initial_context + "\n\n" + "\n".join(_bd_lines), 6000
                )
        except Exception:  # nosec B110
            pass

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
        terminal_tool_name="finish_host_health",
        trigger="resource_poll",
        activity_type="host_health",
        db_path=db_path,
        knowledge_store=knowledge_store,
        timeline_callback=make_activity_timeline_callback(
            "host_health", trigger=f"Host health: {finding.kind}"
        ),
        capture_llm=True,
    )
    from utils.knowledge.runbook_signature import sig_host_health

    increment_active_agent()
    try:
        result = await loop.run(
            initial_context=initial_context,
            knowledge_query=host_health_knowledge_query(finding),
            signature=sig_host_health(finding.kind, finding.evidence.get("type", "")),
            subject_key=finding.key,
        )
        publish_activity_done("host_health", result.outcome)
        if result.outcome != "success":
            import sqlite3 as _sqlite3

            from utils.hitl.hitl_tracker import mark_investigation_backoff

            with _sqlite3.connect(db_path) as _conn:
                mark_investigation_backoff(_conn, f"host_health_backoff:{finding.key}")
            log.warning(
                "host_health_investigation_backoff",
                outcome=result.outcome,
                finding_key=finding.key,
            )
    except Exception as exc:
        log.error(
            "host_health_investigation_failed",
            finding_key=finding.key,
            error=str(exc),
        )
    finally:
        decrement_active_agent()
