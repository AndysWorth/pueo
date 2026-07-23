#!/usr/bin/env bash
# Runs at the end of every Claude Code session.
# Emits reminders when session-end state suggests something was missed,
# and runs the full CI gate automatically when Python files were modified.

# Include committed changes on this branch vs main, plus any uncommitted changes
CHANGED=$(git diff main...HEAD --name-only 2>/dev/null; git diff --name-only 2>/dev/null; git diff --cached --name-only 2>/dev/null)

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

# CI gate: run automatically when Python files were modified
if echo "$CHANGED" | grep -qE '\.py$'; then
    if [ ! -f .venv/bin/activate ]; then
        echo "⚠️  .venv not found — skipping CI gate"
        exit 0
    fi
    source .venv/bin/activate
    echo ""
    echo "━━━ CI gate (Python files changed) ━━━"

    PASS=true

    echo ""
    echo "▶ black --check"
    if ! black --check . --quiet 2>&1; then
        echo "❌ black: run 'black .' to fix"
        PASS=false
    else
        echo "✅ black"
    fi

    echo ""
    echo "▶ flake8 (hard errors)"
    if ! flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics 2>&1; then
        echo "❌ flake8: hard errors found"
        PASS=false
    else
        echo "✅ flake8"
    fi

    echo ""
    echo "▶ mypy"
    if ! mypy --ignore-missing-imports . --quiet 2>&1; then
        echo "❌ mypy"
        PASS=false
    else
        echo "✅ mypy"
    fi

    echo ""
    echo "▶ bandit"
    if ! bandit -r . -x ./tests,./.venv -q 2>&1; then
        echo "❌ bandit"
        PASS=false
    else
        echo "✅ bandit"
    fi

    echo ""
    echo "▶ pytest"
    if ! pytest --cov=./ --cov-fail-under=90 -q 2>&1; then
        echo "❌ pytest"
        PASS=false
    else
        echo "✅ pytest"
    fi

    echo ""
    if [ "$PASS" = true ]; then
        echo "━━━ CI gate: ALL PASSED ━━━"
    else
        echo "━━━ CI gate: FAILURES — fix before opening PR ━━━"
    fi
fi

exit 0
