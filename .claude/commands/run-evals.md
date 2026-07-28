Run the Pueo agent eval suite from inside `pueo/`:

```bash
python evals/run_evals.py
```

This runs all 10+ scenarios through the real Ollama inference pipeline using `FakeToolExecutor` to intercept tool calls. SSH and HA connections are never made.

**Options:**
- `python evals/run_evals.py --scenario <fragment>` — run only scenarios whose name contains the fragment
- `python evals/run_evals.py --save-baseline` — overwrite `evals/baseline.json` with current scores

**Output:** Per-scenario pass/fail table for outcome accuracy, tool recall, safety compliance, fix parsability, and efficiency. Aggregate metrics with delta vs baseline.

**First run:** Automatically saves scores to `evals/baseline.json`. Commit the result so regressions show deltas.

**Requires:** Ollama running locally with the configured model (`qwen2.5-coder:7b` by default).
