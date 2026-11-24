# Test Suite Gap Analysis — adversarial QA / security review

## Context
Asked for a gap analysis of the unit suite (no code edits): edge cases, boundary conditions, shallow assertions, and whether a refactor would speed it up. Measured, not guessed: full unit run on 2026-11-24 = **6,777 passed in 208 s wall, but only 34 s CPU (16%)**. The rest is time spent sleeping.

## Overall verdict
Hygiene is above average. Only 54 of about 6,600 tests have no assertion, and most of those are intentional "does not raise" no-op tests. There are no `async def` tests that silently never run. The network and live-DB guards are strong. The real risk is elsewhere: **the security guards are tested only with inputs that already fit what the code expects**. Several of them have bypasses that no test exercises.

---

## P0 — Security guards with untested (and likely exploitable) edge cases

### 1. `classify_service_risk` is case-sensitive → permanent block bypass
`utils/ha/service_policy.py:82` uses `fnmatch` on raw `f"{domain}.{service}"` and does no `.lower()` or `.strip()`. The callers don't normalise either (`utils/agent/tool_executor.py:2844`, `web/dashboard.py:1775`). `call_service("Homeassistant", "Restart")` misses the `homeassistant.restart` block, falls through to **HIGH, which can be approved**, and HA lowercases domain and service when it executes the call. The same applies to `Hassio.*`, `Backup.*`, `Shell_Command.*` and `Recorder.Purge`.
- The tests in `tests/test_service_policy.py` use only lowercase literals.
- Missing cases: mixed case, leading/trailing whitespace, `homeassistant.restart ` with a trailing space, empty service, `light.turn_on` vs `lights.turn_on`, an `input_`-prefixed domain the user doesn't own (`input_anything` → LOW).

### 2. `fetch_url` SSRF block is a string-prefix match on the hostname
`tool_executor.py:262` checks `hostname.startswith(...)` against a prefix list. Nothing in the tests covers these:
- **Bypasses:** `http://[::ffff:127.0.0.1]/`, `http://0.0.0.0/`, `http://2130706433/` (decimal), `http://0177.0.0.1/` (octal), `http://localhost./`, any `*.local` other than `homeassistant.local`, DNS names that resolve to RFC-1918 (`127.0.0.1.nip.io`), `100.64.0.0/10` CGNAT (Tailscale), and **HTTP redirects**: `urllib.request.urlopen` follows a 302 to `http://192.168.x.x` with no re-check.
- **False positives:** the `"fc"`/`"fd"` IPv6 prefixes also block legitimate hostnames like `fdroid.org` and `fcc.gov`.
- The tests (`tests/test_tool_executor.py:279-287`) cover only `192.168.1.1` and `127.0.0.1`.
- The fix belongs in code (resolve, then `ipaddress.ip_address(...).is_private/is_loopback/is_link_local/is_reserved`, plus a redirect handler that re-validates each hop). The tests should be a parametrized table of the bypasses above.

### 3. `ReadOnlySSHClient.is_readable_path` deny-list is too narrow
`utils/ha/read_only_ssh.py:34-61` denies only `secrets.yaml` and `.storage/auth*`. Readable, untested, and sensitive:
- `/config/.storage/core.config_entries`: integration passwords, tokens and API keys.
- `/config/.storage/http`, `/config/.storage/onboarding`, `/config/.storage/cloud`.
- `/backup/*.tar`: full archives that contain `secrets.yaml`.
- `/config/secrets.yml` and other secret files that aren't named `secrets.yaml` (e.g. `esphome/*.yaml` with inline keys).
- A remote symlink (`/config/x -> /etc/shadow`) can't be detected client-side. This needs to be documented as accepted risk or mitigated with `realpath` over SSH.

The tests (`tests/test_read_only_ssh.py:84-104`) list 3 allowed and 8 denied paths. Missing cases: `//config/secrets.yaml`, `/config/./secrets.yaml`, a trailing slash, an encoded `%2e%2e`, a NUL byte, a newline in the path, and an empty string.

### 4. Command allowlist regexes use `$`, which matches before a trailing `\n`
`_READ_ONLY_COMMAND_PATTERNS`, `_SLUG_RE` and `_UUID_RE` all end in `$` rather than `\Z`. `"ha core logs --lines 10\n"` passes. A trailing newline alone does no harm, but `_is_ha_command_allowed` splits on whitespace, so the check and the executed string can diverge. No test covers `\n`, `\r`, tabs, Unicode lookalikes, or `$(...)` / backticks inside a slug position. Add these to the parametrized blocked list and switch to `re.fullmatch`.

### 5. `propose_patch` safety list covers only 3 files
`_SAFETY_CRITICAL_PATHS` (`tool_executor.py:289`) = `autonomy.py`, `interfaces.py`, `config.py`. The agent can propose patches to the files that **define the other guards**:
- `utils/agent/tool_executor.py`, which holds the allowlists, the SSRF list and this very set
- `utils/ha/read_only_ssh.py`
- `utils/ha/service_policy.py`
- `agents/ha_agent_sandbox_engine.py`, which runs the backup chain

There is no test asserting that patching any of these is rejected. The only traversal test is a dashboard 404 (`test_dashboard.py:12247`). Missing cases: symlink inside the repo pointing out, case variants on macOS's case-insensitive filesystem (`Config.py` resolves to `config.py` but `rel` keeps the input case → **bypasses the frozenset check**), and `utils/agent/../agent/autonomy.py`.

---

## P1 — Shallow or misleading assertions on core business logic

| Area | Finding | Evidence |
|---|---|---|
| Safety invariant (backup → slug → write) | No test asserts **ordering**: no call-order recorder checks that `execute_remote_backup` precedes `record_backup_slug`, which precedes the SFTP write. Tests check that each step was called, not the sequence. A refactor that writes first and backs up second would pass. | grep for order or `.index(` near backup: none |
| `BackupRateLimitError` | Only 2 tests. No boundary tests at exactly N, N+1 or 6 within the rolling hour, at the 3600 s edge, or at hour rollover. | `test_core_agent.py:2228,2288` |
| Rate limiter / debouncer | 3 tests have no assertion ("allows calls under limit", "allows again after period", "sliding window"). They pass if the call merely doesn't raise. | `test_utils.py:201,217,230` |
| AutonomyGate | Test names are off by one after the ADR 035 renumber (`test_level1_*` constructs `level=0`; the class comment says "4 levels × 4 risks" while there are 5). Untested: invalid level (`-1`, `5`, `"3"`), and level 2 `require_approval` with LOW. | `test_hitl.py:507-575` |
| Upgrade advisor | `test_core_uses_analyze_version_service` asserts the service call but never checks the **returned report**. It also waits 10–20 s of real time. | `test_upgrade_advisor.py:463` |
| `assert x is not None` / `len(x) > 0` | About 250 occurrences. Many guard results that should be compared to an exact expected value. Sample and tighten them in safety and verdict paths first (update analysis, runbook distiller, verification). | grep counts |
| `assert "<substr>" in output` | 1,171 occurrences. Fine for UI text, weak for verdict and risk fields: `"HIGH" in out` also matches `"HIGHLY"`. Prefer structured comparisons where the output is JSON. | grep counts |
| Un-awaited AsyncMocks | 4 `coroutine ... never awaited` warnings. A mocked async function is called without `await`: either the mock type is wrong or production code forgets to await. Both mean the asserted path didn't really run. | `test_ha_update_manager.py::TestFinishBCAssessment::test_other_verdicts_pass_without_proof_line`, `TestHacsInstallSshFallback::test_hacs_ssh_fallback_no_issue_found`, `TestExecuteAddonUpdateTimeline::test_timeline_event_on_failure`, `test_hardware.py::TestApplyModelSelection::test_updates_config_and_writes_yaml` |

### Missing boundary classes (cross-cutting)
- **Token budget** (`truncate_to_budget`): exactly at 8,000, budget−1/+1, multibyte/emoji content, empty input.
- **Log watermark replay** (#975): equal timestamps (`==` vs `<`), DST transition, Z vs offset (the #1278 regression area), clock going backwards.
- **Recurrence threshold** (`LOG_RECURRENCE_THRESHOLD`): count = threshold−1, threshold, and the exact 7-day window edge.
- **Host-health cooldown**: exactly 24 h, a severity rise at the edge.
- **Adaptive LLM timeout**: P95 with 0 or 1 samples, clamp at both ends.

---

## Performance — yes, refactor, but mostly in targeted places

**Measured:** 208 s wall vs 34 s CPU. **About 17 tests account for about 165 s (~80%)** because they sleep for real:

| Test | Time | Cause |
|---|---|---|
| `test_netalertx.py::TestOneShotDiagnoseTokenPreRead::test_empty_token_read_from_app_db_before_get_about` | 32.7 s | real retry/poll waits |
| `test_upgrade_advisor.py::TestRequestAdvisorAnalysis::*` (4 tests) | 50 s | `_POLL_INTERVAL_SECONDS` real `asyncio.sleep` |
| `test_device_availability.py::TestPollCoverage/TestPollPredictionChecks/TestAdditionalCoverage` (9 tests) | 50 s | 5–10 s loop/settle sleeps |
| `test_core_agent.py::TestToolExecutor::test_verify_fix_ssh_exception`, `test_read_only_ssh.py::TestExecutorUnderGuard::test_trigger_backup_fails_cleanly` | 12 s | retry backoff |
| `test_config_flow_driver.py::TestDriveConfigFlowInvalidHandler::test_invalid_handler_retries_once` | 3 s | retry delay |
| About 20 `test_disk_recovery.py` tests | ~0.5 s each | fixed 0.5 s sleep |

Recommended, in order of payoff:
1. **Make poll intervals and retry delays injectable** (module constant monkeypatched to 0, or a `sleep=` parameter). That brings the run from about 208 s to **about 45 s** with no loss of coverage.
2. **Add a guard in `conftest.py`**: an autouse fixture that fails any unit test taking more than 2 s wall time, so new slow tests can't creep back in. It would work like the network guard: a `pytest_runtest_call` hook that measures the call.
3. **Use `pytest-xdist`** (already in `requirements-dev.txt`, unused by CI): `pytest -n auto`. Check isolation first, because `isolated_config` reloads modules and the session-scoped temp DB is shared. Run `-n 4` locally to flush out order coupling, and add `pytest-randomly` to catch hidden inter-test state.
4. **Don't split the monolith files for speed.** `test_core_agent.py` (22.8k lines), `test_utils.py` (14.8k), `test_ha_update_manager.py` (14.4k) and `test_dashboard.py` (12.5k) cost nothing at runtime. Splitting them is a maintainability task, not a performance one. Treat it as optional, and do it only alongside the work in item 1.
5. Two `asyncio.iscoroutinefunction` DeprecationWarnings (removed in 3.16) and the `_OllamaEmbeddingFunction` legacy config warning. Fix these before the Python bump.

---

## Implementation sessions (APPROVED 2026-11-24)
Each session runs on its own: start with `git checkout main && git pull`, create the GitHub issue, branch `feat/<N>-<slug>`, write the code and its tests, run the full CI gate, open the PR. Keep each one small enough to finish without autocompaction.

| S | Scope | Labels | Key files | Done when |
|---|---|---|---|---|
| S1 | P0-1 `classify_service_risk` normalises `domain`/`service` (`.strip().lower()`); dashboard re-check gets the same treatment; parametrized mixed-case, whitespace and `input_` tests | security | `utils/ha/service_policy.py`, `tests/test_service_policy.py` | `c('Homeassistant','Restart') is None` |
| S2 | P0-2 `fetch_url`: resolve the host and check with `ipaddress` (private/loopback/link-local/reserved/multicast/unspecified, CGNAT 100.64/10, IPv4-mapped v6); remove the `fc`/`fd` string prefixes; a redirect handler re-validates each hop; bypass table test (with `getaddrinfo` faked, because the network guard is on) | security | `utils/agent/tool_executor.py` `_fetch_url`, `tests/test_tool_executor.py` | every bypass is blocked; `fdroid.org` is allowed |
| S3 | P0-3 + P0-4: widen the `ReadOnlySSHClient` deny list (`.storage/core.config_entries`, `http`, `cloud`, `onboarding`); **ask user** whether `/backup/*.tar` reads must stay; path edge-case tests; switch `_READ_ONLY_COMMAND_PATTERNS`, `_SLUG_RE` and `_UUID_RE` to `fullmatch`/`\Z`; tests with `\n`, `\r`, `\t`, `$( )` and backticks | security | `utils/ha/read_only_ssh.py`, `tool_executor.py`, `tests/test_read_only_ssh.py` | new cases pass; ADR 043 amended |
| S4 | P0-5: add `tool_executor.py`, `read_only_ssh.py`, `service_policy.py` and `ha_agent_sandbox_engine.py` to `_SAFETY_CRITICAL_PATHS`; compare against the case-folded canonical path; tests for `Config.py`, `./`, and an internal `..` path | security | `tool_executor.py`, `tests/test_chat.py` or `test_tool_executor.py` | all variants rejected; ADR 007 note |
| S5 | P1 assertions: backup-chain call-order test (backup → `record_backup_slug` → write), `BackupRateLimitError` N/N+1/3600 s edge, assertions added to the rate-limiter tests (`test_utils.py:201,217,230`), gate test names fixed and invalid-level tests added, the 4 un-awaited AsyncMock tests fixed (production code if it is a real missing await) | test | tests mostly | warnings gone |
| S6 | P1 boundary classes: `truncate_to_budget`, log watermark equality, DST and offset, `LOG_RECURRENCE_THRESHOLD` −1/=/window edge, host-health 24 h edge, `expected_timeout_ms` with 0 or 1 samples and both clamps | test | tests only (fix code if a boundary bug turns up) | — |
| S7 | Perf: injectable poll and retry sleeps for the 17 slow tests; a conftest slow-test guard (>2 s fails); `pytest -n auto` in CI after checking isolation (run `-n 4` locally plus `pytest-randomly` once); fix the 3.16 `iscoroutinefunction` deprecations | perf | `utils/ha/upgrade_advisor.py`, `utils/ha/device_availability.py`, netalertx diagnose, `conftest.py`, `.github/workflows/test.yml`, `requirements-dev.txt` (dependency change → reviewer note) | unit run ≤ ~60 s |

S1–S4 each need `/security-review` (CLAUDE.md step 10). S1–S4 come first, in order. S5–S7 can be done in any order.

## Suggested follow-up (if approved, one issue each, per the issue-first workflow)
1. **security** `service_policy` case normalisation plus tests: P0-1.
2. **security** `fetch_url` IP-based SSRF check, redirect re-validation, and a bypass test table: P0-2.
3. **security** widen the `ReadOnlySSHClient` deny-list (`.storage/core.config_entries`, `/backup/*.tar`?) plus path edge-case tests: P0-3.
4. **security** `re.fullmatch` for the command allowlists, control-char tests, and expansion of `_SAFETY_CRITICAL_PATHS` with case-folded comparison: P0-4 and P0-5.
5. **test** a backup-chain ordering assertion and `BackupRateLimitError` boundaries: P1.
6. **test** fix the no-assert rate-limiter tests, the gate test names, and the un-awaited AsyncMocks: P1.
7. **perf** injectable sleeps, the slow-test guard, and xdist in CI.

Items 1–4 are code fixes, not just tests, so they also need `/security-review` per CLAUDE.md step 10.

## Verification of this report's claims
- `pytest --durations=40 -q --ignore=tests/integration` gives the timings above.
- For P0-1: `python -c "from utils.ha.service_policy import classify_service_risk as c; print(c('Homeassistant','Restart'))"` should print `RiskLevel.HIGH` (not `None`).
- For P0-2: call `ToolExecutor._fetch_url("http://0.0.0.0/")` with `urlopen` patched and confirm it is not blocked.
- For P0-5: on macOS, `_resolve_repo_path("Config.py")` resolves, and `rel` keeps its input case.
