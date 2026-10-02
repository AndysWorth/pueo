"""Tests for utils/ha/device_summarizer.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.ha.device_summarizer import compress_device_list, device_context_summary
from utils.ha.ha_environment import HAEnvironmentProfile


class TestCompressDeviceList:
    def test_empty_returns_empty_string(self):
        assert compress_device_list([]) == ""

    def test_single_device(self):
        devices = [{"manufacturer": "Philips", "model": "Hue Bulb"}]
        result = compress_device_list(devices)
        assert result == "1× Philips Hue Bulb"

    def test_five_identical_devices(self):
        devices = [{"manufacturer": "Philips", "model": "Hue Bulb"}] * 5
        result = compress_device_list(devices)
        assert result == "5× Philips Hue Bulb"

    def test_sorted_by_count_descending(self):
        devices = [{"manufacturer": "Ikea", "model": "Tradfri"}] * 3 + [
            {"manufacturer": "Philips", "model": "Hue"}
        ] * 7
        lines = compress_device_list(devices).splitlines()
        assert lines[0].startswith("7×")
        assert lines[1].startswith("3×")

    def test_missing_fields_fallback(self):
        devices = [{"manufacturer": "", "model": ""}]
        result = compress_device_list(devices)
        assert "Unknown device" in result

    def test_mixed_missing_fields(self):
        devices = [{"manufacturer": "Sony", "model": ""}]
        result = compress_device_list(devices)
        assert "Sony" in result
        assert "1×" in result


class TestDeviceContextSummary:
    def test_returns_empty_for_none_profile(self):
        assert device_context_summary(None) == ""

    def test_returns_empty_for_empty_profile(self):
        profile = HAEnvironmentProfile()
        assert device_context_summary(profile) == ""

    def test_minimal_profile_ha_version(self):
        profile = HAEnvironmentProfile(ha_version="2026.9.0")
        result = device_context_summary(profile)
        assert "2026.9.0" in result

    def test_integrations_listed(self):
        profile = HAEnvironmentProfile(
            ha_version="2026.9.0",
            installed_integrations=["mqtt", "zha", "hue"],
        )
        result = device_context_summary(profile)
        assert "Integrations" in result
        assert "mqtt" in result

    def test_integrations_truncated_at_10(self):
        integrations = [f"int_{i}" for i in range(15)]
        profile = HAEnvironmentProfile(installed_integrations=integrations)
        result = device_context_summary(profile)
        assert "and 5 more" in result

    def test_hacs_listed(self):
        profile = HAEnvironmentProfile(hacs_integrations=["custom_a", "custom_b"])
        result = device_context_summary(profile)
        assert "HACS" in result
        assert "custom_a" in result

    def test_config_keys_listed(self):
        profile = HAEnvironmentProfile(config_yaml_top_keys=["mqtt", "template"])
        result = device_context_summary(profile)
        assert "mqtt" in result
        assert "template" in result

    def test_full_profile_does_not_raise(self):
        profile = HAEnvironmentProfile(
            ha_version="2026.9.0",
            installed_integrations=["mqtt"],
            hacs_integrations=["custom_a"],
            config_yaml_top_keys=["mqtt", "template"],
        )
        result = device_context_summary(profile)
        assert isinstance(result, str)
        assert len(result) > 0
