"""Read / minimally edit Codex's config.toml (provider + model only)."""
from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from .paths import CONFIG_FILE

# Providers that Codex ships with; user-defined ones come from [model_providers.*].
BUILTIN_PROVIDERS = ("openai", "ollama", "lmstudio")
DEFAULT_PROVIDER = "openai"
BOM = "\ufeff"


class ConfigError(Exception):
    pass


def _table_header_re(table: tuple[str, ...]) -> re.Pattern[str]:
    parts = [rf"(?:{re.escape(part)}|\"{re.escape(part)}\"|'{re.escape(part)}')" for part in table]
    return re.compile(r"\s*\[\s*" + r"\s*\.\s*".join(parts) + r"\s*\]\s*(#.*)?$")


def edit_string(text: str, table: tuple[str, ...], key: str, value: str | None) -> str:
    """Return `text` with string setting `key` of `table` set, or removed if None.

    `table` is the path of an existing [table] header; () means the root table.
    Only the one `key = ...` line is replaced / inserted / deleted, so comments,
    ordering and every other setting stay byte-for-byte the same. The result is
    re-parsed and compared against the expected data; if anything other than
    that key would change, ConfigError is raised and nothing is written.
    """
    try:
        expected = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config.toml is not valid TOML: {exc}") from exc
    node = expected
    for part in table:
        node = node.get(part) if isinstance(node, dict) else None
    if not isinstance(node, dict):
        raise ConfigError(f"config.toml has no [{'.'.join(table)}] table")
    if value is None:
        node.pop(key, None)
    else:
        node[key] = value

    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    if table:
        header = _table_header_re(table)
        starts = [index for index, line in enumerate(lines) if header.match(line.rstrip("\r\n"))]
        if len(starts) != 1:
            raise ConfigError(f"cannot locate a unique [{'.'.join(table)}] header in config.toml")
        start = starts[0] + 1
    else:
        start = 0
    end = len(lines)
    for index in range(start, len(lines)):
        if re.match(r"\s*\[", lines[index]):
            end = index
            break

    pattern = re.compile(rf"\s*{re.escape(key)}\s*=")
    found = next((index for index in range(start, end) if pattern.match(lines[index])), None)
    if value is None:
        if found is not None:
            del lines[found]
    else:
        new_line = f"{key} = {json.dumps(value, ensure_ascii=False)}"
        if found is not None:
            ending = lines[found][len(lines[found].rstrip("\r\n")):]
            lines[found] = new_line + ending
        else:
            if start > 0 and not lines[start - 1].endswith(("\n", "\r")):
                lines[start - 1] += eol  # header was the last line, without a newline
            lines.insert(start, new_line + eol)

    new_text = "".join(lines)
    try:
        actual = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"refusing to write: edited config would be invalid TOML ({exc})") from exc
    if actual != expected:
        raise ConfigError("refusing to write: edit would change settings other than " + ".".join((*table, key)))
    return new_text


def append_table(text: str, table: tuple[str, ...], values: dict[str, str]) -> str:
    """Return `text` with a new [table] of string settings appended at the end."""
    try:
        expected = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config.toml is not valid TOML: {exc}") from exc
    node = expected
    for part in table[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            raise ConfigError(f"config.toml: {part} is not a table")
    if table[-1] in node:
        raise ConfigError(f"config.toml already has [{'.'.join(table)}]")
    node[table[-1]] = dict(values)
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = [f"[{'.'.join(table)}]"] + [f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in values.items()]
    gap = "" if not text else (eol if text.endswith(("\n", "\r")) else eol * 2)
    new_text = text + gap + "".join(line + eol for line in lines)
    try:
        actual = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"refusing to write: edited config would be invalid TOML ({exc})") from exc
    if actual != expected:
        raise ConfigError("refusing to write: edit would change settings other than " + ".".join(table))
    return new_text


def set_top_level_string(text: str, key: str, value: str) -> str:
    return edit_string(text, (), key, value)


class CodexConfig:
    """config.toml wrapper exposing only provider and model."""

    def __init__(self, path: Path = CONFIG_FILE):
        self.path = path
        self.data: dict[str, Any] = {}
        self.stat: tuple[float, int] | None = None

    def _read_text(self) -> tuple[str, bool]:
        raw = self.path.read_bytes().decode("utf-8")
        has_bom = raw.startswith(BOM)
        return (raw[1:] if has_bom else raw), has_bom

    def _current_stat(self) -> tuple[float, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime, stat.st_size)

    def load(self) -> None:
        self.stat = self._current_stat()
        if self.stat is None:
            self.data = {}
            return
        text, _ = self._read_text()
        try:
            self.data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            self.data = {}
            raise ConfigError(f"{self.path}: {exc}") from exc

    def changed_on_disk(self) -> bool:
        return self._current_stat() != self.stat

    @property
    def model(self) -> str:
        value = self.data.get("model", "")
        return value if isinstance(value, str) else ""

    @property
    def provider(self) -> str:
        value = self.data.get("model_provider", "")
        return value if isinstance(value, str) and value else DEFAULT_PROVIDER

    def providers(self) -> list[str]:
        names = list(BUILTIN_PROVIDERS)
        custom = self.data.get("model_providers", {})
        if isinstance(custom, dict):
            names += [name for name in custom if name not in names]
        if self.provider not in names:
            names.append(self.provider)
        return names

    def get(self, table: tuple[str, ...], key: str) -> Any:
        node: Any = self.data
        for part in table:
            node = node.get(part) if isinstance(node, dict) else None
        return node.get(key) if isinstance(node, dict) else None

    def set_value(self, key: str, value: str) -> None:
        """Write one top-level string setting and reload."""
        self.set_table_value((), key, value)

    def set_table_value(self, table: tuple[str, ...], key: str, value: str | None) -> None:
        """Write (or, with None, remove) one string setting and reload."""
        self._edit(lambda text: edit_string(text, table, key, value))

    def add_table(self, table: tuple[str, ...], values: dict[str, str]) -> None:
        """Append a new [table] holding string settings and reload."""
        self._edit(lambda text: append_table(text, table, values))

    def _edit(self, edit) -> None:  # noqa: ANN001
        if self.path.exists():
            text, has_bom = self._read_text()
        else:
            text, has_bom = "", False
        new_text = edit(text)
        if new_text != text:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".cct-tmp")
            tmp.write_bytes(((BOM if has_bom else "") + new_text).encode("utf-8"))
            os.replace(tmp, self.path)
        self.load()
