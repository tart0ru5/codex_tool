"""The tool's own settings: config.json plus a .env file holding the API keys."""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any

from .exchange_log import DEFAULT_LOG_KEEP, MAX_LOG_KEEP, MIN_LOG_KEEP
from .injections import normalize_state as normalize_injection_state

TOOL_DIR = Path(__file__).resolve().parent.parent
# CCT_SETTINGS lets tests use a throw-away settings file.
SETTINGS_FILE = Path(os.environ.get("CCT_SETTINGS", "").strip() or TOOL_DIR / "config.json")

ENV_FILE_NAME = ".env"
LOG_DIR_NAME = "logs"  # proxy request/response logs, next to config.json

DEFAULT_PROXY_PORT = 15721  # five digits by default; the user may pick any port
MIN_PORT, MAX_PORT = 1, 65535  # simply what TCP allows
KEY_ENV_PREFIX = "CCT_API_KEY_"
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ENV_LINE_RE = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
ENV_HEADER = [
    "# Codex Context Tool - API keys of the proxy presets.",
    "# Maintained by the tool; each preset in config.json names its variable in apiKeyEnv.",
]


class EnvFile:
    """Minimal KEY=value file. Lines it does not own are kept untouched."""

    def __init__(self, path: Path):
        self.path = path
        self.lines: list[str] = []
        self._loaded_text: str | None = None

    def load(self) -> None:
        if not self.path.exists():
            self.lines, self._loaded_text = [], None
            return
        text = self.path.read_bytes().decode("utf-8-sig")
        self.lines, self._loaded_text = text.splitlines(), text

    @staticmethod
    def _parse(line: str) -> tuple[str, str] | None:
        match = ENV_LINE_RE.match(line)
        if not match or line.lstrip().startswith("#"):
            return None
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return match.group(1), value

    def get(self, name: str) -> str:
        value = ""
        for line in self.lines:
            parsed = self._parse(line)
            if parsed and parsed[0] == name:
                value = parsed[1]  # last assignment wins, as in a shell
        return value

    def set(self, name: str, value: str | None) -> None:
        """Set `name`, or remove it when value is None / empty."""
        if value and ("\n" in value or "\r" in value):
            raise ValueError(f"{name}: value must be a single line")
        if value and (value != value.strip() or (len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'")):
            raise ValueError(f"{name}: value must not be quoted or padded with whitespace")
        kept = [line for line in self.lines if (self._parse(line) or ("",))[0] != name]
        if value:
            if not kept:
                kept = list(ENV_HEADER)
            kept.append(f"{name}={value}")
        self.lines = kept

    def save(self) -> None:
        text = "".join(line + "\n" for line in self.lines)
        if text == (self._loaded_text or ""):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_bytes(text.encode("utf-8"))
        os.replace(tmp, self.path)
        self._loaded_text = text

def display_path(path: Path) -> str:
    """`path` as shown to the user: relative to the tool directory when inside it."""
    try:
        return Path(path).resolve().relative_to(SETTINGS_FILE.resolve().parent).as_posix()
    except (ValueError, OSError):
        return str(path)


PLACEHOLDER_PREFIX = "ph:"  # ids of the placeholder rows in the injection page


def normalize_injection_view(raw: Any) -> dict[str, Any]:
    """How the injection page looks. Display only: nothing here reaches the proxy.

    order: parent id ("" = top level) -> child ids in display order, only for
    parents whose order differs from the catalog; placeholders: id -> name;
    titles: top-level entry id -> name given by the user; expanded: ids of the
    expanded entries (None = never saved); size: [width, height] of the page.
    """
    raw = raw if isinstance(raw, dict) else {}

    def strings(value: Any) -> list[str]:
        return list(dict.fromkeys(item for item in value if isinstance(item, str))) if isinstance(value, list) else []

    def names(value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {key: text for key, text in value.items() if isinstance(key, str) and isinstance(text, str) and text.strip()}

    order = raw.get("order")
    expanded = raw.get("expanded")
    size = raw.get("size")
    return {
        "order": {key: strings(ids) for key, ids in order.items() if isinstance(key, str) and isinstance(ids, list)}
        if isinstance(order, dict)
        else {},
        "placeholders": {key: text for key, text in names(raw.get("placeholders")).items() if key.startswith(PLACEHOLDER_PREFIX)},
        "titles": names(raw.get("titles")),
        "expanded": sorted(strings(expanded)) if isinstance(expanded, list) else None,
        "size": list(size)
        if isinstance(size, list)
        and len(size) == 2
        and all(isinstance(n, int) and not isinstance(n, bool) and 100 <= n <= 20000 for n in size)
        else None,
    }


COMMON_ROLES = ["system", "developer", "user", "assistant"]


def new_prompt_preset(name: str) -> dict[str, Any]:
    """A prompt preset: only the fixed entry standing for Codex's own content."""
    return {"name": name, "entries": [{"codex": True}]}


def normalize_prompts(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    presets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw.get("presets") if isinstance(raw.get("presets"), list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        entries: list[dict[str, Any]] = []
        has_codex = False
        for entry in item.get("entries") if isinstance(item.get("entries"), list) else []:
            if not isinstance(entry, dict):
                continue
            if entry.get("codex") is True:
                if not has_codex:
                    entries.append({"codex": True})
                    has_codex = True
            elif isinstance(entry.get("role"), str) and entry["role"].strip() and isinstance(entry.get("content"), str):
                entries.append(
                    {"role": entry["role"].strip(), "content": entry["content"], "enabled": entry.get("enabled") is not False}
                )
        if not has_codex:
            entries.append({"codex": True})
        presets.append({"name": name, "entries": entries})
    if not presets:
        presets.append(new_prompt_preset(DEFAULT_PRESET_NAME))
    active = raw.get("activePreset")
    if active not in [preset["name"] for preset in presets]:
        active = presets[0]["name"]
    return {"activePreset": active, "presets": presets}


def prompt_route(prompts: dict[str, Any]) -> tuple[str, tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    """(preset name, entries before Codex's content, entries after it); enabled, non-empty ones only."""
    preset = next((p for p in prompts["presets"] if p["name"] == prompts["activePreset"]), prompts["presets"][0])
    before: list[tuple[str, str]] = []
    after: list[tuple[str, str]] = []
    target = before
    for entry in preset["entries"]:
        if entry.get("codex"):
            target = after
        elif entry["enabled"] and entry["content"].strip():
            target.append((entry["role"], entry["content"]))
    return preset["name"], tuple(before), tuple(after)


DEFAULT_PRESET_NAME = "默认"


def new_preset(name: str) -> dict[str, Any]:
    """An API preset. Empty model / key / effort mean "pass Codex's own value through".

    baseUrl is required for forwarding: without it the proxy answers every
    request with an error. apiKey only lives in memory and in the .env file,
    under the variable named by apiKeyEnv; it is never written to config.json.
    """
    return {
        "name": name,
        "baseUrl": "",
        "model": "",
        "apiKey": "",
        "apiKeyEnv": "",
        "extraHeaders": {},
        "extraBody": {},
        "reasoningEffort": "",
    }


def normalize_proxy(raw: Any) -> dict[str, Any]:
    """Return a complete, well-typed proxy settings dict built from `raw`."""
    raw = raw if isinstance(raw, dict) else {}
    port = raw.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not MIN_PORT <= port <= MAX_PORT:
        port = DEFAULT_PROXY_PORT

    presets: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_env: set[str] = set()
    for item in raw.get("presets") if isinstance(raw.get("presets"), list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        preset = new_preset(name)
        for key in ("baseUrl", "model", "apiKey", "reasoningEffort"):
            if isinstance(item.get(key), str):
                preset[key] = item[key].strip()
        for key in ("extraHeaders", "extraBody"):
            if isinstance(item.get(key), dict):
                preset[key] = copy.deepcopy(item[key])
        env_name = item.get("apiKeyEnv")
        if isinstance(env_name, str) and ENV_NAME_RE.fullmatch(env_name) and env_name not in seen_env:
            seen_env.add(env_name)
            preset["apiKeyEnv"] = env_name
        presets.append(preset)
    if not presets:
        presets.append(new_preset(DEFAULT_PRESET_NAME))

    active = raw.get("activePreset")
    if active not in [preset["name"] for preset in presets]:
        active = presets[0]["name"]

    result: dict[str, Any] = {
        "enabled": raw.get("enabled") is True,
        "port": port,
        "activePreset": active,
        "presets": presets,
    }
    # `applied` records what the tool changed in Codex's config.toml so it can
    # be undone later. Never invent or drop it here.
    if isinstance(raw.get("applied"), dict):
        result["applied"] = copy.deepcopy(raw["applied"])
    return result


def active_preset(proxy: dict[str, Any]) -> dict[str, Any]:
    for preset in proxy["presets"]:
        if preset["name"] == proxy["activePreset"]:
            return preset
    return proxy["presets"][0]


class Settings:
    def __init__(self, path: Path = SETTINGS_FILE, env_path: Path | None = None):
        self.path = path
        self.env = EnvFile(env_path or path.with_name(ENV_FILE_NAME))
        self.data: dict[str, Any] = {}
        self.load_error: str | None = None

    def load(self) -> None:
        self.data, self.load_error = {}, None
        try:
            self.env.load()
        except Exception as exc:
            self.load_error = f"{display_path(self.env.path)}: {exc}"
            return
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("top level is not a JSON object")
            self.data = data
        except Exception as exc:  # keep running with defaults, but say so
            self.load_error = f"{display_path(self.path)}: {exc}"

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    @property
    def proxy(self) -> dict[str, Any]:
        """Proxy settings with each preset's apiKey filled in from the .env file."""
        proxy = normalize_proxy(self.data.get("proxy"))
        self.env.load()  # pick up hand edits; errors propagate rather than hide a key
        for preset in proxy["presets"]:
            if preset["apiKeyEnv"]:
                preset["apiKey"] = self.env.get(preset["apiKeyEnv"])
        return proxy

    def set_proxy(self, proxy: dict[str, Any]) -> None:
        """Store proxy settings: keys go to the .env file, the rest to config.json."""
        proxy = normalize_proxy(proxy)
        self.env.load()
        old_names = {
            preset["apiKeyEnv"] for preset in normalize_proxy(self.data.get("proxy"))["presets"] if preset["apiKeyEnv"]
        }
        used = {preset["apiKeyEnv"] for preset in proxy["presets"] if preset["apiKeyEnv"]}
        for preset in proxy["presets"]:
            key = preset.pop("apiKey")
            if key and not preset["apiKeyEnv"]:
                number = 1
                while f"{KEY_ENV_PREFIX}{number}" in used | old_names or self.env.get(f"{KEY_ENV_PREFIX}{number}"):
                    number += 1
                preset["apiKeyEnv"] = f"{KEY_ENV_PREFIX}{number}"
                used.add(preset["apiKeyEnv"])
            if preset["apiKeyEnv"]:
                self.env.set(preset["apiKeyEnv"], key)
        for name in old_names - used:  # presets that were deleted
            self.env.set(name, None)
        self.env.save()
        self.data["proxy"] = proxy
        self.save()

    @property
    def prompts(self) -> dict[str, Any]:
        return normalize_prompts(self.data.get("prompts"))

    def set_prompts(self, prompts: dict[str, Any]) -> None:
        self.data["prompts"] = normalize_prompts(prompts)
        self.save()

    @property
    def injections(self) -> dict[str, Any]:
        """Switches of the Codex prompt injections: {"blockAll": bool, "off": [ids]}."""
        return normalize_injection_state(self.data.get("injections"))

    def set_injections(self, state: dict[str, Any]) -> None:
        self.data["injections"] = normalize_injection_state(state)
        self.save()

    @property
    def injection_view(self) -> dict[str, Any]:
        """Layout of the injection page (order, placeholders, names, expansion, size)."""
        return normalize_injection_view(self.data.get("injectionView"))

    def set_injection_view(self, view: dict[str, Any]) -> None:
        view = normalize_injection_view(view)
        if self.data.get("injectionView") != view:
            self.data["injectionView"] = view
            self.save()

    @property
    def log_dir(self) -> Path:
        return self.path.with_name(LOG_DIR_NAME)

    @property
    def log_keep(self) -> int:
        """How many request/response logs to keep."""
        keep = self.data.get("logKeep")
        if isinstance(keep, int) and not isinstance(keep, bool) and MIN_LOG_KEEP <= keep <= MAX_LOG_KEEP:
            return keep
        return DEFAULT_LOG_KEEP

    def set_log_keep(self, keep: int) -> None:
        if self.data.get("logKeep") != keep:
            self.data["logKeep"] = keep
            self.save()

    @property
    def splitter_sizes(self) -> list[int] | None:
        sizes = self.data.get("splitterSizes")
        if (
            isinstance(sizes, list)
            and len(sizes) == 3
            and all(isinstance(size, int) and not isinstance(size, bool) and size >= 0 for size in sizes)
            and sum(sizes) > 0
        ):
            return sizes
        return None

    def set_splitter_sizes(self, sizes: list[int]) -> None:
        if self.data.get("splitterSizes") != sizes:
            self.data["splitterSizes"] = sizes
            self.save()
