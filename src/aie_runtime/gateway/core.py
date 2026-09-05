from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable, Mapping

from aie_runtime.engine import AuthorityLease
from aie_runtime.capabilities import capability_set_allows
from aie_runtime.errors import AIEError
from aie_runtime.store import InMemoryState

from .durable import SQLiteGatewayStore
from .evidence import build_gateway_evidence
from .forwarding import ForwardResult, UpstreamAuthenticationError, UpstreamTransportError
from .identity import TransportIdentity, VerifiedIdentityResolver
from .model import GatewayDecision, NormalizedAction, ProtocolError
from .policy import PolicyAdapter
from .protocols.a2a import normalize_a2a_request
from .protocols.mcp import normalize_mcp_request


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def request_fingerprint(
    identity: TransportIdentity,
    protocol: str,
    action: NormalizedAction,
    body: Mapping[str, Any],
    headers: Mapping[str, str],
) -> str:
    """Dedupe discriminator: JSON-RPC ids are only unique per client session, so
    a true replay is same id AND same content. Host/Origin are included because
    rebinding probes reuse one id across evil and valid requests."""
    canonical = json.dumps(
        {
            "spiffe": identity.spiffe_id,
            "verified": bool(identity.verified),
            "protocol": protocol,
            "operation": action.operation,
            "body": dict(body),
            "host": _header(headers, "Host"),
            "origin": _header(headers, "Origin"),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class AIEGateway:
    def __init__(
        self,
        *,
        state: InMemoryState,
        store: SQLiteGatewayStore,
        policy: PolicyAdapter,
        identity_resolver: VerifiedIdentityResolver | None = None,
        clock: Callable[[], datetime] | None = None,
        evidence_exporter: Any | None = None,
        authority_bindings: Mapping[str, tuple[str, str]] | None = None,
        protocol_passthrough_on_parse_error: bool = False,
    ):
        self.state = state
        self.store = store
        self.policy = policy
        self.identity_resolver = identity_resolver or VerifiedIdentityResolver()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.evidence_exporter = evidence_exporter
        self.authority_bindings = dict(authority_bindings or {})
        self.protocol_passthrough_on_parse_error = bool(protocol_passthrough_on_parse_error)
        for lease in self.state.leases.values():
            self.store.initialize_budget(lease.id, float(lease.budget_remaining))

    def _normalize(self, protocol: str, headers: Mapping[str, str], body: Mapping[str, Any]) -> NormalizedAction:
        if protocol == "mcp":
            return normalize_mcp_request(headers, body)
        if protocol == "a2a":
            return normalize_a2a_request(headers, body)
        raise ProtocolError("AIE-PROTO-001", f"unsupported protocol: {protocol}")

    def _transport_passthrough_action(
        self,
        protocol: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any],
    ) -> NormalizedAction:
        if protocol != "mcp":
            raise ProtocolError("AIE-PROTO-001", "protocol parse-error passthrough is MCP-only")
        request_id = body.get("id")
        if request_id is None:
            canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            request_id = "raw-" + hashlib.sha256(canonical).hexdigest()[:24]
        return NormalizedAction(
            protocol="mcp",
            protocol_version=_header(headers, "MCP-Protocol-Version") or "unknown",
            action_id=str(request_id),
            capability="mcp.transport.forward",
            resource="mcp://transport/http",
            operation="transport/forward",
            metadata={"parseErrorPassthrough": True},
        )

    def _principal_for_identity(self, spiffe_id: str):
        for principal in self.state.principals.values():
            if principal.identity_ref == spiffe_id:
                return principal
        raise AIEError("AIE-AUTH-001")

    def _ancestor_revoked(self, lease: AuthorityLease) -> bool:
        current: AuthorityLease | None = lease
        seen: set[str] = set()
        while current is not None and current.id not in seen:
            seen.add(current.id)
            if current.revoked or self.store.is_revoked(current.id):
                return True
            parent_id = current.parent_lease_id
            current = self.state.leases.get(parent_id) if parent_id else None
        return False

    def _resolve_authority(
        self,
        *,
        action: NormalizedAction,
        headers: Mapping[str, str],
        identity: TransportIdentity,
    ) -> tuple[str, str, AuthorityLease, float]:
        spiffe_id = self.identity_resolver.resolve(identity)
        principal = self._principal_for_identity(spiffe_id)
        explicit_mission = _header(headers, "AIE-Mission-Id")
        explicit_lease = _header(headers, "AIE-Authority-Lease")
        if explicit_mission is None and explicit_lease is None:
            binding = self.authority_bindings.get(spiffe_id)
            if binding is None:
                raise AIEError("AIE-AUTH-001")
            mission_id, lease_id = binding
        elif explicit_mission is None or explicit_lease is None:
            raise AIEError("AIE-AUTH-001")
        else:
            mission_id, lease_id = explicit_mission, explicit_lease
        mission = self.state.missions.get(mission_id)
        lease = self.state.leases.get(lease_id)
        if mission is None or lease is None:
            raise AIEError("AIE-AUTH-001")
        if lease.principal_id != principal.id or lease.mission_id != mission.id:
            raise AIEError("AIE-AUTH-001")
        if lease.expires_at <= self.clock():
            raise AIEError("AIE-AUTH-002")
        if self._ancestor_revoked(lease):
            raise AIEError("AIE-AUTH-003")
        if not capability_set_allows(lease.capabilities, action.capability) or not any(
            action.resource.startswith(prefix) for prefix in lease.resource_prefixes
        ):
            raise AIEError("AIE-AUTH-004")
        try:
            budget_cost = float(_header(headers, "AIE-Budget-Cost") or 0.0)
        except (TypeError, ValueError) as exc:
            raise AIEError("AIE-BUDGET-001") from exc
        if budget_cost < 0:
            raise AIEError("AIE-BUDGET-001")
        return principal.id, mission.id, lease, budget_cost

    def _record(
        self,
        action: NormalizedAction,
        decision: GatewayDecision,
        identity: TransportIdentity,
        *,
        principal_id: str,
        mission_id: str,
        lease_id: str,
        carrier: Mapping[str, str] | None = None,
        fingerprint: str | None = None,
    ) -> None:
        self.store.put_outcome(
            action.action_id,
            status=decision.status,
            protocol=action.protocol,
            error_code=decision.error_code,
            fingerprint=fingerprint,
        )
        event = build_gateway_evidence(
            action,
            decision,
            identity,
            principal_id=principal_id,
            mission_id=mission_id,
            lease_id=lease_id,
        )
        self.store.append_evidence(event)
        if self.evidence_exporter is not None:
            self.evidence_exporter.emit(event, carrier=carrier or {})

    @staticmethod
    def _is_true_replay(prior: dict[str, Any], fingerprint: str) -> bool:
        stored = prior.get("fingerprint")
        if stored is None:
            return True  # legacy row without discriminator: preserve prior behavior
        return stored == fingerprint

    def forward(
        self,
        protocol: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any],
        identity: TransportIdentity,
        forwarder: Any,
    ) -> ForwardResult:
        action_id = str(body.get("id") or "unknown")
        try:
            action = self._normalize(protocol, headers, body)
        except ProtocolError as exc:
            if not self.protocol_passthrough_on_parse_error:
                return ForwardResult(GatewayDecision("denied", action_id, protocol, exc.code))
            try:
                action = self._transport_passthrough_action(protocol, headers, body)
            except ProtocolError as passthrough_exc:
                return ForwardResult(GatewayDecision("denied", action_id, protocol, passthrough_exc.code))

        fingerprint = request_fingerprint(identity, protocol, action, body, headers)
        prior = self.store.get_outcome(action.action_id)
        if prior is not None and not self._is_true_replay(prior, fingerprint):
            # Same id, different content: a new request, not a replay. Retire the
            # stale budget marker (money-neutral) and evaluate on the merits.
            self.store.clear_reservation(action.action_id)
            prior = None
        if prior is not None:
            return ForwardResult(
                GatewayDecision("prior-outcome", action.action_id, action.protocol, "AIE-REPLAY-001", prior=True)
            )

        principal_id = ""
        mission_id = _header(headers, "AIE-Mission-Id") or ""
        lease_id = _header(headers, "AIE-Authority-Lease") or ""
        reservation_made = False
        try:
            principal_id, mission_id, lease, budget_cost = self._resolve_authority(
                action=action, headers=headers, identity=identity
            )
            lease_id = lease.id
            self.store.reserve_budget(lease.id, action.action_id, budget_cost)
            reservation_made = True
            decision_input = {
                "principal": principal_id,
                "mission": mission_id,
                "lease": lease.id,
                "capability": action.capability,
                "resource": action.resource,
                "actionId": action.action_id,
                "protocol": action.protocol,
                "protocolVersion": action.protocol_version,
            }
            if not self.policy.evaluate(decision_input):
                raise AIEError("AIE-POLICY-001")
            try:
                upstream = forwarder.forward(protocol=protocol, headers=headers, body=body)
            except UpstreamAuthenticationError as exc:
                raise AIEError("AIE-UPSTREAM-001") from exc
            except UpstreamTransportError:
                self.store.commit_budget(action.action_id)
                decision = GatewayDecision("uncertain", action.action_id, action.protocol, "AIE-UPSTREAM-002")
                self._record(
                    action, decision, identity, principal_id=principal_id, mission_id=mission_id,
                    lease_id=lease_id, carrier=headers, fingerprint=fingerprint,
                )
                return ForwardResult(decision)
            self.store.commit_budget(action.action_id)
            decision = GatewayDecision("admitted", action.action_id, action.protocol)
            self._record(
                action, decision, identity, principal_id=principal_id, mission_id=mission_id,
                lease_id=lease_id, carrier=headers, fingerprint=fingerprint,
            )
            return ForwardResult(decision, upstream)
        except AIEError as exc:
            if exc.code == "AIE-REPLAY-001" and not reservation_made:
                return ForwardResult(
                    GatewayDecision(
                        "prior-outcome",
                        action.action_id,
                        action.protocol,
                        "AIE-REPLAY-001",
                        prior=True,
                    )
                )
            if reservation_made and self.store.reservation_state(action.action_id) == "reserved":
                self.store.rollback_budget(action.action_id)
            decision = GatewayDecision("denied", action.action_id, action.protocol, exc.code)
            self._record(
                action, decision, identity, principal_id=principal_id, mission_id=mission_id,
                lease_id=lease_id, carrier=headers, fingerprint=fingerprint,
            )
            return ForwardResult(decision)

    def handle(
        self,
        protocol: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any],
        identity: TransportIdentity,
    ) -> GatewayDecision:
        action_id = str(body.get("id") or "unknown")
        try:
            action = self._normalize(protocol, headers, body)
        except ProtocolError as exc:
            return GatewayDecision("denied", action_id, protocol, exc.code)

        fingerprint = request_fingerprint(identity, protocol, action, body, headers)
        prior = self.store.get_outcome(action.action_id)
        if prior is not None and not self._is_true_replay(prior, fingerprint):
            self.store.clear_reservation(action.action_id)
            prior = None
        if prior is not None:
            return GatewayDecision(
                "prior-outcome",
                action.action_id,
                action.protocol,
                "AIE-REPLAY-001",
                prior=True,
            )

        principal_id = ""
        mission_id = _header(headers, "AIE-Mission-Id") or ""
        lease_id = _header(headers, "AIE-Authority-Lease") or ""
        reservation_made = False
        try:
            principal_id, mission_id, lease, budget_cost = self._resolve_authority(
                action=action, headers=headers, identity=identity
            )
            lease_id = lease.id
            self.store.reserve_budget(lease.id, action.action_id, budget_cost)
            reservation_made = True
            decision_input = {
                "principal": principal_id,
                "mission": mission_id,
                "lease": lease.id,
                "capability": action.capability,
                "resource": action.resource,
                "actionId": action.action_id,
                "protocol": action.protocol,
                "protocolVersion": action.protocol_version,
            }
            allowed = self.policy.evaluate(decision_input)
            if not allowed:
                raise AIEError("AIE-POLICY-001")
            self.store.commit_budget(action.action_id)
            decision = GatewayDecision("admitted", action.action_id, action.protocol)
            self._record(
                action,
                decision,
                identity,
                principal_id=principal_id,
                mission_id=mission_id,
                lease_id=lease_id,
                carrier=headers,
                fingerprint=fingerprint,
            )
            return decision
        except AIEError as exc:
            if exc.code == "AIE-REPLAY-001" and not reservation_made:
                return GatewayDecision(
                    "prior-outcome",
                    action.action_id,
                    action.protocol,
                    "AIE-REPLAY-001",
                    prior=True,
                )
            if reservation_made and self.store.reservation_state(action.action_id) == "reserved":
                self.store.rollback_budget(action.action_id)
            decision = GatewayDecision("denied", action.action_id, action.protocol, exc.code)
            self._record(
                action,
                decision,
                identity,
                principal_id=principal_id,
                mission_id=mission_id,
                lease_id=lease_id,
                carrier=headers,
                fingerprint=fingerprint,
            )
            return decision
