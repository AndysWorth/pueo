"""Validate LLM-proposed YAML fixes before any write operations."""

import difflib
from dataclasses import dataclass, field

import yaml


@dataclass
class ValidationResult:
    is_safe: bool
    reasons: list[str] = field(default_factory=list)


def _check_parse_and_structure(proposed_yaml: str) -> tuple[dict | None, list[str]]:
    """Return (parsed_dict_or_None, reasons). Early-exit failures return None."""
    if not proposed_yaml or not proposed_yaml.strip():
        return None, ["proposed YAML is empty"]
    try:
        parsed = yaml.safe_load(proposed_yaml)
    except yaml.YAMLError as exc:
        return None, [f"proposed YAML does not parse: {exc}"]
    if not isinstance(parsed, dict):
        return None, ["proposed YAML is not a mapping at the top level"]
    return parsed, []


def _check_content(original_yaml: str, proposed: dict, proposed_yaml: str) -> list[str]:
    """Check homeassistant block, key removal, and similarity threshold."""
    reasons: list[str] = []

    if "homeassistant" not in proposed:
        reasons.append("proposed YAML is missing the 'homeassistant:' block")

    try:
        original = yaml.safe_load(original_yaml)
    except yaml.YAMLError:
        original = {}

    if isinstance(original, dict):
        removed_keys = set(original.keys()) - set(proposed.keys())
        if removed_keys:
            reasons.append(
                f"top-level keys removed from original: {', '.join(sorted(removed_keys))}"
            )

    original_lines = original_yaml.splitlines()
    proposed_lines = proposed_yaml.splitlines()
    if original_lines and proposed_lines:
        similarity = difflib.SequenceMatcher(
            None, original_lines, proposed_lines
        ).ratio()
        if similarity < 0.2:
            reasons.append(
                f"proposed YAML differs too much from original "
                f"(similarity {similarity:.0%}, threshold 20%)"
            )

    return reasons


def validate_proposed_fix(original_yaml: str, proposed_yaml: str) -> ValidationResult:
    """Check that a proposed YAML fix is structurally safe before deployment."""
    proposed, early_reasons = _check_parse_and_structure(proposed_yaml)
    if early_reasons:
        return ValidationResult(is_safe=False, reasons=early_reasons)

    assert proposed is not None
    reasons = _check_content(original_yaml, proposed, proposed_yaml)
    return ValidationResult(is_safe=len(reasons) == 0, reasons=reasons)
