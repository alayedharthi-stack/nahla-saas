#!/usr/bin/env python3
"""Assemble unsigned network evidence from firewall state and probe results."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from ops.internal_e2e_runner.lib.config import load_runner_config, normalize_operator_command
from ops.internal_e2e_runner.lib.evidence import build_network_evidence, write_network_evidence
from ops.internal_e2e_runner.scripts.probe_connectivity import positive_probes_ready


def parse_operator_command_json(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit("operator_command_json_invalid") from exc
    if not isinstance(value, list):
        raise SystemExit("operator_command_json_not_list")
    if not value:
        raise SystemExit("operator_command_json_empty")
    if not all(isinstance(item, str) for item in value):
        raise SystemExit("operator_command_json_not_strings")
    try:
        return normalize_operator_command(value)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def validate_negative_control_binding(
    expected: list[str], baseline: list[str], runner: list[str]
) -> None:
    if (
        not expected
        or Counter(expected) != Counter(baseline)
        or Counter(expected) != Counter(runner)
    ):
        raise ValueError("negative_probe_control_binding_mismatch")


def validate_positive_probe_identity(probes: list[dict]) -> None:
    if not positive_probes_ready(probes):
        raise ValueError("positive_probe_identity_incomplete")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--completed-at", default="")
    parser.add_argument("--capability-proof", required=True)
    parser.add_argument("--firewall-backend", required=True)
    parser.add_argument("--rules-dump", required=True)
    parser.add_argument("--hosts-pinning", required=True)
    parser.add_argument("--probe-results", required=True)
    parser.add_argument("--image-digest-input", required=True)
    parser.add_argument("--database-url-fingerprint", required=True)
    parser.add_argument("--operator-command-json", required=True)
    parser.add_argument("--runtime-verification-status", default="pending_container_runtime")
    parser.add_argument("--docker-inspect", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    config = load_runner_config(args.config)
    with open(args.capability_proof, encoding="utf-8") as handle:
        capability_proof = json.load(handle)
    with open(args.rules_dump, encoding="utf-8") as handle:
        rules_dump = handle.read()
    with open(args.hosts_pinning, encoding="utf-8") as handle:
        hosts_pinning = json.load(handle)
    with open(args.probe_results, encoding="utf-8") as handle:
        probe_results = json.load(handle)
    with open(args.docker_inspect, encoding="utf-8-sig") as handle:
        docker_topology = json.load(handle)

    try:
        validate_positive_probe_identity(probe_results.get("positive") or [])
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    expected_controls = [
        f"{target.hostname}|{ip}|{target.port}"
        for target in config.negative_probe_targets
        for ip in target.ips
    ]
    baseline_controls = [
        str(item.get("control_id") or "")
        for item in (
            (docker_topology.get("egress_control_baseline") or {}).get("reachable")
            or []
        )
    ]
    runner_controls = [
        str(item.get("control_id") or "")
        for item in (probe_results.get("direct_negative") or [])
    ]
    try:
        validate_negative_control_binding(
            expected_controls, baseline_controls, runner_controls
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    started = datetime.fromisoformat(args.started_at.replace("Z", "+00:00"))
    completed_raw = args.completed_at or datetime.now(timezone.utc).isoformat()
    completed = datetime.fromisoformat(completed_raw.replace("Z", "+00:00"))

    payload = build_network_evidence(
        config=config,
        started_at_utc=started,
        completed_at_utc=completed,
        capability_proof=capability_proof,
        firewall_backend=args.firewall_backend,
        rules_dump_sanitized=rules_dump,
        hosts_pinning=hosts_pinning,
        positive_probes=probe_results.get("positive") or [],
        negative_probes=(
            list(probe_results.get("proxy_negative") or [])
            + list(probe_results.get("direct_negative") or [])
        ),
        image_digest_input=args.image_digest_input,
        database_url_fingerprint=args.database_url_fingerprint,
        operator_command=parse_operator_command_json(args.operator_command_json),
        runtime_verification_status=args.runtime_verification_status,
        docker_topology=docker_topology,
    )
    write_network_evidence(args.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
