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
    hosts_pinning: Mapping[str, Sequence[str]],
    positive_probes: Sequence[Mapping[str, Any]],
    negative_probes: Sequence[Mapping[str, Any]],
    image_digest_input: str,
    database_url_fingerprint: str,
    operator_command: Sequence[str],
    runtime_verification_status: str,
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
        "allowed_endpoints": {
            "llm": {
                "host": config.llm_endpoint.hostname,
                "port": config.llm_endpoint.port,
                "ips": list(config.llm_endpoint.ips),
            },
            "db_proxy": {
                "host": config.db_proxy_endpoint.hostname,
                "port": config.db_proxy_endpoint.port,
                "ips": list(config.db_proxy_endpoint.ips),
            },
            "dns_resolver": {
                "host": config.dns_resolver.hostname,
                "port": config.dns_resolver.port,
                "ips": list(config.dns_resolver.ips),
            },
        },
        "hosts_pinning": {
            host: list(ips) for host, ips in hosts_pinning.items()
        },
        "dns_posture": {
            "strategy": "pre_resolve_then_etc_hosts_pinning",
            "dns_egress_removed": True,
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
    }
    payload["evidence_hash_sha256"] = sha256_text(_canonical(payload))
    return payload


def write_network_evidence(path: str, payload: Mapping[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
