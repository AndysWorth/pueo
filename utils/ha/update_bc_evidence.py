"""Per-BC entity and configuration evidence gatherer for Core update analysis.

Called once per Core release, after the release report is built.  Pure-ish:
no LLM calls, all I/O is read-only.  Clients may be None — each data source
degrades gracefully when its client is unavailable.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any, Optional

from utils.core.logging import get_logger

log = get_logger("update_bc_evidence")

_MAX_ENTITY_EXAMPLES = 15
_MAX_DASHBOARD_HITS = 20
_MAX_AUTOMATION_HITS = 20
_MAX_TOKENS = 20

# YAML config paths to grep for token search (mirrors tool_executor._BC_AUTOMATION_SEARCH_PATHS)
_AUTOMATION_PATHS = (
    "/config/automations.yaml",
    "/config/automations",
    "/config/scripts.yaml",
    "/config/scripts",
    "/config/packages",
)
_EXCLUDE_WORDS = ("secret", "credential", "token", "password", "passwd")

# Token extraction patterns from BC body
_SERVICE_CALL_RE = re.compile(r"\b([a-z_]{2,})\.([a-z_]{2,})\b")
_YAML_KEY_RE = re.compile(r"\b([a-z][a-z]*_[a-z_]+)\b")
_QUOTED_VALUE_RE = re.compile(r'"([^"]{2,25})"')
_CARD_TYPE_RE = re.compile(r"\b([a-z]+)\s+card\b", re.IGNORECASE)
_TEMPLATE_FN_RE = re.compile(r"\b([a-z_]{5,})\s*\(")

# YAML keys too generic to search for usefully
_SKIP_KEYS = frozenset(
    {
        "home_assistant",
        "entity_id",
        "device_id",
        "platform",
        "condition",
        "action",
        "trigger",
        "service",
        "value_template",
        "state",
        "friendly_name",
    }
)


def _extract_tokens(bc_name: str, bc_body: str) -> list[str]:
    """Extract searchable tokens from a BC entry's name and body.

    Returns a deduplicated, sorted list capped at _MAX_TOKENS.
    """
    tokens: set[str] = set()
    text = bc_name + " " + bc_body

    # domain.service call patterns (e.g. "matter.sensor")
    for m in _SERVICE_CALL_RE.finditer(text):
        tokens.add(f"{m.group(1)}.{m.group(2)}")

    # YAML keys: snake_case words not in the skip list
    for m in _YAML_KEY_RE.finditer(text):
        key = m.group(1)
        if key not in _SKIP_KEYS and len(key) > 4:
            tokens.add(key)

    # Quoted values (not secret-adjacent)
    for m in _QUOTED_VALUE_RE.finditer(bc_body):
        val = m.group(1)
        if val.strip() and not any(w in val.lower() for w in _EXCLUDE_WORDS):
            tokens.add(f'"{val}"')

    # Card types: "Alert card" → "type: alert"
    for m in _CARD_TYPE_RE.finditer(bc_body):
        word = m.group(1).lower()
        if len(word) > 2:
            tokens.add(f"type: {word}")

    # Template-style function names
    for m in _TEMPLATE_FN_RE.finditer(bc_body):
        fn = m.group(1)
        if fn not in _SKIP_KEYS:
            tokens.add(fn)

    return sorted(tokens)[:_MAX_TOKENS]


def _entity_summary(
    bc_domains: list[str],
    entity_registry: list[dict],
    device_registry: list[dict],
) -> tuple[int, list[str], list[str]]:
    """Return (count, example_entity_ids, multi_channel_device_names).

    Match entity domains or integration platforms (e.g. light.* on Tuya).
    Multi-channel: devices with >1 matched entity of the same entity type (the
    entity_id prefix) where one entity_id ends in a digit, a common pattern for
    Tuya/Z-Wave multi-endpoint devices. Grouping is by entity type even for a
    platform match, so a device's mixed sensors and switches are not channels.
    """
    if not bc_domains:
        return 0, [], []

    domain_set = set(bc_domains)
    matching = [
        e
        for e in entity_registry
        if e.get("entity_id", "").split(".")[0] in domain_set
        or e.get("platform", "") in domain_set
    ]

    count = len(matching)
    examples = [
        e["entity_id"] for e in matching[:_MAX_ENTITY_EXAMPLES] if "entity_id" in e
    ]

    # Multi-channel detection: group by (device, entity type)
    channel_groups: dict[tuple[str, str], list[str]] = {}
    for e in matching:
        dev_id = e.get("device_id")
        entity_id = e.get("entity_id", "")
        if dev_id:
            key = (dev_id, entity_id.split(".")[0])
            channel_groups.setdefault(key, []).append(entity_id)

    device_id_to_name: dict[str, str] = {
        d.get("id", ""): d.get("name_by_user") or d.get("name", "")
        for d in device_registry
        if d.get("id")
    }

    multi_channel: list[str] = []
    for (dev_id, _entity_type), entity_ids in channel_groups.items():
        if len(entity_ids) > 1 and any(eid[-1:].isdigit() for eid in entity_ids):
            name = device_id_to_name.get(dev_id, dev_id[:8])
            if name and name not in multi_channel:
                multi_channel.append(name)

    return count, examples, multi_channel[:5]


def _token_to_search_terms(token: str) -> list[str]:
    """Return one or more strings to search for in a JSON-serialized dashboard config.

    ``type: alert`` → searches for both ``"type": "alert"`` (JSON) and the bare
    ``type: alert`` form so YAML-mode and storage-mode dashboards are both covered.
    Quoted-value tokens like ``"None"`` are searched as ``None`` (the raw value).
    """
    # Card type token: "type: alert" → also try JSON form
    if token.startswith("type: "):
        word = token[len("type: ") :]
        return [f'"type": "{word}"', token]
    # Quoted value: strip the quotes for the actual string search
    if token.startswith('"') and token.endswith('"'):
        return [token[1:-1]]
    return [token]


def _search_dashboards(
    tokens: list[str],
    lovelace_configs: list[tuple[str, dict]],
) -> list[str]:
    """Return token matches in lovelace config JSON as 'dashboard:<path>:<token>'."""
    hits: list[str] = []
    for url_path, config in lovelace_configs:
        config_str = json.dumps(config)
        for token in tokens:
            if any(term in config_str for term in _token_to_search_terms(token)):
                hit = f"dashboard:{url_path or 'default'}:{token}"
                if hit not in hits:
                    hits.append(hit)
        if len(hits) >= _MAX_DASHBOARD_HITS:
            break
    return hits


async def _grep_automation_yaml(
    tokens: list[str],
    ssh_client: Any,
) -> list[str]:
    """Search automation/script YAML on the HA host for specific tokens.

    Only non-service-call tokens are searched (YAML keys and quoted values).
    Returns hit strings as returned by grep.  Empty list on error or no SSH.
    """
    if ssh_client is None or not tokens:
        return []

    hits: list[str] = []
    excludes = " ".join(f"! -name '*{w}*'" for w in _EXCLUDE_WORDS)
    path_args = " ".join(shlex.quote(p) for p in _AUTOMATION_PATHS)

    # Only search specific YAML keys and values — service-call patterns
    # (domain.service) are too broad and not reliably in YAML text form.
    search_tokens = [
        t
        for t in tokens[:8]
        if not ("." in t and not t.startswith('"'))  # skip service-call patterns
    ]

    for token in search_tokens:
        search = token.strip('"')
        if not search or len(search) < 3:
            continue
        script = (
            "set -f\nIFS='\n'\nset --\n"
            f"for p in {path_args}; do "
            '[ -e "$p" ] && set -- "$@" "$p"; done\n'
            "[ $# -eq 0 ] && exit 1\n"
            f"files=$(find \"$@\" -type f -name '*.yaml' {excludes})\n"
            '[ -z "$files" ] && exit 1\n'
            f"grep -nH -F -e {shlex.quote(search)} -- $files 2>/dev/null\n"
        )
        try:
            rc, stdout, _stderr = await ssh_client.run(
                f"sh -c {shlex.quote(script)}", check=False
            )
            if rc == 0 and stdout.strip():
                for line in stdout.strip().splitlines()[:3]:
                    hit = f"{line.strip()[:120]} [{token}]"
                    if hit not in hits:
                        hits.append(hit)
        except Exception:  # nosec B110
            pass
        if len(hits) >= _MAX_AUTOMATION_HITS:
            break

    return hits


async def _build_one_evidence(
    name: str,
    body: str,
    domains: list[str],
    entity_registry: list[dict],
    device_registry: list[dict],
    lovelace_configs: list[tuple[str, dict]],
    ssh_client: Optional[Any],
) -> "Any":
    """Build BCEvidence for a single entry (BC or flag).  Returns a BCEvidence."""
    from utils.knowledge.ha_release_report import BCEvidence

    entity_count, entity_examples, multi_channel = _entity_summary(
        domains, entity_registry, device_registry
    )
    tokens = _extract_tokens(name, body)
    dashboard_hits = _search_dashboards(tokens, lovelace_configs)
    automation_hits: list[str] = []
    if ssh_client is not None and tokens:
        automation_hits = await _grep_automation_yaml(tokens, ssh_client)

    found = entity_count > 0 or bool(automation_hits) or bool(dashboard_hits)
    return BCEvidence(
        domains=domains,
        entity_count=entity_count,
        entity_examples=entity_examples,
        multi_channel_devices=multi_channel,
        tokens=tokens,
        automation_hits=automation_hits,
        dashboard_hits=dashboard_hits,
        found=found,
    )


async def build_bc_evidence(
    report: Any,  # CoreReleaseReport
    ws_client: Optional[Any] = None,
    rest_client: Optional[Any] = None,  # noqa: ARG001 — reserved for future use
    ssh_client: Optional[Any] = None,
) -> "tuple[dict[str, Any], dict[str, Any]]":
    """Build per-BC and per-flag evidence for all entries in a CoreReleaseReport.

    Returns (bc_evidence, flag_evidence):
      bc_evidence  — dict keyed by BCEntry.name
      flag_evidence — dict keyed by ScreeningFlag.change text
    Clients may be None — each data source degrades gracefully.
    """
    # ── Gather shared data once ───────────────────────────────────────────
    entity_registry: list[dict] = []
    device_registry: list[dict] = []
    lovelace_configs: list[tuple[str, dict]] = []

    if ws_client is not None:
        try:
            entity_registry = await ws_client.get_entity_registry()
        except Exception as exc:
            log.warning("bc_evidence_entity_registry_failed", error=str(exc))
        try:
            device_registry = await ws_client.get_device_registry()
        except Exception as exc:
            log.warning("bc_evidence_device_registry_failed", error=str(exc))
        try:
            dashboards = await ws_client.get_lovelace_dashboards()
            for dash in dashboards:
                url_path = dash.get("url_path")
                try:
                    config = await ws_client.get_lovelace_config(url_path)
                    lovelace_configs.append((url_path or "default", config))
                except Exception:  # nosec B110
                    pass
            # Always attempt the default dashboard (url_path=None)
            if not any(p in ("default", None) for p, _ in lovelace_configs):
                try:
                    config = await ws_client.get_lovelace_config(None)
                    lovelace_configs.append(("default", config))
                except Exception:  # nosec B110
                    pass
        except Exception as exc:
            log.warning("bc_evidence_lovelace_failed", error=str(exc))

    _shared = (entity_registry, device_registry, lovelace_configs, ssh_client)

    # ── Per-BC evidence ───────────────────────────────────────────────────
    all_bcs = list(report.bc_installed) + list(report.bc_general)
    bc_result: dict[str, Any] = {}

    for bc in all_bcs:
        name: str = bc.name
        ev = await _build_one_evidence(name, bc.body or "", bc.domains or [], *_shared)
        bc_result[name] = ev
        log.info(
            "bc_evidence_built",
            bc=name,
            entity_count=ev.entity_count,
            dashboard_hits=len(ev.dashboard_hits),
            automation_hits=len(ev.automation_hits),
            found=ev.found,
        )

    # ── Per-flag evidence ─────────────────────────────────────────────────
    flag_result: dict[str, Any] = {}

    for flag in list(getattr(report, "screening_flags", [])):
        change_text: str = flag.change
        domains: list[str] = [flag.domain] if flag.domain else []
        ev = await _build_one_evidence(
            change_text, flag.concern or "", domains, *_shared
        )
        flag_result[change_text] = ev
        log.info(
            "flag_evidence_built",
            flag_domain=flag.domain,
            entity_count=ev.entity_count,
            dashboard_hits=len(ev.dashboard_hits),
            automation_hits=len(ev.automation_hits),
            found=ev.found,
        )

    return bc_result, flag_result
