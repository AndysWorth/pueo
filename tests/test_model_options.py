#!/usr/bin/env python3
"""Tests for utils/llm/model_options.py — ModelCallOptions derivation."""

from __future__ import annotations

import pytest

from utils.llm.model_options import ModelCallOptions, _safe_ctx, derive_call_options


def _derive(**overrides):
    """Return derive_call_options with sensible defaults, overridable per test."""
    defaults = dict(
        has_thinking=True,
        recommended_temperature=0.6,
        context_length=32768,
        available_ram_gb=20.0,
        debug_level=0,
        think_mode_cfg="auto",
        num_ctx_override=0,
        supervisor_mode=False,
    )
    defaults.update(overrides)
    return derive_call_options(**defaults)


# ---------------------------------------------------------------------------
# _safe_ctx
# ---------------------------------------------------------------------------


class TestSafeCtx:
    def test_high_ram_uses_131072_cap(self):
        assert _safe_ctx(200000, 40.0) == 131072

    def test_medium_ram_caps_at_32768(self):
        assert _safe_ctx(200000, 20.0) == 32768

    def test_low_ram_caps_at_16384(self):
        assert _safe_ctx(200000, 10.0) == 16384

    def test_very_low_ram_caps_at_8192(self):
        assert _safe_ctx(200000, 5.0) == 8192

    def test_model_ctx_shorter_than_cap_is_respected(self):
        assert _safe_ctx(8192, 64.0) == 8192

    def test_zero_context_length_falls_back_to_cap(self):
        assert _safe_ctx(0, 64.0) == 131072


# ---------------------------------------------------------------------------
# derive_call_options — think
# ---------------------------------------------------------------------------


class TestDeriveThink:
    def test_auto_debug_0_returns_false(self):
        opts = _derive(has_thinking=True, think_mode_cfg="auto", debug_level=0)
        assert opts.think is False

    def test_auto_debug_1_returns_low(self):
        opts = _derive(has_thinking=True, think_mode_cfg="auto", debug_level=1)
        assert opts.think == "low"

    def test_auto_debug_2_returns_high(self):
        opts = _derive(has_thinking=True, think_mode_cfg="auto", debug_level=2)
        assert opts.think == "high"

    def test_off_overrides_debug_level(self):
        opts = _derive(has_thinking=True, think_mode_cfg="off", debug_level=2)
        assert opts.think is False

    def test_fixed_mode_medium(self):
        opts = _derive(has_thinking=True, think_mode_cfg="medium", debug_level=0)
        assert opts.think == "medium"

    def test_no_thinking_model_returns_none(self):
        opts = _derive(has_thinking=False, think_mode_cfg="auto", debug_level=2)
        assert opts.think is None

    def test_unknown_think_mode_defaults_false(self):
        opts = _derive(has_thinking=True, think_mode_cfg="bogus", debug_level=0)
        assert opts.think is False


# ---------------------------------------------------------------------------
# derive_call_options — num_ctx
# ---------------------------------------------------------------------------


class TestDeriveNumCtx:
    def test_derives_from_context_length(self):
        opts = _derive(context_length=16384, available_ram_gb=64.0)
        assert opts.num_ctx == 16384

    def test_override_wins_over_derived(self):
        opts = _derive(
            context_length=131072, available_ram_gb=64.0, num_ctx_override=4096
        )
        assert opts.num_ctx == 4096

    def test_ram_constrains_ctx(self):
        # 5 GB available → cap 8192, even if model supports more
        opts = _derive(context_length=131072, available_ram_gb=5.0)
        assert opts.num_ctx == 8192


# ---------------------------------------------------------------------------
# derive_call_options — temperature
# ---------------------------------------------------------------------------


class TestDeriveTemperature:
    def test_thinking_active_uses_model_rec_temp(self):
        # think='low' is truthy → use recommended_temperature
        opts = _derive(
            has_thinking=True,
            think_mode_cfg="auto",
            debug_level=1,  # → think='low'
            recommended_temperature=0.6,
        )
        assert opts.temperature == pytest.approx(0.6)

    def test_no_thinking_uses_zero(self):
        opts = _derive(has_thinking=False, recommended_temperature=0.6)
        assert opts.temperature == pytest.approx(0.0)

    def test_think_false_uses_zero_even_with_rec_temp(self):
        # think=False (production auto) is falsy → temperature stays 0.0
        opts = _derive(
            has_thinking=True,
            think_mode_cfg="auto",
            debug_level=0,  # → think=False
            recommended_temperature=0.6,
        )
        assert opts.temperature == pytest.approx(0.0)

    def test_thinking_without_rec_temp_uses_zero(self):
        opts = _derive(
            has_thinking=True,
            think_mode_cfg="auto",
            debug_level=1,
            recommended_temperature=None,
        )
        assert opts.temperature == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# derive_call_options — keep_alive
# ---------------------------------------------------------------------------


class TestDeriveKeepAlive:
    def test_supervisor_mode_keeps_alive_forever(self):
        opts = _derive(supervisor_mode=True)
        assert opts.keep_alive == "-1"

    def test_oneshot_mode_uses_5m(self):
        opts = _derive(supervisor_mode=False)
        assert opts.keep_alive == "5m"


# ---------------------------------------------------------------------------
# derive_call_options — num_predict
# ---------------------------------------------------------------------------


class TestDeriveNumPredict:
    def test_one_shot_caps_at_1024(self):
        opts = derive_call_options(
            has_thinking=False,
            recommended_temperature=None,
            context_length=32768,
            available_ram_gb=20.0,
            debug_level=0,
            think_mode_cfg="auto",
            num_ctx_override=0,
            supervisor_mode=False,
            one_shot=True,
        )
        assert opts.num_predict == 1024

    def test_agent_loop_has_no_limit(self):
        opts = _derive()
        assert opts.num_predict is None

    def test_seed_passed_through(self):
        opts = derive_call_options(
            has_thinking=False,
            recommended_temperature=None,
            context_length=32768,
            available_ram_gb=20.0,
            debug_level=0,
            think_mode_cfg="auto",
            num_ctx_override=0,
            supervisor_mode=False,
            seed=42,
        )
        assert opts.seed == 42
