"""Tests for utils/llm/ollama_monitor.py."""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from utils.llm.ollama_monitor import OllamaRunningModel, poll_ollama_ps


_SAMPLE_RESPONSE = {
    "models": [
        {"name": "qwen2.5-coder:7b", "size": 5_000_000_000, "size_vram": 4_500_000_000},
        {
            "name": "nomic-embed-text:latest",
            "size": 300_000_000,
            "size_vram": 280_000_000,
        },
        {"name": "llava:13b", "size": 8_000_000_000, "size_vram": 7_800_000_000},
    ]
}


def _make_urlopen_mock(data: dict):
    """Return a context-manager mock that yields a response with JSON data."""
    body = json.dumps(data).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)

    class _FakeRead:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _FakeRead()


@patch("config.OLLAMA_MODEL", "qwen2.5-coder:7b")
@patch("config.RAG_EMBED_MODEL", "nomic-embed-text:latest")
def test_poll_ollama_ps_returns_models(monkeypatch):
    """Valid /api/ps response returns correct OllamaRunningModel list."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda url, timeout: _make_urlopen_mock(_SAMPLE_RESPONSE),
    )
    models = poll_ollama_ps("http://localhost:11434")
    assert len(models) == 3
    names = [m.name for m in models]
    assert "qwen2.5-coder:7b" in names
    assert "nomic-embed-text:latest" in names
    assert "llava:13b" in names


@patch("config.OLLAMA_MODEL", "qwen2.5-coder:7b")
@patch("config.RAG_EMBED_MODEL", "nomic-embed-text:latest")
def test_attribution_inference(monkeypatch):
    """OLLAMA_MODEL gets attribution='inference'."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda url, timeout: _make_urlopen_mock(_SAMPLE_RESPONSE),
    )
    models = poll_ollama_ps("http://localhost:11434")
    inf = next(m for m in models if m.name == "qwen2.5-coder:7b")
    assert inf.attribution == "inference"


@patch("config.OLLAMA_MODEL", "qwen2.5-coder:7b")
@patch("config.RAG_EMBED_MODEL", "nomic-embed-text:latest")
def test_attribution_embedding(monkeypatch):
    """RAG_EMBED_MODEL gets attribution='embedding'."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda url, timeout: _make_urlopen_mock(_SAMPLE_RESPONSE),
    )
    models = poll_ollama_ps("http://localhost:11434")
    emb = next(m for m in models if m.name == "nomic-embed-text:latest")
    assert emb.attribution == "embedding"


@patch("config.OLLAMA_MODEL", "qwen2.5-coder:7b")
@patch("config.RAG_EMBED_MODEL", "nomic-embed-text:latest")
def test_attribution_external(monkeypatch):
    """Unknown models get attribution='external'."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda url, timeout: _make_urlopen_mock(_SAMPLE_RESPONSE),
    )
    models = poll_ollama_ps("http://localhost:11434")
    ext = next(m for m in models if m.name == "llava:13b")
    assert ext.attribution == "external"


@patch("config.OLLAMA_MODEL", "qwen2.5-coder:7b")
@patch("config.RAG_EMBED_MODEL", "nomic-embed-text:latest")
def test_poll_ollama_ps_unreachable_returns_empty(monkeypatch):
    """Network errors return [] without raising."""
    import urllib.error

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda url, timeout: (_ for _ in ()).throw(
            urllib.error.URLError("connection refused")
        ),
    )
    models = poll_ollama_ps("http://localhost:11434")
    assert models == []


@patch("config.OLLAMA_MODEL", "qwen2.5-coder:7b")
@patch("config.RAG_EMBED_MODEL", "nomic-embed-text:latest")
def test_poll_ollama_ps_empty_models(monkeypatch):
    """Empty models list returns []."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda url, timeout: _make_urlopen_mock({"models": []}),
    )
    models = poll_ollama_ps("http://localhost:11434")
    assert models == []


@patch("config.OLLAMA_MODEL", "qwen2.5-coder:7b")
@patch("config.RAG_EMBED_MODEL", "nomic-embed-text:latest")
def test_size_bytes_parsed(monkeypatch):
    """size_bytes and size_vram_bytes are populated correctly."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda url, timeout: _make_urlopen_mock(_SAMPLE_RESPONSE),
    )
    models = poll_ollama_ps("http://localhost:11434")
    inf = next(m for m in models if m.name == "qwen2.5-coder:7b")
    assert inf.size_bytes == 5_000_000_000
    assert inf.size_vram_bytes == 4_500_000_000


@patch("config.OLLAMA_MODEL", "qwen2.5-coder:7b")
@patch("config.RAG_EMBED_MODEL", "nomic-embed-text:latest")
def test_poll_ollama_ps_bad_json(monkeypatch):
    """Malformed JSON returns [] without raising."""

    class _BadRead:
        def read(self):
            return b"not json at all"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout: _BadRead())
    models = poll_ollama_ps("http://localhost:11434")
    assert models == []
