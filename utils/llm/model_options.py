"""Translate detected model capabilities into concrete Ollama call parameters.

derive_call_options() is the single place that reads hardware + model capabilities
and produces a ModelCallOptions ready to pass into every LLM call.  All five call
parameters (think, num_ctx, temperature, keep_alive, num_predict) are derived here
so individual call sites never need to make policy decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Union

ThinkValue = Union[bool, Literal["low", "medium", "high"], None]


@dataclass
class ModelCallOptions:
    think: (
        ThinkValue  # None = don't pass the param; False = disable; str = budget level
    )
    num_ctx: int  # 0 = let Ollama decide (not recommended); derived value otherwise
    temperature: float
    keep_alive: int | str  # -1 = permanent; "5m" = 5 minute default
    num_predict: Optional[int] = None  # None = unlimited
    seed: Optional[int] = None


def _safe_ctx(context_length: int, available_ram_gb: float) -> int:
    """Derive a safe num_ctx from the model's context_length and available RAM."""
    if available_ram_gb >= 40.0:
        cap = 131072
    elif available_ram_gb >= 20.0:
        cap = 32768
    elif available_ram_gb >= 10.0:
        cap = 16384
    else:
        cap = 8192
    return min(context_length, cap) if context_length > 0 else cap


def derive_call_options(
    *,
    has_thinking: bool,
    recommended_temperature: Optional[float],
    context_length: int,
    available_ram_gb: float,
    debug_level: int,
    think_mode_cfg: str,
    num_ctx_override: int,
    supervisor_mode: bool,
    one_shot: bool = False,
    seed: Optional[int] = None,
) -> ModelCallOptions:
    """Derive ModelCallOptions from model capabilities and runtime context.

    Parameters
    ----------
    has_thinking:
        Whether the model supports thinking mode (from OllamaModelInfo).
    recommended_temperature:
        Model's preferred temperature (from `ollama show`), or None.
    context_length:
        Model's declared context window (from OllamaModelInfo).
    available_ram_gb:
        Estimated available RAM after model load.
    debug_level:
        Current DEBUG_LEVEL config value (0 = production, 1+ = debug).
    think_mode_cfg:
        OLLAMA_THINK_MODE config value: "auto" | "off" | "low" | "medium" | "high".
    num_ctx_override:
        OLLAMA_NUM_CTX config value; 0 = auto-derive.
    supervisor_mode:
        True when the work queue is running (keep model loaded).
    one_shot:
        True for triage/analysis calls that only need a short JSON response.
    seed:
        Optional seed for eval reproducibility.
    """
    # --- think ---
    think: ThinkValue
    if not has_thinking:
        think = None  # don't pass the parameter to non-thinking models
    elif think_mode_cfg == "off":
        think = False
    elif think_mode_cfg == "auto":
        # Production: think=False (avoids #17617 </think> leak → infinite loop).
        # Debug 1: low budget for inspection. Debug 2+: full reasoning.
        if debug_level == 0:
            think = False
        elif debug_level == 1:
            think = "low"
        else:
            think = "high"
    elif think_mode_cfg in ("low", "medium", "high"):
        think = think_mode_cfg  # type: ignore[assignment]
    else:
        think = False  # unknown value → safe default

    # --- num_ctx ---
    num_ctx = (
        num_ctx_override
        if num_ctx_override > 0
        else _safe_ctx(context_length, available_ram_gb)
    )

    # --- temperature ---
    # Use model's recommended temperature only when thinking is active and a
    # recommendation exists (e.g. 0.6 for qwen3 — avoids repetitive loops at 0.0).
    if think and recommended_temperature is not None:
        temperature = recommended_temperature
    else:
        temperature = 0.0

    # --- keep_alive ---
    # -1 (int) = keep loaded forever; Ollama rejects "-1" string (no time unit).
    if supervisor_mode:
        keep_alive: int | str = -1
    else:
        keep_alive = "5m"

    # --- num_predict ---
    num_predict = 1024 if one_shot else None

    return ModelCallOptions(
        think=think,
        num_ctx=num_ctx,
        temperature=temperature,
        keep_alive=keep_alive,
        num_predict=num_predict,
        seed=seed,
    )


def one_shot_options(model_name: str | None = None) -> dict:
    """Return an Ollama options dict for a one-shot structured output call.

    Derives num_ctx from cached hardware + model info; uses temperature=0.0 and
    num_predict=1024 to cap generation for short JSON responses.  Safe to call
    synchronously from any context — relies on cached hardware/model data.
    """
    import config as _cfg
    from utils.disk.hardware import detect_local_hardware, list_ollama_models

    name = model_name or _cfg.OLLAMA_MODEL
    hw = detect_local_hardware()
    models = list_ollama_models()
    info = next((m for m in models if m.name == name), None)

    if info is not None:
        available_ram_gb = max(0.0, hw.ram_gb - info.size_gb)
        ctx = _safe_ctx(info.context_length, available_ram_gb)
    else:
        ctx = _safe_ctx(0, hw.ram_gb * 0.35)

    if _cfg.OLLAMA_NUM_CTX > 0:
        ctx = _cfg.OLLAMA_NUM_CTX

    return {"temperature": 0.0, "num_ctx": ctx, "num_predict": 1024}
