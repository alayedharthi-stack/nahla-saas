#!/usr/bin/env python3
"""Fail closed unless Docker inspect proves the required network topology."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from ops.internal_e2e_runner.lib.topology import validate_topology


def _one(value):
    if isinstance(value, dict) and set(value) == {"value", "Count"}:
        raise ValueError("docker_inspect_wrapper_rejected")
    if isinstance(value, list):
        if len(value) != 1:
            raise ValueError("docker_inspect_cardinality_invalid")
        return value[0]
    return value


def _networks(container) -> list[str]:
    return sorted(_one(container)["NetworkSettings"]["Networks"].keys())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", required=True)
    parser.add_argument("--egress-baseline", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    raw_bytes = Path(args.inspect).read_bytes()
    raw = json.loads(raw_bytes.decode("utf-8-sig"))
    baseline = json.loads(
        Path(args.egress_baseline).read_text(encoding="utf-8-sig")
    )
    if len(baseline.get("reachable") or []) < 2:
        raise SystemExit("egress_control_baseline_incomplete")
    internal = _one(raw["internal_network"])["Name"]
    egress = _one(raw["egress_network"])["Name"]
    runner_networks = _networks(raw["runner"])
    proxy_networks = _networks(raw["connect_proxy"])
    relay_networks = _networks(raw["db_relay"])
    runner = _one(raw["runner"])
    blockers = validate_topology(
        runner_networks=runner_networks,
        proxy_networks=proxy_networks,
        relay_networks=relay_networks,
        internal_network=internal,
        egress_network=egress,
    )
    if blockers:
        raise SystemExit("docker_topology_invalid:" + ",".join(blockers))
    host_config = runner.get("HostConfig") or {}
    if "CAP_NET_ADMIN" not in (host_config.get("CapAdd") or []):
        raise SystemExit("runner_net_admin_capability_missing")
    if "no-new-privileges:true" not in (host_config.get("SecurityOpt") or []):
        raise SystemExit("runner_no_new_privileges_missing")
    if host_config.get("ReadonlyRootfs") is not True:
        raise SystemExit("runner_read_only_rootfs_missing")
    runner_image = _one(raw["runner_image"])
    sidecar_image = _one(raw["sidecar_image"])
    runner_revision = (
        (runner_image.get("Config") or {}).get("Labels") or {}
    ).get("nahla.pinned_revision")
    sidecar_revision = (
        (sidecar_image.get("Config") or {}).get("Labels") or {}
    ).get("nahla.pinned_revision")
    if (
        runner_revision != sidecar_revision
        or runner_revision != args.expected_revision
    ):
        raise SystemExit("image_revision_labels_mismatch")
    result = {
        "verified": True,
        "runner_networks": runner_networks,
        "connect_proxy_networks": proxy_networks,
        "db_relay_networks": relay_networks,
        "internal_network_internal_flag": bool(
            _one(raw["internal_network"]).get("Internal")
        ),
        "docker_inspect_hash_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "runner_image_id": runner_image["Id"],
        "sidecar_image_id": sidecar_image["Id"],
        "image_revision_label": runner_revision,
        "egress_control_baseline": baseline,
    }
    if not result["internal_network_internal_flag"]:
        raise SystemExit("internal_network_flag_missing")
    Path(args.output).write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
