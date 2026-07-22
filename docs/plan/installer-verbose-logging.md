# Item 26 — Installer Verbose Progress Logging

Part of the [Implementation Plan](../implementation-plan.md) · 1 session.

**Depends on:** Item 20 (plain-text console formatter), Items 11–12 (installer state machine).

## Status
✅ Done (2026-07-22) — PR #48

## Problem

Running `--mode netalertx-setup` already emits log lines at each step boundary, but the terminal
goes silent for long stretches with no indication of what Pueo is doing or whether it is stuck.
Specific gaps observed during a live install run (2026-07-21):

- **Poll loops** — `_poll_addon_state` and `_poll_addon_not_state` loop for up to 5 minutes with no
  output between attempts. The terminal looks hung.
- **Slow SSH commands** — commands like `ha apps install`, `ha apps start`, `ha apps restart`, and
  `ha core restart` can take 30–120 seconds. There is no "about to run X" line before the wait.
- **HITL gate waits** — after `gate.require_approval()` sends a notification, there is no terminal
  line saying Pueo is waiting for human input. The user may not know an approval is pending.
- **HA restart wait** — after the step 8 HA core restart, Pueo waits ~60 seconds for HA to come
  back up. No terminal output during this wait.

## Solution

Add targeted `log.info()` calls at the points where the terminal goes silent. No new
infrastructure needed — the plain-text console formatter from item 20 already renders these as
human-readable lines.

### Poll loop: log each attempt

In `_poll_addon_state` and `_poll_addon_not_state`, log at the start of each iteration:

```python
log.info(
    "poll_waiting",
    addon_id=addon_id,
    expected=expected,
    attempt=attempt_num,
    elapsed_s=round(attempt_num * delay),
)
```

### Before slow SSH commands

Add a `log.info("running_command", cmd=...)` line immediately before each long-running SSH call
(`ha apps install`, `ha apps start`, `ha apps restart`, `ha core restart`). The existing
post-command log lines already capture success/failure; these new lines fill the gap before the
command returns.

### Before and after HITL gate waits

In `AutonomyGate.require_approval()`, log before blocking:

```python
log.info("hitl_waiting_for_approval", subject=subject, risk=risk.name)
```

And when the decision comes back:

```python
log.info("hitl_approval_received", approved=approved, subject=subject)
```

### HA restart wait (step 8)

After issuing `ha core restart`, log that HA is restarting and Pueo is waiting:

```python
log.info("ha_restarting", wait_s=60)
await asyncio.sleep(60)
log.info("ha_restart_wait_complete")
```

## Expected terminal output (netalertx-setup, step 5)

```
INFO     step5_start  step='install_addon'  slug='db21ed7f_netalertx_fa'
INFO     running_command  cmd='ha apps install db21ed7f_netalertx_fa'
INFO     poll_waiting  addon_id='db21ed7f_netalertx_fa'  expected='unknown'  attempt=1  elapsed_s=0
INFO     poll_waiting  addon_id='db21ed7f_netalertx_fa'  expected='unknown'  attempt=2  elapsed_s=5
INFO     poll_waiting  addon_id='db21ed7f_netalertx_fa'  expected='unknown'  attempt=3  elapsed_s=10
INFO     step5_addon_installed  slug='db21ed7f_netalertx_fa'
INFO     running_command  cmd='ha apps start db21ed7f_netalertx_fa'
INFO     poll_waiting  addon_id='db21ed7f_netalertx_fa'  expected='running'  attempt=1  elapsed_s=0
INFO     step5_addon_running  slug='db21ed7f_netalertx_fa'
```

And for an HITL wait (step 7):

```
INFO     hitl_waiting_for_approval  subject='NetAlertX installer: configure MQTT integration in HA'  risk='MEDIUM'
INFO     hitl_approval_received  approved=True  subject='NetAlertX installer: configure MQTT integration in HA'
```

## Files to change

| File | Change |
|------|--------|
| `netalertx/installer.py` | Log before slow SSH commands; log each poll attempt |
| `utils/autonomy.py` | Log before blocking on approval; log decision |
| `tests/test_core.py` | Assert new log keys appear in relevant test scenarios |

## Tests

- `test_poll_addon_state_logs_each_attempt` — verify `poll_waiting` is logged N times when polling
  runs N iterations before success
- `test_autonomy_gate_logs_hitl_wait_and_result` — verify `hitl_waiting_for_approval` and
  `hitl_approval_received` are emitted by `FakeAutonomyGate` (or the real gate under test)

## Verification

```bash
black --check .
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
mypy --ignore-missing-imports .
bandit -r . -x ./tests,./.venv
pytest --cov --cov-fail-under=90
```
