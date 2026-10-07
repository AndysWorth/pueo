# ai_agent_ha vs Pueo

Researched 2026-10-07 against [sbenodiz/ai_agent_ha](https://github.com/sbenodiz/ai_agent_ha) at HEAD (release v1.42, last push 2026-07-14; MIT; ~160 stars). Tracking issue: #805.

## What ai_agent_ha is

A HACS custom integration (`custom_components/ai_agent_ha`) that adds a multi-provider LLM chat panel to the HA sidebar. It runs **inside** HA and talks to it through `hass` internals rather than SSH/REST. It also registers the services `ai_agent_ha.query`, `create_automation`, `create_dashboard`, and `update_dashboard`.

- **Protocol:** JSON-in-content, not native tool calling. The model returns a `request_type` (`data_request`, `call_service`, `automation_suggestion`, `dashboard_suggestion`, `final_response`) and `agent.py`, a single 206 KB file, dispatches on it (`process_query`, ~L3185).
- **Providers:** OpenAI, Anthropic, Gemini, OpenRouter, Llama API, z.ai, Alter, local Ollama, and any OpenAI-compatible endpoint (`OpenaiCompatibleClient`, L488).
- **Read tools:** entity state; entities by domain, area, or `device_class`; climate entities; entity, device, and area registries; history; long-term statistics (`get_statistics`, L2437); calendar events; persons; scenes; weather; automations; dashboards.
- **Write tools:**
  - `call_service` (L4324): calls any domain with no block list or risk tiering.
  - `set_entity_state` (L4236): maps light, switch, cover, climate, and fan to services. For every other domain it falls back to `hass.states.async_set`, which **fakes the entity state without touching the device**.
  - `create_automation` (L2573): sanitises the config, then writes `automations.yaml` with a `.bak` copy and an atomic temp-file + `os.replace` (`_write_automations_file`, L1813), then calls `automation.reload`. There's **no `check_config` before the reload**.
  - `create_dashboard` / `update_dashboard`: write lovelace files directly.
- **Approval:** an in-chat suggestion that the user accepts, for automations and dashboards only. Service calls run immediately.
- **Scope:** interactive assistant only. There's no monitoring, repair, update management, RAG, cross-session memory, or audit trail.

## Overlap / gap matrix

| Capability | ai_agent_ha | Pueo | Verdict |
|---|---|---|---|
| Autonomous monitoring, repair, updates, backups, RAG, runbooks, HITL cards | — | ✅ | Pueo's core; nothing to take |
| Entity / history / logbook / registry / area reads | ✅ | ✅ `get_entity_history`, `get_logbook`, `get_area_layout`, `get_device_info` | Parity |
| `call_service` | ✅ ungated | ✅ risk-tiered + approval (`utils/ha/service_policy.py`) | Pueo is stronger |
| Natural-language automation authoring | ✅ | ❌ (repairs existing config only) | **Adopt, gated**: #801 |
| Long-term statistics | ✅ | ❌ | **Adopt**: #800 |
| OpenAI-compatible LLM endpoint | ✅ | ❌ (Ollama + Anthropic) | **Adopt**: #802 |
| In-HA sidebar chat | ✅ | ❌ (separate dashboard) | **Docs only**: #804 |
| Query by `device_class` | ✅ | partial (`render_ha_template`) | Deferred |
| Dashboard generation (+ built-in templates) | ✅ | ❌ (dashboard health only) | Deferred; off-mission |
| Calendar / weather / person | ✅ | ❌ | Skip; off-mission |
| `SOUL.md` / `agent.yaml` (gitagent spec) | ✅ | ❌ | Optional docs nicety |

## How the projects can help each other

**Pueo as a safety net for ai_agent_ha (#803).** Following the companion-integration pattern (ADR 034):
- Detect `ai_agent_ha` in `HAEnvironmentProfile`.
- When `HAEventSubscriber` sees an automation or lovelace change, wake a validation investigation and raise a card if the change broke something.
- Treat ai_agent_ha's `states.async_set` fallback as a known source of phantom entity state when diagnosing state/device mismatches.

**Upstream contributions.** Pueo's existing patterns map directly onto open gaps in ai_agent_ha:
- Run `homeassistant.check_config` before `automation.reload`. The `.bak` and atomic write are already in place.
- Replace the `hass.states.async_set` fallback in `set_entity_state` with a real service call or an error.
- Add risk tiers or a block list for `call_service` (prior art: `service_policy.py`).
- Upstream #23 ("Allow modifying entire /config directory") asks for exactly what Pueo's sandbox-then-swap repair pipeline does. Pointing users to Pueo, or sharing the sandbox approach, is a natural fit.
- Upstream #48 (a failed query poisons the conversation history) matches failure modes Pueo handles in `AgentLoop`.

**MCP bridge.** Pueo already serves 21 read-only tools over MCP (ADR 028). An MCP-client feature in ai_agent_ha would let its sidebar answer "what has Pueo done / why is X broken?" with no Pueo changes. Upstream has no MCP or native tool-calling work on its issue tracker today.

**Activity caveat.** Upstream's last push was 2026-07-14, and 14 issues are open. Contributions may sit unreviewed, so open issues there to gauge maintainer interest before writing PRs.
