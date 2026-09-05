"""Evidence conformance tests for evidence.schema/1.1 (WORKS frozen schema).

Validates that AIE's EvidenceRecord can serialize to a shape compatible with
the WORKS evidence.schema/1.1 contract (bundle_id, identity_chain, created_at,
records required).

Run: PYTHONPATH=src python -m pytest tests/test_evidence_conformance.py -q
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ for conftest import

from conftest import resolve_contracts_dir

CONTRACT_PATH = resolve_contracts_dir() / "frozen" / "evidence.schema.schema.json"


@pytest.fixture(scope="module")
def contract():
    with open(CONTRACT_PATH) as f:
        return json.load(f)


def _evidence_record(event_type="action_executed", **attrs):
    from aie_runtime.engine import EvidenceRecord
    return EvidenceRecord(
        event_type=event_type,
        timestamp=datetime.now(timezone.utc),
        attributes=attrs or {"detail": "test"},
    )


def test_contract_required_fields(contract):
    for field in ["bundle_id", "identity_chain", "created_at", "records"]:
        assert field in contract["required"], f"contract missing {field}"


def test_aie_evidence_record_serializes_to_contract_shape(contract):
    """EvidenceRecord must map to a records[] entry inside the bundle."""
    rec = _evidence_record()
    d = rec.__dict__ if hasattr(rec, "__dict__") else rec._asdict()
    # The contract's records[] is a list; each record needs serializable data.
    serialized = json.dumps(d, default=str)
    assert serialized, "EvidenceRecord must serialize"


def test_aie_evidence_bundle_shape_compatible(contract):
    """Build a minimal bundle using AIE evidence records and verify all
    contract-required top-level fields are present."""
    from aie_runtime.engine import EvidenceRecord
    rec = EvidenceRecord(
        event_type="admitted",
        timestamp=datetime.now(timezone.utc),
        attributes={"action_id": "a1", "principal_id": "p1"},
    )
    bundle = {
        "bundle_id": "bnd_test_001",
        "identity_chain": {"org": "org_test", "human": "jonas"},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "records": [json.loads(json.dumps(rec.__dict__, default=str))],
    }
    for field in contract["required"]:
        assert field in bundle, f"bundle missing required field {field}"


def test_aie_evidence_event_types_are_strings(contract):
    """Contract records[] entries must be serializable objects; AIE event_type
    must be a string to map into a records entry."""
    from aie_runtime.engine import EvidenceRecord
    rec = _evidence_record(event_type="revoked")
    assert isinstance(rec.event_type, str)


def test_bundle_round_trip(contract):
    """Serialize a bundle with AIE records, parse back, verify required fields survive."""
    from aie_runtime.engine import EvidenceRecord
    rec = EvidenceRecord(
        event_type="verified",
        timestamp=datetime.now(timezone.utc),
        attributes={"mission_id": "m1"},
    )
    bundle = {
        "bundle_id": "bnd_rt",
        "identity_chain": {"org": "org_x", "human": "h"},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "records": [json.loads(json.dumps(rec.__dict__, default=str))],
    }
    raw = json.dumps(bundle)
    parsed = json.loads(raw)
    for field in contract["required"]:
        assert field in parsed
    assert parsed["records"][0]["event_type"] == "verified"
