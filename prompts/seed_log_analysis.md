---
trigger_pattern: "analyze log lines from [time range]|what happened in the log|log analysis [HH:MM]|Analyze the log from|what happened between|sparkline|log window"
recommended_tools: [summarize_log_window, search_log, read_pueo_log]
state: seed
---

# Runbook: Time-Range Log Analysis

## When this applies

The user (or code) has asked to analyze Pueo or HA logs for a specific time window.
The sparkline click in the dashboard generates this query with a pre-filled time range.

## Phase 3 — Investigation plan

1. **`summarize_log_window(log_name, after, before)`** — get a structured digest: counts
   by level, full text of ERROR/WARNING events, INFO suppressed with count. This is the
   only call needed to get oriented. Do NOT use `read_pueo_log` for time-range queries —
   it has no time filter and returns unrelated lines.

2. **If errors are found**: call `search_log(log_name, pattern=<error_keyword>)` to get
   surrounding context for each distinct error type. One call per error pattern.

3. **If no errors**: summarize what INFO events show was happening during that window
   (loop restarts, triage calls, SSH operations, etc.).

4. **If the window is quiet** (very few events): expand the window and look before/after
   to understand whether the quiet period is normal or a symptom of a stopped loop.

## Phase 5 — Response format

Provide:
- Time window analyzed
- Brief summary: "N errors, M warnings, K info events"
- For each error: what happened and (if determinable) why
- For each warning: same
- Overall assessment: normal operation / degraded / error state

## Gaps to note

If errors are found that Pueo cannot explain from its knowledge base, call
`save_runbook(type="gap")` describing the error and what investigation was attempted.
