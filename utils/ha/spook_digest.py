"""Deterministic payload builder for known Spook cleanup digest cards.

Known cleanup keys (empty_areas, unused_labels, unused_blueprints,
orphaned_statistics) are fully self-describing from their
translation_placeholders — no LLM investigation is needed.

build_spook_digest_payload() returns a CARD_TYPE_SPOOK_DIGEST payload for
these keys, or None for unknown keys that should go through the LLM digest path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from utils.ha.ha_rest_client import HARepairIssue

# Keys whose placeholders fully describe the cleanup — no LLM needed.
KNOWN_CLEANUP_KEYS: frozenset[str] = frozenset(
    {"empty_areas", "unused_labels", "unused_blueprints", "orphaned_statistics"}
)

# Which placeholder field holds the human-readable item name for each key.
_PLACEHOLDER_LABEL_FIELD: dict[str, str] = {
    "empty_areas": "area",
    "unused_labels": "label",
    "unused_blueprints": "blueprint",
    "orphaned_statistics": "entity_id",
}

# Short description of what each key means (used in the card body).
_KEY_DESCRIPTION: dict[str, str] = {
    "empty_areas": "Empty area — no entities are assigned to it",
    "unused_labels": "Unused label — not assigned to any entity",
    "unused_blueprints": "Unused blueprint — no automation uses it",
    "orphaned_statistics": "Orphaned statistics — sensor no longer exists",
}


def build_spook_digest_payload(
    tkey: str,
    issues: "list[HARepairIssue]",
) -> Optional[dict]:
    """Build a CARD_TYPE_SPOOK_DIGEST payload for a known cleanup key.

    Returns None when tkey is not in KNOWN_CLEANUP_KEYS; callers must fall
    back to the LLM investigation path in that case.

    Each item in the payload carries the issue_id, a human-readable label
    from translation_placeholders, is_fixable, and learn_more_url.
    """
    if tkey not in KNOWN_CLEANUP_KEYS:
        return None

    from utils.hitl.card_types import CARD_TYPE_SPOOK_DIGEST
    from utils.hitl.hitl_tracker import stable_nid

    suppression_key = f"spook/digest:{tkey}"
    label_field = _PLACEHOLDER_LABEL_FIELD.get(tkey, "")

    items = []
    for issue in issues:
        placeholders: dict = issue.data.get("translation_placeholders") or {}
        label = placeholders.get(label_field) or issue.issue_id
        items.append(
            {
                "issue_id": issue.issue_id,
                "label": label,
                "is_fixable": bool(issue.data.get("is_fixable", True)),
                "learn_more_url": issue.data.get("learn_more_url") or "",
                "placeholders": placeholders,
            }
        )

    description = _KEY_DESCRIPTION.get(tkey, f"Spook: {tkey}")
    body_lines = [f"Spook repair — {tkey} ({len(items)} item(s)):"]
    for item in items:
        fix_note = "Spook can fix" if item["is_fixable"] else "manual action needed"
        body_lines.append(f"  • {item['label']} — {fix_note}")

    return {
        "notification_id": stable_nid(suppression_key),
        "card_type": CARD_TYPE_SPOOK_DIGEST,
        "suppression_key": suppression_key,
        "translation_key": tkey,
        "description": description,
        "items": items,
        "body": "\n".join(body_lines),
    }
