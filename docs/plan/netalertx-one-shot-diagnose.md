# Item 27 — NetAlertX One-Shot Diagnosis (`--mode netalertx-diagnose`)

Part of the [Implementation Plan](../implementation-plan.md) · 1 session.

**Depends on:** Items 15–18 (log monitor, health poller, diagnosis, healer), Item 20
(plain-text console formatter).

## Status
✅ Done (2026-07-22)

## Problem

`--mode netalertx` is a reactive daemon — it waits for errors to appear in logs or the health
API to go bad, but it doesn't answer the question "what is wrong right now?" There is no
one-shot equivalent of `--mode diagnose` for NetAlertX: a command that inspects the current
state, synthesises a diagnosis, and optionally attempts a fix.

## Solution

A new `--mode netalertx-diagnose` that wires together the already-built primitives into a
single proactive pass:

1. **Connectivity check** — `GET /health` via the NetAlertX API. Unreachable → clear error,
   exit early, no diagnosis attempted.
2. **Health report** — `NetAlertXHealthMonitor.poll_once()` with an empty MQTT queue. No MQTT
   subscriber is needed for a one-shot.
3. **Log snapshot triage** — `docker exec {container} tail -n 100 /data/app.log` over SSH.
   If any line matches `CRITICAL_LOG_PATTERN`, feed the snapshot to
   `analyze_log_line_with_ai()` and capture the `LogEvaluation`.
4. **Config validation** — `ssh_client.read_file("/data/app.conf")` then
   `validate_app_conf()` → `list[ConfigIssue]`.
5. **AI synthesis** — `diagnose_health_report(report, config_issues)` →
   `(NetAlertXDiagnostic | None, LLMTrace | None)`.
6. **Print summary** — human-readable output (severity, category, issue, recommended fix,
   log triage result if present). `console_text=True` activates the plain-text formatter,
   consistent with `--mode netalertx-setup`.
7. **Optional healing** — if `diagnostic is not None`, pass it to
   `NetAlertXHealer.heal(diagnostic)`, gated by `AutonomyGate` at the configured autonomy
   level. At level 1 (report only), `heal()` is never called.

## Entry point structure

New file `netalertx/one_shot_diagnose.py`:

```python
async def run_diagnose(
    ssh_client: SSHClientProtocol | None = None,       # NetAlertX host
    ha_ssh_client: SSHClientProtocol | None = None,    # HA host (for healer only)
    api_client: NetAlertXAPIClient | None = None,
    llm_client: LLMClientProtocol | None = None,
    gate: AutonomyGate | None = None,
    notifier: NotifierProtocol | None = None,
) -> None:
    # 1. Connectivity check — exit early on failure
    try:
        await _api.get_about()
    except Exception as exc:
        log.error("netalertx_api_unreachable", error=str(exc))
        return

    # 2. Health report (no MQTT subscriber)
    monitor = NetAlertXHealthMonitor(api_client=_api)
    report = await monitor.poll_once(asyncio.Queue())

    # 3. Log snapshot triage
    log_lines = await _fetch_log_snapshot(_ssh)
    log_evaluation = None
    if any(CRITICAL_LOG_PATTERN.search(line) for line in log_lines):
        log_evaluation, _ = await analyze_log_line_with_ai(log_lines, llm_client=_llm)

    # 4. Config validation
    try:
        conf_text = await _ssh.read_file("/data/app.conf")
        config_issues = validate_app_conf(conf_text)
    except (FileNotFoundError, OSError):
        config_issues = []

    # 5. AI synthesis
    diagnostic, llm_trace = await diagnose_health_report(report, config_issues, _llm)

    # 6. Print summary
    _print_summary(report, log_evaluation, config_issues, diagnostic)

    # 7. Optional healing
    if diagnostic is not None:
        healer = NetAlertXHealer(
            gate=_gate, ssh_client=_ssh, ha_ssh_client=_ha_ssh, ...
        )
        await healer.heal(diagnostic)
```

`_fetch_log_snapshot()` runs `docker exec {NETALERTX_LOG_CONTAINER_NAME} tail -n 100
/data/app.log` via SSH and returns the lines as a list. If the command fails (container not
running), return an empty list and log a warning — do not abort.

`_ha_ssh_client` defaults to a new `AsyncSSHClient` pointed at `HA_HOST`. It is only used
if `healer.heal()` is called and the diagnostic category is `mqtt` or `ha_integration`; the
healer handles absence gracefully for other categories.

## Files to change

| File | Change |
|------|--------|
| `netalertx/one_shot_diagnose.py` | **New.** `run_diagnose()` async entry point + `_fetch_log_snapshot()` helper |
| `main.py` | Add `netalertx-diagnose` to `choices`; dispatch with `console_text=True` (same as `netalertx-setup`) |
| `README.md` | Add `netalertx-diagnose` row to the command table |
| `tests/test_netalertx_one_shot_diagnose.py` | **New.** 5 tests (see below) |
| `docs/implementation-plan.md` | Add item 27 to status table; add Phase 9 section |

## Tests

Five tests in a new `tests/test_netalertx_one_shot_diagnose.py`:

| Test | Scenario | Key assertions |
|------|----------|----------------|
| `test_api_unreachable_exits_early` | `api_client.get_about()` raises `OSError` | `diagnose_health_report` never called; `healer.heal` never called |
| `test_healthy_system_no_heal` | No anomalies, no config issues, no critical log lines | `diagnose_health_report` returns `(None, None)`; `healer.heal` not called |
| `test_stale_scan_triggers_diagnosis_and_heal` | `poll_once` returns a report with stale-scan anomaly | `diagnose_health_report` called; `healer.heal` dispatched with the diagnostic |
| `test_config_issues_trigger_diagnosis` | `validate_app_conf` returns one HIGH issue | `diagnose_health_report` called with that `ConfigIssue` in the list |
| `test_critical_log_line_triggers_triage` | Log snapshot contains an `ERROR.*scan` line | `analyze_log_line_with_ai` called; result captured in summary |

Use `FakeSSHClient`, `FakeLLMClient`, and `FakeAutonomyGate` — no real SSH or Ollama calls.

## Done criteria

- `python main.py --mode netalertx-diagnose` connects, prints a human-readable summary of
  NetAlertX health, config issues, and log triage, then optionally heals — all in one pass.
- All five tests pass.
- Full CI gate passes:

```bash
black --check .
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
mypy --ignore-missing-imports .
bandit -r . -x ./tests,./.venv
pytest --cov --cov-fail-under=90
```
