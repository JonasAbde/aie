from pathlib import Path

import pytest

from aie_runtime.errors import AIEError
from aie_runtime.gateway.durable import SQLiteGatewayStore


def make_store(tmp_path: Path) -> SQLiteGatewayStore:
    return SQLiteGatewayStore(tmp_path / "gateway.db")


def test_outcome_persists_across_store_instances(tmp_path):
    store = make_store(tmp_path)
    store.put_outcome("action-1", status="admitted", protocol="mcp", error_code=None)
    reopened = make_store(tmp_path)
    assert reopened.get_outcome("action-1") == {
        "action_id": "action-1",
        "status": "admitted",
        "protocol": "mcp",
        "error_code": None,
        "fingerprint": None,
    }


def test_revocation_persists(tmp_path):
    store = make_store(tmp_path)
    store.revoke("lease-1")
    reopened = make_store(tmp_path)
    assert reopened.is_revoked("lease-1") is True
    assert reopened.is_revoked("lease-2") is False


def test_budget_reservation_commit_conserves_budget(tmp_path):
    store = make_store(tmp_path)
    store.initialize_budget("lease-1", 10.0)
    store.reserve_budget("lease-1", "action-1", 3.0)
    assert store.remaining_budget("lease-1") == 7.0
    store.commit_budget("action-1")
    assert store.remaining_budget("lease-1") == 7.0
    assert store.reservation_state("action-1") == "committed"


def test_budget_reservation_rollback_restores_budget(tmp_path):
    store = make_store(tmp_path)
    store.initialize_budget("lease-1", 10.0)
    store.reserve_budget("lease-1", "action-1", 3.0)
    store.rollback_budget("action-1")
    assert store.remaining_budget("lease-1") == 10.0
    assert store.reservation_state("action-1") == "rolled_back"


def test_budget_reservation_fails_when_insufficient(tmp_path):
    store = make_store(tmp_path)
    store.initialize_budget("lease-1", 2.0)
    with pytest.raises(AIEError) as exc:
        store.reserve_budget("lease-1", "action-1", 3.0)
    assert exc.value.code == "AIE-BUDGET-001"
    assert store.remaining_budget("lease-1") == 2.0


def test_evidence_persists_and_is_ordered(tmp_path):
    store = make_store(tmp_path)
    store.append_evidence({"event_type": "first", "aie.action.id": "a"})
    store.append_evidence({"event_type": "second", "aie.action.id": "b"})
    events = make_store(tmp_path).list_evidence()
    assert [event["event_type"] for event in events] == ["first", "second"]


def test_outcome_can_transition_from_in_flight_to_terminal(tmp_path):
    store = make_store(tmp_path)
    store.put_outcome("stream-1", status="in-flight", protocol="a2a", error_code=None)
    store.put_outcome("stream-1", status="admitted", protocol="a2a", error_code=None)
    assert store.get_outcome("stream-1")["status"] == "admitted"
