"""Tests for utils/core/logging.py — set_log_level helper."""

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
