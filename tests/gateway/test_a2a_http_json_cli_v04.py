from __future__ import annotations

import json
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from aie_runtime.gateway.workload_api import RotatingTLSContextProvider, WorkloadAPISVID, WorkloadAPISVIDWatcher
from tls_material import issue_test_pki

NOW = datetime(2026, 9, 3, 1, 0, tzinfo=timezone.utc)


def _base_config(tmp_path):
    return {
        "store": str(tmp_path / "gateway.db"),
        "principals": [
            {"id": "p", "type": "agent", "identity_ref": "spiffe://example.org/client/a"}
        ],
        "missions": [{"id": "m", "state": "active"}],
        "leases": [
            {
                "id": "l",
                "principal_id": "p",
                "mission_id": "m",
                "capabilities": ["a2a.task.get"],
                "resource_prefixes": ["a2a://tenant/acme/task/"],
                "expires_at": "2026-09-03T05:00:00+00:00",
                "budget_remaining": 10,
            }
        ],
        "policy": {"type": "local", "decision": "allow"},
    }


def test_pyproject_registers_http_json_console_script():
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert project["project"]["scripts"]["aie-a2a-http-json"] == "aie_runtime.gateway.a2a_http_json_cli:main"


def test_build_http_json_transport_from_config(tmp_path):
    from aie_runtime.gateway.a2a_http_json_cli import build_a2a_http_json_from_config

    config = _base_config(tmp_path)
    config["a2a_http_json"] = {
        "tenant": "acme",
        "listen": {"host": "127.0.0.1", "port": 0},
        "upstream": {"url": "http://127.0.0.1:3001/api", "timeout": 7.5},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    built = build_a2a_http_json_from_config(path, clock=lambda: NOW, trust_header_identity=True)
    try:
        assert built.server.tenant == "acme"
        assert built.server.trust_header_identity is True
        assert built.server.tls_enabled is False
        assert built.server.forwarder.base_url == "http://127.0.0.1:3001/api"
        assert built.server.forwarder.timeout == 7.5
        assert built.watcher is None
    finally:
        built.server.server_close()


def test_build_http_json_transport_rejects_invalid_tenant(tmp_path):
    from aie_runtime.gateway.a2a_http_json_cli import build_a2a_http_json_from_config

    config = _base_config(tmp_path)
    config["a2a_http_json"] = {
        "tenant": "acme/escape",
        "listen": {"host": "127.0.0.1", "port": 0},
        "upstream": {"url": "http://127.0.0.1:3001"},
    }
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    try:
        build_a2a_http_json_from_config(path, clock=lambda: NOW)
    except ValueError as exc:
        assert "tenant" in str(exc).lower()
    else:
        raise AssertionError("invalid tenant must fail closed at startup")


def test_workload_api_watch_drives_inbound_and_outbound_tls(monkeypatch, tmp_path):
    from aie_runtime.gateway.a2a_http_json_cli import build_a2a_http_json_from_config

    pki = issue_test_pki(tmp_path / "pki")
    leaf = x509.load_pem_x509_certificate(pki["gw_a_crt"].read_bytes())
    ca = x509.load_pem_x509_certificate(pki["ca"].read_bytes())
    key = serialization.load_pem_private_key(pki["gw_a_key"].read_bytes(), password=None)
    material = WorkloadAPISVID(
        spiffe_id="spiffe://example.org/gateway/a",
        x509_svid=leaf.public_bytes(serialization.Encoding.DER),
        x509_svid_key=key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        bundle=ca.public_bytes(serialization.Encoding.DER),
        hint="a2a-http-json",
    )

    class FakeClient:
        def __init__(self, endpoint):
            assert endpoint == "unix:///run/spire/sockets/agent.sock"

        def fetch_x509_svid(self, *, timeout, hint):
            assert timeout == 2.0
            assert hint == "a2a-http-json"
            return material

        def subscribe_x509_svid(self, *, timeout=None, hint=None):
            raise AssertionError("watcher must not start during config build")

    monkeypatch.setattr("aie_runtime.gateway.a2a_http_json_cli.WorkloadAPIClient", FakeClient)
    config = _base_config(tmp_path)
    config["workload_api"] = {
        "endpoint": "unix:///run/spire/sockets/agent.sock",
        "hint": "a2a-http-json",
        "timeout": 2.0,
        "watch": True,
        "reconnect_delay": 0.25,
    }
    config["a2a_http_json"] = {
        "tenant": "acme",
        "listen": {
            "host": "127.0.0.1",
            "port": 0,
            "tls": {"source": "workload_api", "require_client_cert": True},
        },
        "upstream": {
            "url": "https://127.0.0.1:3001",
            "tls": {"source": "workload_api"},
            "expected_spiffe_id": "spiffe://example.org/a2a/server",
        },
    }
    path = tmp_path / "workload.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    built = build_a2a_http_json_from_config(path, clock=lambda: NOW)
    try:
        assert isinstance(built.server.tls_context_provider, RotatingTLSContextProvider)
        assert built.server.tls_enabled is True
        assert built.server.forwarder.ssl_context is None
        assert built.server.forwarder.ssl_context_provider is not None
        assert built.server.forwarder.ssl_context_provider() is built.server.tls_context_provider.client_context()
        assert built.server.forwarder.expected_peer_spiffe_id == "spiffe://example.org/a2a/server"
        assert isinstance(built.watcher, WorkloadAPISVIDWatcher)
    finally:
        built.server.server_close()
