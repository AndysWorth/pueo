"""Tests for inspect_custom_integration and probe_config_flow tool executor methods."""

import asyncio
import json

from utils.agent.autonomy import FakeAutonomyGate
from utils.agent.tool_executor import ToolExecutor
from utils.agent.tool_registry import (
    INSPECT_CUSTOM_INTEGRATION,
    PROBE_CONFIG_FLOW,
    build_chat_tool_registry,
    build_ha_tool_registry,
)
from utils.ha.ha_rest_client import FakeHARestClient
from utils.ha.ssh_client import FakeSSHClient
from utils.hitl.notify import FakeNotifier


def _run(coro):
    return asyncio.run(coro)


def _make_executor(ssh: FakeSSHClient, rest: FakeHARestClient) -> ToolExecutor:
    ex = ToolExecutor(
        ha_ssh_client=ssh,
        gate=FakeAutonomyGate(),  # type: ignore[arg-type]
        notifier=FakeNotifier(),
    )
    ex.set_rest_client(rest)
    return ex


_MANIFEST = json.dumps(
    {
        "domain": "noaa_it_all",
        "version": "0.11.1",
        "config_flow": True,
        "iot_class": "cloud_polling",
        "requirements": ["voluptuous"],
        "homeassistant": "2026.3.0",
    }
)

_HACS = json.dumps({"homeassistant": "2026.3.0", "name": "NOAA It All"})

_STRINGS = json.dumps(
    {
        "config": {
            "step": {
                "user": {
                    "menu_options": {
                        "location": "Weather & Surf",
                        "surf": "Surf only",
                    }
                },
                "location": {
                    "data": {
                        "latitude": "Latitude",
                        "longitude": "Longitude",
                    }
                },
            }
        }
    }
)


# ---------------------------------------------------------------------------
# inspect_custom_integration
# ---------------------------------------------------------------------------


class TestInspectCustomIntegration:
    def _ssh(self, extra: dict | None = None) -> FakeSSHClient:
        files = {
            "/config/custom_components/noaa_it_all/manifest.json": _MANIFEST,
            "/config/custom_components/noaa_it_all/hacs.json": _HACS,
            "/config/custom_components/noaa_it_all/strings.json": _STRINGS,
        }
        if extra:
            files.update(extra)
        return FakeSSHClient(file_contents=files)

    def test_returns_manifest_fields(self):
        ex = _make_executor(self._ssh(), FakeHARestClient())
        result = _run(ex._inspect_custom_integration("noaa_it_all"))
        assert result.success is True
        assert "version: 0.11.1" in result.output
        assert "config_flow: True" in result.output
        assert "cloud_polling" in result.output
        assert "voluptuous" in result.output

    def test_returns_min_ha_version_from_manifest(self):
        ex = _make_executor(self._ssh(), FakeHARestClient())
        result = _run(ex._inspect_custom_integration("noaa_it_all"))
        assert "min_ha_version (manifest): 2026.3.0" in result.output

    def test_returns_min_ha_version_from_hacs_json(self):
        # manifest without homeassistant key, hacs.json has it
        manifest_no_ha = json.dumps(
            {"domain": "noaa_it_all", "version": "0.11.1", "config_flow": True}
        )
        ssh = FakeSSHClient(
            file_contents={
                "/config/custom_components/noaa_it_all/manifest.json": manifest_no_ha,
                "/config/custom_components/noaa_it_all/hacs.json": _HACS,
                "/config/custom_components/noaa_it_all/strings.json": _STRINGS,
            }
        )
        ex = _make_executor(ssh, FakeHARestClient())
        result = _run(ex._inspect_custom_integration("noaa_it_all"))
        assert "min_ha_version (hacs.json): 2026.3.0" in result.output

    def test_returns_step_labels_from_strings(self):
        ex = _make_executor(self._ssh(), FakeHARestClient())
        result = _run(ex._inspect_custom_integration("noaa_it_all"))
        assert "Weather & Surf" in result.output
        assert "Latitude" in result.output

    def test_falls_back_to_translations_en_json(self):
        ssh = FakeSSHClient(
            file_contents={
                "/config/custom_components/noaa_it_all/manifest.json": _MANIFEST,
                "/config/custom_components/noaa_it_all/translations/en.json": _STRINGS,
            }
        )
        ex = _make_executor(ssh, FakeHARestClient())
        result = _run(ex._inspect_custom_integration("noaa_it_all"))
        assert "Weather & Surf" in result.output

    def test_succeeds_without_hacs_json(self):
        ssh = FakeSSHClient(
            file_contents={
                "/config/custom_components/noaa_it_all/manifest.json": _MANIFEST,
            }
        )
        ex = _make_executor(ssh, FakeHARestClient())
        result = _run(ex._inspect_custom_integration("noaa_it_all"))
        assert result.success is True
        assert "config_flow: True" in result.output

    def test_fails_when_manifest_missing(self):
        ex = _make_executor(FakeSSHClient(), FakeHARestClient())
        result = _run(ex._inspect_custom_integration("no_such_domain"))
        assert result.success is False
        assert "manifest.json" in result.error

    def test_rejects_path_traversal_in_domain(self):
        ex = _make_executor(FakeSSHClient(), FakeHARestClient())
        for bad in ("../etc/passwd", "a/b", "foo/../bar"):
            result = _run(ex._inspect_custom_integration(bad))
            assert result.success is False
            assert "Invalid domain" in result.error


# ---------------------------------------------------------------------------
# probe_config_flow
# ---------------------------------------------------------------------------


class TestProbeConfigFlow:
    def _ssh(self) -> FakeSSHClient:
        return FakeSSHClient(
            file_contents={
                "/config/custom_components/noaa_it_all/strings.json": _STRINGS,
            }
        )

    def _rest_menu(self) -> FakeHARestClient:
        return FakeHARestClient(
            flow_responses={
                "start:noaa_it_all": {
                    "flow_id": "probe-f1",
                    "type": "menu",
                    "step_id": "user",
                    "menu_options": ["location", "surf"],
                }
            }
        )

    def _rest_form(self) -> FakeHARestClient:
        return FakeHARestClient(
            flow_responses={
                "start:noaa_it_all": {
                    "flow_id": "probe-f2",
                    "type": "form",
                    "step_id": "location",
                    "data_schema": [
                        {"name": "latitude", "required": True, "default": 42.6},
                        {"name": "longitude", "required": True},
                    ],
                }
            }
        )

    def test_returns_menu_step_summary(self):
        ex = _make_executor(self._ssh(), self._rest_menu())
        result = _run(ex._probe_config_flow("noaa_it_all"))
        assert result.success is True
        assert "Step: user (menu)" in result.output
        assert "Weather & Surf" in result.output  # label from translations

    def test_returns_form_step_summary(self):
        ex = _make_executor(self._ssh(), self._rest_form())
        result = _run(ex._probe_config_flow("noaa_it_all"))
        assert result.success is True
        assert "Step: location (form)" in result.output
        assert "latitude" in result.output

    def test_always_aborts_on_menu_step(self):
        rest = self._rest_menu()
        ex = _make_executor(self._ssh(), rest)
        _run(ex._probe_config_flow("noaa_it_all"))
        assert "/api/config/config_entries/flow/probe-f1" in rest.deleted

    def test_always_aborts_on_form_step(self):
        rest = self._rest_form()
        ex = _make_executor(self._ssh(), rest)
        _run(ex._probe_config_flow("noaa_it_all"))
        assert "/api/config/config_entries/flow/probe-f2" in rest.deleted

    def test_aborts_on_exception(self):
        rest = FakeHARestClient(
            flow_responses={
                "start:noaa_it_all": {
                    "flow_id": "probe-exc",
                    "type": "form",
                    "step_id": "user",
                    "data_schema": [],
                }
            }
        )

        async def raise_after_start(handler):
            return {
                "flow_id": "probe-exc",
                "type": "form",
                "step_id": "user",
                "data_schema": [],
            }

        # Make summarize_flow_step fail by patching it
        import utils.ha.config_flow_driver as _driver

        original = _driver.summarize_flow_step

        def boom(resp, translations=None):
            raise RuntimeError("oops")

        _driver.summarize_flow_step = boom
        try:
            ex = _make_executor(self._ssh(), rest)
            result = _run(ex._probe_config_flow("noaa_it_all"))
        finally:
            _driver.summarize_flow_step = original

        assert result.success is False
        assert "/api/config/config_entries/flow/probe-exc" in rest.deleted

    def test_fails_without_rest_client(self):
        ex = ToolExecutor(
            ha_ssh_client=self._ssh(),
            gate=FakeAutonomyGate(),  # type: ignore[arg-type]
            notifier=FakeNotifier(),
        )
        result = _run(ex._probe_config_flow("noaa_it_all"))
        assert result.success is False
        assert "REST client" in result.error

    def test_rejects_path_traversal_in_domain(self):
        ex = _make_executor(self._ssh(), self._rest_menu())
        for bad in ("../etc", "a/b"):
            result = _run(ex._probe_config_flow(bad))
            assert result.success is False
            assert "Invalid domain" in result.error

    def test_output_includes_flow_id(self):
        ex = _make_executor(self._ssh(), self._rest_menu())
        result = _run(ex._probe_config_flow("noaa_it_all"))
        assert "probe-f1" in result.output


# ---------------------------------------------------------------------------
# Registry membership
# ---------------------------------------------------------------------------


class TestRegistryMembership:
    def test_inspect_custom_integration_in_ha_registry(self):
        reg = build_ha_tool_registry()
        assert "inspect_custom_integration" in reg

    def test_inspect_custom_integration_in_chat_registry(self):
        reg = build_chat_tool_registry()
        assert "inspect_custom_integration" in reg

    def test_probe_config_flow_in_ha_registry(self):
        reg = build_ha_tool_registry()
        assert "probe_config_flow" in reg

    def test_probe_config_flow_in_chat_registry(self):
        reg = build_chat_tool_registry()
        assert "probe_config_flow" in reg
