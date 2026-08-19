# ADR 016 — Diagnostic WAN fetch via `fetch_url`

## Status
Accepted

## Context
Pueo's investigation loops can read logs, HA config, and HA component source code, but they cannot verify whether an external service is currently reachable. During a NOAA Tides integration failure (NameResolutionError + 504), the correct diagnosis was that the NOAA CO-OPS API was transiently unreachable — but confirming that the API had recovered required a manual HTTP probe that Pueo could not automate.

Without an outbound HTTP tool:
- The agent reasons about external API availability from log evidence alone, which is indirect and stale.
- Confirming recovery requires the user to manually check the API and report back.
- The agent cannot distinguish "API is still down" from "API recovered between the log line and now."

## Decision
Add a `fetch_url(url)` tool registered in all three agent registries (HA, Chat, NetAlertX). The tool performs an HTTP GET to an external URL and returns the response body (truncated at 8,000 characters).

### Security constraints
- **GET only** — no POST, PUT, PATCH, DELETE; the tool has no body parameter.
- **Private IP block** — `_PRIVATE_IP_BLOCKS` in `utils/tool_executor.py` blocks all RFC-1918 ranges, loopback (127.x, ::1), link-local (169.254.x), and the HA local hostname.
- **Response size cap** — 8,000 characters; prevents the agent's context window from being flooded by large HTML pages.
- **Config flag** — `ALLOW_DIAGNOSTIC_WAN` (default `true`); users who want strict 0-WAN operation set this to `false` in `config.yaml`.
- **Timeout** — `DIAGNOSTIC_WAN_TIMEOUT_SECONDS` (default 60), configurable.

### What `fetch_url` is for
Verifying that an external service has recovered after a suspected outage (e.g., `fetch_url("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?...")` with the same parameters visible in the failing log line). It is not for scraping HA's own API or for any write operation.

## Rationale
The private-IP block list ensures the tool cannot be used to probe the local network or call HA's own REST API (which should use `run_ha_command` or `HARestClientProtocol`). The config flag gives users who deploy Pueo in strictly firewalled environments a single toggle to disable WAN fetches entirely.

The `ALLOW_DIAGNOSTIC_WAN` flag is distinct from `LLM_PROVIDER`'s WAN gate. Even in `LLM_PROVIDER=local` mode (0 WAN for inference), diagnostic probes are a different class of network access — they are a one-off GET to verify a hypothesis, not a streaming inference call. Separating the flags gives users fine-grained control.

## Consequences
- `FETCH_URL` ToolDefinition in `utils/tool_registry.py`; registered in `build_ha_tool_registry()`, `build_chat_tool_registry()`, `build_netalertx_tool_registry()`
- `_fetch_url()` method in `utils/tool_executor.py` with `_PRIVATE_IP_BLOCKS` and `_MAX_FETCH_URL_CHARS` module constants
- Config: `ALLOW_DIAGNOSTIC_WAN` (bool, default True) and `DIAGNOSTIC_WAN_TIMEOUT_SECONDS` (int, default 60) — triple-update: `config.py` + `config.yaml.default` + `setup.sh`
- Tests: 6 in `TestFetchUrl` covering all security constraint branches

## Related decisions
- [ADR 006 — LLM provider abstraction](006-llm-provider-abstraction.md): `ALLOW_DIAGNOSTIC_WAN` is independent of `LLM_PROVIDER`; they gate different classes of WAN access.
- [ADR 011 — HA live lookup](011-ha-live-lookup.md): `fetch_ha_docs` serves from cache in local mode; `fetch_url` is always live (governed by `ALLOW_DIAGNOSTIC_WAN`). The two tools complement each other — `fetch_ha_docs` retrieves HA source, `fetch_url` probes external APIs.
- [ADR 012 — Hypothesis-driven repair](012-hypothesis-driven-repair.md): `fetch_url` is the verification step that closes the investigation loop — confirms or refutes the transient-outage hypothesis with live data rather than log inference.
