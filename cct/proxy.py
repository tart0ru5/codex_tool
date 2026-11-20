"""Local transparent HTTP proxy that sits between Codex and the model API.

Codex is pointed at http://127.0.0.1:<port>; every request is forwarded to the
route's upstream base URL with the same method, path, query, headers and body.
Only the overrides configured in the active preset are applied on the way.
"""
from __future__ import annotations

import base64
import gzip
import http.client
import json
import threading
import time
import urllib.request
import zlib
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from pathlib import Path
from urllib.parse import urlsplit

from .exchange_log import ExchangeLog, merge_response

LISTEN_HOST = "127.0.0.1"
UPSTREAM_TIMEOUT = 600
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


@dataclass(frozen=True)
class Route:
    """Where to forward to and what to override. Empty value = pass through.

    An empty upstream is not passed through: requests are answered with an error.
    """

    upstream: str
    preset: str = ""
    model: str = ""
    api_key: str = ""
    extra_headers: dict[str, Any] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    reasoning_effort: str = ""
    # Prompt preset: (role, content) entries put before / after what Codex sent.
    prompt_preset: str = ""
    prompt_before: tuple[tuple[str, str], ...] = ()
    prompt_after: tuple[tuple[str, str], ...] = ()
    # injections.InjectionFilter: the Codex-injected prompts to leave out, or None.
    injection_filter: Any = None

    @property
    def rewrites_body(self) -> bool:
        return bool(
            self.model
            or self.extra_body
            or self.reasoning_effort
            or self.prompt_before
            or self.prompt_after
            or self.injection_filter
        )


def deep_merge(target: dict[str, Any], extra: dict[str, Any]) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_merge(target[key], value)
        else:
            target[key] = json.loads(json.dumps(value))


def prompt_item(role: str, content: str, *, chat: bool) -> dict[str, Any]:
    if chat:
        return {"role": role, "content": content}
    part = "output_text" if role == "assistant" else "input_text"
    return {"type": "message", "role": role, "content": [{"type": part, "text": content}]}


def apply_prompts(obj: dict[str, Any], route: Route) -> bool:
    """Put the prompt preset's entries around the conversation Codex sent.

    Returns False when the body has no conversation to put them around.
    """
    chat = "messages" in obj  # Chat Completions wire format; otherwise Responses
    key = "messages" if chat else "input"
    current = obj.get(key)
    if isinstance(current, str):
        current = [prompt_item("user", current, chat=chat)]
    if not isinstance(current, list):
        return False
    before = [prompt_item(role, content, chat=chat) for role, content in route.prompt_before]
    after = [prompt_item(role, content, chat=chat) for role, content in route.prompt_after]
    obj[key] = before + current + after
    return True


def apply_overrides(obj: dict[str, Any], route: Route) -> None:
    # Before the prompt preset is added, so only what Codex sent is filtered.
    if route.injection_filter and "model" in obj:
        route.injection_filter.apply(obj)
    if route.prompt_before or route.prompt_after:
        apply_prompts(obj, route)
    if route.model:
        obj["model"] = route.model
    if route.reasoning_effort:
        if "messages" in obj:  # Chat Completions wire format
            obj["reasoning_effort"] = route.reasoning_effort
        else:  # Responses wire format
            reasoning = obj.get("reasoning")
            reasoning = dict(reasoning) if isinstance(reasoning, dict) else {}
            reasoning["effort"] = route.reasoning_effort
            obj["reasoning"] = reasoning
    if route.extra_body:
        deep_merge(obj, route.extra_body)


def _zstd_decompress(data: bytes) -> bytes:
    try:
        from compression import zstd  # Python 3.14+
    except ImportError:
        import zstandard  # ImportError here is reported by the caller

        return zstandard.ZstdDecompressor().decompressobj().decompress(data)
    return zstd.decompress(data)


def decode_body(body: bytes, content_encoding: str) -> bytes:
    encoding = content_encoding.strip().lower()
    if encoding in ("", "identity"):
        return body
    if encoding == "gzip":
        return gzip.decompress(body)
    if encoding == "deflate":
        return zlib.decompress(body)
    if encoding == "zstd":
        return _zstd_decompress(body)
    raise ValueError(f"unsupported Content-Encoding {content_encoding!r}")


def rewrite_body(body: bytes, content_encoding: str, route: Route) -> bytes | None:
    """Return the overridden (uncompressed) JSON body, or None to forward as is.

    Only model requests - JSON objects that carry a "model" field - are touched.
    Raises on bodies that cannot be decoded.
    """
    if not body or not route.rewrites_body:
        return None
    obj = json.loads(decode_body(body, content_encoding))
    if not isinstance(obj, dict) or "model" not in obj:
        return None
    apply_overrides(obj, route)
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


SECRET_HEADERS = {"authorization", "proxy-authorization", "api-key", "x-api-key", "cookie", "set-cookie"}


def mask_secret(value: str) -> str:
    """Keep the scheme and the last 4 characters so keys do not end up in log files."""
    scheme, space, secret = value.partition(" ")
    if not space:
        scheme, secret = "", value
    tail = secret[-4:] if len(secret) >= 12 else ""
    return f"{scheme}{space}***{tail}"


def loggable_headers(headers: Any) -> list[list[str]]:
    return [[str(k), mask_secret(str(v)) if str(k).lower() in SECRET_HEADERS else str(v)] for k, v in headers]


def loggable_body(body: bytes, content_encoding: str = "") -> dict[str, Any]:
    """Body as JSON if it is JSON, else text, else base64. Compression is undone."""
    fields: dict[str, Any] = {"bodySize": len(body)}
    data = body
    if body and content_encoding.strip().lower() not in ("", "identity"):
        try:
            data = decode_body(body, content_encoding)
            fields["bodyNote"] = f"已解压 Content-Encoding: {content_encoding.strip()}"
        except Exception as exc:
            fields["bodyNote"] = f"无法解压 Content-Encoding: {content_encoding.strip()}（{type(exc).__name__}: {exc}）"
            fields["bodyBase64"] = base64.b64encode(body).decode("ascii")
            return fields
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        fields["bodyBase64"] = base64.b64encode(data).decode("ascii")
        return fields
    try:
        fields["body"] = json.loads(text) if text[:1] in ("{", "[") else text
    except ValueError:
        fields["body"] = text
    return fields


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def is_loopback(host: str) -> bool:
    return host in ("localhost", "::1") or host.startswith("127.")


def system_proxy_for(scheme: str, host: str) -> tuple[str, int, str | None] | None:
    """(host, port, basic-auth) of the system HTTP proxy for this target, if any.

    Honours HTTP(S)_PROXY / NO_PROXY and the Windows system proxy setting, so
    the hop through this tool does not change how the upstream is reached.
    """
    if is_loopback(host):
        return None
    try:
        if urllib.request.proxy_bypass(host):
            return None
        proxy = urllib.request.getproxies().get(scheme)
    except Exception:
        return None
    if not proxy:
        return None
    parts = urlsplit(proxy if "://" in proxy else "http://" + proxy)
    if parts.scheme != "http" or not parts.hostname:
        return None  # only plain HTTP proxies are supported; otherwise connect directly
    auth = None
    if parts.username:
        auth = base64.b64encode(f"{parts.username}:{parts.password or ''}".encode()).decode()
    return parts.hostname, parts.port or 8080, auth


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "_Server"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass  # request logging is done explicitly in _handle

    def _read_body(self) -> bytes:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            chunks = []
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline().strip():
                        pass  # trailers
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _send_error_json(self, status: int, message: str) -> None:
        payload = json.dumps(
            {"error": {"message": message, "type": "codex_tool_proxy_error"}}, ensure_ascii=False
        ).encode("utf-8")
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        self._log_response(
            status, "", [("Content-Type", "application/json; charset=utf-8")], payload, "", source="tool", error=message
        )

    # Exchange logging. A logging failure is reported in the proxy log and
    # never affects the request being proxied.
    _log_dir: Path | None = None
    _started = 0.0

    def _log_request(self, body: bytes) -> None:
        """Store what Codex sent, untouched (request_original)."""
        owner = self.server.owner
        self._log_dir = None
        if owner.exchange_log is None:
            return
        try:
            self._log_dir, problems = owner.exchange_log.begin()
            for problem in problems:
                owner.log(problem)
            route = owner.route
            record = {
                "time": now_text(),
                "method": self.command,
                "path": self.path,
                "preset": route.preset if route else "",
                "headers": loggable_headers(self.headers.items()),
                **loggable_body(body, self.headers.get("Content-Encoding", "")),
            }
            ExchangeLog.write(self._log_dir, "request_original", record)
        except Exception as exc:
            self._log_dir = None
            owner.log(f"无法写入请求日志: {type(exc).__name__}: {exc}")

    def _log_forwarded(
        self,
        route: Route,
        url: str,
        headers: list[tuple[str, str]],
        body: bytes,
        content_encoding: str,
        rewritten: bool,
        notes: list[str],
    ) -> None:
        """Store the request as it really goes upstream (request_forwarded)."""
        if self._log_dir is None:
            return
        record: dict[str, Any] = {
            "time": now_text(),
            "method": self.command,
            "url": url,
            "preset": route.preset,
            "promptPreset": route.prompt_preset,
            "bodyRewritten": rewritten,
            "notes": list(notes),
            "headers": loggable_headers(headers),
            **loggable_body(body, content_encoding),
        }
        try:
            ExchangeLog.write(self._log_dir, "request_forwarded", record)
        except Exception as exc:
            self.server.owner.log(f"无法写入请求日志: {type(exc).__name__}: {exc}")

    def _log_response(
        self,
        status: int,
        reason: str,
        headers: Any,
        body: bytes,
        content_encoding: str,
        *,
        source: str,
        error: str = "",
    ) -> None:
        """Store the reply twice: as received (response_raw) and assembled (response_merged)."""
        if self._log_dir is None:
            return
        record: dict[str, Any] = {
            "time": now_text(),
            "source": source,  # "upstream" or "tool" (the proxy answered by itself)
            "status": status,
            "reason": reason,
            "elapsedSeconds": round(time.monotonic() - self._started, 3),
        }
        if error:
            record["error"] = error
        record["headers"] = loggable_headers(headers)
        record.update(loggable_body(body, content_encoding))
        try:
            ExchangeLog.write(self._log_dir, "response_raw", record)
        except Exception as exc:
            self.server.owner.log(f"无法写入回复日志: {type(exc).__name__}: {exc}")
        try:
            ExchangeLog.write(self._log_dir, "response_merged", merge_response(record))
        except Exception as exc:
            self.server.owner.log(f"无法写入整理后的回复日志: {type(exc).__name__}: {exc}")

    def _handle(self) -> None:
        owner = self.server.owner
        started = self._started = time.monotonic()
        self._log_dir = None  # the handler is reused on keep-alive
        try:
            body = self._read_body()
        except Exception as exc:
            self.close_connection = True
            owner.log(f"{self.command} {self.path} -> 无法读取请求体: {exc}")
            return
        self._log_request(body)

        route = owner.route
        if route is None:
            self._send_error_json(503, "codex_tool proxy has no upstream configured")
            owner.log(f"{self.command} {self.path} -> 503 未配置上游")
            return
        if not route.upstream:
            self._send_error_json(
                502,
                f"codex_tool proxy: preset {route.preset!r} has no baseURL. "
                "Fill in baseURL in the proxy configuration dialog (代理配置).",
            )
            owner.log(f"{self.command} {self.path} -> 502 预设“{route.preset}”未填写 baseURL，未转发")
            return
        if "websocket" in self.headers.get("Upgrade", "").lower():
            # WebSocket transport is not proxied; answer 426 so the client uses HTTP.
            self._send_error_json(426, "codex_tool proxy does not support WebSocket; use HTTP")
            owner.log(f"{self.command} {self.path} -> 426 不代理 WebSocket")
            return

        upstream = urlsplit(route.upstream)
        host = upstream.hostname or ""
        if upstream.scheme not in ("http", "https") or not host:
            self._send_error_json(502, f"invalid upstream base URL: {route.upstream!r}")
            owner.log(f"{self.command} {self.path} -> 502 上游地址无效: {route.upstream}")
            return
        port = upstream.port or (443 if upstream.scheme == "https" else 80)
        if is_loopback(host) and port == owner.port:
            self._send_error_json(508, "upstream base URL points back at the codex_tool proxy itself")
            owner.log(f"{self.command} {self.path} -> 508 上游地址指向代理自身")
            return
        netloc = upstream.netloc.rpartition("@")[2]
        target = upstream.path.rstrip("/") + self.path
        shown = f"{upstream.scheme}://{netloc}{target.split('?')[0]}"

        dropped = HOP_BY_HOP | {"host", "content-length"}
        dropped |= {token.strip().lower() for token in self.headers.get("Connection", "").split(",")}
        headers = [(key, value) for key, value in self.headers.items() if key.lower() not in dropped]

        def set_header(name: str, value: str | None) -> None:
            headers[:] = [(k, v) for k, v in headers if k.lower() != name.lower()]
            if value is not None:
                headers.append((name, value))

        notes = []
        try:
            new_body = rewrite_body(body, self.headers.get("Content-Encoding", ""), route)
        except Exception as exc:
            new_body = None
            notes.append(f"预设的请求体覆盖未生效，已原样转发（{type(exc).__name__}: {exc}）")
        if new_body is not None:
            body = new_body
            set_header("Content-Encoding", None)
            notes.append("已改写请求体")
        if route.api_key:
            set_header("Authorization", f"Bearer {route.api_key}")
        for name, value in route.extra_headers.items():
            set_header(str(name), None if value is None else str(value))
        sent_headers = [("Host", netloc), *headers]
        if body or self.command in ("POST", "PUT", "PATCH"):
            sent_headers.append(("Content-Length", str(len(body))))
        self._log_forwarded(
            route,
            f"{upstream.scheme}://{netloc}{target}",
            sent_headers,
            body,
            "" if new_body is not None else self.headers.get("Content-Encoding", ""),
            new_body is not None,
            notes,
        )

        conn: http.client.HTTPConnection | None = None
        try:
            proxy = system_proxy_for(upstream.scheme, host)
            if upstream.scheme == "https":
                if proxy:
                    conn = http.client.HTTPSConnection(proxy[0], proxy[1], timeout=UPSTREAM_TIMEOUT)
                    conn.set_tunnel(host, port, {"Proxy-Authorization": f"Basic {proxy[2]}"} if proxy[2] else {})
                else:
                    conn = http.client.HTTPSConnection(host, port, timeout=UPSTREAM_TIMEOUT)
            elif proxy:
                conn = http.client.HTTPConnection(proxy[0], proxy[1], timeout=UPSTREAM_TIMEOUT)
                target = f"http://{netloc}{target}"
                if proxy[2]:
                    headers.append(("Proxy-Authorization", f"Basic {proxy[2]}"))
            else:
                conn = http.client.HTTPConnection(host, port, timeout=UPSTREAM_TIMEOUT)
            conn.putrequest(self.command, target, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", netloc)
            for key, value in headers:
                conn.putheader(key, value)
            if body or self.command in ("POST", "PUT", "PATCH"):
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body or None)
            response = conn.getresponse()
        except Exception as exc:
            if conn is not None:
                conn.close()
            self._send_error_json(502, f"codex_tool proxy could not reach {shown}: {exc}")
            owner.log(f"{self.command} {self.path} -> 502 无法连接上游 {shown}: {type(exc).__name__}: {exc}")
            return

        received: list[bytes] = []
        response_headers = response.getheaders()
        response_encoding = response.getheader("Content-Encoding") or ""
        try:
            self.send_response_only(response.status, response.reason)
            has_length = False
            for key, value in response.getheaders():
                lower = key.lower()
                if lower in HOP_BY_HOP:
                    continue
                has_length = has_length or lower == "content-length"
                self.send_header(key, value)
            no_body = self.command == "HEAD" or response.status in (204, 304) or response.status < 200
            chunked = not no_body and not has_length
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if not no_body:
                while True:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    received.append(chunk)
                    if chunked:
                        self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n")
                    else:
                        self.wfile.write(chunk)
                    self.wfile.flush()
                if chunked:
                    self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            suffix = f"（{'；'.join(notes)}）" if notes else ""
            owner.log(
                f"{self.command} {self.path.split('?')[0]} -> {response.status} {shown} "
                f"{time.monotonic() - started:.1f}s{suffix}"
            )
            self._log_response(
                response.status, response.reason, response_headers, b"".join(received), response_encoding,
                source="upstream",
            )
        except Exception as exc:
            self.close_connection = True
            owner.log(f"{self.command} {self.path} -> 转发响应中断: {type(exc).__name__}: {exc}")
            self._log_response(
                response.status, response.reason, response_headers, b"".join(received), response_encoding,
                source="upstream", error=f"转发响应中断，内容不完整: {type(exc).__name__}: {exc}",
            )
        finally:
            conn.close()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR would let us bind a port another program is
    # already listening on, hiding the conflict. Never set it.
    allow_reuse_address = False
    owner: "ProxyServer"


class ProxyServer:
    def __init__(self, exchange_log: ExchangeLog | None = None) -> None:
        self.route: Route | None = None
        self.exchange_log = exchange_log  # None = do not store exchanges on disk
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._log: deque[str] = deque(maxlen=200)

    @property
    def port(self) -> int | None:
        return self._server.server_address[1] if self._server else None

    @property
    def running(self) -> bool:
        return self._server is not None

    def log(self, message: str) -> None:
        self._log.append(f"{time.strftime('%H:%M:%S')}  {message}")

    def log_lines(self) -> list[str]:
        return list(self._log)

    def set_route(self, route: Route | None) -> None:
        self.route = route  # read once per request; a plain attribute swap is atomic

    def start(self, port: int) -> None:
        """Listen on 127.0.0.1:port. Raises OSError if the port is unavailable."""
        if self._server is not None:
            if self.port == port:
                return
            self.stop()
        server = _Server((LISTEN_HOST, port), _Handler)
        server.owner = self
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="cct-proxy", daemon=True)
        self._thread.start()
        self.log(f"开始监听 {LISTEN_HOST}:{port}")

    def stop(self) -> None:
        server, thread = self._server, self._thread
        if server is None:
            return
        port = self.port
        self._server = self._thread = None
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        self.log(f"停止监听 {LISTEN_HOST}:{port}")
