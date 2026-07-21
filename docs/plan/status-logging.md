# Item 20 — NetAlertX Setup Status Logging

Part of the [Implementation Plan](../implementation-plan.md) · Phase 5 · 1 session.

**Depends on:** Item 5 (structured logging infrastructure — `utils/logging.py`, `setup_logging()`), Items 11–12 (installer state machine whose output this makes visible).

## Status
✅ Done (2026-07-20) — PR #36

## Problem

Running `python main.py --mode netalertx-setup` produces no terminal output. The installer (`netalertx/installer.py`) already emits rich structured log events at every step (`step1_start`, `step2_complete`, `install_state_updated`, etc.), but `setup_logging()` is never called in this dispatch path — the `pueo` logger has no handlers attached, so all output is silently dropped. The same gap affects `--mode netalertx`.

## Solution

Call `setup_logging()` centrally in `main.py` (after `PUEO_CONFIG` is set, before mode dispatch), covering all modes uniformly. For `--mode netalertx-setup` use a human-readable plain-text console formatter on stderr; the file handler always stays JSON.

## Changes

### `pueo/utils/logging.py`

Add `_TextFormatter` alongside `_JsonFormatter`:

```python
class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _STANDARD_ATTRS
            and not k.startswith("_")
            and k != "correlation_id"
        }
        suffix = ("  " + "  ".join(f"{k}={v!r}" for k, v in extras.items())) if extras else ""
        return f"{record.levelname:<8} {record.message}{suffix}"
```

Modify `setup_logging()`:

```python
def setup_logging(console_text: bool = False) -> None:
    ...
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(_TextFormatter() if console_text else formatter)
    logger.addHandler(console_handler)
```

### `pueo/main.py`

```python
from utils.logging import setup_logging

# After os.environ["PUEO_CONFIG"] = str(config_path), before mode dispatch:
setup_logging(console_text=(args.mode == "netalertx-setup"))
```

Individual modules that already call `setup_logging()` internally are safe — it is idempotent.

### `pueo/tests/test_core.py`

New `TestTextFormatter` class:

- `test_text_formatter_basic` — format `"INFO     event_name"` with no extras
- `test_text_formatter_with_extras` — extras render as `key='value'` pairs; `correlation_id` excluded
- `test_setup_logging_console_text_uses_text_formatter` — verify the stderr handler's formatter is `_TextFormatter` when `console_text=True`

## Expected terminal output (netalertx-setup)

```
INFO     installer_start  current_state='NOT_INSTALLED'  steps='1-4'
INFO     step1_start  step='detect_deployment'
INFO     step1_supervisor_found  mode='addon'
INFO     step1_complete  mode='addon'
INFO     step2_start  step='install_mosquitto'
...
INFO     netalertx_setup_done  state='FULLY_OPERATIONAL'
```

JSON lines continue to be written to `pueo.log`.

## Verification

```bash
black --check .
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
mypy --ignore-missing-imports .
bandit -r . -x ./tests
pytest --cov
```
