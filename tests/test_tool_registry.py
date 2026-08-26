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

    def test_ha_registry_excludes_query_netalertx(self):
        # Sandbox engine executor has no NAX client; the tool would always error.
        reg = build_ha_tool_registry()
        assert "query_netalertx" not in reg

    def test_all_registries_include_save_strategy(self):
        from utils.agent.tool_registry import build_netalertx_tool_registry

        for reg in (
            build_ha_tool_registry(),
            build_netalertx_tool_registry(),
            build_chat_tool_registry(),
        ):
            assert "save_strategy" in reg

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
            "save_strategy",
            "finish_diagnosis",
        ):
            assert name in reg, f"expected {name!r} in config_analysis registry"

    def test_impact_analysis_registry_membership(self):
        from utils.agent.tool_registry import build_impact_analysis_registry

        reg = build_impact_analysis_registry()
        for name in (
            "read_file",
            "run_ha_command",
            "fetch_ha_docs",
            "query_knowledge",
            "save_strategy",
            "finish_impact_analysis",
        ):
            assert name in reg, f"expected {name!r} in impact_analysis registry"

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
            assert "search_integrations" in reg, (
                f"search_integrations missing from {registry_fn.__name__}"
            )

    def test_search_integrations_schema_has_query_param(self):
        from utils.agent.tool_registry import SEARCH_INTEGRATIONS

        props = SEARCH_INTEGRATIONS.parameters.get("properties", {})
        assert "query" in props
        assert props["query"].get("type") == "string"
        required = SEARCH_INTEGRATIONS.parameters.get("required", [])
        assert "query" in required
