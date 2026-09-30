# Contributing to Pueo

Pueo is a multi-person project. This guide covers project-specific conventions; the full development procedure (branching, CI gate, PR, merge) is in the **Development Procedure** section of `CLAUDE.md`.

---

## Before you start

Every change needs a GitHub issue — no issue, no branch. Read the architecture decision records in `docs/decisions/` for the area you are touching. Work is tracked in [GitHub Issues](https://github.com/AndysWorth/pueo/issues).

---

## Branch strategy

- All work branches off `main` and is named `feat/<issue-number>-<slug>`.
- There is no separate integration branch — feature branches merge directly to `main` via PR. Direct pushes to `main` are blocked.
- Keep branches short-lived; rebase onto `main` daily for any branch open more than one day.
- Hotfix branches for production bugs branch off the relevant release tag.

---

## Commit convention

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short summary>

[optional body]
[optional footer: closes #issue]
```

**Types:** `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `ci`

**Scope** (optional): `core`, `advanced`, `sandbox`, `monitor`, `supervisor`, `dashboard`, `web`, `netalertx`, `resource`, `evals`, `config`, `utils`, `tests`, `ci`

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
| Any of the above | Coverage must not drop below 90% |

Run the full suite before pushing:

```bash
pytest --cov --cov-fail-under=90 --ignore=tests/integration
```

---

## Code style

- Formatting: `black` (enforced by pre-commit and CI)
- Linting: `flake8` (errors and undefined names only — `E9,F63,F7,F82`)
- Types: `mypy --ignore-missing-imports` (no new `Any` suppressions without justification)
- Security: `bandit -r . -x ./tests`
- No bare `print()` in agent code — use structured logging
- No comments explaining *what* code does — only *why* it does something non-obvious

---

## Pull request process

1. Open a PR against `main` and fill out every section of the PR template.
2. The description explains *why*, not *what*, and references the issue (`Closes #N`).
3. CI must pass before review:
   ```bash
   black --check .
   flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
   mypy --ignore-missing-imports .
   bandit -r . -x ./tests,./.venv
   pytest --cov --cov-fail-under=90 --ignore=tests/integration
   ```
4. At least one approving review is required. The PR author may not merge their own PR.
5. Squash merge to keep `main` history clean and bisectable.

**Migrations and production config writes:** flag them explicitly and include a rollback plan (revert commit + migration version) in the PR description. Test migrations against a real local copy of `ha_agent_state.db`.

**Breaking changes:** for public interface changes, add a deprecation warning for at least one version, bump the semver major version, and include a migration guide in the changelog.

**Dependency changes:** changes to `requirements*.txt` warrant a second reviewer; call out transitive dependency risk in the PR description.

---

## Audit reports

The `audits/` directory (gitignored) is for operational state snapshots and diagnostic reports gathered from a live Pueo + HA instance. Save reports there when you want to track what a real deployment looked like at a point in time without publishing potentially sensitive state to the public repo.

`python main.py --mode audit` produces these reports automatically: it checks SSH connectivity, verifies HA state and disk, inspects the SQLite database, and surfaces a structured gap report comparing intended vs. actual operational state. Reports are saved to `audits/` with a datestamped filename. Insights from real audit reports inform improvements that can be contributed back via the normal PR process.

---

## Reporting issues

Use the GitHub issue templates:
- **Bug report** — for broken or unexpected behavior
- **Feature request** — for new capabilities (check existing issues first)

Security vulnerabilities: see `SECURITY.md`.
