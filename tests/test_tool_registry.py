"""Tests for tool_registry.py Pydantic schemas and ToolRegistry."""

import pytest
from pydantic import ValidationError

from utils.agent.tool_registry import (
    FixEnrichment,
    build_chat_tool_registry,
    build_ha_tool_registry,
    build_netalertx_tool_registry,
)


class TestFixEnrichment:
    def test_valid_construction(self):
        e = FixEnrichment(
            relevant_config_section="http:\n  server_port: 8123",
            explanation="The http integration port is missing.",
            confidence="high",
            suggested_fix_summary="Add server_port: 8123 under http:",
        )
        assert e.confidence == "high"
        assert e.suggested_fix_summary == "Add server_port: 8123 under http:"

    def test_suggested_fix_summary_optional(self):
        e = FixEnrichment(
            relevant_config_section="homeassistant:",
            explanation="Minimal config detected.",
            confidence="low",
        )
        assert e.suggested_fix_summary is None

    def test_missing_required_field_raises(self):
        with pytest.raises(ValidationError):
            FixEnrichment(
                relevant_config_section="x:",
                confidence="medium",
                # explanation is missing
            )

    def test_invalid_confidence_raises(self):
        with pytest.raises(ValidationError):
            FixEnrichment(
                relevant_config_section="x:",
                explanation="ok",
                confidence="very_high",  # not in Literal
            )

    def test_json_round_trip(self):
        e = FixEnrichment(
            relevant_config_section="logger:\n  default: warning",
            explanation="Logger level is too verbose.",
            confidence="medium",
        )
        serialised = e.model_dump_json()
        restored = FixEnrichment.model_validate_json(serialised)
        assert restored == e


class TestRegistryMembership:
    def test_ha_registry_includes_read_source(self):
        reg = build_ha_tool_registry()
        assert "read_source" in reg

    def test_ha_registry_includes_fetch_ha_docs(self):
        reg = build_ha_tool_registry()
        assert "fetch_ha_docs" in reg

    def test_netalertx_registry_includes_read_source(self):
        reg = build_netalertx_tool_registry()
        assert "read_source" in reg

    def test_ha_registry_includes_investigate_device(self):
        reg = build_ha_tool_registry()
        assert "investigate_device" in reg

    def test_netalertx_registry_includes_investigate_device_and_query_knowledge(self):
        reg = build_netalertx_tool_registry()
        assert "investigate_device" in reg
        assert "query_knowledge" in reg

    def test_chat_registry_includes_fetch_ha_docs(self):
        reg = build_chat_tool_registry()
        assert "fetch_ha_docs" in reg

    def test_chat_registry_includes_nax_action_tools(self):
        reg = build_chat_tool_registry()
        assert "restart_netalertx" in reg
        assert "rewrite_netalertx_conf" in reg

    def test_chat_registry_includes_dashboard_health(self):
        reg = build_chat_tool_registry()
        assert "get_dashboard_entity_health" in reg

    def test_ha_registry_excludes_query_netalertx(self):
        # Sandbox engine executor has no NAX client; the tool would always error.
        reg = build_ha_tool_registry()
        assert "query_netalertx" not in reg

    def test_all_registries_include_save_runbook(self):
        from utils.agent.tool_registry import build_netalertx_tool_registry

        for reg in (
            build_ha_tool_registry(),
            build_netalertx_tool_registry(),
            build_chat_tool_registry(),
        ):
            assert "save_runbook" in reg

    def test_all_registries_include_log_reading_tools(self):
        from utils.agent.tool_registry import build_netalertx_tool_registry

        for reg in (
            build_ha_tool_registry(),
            build_netalertx_tool_registry(),
            build_chat_tool_registry(),
        ):
            assert "read_pueo_log" in reg
            assert "search_log" in reg

    def test_config_analysis_registry_membership(self):
        from utils.agent.tool_registry import build_config_analysis_registry

        reg = build_config_analysis_registry()
        for name in (
            "read_file",
            "run_ha_command",
            "query_knowledge",
            "save_runbook",
            "finish_diagnosis",
        ):
            assert name in reg, f"expected {name!r} in config_analysis registry"

    def test_update_analysis_registry_membership(self):
        from utils.agent.tool_registry import build_update_analysis_registry

        reg = build_update_analysis_registry()
        for name in (
            "get_update_release_notes",
            "get_pueo_command_catalog",
            "check_config_against_breaking_change",
            "query_knowledge",
            "save_runbook",
            "finish_update_analysis",
        ):
            assert name in reg, f"expected {name!r} in update_analysis registry"

    def test_ha_registry_includes_get_ha_profile(self):
        reg = build_ha_tool_registry()
        assert "get_ha_profile" in reg

    def test_get_ha_profile_schema_has_field_param(self):
        from utils.agent.tool_registry import GET_HA_PROFILE

        props = GET_HA_PROFILE.parameters.get("properties", {})
        assert "field" in props
        assert props["field"].get("type") == "string"
        assert "enum" in props["field"]

    def test_search_integrations_in_all_registries(self):
        from utils.agent.tool_registry import (
            build_chat_tool_registry,
            build_code_proposal_registry,
            build_ha_tool_registry,
            build_netalertx_tool_registry,
        )

        for registry_fn in (
            build_ha_tool_registry,
            build_netalertx_tool_registry,
            build_chat_tool_registry,
            build_code_proposal_registry,
        ):
            reg = registry_fn()
            assert (
                "search_integrations" in reg
            ), f"search_integrations missing from {registry_fn.__name__}"

    def test_search_integrations_schema_has_query_param(self):
        from utils.agent.tool_registry import SEARCH_INTEGRATIONS

        props = SEARCH_INTEGRATIONS.parameters.get("properties", {})
        assert "query" in props
        assert props["query"].get("type") == "string"
        required = SEARCH_INTEGRATIONS.parameters.get("required", [])
        assert "query" in required

    def test_search_ha_docs_in_all_registries(self):
        from utils.agent.tool_registry import (
            build_chat_tool_registry,
            build_code_proposal_registry,
            build_ha_tool_registry,
            build_netalertx_tool_registry,
        )

        for registry_fn in (
            build_ha_tool_registry,
            build_netalertx_tool_registry,
            build_chat_tool_registry,
            build_code_proposal_registry,
        ):
            reg = registry_fn()
            assert (
                "search_ha_docs" in reg
            ), f"search_ha_docs missing from {registry_fn.__name__}"

    def test_search_ha_docs_schema_has_query_param(self):
        from utils.agent.tool_registry import SEARCH_HA_DOCS

        props = SEARCH_HA_DOCS.parameters.get("properties", {})
        assert "query" in props
        assert props["query"].get("type") == "string"
        required = SEARCH_HA_DOCS.parameters.get("required", [])
        assert "query" in required

    def test_history_logbook_template_in_ha_and_chat_registries(self):
        from utils.agent.tool_registry import (
            build_chat_tool_registry,
            build_ha_tool_registry,
        )

        for registry_fn, label in (
            (build_ha_tool_registry, "ha"),
            (build_chat_tool_registry, "chat"),
        ):
            reg = registry_fn()
            for tool_name in (
                "get_entity_history",
                "get_logbook",
                "render_ha_template",
            ):
                assert tool_name in reg, f"{tool_name!r} missing from {label} registry"

    def test_history_logbook_in_lovelace_registry(self):
        from utils.agent.tool_registry import build_lovelace_investigation_registry

        reg = build_lovelace_investigation_registry()
        assert "get_entity_history" in reg
        assert "get_logbook" in reg

    def test_render_ha_template_not_in_lovelace_registry(self):
        from utils.agent.tool_registry import build_lovelace_investigation_registry

        reg = build_lovelace_investigation_registry()
        assert "render_ha_template" not in reg

    def test_get_entity_history_schema(self):
        from utils.agent.tool_registry import GET_ENTITY_HISTORY

        props = GET_ENTITY_HISTORY.parameters.get("properties", {})
        assert "entity_id" in props
        assert "hours" in props
        required = GET_ENTITY_HISTORY.parameters.get("required", [])
        assert "entity_id" in required
        assert "hours" not in required  # optional

    def test_render_ha_template_schema(self):
        from utils.agent.tool_registry import RENDER_HA_TEMPLATE

        props = RENDER_HA_TEMPLATE.parameters.get("properties", {})
        assert "template" in props
        required = RENDER_HA_TEMPLATE.parameters.get("required", [])
        assert "template" in required

    def test_new_tools_in_mcp_names(self):
        from utils.mcp.pueo_mcp_server import _MCP_TOOL_NAMES

        for tool_name in ("get_entity_history", "get_logbook", "render_ha_template"):
            assert (
                tool_name in _MCP_TOOL_NAMES
            ), f"{tool_name!r} missing from _MCP_TOOL_NAMES"

    def test_get_system_error_log_in_ha_chat_notification_registries(self):
        from utils.agent.tool_registry import (
            build_chat_tool_registry,
            build_ha_tool_registry,
            build_notification_investigation_registry,
        )

        for registry_fn, label in (
            (build_ha_tool_registry, "ha"),
            (build_chat_tool_registry, "chat"),
            (build_notification_investigation_registry, "notification_investigation"),
        ):
            reg = registry_fn()
            assert (
                "get_system_error_log" in reg
            ), f"get_system_error_log missing from {label} registry"

    def test_get_system_error_log_schema(self):
        from utils.agent.tool_registry import GET_SYSTEM_ERROR_LOG

        props = GET_SYSTEM_ERROR_LOG.parameters.get("properties", {})
        assert "level" in props
        assert "limit" in props
        assert "logger_filter" in props
        required = GET_SYSTEM_ERROR_LOG.parameters.get("required", [])
        assert required == []  # all params optional

    def test_get_system_error_log_in_mcp_names(self):
        from utils.mcp.pueo_mcp_server import _MCP_TOOL_NAMES

        assert "get_system_error_log" in _MCP_TOOL_NAMES

    def test_get_area_layout_in_ha_chat_lovelace_registries(self):
        from utils.agent.tool_registry import (
            build_chat_tool_registry,
            build_ha_tool_registry,
            build_lovelace_investigation_registry,
        )

        for registry_fn, label in (
            (build_ha_tool_registry, "ha"),
            (build_chat_tool_registry, "chat"),
            (build_lovelace_investigation_registry, "lovelace_investigation"),
        ):
            reg = registry_fn()
            assert (
                "get_area_layout" in reg
            ), f"get_area_layout missing from {label} registry"

    def test_get_area_layout_schema(self):
        from utils.agent.tool_registry import GET_AREA_LAYOUT

        props = GET_AREA_LAYOUT.parameters.get("properties", {})
        assert "area" in props
        required = GET_AREA_LAYOUT.parameters.get("required", [])
        assert required == []  # area is optional

    def test_get_area_layout_in_mcp_names(self):
        from utils.mcp.pueo_mcp_server import _MCP_TOOL_NAMES

        assert "get_area_layout" in _MCP_TOOL_NAMES

    def test_registry_schema_token_budget(self):
        """Token cost of chat + ha registry schemas must stay within a safe ceiling."""
        import json

        from utils.agent.tool_registry import (
            build_chat_tool_registry,
            build_ha_tool_registry,
        )
        from utils.core.context import estimate_tokens

        chat_tokens = estimate_tokens(
            json.dumps(build_chat_tool_registry().get_ollama_tools())
        )
        ha_tokens = estimate_tokens(
            json.dumps(build_ha_tool_registry().get_ollama_tools())
        )

        # Ceiling = observed value at test-write time + 25% headroom.
        # chat registry: ~7000 tokens; ha registry: ~5000 tokens.
        # Bump these numbers (with the same +25% formula) when you intentionally
        # add new tools, not as a shortcut past a surprise regression.
        CHAT_CEILING = 10_000
        HA_CEILING = 8_000
        assert chat_tokens <= CHAT_CEILING, (
            f"Chat registry schema is {chat_tokens} tokens (ceiling {CHAT_CEILING}). "
            "Either the registry grew unexpectedly or the ceiling needs bumping."
        )
        assert ha_tokens <= HA_CEILING, (
            f"HA registry schema is {ha_tokens} tokens (ceiling {HA_CEILING}). "
            "Either the registry grew unexpectedly or the ceiling needs bumping."
        )

    def test_get_automation_traces_in_ha_chat_registries(self):
        from utils.agent.tool_registry import (
            build_ha_tool_registry,
            build_chat_tool_registry,
        )

        for label, reg in (
            ("ha", build_ha_tool_registry()),
            ("chat", build_chat_tool_registry()),
        ):
            assert (
                "get_automation_traces" in reg
            ), f"get_automation_traces missing from {label} registry"

    def test_get_automation_traces_schema(self):
        from utils.agent.tool_registry import GET_AUTOMATION_TRACES

        props = GET_AUTOMATION_TRACES.parameters.get("properties", {})
        assert "entity_id" in props
        assert "run_id" in props
        required = GET_AUTOMATION_TRACES.parameters.get("required", [])
        assert "entity_id" in required
        assert "run_id" not in required

    def test_get_automation_traces_in_mcp_names(self):
        from utils.mcp.pueo_mcp_server import _MCP_TOOL_NAMES

        assert "get_automation_traces" in _MCP_TOOL_NAMES

    def test_get_integration_diagnostics_in_ha_chat_registries(self):
        from utils.agent.tool_registry import (
            build_chat_tool_registry,
            build_ha_tool_registry,
        )

        for label, reg in (
            ("ha", build_ha_tool_registry()),
            ("chat", build_chat_tool_registry()),
        ):
            assert (
                "get_integration_diagnostics" in reg
            ), f"get_integration_diagnostics missing from {label} registry"

    def test_get_integration_diagnostics_in_mcp_names(self):
        from utils.mcp.pueo_mcp_server import _MCP_TOOL_NAMES

        assert "get_integration_diagnostics" in _MCP_TOOL_NAMES

    def test_get_integration_diagnostics_schema(self):
        from utils.agent.tool_registry import GET_INTEGRATION_DIAGNOSTICS

        props = GET_INTEGRATION_DIAGNOSTICS.parameters.get("properties", {})
        assert "domain_or_entry_id" in props
        required = GET_INTEGRATION_DIAGNOSTICS.parameters.get("required", [])
        assert "domain_or_entry_id" in required

    def test_reload_integration_in_ha_chat_registries(self):
        from utils.agent.tool_registry import (
            build_chat_tool_registry,
            build_ha_tool_registry,
        )

        for label, reg in (
            ("ha", build_ha_tool_registry()),
            ("chat", build_chat_tool_registry()),
        ):
            assert (
                "reload_integration" in reg
            ), f"reload_integration missing from {label} registry"

    def test_reload_integration_not_in_mcp_names(self):
        from utils.mcp.pueo_mcp_server import _MCP_TOOL_NAMES

        assert "reload_integration" not in _MCP_TOOL_NAMES

    def test_reload_integration_schema(self):
        from utils.agent.tool_registry import RELOAD_INTEGRATION

        props = RELOAD_INTEGRATION.parameters.get("properties", {})
        assert "domain_or_entry_id" in props
        assert "reason" in props
        required = RELOAD_INTEGRATION.parameters.get("required", [])
        assert "domain_or_entry_id" in required
        assert "reason" in required

    def test_call_service_in_ha_chat_registries(self):
        from utils.agent.tool_registry import (
            build_chat_tool_registry,
            build_ha_tool_registry,
        )

        for label, reg in (
            ("ha", build_ha_tool_registry()),
            ("chat", build_chat_tool_registry()),
        ):
            assert "call_service" in reg, f"call_service missing from {label} registry"

    def test_call_service_not_in_mcp_names(self):
        from utils.mcp.pueo_mcp_server import _MCP_TOOL_NAMES

        assert "call_service" not in _MCP_TOOL_NAMES

    def test_call_service_schema(self):
        from utils.agent.tool_registry import CALL_SERVICE

        props = CALL_SERVICE.parameters.get("properties", {})
        assert "domain" in props
        assert "service" in props
        assert "reason" in props
        required = CALL_SERVICE.parameters.get("required", [])
        assert "domain" in required
        assert "service" in required
        assert "reason" in required

    def test_get_recent_events_in_ha_and_chat_registries(self):
        from utils.agent.tool_registry import (
            build_chat_tool_registry,
            build_ha_tool_registry,
        )

        for label, reg in (
            ("ha", build_ha_tool_registry()),
            ("chat", build_chat_tool_registry()),
        ):
            assert (
                "get_recent_events" in reg
            ), f"get_recent_events missing from {label} registry"

    def test_get_recent_events_in_mcp_names(self):
        from utils.mcp.pueo_mcp_server import _MCP_TOOL_NAMES

        assert "get_recent_events" in _MCP_TOOL_NAMES

    def test_get_recent_events_schema(self):
        from utils.agent.tool_registry import GET_RECENT_EVENTS

        props = GET_RECENT_EVENTS.parameters.get("properties", {})
        assert "event_type" in props
        assert "entity_id" in props
        assert "limit" in props
        required = GET_RECENT_EVENTS.parameters.get("required", [])
        assert required == []  # all params optional

    def test_get_spook_issues_in_lovelace_registry(self):
        from utils.agent.tool_registry import build_lovelace_investigation_registry

        reg = build_lovelace_investigation_registry()
        assert "get_spook_issues" in reg

    def test_get_spook_issues_in_update_analysis_registry(self):
        from utils.agent.tool_registry import build_update_analysis_registry

        reg = build_update_analysis_registry()
        assert "get_spook_issues" in reg

    def test_get_spook_issues_in_mcp_names(self):
        from utils.mcp.pueo_mcp_server import _MCP_TOOL_NAMES

        assert "get_spook_issues" in _MCP_TOOL_NAMES

    def test_get_spook_issues_not_in_code_proposal_registry(self):
        from utils.agent.tool_registry import build_code_proposal_registry

        reg = build_code_proposal_registry()
        assert "get_spook_issues" not in reg

    def test_get_statistics_in_ha_and_chat_registries(self):
        from utils.agent.tool_registry import (
            build_chat_tool_registry,
            build_ha_tool_registry,
        )

        for registry_fn, label in (
            (build_ha_tool_registry, "ha"),
            (build_chat_tool_registry, "chat"),
        ):
            reg = registry_fn()
            assert (
                "get_statistics" in reg
            ), f"get_statistics missing from {label} registry"

    def test_get_statistics_in_mcp_names(self):
        from utils.mcp.pueo_mcp_server import _MCP_TOOL_NAMES

        assert "get_statistics" in _MCP_TOOL_NAMES

    def test_get_statistics_schema(self):
        from utils.agent.tool_registry import GET_STATISTICS

        props = GET_STATISTICS.parameters.get("properties", {})
        assert "statistic_ids" in props
        assert "start_time" in props
        assert "end_time" in props
        assert "period" in props
        assert "types" in props
        assert "units" in props
        required = GET_STATISTICS.parameters.get("required", [])
        assert "statistic_ids" in required
        assert "start_time" in required
        assert "period" in required
        assert "types" in required
        assert "end_time" not in required  # optional
        assert "units" not in required  # optional

    def test_get_statistics_not_in_code_proposal_registry(self):
        from utils.agent.tool_registry import build_code_proposal_registry

        reg = build_code_proposal_registry()
        assert "get_statistics" not in reg

    def test_propose_automation_in_chat_registry_only(self):
        """propose_automation is in chat registry but not in ha/netalertx/code-proposal."""
        from utils.agent.tool_registry import (
            build_chat_tool_registry,
            build_code_proposal_registry,
            build_ha_tool_registry,
            build_netalertx_tool_registry,
        )

        assert "propose_automation" in build_chat_tool_registry()
        assert "propose_automation" not in build_ha_tool_registry()
        assert "propose_automation" not in build_netalertx_tool_registry()
        assert "propose_automation" not in build_code_proposal_registry()

    def test_propose_automation_schema(self):
        """propose_automation has required alias/description/mode/trigger/action."""
        from utils.agent.tool_registry import PROPOSE_AUTOMATION

        props = PROPOSE_AUTOMATION.parameters.get("properties", {})
        assert "alias" in props
        assert "description" in props
        assert "mode" in props
        assert "trigger" in props
        assert "condition" in props
        assert "action" in props
        required = PROPOSE_AUTOMATION.parameters.get("required", [])
        assert "alias" in required
        assert "description" in required
        assert "mode" in required
        assert "trigger" in required
        assert "action" in required
        assert "condition" not in required

    def test_propose_automation_not_in_mcp(self):
        """propose_automation is never exposed via MCP."""
        from utils.mcp.pueo_mcp_server import _MCP_TOOL_NAMES

        assert "propose_automation" not in _MCP_TOOL_NAMES
