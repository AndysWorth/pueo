"""Anonymize episode text for safe export (Phase 19, item 79).

One Anonymizer instance per episode ensures consistent placeholder mapping
within the episode. Counters reset across episodes in the same export batch.
"""

from __future__ import annotations

import json
import re
from typing import Any

_IPV4_RE = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")
_IPV6_RE = re.compile(
    r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b"
    r"|::(?:[0-9a-fA-F]{1,4}:){0,6}[0-9a-fA-F]{1,4}\b"
)
_SLUG_RE = re.compile(r"\b([0-9a-f]{8})\b")
_CONFIG_PATH_RE = re.compile(r"(/config/(?:[^/\s]+/)+)([^/\s]+)")


class Anonymizer:
    """State-tracking anonymizer for a single episode."""

    def __init__(self) -> None:
        self._hosts: dict[str, str] = {}
        self._slugs: dict[str, str] = {}
        self._n_hosts = 0
        self._n_slugs = 0

    def _host_for(self, addr: str) -> str:
        if addr not in self._hosts:
            self._n_hosts += 1
            self._hosts[addr] = f"<host_{self._n_hosts}>"
        return self._hosts[addr]

    def _slug_for(self, slug: str) -> str:
        if slug not in self._slugs:
            self._n_slugs += 1
            self._slugs[slug] = f"<slug_{self._n_slugs}>"
        return self._slugs[slug]

    def text(self, value: str) -> str:
        """Anonymize a single string."""
        if not value:
            return value
        value = _IPV4_RE.sub(lambda m: self._host_for(m.group(1)), value)
        value = _IPV6_RE.sub(lambda m: self._host_for(m.group()), value)
        value = _SLUG_RE.sub(lambda m: self._slug_for(m.group(1)), value)
        # Preserve /config/ directory structure; anonymize the filename
        value = _CONFIG_PATH_RE.sub(lambda m: m.group(1) + "<file>", value)
        return value

    def args(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Anonymize tool call arguments (shallow string values only)."""
        serialized = self.text(json.dumps(arguments))
        try:
            return dict(json.loads(serialized))  # type: ignore[arg-type]
        except (json.JSONDecodeError, ValueError):
            return arguments
