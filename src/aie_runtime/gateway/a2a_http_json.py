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


def _copy_stream_headers(
    handler: BaseHTTPRequestHandler,
    status: int,
    headers: Mapping[str, str],
) -> None:
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
    the socket before declaring the stream delivered. Transport ambiguity after
    dispatch maps conservatively to AIE-UPSTREAM-002.
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
        timeout: float = 5.0,
        ssl_context: ssl.SSLContext | None = None,
        ssl_context_provider: Callable[[], ssl.SSLContext] | None = None,
        expected_peer_spiffe_id: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.ssl_context = ssl_context
        self.ssl_context_provider = ssl_context_provider
        self.expected_peer_spiffe_id = expected_peer_spiffe_id

    def _target(self, path: str) -> str:
        base = urlsplit(self.base_url)
        relative = urlsplit(path)
        return urlunsplit(
            (
                base.scheme,
                base.netloc,
                base.path.rstrip("/") + "/" + relative.path.lstrip("/"),
                relative.query,
                "",
            )
        )

    def bind(
        self,
        *,
        method: str,
        path: str,
        raw_body: bytes,
        stream_handler: BaseHTTPRequestHandler | None = None,
        on_dispatched: Callable[[], None] | None = None,
    ) -> _BoundForwarder:
        return _BoundForwarder(
            self,
            method=method,
            path=path,
            raw_body=raw_body,
            stream_handler=stream_handler,
            on_dispatched=on_dispatched,
        )

    def request(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        raw_body: bytes,
    ) -> UpstreamResponse:
        target = self._target(path)
        out_headers = _forward_headers(headers)
        ssl_context = (
            self.ssl_context_provider()
            if self.ssl_context_provider is not None
            else self.ssl_context
        )
        if self.expected_peer_spiffe_id:
            if ssl_context is None:
                raise UpstreamTransportError("SPIFFE peer verification requires TLS context")
            try:
                status, payload, response_headers = request_bytes_with_peer_identity(
                    method,
                    target,
                    raw_body,
                    out_headers,
                    timeout=self.timeout,
                    ssl_context=ssl_context,
                    expected_peer_spiffe_id=self.expected_peer_spiffe_id,
                )
                return UpstreamResponse(status, payload, response_headers)
            except AIEError as exc:
                if exc.code == "AIE-IDENT-002":
                    raise UpstreamAuthenticationError(str(exc)) from exc
                raise UpstreamTransportError(str(exc)) from exc
            except Exception as exc:
                raise UpstreamTransportError(str(exc)) from exc

        request = urllib.request.Request(
            target,
            data=raw_body or None,
            headers=out_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
                context=ssl_context,
            ) as response:
                return UpstreamResponse(
                    int(response.status),
                    response.read(),
                    dict(response.headers.items()),
                )
        except urllib.error.HTTPError as exc:
            return UpstreamResponse(int(exc.code), exc.read(), dict(exc.headers.items()))
        except Exception as exc:
            raise UpstreamTransportError(str(exc)) from exc

    def stream(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        raw_body: bytes,
        downstream: BaseHTTPRequestHandler,
        bound: _BoundForwarder,
    ) -> UpstreamResponse:
        target = self._target(path)
        parsed = urlsplit(target)
        out_headers = _forward_headers(headers)
        out_headers.setdefault("Accept", "text/event-stream")
        out_headers.setdefault("Cache-Control", "no-store")
        ssl_context = (
            self.ssl_context_provider()
            if self.ssl_context_provider is not None
            else self.ssl_context
        )

        if parsed.scheme == "https":
            if ssl_context is None:
                raise UpstreamTransportError("HTTPS streaming requires TLS context")
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                parsed.hostname,
                parsed.port or 443,
                timeout=self.timeout,
                context=ssl_context,
            )
        elif parsed.scheme == "http":
            if self.expected_peer_spiffe_id is not None:
                raise UpstreamTransportError("SPIFFE peer verification requires HTTPS")
            conn = http.client.HTTPConnection(
                parsed.hostname,
                parsed.port or 80,
                timeout=self.timeout,
            )
        else:
            raise UpstreamTransportError(f"unsupported upstream scheme: {parsed.scheme!r}")

        target_path = parsed.path or "/"
        if parsed.query:
            target_path += "?" + parsed.query
        try:
            conn.connect()
            if self.expected_peer_spiffe_id is not None:
                sock = getattr(conn, "sock", None)
                cert_der = sock.getpeercert(binary_form=True) if sock is not None else None
                actual = validate_x509_svid_der(cert_der or b"", verified=bool(cert_der))
                if actual != self.expected_peer_spiffe_id:
                    raise UpstreamAuthenticationError("AIE-IDENT-002")

            bound.mark_dispatched()
            conn.request(
                method,
                target_path,
                body=raw_body if raw_body else None,
                headers=out_headers,
            )
            response = conn.getresponse()
            response_headers = {key: value for key, value in response.getheaders()}
            content_type = response_headers.get(
                "Content-Type",
                response_headers.get("content-type", ""),
            )
            if "text/event-stream" not in content_type.lower():
                return UpstreamResponse(
                    int(response.status),
                    response.read(),
                    response_headers,
                )

            stream_socket = getattr(conn, "sock", None)
            if stream_socket is not None:
                stream_socket.settimeout(None)

            _copy_stream_headers(downstream, int(response.status), response_headers)
            bound.response_started = True
            while True:
                chunk = response.read1(4096)
                if not chunk:
                    break
                downstream.wfile.write(chunk)
                downstream.wfile.flush()
                if _downstream_peer_closed(downstream):
                    raise UpstreamTransportError(
                        "downstream disconnected after stream dispatch"
                    )
            if _downstream_peer_closed(downstream):
                raise UpstreamTransportError(
                    "downstream disconnected after stream dispatch"
                )
            return UpstreamResponse(int(response.status), b"", response_headers)
        except UpstreamAuthenticationError:
            raise
        except Exception as exc:
            if isinstance(exc, UpstreamTransportError):
                raise
            raise UpstreamTransportError(str(exc)) from exc
        finally:
            conn.close()


class A2AHTTPJSONServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address,
        *,
        gateway: AIEGateway,
        forwarder: A2AHTTPJSONForwarder,
        tenant: str | None = None,
        trust_header_identity: bool = False,
        ssl_context: ssl.SSLContext | None = None,
        tls_context_provider: Any | None = None,
    ):
        super().__init__(address, _Handler)
        self.gateway = gateway
        self.forwarder = forwarder
        self.tenant = tenant
        self.trust_header_identity = trust_header_identity
        self.tls_context_provider = tls_context_provider
        self.tls_enabled = ssl_context is not None or tls_context_provider is not None
        if ssl_context is not None:
            self.socket = ssl_context.wrap_socket(self.socket, server_side=True)

    def get_request(self):
        sock, addr = super().get_request()
        if self.tls_context_provider is not None:
            try:
                sock = self.tls_context_provider.server_context().wrap_socket(
                    sock,
                    server_side=True,
                )
            except Exception:
                sock.close()
                raise
        return sock, addr


class _Handler(BaseHTTPRequestHandler):
    server: A2AHTTPJSONServer

    def log_message(self, *args: Any) -> None:
        return

    def _identity(self) -> TransportIdentity:
        if self.server.tls_enabled:
            try:
                cert_der = self.connection.getpeercert(binary_form=True)
                spiffe_id = validate_x509_svid_der(
                    cert_der or b"",
                    verified=bool(cert_der),
                )
                return TransportIdentity(spiffe_id, True, "spiffe-mtls")
            except Exception:
                return TransportIdentity(None, False, "spiffe-mtls")
        if self.server.trust_header_identity:
            return TransportIdentity(
                self.headers.get("X-AIE-Verified-Spiffe-ID"),
                self.headers.get("X-AIE-Identity-Verified", "").lower() == "true",
                "trusted-header-reference",
            )
        return TransportIdentity(None, False, "http")

    def _send(
        self,
        status: int,
        payload: dict[str, Any] | bytes,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        raw = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, separators=(",", ":")).encode()
        )
        self.send_response(status)
        emitted_content_type = False
        for key, value in (headers or {}).items():
            if key.lower() not in {"content-length", "connection", "transfer-encoding"}:
                self.send_header(key, value)
                emitted_content_type = emitted_content_type or key.lower() == "content-type"
        if not emitted_content_type:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode()) if raw else {}
            if not isinstance(body, dict):
                raise ValueError
            headers = {key: value for key, value in self.headers.items()}
            admission_headers, internal = _admission(
                method,
                self.path,
                headers,
                body,
                self.server.tenant,
            )
        except ProtocolError as exc:
            self._send(400, {"error_code": exc.code})
            return
        except Exception:
            self._send(400, {"error_code": "AIE-PROTO-002"})
            return

        is_stream = internal.get("method") in {"message/stream", "tasks/subscribe"}

        def mark_in_flight() -> None:
            self.server.gateway.store.put_outcome(
                str(internal["id"]),
                status="in-flight",
                protocol="a2a",
                error_code=None,
            )

        bound = self.server.forwarder.bind(
            method=method,
            path=self.path,
            raw_body=raw,
            stream_handler=self if is_stream else None,
            on_dispatched=mark_in_flight if is_stream else None,
        )
        result = self.server.gateway.forward(
            "a2a",
            admission_headers,
            internal,
            self._identity(),
            bound,
        )
        if bound.response_started:
            return
        if result.upstream:
            self._send(
                result.upstream.status,
                result.upstream.body,
                result.upstream.headers,
            )
            return
        decision = result.decision
        status = (
            409
            if decision.status == "prior-outcome"
            else 502
            if decision.status == "uncertain"
            else 403
        )
        self._send(
            status,
            {
                "status": decision.status,
                "action_id": decision.action_id,
                "error_code": decision.error_code,
                "prior": decision.prior,
            },
        )

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


def create_a2a_http_json_server(
    gateway: AIEGateway,
    forwarder: A2AHTTPJSONForwarder,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    tenant: str | None = None,
    trust_header_identity: bool = False,
    ssl_context: ssl.SSLContext | None = None,
    tls_context_provider: Any | None = None,
) -> A2AHTTPJSONServer:
    return A2AHTTPJSONServer(
        (host, port),
        gateway=gateway,
        forwarder=forwarder,
        tenant=tenant,
        trust_header_identity=trust_header_identity,
        ssl_context=ssl_context,
        tls_context_provider=tls_context_provider,
    )
