# A2A 1.0 HTTP+JSON binding — v0.4 S2 workstream

This reference binding adapts A2A HTTP+JSON requests to the existing AIE authority pipeline while preserving the original HTTP method, path, query string, body, and non-AIE headers upstream.

## Implemented surface

- `POST /message:send`
- `POST /message:stream` using SSE
- `GET /tasks`
- `GET /tasks/{id}`
- `POST /tasks/{id}:cancel`
- `POST /tasks/{id}:subscribe` using SSE
- optional configured tenant prefix, e.g. `/acme/tasks/{id}`

Push-notification resources and the extended Agent Card endpoint remain outside this slice. gRPC remains a separate S2 transport gap.

## Authority semantics

A configured tenant is part of the canonical AIE resource identity, for example `a2a://tenant/acme/task/task-1`. Percent-encoded identifiers are decoded for authority/evidence identity, while decoded path separators, backslashes, and NUL are rejected. The original wire path is forwarded unchanged.

Repeatable reads and subscriptions receive per-request action IDs. Consequential mutations use stable identities where the protocol provides one: `messageId` for send/stream and a deterministic tenant-scoped cancel ID for task cancellation.

## Streaming durability

Streaming uses a durable `in-flight` replay marker after the upstream request has been dispatched. This prevents a duplicate `messageId` from being redispatched while the first stream is active. Terminal completion replaces the marker with `admitted`; post-dispatch transport ambiguity becomes `uncertain` with `AIE-UPSTREAM-002` and remains replay protected.

The normal connection timeout applies to connect/response establishment. Once a valid SSE response is established, the stream read itself is not terminated merely because it is idle.

## Trust

Inbound callers can use verified X.509-SVID mTLS. Outbound HTTPS can pin an exact expected SPIFFE workload identity before HTTP dispatch. Workload API-backed TLS contexts may be rotated without restarting the HTTP+JSON server.

Trusted identity headers are reference/test mode only and require explicit opt-in.

## Non-claim

This is reference implementation evidence, not an A2A TCK PASS or S2 promotion. Official direct/SPIFFE/AIE TCK parity across HTTP+JSON, JSON-RPC and gRPC remains externally gated, and S2 remains blocked by the canonical S1 external attestation.
