"""Tests for utils/core/logging.py — set_log_level helper and reserved kwarg guard."""

import logging

import pytest


class TestSetLogLevel:
    def test_set_debug_changes_effective_level(self):
        from utils.core.logging import set_log_level

        logger = logging.getLogger("pueo")
        original = logger.level
        try:
            set_log_level("DEBUG")
            assert logger.isEnabledFor(logging.DEBUG)
        finally:
            logger.setLevel(original)

    def test_set_info_restores_info_level(self):
        from utils.core.logging import set_log_level

        logger = logging.getLogger("pueo")
        original = logger.level
        try:
            set_log_level("DEBUG")
            set_log_level("INFO")
            assert logger.isEnabledFor(logging.INFO)
            assert not logger.isEnabledFor(logging.DEBUG)
        finally:
            logger.setLevel(original)

    def test_invalid_level_falls_back_to_info(self):
        from utils.core.logging import set_log_level

        logger = logging.getLogger("pueo")
        original = logger.level
        try:
            set_log_level("NOTAVALIDLEVEL")
            assert logger.level == logging.INFO
        finally:
            logger.setLevel(original)


class TestStructuredLoggerReservedKwargs:
    """StructuredLogger must not pass reserved LogRecord attrs through extra=."""

    def _make_capturing_logger(self):
        from utils.core.logging import StructuredLogger

        records = []

        class _CapturingHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        inner = logging.getLogger("pueo.test_reserved_kwargs")
        inner.handlers = []
        inner.propagate = False
        inner.setLevel(logging.DEBUG)
        handler = _CapturingHandler()
        inner.addHandler(handler)
        return StructuredLogger(inner), records

    def test_exc_info_true_is_forwarded_as_tuple(self):
        """exc_info=True must be passed to Logger.log() directly so the stdlib
        captures the current exception traceback into LogRecord.exc_info as a tuple."""
        log, records = self._make_capturing_logger()
        try:
            raise ValueError("test error")
        except ValueError:
            log.error("some_event", exc_info=True)
        assert len(records) == 1
        ei = records[0].exc_info
        assert isinstance(ei, tuple) and len(ei) == 3
        assert ei[0] is ValueError

    def test_exc_info_not_in_extra_fields(self):
        """exc_info= must not appear as a structured extra field in the record."""
        log, records = self._make_capturing_logger()
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            log.error("another_event", exc_info=True)
        record = records[0]
        from utils.core.logging import _STANDARD_ATTRS

        extra_keys = {
            k
            for k in record.__dict__
            if k not in _STANDARD_ATTRS and not k.startswith("_")
        }
        assert "exc_info" not in extra_keys

    def test_tool_args_kwarg_does_not_crash(self):
        """tool_args= (the renamed arg) must not raise — it is not a reserved name."""
        log, records = self._make_capturing_logger()
        log.debug(
            "tool_execute_args", tool="read_config", tool_args={"path": "/config"}
        )
        assert len(records) == 1
        assert records[0].__dict__.get("tool_args") == {"path": "/config"}
