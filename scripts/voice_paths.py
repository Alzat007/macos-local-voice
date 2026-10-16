"""Shared paths for the portable skill and its external, private voice data."""

import os
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = Path.home() / "Library" / "Application Support" / "macos-local-voice"


def resolve_data_dir(value=None):
    """Resolve CLI > environment > default without creating any directory."""
    selected = value if value is not None else os.environ.get("LOCAL_VOICE_DATA_DIR")
    directory = Path(selected if selected else DEFAULT_DATA_DIR).expanduser().resolve()
    if directory == SKILL_ROOT or SKILL_ROOT in directory.parents:
        raise ValueError("Private voice data must be outside the skill source directory.")
    return directory
