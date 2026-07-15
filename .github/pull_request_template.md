## Summary

<!-- What does this PR do and why? -->

## Type of change

- [ ] Bug fix
- [ ] New feature (implementation-plan item — link: )
- [ ] Refactor / tech debt
- [ ] Docs / config only

## Checklist

### Safety invariant
- [ ] No write to HA config proceeds without a confirmed backup slug
- [ ] `execute_remote_backup()` → `record_backup_slug()` → remediation order is preserved
- [ ] New HITL-eligible paths (CRITICAL severity, HACS, DB migrations) are gated before step 1

### Code quality
- [ ] `pre-commit run --all-files` passes locally
- [ ] `mypy` reports no new errors
- [ ] No bare `print()` calls added (use structured logging)

### Config changes (if any)
- [ ] New key added to `config.yaml.default`
- [ ] New key added to `config.py` with a typed constant and fallback default
- [ ] New key added to `setup.sh` prompt

### Tests
- [ ] New Pydantic schema → 3 tests (valid construction, invalid fields, JSON round-trip)
- [ ] New `config.py` key → test in `TestConfigDefaults` using `isolated_config`
- [ ] New pure-logic function → unit test (no SSH/Ollama mocks)
- [ ] Coverage does not drop below 80%

### Docs
- [ ] ADR added or updated if this changes an architectural decision
- [ ] Implementation-plan status markers updated if completing a phase item
- [ ] Roadmap updated if completing a milestone
