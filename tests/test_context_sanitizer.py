"""Tests for utils/ha/context_sanitizer.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.ha.context_sanitizer import sanitize_update_context


class TestSanitizeUpdateContext:
    def test_redacts_bearer_token(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc"
        result = sanitize_update_context(text)
        assert "Bearer <REDACTED>" in result
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result

    def test_redacts_api_token_yaml(self):
        text = "api_token: supersecretvalue123"
        result = sanitize_update_context(text)
        assert "<REDACTED>" in result
        assert "supersecretvalue123" not in result

    def test_redacts_password_yaml(self):
        text = "password: mypassword"
        result = sanitize_update_context(text)
        assert "<REDACTED>" in result
        assert "mypassword" not in result

    def test_redacts_api_key(self):
        text = "api_key=abc123def456"
        result = sanitize_update_context(text)
        assert "<REDACTED>" in result
        assert "abc123def456" not in result

    def test_preserves_normal_yaml(self):
        text = "component: core\nversion: 2026.9.0\nrisk: low"
        result = sanitize_update_context(text)
        assert result == text

    def test_idempotent(self):
        text = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.longtoken"
        once = sanitize_update_context(text)
        twice = sanitize_update_context(once)
        assert once == twice

    def test_empty_string(self):
        assert sanitize_update_context("") == ""

    def test_bearer_too_short_not_redacted(self):
        # Bearer token shorter than 20 chars is left as-is (unlikely a real token)
        text = "Bearer short"
        result = sanitize_update_context(text)
        assert result == text
