# ADR 023 — External API Resilience and Schema Evolution

## Status
Accepted

## Context
Pueo integrates with three external systems whose schemas can change independently of Pueo's code: Home Assistant (REST and WebSocket APIs, SSH command output), NetAlertX (HTTP API), and Ollama/Anthropic (LLM response envelopes). Two patterns in the current code create silent failure modes when those schemas drift:

1. **Silent field drops.** `load_environment_profile` and other consumers filter external dicts to known fields using set-intersection. This is correct — persisting unknown fields would pollute the schema. But the filtering happens silently: when HA adds a field Pueo does not recognize, there is no log event to signal "this code is behind the API."

2. **Hard-bracket access on external dicts.** `entity["entity_id"]`, `b["slug"]`, `item["setKey"]`, and similar expressions raise opaque `KeyError`s when a field is renamed or removed. The traceback identifies the line but not the source of the unexpected shape.

Together, these patterns mean Pueo can silently degrade: the agent loop's pre-injection steps fail quietly, repair cards present incomplete data, and schema regressions go undetected until they cause an observable failure.

## Decisions

### Decision 1 — Detect schema drift, don't just ignore it

When filtering unknown fields from any external data source, always emit a structured warning so operators and the self-healing loop can notice drift. The canonical pattern:

```python
known = {f.name for f in dataclasses.fields(Model)}
unknown = set(data) - known
if unknown:
    log.warning("schema_drift_detected", source="ha_profile", unknown_fields=sorted(unknown))
```

This applies to `HAEnvironmentProfile`, all HA REST entity shapes, WebSocket result envelopes, and NetAlertX device/event dicts.

### Decision 2 — No hard-bracket access on external data

Replace `external_dict["key"]` with `.get("key")` plus explicit handling when the value is absent. The handling depends on whether the field is required or optional:

- **Required fields** (absence changes control flow or produces silently wrong results): log `log.error("required_field_missing", field="entity_id", source="ha_rest")` and skip the record.
- **Optional fields**: `.get(key, default)` is correct as-is; no change needed.

For LLM response envelopes, use `response.get("message", {}).get("content", "")` rather than `response["message"]["content"]`.

### Decision 3 — Distinguish optional-missing from required-missing

`.get(key, "")` and `.get(key, None)` are silent: they cannot distinguish "the field was absent" from "the field was present with a falsy value." For required fields, use the explicit-check pattern from Decision 2 rather than a silent default.

### Decision 4 — Schema drift warnings are self-healing signals

The `schema_drift_detected` and `required_field_missing` structured log events are surfaced in the Pueo log tab (via the `read_pueo_log` tool) and are queryable by the self-healing agent. When Pueo detects schema drift during a repair session, it can call `read_pueo_log` to retrieve the unknown fields and use `save_strategy` to record an investigation note. Future work: wire these events into the code-proposal loop so Pueo can suggest its own schema updates when drift is detected.

## Consequences

- Every external dict consumer (HA REST, WS, SSH output, NetAlertX API, LLM responses) must follow the `.get()` + log pattern for required fields.
- The drift-detection log is a one-line addition after any set-intersection filter; it adds no runtime cost when no drift is present.
- `schema_drift_detected` events create a feedback loop: as HA releases new versions, Pueo operators can see which fields are new before they become load-bearing.
- Tests that mock external dicts should include a spurious unknown field to confirm the drift log fires; tests that mock missing required fields should confirm the error log fires and the record is skipped.

## Related decisions
- [ADR 002 — Safety invariant](002-safety-invariant.md): schema drift in backup or restore API responses must not silently bypass the backup-before-write chain; `log.error` + abort is the correct handling for missing `slug` fields.
- [ADR 003 — Structured LLM output](003-structured-llm-output.md): LLM response envelopes are also external data; Pydantic's `model_validate_json` already raises on schema violations for structured output paths, satisfying Decision 2 for those paths.
- [ADR 011 — HA live lookup](011-ha-live-lookup.md): `fetch_ha_docs` result content is external; filename and domain are user/agent-supplied inputs, not trusted external responses, so the path-traversal guard rather than schema drift detection applies.
