# ADR 017 — Chat tool parity: shared enrichment functions and client completeness

## Status
Accepted

## Context
`enrich_http_login()` in `ha_notification_manager.py` performs rich IP enrichment: ARP→MAC, OUI vendor lookup, randomized-MAC detection, reverse DNS, NetAlertX device match, HA device registry lookup, and DHCP hostname from router leases. The automated notification pipeline supplies all three arguments — `ip`, `netalertx_client`, and `ws_client`.

The chat `INVESTIGATE_DEVICE` tool (`utils/tool_executor.py::_investigate_device`) imports and calls the same function, but was calling it with `ws_client=None`. The HA device registry step was silently skipped in every chat session. The root cause was structural: `ToolExecutor.__init__` had no `ha_ws_client` parameter, so there was no client to pass.

This revealed a general gap: `ToolExecutor` holds references only to the clients it was originally designed around (SSH, NetAlertX API, LLM, knowledge store). As new shared functions were imported into chat tools, there was no established rule requiring the caller to pass the same full set of arguments as the automated pipeline.

## Decision
Two rules, enforced structurally and by code review:

### Rule 1 — Shared functions receive the same clients in both contexts
Any enrichment or analysis function callable by an automated pipeline must be callable from chat with the same set of clients. When a shared function is imported inside a `ToolExecutor` method, it must receive the same arguments as the automated caller — never `ws_client=None` or other stubs that silently degrade the result.

### Rule 2 — `ToolExecutor` is the authority on available clients
`ToolExecutor` must hold a reference to every client type needed by the tools it implements. Adding a new shared function that requires a client not yet in `ToolExecutor.__init__` is a signal to add that client, not to stub it out.

When the client is only available after construction (e.g., `ha_ws_client` requires `HA_API_TOKEN`, which may not be known at executor creation time), use the `set_*` deferred-injection pattern already established by `set_ha_profile()`. `main.py` calls the setter once the client is available.

### Concrete fix
- Added `ha_ws_client: Optional[HAWebSocketClientProtocol] = None` to `ToolExecutor.__init__`
- Added `set_ws_client(client)` deferred-injection method
- `_investigate_device` now passes `ws_client=self._ws_client`
- `main.py` calls `_shared_executor.set_ws_client(_profile_ws)` inside the `if cfg.HA_API_TOKEN:` block, immediately after `_profile_ws` is created

## Rationale
Pueo's design principle (see `CLAUDE.md` Key Patterns, "LLM-guided all actions") holds that chat is a first-class mode of interacting with the system, equivalent to automated repair loops. A user asking "investigate device 192.168.1.42" in chat should get the same rich answer that the automated notification pipeline would produce for the same IP — including the HA device registry lookup.

Silently returning `ha_device_name: null` when the data was available is a correctness failure that is hard to notice because no error is raised. Structural enforcement (the client is in `ToolExecutor`, the method always passes it) removes the failure mode.

## Consequences
- Any future `ToolExecutor` method that calls a shared function must use `self._ws_client` (and any other stored client), not `None`
- Adding a new shared function that requires a new client type → add the client to `ToolExecutor.__init__` (and a `set_*` method if deferred injection is needed)
- Code review checklist: "does this shared function receive the same args in chat as in the automated pipeline?"
- `dashboard.py` line 2522 constructs a one-shot audit `ToolExecutor` with no WS client — this is intentional; that executor is used only for `--mode audit` where no `HA_API_TOKEN` is available

## Related decisions
- [ADR 010 — Agent self-awareness](010-agent-self-awareness.md): `read_source` was similarly added to all registries so the same self-inspection capability is available in all agent modes. That ADR established the precedent; this ADR generalises it to client availability, not just tool registration.
- [ADR 006 — LLM provider abstraction](006-llm-provider-abstraction.md): `LLMClientProtocol` is injected into `ToolExecutor` by the same pattern; this ADR extends the same discipline to `HAWebSocketClientProtocol`.
