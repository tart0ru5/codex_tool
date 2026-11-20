"""Glue between the proxy server, the tool's settings and Codex's config.toml.

Enabling the proxy rewrites the base URL of Codex's active provider so that it
points at the local listener; the previous value is remembered in the tool's
config.json (`proxy.applied`) and put back when the proxy is switched off or
the tool exits.

While the proxy is on, config.toml also carries a `model_catalog_json` line
pointing at the model catalog the tool maintains in Codex's directory (see
model_catalog.py); the line is taken out again together with the redirect, the
file itself stays.
"""
from __future__ import annotations

import errno
from typing import Any
from urllib.parse import urlsplit

from pathlib import Path

from . import injections, model_catalog
from .config import CodexConfig, ConfigError
from .proxy import LISTEN_HOST, ProxyServer, Route, is_loopback
from .settings import Settings, active_preset, prompt_route

# EADDRINUSE, plus the Windows codes WSAEADDRINUSE / WSAEACCES (port held exclusively).
PORT_IN_USE_ERRNOS = {errno.EADDRINUSE, 10048, 10013}

# Provider the tool registers in Codex's config.toml; it always points at the tool's port.
TOOL_PROVIDER = "codex_tool"


class ProxyError(Exception):
    pass


def proxy_url(port: int) -> str:
    return f"http://{LISTEN_HOST}:{port}"


def base_url_location(provider: str, config: CodexConfig) -> tuple[tuple[str, ...], str]:
    """(table path, key) of the config.toml setting holding `provider`'s base URL."""
    if provider == "openai":
        return (), "openai_base_url"
    custom = config.data.get("model_providers")
    if isinstance(custom, dict) and isinstance(custom.get(provider), dict):
        return ("model_providers", provider), "base_url"
    raise ProxyError(
        f"当前 provider 是 Codex 内置的 {provider!r}，config.toml 中没有可修改的 base URL，无法接入代理。\n"
        "请切换到 openai 或 [model_providers.*] 中自定义的 provider。"
    )


def points_at_local_port(url: Any, port: int) -> bool:
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    try:
        return is_loopback(parts.hostname or "") and parts.port == port
    except ValueError:
        return False


class ProxyController:
    def __init__(self, config: CodexConfig, settings: Settings, server: ProxyServer):
        self.config = config
        self.settings = settings
        self.server = server
        self.upstream = ""

    # -- helpers -------------------------------------------------------------

    def _save_applied(self, applied: dict[str, Any] | None) -> None:
        proxy = self.settings.proxy
        if applied is None:
            proxy.pop("applied", None)
        else:
            proxy["applied"] = applied
        self.settings.set_proxy(proxy)

    def _restore(self) -> str:
        """Put back the base URL recorded in `applied`. Returns a note for the user."""
        applied = self.settings.proxy.get("applied")
        if not applied:
            return ""
        table = tuple(applied.get("table") or ())
        key = applied.get("key")
        note = ""
        if isinstance(key, str) and key:
            self.config.load()
            current = self.config.get(table, key)
            if current == applied.get("proxyUrl"):
                original = applied.get("original")
                self.config.set_table_value(table, key, original if isinstance(original, str) else None)
                note = f"已恢复 Codex 的 {'.'.join((*table, key))}"
            else:
                note = f"{'.'.join((*table, key))} 已被其他程序改动，未恢复"
        self._save_applied(None)
        return note

    # -- model catalog -------------------------------------------------------

    def catalog_path(self) -> Path:
        """The catalog file: in Codex's own directory, next to its config.toml."""
        return Path(self.config.path).resolve().with_name(model_catalog.CATALOG_FILE_NAME)

    def catalog_setting(self) -> str:
        """Value written to config.toml for the catalog file.

        With the home directory written as `~/` (Codex expands it, as in
        DeepSeek's Codex guide) when the file is inside it; the full path
        otherwise, e.g. when CODEX_HOME points somewhere else.
        """
        return self._home_relative(self.catalog_path())

    def config_display(self) -> str:
        """Codex's config.toml as shown to the user, in the same `~/` form."""
        return self._home_relative(Path(self.config.path).resolve())

    @staticmethod
    def _home_relative(path: Path) -> str:
        try:
            relative = path.relative_to(Path.home().resolve())
        except ValueError:
            return str(path)
        return "~/" + relative.as_posix()

    def _own_catalog_values(self) -> set[str]:
        """Values of the config.toml line that are the tool's own (earlier versions wrote the full path)."""
        return {self.catalog_setting(), str(self.catalog_path())}

    def _sync_catalog(self, proxy: dict[str, Any]) -> tuple[bool, str]:
        """Add the presets' model names to the catalog file.

        Returns (file can be handed to Codex, note for the user). Codex refuses
        to start on a catalog that is unreadable or empty, so such a file must
        not be referenced from config.toml.
        """
        name = model_catalog.CATALOG_FILE_NAME
        try:
            added, total = model_catalog.sync(self.catalog_path(), model_catalog.preset_models(proxy))
        except model_catalog.CatalogError as exc:
            return False, f"模型适配未生效：{exc}"
        return total > 0, f"{name} 新增模型 {'、'.join(added)}" if added else ""

    def _set_catalog_line(self, usable: bool) -> str:
        """Point Codex at the catalog file (config.toml must be loaded)."""
        key = model_catalog.CONFIG_KEY
        ours = self.catalog_setting()
        current = self.config.get((), key)
        if current in self._own_catalog_values():
            if not usable:
                self.config.set_table_value((), key, None)
            elif current != ours:
                self.config.set_table_value((), key, ours)
                return f"已把 config.toml 中的 {key} 改写为 {ours}"
            return ""
        if current is not None:
            return f"config.toml 中已有指向别处的 {key}，未改动"
        if not usable:
            return ""
        self.config.set_table_value((), key, ours)
        return f"已在 config.toml 加入 {key}"

    def _remove_catalog_line(self) -> str:
        """Take the tool's own model_catalog_json line out of config.toml."""
        key = model_catalog.CONFIG_KEY
        try:
            self.config.load()
        except ConfigError as exc:
            return f"无法读取 config.toml，未检查其中的 {key}：{exc}"
        if self.config.get((), key) not in self._own_catalog_values():
            return ""  # absent, or someone else's
        self.config.set_table_value((), key, None)
        return f"已从 config.toml 去除 {key}"

    # -- public --------------------------------------------------------------

    def _ensure_tool_provider(self, url: str) -> str:
        """Register [model_providers.codex_tool] if missing; keep it on the tool's port."""
        custom = self.config.data.get("model_providers")
        entry = custom.get(TOOL_PROVIDER) if isinstance(custom, dict) else None
        table = ("model_providers", TOOL_PROVIDER)
        if not isinstance(entry, dict):
            self.config.add_table(table, {"name": TOOL_PROVIDER, "base_url": url, "wire_api": "responses"})
            return f"已注册 provider {TOOL_PROVIDER}"
        if entry.get("base_url") != url:
            self.config.set_table_value(table, "base_url", url)
            return f"provider {TOOL_PROVIDER} 的 base_url 已更新"
        return ""

    def apply(self) -> str:
        """Bring listener + Codex config in line with the saved proxy settings."""
        proxy = self.settings.proxy
        # Every start of the proxy and every saved preset change ends up here.
        catalog_usable, catalog_note = self._sync_catalog(proxy)
        if not proxy["enabled"]:
            return "；".join(filter(None, [self.release() or "代理未开启", catalog_note]))

        self.config.load()
        provider = self.config.provider
        port = proxy["port"]
        url = proxy_url(port)
        # The tool's own provider always points at the tool: nothing to redirect or restore.
        own = provider == TOOL_PROVIDER
        table, key = ((), "") if own else base_url_location(provider, self.config)

        preset = active_preset(proxy)
        # No fallback: a preset without baseURL makes the proxy answer requests
        # with an error instead of quietly forwarding somewhere else.
        upstream = preset["baseUrl"]
        if upstream:
            parts = urlsplit(upstream)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ProxyError(f"上游地址无效：{upstream}")
            if points_at_local_port(upstream, port):
                raise ProxyError(f"上游地址 {upstream} 指向代理自身监听的端口。")

        try:
            self.server.start(port)
        except OSError as exc:
            if exc.errno in PORT_IN_USE_ERRNOS or getattr(exc, "winerror", None) in PORT_IN_USE_ERRNOS:
                raise ProxyError(f"端口 {port} 已被占用，请换一个端口。（{exc}）") from exc
            raise ProxyError(f"无法监听 {LISTEN_HOST}:{port}：{exc}") from exc
        prompt_name, prompt_before, prompt_after = prompt_route(self.settings.prompts)
        injection_filter = self._injection_filter()
        self.server.set_route(
            Route(
                upstream=upstream,
                preset=preset["name"],
                model=preset["model"],
                api_key=preset["apiKey"],
                extra_headers=preset["extraHeaders"],
                extra_body=preset["extraBody"],
                reasoning_effort=preset["reasoningEffort"],
                prompt_preset=prompt_name,
                prompt_before=prompt_before,
                prompt_after=prompt_after,
                injection_filter=injection_filter,
            )
        )
        self.upstream = upstream

        applied = proxy.get("applied")
        if applied and (own or tuple(applied.get("table") or ()) != table or applied.get("key") != key):
            self._restore()  # provider changed since last time
            applied = None
        self.config.load()
        notes = [self._ensure_tool_provider(url), catalog_note, self._set_catalog_line(catalog_usable)]
        target = upstream or "未填写 baseURL，所有请求都会报错"
        head = f"代理已开启：{url} → {target}（预设：{preset['name']}"
        if injection_filter:
            head += "；正在拦截部分 Codex 注入提示词"
        tail = "".join(f"；{note}" for note in notes if note)
        if own:
            return f"{head}；Codex 当前使用 provider {TOOL_PROVIDER}）{tail}"

        current = self.config.get(table, key)
        if applied and current == applied.get("proxyUrl"):
            original = applied.get("original")
        else:
            original = current
        if not isinstance(original, str) or not original.strip() or points_at_local_port(original, port):
            original = None
        # Record first, then write: a crash in between still leaves enough to undo.
        self._save_applied(
            {"provider": provider, "table": list(table), "key": key, "original": original, "proxyUrl": url}
        )
        if current != url:
            self.config.set_table_value(table, key, url)
        return f"{head}；Codex 的 {'.'.join((*table, key))} 已指向代理）{tail}"

    def _injection_filter(self) -> injections.InjectionFilter | None:
        """What the user switched off in the injection page; None = block nothing."""
        state = self.settings.injections
        if not state["blockAll"] and not state["off"]:
            return None
        try:
            catalog = injections.load_catalog()
        except (OSError, ValueError) as exc:
            # Not blocking what the user switched off must not go unnoticed.
            raise ProxyError(f"无法读取注入提示词结构文件 cct/{injections.CATALOG_FILE.name}：{exc}") from exc
        return injections.build_filter(catalog, state) or None

    def release(self) -> str:
        """Stop listening and undo the tool's changes to config.toml. Keeps the `enabled` flag."""
        note = "；".join(filter(None, [self._restore(), self._remove_catalog_line()]))
        was_running = self.server.running
        self.server.stop()
        self.server.set_route(None)
        self.upstream = ""
        if was_running or note:
            return "代理已关闭" + (f"；{note}" if note else "")
        return ""
