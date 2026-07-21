# Installer Diagnostics + NetAlertX Always-On — Items 21–22

Part of the [Implementation Plan](../implementation-plan.md) · Phase 6 · 2 sessions.

> **Documentation verified 2026-07-21** against:
> - Home Assistant Supervisor CLI reference + community reports
> - Mosquitto broker add-on (`core_mosquitto`) known failure modes
> - Ollama structured output API (docs.ollama.com)
> - NetAlertX HA add-on (alexbelgium/hassio-addons) + docs.netalertx.com
>
> **Corrections to existing code found during this review (fixed in item 21):**
> - `ha core reload` is not a valid HA CLI subcommand. Documented subcommands are:
>   `check`, `info`, `logs`, `options`, `rebuild`, `restart`, `start`, `stats`, `stop`, `update`.
>   Replace with `ha core restart` everywhere (3 files: `ha_agent_sandbox_engine.py`,
>   `netalertx/healer.py`, `netalertx/installer.py`).
> - `NETALERTX_ADDON_REPOSITORY_URL` defaults to `https://github.com/jokob-sk/NetAlertX`
>   (the upstream project repo). The correct HA Supervisor add-on repository is
>   `https://github.com/alexbelgium/hassio-addons`. The add-on slug is `netalertx` (standard)
>   or `netalertx_fa` (Full Access — required for ARP/network scanning).
> - `ha backup new` (singular) may or may not be a valid alias. Documented form is
>   `ha backups new --name X` (plural). Verify against live HA before changing — the existing
>   code uses the singular form throughout and tests mock it; leave it until confirmed broken.
> - `ss -tlnp sport = :1883` (BPF filter syntax) is unreliable in the BusyBox HAOS shell.
>   Use `ss -tlnp | grep 1883` instead.
> - The Ollama Python client returns a `ChatResponse` object, not a dict. The existing code
>   uses `response["message"]["content"]` — this works because the object supports subscript
>   access. New code should follow the same pattern for consistency.

---

### 21. CLI Corrections, NetAlertX Repository Fix, and Remove Optionality ✅ Done (2026-07-21) — PR #39

**Depends on:** Items 1–20 (all complete)

**Problem:** Three separate issues discovered during documentation review for item 22, plus the
design decision to make NetAlertX always-on. Grouped in one item because they are all small
configuration/command corrections with no new logic.

**Fix 1 — `ha core reload` → `ha core restart`**

`ha core reload` is not a documented HA Supervisor CLI subcommand and silently fails in
production (all call sites use `check=False`). The intent is to reload HA after a config write.
The correct command is `ha core restart`. Note: `ha core restart` restarts the HA Core container
(more disruptive than a reload). For future improvement, consider calling the HA REST API
`POST /api/services/homeassistant/reload_all` instead — but that is out of scope here.

Modify these three files:
- `ha_agent_sandbox_engine.py:275` — `"ha core reload"` → `"ha core restart"`
- `netalertx/healer.py:343` — `"ha core reload"` → `"ha core restart"`
- `netalertx/installer.py:954` — `"ha core reload"` → `"ha core restart"`
- Update test mocks in `tests/test_core.py` and `tests/conftest.py` from `"ha core reload"` to
  `"ha core restart"` (do a replace-all; ~15 occurrences)
- Update `utils/autonomy.py` docstring, `docs/plan/netalertx.md`, `docs/plan/foundation.md`,
  `docs/plan/autonomy.md`, `CLAUDE.md`, `docs/decisions/002-safety-invariant.md`

**Fix 2 — NetAlertX add-on repository URL and slug**

The current default `NETALERTX_ADDON_REPOSITORY_URL = https://github.com/jokob-sk/NetAlertX`
points to the upstream project repo, not the HA Supervisor add-on repo. The add-on is in
`https://github.com/alexbelgium/hassio-addons`. The slug resolved from this repo is `netalertx`
(standard, port scanning only) or `netalertx_fa` (Full Access — has `NET_RAW` capability needed
for ARP scanning). Use `netalertx_fa` as the default since Pueo requires network scanning.

Modify:
- `config.py` — update `NETALERTX_ADDON_REPOSITORY_URL` default to
  `https://github.com/alexbelgium/hassio-addons`
- `config.yaml.default` — update `addon_repository_url` comment line
- `setup.sh` — update commented `addon_repository_url` line
- `tests/test_core.py` — update all hardcoded `jokob-sk/NetAlertX` and `jokob-sk_NetAlertX`
  strings to `alexbelgium/hassio-addons` and `netalertx_fa` respectively (~20 occurrences;
  also update `_SLUG = "jokob-sk_NetAlertX"` constant at line 4363)
- `docs/plan/netalertx.md` — update the `addon_repository_url` default note

**Fix 3 — Remove `NETALERTX_ENABLED`**

NetAlertX is fundamental to Pueo and should always be installed. Remove the enabled/disabled toggle.

- `config.py:58` — delete `NETALERTX_ENABLED: bool = bool(_nax.get("enabled", False))`
- `config.yaml.default` — remove `enabled: false` line under `netalertx:`
- `setup.sh` — remove the `NAX_ENABLED` variable (line 192), the "Enable NetAlertX?" prompt
  block (lines 276–285), the `NAX_ENABLED` read-back block (lines 342–345), and the
  `if [ "${NAX_ENABLED}" = "true" ]` guard on the done-section NetAlertX commands (lines 357–361)
  — the NetAlertX commands should always print
- `setup.sh` — update the NetAlertX section header from "optional" to just "── NetAlertX ──"
- `tests/test_core.py` — remove the two `NETALERTX_ENABLED` tests (lines 3057 and 3189)

**Done when:**
- All three `"ha core reload"` calls replaced with `"ha core restart"`; all test mocks updated
- `NETALERTX_ADDON_REPOSITORY_URL` defaults to `https://github.com/alexbelgium/hassio-addons`;
  tests reference `netalertx_fa` as the expected slug
- `NETALERTX_ENABLED` no longer exists in config.py, config.yaml.default, or setup.sh
- `pytest --cov --cov-fail-under=90` passes

---

### 22. Installer Diagnostic Intelligence ✅ Done (2026-07-21) — PR #40

**Depends on:** Item 21 (corrections must be in place before adding new diagnostics)

**Problem:** When installer steps fail (as demonstrated live on 2026-07-21 with Mosquitto failing
to start), Pueo currently hangs indefinitely with the message "Aborting." and no explanation.
A senior engineer can identify the cause in seconds by gathering evidence from the host. Pueo
should do the same: gather SSH observations, reason about them with the local Ollama model, and
present a structured diagnosis with a proposed fix — implementing current LLM "superintelligence"
best practices (evidence-first reasoning, structured hypothesis management, confidence scoring,
grounded claims).

**Research note — superintelligence patterns applied here:**
Evidence-first investigative agents score ~2x better on diagnostic tasks than single-prompt
approaches using the same underlying model. The patterns with highest impact for a local 7b model:
1. **Evidence-first**: gather SSH observations before calling LLM; never diagnose on symptom alone
2. **Structured hypotheses**: schema forces ranked competing explanations, not one answer
3. **Confidence scoring**: LLM rates certainty; low confidence triggers human escalation
4. **Grounding**: prompt instructs "cite only what you observed; label uncertainty"
5. **Verification**: schema includes how to confirm the fix worked

Full ReAct multi-turn loops and critic/reflection passes are out of scope for this item —
the installer has well-bounded failure modes where a single evidence-gather + LLM call suffices.
ReAct loops are better suited for open-ended monitoring diagnosis (future Milestone 2/5).

---

#### New Pydantic schema: `InstallerDiagnostic`

Defined in `netalertx/installer_diagnostics.py`:

```python
class InstallerDiagnostic(BaseModel):
    primary_hypothesis: str = Field(
        description="Most likely cause in plain English, e.g. 'Port 1883 is in use by another process'"
    )
    confidence: float = Field(
        description="Certainty 0.0–1.0. Below 0.6 means evidence is insufficient to conclude."
    )
    supporting_evidence: list[str] = Field(
        description="Specific observations from the evidence that support the primary hypothesis. "
                    "Cite exact log lines or command output — do not infer."
    )
    alternative_hypotheses: list[str] = Field(
        description="Other possible causes not ruled out by the evidence."
    )
    recommended_action: str = Field(
        description="Concrete, specific action to resolve the issue. Include exact commands or UI steps."
    )
    can_auto_fix: bool = Field(
        description="True only if the fix can be executed via a single SSH command with no side effects."
    )
    auto_fix_command: Optional[str] = Field(
        default=None,
        description="The exact SSH command to run if can_auto_fix is True."
    )
    verification_command: Optional[str] = Field(
        default=None,
        description="SSH command to run after the fix to confirm it worked."
    )
```

---

#### New prompt: `prompts/diagnose_installer.md`

System prompt covering:

**Mosquitto (`core_mosquitto`) failure modes:**
- Port 1883 already bound — error surfaces at Supervisor level as
  `Port '1883' is already in use` or in Docker as `listen tcp 0.0.0.0:1883: bind: address already in use`
- SSL/TLS cert mismatch — add-on starts then stops with `Error: Server certificate/key are inconsistent`
- Add-on in `error` state after install — check `ha addons info core_mosquitto` for `state: error`
- Add-on config issue — add-on starts then immediately exits with no Supervisor-level error message;
  check the add-on log for app-level errors

**NetAlertX add-on (`netalertx_fa`) failure modes:**
- Slug not found in store — repo not yet indexed; Supervisor needs a refresh (`ha supervisor reload`)
- Add-on stuck in `installing` state — network issue downloading container image; will self-resolve
- Add-on in `error` state after start — check add-on log for config issues, missing permissions,
  or `NET_RAW` capability problems
- ARP scan not working — add-on requires `netalertx_fa` (Full Access) variant for `NET_RAW` capability

**HA Supervisor failure modes:**
- `ha supervisor info` not found or returns error — HA OS / Supervised not running; check Docker
- Supervisor updating — commands may fail transiently; wait for Supervisor restart

**Grounding instruction:**
"Output only claims supported by the evidence provided. If a specific log line supports the
hypothesis, quote it in supporting_evidence. If the evidence is ambiguous or insufficient,
lower your confidence score and add multiple entries to alternative_hypotheses rather than
committing to a primary hypothesis. Never use parametric knowledge as a substitute for
missing evidence."

---

#### New module: `netalertx/installer_diagnostics.py`

**Evidence gatherers** (pure SSH reads, no writes, token-budgeted):

```python
async def gather_mosquitto_evidence(ssh_client: SSHClientProtocol) -> dict[str, str]:
    """Gather diagnostic evidence when core_mosquitto fails to start."""
    evidence = {}
    _, out, _ = await ssh_client.run("ha addons info core_mosquitto")
    evidence["addon_info"] = out
    _, out, _ = await ssh_client.run("ha addons logs core_mosquitto -n 50")
    evidence["addon_logs"] = out
    _, out, _ = await ssh_client.run("ss -tlnp | grep 1883")
    evidence["port_1883"] = out or "(nothing listening on 1883)"
    _, out, _ = await ssh_client.run("ha supervisor info")
    evidence["supervisor_info"] = out
    return evidence


async def gather_addon_install_evidence(
    ssh_client: SSHClientProtocol, slug: str
) -> dict[str, str]:
    """Gather diagnostic evidence when a named add-on fails to install."""
    evidence = {}
    _, out, _ = await ssh_client.run(f"ha addons info {slug}")
    evidence["addon_info"] = out
    _, out, _ = await ssh_client.run("ha supervisor info")
    evidence["supervisor_info"] = out
    return evidence


async def gather_addon_start_evidence(
    ssh_client: SSHClientProtocol, slug: str
) -> dict[str, str]:
    """Gather diagnostic evidence when a named add-on fails to reach running state."""
    evidence = {}
    _, out, _ = await ssh_client.run(f"ha addons info {slug}")
    evidence["addon_info"] = out
    _, out, _ = await ssh_client.run(f"ha addons logs {slug} -n 50")
    evidence["addon_logs"] = out
    return evidence
```

**Context builder** (applies token budget via `truncate_to_budget`):

```python
def _build_evidence_context(failure_type: str, evidence: dict[str, str]) -> str:
    """Format evidence dict into a prompt-ready context string within token budget."""
```

**Main entry point:**

```python
async def diagnose_installer_failure(
    failure_type: str,   # "mosquitto_start" | "addon_install" | "addon_start"
    ssh_client: SSHClientProtocol,
    llm_client: LLMClientProtocol | None = None,
    slug: str = "",
) -> InstallerDiagnostic:
    """Gather evidence and return a structured LLM diagnosis for an installer failure."""
    # 1. Route to appropriate evidence gatherer
    # 2. Build evidence context string
    # 3. Call llm_client.chat() with InstallerDiagnostic schema + diagnose_installer prompt
    # 4. Validate and return InstallerDiagnostic
```

**HITL formatter:**

```python
def format_diagnostic_for_hitl(diagnostic: InstallerDiagnostic) -> str:
    """Render InstallerDiagnostic as human-readable text for HITL notification body.

    Example output:
      Diagnosis: Port 1883 is in use by another process (confidence: 82%)

      Evidence:
        • Supervisor log: "Port '1883' is already in use"
        • Nothing else listening on port 1883 per ss output

      Other possibilities: SSL cert mismatch; add-on configuration error

      Recommended action: Stop the conflicting process and restart core_mosquitto
        SSH command: ha addons restart core_mosquitto
        Verify with: ha addons info core_mosquitto (expect state: running)

      Pueo can attempt this fix automatically if approved.
    """
```

---

#### Update `netalertx/installer.py`

Add `llm_client: LLMClientProtocol | None = None` to:
- `_step2_install_mosquitto`
- `_step5_install_addon`
- `run_steps_1_to_4`, `run_steps_5_to_8`, `run_installer`, `main`

**Failure path pattern** (step 2 shown; same pattern for steps 5a and 5b):

```python
# BEFORE: abort notification with "Aborting." body
# AFTER: diagnose → enrich HITL body → optionally attempt auto-fix

running = await _poll_addon_state(ssh_client, "core_mosquitto", "running")
if not running:
    diagnostic = await diagnose_installer_failure(
        "mosquitto_start", ssh_client, llm_client
    )
    log.error(
        "step2_mosquitto_not_running",
        hypothesis=diagnostic.primary_hypothesis,
        confidence=diagnostic.confidence,
        correlation_id=cid,
    )
    approved = await gate.require_approval(
        subject="NetAlertX installer: Mosquitto failed to start",
        body=format_diagnostic_for_hitl(diagnostic),
        payload={
            "notification_id": f"{cid}_step2_abort",
            "step": 2,
            "can_auto_fix": diagnostic.can_auto_fix,
            "auto_fix_command": diagnostic.auto_fix_command,
        },
        notifier=notifier,
        risk=RiskLevel.HIGH,   # downgraded from CRITICAL: cause is known
    )
    if approved and diagnostic.can_auto_fix and diagnostic.auto_fix_command:
        ec, _, _ = await ssh_client.run(diagnostic.auto_fix_command, check=False)
        log.info("step2_auto_fix_attempted", command=diagnostic.auto_fix_command,
                 ec=ec, correlation_id=cid)
        if ec == 0:
            running = await _poll_addon_state(ssh_client, "core_mosquitto", "running")
            if running:
                _write_install_state(db_path, "MQTT_RUNNING", details, cid)
                log.info("step2_complete_after_fix", correlation_id=cid)
                return True
    return False
```

Apply the same pattern to:
- Step 5a: `_poll_addon_not_state` timeout → `diagnose_installer_failure("addon_install", ..., slug)`
- Step 5b: `_poll_addon_state` timeout → `diagnose_installer_failure("addon_start", ..., slug)`

Steps 6, 7, 8 failures are deterministic (file not found, HA check output, etc.) — their error
messages are already specific. Keep existing error handling for those steps.

---

#### Tests (in `tests/test_core.py`)

**Schema tests** (required for every new Pydantic schema per CLAUDE.md):
- `InstallerDiagnostic` valid construction with all fields
- `InstallerDiagnostic` missing required field raises `ValidationError`
- `InstallerDiagnostic` JSON round-trip via `model_validate_json`

**Evidence gatherer tests** (using `FakeSSHClient`):
- `gather_mosquitto_evidence` returns dict with expected keys; SSH commands called in expected order
- `gather_addon_install_evidence` with a slug parameter
- `gather_addon_start_evidence` with error-state add-on output

**Diagnostic flow tests**:
- `diagnose_installer_failure("mosquitto_start")` with `FakeLLMClient` returning valid
  `InstallerDiagnostic` JSON — verifies evidence gathered → LLM called → schema returned
- `diagnose_installer_failure("addon_install", slug="netalertx_fa")` — verifies slug passed through
- `format_diagnostic_for_hitl` renders `primary_hypothesis`, `confidence`, `supporting_evidence`,
  `recommended_action`, and auto-fix hint when `can_auto_fix=True`

**Installer integration tests**:
- Step 2 failure path with poll returning False: verifies `FakeLLMClient.calls` has 1 entry
  (diagnosis was called), `gate.require_approval_calls` has enriched body text
- Step 2 auto-fix path: `FakeLLMClient` returns `can_auto_fix=True`,
  `FakeSSHClient` returns ec=0 for `auto_fix_command`, subsequent poll returns running → returns True
- Step 2 auto-fix path: ec != 0 → still returns False (auto-fix failed)

---

#### Files Modified / Created

| File | Change |
|---|---|
| `netalertx/installer_diagnostics.py` | **NEW** — `InstallerDiagnostic`, evidence gatherers, `diagnose_installer_failure`, `format_diagnostic_for_hitl` |
| `prompts/diagnose_installer.md` | **NEW** — system prompt with Mosquitto/NetAlertX add-on/Supervisor failure mode knowledge |
| `netalertx/installer.py` | Add `llm_client` param; wire diagnostics into steps 2, 5a, 5b |
| `tests/test_core.py` | Add `InstallerDiagnostic` schema tests + evidence gatherer tests + installer integration tests |

No changes to: `interfaces.py`, `utils/autonomy.py`, `utils/notify.py`, `utils/ollama_client.py`,
`netalertx/diagnosis.py` — existing patterns reused unchanged.

---

**Done when:**
- `pytest --cov --cov-fail-under=90` passes with all new tests
- `black --check . && flake8 . && mypy --ignore-missing-imports . && bandit -r . -x ./tests,./.venv`
- Live test: HITL notification body for a Mosquitto failure contains a human-readable diagnosis
  with primary hypothesis, confidence %, evidence, and recommended action — not "Aborting."
