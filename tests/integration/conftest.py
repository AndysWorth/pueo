"""Shared fixtures and markers for integration and eval tests.

Tests here are excluded from CI with --ignore=tests/integration.
Run locally on demand:

    # Seam tests only (fast, no external services needed):
    pytest tests/integration/ -m "not live_ha and not ollama" -v

    # Seam tests + evals together (Ollama must be running):
    pytest tests/integration/ -m "not live_ha" -v

    # Evals only:
    pytest tests/integration/ -m ollama -v

    # Live-HA smoke tests (HA_HOST must be set):
    HA_HOST=homeassistant.local pytest tests/integration/ -m live_ha -v
"""

import functools
import os
import sys
from pathlib import Path

import pytest

# Keep the project root importable from within this subdirectory.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@functools.lru_cache(maxsize=1)
def _ollama_reachable() -> bool:
    """Return True if a local Ollama instance answers on its API port."""
    try:
        import httpx
        from config import OLLAMA_ENDPOINT

        r = httpx.get(f"{OLLAMA_ENDPOINT}/api/tags", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


@pytest.fixture(autouse=True)
def skip_without_live_ha(request):
    if request.node.get_closest_marker("live_ha"):
        if not os.environ.get("HA_HOST"):
            pytest.skip("live_ha: HA_HOST env var not set")


@pytest.fixture(autouse=True)
def skip_without_ollama(request):
    if request.node.get_closest_marker("ollama"):
        if not _ollama_reachable():
            pytest.skip(
                "ollama: local Ollama not reachable (start with `ollama serve`)"
            )
