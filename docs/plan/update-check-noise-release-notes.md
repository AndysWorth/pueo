# OTBR 3.2.0 → 3.2.1 update: what happened, what should have happened, fix plan

## Context — what the evidence shows (13:09–13:12 local, 2026-10-06)

Sources: debug episode `update_poll_4ea84aa6…`, `pueo.log` (UTC 17:09–17:14), `timeline_events` ids 56–67.

**What Pueo did**
1. 13:09:02 — supervisor restart (picking up #781/#783). Startup `update_check` saw OTBR 3.2.1 → queued `update_analysis`. (The 11:12 run that logged `finish_update_analysis_skipped_already_approved` was the pre-#781 stale-approval bug; #781 fixed it, which is why this run went through.)
2. Agent loop, 12 tool calls, 94 s:
   - `get_update_release_notes("openthread_border_router 3.2.1")` → "unavailable — try fetch_url(…/<slug>/CHANGELOG.md)".
   - Model guessed the slug wrong 3× (`openthread_border-router` twice — identical call repeated — then `openthread-border-router`), all 404. Then 4 more guessed GitHub URLs (3 × 404, one returned useless HTML). **It never tried the right path** (`…/openthread_border_router/CHANGELOG.md`, which returns 200).
   - `finish_update_analysis(safe_to_update=true, confidence=0.9, summary="No breaking changes found in release notes…")` — **false claim**: no release notes were ever read.
3. 13:10:37 — auto-apply: backup `9adfd10a` (offloaded, SHA verified) → `update.install` → expected 30 s REST timeout → poll succeeded 13:12:14 → post-update card + restart-pending flag + repair scan. The update succeeded.
4. 13:11:04–13:12:10 — **7 extra "Update available: openthread_border_router 3.2.0 → 3.2.1" timeline entries** while the install was running.

**What Pueo should have done**
- Read the real changelog. HA already exposes it: WS `update/release_notes {entity_id}` returns the add-on CHANGELOG (3.2.1 = "Keep retrying unavailable network RCPs without crashing the app"). That's a low-risk bug fix, so "safe, auto-apply" was the right verdict, but Pueo got there by luck and said something untrue to get there.
- If notes can't be fetched, say so honestly and send an approval card instead of auto-applying (user decision).
- Log one "Update available" entry, then show the actual install steps in the timeline (backup → install → result). None of those steps reach the timeline today.

**Why so many update_checks** (root cause, confirmed in code)
- `_ha_event_wake_dispatcher` (`main.py:1208`) wakes `update_check` on *every* `update.*` `state_changed`, attribute-only changes included. During an install HA updates `in_progress` / `update_percentage` every few seconds, which gives a wake every ~5 s (the debounce).
- `poll_for_updates` (`agents/ha_log_monitor.py:968`) still sees `update_available=True` (installed = 3.2.0 until it finishes). The suppression row is only written *after* `execute_update` returns (`ha_update_manager.py:743`). So `should_send` is True, and it logs `update_available` plus writes a timeline event, then calls `_run_update_analysis`.
- The resubmit is silently dropped by work-queue dedup (`work_queue_dedup_running` is DEBUG-level). The timeline entry is written anyway, because `_run_update_analysis` doesn't report whether the item was accepted.

**ha-upgrade-advisor:** not used. The integration isn't installed (the HACS list is only `noaa_it_all`), and the episode's `initial_context` has no "Third-party upgrade-advisor report" block. Even if it were installed, it wouldn't have helped here. `read_advisor_report` only uses a report whose `available_version` equals the target version, and the advisor tracks HA Core upgrades, not add-on versions like 3.2.1. No change planned.

## Secondary bugs found
- Release-notes cache for non-core updates is keyed by version only (`{version}.txt`), so `3.2.1.txt` would be shared by every add-on/HACS integration at 3.2.1 (`fetch_release_notes_cached`, `ha_update_manager.py:198`). An existing `v0.7.1.txt` proves this path is live.
- `_get_update_release_notes` only uses the pending update's `release_url` when `target_version == latest_version` exactly. The model passed `"openthread_border_router 3.2.1"`, so it fell through.
- Prompt `agent_loop_update_analysis.md:56` tells the model to mark `safe_to_update=true` when notes are unavailable. That encourages exactly this false summary.

## Plan (one issue, one branch `feat/<N>-update-check-noise-release-notes`)

### 1. Stop the update_check wake storm
- `main.py::_ha_event_wake_dispatcher`: for `update.*` `state_changed`, wake only when `old_state.state != new_state.state` or `attributes.latest_version` changed. Ignore `in_progress` / `update_percentage`-only changes. Extract the predicate as a pure function, `_is_update_wake_worthy(event) -> bool`, so it can be unit-tested.
- `poll_for_updates`: `continue` when `u.in_progress` is True (`UpdateStatus.in_progress` already exists).
- `_run_update_analysis` returns `bool` (the `submit()` result, or True on the direct path). `poll_for_updates` writes the `update_available` log and timeline entry only when it returns True. Otherwise it logs at DEBUG `update_analysis_already_queued`.

### 2. Get real release notes for add-ons
- `utils/ha/ha_ws_client.py`: add `get_update_release_notes(entity_id) -> str | None` (WS `update/release_notes`), plus the matching Fake method and a Protocol entry in `interfaces.py`.
- `fetch_release_notes_cached`: add an `entity_id` kwarg and an optional `ws_fetcher`. For non-core versions, order is: WS release notes → `release_url` → fallback. Cache key becomes `{entity_id}_{version}.txt` for non-core (core stays `{version}.txt`).
- Fallback message: build the exact CHANGELOG URL from the entity's component name (`https://raw.githubusercontent.com/home-assistant/addons/master/{component}/CHANGELOG.md`) instead of a `<slug>` placeholder the model must guess.
- `ToolExecutor._get_update_release_notes`: when a pending update exists and `target_version` *contains* its `latest_version`, use the pending entity_id/release_url; pass `self._ws_client`.
- `_run_update_analysis`: construct/inject an `HAWebSocketClient` into the ToolExecutor (chat-parity rule: also make sure the chat/shared executor path passes its ws client through).

### 3. Honest outcome when notes are missing (user choice: card, no auto-apply)
- `ToolExecutor`: track `self._release_notes_obtained`, set True only when `_get_update_release_notes` returns real notes (not the "unavailable" sentinel).
- `_finish_update_analysis`: if a pending update exists and notes were never obtained, force `create_hitl_card=True` and cap `confidence` at 0.5. Prefix the summary/recommendation with "Release notes could not be retrieved — verdict not verified against the changelog." Log `update_analysis_no_release_notes`.
- Prompt `agent_loop_update_analysis.md`: replace line 56 with "If notes are unavailable, say so explicitly; never claim the notes contain no breaking changes. Pueo will request approval." Also add "do not repeat a fetch_url that already 404'd."

### 4. Timeline transparency for auto-applied updates
- `execute_addon_update` (and the shared `_send_post_update_card` path): write timeline events for backup created (slug), install started, and result (success/timeout), under source `update_check`.

### Related files / docs
- `CLAUDE.md` "Event-driven supervisor wake" paragraph: note attribute-only update changes don't wake.
- ADR 033: add a consequence line about the wake filter. No new config keys, no migrations, no dependency changes.

## Tests (same session)
- `tests/test_ha_update_manager.py`:
  - `fetch_release_notes_cached` uses WS first, then release_url, then fallback with the concrete URL.
  - Cache key is per-entity for non-core (two entities at the same version don't collide).
  - `_run_update_analysis` returns False on dedup.
  - `execute_addon_update` writes timeline events.
- Tool-executor tests:
  - `_get_update_release_notes` matches `"openthread_border_router 3.2.1"` to the pending update.
  - `_finish_update_analysis` without notes produces a card, no auto-apply, and capped confidence. With notes it auto-applies as before (both branches, for codecov).
- `poll_for_updates` logic: `in_progress` updates are skipped, and no timeline write when the submit is deduped. Test helpers directly per the async-loop testing rule.
- `_is_update_wake_worthy`: state change, latest_version change, attribute-only change (False), non-update entity.
- WS client Fake + Protocol conformance.

## Verification
- Full CI gate: black / flake8 / mypy / bandit / `pytest --cov --cov-fail-under=90 --ignore=tests/integration`.
- Live: run `python -c` with `HAWebSocketClient.get_update_release_notes("update.openthread_border_router_update")` against HA (read-only) and confirm it returns changelog text (or the 3.2.1 notes, if still cached by HA).
- Restart supervisor. On the next add-on update, confirm in the timeline:
  - exactly one "Update available" entry, then backup/install/result entries;
  - the debug episode shows `get_update_release_notes` returning real changelog text, with no URL guessing.
- Replay sanity: the unit test for `_is_update_wake_worthy` uses a captured `update_percentage`-only event shape.
