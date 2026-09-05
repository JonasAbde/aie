import os
"""Budget conformance tests for kernel.budget/1.0 (WORKS frozen schema).

Validates that AIE's BudgetLedger semantics are compatible with the WORKS
kernel.budget/1.0 contract fields (reserved, consumed, ceiling).

Run: PYTHONPATH=src python -m pytest tests/test_budget_conformance.py -q
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

CONTRACT_PATH = Path(os.environ.get("GOVERNANCE_DIR", str(Path(__file__).resolve().parent.parent.parent / "after-graph-governance"))) / "docs" / "contracts" / "frozen" / "kernel.budget.schema.json"


@pytest.fixture(scope="module")
def contract():
    with open(CONTRACT_PATH) as f:
        return json.load(f)


def _ledger(budget=100.0):
    from aie_runtime.store import BudgetLedger
    return BudgetLedger(budget_usd=budget)


def test_contract_requires_work_id_ceiling_reserved_consumed(contract):
    required = contract["required"]
    for field in ["work_id", "ceiling", "reserved", "consumed", "clock_state"]:
        assert field in required, f"contract missing required field {field}"


def test_aie_budget_semantics_map_to_contract_fields(contract):
    """AIE budget_usd/spent_usd/reserved_usd correspond to WORKS
    ceiling.compute_eur/consumed/reserved."""
    ledger = _ledger(budget=100.0)
    now = datetime.now(timezone.utc)
    # reserve → consumed + reserved semantics
    assert ledger.reserve("a1", 10.0, now) is True
    assert ledger.reserved_usd == 10.0
    assert ledger.spent_usd == 0.0
    # commit moves reserved → spent
    assert ledger.commit("a1") is True
    assert ledger.reserved_usd == 0.0
    assert ledger.spent_usd == 10.0
    # The mapping: budget_usd == ceiling.compute_eur, spent_usd == consumed,
    # reserved_usd == reserved. All non-negative per contract minimums.
    assert ledger.budget_usd >= 0
    assert ledger.spent_usd >= 0
    assert ledger.reserved_usd >= 0


def test_aie_budget_monotonic_spending(contract):
    """Contract requires consumed minimum 0; AIE must be monotonic non-decreasing."""
    ledger = _ledger(budget=50.0)
    now = datetime.now(timezone.utc)
    ledger.reserve("x1", 20.0, now)
    ledger.commit("x1")
    spent_after_first = ledger.spent_usd
    ledger.reserve("x2", 10.0, now)
    ledger.commit("x2")
    assert ledger.spent_usd >= spent_after_first, "spent must be monotonic non-decreasing"


def test_aie_budget_idempotent_replay(contract):
    """Contract requires reservation idempotency (replay-safe)."""
    ledger = _ledger(budget=100.0)
    now = datetime.now(timezone.utc)
    assert ledger.reserve("dup", 10.0, now) is True
    # same action_id again → rejected (already committed)
    assert ledger.reserve("dup", 10.0, now) is False
    # spent unchanged after replay attempt
    assert ledger.spent_usd == 0.0


def test_aie_budget_over_ceiling_rejected(contract):
    """Contract hard_stop=compute semantics: cannot exceed ceiling."""
    ledger = _ledger(budget=20.0)
    now = datetime.now(timezone.utc)
    assert ledger.reserve("big", 50.0, now) is False, "over-ceiling reservation must fail"


def test_contract_hard_stop_enum_covers_aie_semantics(contract):
    hard_stop_values = set(contract["properties"]["hard_stop"]["enum"])
    # AIE enforces compute-based hard stop via BudgetLedger ceiling
    assert "compute" in hard_stop_values
    assert "none" in hard_stop_values
