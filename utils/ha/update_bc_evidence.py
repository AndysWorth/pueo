"""Per-BC entity and configuration evidence gatherer for Core update analysis.

Called once per Core release, after the release report is built.  Pure-ish:
no LLM calls, all I/O is read-only.  Clients may be None — each data source
degrades gracefully when its client is unavailable.
"""

from __future__ import annotations

import ipaddress
import json
import re
import shlex
import sqlite3
import time
from typing import Any, Optional

from utils.core.logging import get_logger

log = get_logger("update_bc_evidence")

_MAX_ENTITY_EXAMPLES = 15
_MAX_DASHBOARD_HITS = 20
_MAX_AUTOMATION_HITS = 20
_MAX_TOKENS = 20

# Common YAML/state values that appear everywhere and don't distinguish a BC
_STATE_WORDS = frozenset(
    {"on", "off", "none", "true", "false", "unknown", "unavailable"}
)

# HA entity-domain prefixes that could appear in BC bodies (for zero-count filling)
_ENTITY_DOMAIN_WORDS = frozenset(
    {
        "alarm_control_panel",
        "binary_sensor",
        "button",
        "calendar",
        "camera",
        "climate",
        "cover",
        "device_tracker",
        "event",
        "fan",
        "image",
        "lawn_mower",
        "light",
        "lock",
        "media_player",
        "notify",
        "number",
        "remote",
        "scene",
        "select",
        "sensor",
        "siren",
        "stt",
        "switch",
        "text",
        "todo",
        "tts",
        "update",
        "vacuum",
        "valve",
        "wake_word",
        "water_heater",
        "weather",
    }
)

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
_TEMPLATE_FN_RE = re.compile(r"\b([a-z_]{5,})\s*\(")
# "set the effect to "None"" → key/value pair the BC tells users to change.  The
# bare value is a state word and far too common to search for; the pair is not.
_KEY_VALUE_RE = re.compile(r'\b([a-z][a-z_]{2,})\s+to\s+"([^"]{2,25})"')
_KEY_VALUE_TOKEN_RE = re.compile(r"^([a-z][a-z_]*): (.+)$")
# BC text about things Pueo cannot observe from HA's registries or config files.
# Each match becomes an "Outside the evidence" line; the verdict stays with the
# seed (#1213).
_OUTSIDE_EVIDENCE_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    (
        "which browsers, tablets and phones open the Home Assistant UI",
        re.compile(
            r"\b(?:Safari|Chrome|Firefox|WebView|iPadOS|kiosk browsers?"
            r"|older browsers?|browser versions?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "how Home Assistant is reached from outside the home"
        " (public address, VPN, reverse proxy)",
        re.compile(
            r"reverse proxy|trusted prox|public (?:IP )?address|global IPv6"
            r"|\bTailscale\b|\bVPN\b|not on a local network",
            re.IGNORECASE,
        ),
    ),
)
# Backtick-quoted identifiers (Markdown inline code): captures attribute names like `friendly_name`
_BACKTICK_IDENT_RE = re.compile(r"`([a-z][a-z0-9_]{2,30})`")

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

# Card-type regex: require capitalized word.  Common English articles/determiners
# that happen to be capitalized (e.g. "The card") are excluded by _SKIP_CARD_WORDS.
_CARD_TYPE_RE = re.compile(r"\b([A-Z][a-z]+)\s+card\b")
_SKIP_CARD_WORDS = frozenset(
    {"the", "a", "an", "this", "that", "these", "those", "each", "any", "one", "two"}
)

# PR reference pattern used to split multi-part BC bodies: ( @author - #12345 )
_PR_REF_RE = re.compile(r"\(\s*@\S+\s*-\s*#\d+\s*\)", re.IGNORECASE)

# Tailscale CGNAT range
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")

# Sensor device_class keyword → canonical device_class value.
# Used to extract which device classes a BC body mentions so we can zero-fill
# sensor state counts for classes the BC names but this instance lacks.
_DC_KEYWORD_MAP: dict[str, str] = {
    "tvoc": "volatile_organic_compounds",
    "volatile organic": "volatile_organic_compounds",
    "volatile_organic_compounds": "volatile_organic_compounds",
    "carbon monoxide": "carbon_monoxide",
    "carbon_monoxide": "carbon_monoxide",
    "nitrogen dioxide": "nitrogen_dioxide",
    "nitrogen_dioxide": "nitrogen_dioxide",
    "ozone": "ozone",
    "pm2.5": "pm25",
    "pm10": "pm10",
    "pm1": "pm1",
    "co2": "carbon_dioxide",
    "carbon dioxide": "carbon_dioxide",
    "carbon_dioxide": "carbon_dioxide",
}

# Common English words to exclude when computing distinctive part tokens
_COMMON_PART_WORDS = frozenset(
    {
        "home",
        "assistant",
        "this",
        "that",
        "with",
        "from",
        "have",
        "been",
        "will",
        "when",
        "which",
        "their",
        "they",
        "were",
        "your",
        "also",
        "only",
        "then",
        "some",
        "each",
        "other",
        "these",
        "those",
        "over",
        "into",
        "than",
        "such",
        "used",
        "uses",
        "both",
        "more",
        "less",
        "made",
        "make",
        "adds",
        "added",
        "before",
        "after",
        "changed",
        "change",
        "entity",
        "entities",
        "device",
        "devices",
        "integration",
        "support",
        "supported",
        "setting",
        "settings",
        "example",
        "value",
        "values",
        "would",
        "should",
        "could",
        "their",
        "using",
        "being",
        "already",
        "however",
        "without",
    }
)

_PART_WORD_RE = re.compile(r"\b([a-z][a-z0-9_]{3,30})\b")


def _classify_url(url: str) -> dict[str, str]:
    """Classify a URL's host as domain, lan_ip, public_ip, tailscale_ip, or global_ipv6.

    Returns {"url": url, "type": type, "host": host}.
    """
    if not url:
        return {"url": url, "type": "none", "host": ""}
    without_proto = url.split("://", 1)[-1] if "://" in url else url
    authority = without_proto.split("/")[0]
    # Bracketed IPv6 literal: "[2001:db8::1]:8123" → "2001:db8::1"
    if authority.startswith("[") and "]" in authority:
        host = authority[1 : authority.index("]")]
    else:
        host = authority.split(":")[0]
    host = host.strip("[]")
    if not host:
        return {"url": url, "type": "none", "host": ""}
    try:
        addr = ipaddress.ip_address(host)
        if addr.is_loopback or addr.is_private:
            return {"url": url, "type": "lan_ip", "host": host}
        if isinstance(addr, ipaddress.IPv4Address) and addr in _TAILSCALE_NET:
            return {"url": url, "type": "tailscale_ip", "host": host}
        if isinstance(addr, ipaddress.IPv6Address) and addr.is_global:
            return {"url": url, "type": "global_ipv6", "host": host}
        return {"url": url, "type": "public_ip", "host": host}
    except ValueError:
        return {"url": url, "type": "domain", "host": host}


def _device_classes_from_text(text: str) -> list[str]:
    """Extract sensor device_class values mentioned in a BC text."""
    text_lower = text.lower()
    found: set[str] = set()
    for keyword, dc in _DC_KEYWORD_MAP.items():
        if keyword in text_lower:
            found.add(dc)
    return sorted(found)


def _split_bc_parts(body: str) -> list[str]:
    """Split a multi-part BC body into individual parts by PR reference markers.

    Returns the list of non-empty part bodies.
    """
    parts = _PR_REF_RE.split(body)
    return [p.strip() for p in parts if p.strip()]


def _part_key_tokens(part_text: str, all_parts: list[str], part_idx: int) -> list[str]:
    """Extract up to 5 tokens that are distinctive to this part vs all other parts."""
    my_words = set(_PART_WORD_RE.findall(part_text.lower()))
    other_words: set[str] = set()
    for i, p in enumerate(all_parts):
        if i != part_idx:
            other_words.update(_PART_WORD_RE.findall(p.lower()))
    unique = (
        my_words
        - other_words
        - _STATE_WORDS
        - frozenset(_SKIP_KEYS)
        - _COMMON_PART_WORDS
    )
    return sorted(w for w in unique if len(w) >= 5)[:5]


def _build_mobile_clients(
    entity_registry: list[dict],
    device_registry: list[dict],
) -> list[str]:
    """Return display labels for devices registered via the mobile_app platform."""
    mobile_device_ids: set[str] = set()
    for e in entity_registry:
        if e.get("platform") == "mobile_app" and e.get("device_id"):
            mobile_device_ids.add(e["device_id"])
    device_map = {d.get("id", ""): d for d in device_registry if d.get("id")}
    clients: list[str] = []
    for dev_id in mobile_device_ids:
        d = device_map.get(dev_id)
        if d:
            name = d.get("name_by_user") or d.get("name") or ""
            model = d.get("model") or ""
            sw = d.get("sw_version") or ""
            if model and sw:
                label = f"{name} ({model}, {sw})" if name else f"{model} ({sw})"
            elif model:
                label = f"{name} ({model})" if name else model
            elif name:
                label = name
            else:
                label = dev_id[:8]
            clients.append(label)
    return sorted(clients)


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

    # Quoted values — skip bare state/boolean words and plain English (no _ or .)
    for m in _QUOTED_VALUE_RE.finditer(bc_body):
        val = m.group(1).strip()
        if not val:
            continue
        if any(w in val.lower() for w in _EXCLUDE_WORDS):
            continue
        if val.lower() in _STATE_WORDS:
            continue
        # Skip plain English words (no underscore, dot, digit, colon)
        if val.replace(" ", "").isalpha() and "_" not in val and "." not in val:
            continue
        tokens.add(f'"{val}"')

    # Key/value pairs: 'set the effect to "None"' → "effect: None".  A quoted
    # state word is skipped below as too common, but paired with its key it is
    # exactly what the BC tells users to look for (#1211).
    for m in _KEY_VALUE_RE.finditer(bc_body):
        key, val = m.group(1), m.group(2).strip()
        if not val or key in _SKIP_KEYS:
            continue
        if any(w in (key + val).lower() for w in _EXCLUDE_WORDS):
            continue
        tokens.add(f"{key}: {val}")

    # Card types: "Alert card" → "type: alert" (capitalised non-article word only)
    for m in _CARD_TYPE_RE.finditer(bc_body):
        word = m.group(1).lower()
        if len(word) > 2 and word not in _SKIP_CARD_WORDS:
            tokens.add(f"type: {word}")

    # Template-style function names
    for m in _TEMPLATE_FN_RE.finditer(bc_body):
        fn = m.group(1)
        if fn not in _SKIP_KEYS and "_" in fn:
            tokens.add(fn)

    # Backtick-quoted attribute names from Markdown inline code (e.g. `friendly_name`).
    # These bypass _SKIP_KEYS: a BC that tells users to rename `friendly_name` needs
    # it searched even though generic YAML key extraction skips it.
    for m in _BACKTICK_IDENT_RE.finditer(bc_body):
        ident = m.group(1)
        if "_" in ident and ident not in _STATE_WORDS:
            tokens.add(ident)

    return sorted(tokens)[:_MAX_TOKENS]


def _outside_evidence(bc_body: str, has_url_classification: bool = False) -> list[str]:
    """Return what the BC text depends on that the evidence can never show.

    When has_url_classification is True, the "how HA is reached from outside"
    entry is suppressed because url_classification already carries that answer.
    """
    lines = []
    for label, pattern in _OUTSIDE_EVIDENCE_PATTERNS:
        if not pattern.search(bc_body):
            continue
        if has_url_classification and "reached from outside" in label:
            continue
        lines.append(label)
    return lines


def _entity_summary(
    bc_domains: list[str],
    entity_registry: list[dict],
    device_registry: list[dict],
) -> "tuple[int, list[str], list[str], dict[str, int], dict[str, int]]":
    """Return (count, examples, multi_channel, entities_by_type, platforms).

    Match entity domains or integration platforms (e.g. light.* on Tuya).
    Multi-channel: devices with >1 matched entity of the same entity type (the
    entity_id prefix) where one entity_id ends in a digit, a common pattern for
    Tuya/Z-Wave multi-endpoint devices. Grouping is by entity type even for a
    platform match, so a device's mixed sensors and switches are not channels.

    entities_by_type: count per entity-domain prefix ({"light": 14, "sensor": 60}).
    platforms: count per integration platform ({"mobile_app": 2}).
    """
    if not bc_domains:
        return 0, [], [], {}, {}

    domain_set = set(bc_domains)
    seen_ids: set[str] = set()
    matching: list[dict] = []
    for e in entity_registry:
        eid = e.get("entity_id", "")
        if eid in seen_ids:
            continue
        if eid.split(".")[0] in domain_set or e.get("platform", "") in domain_set:
            seen_ids.add(eid)
            matching.append(e)

    count = len(matching)
    examples = [
        e["entity_id"] for e in matching[:_MAX_ENTITY_EXAMPLES] if "entity_id" in e
    ]

    # Type / platform breakdown
    entities_by_type: dict[str, int] = {}
    platforms: dict[str, int] = {}
    for e in matching:
        etype = e.get("entity_id", "").split(".")[0]
        if etype:
            entities_by_type[etype] = entities_by_type.get(etype, 0) + 1
        plat = e.get("platform", "")
        if plat:
            platforms[plat] = platforms.get(plat, 0) + 1

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

    return count, examples, multi_channel[:5], entities_by_type, platforms


def _token_to_search_terms(token: str) -> list[str]:
    """Return one or more strings to search for in a JSON-serialized dashboard config.

    ``type: alert`` → searches for both ``"type": "alert"`` (JSON) and the bare
    ``type: alert`` form so YAML-mode and storage-mode dashboards are both covered.
    Quoted-value tokens like ``"None"`` are searched as ``None`` (the raw value).
    """
    # Key/value token ("type: alert", "effect: None") → JSON form for
    # storage-mode dashboards, then the bare and quoted YAML forms.
    kv = _KEY_VALUE_TOKEN_RE.match(token)
    if kv:
        key, val = kv.group(1), kv.group(2)
        return [
            f'"{key}": "{val}"',
            token,
            f'{key}: "{val}"',
            f"{key}: '{val}'",
        ]
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
        # A key/value token is searched in every YAML spelling of the value.
        terms = (
            _token_to_search_terms(token)
            if _KEY_VALUE_TOKEN_RE.match(token)
            else [search]
        )
        patterns = " ".join(f"-e {shlex.quote(t)}" for t in terms)
        script = (
            "set -f\nIFS='\n'\nset --\n"
            f"for p in {path_args}; do "
            '[ -e "$p" ] && set -- "$@" "$p"; done\n'
            "[ $# -eq 0 ] && exit 1\n"
            f"files=$(find \"$@\" -type f -name '*.yaml' {excludes})\n"
            '[ -z "$files" ] && exit 1\n'
            f"grep -nH -F {patterns} -- $files 2>/dev/null\n"
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


def _all_registry_domains(entity_registry: list[dict]) -> set[str]:
    """Return every entity-domain prefix and platform seen in the full registry."""
    domains: set[str] = set()
    for e in entity_registry:
        eid = e.get("entity_id", "")
        if "." in eid:
            domains.add(eid.split(".")[0])
        plat = e.get("platform", "")
        if plat:
            domains.add(plat)
    return domains


def _zero_fill_entity_types(
    bc_body: str, entities_by_type: dict[str, int]
) -> dict[str, int]:
    """Add zero counts for entity-domain types named in the BC body but not found."""
    result = dict(entities_by_type)
    text_lower = bc_body.lower()
    for domain in _ENTITY_DOMAIN_WORDS:
        if domain not in result and domain in text_lower:
            result[domain] = 0
    return result


async def _build_one_evidence(
    name: str,
    body: str,
    domains: list[str],
    entity_registry: list[dict],
    device_registry: list[dict],
    lovelace_configs: list[tuple[str, dict]],
    ssh_client: Optional[Any],
    all_registry_domains: set[str],
    config_yaml_top_keys: list[str],
    ha_config_data: Optional[dict] = None,
    sensor_states: Optional[list] = None,
) -> "Any":
    """Build BCEvidence for a single entry (BC or flag).  Returns a BCEvidence."""
    from utils.knowledge.ha_release_report import BCEvidence

    entity_count, entity_examples, multi_channel, entities_by_type, platforms = (
        _entity_summary(domains, entity_registry, device_registry)
    )

    # Fill zeros for entity types named in the BC body but absent on this instance
    if domains:
        entities_by_type = _zero_fill_entity_types(body, entities_by_type)

    # A domain is a "core component" when it never produces entities anywhere in
    # the registry — it's a built-in HA component (auth, frontend, http, etc.)
    is_core = (
        bool(domains)
        and entity_count == 0
        and not any(d in all_registry_domains for d in domains)
    )

    # Top-level config.yaml keys that name one of this BC's domains
    domain_set = set(domains)
    config_keys = [k for k in config_yaml_top_keys if k in domain_set]

    tokens = _extract_tokens(name, body)
    tokens_searched = len(tokens)
    dashboard_hits = _search_dashboards(tokens, lovelace_configs)
    automation_hits: list[str] = []
    if ssh_client is not None and tokens:
        automation_hits = await _grep_automation_yaml(tokens, ssh_client)

    tokens_hit = len(
        set(t for t in tokens if any(t in h for h in automation_hits + dashboard_hits))
    )

    # URL classification for auth/http/frontend BCs
    url_classification: dict[str, dict[str, str]] = {}
    if ha_config_data and domain_set & {"auth", "http", "frontend"}:
        for key in ("external_url", "internal_url"):
            val = ha_config_data.get(key) or ""
            if val:
                url_classification[key] = _classify_url(val)

    # Mobile app clients for frontend BCs
    mobile_clients: list[str] = []
    if domain_set & {"frontend", "mobile_app"}:
        mobile_clients = _build_mobile_clients(entity_registry, device_registry)

    # Sensor device_class counts for BCs mentioning concentration/sensor device classes
    sensor_device_class_counts: dict[str, int] = {}
    if sensor_states is not None:
        mentioned_dcs = _device_classes_from_text(body)
        if mentioned_dcs:
            all_dc_counts: dict[str, int] = {}
            for s in sensor_states:
                dc = (s.get("attributes") or {}).get("device_class", "")
                if dc:
                    all_dc_counts[dc] = all_dc_counts.get(dc, 0) + 1
            for dc in mentioned_dcs:
                sensor_device_class_counts[dc] = all_dc_counts.get(dc, 0)

    # Per-part key tokens for multi-part BC reason-coverage validation.
    # Only computed for BCs with repeated domain entries (the marker of a multi-part BC).
    bc_part_tokens: list[list[str]] = []
    if domains and len(domains) != len(set(domains)):
        parts = _split_bc_parts(body)
        if len(parts) > 1:
            for idx, part in enumerate(parts):
                bc_part_tokens.append(_part_key_tokens(part, parts, idx))

    found = entity_count > 0 or bool(automation_hits) or bool(dashboard_hits)
    return BCEvidence(
        domains=domains,
        entity_count=entity_count,
        entity_examples=entity_examples,
        entities_by_type=entities_by_type,
        platforms=platforms,
        multi_channel_devices=multi_channel,
        is_core_component=is_core,
        config_keys_present=config_keys,
        tokens=tokens,
        tokens_searched=tokens_searched,
        tokens_hit=tokens_hit,
        automation_hits=automation_hits,
        dashboard_hits=dashboard_hits,
        outside_evidence=_outside_evidence(
            body, has_url_classification=bool(url_classification)
        ),
        found=found,
        url_classification=url_classification,
        mobile_clients=mobile_clients,
        sensor_device_class_counts=sensor_device_class_counts,
        bc_part_tokens=bc_part_tokens,
    )


_MAX_RELATED_PER_DOMAIN = 3
_RELATED_WINDOW_DAYS = 30


def recent_instance_events_for_domains(
    conn: sqlite3.Connection,
    domains: list[str],
    days: int = _RELATED_WINDOW_DAYS,
) -> dict[str, list[dict]]:
    """Return recent instance events keyed by domain, capped at _MAX_RELATED_PER_DOMAIN each.

    Sources (all exact-domain match, never substring):
    - session_outcomes: signatures whose domain segment equals the domain
      (device:offline:<d>, repair:<d>:..., log:homeassistant_components_<d>:...,
       lovelace:<d>:..., setup:integration:<d>)
    - ha_repair_history: issue_key starting with '<d>/'
    - timeline_events: WARN/ERROR rows with detail_json.domain == domain

    Returns {domain: [{ts: float, summary: str, source: str}, ...]} sorted
    newest-first, deduped by (source, summary), capped at _MAX_RELATED_PER_DOMAIN.
    """
    cutoff = time.time() - days * 86400
    domain_set = set(domains)
    result: dict[str, list[dict]] = {}

    def _add(domain: str, ts: float, summary: str, source: str) -> None:
        if domain not in domain_set:
            return
        lst = result.setdefault(domain, [])
        if len(lst) >= _MAX_RELATED_PER_DOMAIN:
            return
        key = (source, summary[:80])
        if any((e["_key"] == key) for e in lst):
            return
        lst.append({"ts": ts, "summary": summary[:120], "source": source, "_key": key})

    # --- session_outcomes ---
    try:
        rows = conn.execute(
            "SELECT signature, created_at FROM session_outcomes"
            " WHERE created_at >= ?"
            " ORDER BY created_at DESC",
            (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff)),),
        ).fetchall()
        for sig, created_at in rows:
            if not sig:
                continue
            parts = sig.split(":", 2)
            sig_type = parts[0] if parts else ""
            domain = None
            if sig_type == "device" and len(parts) >= 3 and parts[1] == "offline":
                domain = parts[2] if parts[2] != "post_restart" else None
            elif sig_type == "repair" and len(parts) >= 2:
                domain = parts[1]
            elif sig_type == "lovelace" and len(parts) >= 2:
                domain = parts[1]
            elif sig_type == "setup" and len(parts) >= 3 and parts[1] == "integration":
                domain = parts[2]
            elif sig_type == "log" and len(parts) >= 2:
                # log:homeassistant_components_<domain>:<exc>
                logger_part = parts[1]
                prefix = "homeassistant_components_"
                if logger_part.startswith(prefix):
                    candidate = logger_part[len(prefix) :]
                    # strip any trailing sub-component (e.g. homeassistant_components_sonos_media_player)
                    domain = candidate.split("_")[0]
            if domain and domain in domain_set:
                try:
                    ts = _iso_to_ts(created_at)
                except Exception:  # nosec B112
                    continue
                if ts < cutoff:
                    continue
                label = sig_type.replace("_", " ")
                _add(domain, ts, f"{label} session: {sig}", "session_outcomes")
    except Exception:  # nosec B110
        pass

    # --- ha_repair_history ---
    try:
        rows = conn.execute(
            "SELECT issue_key, first_seen_at FROM ha_repair_history"
            " WHERE first_seen_at >= ? AND resolved_at IS NULL"
            " ORDER BY first_seen_at DESC",
            (cutoff,),
        ).fetchall()
        for issue_key, first_seen_at in rows:
            if "/" not in str(issue_key):
                continue
            domain = issue_key.split("/")[0]
            if domain in domain_set:
                _add(
                    domain,
                    float(first_seen_at),
                    f"HA repair: {issue_key}",
                    "ha_repair_history",
                )
    except Exception:  # nosec B110
        pass

    # --- timeline_events (WARN/ERROR with detail_json.domain) ---
    try:
        rows = conn.execute(
            "SELECT ts, message, detail_json FROM timeline_events"
            " WHERE level IN ('WARNING', 'ERROR', 'WARN')"
            "   AND ts >= ?"
            "   AND detail_json IS NOT NULL"
            " ORDER BY ts DESC"
            " LIMIT 500",
            (cutoff,),
        ).fetchall()
        for ts, message, detail_json in rows:
            try:
                detail = json.loads(detail_json)
            except Exception:  # nosec B112
                continue
            domain = detail.get("domain")
            if domain and domain in domain_set:
                _add(domain, float(ts), message[:120], "timeline_events")
    except Exception:  # nosec B110
        pass

    # Strip internal dedup key and sort newest-first
    for domain in result:
        result[domain] = sorted(
            [
                {"ts": e["ts"], "summary": e["summary"], "source": e["source"]}
                for e in result[domain]
            ],
            key=lambda e: -e["ts"],
        )

    return result


def _iso_to_ts(s: str) -> float:
    """Parse an ISO8601 UTC datetime string (YYYY-MM-DDTHH:MM:SSZ) to a Unix timestamp."""
    import datetime

    dt = datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc
    )
    return dt.timestamp()


async def scan_custom_components(
    symbols: list[str],
    ssh_client: Any,
) -> dict[str, list[str]]:
    """grep /config/custom_components for each Python identifier symbol.

    Returns {symbol: [matched lines]} for each symbol.  Symbols must be plain
    Python identifiers (letters/digits/underscores) — no shell metacharacters.
    Missing or unreadable custom_components directory returns empty lists.
    """
    import re as _re

    _ident_re = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    results: dict[str, list[str]] = {}
    for symbol in symbols:
        if not _ident_re.match(symbol):
            log.warning(
                "scan_custom_components_skipped_invalid_symbol", symbol=symbol[:80]
            )
            results[symbol] = []
            continue
        cmd = f"grep -rnF {symbol} /config/custom_components"
        try:
            rc, stdout, _stderr = await ssh_client.run(cmd)
            if rc == 0 and stdout:
                results[symbol] = [
                    line.strip() for line in stdout.strip().splitlines() if line.strip()
                ]
            else:
                results[symbol] = []
        except Exception as exc:
            log.warning(
                "scan_custom_components_error", symbol=symbol[:80], error=str(exc)
            )
            results[symbol] = []
    return results


async def build_bc_evidence(
    report: Any,  # CoreReleaseReport
    ws_client: Optional[Any] = None,
    rest_client: Optional[Any] = None,
    ssh_client: Optional[Any] = None,
    config_yaml_top_keys: Optional[list[str]] = None,
) -> "tuple[dict[str, Any], dict[str, Any]]":
    """Build per-BC and per-flag evidence for all entries in a CoreReleaseReport.

    Returns (bc_evidence, flag_evidence):
      bc_evidence  — dict keyed by BCEntry.name
      flag_evidence — dict keyed by ScreeningFlag.change text
    Clients may be None — each data source degrades gracefully.
    config_yaml_top_keys: top-level keys from configuration.yaml (from HAEnvironmentProfile)
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

    # ── REST data: HA system config (URL data) and sensor states (device_class) ──
    ha_config_data: Optional[dict] = None
    sensor_states: Optional[list] = None
    if rest_client is not None:
        try:
            ha_config_data = await rest_client.get_raw("/api/config")
        except Exception as exc:
            log.warning("bc_evidence_ha_config_failed", error=str(exc))
        # Only fetch sensor states when any BC mentions sensor device_class keywords
        all_bc_text = " ".join(
            (bc.name + " " + (bc.body or ""))
            for bc in list(report.bc_installed) + list(report.bc_general)
        )
        if _device_classes_from_text(all_bc_text):
            try:
                sensor_states = await rest_client.get_states("sensor.")
            except Exception as exc:
                log.warning("bc_evidence_sensor_states_failed", error=str(exc))

    all_reg_domains = _all_registry_domains(entity_registry)
    cfg_keys = config_yaml_top_keys or []

    _shared = (
        entity_registry,
        device_registry,
        lovelace_configs,
        ssh_client,
        all_reg_domains,
        cfg_keys,
        ha_config_data,
        sensor_states,
    )

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

    # ── Developer-flag custom-component scan ─────────────────────────────────
    if ssh_client is not None:
        dev_flags = [
            f
            for f in list(getattr(report, "screening_flags", []))
            if getattr(f, "audience", "user") == "developer"
            and getattr(f, "symbol", None)
        ]
        if dev_flags:
            symbols = [f.symbol for f in dev_flags]
            scan = await scan_custom_components(symbols, ssh_client)  # type: ignore[arg-type]
            # Build flag_verdicts entries for developer flags based on scan hits.
            existing_verdict_flags = {
                v.get("flag", "") for v in getattr(report, "flag_verdicts", [])
            }
            new_verdicts: list[dict] = []
            for flag in dev_flags:
                sym = flag.symbol
                if flag.change in existing_verdict_flags:
                    continue
                hits = scan.get(sym, [])
                if hits:
                    # Extract integration names from hit paths like
                    # /config/custom_components/my_integration/sensor.py:12:...
                    integrations: list[str] = []
                    for hit in hits[:20]:
                        parts = hit.split("/")
                        if "custom_components" in parts:
                            idx = parts.index("custom_components")
                            if idx + 1 < len(parts):
                                integrations.append(parts[idx + 1])
                    unique_integrations = list(dict.fromkeys(integrations))
                    new_verdicts.append(
                        {
                            "flag": flag.change,
                            "verdict": "user_check",
                            "reason": (
                                f"Custom integration(s) {', '.join(unique_integrations) or 'unknown'} "
                                f"use {sym!r} which is being removed."
                            ),
                            "integrations": unique_integrations,
                            "hits": hits[:10],
                        }
                    )
                    log.info(
                        "dev_flag_custom_component_hit",
                        symbol=sym,
                        integrations=unique_integrations,
                    )
                else:
                    new_verdicts.append(
                        {
                            "flag": flag.change,
                            "verdict": "no_action",
                            "reason": f"No custom integration uses {sym!r}.",
                            "integrations": [],
                            "hits": [],
                        }
                    )
            if new_verdicts and hasattr(report, "flag_verdicts"):
                report.flag_verdicts = (
                    list(getattr(report, "flag_verdicts", [])) + new_verdicts
                )
                log.info(
                    "dev_flag_verdicts_built",
                    total=len(new_verdicts),
                    user_check=sum(
                        1 for v in new_verdicts if v["verdict"] == "user_check"
                    ),
                )

    return bc_result, flag_result
