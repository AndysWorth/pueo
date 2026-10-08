"""Unit tests for utils/knowledge/runbook_signature.py."""

import pytest

from utils.knowledge.runbook_signature import (
    _part,
    _sanitize,
    sig_chat,
    sig_log,
    sig_lovelace,
    sig_notification,
    sig_repair,
    sig_update,
)


class TestPart:
    def test_basic_lowercase(self):
        assert _part("Recorder") == "recorder"

    def test_spaces_become_underscores(self):
        assert _part("home assistant") == "home_assistant"

    def test_slashes_become_underscores(self):
        assert _part("homeassistant/components") == "homeassistant_components"

    def test_strips_special_characters(self):
        assert _part("foo-bar!baz") == "foobarbaz"

    def test_truncates_to_max_len(self):
        long = "a" * 100
        result = _part(long)
        assert len(result) == 64

    def test_empty_string_returns_unknown(self):
        assert _part("") == "unknown"

    def test_none_returns_unknown(self):
        assert _part(None) == "unknown"

    def test_only_special_chars_returns_unknown(self):
        assert _part("---!!!") == "unknown"

    def test_colons_stripped(self):
        # colons are not allowed in a single part
        assert _part("foo:bar") == "foobar"

    def test_numeric_allowed(self):
        assert _part("error404") == "error404"

    def test_underscore_preserved(self):
        assert _part("backup_not_available") == "backup_not_available"


class TestSanitize:
    def test_preserves_colons(self):
        assert _sanitize("repair:recorder:backup_not_available") == (
            "repair:recorder:backup_not_available"
        )

    def test_lowercases(self):
        assert _sanitize("Repair:Recorder") == "repair:recorder"

    def test_spaces_become_underscores(self):
        assert _sanitize("a b:c d") == "a_b:c_d"

    def test_slashes_become_underscores(self):
        assert _sanitize("a/b:c/d") == "a_b:c_d"

    def test_strips_non_alphanumeric_except_colon_underscore(self):
        assert _sanitize("foo!bar:baz") == "foobar:baz"

    def test_none_returns_unknown(self):
        assert _sanitize(None) == "unknown"

    def test_empty_returns_unknown(self):
        assert _sanitize("") == "unknown"

    def test_only_special_chars_returns_unknown(self):
        assert _sanitize("!!!") == "unknown"


class TestSigRepair:
    def test_basic(self):
        assert sig_repair("recorder", "backup_not_available") == (
            "repair:recorder:backup_not_available"
        )

    def test_uppercased_domain_normalised(self):
        assert sig_repair("Recorder", "Backup_Not_Available").startswith(
            "repair:recorder:"
        )

    def test_empty_domain(self):
        assert sig_repair("", "some_key") == "repair:unknown:some_key"

    def test_empty_key(self):
        assert sig_repair("recorder", "") == "repair:recorder:unknown"

    def test_both_empty(self):
        assert sig_repair("", "") == "repair:unknown:unknown"

    def test_slash_in_domain(self):
        assert (
            sig_repair("homeassistant/core", "key") == "repair:homeassistant_core:key"
        )


class TestSigUpdate:
    def test_core(self):
        assert sig_update("core", "homeassistant") == "update:core:homeassistant"

    def test_os(self):
        assert sig_update("os", "homeassistant") == "update:os:homeassistant"

    def test_addon(self):
        assert sig_update("addon", "mosquitto") == "update:addon:mosquitto"

    def test_hacs(self):
        assert sig_update("hacs", "lovelace-mushroom") == "update:hacs:lovelacemushroom"

    def test_empty_slug(self):
        assert sig_update("core", "") == "update:core:unknown"

    def test_empty_type(self):
        assert sig_update("", "ha") == "update:unknown:ha"


class TestSigLog:
    def test_basic(self):
        assert (
            sig_log("homeassistant.components.recorder", "DatabaseError")
            == "log:homeassistant_components_recorder:databaseerror"
        )

    def test_none_exception_class(self):
        assert sig_log("homeassistant.core", None) == "log:homeassistant_core:none"

    def test_empty_exception_class(self):
        assert sig_log("homeassistant.core", "") == "log:homeassistant_core:none"

    def test_empty_logger(self):
        assert sig_log("", "ValueError") == "log:unknown:valueerror"

    def test_dots_become_underscores(self):
        result = sig_log("foo.bar", "Error")
        assert result == "log:foo_bar:error"


class TestSigLovelace:
    def test_basic(self):
        assert sig_lovelace("sensor", "unavailable") == "lovelace:sensor:unavailable"

    def test_uppercased(self):
        assert sig_lovelace("Climate", "Error") == "lovelace:climate:error"

    def test_empty_domain(self):
        assert sig_lovelace("", "unavailable") == "lovelace:unknown:unavailable"

    def test_empty_failure_kind(self):
        assert sig_lovelace("sensor", "") == "lovelace:sensor:unknown"


class TestSigNotification:
    def test_dot_separator(self):
        assert sig_notification("homeassistant.backup") == "notification:homeassistant"

    def test_underscore_separator(self):
        assert sig_notification("update_all_package") == "notification:update"

    def test_no_separator(self):
        assert sig_notification("firmware") == "notification:firmware"

    def test_empty(self):
        assert sig_notification("") == "notification:unknown"

    def test_leading_underscore(self):
        # split on _ gives "" first → _part("") → "unknown"
        assert sig_notification("_config") == "notification:unknown"

    def test_uppercase_prefix(self):
        assert sig_notification("Config.some_key") == "notification:config"


class TestSigChat:
    def test_with_sig(self):
        assert sig_chat("repair:recorder:backup_not_available") == (
            "chat:repair:recorder:backup_not_available"
        )

    def test_none_returns_unclassified(self):
        assert sig_chat(None) == "chat:unclassified"

    def test_empty_string_returns_unclassified(self):
        assert sig_chat("") == "chat:unclassified"

    def test_normalises_case(self):
        assert sig_chat("Update:Core:HA") == "chat:update:core:ha"

    def test_nested_chat_preserved(self):
        result = sig_chat("chat:repair:recorder:key")
        assert result == "chat:chat:repair:recorder:key"
