# ADR 041 — Host-health triggers

**Status:** Accepted
**Date:** 2026-10-13

## Context

Issues #853 and #856 added a host-health strip to the Resources tab and an
investigation pipeline (`utils/disk/host_health_investigation.py`) that fires when
host metrics exceed thresholds (swap pressure, memory low, eMMC disk wear, unexpected
reboot, Supervisor unhealthy/unsupported, resolution issues).

During an investigation the agent may determine that a Supervisor resolution suggestion
should be applied, a known-benign issue dismissed, a health recheck run, or an app
restarted. These four actions are natural follow-ups — but they require variable
arguments (UUIDs from `ha resolution info` and app slugs from `ha apps list`) that
the previous `_HA_COMMAND_RISK` exact-match dict cannot handle.

## Decision

Introduce two module-level helpers in `utils/agent/tool_executor.py`:

### `_is_ha_command_allowed(normalized: str) -> bool`

Single source of truth for whether a command may execute at all. Replaces the
inline boolean expression in `_run_ha_command` and the `command not in
_HA_COMMAND_ALLOWLIST` check in `_execute_ha_command`. Handles both:

- Exact membership in `_HA_COMMAND_ALLOWLIST` (static commands)
- Pattern-matched parameterized commands validated by `_SLUG_RE` / `_UUID_RE`

### `_ha_command_risk(normalized: str) -> str | None`

Returns the risk tier string for a state-changing command, or `None` for
read-only commands. Replaces `_HA_COMMAND_RISK.get(normalized)`. Checks exact
static entries first, then pattern-matches the parameterized gated commands.

### New parameterized gated commands

| Command pattern | Risk | Validation |
|---|---|---|
| `ha resolution suggestion apply <uuid>` | MEDIUM | `_UUID_RE` |
| `ha resolution issue dismiss <uuid>` | LOW | `_UUID_RE` |
| `ha apps restart <slug>` | MEDIUM | `_SLUG_RE` |

Static addition to `_HA_COMMAND_ALLOWLIST` and `_HA_COMMAND_RISK`:

| Command | Risk |
|---|---|
| `ha resolution healthcheck` | LOW |

### Validation regexes

```python
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")
_UUID_RE = re.compile(
    r"^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
```

`_UUID_RE` accepts both compact 32-char hex (some HA Supervisor versions) and
standard dashed UUID4 (others). `_SLUG_RE` disallows leading hyphens/underscores
and shell metacharacters to prevent command injection.

### Defense-in-depth in `_execute_ha_command`

The dashboard card executor re-validates at execution time using the same helpers,
so a tampered card payload cannot bypass the allowlist or risk map checks.

## Consequences

- The four new commands are now available to agent loops running under any autonomy
  level: at AUTONOMOUS they execute directly; at lower levels they produce a
  `CARD_TYPE_HA_COMMAND` approval card.
- `prompts/seed_host_health.md` and `prompts/agent_loop_host_health.md` list these
  commands so the agent knows to use them during host-health investigations.
- `_HA_COMMAND_RISK` remains the registry for static state-changing commands.
  Parameterized commands are not in `_HA_COMMAND_RISK`; callers must use
  `_ha_command_risk()` for the complete picture.
- Existing tests that imported `_HA_COMMAND_RISK` directly are updated to import and
  call `_ha_command_risk()` for the parameterized cases.
