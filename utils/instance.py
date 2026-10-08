"""Stable per-installation identity for federated runbook evidence."""

import uuid
from pathlib import Path


def get_instance_id(state_dir: Path) -> str:
    """Return this installation's UUID4, persisted in state_dir/instance_id.

    Creates the file on first call; safe to call repeatedly — always returns
    the same id for a given installation.
    """
    id_file = state_dir / "instance_id"
    try:
        text = id_file.read_text().strip()
        if text:
            return text
    except OSError:
        pass
    instance_id = str(uuid.uuid4())
    id_file.parent.mkdir(parents=True, exist_ok=True)
    id_file.write_text(instance_id)
    return instance_id
