from __future__ import annotations

import http.client
import json
import select
import socket
import ssl
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlsplit, urlunsplit

from aie_runtime.errors import AIEError

from .core import AIEGateway
from .forwarding import UpstreamAuthenticationError, UpstreamResponse, UpstreamTransportError
from .identity import TransportIdentity, validate_x509_svid_der
from .model import ProtocolError
from .spiffe_http import request_bytes_with_peer_identity


def _canonical_segment(value: str) -> str:
    decoded = unquote(value)
    if not decoded or any(ch in decoded for ch in ("/", "\\", "\x00")):
        raise ProtocolError("AIE-PROTO-002", "invalid encoded A2A path identifier")
    return decoded


def _admission(
    method: str,
    raw_path: str,
    headers: Mapping[str, str],
    body: Mapping[str, Any],
    configured_tenant: str | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    parsed = urlsplit(raw_path)
    segments = [segment for segment in parsed.path.split("/") if segment]
    tenant = configured_tenant
    if tenant is not None:
        if not segments or _canonical_segment(segments[0]) != tenant:
            raise ProtocolError(
                "AIE-PROTO-002",
                "HTTP+JSON tenant path does not match configured AgentInterface tenant",
            )
        segments.pop(0)
    path = "/" + "/".join(segments)
    out_headers = dict(headers)
    if tenant:
        out_headers["AIE-A2A-Tenant"] = tenant

    if method == "POST" and path in {"/message:send", "/message:stream"}:
        message = body.get("message") if isinstance(body.get("message"), Mapping) else {}
        message_id = str(message.get("messageId") or "")
        if not message_id:
            raise ProtocolError("AIE-PROTO-002", "message.messageId is required")
        operation = "message/stream" if path.endswith(":stream") else "message/send"
        internal = {
            "jsonrpc": "2.0",
            "id": message_id,
            "method": operation,
            "params": dict(body),
        }
    elif method == "GET" and path == "/tasks":
        internal = {
            "jsonrpc": "2.0",
            "id": "read-" + uuid.uuid4().hex,
            "method": "tasks/list",
            "params": {},
        }
    elif method == "GET" and len(segments) == 2 and segments[0] == "tasks":
        task_id = _canonical_segment(segments[1])
        internal = {
            "jsonrpc": "2.0",
            "id": "read-" + uuid.uuid4().hex,
            "method": "tasks/get",
            "params": {"id": task_id},
        }
    elif (
        method == "POST"
        and len(segments) == 2
        and segments[0] == "tasks"
        and segments[1].endswith(":cancel")
    ):
        task_id = _canonical_segment(segments[1][:-7])
        prefix = f"{tenant}:" if tenant else ""
        internal = {
            "jsonrpc": "2.0",
            "id": f"cancel:{prefix}{task_id}",
            "method": "tasks/cancel",
            "params": {"id": task_id},
        }
    elif (
        method == "POST"
        and len(segments) == 2
        and segments[0] == "tasks"
        and segments[1].endswith(":subscribe")
    ):
        task_id = _canonical_segment(segments[1][:-10])
        internal = {
            "jsonrpc": "2.0",
            "id": "subscribe-" + uuid.uuid4().hex,
            "method": "tasks/subscribe",
            "params": {"id": task_id},
        }
    else:
        raise ProtocolError(
            "AIE-PROTO-001",
            f"unsupported A2A HTTP+JSON operation: {method} {path}",
        )
    return out_headers, internal


def _forward_headers(headers: Mapping[str, str]) -> dict[str, str]:
    hop = {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
    }
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in hop
        and not (
            key.lower() == "aie"
            or key.lower().startswith("aie-")
            or key.lower().startswith("x-aie-")
        )
    }


def _copy_stream_headers(handler: BaseHTTPRequestHandler, status: int, headers: Mapping[str, str]) -> None:
    handler.send_response(status)
    emitted_content_type = False
    for key, value in headers.items():
        if key.lower() in {"content-length", "connection", "transfer-encoding"}:
            continue
        handler.send_header(key, value)
        emitted_content_type = emitted_content_type or key.lower() == "content-type"
    if not emitted_content_type:
        handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True


def _downstream_peer_closed(handler: BaseHTTPRequestHandler) -> bool:
    """Best-effort detection of a peer that closed after streaming dispatch.

    A successful socket write only proves that the local kernel accepted bytes. A FIN
    from the client may already be queued for reading, so check readability and peek
    the socket before declaring the stream delivered. This is deliberately
    conservative: transport ambiguity after dispatch maps to AIE-UPSTREAM-002.
    """
    connection = handler.connection
    if getattr(connection, "_closed", False):
        return True
    try:
        readable, _, _ = select.select([connection], [], [], 0)
    except (OSError, ValueError):
        return True
    if not readable:
        return False
    try:
        if isinstance(connection, ssl.SSLSocket):
            if connection.pending() > 0:
                return False
            return connection.recv(1) == b""
        return connection.recv(1, socket.MSG_PEEK) == b""
    except (BlockingIOError, ssl.SSLWantReadError):
        return False
    except (ConnectionResetError, BrokenPipeError, OSError, ssl.SSLError):
        return True


class _BoundForwarder:
    def __init__(
        self,
        parent: "A2AHTTPJSONForwarder",
        *,
        method: str,
        path: str,
        raw_body: bytes,
        stream_handler: BaseHTTPRequestHandler | None,
        on_dispatched: Callable[[], None] | None = None,
    ):
        self.parent = parent
        self.method = method
        self.path = path
        self.raw_body = raw_body
        self.stream_handler = stream_handler
        self.on_dispatched = on_dispatched
        self.response_started = False
        self.dispatched = False


    def mark_dispatched(self) -> None:
        if self.dispatched:
            return
        self.dispatched = True
        if self.on_dispatched is not None:
            self.on_dispatched()

    def forward(
        self,
        *,
        protocol: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any],
    ) -> UpstreamResponse:
        if self.stream_handler is None:
            return self.parent.request(self.method, self.path, headers, self.raw_body)
        return self.parent.stream(
            self.method,
            self.path,
            headers,
            self.raw_body,
            self.stream_handler,
            self,
        )


class A2AHTTPJSONForwarder:
    def __init__(
        self,
        base_url: str,
        *,
        timeout