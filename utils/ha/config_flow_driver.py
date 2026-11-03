"""Config flow driver for programmatic HA integration setup."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from interfaces import HARestClientProtocol

_log = logging.getLogger("config_flow_driver")

_MAX_STEPS = 15


class FlowResult(NamedTuple):
    success: bool
    entry_ids: list[str]
    abort_reason: str
    error_message: str


def summarize_flow_step(resp: dict, translations: dict | None = None) -> str:
    """Render a flow step response as human-readable text.

    Pure function — no I/O. Uses translation labels from strings.json/en.json
    when provided (``{"config": {"step": {step_id: {"data": {}, "menu_options": {}}}}}``).
    """
    step_type = resp.get("type", "")
    step_id = resp.get("step_id", "")
    lines: list[str] = []

    if step_id:
        lines.append(f"Step: {step_id} ({step_type})")
    else:
        lines.append(f"Type: {step_type}")

    step_labels: dict[str, str] = {}
    menu_labels: dict[str, str] = {}
    if translations and step_id:
        step_block = translations.get("config", {}).get("step", {}).get(step_id, {})
        step_labels = step_block.get("data", {})
        menu_labels = step_block.get("menu_options", {})

    if step_type == "menu":
        options = resp.get("menu_options", [])
        lines.append("Options:")
        for opt in options:
            label = menu_labels.get(opt, opt)
            lines.append(f"  - {opt}: {label}")

    elif step_type == "form":
        schema = resp.get("data_schema", [])
        if schema:
            lines.append("Fields:")
            for field in schema:
                name = field.get("name", "")
                label = step_labels.get(name, name)
                required = "required" if field.get("required") else "optional"
                default = field.get("default")
                selector = field.get("selector", {})

                field_line = f"  - {name} ({label}): {required}"
                if default is not None:
                    field_line += f", default={default!r}"

                if "select" in selector:
                    opts = selector["select"].get("options", [])
                    if opts:
                        if isinstance(opts[0], dict):
                            opt_strs = [str(o.get("value", o)) for o in opts[:5]]
                        else:
                            opt_strs = [str(o) for o in opts[:5]]
                        if len(opts) > 5:
                            opt_strs.append("…")
                        field_line += f", choices=[{', '.join(opt_strs)}]"

                lines.append(field_line)

    if resp.get("errors"):
        lines.append(f"Errors: {resp['errors']}")

    return "\n".join(lines)


async def drive_config_flow(
    rest: "HARestClientProtocol",
    domain: str,
    plan: dict[str, dict],
    *,
    max_steps: int = _MAX_STEPS,
) -> FlowResult:
    """Walk a config flow, submitting answers from plan.

    plan maps step_id → answers dict. Use ``"*"`` as a wildcard for any step.

    Aborts via DELETE on unexpected step, validation errors, or exception.
    Returns FlowResult(success=True, entry_ids=[...]) on create_entry.
    """
    flow_id = ""
    try:
        resp = await rest.start_config_flow(domain)

        if resp.get("type") == "invalid_handler":
            await asyncio.sleep(3)
            resp = await rest.start_config_flow(domain)

        flow_id = resp.get("flow_id", "")

        for _ in range(max_steps):
            resp_type = resp.get("type", "")

            if resp_type == "create_entry":
                result = resp.get("result", {})
                if isinstance(result, dict):
                    entry_ids = (
                        [str(result["entry_id"])] if result.get("entry_id") else []
                    )
                elif isinstance(result, list):
                    entry_ids = [
                        str(r["entry_id"]) for r in result if r.get("entry_id")
                    ]
                else:
                    entry_ids = []
                return FlowResult(
                    success=True,
                    entry_ids=entry_ids,
                    abort_reason="",
                    error_message="",
                )

            if resp_type == "abort":
                abort_reason = resp.get("reason", "unknown")
                return FlowResult(
                    success=False,
                    entry_ids=[],
                    abort_reason=abort_reason,
                    error_message=f"Flow aborted: {abort_reason}",
                )

            if resp_type not in ("form", "menu"):
                return FlowResult(
                    success=False,
                    entry_ids=[],
                    abort_reason="",
                    error_message=f"Unexpected flow type: {resp_type!r}",
                )

            step_id = resp.get("step_id", "")
            if not flow_id:
                flow_id = resp.get("flow_id", flow_id)

            answers = plan.get(step_id) if step_id in plan else plan.get("*")
            if answers is None:
                _log.warning(
                    "config_flow_driver: no plan for step_id=%r domain=%s, aborting",
                    step_id,
                    domain,
                )
                await rest.abort_config_flow(flow_id)
                return FlowResult(
                    success=False,
                    entry_ids=[],
                    abort_reason="no_plan",
                    error_message=f"No answer in plan for step {step_id!r}",
                )

            resp = await rest.submit_config_flow(flow_id, answers)

            if resp.get("errors"):
                _log.warning(
                    "config_flow_driver: step errors step_id=%r errors=%r, aborting",
                    step_id,
                    resp.get("errors"),
                )
                await rest.abort_config_flow(flow_id)
                return FlowResult(
                    success=False,
                    entry_ids=[],
                    abort_reason="step_errors",
                    error_message=f"Step {step_id!r} returned errors: {resp.get('errors')}",
                )

        if flow_id:
            await rest.abort_config_flow(flow_id)
        return FlowResult(
            success=False,
            entry_ids=[],
            abort_reason="max_steps",
            error_message=f"Flow did not complete within {max_steps} steps",
        )

    except Exception as exc:
        if flow_id:
            try:
                await rest.abort_config_flow(flow_id)
            except Exception:  # nosec B110
                pass
        return FlowResult(
            success=False,
            entry_ids=[],
            abort_reason="exception",
            error_message=str(exc),
        )
