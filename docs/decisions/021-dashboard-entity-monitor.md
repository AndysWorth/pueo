# ADR 021 — Dashboard entity health monitor

## Status
Accepted

## Context
Lovelace dashboards frequently reference entity IDs that no longer exist — because an integration was removed, a device was renamed, or an entity was disabled. The HA UI renders affected cards as "Entity not found" silently, and the user has to hunt for the broken reference. Pueo already monitors updates, repairs, and notifications on a polling loop; dashboard entity health is a natural fourth periodic check using the same infrastructure.

## Decision
Add `ha_lovelace_monitor.py` — a new polling loop that:

1. Fetches the full Lovelace config via REST (`GET /api/lovelace/config`)
2. Walks the card tree to extract all entity_id references (`_extract_entity_refs`)
3. Fetches the live entity registry via WebSocket (`config/entity_registry/list`)
4. Cross-references: any entity_id in the card tree but not in the registry is flagged
5. For each missing entity, runs a single-shot LLM call (`_analyze_missing_entity`) to determine likely cause and recommend an action (replace | remove | investigate)
6. Surfaces a HITL approval card via the standard `hitl_suppression` mechanism
7. On approve: patches Lovelace config via `POST /api/lovelace/config`
8. Reconciles: marks cards resolved when the entity reappears in the registry

### REST-only for Lovelace fetch
`GET /api/lovelace/config` is the documented endpoint and returns the full dashboard JSON in one call. WebSocket subscriptions exist but add connection-management overhead with no benefit for a polling use case.

### Single-shot LLM analysis (vs full AgentLoop)
The goal is explanation + one concrete recommendation. There is no need to read additional files or run HA commands — all the relevant context (missing entity_id, registry candidates) is available at analysis time. A full AgentLoop with a tool budget would consume more tokens and add latency without improving the output quality for this narrow, well-defined task.

### `hitl_suppression` reuse (vs new per-table dedup)
The existing `hitl_suppression` table already implements rejection memory, cooldown, Known Issues, and deferred-until logic. Creating a new table would duplicate this logic for no gain. `card_key` format: `dashboard_entity:<entity_id>`.

### `post()` addition to `HARestClientProtocol`
Writing updated Lovelace config requires a POST to `/api/lovelace/config`. Adding `post(path, payload)` to `HARestClientProtocol` generalises the client for future write operations without needing a separate write client. `FakeHARestClient.posted` captures calls for tests.

### Entity deduplication
The `_extract_entity_refs` function returns one `EntityRef` per unique `entity_id`. If the same entity appears in multiple cards, only the first occurrence is tracked — the fix (replace or remove) is a global string operation on the serialised config JSON, so all occurrences are handled by a single approval.

## Consequences
- `get_entity_registry()` is added to `HAWebSocketClientProtocol`, `HAWebSocketClient`, and `FakeHAWebSocketClient`
- `post()` is added to `HARestClientProtocol`, `HARestClient`, and `FakeHARestClient`
- `CARD_TYPE_DASHBOARD_ENTITY = "dashboard_entity"` is added to `utils/card_types.py`
- `HA_LOVELACE_CHECK_INTERVAL_MINUTES` config key follows the triple-update rule
- The Lovelace patch is a JSON-level string replacement — it handles the common cases (single entity reference, entity in a list) but may leave stray empty strings for complex card structures; the user sees the proposed change in the card body before approving
- The `investigate` action produces no config write; the card is acknowledged and the user is directed to investigate the integration manually

## Related decisions
- [ADR 002 — Safety invariant](002-safety-invariant.md): Lovelace config writes do not touch `configuration.yaml`; the backup-before-write invariant does not apply. The existing HA backup mechanism covers Lovelace config independently.
- [ADR 017 — Chat tool parity](017-chat-tool-parity.md): `get_entity_registry` follows the established pattern of adding new WS methods to all four locations (real client, fake, interface, call site) to maintain structural completeness.
- [ADR 013 — Prompt externalization](013-prompt-externalization.md): the LLM prompt lives in `prompts/triage_dashboard_entity.md`, not inline in the module.
