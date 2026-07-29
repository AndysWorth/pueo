"""Shared fixtures and markers for integration tests.

Integration tests live here rather than in tests/ so the unit CI job can skip
them with --ignore=tests/integration.  Run manually:

    pytest tests/integration/ -m "not live_ha" -v        # no live HA needed
    HA_HOST=homeassistant.local pytest tests/integration/ -m live_ha -v
"""

import os
import sys
from pathlib import Path

import pytest

# Keep the project root importable from within this subdirectory.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture(autouse=True)
def skip_without_live_ha(request):
    if request.node.get_closest_marker("live_ha"):
        if not os.environ.get("HA_HOST"):
            pytest.skip("live_ha: HA_HOST env var not set")
