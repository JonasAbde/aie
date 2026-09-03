# AIE Spec → Runtime Reconciliation Design

**Status:** proposed design for review  
**Date:** 2026-09-03  
**Scope:** reconcile the original *After Graph / The Institution Layer* research and Candidate-readiness package with the current `JonasAbde/aie` repository before further feature expansion.

## 1. Purpose

AIE started as a standards thesis, not as an A2A or MCP proxy project. The reference gateway is the deliberately chosen wedge implementation of a broader institution model.

This reconciliation prevents the implementation from silently redefining the standard. It restores an explicit chain:

`research thesis → normative semantics → conformance claims → reference runtime → protocol bindings → external evidence → promotion`

The central architecture remains:

> **Graph defines coordination. Control enforces execution. Institution resolves legitimate authority.**

AIE remains protocol-neutral semantic glue above replaceable bindings such as MCP, A2A, SPIFFE/OIDC/OAuth, OPA/Cedar, OpenTelemetry, durable runtimes and ACS-compatible runtime controls.

## 2. Source-of-truth hierarchy

During reconciliation, evidence and authority are ordered as follows:

1. **Research intent:** `After_Graph_The_Institution_Layer_Research_Standards_v4_2026.pdf` and the associated `After_Graph_AIE_Candidate_Readiness_Package_v4_2026.zip` define the pre-code Draft 0.3 design baseline.
2. **Normative repository artifacts:** after reconciliation, `spec/` is the canonical operational source for Draft 0.3 semantics and registries.
3. **Executable conformance:** `conformance/` proves which normative requirements the reference runtimes actually implement. It must not claim a normative suite version it does not cover.
4. **Reference implementation:** `src/` demonstrates one implementation and may not redefine normative meanings locally.
5. **Protocol bindings:** MCP/A2A/HTTP/gRPC/SPIFFE adapters are replaceable bindings and must preserve core semantics.
6. **Interop evidence:** `interop/` and `evidence/` prove external behavior. Local tests cannot promote an external interoperability profile.
7. **Promotion claims:** governance gates decide maturity; code volume and local pass counts do not.

When two layers disagree, the disagreement is recorded as drift and resolved explicitly. Runtime behavior does not silently become specification text merely because it exists.

## 3. What Draft 0.3 actually stabilizes

The Candidate-readiness package defines these institution primitives as the target semantic model:

- `Principal`
- `Role`
- `AuthorityLease`
- `MissionContract`
- `PolicyDecisionRecord`
- `DelegationRecord`
- `BudgetLedger`
- `TopologyMutation`
- `RevocationRecord`
- `EvidenceRecord`
- `SettlementRecord`
- `Extension`

The critical invariants include:

- globally unique `actionId` or equivalent replay key for consequential actions;
- execution-time resolution of `Principal`, `MissionContract` and ACTIVE `AuthorityLease`;
- expired/revoked authority never executes and re-authorization creates a new lease;
- delegation attenuates scope, expiry, depth and conserved budgets;
- budget reservation prevents declared-dimension double-spend;
- topology mutation uses the same admission/evidence pipeline as tool actions;
- admission emits a normalized `PolicyDecisionRecord`;
- unknown freshness/policy state fails closed unless a named degraded mode is explicitly authorized;
- revocation declares a freshness/propagation objective;
- evidence minimizes sensitive payloads by default;
- every terminal mission produces a `SettlementRecord` linking outcome evidence and final budget accounting.

## 4. Current repository drift

### 4.1 Normative package drift

The V4 Candidate-readiness package contains artifacts that are not currently represented as first-class canonical files under `spec/`:

- `aie-draft-0.3.extensions.yaml`
- `reference_admission.py`
- `OPEN_ISSUES.md`
- package-level `spec/README.md`

The repository does already carry Draft 0.3 schema/reference/error/event/conformance artifacts and governance, so this is a reconciliation/migration problem rather than a new-spec invention.

### 4.2 Conformance-version drift

Three values currently coexist:

- normative Draft 0.3 package and `spec/AIE_Draft_0.3_Conformance.yaml`: `suiteVersion: 0.3.0`;
- generic executable `conformance/vectors.yaml`: `suiteVersion: 0.1.0` while declaring `semanticDraft: "0.3"`;
- one V4 prose example still contains a historical `suite 0.2.0` claim string.

**Design decision:** `0.3.0` is the normative Draft 0.3 conformance-suite identifier. The executable runtime vectors MUST NOT be relabeled to `0.3.0` until they cover the normative 0.3.0 claim surface. Until then, their current version remains implementation-suite metadata and a coverage matrix exposes the gap explicitly.

The historical prose example is treated as document drift, not as an alternate normative version.

### 4.3 Runtime-coverage drift

The gateway strongly implements admission, replay, durable budget/outcome state, authority checks, SPIFFE identity, forwarding, federation plumbing and privacy-minimized evidence.

The broader institution runtime remains incomplete relative to Draft 0.3, especially around:

- full `MissionContract` lifecycle and settlement criteria;
- normalized portable `PolicyDecisionRecord`;
- first-class `SettlementRecord` and evidence-incomplete settlement behavior;
- complete `TopologyMutation` lifecycle rather than only authorization hooks;
- human-approval lifecycle;
- explicit multi-dimensional budget representation;
- cryptographic evidence profile and selective disclosure;
- partitioned descendant revocation demonstrated between independent runtimes.

A protocol-binding pass cannot be counted as closure of these core semantic gaps.

## 5. Five independent maturity axes

`STATUS.md` will stop compressing maturity into one line. AIE shall report five independent axes:

### A. Core Semantic Maturity

How much of Draft 0.3's object model, invariants, lifecycles and registries is defined and internally consistent.

### B. Reference Runtime Coverage

Which normative objects/invariants are implemented and covered by executable tests in the reference runtimes.

### C. Binding Maturity

MCP, A2A HTTP+JSON/SSE, gRPC, SPIFFE, OPA-like policy, telemetry and other bindings. A binding may mature without changing Core maturity.

### D. External Interoperability

Direct/bridge/AIE parity against official external suites, live trust infrastructure, raw evidence and machine-readable promotion reports.

### E. Standard Governance Maturity

Research Draft → Implementer Draft → Interop Draft → Candidate Standard → 1.0, governed by independent implementations, published cross-runtime evidence, security review and registry governance.

No single green axis implies the others are green.

## 6. Canonical coverage matrix

Add a machine-readable mapping owned by `conformance/` with one row per normative requirement/vector.

Each row records:

- `semanticDraft`
- `suiteVersion`
- `profile`
- normative `testId` or invariant ID
- referenced normative artifact
- implementation surface(s)
- executable test(s)
- evidence location, if any
- status: `implemented`, `partially-implemented`, `not-implemented`, `blocked-external`
- notes explaining semantic deltas

The matrix is descriptive evidence, not a mechanism for granting conformance. A normative profile passes only when its required vectors execute successfully under the published profile rules.

## 7. Candidate-blocker ledger

The pre-code Candidate-readiness package listed twelve blockers. They return as the real standards roadmap:

1. canonical capability/resource identifiers;
2. cryptographic evidence profile;
3. revocation across partitions;
4. budget dimensions and numeric representation;
5. clock model;
6. `PolicyDecisionRecord` portability;
7. human approval semantics;
8. long-running action cancellation after irreversible side effects begin;
9. evidence privacy/redaction/retention/selective disclosure;
10. settlement disputes;
11. registry governance bootstrap;
12. independent implementations with published cross-runtime results.

These are standards blockers. Binding-specific tasks such as A2A gRPC or Agent Card support remain interop/binding work and do not replace this ledger.

## 8. Reconciliation waves

### Wave R0 — Canonicalization and observability

No new semantics.

- import the missing V4 normative/support artifacts into `spec/` with repository naming conventions;
- document V4 package provenance and exact relationship to the repository artifacts;
- create the conformance coverage matrix;
- expose the five maturity axes in `STATUS.md`;
- restore the Candidate-blocker ledger to the repository roadmap/issues;
- document the version discrepancy instead of papering over it;
- classify open binding PRs, including A2A streaming, by maturity axis.

**Exit:** a reviewer can trace every Draft 0.3 claim from source text to implementation/test/evidence or to an explicit gap.

### Wave R1 — Institution-core closure

Close missing core semantics using TDD against normative vectors, prioritizing:

1. `MissionContract` lifecycle;
2. normalized `PolicyDecisionRecord`;
3. `EvidenceRecord`/`SettlementRecord` behavior, including `AIE-EVID-001` and `AIE-SETTLE-001`;
4. first-class governed `TopologyMutation`;
5. budget/clock representation decisions required for deterministic behavior.

**Exit:** executable reference-runtime vectors faithfully cover the normative C0/D1/T1 claim surface they advertise.

### Wave R2 — Interop and independent proof

Continue the existing external work without conflating it with core semantics:

- S1.2 external SPIRE + official MCP parity;
- S2 official A2A TCK across required bindings;
- independent-runtime/federation revocation evidence;
- published raw evidence and canonical reports.

**Exit:** Interop Draft gates can be evaluated honestly. Candidate remains separately governed.

## 9. PR #22 classification

PR #22 (`feat: add durable A2A HTTP+JSON SSE streaming`) is a **Binding Maturity** change.

Its SSE lifecycle, durable `in-flight → admitted|uncertain`, replay-race handling, tenant scoping, SPIFFE rotation and transport transparency are important implementation work. They do not by themselves advance Core Semantic Maturity or Standard Governance Maturity.

The PR should therefore be completed and merged on its own technical gates, while issue #6 remains blocked until official A2A/S1.2 requirements are satisfied.

## 10. Governance and promotion boundary

The repository's evidence hierarchy remains:

`external reproducible evidence > independent implementation evidence > conformance vectors > unit/integration tests > prose claims`

Candidate Standard is not claimable merely because two runtime code paths exist in one repository. Candidate requires separately developed implementations and public cross-runtime evidence plus the published governance/security gates.

The reconciliation work MUST NOT weaken this promotion boundary.

## 11. Non-goals

This design does not:

- rename AIE;
- create a new wire protocol;
- replace MCP/A2A/SPIFFE/OIDC/OPA/OpenTelemetry/ACS/durable runtimes;
- declare Candidate Standard;
- force all Candidate blockers into the next coding sprint;
- block useful binding work while core semantics are reconciled;
- change Draft 0.3 normative meanings merely to match existing implementation behavior.

## 12. Acceptance criteria for reconciliation

The reconciliation is complete when:

1. every artifact from the V4 normative package is either represented canonically in the repo or explicitly documented as non-canonical/supporting material;
2. exactly one normative Draft 0.3 conformance-suite version is documented (`0.3.0`), with implementation-suite versions separately named;
3. a machine-readable coverage matrix maps all Draft 0.3 profiles/vectors to implementation/tests/evidence/gaps;
4. `STATUS.md` reports the five maturity axes separately;
5. all twelve Candidate blockers exist in a durable repository ledger;
6. README/governance language continues to prohibit promotion from local tests alone;
7. PR #22 and future binding PRs cannot accidentally imply Core/Candidate promotion;
8. no reconciliation commit changes runtime semantics without a separate normative/TDD review.

## 13. Intended implementation decomposition

After this design is approved, implementation should be split into independently reviewable tasks:

1. **Normative package migration** — missing V4 artifacts + provenance, no semantic edits.
2. **Version/coverage reconciliation** — conformance metadata and machine-readable coverage map.
3. **Maturity status model** — five-axis `STATUS.md` and README alignment.
4. **Candidate blocker ledger** — durable issue/roadmap representation.
5. **Core semantic closure plans** — separate TDD plans for Mission/PolicyDecision/Settlement/Topology rather than one giant refactor.

This separation keeps standards reconciliation reviewable and prevents binding work from becoming an accidental semantic rewrite.
