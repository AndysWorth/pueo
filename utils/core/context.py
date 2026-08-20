"""Token budget utilities for keeping prompts within model context limits."""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """Character-based token estimate (4 chars ≈ 1 token). Fast, dependency-free."""
    return max(1, len(text) // 4)


def truncate_to_budget(text: str, max_tokens: int, strategy: str = "tail") -> str:
    """Return text truncated to fit within max_tokens.

    Strategies: "head" keeps the start, "tail" keeps the end (default; good for
    recent logs), "smart" keeps first quarter + last three quarters with a separator
    (good for config files where both start and end matter).
    """
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    if strategy == "head":
        return text[:max_chars]
    elif strategy == "tail":
        return text[-max_chars:]
    else:  # "smart"
        separator = "\n...[truncated]...\n"
        head_chars = max_chars // 4
        tail_chars = max_chars - head_chars - len(separator)
        return text[:head_chars] + separator + text[-tail_chars:]


def sliding_window_lines(lines: list[str], max_tokens: int) -> list[str]:
    """Return the most recent lines that fit within max_tokens (tail-priority)."""
    max_chars = max_tokens * 4
    total = 0
    result: list[str] = []
    for line in reversed(lines):
        length = len(line) + 1  # +1 for newline
        if total + length > max_chars:
            break
        result.append(line)
        total += length
    return list(reversed(result))
