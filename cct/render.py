"""Turn Codex rollout entries into list labels and readable text."""
from __future__ import annotations

import json
from typing import Any


def json_dump(value: Any, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, indent=indent)


def _payload(obj: dict[str, Any]) -> dict[str, Any]:
    payload = obj.get("payload")
    return payload if isinstance(payload, dict) else {}


def content_text(content: Any) -> str:
    """Flatten a Responses-API style content value into plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else json_dump(content)
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            parts.append(part["text"])
        elif isinstance(part, dict):
            parts.append(f"[{part.get('type', 'part')}]")
        else:
            parts.append(str(part))
    return "\n".join(parts)


def is_tool_record(obj: dict[str, Any]) -> bool:
    """True for tool call / tool output records (function_call, custom_tool_call_output, ...)."""
    if obj.get("type") != "response_item":
        return False
    kind = str(_payload(obj).get("type", ""))
    return kind.endswith("_call") or kind.endswith("_call_output")


def entry_kind(obj: dict[str, Any]) -> str:
    if "_parse_error" in obj:
        return "parse-error"
    top = str(obj.get("type", "?"))
    payload = _payload(obj)
    if top == "response_item":
        kind = str(payload.get("type", "?"))
        if kind == "message":
            return str(payload.get("role", "message"))
        return kind
    if top == "event_msg":
        return f"event:{payload.get('type', '?')}"
    return top


def entry_preview(obj: dict[str, Any], limit: int = 180) -> str:
    payload = _payload(obj)
    top = obj.get("type")
    kind = payload.get("type")
    if "_parse_error" in obj:
        text = f"PARSE ERROR: {obj.get('_parse_error')} {obj.get('_raw', '')}"
    elif top == "response_item" and kind == "message":
        text = content_text(payload.get("content"))
    elif top == "response_item" and kind in ("function_call", "custom_tool_call"):
        text = f"{payload.get('name', '?')}  {payload.get('arguments', payload.get('input', ''))}"
    elif top == "response_item" and str(kind).endswith("_output"):
        text = content_text(payload.get("output"))
    elif top == "response_item" and kind == "reasoning":
        text = content_text(payload.get("summary")) or "(encrypted)"
    elif top == "session_meta":
        text = f"cwd={payload.get('cwd', '')} cli={payload.get('cli_version', '')} provider={payload.get('model_provider', '')}"
    elif top == "turn_context":
        text = f"model={payload.get('model', '')} effort={payload.get('effort', '')} cwd={payload.get('cwd', '')}"
    elif top == "event_msg" and kind == "task_complete":
        text = str(payload.get("last_agent_message", ""))
    elif top == "event_msg" and kind == "item_completed":
        item = payload.get("item")
        text = str(item.get("type", "")) if isinstance(item, dict) else ""
    else:
        text = json_dump({k: v for k, v in payload.items() if k != "type"}) if payload else ""
    text = " ".join(str(text).split())
    return text[:limit] + ("…" if len(text) > limit else "")


def expand_json_strings(value: Any) -> Any:
    """Recursively parse strings that themselves contain a JSON object/array."""
    if isinstance(value, dict):
        return {key: expand_json_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_json_strings(item) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if len(stripped) >= 2 and stripped[0] in "{[" and stripped[-1] in "}]":
            try:
                return expand_json_strings(json.loads(stripped))
            except Exception:
                return value
    return value


def render_segments(index: int, line: int, obj: dict[str, Any]) -> list[tuple[str, str]]:
    """Readable ("rendered") view of one entry, as (kind, text) pieces.

    Joined with newlines the pieces are the rendered text. `kind` only says how the
    view should colour a piece: "meta" (a `name: value` line), "title" (a section
    heading), "json" (indent=2 JSON), "text" (anything else) or "blank".
    """
    payload = _payload(obj)
    top = obj.get("type")
    kind = payload.get("type")
    segments: list[tuple[str, str]] = []

    def meta(text: str) -> None:
        segments.append(("meta", text))

    def section(title: str, body: str, body_kind: str = "text") -> None:
        segments.extend([("blank", ""), ("title", title), (body_kind, body)])

    def pretty_section(title: str, value: Any) -> None:
        expanded = expand_json_strings(value)
        if isinstance(expanded, str):
            section(title, expanded)
        else:
            section(title, json_dump(expanded, indent=2), "json")

    meta(f"Index: {index + 1}")
    meta(f"Line: {line}")
    meta(f"Kind: {entry_kind(obj)}")
    for key in ("timestamp", "ordinal"):
        if key in obj:
            meta(f"{key}: {obj.get(key)}")
    for key in ("id", "role", "name", "namespace", "call_id", "status", "turn_id"):
        if key in payload:
            meta(f"{key}: {payload.get(key)}")

    if "_parse_error" in obj:
        section("Raw line:", str(obj.get("_raw", "")))
    elif top == "response_item" and kind == "message":
        section("Content:", content_text(payload.get("content")))
    elif top == "response_item" and kind in ("function_call", "custom_tool_call"):
        key = "arguments" if "arguments" in payload else "input"
        pretty_section(f"{key.capitalize()}:", payload.get(key, ""))
    elif top == "response_item" and str(kind).endswith("_output"):
        output = payload.get("output")
        pretty_section("Output:", content_text(output) if isinstance(output, list) else output)
    elif top == "response_item" and kind == "reasoning":
        section("Summary:", content_text(payload.get("summary")) or "(none)")
        if payload.get("content"):
            section("Content:", content_text(payload.get("content")))
        if payload.get("encrypted_content"):
            segments.append(("blank", ""))
            meta(f"encrypted_content: {len(str(payload['encrypted_content']))} chars (see raw)")
    else:
        body = payload if payload else {k: v for k, v in obj.items() if k not in ("timestamp", "ordinal")}
        pretty_section("Payload:", body)
    return segments


def render_entry(index: int, line: int, obj: dict[str, Any]) -> str:
    return "\n".join(text for _kind, text in render_segments(index, line, obj))
