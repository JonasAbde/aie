from __future__ import annotations

from typing import Any, Mapping

from ..model import NormalizedAction, ProtocolError

A2A_VERSION = "1.0"


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def normalize_a2a_request(headers: Mapping[str, str], body: Mapping[str, Any]) -> NormalizedAction:
    version = _header(headers, "A2A-Version") or A2A_VERSION
    if version != A2A_VERSION:
        raise ProtocolError("AIE-PROTO-001", f"unsupported A2A version: {version!r}")

    method = str(body.get("method") or "")
    request_id = body.get("id")
    if not method or request_id is None:
        raise ProtocolError("AIE-PROTO-002", "A2A JSON-RPC method and id are required")

    params = body.get("params") if isinstance(body.get("params"), Mapping) else {}
    tenant = _header(headers, "AIE-A2A-Tenant")
    tenant_prefix = f"tenant/{tenant}/" if tenant else ""
    if method in {"message/send", "SendMessage", "message/stream", "SendStreamingMessage"}:
        message = params.get("message") if isinstance(params.get("message"), Mapping) else {}
        message_id = str(message.get("messageId") or message.get("message_id") or request_id)
        capability = "a2a.message.stream" if method in {"message/stream", "SendStreamingMessage"} else "a2a.message.send"
        resource = f"a2a://{tenant_prefix}message/{message_id}"
        subject_id = message_id
    elif method == "tasks/list":
        capability = "a2a.task.list"
        resource = f"a2a://{tenant_prefix}task"
        subject_id = None
    elif method.startswith("tasks/"):
        op = method.split("/", 1)[1].replace("/", ".")
        task_id = str(params.get("id") or params.get("taskId") or request_id)
        capability = f"a2a.task.{op}"
        resource = f"a2a://{tenant_prefix}task/{task_id}"
        subject_id = task_id
    else:
        normalized_method = method.replace("/", ".")
        capability = f"a2a.{normalized_method}"
        resource = f"a2a://operation/{method}"
        subject_id = None

    return NormalizedAction(
        protocol="a2a",
        protocol_version=version,
        action_id=str(request_id),
        capability=capability,
        resource=resource,
        operation=method,
        subject_id=subject_id,
        metadata={"jsonrpc": body.get("jsonrpc", "2.0")},
    )
