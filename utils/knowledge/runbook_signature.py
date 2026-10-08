"""Deterministic symptom-signature functions for runbook keying.

A signature identifies the symptom class of an AgentLoop session — independent of which
specific entity or error message triggered it.  Signatures are the single key for
runbook dedup, gap merging, usage stats, and resolution.

Grammar:  <type> ":" <part1> [ ":" <part2> ]

All public functions are pure and have no side effects.
"""

import re
from typing import Optional

_MAX_PART_LEN = 64


def _part(s: Optional[str]) -> str:
    """Sanitize one signature component (no colons).

    - Lowercase
    - Spaces and slashes → underscore
    - Strip everything except [a-z0-9_]
    - Truncate to _MAX_PART_LEN
    - Empty / None → "unknown"
    """
    if not s:
        return "unknown"
    s = s.lower()
    s = s.replace(" ", "_").replace("/", "_").replace(".", "_")
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = s[:_MAX_PART_LEN]
    return s or "unknown"


def _sanitize(s: Optional[str]) -> str:
    """Sanitize a full signature string (colons preserved as separators).

    Used for sig_chat where *s* may already be a composed signature.
    """
    if not s:
        return "unknown"
    s = s.lower()
    s = s.replace(" ", "_").replace("/", "_")
    s = re.sub(r"[^a-z0-9_:]", "", s)
    return s[: _MAX_PART_LEN * 3] or "unknown"


def sig_repair(domain: str, translation_key: str) -> str:
    """repair:<domain>:<translation_key>"""
    return f"repair:{_part(domain)}:{_part(translation_key)}"


def sig_update(update_type: str, slug: str) -> str:
    """update:<core|os|addon|hacs>:<slug>"""
    return f"update:{_part(update_type)}:{_part(slug)}"


def sig_log(logger: str, exception_class: Optional[str]) -> str:
    """log:<logger>:<exception_class>  (exception_class defaults to "none")"""
    return f"log:{_part(logger)}:{_part(exception_class or 'none')}"


def sig_lovelace(entity_domain: str, failure_kind: str) -> str:
    """lovelace:<entity_domain>:<failure_kind>"""
    return f"lovelace:{_part(entity_domain)}:{_part(failure_kind)}"


def sig_notification(notification_id: str) -> str:
    """notification:<id_prefix>  (prefix = portion before first '.' or '_')"""
    if not notification_id:
        return "notification:unknown"
    prefix = re.split(r"[._]", notification_id)[0]
    return f"notification:{_part(prefix)}"


def sig_chat(top_sig: Optional[str]) -> str:
    """chat:<top retrieved runbook's signature>  or  chat:unclassified"""
    if top_sig:
        return f"chat:{_sanitize(top_sig)}"
    return "chat:unclassified"
