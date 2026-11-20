"""Filesystem locations used by Codex Context Tool."""
from __future__ import annotations

import os
from pathlib import Path


def codex_home() -> Path:
    """Codex data directory: %CODEX_HOME% if set, otherwise ~/.codex."""
    override = os.environ.get("CODEX_HOME", "").strip()
    return Path(override) if override else Path.home() / ".codex"


CODEX_HOME = codex_home()
CONFIG_FILE = CODEX_HOME / "config.toml"
SESSION_INDEX_FILE = CODEX_HOME / "session_index.jsonl"

# Conversation record directories shown in the left tree: (label, path).
SESSION_ROOTS: list[tuple[str, Path]] = [
    ("sessions", CODEX_HOME / "sessions"),
    ("archived_sessions", CODEX_HOME / "archived_sessions"),
]
