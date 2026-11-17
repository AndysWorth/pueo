"""Deterministic OS-release applicability — parses board headings from release notes."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from interfaces import SSHClientProtocol

# Heading patterns that indicate a board-specific section.
# Keys are canonical board IDs returned by ha os info --raw-json data.board.
# Values are case-insensitive patterns that identify the board in notes.
_BOARD_PATTERNS: dict[str, list[str]] = {
    "yellow": ["home assistant yellow", "homeassistant yellow", "yellow"],
    "green": ["home assistant green", "homeassistant green", "green"],
    # rpi* entries share the generic "raspberry pi" alias so plain headings match.
    "rpi5": ["raspberry pi 5", "rpi5", "pi 5", "raspberry pi"],
    "rpi4": ["raspberry pi 4", "rpi4", "pi 4", "raspberry pi"],
    "rpi3": ["raspberry pi 3", "rpi3", "pi 3", "raspberry pi"],
    "odroid-n2": ["odroid-n2", "odroid n2", "odroid"],
    "odroid-m1": ["odroid-m1", "odroid m1", "odroid"],
    "khadas-vim3": ["khadas vim3", "vim3"],
    "generic-x86-64": ["generic x86", "x86-64", "generic-x86"],
    "tinker": ["tinker board", "asus tinker"],
}

# Pattern: "Only for X" or "X only" in a heading.
# Used to detect board-specific-only sections.
_ONLY_RE = re.compile(r"\b(\w[\w\s\-]+?)\s+only\b", re.IGNORECASE)


def _board_aliases(board: str) -> list[str]:
    """Return all aliases for a board id (lower-case)."""
    b = board.lower().strip()
    for canonical, aliases in _BOARD_PATTERNS.items():
        if b == canonical or b in aliases:
            return [canonical] + aliases
    # Unknown board: use the raw value as its own alias.
    return [b]


def os_release_applicability(notes: str, board: str) -> dict:
    """Parse board headings from OS release notes to determine applicability.

    Returns a dict with:
      - applies: "yes" | "no" | "unknown"
      - reason: human-readable explanation
      - board_sections: list of heading strings that mention this board
      - other_board_sections: list of board-specific headings for OTHER boards
    """
    if not notes or not board:
        return {
            "applies": "unknown",
            "reason": "No release notes or board identifier available.",
            "board_sections": [],
            "other_board_sections": [],
        }

    aliases = _board_aliases(board)
    lines = notes.splitlines()

    # Collect all level-2/3 headings (## / ###)
    headings: list[str] = [
        ln.lstrip("# ").strip() for ln in lines if re.match(r"^#{1,4}\s", ln)
    ]

    board_sections: list[str] = []
    other_board_sections: list[str] = []
    all_board_aliases = {
        alias for aliases_list in _BOARD_PATTERNS.values() for alias in aliases_list
    }

    for h in headings:
        h_lower = h.lower()
        # Check if this heading names this board
        if any(alias in h_lower for alias in aliases):
            board_sections.append(h)
            continue
        # Check if heading names a *different* board
        matched_other = False
        for _canonical, _aliases in _BOARD_PATTERNS.items():
            if _canonical.lower() not in [a.lower() for a in aliases]:
                if any(a in h_lower for a in _aliases):
                    other_board_sections.append(h)
                    matched_other = True
                    break
        # Check "X only" patterns in body lines under this heading (not needed for
        # basic applicability, but we capture headings that contain "only")
        if not matched_other:
            m = _ONLY_RE.search(h)
            if m:
                qualifier = m.group(1).lower()
                # If the qualifier doesn't name our board, it's another board.
                if not any(alias in qualifier for alias in aliases):
                    # Check if qualifier names any known board
                    if any(a in qualifier for a in all_board_aliases):
                        other_board_sections.append(h)

    if board_sections:
        return {
            "applies": "yes",
            "reason": f"Release notes include a section for {board}: {board_sections[0]}",
            "board_sections": board_sections,
            "other_board_sections": other_board_sections,
        }

    if other_board_sections and not board_sections:
        # Notes have board-specific sections, but none for this board.
        return {
            "applies": "no",
            "reason": (
                f"Release notes have board-specific sections "
                f"({', '.join(other_board_sections[:2])}) but none for {board}."
            ),
            "board_sections": [],
            "other_board_sections": other_board_sections,
        }

    # No board-specific sections at all — applies to all boards.
    return {
        "applies": "yes",
        "reason": "Release notes contain no board-specific sections — applies to all boards.",
        "board_sections": [],
        "other_board_sections": [],
    }


async def fetch_os_board_context(
    ssh_client: "SSHClientProtocol",
) -> Optional[dict]:
    """Fetch board identifier and kernel version via SSH.

    Returns dict with keys: board, kernel, raw_cpe or None on failure.
    board comes from ha os info --raw-json data.board.
    kernel comes from ha host info --raw-json data.kernel.
    """
    result: dict = {}
    try:
        _, stdout, _ = await ssh_client.run("ha os info --raw-json")
        data = json.loads(stdout.strip()).get("data", {})
        board = str(data.get("board", "")).strip()
        if board:
            result["board"] = board
    except Exception:  # nosec B110
        pass

    try:
        _, stdout, _ = await ssh_client.run("ha host info --raw-json")
        data = json.loads(stdout.strip()).get("data", {})
        kernel = str(data.get("kernel", "")).strip()
        if kernel:
            result["kernel"] = kernel
        # cpe contains the board type too; keep for reference
        cpe = str(data.get("cpe", "")).strip()
        if cpe:
            result["raw_cpe"] = cpe
    except Exception:  # nosec B110
        pass

    return result if result else None
