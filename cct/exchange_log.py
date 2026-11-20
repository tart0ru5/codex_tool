"""On-disk log of what the proxy received from Codex and what came back.

Layout: one directory per proxied request, <root>/<timestamp>/, holding

    request_original.json   what Codex sent to the tool
    request_forwarded.json  what the tool really sent upstream (absent if not forwarded)
    response_raw.json       the reply exactly as received (stream chunks as they came)
    response_merged.json    the same reply with the stream assembled into one object

Only the newest `keep` directories are kept; creating one more removes the oldest.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_LOG_KEEP = 20
MIN_LOG_KEEP, MAX_LOG_KEEP = 1, 100000
KINDS = ("request_original", "request_forwarded", "response_raw", "response_merged")
BODY_KEYS = ("body", "bodyBase64", "bodySize", "bodyNote")
# Only directories with this name shape are listed or deleted, so anything else
# a user puts into the log directory is left alone.
ENTRY_NAME_RE = re.compile(r"\d{8}-\d{6}-\d{3}(?:-\d+)?")


class MissingLog(Exception):
    """This part of an exchange was never stored."""


def _sort_key(name: str) -> tuple[str, int]:
    return name[:19], int(name[20:] or 1)


def _load(path: Path) -> dict[str, Any]:
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError("log file is not a JSON object")
    return record


class ExchangeLog:
    def __init__(self, root: Path, keep: int = DEFAULT_LOG_KEEP):
        self.root = root
        self.keep = keep
        self._lock = threading.Lock()

    def entries(self) -> list[str]:
        """Names of the stored exchanges, newest first."""
        if not self.root.is_dir():
            return []
        names = [p.name for p in self.root.iterdir() if p.is_dir() and ENTRY_NAME_RE.fullmatch(p.name)]
        return sorted(names, key=_sort_key, reverse=True)

    def begin(self) -> tuple[Path, list[str]]:
        """Create the directory for a new exchange and drop the oldest ones.

        Returns the directory and a list of problems met while deleting old
        entries (deleting must not stop the new exchange from being logged).
        """
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            now = time.time()
            base = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"
            name, number = base, 1
            while True:
                try:
                    (self.root / name).mkdir()
                    break
                except FileExistsError:
                    number += 1
                    name = f"{base}-{number}"
            problems = []
            for old in self.entries()[max(self.keep, 1):]:
                try:
                    shutil.rmtree(self.root / old)
                except OSError as exc:
                    problems.append(f"无法删除旧日志 {old}: {exc}")
            return self.root / name, problems

    @staticmethod
    def write(directory: Path, kind: str, record: dict[str, Any]) -> None:
        """Write <kind>.json. Fails if the entry was already pruned."""
        path = directory / f"{kind}.json"
        tmp = directory / f"{kind}.json.tmp"
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def file(self, name: str, kind: str) -> Path:
        return self.root / name / f"{kind}.json"

    def source_file(self, name: str, kind: str) -> Path:
        """The file `read` takes this part from (older logs used request/response.json)."""
        path = self.file(name, kind)
        if path.exists():
            return path
        legacy = self.root / name / f"{kind.split('_')[0]}.json"
        return legacy if legacy.exists() else path

    def read(self, name: str, kind: str) -> dict[str, Any]:
        path = self.source_file(name, kind)
        if not path.exists():
            raise MissingLog(kind)
        record = _load(path)
        if path == self.file(name, kind):
            return record
        # Entry written by an older version of the tool: one file per direction.
        if kind == "request_original":
            record.pop("forwarded", None)
            return record
        if kind == "request_forwarded":
            forwarded = record.get("forwarded")
            if not isinstance(forwarded, dict):
                raise MissingLog(kind)
            result = {key: record.get(key) for key in ("time", "method", "preset")}
            result["url"] = forwarded.get("url")
            result["bodyRewritten"] = bool(forwarded.get("bodyRewritten"))
            result["headers"] = forwarded.get("headers")
            body_source = forwarded if result["bodyRewritten"] else record
            result.update({key: body_source[key] for key in BODY_KEYS if key in body_source})
            return result
        if kind == "response_merged":
            return merge_response(record)
        return record


# ---------------------------------------------------------------- merging


def parse_sse(text: str) -> list[dict[str, Any]]:
    """Server-sent events of `text` as [{"event": name, "data": parsed-or-str}]."""
    events = []
    name, data = "", []

    def flush() -> None:
        nonlocal name, data
        if data:
            joined = "\n".join(data)
            try:
                parsed: Any = json.loads(joined)
            except ValueError:
                parsed = joined
            events.append({"event": name, "data": parsed})
        name, data = "", []

    for line in text.splitlines():
        if not line.strip():
            flush()
        elif line.startswith(":"):
            continue  # comment / keep-alive
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    flush()
    return events


def _assemble(events: list[dict[str, Any]]) -> tuple[Any, str]:
    """One Responses-API response object built from stream events, plus a note."""
    final = started = None
    final_type = ""
    items: dict[Any, Any] = {}
    texts: dict[Any, str] = {}
    for event in events:
        data = event["data"]
        if not isinstance(data, dict):
            continue
        kind = data.get("type") or event["event"]
        response = data.get("response")
        if kind in ("response.completed", "response.incomplete", "response.failed") and isinstance(response, dict):
            final, final_type = response, kind
        elif kind in ("response.created", "response.in_progress") and isinstance(response, dict):
            started = response
        elif kind == "response.output_item.done" and "item" in data:
            items[data.get("output_index", len(items))] = data["item"]
        elif kind == "response.output_text.delta" and isinstance(data.get("delta"), str):
            key = data.get("output_index", 0)
            texts[key] = texts.get(key, "") + data["delta"]
    if final is not None:
        result = copy.deepcopy(final)
        note = f"取自流中的 {final_type} 事件"
        if not result.get("output") and items:
            result["output"] = list(items.values())
            note += "；该事件里没有 output，已用各 response.output_item.done 事件补上"
        return result, note
    if started is not None or items or texts:
        result = copy.deepcopy(started) if started is not None else {}
        result["output"] = list(items.values())
        partial = [text for key, text in texts.items() if key not in items]
        if partial:
            result["partialText"] = partial
        return result, "流没有正常结束（没有 response.completed 事件），以下由已收到的事件拼成，可能不完整"
    return [event["data"] for event in events], "不是 Responses API 的事件流，未能拼成一个对象，按事件逐条列出"


def merge_response(raw: dict[str, Any]) -> dict[str, Any]:
    """The "assembled" form of a raw response record (status, headers etc. are kept)."""
    merged = {key: value for key, value in raw.items() if key not in BODY_KEYS}
    body = raw.get("body")
    events: list[dict[str, Any]] = []
    if isinstance(body, str) and body.lstrip().startswith(("event:", "data:", ":")):
        events = parse_sse(body)
    if not events:
        merged["streamed"] = False
        merged["mergeNote"] = "不是流式回复，内容与原始回复相同"
        merged.update({key: raw[key] for key in BODY_KEYS if key in raw})
        return merged
    counts: dict[str, int] = {}
    for event in events:
        data = event["data"]
        kind = (data.get("type") if isinstance(data, dict) else None) or event["event"] or "(未命名)"
        counts[str(kind)] = counts.get(str(kind), 0) + 1
    merged["streamed"] = True
    merged["eventCount"] = len(events)
    merged["eventTypes"] = counts
    if raw.get("bodyNote"):
        merged["bodyNote"] = raw["bodyNote"]
    merged["body"], merged["mergeNote"] = _assemble(events)
    return merged
