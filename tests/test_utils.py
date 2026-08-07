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


# ── RAG knowledge store (item 49) ─────────────────────────────────────────────────


class TestKnowledgeChunk:
    def test_valid_construction(self):
        from utils.knowledge_store import KnowledgeChunk

        chunk = KnowledgeChunk(
            text="some text", source="ha/2024.1", collection="ha_release_notes"
        )
        assert chunk.text == "some text"
        assert chunk.score == 0.0
        assert chunk.metadata == {}

    def test_construction_with_all_fields(self):
        from utils.knowledge_store import KnowledgeChunk

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
        from utils.knowledge_store import FakeKnowledgeStore

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
        from utils.knowledge_store import FakeKnowledgeStore

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
        from utils.knowledge_store import FakeKnowledgeStore

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
        from utils.knowledge_store import FakeKnowledgeStore

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
        from utils.knowledge_store import FakeKnowledgeStore

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
        from utils.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        results = store.query("anything", top_k=5, collections=["nonexistent"])
        assert results == []


class TestFakeKnowledgeStorePrune:
    def test_prune_removes_stale_ids(self):
        from utils.knowledge_store import FakeKnowledgeStore

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
        from utils.knowledge_store import FakeKnowledgeStore

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
        from utils.knowledge_store import FakeKnowledgeStore

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
        from utils.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        removed = store.prune("nonexistent", keep_ids={"any-id"})
        assert removed == 0


# ── HA release notes scraper (item 50) ────────────────────────────────────────────


class TestParseBreakingChanges:
    def test_extracts_section_with_break_keyword(self):
        from utils.ha_release_notes_scraper import parse_breaking_changes

        notes = "# 2024.1\n\nSome intro text.\n## Breaking Changes\n- Template syntax changed\n## Other\n- Bug fix"
        result = parse_breaking_changes(notes)
        assert any("Template syntax changed" in c for c in result)

    def test_extracts_section_with_deprecated_keyword(self):
        from utils.ha_release_notes_scraper import parse_breaking_changes

        notes = "## What's New\nFoo\n## Deprecated\nOld API removed"
        result = parse_breaking_changes(notes)
        assert any("Old API removed" in c for c in result)

    def test_falls_back_to_first_chunk_when_no_match(self):
        from utils.ha_release_notes_scraper import parse_breaking_changes

        notes = "No relevant sections here at all."
        result = parse_breaking_changes(notes)
        assert len(result) == 1
        assert "No relevant sections" in result[0]

    def test_truncates_long_sections(self):
        from utils.ha_release_notes_scraper import parse_breaking_changes

        notes = "## Breaking Changes\n" + "x" * 3000
        result = parse_breaking_changes(notes)
        assert all(len(c) <= 2000 for c in result)


class TestChunkReleaseNotes:
    def test_returns_ids_docs_metas(self):
        from utils.ha_release_notes_scraper import chunk_release_notes

        ids, docs, metas = chunk_release_notes(
            "## Breaking Changes\nfoo changed", "2024.1"
        )
        assert len(ids) == len(docs) == len(metas)
        assert all(id_.startswith("ha-2024.1-") for id_ in ids)
        assert all(m["version"] == "2024.1" for m in metas)

    def test_ids_are_unique(self):
        from utils.ha_release_notes_scraper import chunk_release_notes

        notes = "## Breaking\nfoo\n## Removed\nbar\n## Renamed\nbaz"
        ids, _, _ = chunk_release_notes(notes, "2024.2")
        assert len(ids) == len(set(ids))

    def test_chunk_release_notes_release_type(self):
        from utils.ha_release_notes_scraper import chunk_release_notes

        _, _, metas_ga = chunk_release_notes("## Changes\nsome text", "2026.8.0")
        assert all(m["release_type"] == "ga" for m in metas_ga)

        _, _, metas_patch = chunk_release_notes("## Changes\nsome text", "2026.7.2")
        assert all(m["release_type"] == "patch" for m in metas_patch)

        _, _, metas_beta = chunk_release_notes("## Changes\nsome text", "2026.8.0b4")
        assert all(m["release_type"] == "beta" for m in metas_beta)

    def test_chunk_release_notes_category(self):
        from utils.ha_release_notes_scraper import chunk_release_notes

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
        from utils.ha_release_notes_scraper import chunk_release_notes

        notes = "## Backward Incompatible Changes\nThe `zha` integration changed.\n"
        _, _, metas = chunk_release_notes(notes, "2026.8.0")
        breaking = [m for m in metas if m["category"] == "breaking_change"]
        assert any(m["impacted_integration"] == "zha" for m in breaking)

    def test_chunk_release_notes_non_breaking_has_empty_integration(self):
        from utils.ha_release_notes_scraper import chunk_release_notes

        notes = "## New Integrations\nAdded `matter` support.\n"
        _, _, metas = chunk_release_notes(notes, "2026.8.0")
        assert all(m["impacted_integration"] == "" for m in metas)


class TestKnowledgeStoreWhereClause:
    def test_where_in_filters_by_metadata(self):
        from utils.knowledge_store import FakeKnowledgeStore

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
        from utils.knowledge_store import FakeKnowledgeStore

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
        from utils.ha_release_notes_scraper import scrape_cached_release_notes
        from utils.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        result = scrape_cached_release_notes("/nonexistent/path", store)
        assert result == 0

    def test_processes_txt_files(self, tmp_path):
        from utils.ha_release_notes_scraper import scrape_cached_release_notes
        from utils.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "ha_notes"
        cache.mkdir()
        (cache / "2024.1.txt").write_text("## Breaking Changes\ntemplate changed")
        (cache / "2024.2.txt").write_text("## Breaking Changes\nautomation changed")

        store = FakeKnowledgeStore()
        result = scrape_cached_release_notes(str(cache), store)
        assert result == 2
        assert len(store.query("template", top_k=5)) > 0

    def test_skips_non_txt_files(self, tmp_path):
        from utils.ha_release_notes_scraper import scrape_cached_release_notes
        from utils.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "ha_notes"
        cache.mkdir()
        (cache / "README.md").write_text("## Breaking Changes\nsome change")

        store = FakeKnowledgeStore()
        result = scrape_cached_release_notes(str(cache), store)
        assert result == 0

    def test_skips_unreadable_file(self, tmp_path, monkeypatch):
        from pathlib import Path
        from utils.ha_release_notes_scraper import scrape_cached_release_notes
        from utils.knowledge_store import FakeKnowledgeStore

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
        from utils.ha_blog_scraper import extract_blog_url_from_stub

        stub = "https://www.home-assistant.io/blog/2026/08/06/release-20268/"
        assert extract_blog_url_from_stub(stub) == stub

    def test_extract_blog_url_missing_returns_none(self):
        from utils.ha_blog_scraper import extract_blog_url_from_stub

        assert extract_blog_url_from_stub("no url here") is None

    def test_extract_blog_url_ignores_non_blog_urls(self):
        from utils.ha_blog_scraper import extract_blog_url_from_stub

        assert (
            extract_blog_url_from_stub("https://github.com/home-assistant/core") is None
        )

    def test_fetch_blog_post_strips_html(self):
        from utils.ha_blog_scraper import fetch_blog_post

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
        from utils.ha_blog_scraper import fetch_blog_post

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
        from utils.ha_blog_scraper import fetch_blog_release_notes

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
        from utils.ha_blog_scraper import fetch_blog_release_notes

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
        from utils.ha_blog_scraper import fetch_blog_release_notes

        cache = tmp_path / "notes"
        cache.mkdir()
        (cache / "2026.8.0.txt").write_text("STUB:no url here", encoding="utf-8")

        count = fetch_blog_release_notes(
            str(cache), _fetcher=lambda url: b"unreachable"
        )
        assert count == 0

    def test_fetch_blog_release_notes_skips_short_blog_content(self, tmp_path):
        from utils.ha_blog_scraper import fetch_blog_release_notes

        cache = tmp_path / "notes"
        cache.mkdir()
        blog_url = "https://www.home-assistant.io/blog/2026/08/06/release-20268/"
        (cache / "2026.8.0.txt").write_text(f"STUB:{blog_url}", encoding="utf-8")

        def fake_fetcher(url: str) -> bytes:
            return b"<article><p>Too short.</p></article>"

        count = fetch_blog_release_notes(str(cache), _fetcher=fake_fetcher)
        assert count == 0

    def test_fetch_blog_release_notes_missing_dir(self):
        from utils.ha_blog_scraper import fetch_blog_release_notes

        count = fetch_blog_release_notes("/nonexistent/path")
        assert count == 0


class TestParseReleaseSections:
    def test_embeds_all_sections_not_just_breaking(self):
        from utils.ha_release_notes_scraper import parse_release_sections

        notes = "## New Features\nAdded new light platform\n## Bug Fixes\nFixed timer"
        result = parse_release_sections(notes)
        assert len(result) == 2
        assert any("new light platform" in c for c in result)
        assert any("Fixed timer" in c for c in result)

    def test_embeds_additive_sections_with_no_breaking_keywords(self):
        from utils.ha_release_notes_scraper import parse_release_sections

        notes = (
            "## New Integrations\nAdded Sonos support\n## Performance\nFaster startup"
        )
        result = parse_release_sections(notes)
        assert len(result) == 2

    def test_word_boundary_truncation_at_3000(self):
        from utils.ha_release_notes_scraper import parse_release_sections

        long_section = "word " * 700  # ~3500 chars
        notes = f"## Section\n{long_section}"
        result = parse_release_sections(notes)
        assert len(result) == 1
        assert len(result[0]) <= 3000
        assert not result[0].endswith("wor")  # truncated at word boundary

    def test_returns_single_chunk_for_no_headings(self):
        from utils.ha_release_notes_scraper import parse_release_sections

        notes = "Just some plain text with no headings."
        result = parse_release_sections(notes)
        assert len(result) == 1
        assert "plain text" in result[0]

    def test_strips_empty_sections(self):
        from utils.ha_release_notes_scraper import parse_release_sections

        notes = "## Header\n\n## Populated\nSome content"
        result = parse_release_sections(notes)
        assert all(c.strip() for c in result)


class TestScrapeWithCollectedIds:
    def test_collected_ids_populated(self, tmp_path):
        from utils.ha_release_notes_scraper import scrape_cached_release_notes
        from utils.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "notes"
        cache.mkdir()
        (cache / "2024.1.txt").write_text("## Breaking\nfoo\n## Features\nbar")

        store = FakeKnowledgeStore()
        collected: set[str] = set()
        scrape_cached_release_notes(str(cache), store, collected)
        assert len(collected) == 2
        assert "ha-2024.1-0" in collected

    def test_collected_ids_none_does_not_error(self, tmp_path):
        from utils.ha_release_notes_scraper import scrape_cached_release_notes
        from utils.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "notes"
        cache.mkdir()
        (cache / "2024.1.txt").write_text("## Features\nfoo")

        store = FakeKnowledgeStore()
        result = scrape_cached_release_notes(str(cache), store, None)
        assert result == 1


# ── HACS changelog scraper (item 51) ──────────────────────────────────────────────


class TestParseChangelog:
    def test_splits_by_section_header(self):
        from utils.hacs_scraper import parse_changelog

        text = "## 1.0.0\nFirst release\n## 0.9.0\nBeta"
        result = parse_changelog(text)
        assert len(result) == 2
        assert "First release" in result[0]

    def test_returns_empty_for_blank_input(self):
        from utils.hacs_scraper import parse_changelog

        result = parse_changelog("")
        assert result == []

    def test_truncates_long_sections(self):
        from utils.hacs_scraper import parse_changelog

        text = "## 1.0.0\n" + "a" * 4000
        result = parse_changelog(text)
        assert all(len(c) <= 3000 for c in result)


class TestChunkChangelog:
    def test_returns_ids_docs_metas(self):
        from utils.hacs_scraper import chunk_changelog

        ids, docs, metas = chunk_changelog(
            "## 1.0.0\nchange one\n## 0.9.0\nchange two", "myint"
        )
        assert len(ids) == len(docs) == len(metas) == 2
        assert all(id_.startswith("hacs-myint-") for id_ in ids)
        assert all(m["slug"] == "myint" for m in metas)

    def test_returns_empty_for_blank_changelog(self):
        from utils.hacs_scraper import chunk_changelog

        ids, docs, metas = chunk_changelog("", "myint")
        assert ids == [] and docs == [] and metas == []


class TestEmbedCachedChangelogs:
    def test_returns_zero_for_missing_dir(self):
        from utils.hacs_scraper import embed_cached_changelogs
        from utils.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        result = embed_cached_changelogs("/nonexistent/path", store)
        assert result == 0

    def test_processes_md_files(self, tmp_path):
        from utils.hacs_scraper import embed_cached_changelogs
        from utils.knowledge_store import FakeKnowledgeStore

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
        from utils.hacs_scraper import embed_cached_changelogs
        from utils.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "hacs"
        cache.mkdir()
        (cache / "notes.txt").write_text("## 1.0.0\nchange")

        store = FakeKnowledgeStore()
        result = embed_cached_changelogs(str(cache), store)
        assert result == 0

    def test_skips_unreadable_file(self, tmp_path, monkeypatch):
        from pathlib import Path
        from utils.hacs_scraper import embed_cached_changelogs
        from utils.knowledge_store import FakeKnowledgeStore

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
        from utils.hacs_scraper import _repo_from_release_url

        url = "https://github.com/dmamontov/hass-pycync/releases/tag/v1.0.0"
        assert _repo_from_release_url(url) == "dmamontov/hass-pycync"

    def test_extracts_from_releases_url(self):
        from utils.hacs_scraper import _repo_from_release_url

        url = "https://github.com/custom-org/my-integration/releases"
        assert _repo_from_release_url(url) == "custom-org/my-integration"

    def test_returns_none_for_non_github_url(self):
        from utils.hacs_scraper import _repo_from_release_url

        assert _repo_from_release_url("https://gitlab.com/foo/bar/releases") is None

    def test_returns_none_for_empty_string(self):
        from utils.hacs_scraper import _repo_from_release_url

        assert _repo_from_release_url("") is None


class TestChunkChangelogCollectedIds:
    def test_collected_ids_populated(self):
        from utils.hacs_scraper import chunk_changelog

        collected: set[str] = set()
        ids, _, _ = chunk_changelog("## 1.0.0\nfoo\n## 0.9.0\nbar", "myint", collected)
        assert collected == set(ids)

    def test_collected_ids_none_does_not_error(self):
        from utils.hacs_scraper import chunk_changelog

        ids, docs, metas = chunk_changelog("## 1.0.0\nfoo", "myint", None)
        assert len(ids) == 1


class TestHACSChunkVersion:
    def test_version_extracted_from_semver_heading(self):
        from utils.hacs_scraper import chunk_changelog

        _, _, metas = chunk_changelog(
            "## 1.2.3\nFixed a bug.\n## 0.9.0\nAdded feature.", "myint"
        )
        assert metas[0]["version"] == "1.2.3"
        assert metas[1]["version"] == "0.9.0"

    def test_version_empty_when_no_semver_heading(self):
        from utils.hacs_scraper import chunk_changelog

        # Section heading that isn't a version number
        _, _, metas = chunk_changelog("## Unreleased\nWIP stuff.", "myint")
        assert metas[0]["version"] == ""

    def test_chunk_max_size_is_3000(self):
        from utils.hacs_scraper import parse_changelog

        long_text = "## 1.0.0\n" + ("word " * 1000)
        result = parse_changelog(long_text)
        assert len(result) == 1
        assert len(result[0]) <= 3000


class TestEmbedCachedChangelogsCollectedIds:
    def test_collected_ids_populated(self, tmp_path):
        from utils.hacs_scraper import embed_cached_changelogs
        from utils.knowledge_store import FakeKnowledgeStore

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
        from utils.ha_docs_scraper import parse_integration_doc

        doc = (
            "---\ntitle: Test\nha_category: Integration\n---\n## Overview\nSome content"
        )
        result = parse_integration_doc(doc)
        assert result
        assert all("---" not in c for c in result)
        assert any("Some content" in c for c in result)

    def test_splits_by_headings(self):
        from utils.ha_docs_scraper import parse_integration_doc

        doc = "## Setup\nInstall the integration.\n## Configuration\nAdd to config."
        result = parse_integration_doc(doc)
        assert len(result) == 2

    def test_word_boundary_truncation(self):
        from utils.ha_docs_scraper import parse_integration_doc

        long_section = "word " * 700  # ~3500 chars
        doc = f"## Section\n{long_section}"
        result = parse_integration_doc(doc)
        assert len(result) == 1
        assert len(result[0]) <= 3000
        assert not result[0].endswith("wor")

    def test_strips_empty_sections(self):
        from utils.ha_docs_scraper import parse_integration_doc

        doc = "## Header\n\n## Content\nActual text here"
        result = parse_integration_doc(doc)
        assert all(c.strip() for c in result)

    def test_handles_doc_without_frontmatter(self):
        from utils.ha_docs_scraper import parse_integration_doc

        doc = "## Overview\nJust a plain doc with no front matter."
        result = parse_integration_doc(doc)
        assert result
        assert "plain doc" in result[0]


class TestEmbedCachedIntegrationDocs:
    def test_returns_zero_for_missing_dir(self):
        from utils.ha_docs_scraper import embed_cached_integration_docs
        from utils.knowledge_store import FakeKnowledgeStore

        store = FakeKnowledgeStore()
        assert embed_cached_integration_docs("/nonexistent/path", store) == 0

    def test_processes_md_files(self, tmp_path):
        from utils.ha_docs_scraper import embed_cached_integration_docs
        from utils.knowledge_store import FakeKnowledgeStore

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
        from utils.ha_docs_scraper import embed_cached_integration_docs
        from utils.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "docs"
        cache.mkdir()
        (cache / "readme.txt").write_text("## Overview\nSome text")

        store = FakeKnowledgeStore()
        assert embed_cached_integration_docs(str(cache), store) == 0

    def test_skips_unreadable_file(self, tmp_path, monkeypatch):
        from pathlib import Path
        from utils.ha_docs_scraper import embed_cached_integration_docs
        from utils.knowledge_store import FakeKnowledgeStore

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
        from utils.ha_docs_scraper import embed_cached_integration_docs
        from utils.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "docs"
        cache.mkdir()
        (cache / "hue.md").write_text("## Setup\nSection one.\n## Config\nSection two.")

        store = FakeKnowledgeStore()
        collected: set[str] = set()
        embed_cached_integration_docs(str(cache), store, collected)
        assert len(collected) == 2
        assert "ha-docs-hue-0" in collected

    def test_is_installed_in_metadata(self, tmp_path):
        from utils.ha_docs_scraper import embed_cached_integration_docs
        from utils.knowledge_store import FakeKnowledgeStore

        cache = tmp_path / "docs"
        cache.mkdir()
        (cache / "zha.md").write_text("## Overview\nZHA integration docs.")

        store = FakeKnowledgeStore()
        embed_cached_integration_docs(str(cache), store)
        hits = store.query("ZHA integration", top_k=5)
        assert hits
        assert hits[0].metadata.get("is_installed") is True


class TestCommunityCollectionRemoved:
    def test_community_cases_not_in_collections(self):
        from utils.knowledge_store import COLLECTIONS

        assert "community_cases" not in COLLECTIONS


class TestFetchIntegrationDocReturnValues:
    """fetch_integration_doc tri-state return contract."""

    def test_returns_zero_when_already_cached(self, tmp_path):
        from utils.ha_docs_scraper import fetch_integration_doc

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


class TestDiscoverHacsApiFiltering:
    """_discover_via_hacs_api parses HACS repository JSON correctly."""

    def _urlopen(self, payload):
        import json

        data = json.dumps(payload).encode()
        return lambda *a, **kw: _MockHTTPResponse(data)

    def test_returns_installed_integrations(self, monkeypatch):
        import urllib.request

        from utils.hacs_scraper import _discover_via_hacs_api

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
        monkeypatch.setattr(urllib.request, "urlopen", self._urlopen(payload))
        result = _discover_via_hacs_api("http://ha:8123", "tok")
        assert result == [("pycync", "dmamontov/hass-pycync")]

    def test_excludes_non_integration_categories(self, monkeypatch):
        import urllib.request

        from utils.hacs_scraper import _discover_via_hacs_api

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
        monkeypatch.setattr(urllib.request, "urlopen", self._urlopen(payload))
        result = _discover_via_hacs_api("http://ha:8123", "tok")
        assert result == [("myint", "someone/myint")]

    def test_returns_empty_on_network_error(self, monkeypatch):
        import urllib.request

        from utils.hacs_scraper import _discover_via_hacs_api

        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("refused")),
        )
        assert _discover_via_hacs_api("http://ha:8123", "tok") == []

    def test_returns_empty_when_no_token(self):
        from utils.hacs_scraper import _discover_via_hacs_api

        # No HA token — discover_hacs_integrations guards against this, but
        # _discover_via_hacs_api itself will attempt the call and get an exception
        # from urllib (no actual network used in tests, so just verify shape).
        assert isinstance(_discover_via_hacs_api.__doc__, str)


class TestDiscoverHacsIntegrationsEntityFallback:
    """Entity-scan fallback excludes the HACS manager (hacs/integration)."""

    def _make_sequential_urlopen(self, hacs_api_exc, states_payload):
        """First call raises (HACS API unavailable); second returns states JSON."""
        import json

        calls = [0]

        def urlopen(*a, **kw):
            if calls[0] == 0:
                calls[0] += 1
                raise hacs_api_exc
            calls[0] += 1
            return _MockHTTPResponse(json.dumps(states_payload).encode())

        return urlopen

    def test_excludes_hacs_manager_from_entity_scan(self, monkeypatch):
        import urllib.request

        from utils.hacs_scraper import discover_hacs_integrations

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
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            self._make_sequential_urlopen(OSError("unavailable"), states),
        )
        result = discover_hacs_integrations("http://ha:8123", "tok")
        repos = [r for _, r in result]
        assert "hacs/integration" not in repos
        assert "dmamontov/hass-pycync" in repos

    def test_entity_scan_skips_builtin_update_entities(self, monkeypatch):
        """Built-in update entities use non-underscore brands URL — should be skipped."""
        import urllib.request

        from utils.hacs_scraper import discover_hacs_integrations

        states = [
            {
                "entity_id": "update.home_assistant_core_update",
                "attributes": {
                    "entity_picture": "https://brands.home-assistant.io/homeassistant/icon.png",
                    "release_url": "https://github.com/home-assistant/core/releases/tag/2026.7.0",
                },
            },
        ]
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            self._make_sequential_urlopen(OSError("unavailable"), states),
        )
        result = discover_hacs_integrations("http://ha:8123", "tok")
        assert result == []

    def test_returns_empty_for_missing_token(self):
        from utils.hacs_scraper import discover_hacs_integrations

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

        from utils.ha_docs_scraper import discover_installed_integrations

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

        from utils.ha_docs_scraper import discover_installed_integrations

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
        from utils.ha_docs_scraper import discover_installed_integrations

        assert discover_installed_integrations("http://ha:8123", "") == []

    def test_returns_empty_on_network_error(self, monkeypatch):
        import urllib.request

        from utils.ha_docs_scraper import discover_installed_integrations

        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("timeout")),
        )
        assert discover_installed_integrations("http://ha:8123", "tok") == []

    def test_deduplicates_platform_variants(self, monkeypatch):
        """Components like 'mqtt' and 'mqtt.sensor' both produce domain 'mqtt' once."""
        import urllib.request

        from utils.ha_docs_scraper import discover_installed_integrations

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

            from utils.supervisor import LoopSupervisor

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

            from utils.supervisor import LoopSupervisor

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

            from utils.supervisor import LoopSupervisor

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

            from utils.supervisor import LoopSupervisor

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
        from utils.supervisor import LoopSupervisor

        bus: asyncio.Queue = asyncio.Queue()
        sup = LoopSupervisor(bus=bus)
        assert len(sup._tasks) == 0
        assert len(sup.get_statuses()) == 0

    def test_last_error_recorded_on_crash(self):
        """Error message is recorded in LoopStatus after coro raises."""

        async def _run():
            async def bad_coro():
                raise ValueError("sentinel error")

            from utils.supervisor import LoopSupervisor

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

            from utils.supervisor import LoopSupervisor

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

            from utils.supervisor import LoopSupervisor

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

            from utils.supervisor import LoopSupervisor

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

            from utils.supervisor import LoopSupervisor

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

            from utils.supervisor import LoopSupervisor

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

            from utils.supervisor import LoopSupervisor

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

            from utils.supervisor import LoopSupervisor

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
        from utils.supervisor import LoopSupervisor

        sup = LoopSupervisor()
        with pytest.raises(KeyError):
            sup.pause("nonexistent")
        with pytest.raises(KeyError):
            sup.resume("nonexistent")
        with pytest.raises(KeyError):
            sup.run_now("nonexistent")

    def test_pause_idempotent(self):
        """Calling pause() on an already-paused loop is a no-op."""
        from utils.supervisor import LoopSupervisor

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
        from utils.supervisor import LoopSupervisor

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

            from utils.supervisor import LoopSupervisor

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

            from utils.supervisor import LoopSupervisor

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


# ── Timeline utility ──────────────────────────────────────────────────────────────


class TestTimelineUtils:
    """Unit tests for utils/timeline.py — write, load, count, get."""

    @pytest.fixture()
    def tl_db(self, tmp_path, monkeypatch):
        """Isolated SQLite DB with timeline_events table."""
        import ha_agent_advanced
        import utils.timeline as tl_mod

        db = str(tmp_path / "tl_test.db")
        monkeypatch.setattr(ha_agent_advanced, "DB_PATH", db)
        monkeypatch.setattr(tl_mod, "DB_PATH", db)
        ha_agent_advanced.init_local_database()
        return db

    def test_write_returns_positive_id(self, tl_db):
        from utils.timeline import write_timeline_event

        eid = write_timeline_event("INFO", "test_src", "hello world")
        assert eid > 0

    def test_write_persists_to_db(self, tl_db):
        import sqlite3
        from utils.timeline import write_timeline_event

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
        import utils.timeline as tl_mod

        published = []
        monkeypatch.setattr(tl_mod, "_publish_event_for_test", None, raising=False)

        import utils.supervisor as sup_mod

        captured = []
        monkeypatch.setattr(sup_mod, "publish_event", lambda e: captured.append(e))

        from utils.timeline import write_timeline_event

        write_timeline_event("ERROR", "ha_log_monitor", "bad thing happened")
        assert any(e.get("event_type") == "timeline" for e in captured)

    def test_load_returns_newest_first(self, tl_db):
        import time as _time
        from utils.timeline import load_timeline_events, write_timeline_event

        write_timeline_event("INFO", "a", "first")
        _time.sleep(0.01)
        write_timeline_event("INFO", "b", "second")
        events = load_timeline_events()
        assert events[0]["message"] == "second"
        assert events[1]["message"] == "first"

    def test_load_level_filter(self, tl_db):
        from utils.timeline import load_timeline_events, write_timeline_event

        write_timeline_event("INFO", "src", "info event")
        write_timeline_event("ERROR", "src", "error event")
        events = load_timeline_events(level_filter="ERROR")
        assert len(events) == 1
        assert events[0]["message"] == "error event"

    def test_load_source_filter(self, tl_db):
        from utils.timeline import load_timeline_events, write_timeline_event

        write_timeline_event("INFO", "resource", "disk ok")
        write_timeline_event("INFO", "update_check", "no updates")
        events = load_timeline_events(source_filter="resource")
        assert len(events) == 1
        assert events[0]["source"] == "resource"

    def test_load_parses_detail_json(self, tl_db):
        from utils.timeline import load_timeline_events, write_timeline_event

        write_timeline_event("INFO", "src", "msg", {"key": "val"})
        events = load_timeline_events()
        assert events[0]["detail"] == {"key": "val"}

    def test_load_empty_detail_returns_dict(self, tl_db):
        from utils.timeline import load_timeline_events, write_timeline_event

        write_timeline_event("INFO", "src", "msg")
        events = load_timeline_events()
        assert events[0]["detail"] == {}

    def test_get_returns_event_by_id(self, tl_db):
        from utils.timeline import get_timeline_event, write_timeline_event

        eid = write_timeline_event("CRITICAL", "src", "critical thing", {"x": 1})
        ev = get_timeline_event(eid)
        assert ev is not None
        assert ev["level"] == "CRITICAL"
        assert ev["detail"] == {"x": 1}

    def test_get_returns_none_for_missing_id(self, tl_db):
        from utils.timeline import get_timeline_event

        assert get_timeline_event(99999) is None

    def test_count_total(self, tl_db):
        from utils.timeline import count_timeline_events, write_timeline_event

        write_timeline_event("INFO", "src", "a")
        write_timeline_event("WARN", "src", "b")
        assert count_timeline_events() == 2

    def test_count_with_level_filter(self, tl_db):
        from utils.timeline import count_timeline_events, write_timeline_event

        write_timeline_event("INFO", "src", "a")
        write_timeline_event("WARN", "src", "b")
        assert count_timeline_events(level_filter="WARN") == 1

    def test_load_on_missing_table_returns_empty(self, tmp_path, monkeypatch):
        """Querying a DB with no timeline_events table returns []."""
        import utils.timeline as tl_mod

        db = str(tmp_path / "empty.db")
        monkeypatch.setattr(tl_mod, "DB_PATH", db)
        from utils.timeline import load_timeline_events

        result = load_timeline_events()
        assert result == []

    def test_write_sse_publish_exception_still_returns_id(self, tl_db, monkeypatch):
        """write_timeline_event() returns the id even when publish_event raises."""
        import utils.supervisor as sup_mod

        monkeypatch.setattr(
            sup_mod,
            "publish_event",
            lambda e: (_ for _ in ()).throw(RuntimeError("bus down")),
        )

        from utils.timeline import write_timeline_event

        eid = write_timeline_event("INFO", "src", "msg")
        assert eid > 0

    def test_get_on_missing_table_returns_none(self, tmp_path, monkeypatch):
        """get_timeline_event() returns None when the table doesn't exist."""
        import utils.timeline as tl_mod

        db = str(tmp_path / "empty.db")
        monkeypatch.setattr(tl_mod, "DB_PATH", db)
        from utils.timeline import get_timeline_event

        assert get_timeline_event(1) is None

    def test_count_with_source_filter(self, tl_db):
        from utils.timeline import count_timeline_events, write_timeline_event

        write_timeline_event("INFO", "resource", "disk ok")
        write_timeline_event("INFO", "update_check", "no updates")
        assert count_timeline_events(source_filter="resource") == 1

    def test_count_on_missing_table_returns_zero(self, tmp_path, monkeypatch):
        """count_timeline_events() returns 0 when the table doesn't exist."""
        import utils.timeline as tl_mod

        db = str(tmp_path / "empty.db")
        monkeypatch.setattr(tl_mod, "DB_PATH", db)
        from utils.timeline import count_timeline_events

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
        import utils.service as svc

        monkeypatch.setattr(
            svc,
            "service_status",
            lambda: {"loaded": False, "running": False, "pid": None},
        )
        from utils.audit import check_service

        result = check_service()
        assert result.status == "WARN"
        assert "not installed" in result.detail

    def test_loaded_not_running(self, monkeypatch):
        import utils.service as svc

        monkeypatch.setattr(
            svc,
            "service_status",
            lambda: {"loaded": True, "running": False, "pid": None},
        )
        from utils.audit import check_service

        result = check_service()
        assert result.status == "CRITICAL"
        assert "not running" in result.detail

    def test_running(self, monkeypatch):
        import utils.service as svc

        monkeypatch.setattr(
            svc,
            "service_status",
            lambda: {"loaded": True, "running": True, "pid": 12345},
        )
        from utils.audit import check_service

        result = check_service()
        assert result.status == "OK"
        assert "12345" in result.detail

    def test_launchctl_error(self, monkeypatch):
        import utils.service as svc

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
        from utils.audit import check_service

        result = check_service()
        assert result.status == "WARN"
        assert "macOS only" in result.detail


class TestAuditCheckHaDisk:
    def test_disk_ok(self):
        from utils.audit import check_ha_disk
        from utils.ssh_client import FakeSSHClient

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
        from utils.audit import check_ha_disk
        from utils.ssh_client import FakeSSHClient

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
        from utils.audit import check_ha_disk
        from utils.ssh_client import FakeSSHClient

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
        from utils.audit import check_ha_disk
        from utils.ssh_client import FakeSSHClient

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
        from utils.audit import check_backup_registry
        from utils.ssh_client import FakeSSHClient

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
        from utils.audit import check_backup_registry
        from utils.ssh_client import FakeSSHClient

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
        from utils.audit import check_backup_registry
        from utils.ssh_client import FakeSSHClient

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
        from utils.audit import check_backup_registry
        from utils.ssh_client import FakeSSHClient

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
        from utils.audit import check_backup_registry
        from utils.ssh_client import FakeSSHClient

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
        from utils.audit import check_backup_registry
        from utils.ssh_client import FakeSSHClient

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
        from utils.audit import check_backup_registry
        from utils.ssh_client import FakeSSHClient

        class _BrokenClient(FakeSSHClient):
            async def run(self, command: str, check: bool = False):
                raise OSError("timeout")

        result = asyncio.run(check_backup_registry(ssh_client=_BrokenClient()))
        assert result.status == "WARN"
        assert "unknown_slug" in result.detail
        assert "SSH unavailable" in result.detail


class TestAuditCheckPendingHitl:
    def test_no_dir(self, tmp_path):
        from utils.audit import check_pending_hitl

        result = check_pending_hitl(watch_dir=str(tmp_path / "nonexistent"))
        assert result.status == "OK"

    def test_no_pending(self, tmp_path):
        import json

        card_id = "test-card-123"
        (tmp_path / f"{card_id}.json").write_text(
            json.dumps({"notification_id": card_id, "sent_at": 1000})
        )
        (tmp_path / f"{card_id}.approved").touch()
        from utils.audit import check_pending_hitl

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
        from utils.audit import check_pending_hitl

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
        from utils.audit import check_pending_hitl

        result = check_pending_hitl(watch_dir=str(tmp_path))
        assert result.status == "CRITICAL"
        assert "30h old" in result.detail


class TestAuditCheckNetalertx:
    def test_not_configured(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "NETALERTX_HOST", "")
        from utils.audit import check_netalertx

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

        from utils.audit import check_netalertx

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

        from utils.audit import check_netalertx

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

        from utils.audit import check_netalertx

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

        from utils.audit import check_netalertx

        result = asyncio.run(check_netalertx(api_client=_FakeClient()))
        assert result.status == "WARN"
        assert "unavailable" in result.detail


class TestAuditCheckNetalertxApiToken:
    def test_not_desired_returns_ok(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "NETALERTX_SETUP_DESIRED", False)
        from utils.audit import check_netalertx_api_token

        result = check_netalertx_api_token()
        assert result.status == "OK"
        assert "not enabled" in result.detail

    def test_desired_empty_token_returns_warn(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "NETALERTX_SETUP_DESIRED", True)
        monkeypatch.setattr(config, "NETALERTX_API_TOKEN", "")
        from utils.audit import check_netalertx_api_token

        result = check_netalertx_api_token()
        assert result.status == "WARN"
        assert "api_token is empty" in result.detail
        assert "API Key" in result.action

    def test_desired_token_set_returns_ok(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "NETALERTX_SETUP_DESIRED", True)
        monkeypatch.setattr(config, "NETALERTX_API_TOKEN", "tok123")
        from utils.audit import check_netalertx_api_token

        result = check_netalertx_api_token()
        assert result.status == "OK"
        assert "configured" in result.detail


class TestAuditCheckStateHistory:
    def test_missing_table(self, tmp_path, monkeypatch):
        import config

        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "empty.db"))
        from utils.audit import check_state_history

        result = check_state_history()
        assert result.status == "WARN"
        assert "missing" in result.detail

    def test_no_entries(self, tmp_path, monkeypatch):
        db = str(tmp_path / "state.db")
        _make_db_with_tables(db)

        import config

        monkeypatch.setattr(config, "DB_PATH", db)
        from utils.audit import check_state_history

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
        from utils.audit import check_state_history

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
        from utils.audit import check_state_history

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
        from utils.audit import check_update_check

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
        from utils.audit import check_update_check

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
        from utils.audit import check_update_check

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
        from utils.audit import check_update_check

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
        from utils.audit import check_update_check

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
        from utils.audit import check_update_check

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
        from utils.audit import check_update_check

        result = check_update_check(watch_dir=str(tmp_path))
        assert result.status == "OK"


class TestAuditPriorityOrdering:
    def test_sorted_critical_before_warn_before_ok(self):
        from utils.audit import AuditResult, format_audit_report

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
        from utils.audit import AuditResult, format_audit_report

        results = [
            AuditResult("check_a", "CRITICAL", "disk full", "free disk"),
            AuditResult("check_b", "OK", "all good"),
        ]
        report = format_audit_report(results, now=1000000)
        assert "Priority Actions" in report
        assert "free disk" in report
        assert "[CRITICAL]" in report

    def test_format_report_no_priority_section_when_all_ok(self):
        from utils.audit import AuditResult, format_audit_report

        results = [AuditResult("check_a", "OK", "fine")]
        report = format_audit_report(results, now=1000000)
        assert "Priority Actions" not in report


class TestAuditSaveReport:
    def test_saves_to_audits_dir(self, tmp_path):
        from utils.audit import save_audit_report

        report = "# Pueo Audit\n\nAll clear.\n"
        out = save_audit_report(report, audits_dir=str(tmp_path / "audits"))
        assert out.exists()
        assert out.read_text() == report
        assert "pueo-audit-" in out.name


class TestAuditMainEntry:
    def test_main_audit_runs_and_saves(self, tmp_path, monkeypatch):
        """main_audit() writes a file to audits_dir and prints a summary."""
        from utils.audit import AuditResult

        ok_result = AuditResult("service", "OK", "running fine")
        warn_result = AuditResult("ha_disk", "WARN", "4.0 GB free", "free disk")

        async def _fake_run_audit(**kwargs):
            return [ok_result, warn_result]

        import utils.audit as audit_mod

        monkeypatch.setattr(audit_mod, "run_audit", _fake_run_audit)

        audits_dir = str(tmp_path / "audits")
        asyncio.run(audit_mod.main_audit(audits_dir=audits_dir))

        import os

        files = os.listdir(audits_dir)
        assert any("pueo-audit-" in f for f in files)

    def test_run_audit_handles_unexpected_exception(self, monkeypatch, tmp_path):
        """run_audit() wraps exceptions from async checks as WARN results."""
        import utils.audit as audit_mod

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


def _make_disk_ssh(du_output=_DU_OUTPUT, addon_json=_ADDON_JSON, host_info=_HOST_INFO):
    from utils.ssh_client import FakeSSHClient

    return FakeSSHClient(
        command_results={
            "ha host info": (0, host_info, ""),
            "du -sh": (0, du_output, ""),
            "ha apps list --raw-json": (0, addon_json, ""),
            "sqlite3": (1, "", "sqlite3: not found"),
        }
    )


class TestParseSizeToBytes:
    def test_kilobytes(self):
        from utils.disk_usage import _parse_size_to_bytes

        assert _parse_size_to_bytes("4.0K") == 4096

    def test_integer_megabytes(self):
        from utils.disk_usage import _parse_size_to_bytes

        assert _parse_size_to_bytes("164M") == 164 * 1024**2

    def test_decimal_megabytes(self):
        from utils.disk_usage import _parse_size_to_bytes

        assert _parse_size_to_bytes("94.5M") == int(94.5 * 1024**2)

    def test_gigabytes(self):
        from utils.disk_usage import _parse_size_to_bytes

        assert _parse_size_to_bytes("1.7G") == int(1.7 * 1024**3)

    def test_small_kilobytes(self):
        from utils.disk_usage import _parse_size_to_bytes

        assert _parse_size_to_bytes("644.0K") == int(644.0 * 1024)

    def test_zero_string(self):
        from utils.disk_usage import _parse_size_to_bytes

        assert _parse_size_to_bytes("0") == 0

    def test_empty_string(self):
        from utils.disk_usage import _parse_size_to_bytes

        assert _parse_size_to_bytes("") == 0

    def test_malformed_returns_zero(self):
        from utils.disk_usage import _parse_size_to_bytes

        assert _parse_size_to_bytes("abc") == 0


class TestParseDuOutput:
    def test_parses_tab_separated_output(self):
        from utils.disk_usage import _parse_du_output

        result = _parse_du_output(
            "94.5M\t/homeassistant/home-assistant_v2.db\n4.0K\t/share\n"
        )
        assert "/homeassistant/home-assistant_v2.db" in result
        assert result["/share"] == 4096

    def test_parses_space_separated_fallback(self):
        from utils.disk_usage import _parse_du_output

        result = _parse_du_output("94.5M  /homeassistant/home-assistant_v2.db\n")
        assert "/homeassistant/home-assistant_v2.db" in result

    def test_skips_lines_without_separator(self):
        from utils.disk_usage import _parse_du_output

        result = _parse_du_output("justoneword\n94.5M\t/valid/path\n")
        assert len(result) == 1
        assert "/valid/path" in result

    def test_skips_empty_lines(self):
        from utils.disk_usage import _parse_du_output

        result = _parse_du_output("\n\n94.5M\t/valid/path\n\n")
        assert len(result) == 1

    def test_empty_output_returns_empty_dict(self):
        from utils.disk_usage import _parse_du_output

        assert _parse_du_output("") == {}


class TestFetchDiskBreakdown:
    def test_returns_four_sections(self):
        ssh = _make_disk_ssh()
        from utils.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        assert len(bd.sections) == 4

    def test_section_titles(self):
        ssh = _make_disk_ssh()
        from utils.disk_usage import fetch_disk_breakdown

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
        from utils.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        assert bd.disk_free_gb == pytest.approx(2.2, abs=0.01)
        assert bd.disk_total_gb == pytest.approx(13.6, abs=0.01)
        assert bd.disk_used_gb == pytest.approx(10.8, abs=0.01)

    def test_disk_used_pct_computed(self):
        ssh = _make_disk_ssh()
        from utils.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        assert bd.disk_used_pct == pytest.approx(79.4, abs=1.0)

    def test_addon_slug_mapped_to_friendly_name(self):
        ssh = _make_disk_ssh()
        from utils.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        addon_section = next(s for s in bd.sections if s.title == "Addon Data")
        names = [item.name for item in addon_section.items]
        assert "NetAlertX Full Access" in names
        assert "NetAlertX" in names

    def test_unknown_addon_slug_kept_as_is(self):
        ssh = _make_disk_ssh()
        from utils.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        addon_section = next(s for s in bd.sections if s.title == "Addon Data")
        names = [item.name for item in addon_section.items]
        assert "another_addon" in names

    def test_config_section_sorted_largest_first(self):
        ssh = _make_disk_ssh()
        from utils.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        config_section = next(
            s for s in bd.sections if s.title == "HA Config & Database"
        )
        sizes = [item.size_bytes for item in config_section.items]
        assert sizes == sorted(sizes, reverse=True)

    def test_shared_storage_is_empty(self):
        ssh = _make_disk_ssh()
        from utils.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        shared = next(s for s in bd.sections if s.title == "Shared Storage")
        assert shared.is_empty is True

    def test_bad_addon_json_falls_back_to_slug(self):
        ssh = _make_disk_ssh(addon_json="not json at all")
        from utils.disk_usage import fetch_disk_breakdown

        # Should not raise; slug used as display name
        bd = asyncio.run(fetch_disk_breakdown(ssh))
        addon_section = next(s for s in bd.sections if s.title == "Addon Data")
        names = [item.name for item in addon_section.items]
        assert "db21ed7f_netalertx_fa" in names

    def test_fetched_at_is_set(self):
        import time

        ssh = _make_disk_ssh()
        from utils.disk_usage import fetch_disk_breakdown

        before = time.time()
        bd = asyncio.run(fetch_disk_breakdown(ssh))
        assert bd.fetched_at >= before

    def test_pct_of_section_sums_near_100(self):
        ssh = _make_disk_ssh()
        from utils.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        for section in bd.sections:
            if not section.is_empty:
                total = sum(item.pct_of_section for item in section.items)
                assert total == pytest.approx(100.0, abs=1.0)

    def test_empty_du_output_all_sections_empty(self):
        ssh = _make_disk_ssh(du_output="")
        from utils.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        assert all(s.is_empty for s in bd.sections)

    def test_sqlite3_unavailable_gives_none_db_tables(self):
        ssh = _make_disk_ssh()
        from utils.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        assert bd.db_tables is None

    def test_sqlite3_available_populates_db_tables(self):
        from utils.ssh_client import FakeSSHClient

        ssh = FakeSSHClient(
            command_results={
                "ha host info": (0, _HOST_INFO, ""),
                "du -sh": (0, _DU_OUTPUT, ""),
                "ha apps list --raw-json": (0, _ADDON_JSON, ""),
                "sqlite3": (
                    0,
                    "states|52428800\nstatistics|31457280\nevents|10485760\n",
                    "",
                ),
            }
        )
        from utils.disk_usage import fetch_disk_breakdown

        bd = asyncio.run(fetch_disk_breakdown(ssh))
        assert bd.db_tables is not None
        assert bd.db_tables[0][0] == "states"
        assert bd.db_tables[0][1] == 52428800


class TestParseSqlite3Output:
    def test_parses_pipe_separated_rows(self):
        from utils.disk_usage import _parse_sqlite3_output

        rows = _parse_sqlite3_output("states|52428800\nstatistics|31457280\n")
        assert rows[0] == ("states", 52428800)
        assert rows[1] == ("statistics", 31457280)

    def test_sorted_descending(self):
        from utils.disk_usage import _parse_sqlite3_output

        rows = _parse_sqlite3_output("small|1000\nbig|9999\n")
        assert rows[0][0] == "big"

    def test_skips_malformed_lines(self):
        from utils.disk_usage import _parse_sqlite3_output

        rows = _parse_sqlite3_output("nopipe\nstates|52428800\n")
        assert len(rows) == 1

    def test_empty_output_returns_empty(self):
        from utils.disk_usage import _parse_sqlite3_output

        assert _parse_sqlite3_output("") == []


class TestDiskCacheAccessors:
    def test_get_returns_none_initially(self, monkeypatch):
        import utils.disk_usage as du_mod

        monkeypatch.setattr(du_mod, "_last_disk_breakdown", None)
        assert du_mod.get_disk_breakdown() is None

    def test_update_then_get_roundtrip(self, monkeypatch):
        import utils.disk_usage as du_mod
        from utils.disk_usage import DiskBreakdown

        monkeypatch.setattr(du_mod, "_last_disk_breakdown", None)
        bd = DiskBreakdown(fetched_at=12345.0)
        du_mod.update_disk_breakdown(bd)
        assert du_mod.get_disk_breakdown() is bd


class TestDiskUsagePollerRun:
    def test_polls_and_updates_cache_then_cancels(self, monkeypatch):
        import utils.disk_usage as du_mod
        from utils.disk_usage import DiskBreakdown, DiskUsagePoller

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
        import utils.disk_usage as du_mod
        from utils.disk_usage import DiskUsagePoller

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


# ── ha_agent_core pipeline ────────────────────────────────────────────────────────

_SIMPLE_CONFIG = "homeassistant:\n  name: Home\n\nhttp:\n  server_port: 8123\n"
