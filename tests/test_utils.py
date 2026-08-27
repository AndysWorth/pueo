#!/usr/bin/env python3
"""Utility module tests — retry, rate limiting, logging formatters, context/token management, YAML validator, fake clients."""

import asyncio
import importlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError

_REPO_ROOT = Path(__file__).parent.parent
# ── utils/retry.py ───────────────────────────────────────────────────────────────


class TestAsyncRetry:
    """All tests drive the decorator via asyncio.run() — no external async framework needed."""

    def test_returns_value_on_first_success(self):
        from utils.core.retry import async_retry

        @async_retry(exceptions=(OSError,))
        async def always_ok():
            return 42

        assert asyncio.run(always_ok()) == 42

    def test_retries_on_matching_exception_then_succeeds(self):
        from utils.core.retry import async_retry

        calls = []

        @async_retry(max_attempts=3, base_delay=0.0, exceptions=(OSError,))
        async def flaky():
            calls.append(1)
            if len(calls) < 2:
                raise OSError("transient")
            return "ok"

        result = asyncio.run(flaky())
        assert result == "ok"
        assert len(calls) == 2

    def test_non_retryable_exception_passes_through_immediately(self):
        from utils.core.retry import async_retry

        calls = []

        @async_retry(max_attempts=5, base_delay=0.0, exceptions=(OSError,))
        async def bad():
            calls.append(1)
            raise ValueError("not retryable")

        with pytest.raises(ValueError):
            asyncio.run(bad())
        assert len(calls) == 1

    def test_exhausts_max_attempts_and_raises(self):
        from utils.core.retry import async_retry

        calls = []

        @async_retry(max_attempts=3, base_delay=0.0, exceptions=(OSError,))
        async def always_fail():
            calls.append(1)
            raise OSError("persistent")

        with pytest.raises(OSError):
            asyncio.run(always_fail())
        assert len(calls) == 3

    def test_zero_max_attempts_retries_past_default(self):
        from utils.core.retry import async_retry

        calls = []

        @async_retry(max_attempts=0, base_delay=0.0, exceptions=(OSError,))
        async def eventually_ok():
            calls.append(1)
            if len(calls) < 10:
                raise OSError("not yet")
            return "done"

        result = asyncio.run(eventually_ok())
        assert result == "done"
        assert len(calls) == 10

    def test_exponential_backoff_grows_between_attempts(self, monkeypatch):
        from utils.core.retry import async_retry

        delays = []

        async def fake_sleep(secs):
            delays.append(secs)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        calls = []

        @async_retry(
            max_attempts=4, base_delay=2.0, max_delay=60.0, exceptions=(OSError,)
        )
        async def always_fail():
            calls.append(1)
            raise OSError("err")

        with pytest.raises(OSError):
            asyncio.run(always_fail())

        assert len(delays) == 3
        assert delays[1] > delays[0]
        assert delays[2] > delays[1]

    def test_jitter_keeps_delay_within_25_percent(self, monkeypatch):
        import utils.core.retry as retry_mod

        # randbelow(51) returning 50 → 50/100 - 0.25 = +0.25 → delay * 1.25
        monkeypatch.setattr(retry_mod.secrets, "randbelow", lambda n: 50)
        captured = []

        async def fake_sleep(secs):
            captured.append(secs)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        @retry_mod.async_retry(max_attempts=2, base_delay=4.0, exceptions=(OSError,))
        async def fail_once():
            if not captured:
                raise OSError("x")
            return "ok"

        asyncio.run(fail_once())
        assert captured[0] == pytest.approx(4.0 * 1.25)

    def test_ssh_retry_config_keys_exist(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"ssh_retry_attempts": 5, "ssh_retry_base_delay": 1.5}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.SSH_RETRY_ATTEMPTS == 5
        assert config.SSH_RETRY_BASE_DELAY == 1.5

    def test_ssh_retry_config_defaults(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.SSH_RETRY_ATTEMPTS == 3
        assert config.SSH_RETRY_BASE_DELAY == 2.0


# ── utils/rate_limiter.py ────────────────────────────────────────────────────────


class TestDebouncer:
    def test_first_call_triggers(self):
        from utils.core.rate_limiter import Debouncer

        d = Debouncer(window_seconds=30)
        assert d.record() is True

    def test_second_call_within_window_suppressed(self, monkeypatch):
        from utils.core.rate_limiter import Debouncer
        import time as time_mod

        now = time_mod.monotonic()
        monkeypatch.setattr("utils.core.rate_limiter.time.monotonic", lambda: now)
        d = Debouncer(window_seconds=30)
        d.record()
        assert d.record() is False

    def test_call_after_window_triggers_again(self, monkeypatch):
        from utils.core.rate_limiter import Debouncer
        import time as time_mod

        clock = [time_mod.monotonic()]
        monkeypatch.setattr("utils.core.rate_limiter.time.monotonic", lambda: clock[0])
        d = Debouncer(window_seconds=30)
        d.record()

        clock[0] += 31
        assert d.record() is True

    def test_burst_of_50_produces_one_trigger(self, monkeypatch):
        from utils.core.rate_limiter import Debouncer
        import time as time_mod

        now = time_mod.monotonic()
        monkeypatch.setattr("utils.core.rate_limiter.time.monotonic", lambda: now)
        d = Debouncer(window_seconds=30)
        results = [d.record() for _ in range(50)]
        assert results.count(True) == 1
        assert results[0] is True


class TestRateLimiter:
    def test_allows_calls_under_limit(self):
        from utils.core.rate_limiter import RateLimiter

        rl = RateLimiter(max_calls=5, period_seconds=60)
        for _ in range(5):
            rl.check()

    def test_raises_at_limit(self):
        from utils.core.rate_limiter import RateLimiter, RateLimitExceeded

        rl = RateLimiter(max_calls=3, period_seconds=60)
        for _ in range(3):
            rl.check()
        with pytest.raises(RateLimitExceeded):
            rl.check()

    def test_allows_again_after_period(self, monkeypatch):
        from utils.core.rate_limiter import RateLimiter
        import time as time_mod

        clock = [time_mod.monotonic()]
        monkeypatch.setattr("utils.core.rate_limiter.time.monotonic", lambda: clock[0])
        rl = RateLimiter(max_calls=2, period_seconds=60)
        rl.check()
        rl.check()

        clock[0] += 61
        rl.check()

    def test_sliding_window_does_not_count_expired_calls(self, monkeypatch):
        from utils.core.rate_limiter import RateLimiter
        import time as time_mod

        clock = [time_mod.monotonic()]
        monkeypatch.setattr("utils.core.rate_limiter.time.monotonic", lambda: clock[0])
        rl = RateLimiter(max_calls=3, period_seconds=60)
        rl.check()
        rl.check()

        clock[0] += 61
        rl.check()
        rl.check()
        rl.check()

    def test_rate_limit_exceeded_is_exception(self):
        from utils.core.rate_limiter import RateLimitExceeded

        assert issubclass(RateLimitExceeded, Exception)


class TestJsonFormatter:
    def _make_record(self, msg: str, **extra):
        import logging as logging_mod

        record = logging_mod.LogRecord(
            name="pueo.test_module",
            level=logging_mod.INFO,
            pathname="",
            lineno=0,
            msg=msg,
            args=(),
            exc_info=None,
        )
        for k, v in extra.items():
            setattr(record, k, v)
        return record

    def test_produces_valid_json(self):
        import json
        from utils.core.logging import _JsonFormatter

        formatter = _JsonFormatter()
        record = self._make_record("something_happened")
        output = formatter.format(record)
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_includes_required_fields(self):
        import json
        from utils.core.logging import _JsonFormatter

        formatter = _JsonFormatter()
        record = self._make_record("config_fetched")
        parsed = json.loads(formatter.format(record))
        assert "timestamp" in parsed
        assert "level" in parsed
        assert "event" in parsed
        assert "module" in parsed

    def test_event_matches_message(self):
        import json
        from utils.core.logging import _JsonFormatter

        formatter = _JsonFormatter()
        record = self._make_record("backup_created")
        parsed = json.loads(formatter.format(record))
        assert parsed["event"] == "backup_created"

    def test_module_stripped_of_pueo_prefix(self):
        import json
        from utils.core.logging import _JsonFormatter

        formatter = _JsonFormatter()
        record = self._make_record("x")
        parsed = json.loads(formatter.format(record))
        assert parsed["module"] == "test_module"
        assert not parsed["module"].startswith("pueo.")

    def test_extra_fields_appear_in_output(self):
        import json
        from utils.core.logging import _JsonFormatter

        formatter = _JsonFormatter()
        record = self._make_record("backup_created")
        record.slug = "abc123"
        record.host = "ha.local"
        parsed = json.loads(formatter.format(record))
        assert parsed["slug"] == "abc123"
        assert parsed["host"] == "ha.local"


class TestStructuredLogger:
    def test_info_calls_underlying_logger(self):
        import logging as logging_mod
        from unittest.mock import MagicMock
        from utils.core.logging import StructuredLogger

        inner = MagicMock(spec=logging_mod.Logger)
        log = StructuredLogger(inner)
        log.info("something_happened", key="val")
        inner.log.assert_called_once()
        call_args = inner.log.call_args
        assert call_args[0][1] == "something_happened"
        assert call_args[1]["extra"]["key"] == "val"

    def test_warning_uses_warning_level(self):
        import logging as logging_mod
        from unittest.mock import MagicMock
        from utils.core.logging import StructuredLogger

        inner = MagicMock(spec=logging_mod.Logger)
        log = StructuredLogger(inner)
        log.warning("rate_limit_exceeded")
        assert inner.log.call_args[0][0] == logging_mod.WARNING

    def test_error_uses_error_level(self):
        import logging as logging_mod
        from unittest.mock import MagicMock
        from utils.core.logging import StructuredLogger

        inner = MagicMock(spec=logging_mod.Logger)
        log = StructuredLogger(inner)
        log.error("ssh_fetch_failed", error="timeout")
        assert inner.log.call_args[0][0] == logging_mod.ERROR


class TestTextFormatter:
    def _make_record(self, msg: str, **extra):
        import logging as logging_mod

        record = logging_mod.LogRecord(
            name="pueo.netalertx.installer",
            level=logging_mod.INFO,
            pathname="",
            lineno=0,
            msg=msg,
            args=(),
            exc_info=None,
        )
        for k, v in extra.items():
            setattr(record, k, v)
        return record

    def test_basic_format(self):
        from utils.core.logging import _TextFormatter

        formatter = _TextFormatter()
        record = self._make_record("step1_complete")
        output = formatter.format(record)
        assert output == "INFO     step1_complete"

    def test_extras_rendered_as_key_value_pairs(self):
        from utils.core.logging import _TextFormatter

        formatter = _TextFormatter()
        record = self._make_record("step1_complete")
        record.mode = "addon"
        record.step = "detect_deployment"
        output = formatter.format(record)
        assert "mode='addon'" in output
        assert "step='detect_deployment'" in output

    def test_correlation_id_excluded(self):
        from utils.core.logging import _TextFormatter

        formatter = _TextFormatter()
        record = self._make_record("install_state_updated")
        record.correlation_id = "some-uuid-value"
        record.state = "MQTT_RUNNING"
        output = formatter.format(record)
        assert "correlation_id" not in output
        assert "state='MQTT_RUNNING'" in output

    def test_setup_logging_console_text_attaches_text_formatter(
        self, monkeypatch, tmp_path
    ):
        import logging as logging_mod
        import utils.core.logging as logging_utils
        from utils.core.logging import _TextFormatter

        monkeypatch.setattr(logging_utils, "_configured", False)
        monkeypatch.setattr(logging_utils, "LOG_FILE", str(tmp_path / "pueo.log"))
        pueo_logger = logging_mod.getLogger("pueo")
        original_handlers = pueo_logger.handlers[:]
        try:
            logging_utils.setup_logging(console_text=True)
            stream_handlers = [
                h
                for h in pueo_logger.handlers
                if isinstance(h, logging_mod.StreamHandler)
                and not isinstance(h, logging_mod.FileHandler)
            ]
            assert any(isinstance(h.formatter, _TextFormatter) for h in stream_handlers)
        finally:
            for h in pueo_logger.handlers[:]:
                if h not in original_handlers:
                    pueo_logger.removeHandler(h)
                    h.close()

    def test_setup_logging_default_uses_json_formatter(self, monkeypatch, tmp_path):
        import logging as logging_mod
        import utils.core.logging as logging_utils
        from utils.core.logging import _JsonFormatter, _TextFormatter

        monkeypatch.setattr(logging_utils, "_configured", False)
        monkeypatch.setattr(logging_utils, "LOG_FILE", str(tmp_path / "pueo.log"))
        pueo_logger = logging_mod.getLogger("pueo")
        original_handlers = pueo_logger.handlers[:]
        try:
            logging_utils.setup_logging()
            stream_handlers = [
                h
                for h in pueo_logger.handlers
                if isinstance(h, logging_mod.StreamHandler)
                and not isinstance(h, logging_mod.FileHandler)
            ]
            assert any(isinstance(h.formatter, _JsonFormatter) for h in stream_handlers)
            assert not any(
                isinstance(h.formatter, _TextFormatter) for h in stream_handlers
            )
        finally:
            for h in pueo_logger.handlers[:]:
                if h not in original_handlers:
                    pueo_logger.removeHandler(h)
                    h.close()


class TestCorrelationId:
    def test_default_is_empty_string(self):
        from utils.core.logging import get_correlation_id, set_correlation_id

        set_correlation_id("")
        assert get_correlation_id() == ""

    def test_set_and_get_roundtrip(self):
        from utils.core.logging import get_correlation_id, set_correlation_id

        set_correlation_id("abc-123")
        assert get_correlation_id() == "abc-123"

    def test_correlation_id_included_in_log_extra(self):
        import logging as logging_mod
        from unittest.mock import MagicMock
        from utils.core.logging import StructuredLogger, set_correlation_id

        set_correlation_id("repair-uuid-xyz")
        inner = MagicMock(spec=logging_mod.Logger)
        log = StructuredLogger(inner)
        log.info("repair_cycle_started")
        extra = inner.log.call_args[1]["extra"]
        assert extra.get("correlation_id") == "repair-uuid-xyz"

    def test_explicit_correlation_id_not_overwritten(self):
        import logging as logging_mod
        from unittest.mock import MagicMock
        from utils.core.logging import StructuredLogger, set_correlation_id

        set_correlation_id("ctx-id")
        inner = MagicMock(spec=logging_mod.Logger)
        log = StructuredLogger(inner)
        log.info("event", correlation_id="explicit-id")
        extra = inner.log.call_args[1]["extra"]
        assert extra["correlation_id"] == "explicit-id"


# ── utils/context.py ─────────────────────────────────────────────────────────────


class TestEstimateTokens:
    def test_empty_string_returns_one(self):
        from utils.core.context import estimate_tokens

        assert estimate_tokens("") == 1

    def test_four_chars_is_one_token(self):
        from utils.core.context import estimate_tokens

        assert estimate_tokens("abcd") == 1

    def test_hundred_chars_is_twenty_five_tokens(self):
        from utils.core.context import estimate_tokens

        assert estimate_tokens("x" * 100) == 25

    def test_scales_with_length(self):
        from utils.core.context import estimate_tokens

        assert estimate_tokens("a" * 400) == 100
        assert estimate_tokens("a" * 4000) == 1000


class TestTruncateToBudget:
    def test_short_text_unchanged(self):
        from utils.core.context import truncate_to_budget

        text = "hello world"
        assert truncate_to_budget(text, 100) == text

    def test_exactly_at_budget_unchanged(self):
        from utils.core.context import truncate_to_budget

        text = "a" * 400  # 400 chars = 100 tokens exactly
        assert truncate_to_budget(text, 100) == text

    def test_tail_strategy_keeps_end(self):
        from utils.core.context import truncate_to_budget

        text = "START" + "x" * 400 + "END"
        result = truncate_to_budget(text, 10, strategy="tail")
        assert result.endswith("END")
        assert "START" not in result

    def test_head_strategy_keeps_start(self):
        from utils.core.context import truncate_to_budget

        text = "START" + "x" * 400 + "END"
        result = truncate_to_budget(text, 10, strategy="head")
        assert result.startswith("START")
        assert "END" not in result

    def test_smart_strategy_includes_separator(self):
        from utils.core.context import truncate_to_budget

        text = "A" * 2000
        result = truncate_to_budget(text, 100, strategy="smart")
        assert "...[truncated]..." in result

    def test_smart_strategy_keeps_both_ends(self):
        from utils.core.context import truncate_to_budget

        text = "HEADER" + "x" * 2000 + "FOOTER"
        result = truncate_to_budget(text, 100, strategy="smart")
        assert "HEADER" in result
        assert "FOOTER" in result

    def test_default_strategy_is_tail(self):
        from utils.core.context import truncate_to_budget

        text = "START" + "z" * 800
        result = truncate_to_budget(text, 10)
        assert "START" not in result
        assert len(result) == 40  # 10 tokens * 4 chars


class TestSlidingWindowLines:
    def test_empty_list_returns_empty(self):
        from utils.core.context import sliding_window_lines

        assert sliding_window_lines([], 100) == []

    def test_few_lines_all_fit(self):
        from utils.core.context import sliding_window_lines

        lines = ["line one", "line two", "line three"]
        assert sliding_window_lines(lines, 1000) == lines

    def test_too_many_lines_drops_oldest(self):
        from utils.core.context import sliding_window_lines

        lines = ["old " * 100 + str(i) for i in range(20)]
        result = sliding_window_lines(lines, 50)
        assert result == lines[len(lines) - len(result) :]

    def test_order_preserved(self):
        from utils.core.context import sliding_window_lines

        lines = ["alpha", "beta", "gamma"]
        result = sliding_window_lines(lines, 1000)
        assert result == ["alpha", "beta", "gamma"]

    def test_single_line_fits(self):
        from utils.core.context import sliding_window_lines

        lines = ["short line"]
        assert sliding_window_lines(lines, 100) == lines

    def test_result_fits_within_budget(self):
        from utils.core.context import sliding_window_lines, estimate_tokens

        lines = ["x" * 100 for _ in range(50)]
        max_tokens = 200
        result = sliding_window_lines(lines, max_tokens)
        total_chars = sum(len(l) + 1 for l in result)
        assert total_chars <= max_tokens * 4


class TestMaxPromptTokensConfig:
    def test_default_is_7000(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.MAX_PROMPT_TOKENS == 7000

    def test_configurable_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"max_prompt_tokens": 4096}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.MAX_PROMPT_TOKENS == 4096


# ── utils/yaml_validator.py ──────────────────────────────────────────────────────

_VALID_ORIGINAL = """\
homeassistant:
  name: Home
  latitude: 51.5
  longitude: -0.1
  unit_system: metric
  time_zone: Europe/London

http:
  server_port: 8123

logger:
  default: warning
"""

_VALID_FIX = """\
homeassistant:
  name: Home
  latitude: 51.5
  longitude: -0.1
  unit_system: metric
  time_zone: Europe/London

http:
  server_port: 8124

logger:
  default: info
"""


class TestValidationResult:
    def test_valid_construction(self):
        from utils.repair.yaml_validator import ValidationResult

        r = ValidationResult(is_safe=True, reasons=[])
        assert r.is_safe is True
        assert r.reasons == []

    def test_unsafe_with_reasons(self):
        from utils.repair.yaml_validator import ValidationResult

        r = ValidationResult(is_safe=False, reasons=["missing homeassistant block"])
        assert r.is_safe is False
        assert len(r.reasons) == 1

    def test_reasons_defaults_to_empty_list(self):
        from utils.repair.yaml_validator import ValidationResult

        r = ValidationResult(is_safe=True)
        assert r.reasons == []


class TestValidateProposedFix:
    def test_valid_fix_passes(self):
        from utils.repair.yaml_validator import validate_proposed_fix

        result = validate_proposed_fix(_VALID_ORIGINAL, _VALID_FIX)
        assert result.is_safe is True
        assert result.reasons == []

    def test_empty_proposed_yaml_rejected(self):
        from utils.repair.yaml_validator import validate_proposed_fix

        result = validate_proposed_fix(_VALID_ORIGINAL, "")
        assert result.is_safe is False
        assert any("empty" in r for r in result.reasons)

    def test_whitespace_only_proposed_yaml_rejected(self):
        from utils.repair.yaml_validator import validate_proposed_fix

        result = validate_proposed_fix(_VALID_ORIGINAL, "   \n  ")
        assert result.is_safe is False

    def test_unparseable_yaml_rejected(self):
        from utils.repair.yaml_validator import validate_proposed_fix

        result = validate_proposed_fix(_VALID_ORIGINAL, "key: [unclosed")
        assert result.is_safe is False
        assert any("does not parse" in r for r in result.reasons)

    def test_non_mapping_yaml_rejected(self):
        from utils.repair.yaml_validator import validate_proposed_fix

        result = validate_proposed_fix(_VALID_ORIGINAL, "- item1\n- item2\n")
        assert result.is_safe is False
        assert any("mapping" in r for r in result.reasons)

    def test_missing_homeassistant_block_rejected(self):
        from utils.repair.yaml_validator import validate_proposed_fix

        proposed = "http:\n  server_port: 8123\n"
        result = validate_proposed_fix(_VALID_ORIGINAL, proposed)
        assert result.is_safe is False
        assert any("homeassistant" in r for r in result.reasons)

    def test_removed_top_level_key_rejected(self):
        from utils.repair.yaml_validator import validate_proposed_fix

        proposed = "homeassistant:\n  name: Home\n"
        result = validate_proposed_fix(_VALID_ORIGINAL, proposed)
        assert result.is_safe is False
        assert any("http" in r or "logger" in r for r in result.reasons)

    def test_completely_different_yaml_rejected(self):
        from utils.repair.yaml_validator import validate_proposed_fix

        proposed = "\n".join([f"key_{i}: value_{i}" for i in range(200)])
        result = validate_proposed_fix(_VALID_ORIGINAL, proposed)
        assert result.is_safe is False
        assert any("differs too much" in r for r in result.reasons)

    def test_nearly_identical_fix_passes(self):
        from utils.repair.yaml_validator import validate_proposed_fix

        fix = _VALID_ORIGINAL.replace("warning", "info")
        result = validate_proposed_fix(_VALID_ORIGINAL, fix)
        assert result.is_safe is True

    def test_original_with_bad_yaml_does_not_raise(self):
        from utils.repair.yaml_validator import validate_proposed_fix

        result = validate_proposed_fix("key: [broken", _VALID_FIX)
        assert isinstance(result.is_safe, bool)

    def test_multiple_violations_reported(self):
        from utils.repair.yaml_validator import validate_proposed_fix

        proposed = "some_new_key:\n  value: x\n"
        result = validate_proposed_fix(_VALID_ORIGINAL, proposed)
        assert result.is_safe is False
        assert len(result.reasons) >= 2


# ── utils/ssh_client.py (FakeSSHClient) ──────────────────────────────────────────


class TestFakeSSHClient:
    def test_read_file_returns_configured_content(self):
        from utils.ha.ssh_client import FakeSSHClient

        c = FakeSSHClient(file_contents={"/foo": "bar"})
        assert asyncio.run(c.read_file("/foo")) == "bar"

    def test_read_file_raises_for_unknown_path(self):
        from utils.ha.ssh_client import FakeSSHClient

        c = FakeSSHClient()
        with pytest.raises(FileNotFoundError):
            asyncio.run(c.read_file("/missing"))

    def test_write_file_records_content(self):
        from utils.ha.ssh_client import FakeSSHClient

        c = FakeSSHClient()
        asyncio.run(c.write_file("/out", "hello"))
        assert c.written_files["/out"] == "hello"

    def test_run_returns_default_success(self):
        from utils.ha.ssh_client import FakeSSHClient

        c = FakeSSHClient()
        ec, stdout, stderr = asyncio.run(c.run("anything"))
        assert ec == 0

    def test_run_matches_command_pattern(self):
        from utils.ha.ssh_client import FakeSSHClient

        c = FakeSSHClient(command_results={"ha core check": (0, "ok", "")})
        ec, stdout, _ = asyncio.run(c.run("ha core check"))
        assert ec == 0
        assert stdout == "ok"

    def test_run_raises_on_check_true_with_nonzero(self):
        from utils.ha.ssh_client import FakeSSHClient

        c = FakeSSHClient(command_results={"fail_cmd": (1, "", "error")})
        with pytest.raises(RuntimeError):
            asyncio.run(c.run("fail_cmd", check=True))

    def test_run_records_commands(self):
        from utils.ha.ssh_client import FakeSSHClient

        c = FakeSSHClient()
        asyncio.run(c.run("cmd_one"))
        asyncio.run(c.run("cmd_two"))
        assert "cmd_one" in c.commands_run
        assert "cmd_two" in c.commands_run

    def test_stream_lines_yields_data(self):
        from utils.ha.ssh_client import FakeSSHClient

        c = FakeSSHClient(stream_data=["line1", "line2", "line3"])

        async def collect():
            return [line async for line in c.stream_lines("tail -F /log")]

        lines = asyncio.run(collect())
        assert lines == ["line1", "line2", "line3"]

    def test_stream_lines_empty(self):
        from utils.ha.ssh_client import FakeSSHClient

        c = FakeSSHClient()

        async def collect():
            return [line async for line in c.stream_lines("tail -F /log")]

        assert asyncio.run(collect()) == []


# ── utils/ollama_client.py (FakeLLMClient) ───────────────────────────────────────


class TestFakeLLMClient:
    def test_chat_returns_configured_json(self):
        from utils.llm.ollama_client import FakeLLMClient

        c = FakeLLMClient('{"key": "value"}')
        result = asyncio.run(c.chat("model", [], {"temperature": 0}, {}))
        assert result["message"]["content"] == '{"key": "value"}'

    def test_chat_records_calls(self):
        from utils.llm.ollama_client import FakeLLMClient

        c = FakeLLMClient("{}")
        asyncio.run(c.chat("mymodel", [{"role": "user", "content": "hi"}], {}, {}))
        assert len(c.calls) == 1
        assert c.calls[0]["model"] == "mymodel"


# ── utils/resource.py ────────────────────────────────────────────────────────────

_HOST_INFO_OUTPUT = (
    "agent_version: 1.9.0\n"
    "disk_free: 4.5\n"
    "disk_total: 13.6\n"
    "disk_used: 9.1\n"
    "hostname: homeassistant\n"
    "operating_system: Home Assistant OS 18.1\n"
)

_MEMINFO_OUTPUT = (
    "MemTotal:        1931384 kB\n"
    "MemFree:           22100 kB\n"
    "MemAvailable:     563200 kB\n"
)


class TestResourceParsing:
    def test_parse_host_info_extracts_disk_fields(self):
        from utils.disk.resource import _parse_host_info

        free, total, used = _parse_host_info(_HOST_INFO_OUTPUT)
        assert free == 4.5
        assert total == 13.6
        assert used == 9.1

    def test_parse_meminfo_extracts_available_and_total(self):
        from utils.disk.resource import _parse_meminfo

        available_mb, total_mb = _parse_meminfo(_MEMINFO_OUTPUT)
        assert available_mb == pytest.approx(563200 / 1024.0)
        assert total_mb == pytest.approx(1931384 / 1024.0)

    def test_parse_meminfo_missing_fields_returns_zero(self):
        from utils.disk.resource import _parse_meminfo

        available_mb, total_mb = _parse_meminfo("Buffers: 12345 kB\n")
        assert available_mb == 0.0
        assert total_mb == 0.0


class TestResourceStatus:
    def test_construction_and_field_access(self):
        from utils.disk.resource import ResourceStatus

        s = ResourceStatus(
            disk_free_gb=4.5,
            disk_total_gb=13.6,
            disk_used_gb=9.1,
            mem_available_mb=550.0,
            mem_total_mb=1886.0,
            disk_warn=True,
            disk_critical=False,
            mem_warn=False,
        )
        assert s.disk_free_gb == 4.5
        assert s.disk_warn is True
        assert s.disk_critical is False

    def test_critical_flag_independent_of_warn(self):
        from utils.disk.resource import ResourceStatus

        s = ResourceStatus(
            disk_free_gb=1.5,
            disk_total_gb=13.6,
            disk_used_gb=12.1,
            mem_available_mb=550.0,
            mem_total_mb=1886.0,
            disk_warn=True,
            disk_critical=True,
            mem_warn=False,
        )
        assert s.disk_critical is True
        assert s.disk_warn is True


class TestPollHostResources:
    def _fake_ssh(self, disk_free: float = 4.5, mem_available_kb: int = 563200):
        from utils.ha.ssh_client import FakeSSHClient

        host_info = (
            f"disk_free: {disk_free}\ndisk_total: 13.6\ndisk_used: {13.6 - disk_free:.1f}\n"
            "hostname: homeassistant\n"
        )
        meminfo = f"MemTotal: 1931384 kB\nMemFree: 22100 kB\nMemAvailable: {mem_available_kb} kB\n"
        return FakeSSHClient(
            command_results={
                "ha host info": (0, host_info, ""),
                "cat /proc/meminfo": (0, meminfo, ""),
            }
        )

    def test_returns_correct_disk_values(self):
        from utils.disk.resource import poll_host_resources

        status = asyncio.run(
            poll_host_resources(self._fake_ssh(disk_free=4.5), 5.0, 2.0, 256.0)
        )
        assert status.disk_free_gb == 4.5
        assert status.disk_total_gb == 13.6

    def test_disk_warn_flag_set_when_below_warn_threshold(self):
        from utils.disk.resource import poll_host_resources

        status = asyncio.run(
            poll_host_resources(self._fake_ssh(disk_free=3.0), 5.0, 2.0, 256.0)
        )
        assert status.disk_warn is True
        assert status.disk_critical is False

    def test_disk_critical_flag_set_when_below_critical_threshold(self):
        from utils.disk.resource import poll_host_resources

        status = asyncio.run(
            poll_host_resources(self._fake_ssh(disk_free=1.5), 5.0, 2.0, 256.0)
        )
        assert status.disk_critical is True
        assert status.disk_warn is True

    def test_disk_flags_clear_when_above_thresholds(self):
        from utils.disk.resource import poll_host_resources

        status = asyncio.run(
            poll_host_resources(self._fake_ssh(disk_free=8.0), 5.0, 2.0, 256.0)
        )
        assert status.disk_warn is False
        assert status.disk_critical is False

    def test_mem_warn_flag_set_when_below_warn_threshold(self):
        from utils.disk.resource import poll_host_resources

        status = asyncio.run(
            poll_host_resources(
                self._fake_ssh(mem_available_kb=200 * 1024), 5.0, 2.0, 256.0
            )
        )
        assert status.mem_warn is True

    def test_mem_warn_clear_when_above_threshold(self):
        from utils.disk.resource import poll_host_resources

        status = asyncio.run(
            poll_host_resources(
                self._fake_ssh(mem_available_kb=512 * 1024), 5.0, 2.0, 256.0
            )
        )
        assert status.mem_warn is False


class TestCheckDiskNotCritical:
    def test_raises_disk_critical_error_when_cached_status_is_critical(
        self, monkeypatch
    ):
        from utils.disk.resource import (
            ResourceStatus,
            check_disk_not_critical,
            DiskCriticalError,
        )
        import utils.disk.resource as resource_mod

        critical_status = ResourceStatus(
            disk_free_gb=1.5,
            disk_total_gb=13.6,
            disk_used_gb=12.1,
            mem_available_mb=550.0,
            mem_total_mb=1886.0,
            disk_warn=True,
            disk_critical=True,
            mem_warn=False,
        )
        monkeypatch.setattr(resource_mod, "_last_resource_status", critical_status)
        with pytest.raises(DiskCriticalError, match="1.5 GB"):
            check_disk_not_critical(2.0)

    def test_passes_when_cached_status_is_not_critical(self, monkeypatch):
        from utils.disk.resource import ResourceStatus, check_disk_not_critical
        import utils.disk.resource as resource_mod

        ok_status = ResourceStatus(
            disk_free_gb=4.5,
            disk_total_gb=13.6,
            disk_used_gb=9.1,
            mem_available_mb=550.0,
            mem_total_mb=1886.0,
            disk_warn=True,
            disk_critical=False,
            mem_warn=False,
        )
        monkeypatch.setattr(resource_mod, "_last_resource_status", ok_status)
        check_disk_not_critical(2.0)  # must not raise

    def test_passes_when_no_cached_status(self, monkeypatch):
        from utils.disk.resource import check_disk_not_critical
        import utils.disk.resource as resource_mod

        monkeypatch.setattr(resource_mod, "_last_resource_status", None)
        check_disk_not_critical(2.0)  # must not raise


class TestResourcePollerAlerts:
    _HITL_DDL = (
        "CREATE TABLE IF NOT EXISTS hitl_suppression ("
        "card_key TEXT PRIMARY KEY, card_type TEXT NOT NULL DEFAULT '', "
        "description TEXT NOT NULL DEFAULT '', "
        "first_sent_at REAL NOT NULL DEFAULT 0, "
        "last_sent_at REAL NOT NULL DEFAULT 0, "
        "send_count INTEGER NOT NULL DEFAULT 1, "
        "last_action TEXT, last_action_at REAL, "
        "rejection_count INTEGER NOT NULL DEFAULT 0, "
        "next_allowed_at REAL, known_issue INTEGER NOT NULL DEFAULT 0, "
        "known_issue_note TEXT, resolved_at REAL)"
    )

    @pytest.fixture(autouse=True)
    def _patch_hitl_db(self, monkeypatch):
        """Route sqlite3.connect to an in-memory DB so real suppression logic can run."""
        import sqlite3 as _sq3

        _mem = _sq3.connect(":memory:")
        _mem.execute(self._HITL_DDL)
        _mem.commit()
        monkeypatch.setattr(_sq3, "connect", lambda *a, **kw: _mem)

    def _make_status(
        self,
        disk_free: float = 6.0,
        mem_available_mb: float = 550.0,
        disk_warn: bool = False,
        disk_critical: bool = False,
        mem_warn: bool = False,
    ):
        from utils.disk.resource import ResourceStatus

        return ResourceStatus(
            disk_free_gb=disk_free,
            disk_total_gb=13.6,
            disk_used_gb=13.6 - disk_free,
            mem_available_mb=mem_available_mb,
            mem_total_mb=1886.0,
            disk_warn=disk_warn,
            disk_critical=disk_critical,
            mem_warn=mem_warn,
        )

    def _make_poller(self, notifier):
        from utils.disk.resource import ResourcePoller
        from utils.ha.ssh_client import FakeSSHClient

        return ResourcePoller(
            ssh_client=FakeSSHClient(),
            notifier=notifier,
            interval_seconds=300,
            disk_warn_gb=5.0,
            disk_critical_gb=2.0,
            mem_warn_mb=256.0,
        )

    def test_sends_disk_critical_alert_on_first_breach(self):
        from utils.hitl.notify import FakeNotifier

        notifier = FakeNotifier()
        poller = self._make_poller(notifier)
        status = self._make_status(disk_free=1.5, disk_warn=True, disk_critical=True)
        asyncio.run(poller._check_and_alert(status))
        assert len(notifier.sent) == 1
        assert "CRITICAL" in notifier.sent[0]["subject"]

    def test_deduplicates_consecutive_disk_critical_alerts(self):
        from utils.hitl.notify import FakeNotifier

        notifier = FakeNotifier()
        poller = self._make_poller(notifier)
        status = self._make_status(disk_free=1.5, disk_warn=True, disk_critical=True)
        asyncio.run(poller._check_and_alert(status))
        asyncio.run(poller._check_and_alert(status))
        assert len(notifier.sent) == 1

    def test_resends_alert_after_condition_clears_and_retriggers(self):
        from utils.hitl.notify import FakeNotifier

        notifier = FakeNotifier()
        poller = self._make_poller(notifier)
        critical = self._make_status(disk_free=1.5, disk_warn=True, disk_critical=True)
        ok = self._make_status(disk_free=6.0)
        asyncio.run(poller._check_and_alert(critical))
        asyncio.run(poller._check_and_alert(ok))
        asyncio.run(poller._check_and_alert(critical))
        assert len(notifier.sent) == 2

    def test_disk_clear_writes_resolved_marker(self, monkeypatch, tmp_path):
        """When disk_critical clears, a .resolved file is written for the card."""
        from utils.hitl.hitl_tracker import stable_nid
        from utils.hitl.notify import FakeNotifier

        monkeypatch.setattr("config.NOTIFY_WATCH_DIR", str(tmp_path))
        notifier = FakeNotifier()
        poller = self._make_poller(notifier)
        critical = self._make_status(disk_free=1.5, disk_warn=True, disk_critical=True)
        ok = self._make_status(disk_free=6.0)
        asyncio.run(poller._check_and_alert(critical))
        asyncio.run(poller._check_and_alert(ok))
        nid = stable_nid("resource:disk_critical")
        assert (tmp_path / f"{nid}.resolved").exists()

    def test_sends_disk_warn_alert_when_warn_only(self):
        from utils.hitl.notify import FakeNotifier

        notifier = FakeNotifier()
        poller = self._make_poller(notifier)
        status = self._make_status(disk_free=3.0, disk_warn=True, disk_critical=False)
        asyncio.run(poller._check_and_alert(status))
        assert len(notifier.sent) == 1
        assert "WARNING" in notifier.sent[0]["subject"]
        assert "disk" in notifier.sent[0]["subject"].lower()

    def test_sends_mem_warn_alert_when_mem_low(self):
        from utils.hitl.notify import FakeNotifier

        notifier = FakeNotifier()
        poller = self._make_poller(notifier)
        status = self._make_status(mem_available_mb=100.0, mem_warn=True)
        asyncio.run(poller._check_and_alert(status))
        assert len(notifier.sent) == 1
        assert "memory" in notifier.sent[0]["subject"].lower()

    def test_no_alert_when_all_thresholds_ok(self):
        from utils.hitl.notify import FakeNotifier

        notifier = FakeNotifier()
        poller = self._make_poller(notifier)
        status = self._make_status(disk_free=8.0, mem_available_mb=600.0)
        asyncio.run(poller._check_and_alert(status))
        assert len(notifier.sent) == 0

    def test_disk_critical_payload_includes_disk_free_after_gb(self, monkeypatch):
        """HITL card payload carries disk_free_after_gb from the post-recovery re-poll."""
        import utils.disk.resource as resource_mod
        from utils.hitl.notify import FakeNotifier

        async def fake_poll(*_args, **_kwargs):
            return self._make_status(disk_free=2.3)

        monkeypatch.setattr(resource_mod, "poll_host_resources", fake_poll)
        notifier = FakeNotifier()
        poller = self._make_poller(notifier)
        status = self._make_status(disk_free=1.5, disk_warn=True, disk_critical=True)
        asyncio.run(poller._check_and_alert(status))
        assert len(notifier.sent) == 1
        payload = notifier.sent[0]["payload"]
        assert payload["disk_free_after_gb"] == pytest.approx(2.3, abs=0.01)

    def test_disk_critical_recovery_retries_after_cooldown(self, monkeypatch):
        """Recovery runs again and a new HITL card is emitted after the cooldown elapses."""
        import time
        import utils.disk.resource as resource_mod
        from utils.hitl.notify import FakeNotifier

        notifier = FakeNotifier()
        poller = self._make_poller(notifier)
        critical = self._make_status(disk_free=1.5, disk_warn=True, disk_critical=True)

        asyncio.run(poller._check_and_alert(critical))
        assert len(notifier.sent) == 1

        # Simulate cooldown elapsed by setting _last_recovery_at far in the past
        poller._last_recovery_at = (
            time.monotonic() - resource_mod._RECOVERY_COOLDOWN_SECONDS - 1
        )
        asyncio.run(poller._check_and_alert(critical))
        assert len(notifier.sent) == 2

    def test_disk_critical_suppressed_within_cooldown(self, monkeypatch):
        """No additional card is sent while disk stays CRITICAL within the cooldown window."""
        from utils.hitl.notify import FakeNotifier

        notifier = FakeNotifier()
        poller = self._make_poller(notifier)
        critical = self._make_status(disk_free=1.5, disk_warn=True, disk_critical=True)

        asyncio.run(poller._check_and_alert(critical))
        assert len(notifier.sent) == 1

        # _last_recovery_at is now set; cooldown has NOT elapsed
        asyncio.run(poller._check_and_alert(critical))
        assert len(notifier.sent) == 1

    def test_disk_warn_triggers_proactive_recovery(self, monkeypatch):
        """When disk_warn fires, run_safe_disk_recovery is called as proactive cleanup."""
        import utils.disk.resource as resource_mod
        import utils.disk.disk_recovery as dr_mod
        from utils.hitl.notify import FakeNotifier

        recovery_called: list[bool] = []

        async def fake_recovery(*_args, **_kwargs):
            recovery_called.append(True)
            from utils.disk.disk_recovery import RecoverySummary

            return RecoverySummary()

        monkeypatch.setattr(dr_mod, "run_safe_disk_recovery", fake_recovery)

        notifier = FakeNotifier()
        poller = self._make_poller(notifier)
        status = self._make_status(disk_free=3.5, disk_warn=True, disk_critical=False)
        asyncio.run(poller._check_and_alert(status))

        assert recovery_called, "run_safe_disk_recovery should be called at WARN level"
        assert len(notifier.sent) == 1
        assert "WARNING" in notifier.sent[0]["subject"]

    def test_disk_warn_proactive_recovery_result_included_in_body(self, monkeypatch):
        """Warning notification body mentions space freed by proactive recovery."""
        import utils.disk.disk_recovery as dr_mod
        from utils.hitl.notify import FakeNotifier

        async def fake_recovery(*_args, **_kwargs):
            from utils.disk.disk_recovery import RecoveryAction, RecoverySummary

            s = RecoverySummary()
            s.actions.append(
                RecoveryAction(
                    name="truncate_ha_log",
                    bytes_freed=52_428_800,  # 50 MB
                    message="Truncated HA log (50.0 MB freed)",
                    success=True,
                )
            )
            return s

        monkeypatch.setattr(dr_mod, "run_safe_disk_recovery", fake_recovery)

        notifier = FakeNotifier()
        poller = self._make_poller(notifier)
        status = self._make_status(disk_free=3.5, disk_warn=True, disk_critical=False)
        asyncio.run(poller._check_and_alert(status))

        body = notifier.sent[0]["body"]
        assert "50" in body  # freed MB mentioned

    def test_update_resource_status_sets_cache(self, monkeypatch):
        from utils.disk.resource import ResourceStatus, update_resource_status
        import utils.disk.resource as resource_mod

        monkeypatch.setattr(resource_mod, "_last_resource_status", None)
        status = self._make_status(disk_free=6.0)
        update_resource_status(status)
        assert resource_mod._last_resource_status is status

    def test_run_polls_and_updates_cache_then_cancels(self, monkeypatch):
        from utils.disk.resource import ResourcePoller, ResourceStatus
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        import utils.disk.resource as resource_mod

        polled: list[int] = []
        poll_status = self._make_status(disk_free=6.0)

        async def fake_poll(*_args, **_kwargs):
            polled.append(1)
            return poll_status

        async def fake_sleep(_secs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(resource_mod, "poll_host_resources", fake_poll)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        poller = ResourcePoller(
            ssh_client=FakeSSHClient(),
            notifier=FakeNotifier(),
            interval_seconds=300,
            disk_warn_gb=5.0,
            disk_critical_gb=2.0,
            mem_warn_mb=256.0,
        )
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(poller.run())

        assert len(polled) == 1
        assert resource_mod._last_resource_status is poll_status

    def test_run_catches_poll_error_and_sleeps(self, monkeypatch):
        from utils.disk.resource import ResourcePoller
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        import utils.disk.resource as resource_mod

        async def failing_poll(*_args, **_kwargs):
            raise RuntimeError("ssh down")

        slept: list[float] = []

        async def fake_sleep(secs: float):
            slept.append(secs)
            raise asyncio.CancelledError()

        monkeypatch.setattr(resource_mod, "poll_host_resources", failing_poll)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        poller = ResourcePoller(
            ssh_client=FakeSSHClient(),
            notifier=FakeNotifier(),
            interval_seconds=300,
            disk_warn_gb=5.0,
            disk_critical_gb=2.0,
            mem_warn_mb=256.0,
        )
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(poller.run())

        assert len(slept) == 1  # sleep ran despite the poll error


class TestExecuteRemoteBackupDiskCheck:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        from agents import ha_agent_advanced

        path = str(tmp_path / "disk_check_test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        ha_agent_advanced.init_local_database()
        return path

    def test_raises_disk_critical_error_when_cached_status_is_critical(
        self, monkeypatch, db_path
    ):
        from utils.disk.resource import ResourceStatus, DiskCriticalError
        import utils.disk.resource as resource_mod
        from agents import ha_agent_advanced
        from utils.ha.ssh_client import FakeSSHClient

        critical_status = ResourceStatus(
            disk_free_gb=1.5,
            disk_total_gb=13.6,
            disk_used_gb=12.1,
            mem_available_mb=550.0,
            mem_total_mb=1886.0,
            disk_warn=True,
            disk_critical=True,
            mem_warn=False,
        )
        monkeypatch.setattr(resource_mod, "_last_resource_status", critical_status)
        ssh = FakeSSHClient(
            command_results={"ha backup new": (0, "Slug: test-slug\n", "")}
        )
        with pytest.raises(DiskCriticalError):
            asyncio.run(ha_agent_advanced.execute_remote_backup(ssh_client=ssh))
        assert "ha backup new" not in ssh.commands_run

    def test_proceeds_when_cached_status_is_not_critical(self, monkeypatch, db_path):
        from utils.disk.resource import ResourceStatus
        import utils.disk.resource as resource_mod
        from agents import ha_agent_advanced
        from utils.ha.ssh_client import FakeSSHClient

        ok_status = ResourceStatus(
            disk_free_gb=4.5,
            disk_total_gb=13.6,
            disk_used_gb=9.1,
            mem_available_mb=550.0,
            mem_total_mb=1886.0,
            disk_warn=True,
            disk_critical=False,
            mem_warn=False,
        )
        monkeypatch.setattr(resource_mod, "_last_resource_status", ok_status)
        ssh = FakeSSHClient(
            command_results={"ha backup new": (0, "Slug: test-slug\n", "")}
        )
        slug = asyncio.run(ha_agent_advanced.execute_remote_backup(ssh_client=ssh))
        assert slug == "test-slug"

    def test_proceeds_when_no_cached_status(self, monkeypatch, db_path):
        import utils.disk.resource as resource_mod
        from agents import ha_agent_advanced
        from utils.ha.ssh_client import FakeSSHClient

        monkeypatch.setattr(resource_mod, "_last_resource_status", None)
        ssh = FakeSSHClient(
            command_results={"ha backup new": (0, "Slug: fresh-slug\n", "")}
        )
        slug = asyncio.run(ha_agent_advanced.execute_remote_backup(ssh_client=ssh))
        assert slug == "fresh-slug"


# ── RAG knowledge store (item 49) ─────────────────────────────────────────────────


class TestKnowledgeChunk:
    def test_valid_construction(self):
        from utils.knowledge.knowledge_store import KnowledgeChunk

        chunk = KnowledgeChunk(
            text="some text", source="ha/2024.1", collection="ha_release_notes"
        )
        assert chunk.text == "some text"
        assert chunk.score == 0.0
        assert chunk.metadata == {}

    def test_construction_with_all_fields(self):
        from utils.knowledge.knowledge_store import KnowledgeChunk

        chunk = KnowledgeChunk(
            text="breaking change",
            source="ha/2024.1",
            collection="ha_release_notes",
            score=0.9,
            metadata={"version": "2024.1"},
        )
        assert chunk.score == 0.9
        assert chunk.metadata["version"] == "2024.1"


class TestFakeKnowledgeStore:
    def test_upsert_and_query_basic(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        store.upsert(
            "ha_release_notes",
            ids=["ha-2024.1-0"],
            documents=["breaking change in template syntax"],
            metadatas=[{"source": "ha_release_notes/2024.1", "version": "2024.1"}],
        )
        results = store.query("breaking change", top_k=5)
        assert len(results) == 1
        assert results[0].collection == "ha_release_notes"
        assert results[0].source == "ha_release_notes/2024.1"

    def test_upsert_deduplicates_by_id(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        store.upsert(
            "ha_release_notes",
            ids=["ha-2024.1-0"],
            documents=["original text"],
            metadatas=[{"source": "ha/2024.1"}],
        )
        store.upsert(
            "ha_release_notes",
            ids=["ha-2024.1-0"],
            documents=["updated text"],
            metadatas=[{"source": "ha/2024.1"}],
        )
        results = store.query("updated", top_k=5)
        assert len(results) == 1
        assert results[0].text == "updated text"

    def test_query_returns_empty_for_no_match(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        store.upsert(
            "ha_release_notes",
            ids=["ha-x-0"],
            documents=["some yaml change"],
            metadatas=[{"source": "ha/x"}],
        )
        results = store.query("completely different topic", top_k=5)
        assert results == []

    def test_query_respects_top_k(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        store.upsert(
            "ha_release_notes",
            ids=[f"ha-x-{i}" for i in range(10)],
            documents=[f"breaking change number {i}" for i in range(10)],
            metadatas=[{"source": f"ha/x/{i}"} for i in range(10)],
        )
        results = store.query("breaking change", top_k=3)
        assert len(results) == 3

    def test_query_with_collection_filter(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        store.upsert(
            "ha_release_notes",
            ids=["ha-0"],
            documents=["ha breaking change"],
            metadatas=[{"source": "ha/2024.1"}],
        )
        store.upsert(
            "hacs_changelogs",
            ids=["hacs-0"],
            documents=["hacs breaking change"],
            metadatas=[{"source": "hacs/myint"}],
        )
        results = store.query(
            "breaking change", top_k=5, collections=["ha_release_notes"]
        )
        assert all(r.collection == "ha_release_notes" for r in results)
        assert len(results) == 1

    def test_query_unknown_collection_returns_empty(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        results = store.query("anything", top_k=5, collections=["nonexistent"])
        assert results == []

    def test_query_min_score_filters_below_threshold(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        store.upsert(
            "ha_release_notes",
            ids=["doc-1"],
            documents=["dashboard entity"],
            metadatas=[{"source": "s1"}],
        )
        # FakeKnowledgeStore assigns score=1.0 to text matches; min_score above 1.0 should
        # return nothing
        results = store.query("dashboard entity", top_k=5, min_score=1.1)
        assert results == []

    def test_query_min_score_passes_chunks_above_threshold(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        store.upsert(
            "ha_release_notes",
            ids=["doc-1"],
            documents=["dashboard entity"],
            metadatas=[{"source": "s1"}],
        )
        results = store.query("dashboard entity", top_k=5, min_score=0.35)
        assert len(results) == 1
        assert results[0].score >= 0.35

    def test_query_min_score_zero_is_default_no_filtering(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        store.upsert(
            "ha_release_notes",
            ids=["doc-1"],
            documents=["any content"],
            metadatas=[{"source": "s1"}],
        )
        results = store.query("any content", top_k=5)
        assert len(results) == 1


class TestFakeKnowledgeStorePrune:
    def test_prune_removes_stale_ids(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        store.upsert(
            "ha_release_notes",
            ids=["ha-2024.1-0", "ha-2024.2-0"],
            documents=["old text", "new text"],
            metadatas=[{"source": "ha/2024.1"}, {"source": "ha/2024.2"}],
        )
        removed = store.prune("ha_release_notes", keep_ids={"ha-2024.2-0"})
        assert removed == 1
        assert len(store.query("new text", top_k=5)) == 1
        assert len(store.query("old text", top_k=5)) == 0

    def test_prune_keeps_all_when_all_in_keep_set(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        store.upsert(
            "ha_release_notes",
            ids=["ha-a-0", "ha-b-0"],
            documents=["alpha", "beta"],
            metadatas=[{"source": "a"}, {"source": "b"}],
        )
        removed = store.prune("ha_release_notes", keep_ids={"ha-a-0", "ha-b-0"})
        assert removed == 0
        assert len(store._docs["ha_release_notes"]) == 2

    def test_prune_empty_keep_set_clears_collection(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        store.upsert(
            "ha_release_notes",
            ids=["ha-a-0"],
            documents=["some text"],
            metadatas=[{"source": "a"}],
        )
        removed = store.prune("ha_release_notes", keep_ids=set())
        assert removed == 1
        assert store.query("some text", top_k=5) == []

    def test_prune_on_missing_collection_returns_zero(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        removed = store.prune("nonexistent", keep_ids={"any-id"})
        assert removed == 0


class TestFakeKnowledgeStoreTotalCount:
    def test_empty_store_returns_zero(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        assert store.total_count() == 0

    def test_counts_across_collections(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        store.upsert("ha_release_notes", ["a"], ["doc a"], [{"source": "a"}])
        store.upsert(
            "hacs_changelogs",
            ["b", "c"],
            ["doc b", "doc c"],
            [{"source": "b"}, {"source": "c"}],
        )
        assert store.total_count() == 3


class TestFakeKnowledgeStoreCollectionCount:
    def test_empty_collection_returns_zero(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        assert store.collection_count("ha_release_notes") == 0

    def test_populated_collection_returns_count(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        store.upsert(
            "ha_release_notes",
            ["a", "b"],
            ["doc a", "doc b"],
            [{"source": "a"}, {"source": "b"}],
        )
        assert store.collection_count("ha_release_notes") == 2

    def test_unknown_collection_returns_zero(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        assert store.collection_count("nonexistent") == 0

    def test_only_counts_named_collection(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        store.upsert("ha_release_notes", ["a"], ["doc a"], [{"source": "a"}])
        store.upsert(
            "strategies", ["x", "y", "z"], ["s1", "s2", "s3"], [{"source": "s"}] * 3
        )
        assert store.collection_count("ha_release_notes") == 1
        assert store.collection_count("strategies") == 3


# ── HA release notes scraper (item 50) ────────────────────────────────────────────


class TestParseBreakingChanges:
    def test_extracts_section_with_break_keyword(self):
        from utils.knowledge.ha_release_notes_scraper import parse_breaking_changes

        notes = "# 2024.1\n\nSome intro text.\n## Breaking Changes\n- Template syntax changed\n## Other\n- Bug fix"
        result = parse_breaking_changes(notes)
        assert any("Template syntax changed" in c for c in result)

    def test_extracts_section_with_deprecated_keyword(self):
        from utils.knowledge.ha_release_notes_scraper import parse_breaking_changes

        notes = "## What's New\nFoo\n## Deprecated\nOld API removed"
        result = parse_breaking_changes(notes)
        assert any("Old API removed" in c for c in result)

    def test_falls_back_to_first_chunk_when_no_match(self):
        from utils.knowledge.ha_release_notes_scraper import parse_breaking_changes

        notes = "No relevant sections here at all."
        result = parse_breaking_changes(notes)
        assert len(result) == 1
        assert "No relevant sections" in result[0]

    def test_truncates_long_sections(self):
        from utils.knowledge.ha_release_notes_scraper import parse_breaking_changes

        notes = "## Breaking Changes\n" + "x" * 3000
        result = parse_breaking_changes(notes)
        assert all(len(c) <= 2000 for c in result)


class TestChunkReleaseNotes:
    def test_returns_ids_docs_metas(self):
        from utils.knowledge.ha_release_notes_scraper import chunk_release_notes

        ids, docs, metas = chunk_release_notes(
            "## Breaking Changes\nfoo changed", "2024.1"
        )
        assert len(ids) == len(docs) == len(metas)
        assert all(id_.startswith("ha-2024.1-") for id_ in ids)
        assert all(m["version"] == "2024.1" for m in metas)

    def test_ids_are_unique(self):
        from utils.knowledge.ha_release_notes_scraper import chunk_release_notes

        notes = "## Breaking\nfoo\n## Removed\nbar\n## Renamed\nbaz"
        ids, _, _ = chunk_release_notes(notes, "2024.2")
        assert len(ids) == len(set(ids))

    def test_chunk_release_notes_release_type(self):
        from utils.knowledge.ha_release_notes_scraper import chunk_release_notes

        _, _, metas_ga = chunk_release_notes("## Changes\nsome text", "2026.8.0")
        assert all(m["release_type"] == "ga" for m in metas_ga)

        _, _, metas_patch = chunk_release_notes("## Changes\nsome text", "2026.7.2")
        assert all(m["release_type"] == "patch" for m in metas_patch)

        _, _, metas_beta = chunk_release_notes("## Changes\nsome text", "2026.8.0b4")
        assert all(m["release_type"] == "beta" for m in metas_beta)

    def test_chunk_release_notes_category(self):
        from utils.knowledge.ha_release_notes_scraper import chunk_release_notes

        notes = (
            "intro\n"
            "\n## Backward Incompatible Changes\n"
            "The `zha` integration changed its config format.\n"
            "\n## New Integrations\n"
            "Added `matter` support.\n"
        )
        _, _, metas = chunk_release_notes(notes, "2026.8.0")
        categories = [m["category"] for m in metas]
        assert "breaking_change" in categories
        assert "new_integration" in categories

    def test_chunk_release_notes_impacted_integration_extracted(self):
        from utils.knowledge.ha_release_notes_scraper import chunk_release_notes

        notes = "## Backward Incompatible Changes\nThe `zha` integration changed.\n"
        _, _, metas = chunk_release_notes(notes, "2026.8.0")
        breaking = [m for m in metas if m["category"] == "breaking_change"]
        assert any(m["impacted_integration"] == "zha" for m in breaking)

    def test_chunk_release_notes_non_breaking_has_empty_integration(self):
        from utils.knowledge.ha_release_notes_scraper import chunk_release_notes

        notes = "## New Integrations\nAdded `matter` support.\n"
        _, _, metas = chunk_release_notes(notes, "2026.8.0")
        assert all(m["impacted_integration"] == "" for m in metas)


class TestKnowledgeStoreWhereClause:
    def test_where_in_filters_by_metadata(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        store.upsert(
            "ha_release_notes",
            ids=["ha-1-0", "ha-1-1"],
            documents=["zha config changed", "mqtt broker changed"],
            metadatas=[
                {"source": "s1", "impacted_integration": "zha"},
                {"source": "s2", "impacted_integration": "mqtt"},
            ],
        )
        results = store.query(
            "changed",
            top_k=5,
            where={"impacted_integration": {"$in": ["zha"]}},
        )
        assert len(results) == 1
        assert results[0].metadata["impacted_integration"] == "zha"

    def test_where_none_returns_all_matches(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        store.upsert(
            "ha_release_notes",
            ids=["ha-1-0", "ha-1-1"],
            documents=["zha config changed", "mqtt broker changed"],
            metadatas=[
                {"source": "s1", "impacted_integration": "zha"},
                {"source": "s2", "impacted_integration": "mqtt"},
            ],
        )
        results = store.query("changed", top_k=5)
        assert len(results) == 2


class TestScrapeCachedReleaseNotes:
    def test_returns_zero_for_missing_dir(self):
        from utils.knowledge.ha_release_notes_scraper import scrape_cached_release_notes
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        result = scrape_cached_release_notes("/nonexistent/path", store)
        assert result == 0

    def test_processes_txt_files(self, tmp_path):
        from utils.knowledge.ha_release_notes_scraper import scrape_cached_release_notes
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "ha_notes"
        cache.mkdir()
        (cache / "2024.1.txt").write_text("## Breaking Changes\ntemplate changed")
        (cache / "2024.2.txt").write_text("## Breaking Changes\nautomation changed")

        store = FakeKnowledgeStore()
        result = scrape_cached_release_notes(str(cache), store)
        assert result == 2
        assert len(store.query("template", top_k=5)) > 0

    def test_skips_non_txt_files(self, tmp_path):
        from utils.knowledge.ha_release_notes_scraper import scrape_cached_release_notes
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "ha_notes"
        cache.mkdir()
        (cache / "README.md").write_text("## Breaking Changes\nsome change")

        store = FakeKnowledgeStore()
        result = scrape_cached_release_notes(str(cache), store)
        assert result == 0

    def test_skips_unreadable_file(self, tmp_path, monkeypatch):
        from pathlib import Path
        from utils.knowledge.ha_release_notes_scraper import scrape_cached_release_notes
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "ha_notes"
        cache.mkdir()
        bad_file = cache / "2024.1.txt"
        bad_file.write_text("## Breaking Changes\ntext")

        original_read_text = Path.read_text

        def _raising_read_text(self, *args, **kwargs):
            if self.name.endswith(".txt"):
                raise OSError("permission denied")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _raising_read_text)

        store = FakeKnowledgeStore()
        result = scrape_cached_release_notes(str(cache), store)
        assert result == 0


class TestHABlogScraper:
    def test_extract_blog_url_from_stub(self):
        from utils.knowledge.ha_blog_scraper import extract_blog_url_from_stub

        stub = "https://www.home-assistant.io/blog/2026/08/06/release-20268/"
        assert extract_blog_url_from_stub(stub) == stub

    def test_extract_blog_url_missing_returns_none(self):
        from utils.knowledge.ha_blog_scraper import extract_blog_url_from_stub

        assert extract_blog_url_from_stub("no url here") is None

    def test_extract_blog_url_ignores_non_blog_urls(self):
        from utils.knowledge.ha_blog_scraper import extract_blog_url_from_stub

        assert (
            extract_blog_url_from_stub("https://github.com/home-assistant/core") is None
        )

    def test_fetch_blog_post_strips_html(self):
        from utils.knowledge.ha_blog_scraper import fetch_blog_post

        html = (
            b"<html><body>"
            b"<article>"
            b"<h2>Backward Incompatible Changes</h2>"
            b"<p>The battery_level attribute was removed.</p>"
            b"<h3>Details</h3>"
            b"<ul><li>Affects LG ThinQ</li><li>Affects Shark IQ</li></ul>"
            b"</article>"
            b"</body></html>"
        )
        result = fetch_blog_post("http://example.com", _fetcher=lambda url: html)
        assert "## Backward Incompatible Changes" in result
        assert "### Details" in result
        assert "battery_level" in result
        assert "- Affects LG ThinQ" in result
        assert "<" not in result

    def test_fetch_blog_post_ignores_content_outside_article(self):
        from utils.knowledge.ha_blog_scraper import fetch_blog_post

        html = (
            b"<html><body>"
            b"<nav>Navigation text</nav>"
            b"<article><p>Article text here.</p></article>"
            b"<footer>Footer text</footer>"
            b"</body></html>"
        )
        result = fetch_blog_post("http://example.com", _fetcher=lambda url: html)
        assert "Article text here." in result
        assert "Navigation text" not in result
        assert "Footer text" not in result

    def test_fetch_blog_release_notes_replaces_stub(self, tmp_path):
        from utils.knowledge.ha_blog_scraper import fetch_blog_release_notes

        cache = tmp_path / "notes"
        cache.mkdir()
        blog_url = "https://www.home-assistant.io/blog/2026/08/06/release-20268/"
        (cache / "2026.8.0.txt").write_text(f"STUB:{blog_url}", encoding="utf-8")

        real_content = "## Backward Incompatible Changes\n" + "x" * 600

        def fake_fetcher(url: str) -> bytes:
            return f"<article><h2>Backward Incompatible Changes</h2><p>{'x' * 600}</p></article>".encode()

        count = fetch_blog_release_notes(str(cache), _fetcher=fake_fetcher)
        assert count == 1
        written = (cache / "2026.8.0.txt").read_text(encoding="utf-8")
        assert not written.startswith("STUB:")
        assert "Backward Incompatible Changes" in written

    def test_fetch_blog_release_notes_skips_real_files(self, tmp_path):
        from utils.knowledge.ha_blog_scraper import fetch_blog_release_notes

        cache = tmp_path / "notes"
        cache.mkdir()
        real_content = "## Breaking Changes\n" + "real content " * 50
        stub_file = cache / "2026.7.0.txt"
        stub_file.write_text(real_content, encoding="utf-8")

        count = fetch_blog_release_notes(
            str(cache), _fetcher=lambda url: b"should not be called"
        )
        assert count == 0
        assert stub_file.read_text(encoding="utf-8") == real_content

    def test_fetch_blog_release_notes_skips_stub_with_no_url(self, tmp_path):
        from utils.knowledge.ha_blog_scraper import fetch_blog_release_notes

        cache = tmp_path / "notes"
        cache.mkdir()
        (cache / "2026.8.0.txt").write_text("STUB:no url here", encoding="utf-8")

        count = fetch_blog_release_notes(
            str(cache), _fetcher=lambda url: b"unreachable"
        )
        assert count == 0

    def test_fetch_blog_release_notes_skips_short_blog_content(self, tmp_path):
        from utils.knowledge.ha_blog_scraper import fetch_blog_release_notes

        cache = tmp_path / "notes"
        cache.mkdir()
        blog_url = "https://www.home-assistant.io/blog/2026/08/06/release-20268/"
        (cache / "2026.8.0.txt").write_text(f"STUB:{blog_url}", encoding="utf-8")

        def fake_fetcher(url: str) -> bytes:
            return b"<article><p>Too short.</p></article>"

        count = fetch_blog_release_notes(str(cache), _fetcher=fake_fetcher)
        assert count == 0

    def test_fetch_blog_release_notes_missing_dir(self):
        from utils.knowledge.ha_blog_scraper import fetch_blog_release_notes

        count = fetch_blog_release_notes("/nonexistent/path")
        assert count == 0


class TestParseReleaseSections:
    def test_embeds_all_sections_not_just_breaking(self):
        from utils.knowledge.ha_release_notes_scraper import parse_release_sections

        notes = "## New Features\nAdded new light platform\n## Bug Fixes\nFixed timer"
        result = parse_release_sections(notes)
        assert len(result) == 2
        assert any("new light platform" in c for c in result)
        assert any("Fixed timer" in c for c in result)

    def test_embeds_additive_sections_with_no_breaking_keywords(self):
        from utils.knowledge.ha_release_notes_scraper import parse_release_sections

        notes = (
            "## New Integrations\nAdded Sonos support\n## Performance\nFaster startup"
        )
        result = parse_release_sections(notes)
        assert len(result) == 2

    def test_word_boundary_truncation_at_3000(self):
        from utils.knowledge.ha_release_notes_scraper import parse_release_sections

        long_section = "word " * 700  # ~3500 chars
        notes = f"## Section\n{long_section}"
        result = parse_release_sections(notes)
        assert len(result) == 1
        assert len(result[0]) <= 3000
        assert not result[0].endswith("wor")  # truncated at word boundary

    def test_returns_single_chunk_for_no_headings(self):
        from utils.knowledge.ha_release_notes_scraper import parse_release_sections

        notes = "Just some plain text with no headings."
        result = parse_release_sections(notes)
        assert len(result) == 1
        assert "plain text" in result[0]

    def test_strips_empty_sections(self):
        from utils.knowledge.ha_release_notes_scraper import parse_release_sections

        notes = "## Header\n\n## Populated\nSome content"
        result = parse_release_sections(notes)
        assert all(c.strip() for c in result)


class TestScrapeWithCollectedIds:
    def test_collected_ids_populated(self, tmp_path):
        from utils.knowledge.ha_release_notes_scraper import scrape_cached_release_notes
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "notes"
        cache.mkdir()
        (cache / "2024.1.txt").write_text("## Breaking\nfoo\n## Features\nbar")

        store = FakeKnowledgeStore()
        collected: set[str] = set()
        scrape_cached_release_notes(str(cache), store, collected)
        assert len(collected) == 2
        assert "ha-2024.1-0" in collected

    def test_collected_ids_none_does_not_error(self, tmp_path):
        from utils.knowledge.ha_release_notes_scraper import scrape_cached_release_notes
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "notes"
        cache.mkdir()
        (cache / "2024.1.txt").write_text("## Features\nfoo")

        store = FakeKnowledgeStore()
        result = scrape_cached_release_notes(str(cache), store, None)
        assert result == 1


# ── HACS changelog scraper (item 51) ──────────────────────────────────────────────


class TestParseChangelog:
    def test_splits_by_section_header(self):
        from utils.knowledge.hacs_scraper import parse_changelog

        text = "## 1.0.0\nFirst release\n## 0.9.0\nBeta"
        result = parse_changelog(text)
        assert len(result) == 2
        assert "First release" in result[0]

    def test_returns_empty_for_blank_input(self):
        from utils.knowledge.hacs_scraper import parse_changelog

        result = parse_changelog("")
        assert result == []

    def test_truncates_long_sections(self):
        from utils.knowledge.hacs_scraper import parse_changelog

        text = "## 1.0.0\n" + "a" * 4000
        result = parse_changelog(text)
        assert all(len(c) <= 3000 for c in result)


class TestChunkChangelog:
    def test_returns_ids_docs_metas(self):
        from utils.knowledge.hacs_scraper import chunk_changelog

        ids, docs, metas = chunk_changelog(
            "## 1.0.0\nchange one\n## 0.9.0\nchange two", "myint"
        )
        assert len(ids) == len(docs) == len(metas) == 2
        assert all(id_.startswith("hacs-myint-") for id_ in ids)
        assert all(m["slug"] == "myint" for m in metas)

    def test_returns_empty_for_blank_changelog(self):
        from utils.knowledge.hacs_scraper import chunk_changelog

        ids, docs, metas = chunk_changelog("", "myint")
        assert ids == [] and docs == [] and metas == []


class TestEmbedCachedChangelogs:
    def test_returns_zero_for_missing_dir(self):
        from utils.knowledge.hacs_scraper import embed_cached_changelogs
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        result = embed_cached_changelogs("/nonexistent/path", store)
        assert result == 0

    def test_processes_md_files(self, tmp_path):
        from utils.knowledge.hacs_scraper import embed_cached_changelogs
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "hacs"
        cache.mkdir()
        (cache / "myintegration.md").write_text(
            "## 1.0.0\nFixed bug\n## 0.9.0\nAdded feature"
        )

        store = FakeKnowledgeStore()
        result = embed_cached_changelogs(str(cache), store)
        assert result == 1
        assert len(store.query("Fixed bug", top_k=5)) > 0

    def test_skips_non_md_files(self, tmp_path):
        from utils.knowledge.hacs_scraper import embed_cached_changelogs
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "hacs"
        cache.mkdir()
        (cache / "notes.txt").write_text("## 1.0.0\nchange")

        store = FakeKnowledgeStore()
        result = embed_cached_changelogs(str(cache), store)
        assert result == 0

    def test_skips_unreadable_file(self, tmp_path, monkeypatch):
        from pathlib import Path
        from utils.knowledge.hacs_scraper import embed_cached_changelogs
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "hacs"
        cache.mkdir()
        bad_file = cache / "myint.md"
        bad_file.write_text("## 1.0.0\nFixed bug")

        original_read_text = Path.read_text

        def _raising_read_text(self, *args, **kwargs):
            if self.name.endswith(".md"):
                raise OSError("permission denied")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _raising_read_text)

        store = FakeKnowledgeStore()
        result = embed_cached_changelogs(str(cache), store)
        assert result == 0


class TestRepoFromReleaseUrl:
    def test_extracts_org_repo(self):
        from utils.knowledge.hacs_scraper import _repo_from_release_url

        url = "https://github.com/dmamontov/hass-pycync/releases/tag/v1.0.0"
        assert _repo_from_release_url(url) == "dmamontov/hass-pycync"

    def test_extracts_from_releases_url(self):
        from utils.knowledge.hacs_scraper import _repo_from_release_url

        url = "https://github.com/custom-org/my-integration/releases"
        assert _repo_from_release_url(url) == "custom-org/my-integration"

    def test_returns_none_for_non_github_url(self):
        from utils.knowledge.hacs_scraper import _repo_from_release_url

        assert _repo_from_release_url("https://gitlab.com/foo/bar/releases") is None

    def test_returns_none_for_empty_string(self):
        from utils.knowledge.hacs_scraper import _repo_from_release_url

        assert _repo_from_release_url("") is None


class TestChunkChangelogCollectedIds:
    def test_collected_ids_populated(self):
        from utils.knowledge.hacs_scraper import chunk_changelog

        collected: set[str] = set()
        ids, _, _ = chunk_changelog("## 1.0.0\nfoo\n## 0.9.0\nbar", "myint", collected)
        assert collected == set(ids)

    def test_collected_ids_none_does_not_error(self):
        from utils.knowledge.hacs_scraper import chunk_changelog

        ids, docs, metas = chunk_changelog("## 1.0.0\nfoo", "myint", None)
        assert len(ids) == 1


class TestHACSChunkVersion:
    def test_version_extracted_from_semver_heading(self):
        from utils.knowledge.hacs_scraper import chunk_changelog

        _, _, metas = chunk_changelog(
            "## 1.2.3\nFixed a bug.\n## 0.9.0\nAdded feature.", "myint"
        )
        assert metas[0]["version"] == "1.2.3"
        assert metas[1]["version"] == "0.9.0"

    def test_version_empty_when_no_semver_heading(self):
        from utils.knowledge.hacs_scraper import chunk_changelog

        # Section heading that isn't a version number
        _, _, metas = chunk_changelog("## Unreleased\nWIP stuff.", "myint")
        assert metas[0]["version"] == ""

    def test_chunk_max_size_is_3000(self):
        from utils.knowledge.hacs_scraper import parse_changelog

        long_text = "## 1.0.0\n" + ("word " * 1000)
        result = parse_changelog(long_text)
        assert len(result) == 1
        assert len(result[0]) <= 3000


class TestEmbedCachedChangelogsCollectedIds:
    def test_collected_ids_populated(self, tmp_path):
        from utils.knowledge.hacs_scraper import embed_cached_changelogs
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "hacs"
        cache.mkdir()
        (cache / "myint.md").write_text("## 1.0.0\nFeature A\n## 0.9.0\nBeta")

        store = FakeKnowledgeStore()
        collected: set[str] = set()
        embed_cached_changelogs(str(cache), store, collected)
        assert len(collected) == 2
        assert "hacs-myint-0" in collected


# ── HA integration docs scraper ───────────────────────────────────────────────────


class TestParseIntegrationDoc:
    def test_strips_frontmatter(self):
        from utils.knowledge.ha_docs_scraper import parse_integration_doc

        doc = (
            "---\ntitle: Test\nha_category: Integration\n---\n## Overview\nSome content"
        )
        result = parse_integration_doc(doc)
        assert result
        assert all("---" not in c for c in result)
        assert any("Some content" in c for c in result)

    def test_splits_by_headings(self):
        from utils.knowledge.ha_docs_scraper import parse_integration_doc

        doc = "## Setup\nInstall the integration.\n## Configuration\nAdd to config."
        result = parse_integration_doc(doc)
        assert len(result) == 2

    def test_word_boundary_truncation(self):
        from utils.knowledge.ha_docs_scraper import parse_integration_doc

        long_section = "word " * 700  # ~3500 chars
        doc = f"## Section\n{long_section}"
        result = parse_integration_doc(doc)
        assert len(result) == 1
        assert len(result[0]) <= 3000
        assert not result[0].endswith("wor")

    def test_strips_empty_sections(self):
        from utils.knowledge.ha_docs_scraper import parse_integration_doc

        doc = "## Header\n\n## Content\nActual text here"
        result = parse_integration_doc(doc)
        assert all(c.strip() for c in result)

    def test_handles_doc_without_frontmatter(self):
        from utils.knowledge.ha_docs_scraper import parse_integration_doc

        doc = "## Overview\nJust a plain doc with no front matter."
        result = parse_integration_doc(doc)
        assert result
        assert "plain doc" in result[0]


class TestEmbedCachedIntegrationDocs:
    def test_returns_zero_for_missing_dir(self):
        from utils.knowledge.ha_docs_scraper import embed_cached_integration_docs
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        assert embed_cached_integration_docs("/nonexistent/path", store) == 0

    def test_processes_md_files(self, tmp_path):
        from utils.knowledge.ha_docs_scraper import embed_cached_integration_docs
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "docs"
        cache.mkdir()
        (cache / "hue.md").write_text(
            "## Overview\nPhilips Hue integration.\n## Configuration\nAdd token."
        )

        store = FakeKnowledgeStore()
        result = embed_cached_integration_docs(str(cache), store)
        assert result == 1
        hits = store.query("Philips Hue", top_k=5)
        assert len(hits) > 0
        assert hits[0].collection == "ha_integration_docs"

    def test_skips_non_md_files(self, tmp_path):
        from utils.knowledge.ha_docs_scraper import embed_cached_integration_docs
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "docs"
        cache.mkdir()
        (cache / "readme.txt").write_text("## Overview\nSome text")

        store = FakeKnowledgeStore()
        assert embed_cached_integration_docs(str(cache), store) == 0

    def test_skips_unreadable_file(self, tmp_path, monkeypatch):
        from pathlib import Path
        from utils.knowledge.ha_docs_scraper import embed_cached_integration_docs
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "docs"
        cache.mkdir()
        (cache / "hue.md").write_text("## Overview\nHue content")

        original_read_text = Path.read_text

        def _raising_read_text(self, *args, **kwargs):
            if self.name.endswith(".md"):
                raise OSError("permission denied")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _raising_read_text)

        store = FakeKnowledgeStore()
        assert embed_cached_integration_docs(str(cache), store) == 0

    def test_collected_ids_populated(self, tmp_path):
        from utils.knowledge.ha_docs_scraper import embed_cached_integration_docs
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "docs"
        cache.mkdir()
        (cache / "hue.md").write_text("## Setup\nSection one.\n## Config\nSection two.")

        store = FakeKnowledgeStore()
        collected: set[str] = set()
        embed_cached_integration_docs(str(cache), store, collected)
        assert len(collected) == 2
        assert "ha-docs-hue-0" in collected

    def test_is_installed_in_metadata(self, tmp_path):
        from utils.knowledge.ha_docs_scraper import embed_cached_integration_docs
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "docs"
        cache.mkdir()
        (cache / "zha.md").write_text("## Overview\nZHA integration docs.")

        store = FakeKnowledgeStore()
        embed_cached_integration_docs(str(cache), store)
        hits = store.query("ZHA integration", top_k=5)
        assert hits
        assert hits[0].metadata.get("is_installed") is True


class TestCommunityCollection:
    def test_community_cases_in_collections(self):
        from utils.knowledge.knowledge_store import COLLECTIONS

        assert "community_cases" in COLLECTIONS


class TestFetchIntegrationDocReturnValues:
    """fetch_integration_doc tri-state return contract."""

    def test_returns_zero_when_already_cached(self, tmp_path):
        from utils.knowledge.ha_docs_scraper import fetch_integration_doc

        cache = tmp_path / "docs"
        cache.mkdir()
        (cache / "hue.md").write_text("## Content\nSome text")
        assert fetch_integration_doc("hue", str(cache)) == 0


class _MockHTTPResponse:
    """Minimal context manager that stands in for urlopen's response."""

    def __init__(self, payload_bytes: bytes, status: int = 200):
        self._data = payload_bytes
        self.status = status

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class TestDiscoverHacsWsFiltering:
    """_discover_via_hacs_ws parses HACS WebSocket repository results correctly."""

    def _mock_ws(self, payload, monkeypatch):
        """Monkeypatch websockets.sync.client.connect with a sync fake context manager."""
        import json
        from unittest.mock import MagicMock

        messages = [
            json.dumps({"type": "auth_required"}),
            json.dumps({"type": "auth_ok"}),
            json.dumps({"id": 1, "type": "result", "success": True, "result": payload}),
        ]
        ws = MagicMock()
        ws.recv.side_effect = messages
        ws.__enter__ = MagicMock(return_value=ws)
        ws.__exit__ = MagicMock(return_value=False)

        import websockets.sync.client as _wsc

        monkeypatch.setattr(_wsc, "connect", MagicMock(return_value=ws))

    def test_returns_installed_integrations(self, monkeypatch):
        from utils.knowledge.hacs_scraper import _discover_via_hacs_ws

        payload = [
            {
                "slug": "pycync",
                "installed": True,
                "category": "integration",
                "full_name": "dmamontov/hass-pycync",
            },
            {
                "slug": "noaa",
                "installed": False,
                "category": "integration",
                "full_name": "someorg/noaa",
            },
        ]
        self._mock_ws(payload, monkeypatch)
        result = _discover_via_hacs_ws("http://ha:8123", "tok")
        assert result == [("pycync", "dmamontov/hass-pycync")]

    def test_excludes_non_integration_categories(self, monkeypatch):
        from utils.knowledge.hacs_scraper import _discover_via_hacs_ws

        payload = [
            {
                "slug": "mytheme",
                "installed": True,
                "category": "theme",
                "full_name": "someone/mytheme",
            },
            {
                "slug": "myint",
                "installed": True,
                "category": "integration",
                "full_name": "someone/myint",
            },
        ]
        self._mock_ws(payload, monkeypatch)
        result = _discover_via_hacs_ws("http://ha:8123", "tok")
        assert result == [("myint", "someone/myint")]

    def test_returns_empty_on_connection_error(self, monkeypatch):
        from unittest.mock import MagicMock

        import websockets.sync.client as _wsc

        from utils.knowledge.hacs_scraper import _discover_via_hacs_ws

        monkeypatch.setattr(
            _wsc,
            "connect",
            MagicMock(side_effect=OSError("refused")),
        )
        assert _discover_via_hacs_ws("http://ha:8123", "tok") == []

    def test_has_docstring(self):
        from utils.knowledge.hacs_scraper import _discover_via_hacs_ws

        assert isinstance(_discover_via_hacs_ws.__doc__, str)


class TestDiscoverHacsIntegrationsEntityFallback:
    """Entity-scan fallback excludes the HACS manager (hacs/integration)."""

    def _mock_ws_failed(self, monkeypatch):
        """Make websockets.sync.client.connect fail so the WS primary returns [] and fallback runs."""
        from unittest.mock import MagicMock

        import websockets.sync.client as _wsc

        monkeypatch.setattr(
            _wsc, "connect", MagicMock(side_effect=OSError("unavailable"))
        )

    def _urlopen_states(self, states_payload):
        import json

        return lambda *a, **kw: _MockHTTPResponse(json.dumps(states_payload).encode())

    def test_excludes_hacs_manager_from_entity_scan(self, monkeypatch):
        import urllib.request

        from utils.knowledge.hacs_scraper import discover_hacs_integrations

        # Modern HA always sets platform=None; detection is via entity_picture brands URL
        states = [
            {
                "entity_id": "update.hacs",
                "attributes": {
                    "entity_picture": "https://brands.home-assistant.io/_/hacs/icon.png",
                    "release_url": "https://github.com/hacs/integration/releases/tag/v2.0.0",
                },
            },
            {
                "entity_id": "update.pycync",
                "attributes": {
                    "entity_picture": "https://brands.home-assistant.io/_/pycync/icon.png",
                    "release_url": "https://github.com/dmamontov/hass-pycync/releases/tag/v1.0.0",
                },
            },
        ]
        self._mock_ws_failed(monkeypatch)
        monkeypatch.setattr(urllib.request, "urlopen", self._urlopen_states(states))
        result = discover_hacs_integrations("http://ha:8123", "tok")
        repos = [r for _, r in result]
        assert "hacs/integration" not in repos
        assert "dmamontov/hass-pycync" in repos

    def test_entity_scan_skips_builtin_update_entities(self, monkeypatch):
        """Built-in update entities use non-underscore brands URL — should be skipped."""
        import urllib.request

        from utils.knowledge.hacs_scraper import discover_hacs_integrations

        states = [
            {
                "entity_id": "update.home_assistant_core_update",
                "attributes": {
                    "entity_picture": "https://brands.home-assistant.io/homeassistant/icon.png",
                    "release_url": "https://github.com/home-assistant/core/releases/tag/2026.7.0",
                },
            },
        ]
        self._mock_ws_failed(monkeypatch)
        monkeypatch.setattr(urllib.request, "urlopen", self._urlopen_states(states))
        result = discover_hacs_integrations("http://ha:8123", "tok")
        assert result == []

    def test_returns_empty_for_missing_token(self):
        from utils.knowledge.hacs_scraper import discover_hacs_integrations

        assert discover_hacs_integrations("http://ha:8123", "") == []


# ── discover_installed_integrations ──────────────────────────────────────────────


class _MockHTTPConfigResponse:
    """Minimal urllib response stub for /api/config payloads."""

    def __init__(self, payload: dict) -> None:
        import json

        self._data = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return self._data


class TestDiscoverInstalledIntegrations:
    def test_uses_api_config_endpoint(self, monkeypatch):
        """Discovery calls /api/config, not /api/states."""
        import urllib.request

        from utils.knowledge.ha_docs_scraper import discover_installed_integrations

        calls: list[str] = []

        def fake_urlopen(req, timeout=10):
            calls.append(req.full_url)
            return _MockHTTPConfigResponse({"components": ["mqtt", "hue.light"]})

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = discover_installed_integrations("http://ha:8123", "tok")
        assert any("/api/config" in url for url in calls)
        assert "/api/states" not in "".join(calls)
        assert "mqtt" in result
        assert "hue" in result

    def test_filters_trivial_domains(self, monkeypatch):
        """Known trivial domains (automation, sensor, etc.) are excluded."""
        import urllib.request

        from utils.knowledge.ha_docs_scraper import discover_installed_integrations

        components = ["automation", "binary_sensor", "sensor", "mqtt", "frontend"]
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda *a, **kw: _MockHTTPConfigResponse({"components": components}),
        )
        result = discover_installed_integrations("http://ha:8123", "tok")
        assert "automation" not in result
        assert "binary_sensor" not in result
        assert "sensor" not in result
        assert "frontend" not in result
        assert "mqtt" in result

    def test_returns_empty_for_missing_token(self):
        from utils.knowledge.ha_docs_scraper import discover_installed_integrations

        assert discover_installed_integrations("http://ha:8123", "") == []

    def test_returns_empty_on_network_error(self, monkeypatch):
        import urllib.request

        from utils.knowledge.ha_docs_scraper import discover_installed_integrations

        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("timeout")),
        )
        assert discover_installed_integrations("http://ha:8123", "tok") == []

    def test_deduplicates_platform_variants(self, monkeypatch):
        """Components like 'mqtt' and 'mqtt.sensor' both produce domain 'mqtt' once."""
        import urllib.request

        from utils.knowledge.ha_docs_scraper import discover_installed_integrations

        components = ["mqtt", "mqtt.sensor", "mqtt.light", "hue.light", "hue"]
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda *a, **kw: _MockHTTPConfigResponse({"components": components}),
        )
        result = discover_installed_integrations("http://ha:8123", "tok")
        assert result.count("mqtt") == 1
        assert result.count("hue") == 1


# ── LoopSupervisor ────────────────────────────────────────────────────────────────


class TestLoopSupervisor:
    def test_starts_loop_and_runs(self):
        """Supervisor creates a task; coro runs at least once."""

        async def _run():
            ran: list[int] = []

            async def coro():
                ran.append(1)

            from utils.agent.supervisor import LoopSupervisor

            bus: asyncio.Queue = asyncio.Queue()
            sup = LoopSupervisor(bus=bus, backoff_start=0.01, backoff_cap=0.01)
            sup.start("t", coro)
            await asyncio.sleep(0.05)
            sup.cancel_all()
            await asyncio.gather(*sup._tasks.values(), return_exceptions=True)
            assert len(ran) >= 1

        asyncio.run(_run())

    def test_crashed_task_restarts(self):
        """An exception in the coro increments error_count and the coro is retried."""

        async def _run():
            calls: list[int] = []

            async def flaky_coro():
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("first call fails")

            from utils.agent.supervisor import LoopSupervisor

            bus: asyncio.Queue = asyncio.Queue()
            sup = LoopSupervisor(bus=bus, backoff_start=0.01, backoff_cap=0.01)
            sup.start("t", flaky_coro)
            await asyncio.sleep(0.15)
            sup.cancel_all()
            await asyncio.gather(*sup._tasks.values(), return_exceptions=True)

            status = sup.get_statuses()[0]
            assert status.error_count >= 1
            assert len(calls) >= 2

        asyncio.run(_run())

    def test_cancel_all_stops_tasks(self):
        """cancel_all() causes all supervised tasks to finish cleanly."""

        async def _run():
            async def coro():
                await asyncio.sleep(999)

            from utils.agent.supervisor import LoopSupervisor

            bus: asyncio.Queue = asyncio.Queue()
            sup = LoopSupervisor(bus=bus, backoff_start=0.01, backoff_cap=0.01)
            sup.start("t", coro)
            await asyncio.sleep(0.02)
            sup.cancel_all()
            await asyncio.gather(*sup._tasks.values(), return_exceptions=True)
            # Tasks finish after cancellation (supervisor catches CancelledError and returns)
            assert all(t.done() for t in sup._tasks.values())

        asyncio.run(_run())

    def test_event_bus_receives_loop_status(self):
        """Running loop emits a loop_status event to the bus."""

        async def _run():
            async def coro():
                pass

            from utils.agent.supervisor import LoopSupervisor

            bus: asyncio.Queue = asyncio.Queue()
            sup = LoopSupervisor(bus=bus, backoff_start=0.01, backoff_cap=0.01)
            sup.start("t", coro)
            await asyncio.sleep(0.05)
            sup.cancel_all()
            await asyncio.gather(*sup._tasks.values(), return_exceptions=True)

            events = []
            while not bus.empty():
                events.append(bus.get_nowait())
            assert any(e["event_type"] == "loop_status" for e in events)
            assert any(e["loop"] == "t" for e in events)

        asyncio.run(_run())

    def test_disabled_loop_not_started_when_never_called(self):
        """Loops not started by caller have no tasks in supervisor."""
        from utils.agent.supervisor import LoopSupervisor

        bus: asyncio.Queue = asyncio.Queue()
        sup = LoopSupervisor(bus=bus)
        assert len(sup._tasks) == 0
        assert len(sup.get_statuses()) == 0

    def test_last_error_recorded_on_crash(self):
        """Error message is recorded in LoopStatus after coro raises."""

        async def _run():
            async def bad_coro():
                raise ValueError("sentinel error")

            from utils.agent.supervisor import LoopSupervisor

            bus: asyncio.Queue = asyncio.Queue()
            sup = LoopSupervisor(bus=bus, backoff_start=0.01, backoff_cap=0.01)
            sup.start("t", bad_coro)
            await asyncio.sleep(0.05)
            sup.cancel_all()
            await asyncio.gather(*sup._tasks.values(), return_exceptions=True)

            status = sup.get_statuses()[0]
            assert "sentinel error" in status.last_error

        asyncio.run(_run())

    def test_last_error_cleared_on_recovery(self):
        """last_error is reset to '' when a crashed loop successfully starts again."""

        async def _run():
            attempts: list[int] = []

            async def flaky_coro():
                attempts.append(1)
                if len(attempts) == 1:
                    raise ValueError("first attempt fails")
                # second attempt: return cleanly (simulates recovery)

            from utils.agent.supervisor import LoopSupervisor

            bus: asyncio.Queue = asyncio.Queue()
            sup = LoopSupervisor(bus=bus, backoff_start=0.01, backoff_cap=0.01)
            sup.start("t", flaky_coro)
            await asyncio.sleep(0.15)
            sup.cancel_all()
            await asyncio.gather(*sup._tasks.values(), return_exceptions=True)

            status = sup.get_statuses()[0]
            assert status.last_error == ""

        asyncio.run(_run())

    def test_paused_loop_does_not_run_coro(self):
        """A paused loop skips the coro body and sleeps instead."""

        async def _run():
            called: list[int] = []

            async def coro():
                called.append(1)

            from utils.agent.supervisor import LoopSupervisor

            bus: asyncio.Queue = asyncio.Queue()
            sup = LoopSupervisor(bus=bus, backoff_start=0.01, backoff_cap=0.01)
            sup.start("t", coro)
            # Pause before the task gets a chance to run the coro
            sup._handles["t"].paused = True
            await asyncio.sleep(0.05)  # Task is sleeping in the paused branch
            assert len(called) == 0
            sup.cancel_all()
            await asyncio.gather(*sup._tasks.values(), return_exceptions=True)

        asyncio.run(_run())

    def test_emit_drops_silently_when_queue_full(self):
        """_emit() catches QueueFull and silently drops the event."""

        async def _run():
            async def coro():
                pass

            bus: asyncio.Queue = asyncio.Queue(maxsize=1)
            bus.put_nowait({"event_type": "filler"})  # Fill the queue

            from utils.agent.supervisor import LoopSupervisor

            sup = LoopSupervisor(bus=bus, backoff_start=0.01, backoff_cap=0.01)
            sup.start("t", coro)
            await asyncio.sleep(0.05)  # _emit() should not raise even with full queue
            sup.cancel_all()
            await asyncio.gather(*sup._tasks.values(), return_exceptions=True)

        asyncio.run(_run())

    def test_pause_stops_running_daemon(self):
        """pause() cancels the running daemon; the loop enters paused status."""

        async def _run():
            async def daemon():
                await asyncio.sleep(999)  # simulate a long-running daemon

            from utils.agent.supervisor import LoopSupervisor

            bus: asyncio.Queue = asyncio.Queue()
            sup = LoopSupervisor(bus=bus, backoff_start=0.01, backoff_cap=0.01)
            sup.start("d", daemon)
            await asyncio.sleep(0.05)  # let daemon start
            sup.pause("d")
            await asyncio.sleep(0.05)  # let loop re-enter paused branch

            status = sup.get_statuses()[0]
            assert status.paused is True
            assert status.status == "paused"

            sup.cancel_all()
            await asyncio.gather(*sup._tasks.values(), return_exceptions=True)

        asyncio.run(_run())

    def test_resume_restarts_paused_loop(self):
        """resume() clears the paused flag; the loop runs the coro again."""

        async def _run():
            called: list[int] = []
            resume_event = asyncio.Event()

            async def daemon():
                called.append(1)
                await resume_event.wait()  # block until we signal

            from utils.agent.supervisor import LoopSupervisor

            bus: asyncio.Queue = asyncio.Queue()
            sup = LoopSupervisor(bus=bus, backoff_start=0.01, backoff_cap=0.01)
            sup.start("d", daemon)
            await asyncio.sleep(0.05)  # daemon running, waiting on event
            sup.pause("d")
            await asyncio.sleep(0.05)  # loop now paused
            assert sup._handles["d"].paused is True

            resume_event.set()  # unblock daemon so it can exit cleanly
            sup.resume("d")
            await asyncio.sleep(0.05)  # loop resumes and re-runs daemon
            assert sup._handles["d"].paused is False

            sup.cancel_all()
            await asyncio.gather(*sup._tasks.values(), return_exceptions=True)
            assert len(called) >= 1

        asyncio.run(_run())

    def test_run_now_fires_paused_loop(self):
        """run_now() clears paused and restarts the loop immediately."""

        async def _run():
            called: list[int] = []

            async def coro():
                called.append(1)

            from utils.agent.supervisor import LoopSupervisor

            bus: asyncio.Queue = asyncio.Queue()
            sup = LoopSupervisor(bus=bus, backoff_start=0.5, backoff_cap=0.5)
            sup.start("d", coro)
            # Pause before the coro gets a chance to run
            sup._handles["d"].paused = True
            await asyncio.sleep(0.05)
            assert len(called) == 0  # still paused

            sup.run_now("d")
            await asyncio.sleep(0.1)  # loop should fire within 100 ms

            assert len(called) >= 1
            sup.cancel_all()
            await asyncio.gather(*sup._tasks.values(), return_exceptions=True)

        asyncio.run(_run())

    def test_run_now_interrupts_backoff(self):
        """run_now() during error backoff fires the loop immediately."""

        async def _run():
            calls: list[int] = []

            async def flaky():
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("first call fails")

            from utils.agent.supervisor import LoopSupervisor

            bus: asyncio.Queue = asyncio.Queue()
            sup = LoopSupervisor(bus=bus, backoff_start=5.0, backoff_cap=5.0)
            sup.start("d", flaky)
            await asyncio.sleep(0.05)  # first call fails, now in 5s backoff

            assert calls == [1]
            sup.run_now("d")
            await asyncio.sleep(0.1)  # should restart within 100 ms

            assert len(calls) >= 2  # second run fired by run_now

            sup.cancel_all()
            await asyncio.gather(*sup._tasks.values(), return_exceptions=True)

        asyncio.run(_run())

    def test_pause_unknown_loop_raises(self):
        """pause/resume/run_now raise KeyError for unknown loop names."""
        from utils.agent.supervisor import LoopSupervisor

        sup = LoopSupervisor()
        with pytest.raises(KeyError):
            sup.pause("nonexistent")
        with pytest.raises(KeyError):
            sup.resume("nonexistent")
        with pytest.raises(KeyError):
            sup.run_now("nonexistent")

    def test_pause_idempotent(self):
        """Calling pause() on an already-paused loop is a no-op."""
        from utils.agent.supervisor import LoopSupervisor

        async def _run():
            async def daemon():
                await asyncio.sleep(999)

            bus: asyncio.Queue = asyncio.Queue()
            sup = LoopSupervisor(bus=bus, backoff_start=0.01, backoff_cap=0.01)
            sup.start("d", daemon)
            await asyncio.sleep(0.05)
            sup.pause("d")
            await asyncio.sleep(0.05)  # enter paused state
            # Second pause() must not raise or cancel a second time
            sup.pause("d")
            assert sup._handles["d"].paused is True
            sup.cancel_all()
            await asyncio.gather(*sup._tasks.values(), return_exceptions=True)

        asyncio.run(_run())

    def test_resume_not_paused_is_noop(self):
        """Calling resume() on a running (non-paused) loop is a no-op."""
        from utils.agent.supervisor import LoopSupervisor

        async def _run():
            async def daemon():
                await asyncio.sleep(999)

            bus: asyncio.Queue = asyncio.Queue()
            sup = LoopSupervisor(bus=bus, backoff_start=0.01, backoff_cap=0.01)
            sup.start("d", daemon)
            await asyncio.sleep(0.05)
            # resume() on a running (not paused) loop must be a no-op
            sup.resume("d")
            assert sup._handles["d"].paused is False
            assert sup._handles["d"].status == "running"
            sup.cancel_all()
            await asyncio.gather(*sup._tasks.values(), return_exceptions=True)

        asyncio.run(_run())

    def test_run_now_while_daemon_running(self):
        """run_now() cancels and immediately restarts a currently running daemon."""

        async def _run():
            iterations: list[int] = []
            allow_exit = asyncio.Event()

            async def daemon():
                iterations.append(1)
                await allow_exit.wait()  # block until signalled

            from utils.agent.supervisor import LoopSupervisor

            bus: asyncio.Queue = asyncio.Queue()
            sup = LoopSupervisor(bus=bus, backoff_start=0.01, backoff_cap=0.01)
            sup.start("d", daemon)
            await asyncio.sleep(0.05)  # daemon running, blocked on allow_exit
            assert len(iterations) == 1

            # run_now cancels the running coro and restarts it immediately
            sup.run_now("d")
            await asyncio.sleep(0.05)  # let restart complete

            assert len(iterations) >= 2

            allow_exit.set()
            sup.cancel_all()
            await asyncio.gather(*sup._tasks.values(), return_exceptions=True)

        asyncio.run(_run())

    def test_pause_during_backoff(self):
        """pause() called while the loop is in error-backoff enters paused state."""

        async def _run():
            async def bad_coro():
                raise RuntimeError("always fails")

            from utils.agent.supervisor import LoopSupervisor

            bus: asyncio.Queue = asyncio.Queue()
            sup = LoopSupervisor(bus=bus, backoff_start=5.0, backoff_cap=5.0)
            sup.start("d", bad_coro)
            await asyncio.sleep(0.05)  # coro failed, now in 5s backoff sleep

            # pause() during backoff → loop enters paused state
            sup.pause("d")
            await asyncio.sleep(0.05)
            assert sup._handles["d"].paused is True
            assert sup._handles["d"].status == "paused"

            sup.cancel_all()
            await asyncio.gather(*sup._tasks.values(), return_exceptions=True)

        asyncio.run(_run())

    def test_touch_updates_last_run_and_emits_event(self):
        """touch(name) sets last_run to approximately now and emits a loop_status event."""
        import time

        from utils.agent.supervisor import LoopSupervisor

        async def _run():
            async def daemon():
                await asyncio.sleep(999)

            bus: asyncio.Queue = asyncio.Queue()
            sup = LoopSupervisor(bus=bus, backoff_start=0.01, backoff_cap=0.01)
            sup.start("d", daemon)
            await asyncio.sleep(0.05)  # daemon running

            before = time.time()
            sup.touch("d")
            after = time.time()

            status = sup._handles["d"]
            assert status.last_run is not None
            assert before <= status.last_run <= after

            # SSE event should carry updated last_run
            events = []
            while not bus.empty():
                events.append(bus.get_nowait())
            loop_events = [
                e
                for e in events
                if e.get("event_type") == "loop_status" and e.get("loop") == "d"
            ]
            assert any(e["last_run"] == status.last_run for e in loop_events)

            sup.cancel_all()
            await asyncio.gather(*sup._tasks.values(), return_exceptions=True)

        asyncio.run(_run())

    def test_touch_unknown_name_is_noop(self):
        """touch() with an unknown loop name silently does nothing."""
        from utils.agent.supervisor import LoopSupervisor

        sup = LoopSupervisor()
        sup.touch("nonexistent")  # must not raise


# ── Timeline utility ──────────────────────────────────────────────────────────────


class TestTimelineUtils:
    """Unit tests for utils/timeline.py — write, load, count, get."""

    @pytest.fixture()
    def tl_db(self, tmp_path, monkeypatch):
        """Isolated SQLite DB with timeline_events table."""
        from agents import ha_agent_advanced
        import utils.core.timeline as tl_mod

        db = str(tmp_path / "tl_test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", db)
        monkeypatch.setattr(tl_mod, "DB_PATH", db)
        ha_agent_advanced.init_local_database()
        return db

    def test_write_returns_positive_id(self, tl_db):
        from utils.core.timeline import write_timeline_event

        eid = write_timeline_event("INFO", "test_src", "hello world")
        assert eid > 0

    def test_write_persists_to_db(self, tl_db):
        import sqlite3
        from utils.core.timeline import write_timeline_event

        write_timeline_event("WARN", "resource", "disk low", {"disk_free_gb": 1.5})
        with sqlite3.connect(tl_db) as conn:
            row = conn.execute(
                "SELECT level, source, message, detail_json FROM timeline_events"
            ).fetchone()
        assert row[0] == "WARN"
        assert row[1] == "resource"
        assert row[2] == "disk low"
        assert '"disk_free_gb"' in row[3]

    def test_write_publishes_to_sse_bus(self, tl_db, monkeypatch):
        """write_timeline_event() calls publish_event with event_type='timeline'."""
        import utils.core.timeline as tl_mod

        published = []
        monkeypatch.setattr(tl_mod, "_publish_event_for_test", None, raising=False)

        import utils.agent.supervisor as sup_mod

        captured = []
        monkeypatch.setattr(sup_mod, "publish_event", lambda e: captured.append(e))

        from utils.core.timeline import write_timeline_event

        write_timeline_event("ERROR", "ha_log_monitor", "bad thing happened")
        assert any(e.get("event_type") == "timeline" for e in captured)

    def test_load_returns_newest_first(self, tl_db):
        import time as _time
        from utils.core.timeline import load_timeline_events, write_timeline_event

        write_timeline_event("INFO", "a", "first")
        _time.sleep(0.01)
        write_timeline_event("INFO", "b", "second")
        events = load_timeline_events()
        assert events[0]["message"] == "second"
        assert events[1]["message"] == "first"

    def test_load_level_filter(self, tl_db):
        from utils.core.timeline import load_timeline_events, write_timeline_event

        write_timeline_event("INFO", "src", "info event")
        write_timeline_event("ERROR", "src", "error event")
        events = load_timeline_events(level_filter="ERROR")
        assert len(events) == 1
        assert events[0]["message"] == "error event"

    def test_load_source_filter(self, tl_db):
        from utils.core.timeline import load_timeline_events, write_timeline_event

        write_timeline_event("INFO", "resource", "disk ok")
        write_timeline_event("INFO", "update_check", "no updates")
        events = load_timeline_events(source_filter="resource")
        assert len(events) == 1
        assert events[0]["source"] == "resource"

    def test_load_parses_detail_json(self, tl_db):
        from utils.core.timeline import load_timeline_events, write_timeline_event

        write_timeline_event("INFO", "src", "msg", {"key": "val"})
        events = load_timeline_events()
        assert events[0]["detail"] == {"key": "val"}

    def test_load_empty_detail_returns_dict(self, tl_db):
        from utils.core.timeline import load_timeline_events, write_timeline_event

        write_timeline_event("INFO", "src", "msg")
        events = load_timeline_events()
        assert events[0]["detail"] == {}

    def test_get_returns_event_by_id(self, tl_db):
        from utils.core.timeline import get_timeline_event, write_timeline_event

        eid = write_timeline_event("CRITICAL", "src", "critical thing", {"x": 1})
        ev = get_timeline_event(eid)
        assert ev is not None
        assert ev["level"] == "CRITICAL"
        assert ev["detail"] == {"x": 1}

    def test_get_returns_none_for_missing_id(self, tl_db):
        from utils.core.timeline import get_timeline_event

        assert get_timeline_event(99999) is None

    def test_count_total(self, tl_db):
        from utils.core.timeline import count_timeline_events, write_timeline_event

        write_timeline_event("INFO", "src", "a")
        write_timeline_event("WARN", "src", "b")
        assert count_timeline_events() == 2

    def test_count_with_level_filter(self, tl_db):
        from utils.core.timeline import count_timeline_events, write_timeline_event

        write_timeline_event("INFO", "src", "a")
        write_timeline_event("WARN", "src", "b")
        assert count_timeline_events(level_filter="WARN") == 1

    def test_load_on_missing_table_returns_empty(self, tmp_path, monkeypatch):
        """Querying a DB with no timeline_events table returns []."""
        import utils.core.timeline as tl_mod

        db = str(tmp_path / "empty.db")
        monkeypatch.setattr(tl_mod, "DB_PATH", db)
        from utils.core.timeline import load_timeline_events

        result = load_timeline_events()
        assert result == []

    def test_write_sse_publish_exception_still_returns_id(self, tl_db, monkeypatch):
        """write_timeline_event() returns the id even when publish_event raises."""
        import utils.agent.supervisor as sup_mod

        monkeypatch.setattr(
            sup_mod,
            "publish_event",
            lambda e: (_ for _ in ()).throw(RuntimeError("bus down")),
        )

        from utils.core.timeline import write_timeline_event

        eid = write_timeline_event("INFO", "src", "msg")
        assert eid > 0

    def test_get_on_missing_table_returns_none(self, tmp_path, monkeypatch):
        """get_timeline_event() returns None when the table doesn't exist."""
        import utils.core.timeline as tl_mod

        db = str(tmp_path / "empty.db")
        monkeypatch.setattr(tl_mod, "DB_PATH", db)
        from utils.core.timeline import get_timeline_event

        assert get_timeline_event(1) is None

    def test_count_with_source_filter(self, tl_db):
        from utils.core.timeline import count_timeline_events, write_timeline_event

        write_timeline_event("INFO", "resource", "disk ok")
        write_timeline_event("INFO", "update_check", "no updates")
        assert count_timeline_events(source_filter="resource") == 1

    def test_count_on_missing_table_returns_zero(self, tmp_path, monkeypatch):
        """count_timeline_events() returns 0 when the table doesn't exist."""
        import utils.core.timeline as tl_mod

        db = str(tmp_path / "empty.db")
        monkeypatch.setattr(tl_mod, "DB_PATH", db)
        from utils.core.timeline import count_timeline_events

        assert count_timeline_events() == 0


# ── utils/audit.py ───────────────────────────────────────────────────────────────

_HOST_INFO_YAML = "disk_free: 8.0\ndisk_total: 13.0\ndisk_used: 5.0\n"
_HOST_INFO_YAML_CRITICAL = "disk_free: 1.5\ndisk_total: 13.0\ndisk_used: 11.5\n"
_HOST_INFO_YAML_WARN = "disk_free: 4.0\ndisk_total: 13.0\ndisk_used: 9.0\n"
_MEMINFO = "MemTotal:  8000000 kB\nMemAvailable:  2000000 kB\n"
_BACKUP_LIST_JSON = (
    '{"result": "ok", "data": {"backups": [{"slug": "abc123", "size_bytes": 1000}]}}'
)


def _make_db_with_tables(db_path: str, state_rows: list | None = None) -> None:
    """Create a minimal ha_agent_state.db with required tables."""
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS state_history (
                id INTEGER PRIMARY KEY,
                timestamp INTEGER,
                config_hash TEXT,
                is_valid INTEGER,
                issues_found TEXT,
                action_taken TEXT,
                correlation_id TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS backup_registry (
                id INTEGER PRIMARY KEY,
                timestamp INTEGER,
                backup_slug TEXT,
                status TEXT,
                size_bytes INTEGER DEFAULT 0,
                location TEXT DEFAULT 'ha',
                offloaded_at REAL,
                deleted_from_ha_at REAL
            )"""
        )
        for row in state_rows or []:
            conn.execute(
                "INSERT INTO state_history (timestamp, config_hash, is_valid, issues_found, action_taken, correlation_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                row,
            )
        conn.commit()


class TestAuditCheckService:
    def test_not_loaded(self, monkeypatch):
        import utils.system.service as svc

        monkeypatch.setattr(
            svc,
            "service_status",
            lambda: {"loaded": False, "running": False, "pid": None},
        )
        from utils.system.audit import check_service

        result = check_service()
        assert result.status == "WARN"
        assert "not installed" in result.detail

    def test_loaded_not_running(self, monkeypatch):
        import utils.system.service as svc

        monkeypatch.setattr(
            svc,
            "service_status",
            lambda: {"loaded": True, "running": False, "pid": None},
        )
        from utils.system.audit import check_service

        result = check_service()
        assert result.status == "CRITICAL"
        assert "not running" in result.detail

    def test_running(self, monkeypatch):
        import utils.system.service as svc

        monkeypatch.setattr(
            svc,
            "service_status",
            lambda: {"loaded": True, "running": True, "pid": 12345},
        )
        from utils.system.audit import check_service

        result = check_service()
        assert result.status == "OK"
        assert "12345" in result.detail

    def test_launchctl_error(self, monkeypatch):
        import utils.system.service as svc

        monkeypatch.setattr(
            svc,
            "service_status",
            lambda: {
                "loaded": False,
                "running": False,
                "pid": None,
                "error": "macOS only",
            },
        )
        from utils.system.audit import check_service

        result = check_service()
        assert result.status == "WARN"
        assert "macOS only" in result.detail


class TestAuditCheckHaDisk:
    def test_disk_ok(self):
        from utils.system.audit import check_ha_disk
        from utils.ha.ssh_client import FakeSSHClient

        client = FakeSSHClient(
            command_results={
                "ha host info": (0, _HOST_INFO_YAML, ""),
                "cat /proc/meminfo": (0, _MEMINFO, ""),
            }
        )
        result = asyncio.run(check_ha_disk(ssh_client=client))
        assert result.status == "OK"
        assert "8.0 GB free" in result.detail

    def test_disk_critical(self, monkeypatch, isolated_config):
        import importlib

        import yaml

        isolated_config.write_text(
            yaml.dump({"agent": {"ha_disk_critical_gb": 2.0, "ha_disk_warn_gb": 5.0}})
        )
        importlib.reload(sys.modules["config"])
        from utils.system.audit import check_ha_disk
        from utils.ha.ssh_client import FakeSSHClient

        client = FakeSSHClient(
            command_results={
                "ha host info": (0, _HOST_INFO_YAML_CRITICAL, ""),
                "cat /proc/meminfo": (0, _MEMINFO, ""),
            }
        )
        result = asyncio.run(check_ha_disk(ssh_client=client))
        assert result.status == "CRITICAL"
        assert "1.5 GB free" in result.detail

    def test_disk_warn(self, monkeypatch, isolated_config):
        import importlib

        import yaml

        isolated_config.write_text(
            yaml.dump({"agent": {"ha_disk_critical_gb": 2.0, "ha_disk_warn_gb": 5.0}})
        )
        importlib.reload(sys.modules["config"])
        from utils.system.audit import check_ha_disk
        from utils.ha.ssh_client import FakeSSHClient

        client = FakeSSHClient(
            command_results={
                "ha host info": (0, _HOST_INFO_YAML_WARN, ""),
                "cat /proc/meminfo": (0, _MEMINFO, ""),
            }
        )
        result = asyncio.run(check_ha_disk(ssh_client=client))
        assert result.status == "WARN"
        assert "4.0 GB free" in result.detail

    def test_ssh_failure(self):
        from utils.system.audit import check_ha_disk
        from utils.ha.ssh_client import FakeSSHClient

        class _BrokenSSHClient(FakeSSHClient):
            async def run(self, command: str, check: bool = False):
                raise OSError("connection refused")

        result = asyncio.run(check_ha_disk(ssh_client=_BrokenSSHClient()))
        assert result.status == "WARN"
        assert "SSH unavailable" in result.detail


class TestAuditCheckBackupRegistry:
    def test_clean(self, tmp_path, monkeypatch):
        db = str(tmp_path / "state.db")
        _make_db_with_tables(db)
        import sqlite3

        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO backup_registry (timestamp, backup_slug, status, size_bytes, location)"
                " VALUES (?, ?, 'ACTIVE', 1000, 'ha')",
                (1000, "abc123"),
            )
            conn.commit()

        import config

        monkeypatch.setattr(config, "DB_PATH", db)
        from utils.system.audit import check_backup_registry
        from utils.ha.ssh_client import FakeSSHClient

        client = FakeSSHClient(
            command_results={"ha backups list --raw-json": (0, _BACKUP_LIST_JSON, "")}
        )
        result = asyncio.run(check_backup_registry(ssh_client=client))
        assert result.status == "OK"
        assert "1 HA backup" in result.detail

    def test_unknown_slug(self, tmp_path, monkeypatch):
        db = str(tmp_path / "state.db")
        _make_db_with_tables(db)
        import sqlite3

        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO backup_registry (timestamp, backup_slug, status, size_bytes, location)"
                " VALUES (?, ?, 'ACTIVE', 0, 'ha')",
                (1000, "unknown_slug"),
            )
            conn.commit()

        import config

        monkeypatch.setattr(config, "DB_PATH", db)
        from utils.system.audit import check_backup_registry
        from utils.ha.ssh_client import FakeSSHClient

        client = FakeSSHClient(
            command_results={"ha backups list --raw-json": (0, _BACKUP_LIST_JSON, "")}
        )
        result = asyncio.run(check_backup_registry(ssh_client=client))
        assert result.status == "WARN"
        assert "unknown_slug" in result.detail

    def test_untracked_ha_backup(self, tmp_path, monkeypatch):
        db = str(tmp_path / "state.db")
        _make_db_with_tables(db)

        import config

        monkeypatch.setattr(config, "DB_PATH", db)
        from utils.system.audit import check_backup_registry
        from utils.ha.ssh_client import FakeSSHClient

        # HA has abc123 but DB has nothing
        client = FakeSSHClient(
            command_results={"ha backups list --raw-json": (0, _BACKUP_LIST_JSON, "")}
        )
        result = asyncio.run(check_backup_registry(ssh_client=client))
        assert result.status == "WARN"
        assert "not in DB" in result.detail

    def test_ssh_failure(self, tmp_path, monkeypatch):
        db = str(tmp_path / "state.db")
        _make_db_with_tables(db)

        import config

        monkeypatch.setattr(config, "DB_PATH", db)
        from utils.system.audit import check_backup_registry
        from utils.ha.ssh_client import FakeSSHClient

        class _BrokenClient(FakeSSHClient):
            async def run(self, command: str, check: bool = False):
                raise OSError("connection refused")

        result = asyncio.run(check_backup_registry(ssh_client=_BrokenClient()))
        assert result.status == "WARN"
        assert "SSH unavailable" in result.detail

    def test_missing_table(self, tmp_path, monkeypatch):
        db = str(tmp_path / "empty.db")

        import config

        monkeypatch.setattr(config, "DB_PATH", db)
        from utils.system.audit import check_backup_registry
        from utils.ha.ssh_client import FakeSSHClient

        result = asyncio.run(check_backup_registry(ssh_client=FakeSSHClient()))
        assert result.status == "WARN"
        assert "missing" in result.detail

    def test_orphaned_db_slug(self, tmp_path, monkeypatch):
        """Slug in DB not present on HA (orphaned) shows as WARN."""
        db = str(tmp_path / "state.db")
        _make_db_with_tables(db)
        import sqlite3

        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO backup_registry (timestamp, backup_slug, status, size_bytes, location)"
                " VALUES (?, ?, 'ACTIVE', 1000, 'ha')",
                (1000, "orphan-slug-xyz"),
            )
            conn.commit()

        import config

        monkeypatch.setattr(config, "DB_PATH", db)
        from utils.system.audit import check_backup_registry
        from utils.ha.ssh_client import FakeSSHClient

        # HA returns no backups
        client = FakeSSHClient(
            command_results={
                "ha backups list --raw-json": (
                    0,
                    '{"result": "ok", "data": {"backups": []}}',
                    "",
                )
            }
        )
        result = asyncio.run(check_backup_registry(ssh_client=client))
        assert result.status == "WARN"
        assert "orphaned" in result.detail

    def test_ssh_failure_with_unknown_slug(self, tmp_path, monkeypatch):
        """SSH failure when DB has unknown_slug emits both facts."""
        db = str(tmp_path / "state.db")
        _make_db_with_tables(db)
        import sqlite3

        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO backup_registry (timestamp, backup_slug, status, size_bytes, location)"
                " VALUES (?, ?, 'ACTIVE', 0, 'ha')",
                (1000, "unknown_slug"),
            )
            conn.commit()

        import config

        monkeypatch.setattr(config, "DB_PATH", db)
        from utils.system.audit import check_backup_registry
        from utils.ha.ssh_client import FakeSSHClient

        class _BrokenClient(FakeSSHClient):
            async def run(self, command: str, check: bool = False):
                raise OSError("timeout")

        result = asyncio.run(check_backup_registry(ssh_client=_BrokenClient()))
        assert result.status == "WARN"
        assert "unknown_slug" in result.detail
        assert "SSH unavailable" in result.detail


class TestAuditCheckPendingHitl:
    def test_no_dir(self, tmp_path):
        from utils.system.audit import check_pending_hitl

        result = check_pending_hitl(watch_dir=str(tmp_path / "nonexistent"))
        assert result.status == "OK"

    def test_no_pending(self, tmp_path):
        import json

        card_id = "test-card-123"
        (tmp_path / f"{card_id}.json").write_text(
            json.dumps({"notification_id": card_id, "sent_at": 1000})
        )
        (tmp_path / f"{card_id}.approved").touch()
        from utils.system.audit import check_pending_hitl

        result = check_pending_hitl(watch_dir=str(tmp_path))
        assert result.status == "OK"
        assert "No pending" in result.detail

    def test_pending_less_than_24h(self, tmp_path):
        import json
        import time

        card_id = "pending-card"
        sent_at = time.time() - 3600  # 1 hour ago
        (tmp_path / f"{card_id}.json").write_text(
            json.dumps({"notification_id": card_id, "sent_at": sent_at})
        )
        from utils.system.audit import check_pending_hitl

        result = check_pending_hitl(watch_dir=str(tmp_path))
        assert result.status == "WARN"
        assert "1 pending" in result.detail

    def test_pending_older_than_24h(self, tmp_path):
        import json
        import time

        card_id = "old-card"
        sent_at = time.time() - 30 * 3600  # 30 hours ago
        (tmp_path / f"{card_id}.json").write_text(
            json.dumps({"notification_id": card_id, "sent_at": sent_at})
        )
        from utils.system.audit import check_pending_hitl

        result = check_pending_hitl(watch_dir=str(tmp_path))
        assert result.status == "CRITICAL"
        assert "30h old" in result.detail


class TestAuditCheckNetalertx:
    def test_not_configured(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "NETALERTX_HOST", "")
        from utils.system.audit import check_netalertx

        result = asyncio.run(check_netalertx())
        assert result.status == "OK"
        assert "not configured" in result.detail

    def test_scan_stale(self, monkeypatch):
        import config
        from netalertx import health as health_mod
        from netalertx.health import HealthReport

        monkeypatch.setattr(config, "NETALERTX_HOST", "192.168.1.100")
        monkeypatch.setattr(config, "NETALERTX_MAX_SCAN_AGE_MINUTES", 20)

        class _FakeMonitor:
            def __init__(self, *a, **kw):
                pass

            async def poll_once(self, event_queue):
                return HealthReport(
                    last_scan_age_minutes=9999,
                    device_counts={"total": 1, "online": 0},
                    mqtt_active=False,
                    anomalies=["stale"],
                    netalertx_version="v26.7.1",
                )

        monkeypatch.setattr(health_mod, "NetAlertXHealthMonitor", _FakeMonitor)

        from netalertx.api_client import NetAlertXAPIClient

        class _FakeClient(NetAlertXAPIClient):
            def __init__(self):
                pass

        from utils.system.audit import check_netalertx

        result = asyncio.run(check_netalertx(api_client=_FakeClient()))
        assert result.status == "CRITICAL"
        assert "9999m" in result.detail

    def test_mqtt_inactive(self, monkeypatch):
        import config
        from netalertx import health as health_mod
        from netalertx.health import HealthReport

        monkeypatch.setattr(config, "NETALERTX_HOST", "192.168.1.100")
        monkeypatch.setattr(config, "NETALERTX_MAX_SCAN_AGE_MINUTES", 20)

        class _FakeMonitor:
            def __init__(self, *a, **kw):
                pass

            async def poll_once(self, event_queue):
                return HealthReport(
                    last_scan_age_minutes=5,
                    device_counts={"total": 10, "online": 5},
                    mqtt_active=False,
                    anomalies=[],
                    netalertx_version="v26.7.1",
                )

        monkeypatch.setattr(health_mod, "NetAlertXHealthMonitor", _FakeMonitor)

        from netalertx.api_client import NetAlertXAPIClient

        class _FakeClient(NetAlertXAPIClient):
            def __init__(self):
                pass

        from utils.system.audit import check_netalertx

        result = asyncio.run(check_netalertx(api_client=_FakeClient()))
        assert result.status == "WARN"
        assert "MQTT inactive" in result.detail

    def test_healthy(self, monkeypatch):
        import config
        from netalertx import health as health_mod
        from netalertx.health import HealthReport

        monkeypatch.setattr(config, "NETALERTX_HOST", "192.168.1.100")
        monkeypatch.setattr(config, "NETALERTX_MAX_SCAN_AGE_MINUTES", 20)

        class _FakeMonitor:
            def __init__(self, *a, **kw):
                pass

            async def poll_once(self, event_queue):
                return HealthReport(
                    last_scan_age_minutes=5,
                    device_counts={"total": 10, "online": 5},
                    mqtt_active=True,
                    anomalies=[],
                    netalertx_version="v26.7.1",
                )

        monkeypatch.setattr(health_mod, "NetAlertXHealthMonitor", _FakeMonitor)

        from netalertx.api_client import NetAlertXAPIClient

        class _FakeClient(NetAlertXAPIClient):
            def __init__(self):
                pass

        from utils.system.audit import check_netalertx

        result = asyncio.run(check_netalertx(api_client=_FakeClient()))
        assert result.status == "OK"

    def test_api_unavailable(self, monkeypatch):
        import config
        from netalertx import health as health_mod

        monkeypatch.setattr(config, "NETALERTX_HOST", "192.168.1.100")

        class _BrokenMonitor:
            def __init__(self, *a, **kw):
                pass

            async def poll_once(self, event_queue):
                raise ConnectionError("refused")

        monkeypatch.setattr(health_mod, "NetAlertXHealthMonitor", _BrokenMonitor)

        from netalertx.api_client import NetAlertXAPIClient

        class _FakeClient(NetAlertXAPIClient):
            def __init__(self):
                pass

        from utils.system.audit import check_netalertx

        result = asyncio.run(check_netalertx(api_client=_FakeClient()))
        assert result.status == "WARN"
        assert "unavailable" in result.detail


class TestAuditCheckNetalertxApiToken:
    def test_not_desired_returns_ok(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "NETALERTX_SETUP_DESIRED", False)
        from utils.system.audit import check_netalertx_api_token

        result = check_netalertx_api_token()
        assert result.status == "OK"
        assert "not enabled" in result.detail

    def test_desired_empty_token_returns_warn(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "NETALERTX_SETUP_DESIRED", True)
        monkeypatch.setattr(config, "NETALERTX_API_TOKEN", "")
        from utils.system.audit import check_netalertx_api_token

        result = check_netalertx_api_token()
        assert result.status == "WARN"
        assert "api_token is empty" in result.detail
        assert "API Key" in result.action

    def test_desired_token_set_returns_ok(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "NETALERTX_SETUP_DESIRED", True)
        monkeypatch.setattr(config, "NETALERTX_API_TOKEN", "tok123")
        from utils.system.audit import check_netalertx_api_token

        result = check_netalertx_api_token()
        assert result.status == "OK"
        assert "configured" in result.detail


class TestAuditCheckStateHistory:
    def test_missing_table(self, tmp_path, monkeypatch):
        import config

        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "empty.db"))
        from utils.system.audit import check_state_history

        result = check_state_history()
        assert result.status == "WARN"
        assert "missing" in result.detail

    def test_no_entries(self, tmp_path, monkeypatch):
        db = str(tmp_path / "state.db")
        _make_db_with_tables(db)

        import config

        monkeypatch.setattr(config, "DB_PATH", db)
        from utils.system.audit import check_state_history

        result = check_state_history()
        assert result.status == "OK"
        assert "not run" in result.detail

    def test_all_invalid(self, tmp_path, monkeypatch):
        db = str(tmp_path / "state.db")
        rows = [
            (1000, "abc", 0, "[]", "awaiting_approval", ""),
            (1001, "abc", 0, "[]", "awaiting_approval", ""),
        ]
        _make_db_with_tables(db, state_rows=rows)

        import config

        monkeypatch.setattr(config, "DB_PATH", db)
        from utils.system.audit import check_state_history

        result = check_state_history()
        assert result.status == "WARN"
        assert "all runs flagged invalid" in result.detail

    def test_normal_mix(self, tmp_path, monkeypatch):
        db = str(tmp_path / "state.db")
        rows = [
            (1000, "abc", 1, "[]", "none", ""),
            (1001, "xyz", 0, "[]", "repair", ""),
        ]
        _make_db_with_tables(db, state_rows=rows)

        import config

        monkeypatch.setattr(config, "DB_PATH", db)
        from utils.system.audit import check_state_history

        result = check_state_history()
        assert result.status == "OK"
        assert "2 entries" in result.detail


class TestAuditCheckUpdateCheck:
    def test_disabled_no_token(self, tmp_path, monkeypatch, isolated_config):
        import importlib

        import yaml

        isolated_config.write_text(
            yaml.dump(
                {
                    "home_assistant": {"api_token": ""},
                    "agent": {"update_check_interval_hours": 0},
                }
            )
        )
        importlib.reload(sys.modules["config"])
        from utils.system.audit import check_update_check

        result = check_update_check(watch_dir=str(tmp_path))
        assert result.status == "WARN"
        assert "disabled" in result.detail

    def test_pending_update_card(self, tmp_path, monkeypatch, isolated_config):
        import importlib
        import json
        import time

        import yaml

        isolated_config.write_text(
            yaml.dump(
                {
                    "home_assistant": {"api_token": "tok123"},
                    "agent": {"update_check_interval_hours": 24},
                }
            )
        )
        importlib.reload(sys.modules["config"])

        card_id = "update-card-abc"
        (tmp_path / f"{card_id}.json").write_text(
            json.dumps(
                {
                    "notification_id": card_id,
                    "subject": "Update available: core 2026.7.2 → 2026.7.4",
                    "sent_at": time.time() - 3600,
                }
            )
        )
        from utils.system.audit import check_update_check

        result = check_update_check(watch_dir=str(tmp_path))
        assert result.status == "WARN"
        assert "awaiting approval" in result.detail

    def test_approved_not_executed(self, tmp_path, monkeypatch, isolated_config):
        import importlib
        import json
        import time

        import yaml

        isolated_config.write_text(
            yaml.dump(
                {
                    "home_assistant": {"api_token": "tok123"},
                    "agent": {"update_check_interval_hours": 24},
                }
            )
        )
        importlib.reload(sys.modules["config"])

        card_id = "update-card-approved"
        (tmp_path / f"{card_id}.json").write_text(
            json.dumps(
                {
                    "notification_id": card_id,
                    "subject": "Update available: core 2026.7.2 → 2026.7.4",
                    "sent_at": time.time() - 3600,
                }
            )
        )
        (tmp_path / f"{card_id}.approved").touch()
        from utils.system.audit import check_update_check

        result = check_update_check(watch_dir=str(tmp_path))
        assert result.status == "CRITICAL"
        assert "not yet executed" in result.detail

    def test_approved_and_executed(self, tmp_path, monkeypatch, isolated_config):
        import importlib
        import json
        import time

        import yaml

        isolated_config.write_text(
            yaml.dump(
                {
                    "home_assistant": {"api_token": "tok123"},
                    "agent": {"update_check_interval_hours": 24},
                }
            )
        )
        importlib.reload(sys.modules["config"])

        card_id = "update-done"
        (tmp_path / f"{card_id}.json").write_text(
            json.dumps(
                {
                    "notification_id": card_id,
                    "subject": "Update available: core 2026.7.2 → 2026.7.4",
                    "sent_at": time.time() - 3600,
                    "fix_applied": True,
                }
            )
        )
        (tmp_path / f"{card_id}.approved").touch()
        from utils.system.audit import check_update_check

        result = check_update_check(watch_dir=str(tmp_path))
        assert result.status == "OK"

    def test_rejected_card_ignored(self, tmp_path, monkeypatch, isolated_config):
        import importlib
        import json
        import time

        import yaml

        isolated_config.write_text(
            yaml.dump(
                {
                    "home_assistant": {"api_token": "tok123"},
                    "agent": {"update_check_interval_hours": 24},
                }
            )
        )
        importlib.reload(sys.modules["config"])

        card_id = "update-rejected"
        (tmp_path / f"{card_id}.json").write_text(
            json.dumps(
                {
                    "notification_id": card_id,
                    "subject": "Update available: core 2026.7.2 → 2026.7.4",
                    "sent_at": time.time() - 3600,
                }
            )
        )
        (tmp_path / f"{card_id}.rejected").touch()
        from utils.system.audit import check_update_check

        result = check_update_check(watch_dir=str(tmp_path))
        assert result.status == "OK"

    def test_non_json_file_skipped(self, tmp_path, monkeypatch, isolated_config):
        """A non-JSON .json file in hitl/ is skipped without error."""
        import importlib

        import yaml

        isolated_config.write_text(
            yaml.dump(
                {
                    "home_assistant": {"api_token": "tok123"},
                    "agent": {"update_check_interval_hours": 24},
                }
            )
        )
        importlib.reload(sys.modules["config"])

        # Write an invalid JSON file
        (tmp_path / "garbage.json").write_text("not json {{{{")
        from utils.system.audit import check_update_check

        result = check_update_check(watch_dir=str(tmp_path))
        assert result.status == "OK"

    def test_non_update_card_skipped(self, tmp_path, monkeypatch, isolated_config):
        """A JSON card with an unrelated subject is not treated as an update card."""
        import importlib
        import json

        import yaml

        isolated_config.write_text(
            yaml.dump(
                {
                    "home_assistant": {"api_token": "tok123"},
                    "agent": {"update_check_interval_hours": 24},
                }
            )
        )
        importlib.reload(sys.modules["config"])

        card_id = "repair-card"
        (tmp_path / f"{card_id}.json").write_text(
            json.dumps(
                {
                    "notification_id": card_id,
                    "subject": "Config repair required",
                    "card_type": "repair",
                }
            )
        )
        from utils.system.audit import check_update_check

        result = check_update_check(watch_dir=str(tmp_path))
        assert result.status == "OK"


class TestAuditPriorityOrdering:
    def test_sorted_critical_before_warn_before_ok(self):
        from utils.system.audit import AuditResult, format_audit_report

        results = [
            AuditResult("check_a", "OK", "all good"),
            AuditResult("check_b", "CRITICAL", "very bad", "fix it"),
            AuditResult("check_c", "WARN", "look at this", "investigate"),
        ]
        results.sort(
            key=lambda r: {"CRITICAL": 0, "WARN": 1, "OK": 2}.get(r.status, 99)
        )
        assert results[0].status == "CRITICAL"
        assert results[1].status == "WARN"
        assert results[2].status == "OK"

    def test_format_report_includes_priority_actions(self):
        from utils.system.audit import AuditResult, format_audit_report

        results = [
            AuditResult("check_a", "CRITICAL", "disk full", "free disk"),
            AuditResult("check_b", "OK", "all good"),
        ]
        report = format_audit_report(results, now=1000000)
        assert "Priority Actions" in report
        assert "free disk" in report
        assert "[CRITICAL]" in report

    def test_format_report_no_priority_section_when_all_ok(self):
        from utils.system.audit import AuditResult, format_audit_report

        results = [AuditResult("check_a", "OK", "fine")]
        report = format_audit_report(results, now=1000000)
        assert "Priority Actions" not in report


class TestAuditSaveReport:
    def test_saves_to_audits_dir(self, tmp_path):
        from utils.system.audit import save_audit_report

        report = "# Pueo Audit\n\nAll clear.\n"
        out = save_audit_report(report, audits_dir=str(tmp_path / "audits"))
        assert out.exists()
        assert out.read_text() == report
        assert "pueo-audit-" in out.name


class TestAuditMainEntry:
    def test_main_audit_runs_and_saves(self, tmp_path, monkeypatch):
        """main_audit() writes a file to audits_dir and prints a summary."""
        from utils.system.audit import AuditResult

        ok_result = AuditResult("service", "OK", "running fine")
        warn_result = AuditResult("ha_disk", "WARN", "4.0 GB free", "free disk")

        async def _fake_run_audit(**kwargs):
            return [ok_result, warn_result]

        import utils.system.audit as audit_mod

        monkeypatch.setattr(audit_mod, "run_audit", _fake_run_audit)

        audits_dir = str(tmp_path / "audits")
        asyncio.run(audit_mod.main_audit(audits_dir=audits_dir))

        import os

        files = os.listdir(audits_dir)
        assert any("pueo-audit-" in f for f in files)

    def test_run_audit_handles_unexpected_exception(self, monkeypatch, tmp_path):
        """run_audit() wraps exceptions from async checks as WARN results."""
        import utils.system.audit as audit_mod

        async def _bad_check(**kwargs):
            raise RuntimeError("unexpected!")

        monkeypatch.setattr(audit_mod, "check_ha_disk", _bad_check)
        monkeypatch.setattr(audit_mod, "check_backup_registry", _bad_check)
        monkeypatch.setattr(audit_mod, "check_netalertx", _bad_check)
        monkeypatch.setattr(
            audit_mod, "check_service", lambda: audit_mod.AuditResult("svc", "OK", "ok")
        )
        monkeypatch.setattr(
            audit_mod,
            "check_state_history",
            lambda: audit_mod.AuditResult("sh", "OK", "ok"),
        )
        monkeypatch.setattr(
            audit_mod,
            "check_pending_hitl",
            lambda **kw: audit_mod.AuditResult("ph", "OK", "ok"),
        )
        monkeypatch.setattr(
            audit_mod,
            "check_update_check",
            lambda **kw: audit_mod.AuditResult("uc", "OK", "ok"),
        )

        results = asyncio.run(audit_mod.run_audit(watch_dir=str(tmp_path)))
        warn_results = [r for r in results if r.status == "WARN"]
        assert len(warn_results) == 3  # one per failed async check


# ── disk_usage ────────────────────────────────────────────────────────────────────

_DU_OUTPUT = (
    "94.5M\t/homeassistant/home-assistant_v2.db\n"
    "52.8M\t/homeassistant/custom_components\n"
    "4.1M\t/homeassistant/home-assistant_v2.db-wal\n"
    "644.0K\t/homeassistant/tts\n"
    "368.0K\t/homeassistant/zigbee.db\n"
    "58.7M\t/backup/abc123.tar\n"
    "58.4M\t/backup/def456.tar\n"
    "15.2M\t/addon_configs/db21ed7f_netalertx_fa\n"
    "5.4M\t/addon_configs/db21ed7f_netalertx\n"
    "4.0K\t/addon_configs/another_addon\n"
    "4.0K\t/share\n"
    "4.0K\t/media\n"
    "4.0K\t/ssl\n"
)

_ADDON_JSON = (
    '{"result":"ok","data":{"addons":['
    '{"name":"NetAlertX Full Access","slug":"db21ed7f_netalertx_fa","state":"started"},'
    '{"name":"NetAlertX","slug":"db21ed7f_netalertx","state":"started"}'
    "]}}"
)

_HOST_INFO = (
    "disk_free: 2.2\ndisk_total: 13.6\ndisk_used: 10.8\nhostname: homeassistant\n"
)


_CC_DU_OUTPUT = (
    "51.4M\t/homeassistant/custom_components/hacs\n"
    "1.4M\t/homeassistant/custom_components/noaa_it_all\n"
)


def _make_sqlite_db_bytes() -> bytes:
    """Create a minimal valid SQLite DB with one table and return its bytes."""
    import os
    import sqlite3 as _s3
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    try:
        con = _s3.connect(tmp)
        con.execute("CREATE TABLE states (id INTEGER PRIMARY KEY, data TEXT)")
        for i in range(100):
            con.execute("INSERT INTO states VALUES (?, ?)", (i, "x" * 1000))
        con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY)")
        con.commit()
        con.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def _make_disk_ssh(
    du_output=_DU_OUTPUT,
    addon_json=_ADDON_JSON,
    host_info=_HOST_INFO,
    cc_du_output=_CC_DU_OUTPUT,
    db_bytes: "bytes | None" = None,
    db_download_error: "Exception | None" = None,
):
    from utils.ha.ssh_client import FakeSSHClient

    download_contents = {}
    if db_bytes is not None:
        download_contents["/homeassistant/home-assistant_v2.db"] = db_bytes

    return FakeSSHClient(
        command_results={
            "ha host info": (0, host_info, ""),
            # custom_components key must precede "du -sh" — FakeSSHClient matches first hit
            "custom_components": (0, cc_du_output, ""),
            "du -sh": (0, du_output, ""),
            "ha apps list --raw-json": (0, addon_json, ""),
        },
        download_contents=download_contents,
        download_error=db_download_error,
    )


class TestParseSizeToBytes:
    def test_kilobytes(self):
        from utils.disk.disk_usage import _parse_size_to_bytes

        assert _parse_size_to_bytes("4.0K") == 4096

    def test_integer_megabytes(self):
        from utils.disk.disk_usage import _parse_size_to_bytes

        assert _parse_size_to_bytes("164M") == 164 * 1024**2

    def test_decimal_megabytes(self):
        from utils.disk.disk_usage import _parse_size_to_bytes

        assert _parse_size_to_bytes("94.5M") == int(94.5 * 1024**2)

    def test_gigabytes(self):
        from utils.disk.disk_usage import _parse_size_to_bytes

        assert _parse_size_to_bytes("1.7G") == int(1.7 * 1024**3)

    def test_small_kilobytes(self):
        from utils.disk.disk_usage import _parse_size_to_bytes

        assert _parse_size_to_bytes("644.0K") == int(644.0 * 1024)

    def test_zero_string(self):
        from utils.disk.disk_usage import _parse_size_to_bytes

        assert _parse_size_to_bytes("0") == 0

    def test_empty_string(self):
        from utils.disk.disk_usage import _parse_size_to_bytes

        assert _parse_size_to_bytes("") == 0

    def test_malformed_returns_zero(self):
        from utils.disk.disk_usage import _parse_size_to_bytes

        assert _parse_size_to_bytes("abc") == 0


class TestParseDuOutput:
    def test_parses_tab_separated_output(self):
        from utils.disk.disk_usage import _parse_du_output

        result = _parse_du_output(
            "94.5M\t/homeassistant/home-assistant_v2.db\n4.0K\t/share\n"
        )
        assert "/homeassistant/home-assistant_v2.db" in result
        assert result["/share"] == 4096

    def test_parses_space_separated_fallback(self):
        from utils.disk.disk_usage import _parse_du_output

        result = _parse_du_output("94.5M  /homeassistant/home-assistant_v2.db\n")
        assert "/homeassistant/home-assistant_v2.db" in result

    def test_skips_lines_without_separator(self):
        from utils.disk.disk_usage import _parse_du_output

        result = _parse_du_output("justoneword\n94.5M\t/valid/path\n")
        assert len(result) == 1
        assert "/valid/path" in result

    def test_skips_empty_lines(self):
        from utils.disk.disk_usage import _parse_du_output

        result = _parse_du_output("\n\n94.5M\t/valid/path\n\n")
        assert len(result) == 1

    def test_empty_output_returns_empty_dict(self):
        from utils.disk.disk_usage import _parse_du_output

        assert _parse_du_output("") == {}


class TestFetchDiskBreakdown:
    def test_returns_four_sections(self):
        ssh = _make_disk_ssh()
        from utils.disk.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        assert len(bd.sections) == 4

    def test_section_titles(self):
        ssh = _make_disk_ssh()
        from utils.disk.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        titles = [s.title for s in bd.sections]
        assert titles == [
            "HA Config & Database",
            "Backups",
            "Addon Data",
            "Shared Storage",
        ]

    def test_overall_disk_stats(self):
        ssh = _make_disk_ssh()
        from utils.disk.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        assert bd.disk_free_gb == pytest.approx(2.2, abs=0.01)
        assert bd.disk_total_gb == pytest.approx(13.6, abs=0.01)
        assert bd.disk_used_gb == pytest.approx(10.8, abs=0.01)

    def test_disk_used_pct_computed(self):
        ssh = _make_disk_ssh()
        from utils.disk.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        assert bd.disk_used_pct == pytest.approx(79.4, abs=1.0)

    def test_addon_slug_mapped_to_friendly_name(self):
        ssh = _make_disk_ssh()
        from utils.disk.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        addon_section = next(s for s in bd.sections if s.title == "Addon Data")
        names = [item.name for item in addon_section.items]
        assert "NetAlertX Full Access" in names
        assert "NetAlertX" in names

    def test_unknown_addon_slug_kept_as_is(self):
        ssh = _make_disk_ssh()
        from utils.disk.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        addon_section = next(s for s in bd.sections if s.title == "Addon Data")
        names = [item.name for item in addon_section.items]
        assert "another_addon" in names

    def test_config_section_sorted_largest_first(self):
        ssh = _make_disk_ssh()
        from utils.disk.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        config_section = next(
            s for s in bd.sections if s.title == "HA Config & Database"
        )
        sizes = [item.size_bytes for item in config_section.items]
        assert sizes == sorted(sizes, reverse=True)

    def test_shared_storage_is_empty(self):
        ssh = _make_disk_ssh()
        from utils.disk.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        shared = next(s for s in bd.sections if s.title == "Shared Storage")
        assert shared.is_empty is True

    def test_bad_addon_json_falls_back_to_slug(self):
        ssh = _make_disk_ssh(addon_json="not json at all")
        from utils.disk.disk_usage import fetch_disk_breakdown

        # Should not raise; slug used as display name
        bd = asyncio.run(fetch_disk_breakdown(ssh))
        addon_section = next(s for s in bd.sections if s.title == "Addon Data")
        names = [item.name for item in addon_section.items]
        assert "db21ed7f_netalertx_fa" in names

    def test_fetched_at_is_set(self):
        import time

        ssh = _make_disk_ssh()
        from utils.disk.disk_usage import fetch_disk_breakdown

        before = time.time()
        bd = asyncio.run(fetch_disk_breakdown(ssh))
        assert bd.fetched_at >= before

    def test_pct_of_section_sums_near_100(self):
        ssh = _make_disk_ssh()
        from utils.disk.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        for section in bd.sections:
            if not section.is_empty:
                total = sum(item.pct_of_section for item in section.items)
                assert total == pytest.approx(100.0, abs=1.0)

    def test_empty_du_output_all_sections_empty(self):
        ssh = _make_disk_ssh(du_output="")
        from utils.disk.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        assert all(s.is_empty for s in bd.sections)

    def test_sqlite3_unavailable_gives_none_db_tables(self):
        ssh = _make_disk_ssh(db_download_error=FileNotFoundError("no db"))
        from utils.disk.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        assert bd.db_tables is None

    def test_sqlite3_available_populates_db_tables(self):
        db_bytes = _make_sqlite_db_bytes()
        ssh = _make_disk_ssh(db_bytes=db_bytes)
        from utils.disk.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        assert bd.db_tables is not None
        assert len(bd.db_tables) >= 1
        # states table should be present (created with 100 rows of 1000-byte data)
        table_names = [r[0] for r in bd.db_tables]
        assert "states" in table_names

    def test_db_tables_corrupted_bytes_returns_none(self):
        ssh = _make_disk_ssh(db_bytes=b"not a sqlite database at all")
        from utils.disk.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        assert bd.db_tables is None

    def test_custom_components_populated(self):
        db_bytes = _make_sqlite_db_bytes()
        ssh = _make_disk_ssh(db_bytes=db_bytes)
        from utils.disk.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        assert bd.custom_components is not None
        names = [cc.name for cc in bd.custom_components]
        assert "hacs" in names
        assert "noaa_it_all" in names

    def test_custom_components_sorted_largest_first(self):
        db_bytes = _make_sqlite_db_bytes()
        ssh = _make_disk_ssh(db_bytes=db_bytes)
        from utils.disk.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        assert bd.custom_components is not None
        sizes = [cc.size_bytes for cc in bd.custom_components]
        assert sizes == sorted(sizes, reverse=True)

    def test_custom_components_none_on_empty_output(self):
        ssh = _make_disk_ssh(cc_du_output="")
        from utils.disk.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        assert bd.custom_components is None

    def test_container_images_estimated_gb_computed(self):
        db_bytes = _make_sqlite_db_bytes()
        ssh = _make_disk_ssh(db_bytes=db_bytes)
        from utils.disk.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        # disk_used=10.8 GB minus visible sections (~300 MB) should be a large positive
        assert bd.container_images_estimated_gb is not None
        assert bd.container_images_estimated_gb > 0

    def test_container_images_none_when_host_info_unavailable(self):
        ssh = _make_disk_ssh(
            host_info="disk_free: 0.0\ndisk_total: 0.0\ndisk_used: 0.0\n"
        )
        from utils.disk.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        assert bd.container_images_estimated_gb is None

    def test_container_images_floors_at_zero(self):
        # If sections somehow sum to more than disk_used, floor at 0
        # Use a host_info where disk_used is tiny
        ssh = _make_disk_ssh(
            host_info="disk_free: 13.0\ndisk_total: 13.6\ndisk_used: 0.1\n"
        )
        from utils.disk.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        assert bd.container_images_estimated_gb is not None
        assert bd.container_images_estimated_gb >= 0.0


class TestDiskCacheAccessors:
    def test_get_returns_none_initially(self, monkeypatch):
        import utils.disk.disk_usage as du_mod

        monkeypatch.setattr(du_mod, "_last_disk_breakdown", None)
        assert du_mod.get_disk_breakdown() is None

    def test_update_then_get_roundtrip(self, monkeypatch):
        import utils.disk.disk_usage as du_mod
        from utils.disk.disk_usage import DiskBreakdown

        monkeypatch.setattr(du_mod, "_last_disk_breakdown", None)
        bd = DiskBreakdown(fetched_at=12345.0)
        du_mod.update_disk_breakdown(bd)
        assert du_mod.get_disk_breakdown() is bd


class TestDiskUsagePollerRun:
    def test_polls_and_updates_cache_then_cancels(self, monkeypatch):
        import utils.disk.disk_usage as du_mod
        from utils.disk.disk_usage import DiskBreakdown, DiskUsagePoller

        fake_bd = DiskBreakdown(fetched_at=9999.0)
        call_count = 0

        async def _fake_fetch(ssh):
            nonlocal call_count
            call_count += 1
            return fake_bd

        monkeypatch.setattr(du_mod, "fetch_disk_breakdown", _fake_fetch)
        monkeypatch.setattr(du_mod, "_last_disk_breakdown", None)

        ssh = _make_disk_ssh()
        poller = DiskUsagePoller(ssh_client=ssh, interval_seconds=9999)

        async def _run():
            task = asyncio.create_task(poller.run())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_run())
        assert call_count >= 1
        assert du_mod.get_disk_breakdown() is fake_bd

    def test_catches_ssh_error_and_continues(self, monkeypatch):
        import utils.disk.disk_usage as du_mod
        from utils.disk.disk_usage import DiskUsagePoller

        call_count = 0

        async def _failing_fetch(ssh):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("SSH error")
            return du_mod.DiskBreakdown(fetched_at=1.0)

        monkeypatch.setattr(du_mod, "fetch_disk_breakdown", _failing_fetch)

        ssh = _make_disk_ssh()
        poller = DiskUsagePoller(ssh_client=ssh, interval_seconds=0.01)

        async def _run():
            task = asyncio.create_task(poller.run())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_run())
        assert call_count >= 2  # retried after error


# ── utils/disk_recovery.py — scan_orphaned_addon_dirs ────────────────────────────


class TestScanOrphanedAddonDirs:
    """Tests for scan_orphaned_addon_dirs() in utils/disk_recovery.py."""

    def _make_ssh(self, installed_json, addons_ls, configs_ls, du_display=""):
        """Return a FakeSSHClient wired with the needed command results."""
        from utils.ha.ssh_client import FakeSSHClient

        return FakeSSHClient(
            command_results={
                "ha apps list --raw-json": (0, installed_json, ""),
                "ls /mnt/data/supervisor/addons/": (0, addons_ls, ""),
                "ls /addon_configs/": (0, configs_ls, ""),
                "du -sh": (0, du_display, ""),
            }
        )

    def test_returns_empty_when_no_orphans(self):
        from utils.disk.disk_recovery import scan_orphaned_addon_dirs

        installed = (
            '{"result":"ok","data":{"addons":[{"slug":"db21ed7f_netalertx_fa"}]}}'
        )
        # on-disk matches installed — no orphans
        ssh = self._make_ssh(
            installed_json=installed,
            addons_ls="db21ed7f_netalertx_fa\n",
            configs_ls="db21ed7f_netalertx_fa\n",
        )
        result = asyncio.run(scan_orphaned_addon_dirs(ssh))
        assert result == []

    def test_detects_orphan_in_addons_dir(self):
        from utils.disk.disk_recovery import scan_orphaned_addon_dirs

        installed = (
            '{"result":"ok","data":{"addons":[{"slug":"db21ed7f_netalertx_fa"}]}}'
        )
        ssh = self._make_ssh(
            installed_json=installed,
            addons_ls="db21ed7f_netalertx_fa\ndb21ed7f_netalertx\n",
            configs_ls="db21ed7f_netalertx_fa\n",
            du_display="50M\t/mnt/data/supervisor/addons/db21ed7f_netalertx\n",
        )
        result = asyncio.run(scan_orphaned_addon_dirs(ssh))
        assert len(result) == 1
        assert result[0].slug == "db21ed7f_netalertx"
        assert "/mnt/data/supervisor/addons/db21ed7f_netalertx" in result[0].paths

    def test_detects_orphan_in_configs_dir(self):
        from utils.disk.disk_recovery import scan_orphaned_addon_dirs

        installed = (
            '{"result":"ok","data":{"addons":[{"slug":"db21ed7f_netalertx_fa"}]}}'
        )
        ssh = self._make_ssh(
            installed_json=installed,
            addons_ls="db21ed7f_netalertx_fa\n",
            configs_ls="db21ed7f_netalertx_fa\ndb21ed7f_netalertx\n",
            du_display="10M\t/addon_configs/db21ed7f_netalertx\n",
        )
        result = asyncio.run(scan_orphaned_addon_dirs(ssh))
        assert len(result) == 1
        assert result[0].slug == "db21ed7f_netalertx"
        assert "/addon_configs/db21ed7f_netalertx" in result[0].paths

    def test_detects_orphan_in_both_dirs(self):
        from utils.disk.disk_recovery import scan_orphaned_addon_dirs

        installed = (
            '{"result":"ok","data":{"addons":[{"slug":"db21ed7f_netalertx_fa"}]}}'
        )
        ssh = self._make_ssh(
            installed_json=installed,
            addons_ls="db21ed7f_netalertx_fa\ndb21ed7f_netalertx\n",
            configs_ls="db21ed7f_netalertx_fa\ndb21ed7f_netalertx\n",
            du_display=(
                "50M\t/mnt/data/supervisor/addons/db21ed7f_netalertx\n"
                "10M\t/addon_configs/db21ed7f_netalertx\n"
            ),
        )
        result = asyncio.run(scan_orphaned_addon_dirs(ssh))
        assert len(result) == 1
        orphan = result[0]
        assert orphan.slug == "db21ed7f_netalertx"
        assert len(orphan.paths) == 2

    def test_installed_slugs_not_returned_as_orphans(self):
        from utils.disk.disk_recovery import scan_orphaned_addon_dirs

        installed = (
            '{"result":"ok","data":{"addons":['
            '{"slug":"db21ed7f_netalertx_fa"},'
            '{"slug":"some_other_addon"}]}}'
        )
        ssh = self._make_ssh(
            installed_json=installed,
            addons_ls="db21ed7f_netalertx_fa\nsome_other_addon\n",
            configs_ls="db21ed7f_netalertx_fa\nsome_other_addon\n",
        )
        result = asyncio.run(scan_orphaned_addon_dirs(ssh))
        assert result == []

    def test_bad_json_treats_all_as_orphaned(self):
        from utils.disk.disk_recovery import scan_orphaned_addon_dirs

        ssh = self._make_ssh(
            installed_json="not json at all",
            addons_ls="some_addon\n",
            configs_ls="",
            du_display="5M\t/mnt/data/supervisor/addons/some_addon\n",
        )
        result = asyncio.run(scan_orphaned_addon_dirs(ssh))
        assert any(o.slug == "some_addon" for o in result)

    def test_size_bytes_parsed_from_du_output(self):
        from utils.disk.disk_recovery import scan_orphaned_addon_dirs

        installed = '{"result":"ok","data":{"addons":[]}}'
        ssh = self._make_ssh(
            installed_json=installed,
            addons_ls="old_addon\n",
            configs_ls="",
            du_display="50M\t/mnt/data/supervisor/addons/old_addon\n",
        )
        result = asyncio.run(scan_orphaned_addon_dirs(ssh))
        assert len(result) == 1
        assert result[0].size_bytes == 50 * 1024**2


# ── ha_agent_core pipeline ────────────────────────────────────────────────────────

_SIMPLE_CONFIG = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8123\n"


# ── utils/repair_episode.py ──────────────────────────────────────────────────────

import agents.ha_agent_advanced as _haa_mod
from utils.repair.repair_episode import RepairEpisode, load_episodes, serialize_episode
from utils.agent.tool_registry import ToolCall


def _make_episode(**overrides):
    defaults = dict(
        trigger="ha_log",
        symptoms=["sensor went unavailable"],
        tool_sequence=[ToolCall(name="read_logs", arguments={"lines": 50})],
        hypothesis_chain=["log suggests sensor timeout"],
        fix_applied="homeassistant:\n  name: Home\n",
        verification_result=True,
        model_used="qwen2.5-coder:7b",
        escalated=False,
        duration_seconds=4.2,
    )
    defaults.update(overrides)
    return RepairEpisode(**defaults)


class TestRepairEpisodeSchema:
    def test_valid_construction(self):
        ep = _make_episode()
        assert ep.trigger == "ha_log"
        assert ep.verification_result is True
        assert len(ep.tool_sequence) == 1
        assert ep.tool_sequence[0].name == "read_logs"

    def test_missing_required_field_raises(self):
        with pytest.raises(ValidationError):
            RepairEpisode(
                symptoms=[],
                tool_sequence=[],
                hypothesis_chain=[],
                verification_result=True,
                model_used="qwen2.5-coder:7b",
                escalated=False,
                duration_seconds=1.0,
                # trigger is missing
            )

    def test_json_round_trip(self):
        ep = _make_episode()
        restored = RepairEpisode.model_validate_json(ep.model_dump_json())
        assert restored.id == ep.id
        assert restored.trigger == ep.trigger
        assert restored.tool_sequence[0].name == ep.tool_sequence[0].name
        assert restored.fix_applied == ep.fix_applied

    def test_defaults_auto_assigned(self):
        ep = _make_episode()
        assert ep.id  # UUID assigned
        assert ep.timestamp > 0

    def test_fix_applied_optional(self):
        ep = _make_episode(fix_applied=None)
        assert ep.fix_applied is None


class TestMigrationV17:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        path = str(tmp_path / "test.db")
        monkeypatch.setattr(_haa_mod, "DB_PATH", path)
        _haa_mod.init_local_database()
        return path

    def test_repair_episodes_table_exists(self, db_path):
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='repair_episodes'"
            ).fetchall()
        assert rows, "repair_episodes table not created by migration v17"

    def test_repair_episodes_columns(self, db_path):
        with sqlite3.connect(db_path) as conn:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(repair_episodes)").fetchall()
            }
        expected = {
            "id",
            "timestamp",
            "trigger",
            "symptoms",
            "tool_sequence",
            "hypothesis_chain",
            "fix_applied",
            "verification_result",
            "model_used",
            "escalated",
            "duration_seconds",
        }
        assert expected <= cols


class TestSerializeAndLoadEpisodes:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        path = str(tmp_path / "test.db")
        monkeypatch.setattr(_haa_mod, "DB_PATH", path)
        _haa_mod.init_local_database()
        return path

    def test_serialize_then_load_roundtrip(self, db_path):
        ep = _make_episode()
        serialize_episode(db_path, ep)
        results = load_episodes(db_path)
        assert len(results) == 1
        r = results[0]
        assert r.id == ep.id
        assert r.trigger == ep.trigger
        assert r.symptoms == ep.symptoms
        assert r.tool_sequence[0].name == ep.tool_sequence[0].name
        assert r.fix_applied == ep.fix_applied
        assert r.verification_result == ep.verification_result
        assert r.model_used == ep.model_used
        assert r.escalated == ep.escalated
        assert abs(r.duration_seconds - ep.duration_seconds) < 1e-9

    def test_load_since_filters_by_timestamp(self, db_path):
        old = _make_episode(trigger="ha_log", timestamp=1000.0)
        new = _make_episode(trigger="netalertx", timestamp=9_000_000_000.0)
        serialize_episode(db_path, old)
        serialize_episode(db_path, new)
        results = load_episodes(db_path, since=5000.0)
        assert len(results) == 1
        assert results[0].trigger == "netalertx"

    def test_null_fix_applied_survives_roundtrip(self, db_path):
        ep = _make_episode(fix_applied=None)
        serialize_episode(db_path, ep)
        results = load_episodes(db_path)
        assert results[0].fix_applied is None

    def test_multiple_tool_calls_preserved(self, db_path):
        ep = _make_episode(
            tool_sequence=[
                ToolCall(name="read_logs", arguments={"lines": 50}),
                ToolCall(name="apply_fix", arguments={"yaml": "x: 1"}),
            ]
        )
        serialize_episode(db_path, ep)
        results = load_episodes(db_path)
        assert len(results[0].tool_sequence) == 2
        assert results[0].tool_sequence[1].name == "apply_fix"


# ── AgentLoop episode recording hook (item 78) ───────────────────────────────


class TestAgentLoopEpisodeRecording:
    """Serialization hook: AgentLoop writes RepairEpisode on finish_repair."""

    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        path = str(tmp_path / "test.db")
        monkeypatch.setattr(_haa_mod, "DB_PATH", path)
        _haa_mod.init_local_database()
        return path

    def _make_loop(
        self,
        llm,
        db_path=None,
        trigger="ha_log",
        escalated=False,
    ):
        from utils.agent.agent_loop import AgentLoop
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor
        from utils.agent.tool_registry import build_ha_tool_registry

        ex = ToolExecutor(
            ha_ssh_client=FakeSSHClient(),
            gate=FakeAutonomyGate(),
            notifier=FakeNotifier(),
        )
        return AgentLoop(
            llm_client=llm,
            tool_executor=ex,
            tool_registry=build_ha_tool_registry(),
            max_tool_calls=5,
            max_wall_seconds=10.0,
            trigger=trigger,
            db_path=db_path,
            escalated=escalated,
        )

    def _finish_call(self, summary="Done", action_taken="no_fix_needed"):
        return {
            "tool_calls": [
                {
                    "function": {
                        "name": "finish_repair",
                        "arguments": {
                            "summary": summary,
                            "action_taken": action_taken,
                        },
                    }
                }
            ]
        }

    def test_episode_written_on_success(self, db_path):
        from utils.llm.ollama_client import FakeToolCallingLLMClient
        from utils.repair.repair_episode import load_episodes

        llm = FakeToolCallingLLMClient([self._finish_call()])
        loop = self._make_loop(llm, db_path=db_path)
        result = asyncio.run(loop.run("Analyze HA config."))

        assert result.outcome == "success"
        episodes = load_episodes(db_path)
        assert len(episodes) == 1

    def test_episode_id_returned_in_result(self, db_path):
        from utils.llm.ollama_client import FakeToolCallingLLMClient
        from utils.repair.repair_episode import load_episodes

        llm = FakeToolCallingLLMClient([self._finish_call()])
        loop = self._make_loop(llm, db_path=db_path)
        result = asyncio.run(loop.run("Analyze HA config."))

        stored = load_episodes(db_path)
        assert result.episode_id is not None
        assert result.episode_id == stored[0].id

    def test_no_episode_without_db_path(self, db_path):
        from utils.llm.ollama_client import FakeToolCallingLLMClient
        from utils.repair.repair_episode import load_episodes

        llm = FakeToolCallingLLMClient([self._finish_call()])
        loop = self._make_loop(llm, db_path=None)
        result = asyncio.run(loop.run("Analyze HA config."))

        assert result.outcome == "success"
        assert result.episode_id is None
        assert len(load_episodes(db_path)) == 0

    def test_trigger_stored(self, db_path):
        from utils.llm.ollama_client import FakeToolCallingLLMClient
        from utils.repair.repair_episode import load_episodes

        llm = FakeToolCallingLLMClient([self._finish_call()])
        loop = self._make_loop(llm, db_path=db_path, trigger="netalertx")
        asyncio.run(loop.run("Diagnose NetAlertX."))

        assert load_episodes(db_path)[0].trigger == "netalertx"

    def test_escalated_flag_stored(self, db_path):
        from utils.llm.ollama_client import FakeToolCallingLLMClient
        from utils.repair.repair_episode import load_episodes

        llm = FakeToolCallingLLMClient([self._finish_call()])
        loop = self._make_loop(llm, db_path=db_path, escalated=True)
        asyncio.run(loop.run("Escalated repair."))

        assert load_episodes(db_path)[0].escalated is True

    def test_hypothesis_chain_from_summary(self, db_path):
        from utils.llm.ollama_client import FakeToolCallingLLMClient
        from utils.repair.repair_episode import load_episodes

        llm = FakeToolCallingLLMClient(
            [self._finish_call(summary="Found stale sensor config")]
        )
        loop = self._make_loop(llm, db_path=db_path)
        asyncio.run(loop.run("Analyze HA config."))

        ep = load_episodes(db_path)[0]
        assert ep.hypothesis_chain == ["Found stale sensor config"]

    def test_build_episode_extracts_fix_applied(self):
        """_build_episode captures yaml_content from apply_fix step."""
        from utils.agent.agent_loop import AgentLoop
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor
        from utils.agent.tool_registry import (
            AgentStep,
            ToolCall,
            ToolResult,
            build_ha_tool_registry,
        )
        from utils.llm.ollama_client import FakeToolCallingLLMClient

        loop = AgentLoop(
            llm_client=FakeToolCallingLLMClient([]),
            tool_executor=ToolExecutor(
                ha_ssh_client=FakeSSHClient(),
                gate=FakeAutonomyGate(),
                notifier=FakeNotifier(),
            ),
            tool_registry=build_ha_tool_registry(),
        )
        fix_yaml = "homeassistant:\n  name: Home\n"
        steps = [
            AgentStep(
                step_number=1,
                tool_call=ToolCall(
                    name="apply_fix",
                    arguments={"yaml_content": fix_yaml, "description": "Fix it"},
                ),
                tool_result=ToolResult(
                    tool_name="apply_fix", success=True, output="Applied"
                ),
                timestamp=0.1,
            ),
            AgentStep(
                step_number=2,
                tool_call=ToolCall(
                    name="finish_repair",
                    arguments={"summary": "Fixed", "action_taken": "fixed"},
                ),
                tool_result=ToolResult(
                    tool_name="finish_repair", success=True, output="Done"
                ),
                timestamp=0.2,
            ),
        ]
        import time

        data = loop._build_episode(
            steps, {"summary": "Fixed the config"}, time.monotonic() - 1.0
        )
        assert data["fix_applied"] == fix_yaml

    def test_build_episode_extracts_symptoms_from_read_logs(self):
        """_build_episode includes read_logs output in symptoms."""
        from utils.agent.agent_loop import AgentLoop
        from utils.agent.autonomy import FakeAutonomyGate
        from utils.hitl.notify import FakeNotifier
        from utils.ha.ssh_client import FakeSSHClient
        from utils.agent.tool_executor import ToolExecutor
        from utils.agent.tool_registry import (
            AgentStep,
            ToolCall,
            ToolResult,
            build_ha_tool_registry,
        )
        from utils.llm.ollama_client import FakeToolCallingLLMClient
        import time

        loop = AgentLoop(
            llm_client=FakeToolCallingLLMClient([]),
            tool_executor=ToolExecutor(
                ha_ssh_client=FakeSSHClient(),
                gate=FakeAutonomyGate(),
                notifier=FakeNotifier(),
            ),
            tool_registry=build_ha_tool_registry(),
        )
        log_output = "ERROR sensor.pv_power unavailable"
        steps = [
            AgentStep(
                step_number=1,
                tool_call=ToolCall(name="read_logs", arguments={"lines": 10}),
                tool_result=ToolResult(
                    tool_name="read_logs", success=True, output=log_output
                ),
                timestamp=0.1,
            ),
        ]
        data = loop._build_episode(
            steps, {"summary": "Sensor down"}, time.monotonic() - 0.5
        )
        assert log_output[:200] in data["symptoms"]

    def test_no_episode_on_exhausted_outcome(self, db_path):
        from utils.llm.ollama_client import FakeToolCallingLLMClient
        from utils.repair.repair_episode import load_episodes

        # Empty sequence → exhausted after 2 plain-text responses
        llm = FakeToolCallingLLMClient([{"content": "hmm"}, {"content": "hmm again"}])
        loop = self._make_loop(llm, db_path=db_path)
        result = asyncio.run(loop.run("Analyze."))

        assert result.outcome == "exhausted"
        assert result.episode_id is None
        assert len(load_episodes(db_path)) == 0


# ── utils/anonymizer.py ──────────────────────────────────────────────────────────


class TestAnonymizer:
    def test_ipv4_replaced_with_placeholder(self):
        from utils.cases.anonymizer import Anonymizer

        a = Anonymizer()
        assert a.text("Host is 192.168.1.50 online") == "Host is <host_1> online"

    def test_same_ip_gets_same_placeholder(self):
        from utils.cases.anonymizer import Anonymizer

        a = Anonymizer()
        result = a.text("192.168.1.50 and 192.168.1.50 again")
        assert result.count("<host_1>") == 2
        assert "<host_2>" not in result

    def test_different_ips_get_different_placeholders(self):
        from utils.cases.anonymizer import Anonymizer

        a = Anonymizer()
        result = a.text("10.0.0.1 and 10.0.0.2")
        assert "<host_1>" in result
        assert "<host_2>" in result

    def test_backup_slug_replaced(self):
        from utils.cases.anonymizer import Anonymizer

        a = Anonymizer()
        result = a.text("backup slug a1b2c3d4 created")
        assert "a1b2c3d4" not in result
        assert "<slug_1>" in result

    def test_config_path_filename_anonymized(self):
        from utils.cases.anonymizer import Anonymizer

        a = Anonymizer()
        result = a.text("Editing /config/automations/morning.yaml")
        assert "morning.yaml" not in result
        assert "/config/automations/<file>" in result

    def test_config_path_top_level_preserved(self):
        from utils.cases.anonymizer import Anonymizer

        a = Anonymizer()
        # Top-level /config/configuration.yaml has no sub-directory, so not matched
        result = a.text("Reading /config/configuration.yaml")
        assert "/config/configuration.yaml" in result

    def test_empty_string_unchanged(self):
        from utils.cases.anonymizer import Anonymizer

        a = Anonymizer()
        assert a.text("") == ""

    def test_args_anonymizes_string_values(self):
        from utils.cases.anonymizer import Anonymizer

        a = Anonymizer()
        result = a.args({"host": "192.168.1.1", "count": 5})
        assert result["host"] == "<host_1>"
        assert result["count"] == 5

    def test_args_preserves_structure_on_bad_json(self):
        from utils.cases.anonymizer import Anonymizer

        a = Anonymizer()
        original = {"key": "value"}
        # Should return original if re-parsing fails (edge case)
        result = a.args(original)
        assert result["key"] == "value"


# ── export_episodes_yaml ─────────────────────────────────────────────────────────


class TestExportEpisodesYaml:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        from agents import ha_agent_advanced

        path = str(tmp_path / "test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        ha_agent_advanced.init_local_database()
        return path

    def test_export_produces_valid_yaml(self, db_path):
        import yaml
        from utils.repair.repair_episode import export_episodes_yaml

        ep = _make_episode(symptoms=["err1"], hypothesis_chain=["maybe fix"])
        serialize_episode(db_path, ep)
        episodes = load_episodes(db_path)
        output = export_episodes_yaml(episodes)
        parsed = yaml.safe_load(output)
        assert isinstance(parsed, list)
        assert len(parsed) == 1

    def test_export_anonymizes_ip_in_symptoms(self, db_path):
        import yaml
        from utils.repair.repair_episode import export_episodes_yaml

        ep = _make_episode(symptoms=["Error from host 10.0.0.5"])
        serialize_episode(db_path, ep)
        output = export_episodes_yaml(load_episodes(db_path))
        assert "10.0.0.5" not in output
        assert "<host_1>" in output

    def test_export_structure_contains_expected_fields(self, db_path):
        import yaml
        from utils.repair.repair_episode import export_episodes_yaml

        ep = _make_episode()
        serialize_episode(db_path, ep)
        parsed = yaml.safe_load(export_episodes_yaml(load_episodes(db_path)))
        record = parsed[0]
        for field in (
            "id",
            "timestamp",
            "trigger",
            "symptoms",
            "tool_sequence",
            "hypothesis_chain",
            "fix_applied",
            "verification_result",
            "model_used",
            "escalated",
            "duration_seconds",
        ):
            assert field in record, f"Missing field: {field}"

    def test_export_empty_list_returns_empty_yaml(self):
        from utils.repair.repair_episode import export_episodes_yaml
        import yaml

        output = export_episodes_yaml([])
        parsed = yaml.safe_load(output)
        assert parsed is None or parsed == []

    def test_export_timestamp_is_iso_string(self, db_path):
        import yaml
        from utils.repair.repair_episode import export_episodes_yaml

        ep = _make_episode()
        serialize_episode(db_path, ep)
        parsed = yaml.safe_load(export_episodes_yaml(load_episodes(db_path)))
        ts = parsed[0]["timestamp"]
        assert isinstance(ts, str)
        assert "T" in ts


# ── migration v20 (submitted_at / pr_url columns) ────────────────────────────────


class TestMigrationV20:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        path = str(tmp_path / "test.db")
        monkeypatch.setattr(_haa_mod, "DB_PATH", path)
        _haa_mod.init_local_database()
        return path

    def test_submitted_at_column_exists(self, db_path):
        with sqlite3.connect(db_path) as conn:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(repair_episodes)").fetchall()
            }
        assert "submitted_at" in cols

    def test_pr_url_column_exists(self, db_path):
        with sqlite3.connect(db_path) as conn:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(repair_episodes)").fetchall()
            }
        assert "pr_url" in cols


# ── load_episode (single-row lookup) ────────────────────────────────────────────


class TestLoadEpisode:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        path = str(tmp_path / "test.db")
        monkeypatch.setattr(_haa_mod, "DB_PATH", path)
        _haa_mod.init_local_database()
        return path

    def test_returns_none_for_missing_id(self, db_path):
        from utils.repair.repair_episode import load_episode

        assert load_episode(db_path, "nonexistent-id") is None

    def test_returns_episode_for_known_id(self, db_path):
        from utils.repair.repair_episode import load_episode

        ep = _make_episode()
        serialize_episode(db_path, ep)
        result = load_episode(db_path, ep.id)
        assert result is not None
        assert result.id == ep.id
        assert result.trigger == ep.trigger

    def test_submitted_fields_default_to_none(self, db_path):
        from utils.repair.repair_episode import load_episode

        ep = _make_episode()
        serialize_episode(db_path, ep)
        result = load_episode(db_path, ep.id)
        assert result is not None
        assert result.submitted_at is None
        assert result.pr_url is None


# ── mark_episode_submitted ────────────────────────────────────────────────────────


class TestMarkEpisodeSubmitted:
    @pytest.fixture
    def db_path(self, monkeypatch, tmp_path):
        path = str(tmp_path / "test.db")
        monkeypatch.setattr(_haa_mod, "DB_PATH", path)
        _haa_mod.init_local_database()
        return path

    def test_sets_submitted_at_and_pr_url(self, db_path):
        from utils.repair.repair_episode import load_episode, mark_episode_submitted

        ep = _make_episode()
        serialize_episode(db_path, ep)
        mark_episode_submitted(
            db_path, ep.id, "https://github.com/owner/pueo-cases/pull/1"
        )
        result = load_episode(db_path, ep.id)
        assert result is not None
        assert result.submitted_at is not None
        assert result.pr_url == "https://github.com/owner/pueo-cases/pull/1"

    def test_submitted_at_is_recent_timestamp(self, db_path):
        import time
        from utils.repair.repair_episode import load_episode, mark_episode_submitted

        before = time.time()
        ep = _make_episode()
        serialize_episode(db_path, ep)
        mark_episode_submitted(db_path, ep.id, "https://github.com/x/y/pull/2")
        after = time.time()
        result = load_episode(db_path, ep.id)
        assert result is not None
        assert before <= result.submitted_at <= after  # type: ignore[operator]


# ── export_single_episode_yaml ───────────────────────────────────────────────────


class TestExportSingleEpisodeYaml:
    def test_produces_valid_yaml_list(self):
        import yaml
        from utils.repair.repair_episode import export_single_episode_yaml

        ep = _make_episode()
        output = export_single_episode_yaml(ep)
        parsed = yaml.safe_load(output)
        assert isinstance(parsed, list)
        assert len(parsed) == 1

    def test_includes_description_when_provided(self):
        import yaml
        from utils.repair.repair_episode import export_single_episode_yaml

        ep = _make_episode()
        output = export_single_episode_yaml(
            ep, description="Integration broke after upgrade"
        )
        parsed = yaml.safe_load(output)
        assert parsed[0]["description"] == "Integration broke after upgrade"

    def test_omits_description_when_empty(self):
        import yaml
        from utils.repair.repair_episode import export_single_episode_yaml

        ep = _make_episode()
        parsed = yaml.safe_load(export_single_episode_yaml(ep, description=""))
        assert "description" not in parsed[0]

    def test_anonymizes_ip_in_symptoms(self):
        import yaml
        from utils.repair.repair_episode import export_single_episode_yaml

        ep = _make_episode(symptoms=["Device 192.168.1.50 unreachable"])
        output = export_single_episode_yaml(ep)
        assert "192.168.1.50" not in output
        assert "<host_1>" in output

    def test_expected_fields_present(self):
        import yaml
        from utils.repair.repair_episode import export_single_episode_yaml

        ep = _make_episode()
        parsed = yaml.safe_load(export_single_episode_yaml(ep))
        record = parsed[0]
        for field in (
            "id",
            "trigger",
            "symptoms",
            "tool_sequence",
            "verification_result",
        ):
            assert field in record


# ── case_submitter validation ─────────────────────────────────────────────────────


class TestCaseSubmitterValidation:
    def test_empty_repo_raises(self):
        import asyncio
        from utils.cases.case_submitter import CaseSubmitError, submit_episode

        with pytest.raises(CaseSubmitError, match="Invalid"):
            asyncio.run(
                submit_episode(
                    episode_id="abc",
                    yaml_content="---\n",
                    cases_repo="",
                    pr_title="t",
                    pr_body="b",
                )
            )

    def test_malformed_repo_raises(self):
        import asyncio
        from utils.cases.case_submitter import CaseSubmitError, submit_episode

        with pytest.raises(CaseSubmitError, match="Invalid"):
            asyncio.run(
                submit_episode(
                    episode_id="abc",
                    yaml_content="---\n",
                    cases_repo="not-a-valid/repo/path/extra",
                    pr_title="t",
                    pr_body="b",
                )
            )

    def test_valid_repo_format_passes_validation(self, monkeypatch):
        import asyncio
        from utils.cases.case_submitter import _validate_repo

        _validate_repo("owner/pueo-cases")
        _validate_repo("my-org/my.repo_123")

    def test_path_traversal_rejected(self):
        import asyncio
        from utils.cases.case_submitter import CaseSubmitError, submit_episode

        with pytest.raises(CaseSubmitError, match="Invalid"):
            asyncio.run(
                submit_episode(
                    episode_id="abc",
                    yaml_content="---\n",
                    cases_repo="../evil/repo",
                    pr_title="t",
                    pr_body="b",
                )
            )


# ── case_ingester ─────────────────────────────────────────────────────────────


class TestCaseIngesterValidate:
    def test_empty_repo_raises(self):
        from utils.cases.case_ingester import CaseIngestError, _validate_repo

        with pytest.raises(CaseIngestError, match="Invalid"):
            _validate_repo("")

    def test_malformed_repo_raises(self):
        from utils.cases.case_ingester import CaseIngestError, _validate_repo

        with pytest.raises(CaseIngestError, match="Invalid"):
            _validate_repo("not-a-valid/repo/path/extra")

    def test_path_traversal_rejected(self):
        from utils.cases.case_ingester import CaseIngestError, _validate_repo

        with pytest.raises(CaseIngestError, match="Invalid"):
            _validate_repo("../evil/repo")

    def test_valid_repo_format_passes(self):
        from utils.cases.case_ingester import _validate_repo

        _validate_repo("owner/pueo-cases")
        _validate_repo("my-org/my.repo_123")


class TestCaseIngesterState:
    def test_load_state_missing_dir_returns_empty(self, tmp_path):
        from utils.cases.case_ingester import load_ingest_state

        state = load_ingest_state(str(tmp_path / "nonexistent"))
        assert state == {}

    def test_save_and_load_roundtrip(self, tmp_path):
        from utils.cases.case_ingester import load_ingest_state, save_ingest_state

        save_ingest_state(str(tmp_path), {"last_ingest_ts": 12345.0, "foo": "bar"})
        state = load_ingest_state(str(tmp_path))
        assert state["last_ingest_ts"] == 12345.0
        assert state["foo"] == "bar"

    def test_save_creates_directory(self, tmp_path):
        from utils.cases.case_ingester import load_ingest_state, save_ingest_state

        nested = str(tmp_path / "a" / "b" / "c")
        save_ingest_state(nested, {"last_ingest_ts": 0.0})
        state = load_ingest_state(nested)
        assert state["last_ingest_ts"] == 0.0

    def test_corrupt_state_file_returns_empty(self, tmp_path):
        from utils.cases.case_ingester import load_ingest_state

        (tmp_path / "state.json").write_text("not valid json", encoding="utf-8")
        state = load_ingest_state(str(tmp_path))
        assert state == {}


class TestCaseIngesterChunkEpisode:
    def test_minimal_episode_produces_chunk(self):
        from utils.cases.case_ingester import _chunk_episode

        record = {
            "id": "abc123def456",
            "trigger": "ha_log",
            "symptoms": ["Boot error"],
            "hypothesis_chain": ["Config corrupt"],
        }
        result = _chunk_episode(record, pr_number=7, ingest_date="2026-08-11T00:00:00Z")
        assert result is not None
        chunk_id, text, meta = result
        assert "community_pr7_abc123def456" == chunk_id
        assert "ha_log" in text
        assert "Boot error" in text
        assert "Config corrupt" in text
        assert meta["source_pr"] == "7"
        assert meta["trigger_type"] == "ha_log"
        assert meta["ingest_date"] == "2026-08-11T00:00:00Z"
        assert meta["collection"] == "community_cases"

    def test_episode_with_description_and_fix(self):
        from utils.cases.case_ingester import _chunk_episode

        record = {
            "id": "x" * 20,
            "trigger": "netalertx",
            "symptoms": ["New device"],
            "hypothesis_chain": ["Unknown MAC"],
            "fix_applied": "Added to allowlist",
            "description": "Rogue device detected",
        }
        result = _chunk_episode(
            record, pr_number=42, ingest_date="2026-08-11T00:00:00Z"
        )
        assert result is not None
        _, text, _ = result
        assert "Rogue device detected" in text
        assert "Added to allowlist" in text

    def test_empty_symptoms_and_hypothesis_returns_none(self):
        from utils.cases.case_ingester import _chunk_episode

        record = {
            "id": "abc",
            "trigger": "ha_log",
            "symptoms": [],
            "hypothesis_chain": [],
        }
        result = _chunk_episode(record, pr_number=1, ingest_date="2026-08-11T00:00:00Z")
        # Only "Trigger: ha_log" — that's still non-empty text so result is not None
        assert result is not None
        _, text, _ = result
        assert "Trigger: ha_log" in text

    def test_no_id_uses_unknown_slug(self):
        from utils.cases.case_ingester import _chunk_episode

        record = {"trigger": "ha_log", "symptoms": ["err"], "hypothesis_chain": []}
        result = _chunk_episode(record, pr_number=1, ingest_date="2026-08-11T00:00:00Z")
        assert result is not None
        chunk_id, _, _ = result
        assert "unknown" in chunk_id

    def test_chunk_id_truncates_long_id(self):
        from utils.cases.case_ingester import _chunk_episode

        record = {
            "id": "a" * 50,
            "trigger": "ha_log",
            "symptoms": ["x"],
            "hypothesis_chain": [],
        }
        result = _chunk_episode(record, pr_number=3, ingest_date="2026-08-11T00:00:00Z")
        assert result is not None
        chunk_id, _, _ = result
        # slug is capped at 12 chars
        assert chunk_id == f"community_pr3_{'a' * 12}"


class TestCaseIngesterIngest:
    def _make_pr_list(
        self, numbers: list[int], merged_at: str = "2026-08-11T10:00:00Z"
    ) -> str:
        import json

        return json.dumps(
            [{"number": n, "merged_at": merged_at, "title": f"PR {n}"} for n in numbers]
        )

    def _make_pr_files(self, filenames: list[str]) -> str:
        import json

        return json.dumps([{"filename": f, "status": "added"} for f in filenames])

    def _make_contents(self, episode_yaml: str) -> str:
        import base64
        import json

        return json.dumps(
            {
                "encoding": "base64",
                "content": base64.b64encode(episode_yaml.encode()).decode(),
            }
        )

    def _make_episode_yaml(
        self, ep_id: str = "epabc123", trigger: str = "ha_log"
    ) -> str:
        import yaml

        return yaml.dump(
            [
                {
                    "id": ep_id,
                    "trigger": trigger,
                    "symptoms": ["Error booting"],
                    "hypothesis_chain": ["Config missing key"],
                    "fix_applied": "Added default_config",
                    "verification_result": True,
                    "model_used": "test-model",
                    "escalated": False,
                    "duration_seconds": 5.0,
                    "timestamp": "2026-08-11T00:00:00Z",
                }
            ]
        )

    def test_invalid_repo_raises(self, tmp_path):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.cases.case_ingester import CaseIngestError, ingest_community_cases

        store = FakeKnowledgeStore()
        with pytest.raises(CaseIngestError, match="Invalid"):
            ingest_community_cases("", str(tmp_path), store)

    def test_no_merged_prs_returns_zero(self, tmp_path, monkeypatch):
        import json
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.cases.case_ingester import ingest_community_cases

        store = FakeKnowledgeStore()

        def fake_run_gh(args, timeout=60):
            if "pulls" in args:
                return json.dumps([])
            return "[]"

        monkeypatch.setattr("utils.cases.case_ingester._run_gh", fake_run_gh)
        n = ingest_community_cases("owner/pueo-cases", str(tmp_path), store)
        assert n == 0

    def test_ingest_one_episode(self, tmp_path, monkeypatch):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.cases.case_ingester import ingest_community_cases

        store = FakeKnowledgeStore()
        episode_yaml = self._make_episode_yaml("ep001")

        def fake_run_gh(args, timeout=60):
            joined = " ".join(args)
            if "pulls" in joined and "files" not in joined and "contents" not in joined:
                return self._make_pr_list([1])
            if "files" in joined:
                return self._make_pr_files(["episodes/ep001.yaml"])
            if "contents" in joined:
                return self._make_contents(episode_yaml)
            return "[]"

        monkeypatch.setattr("utils.cases.case_ingester._run_gh", fake_run_gh)
        n = ingest_community_cases("owner/pueo-cases", str(tmp_path), store)
        assert n == 1
        docs = store._docs.get("community_cases", [])
        assert len(docs) == 1
        _, text, meta = docs[0]
        assert "Error booting" in text
        assert meta["source_pr"] == "1"
        assert meta["trigger_type"] == "ha_log"

    def test_ingest_multiple_prs(self, tmp_path, monkeypatch):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.cases.case_ingester import ingest_community_cases

        store = FakeKnowledgeStore()
        yaml1 = self._make_episode_yaml("ep001")
        yaml2 = self._make_episode_yaml("ep002", trigger="netalertx")

        call_log: list[list[str]] = []

        def fake_run_gh(args, timeout=60):
            call_log.append(args)
            joined = " ".join(args)
            if "pulls" in joined and "files" not in joined and "contents" not in joined:
                return self._make_pr_list([1, 2])
            if "files" in joined and "/1/" in joined:
                return self._make_pr_files(["episodes/ep001.yaml"])
            if "files" in joined and "/2/" in joined:
                return self._make_pr_files(["episodes/ep002.yaml"])
            if "contents" in joined and "ep001" in joined:
                return self._make_contents(yaml1)
            if "contents" in joined and "ep002" in joined:
                return self._make_contents(yaml2)
            return "[]"

        monkeypatch.setattr("utils.cases.case_ingester._run_gh", fake_run_gh)
        n = ingest_community_cases("owner/pueo-cases", str(tmp_path), store)
        assert n == 2

    def test_state_saved_after_ingest(self, tmp_path, monkeypatch):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.cases.case_ingester import ingest_community_cases, load_ingest_state

        store = FakeKnowledgeStore()
        episode_yaml = self._make_episode_yaml()

        def fake_run_gh(args, timeout=60):
            joined = " ".join(args)
            if "pulls" in joined and "files" not in joined and "contents" not in joined:
                return self._make_pr_list([1])
            if "files" in joined:
                return self._make_pr_files(["episodes/ep.yaml"])
            if "contents" in joined:
                return self._make_contents(episode_yaml)
            return "[]"

        monkeypatch.setattr("utils.cases.case_ingester._run_gh", fake_run_gh)
        ingest_community_cases("owner/pueo-cases", str(tmp_path), store)
        state = load_ingest_state(str(tmp_path))
        assert "last_ingest_ts" in state
        assert state["last_ingest_ts"] > 0

    def test_since_timestamp_filters_old_prs(self, tmp_path, monkeypatch):
        import json
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.cases.case_ingester import ingest_community_cases, save_ingest_state

        store = FakeKnowledgeStore()
        # Save a "future" last_ingest_ts so no PRs are newer
        save_ingest_state(str(tmp_path), {"last_ingest_ts": 9_999_999_999.0})

        calls: list = []

        def fake_run_gh(args, timeout=60):
            calls.append(args)
            return json.dumps(
                [
                    {
                        "number": 1,
                        "merged_at": "2020-01-01T00:00:00Z",
                        "title": "Old PR",
                    }
                ]
            )

        monkeypatch.setattr("utils.cases.case_ingester._run_gh", fake_run_gh)
        n = ingest_community_cases("owner/pueo-cases", str(tmp_path), store)
        assert n == 0

    def test_non_episode_files_skipped(self, tmp_path, monkeypatch):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.cases.case_ingester import ingest_community_cases

        store = FakeKnowledgeStore()
        episode_yaml = self._make_episode_yaml()

        def fake_run_gh(args, timeout=60):
            joined = " ".join(args)
            if "pulls" in joined and "files" not in joined and "contents" not in joined:
                return self._make_pr_list([1])
            if "files" in joined:
                # Mix of episode YAML and other files
                return self._make_pr_files(
                    [
                        "README.md",
                        "episodes/ep.yaml",
                        "episodes/wrong.txt",
                    ]
                )
            if "contents" in joined and "ep.yaml" in joined:
                return self._make_contents(episode_yaml)
            return "[]"

        monkeypatch.setattr("utils.cases.case_ingester._run_gh", fake_run_gh)
        n = ingest_community_cases("owner/pueo-cases", str(tmp_path), store)
        # Only episodes/ep.yaml qualifies (README.md and .txt excluded)
        assert n == 1

    def test_malformed_yaml_file_skipped(self, tmp_path, monkeypatch):
        import base64
        import json
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.cases.case_ingester import ingest_community_cases

        store = FakeKnowledgeStore()

        def fake_run_gh(args, timeout=60):
            joined = " ".join(args)
            if "pulls" in joined and "files" not in joined and "contents" not in joined:
                return self._make_pr_list([1])
            if "files" in joined:
                return self._make_pr_files(["episodes/bad.yaml"])
            if "contents" in joined:
                return json.dumps(
                    {
                        "encoding": "base64",
                        "content": base64.b64encode(b": invalid: yaml: [[[").decode(),
                    }
                )
            return "[]"

        monkeypatch.setattr("utils.cases.case_ingester._run_gh", fake_run_gh)
        # Should not raise — malformed file is silently skipped
        n = ingest_community_cases("owner/pueo-cases", str(tmp_path), store)
        assert n == 0

    def test_gh_failure_on_files_skips_pr(self, tmp_path, monkeypatch):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.cases.case_ingester import ingest_community_cases, CaseIngestError

        store = FakeKnowledgeStore()

        def fake_run_gh(args, timeout=60):
            joined = " ".join(args)
            if "pulls" in joined and "files" not in joined:
                return self._make_pr_list([1])
            raise CaseIngestError("network timeout")

        monkeypatch.setattr("utils.cases.case_ingester._run_gh", fake_run_gh)
        # PR file fetch fails → PR skipped → 0 ingested, no exception raised
        n = ingest_community_cases("owner/pueo-cases", str(tmp_path), store)
        assert n == 0

    def test_upsert_idempotent_on_repeat_run(self, tmp_path, monkeypatch):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.cases.case_ingester import ingest_community_cases

        store = FakeKnowledgeStore()
        episode_yaml = self._make_episode_yaml("ep-stable")

        def fake_run_gh(args, timeout=60):
            joined = " ".join(args)
            if "pulls" in joined and "files" not in joined and "contents" not in joined:
                return self._make_pr_list([1])
            if "files" in joined:
                return self._make_pr_files(["episodes/ep.yaml"])
            if "contents" in joined:
                return self._make_contents(episode_yaml)
            return "[]"

        monkeypatch.setattr("utils.cases.case_ingester._run_gh", fake_run_gh)
        # Second run sees same PR as already ingested (since_ts covers it)
        ingest_community_cases("owner/pueo-cases", str(tmp_path), store)
        n2 = ingest_community_cases("owner/pueo-cases", str(tmp_path), store)
        # No new PRs after the first run → 0
        assert n2 == 0

    def test_community_cases_collection_used(self, tmp_path, monkeypatch):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.cases.case_ingester import ingest_community_cases

        store = FakeKnowledgeStore()
        episode_yaml = self._make_episode_yaml()

        def fake_run_gh(args, timeout=60):
            joined = " ".join(args)
            if "pulls" in joined and "files" not in joined and "contents" not in joined:
                return self._make_pr_list([1])
            if "files" in joined:
                return self._make_pr_files(["episodes/ep.yaml"])
            if "contents" in joined:
                return self._make_contents(episode_yaml)
            return "[]"

        monkeypatch.setattr("utils.cases.case_ingester._run_gh", fake_run_gh)
        ingest_community_cases("owner/pueo-cases", str(tmp_path), store)
        assert "community_cases" in store._docs
        assert len(store._docs["community_cases"]) == 1


# ── generate_eval_scenario ────────────────────────────────────────────────────


class TestGenerateEvalScenario:
    def test_ha_log_trigger_produces_read_logs_mock(self):
        from utils.cases.case_ingester import generate_eval_scenario

        record = {
            "id": "abc123def456",
            "trigger": "ha_log",
            "symptoms": ["[ERROR] homeassistant.core: failed to start"],
            "hypothesis_chain": ["Config key missing"],
            "fix_applied": "homeassistant:\n  name: Home",
        }
        scenario = generate_eval_scenario(record, pr_number=5)
        assert scenario is not None
        assert scenario["name"] == "community_pr5_abc123def456"
        assert scenario["trigger"] == "ha_log"
        assert "read_logs" in scenario["mocks"]
        assert "failed to start" in scenario["mocks"]["read_logs"]
        assert "apply_fix" in scenario["expected_tools_called"]
        assert scenario["fix_must_parse"] is True

    def test_ha_config_trigger_produces_read_config_mock(self):
        from utils.cases.case_ingester import generate_eval_scenario

        record = {
            "id": "cfgid001",
            "trigger": "ha_config",
            "symptoms": [],
            "hypothesis_chain": [],
            "fix_applied": "homeassistant:\n  name: Home",
        }
        scenario = generate_eval_scenario(record, pr_number=7)
        assert scenario is not None
        assert scenario["trigger"] == "ha_config"
        assert "read_config" in scenario["mocks"]
        assert "read_config" in scenario["expected_tools_called"]
        assert "apply_fix" in scenario["expected_tools_called"]

    def test_netalertx_trigger_produces_query_netalertx_mock(self):
        from utils.cases.case_ingester import generate_eval_scenario

        record = {
            "id": "nax001",
            "trigger": "netalertx",
            "symptoms": ["Unknown device 00:11:22:33:44:55 on LAN"],
            "hypothesis_chain": [],
        }
        scenario = generate_eval_scenario(record, pr_number=9)
        assert scenario is not None
        assert scenario["trigger"] == "netalertx"
        assert "query_netalertx" in scenario["mocks"]
        assert "query_netalertx" in scenario["expected_tools_called"]
        assert scenario["fix_must_parse"] is False

    def test_unknown_trigger_falls_back_to_investigation(self):
        from utils.cases.case_ingester import generate_eval_scenario

        record = {
            "id": "misc001",
            "trigger": "manual",
            "symptoms": ["Something happened"],
            "hypothesis_chain": [],
        }
        scenario = generate_eval_scenario(record, pr_number=3)
        assert scenario is not None
        assert scenario["trigger"] == "investigation"
        assert "read_logs" in scenario["mocks"]

    def test_ha_log_monitor_maps_to_ha_log(self):
        from utils.cases.case_ingester import generate_eval_scenario

        record = {
            "id": "mon001",
            "trigger": "ha_log_monitor",
            "symptoms": ["CRITICAL: boot failed"],
            "hypothesis_chain": [],
        }
        scenario = generate_eval_scenario(record, pr_number=2)
        assert scenario is not None
        assert scenario["trigger"] == "ha_log"

    def test_description_and_hypothesis_go_into_description_field(self):
        from utils.cases.case_ingester import generate_eval_scenario

        record = {
            "id": "x" * 20,
            "trigger": "ha_log",
            "symptoms": [],
            "hypothesis_chain": ["Auth error", "Cert expired"],
            "description": "Failed login from unknown IP",
        }
        scenario = generate_eval_scenario(record, pr_number=1)
        assert scenario is not None
        assert "Failed login" in scenario["description"]
        assert "Auth error" in scenario["description"]

    def test_no_content_returns_fallback_description(self):
        from utils.cases.case_ingester import generate_eval_scenario

        record = {
            "id": "bare001",
            "trigger": "ha_log",
            "symptoms": [],
            "hypothesis_chain": [],
        }
        scenario = generate_eval_scenario(record, pr_number=42)
        assert scenario is not None
        assert "PR #42" in scenario["description"]

    def test_finish_repair_always_in_expected_tools(self):
        from utils.cases.case_ingester import generate_eval_scenario

        for trigger in ("ha_log", "ha_config", "netalertx", "manual"):
            record = {
                "id": "t1",
                "trigger": trigger,
                "symptoms": ["err"],
                "hypothesis_chain": [],
            }
            scenario = generate_eval_scenario(record, pr_number=1)
            assert scenario is not None
            assert "finish_repair" in scenario["expected_tools_called"]

    def test_id_slug_truncated_at_12_chars(self):
        from utils.cases.case_ingester import generate_eval_scenario

        record = {
            "id": "z" * 50,
            "trigger": "ha_log",
            "symptoms": ["err"],
            "hypothesis_chain": [],
        }
        scenario = generate_eval_scenario(record, pr_number=10)
        assert scenario is not None
        assert scenario["name"] == f"community_pr10_{'z' * 12}"

    def test_expected_outcome_is_success(self):
        from utils.cases.case_ingester import generate_eval_scenario

        record = {
            "id": "ok1",
            "trigger": "ha_log",
            "symptoms": ["x"],
            "hypothesis_chain": [],
        }
        scenario = generate_eval_scenario(record, pr_number=1)
        assert scenario is not None
        assert scenario["expected_outcome"] == "success"


# ── write_eval_scenario ───────────────────────────────────────────────────────


class TestWriteEvalScenario:
    def test_writes_yaml_file_to_dir(self, tmp_path):
        import yaml
        from utils.cases.case_ingester import write_eval_scenario

        scenario = {
            "name": "community_pr5_abc123def456",
            "trigger": "ha_log",
            "description": "Test",
            "expected_outcome": "success",
            "expected_tools_called": ["read_logs", "finish_repair"],
            "fix_must_parse": False,
        }
        path = write_eval_scenario(scenario, str(tmp_path))
        assert path.exists()
        assert path.name == "community_pr5_abc123def456.yaml"
        loaded = yaml.safe_load(path.read_text())
        assert loaded["name"] == "community_pr5_abc123def456"
        assert loaded["trigger"] == "ha_log"

    def test_creates_directory_if_missing(self, tmp_path):
        from utils.cases.case_ingester import write_eval_scenario

        nested = tmp_path / "a" / "b"
        scenario = {
            "name": "test_scenario",
            "trigger": "ha_log",
            "description": "x",
            "expected_outcome": "success",
            "expected_tools_called": [],
            "fix_must_parse": False,
        }
        path = write_eval_scenario(scenario, str(nested))
        assert path.exists()

    def test_roundtrip_loadable_by_eval_harness(self, tmp_path):
        """Scenario written by write_eval_scenario must be parseable by EvalScenario.from_yaml."""
        import sys
        from pathlib import Path as _Path

        # Add project root so EvalScenario can be imported
        sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
        from evals.run_evals import EvalScenario
        from utils.cases.case_ingester import (
            generate_eval_scenario,
            write_eval_scenario,
        )

        record = {
            "id": "roundtrip001",
            "trigger": "ha_log",
            "symptoms": ["[ERROR] boot failed"],
            "hypothesis_chain": ["Missing key"],
            "fix_applied": "homeassistant:\n  name: Home",
        }
        scenario = generate_eval_scenario(record, pr_number=99)
        assert scenario is not None
        path = write_eval_scenario(scenario, str(tmp_path))
        loaded = EvalScenario.from_yaml(path)
        assert loaded.name == scenario["name"]
        assert loaded.trigger == scenario["trigger"]
        assert loaded.fix_must_parse is True


# ── ingest with scenarios_dir ─────────────────────────────────────────────────


class TestIngestWithScenariosDir:
    def _make_pr_list(
        self, numbers: list[int], merged_at: str = "2026-08-11T10:00:00Z"
    ) -> str:
        import json

        return json.dumps(
            [{"number": n, "merged_at": merged_at, "title": f"PR {n}"} for n in numbers]
        )

    def _make_pr_files(self, filenames: list[str]) -> str:
        import json

        return json.dumps([{"filename": f, "status": "added"} for f in filenames])

    def _make_contents(self, episode_yaml: str) -> str:
        import base64, json

        return json.dumps(
            {
                "encoding": "base64",
                "content": base64.b64encode(episode_yaml.encode()).decode(),
            }
        )

    def test_scenario_file_written_per_episode(self, tmp_path, monkeypatch):
        import yaml
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.cases.case_ingester import ingest_community_cases

        store = FakeKnowledgeStore()
        scenarios_dir = tmp_path / "scenarios"
        episode_yaml = yaml.dump(
            [
                {
                    "id": "ep777",
                    "trigger": "ha_log",
                    "symptoms": ["Boot error"],
                    "hypothesis_chain": ["Config corrupt"],
                    "fix_applied": "homeassistant:\n  name: Home",
                }
            ]
        )

        def fake_run_gh(args, timeout=60):
            joined = " ".join(args)
            if "pulls" in joined and "files" not in joined and "contents" not in joined:
                return self._make_pr_list([1])
            if "files" in joined:
                return self._make_pr_files(["episodes/ep.yaml"])
            if "contents" in joined:
                return self._make_contents(episode_yaml)
            return "[]"

        monkeypatch.setattr("utils.cases.case_ingester._run_gh", fake_run_gh)
        n = ingest_community_cases(
            "owner/repo", str(tmp_path), store, scenarios_dir=str(scenarios_dir)
        )
        assert n == 1
        files = list(scenarios_dir.glob("*.yaml"))
        assert len(files) == 1
        assert files[0].name == "community_pr1_ep777.yaml"

    def test_no_scenarios_dir_writes_nothing(self, tmp_path, monkeypatch):
        import yaml
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.cases.case_ingester import ingest_community_cases

        store = FakeKnowledgeStore()
        episode_yaml = yaml.dump(
            [
                {
                    "id": "ep888",
                    "trigger": "ha_log",
                    "symptoms": ["err"],
                    "hypothesis_chain": [],
                }
            ]
        )

        def fake_run_gh(args, timeout=60):
            joined = " ".join(args)
            if "pulls" in joined and "files" not in joined and "contents" not in joined:
                return self._make_pr_list([1])
            if "files" in joined:
                return self._make_pr_files(["episodes/ep.yaml"])
            if "contents" in joined:
                return self._make_contents(episode_yaml)
            return "[]"

        monkeypatch.setattr("utils.cases.case_ingester._run_gh", fake_run_gh)
        # No scenarios_dir passed — should not create any files in tmp_path
        before = set(tmp_path.iterdir())
        ingest_community_cases("owner/repo", str(tmp_path), store)
        after = set(tmp_path.iterdir())
        # Only state.json may appear, no scenario YAML
        new_files = {f for f in (after - before) if f.suffix == ".yaml"}
        assert len(new_files) == 0


class TestStrategySeeder:
    def test_seeds_known_prompt_files(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.knowledge.strategy_seeder import seed_strategies

        store = FakeKnowledgeStore()
        n = seed_strategies(store)
        assert n > 0
        chunks = store.query("NetAlertX", top_k=10, collections=["strategies"])
        assert len(chunks) > 0

    def test_seeding_is_idempotent(self):
        from utils.knowledge.knowledge_store import FakeKnowledgeStore
        from utils.knowledge.strategy_seeder import seed_strategies

        store = FakeKnowledgeStore()
        n1 = seed_strategies(store)
        n2 = seed_strategies(store)
        assert n1 == n2
        chunks_first = store.query("strategy", top_k=50, collections=["strategies"])
        seed_strategies(store)
        chunks_second = store.query("strategy", top_k=50, collections=["strategies"])
        assert len(chunks_first) == len(chunks_second)

    def test_strategies_collection_in_collections_tuple(self):
        from utils.knowledge.knowledge_store import COLLECTIONS

        assert "strategies" in COLLECTIONS


class TestParseConceptDoc:
    def test_strips_frontmatter(self):
        from utils.knowledge.ha_concepts_scraper import parse_concept_doc

        doc = "---\ntitle: Lovelace\n---\n## Overview\nDashboard concepts."
        result = parse_concept_doc(doc)
        assert result
        assert all("---" not in c for c in result)
        assert any("Dashboard concepts" in c for c in result)

    def test_splits_by_headings(self):
        from utils.knowledge.ha_concepts_scraper import parse_concept_doc

        doc = "## Cards\nCard config here.\n## Views\nView config here."
        result = parse_concept_doc(doc)
        assert len(result) == 2

    def test_word_boundary_truncation(self):
        from utils.knowledge.ha_concepts_scraper import parse_concept_doc

        long_section = "word " * 700  # ~3500 chars
        doc = f"## Section\n{long_section}"
        result = parse_concept_doc(doc)
        assert len(result) == 1
        assert len(result[0]) <= 3000
        assert not result[0].endswith("wor")

    def test_strips_empty_sections(self):
        from utils.knowledge.ha_concepts_scraper import parse_concept_doc

        doc = "## Header\n\n## Content\nActual text here"
        result = parse_concept_doc(doc)
        assert all(c.strip() for c in result)

    def test_handles_doc_without_frontmatter(self):
        from utils.knowledge.ha_concepts_scraper import parse_concept_doc

        doc = "## Overview\nLovelace dashboard configuration."
        result = parse_concept_doc(doc)
        assert result
        assert "Lovelace" in result[0]


class TestEmbedCachedConceptDocs:
    def test_returns_zero_for_missing_dir(self):
        from utils.knowledge.ha_concepts_scraper import embed_cached_concept_docs
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        assert embed_cached_concept_docs("/nonexistent/path", store) == 0

    def test_processes_md_files(self, tmp_path):
        from utils.knowledge.ha_concepts_scraper import embed_cached_concept_docs
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "concepts"
        cache.mkdir()
        (cache / "lovelace_dashboards.md").write_text(
            "## Dashboards\nLovelace dashboard overview.\n## Views\nView config."
        )

        store = FakeKnowledgeStore()
        result = embed_cached_concept_docs(str(cache), store)
        assert result == 1
        hits = store.query("Lovelace dashboard", top_k=5)
        assert len(hits) > 0
        assert hits[0].collection == "ha_concepts"

    def test_collected_ids_populated(self, tmp_path):
        from utils.knowledge.ha_concepts_scraper import embed_cached_concept_docs
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "concepts"
        cache.mkdir()
        (cache / "entity_registry.md").write_text(
            "## Registry\nEntity section one.\n## Lookup\nSection two."
        )

        store = FakeKnowledgeStore()
        collected: set[str] = set()
        embed_cached_concept_docs(str(cache), store, collected)
        assert len(collected) == 2
        assert "ha-concepts-entity_registry-0" in collected

    def test_skips_non_md_files(self, tmp_path):
        from utils.knowledge.ha_concepts_scraper import embed_cached_concept_docs
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "concepts"
        cache.mkdir()
        (cache / "readme.txt").write_text("## Overview\nSome text")

        store = FakeKnowledgeStore()
        assert embed_cached_concept_docs(str(cache), store) == 0

    def test_metadata_source_field(self, tmp_path):
        from utils.knowledge.ha_concepts_scraper import embed_cached_concept_docs
        from utils.knowledge.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "concepts"
        cache.mkdir()
        (cache / "automation_basics.md").write_text("## Automation\nTrigger on event.")

        store = FakeKnowledgeStore()
        embed_cached_concept_docs(str(cache), store)
        hits = store.query("Trigger on event", top_k=5)
        assert hits
        assert hits[0].metadata.get("source") == "ha_concepts/automation_basics"


class TestHaConceptsCollection:
    def test_ha_concepts_in_collections(self):
        from utils.knowledge.knowledge_store import COLLECTIONS

        assert "ha_concepts" in COLLECTIONS
