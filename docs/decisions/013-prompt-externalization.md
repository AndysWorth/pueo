# ADR 013 — System prompts live in `prompts/`, not inline strings

## Status
Accepted

## Context
Three categories of system prompts existed as inline string literals scattered across the codebase:

1. `_AGENT_LOOP_SYSTEM_PROMPT` in `utils/agent_loop.py` — the repair agent's primary instruction set
2. The notification analysis prompt in `ha_notification_manager.py` (line 264)
3. Two prompts in `ha_update_manager.py` (breaking-change analysis ~line 172; self-check cross-reference ~line 648)

A `prompts/` directory already existed and a `load_prompt()` loader was already in use for some prompts. The inline strings were invisible to anyone browsing `prompts/` and were harder to tune — changing a prompt required finding the right constant in a `.py` file, not editing a focused text file. A dead file (`prompts/agent_loop_ha.md`) had existed without being loaded, signalling that the convention was the intended direction but had not been applied consistently.

## Decision
All system prompts move to `prompts/` as standalone `.md` files, loaded at call-time via `load_prompt()`. No system prompt content lives as an inline string in a `.py` file.

Affected files and their target paths:

| Old location | New file |
|---|---|
| `utils/agent_loop.py::_AGENT_LOOP_SYSTEM_PROMPT` | `prompts/agent_loop_ha.md` |
| `utils/agent_loop.py::_CHAT_SYSTEM_PROMPT` | `prompts/agent_loop_chat.md` |
| `ha_notification_manager.py` prompt | `prompts/triage_notification.md` |
| `ha_update_manager.py` breaking-change prompt | `prompts/analyze_breaking_changes.md` |
| `ha_update_manager.py` self-check prompt | `prompts/self_check_command_risk.md` |
| `ha_log_monitor.py` repair issue prompt | `prompts/triage_repair_issue.md` |

New prompts follow the same pattern: `load_prompt("name")` loads `prompts/name.md` relative to the project root. The loader is already implemented; no new infrastructure is needed.

## Rationale
Prompts are a first-class design artefact — as important as code, but different in character. Keeping them in `.md` files makes them:

- **Discoverable:** browsing `prompts/` shows the full set; no grep required to find the active wording.
- **Tunable:** an editor with markdown preview can be used; the surrounding Python is not visible as a distraction.
- **Versionable at the right granularity:** a commit that changes only a prompt's wording shows a clean diff in the `.md` file, not a `py` file with scattered changes.

The alternative — a single constants file for prompts — was rejected because it provides discoverability without tunability; the `.md` format is clearer for text that spans many lines.

## Consequences
- `prompts/agent_loop_ha.md` replaces the dead placeholder that existed but was never loaded.
- `load_prompt()` is called at module load time for prompts used in every session; startup fails fast if a file is missing.
- Adding a new agent or call site requires creating the corresponding `.md` file; no inline strings allowed in new code.
- Tests that previously compared against an inline string constant must load the prompt from the file to stay in sync. A single test per prompt file confirming it loads without error is sufficient coverage for the externalization itself.

## Related decisions
- [ADR 003 — Structured LLM output](003-structured-llm-output.md): the prompt format (instruction → schema hint) is unchanged; only the storage location changes.
- [ADR 012 — Hypothesis-driven repair cycle](012-hypothesis-driven-repair.md): the restructured repair prompt is the first beneficiary of this convention.
