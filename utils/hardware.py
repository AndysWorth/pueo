"""Hardware detection and Ollama model recommendation.

Detects local CPU/RAM/architecture, lists installed Ollama models with
their capabilities, and recommends the best-fit model for the hardware tier.
"""

from __future__ import annotations

import platform
import re
import subprocess  # nosec B404 — only sysctl, system_profiler, ollama CLI; no user input
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import config
from utils.logging import get_logger

log = get_logger("hardware")

# ---------------------------------------------------------------------------
# Candidate model catalog
# (name_prefix, tier)
# tier ranks model quality for Pueo's use case (higher = better).
# qwen3 family is preferred over qwen2.5-coder at equivalent size due to
# significantly better tool-call compliance and thinking-mode support.
# Within a tier, list order breaks ties (first match wins).
# ---------------------------------------------------------------------------
CANDIDATE_MODELS: list[tuple[str, int]] = [
    # qwen3 — best tool compliance; thinking mode available via /think flag
    ("qwen3:30b-a3b", 7),  # MoE: dense 32b quality at ~14b active-param footprint
    ("qwen3:32b", 7),
    ("qwen3:14b", 5),
    ("qwen3:8b", 4),
    ("qwen3:4b", 3),
    # qwen2.5-coder — solid but lower tool-call reliability than qwen3
    ("qwen2.5-coder:32b", 6),
    ("qwen2.5-coder:14b", 4),
    ("qwen2.5-coder:7b", 3),
    ("qwen2.5-coder:3b", 2),
    ("qwen2.5-coder:1.5b", 1),
    # other models
    ("mistral", 3),
    ("llama3.2", 1),
]

# Fraction of total RAM the model size must fit within.
# Leaves headroom for the OS and other processes.
MEMORY_BUDGET_FRAC: float = 0.65


@dataclass
class HardwareProfile:
    chip: str
    arch: str
    ram_gb: float
    cpu_cores: int
    detected_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class OllamaModelInfo:
    name: str
    size_gb: float
    has_tools: bool
    context_length: int
    last_seen_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def detect_local_hardware() -> HardwareProfile:
    """Return a HardwareProfile for the machine running Pueo.

    Never raises — returns zero/unknown values on detection failure.
    """
    arch = platform.machine()  # "arm64" on Apple Silicon, "x86_64" on Intel
    cores = 0
    ram_gb = 0.0
    chip = "Unknown"

    system = platform.system()
    try:
        if system == "Darwin":
            raw = subprocess.check_output(  # nosec B603 B607
                ["sysctl", "-n", "hw.memsize"], text=True
            ).strip()
            ram_gb = int(raw) / (1024**3)
            cores_raw = subprocess.check_output(  # nosec B603 B607
                ["sysctl", "-n", "hw.physicalcpu"], text=True
            ).strip()
            cores = int(cores_raw)
            profiler = subprocess.check_output(  # nosec B603 B607
                ["system_profiler", "SPHardwareDataType"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            for line in profiler.splitlines():
                if "Chip:" in line:
                    chip = line.split("Chip:", 1)[1].strip()
                    break
                if "Processor Name:" in line:
                    chip = line.split("Processor Name:", 1)[1].strip()
        elif system == "Linux":
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        ram_gb = kb / (1024**2)
                        break
            with open("/proc/cpuinfo") as fh:
                cpu_lines = [ln for ln in fh if ln.startswith("model name")]
                if cpu_lines:
                    chip = cpu_lines[0].split(":", 1)[1].strip()
                cores = len(cpu_lines) or 1
    except Exception as exc:  # nosec B110
        log.warning("hardware_detect_error", error=str(exc))

    return HardwareProfile(
        chip=chip, arch=arch, ram_gb=round(ram_gb, 1), cpu_cores=cores
    )


def list_ollama_models() -> list[OllamaModelInfo]:
    """Query the local Ollama instance for installed models and their capabilities.

    Returns an empty list if Ollama is not running rather than raising.
    Skips embedding-only models (name contains 'embed').
    """
    try:
        raw = subprocess.check_output(  # nosec B603 B607
            ["ollama", "list"], text=True, stderr=subprocess.DEVNULL
        )
    except Exception:  # nosec B110
        log.info("ollama_not_running_or_not_found")
        return []

    models: list[OllamaModelInfo] = []
    now = datetime.now(timezone.utc).isoformat()
    for line in raw.splitlines()[1:]:  # skip header row
        parts = line.split()
        if not parts:
            continue
        name = parts[0]
        if "embed" in name.lower():
            continue
        size_gb = _parse_size_from_parts(parts)
        has_tools, ctx_len = _check_model_caps(name)
        models.append(
            OllamaModelInfo(
                name=name,
                size_gb=size_gb,
                has_tools=has_tools,
                context_length=ctx_len,
                last_seen_at=now,
            )
        )

    return models


def _parse_size_from_parts(parts: list[str]) -> float:
    """Extract model size in GB from an `ollama list` output row."""
    # Row format: NAME    ID    SIZE    MODIFIED
    # SIZE field: "4.7 GB", "986 MB", "23 GB"
    for i, p in enumerate(parts):
        try:
            val = float(p)
        except ValueError:
            continue
        unit = parts[i + 1].upper() if i + 1 < len(parts) else ""
        if unit == "GB":
            return val
        if unit == "MB":
            return val / 1024.0
        if unit == "KB":
            return val / (1024.0**2)
    return 0.0


def _check_model_caps(model_name: str) -> tuple[bool, int]:
    """Return (has_tools, context_length) via `ollama show`."""
    try:
        raw = subprocess.check_output(  # nosec B603 B607
            ["ollama", "show", model_name], text=True, stderr=subprocess.DEVNULL
        )
        has_tools = "tools" in raw.lower()
        ctx = 0
        for line in raw.splitlines():
            if "context length" in line.lower():
                m = re.search(r"(\d+)", line)
                if m:
                    ctx = int(m.group(1))
                break
        return has_tools, ctx
    except Exception:  # nosec B110
        return False, 0


def recommend_model(
    profile: HardwareProfile, available: list[OllamaModelInfo]
) -> Optional[str]:
    """Pick the best installed model for the hardware tier.

    Selection rules (in order):
    1. Model must support the Ollama tools API (required for AgentLoop)
    2. Model size must fit within MEMORY_BUDGET_FRAC × total RAM
    3. Among fitting models, walk CANDIDATE_MODELS best-first (by tier)
       and return the first match by model name base
    4. If no candidate matches, return the largest fitting tools-capable model
    5. Return None if nothing fits or no tools-capable model is installed
    """
    budget_gb = profile.ram_gb * MEMORY_BUDGET_FRAC
    fitting = [m for m in available if m.has_tools and m.size_gb <= budget_gb]
    if not fitting:
        log.info(
            "recommend_model_no_candidates",
            budget_gb=round(budget_gb, 1),
            tools_capable=sum(1 for m in available if m.has_tools),
        )
        return None

    installed = {m.name: m for m in fitting}

    # Walk candidates best-first; match on model name base prefix
    for name_prefix, tier in sorted(CANDIDATE_MODELS, key=lambda x: x[1], reverse=True):
        base = name_prefix.split(":")[0]
        for inst_name in installed:
            if inst_name.split(":")[0] == base:
                log.info(
                    "recommend_model_selected",
                    model=inst_name,
                    tier=tier,
                    size_gb=installed[inst_name].size_gb,
                )
                return inst_name

    # Fallback: largest fitting model
    best = max(fitting, key=lambda m: m.size_gb)
    log.info("recommend_model_fallback", model=best.name, size_gb=best.size_gb)
    return best.name


def select_best_model() -> str:
    """Detect hardware, list installed models, recommend the best fit.

    Returns the current config.OLLAMA_MODEL unchanged if recommendation fails.
    """
    profile = detect_local_hardware()
    available = list_ollama_models()
    candidate = recommend_model(profile, available)
    if candidate is None:
        return config.OLLAMA_MODEL
    return candidate


def apply_model_selection(model_name: str) -> None:
    """Switch the active model in-process and persist to config.yaml."""
    import yaml

    config.OLLAMA_MODEL = model_name  # type: ignore[attr-defined]

    cfg_path = config._config_path  # type: ignore[attr-defined]
    cfg: dict = {}
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    if "ollama" not in cfg or not isinstance(cfg["ollama"], dict):
        cfg["ollama"] = {}
    cfg["ollama"]["model"] = model_name
    cfg_path.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True))
    log.info("model_applied", model=model_name)
