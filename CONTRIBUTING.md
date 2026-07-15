# Contributing to Pueo

Thank you for contributing to Pueo. This document covers everything you need to get your change merged safely.

---

## Before you start

Read the architecture decision records in `docs/decisions/` and the backlog in `docs/implementation-plan.md`. Most new work maps to an existing phase item — link to it in your PR.

---

## Branch strategy

| Branch | Purpose |
|--------|---------|
| `main` | Stable, tagged releases only. Direct pushes are blocked. |
| `develop` | Integration branch. All feature PRs target here. |
| `feature/<short-name>` | One feature or implementation-plan item per branch. |
| `fix/<short-name>` | Bug fixes. |

CI runs on every push to `main` and `develop`, and on every PR targeting `main`.

---

## Commit convention

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short summary>

[optional body]
[optional footer: closes #issue]
```

**Types:** `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `ci`

**Scope** (optional): `core`, `advanced`, `sandbox`, `monitor`, `config`, `utils`, `tests`, `ci`

**Examples:**
```
feat(monitor): add debounce window before triggering repair pipeline
fix(sandbox): revert config on SFTP write timeout
docs(decisions): add ADR 004 for retry strategy
test(config): add isolated_config tests for new agent keys
```

---

## Setup

```bash
pip install -r requirements-dev.txt
pre-commit install        # installs git hooks — run once after cloning
```

After `pre-commit install`, Black, flake8, mypy, and bandit run automatically on every `git commit`.

---

## The three-file rule for config changes

Adding any new configuration key requires exactly three file changes — no more, no fewer:

| File | What to add |
|------|------------|
| `config.py` | Typed module-level constant with fallback default |
| `config.yaml.default` | Key with a comment explaining its purpose and valid range |
| `setup.sh` | Interactive prompt so `./setup.sh` can generate it |

Adding a key in only one or two of these files will fail the `TestConfigDefaults` test suite.

---

## Safety invariant

No change may alter the backup-before-write ordering:

```
execute_remote_backup() → record_backup_slug() → remediation
```

If your change touches any part of the repair pipeline, re-read `docs/decisions/002-safety-invariant.md` before opening a PR. The PR template checklist enforces this — all boxes must be checked.

---

## Testing requirements

| What you added | Required tests |
|----------------|---------------|
| New Pydantic schema | 3 tests: valid construction, invalid/missing fields, JSON round-trip |
| New `config.py` key | Test in `TestConfigDefaults` using the `isolated_config` fixture |
| New pure-logic function | Unit test (no SSH/Ollama mocks — those are integration concerns) |
| Any of the above | Coverage must not drop below 80% |

Run the full suite before pushing:

```bash
pytest --cov=./ --cov-report=term-missing --cov-fail-under=80
```

---

## Code style

- Formatting: `black` (enforced by pre-commit and CI)
- Linting: `flake8` (errors and undefined names only — `E9,F63,F7,F82`)
- Types: `mypy --ignore-missing-imports` (no new `Any` suppressions without justification)
- Security: `bandit -r . -x ./tests`
- No bare `print()` in agent code — use structured logging (Phase 2 item)
- No comments explaining *what* code does — only *why* it does something non-obvious

---

## Pull request process

1. Open a PR against `develop` (not `main`)
2. Fill out every section of the PR template
3. All CI checks must be green before review
4. One approving review required for merge to `develop`
5. Merges to `main` are tagged releases — coordinate with maintainers

---

## Reporting issues

Use the GitHub issue templates:
- **Bug report** — for broken or unexpected behavior
- **Feature request** — for new capabilities (check the implementation plan first)

Security vulnerabilities: see `SECURITY.md`.
