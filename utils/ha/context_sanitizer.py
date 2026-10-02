"""Redact sensitive values from text before LLM dispatch.

Pure function — no state, no side effects. Safe to call multiple times (idempotent).
"""

import re

_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}")
_SECRET_KEY_RE = re.compile(
    r"(api_token|password|secret_key|api_key|access_token)\s*[:=]\s*\S+",
    re.IGNORECASE,
)


def sanitize_update_context(text: str) -> str:
    """Redact Bearer tokens and YAML secret-key values."""
    text = _BEARER_RE.sub("Bearer <REDACTED>", text)
    text = _SECRET_KEY_RE.sub(lambda m: f"{m.group(1)}: <REDACTED>", text)
    return text
