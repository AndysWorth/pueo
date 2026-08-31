"""Lovelace dashboard utilities shared between ha_lovelace_monitor and chat tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class EntityRef:
    entity_id: str
    view_title: str
    card_index: int
    path: str
    view_path: str = ""
    dashboard_url_path: Optional[str] = None
    dashboard_title: str = ""
    card_title: str = ""


def _extract_entity_refs(
    lovelace_cfg: dict,
    *,
    dashboard_url_path: Optional[str] = None,
    dashboard_title: str = "",
) -> list[EntityRef]:
    """Walk the Lovelace card tree and return all entity_id references."""
    refs: dict[str, EntityRef] = {}  # deduplicate by entity_id

    def _add(
        entity_id: str,
        view_title: str,
        card_index: int,
        path: str,
        view_path: str,
        card_title: str = "",
    ) -> None:
        if not entity_id or not isinstance(entity_id, str):
            return
        if entity_id not in refs:
            refs[entity_id] = EntityRef(
                entity_id=entity_id,
                view_title=view_title,
                card_index=card_index,
                path=path,
                view_path=view_path,
                dashboard_url_path=dashboard_url_path,
                dashboard_title=dashboard_title,
                card_title=card_title,
            )

    def _walk_entity(
        val: object,
        view_title: str,
        card_index: int,
        path: str,
        view_path: str,
        card_title: str = "",
    ) -> None:
        if isinstance(val, str):
            _add(val, view_title, card_index, path, view_path, card_title)
        elif isinstance(val, dict):
            _add(
                val.get("entity") or val.get("entity_id") or "",
                view_title,
                card_index,
                path,
                view_path,
                card_title,
            )

    def _walk_card(
        card: dict, view_title: str, card_index: int, view_path: str, depth: int = 0
    ) -> None:
        if not isinstance(card, dict):
            return
        card_title: str = card.get("title", "") or ""
        prefix = f"card[{card_index}]"
        if depth > 0:
            prefix += f".nested[{depth}]"

        entity = card.get("entity") or card.get("entity_id") or ""
        if entity:
            _add(
                entity,
                view_title,
                card_index,
                prefix + ".entity",
                view_path,
                card_title,
            )

        entities = card.get("entities", [])
        if isinstance(entities, list):
            for ent in entities:
                _walk_entity(
                    ent,
                    view_title,
                    card_index,
                    prefix + ".entities",
                    view_path,
                    card_title,
                )

        for nested in card.get("cards", []):
            _walk_card(nested, view_title, card_index, view_path, depth + 1)

    for view in lovelace_cfg.get("views", []):
        if not isinstance(view, dict):
            continue
        view_title: str = str(view.get("title") or view.get("path") or "unnamed")
        view_path_val: str = str(view.get("path") or "")

        for badge in view.get("badges", []):
            _walk_entity(badge, view_title, -1, "badges", view_path_val)

        for idx, card in enumerate(view.get("cards", [])):
            _walk_card(card, view_title, idx, view_path_val)

        for section in view.get("sections", []):
            if not isinstance(section, dict):
                continue
            for card_idx, card in enumerate(section.get("cards", [])):
                _walk_card(card, view_title, card_idx, view_path_val)

    return list(refs.values())


def _fuzzy_candidates(
    entity_id: str, registry_ids: set[str], max_results: int = 5
) -> list[str]:
    """Return registry entity IDs closest to the missing one by simple edit distance."""
    domain = entity_id.split(".")[0] if "." in entity_id else ""
    suffix = entity_id.split(".", 1)[1] if "." in entity_id else entity_id

    same_domain = [r for r in registry_ids if r.startswith(f"{domain}.")]

    def _dist(a: str, b: str) -> int:
        la, lb = len(a), len(b)
        if abs(la - lb) > 20:
            return 999
        prev = list(range(lb + 1))
        for i, ca in enumerate(a):
            curr = [i + 1]
            for j, cb in enumerate(b):
                curr.append(
                    min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (0 if ca == cb else 1))
                )
            prev = curr
        return prev[lb]

    ranked = sorted(same_domain, key=lambda r: _dist(suffix, r.split(".", 1)[-1]))
    return ranked[:max_results]
