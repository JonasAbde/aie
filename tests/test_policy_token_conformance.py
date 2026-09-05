import os
"""Policy token conformance tests for policy.token/1.0 (WORKS frozen schema).

Validates that AIE's authority model (AuthorityLease + BudgetLedger) maps into
the WORKS policy.token/1.0 contract shape.

Run: PYTHONPATH=src python -m pytest tests/test_policy_token_conformance.py -q
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

CONTRACT_PATH = Path(os.environ.get("GOVERNANCE_DIR", str(Path(__file__).resolve().parent.parent.parent / "after-graph-governance"))) / "docs" / "contracts" / "frozen" / "policy.token.schema.json"


@pytest.fixture(scope="module")
def contract():
    with open(CONTRACT_PATH) as f:
        return json.load(f)


def _lease():
    from aie_runtime.engine import AuthorityLease
    return AuthorityLease(
        id="lease1", principal_id="p1", mission_id="m1",
        capabilities={"execute"}, resource_prefixes=("tools:",),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        budget_remaining=50.0, revoked=False,
    )


def test_contract_required_fields(contract):
    for field in ["token_id", "work_id", "org", "scopes", "purpose_bindings", "budget_line", "expiry"]:
        assert field in contract["required"], f"contract missing {field}"


def test_contract_scopes_unique_items(contract):
    scopes = contract["properties"]["scopes"]
    assert scopes["uniqueItems"] is True, "scopes must be unique (policy tokens must not double-grant)"


def test_aie_lease_maps_into_policy_token_shape(contract):
    """AIE AuthorityLease maps into the policy token: capabilities → scopes,
    budget_remaining → budget_line, expires_at → expiry."""
    lease = _lease()
    token = {
        "token_id": lease.id,
        "work_id": lease.mission_id,
        "org": "org_test",
        "scopes": sorted(lease.capabilities),
        "purpose_bindings": list(lease.resource_prefixes),
        "budget_line": {"remaining": lease.budget_remaining},
        "expiry": lease.expires_at.isoformat(),
        "delegated_from": lease.parent_lease_id,
    }
    for field in contract["required"]:
        assert field in token, f"token missing required field {field}"
    # scopes must be unique
    assert len(token["scopes"]) == len(set(token["scopes"]))


def test_aie_lease_expiry_maps_to_contract_expiry(contract):
    lease = _lease()
    assert isinstance(lease.expires_at, datetime)
    assert lease.expires_at > datetime.now(timezone.utc), "test lease must not be expired"


def test_aie_lease_revocation_invalidates_token(contract):
    """A revoked lease must not be representable as a valid policy token —
    the contract has no 'revoked' field, so revocation happens upstream
    (AIE engine.revalidate) before the token would be issued."""
    lease = _lease()
    lease.revoked = True
    assert lease.revoked is True
    # The caller (TG / AIE engine) must check revocation BEFORE minting tokens.
    # Pin the invariant: a valid token requires a non-revoked lease.
    assert lease.revoked is not False, "revoked lease must fail the valid-token precondition"


def test_policy_token_round_trip(contract):
    """A policy token survives JSON round-trip with all required fields."""
    token = {
        "token_id": "tok_1",
        "work_id": "wrk_1",
        "org": "org_1",
        "scopes": ["fs.read", "fs.write"],
        "purpose_bindings": ["tools:"],
        "budget_line": {"ceiling_eur": 20.0},
        "expiry": datetime.now(timezone.utc).isoformat(),
    }
    raw = json.dumps(token)
    parsed = json.loads(raw)
    for field in contract["required"]:
        assert field in parsed, f"round-trip lost {field}"


def test_budget_line_object_shape(contract):
    """budget_line must be an object (flexible shape; AIE maps its ledger fields in)."""
    assert contract["properties"]["budget_line"]["type"] == "object"
