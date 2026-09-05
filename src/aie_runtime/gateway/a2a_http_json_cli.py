from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Any

from .a2a_http_json import A2AHTTPJSONForwarder, A2AHTTPJSONServer, create_a2a_http_json_server
from .cli import build_gateway_from_config
from .tls import build_client_ssl_context, build_server_ssl_context
from .workload_api import (
    RotatingTLSContextProvider,
    WorkloadAPIClient,
    WorkloadAPISVIDWatcher,
    build_ssl_contexts_from_svid,
)


@dataclass
class BuiltA2AHTTPJSON:
    server: A2AHTTPJSONServer
    watcher: WorkloadAPISVIDWatcher | None = None


def _resolve(base: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


def _tenant(value: Any) -> str | None:
    if value is None:
        return None
    tenant = str(value)
    if not tenant or any(ch in tenant for ch in ("/", "\\", "\x00")):
        raise ValueError("a2a_http_json tenant must be one non-empty path segment")
    return tenant


def build_a2a_http_json_from_config(
    config_path: str | Path,
    *,
    clock: Callable[[], datetime] | None = None,
    trust_header_identity: bool = False,
) -> BuiltA2AHTTPJSON:
    path = Path(config_path)
    config = json.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    transport = config.get("a2a_http_json")
    if not isinstance(transport, dict):
        raise ValueError("a2a_http_json configuration is required")
    listen = transport.get("listen") or {}
    upstream = transport.get("upstream") or {}
    if not upstream.get("url"):
        raise ValueError("a2a_http_json upstream.url is required")
    tenant = _tenant(transport.get("tenant"))
    gateway = build_gateway_from_config(path, clock=clock)

    material = None
    provider = None
    watcher = None
    workload = config.get("workload_api")
    if workload:
        endpoint = str(workload.get("endpoint") or os.environ.get("SPIFFE_ENDPOINT_SOCKET") or "")
        if not endpoint:
            raise ValueError("workload_api endpoint or SPIFFE_ENDPOINT_SOCKET is required")
        client = WorkloadAPIClient(endpoint)
        material = client.fetch_x509_svid(
            timeout=float(workload.get("timeout", 5.0)),
            hint=workload.get("hint"),
        )
        if bool(workload.get("watch", False)):
            provider = RotatingTLSContextProvider(
                material,
                require_client_cert=bool((listen.get("tls") or {}).get("require_client_cert", True)),
            )
            watcher = WorkloadAPISVIDWatcher(
                client,
                provider,
                hint=workload.get("hint"),
                reconnect_delay=float(workload.get("reconnect_delay", 0.5)),
            )

    inbound_context = None
    inbound_provider = None
    listen_tls = listen.get("tls")
    if listen_tls:
        if listen_tls.get("source") == "workload_api":
            if provider is not None:
                inbound_provider = provider
            elif material is not None:
                inbound_context, _ = build_ssl_contexts_from_svid(
                    material,
                    require_client_cert=bool(listen_tls.get("require_client_cert", True)),
                )
            else:
                raise ValueError("workload_api listen TLS requires workload_api configuration")
        else:
            inbound_context = build_server_ssl_context(
                certfile=_resolve(base, listen_tls["certfile"]),
                keyfile=_resolve(base, listen_tls["keyfile"]),
                cafile=_resolve(base, listen_tls["cafile"]),
                require_client_cert=bool(listen_tls.get("require_client_cert", True)),
            )

    outbound_context = None
    outbound_provider = None
    upstream_tls = upstream.get("tls")
    if upstream_tls:
        if upstream_tls.get("source") == "workload_api":
            if provider is not None:
                outbound_provider = provider.client_context
            elif material is not None:
                _, outbound_context = build_ssl_contexts_from_svid(material, require_client_cert=True)
            else:
                raise ValueError("workload_api upstream TLS requires workload_api configuration")
        else:
            outbound_context = build_client_ssl_context(
                certfile=_resolve(base, upstream_tls["certfile"]),
                keyfile=_resolve(base, upstream_tls["keyfile"]),
                cafile=_resolve(base, upstream_tls["cafile"]),
            )

    forwarder = A2AHTTPJSONForwarder(
        str(upstream["url"]),
        timeout=float(upstream.get("timeout", 5.0)),
        ssl_context=outbound_context,
        ssl_context_provider=outbound_provider,
        expected_peer_spiffe_id=upstream.get("expected_spiffe_id"),
    )
    server = create_a2a_http_json_server(
        gateway,
        forwarder,
        host=str(listen.get("host", "127.0.0.1")),
        port=int(listen.get("port", 0)),
        tenant=tenant,
        trust_header_identity=trust_header_identity,
        ssl_context=inbound_context,
        tls_context_provider=inbound_provider,
    )
    return BuiltA2AHTTPJSON(server=server, watcher=watcher)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AIE A2A 1.0 HTTP+JSON transport gateway")
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--trust-header-identity",
        action="store_true",
        help="TEST/REFERENCE ONLY: trust verified SPIFFE identity headers instead of mTLS",
    )
    args = parser.parse_args(argv)
    built = build_a2a_http_json_from_config(
        args.config,
        trust_header_identity=args.trust_header_identity,
    )
    if built.watcher is not None:
        built.watcher.start()
    try:
        built.server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if built.watcher is not None:
            built.watcher.stop()
            built.watcher.join(timeout=2.0)
        built.server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
