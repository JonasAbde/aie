"""Shared contract-path resolution for AIE conformance tests.

Resolves the GOV contract tree in this order (first hit wins):

1. AGC_CONTRACTS_DIR env var (explicit override, e.g. a CI checkout path)
2. Side-by-side checkout of after-graph-governance (repo sibling, dev setup)
3. Vendored copy under contracts/ (committed, synced via
   scripts/sync-contracts.sh against GOV remote main)

This removes the hard requirement for a side-by-side checkout, so the
conformance suite runs on any host (CI runner, fresh clone) without
additional repo setup.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def resolve_contracts_dir() -> Path:
    # 1. env override
    env = os.environ.get("AGC_CONTRACTS_DIR")
    if env:
        p = Path(env)
        if (p / "frozen").is_dir():
            return p
    # 2. side-by-side sibling clone
    sibling = REPO_ROOT.parent / "after-graph-governance" / "docs" / "contracts"
    if sibling.is_dir():
        return sibling
    # 3. vendored copy
    vendored = REPO_ROOT / "contracts"
    if vendored.is_dir():
        return vendored
    raise FileNotFoundError(
        "no contract source: set AGC_CONTRACTS_DIR, checkout "
        "after-graph-governance as sibling, or run scripts/sync-contracts.sh"
    )
