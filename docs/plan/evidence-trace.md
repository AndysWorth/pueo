# Phase 7 — Evidence Capture and HITL Display (Items 23–24)

## Version Compatibility (audited 2026-07-21)

- **Home Assistant 2026.7.3** — patch release (no breaking changes). All Supervisor CLI commands used in evidence gathering are verified valid:
  - `ha addons info <slug>` ✓
  - `ha addons logs <slug> -n 50` ✓ (`-n` is the registered short alias for `--lines` in HA CLI)
  - `ha supervisor info` ✓
  - `ha core restart` ✓ (corrected from `ha core reload` in item 21)
- **NetAlertX v26.7.1** — no new release since last audit. No version-specific concerns for items 23–24:
  - All evidence-gathering runs existing SSH commands (no NetAlertX REST API calls added)
  - `app.log` already in use (not deprecated `stdout.log`)
  - All API client calls use current endpoints (`/devices`, `/events`, `/health`, `/settings/<key>`, etc.) — old `/API_OLD` endpoints not referenced anywhere in the codebase
  - Old `/API_OLD` endpoints are slated for removal in the next NetAlertX release — tracked separately as item 25

Items 23–24 introduce **zero new API calls or CLI commands**. They only capture and surface data that existing code already collects.

---

## Motivation

When Pueo encounters a problem it can't fix, it sends a HITL notification to the web dashboard. Today that notification contains only a short text summary. All the useful context — raw command outputs, log buffer snapshots, the full structured diagnosis, and the LLM prompt and response — is discarded immediately after use. The user then has to manually re-gather evidence to understand what went wrong.

This phase closes that gap by: (1) capturing evidence and LLM interactions at each failure path and storing them in the HITL payload (which is already persisted to disk as a `.json` file), and (2) rendering that data in named collapsible sections in the web dashboard.

---

## Item 23 — Evidence and LLM Trace Capture

**Scope:** Backend only — Python changes to 8 existing files + 1 new file.  
**Session estimate:** 1–2 sessions.  
**Depends on:** Items 9.5, 19.5 (both complete).

### 23.1 — New file: `utils/llm_trace.py`

```python
@dataclass
class LLMTrace:
    model: str
    system_prompt: str
    user_prompt: str
    raw_response: str
    timestamp: int          # int(time.time())

    def as_dict(self) -> dict:
        # Caps system_prompt and user_prompt at 4000 chars (with truncation marker)
        # to keep HITL JSON files from growing unbounded.
        # Uses truncate_to_budget() from utils/context.py for the cap.
        ...
```

The `_truncate(s, limit=4000)` helper inside this module calls `utils/context.py`'s `truncate_to_budget()` for the cap — keeps the 4-chars-per-token approximation consistent with the rest of the codebase.

### 23.2 — Six LLM call sites: change return type to tuple

| File | Function | Return type |
|---|---|---|
| `ha_agent_core.py` | `analyze_config_locally` | `tuple[DiagnosticsReport, LLMTrace]` |
| `ha_agent_sandbox_engine.py` | `analyze_config_locally` | `tuple[DiagnosticsReport, LLMTrace]` |
| `ha_log_monitor.py` | `analyze_log_line_with_ai` | `tuple[LogEvaluation, LLMTrace]` |
| `netalertx/installer_diagnostics.py` | `diagnose_installer_failure` | `tuple[InstallerDiagnostic, LLMTrace, dict[str, str]]` |
| `netalertx/diagnosis.py` | `diagnose_health_report` | `tuple[Optional[NetAlertXDiagnostic], Optional[LLMTrace]]` |
| `netalertx/log_monitor.py` | `analyze_log_line_with_ai` | `tuple[LogEvaluation, LLMTrace]` |

At each site: capture `raw_output` before `model_validate_json`, build `LLMTrace(...)`, return as tuple. Exception / early-exit branches return a sentinel `LLMTrace(raw_response="")` — keeps the return type uniform and avoids `Optional[LLMTrace]` infecting callers (except for `diagnose_health_report`'s existing `None` early-exit path).

`diagnose_installer_failure` returns a **3-tuple** because the raw evidence dict (`gather_*_evidence` output) is built inside that function and must be surfaced to callers so it can be included in the HITL payload without duplicating the SSH calls.

### 23.3 — Thread trace + evidence to HITL payload

Five HITL call sites need enrichment:

**`ha_agent_sandbox_engine.py` — `main()`**
```python
report, llm_trace = await analyze_config_locally(...)
# in require_approval payload:
"diagnosis": report.model_dump(),
"evidence_raw": {"yaml_snippet": yaml_content[:2000]},
"llm_trace": llm_trace.as_dict(),
```

**`ha_log_monitor.py` — HITL path in triage function**
```python
evaluation, llm_trace = await analyze_log_line_with_ai(...)
# in notifier.send payload:
"diagnosis": evaluation.model_dump(),
"evidence_raw": {"log_buffer_snapshot": list(_log_buffer)},
"llm_trace": llm_trace.as_dict(),
```

**`netalertx/installer.py` — steps 2, 5a, 5b**
```python
diagnostic, llm_trace, evidence = await diagnose_installer_failure(...)
# in gate.require_approval payload:
"diagnosis": diagnostic.model_dump(),
"evidence_raw": evidence,
"llm_trace": llm_trace.as_dict(),
```

**`netalertx/log_monitor.py` — HITL path**
Same pattern as `ha_log_monitor.py`.

**Non-HITL callers** (`ha_agent_core.main()`, `netalertx/healer.py`) unpack the tuple and discard the trace with `_trace`.

### 23.4 — Test changes (`tests/test_core.py`)

**Direct-call tests that need tuple unpack** (~10 tests — change `result =` to `result, _trace =` or `result, _trace, _evidence =`):
- `TestLogMonitorTriage`: 3 tests
- `TestNetAlertXLogMonitor`: 3 tests
- `TestInstallerDiagnostics`: 2 tests (3-tuple unpack)
- `TestNetAlertXDiagnostic`: 2 tests

Pipeline tests (`asyncio.run(main(...))`) do **not** need changes — they observe side effects, not return values.

**New `TestLLMTrace` class** (9 tests):
1. `test_construction` — all fields set
2. `test_as_dict_keys` — five expected keys present
3. `test_system_prompt_truncated_in_as_dict` — long string → capped at 4000 chars with marker
4. `test_analyze_config_returns_trace` — `FakeLLMClient` → trace has `model` and `system_prompt` set
5. `test_analyze_log_returns_trace` — same for `ha_log_monitor`
6. `test_diagnose_installer_returns_evidence_dict` — third return is dict with SSH command keys
7. `test_hitl_payload_contains_llm_trace` — sandbox pipeline → `notifier.sent[0]["payload"]["llm_trace"]` has sub-keys
8. `test_hitl_payload_contains_diagnosis` — same → `payload["diagnosis"]["severity"]` present
9. `test_exception_branch_returns_sentinel_trace` — bad JSON from `FakeLLMClient` → `trace.raw_response == ""`

### Validation gate

- `black --check .` — no reformatting needed
- `mypy --ignore-missing-imports .` — all 6 return type annotations explicit; callers all unpack
- `bandit -r . -x ./tests,./.venv` — no new issues; `LLMTrace` fields are plain strings stored locally
- `pytest --cov --cov-fail-under=90` — existing 533 tests pass; new 9 tests pass

---

## Item 24 — Dashboard Evidence UI

**Scope:** Frontend only — 2 template files + 1 minor Python change.  
**Session estimate:** 1 session.  
**Depends on:** Item 23.

### 24.1 — `web/dashboard.py` — timestamp filter

```python
from datetime import datetime
# after Jinja2Templates(...):
templates.env.filters["epoch_to_iso"] = (
    lambda ts: datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
)
```

### 24.2 — `web/templates/base.html` — CSS additions

Add to existing `<style>` block: colour-coded section label chips (blue=evidence, green=diagnosis, amber=LLM), two-column `kv-grid` for key/value display, `white-space: pre-wrap` for log content.

### 24.3 — `web/templates/index.html` — 3 new collapsible sections

Insert between the existing `<p class="body">` and the existing full-payload `<details>`. All three guarded with `{% if r.payload.get(...) %}` so older HITL JSON files render unchanged.

**Evidence** (`payload.evidence_raw`):
- `log_buffer_snapshot` key → render as `<pre>` with lines joined by newline
- Any other shape → two-column `kv-grid` over the dict (installer command outputs)

**Diagnosis** (`payload.diagnosis`):
- Generic `kv-grid` over `model_dump()` output — works for `DiagnosticsReport`, `LogEvaluation`, and `InstallerDiagnostic` without the template knowing which type it received
- List fields joined with `", "` via `{% if v is iterable and v is not string %}`

**LLM Interaction** (`payload.llm_trace`):
- Model name + ISO timestamp via `epoch_to_iso` filter
- Three nested `<details>` (closed by default): System prompt / User prompt / Raw response — each in `<pre>`

**Rename existing fallback** from "Full payload" to "Full payload (raw JSON)" to clarify it's a developer fallback.

### 24.4 — Test changes (`tests/test_core.py`)

New `TestDashboardRichPayload` class (7 tests), reusing the existing `httpx.AsyncClient(app=app)` + `tmp_path` watch dir pattern from `TestDashboardRoutes`:

1. `test_evidence_section_rendered_when_present`
2. `test_evidence_section_absent_when_missing`
3. `test_diagnosis_section_rendered_when_present`
4. `test_llm_interaction_section_rendered_when_present`
5. `test_log_buffer_snapshot_rendered_as_pre`
6. `test_full_payload_fallback_still_present` — regression guard
7. `test_epoch_to_iso_filter_registered`

### Validation gate

- `pytest --cov --cov-fail-under=90` — all existing + new tests pass
- Visual: open `http://localhost:8080` with an enriched HITL JSON file → Evidence, Diagnosis, LLM Interaction sections render; "Full payload (raw JSON)" still visible
- Visual: open a pre-Item-23 HITL JSON file → no empty `<details>` sections; page renders cleanly

---

## Files Modified

| File | Change |
|---|---|
| `utils/llm_trace.py` | **NEW** — `LLMTrace` dataclass + `_truncate` helper |
| `ha_agent_core.py` | Return type change |
| `ha_agent_sandbox_engine.py` | Return type change + HITL payload enrichment |
| `ha_log_monitor.py` | Return type change + HITL payload enrichment |
| `netalertx/installer_diagnostics.py` | Return type → 3-tuple; surface evidence dict |
| `netalertx/installer.py` | 3-tuple unpack + HITL payload enrichment at steps 2/5a/5b |
| `netalertx/diagnosis.py` | Return type → Optional tuple |
| `netalertx/log_monitor.py` | Return type change + HITL payload enrichment |
| `netalertx/healer.py` | Tuple unpack (trace discarded) |
| `web/dashboard.py` | `epoch_to_iso` Jinja2 filter |
| `web/templates/base.html` | CSS additions |
| `web/templates/index.html` | 3 new collapsible sections; fallback renamed |
| `tests/test_core.py` | ~10 tuple-unpack fixes; 9 new `TestLLMTrace` tests; 7 new `TestDashboardRichPayload` tests |

## Utilities to Reuse

- `utils/context.py` → `truncate_to_budget()` — reuse for `_truncate` helper in `llm_trace.py`
- `utils/interfaces.py` → `LLMClientProtocol` / `FakeLLMClient` — **unchanged**; trace is built from already-available inputs, not from the client's return value
- `TestDashboardRoutes` pattern in `tests/test_core.py` — reuse `httpx.AsyncClient` + `tmp_path` for `TestDashboardRichPayload`
