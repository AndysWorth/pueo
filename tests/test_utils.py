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
        from utils.retry import async_retry

        @async_retry(exceptions=(OSError,))
        async def always_ok():
            return 42

        assert asyncio.run(always_ok()) == 42

    def test_retries_on_matching_exception_then_succeeds(self):
        from utils.retry import async_retry

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
        from utils.retry import async_retry

        calls = []

        @async_retry(max_attempts=5, base_delay=0.0, exceptions=(OSError,))
        async def bad():
            calls.append(1)
            raise ValueError("not retryable")

        with pytest.raises(ValueError):
            asyncio.run(bad())
        assert len(calls) == 1

    def test_exhausts_max_attempts_and_raises(self):
        from utils.retry import async_retry

        calls = []

        @async_retry(max_attempts=3, base_delay=0.0, exceptions=(OSError,))
        async def always_fail():
            calls.append(1)
            raise OSError("persistent")

        with pytest.raises(OSError):
            asyncio.run(always_fail())
        assert len(calls) == 3

    def test_zero_max_attempts_retries_past_default(self):
        from utils.retry import async_retry

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
        from utils.retry import async_retry

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
        import utils.retry as retry_mod

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
        from utils.rate_limiter import Debouncer

        d = Debouncer(window_seconds=30)
        assert d.record() is True

    def test_second_call_within_window_suppressed(self, monkeypatch):
        from utils.rate_limiter import Debouncer
        import time as time_mod

        now = time_mod.monotonic()
        monkeypatch.setattr("utils.rate_limiter.time.monotonic", lambda: now)
        d = Debouncer(window_seconds=30)
        d.record()
        assert d.record() is False

    def test_call_after_window_triggers_again(self, monkeypatch):
        from utils.rate_limiter import Debouncer
        import time as time_mod

        clock = [time_mod.monotonic()]
        monkeypatch.setattr("utils.rate_limiter.time.monotonic", lambda: clock[0])
        d = Debouncer(window_seconds=30)
        d.record()

        clock[0] += 31
        assert d.record() is True

    def test_burst_of_50_produces_one_trigger(self, monkeypatch):
        from utils.rate_limiter import Debouncer
        import time as time_mod

        now = time_mod.monotonic()
        monkeypatch.setattr("utils.rate_limiter.time.monotonic", lambda: now)
        d = Debouncer(window_seconds=30)
        results = [d.record() for _ in range(50)]
        assert results.count(True) == 1
        assert results[0] is True


class TestRateLimiter:
    def test_allows_calls_under_limit(self):
        from utils.rate_limiter import RateLimiter

        rl = RateLimiter(max_calls=5, period_seconds=60)
        for _ in range(5):
            rl.check()

    def test_raises_at_limit(self):
        from utils.rate_limiter import RateLimiter, RateLimitExceeded

        rl = RateLimiter(max_calls=3, period_seconds=60)
        for _ in range(3):
            rl.check()
        with pytest.raises(RateLimitExceeded):
            rl.check()

    def test_allows_again_after_period(self, monkeypatch):
        from utils.rate_limiter import RateLimiter
        import time as time_mod

        clock = [time_mod.monotonic()]
        monkeypatch.setattr("utils.rate_limiter.time.monotonic", lambda: clock[0])
        rl = RateLimiter(max_calls=2, period_seconds=60)
        rl.check()
        rl.check()

        clock[0] += 61
        rl.check()

    def test_sliding_window_does_not_count_expired_calls(self, monkeypatch):
        from utils.rate_limiter import RateLimiter
        import time as time_mod

        clock = [time_mod.monotonic()]
        monkeypatch.setattr("utils.rate_limiter.time.monotonic", lambda: clock[0])
        rl = RateLimiter(max_calls=3, period_seconds=60)
        rl.check()
        rl.check()

        clock[0] += 61
        rl.check()
        rl.check()
        rl.check()

    def test_rate_limit_exceeded_is_exception(self):
        from utils.rate_limiter import RateLimitExceeded

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
        from utils.logging import _JsonFormatter

        formatter = _JsonFormatter()
        record = self._make_record("something_happened")
        output = formatter.format(record)
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_includes_required_fields(self):
        import json
        from utils.logging import _JsonFormatter

        formatter = _JsonFormatter()
        record = self._make_record("config_fetched")
        parsed = json.loads(formatter.format(record))
        assert "timestamp" in parsed
        assert "level" in parsed
        assert "event" in parsed
        assert "module" in parsed

    def test_event_matches_message(self):
        import json
        from utils.logging import _JsonFormatter

        formatter = _JsonFormatter()
        record = self._make_record("backup_created")
        parsed = json.loads(formatter.format(record))
        assert parsed["event"] == "backup_created"

    def test_module_stripped_of_pueo_prefix(self):
        import json
        from utils.logging import _JsonFormatter

        formatter = _JsonFormatter()
        record = self._make_record("x")
        parsed = json.loads(formatter.format(record))
        assert parsed["module"] == "test_module"
        assert not parsed["module"].startswith("pueo.")

    def test_extra_fields_appear_in_output(self):
        import json
        from utils.logging import _JsonFormatter

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
        from utils.logging import StructuredLogger

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
        from utils.logging import StructuredLogger

        inner = MagicMock(spec=logging_mod.Logger)
        log = StructuredLogger(inner)
        log.warning("rate_limit_exceeded")
        assert inner.log.call_args[0][0] == logging_mod.WARNING

    def test_error_uses_error_level(self):
        import logging as logging_mod
        from unittest.mock import MagicMock
        from utils.logging import StructuredLogger

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
        from utils.logging import _TextFormatter

        formatter = _TextFormatter()
        record = self._make_record("step1_complete")
        output = formatter.format(record)
        assert output == "INFO     step1_complete"

    def test_extras_rendered_as_key_value_pairs(self):
        from utils.logging import _TextFormatter

        formatter = _TextFormatter()
        record = self._make_record("step1_complete")
        record.mode = "addon"
        record.step = "detect_deployment"
        output = formatter.format(record)
        assert "mode='addon'" in output
        assert "step='detect_deployment'" in output

    def test_correlation_id_excluded(self):
        from utils.logging import _TextFormatter

        formatter = _TextFormatter()
        record = self._make_record("install_state_updated")
        record.correlation_id = "some-uuid-value"
        record.state = "MQTT_RUNNING"
        output = formatter.format(record)
        assert "correlation_id" not in output
        assert "state='MQTT_RUNNING'" in output

    def test_setup_logging_console_text_attaches_text_formatter(self, monkeypatch):
        import logging as logging_mod
        import utils.logging as logging_utils
        from utils.logging import _TextFormatter

        monkeypatch.setattr(logging_utils, "_configured", False)
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

    def test_setup_logging_default_uses_json_formatter(self, monkeypatch):
        import logging as logging_mod
        import utils.logging as logging_utils
        from utils.logging import _JsonFormatter, _TextFormatter

        monkeypatch.setattr(logging_utils, "_configured", False)
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
        from utils.logging import get_correlation_id, set_correlation_id

        set_correlation_id("")
        assert get_correlation_id() == ""

    def test_set_and_get_roundtrip(self):
        from utils.logging import get_correlation_id, set_correlation_id

        set_correlation_id("abc-123")
        assert get_correlation_id() == "abc-123"

    def test_correlation_id_included_in_log_extra(self):
        import logging as logging_mod
        from unittest.mock import MagicMock
        from utils.logging import StructuredLogger, set_correlation_id

        set_correlation_id("repair-uuid-xyz")
        inner = MagicMock(spec=logging_mod.Logger)
        log = StructuredLogger(inner)
        log.info("repair_cycle_started")
        extra = inner.log.call_args[1]["extra"]
        assert extra.get("correlation_id") == "repair-uuid-xyz"

    def test_explicit_correlation_id_not_overwritten(self):
        import logging as logging_mod
        from unittest.mock import MagicMock
        from utils.logging import StructuredLogger, set_correlation_id

        set_correlation_id("ctx-id")
        inner = MagicMock(spec=logging_mod.Logger)
        log = StructuredLogger(inner)
        log.info("event", correlation_id="explicit-id")
        extra = inner.log.call_args[1]["extra"]
        assert extra["correlation_id"] == "explicit-id"


# ── utils/context.py ─────────────────────────────────────────────────────────────


class TestEstimateTokens:
    def test_empty_string_returns_one(self):
        from utils.context import estimate_tokens

        assert estimate_tokens("") == 1

    def test_four_chars_is_one_token(self):
        from utils.context import estimate_tokens

        assert estimate_tokens("abcd") == 1

    def test_hundred_chars_is_twenty_five_tokens(self):
        from utils.context import estimate_tokens

        assert estimate_tokens("x" * 100) == 25

    def test_scales_with_length(self):
        from utils.context import estimate_tokens

        assert estimate_tokens("a" * 400) == 100
        assert estimate_tokens("a" * 4000) == 1000


class TestTruncateToBudget:
    def test_short_text_unchanged(self):
        from utils.context import truncate_to_budget

        text = "hello world"
        assert truncate_to_budget(text, 100) == text

    def test_exactly_at_budget_unchanged(self):
        from utils.context import truncate_to_budget

        text = "a" * 400  # 400 chars = 100 tokens exactly
        assert truncate_to_budget(text, 100) == text

    def test_tail_strategy_keeps_end(self):
        from utils.context import truncate_to_budget

        text = "START" + "x" * 400 + "END"
        result = truncate_to_budget(text, 10, strategy="tail")
        assert result.endswith("END")
        assert "START" not in result

    def test_head_strategy_keeps_start(self):
        from utils.context import truncate_to_budget

        text = "START" + "x" * 400 + "END"
        result = truncate_to_budget(text, 10, strategy="head")
        assert result.startswith("START")
        assert "END" not in result

    def test_smart_strategy_includes_separator(self):
        from utils.context import truncate_to_budget

        text = "A" * 2000
        result = truncate_to_budget(text, 100, strategy="smart")
        assert "...[truncated]..." in result

    def test_smart_strategy_keeps_both_ends(self):
        from utils.context import truncate_to_budget

        text = "HEADER" + "x" * 2000 + "FOOTER"
        result = truncate_to_budget(text, 100, strategy="smart")
        assert "HEADER" in result
        assert "FOOTER" in result

    def test_default_strategy_is_tail(self):
        from utils.context import truncate_to_budget

        text = "START" + "z" * 800
        result = truncate_to_budget(text, 10)
        assert "START" not in result
        assert len(result) == 40  # 10 tokens * 4 chars


class TestSlidingWindowLines:
    def test_empty_list_returns_empty(self):
        from utils.context import sliding_window_lines

        assert sliding_window_lines([], 100) == []

    def test_few_lines_all_fit(self):
        from utils.context import sliding_window_lines

        lines = ["line one", "line two", "line three"]
        assert sliding_window_lines(lines, 1000) == lines

    def test_too_many_lines_drops_oldest(self):
        from utils.context import sliding_window_lines

        lines = ["old " * 100 + str(i) for i in range(20)]
        result = sliding_window_lines(lines, 50)
        assert result == lines[len(lines) - len(result) :]

    def test_order_preserved(self):
        from utils.context import sliding_window_lines

        lines = ["alpha", "beta", "gamma"]
        result = sliding_window_lines(lines, 1000)
        assert result == ["alpha", "beta", "gamma"]

    def test_single_line_fits(self):
        from utils.context import sliding_window_lines

        lines = ["short line"]
        assert sliding_window_lines(lines, 100) == lines

    def test_result_fits_within_budget(self):
        from utils.context import sliding_window_lines, estimate_tokens

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
        from utils.yaml_validator import ValidationResult

        r = ValidationResult(is_safe=True, reasons=[])
        assert r.is_safe is True
        assert r.reasons == []

    def test_unsafe_with_reasons(self):
        from utils.yaml_validator import ValidationResult

        r = ValidationResult(is_safe=False, reasons=["missing homeassistant block"])
        assert r.is_safe is False
        assert len(r.reasons) == 1

    def test_reasons_defaults_to_empty_list(self):
        from utils.yaml_validator import ValidationResult

        r = ValidationResult(is_safe=True)
        assert r.reasons == []


class TestValidateProposedFix:
    def test_valid_fix_passes(self):
        from utils.yaml_validator import validate_proposed_fix

        result = validate_proposed_fix(_VALID_ORIGINAL, _VALID_FIX)
        assert result.is_safe is True
        assert result.reasons == []

    def test_empty_proposed_yaml_rejected(self):
        from utils.yaml_validator import validate_proposed_fix

        result = validate_proposed_fix(_VALID_ORIGINAL, "")
        assert result.is_safe is False
        assert any("empty" in r for r in result.reasons)

    def test_whitespace_only_proposed_yaml_rejected(self):
        from utils.yaml_validator import validate_proposed_fix

        result = validate_proposed_fix(_VALID_ORIGINAL, "   \n  ")
        assert result.is_safe is False

    def test_unparseable_yaml_rejected(self):
        from utils.yaml_validator import validate_proposed_fix

        result = validate_proposed_fix(_VALID_ORIGINAL, "key: [unclosed")
        assert result.is_safe is False
        assert any("does not parse" in r for r in result.reasons)

    def test_non_mapping_yaml_rejected(self):
        from utils.yaml_validator import validate_proposed_fix

        result = validate_proposed_fix(_VALID_ORIGINAL, "- item1\n- item2\n")
        assert result.is_safe is False
        assert any("mapping" in r for r in result.reasons)

    def test_missing_homeassistant_block_rejected(self):
        from utils.yaml_validator import validate_proposed_fix

        proposed = "http:\n  server_port: 8123\n"
        result = validate_proposed_fix(_VALID_ORIGINAL, proposed)
        assert result.is_safe is False
        assert any("homeassistant" in r for r in result.reasons)

    def test_removed_top_level_key_rejected(self):
        from utils.yaml_validator import validate_proposed_fix

        proposed = "homeassistant:\n  name: Home\n"
        result = validate_proposed_fix(_VALID_ORIGINAL, proposed)
        assert result.is_safe is False
        assert any("http" in r or "logger" in r for r in result.reasons)

    def test_completely_different_yaml_rejected(self):
        from utils.yaml_validator import validate_proposed_fix

        proposed = "\n".join([f"key_{i}: value_{i}" for i in range(200)])
        result = validate_proposed_fix(_VALID_ORIGINAL, proposed)
        assert result.is_safe is False
        assert any("differs too much" in r for r in result.reasons)

    def test_nearly_identical_fix_passes(self):
        from utils.yaml_validator import validate_proposed_fix

        fix = _VALID_ORIGINAL.replace("warning", "info")
        result = validate_proposed_fix(_VALID_ORIGINAL, fix)
        assert result.is_safe is True

    def test_original_with_bad_yaml_does_not_raise(self):
        from utils.yaml_validator import validate_proposed_fix

        result = validate_proposed_fix("key: [broken", _VALID_FIX)
        assert isinstance(result.is_safe, bool)

    def test_multiple_violations_reported(self):
        from utils.yaml_validator import validate_proposed_fix

        proposed = "some_new_key:\n  value: x\n"
        result = validate_proposed_fix(_VALID_ORIGINAL, proposed)
        assert result.is_safe is False
        assert len(result.reasons) >= 2


# ── utils/ssh_client.py (FakeSSHClient) ──────────────────────────────────────────


class TestFakeSSHClient:
    def test_read_file_returns_configured_content(self):
        from utils.ssh_client import FakeSSHClient

        c = FakeSSHClient(file_contents={"/foo": "bar"})
        assert asyncio.run(c.read_file("/foo")) == "bar"

    def test_read_file_raises_for_unknown_path(self):
        from utils.ssh_client import FakeSSHClient

        c = FakeSSHClient()
        with pytest.raises(FileNotFoundError):
            asyncio.run(c.read_file("/missing"))

    def test_write_file_records_content(self):
        from utils.ssh_client import FakeSSHClient

        c = FakeSSHClient()
        asyncio.run(c.write_file("/out", "hello"))
        assert c.written_files["/out"] == "hello"

    def test_run_returns_default_success(self):
        from utils.ssh_client import FakeSSHClient

        c = FakeSSHClient()
        ec, stdout, stderr = asyncio.run(c.run("anything"))
        assert ec == 0

    def test_run_matches_command_pattern(self):
        from utils.ssh_client import FakeSSHClient

        c = FakeSSHClient(command_results={"ha core check": (0, "ok", "")})
        ec, stdout, _ = asyncio.run(c.run("ha core check"))
        assert ec == 0
        assert stdout == "ok"

    def test_run_raises_on_check_true_with_nonzero(self):
        from utils.ssh_client import FakeSSHClient

        c = FakeSSHClient(command_results={"fail_cmd": (1, "", "error")})
        with pytest.raises(RuntimeError):
            asyncio.run(c.run("fail_cmd", check=True))

    def test_run_records_commands(self):
        from utils.ssh_client import FakeSSHClient

        c = FakeSSHClient()
        asyncio.run(c.run("cmd_one"))
        asyncio.run(c.run("cmd_two"))
        assert "cmd_one" in c.commands_run
        assert "cmd_two" in c.commands_run

    def test_stream_lines_yields_data(self):
        from utils.ssh_client import FakeSSHClient

        c = FakeSSHClient(stream_data=["line1", "line2", "line3"])

        async def collect():
            return [line async for line in c.stream_lines("tail -F /log")]

        lines = asyncio.run(collect())
        assert lines == ["line1", "line2", "line3"]

    def test_stream_lines_empty(self):
        from utils.ssh_client import FakeSSHClient

        c = FakeSSHClient()

        async def collect():
            return [line async for line in c.stream_lines("tail -F /log")]

        assert asyncio.run(collect()) == []


# ── utils/ollama_client.py (FakeLLMClient) ───────────────────────────────────────


class TestFakeLLMClient:
    def test_chat_returns_configured_json(self):
        from utils.ollama_client import FakeLLMClient

        c = FakeLLMClient('{"key": "value"}')
        result = asyncio.run(c.chat("model", [], {"temperature": 0}, {}))
        assert result["message"]["content"] == '{"key": "value"}'

    def test_chat_records_calls(self):
        from utils.ollama_client import FakeLLMClient

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
        from utils.resource import _parse_host_info

        free, total, used = _parse_host_info(_HOST_INFO_OUTPUT)
        assert free == 4.5
        assert total == 13.6
        assert used == 9.1

    def test_parse_meminfo_extracts_available_and_total(self):
        from utils.resource import _parse_meminfo

        available_mb, total_mb = _parse_meminfo(_MEMINFO_OUTPUT)
        assert available_mb == pytest.approx(563200 / 1024.0)
        assert total_mb == pytest.approx(1931384 / 1024.0)

    def test_parse_meminfo_missing_fields_returns_zero(self):
        from utils.resource import _parse_meminfo

        available_mb, total_mb = _parse_meminfo("Buffers: 12345 kB\n")
        assert available_mb == 0.0
        assert total_mb == 0.0


class TestResourceStatus:
    def test_construction_and_field_access(self):
        from utils.resource import ResourceStatus

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
        from utils.resource import ResourceStatus

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
        from utils.ssh_client import FakeSSHClient

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
        from utils.resource import poll_host_resources

        status = asyncio.run(
            poll_host_resources(self._fake_ssh(disk_free=4.5), 5.0, 2.0, 256.0)
        )
        assert status.disk_free_gb == 4.5
        assert status.disk_total_gb == 13.6

    def test_disk_warn_flag_set_when_below_warn_threshold(self):
        from utils.resource import poll_host_resources

        status = asyncio.run(
            poll_host_resources(self._fake_ssh(disk_free=3.0), 5.0, 2.0, 256.0)
        )
        assert status.disk_warn is True
        assert status.disk_critical is False

    def test_disk_critical_flag_set_when_below_critical_threshold(self):
        from utils.resource import poll_host_resources

        status = asyncio.run(
            poll_host_resources(self._fake_ssh(disk_free=1.5), 5.0, 2.0, 256.0)
        )
        assert status.disk_critical is True
        assert status.disk_warn is True

    def test_disk_flags_clear_when_above_thresholds(self):
        from utils.resource import poll_host_resources

        status = asyncio.run(
            poll_host_resources(self._fake_ssh(disk_free=8.0), 5.0, 2.0, 256.0)
        )
        assert status.disk_warn is False
        assert status.disk_critical is False

    def test_mem_warn_flag_set_when_below_warn_threshold(self):
        from utils.resource import poll_host_resources

        status = asyncio.run(
            poll_host_resources(
                self._fake_ssh(mem_available_kb=200 * 1024), 5.0, 2.0, 256.0
            )
        )
        assert status.mem_warn is True

    def test_mem_warn_clear_when_above_threshold(self):
        from utils.resource import poll_host_resources

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
        from utils.resource import (
            ResourceStatus,
            check_disk_not_critical,
            DiskCriticalError,
        )
        import utils.resource as resource_mod

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
        from utils.resource import ResourceStatus, check_disk_not_critical
        import utils.resource as resource_mod

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
        from utils.resource import check_disk_not_critical
        import utils.resource as resource_mod

        monkeypatch.setattr(resource_mod, "_last_resource_status", None)
        check_disk_not_critical(2.0)  # must not raise


class TestResourcePollerAlerts:
    def _make_status(
        self,
        disk_free: float = 6.0,
        mem_available_mb: float = 550.0,
        disk_warn: bool = False,
        disk_critical: bool = False,
        mem_warn: bool = False,
    ):
        from utils.resource import ResourceStatus

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
        from utils.resource import ResourcePoller
        from utils.ssh_client import FakeSSHClient

        return ResourcePoller(
            ssh_client=FakeSSHClient(),
            notifier=notifier,
            interval_seconds=300,
            disk_warn_gb=5.0,
            disk_critical_gb=2.0,
            mem_warn_mb=256.0,
        )

    def test_sends_disk_critical_alert_on_first_breach(self):
        from utils.notify import FakeNotifier

        notifier = FakeNotifier()
        poller = self._make_poller(notifier)
        status = self._make_status(disk_free=1.5, disk_warn=True, disk_critical=True)
        asyncio.run(poller._check_and_alert(status))
        assert len(notifier.sent) == 1
        assert "CRITICAL" in notifier.sent[0]["subject"]

    def test_deduplicates_consecutive_disk_critical_alerts(self):
        from utils.notify import FakeNotifier

        notifier = FakeNotifier()
        poller = self._make_poller(notifier)
        status = self._make_status(disk_free=1.5, disk_warn=True, disk_critical=True)
        asyncio.run(poller._check_and_alert(status))
        asyncio.run(poller._check_and_alert(status))
        assert len(notifier.sent) == 1

    def test_resends_alert_after_condition_clears_and_retriggers(self):
        from utils.notify import FakeNotifier

        notifier = FakeNotifier()
        poller = self._make_poller(notifier)
        critical = self._make_status(disk_free=1.5, disk_warn=True, disk_critical=True)
        ok = self._make_status(disk_free=6.0)
        asyncio.run(poller._check_and_alert(critical))
        asyncio.run(poller._check_and_alert(ok))
        asyncio.run(poller._check_and_alert(critical))
        assert len(notifier.sent) == 2

    def test_sends_disk_warn_alert_when_warn_only(self):
        from utils.notify import FakeNotifier

        notifier = FakeNotifier()
        poller = self._make_poller(notifier)
        status = self._make_status(disk_free=3.0, disk_warn=True, disk_critical=False)
        asyncio.run(poller._check_and_alert(status))
        assert len(notifier.sent) == 1
        assert "WARNING" in notifier.sent[0]["subject"]
        assert "disk" in notifier.sent[0]["subject"].lower()

    def test_sends_mem_warn_alert_when_mem_low(self):
        from utils.notify import FakeNotifier

        notifier = FakeNotifier()
        poller = self._make_poller(notifier)
        status = self._make_status(mem_available_mb=100.0, mem_warn=True)
        asyncio.run(poller._check_and_alert(status))
        assert len(notifier.sent) == 1
        assert "memory" in notifier.sent[0]["subject"].lower()

    def test_no_alert_when_all_thresholds_ok(self):
        from utils.notify import FakeNotifier

        notifier = FakeNotifier()
        poller = self._make_poller(notifier)
        status = self._make_status(disk_free=8.0, mem_available_mb=600.0)
        asyncio.run(poller._check_and_alert(status))
        assert len(notifier.sent) == 0

    def test_update_resource_status_sets_cache(self, monkeypatch):
        from utils.resource import ResourceStatus, update_resource_status
        import utils.resource as resource_mod

        monkeypatch.setattr(resource_mod, "_last_resource_status", None)
        status = self._make_status(disk_free=6.0)
        update_resource_status(status)
        assert resource_mod._last_resource_status is status

    def test_run_polls_and_updates_cache_then_cancels(self, monkeypatch):
        from utils.resource import ResourcePoller, ResourceStatus
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        import utils.resource as resource_mod

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
        from utils.resource import ResourcePoller
        from utils.notify import FakeNotifier
        from utils.ssh_client import FakeSSHClient
        import utils.resource as resource_mod

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
        import ha_agent_advanced

        path = str(tmp_path / "disk_check_test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", path)
        ha_agent_advanced.init_local_database()
        return path

    def test_raises_disk_critical_error_when_cached_status_is_critical(
        self, monkeypatch, db_path
    ):
        from utils.resource import ResourceStatus, DiskCriticalError
        import utils.resource as resource_mod
        import ha_agent_advanced
        from utils.ssh_client import FakeSSHClient

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
        from utils.resource import ResourceStatus
        import utils.resource as resource_mod
        import ha_agent_advanced
        from utils.ssh_client import FakeSSHClient

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
        import utils.resource as resource_mod
        import ha_agent_advanced
        from utils.ssh_client import FakeSSHClient

        monkeypatch.setattr(resource_mod, "_last_resource_status", None)
        ssh = FakeSSHClient(
            command_results={"ha backup new": (0, "Slug: fresh-slug\n", "")}
        )
        slug = asyncio.run(ha_agent_advanced.execute_remote_backup(ssh_client=ssh))
        assert slug == "fresh-slug"


# ── ha_agent_core pipeline ────────────────────────────────────────────────────────

_SIMPLE_CONFIG = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8123\n"
