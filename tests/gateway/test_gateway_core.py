import threading
from datetime import datetime, timedelta, timezone

from aie_runtime.engine import AuthorityLease, Mission, Principal
from aie_runtime.gateway.core import AIEGateway
from aie_runtime.gateway.durable import SQLiteGatewayStore
from aie_runtime.gateway.forwarding import UpstreamResponse
from aie_runtime.gateway.identity import TransportIdentity
from aie_runtime.gateway.policy import LocalPolicyAdapter
from aie_runtime.store import InMemoryState

NOW = datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc)


def make_gateway(tmp_path, policy=lambda _: True):
    state = InMemoryState()
    state.principals["agent:refund"] = Principal(
        "agent:refund", "agent", "spiffe://example.org/agent/refund"
    )
    state.missions["mission:refunds"] = Mission("mission:refunds", "RUNNING")
    state.leases["lease:refund"] = AuthorityLease(
        id="lease:refund",
        principal_id="agent:refund",
        mission_id="mission:refunds",
        capabilities={"mcp.tools.call:refund_customer", "a2a.message.send"},
        resource_prefixes=("mcp://tool/refund_customer", "a2a://message/"),
        expires_at=NOW + timedelta(hours=1),
        budget_remaining=10,
    )
    store = SQLiteGatewayStore(tmp_path / "gateway.db")
    return AIEGateway(
        state=state,
        store=store,
        policy=LocalPolicyAdapter(policy),
        clock=lambda: NOW,
    ), store


def mcp_headers():
    return {
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/call",
        "Mcp-Name": "refund_customer",
        "AIE-Mission-Id": "mission:refunds",
        "AIE-Authority-Lease": "lease:refund",
        "AIE-Budget-Cost": "3",
    }


def mcp_body(action_id="mcp-1"):
    return {
        "jsonrpc": "2.0",
        "id": action_id,
        "method": "tools/call",
        "params": {"name": "refund_customer", "arguments": {"amount": 100}},
    }


def a2a_headers():
    return {
        "A2A-Version": "1.0",
        "AIE-Mission-Id": "mission:refunds",
        "AIE-Authority-Lease": "lease:refund",
    }


def a2a_body(action_id="a2a-race"):
    return {
        "jsonrpc": "2.0",
        "id": action_id,
        "method": "message/send",
        "params": {"message": {"messageId": action_id}},
    }


def identity(verified=True):
    return TransportIdentity("spiffe://example.org/agent/refund", verified=verified)


def test_gateway_admits_mcp_and_commits_budget_and_evidence(tmp_path):
    gateway, store = make_gateway(tmp_path)
    decision = gateway.handle("mcp", mcp_headers(), mcp_body(), identity())
    assert decision.status == "admitted"
    assert store.remaining_budget("lease:refund") == 7.0
    assert store.reservation_state("mcp-1") == "committed"
    assert store.get_outcome("mcp-1")["status"] == "admitted"
    evidence = store.list_evidence()
    assert evidence[-1]["aie.decision"] == "admitted"
    assert "arguments" not in repr(evidence[-1])


def test_gateway_replay_returns_prior_without_second_policy_call_or_charge(tmp_path):
    calls = []
    gateway, store = make_gateway(tmp_path, policy=lambda value: calls.append(value) or True)
    first = gateway.handle("mcp", mcp_headers(), mcp_body(), identity())
    second = gateway.handle("mcp", mcp_headers(), mcp_body(), identity())
    assert first.status == "admitted"
    assert second.status == "prior-outcome"
    assert second.error_code == "AIE-REPLAY-001"
    assert second.prior is True
    assert len(calls) == 1
    assert store.remaining_budget("lease:refund") == 7.0


def test_gateway_id_reuse_with_different_content_is_evaluated_fresh(tmp_path):
    # JSON-RPC ids are only unique per client session: same id with different
    # content (method, body, Host/Origin) is a new request, not a replay.
    # Regression: the official conformance CLI reuses small ids across scenarios
    # (dns-rebinding sends id=1 for evil then valid hosts); bare-id dedupe
    # returned prior-outcome (HTTP 409) for the valid request.
    calls = []
    gateway, store = make_gateway(tmp_path, policy=lambda value: calls.append(value) or True)
    headers = mcp_headers()
    first = gateway.handle("mcp", headers, mcp_body("shared-id"), identity())
    assert first.status == "admitted"
    second_body = mcp_body("shared-id")
    second_body["params"] = {"name": "refund_customer", "arguments": {"amount": 1}}
    second = gateway.handle("mcp", headers, second_body, identity())
    assert second.status == "admitted"
    assert len(calls) == 2
    assert store.remaining_budget("lease:refund") == 4.0
    # exact repeat after that is still a replay
    third = gateway.handle("mcp", headers, second_body, identity())
    assert third.status == "prior-outcome"
    assert third.error_code == "AIE-REPLAY-001"
    assert len(calls) == 2

def test_concurrent_reservation_collision_returns_prior_without_overwriting_in_flight(tmp_path):
    first_reserved = threading.Event()
    second_at_reserve = threading.Event()
    allow_second_reserve = threading.Event()
    first_in_flight = threading.Event()
    release_first = threading.Event()

    class BlockingReplayStore(SQLiteGatewayStore):
        def __init__(self, path):
            super().__init__(path)
            self._reserve_calls = 0
            self._reserve_lock = threading.Lock()

        def reserve_budget(self, lease_id, action_id, amount):
            with self._reserve_lock:
                self._reserve_calls += 1
                call = self._reserve_calls
            if call == 1:
                super().reserve_budget(lease_id, action_id, amount)
                first_reserved.set()
                return
            second_at_reserve.set()
            assert allow_second_reserve.wait(timeout=2)
            return super().reserve_budget(lease_id, action_id, amount)

    def policy(_):
        assert second_at_reserve.wait(timeout=2)
        return True

    gateway, _ = make_gateway(tmp_path, policy=policy)
    store = BlockingReplayStore(tmp_path / "race.db")
    for lease in gateway.state.leases.values():
        store.initialize_budget(lease.id, float(lease.budget_remaining))
    gateway.store = store

    class BlockingForwarder:
        def forward(self, *, protocol, headers, body):
            store.put_outcome(
                "a2a-race",
                status="in-flight",
                protocol="a2a",
                error_code=None,
            )
            first_in_flight.set()
            assert release_first.wait(timeout=2)
            return UpstreamResponse(200, b"{}", {})

    forwarder = BlockingForwarder()
    results = {}

    def invoke(name):
        results[name] = gateway.forward(
            "a2a",
            a2a_headers(),
            a2a_body(),
            identity(),
            forwarder,
        )

    first = threading.Thread(target=invoke, args=("first",), daemon=True)
    second = threading.Thread(target=invoke, args=("second",), daemon=True)
    first.start()
    assert first_reserved.wait(timeout=2)
    second.start()
    assert second_at_reserve.wait(timeout=2)
    assert first_in_flight.wait(timeout=2)

    allow_second_reserve.set()
    second.join(timeout=2)
    assert not second.is_alive()
    assert results["second"].decision.status == "prior-outcome"
    assert results["second"].decision.error_code == "AIE-REPLAY-001"
    assert store.get_outcome("a2a-race")["status"] == "in-flight"

    release_first.set()
    first.join(timeout=2)
    assert not first.is_alive()
    assert results["first"].decision.status == "admitted"
    assert store.get_outcome("a2a-race")["status"] == "admitted"


def test_gateway_live_revocation_fails_closed(tmp_path):
    gateway, store = make_gateway(tmp_path)
    store.revoke("lease:refund")
    decision = gateway.handle("mcp", mcp_headers(), mcp_body("mcp-revoked"), identity())
    assert decision.status == "denied"
    assert decision.error_code == "AIE-AUTH-003"
    assert store.remaining_budget("lease:refund") == 10.0


def test_gateway_policy_deny_rolls_back_budget(tmp_path):
    gateway, store = make_gateway(tmp_path, policy=lambda _: False)
    decision = gateway.handle("mcp", mcp_headers(), mcp_body("mcp-deny"), identity())
    assert decision.status == "denied"
    assert decision.error_code == "AIE-POLICY-001"
    assert store.remaining_budget("lease:refund") == 10.0
    assert store.reservation_state("mcp-deny") == "rolled_back"


def test_gateway_policy_backend_failure_is_fail_closed_and_rolls_back(tmp_path):
    def broken(_):
        raise RuntimeError("policy unavailable")

    gateway, store = make_gateway(tmp_path, policy=broken)
    decision = gateway.handle("mcp", mcp_headers(), mcp_body("mcp-error"), identity())
    assert decision.status == "denied"
    assert decision.error_code == "AIE-POLICY-002"
    assert store.remaining_budget("lease:refund") == 10.0


def test_gateway_unverified_identity_fails_closed(tmp_path):
    gateway, store = make_gateway(tmp_path)
    decision = gateway.handle("mcp", mcp_headers(), mcp_body("mcp-id"), identity(False))
    assert decision.status == "denied"
    assert decision.error_code == "AIE-IDENT-001"
    assert store.remaining_budget("lease:refund") == 10.0
