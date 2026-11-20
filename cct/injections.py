"""Prompts Codex injects into its requests: catalog, scanner and filter.

The catalog (`injections.json`, next to this module) is a tree of the injected
pieces that were recognised in the proxy logs and in Codex's models cache:
tool definitions, developer messages, the context put in front of the user's
message, and so on. Every node says how to find its piece in a request
(`match`), so that the proxy can leave out the pieces the user switched off.

match.where is one of
    group             no piece of its own, only groups its children
    tools_field       the request's top-level "tools" list
    additional_tools  "additional_tools" items in the conversation
    tool              one tool (by name, or by type when it has no name) in the
                      list of its parent (tools field, additional tools, namespace)
    body_key          a top-level field of the request body
    item_type         every conversation item of one type, e.g. "reasoning":
                      the model's earlier reasoning that Codex sends back
    text              one text part of a message: by role and leading <tag>,
                      or by the text it starts with
    section           a markdown section of its text, by heading line
    line              a list line ("- name: ...") of its text, by prefix
    xml               an <element> of its text, by tag name
"""
from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

CATALOG_FILE = Path(__file__).with_name("injections.json")
CATALOG_VERSION = 1

PREVIEW_CHARS = 200
PREFIX_CHARS = 48

TAG_RE = re.compile(r"\s*<([A-Za-z_][\w\- ]{0,60})>")
HEADING_RE = re.compile(r"(#{1,6}) \S")
CLOSING_TAG_RE = re.compile(r"</[^<>]+>")
LIST_KEY_RE = re.compile(r"- (.{1,60}?)(:| \(| = )")
XML_CHILD_RE = re.compile(r"(?m)^[ \t]+<([A-Za-z_][\w\-]*)[ >]")
ONLY_WRAPPER_RE = re.compile(r"\s*<([^<>]+)>\s*</\1>\s*")

# Untagged developer messages that are known Codex base instructions.
KNOWN_PREFIXES = [
    ("You are Codex, an agent based on", "Codex 基础指令（You are Codex, an agent based on …）"),
    ("You are a coding agent running in the Codex CLI", "Codex CLI 基础指令（You are a coding agent …）"),
]
TAG_TITLES = {
    "app-context": "桌面应用上下文",
    "skills_instructions": "技能说明与技能列表",
    "permissions instructions": "权限 / 沙箱说明",
    "collaboration_mode": "协作模式",
    "recommended_plugins": "推荐插件列表",
    "codex_apps_client_time_context": "客户端时间",
    "multi_agent_role": "多智能体角色",
    "multi_agent_mode": "多智能体模式",
    "tools": "延迟加载的工具组清单",
    "environment_context": "环境信息（插在用户消息之前）",
    "context_window_reminder": "上下文窗口提醒",
}
# Text pieces whose list lines / child elements are worth listing one by one.
LINE_TAGS = {"skills_instructions", "recommended_plugins", "tools"}
XML_TAGS = {"environment_context"}


# ------------------------------------------------------------------ catalog


def empty_catalog() -> dict[str, Any]:
    return {"version": CATALOG_VERSION, "generated": "", "sources": [], "items": []}


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    path = path or CATALOG_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("injections.json 的结构不对：缺少 items 列表")
    return data


def save_catalog(catalog: dict[str, Any], path: Path | None = None) -> None:
    path = path or CATALOG_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def iter_nodes(nodes: Iterable[dict[str, Any]]):
    for node in nodes:
        yield node
        yield from iter_nodes(node.get("children") or [])


def normalize_state(raw: Any) -> dict[str, Any]:
    """Saved switches: block everything, plus the ids of the unchecked nodes."""
    raw = raw if isinstance(raw, dict) else {}
    off = raw.get("off")
    return {
        "blockAll": raw.get("blockAll") is True,
        "off": sorted({item for item in off if isinstance(item, str)}) if isinstance(off, list) else [],
    }


# ------------------------------------------------------------------- filter


def tool_key(tool: dict[str, Any]) -> str:
    name = tool.get("name")
    return name if isinstance(name, str) and name else f"type:{tool.get('type')}"


def strip_tag(text: str) -> tuple[str, str]:
    """(leading <tag> name or "", the text after it)."""
    found = TAG_RE.match(text)
    return (found.group(1), text[found.end():].lstrip()) if found else ("", text.lstrip())


def is_wrapped(text: str, tag: str) -> bool:
    """The whole text is one <tag>...</tag> element."""
    return text.rstrip().endswith(f"</{tag}>")


def text_matches(match: dict[str, Any], role: str, text: str) -> bool:
    if match.get("role") and match["role"] != role:
        return False
    tag, rest = strip_tag(text)
    if match.get("tag"):
        # What the user typed may start with "<tag>" too: it must be the whole text.
        return tag == match["tag"] and (role != "user" or is_wrapped(text, tag))
    prefix = match.get("prefix")
    return bool(prefix) and (text.lstrip().startswith(prefix) or rest.startswith(prefix))


def _scan_lines(text: str) -> list[tuple[str, bool]]:
    """(line with its line ending, inside a ``` fence)."""
    result = []
    fenced = False
    for line in text.splitlines(keepends=True):
        marker = line.lstrip().startswith("```")
        result.append((line, fenced or marker))
        if marker:
            fenced = not fenced
    return result


def heading_level(line: str) -> int:
    found = HEADING_RE.match(line)
    return len(found.group(1)) if found else 0


def _last_closing_line(lines: list[tuple[str, bool]]) -> int:
    """Index of a wrapper's closing tag when it is the last non-empty line, else -1."""
    for index in range(len(lines) - 1, -1, -1):
        stripped = lines[index][0].strip()
        if stripped:
            return index if CLOSING_TAG_RE.fullmatch(stripped) else -1
    return -1


def section_span(lines: list[tuple[str, bool]], heading: str) -> tuple[int, int] | None:
    level = heading_level(heading)
    closing = _last_closing_line(lines)
    for start, (line, fenced) in enumerate(lines):
        if fenced or line.strip() != heading:
            continue
        end = start + 1
        while end < len(lines):
            other, other_fenced = lines[end]
            if end == closing or (not other_fenced and 0 < heading_level(other.strip()) <= level):
                break
            end += 1
        return start, end
    return None


def remove_section(text: str, heading: str) -> str | None:
    lines = _scan_lines(text)
    span = section_span(lines, heading)
    if span is None:
        return None
    return "".join(line for line, _ in lines[: span[0]] + lines[span[1]:])


def remove_line(text: str, prefix: str) -> str | None:
    lines = text.splitlines(keepends=True)
    for start, line in enumerate(lines):
        if not line.lstrip().startswith(prefix):
            continue
        end = start + 1
        while end < len(lines):  # continuation lines of the same list item
            stripped = lines[end].lstrip()
            if not stripped.strip() or stripped.startswith(("- ", "#", "<")):
                break
            end += 1
        return "".join(lines[:start] + lines[end:])
    return None


def remove_xml(text: str, tag: str) -> str | None:
    name = re.escape(tag)
    pattern = re.compile(rf"[ \t]*<{name}(?:\s[^>]*)?>.*?</{name}>[ \t]*\r?\n?", re.DOTALL)
    result, count = pattern.subn("", text, count=1)
    return result if count else None


TEXT_OPS = {
    "section": lambda text, match: remove_section(text, match["heading"]),
    "line": lambda text, match: remove_line(text, match["prefix"]),
    "xml": lambda text, match: remove_xml(text, match["tag"]),
}


class InjectionFilter:
    """What to leave out of a request. Built by build_filter()."""

    def __init__(self) -> None:
        self.drop_tools = False
        self.tools: set[tuple[str, ...]] = set()
        self.drop_additional = False
        self.additional: set[tuple[str, ...]] = set()
        self.body_keys: set[str] = set()
        self.item_types: set[str] = set()
        # (text match, None = drop the whole text | match of the part to cut out)
        self.texts: list[tuple[dict[str, Any], dict[str, Any] | None]] = []

    def __bool__(self) -> bool:
        return bool(
            self.drop_tools or self.tools or self.drop_additional or self.additional or self.body_keys or self.texts
            or self.item_types
        )

    # -- tools

    def _filter_tools(self, tools: list[Any], rules: set[tuple[str, ...]], prefix: tuple[str, ...]):
        kept, removed = [], 0
        for tool in tools:
            if not isinstance(tool, dict):
                kept.append(tool)
                continue
            path = prefix + (tool_key(tool),)
            if path in rules:
                removed += 1
                continue
            if isinstance(tool.get("tools"), list):
                inner, inner_removed = self._filter_tools(tool["tools"], rules, path)
                if inner_removed:
                    removed += inner_removed
                    if not inner:
                        continue  # a tool group with nothing left in it
                    tool = dict(tool, tools=inner)
            kept.append(tool)
        return kept, removed

    # -- texts

    def _filter_text(self, role: str, text: str) -> tuple[str | None, int]:
        """(None = drop it | the text to send, number of pieces removed)."""
        removed = 0
        for match, part in self.texts:
            if not text_matches(match, role, text):
                continue
            if part is None:
                return None, 1
            result = TEXT_OPS[part["where"]](text, part)
            if result is not None:
                text = result
                removed += 1
        if removed and (not text.strip() or ONLY_WRAPPER_RE.fullmatch(text)):
            return None, removed
        return text, removed

    def _filter_message(self, item: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
        role = item.get("role") if isinstance(item.get("role"), str) else ""
        content = item.get("content")
        if isinstance(content, str):
            text, removed = self._filter_text(role, content)
            if text is None:
                return None, removed
            return (dict(item, content=text) if removed else item), removed
        if not isinstance(content, list):
            return item, 0
        parts, removed = [], 0
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text, count = self._filter_text(role, part["text"])
                removed += count
                if text is None:
                    continue
                if count:
                    part = dict(part, text=text)
            parts.append(part)
        if not removed:
            return item, 0
        return (dict(item, content=parts) if parts else None), removed

    # -- whole body

    def apply(self, obj: dict[str, Any]) -> int:
        """Remove the blocked pieces from a request body. Returns how many were removed."""
        removed = 0
        for key in self.body_keys:
            if key in obj:
                del obj[key]
                removed += 1

        tools = obj.get("tools")
        if isinstance(tools, list) and tools:
            if self.drop_tools:
                kept, count = [], len(tools)
            else:
                kept, count = self._filter_tools(tools, self.tools, ())
            if count:
                removed += count
                if kept:
                    obj["tools"] = kept
                else:  # these fields are only valid together with tools
                    for key in ("tools", "tool_choice", "parallel_tool_calls"):
                        obj.pop(key, None)

        key = "messages" if isinstance(obj.get("messages"), list) else "input"
        items = obj.get(key)
        if isinstance(items, list):
            kept_items = []
            for item in items:
                if isinstance(item, dict) and item.get("type") in self.item_types:
                    removed += 1
                    continue
                if isinstance(item, dict) and item.get("type") == "additional_tools":
                    extra = item.get("tools")
                    if self.drop_additional:
                        removed += 1
                        continue
                    if isinstance(extra, list) and self.additional:
                        inner, count = self._filter_tools(extra, self.additional, ())
                        if count:
                            removed += count
                            if not inner:
                                continue
                            item = dict(item, tools=inner)
                elif isinstance(item, dict) and item.get("type") in (None, "message") and self.texts:
                    item, count = self._filter_message(item)
                    removed += count
                    if item is None:
                        continue
                kept_items.append(item)
            if removed:
                obj[key] = kept_items
        return removed


def build_filter(catalog: dict[str, Any], state: dict[str, Any]) -> InjectionFilter:
    """A node is blocked when it, or anything above it, is switched off."""
    state = normalize_state(state)
    off = set(state["off"])
    block_all = state["blockAll"]
    result = InjectionFilter()

    def context_for(node: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        match = node.get("match") or {}
        where = match.get("where")
        if where == "tools_field":
            return dict(context, tools=("tools", ()))
        if where == "additional_tools":
            return dict(context, tools=("additional", ()))
        if where == "tool" and context.get("tools"):
            kind, path = context["tools"]
            return dict(context, tools=(kind, path + (tool_key(match),)))
        if where == "text":
            return dict(context, text=match)
        return context

    def block(node: dict[str, Any], context: dict[str, Any]) -> None:
        match = node.get("match") or {}
        where = match.get("where")
        inner = context_for(node, context)
        if where == "tools_field":
            result.drop_tools = True
        elif where == "additional_tools":
            result.drop_additional = True
        elif where == "tool" and inner.get("tools"):
            kind, path = inner["tools"]
            (result.tools if kind == "tools" else result.additional).add(path)
        elif where == "body_key" and isinstance(match.get("key"), str):
            result.body_keys.add(match["key"])
        elif where == "item_type" and isinstance(match.get("type"), str):
            result.item_types.add(match["type"])
        elif where == "text":
            result.texts.append((match, None))
        elif where in TEXT_OPS and context.get("text"):
            result.texts.append((context["text"], match))
        else:  # a group: block everything in it
            for child in node.get("children") or []:
                block(child, inner)

    def walk(node: dict[str, Any], context: dict[str, Any]) -> None:
        if block_all or node.get("id") in off:
            block(node, context)
            return
        inner = context_for(node, context)
        for child in node.get("children") or []:
            walk(child, inner)

    for node in catalog.get("items") or []:
        if isinstance(node, dict):
            walk(node, {})
    # Whole-text rules first, so a text that goes entirely is not edited before.
    result.texts.sort(key=lambda rule: rule[1] is not None)
    return result


# ------------------------------------------------------------------ scanner


def preview(text: str) -> str:
    flat = " ".join(text.split())
    return flat[:PREVIEW_CHARS] + ("…" if len(flat) > PREVIEW_CHARS else "")


def new_node(node_id: str, title: str, match: dict[str, Any], note: str = "", text: str = "", chars: int = 0):
    return {
        "id": node_id,
        "title": title,
        "match": match,
        "note": note,
        "preview": preview(text),
        "chars": chars or len(text),
        "children": [],
    }


def merge_node(siblings: list[dict[str, Any]], node: dict[str, Any]) -> dict[str, Any]:
    """Add node to siblings, or fold it into the node that has the same id."""
    for existing in siblings:
        if existing["id"] == node["id"]:
            for key in ("preview", "chars"):
                if node.get(key):
                    existing[key] = node[key]
            if node.get("note") and "未在日志中见过" not in node["note"]:
                existing["note"] = node["note"]
            for child in node.get("children") or []:
                merge_node(existing.setdefault("children", []), child)
            return existing
    siblings.append(node)
    return node


def tool_node(tool: dict[str, Any], parent_id: str) -> dict[str, Any]:
    key = tool_key(tool)
    kind = str(tool.get("type") or "")
    match: dict[str, Any] = {"where": "tool"}
    if isinstance(tool.get("name"), str) and tool["name"]:
        match["name"] = tool["name"]
    else:
        match["type"] = tool.get("type")
    inner = tool.get("tools") if isinstance(tool.get("tools"), list) else []
    note = f"工具组，{len(inner)} 个工具" if inner else f"工具（{kind}）"
    description = tool.get("description") if isinstance(tool.get("description"), str) else ""
    node = new_node(f"{parent_id}/{key}", key, match, note, description, len(json.dumps(tool, ensure_ascii=False)))
    for child in inner:
        if isinstance(child, dict):
            merge_node(node["children"], tool_node(child, node["id"]))
    return node


def classify_text(role: str, text: str) -> tuple[str, str, dict[str, Any]] | None:
    """(id, title, match) of an injected text, or None when it is not recognised."""
    tag, rest = strip_tag(text)
    match: dict[str, Any] = {"where": "text"}
    if role:
        match["role"] = role
    if tag:
        if role == "user" and not is_wrapped(text, tag):
            return None  # something the user typed that merely starts with "<"
        match["tag"] = tag
        title = f"<{tag}>" + (f" {TAG_TITLES[tag]}" if tag in TAG_TITLES else "")
        return f"text/tag:{tag}", title, match
    for prefix, title in KNOWN_PREFIXES:
        if rest.startswith(prefix):
            match["prefix"] = prefix
            return f"text/prefix:{prefix}", title, match
    if role == "user" or role == "assistant":
        return None  # the conversation itself
    first_line = rest.splitlines()[0] if rest else ""
    prefix = first_line.split("{", 1)[0][:PREFIX_CHARS].rstrip()
    if len(prefix) < 12:
        return None
    match["prefix"] = prefix
    return f"text/prefix:{prefix}", prefix + "…", match


def text_node(role: str, text: str) -> dict[str, Any] | None:
    found = classify_text(role, text)
    if found is None:
        return None
    node_id, title, match = found
    node = new_node(node_id, title, match, f"{role or '任意'} 消息", strip_tag(text)[1], len(text))
    tag = match.get("tag", "")
    lines = _scan_lines(text)

    # Markdown sections, nested by heading level.
    stack: list[tuple[int, dict[str, Any], int]] = []  # (level, node, end line)
    owners: list[tuple[int, int, dict[str, Any]]] = []  # (start, end, node) for list lines
    for index, (line, fenced) in enumerate(lines):
        heading = line.strip()
        level = 0 if fenced else heading_level(heading)
        if not level:
            continue
        span = section_span(lines[index:], heading)
        end = index + (span[1] if span else 1)
        body = "".join(item for item, _ in lines[index + 1:end])
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1] if stack else node
        section = new_node(
            f"{parent['id']}/§{heading}",
            heading,
            {"where": "section", "heading": heading},
            "段落",
            body,
            len(body) + len(line),
        )
        section = merge_node(parent["children"], section)
        stack.append((level, section, end))
        owners.append((index, end, section))

    if tag in LINE_TAGS:
        for index, (line, fenced) in enumerate(lines):
            found_key = None if fenced else LIST_KEY_RE.match(line)
            if not found_key:
                continue
            prefix = found_key.group(0)
            parent = node
            for start, end, section in owners:  # innermost section holding the line
                if start < index < end:
                    parent = section
            merge_node(
                parent["children"],
                new_node(
                    f"{parent['id']}/-{prefix}",
                    found_key.group(1),
                    {"where": "line", "prefix": prefix},
                    "列表项",
                    line[len(prefix):],
                    len(line),
                ),
            )
    if tag in XML_TAGS:
        for name in dict.fromkeys(XML_CHILD_RE.findall(text)):
            inner = re.search(rf"<{re.escape(name)}(?:\s[^>]*)?>(.*?)</{re.escape(name)}>", text, re.DOTALL)
            if not inner:
                continue
            merge_node(
                node["children"],
                new_node(
                    f"{node_id}/<{name}>",
                    f"<{name}>",
                    {"where": "xml", "tag": name},
                    "字段",
                    inner.group(1),
                    len(inner.group(0)),
                ),
            )
    return node


def reasoning_text(item: dict[str, Any]) -> str:
    """The readable text of a reasoning item (its content and summary parts)."""
    parts = []
    for key in ("content", "summary"):
        for part in item.get(key) if isinstance(item.get(key), list) else []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return "\n".join(parts)


def reasoning_node() -> dict[str, Any]:
    return new_node(
        "reasoning",
        "历史对话中的 AI 推理内容（对话里的 reasoning 条目）",
        {"where": "item_type", "type": "reasoning"},
        "所有 reasoning 条目",
    )


def request_bodies(log_dir: Path) -> list[dict[str, Any]]:
    """Bodies of the model requests Codex sent, oldest first, from the tool's logs."""
    bodies = []
    if not log_dir.is_dir():
        return bodies
    names = ("request_original.json", "request.json")  # current and old log layout
    files = sorted(path for path in log_dir.rglob("request*.json") if path.name in names)
    for path in files:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        body = record.get("body") if isinstance(record, dict) else None
        if isinstance(body, dict) and "model" in body:
            bodies.append(body)
    return bodies


def message_texts(body: dict[str, Any]):
    """(role, text) of every message text in a request body."""
    items = body.get("messages") if isinstance(body.get("messages"), list) else body.get("input")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("type") not in (None, "message"):
            continue
        role = item.get("role") if isinstance(item.get("role"), str) else ""
        content = item.get("content")
        if isinstance(content, str):
            yield role, content
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                yield role, part["text"]


def cache_templates(codex_home: Path):
    """(file name, model, dotted key, text) of the prompt templates in Codex's models cache."""
    for path in sorted(codex_home.glob("models_cache*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        models = data.get("models") if isinstance(data, dict) else None
        for model in models if isinstance(models, list) else []:
            if not isinstance(model, dict):
                continue

            def walk(value: Any, key: str):
                if isinstance(value, dict):
                    for name, inner in value.items():
                        yield from walk(inner, f"{key}.{name}" if key else str(name))
                elif isinstance(value, str) and value.strip():
                    yield key, value

            for key, text in walk(model.get("model_messages"), ""):
                yield path.name, str(model.get("slug") or ""), key, text
            if isinstance(model.get("base_instructions"), str) and model["base_instructions"].strip():
                yield path.name, str(model.get("slug") or ""), "base_instructions", model["base_instructions"]


def scan(log_dir: Path, codex_home: Path, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the catalog from the proxy logs and Codex's models cache.

    Nodes of `previous` that were not seen this time are kept: the logs only
    hold the most recent requests.
    """
    tools = new_node("tools", "工具定义（请求体的 tools 字段）", {"where": "tools_field"}, "整个字段")
    additional = new_node(
        "additional_tools", "追加的工具（对话里的 additional_tools 条目）", {"where": "additional_tools"}, "整个条目"
    )
    instructions = new_node(
        "body/instructions", "instructions 字段（请求体顶层的系统指令）", {"where": "body_key", "key": "instructions"}, "整个字段"
    )
    developer = new_node("developer", "developer 消息（Codex 插入的指令）", {"where": "group"}, "分组")
    user = new_node("user", "插在用户消息之前的内容（user 角色）", {"where": "group"}, "分组")
    cache = new_node("cache", "models_cache 里的其他提示词模板", {"where": "group"}, "分组，未在日志中见过")
    reasoning = reasoning_node()
    groups = [tools, additional, instructions, developer, user, reasoning, cache]

    bodies = request_bodies(log_dir)
    seen_texts: list[str] = []
    for body in bodies:
        if isinstance(body.get("instructions"), str) and body["instructions"]:
            instructions["preview"] = preview(body["instructions"])
            instructions["chars"] = len(body["instructions"])
        for tool in body.get("tools") if isinstance(body.get("tools"), list) else []:
            if isinstance(tool, dict):
                merge_node(tools["children"], tool_node(tool, "tools"))
        items = body.get("input") if isinstance(body.get("input"), list) else []
        for item in items:
            if isinstance(item, dict) and item.get("type") == "additional_tools":
                for tool in item.get("tools") if isinstance(item.get("tools"), list) else []:
                    if isinstance(tool, dict):
                        merge_node(additional["children"], tool_node(tool, "additional_tools"))
            if isinstance(item, dict) and item.get("type") == "reasoning":
                text = reasoning_text(item)
                reasoning["preview"] = preview(text)
                reasoning["chars"] = len(text) or len(json.dumps(item, ensure_ascii=False))
        for role, text in message_texts(body):
            seen_texts.append(text)
            node = text_node(role, text)
            if node is not None:
                merge_node((user if role == "user" else developer)["children"], node)

    if previous:
        for old in copy.deepcopy(previous.get("items") or []):  # never edit the caller's catalog
            if isinstance(old, dict) and isinstance(old.get("id"), str):
                current = next((group for group in groups if group["id"] == old["id"]), None)
                if current is None:
                    groups.append(old)
                    continue
                for child in old.get("children") or []:
                    _keep_missing(current["children"], child)

    cache_files: list[str] = []
    for file_name, model, key, text in cache_templates(codex_home):
        if file_name not in cache_files:
            cache_files.append(file_name)
        node = text_node("", text)
        if node is None:
            continue
        known = next((item for item in developer["children"] + user["children"] if item["id"] == node["id"]), None)
        if known is not None:  # the same piece as one from the logs: only add its sections
            for child in node["children"]:
                merge_node(known["children"], child)
            continue
        probe = strip_tag(text)[1][:PREFIX_CHARS].split("{", 1)[0]
        if len(probe) >= 12 and any(probe in seen for seen in seen_texts):
            continue  # arrives wrapped inside a piece that is already listed
        node["title"] = f"{key}：{node['title']}"
        node["note"] = f"模板（{model}），未在日志中见过"
        merge_node(cache["children"], node)
    listed = {item["id"] for item in developer["children"] + user["children"]}
    cache["children"] = [item for item in cache["children"] if item["id"] not in listed]

    return {
        "version": CATALOG_VERSION,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sources": [f"代理日志中的 {len(bodies)} 个模型请求"] + cache_files,
        "items": groups,
    }


def _keep_missing(siblings: list[dict[str, Any]], old: dict[str, Any]) -> None:
    for existing in siblings:
        if existing.get("id") == old.get("id"):
            for child in old.get("children") or []:
                _keep_missing(existing.setdefault("children", []), child)
            return
    siblings.append(old)
