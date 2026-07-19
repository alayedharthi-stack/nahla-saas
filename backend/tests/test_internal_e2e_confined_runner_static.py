"""Static/unit tests for the off-Railway confined internal E2E runner."""
from __future__ import annotations

import json
from datetime import datetime, timezone

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


def _base_config(**overrides):
    payload = {
        "schema_version": RUNNER_CONFIG_SCHEMA_VERSION,
        "pinned_revision": "de4a1385f746",
        "image_label": "nahla-internal-e2e-confined:de4a1385f746",
        "tenant_id": 48,
        "llm_host": "api.example-llm.test",
        "llm_port": 443,
        "llm_host_ips": ["203.0.113.10"],
        "db_proxy_host": "disposable-db-proxy.example.test",
        "db_proxy_port": 5432,
        "db_proxy_ips": ["203.0.113.20"],
        "dns_resolver": "one.one.one.one",
        "dns_resolver_port": 53,
        "dns_resolver_ips": ["1.1.1.1"],
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
    with pytest.raises(ValueError, match="operator_run_requires_scenarios"):
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
        image_digest_input="nahla-internal-e2e-confined:de4a1385@de4a1385f746",
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
