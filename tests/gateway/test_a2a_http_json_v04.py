import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aie_runtime.engine import AuthorityLease, Mission, Principal
from aie_runtime.gateway.a2a_http_json import A2AHTTPJSONForwarder, create_a2a_http_json_server
from aie_runtime.gateway.core import AIEGateway
from aie_runtime.gateway.durable import SQLiteGatewayStore
from aie_runtime.gateway.policy import LocalPolicyAdapter
from aie_runtime.store import InMemoryState

NOW = datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc)


class Upstream(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        return

    def _handle(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        type(self).seen.append((self.command, self.path, {key.lower(): value for key, value in self.headers.items()}, raw))
        payload = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _handle
    do_POST = _handle


def gateway(tmp_path, revoked=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    state = InMemoryState()
    state.principals["p"] = Principal("p", "agent", "spiffe://example.org/a")
    state.missions["m"] = Mission("m", "RUNNING")
    state.leases["l"] = AuthorityLease(
        "l",
        "p",
        "m",
        {"a2a.message.send", "a2a.task.get", "a2a.task.list", "a2a.task.cancel"},
        (
            "a2a://message/",
            "a2a://task",
            "a2a://tenant/acme/message/",
            "a2a://tenant/acme/task",
            "a2a://tenant/tasks/task",
        ),
        NOW + timedelta(hours=1),
        100,
        revoked=revoked,
    )
    return AIEGateway(
        state=state,
        store=SQLiteGatewayStore(tmp_path / "g.db"),
        policy=LocalPolicyAdapter(lambda _: True),
        clock=lambda: NOW,
    )


def serve(tmp_path, revoked=False, tenant=None):
    Upstream.seen = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    gw = gateway(tmp_path, revoked)
    server = create_a2a_http_json_server(
        gw,
        A2AHTTPJSONForwarder(f"http://127.0.0.1:{upstream.server_port}/api"),
        tenant=tenant,
        trust_header_identity=True,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, upstream, gw


def request(server, path, method="GET", body=None):
    headers = {
        "X-AIE-Verified-Spiffe-ID": "spiffe://example.org/a",
        "X-AIE-Identity-Verified": "true",
        "AIE-Mission-Id": "m",
        "AIE-Authority-Lease": "l",
        "A2A-Version": "1.0",
        "A2A-Extensions": "ext",
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_send_replay_tenant_and_header_stripping(tmp_path):
    server, upstream, gw = serve(tmp_path, tenant="acme")
    try:
        body = {"message": {"messageId": "m1"}}
        assert request(server, "/acme/message:send", "POST", body) == 200
        assert request(server, "/acme/message:send", "POST", body) == 409
        assert len(Upstream.seen) == 1
        assert Upstream.seen[0][1] == "/api/acme/message:send"
        assert "aie-mission-id" not in Upstream.seen[0][2]
        assert Upstream.seen[0][2]["a2a-extensions"] == "ext"
        assert gw.store.list_evidence()[-1]["aie.resource"] == "a2a://tenant/acme/message/m1"
    finally:
        server.shutdown()
        upstream.shutdown()


def test_repeatable_get_query_and_canonicalization(tmp_path):
    server, upstream, gw = serve(tmp_path, tenant="acme")
    try:
        path = "/acme/tasks/task%20one?historyLength=2"
        assert request(server, path) == 200
        assert request(server, path) == 200
        assert len(Upstream.seen) == 2
        assert all(value[0] == "GET" and value[1] == "/api/acme/tasks/task%20one?historyLength=2" for value in Upstream.seen)
        assert gw.store.list_evidence()[-1]["aie.resource"] == "a2a://tenant/acme/task/task one"
        before = len(Upstream.seen)
        assert request(server, "/acme/tasks/task%2Fescape") == 400
        assert len(Upstream.seen) == before
    finally:
        server.shutdown()
        upstream.shutdown()


def test_list_cancel_revocation_and_streaming_fail_closed(tmp_path):
    server, upstream, gw = serve(tmp_path)
    try:
        assert request(server, "/tasks") == 200
        assert request(server, "/tasks/t2:cancel", "POST", {}) == 200
        evidence = gw.store.list_evidence()
        assert evidence[-2]["aie.resource"] == "a2a://task"
        assert evidence[-1]["aie.resource"] == "a2a://task/t2"
        before = len(Upstream.seen)
        assert request(server, "/message:stream", "POST", {"message": {"messageId": "x"}}) == 403
        assert len(Upstream.seen) == before
    finally:
        server.shutdown()
        upstream.shutdown()

    server, upstream, _ = serve(tmp_path / "revoked", True)
    try:
        assert request(server, "/message:send", "POST", {"message": {"messageId": "x"}}) == 403
        assert Upstream.seen == []
    finally:
        server.shutdown()
        upstream.shutdown()


def test_configured_tenant_disambiguates_reserved_names_and_rejects_wrong_tenant(tmp_path):
    server, upstream, gw = serve(tmp_path, tenant="tasks")
    try:
        assert request(server, "/tasks/tasks") == 200
        assert gw.store.list_evidence()[-1]["aie.resource"] == "a2a://tenant/tasks/task"
        before = len(Upstream.seen)
        assert request(server, "/evil/tasks") == 400
        assert len(Upstream.seen) == before
    finally:
        server.shutdown()
        upstream.shutdown()
