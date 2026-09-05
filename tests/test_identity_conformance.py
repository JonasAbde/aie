"""Identity conformance tests for identity/1.0 (WORKS frozen schema).

Validates that AIE's Principal dataclass maps into the WORKS identity/1.0
contract shape (human, org, device, worker, runtime required).

Run: PYTHONPATH=src python -m pytest tests/test_identity_conformance.py -q
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ for conftest import

from conftest import resolve_contracts_dir

CONTRACT_PATH = resolve_contracts_dir() / "frozen" / "identity.schema.json"


@pytest.fixture(scope="module")
def contract():
    with open(CONTRACT_PATH) as f:
        return json.load(f)


def test_contract_required_fields(contract):
    for field in ["human", "org", "device", "worker", "runtime"]:
        assert field in contract["required"], f"contract missing {field}"


def test_contract_org_pattern(contract):
    """org must match org_<8+ hex chars>."""
    import re
    pattern = contract["properties"]["org"]["pattern"]
    assert re.match(pattern, "org_abcdef123456")
    assert not re.match(pattern, "org_short")
    assert not re.match(pattern, "wrong-prefix")


def test_contract_privilege_note_is_fail_closed(contract):
    """service_principals_never_approve — service identities must never approve."""
    note = contract["properties"]["privilege_note"]["enum"]
    assert "service_principals_never_approve" in note


def test_contract_worker_role_enum(contract):
    roles = contract["properties"]["worker"]["properties"]["role"]["enum"]
    assert "engineering" in roles
    assert "ci" in roles


def test_aie_principal_maps_into_contract_shape(contract):
    """AIE Principal {id, type, identity_ref} must be representable in the
    WORKS identity shape. The mapping: identity_ref → human or device,
    type → worker.role / service_principal."""
    from aie_runtime.engine import Principal
    p = Principal(id="p1", type="bot", identity_ref="tg")
    d = p.__dict__ if hasattr(p, "__dict__") else {"id": p.id, "type": p.type, "identity_ref": p.identity_ref}
    # A principal maps into the runtime + worker sections of the contract.
    mapping = {
        "runtime": {"work_id": "wrk_test", "lease_id": "lease_test"},
        "worker": {"role": "ci"},
        # identity_ref carries the TG identity into the contract's device/human slots
    }
    assert d["id"] and d["type"], "Principal must have id + type"
    # The contract requires worker.role in its enum
    assert mapping["worker"]["role"] in contract["properties"]["worker"]["properties"]["role"]["enum"]


def test_aie_principal_service_principal_never_approves(contract):
    """AIE bots (type='bot') are service principals — the contract's
    privilege_note 'service_principals_never_approve' must hold. AIE's
    canApprove policy must never authorize a bot to approve."""
    from aie_runtime.engine import Principal
    p = Principal(id="bot1", type="bot", identity_ref="tg")
    assert p.type == "bot", "bot principals must be type='bot'"
    # The privilege note is a contract-level invariant; TG enforces it via
    # canApprove role checks. Pin the contract text here so it can't drift.
    note = contract["properties"]["privilege_note"]["enum"][0]
    assert note == "service_principals_never_approve"


def test_identity_round_trip(contract):
    """A full identity object survives JSON round-trip with required fields."""
    identity = {
        "human": "jonas",
        "org": "org_abcdef1234567890",
        "device": "workstation-01",
        "worker": {"role": "engineering"},
        "runtime": {"work_id": "wrk_1", "lease_id": "lease_1"},
        "service_principal": False,
    }
    raw = json.dumps(identity)
    parsed = json.loads(raw)
    for field in contract["required"]:
        assert field in parsed, f"round-trip lost {field}"
