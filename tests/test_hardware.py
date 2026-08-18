#!/usr/bin/env python3
"""Tests for utils/hardware.py — hardware detection and model recommendation logic."""

from __future__ import annotations

import importlib
import sys
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from utils.hardware import (
    CANDIDATE_MODELS,
    HardwareProfile,
    OllamaModelInfo,
    _check_model_caps,
    _parse_size_from_parts,
    apply_model_selection,
    detect_local_hardware,
    list_ollama_models,
    recommend_model,
    select_best_model,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _profile(
    ram_gb: float = 64.0, chip: str = "Apple M1 Max", cores: int = 10
) -> HardwareProfile:
    return HardwareProfile(
        chip=chip, arch="arm64", ram_gb=ram_gb, cpu_cores=cores, detected_at=_now()
    )


def _model(
    name: str, size_gb: float, has_tools: bool = True, ctx: int = 32768
) -> OllamaModelInfo:
    return OllamaModelInfo(
        name=name,
        size_gb=size_gb,
        has_tools=has_tools,
        context_length=ctx,
        last_seen_at=_now(),
    )


# ---------------------------------------------------------------------------
# detect_local_hardware
# ---------------------------------------------------------------------------


class TestDetectLocalHardware:
    def test_macos_happy_path(self):
        sysctl_results = {
            ("sysctl", "-n", "hw.memsize"): "68719476736\n",
            ("sysctl", "-n", "hw.physicalcpu"): "10\n",
        }
        profiler_out = "      Chip: Apple M1 Max\n      Memory: 64 GB\n"

        def fake_check_output(cmd, **kwargs):
            key = tuple(cmd)
            if key in sysctl_results:
                return sysctl_results[key]
            if "system_profiler" in cmd:
                return profiler_out
            raise ValueError(f"unexpected: {cmd}")

        with patch(
            "utils.hardware.subprocess.check_output", side_effect=fake_check_output
        ):
            with patch("utils.hardware.platform.system", return_value="Darwin"):
                with patch("utils.hardware.platform.machine", return_value="arm64"):
                    p = detect_local_hardware()

        assert p.chip == "Apple M1 Max"
        assert p.arch == "arm64"
        assert abs(p.ram_gb - 64.0) < 0.5
        assert p.cpu_cores == 10

    def test_linux_happy_path(self, tmp_path):
        import io

        meminfo_text = "MemTotal:       67108864 kB\nMemFree:       1000 kB\n"
        cpuinfo_text = (
            "model name\t: AMD Ryzen 9 5900X\nmodel name\t: AMD Ryzen 9 5900X\n"
        )

        def fake_open(path, *args, **kwargs):
            text = meminfo_text if "meminfo" in str(path) else cpuinfo_text
            return io.StringIO(text)

        with patch("utils.hardware.platform.system", return_value="Linux"):
            with patch("utils.hardware.platform.machine", return_value="x86_64"):
                with patch("utils.hardware.open", fake_open, create=True):
                    p = detect_local_hardware()

        assert "AMD" in p.chip
        assert p.arch == "x86_64"
        assert abs(p.ram_gb - 64.0) < 0.5
        assert p.cpu_cores == 2

    def test_fallback_on_error(self):
        with patch(
            "utils.hardware.subprocess.check_output",
            side_effect=RuntimeError("no sysctl"),
        ):
            with patch("utils.hardware.platform.system", return_value="Darwin"):
                p = detect_local_hardware()

        assert p.chip == "Unknown"
        assert p.ram_gb == 0.0
        assert p.cpu_cores == 0


# ---------------------------------------------------------------------------
# _parse_size_from_parts
# ---------------------------------------------------------------------------


class TestParseSizeFromParts:
    def test_gb(self):
        assert _parse_size_from_parts(
            ["qwen2.5-coder:7b", "abc123", "4.7", "GB", "4", "hours", "ago"]
        ) == pytest.approx(4.7)

    def test_mb(self):
        assert _parse_size_from_parts(
            ["nomic-embed-text", "abc", "986", "MB", "now"]
        ) == pytest.approx(986 / 1024.0)

    def test_no_size(self):
        assert _parse_size_from_parts(["broken-line"]) == 0.0


# ---------------------------------------------------------------------------
# _check_model_caps
# ---------------------------------------------------------------------------


class TestCheckModelCaps:
    def test_tools_detected(self):
        show_out = (
            "  Capabilities:\n    completion  tools  insert\n  context length: 32768\n"
        )
        with patch("utils.hardware.subprocess.check_output", return_value=show_out):
            has_tools, ctx = _check_model_caps("qwen2.5-coder:7b")
        assert has_tools is True
        assert ctx == 32768

    def test_no_tools(self):
        show_out = "  Capabilities:\n    completion\n  context length: 8192\n"
        with patch("utils.hardware.subprocess.check_output", return_value=show_out):
            has_tools, ctx = _check_model_caps("some-model:latest")
        assert has_tools is False
        assert ctx == 8192

    def test_subprocess_failure_returns_false(self):
        with patch(
            "utils.hardware.subprocess.check_output", side_effect=FileNotFoundError
        ):
            has_tools, ctx = _check_model_caps("missing:model")
        assert has_tools is False
        assert ctx == 0


# ---------------------------------------------------------------------------
# list_ollama_models
# ---------------------------------------------------------------------------


class TestListOllamaModels:
    def test_parses_output(self):
        ollama_list = (
            "NAME                         ID            SIZE      MODIFIED\n"
            "qwen2.5-coder:7b             abc123        4.7 GB    4 hours ago\n"
            "nomic-embed-text:latest      def456        274 MB    1 day ago\n"
            "mistral:latest               ghi789        4.4 GB    3 months ago\n"
        )
        show_tools = "  Capabilities:\n    completion  tools\n  context length: 32768\n"

        def fake_check_output(cmd, **kwargs):
            if cmd == ["ollama", "list"]:
                return ollama_list
            return show_tools  # for ollama show <name>

        with patch(
            "utils.hardware.subprocess.check_output", side_effect=fake_check_output
        ):
            models = list_ollama_models()

        names = [m.name for m in models]
        assert "qwen2.5-coder:7b" in names
        assert "mistral:latest" in names
        # embed model is skipped
        assert "nomic-embed-text:latest" not in names
        qwen = next(m for m in models if m.name == "qwen2.5-coder:7b")
        assert qwen.size_gb == pytest.approx(4.7)
        assert qwen.has_tools is True

    def test_returns_empty_when_ollama_not_running(self):
        with patch(
            "utils.hardware.subprocess.check_output", side_effect=FileNotFoundError
        ):
            models = list_ollama_models()
        assert models == []


# ---------------------------------------------------------------------------
# recommend_model
# ---------------------------------------------------------------------------


class TestRecommendModel:
    def test_picks_best_tier_for_large_ram(self):
        profile = _profile(ram_gb=64.0)
        available = [
            _model("qwen2.5-coder:32b", size_gb=19.0),
            _model("qwen2.5-coder:7b", size_gb=4.7),
        ]
        result = recommend_model(profile, available)
        assert result == "qwen2.5-coder:32b"

    def test_skips_oversized_model(self):
        # 16 GB RAM: budget = 10.4 GB → 32b (19 GB) doesn't fit; 7b (4.7 GB) does
        profile = _profile(ram_gb=16.0)
        available = [
            _model("qwen2.5-coder:32b", size_gb=19.0),
            _model("qwen2.5-coder:7b", size_gb=4.7),
        ]
        result = recommend_model(profile, available)
        assert result == "qwen2.5-coder:7b"

    def test_tools_required_skips_non_tools_model(self):
        profile = _profile(ram_gb=64.0)
        available = [
            _model("some-big-model:latest", size_gb=20.0, has_tools=False),
            _model("qwen2.5-coder:7b", size_gb=4.7, has_tools=True),
        ]
        result = recommend_model(profile, available)
        assert result == "qwen2.5-coder:7b"

    def test_returns_none_when_nothing_fits(self):
        # Only 2 GB RAM; no model fits (smallest is 4.7 GB)
        profile = _profile(ram_gb=2.0)
        available = [_model("qwen2.5-coder:7b", size_gb=4.7)]
        result = recommend_model(profile, available)
        assert result is None

    def test_returns_none_when_no_tools_models(self):
        profile = _profile(ram_gb=64.0)
        available = [_model("text-only:latest", size_gb=4.0, has_tools=False)]
        result = recommend_model(profile, available)
        assert result is None

    def test_returns_none_when_empty_list(self):
        profile = _profile(ram_gb=64.0)
        result = recommend_model(profile, [])
        assert result is None

    def test_fallback_to_largest_unknown_model(self):
        # No model matches the candidate list by name, but two fit the budget
        profile = _profile(ram_gb=32.0)
        available = [
            _model("custom-llm:latest", size_gb=12.0, has_tools=True),
            _model("another-llm:v2", size_gb=8.0, has_tools=True),
        ]
        result = recommend_model(profile, available)
        assert result == "custom-llm:latest"  # largest fitting model

    def test_variant_tag_matches_candidate(self):
        # qwen2.5-coder:32b-q4_K_M should match the "qwen2.5-coder" base
        profile = _profile(ram_gb=64.0)
        available = [_model("qwen2.5-coder:32b-q4_K_M", size_gb=19.0, has_tools=True)]
        result = recommend_model(profile, available)
        assert result == "qwen2.5-coder:32b-q4_K_M"

    def test_qwen3_32b_preferred_over_qwen25_32b(self):
        # qwen3 has better tool compliance — prefer it when both are installed
        profile = _profile(ram_gb=64.0)
        available = [
            _model("qwen3:32b", size_gb=20.0),
            _model("qwen2.5-coder:32b", size_gb=19.0),
        ]
        result = recommend_model(profile, available)
        assert result == "qwen3:32b"

    def test_qwen3_30b_moe_preferred_over_qwen25_32b(self):
        # qwen3:30b-a3b MoE fits in smaller memory; should win at tier 7
        profile = _profile(ram_gb=64.0)
        available = [
            _model("qwen3:30b-a3b", size_gb=17.0),
            _model("qwen2.5-coder:32b", size_gb=19.0),
        ]
        result = recommend_model(profile, available)
        assert result == "qwen3:30b-a3b"

    def test_qwen3_14b_preferred_over_qwen25_32b(self):
        # qwen3 family beats qwen2.5-coder at any size: better tool-call compliance
        # matters more than parameter count for Pueo's tool-calling workload.
        # Users can override via SWITCH_MODEL or the settings UI.
        profile = _profile(ram_gb=64.0)
        available = [
            _model("qwen2.5-coder:32b", size_gb=19.0),
            _model("qwen3:14b", size_gb=9.0),
        ]
        result = recommend_model(profile, available)
        assert result == "qwen3:14b"

    def test_qwen3_8b_preferred_over_qwen25_7b(self):
        # qwen3:8b (tier 4) beats qwen2.5-coder:7b (tier 3)
        profile = _profile(ram_gb=16.0)
        available = [
            _model("qwen3:8b", size_gb=5.2),
            _model("qwen2.5-coder:7b", size_gb=4.7),
        ]
        result = recommend_model(profile, available)
        assert result == "qwen3:8b"


# ---------------------------------------------------------------------------
# select_best_model
# ---------------------------------------------------------------------------


class TestSelectBestModel:
    def test_returns_current_model_when_recommendation_fails(self, isolated_config):
        import importlib
        import config

        importlib.reload(sys.modules["config"])
        import config

        with patch(
            "utils.hardware.detect_local_hardware", return_value=_profile(ram_gb=1.0)
        ):
            with patch("utils.hardware.list_ollama_models", return_value=[]):
                result = select_best_model()

        assert result == config.OLLAMA_MODEL

    def test_returns_recommended_model(self):
        with patch(
            "utils.hardware.detect_local_hardware", return_value=_profile(ram_gb=64.0)
        ):
            with patch(
                "utils.hardware.list_ollama_models",
                return_value=[_model("qwen2.5-coder:32b", 19.0)],
            ):
                result = select_best_model()

        assert result == "qwen2.5-coder:32b"


# ---------------------------------------------------------------------------
# apply_model_selection
# ---------------------------------------------------------------------------


class TestApplyModelSelection:
    def test_updates_config_and_writes_yaml(self, tmp_path, isolated_config):
        import importlib
        import yaml

        isolated_config.write_text(yaml.dump({"ollama": {"model": "qwen2.5-coder:7b"}}))
        importlib.reload(sys.modules["config"])
        import config

        apply_model_selection("qwen2.5-coder:32b")

        assert config.OLLAMA_MODEL == "qwen2.5-coder:32b"
        written = yaml.safe_load(isolated_config.read_text())
        assert written["ollama"]["model"] == "qwen2.5-coder:32b"
