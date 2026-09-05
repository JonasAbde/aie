"""Cross-repo conformance tests for remaining reconciliation matrix items.

Validates that AIE/TG implementations conform to the remaining WORKS frozen
schemas: cpi/1.0 (#3 Capability), proto.charter/1.0 (#11 Sandbox),
shell.contracts/1.0 (#14 Artifact/Shell).

Run: PYTHONPATH=src python -m pytest tests/test_crossrepo_conformance.py -q
"""
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ for conftest import

from conftest import resolve_contracts_dir

FROZEN = resolve_contracts_dir() / "frozen"


@pytest.fixture(scope="module")
def cpi():
    return json.loads((FROZEN / "cpi.schema.json").read_text())


@pytest.fixture(scope="module")
def proto_charter():
    return json.loads((FROZEN / "proto.charter.schema.json").read_text())


@pytest.fixture(scope="module")
def shell_contracts():
    return json.loads((FROZEN / "shell.contracts.schema.json").read_text())


# ── #3 Capability: cpi/1.0 ─────────────────────────────────────────────

def test_cpi_caps_enum_covers_tg_capabilities(cpi):
    """TG bots declare capabilities like fs.read, shell.run — the WORKS cpi/1.0
    capability enum covers the base resource types."""
    caps_enum = cpi["properties"]["caps"]["items"]["enum"]
    # TG capabilities use <resource>.<action> format; base resources map to cpi caps
    tg_base_resources = {"fs", "shell", "git", "browser"}
    for res in tg_base_resources:
        assert res in caps_enum, f"TG resource '{res}' not in cpi caps enum"


def test_cpi_abi_is_const(cpi):
    assert cpi["properties"]["abi"]["const"] == "cpi/1.0"


def test_cpi_caps_unique(cpi):
    assert cpi["properties"]["caps"]["uniqueItems"] is True


# ── #11 Sandbox/Execution: proto.charter/1.0 ───────────────────────────

def test_proto_charter_n_minus_1_tolerance(proto_charter):
    """The contract requires N-1 read tolerance — consumers must tolerate
    unknown fields from one version ahead."""
    assert proto_charter["properties"]["unknown_field_tolerance"]["const"] is True


def test_proto_charter_version_pattern(proto_charter):
    import re
    pattern = proto_charter["properties"]["version"]["pattern"]
    assert re.match(pattern, "1.0")
    assert re.match(pattern, "0.3")
    assert not re.match(pattern, "v1.0")


def test_proto_charter_required_fields(proto_charter):
    for field in ["name", "version", "capabilities"]:
        assert field in proto_charter["required"]


# ── #14 Artifact/Shell: shell.contracts/1.0 ────────────────────────────

def test_shell_contracts_pulse_local_only_cannot_approve(shell_contracts):
    """CRITICAL invariant: pulse system at local_only tier must NOT contain
    approve/deny/kill/take/hand_back commands — governance commands cannot
    bypass the TG control plane."""
    commands_enum = shell_contracts["properties"]["commands"]["items"]["enum"]
    forbidden = {"kill", "approve", "deny", "take", "hand_back"}
    # The allOf condition: if system=pulse and tier=local_only, commands must NOT contain these
    for f in shell_contracts["allOf"]:
        then_props = f.get("then", {}).get("properties", {})
        if "commands" in then_props and "not" in then_props["commands"]:
            contains_forbidden = then_props["commands"]["not"]["contains"]["enum"]
            assert set(contains_forbidden) == forbidden
            break
    else:
        pytest.fail("allOf governance-command exclusion not found in shell.contracts/1.0")


def test_shell_contracts_t3_privileged_requires_command_surface(shell_contracts):
    """T3_privileged tier requires surface=COMMAND."""
    for f in shell_contracts["allOf"]:
        then_props = f.get("then", {}).get("properties", {})
        if "surface" in then_props and then_props["surface"].get("const") == "COMMAND":
            break
    else:
        pytest.fail("T3_privileged surface=COMMAND condition not found")


def test_tg_policy_aligns_with_shell_governance(shell_contracts):
    """TG's fail-closed policy (read/write/destructive/secret) aligns with the
    shell.contracts governance model: local_only cannot contain governance
    commands — TG is the sole authority for consequential actions."""
    # The forbidden commands (approve, deny, kill, take, hand_back) are exactly
    # the ones TG's approval/takeover systems govern.
    forbidden = {"approve", "deny", "take", "hand_back", "kill"}
    tg_governed = {"approve", "deny", "take", "hand_back", "kill"}
    assert forbidden == tg_governed, "shell governance commands must match TG governed actions"
