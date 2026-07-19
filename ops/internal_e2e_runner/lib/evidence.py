"""Unsigned network evidence artifact builder for external attestation review."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ops.internal_e2e_runner.lib.config import (
    EVIDENCE_SCHEMA_VERSION,
    RunnerConfig,
    redact_secrets,
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_network_evidence(
    *,
    config: RunnerConfig,
    started_at_utc: datetime,
    completed_at_utc: datetime,
    capability_proof: Mapping[str, Any],
    firewall_backend: str,
    rules_dump_sanitized: str,
    hosts_pinning: Mapping[str, Any],
    positive_probes: Sequence[Mapping[str, Any]],
    negative_probes: Sequence[Mapping[str, Any]],
    image_digest_input: str,
    database_url_fingerprint: str,
    operator_command: Sequence[str],
    runtime_verification_status: str,
    docker_topology: Mapping[str, Any] | None = None,
    operator_exit_status: int | None = None,
) -> dict[str, Any]:
    sanitized_rules = redact_secrets(rules_dump_sanitized)
    payload: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "network_policy": "default_deny",
        "attestation_status": "unsigned_pending_external_signer",
        "runtime_verification_status": runtime_verification_status,
        "started_at_utc": started_at_utc.astimezone(timezone.utc).isoformat(),
        "completed_at_utc": completed_at_utc.astimezone(timezone.utc).isoformat(),
        "pinned_revision": config.pinned_revision,
        "image_label": config.image_label,
        "image_digest_input": image_digest_input,
        "database_url_fingerprint": database_url_fingerprint,
        "tenant_id": config.tenant_id,
        "capability_proof": dict(capability_proof),
        "firewall_backend": firewall_backend,
        "rules_hash_sha256": sha256_text(sanitized_rules),
        "rules_dump_sanitized": sanitized_rules,
        "runner_firewall_destinations": {
            "connect_proxy": {
                "ip": config.connect_proxy_ip,
                "port": config.connect_proxy_port,
            },
            "db_relay": {
                "ip": config.db_relay_ip,
                "port": config.db_relay_port,
            },
        },
        "sidecar_acl_targets": {
            "llm_connect": {
                "host": config.llm_endpoint.hostname,
                "port": config.llm_endpoint.port,
                "expected_live_dns_ips": list(config.llm_endpoint.ips),
            },
            "disposable_db_proxy": {
                "host": config.db_proxy_endpoint.hostname,
                "port": config.db_proxy_endpoint.port,
                "expected_live_dns_ips": list(config.db_proxy_endpoint.ips),
            },
        },
        "hosts_pinning": dict(hosts_pinning),
        "dns_posture": {
            "strategy": "sidecars_resolve_and_verify_then_runner_uses_internal_endpoints",
            "runner_dns_egress": False,
            "ttl_limitation": (
                "Pinned /etc/hosts entries do not track upstream TTL changes; "
                "evidence is valid only for the confinement window captured "
                "between started_at_utc and completed_at_utc."
            ),
        },
        "probe_results": {
            "positive": list(positive_probes),
            "negative": list(negative_probes),
        },
        "operator_command": list(operator_command),
        "self_attestation_forbidden": True,
        "docker_topology": dict(docker_topology or {}),
        "docker_topology_hash_sha256": sha256_text(
            _canonical(dict(docker_topology or {}))
        ),
        "operator_exit_status": operator_exit_status,
    }
    payload["evidence_hash_sha256"] = sha256_text(_canonical(payload))
    return payload


def write_network_evidence(path: str, payload: Mapping[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
