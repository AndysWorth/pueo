"""Deterministic provisioning of Pueo companion integrations (Spook, ha-upgrade-advisor).

All provisioning is code-driven — no LLM, no tool registration, no MCP exposure.
The only user decision is which conversation agent the upgrade_advisor uses.
Every write goes through the backup chain (ADR 045).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from interfaces import (
        HARestClientProtocol,
        HAWebSocketClientProtocol,
        SSHClientProtocol,
    )


class Step(Enum):
    ADD_CUSTOM_REPO = auto()
    DOWNLOAD = auto()
    RESTART = auto()
    CONFIG_FLOW = auto()
    OPTIONS_FLOW = auto()
    VERIFY = auto()


@dataclass
class CompanionState:
    """Observed HA/HACS state for one companion domain."""

    domain: str
    in_hacs_repo_list: bool = False
    downloaded: bool = False
    loaded: bool = False
    has_config_entry: bool = False
    repo_id: str = ""


@dataclass
class CompanionSetupResult:
    domain: str
    steps_done: list[Step] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.error is None


@dataclass
class _CompanionSpec:
    domain: str
    hacs_full_name: str
    needs_user_choice: bool
    options: dict

    def build_flow_input(self, choice: Optional[str] = None) -> dict:
        if self.domain == "upgrade_advisor" and choice:
            return {"agent_id": choice}
        return {}


_COMPANIONS: dict[str, _CompanionSpec] = {
    "spook": _CompanionSpec(
        domain="spook",
        hacs_full_name="frenck/spook",
        needs_user_choice=False,
        options={},
    ),
    "upgrade_advisor": _CompanionSpec(
        domain="upgrade_advisor",
        hacs_full_name="brianegge/ha-upgrade-advisor",
        needs_user_choice=True,
        options={
            "scan_on_update": False,
            "scan_hacs": True,
            "post_upgrade_check": True,
            "create_repairs": False,
            "include_addons": False,
            "include_automations": True,
        },
    ),
}

COMPANION_DOMAINS = frozenset(_COMPANIONS.keys())


def get_companion_spec(domain: str) -> Optional[_CompanionSpec]:
    return _COMPANIONS.get(domain)


def plan_steps(state: CompanionState) -> list[Step]:
    """Return the remaining steps to complete provisioning.

    Pure function — idempotent. A re-run picks up wherever the last run stopped.
    """
    steps: list[Step] = []
    if not state.in_hacs_repo_list:
        steps.append(Step.ADD_CUSTOM_REPO)
    if not state.downloaded:
        steps.append(Step.DOWNLOAD)
    if not state.loaded:
        steps.append(Step.RESTART)
    if not state.has_config_entry:
        steps.append(Step.CONFIG_FLOW)
    spec = _COMPANIONS.get(state.domain)
    if spec and spec.options and not state.has_config_entry:
        steps.append(Step.OPTIONS_FLOW)
    steps.append(Step.VERIFY)
    return steps


async def _observe_state(
    domain: str,
    rest: "HARestClientProtocol",
    ws: "HAWebSocketClientProtocol",
) -> CompanionState:
    """Query HA/HACS for current provisioning state of a companion domain."""
    spec = _COMPANIONS.get(domain)
    hacs_full_name = spec.hacs_full_name if spec else ""

    in_hacs = False
    downloaded = False
    repo_id = ""

    try:
        repos = await ws.hacs_list_repositories()
        for repo in repos:
            if repo.get("full_name", "").lower() == hacs_full_name.lower():
                in_hacs = True
                repo_id = str(repo.get("id", ""))
                downloaded = bool(repo.get("installed") or repo.get("downloaded"))
                break
    except Exception:  # nosec B110
        pass

    loaded = False
    try:
        components = await ws.get_ha_components()
        loaded = domain in components
    except Exception:  # nosec B110
        pass

    has_entry = False
    try:
        entries = await ws.get_config_entries()
        has_entry = any(e.get("domain") == domain for e in entries)
    except Exception:  # nosec B110
        pass

    return CompanionState(
        domain=domain,
        in_hacs_repo_list=in_hacs,
        downloaded=downloaded,
        loaded=loaded,
        has_config_entry=has_entry,
        repo_id=repo_id,
    )


def _print_step(msg: str) -> None:
    print(f"  → {msg}")


async def run_companion_setup(
    domain: str,
    *,
    choice: Optional[str] = None,
    rest: "HARestClientProtocol",
    ws: "HAWebSocketClientProtocol",
    ssh: "SSHClientProtocol",
    dry_run: bool = False,
    notifier: Any = None,
    _api_poll: Optional[Callable] = None,
    _down_poll: Optional[Callable] = None,
) -> CompanionSetupResult:
    """Run the provisioning chain for one companion domain.

    Chain: backup → add_custom_repo? → download → restart → config_flow → options_flow → verify.

    dry_run=True prints what would happen without writing anything.
    """
    spec = _COMPANIONS.get(domain)
    if spec is None:
        return CompanionSetupResult(
            domain=domain, error=f"Unknown companion domain: {domain}"
        )

    if spec.needs_user_choice and choice is None and not dry_run:
        return CompanionSetupResult(
            domain=domain,
            error=(
                f"{domain} requires a conversation agent (pass --agent-id). "
                "Run --list-agents to see available agents, or install an LLM "
                "conversation integration (e.g. HA's Ollama integration) first."
            ),
        )

    state = await _observe_state(domain, rest, ws)
    steps = plan_steps(state)

    if dry_run:
        print(f"Dry run for {domain} — steps that would run:")
        for s in steps:
            print(f"  {s.name}")
        return CompanionSetupResult(domain=domain, steps_done=[])

    if steps == [Step.VERIFY]:
        print(f"✓  {domain} is already fully set up — nothing to do.")
        state2 = await _observe_state(domain, rest, ws)
        if state2.has_config_entry:
            return CompanionSetupResult(domain=domain, steps_done=[Step.VERIFY])
        return CompanionSetupResult(
            domain=domain, error=f"{domain} appears set up but verify failed"
        )

    from agents.ha_agent_advanced import execute_remote_backup, record_backup_slug

    steps_done: list[Step] = []

    try:
        # Safety invariant: backup before any write.
        _print_step("Creating backup…")
        backup_slug = await execute_remote_backup(ssh_client=ssh)
        record_backup_slug(backup_slug)
        _print_step(f"Backup complete: {backup_slug}")

        if Step.ADD_CUSTOM_REPO in steps:
            _print_step(f"Adding {spec.hacs_full_name} to HACS…")
            await ws.hacs_add_repository(spec.hacs_full_name)
            steps_done.append(Step.ADD_CUSTOM_REPO)
            _print_step(f"Custom repository {spec.hacs_full_name} added.")
            # Re-observe to get repo_id
            state = await _observe_state(domain, rest, ws)

        if Step.DOWNLOAD in steps:
            _print_step(f"Downloading {domain} via HACS (repo_id={state.repo_id!r})…")
            if not state.repo_id:
                return CompanionSetupResult(
                    domain=domain,
                    steps_done=steps_done,
                    error=f"Could not find HACS repo_id for {spec.hacs_full_name}",
                )
            await ws.hacs_download(state.repo_id)
            steps_done.append(Step.DOWNLOAD)
            _print_step(f"Downloaded {domain}.")

        if Step.RESTART in steps:
            from agents.ha_update_manager import execute_ha_core_restart

            _print_step("Restarting HA Core to load the new integration…")
            ok = await execute_ha_core_restart(
                ssh_client=ssh,
                notifier=notifier,
                _api_poll=_api_poll,
                _down_poll=_down_poll,
                skip_backup=True,
            )
            if not ok:
                return CompanionSetupResult(
                    domain=domain,
                    steps_done=steps_done,
                    error="HA Core did not come back online after restart",
                )
            steps_done.append(Step.RESTART)
            _print_step("HA Core is back online.")
            state = await _observe_state(domain, rest, ws)

        if Step.CONFIG_FLOW in steps:
            _print_step(f"Starting config flow for {domain}…")
            flow_input = spec.build_flow_input(choice)
            entry_id = await _run_config_flow(domain, flow_input, rest)
            steps_done.append(Step.CONFIG_FLOW)
            _print_step(f"Config entry created (entry_id={entry_id!r}).")
            state = await _observe_state(domain, rest, ws)

            if Step.OPTIONS_FLOW in steps and spec.options:
                _print_step(f"Setting options for {domain}…")
                entries = await ws.get_config_entries()
                cfg_entry = next(
                    (e for e in entries if e.get("domain") == domain), None
                )
                if cfg_entry:
                    await _run_options_flow(cfg_entry["entry_id"], spec.options, rest)
                    steps_done.append(Step.OPTIONS_FLOW)
                    _print_step(f"Options set for {domain}.")
                else:
                    _print_step(
                        f"⚠  Could not find config entry for {domain} to set options."
                    )

        # Verify
        _print_step(f"Verifying {domain} is loaded…")
        final_state = await _observe_state(domain, rest, ws)
        if not final_state.has_config_entry:
            return CompanionSetupResult(
                domain=domain,
                steps_done=steps_done,
                error=f"{domain} config entry not found after setup",
            )
        steps_done.append(Step.VERIFY)
        print(f"✓  {domain} is set up successfully.")
        return CompanionSetupResult(domain=domain, steps_done=steps_done)

    except Exception as exc:
        return CompanionSetupResult(
            domain=domain,
            steps_done=steps_done,
            error=str(exc),
        )


async def _run_config_flow(
    domain: str,
    flow_input: dict,
    rest: "HARestClientProtocol",
) -> str:
    """Start a config flow for domain and submit it. Returns entry_id."""
    flow = await rest.start_config_flow(domain)
    flow_type = flow.get("type", "")

    if flow_type == "invalid_handler":
        # HA may need a moment after restart; retry once
        await asyncio.sleep(3)
        flow = await rest.start_config_flow(domain)
        flow_type = flow.get("type", "")

    flow_id = flow.get("flow_id", "")
    if not flow_id and flow_type not in ("create_entry", "abort"):
        raise RuntimeError(f"start_config_flow returned no flow_id: {flow!r}")

    if flow_type == "form":
        result = await rest.submit_config_flow(flow_id, flow_input)
    elif flow_type in ("create_entry", "abort"):
        result = flow
    else:
        result = await rest.submit_config_flow(flow_id, flow_input)

    if result.get("type") == "abort":
        abort_reason = result.get("reason", "unknown")
        if abort_reason == "already_configured":
            entries_raw = result.get("result", {})
            return str(entries_raw.get("entry_id", ""))
        raise RuntimeError(f"Config flow aborted: {abort_reason}")

    return str(result.get("result", {}).get("entry_id", ""))


async def _run_options_flow(
    entry_id: str,
    options: dict,
    rest: "HARestClientProtocol",
) -> None:
    """Start and complete an options flow for an existing config entry."""
    flow = await rest.start_options_flow(entry_id)
    flow_id = flow.get("flow_id", "")
    if not flow_id:
        raise RuntimeError(f"start_options_flow returned no flow_id: {flow!r}")
    await rest.submit_options_flow(flow_id, options)
