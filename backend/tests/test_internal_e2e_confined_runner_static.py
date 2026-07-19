"""Static/unit tests for the off-Railway confined internal E2E runner."""
from __future__ import annotations

import json
import os
import socketserver
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from ops.internal_e2e_runner.lib.config import (
    RUNNER_CONFIG_SCHEMA_VERSION,
    database_url_fingerprint,
    default_operator_command,
    normalize_operator_command,
    parse_runner_config,
    redact_secrets,
    validate_database_url_requirements,
    validate_runner_config_blockers,
)
from ops.internal_e2e_runner.lib.evidence import build_network_evidence
from ops.internal_e2e_runner.lib.topology import (
    REQUIRED_SECRET_FILES,
    normalize_operator_command as normalize_closed_command,
    validate_revision_binding,
    validate_topology,
)
from ops.internal_e2e_runner.sidecars.connect_proxy import ExactConnectProxy
from ops.internal_e2e_runner.sidecars.db_relay import ExactDbRelay


def _base_config(**overrides):
    payload = {
        "schema_version": RUNNER_CONFIG_SCHEMA_VERSION,
        "pinned_revision": "d" * 40,
        "image_label": f"nahla-internal-e2e-confined:{'d' * 40}",
        "tenant_id": 48,
        "provider": "anthropic",
        "llm_host": "api.example-llm.test",
        "llm_port": 443,
        "llm_host_ips": ["203.0.113.10"],
        "db_proxy_host": "disposable-db-proxy.example.test",
        "db_proxy_port": 5432,
        "db_proxy_ips": ["203.0.113.20"],
        "connect_proxy_ip": "172.30.0.10",
        "connect_proxy_port": 3128,
        "db_relay_ip": "172.30.0.11",
        "db_relay_port": 5432,
        "negative_probe_targets": [
            {"host": "graph.facebook.com", "port": 443, "ips": ["31.13.64.35"]},
            {"host": "blocked-probe.example.test", "port": 443, "ips": ["203.0.113.99"]},
        ],
    }
    payload.update(overrides)
    return payload


def test_parse_runner_config_accepts_minimal_valid_config() -> None:
    config = parse_runner_config(_base_config())
    assert config.llm_endpoint.hostname == "api.example-llm.test"
    assert config.db_proxy_endpoint.port == 5432
    assert len(config.negative_probe_targets) == 2


@pytest.mark.parametrize(
    "host",
    [
        "postgres-staging",
        "db.postgres-staging.railway.internal",
        "tenant.postgres.railway.internal",
    ],
)
def test_rejects_canonical_or_private_db_hostnames(host: str) -> None:
    blockers = validate_runner_config_blockers(
        _base_config(db_proxy_host=host, db_proxy_ips=["203.0.113.21"]),
        database_url=f"postgresql://user:secret@{host}:5432/sandbox?sslmode=require",
    )
    assert blockers


def test_database_url_requires_tls() -> None:
    blockers = validate_database_url_requirements(
        "postgresql://user:secret@disposable-db-proxy.example.test:5432/sandbox"
    )
    assert "database_url_sslmode_require_missing" in blockers


def test_single_llm_provider_pin_enforced() -> None:
    with pytest.raises(ValueError, match="llm_host_invalid"):
        parse_runner_config(_base_config(llm_host="api.openai.com,api.anthropic.com"))


def test_default_operator_command_is_preflight_only() -> None:
    assert default_operator_command() == ["preflight"]
    assert normalize_operator_command([]) == ["preflight"]
    assert normalize_operator_command(None) == ["preflight"]


def test_operator_run_requires_scenarios() -> None:
    with pytest.raises(ValueError, match="operator_command_invalid"):
        normalize_operator_command(["run"])


def test_secret_redaction_masks_credentials_and_phones() -> None:
    raw = (
        "postgresql://user:topsecret@host/db?sslmode=require "
        "token=abc123 Bearer deadbeef phone 966501234567"
    )
    redacted = redact_secrets(raw)
    assert "topsecret" not in redacted
    assert "abc123" not in redacted
    assert "966501234567" not in redacted
    assert "[REDACTED]" in redacted


def test_evidence_is_unsigned_and_forbids_self_attestation() -> None:
    config = parse_runner_config(_base_config())
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    payload = build_network_evidence(
        config=config,
        started_at_utc=now,
        completed_at_utc=now,
        capability_proof={"cap_net_admin_required": True},
        firewall_backend="iptables",
        rules_dump_sanitized="-P OUTPUT DROP",
        hosts_pinning={config.llm_endpoint.hostname: list(config.llm_endpoint.ips)},
        positive_probes=[{"tcp_ok": True, "label": "llm"}],
        negative_probes=[{"blocked_ok": True, "label": "graph.facebook.com"}],
        image_digest_input=f"nahla-internal-e2e-confined:{'d' * 40}@{'d' * 40}",
        database_url_fingerprint=database_url_fingerprint(
            "postgresql://u:p@disposable-db-proxy.example.test:5432/sandbox?sslmode=require"
        ),
        operator_command=["preflight"],
        runtime_verification_status="pending_container_runtime",
    )
    assert payload["network_policy"] == "default_deny"
    assert payload["attestation_status"] == "unsigned_pending_external_signer"
    assert payload["self_attestation_forbidden"] is True
    assert "signature" not in payload
    assert payload["runtime_verification_status"] == "pending_container_runtime"


def test_fail_closed_on_private_ip_allowlist() -> None:
    with pytest.raises(ValueError, match="llm_host_ips_private_or_reserved_rejected"):
        parse_runner_config(_base_config(llm_host_ips=["10.0.0.5"]))


def test_fail_closed_on_wildcard_ip() -> None:
    with pytest.raises(ValueError, match="db_proxy_host_ips_wildcard_or_cidr_rejected"):
        parse_runner_config(_base_config(db_proxy_ips=["0.0.0.0/0"]))


def test_launcher_plan_json_shape_is_stable() -> None:
  """Guard the dry-run plan keys consumed by operators."""
  plan_keys = {
      "mode",
      "docker_daemon_available",
      "image_label",
      "pinned_revision",
      "security",
      "default_command",
      "runtime_verification",
  }
  sample = {
      "mode": "dry_run",
      "docker_daemon_available": False,
      "image_label": "nahla-internal-e2e-confined:local",
      "pinned_revision": "de4a1385f746",
      "security": {"cap_add": ["NET_ADMIN"]},
      "default_command": ["preflight"],
      "runtime_verification": "pending_docker_daemon_stopped_or_not_executed",
  }
  assert plan_keys.issubset(sample.keys())
  json.dumps(sample)


def test_topology_runner_is_internal_only_and_sidecars_are_dual_homed() -> None:
    assert validate_topology(
        runner_networks=["internal"],
        proxy_networks=["internal", "egress"],
        relay_networks=["internal", "egress"],
        internal_network="internal",
        egress_network="egress",
    ) == []
    assert "runner_must_only_attach_internal_network" in validate_topology(
        runner_networks=["internal", "egress"],
        proxy_networks=["internal", "egress"],
        relay_networks=["internal", "egress"],
        internal_network="internal",
        egress_network="egress",
    )


def test_full_revision_binding_rejects_short_unknown_and_mismatch() -> None:
    sha = "a" * 40
    assert validate_revision_binding(
        config_sha=sha, checkout_sha=sha, image_label_sha=sha, baked_sha=sha
    ) == []
    assert validate_revision_binding(
        config_sha="a" * 12,
        checkout_sha=sha,
        image_label_sha=sha,
        baked_sha=sha,
    )
    assert "revision_binding_mismatch" in validate_revision_binding(
        config_sha=sha,
        checkout_sha=sha,
        image_label_sha="b" * 40,
        baked_sha=sha,
    )


def test_closed_operator_command_rejects_arbitrary_args(tmp_path: Path) -> None:
    scenario = tmp_path / "scenarios.json"
    scenario.write_text("[]", encoding="utf-8")
    assert normalize_closed_command(None).mode == "preflight"
    assert normalize_closed_command(
        ["run", "--scenarios", str(scenario)]
    ).mode == "run"
    with pytest.raises(ValueError, match="operator_command_invalid"):
        normalize_closed_command(["preflight", "--tenant-id", "99"])


def test_required_secret_file_contract_is_complete() -> None:
    assert set(REQUIRED_SECRET_FILES) == {
        "database_url",
        "evidence_hmac_key",
        "attestation_hmac_key",
        "attestation_json",
        "attestation_signature",
        "network_confirm",
        "llm_api_key",
        "tenant_allowlist",
        "test_phone",
        "phone_allowlist",
    }


def test_sidecars_fail_closed_when_live_dns_differs(tmp_path: Path) -> None:
    fake_dns = [
        (2, 1, 6, "", ("198.51.100.9", 443)),
    ]
    connect = ExactConnectProxy(
        "api.anthropic.com",
        {"203.0.113.10"},
        tmp_path / "proxy.jsonl",
    )
    relay = ExactDbRelay(
        "disposable.example.test",
        5432,
        {"203.0.113.20"},
        tmp_path / "relay.jsonl",
    )
    with patch("socket.getaddrinfo", return_value=fake_dns):
        with pytest.raises(RuntimeError, match="llm_live_dns_mismatch"):
            connect.resolve_verified()
        with pytest.raises(RuntimeError, match="db_live_dns_mismatch"):
            relay.resolve_verified()


def test_runner_firewall_has_no_public_llm_or_db_accept_rules() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (
        root / "ops/internal_e2e_runner/scripts/apply_firewall.sh"
    ).read_text(encoding="utf-8")
    assert "connect_proxy_ip" in script
    assert "db_relay_ip" in script
    assert "llm_host_ips" not in script
    assert "db_proxy_ips" not in script
    assert "iptables-save > \"${RULES_OUT}\"" in script
    assert "open(p,\"rb\").read()" in script


def test_entrypoint_exports_secret_files_and_unsets_other_provider_keys() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (
        root / "ops/internal_e2e_runner/scripts/entrypoint.sh"
    ).read_text(encoding="utf-8")
    for name in REQUIRED_SECRET_FILES:
        assert f"/run/secrets/{name}" in script
    assert "unset OPENAI_API_KEY" in script
    assert "export HTTPS_PROXY=" in script
    assert 'Path("/etc/hosts").write_text' not in script


def test_launcher_mounts_all_secrets_and_uses_internal_network() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (
        root / "ops/internal_e2e_runner/run-confined-e2e.ps1"
    ).read_text(encoding="utf-8")
    assert "network create --internal" in script
    assert "--network\", $internalNetwork" in script
    assert "network connect $egressNetwork $proxyName" in script
    assert "network connect $egressNetwork $relayName" in script
    assert "/run/secrets/${name}:ro" in script
    assert "finally {" in script
    assert "dirty_worktree_rejected" in script


def test_anthropic_sdk_honors_https_proxy_hermetically(monkeypatch) -> None:
    anthropic = pytest.importorskip("anthropic")
    seen: list[bytes] = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            seen.append(self.request.recv(4096))
            self.request.sendall(
                b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n"
            )

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        monkeypatch.setenv(
            "HTTPS_PROXY", f"http://127.0.0.1:{server.server_address[1]}"
        )
        monkeypatch.delenv("NO_PROXY", raising=False)
        client = anthropic.Anthropic(
            api_key="test-not-a-secret",
            base_url="https://api.anthropic.com",
            max_retries=0,
            timeout=1,
        )
        with pytest.raises(Exception):
            client.messages.create(
                model="claude-test",
                max_tokens=1,
                messages=[{"role": "user", "content": "hermetic transport test"}],
            )
        thread.join(timeout=2)
    assert seen
    assert seen[0].startswith(b"CONNECT api.anthropic.com:443 ")
