# Status

## Current maturity

- Core semantics: **AIE Draft 0.3**
- Reference gateway: **0.3.x**
- Current workstream: **v0.4-S2 external A2A interop (local TCK PASS; external attestation pending — issues #5/#6 open)**
- Promotion: **local PASS** (direct TCK run 2026-09-04, S2 promotion report `/home/nora/aie-evidence/s2-promotion/AIE_S2_A2A_INTEROP.json`). External A2A S2 promotion gated on issues #5/#6 — do not claim institutional S2 PASS until externally attested.

## Proven locally

- repository baseline: 173/173 tests passing after S1.1 promotion (8 new regression tests for SEP-2575 SSE relay, rotation gates, SPIRE lab, and read1() prompt delivery)
- current S2 A2A-preparation targeted suite: 16/16 tests passing after provenance, S1-attestation, malformed-evidence, and TCK-process-status hardening
- an earlier integrated S2 review tree reached 144/144; after scratch recovery the current PR head is reported conservatively as baseline + targeted evidence rather than claiming a fresh full-suite rerun
- A2A HTTP+JSON SSE/rotation slice: 11/11 targeted tests and 93/93 clean-room gateway regression tests passing locally; this is scoped implementation evidence, not official external TCK evidence
- two independent runtime paths for C0/D1/T1/F1 semantics
- durable replay, budget, revocation, and evidence state
- strict X.509-SVID identity validation and Workload API consumption
- MCP/A2A forwarding behind the same admission semantics
- gateway federation and privacy-minimized evidence
- S1.1/S1.2 harness generation and fail-closed promotion reporting
- provider-neutral self-hosted runner preflight and canonical promotion wrapper
- isolated per-run Python environment on persistent self-hosted runners
- pinned/checksum-verified GitHub Actions Runner bootstrap with non-root service identity and controlled deregistration
- main-only root-capable workflow with immutable checkout/upload Action pins
- fail-safe rollback/revocation of the dedicated runner sudoers grant
- weekly Dependabot coverage for Python and GitHub Actions dependencies
- wheel metadata emits SPDX `Apache-2.0`, bundled `LICENSE`, Markdown README, author, keywords, and canonical project URLs
- official A2A TCK 1.0.0 preparation harness pinned to commit `263b9cfa…`, using the upstream frozen `uv.lock`, official-origin verification, exact TCK exit-status evidence, and direct/SPIFFE/AIE MUST parity hard-gated on a validated canonical S1 attestation
- SEP-2575 SSE stream relay (post-#25): bridge no longer hangs on the chunked terminator; upstream `text/event-stream` responses are forwarded as chunked transfer-encoding to the downstream client
- in-house unit CI on the VDS self-hosted runner: 3.11/3.12/3.13 matrix runs `pip install -e .[dev,otel]` + `pytest -q` + `compileall` and posts the `ci/test (<py>)` check on the PR, so unit CI survives a GitHub Actions billing lock on the account

## External blockers

GitHub Actions events are accepted by the repository, but repeated GitHub-hosted jobs terminated before runner execution with zero steps/logs. This remains an execution-environment blocker, not interoperability evidence.

The hosted external probe is therefore manual-only while the blocker is unresolved. A second manual workflow targets a dedicated Linux x64 self-hosted runner carrying the `aie-interop` label and preserves the same canonical `AIE_S1_1_PROMOTION.json` contract.

The repository now contains a pinned self-hosted runner installer, but registration itself still requires a fresh short-lived GitHub repository runner token and a dedicated/ephemeral Linux host. The connected GitHub integration available in this session cannot issue runner registration/removal tokens or register the host directly.

The next promotion proof remains live SPIRE + official MCP `2026-07-28` conformance parity across direct, bridge, and AIE legs. No provider may mark S1.2 green without that report and its raw evidence.

GitHub Actions artifact storage quota is also exhausted on the `JonasAbde` account, so the S1.1 self-hosted workflow's `actions/upload-artifact` step fails after the promotion report is written. The in-house evidence is still on the VDS under `/home/nora/aie-evidence/<run_id>/`, mirrored to the runner's `_work/aie/aie/interop/s1/results/`. No live evidence is lost; only GH-side artifact retention is.

## S1.1 promotion — RESOLVED (2026-09-04 cycle 5)

**Promotion: PASS** (run 33831755655)

- All 4 external rotation gates PASS: `svid_rotation_live`, `trust_bundle_rotation_live`, `new_trust_works`, `old_trust_rejected`
- All 3 legs correctly demoted to `PASS_UPSTREAM_GAP` (195 checks each): `direct`, `bridge`, `aie`
- `live_spire: PASS`
- Evidence archived at `/home/nora/aie-evidence/33831755655/` (344KB)

### Root cause of the SEP-2575 aie/bridge leg failure

`http.client.HTTPResponse.read(amt)` can wait for and coalesce multiple HTTP chunks on long-lived SSE responses. On the aie leg (3-hop relay chain), this caused the first SSE frame to be delayed past the conformance test's 800ms timeout. The direct leg (2-hop) worked because the delay was within tolerance.

**Fix:** Change `response.read(8192)` to `response.read1(8192)` in all three relay paths:
- `spiffe_http.py::request_stream_with_peer_identity._Stream.__next__` (SPIFFE TLS path)
- `bridge.py::_request_stream._Stream.__next__` (non-SPIFFE TLS path)
- `forwarding.py::HTTPUpstreamForwarder.forward_stream._Stream.__next__` (urllib path)

`read1()` returns the first available buffered bytes without waiting to fill a larger read, so event frames cross the relay promptly.

**Commits:** dabd11c, 2430a42, f7840fc (all by Jonas Abde)

**Regression tests:** `test_plain_bridge_stream_yields_first_available_http_chunk`, `test_plain_forward_stream_yields_first_available_http_chunk`

### What was fixed in the S1.1 promotion effort (cycles 1-5)

- `bridge.py:122` — `if not chunk: continue` → `break` (chunked terminator hang)
- `http.py:91` — same bug in the AIE HTTP gateway
- `http.py:236` — stream POST /mcp `subscriptions/listen` via `forward_stream`
- `s1_interop.py:135` — return `promoted_legs` (with `PASS_UPSTREAM_GAP` demotion applied)
- `rotation_probe.py:87-93` — use the original `old_client_ctx` for `old_trust_rejected` gate
- `spire/server.conf` — `ca_ttl = "90s"` so the lab's bundle shrinks within the rotation window
- `run_live_rotation_gate.sh` — `sleep 2` after SPIRE `revoke` to let the gateway consume the post-revoke bundle
- `spiffe_http.py`, `bridge.py`, `forwarding.py` — `read(8192)` → `read1(8192)` for prompt SSE relay
- Local test suite: **173 passed** (was 165 at cycle start; +8 regression tests)

### Registries

- `evidence/s1.1/registry/claim_evidence_audit.md` — tracked claims and their evidence status
- `evidence/s1.1/registry/decision_log.md` — decisions made during the S1.1 promotion effort
- `evidence/s1.1/registry/experiment_registry.md` — experiments run and their results
- `evidence/s1.1/registry/open_questions.md` — open questions (all resolved)
