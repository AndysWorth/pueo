Audit the Pueo codebase for safety invariant violations. Check each of the following:

**1. Backup-before-write ordering** (`ha_agent_sandbox_engine.py`, `ha_agent_advanced.py`)
Confirm that every code path that calls `commit_atomic_swap()` or writes to `CONFIG_REMOTE_PATH` first calls `execute_remote_backup()` and `record_backup_slug()` — in that order. Flag any path where a write can occur without a preceding backup.

**2. Config single-source discipline** (all `ha_agent_*.py`, `main.py`)
Confirm that no agent script declares its own constants for `HA_HOST`, `HA_USER`, `SSH_KEY_PATH`, `CONFIG_REMOTE_PATH`, `OLLAMA_MODEL`, `DB_PATH`, or `CONFIDENCE_THRESHOLD`. All must import from `config`.

**3. Sandbox revert on failure** (`ha_agent_sandbox_engine.py` → `deploy_and_test_in_sandbox`)
Confirm the `.bak` revert (`mv configuration.yaml.bak configuration.yaml`) runs even if `ha core check` fails — i.e., it's not inside the `if exit_code == 0` branch.

**4. Deferred imports in `main.py`**
Confirm all agent module imports (`ha_log_monitor`, `ha_agent_core`, `ha_agent_sandbox_engine`) are inside `if args.mode` blocks, not at the top of the file. Top-level imports would load `config.py` before `PUEO_CONFIG` is set.

Report each finding as PASS or FAIL with the relevant line numbers.
