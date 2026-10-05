"""Validate LLM-proposed YAML fixes before any write operations."""

import difflib
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ValidationResult:
    is_safe: bool
    reasons: list[str] = field(default_factory=list)


def load_deprecated_keys(cache_dir: str) -> list[dict]:
    """Load deprecated YAML keys from the skills cache JSON.

    Returns an empty list if the file is absent — validate_proposed_fix() stays
    safe without a rag-refresh having run first.
    """
    path = Path(cache_dir) / "deprecated_keys.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _scan_deprecated_keys(node: object, deprecated: list[dict]) -> list[str]:
    """Recursively scan a parsed YAML structure for deprecated key names.

    Returns a list of human-readable violation strings.
    """
    if not deprecated:
        return []
    dep_map = {entry["key"]: entry for entry in deprecated}
    violations: list[str] = []
    _scan(node, dep_map, violations)
    return violations


def _scan(node: object, dep_map: dict, violations: list[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and k in dep_map:
                entry = dep_map[k]
                violations.append(
                    f"deprecated key '{k}' (removed in {entry['removed_version']})"
                )
            _scan(v, dep_map, violations)
    elif isinstance(node, list):
        for item in node:
            _scan(item, dep_map, violations)


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


def validate_proposed_fix(
    original_yaml: str,
    proposed_yaml: str,
    deprecated_keys: list[dict] | None = None,
) -> ValidationResult:
    """Check that a proposed YAML fix is structurally safe before deployment.

    Pass deprecated_keys (from load_deprecated_keys()) to also reject YAML that
    uses removed HA API keys.  When omitted, the deprecated-key check is skipped.
    """
    proposed, early_reasons = _check_parse_and_structure(proposed_yaml)
    if early_reasons:
        return ValidationResult(is_safe=False, reasons=early_reasons)

    if proposed is None:
        return ValidationResult(is_safe=False, reasons=["YAML parsed to None"])
    reasons = _check_content(original_yaml, proposed, proposed_yaml)
    if deprecated_keys:
        reasons.extend(_scan_deprecated_keys(proposed, deprecated_keys))
    return ValidationResult(is_safe=len(reasons) == 0, reasons=reasons)
