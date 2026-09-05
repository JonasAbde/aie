"""Conformance tests for mission-state/1.0 contract (AIE side).

Validates that the AIE Mission dataclass conforms to
after-graph-governance/docs/contracts/mission-state/1.0.json.
The ISR FSM conformance tests live in the ISR repo.
"""
import json
import sys
import os
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

CONTRACT_PATH = Path(os.environ.get("GOVERNANCE_DIR", str(Path(__file__).resolve().parent.parent.parent / "after-graph-governance"))) / "docs" / "contracts" / "mission-state" / "1.0.json"


@pytest.fixture(scope="module")
def contract():
    with open(CONTRACT_PATH) as f:
        return json.load(f)


def test_contract_exists_and_is_valid_json(contract):
    assert contract["$id"].endswith("mission-state/1.0.json")
    assert contract["properties"]["state"]["type"] == "string"


def test_contract_state_machine_covers_all_12_states(contract):
    sm = contract["mission_state_machine"]
    assert len(sm) == 12, f"expected 12 states, got {len(sm)}"
    for state, spec in sm.items():
        assert "to" in spec, f"state {state} missing 'to' transitions"


def test_aie_mission_dataclass_has_id_and_state(contract):
    from aie_runtime.engine import Mission
    fields = Mission.__dataclass_fields__
    assert "id" in fields, "AIE Mission missing 'id' (required by contract)"
    assert "state" in fields, "AIE Mission missing 'state' (required by contract)"


def test_aie_mission_default_state_is_contract_valid(contract):
    from aie_runtime.engine import Mission
    enum_states = set(contract["properties"]["state"]["enum"])
    import dataclasses
    state_field = Mission.__dataclass_fields__["state"]
    default = state_field.default if state_field.default is not dataclasses.MISSING else None
    if default is not None:
        assert default in enum_states, f"AIE Mission default state '{default}' not in contract enum"


def test_contract_invariants_present(contract):
    invariants = contract["mission_invariants"]
    assert len(invariants) >= 4
    text = " ".join(invariants)
    assert "Complete != Verified" in text, "ISR Invariant 1 missing"
    assert "Evidence Gated" in text, "ISR Invariant 2 missing"
    assert "TH-12" in text, "AIE revalidation invariant missing"
    assert "HMAC" in text, "AIE persistence invariant missing"


def test_contract_canonical_sources_reference_aie(contract):
    sources = contract["canonical_sources"]
    assert "aie/src/aie_runtime/engine.py" in sources["authority_engine"]
    assert "aie/src/aie_runtime/persistent_state.py" in sources["persistent_state"]


def test_cli_rejects_invalid_mission_state(contract, tmp_path, monkeypatch):
    """H9: CLI config-load skal afvise mission-states uden for 1.0.json-enum (fail-closed)."""
    import json as _json
    config = {"missions": [{"id": "mission:bad", "state": "active"}]}
    cfg = tmp_path / "config.json"
    cfg.write_text(_json.dumps(config))
    from aie_runtime.gateway.cli import build_gateway_from_config
    with pytest.raises(ValueError, match="invalid mission state"):
        build_gateway_from_config(str(cfg))


def test_cli_accepts_valid_mission_state(contract, tmp_path):
    """H9: gyldigt mission-state (RUNNING) accepteres af CLI-config-load."""
    import json as _json
    config = {"missions": [{"id": "mission:ok", "state": "RUNNING"}]}
    cfg = tmp_path / "config.json"
    cfg.write_text(_json.dumps(config))
    from aie_runtime.gateway.cli import build_gateway_from_config
    gw = build_gateway_from_config(str(cfg))
    assert gw.state.missions["mission:ok"].state == "RUNNING"


def test_engine_mission_states_match_contract_enum(contract):
    """H9: MISSION_STATES-konstanten i engine.py == 1.0.json-enum."""
    from aie_runtime.engine import MISSION_STATES
    enum_states = set(contract["properties"]["state"]["enum"])
    assert MISSION_STATES == enum_states, (
        f"engine.MISSION_STATES {sorted(MISSION_STATES)} != contract enum {sorted(enum_states)}"
    )
