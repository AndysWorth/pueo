"""Tests for tool_registry.py Pydantic schemas and ToolRegistry."""

import pytest
from pydantic import ValidationError

from utils.tool_registry import (
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

    def test_chat_registry_includes_fetch_ha_docs(self):
        reg = build_chat_tool_registry()
        assert "fetch_ha_docs" in reg

    def test_ha_registry_excludes_query_netalertx(self):
        # Sandbox engine executor has no NAX client; the tool would always error.
        reg = build_ha_tool_registry()
        assert "query_netalertx" not in reg
