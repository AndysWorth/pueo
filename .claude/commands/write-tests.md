Write tests for the following: $ARGUMENTS

Follow the conventions in `tests/CLAUDE.md` and the existing patterns in `tests/test_core.py`:

1. **Identify the test class** — find the existing class for this module (e.g., `TestConfigDefaults`, `TestSandboxEngine`, `TestLogMonitor`) or create a new one if the module has no class yet.

2. **Determine what is testable** without SSH or Ollama:
   - Pydantic schema construction (valid, invalid, JSON round-trip)
   - Config loading and fallback defaults (use the `isolated_config` fixture from `conftest.py`)
   - Pure logic: path derivation, regex matching, threshold comparisons, data transformations
   - Error handling for bad inputs

3. **Write the tests**, then run `pytest tests/test_core.py -v` to verify they pass.

4. **Do not** mock SSH or Ollama — those are integration concerns outside the unit suite scope.
