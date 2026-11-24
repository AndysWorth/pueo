"""Tests for utils/ha/service_policy.py."""

import pytest

from utils.agent.autonomy import RiskLevel
from utils.ha.service_policy import classify_service_risk


class TestClassifyServiceRisk:
    """Table-driven tests for classify_service_risk."""

    @pytest.mark.parametrize(
        "domain,service,expected",
        [
            # LOW — device/notify domains
            ("light", "turn_on", RiskLevel.LOW),
            ("light", "turn_off", RiskLevel.LOW),
            ("light", "toggle", RiskLevel.LOW),
            ("switch", "turn_on", RiskLevel.LOW),
            ("fan", "set_percentage", RiskLevel.LOW),
            ("cover", "open_cover", RiskLevel.LOW),
            ("media_player", "play_media", RiskLevel.LOW),
            ("notify", "persistent_notification", RiskLevel.LOW),
            ("persistent_notification", "dismiss", RiskLevel.LOW),
            # LOW — input_* prefix
            ("input_boolean", "turn_on", RiskLevel.LOW),
            ("input_number", "set_value", RiskLevel.LOW),
            ("input_select", "select_option", RiskLevel.LOW),
            ("input_text", "set_value", RiskLevel.LOW),
            ("input_datetime", "set_datetime", RiskLevel.LOW),
            ("input_button", "press", RiskLevel.LOW),
            # MEDIUM — automation / script / scene
            ("automation", "trigger", RiskLevel.MEDIUM),
            ("automation", "turn_on", RiskLevel.MEDIUM),
            ("script", "turn_on", RiskLevel.MEDIUM),
            ("scene", "turn_on", RiskLevel.MEDIUM),
            # MEDIUM — homeassistant reload / toggle / turn
            ("homeassistant", "reload_config_entry", RiskLevel.MEDIUM),
            ("homeassistant", "reload_all", RiskLevel.MEDIUM),
            ("homeassistant", "update_entity", RiskLevel.MEDIUM),
            ("homeassistant", "toggle", RiskLevel.MEDIUM),
            ("homeassistant", "turn_on", RiskLevel.MEDIUM),
            ("homeassistant", "turn_off", RiskLevel.MEDIUM),
            # MEDIUM — group / timer / counter
            ("group", "reload", RiskLevel.MEDIUM),
            ("timer", "start", RiskLevel.MEDIUM),
            ("counter", "increment", RiskLevel.MEDIUM),
            # HIGH — everything else
            ("climate", "set_temperature", RiskLevel.HIGH),
            ("lock", "unlock", RiskLevel.HIGH),
            ("alarm_control_panel", "disarm", RiskLevel.HIGH),
            ("zwave_js", "bulk_set_partial_config_parameters", RiskLevel.HIGH),
            ("homeassistant", "check_config", RiskLevel.HIGH),
        ],
    )
    def test_risk_level(self, domain, service, expected):
        assert classify_service_risk(domain, service) == expected

    @pytest.mark.parametrize(
        "domain,service",
        [
            ("homeassistant", "restart"),
            ("homeassistant", "stop"),
            # Spook-added destructive services
            ("homeassistant", "delete_all_orphaned_entities"),
            ("homeassistant", "disable_user"),
            ("homeassistant", "enable_user"),
            ("repairs", "ignore_all"),
            ("repairs", "unignore_all"),
            ("repairs", "remove"),
            ("recorder", "import_statistics"),
            # Standard blocked set
            ("hassio", "addon_restart"),
            ("hassio", "host_reboot"),
            ("backup", "create"),
            ("recorder", "purge"),
            ("recorder", "purge_entities"),
            ("update", "install"),
            ("shell_command", "run"),
            ("python_script", "exec"),
            ("pyscript", "reload"),
        ],
    )
    def test_blocked_returns_none(self, domain, service):
        assert classify_service_risk(domain, service) is None

    @pytest.mark.parametrize(
        "domain,service",
        [
            # Mixed-case domain/service on blocked entries
            ("Homeassistant", "Restart"),
            ("HOMEASSISTANT", "RESTART"),
            ("Homeassistant", "Stop"),
            ("Hassio", "addon_restart"),
            ("HASSIO", "HOST_REBOOT"),
            ("Backup", "Create"),
            ("Recorder", "Purge"),
            ("Recorder", "Purge_Entities"),
            ("Shell_Command", "run"),
            ("Python_Script", "exec"),
            ("Pyscript", "reload"),
            ("Update", "install"),
            # Whitespace around domain/service
            ("homeassistant", " restart"),
            ("homeassistant ", "restart"),
            (" homeassistant ", " restart "),
            # Mixed case + whitespace
            ("Homeassistant", " Restart "),
        ],
    )
    def test_blocked_case_insensitive(self, domain, service):
        """Case and whitespace variants of blocked services must still return None."""
        assert classify_service_risk(domain, service) is None

    @pytest.mark.parametrize(
        "domain,service,expected",
        [
            # Mixed-case LOW domain
            ("Light", "turn_on", RiskLevel.LOW),
            ("SWITCH", "turn_on", RiskLevel.LOW),
            # Mixed-case input_ prefix → LOW (not misclassified as HIGH)
            ("Input_Boolean", "turn_on", RiskLevel.LOW),
            ("INPUT_NUMBER", "set_value", RiskLevel.LOW),
            # input_ prefix is LOW regardless of what follows
            ("input_anything_at_all", "press", RiskLevel.LOW),
            # Mixed-case MEDIUM
            ("Automation", "trigger", RiskLevel.MEDIUM),
            ("SCRIPT", "turn_on", RiskLevel.MEDIUM),
            ("Homeassistant", "reload_config_entry", RiskLevel.MEDIUM),
            # Whitespace around LOW
            (" light ", "turn_on", RiskLevel.LOW),
            ("light", " turn_on ", RiskLevel.LOW),
            # Empty service → HIGH (not a blocked service, not LOW/MEDIUM match)
            ("climate", "", RiskLevel.HIGH),
        ],
    )
    def test_risk_level_case_insensitive(self, domain, service, expected):
        """Risk tiers are case-insensitive and whitespace-tolerant."""
        assert classify_service_risk(domain, service) == expected
