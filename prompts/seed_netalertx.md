---
# No signature: netalertx sessions use semantic RAG retrieval.
# Add signature: netalertx:<category> and sig_netalertx() in runbook_signature.py
# when netalertx AgentLoops start passing typed signatures.
---

# NetAlertX investigation — method

This seed applies to all NetAlertX investigations (health, availability, config, heal).

## Locating the add-on and its config

**Slug and conf path:** always call `read_netalertx_conf` first when you need to inspect
app.conf. The tool reads the file from the right path on the HA host (`/app_configs/<slug>/config/app.conf`
or `/addon_configs/…` on older HA) and redacts sensitive fields. Never guess the path.

If you need the slug for other purposes, call `run_ha_command("ha apps list")` and parse
the slug for `https://github.com/jokob-sk/NetAlertX` from the output.

**App-config root:** HA renamed `/addon_configs` to `/app_configs` in HA 2026.x (Add-ons →
Apps). Use `ha apps info <slug>` to confirm the add-on is running before reading logs.

## Conf drift — deterministic fixer, not LLM

`SCAN_SUBNETS` containing a 172.16.0.0/12 subnet (Docker/Supervisor bridge noise) is a
**deterministic, auto-correctable issue**. Pueo's startup reconcile (`reconcile_scan_subnets`)
removes it automatically. If it still appears at diagnosis time, ask: did the reconcile run?
(`grep reconcile_scan_subnets ~/Library/Logs/Pueo/pueo.log | tail -5`).

Do **not** send this to the heal loop. Report it with `severity="MEDIUM"` and
`recommended_fix="Restart Pueo to trigger startup reconcile"`.

## App.conf is compiled as Python

NetAlertX `compile()`s `app.conf` as a Python source file. Every value must be a valid
Python literal: strings must be quoted (`'value'` not `value`), empty values must be `''`
not bare, and lists must use Python syntax (`['x', 'y']`). A bare `KEY = ` (no value)
causes a `SyntaxError` on next startup. When `rewrite_netalertx_conf` is called, the safe
writer (S1) runs `compile()` before writing and aborts on error.

## Restart and verify

After any `restart_netalertx` call, the tool polls `get_about()` for up to 2 minutes. If
it fails, it auto-restores `app.conf.pueo-lkg` and retries. The tool returns failure if
both attempts fail. Never call `restart_netalertx` without checking the return value.

## Availability investigation

When the category is `availability` (API unreachable), collect evidence in this order:
1. `read_logs(log_source="ha_app", addon_slug=<slug>, lines=100)` — look for SyntaxError, crash, OOM.
2. `read_netalertx_conf` — check for syntax errors in the current conf.
3. `run_ha_command("ha apps info <slug>")` — confirm running state.
4. If logs show a SyntaxError: use `rewrite_netalertx_conf` with corrected values + `restart_netalertx`.
5. If logs show a crash loop: escalate with `request_escalation`.
