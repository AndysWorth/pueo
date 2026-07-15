#!/usr/bin/env bash
# Runs at the end of every Claude Code session.
# Emits reminders when session-end state suggests something was missed.

CHANGED=$(git diff --name-only 2>/dev/null; git diff --cached --name-only 2>/dev/null)

if [ -z "$CHANGED" ]; then
    exit 0
fi

# Reminder 1: agent or config files changed → may need CLAUDE.md update
if echo "$CHANGED" | grep -qE '(ha_agent|config\.py|main\.py)'; then
    echo "💡 Agent or config files modified — update CLAUDE.md if a design decision was made."
fi

# Reminder 2: code changed but no tests touched → likely needs tests
if echo "$CHANGED" | grep -qE '\.(py)$' && ! echo "$CHANGED" | grep -q 'tests/'; then
    echo "💡 Python files changed without test updates — add tests covering the new behaviour."
fi

exit 0
