import http.client
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aie_runtime.engine import AuthorityLease, Mission, Principal
from aie_runtime.gateway.a2a_http_json import A2AHTTPJSONForwarder, create_a2a_http_json_server
from aie_runtime.gateway.core import AIEGateway
from aie_runtime.gateway.durable import SQLiteGatewayStore
from aie_runtime.gateway.policy import LocalPolicyAdapter
from aie_runtime.store import InMemoryState

NOW = datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc)


class SSEUpstream(BaseHTTPRequestHandler):
    first_sent = threading.Event()
    release = threading.Event()
    calls = []

    def log_message(self, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        type(self).calls.append((self.path, body))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b'data: {"phase":"first"}\n\n')
        self.wfile.flush()
        type(self).first_sent.set()
        type(self).release.wait(timeout=3)
        try:
            self.wfile.write(b'data: {"phase":"second"}\n\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def make_gateway(tmp_path):
    state = InMemoryState()
    state.principals["p"] = Principal("p", "agent", "spiffe://example.org/a")
    state.missions["m"] = Mission("m", "active")
    state.leases["l"] = AuthorityLease(
        "l",
        "p",
        "m",
        {"a2a.message.stream", "a2a.task.subscribe"},
        ("a2a://message/", "a2a://task/"),
        NOW + timedelta(hours=1),
        100,
    )
    return AIEGateway(
        state=state,
        store=SQLiteGatewayStore(tmp_path / "stream.db"),
        policy=LocalPolicyAdapter(lambda _: True),
        clock=lambda: NOW,
    )


def start(tmp_path, *, forwarder_timeout=5.0):
    SSEUpstream.first_sent = threading.Event()
    SSEUpstream.release = threading.Event()
    SSEUpstream.calls = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), SSEUpstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    gateway = make_gateway(tmp_path)
    server = create_a2a_http_json_server(
        gateway,
        A2AHTTPJSONForwarder(f"http://127.0.0.1:{upstream.server_port}", timeout=forwarder_timeout),
        trust_header_identity=True,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, upstream, gateway


def headers():
    return {
        "X-AIE-Verified-Spiffe-ID": "spiffe://example.org/a",
        "X-AIE-Identity-Verified": "true",
        "AIE-Mission-Id": "m",
        "AIE-Authority-Lease": "l",
        "A2A-Version": "1.0",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Cache-Control": "no-store",
    }


def wait_outcome(gateway, action_id, timeout=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = gateway.store.get_outcome(action_id)
        if value is not None:
            return value
        time.sleep(0.01)
    return None


def wait_terminal_outcome(gateway, action_id, timeout=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = gateway.store.get_outcome(action_id)
        if value is not None and value.get("status") != "in-flight":
            return value
        time.sleep(0.01)
    return gateway.store.get_outcome(action_id)


def test_message_stream_forwards_first_event_before_terminal_outcome(tmp_path):
    server, upstream, gateway = start(tmp_path)
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        body = json.dumps({"message": {"messageId": "stream-1"}}).encode()
        conn.request("POST", "/message:stream", body=body, headers=headers())
        response = conn.getresponse()
        assert response.status == 200
        assert "text/event-stream" in response.getheader("Content-Type")
        assert response.readline() == b'data: {"phase":"first"}\n'
        assert response.readline() == b"\n"
        assert SSEUpstream.first_sent.is_set()
        in_flight = gateway.store.get_outcome("stream-1")
        assert in_flight is not None
        assert in_flight["status"] == "in-flight"

        SSEUpstream.release.set()
        rest = response.read()
        assert b'"phase":"second"' in rest
        outcome = wait_outcome(gateway, "stream-1")
        assert outcome is not None
        assert outcome["status"] == "admitted"
    finally:
        SSEUpstream.release.set()
        conn.close()
        server.shutdown()
        upstream.shutdown()


def test_task_subscribe_is_repeatable_and_not_replay_blocked(tmp_path):
    server, upstream, gateway = start(tmp_path)
    try:
        for _ in range(2):
            SSEUpstream.first_sent.clear()
            SSEUpstream.release.set()
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            conn.request("POST", "/tasks/t1:subscribe", body=b"{}", headers=headers())
            response = conn.getresponse()
            assert response.status == 200
            assert b'"phase":"first"' in response.read()
            conn.close()
        assert len(SSEUpstream.calls) == 2
        evidence = gateway.store.list_evidence()
        subscribe = [event for event in evidence if event.get("aie.capability") == "a2a.task.subscribe"]
        assert len(subscribe) == 2
        assert all(event["aie.resource"] == "a2a://task/t1" for event in subscribe)
        assert subscribe[0]["aie.action.id"] != subscribe[1]["aie.action.id"]
    finally:
        SSEUpstream.release.set()
        server.shutdown()
        upstream.shutdown()


def test_client_disconnect_after_dispatch_becomes_uncertain(tmp_path):
    server, upstream, gateway = start(tmp_path)
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    body = json.dumps({"message": {"messageId": "stream-drop"}}).encode()
    try:
        conn.request("POST", "/message:stream", body=body, headers=headers())
        response = conn.getresponse()
        assert response.readline().startswith(b"data:")
        response.close()
        conn.close()
        SSEUpstream.release.set()
        outcome = wait_terminal_outcome(gateway, "stream-drop", timeout=3)
        assert outcome is not None
        assert outcome["status"] == "uncertain"
        assert outcome["error_code"] == "AIE-UPSTREAM-002"
    finally:
        SSEUpstream.release.set()
        try:
            conn.close()
        except Exception:
            pass
        server.shutdown()
        upstream.shutdown()


def test_duplicate_message_stream_is_replay_blocked_while_first_stream_is_active(tmp_path):
    server, upstream, gateway = start(tmp_path)
    first = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    second = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    body = json.dumps({"message": {"messageId": "stream-dup"}}).encode()
    try:
        first.request("POST", "/message:stream", body=body, headers=headers())
        first_response = first.getresponse()
        assert first_response.status == 200
        assert first_response.readline().startswith(b"data:")
        assert first_response.readline() == b"\n"
        in_flight = wait_outcome(gateway, "stream-dup")
        assert in_flight is not None and in_flight["status"] == "in-flight"

        second.request("POST", "/message:stream", body=body, headers=headers())
        second_response = second.getresponse()
        assert second_response.status == 409
        replay = json.loads(second_response.read())
        assert replay["status"] == "prior-outcome"
        assert replay["error_code"] == "AIE-REPLAY-001"
        assert len(SSEUpstream.calls) == 1

        SSEUpstream.release.set()
        assert b'"phase":"second"' in first_response.read()
        terminal = wait_terminal_outcome(gateway, "stream-dup")
        assert terminal is not None and terminal["status"] == "admitted"
    finally:
        SSEUpstream.release.set()
        first.close()
        second.close()
        server.shutdown()
        upstream.shutdown()


def test_idle_sse_stream_is_not_cut_off_by_connect_timeout(tmp_path):
    server, upstream, gateway = start(tmp_path, forwarder_timeout=0.05)
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    body = json.dumps({"message": {"messageId": "stream-idle"}}).encode()
    try:
        conn.request("POST", "/message:stream", body=body, headers=headers())
        response = conn.getresponse()
        assert response.status == 200
        assert response.readline().startswith(b"data:")
        assert response.readline() == b"\n"
        time.sleep(0.15)
        SSEUpstream.release.set()
        rest = response.read()
        assert b'"phase":"second"' in rest
        terminal = wait_terminal_outcome(gateway, "stream-idle")
        assert terminal is not None and terminal["status"] == "admitted"
    finally:
        SSEUpstream.release.set()
        conn.close()
        server.shutdown()
        upstream.shutdown()


def test_streaming_wrong_upstream_spiffe_id_denies_before_http_dispatch(tmp_path):
    import json as _json
    import ssl
    import urllib.error
    import urllib.request

    from aie_runtime.gateway.tls import build_client_ssl_context, build_server_ssl_context
    from tls_material import issue_test_pki

    pki = issue_test_pki(tmp_path / "pki-outbound")
    SSEUpstream.first_sent = threading.Event()
    SSEUpstream.release = threading.Event()
    SSEUpstream.calls = []

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), SSEUpstream)
    upstream_ctx = build_server_ssl_context(
        certfile=pki["gw_b_crt"],
        keyfile=pki["gw_b_key"],
        cafile=pki["ca"],
        require_client_cert=True,
    )
    upstream.socket = upstream_ctx.wrap_socket(upstream.socket, server_side=True)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    gateway = make_gateway(tmp_path)
    outbound_ctx = build_client_ssl_context(
        certfile=pki["gw_a_crt"],
        keyfile=pki["gw_a_key"],
        cafile=pki["ca"],
    )
    server = create_a2a_http_json_server(
        gateway,
        A2AHTTPJSONForwarder(
            f"https://127.0.0.1:{upstream.server_port}",
            ssl_context=outbound_ctx,
            expected_peer_spiffe_id="spiffe://example.org/gateway/not-b",
        ),
        trust_header_identity=True,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        body = _json.dumps({"message": {"messageId": "stream-peer-deny"}}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/message:stream",
            data=body,
            headers=headers(),
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
            payload = _json.loads(exc.read())
            assert payload["error_code"] == "AIE-UPSTREAM-001"
        else:
            raise AssertionError("wrong upstream SPIFFE ID must fail closed")

        assert SSEUpstream.calls == []
        assert gateway.store.remaining_budget("l") == 100.0
        assert gateway.store.reservation_state("stream-peer-deny") == "rolled_back"
        outcome = gateway.store.get_outcome("stream-peer-deny")
        assert outcome is not None and outcome["status"] == "denied"
    finally:
        SSEUpstream.release.set()
        server.shutdown()
        upstream.shutdown()
