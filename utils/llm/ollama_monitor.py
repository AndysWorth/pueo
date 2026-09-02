"""Real-time Ollama model polling via GET /api/ps.

poll_ollama_ps() returns the list of currently-loaded models.  Attribution
classifies each loaded model as 'inference' (Pueo's OLLAMA_MODEL), 'embedding'
(Pueo's RAG_EMBED_MODEL), or 'external' (anything else).
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any


@dataclass
class OllamaRunningModel:
    name: str
    size_bytes: int
    size_vram_bytes: int
    attribution: str  # "inference" | "embedding" | "external"


def poll_ollama_ps(endpoint: str) -> list[OllamaRunningModel]:
    """Query Ollama's /api/ps endpoint and return running models.

    Returns an empty list if Ollama is unreachable or returns an error.
    Never raises.
    """
    import config as _config

    try:
        url = endpoint.rstrip("/") + "/api/ps"
        with urllib.request.urlopen(url, timeout=5) as resp:  # nosec B310
            import json

            data: dict[str, Any] = json.load(resp)
    except (urllib.error.URLError, OSError, JSONDecodeError, Exception):  # nosec B110
        return []

    models = data.get("models") or []
    result: list[OllamaRunningModel] = []
    for entry in models:
        name = entry.get("name", "")
        size = int(entry.get("size", 0))
        size_vram = int(entry.get("size_vram", 0))
        if name == _config.OLLAMA_MODEL:
            attribution = "inference"
        elif name == _config.RAG_EMBED_MODEL:
            attribution = "embedding"
        else:
            attribution = "external"
        result.append(
            OllamaRunningModel(
                name=name,
                size_bytes=size,
                size_vram_bytes=size_vram,
                attribution=attribution,
            )
        )
    return result
