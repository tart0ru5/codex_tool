"""Editable document model for a Codex rollout (.jsonl) file."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Entry:
    line: int
    obj: dict[str, Any]
    # Original line text. Kept so untouched entries are written back
    # byte-for-byte; set to None once the entry is edited.
    raw: str | None = None

    def serialize(self) -> str:
        if self.raw is not None:
            return self.raw
        return json.dumps(self.obj, ensure_ascii=False, separators=(",", ":"))


class SessionDoc:
    """JSONL conversation record loaded as editable dict entries."""

    def __init__(self, path: Path):
        self.path = path
        self.entries: list[Entry] = []
        self.mtime = 0.0
        self.size = 0

    def load(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self.entries.clear()
        self._remember_stat()
        text = self.path.read_bytes().decode("utf-8", errors="replace")
        for line_no, line in enumerate(text.split("\n"), 1):
            line = line.rstrip("\r")
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception as exc:
                obj = {"_parse_error": str(exc), "_raw": line}
            if not isinstance(obj, dict):
                obj = {"_value": obj}
            self.entries.append(Entry(line_no, obj, line))

    def save(self) -> None:
        text = "\n".join(entry.serialize() for entry in self.entries)
        tmp = self.path.with_name(self.path.name + ".cct-tmp")
        tmp.write_bytes((text + ("\n" if text else "")).encode("utf-8"))
        os.replace(tmp, self.path)
        self._remember_stat()

    def changed_on_disk(self) -> bool:
        if not self.path.exists():
            return False
        stat = self.path.stat()
        return stat.st_mtime != self.mtime or stat.st_size != self.size

    def renumber_lines(self) -> None:
        for index, entry in enumerate(self.entries, 1):
            entry.line = index

    def _remember_stat(self) -> None:
        stat = self.path.stat()
        self.mtime = stat.st_mtime
        self.size = stat.st_size
