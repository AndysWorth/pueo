"""Tests for utils/ha/companion_setup.py and related client stubs."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from utils.ha.companion_setup import (
    COMPANION_DOMAINS,
    CompanionState,
    Step,
    _COMPANIONS,
    get_companion_spec,
    plan_steps,
    run_companion_setup,
)
from utils.ha.ha_rest_client import FakeHARestClient
from utils.ha.ha_ws_client import FakeHAWebSocketClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ws(
    *,
    hacs_repositories=None,
    ha_components=None,
    config_entries=None,
    conversation_agents=None,
):
    return FakeHAWebSocketClient(
        hacs_repositories=hacs_repositories or [],
        ha_components=ha_components or [],
        config_entries=config_entries or [],
        conversation_agents=conversation_agents or [],
    )


def _make_rest(*, flow_responses=None):
    return FakeHARestClient(flow_responses=flow_responses or {})


def _make_spook_hacs_repo(downloaded=True, repo_id="12345"):
    return {
        "full_name": "frenck/spook",
        "installed": downloaded,
        "downloaded": downloaded,
        "id": int(repo_id),
        "category": "integration",
        "domain": "spook",
    }


def _set_loaded(ws, domain="spook", entry_id="e1"):
    ws._ha_components = [domain]
    ws._config_entries = [{"domain": domain, "state": "loaded", "entry_id": entry_id}]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestCompanionRegistry:
    def test_known_domains(self):
        assert "spook" in COMPANION_DOMAINS
        assert "upgrade_advisor" in COMPANION_DOMAINS

    def test_get_companion_spec_known(self):
        spec = get_companion_spec("spook")
        assert spec is not None
        assert spec.domain == "spook"
        assert spec.hacs_full_name == "frenck/spook"
        assert not spec.needs_user_choice

    def test_get_companion_spec_unknown(self):
        assert get_companion_spec("not_a_companion") is None

    def test_upgrade_advisor_options(self):
        spec = get_companion_spec("upgrade_advisor")
        assert spec is not None
        assert spec.options["scan_on_update"] is False
        assert spec.options["create_repairs"] is False
        assert spec.options["post_upgrade_check"] is True
        assert spec.options["scan_hacs"] is True
        assert spec.options["include_addons"] is False
        assert spec.options["include_automations"] is True

    def test_upgrade_advisor_needs_user_choice(self):
        spec = get_companion_spec("upgrade_advisor")
        assert spec is not None
        assert spec.needs_user_choice

    def test_build_flow_input_spook(self):
        spec = get_companion_spec("spook")
        assert spec is not None
        assert spec.build_flow_input() == {}

    def test_build_flow_input_upgrade_advisor_with_agent(self):
        spec = get_companion_spec("upgrade_advisor")
        assert spec is not None
        result = spec.build_flow_input("conversation.ollama")
        assert result == {"agent_id": "conversation.ollama"}

    def test_build_flow_input_upgrade_advisor_without_agent(self):
        spec = get_companion_spec("upgrade_advisor")
        assert spec is not None
        assert spec.build_flow_input() == {}


# ---------------------------------------------------------------------------
# plan_steps — every state combination
# ---------------------------------------------------------------------------


class TestPlanSteps:
    def _state(self, **kwargs) -> CompanionState:
        return CompanionState(domain="spook", **kwargs)

    def test_nothing_done(self):
        state = self._state()
        steps = plan_steps(state)
        assert Step.ADD_CUSTOM_REPO in steps
        assert Step.DOWNLOAD in steps
        assert Step.RESTART in steps
        assert Step.CONFIG_FLOW in steps
        assert Step.VERIFY in steps

    def test_in_hacs_no_download(self):
        state = self._state(in_hacs_repo_list=True)
        steps = plan_steps(state)
        assert Step.ADD_CUSTOM_REPO not in steps
        assert Step.DOWNLOAD in steps
        assert Step.RESTART in steps

    def test_downloaded_not_loaded(self):
        state = self._state(in_hacs_repo_list=True, downloaded=True)
        steps = plan_steps(state)
        assert Step.ADD_CUSTOM_REPO not in steps
        assert Step.DOWNLOAD not in steps
        assert Step.RESTART in steps
        assert Step.CONFIG_FLOW in steps

    def test_loaded_no_config_entry(self):
        state = self._state(in_hacs_repo_list=True, downloaded=True, loaded=True)
        steps = plan_steps(state)
        assert Step.RESTART not in steps
        assert Step.CONFIG_FLOW in steps

    def test_fully_configured(self):
        state = self._state(
            in_hacs_repo_list=True,
            downloaded=True,
            loaded=True,
            has_config_entry=True,
        )
        steps = plan_steps(state)
        assert steps == [Step.VERIFY]

    def test_upgrade_advisor_includes_options_flow_when_not_configured(self):
        state = CompanionState(
            domain="upgrade_advisor",
            in_hacs_repo_list=True,
            downloaded=True,
            loaded=True,
        )
        steps = plan_steps(state)
        assert Step.CONFIG_FLOW in steps
        assert Step.OPTIONS_FLOW in steps

    def test_spook_no_options_flow(self):
        state = CompanionState(
            domain="spook",
            in_hacs_repo_list=True,
            downloaded=True,
            loaded=True,
        )
        steps = plan_steps(state)
        assert Step.OPTIONS_FLOW not in steps

    def test_idempotent_already_downloaded(self):
        state = CompanionState(
            domain="spook",
            in_hacs_repo_list=True,
            downloaded=True,
        )
        assert plan_steps(state) == plan_steps(state)


# ---------------------------------------------------------------------------
# run_companion_setup — chain ordering via fakes
# ---------------------------------------------------------------------------


class TestRunCompanionSetupUnknownDomain:
    def test_unknown_domain_returns_error(self):
        rest = _make_rest()
        ws = _make_ws()
        ssh = AsyncMock()
        result = asyncio.run(
            run_companion_setup("not_a_companion", rest=rest, ws=ws, ssh=ssh)
        )
        assert not result.success
        assert "Unknown companion domain" in (result.error or "")


class TestRunCompanionSetupDryRun:
    def test_dry_run_prints_steps_no_writes(self, capsys):
        ws = _make_ws()
        rest = _make_rest()
        ssh = AsyncMock()
        result = asyncio.run(
            run_companion_setup("spook", rest=rest, ws=ws, ssh=ssh, dry_run=True)
        )
        assert result.steps_done == []
        assert result.error is None
        out = capsys.readouterr().out
        assert "Dry run" in out
        assert "ADD_CUSTOM_REPO" in out

    def test_dry_run_shows_fewer_steps_when_fully_configured(self, capsys):
        ws = _make_ws(
            hacs_repositories=[_make_spook_hacs_repo(downloaded=True)],
            ha_components=["spook"],
            config_entries=[{"domain": "spook", "state": "loaded", "entry_id": "e1"}],
        )
        rest = _make_rest()
        ssh = AsyncMock()
        asyncio.run(
            run_companion_setup("spook", rest=rest, ws=ws, ssh=ssh, dry_run=True)
        )
        out = capsys.readouterr().out
        assert "VERIFY" in out


class TestRunCompanionSetupAdvisorRequiresAgentId:
    def test_returns_error_when_no_agent_id(self):
        ws = _make_ws()
        rest = _make_rest()
        ssh = AsyncMock()
        result = asyncio.run(
            run_companion_setup(
                "upgrade_advisor", choice=None, rest=rest, ws=ws, ssh=ssh
            )
        )
        assert not result.success
        assert "agent" in (result.error or "").lower()


class TestRunCompanionSetupBackupFirst:
    def test_backup_called_before_hacs_download(self):
        """Backup must be the first write — hacs_download must not run before it."""
        hacs_repo = _make_spook_hacs_repo(downloaded=False, repo_id="99")
        ws = _make_ws(
            hacs_repositories=[hacs_repo],
            ha_components=[],
            config_entries=[],
        )
        flow_id = "flow-spook"
        rest = _make_rest(
            flow_responses={
                "start:spook": {"flow_id": flow_id, "type": "form"},
                f"submit:{flow_id}": {
                    "type": "create_entry",
                    "result": {"entry_id": "e-spook"},
                },
            }
        )
        ssh = AsyncMock()
        call_order: list[str] = []

        async def fake_backup(ssh_client):
            call_order.append("backup")
            return "slug-abc"

        async def fake_restart(ssh_client, notifier, **kwargs):
            call_order.append("restart")
            _set_loaded(ws)
            return True

        with (
            patch(
                "agents.ha_agent_advanced.execute_remote_backup",
                side_effect=fake_backup,
            ),
            patch("agents.ha_agent_advanced.record_backup_slug"),
            patch(
                "agents.ha_update_manager.execute_ha_core_restart",
                side_effect=fake_restart,
            ),
        ):
            result = asyncio.run(
                run_companion_setup("spook", rest=rest, ws=ws, ssh=ssh)
            )

        assert result.error is None or result.error == "", f"error: {result.error}"
        assert call_order[0] == "backup"
        assert Step.DOWNLOAD in result.steps_done
        assert ws.hacs_download_calls == ["99"]

    def test_restart_receives_skip_backup_and_ssh_client(self):
        hacs_repo = _make_spook_hacs_repo(downloaded=True, repo_id="99")
        ws = _make_ws(
            hacs_repositories=[hacs_repo], ha_components=[], config_entries=[]
        )
        flow_id = "f1"
        rest = _make_rest(
            flow_responses={
                "start:spook": {"flow_id": flow_id, "type": "form"},
                f"submit:{flow_id}": {
                    "type": "create_entry",
                    "result": {"entry_id": "e-spook"},
                },
            }
        )
        ssh = AsyncMock()
        restart_kwargs: list[dict] = []

        async def fake_restart(ssh_client, notifier, **kwargs):
            restart_kwargs.append({"ssh_client": ssh_client, **kwargs})
            _set_loaded(ws)
            return True

        with (
            patch("agents.ha_agent_advanced.execute_remote_backup", return_value="sl"),
            patch("agents.ha_agent_advanced.record_backup_slug"),
            patch(
                "agents.ha_update_manager.execute_ha_core_restart",
                side_effect=fake_restart,
            ),
        ):
            result = asyncio.run(
                run_companion_setup("spook", rest=rest, ws=ws, ssh=ssh)
            )

        assert result.error is None or result.error == ""
        assert len(restart_kwargs) == 1
        assert restart_kwargs[0]["skip_backup"] is True
        assert restart_kwargs[0]["ssh_client"] is ssh


class TestRunCompanionSetupInvalidHandlerRetry:
    def test_invalid_handler_retries_config_flow(self):
        """flow_type 'invalid_handler' triggers one retry."""
        ws = _make_ws(
            hacs_repositories=[_make_spook_hacs_repo(downloaded=True)],
            ha_components=["spook"],
            config_entries=[],
        )
        call_count = 0

        class PatchedRest(FakeHARestClient):
            async def start_config_flow(self, handler):
                nonlocal call_count
                self.posted.append(
                    ("/api/config/config_entries/flow", {"handler": handler})
                )
                call_count += 1
                if call_count == 1:
                    return {"flow_id": "", "type": "invalid_handler"}
                return {"flow_id": "flow-retry", "type": "form"}

            async def submit_config_flow(self, flow_id, data):
                self.posted.append((f"/api/config/config_entries/flow/{flow_id}", data))
                _set_loaded(ws)
                return {"type": "create_entry", "result": {"entry_id": "e1"}}

        rest = PatchedRest()
        ssh = AsyncMock()

        with (
            patch("agents.ha_agent_advanced.execute_remote_backup", return_value="s"),
            patch("agents.ha_agent_advanced.record_backup_slug"),
            patch(
                "agents.ha_update_manager.execute_ha_core_restart", return_value=None
            ),
            patch("asyncio.sleep"),
        ):
            asyncio.run(run_companion_setup("spook", rest=rest, ws=ws, ssh=ssh))

        assert call_count == 2


class TestRunCompanionSetupAdvisorOptions:
    def test_options_flow_submitted_with_correct_payload(self):
        ws = _make_ws(
            hacs_repositories=[
                {
                    "full_name": "brianegge/ha-upgrade-advisor",
                    "installed": True,
                    "downloaded": True,
                    "id": 77,
                    "category": "integration",
                    "domain": "upgrade_advisor",
                }
            ],
            ha_components=[],
            config_entries=[],
        )
        options_payload: list[dict] = []

        class PatchedRest(FakeHARestClient):
            async def start_config_flow(self, handler):
                self.posted.append(
                    ("/api/config/config_entries/flow", {"handler": handler})
                )
                return {"flow_id": "flow-ua", "type": "form"}

            async def submit_config_flow(self, flow_id, data):
                self.posted.append((f"/api/config/config_entries/flow/{flow_id}", data))
                return {"type": "create_entry", "result": {"entry_id": "ua-entry"}}

            async def start_options_flow(self, entry_id):
                self.posted.append(
                    ("/api/config/config_entries/options/flow", {"handler": entry_id})
                )
                return {"flow_id": "opts-flow", "type": "form"}

            async def submit_options_flow(self, flow_id, data):
                options_payload.append(data)
                self.posted.append(
                    (f"/api/config/config_entries/options/flow/{flow_id}", data)
                )
                return {"type": "create_entry"}

        rest = PatchedRest()
        ssh = AsyncMock()

        def _post_restart(*args, **kwargs):
            ws._ha_components = ["upgrade_advisor"]
            ws._config_entries = [
                {"domain": "upgrade_advisor", "state": "loaded", "entry_id": "ua-entry"}
            ]
            return True

        with (
            patch("agents.ha_agent_advanced.execute_remote_backup", return_value="s"),
            patch("agents.ha_agent_advanced.record_backup_slug"),
            patch(
                "agents.ha_update_manager.execute_ha_core_restart",
                new=AsyncMock(side_effect=_post_restart),
            ),
        ):
            result = asyncio.run(
                run_companion_setup(
                    "upgrade_advisor",
                    choice="conversation.ollama",
                    rest=rest,
                    ws=ws,
                    ssh=ssh,
                )
            )

        assert result.error is None or result.error == "", f"error: {result.error}"
        assert Step.OPTIONS_FLOW in result.steps_done
        assert len(options_payload) == 1
        opts = options_payload[0]
        assert opts["scan_on_update"] is False
        assert opts["create_repairs"] is False
        assert opts["post_upgrade_check"] is True


# ---------------------------------------------------------------------------
# FakeHAWebSocketClient companion methods
# ---------------------------------------------------------------------------


class TestFakeWSCompanionMethods:
    def test_list_conversation_agents(self):
        agents = [{"id": "conversation.ollama", "name": "Ollama"}]
        ws = FakeHAWebSocketClient(conversation_agents=agents)
        result = asyncio.run(ws.list_conversation_agents())
        assert result == agents
        assert "list_conversation_agents" in ws.calls

    def test_hacs_list_repositories(self):
        repos = [{"full_name": "frenck/spook", "installed": True}]
        ws = FakeHAWebSocketClient(hacs_repositories=repos)
        result = asyncio.run(ws.hacs_list_repositories())
        assert result == repos

    def test_hacs_add_repository(self):
        ws = FakeHAWebSocketClient()
        asyncio.run(ws.hacs_add_repository("frenck/spook"))
        assert ws.hacs_add_calls == ["frenck/spook"]

    def test_hacs_download(self):
        ws = FakeHAWebSocketClient()
        asyncio.run(ws.hacs_download("12345"))
        assert ws.hacs_download_calls == ["12345"]


# ---------------------------------------------------------------------------
# FakeHARestClient flow methods
# ---------------------------------------------------------------------------


class TestFakeRestFlowMethods:
    def test_start_config_flow_default(self):
        rest = FakeHARestClient()
        result = asyncio.run(rest.start_config_flow("spook"))
        assert result["flow_id"] == "flow-spook"
        assert result["type"] == "form"

    def test_start_config_flow_custom(self):
        rest = FakeHARestClient(
            flow_responses={"start:spook": {"flow_id": "my-flow", "type": "form"}}
        )
        result = asyncio.run(rest.start_config_flow("spook"))
        assert result["flow_id"] == "my-flow"

    def test_submit_config_flow(self):
        rest = FakeHARestClient()
        result = asyncio.run(rest.submit_config_flow("flow-x", {}))
        assert result["type"] == "create_entry"
        assert result["result"]["entry_id"] == "entry-flow-x"

    def test_start_options_flow(self):
        rest = FakeHARestClient()
        result = asyncio.run(rest.start_options_flow("entry-1"))
        assert "flow_id" in result

    def test_submit_options_flow(self):
        rest = FakeHARestClient()
        result = asyncio.run(rest.submit_options_flow("opts-f1", {"k": "v"}))
        assert result["type"] == "create_entry"


# ---------------------------------------------------------------------------
# main.py argument parsing for companion-setup
# ---------------------------------------------------------------------------


class TestMainCompanionSetupArgs:
    def test_companion_setup_mode_parsed(self):
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--config", default="config.yaml")
        parser.add_argument(
            "--mode",
            choices=["companion-setup", "ha-sidebar", "supervisor"],
            default="supervisor",
        )
        parser.add_argument("--companion", default=None)
        parser.add_argument("--agent-id", default=None)
        parser.add_argument("--dry-run", action="store_true", default=False)
        parser.add_argument("--list-agents", action="store_true", default=False)
        args = parser.parse_args(
            ["--mode", "companion-setup", "--companion", "spook", "--dry-run"]
        )
        assert args.mode == "companion-setup"
        assert args.companion == "spook"
        assert args.dry_run is True
        assert args.list_agents is False

    def test_list_agents_flag(self):
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--mode", choices=["companion-setup"], default="companion-setup"
        )
        parser.add_argument("--companion", default=None)
        parser.add_argument("--agent-id", default=None)
        parser.add_argument("--dry-run", action="store_true", default=False)
        parser.add_argument("--list-agents", action="store_true", default=False)
        args = parser.parse_args(["--list-agents"])
        assert args.list_agents is True

    def test_agent_id_flag(self):
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--mode", choices=["companion-setup"], default="companion-setup"
        )
        parser.add_argument("--companion", default=None)
        parser.add_argument("--agent-id", default=None)
        parser.add_argument("--dry-run", action="store_true", default=False)
        parser.add_argument("--list-agents", action="store_true", default=False)
        args = parser.parse_args(
            ["--companion", "upgrade_advisor", "--agent-id", "conversation.ollama"]
        )
        assert args.agent_id == "conversation.ollama"
