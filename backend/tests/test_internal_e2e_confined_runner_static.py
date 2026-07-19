"""Static/unit tests for the off-Railway confined internal E2E runner."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

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
from ops.internal_e2e_runner.scripts.assemble_evidence import (
    main as assemble_evidence_main,
    parse_operator_command_json,
    validate_negative_control_binding,
    validate_positive_probe_identity,
)
from ops.internal_e2e_runner.scripts.probe_connectivity import (
    positive_probes_ready,
    postgres_ssl_probe,
)
from ops.internal_e2e_runner.scripts.validate_config import (
    main as validate_config_main,
)
from ops.internal_e2e_runner.scripts.verify_docker_topology import (
    main as verify_docker_topology_main,
)


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
        "db_tls_spki_sha256": "sha256:" + ("a" * 64),
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
    assert config.db_tls_spki_sha256 == "sha256:" + ("a" * 64)
    assert config.to_public_mapping()["db_tls_spki_sha256"] == (
        "sha256:" + ("a" * 64)
    )
    assert len(config.negative_probe_targets) == 2


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "sha256:" + ("a" * 63),
        "sha256:" + ("A" * 64),
        "sha512:" + ("a" * 64),
        "sha256:not-hex",
    ],
)
def test_db_tls_spki_pin_is_required_and_canonical(value) -> None:
    config = _base_config()
    if value is None:
        config.pop("db_tls_spki_sha256")
    else:
        config["db_tls_spki_sha256"] = value
    with pytest.raises(ValueError, match="db_tls_spki_sha256_invalid"):
        parse_runner_config(config)


def test_high_public_db_port_is_preserved_across_relay_paths() -> None:
    config = parse_runner_config(
        _base_config(db_proxy_port=20736, db_relay_port=20736)
    )
    assert config.db_proxy_endpoint.port == 20736
    assert config.db_relay_port == 20736

    root = Path(__file__).resolve().parents[2]
    launcher = (
        root / "ops/internal_e2e_runner/run-confined-e2e.ps1"
    ).read_text(encoding="utf-8")
    entrypoint = (
        root / "ops/internal_e2e_runner/scripts/entrypoint.sh"
    ).read_text(encoding="utf-8")
    assert "--target-port $config.db_proxy_port" in launcher
    assert "--listen-port $config.db_relay_port" in launcher
    assert '"--add-host", "$($config.db_proxy_host):$($config.db_relay_ip)"' in launcher
    assert '--relay-port "${RELAY_PORT}"' in entrypoint
    assert (
        'export NAHLA_INTERNAL_E2E_DATABASE_URL="$(< /run/secrets/database_url)"'
        in entrypoint
    )


@pytest.mark.parametrize("field", ["db_proxy_port", "db_relay_port"])
def test_db_proxy_and_relay_ports_are_required(field: str) -> None:
    raw = _base_config()
    raw.pop(field)
    with pytest.raises(ValueError, match=f"{field}_invalid"):
        parse_runner_config(raw)


@pytest.mark.parametrize("field", ["db_proxy_port", "db_relay_port"])
@pytest.mark.parametrize("value", [0, 65536, "20736", True])
def test_db_proxy_and_relay_ports_reject_invalid_values(
    field: str,
    value,
) -> None:
    with pytest.raises(ValueError, match=f"{field}_invalid"):
        parse_runner_config(_base_config(**{field: value}))


def test_db_relay_port_must_match_public_proxy_port() -> None:
    with pytest.raises(ValueError, match="db_relay_port_mismatch"):
        parse_runner_config(
            _base_config(db_proxy_port=20736, db_relay_port=5432)
        )


def test_validate_config_main_accepts_valid_config_and_writes_output(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_path = tmp_path / "runner-config.json"
    database_url_path = tmp_path / "database-url"
    output_path = tmp_path / "validation.json"
    config_path.write_text(json.dumps(_base_config()), encoding="utf-8")
    database_url_path.write_text(
        "postgresql://user:secret@"
        "disposable-db-proxy.example.test:5432/sandbox?sslmode=require",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_config.py",
            "--config",
            str(config_path),
            "--database-url-file",
            str(database_url_path),
            "--output",
            str(output_path),
        ],
    )

    assert validate_config_main() == 0
    written = json.loads(output_path.read_text(encoding="utf-8"))
    printed = json.loads(capsys.readouterr().out)
    assert written["ok"] is True
    assert printed == written
    assert written["config"]["llm_host"] == "api.example-llm.test"
    assert "secret" not in output_path.read_text(encoding="utf-8")


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


def _negative_control_ids() -> list[str]:
    return [
        "graph.facebook.com|31.13.64.35|443",
        "blocked-probe.example.test|203.0.113.99|443",
    ]


def _assemble_evidence_fixture_paths(tmp_path: Path) -> dict[str, Path]:
    config_path = tmp_path / "runner-config.json"
    config_path.write_text(json.dumps(_base_config()), encoding="utf-8")
    capability_path = tmp_path / "capability_proof.json"
    capability_path.write_text(
        json.dumps({"cap_net_admin_required": True}), encoding="utf-8"
    )
    rules_path = tmp_path / "firewall_rules.sanitized"
    rules_path.write_text("-P OUTPUT DROP", encoding="utf-8")
    hosts_path = tmp_path / "hosts_pinning.json"
    hosts_path.write_text("{}", encoding="utf-8")
    probe_path = tmp_path / "probe_results.json"
    probe_path.write_text(
        json.dumps(
            {
                "positive": _identity_complete_positive_probes(),
                "proxy_negative": [],
                "direct_negative": [
                    {"control_id": control_id} for control_id in _negative_control_ids()
                ],
            }
        ),
        encoding="utf-8",
    )
    docker_path = tmp_path / "docker-topology-verified.json"
    docker_path.write_text(
        json.dumps(
            {
                "egress_control_baseline": {
                    "reachable": [
                        {"control_id": control_id}
                        for control_id in _negative_control_ids()
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "network_evidence.json"
    return {
        "config": config_path,
        "capability": capability_path,
        "rules": rules_path,
        "hosts": hosts_path,
        "probes": probe_path,
        "docker": docker_path,
        "output": output_path,
    }


def _assemble_evidence_argv(
  paths: dict[str, Path],
  *,
  operator_command_json: str,
) -> list[str]:
    return [
        "assemble_evidence.py",
        "--config",
        str(paths["config"]),
        "--started-at",
        "2026-07-19T12:00:00+00:00",
        "--completed-at",
        "2026-07-19T12:01:00+00:00",
        "--capability-proof",
        str(paths["capability"]),
        "--firewall-backend",
        "iptables",
        "--rules-dump",
        str(paths["rules"]),
        "--hosts-pinning",
        str(paths["hosts"]),
        "--probe-results",
        str(paths["probes"]),
        "--image-digest-input",
        f"nahla-internal-e2e-confined:{'d' * 40}@{'d' * 40}",
        "--database-url-fingerprint",
        database_url_fingerprint(
            "postgresql://u:p@disposable-db-proxy.example.test:5432/sandbox?sslmode=require"
        ),
        "--docker-inspect",
        str(paths["docker"]),
        "--output",
        str(paths["output"]),
        "--operator-command-json",
        operator_command_json,
    ]


def test_assemble_evidence_run_scenarios_json_bridge(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression for argparse collision on run --scenarios <path>."""
    paths = _assemble_evidence_fixture_paths(tmp_path)
    scenario_path = "/run/scenarios/scenarios.json"
    operator_json = json.dumps(["run", "--scenarios", scenario_path])
    monkeypatch.setattr(
        sys,
        "argv",
        _assemble_evidence_argv(
            paths,
            operator_command_json=operator_json,
        ),
    )

    assert assemble_evidence_main() == 0
    payload = json.loads(paths["output"].read_text(encoding="utf-8"))
    assert payload["operator_command"] == [
        "run",
        "--scenarios",
        scenario_path,
    ]
    assert "scenarios.json" not in json.dumps(
        payload.get("probe_results", {})
    )


def test_assemble_evidence_preflight_json_bridge(tmp_path: Path, monkeypatch) -> None:
    paths = _assemble_evidence_fixture_paths(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        _assemble_evidence_argv(
            paths,
            operator_command_json=json.dumps(["preflight"]),
        ),
    )

    assert assemble_evidence_main() == 0
    payload = json.loads(paths["output"].read_text(encoding="utf-8"))
    assert payload["operator_command"] == ["preflight"]


@pytest.mark.parametrize(
    ("operator_json", "expected_error"),
    [
        ("not-json", "operator_command_json_invalid"),
        ("{}", "operator_command_json_not_list"),
        ("[]", "operator_command_json_empty"),
        ('["preflight", 1]', "operator_command_json_not_strings"),
        ('["run"]', "operator_command_invalid"),
        ('["preflight", "--tenant-id", "99"]', "operator_command_invalid"),
        ('["run", "--scenarios", ""]', "operator_command_invalid"),
    ],
)
def test_assemble_evidence_operator_command_json_rejects_invalid(
    operator_json: str,
    expected_error: str,
) -> None:
    with pytest.raises(SystemExit, match=expected_error):
        parse_operator_command_json(operator_json)


def test_entrypoint_forwards_operator_command_as_single_json_argument() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (
        root / "ops/internal_e2e_runner/scripts/entrypoint.sh"
    ).read_text(encoding="utf-8")
    assert 'OPERATOR_COMMAND_JSON="$(python3 -c' in script
    assert 'print(json.dumps(sys.argv[1:]))' in script
    assert '"${NORMALIZED[@]}"' in script
    assert '--operator-command-json "${OPERATOR_COMMAND_JSON}"' in script
    assert '--operator-command "${NORMALIZED[@]}"' not in script


def test_assemble_evidence_cli_rejects_unknown_args(
    tmp_path: Path, monkeypatch
) -> None:
    paths = _assemble_evidence_fixture_paths(tmp_path)
    argv = _assemble_evidence_argv(
        paths,
        operator_command_json=json.dumps(["preflight"]),
    )
    argv.append("--unknown-flag")
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as excinfo:
        assemble_evidence_main()
    assert excinfo.value.code == 2


def test_assemble_evidence_cli_requires_operator_command_json(
    tmp_path: Path, monkeypatch
) -> None:
    paths = _assemble_evidence_fixture_paths(tmp_path)
    argv = [
        item
        for item in _assemble_evidence_argv(
            paths,
            operator_command_json=json.dumps(["preflight"]),
        )
        if item != "--operator-command-json"
        and not item.startswith("[")
    ]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as excinfo:
        assemble_evidence_main()
    assert excinfo.value.code == 2


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
    assert payload["sidecar_acl_targets"]["disposable_db_proxy"]["tls_identity"] == {
        "mode": "spki_sha256",
        "expected_spki_sha256": "sha256:" + ("a" * 64),
    }


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


def _direct_docker_inspect_fixture(revision: str) -> dict:
    internal = "nahla-e2e-internal-fixture"
    egress = "nahla-e2e-egress-fixture"

    def container(*networks: str, runner: bool = False) -> dict:
        payload = {
            "NetworkSettings": {
                "Networks": {network: {} for network in networks},
            },
            "HostConfig": {},
        }
        if runner:
            payload["HostConfig"] = {
                "CapAdd": ["CAP_NET_ADMIN"],
                "SecurityOpt": ["no-new-privileges:true"],
                "ReadonlyRootfs": True,
            }
        return payload

    def image(image_id: str, *, runner: bool = False) -> dict:
        config = {"Labels": {"nahla.pinned_revision": revision}}
        if runner:
            config["Env"] = [f"GIT_COMMIT_SHA={revision}"]
        return {
            "Id": image_id,
            "Config": config,
        }

    return {
        "runner_image": image("sha256:" + ("a" * 64), runner=True),
        "sidecar_image": image("sha256:" + ("b" * 64)),
        "runner": container(internal, runner=True),
        "connect_proxy": container(internal, egress),
        "db_relay": container(internal, egress),
        "internal_network": {"Name": internal, "Internal": True},
        "egress_network": {"Name": egress, "Internal": False},
    }


def test_launcher_produces_direct_single_object_docker_inspect_evidence() -> None:
    """Regression for Windows PowerShell 5.1 wrapping ConvertFrom-Json arrays."""
    root = Path(__file__).resolve().parents[2]
    launcher = (
        root / "ops/internal_e2e_runner/run-confined-e2e.ps1"
    ).read_text(encoding="utf-8")

    assert '$jsonLines = @(& docker @DockerArgs --format "{{json .}}")' in launcher
    assert '$jsonLines.Count -ne 1' in launcher
    assert "docker_inspect_cardinality_invalid" in launcher
    assert launcher.count("Get-DockerInspectObject -DockerArgs") == 7
    assert "runner_image = (& docker image inspect" not in launcher
    assert "internal_network = (& docker network inspect" not in launcher


def test_direct_docker_inspect_fixture_verifies_without_wrapper(
    tmp_path: Path, monkeypatch
) -> None:
    revision = "a" * 40
    inspect_path = tmp_path / "docker-inspect.json"
    baseline_path = tmp_path / "egress-control-baseline.json"
    output_path = tmp_path / "docker-topology-verified.json"
    inspect_path.write_text(
        json.dumps(_direct_docker_inspect_fixture(revision)),
        encoding="utf-8",
    )
    baseline_path.write_text(
        json.dumps(
            {
                "reachable": [
                    {"control_id": "control-a|203.0.113.10|443"},
                    {"control_id": "control-b|203.0.113.11|443"},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_docker_topology.py",
            "--inspect",
            str(inspect_path),
            "--egress-baseline",
            str(baseline_path),
            "--expected-revision",
            revision,
            "--output",
            str(output_path),
        ],
    )

    assert verify_docker_topology_main() == 0
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["verified"] is True
    assert result["image_revision_label"] == revision
    assert result["runner_build_attested_revision"] == revision
    assert result["runner_networks"] == ["nahla-e2e-internal-fixture"]


@pytest.mark.parametrize(
    ("env", "blocker"),
    [
        ([], "runner_image_git_commit_sha_missing"),
        (["GIT_COMMIT_SHA=" + ("b" * 40)], "runner_image_git_commit_sha_mismatch"),
        (
            ["GIT_COMMIT_SHA=" + ("a" * 40), "GIT_COMMIT_SHA=" + ("a" * 40)],
            "runner_image_git_commit_sha_ambiguous",
        ),
    ],
)
def test_runner_build_attested_revision_env_fails_closed(
    tmp_path: Path,
    monkeypatch,
    env: list[str],
    blocker: str,
) -> None:
    revision = "a" * 40
    inspect = _direct_docker_inspect_fixture(revision)
    inspect["runner_image"]["Config"]["Env"] = env
    inspect_path = tmp_path / "docker-inspect.json"
    baseline_path = tmp_path / "egress-control-baseline.json"
    inspect_path.write_text(json.dumps(inspect), encoding="utf-8")
    baseline_path.write_text(
        json.dumps({"reachable": [{"id": "a"}, {"id": "b"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_docker_topology.py",
            "--inspect",
            str(inspect_path),
            "--egress-baseline",
            str(baseline_path),
            "--expected-revision",
            revision,
            "--output",
            str(tmp_path / "should-not-exist.json"),
        ],
    )

    with pytest.raises(SystemExit, match=blocker):
        verify_docker_topology_main()


def test_runner_revision_is_build_baked_and_not_launcher_injected() -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = (
        root / "ops/internal_e2e_runner/Dockerfile"
    ).read_text(encoding="utf-8")
    launcher = (
        root / "ops/internal_e2e_runner/run-confined-e2e.ps1"
    ).read_text(encoding="utf-8")

    assert "ARG NAHLA_PINNED_REVISION" in dockerfile
    assert "ENV GIT_COMMIT_SHA=${NAHLA_PINNED_REVISION}" in dockerfile
    assert "GIT_COMMIT_SHA" not in launcher


def test_powershell_value_count_wrapper_remains_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    revision = "a" * 40
    inspect = _direct_docker_inspect_fixture(revision)
    inspect["internal_network"] = {
        "value": [inspect["internal_network"]],
        "Count": 1,
    }
    inspect_path = tmp_path / "docker-inspect-wrapped.json"
    baseline_path = tmp_path / "egress-control-baseline.json"
    inspect_path.write_text(json.dumps(inspect), encoding="utf-8")
    baseline_path.write_text(
        json.dumps({"reachable": [{"id": "a"}, {"id": "b"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_docker_topology.py",
            "--inspect",
            str(inspect_path),
            "--egress-baseline",
            str(baseline_path),
            "--expected-revision",
            revision,
            "--output",
            str(tmp_path / "should-not-exist.json"),
        ],
    )

    with pytest.raises(ValueError, match="docker_inspect_wrapper_rejected"):
        verify_docker_topology_main()


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


@pytest.mark.parametrize(
    ("endpoint", "expected_ips"),
    [
        (
            ExactConnectProxy(
                "api.anthropic.com",
                {"203.0.113.10", "203.0.113.11"},
                Path("proxy.jsonl"),
            ),
            ["203.0.113.10", "203.0.113.11"],
        ),
        (
            ExactDbRelay(
                "disposable.example.test",
                5432,
                {"203.0.113.20", "203.0.113.21"},
                Path("relay.jsonl"),
            ),
            ["203.0.113.20", "203.0.113.21"],
        ),
    ],
)
def test_sidecars_verify_complete_ipv4_set_and_ignore_aaaa(
    endpoint, expected_ips: list[str]
) -> None:
    dual_stack_answers = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (expected_ips[0], 443)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2607:6bc0::10", 443, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (expected_ips[1], 443)),
    ]

    def family_filtered_dns(host, port, *, family, type):
        assert family == socket.AF_INET
        assert type == socket.SOCK_STREAM
        return [answer for answer in dual_stack_answers if answer[0] == family]

    with patch("socket.getaddrinfo", side_effect=family_filtered_dns) as resolver:
        assert endpoint.resolve_verified() == expected_ips
    resolver.assert_called_once()


def test_connect_proxy_accepts_observed_anthropic_dual_stack_dns() -> None:
    """Regression for Docker resolving api.anthropic.com to A plus AAAA."""
    endpoint = ExactConnectProxy(
        "api.anthropic.com",
        {"160.79.104.10"},
        Path("proxy.jsonl"),
    )
    docker_answers = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("160.79.104.10", 443)),
        (
            socket.AF_INET6,
            socket.SOCK_STREAM,
            6,
            "",
            ("2607:6bc0::10", 443, 0, 0),
        ),
    ]

    def docker_dns(host, port, *, family, type):
        assert host == "api.anthropic.com"
        assert family == socket.AF_INET
        assert type == socket.SOCK_STREAM
        return [answer for answer in docker_answers if answer[0] == family]

    with patch("socket.getaddrinfo", side_effect=docker_dns):
        assert endpoint.resolve_verified() == ["160.79.104.10"]


@pytest.mark.parametrize(
    ("endpoint", "live_ips", "error"),
    [
        (
            ExactConnectProxy(
                "api.anthropic.com",
                {"203.0.113.10", "203.0.113.11"},
                Path("proxy.jsonl"),
            ),
            ["203.0.113.10"],
            "llm_live_dns_mismatch",
        ),
        (
            ExactConnectProxy(
                "api.anthropic.com",
                {"203.0.113.10"},
                Path("proxy.jsonl"),
            ),
            ["203.0.113.10", "203.0.113.11"],
            "llm_live_dns_mismatch",
        ),
        (
            ExactDbRelay(
                "disposable.example.test",
                5432,
                {"203.0.113.20", "203.0.113.21"},
                Path("relay.jsonl"),
            ),
            ["203.0.113.20"],
            "db_live_dns_mismatch",
        ),
        (
            ExactDbRelay(
                "disposable.example.test",
                5432,
                {"203.0.113.20"},
                Path("relay.jsonl"),
            ),
            ["203.0.113.20", "203.0.113.21"],
            "db_live_dns_mismatch",
        ),
    ],
)
def test_sidecars_fail_closed_on_ipv4_addition_or_removal(
    endpoint, live_ips: list[str], error: str
) -> None:
    fake_dns = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
        for ip in live_ips
    ]
    with patch("socket.getaddrinfo", return_value=fake_dns) as resolver:
        with pytest.raises(RuntimeError, match=error):
            endpoint.resolve_verified()
    assert resolver.call_args.kwargs["family"] == socket.AF_INET


@pytest.mark.parametrize(
    ("kind", "factory", "expected_port"),
    [
        (
            "llm",
            lambda path: ExactConnectProxy(
                "api.anthropic.com", {"203.0.113.10"}, path
            ),
            443,
        ),
        (
            "db",
            lambda path: ExactDbRelay(
                "disposable.example.test", 5432, {"203.0.113.20"}, path
            ),
            5432,
        ),
    ],
)
def test_sidecar_transport_uses_verified_ip_not_hostname(
    tmp_path: Path, kind: str, factory, expected_port: int
) -> None:
    expected_ip = "203.0.113.10" if kind == "llm" else "203.0.113.20"
    endpoint = factory(tmp_path / f"{kind}.jsonl")
    dns_calls = 0

    def changing_dns(*args, **kwargs):
        nonlocal dns_calls
        dns_calls += 1
        assert kwargs["family"] == socket.AF_INET
        assert kwargs["type"] == socket.SOCK_STREAM
        ip = expected_ip if dns_calls == 1 else "198.51.100.99"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, expected_port))]

    open_connection = AsyncMock(return_value=(object(), object()))
    with patch("socket.getaddrinfo", side_effect=changing_dns), patch(
        "asyncio.open_connection", open_connection
    ):
        selected_ip, _, _ = asyncio.run(endpoint.open_verified_upstream())

    assert selected_ip == expected_ip
    open_connection.assert_awaited_once_with(expected_ip, expected_port)
    assert endpoint.host not in open_connection.await_args.args
    assert all(":" not in str(arg) for arg in open_connection.await_args.args)
    assert dns_calls == 1


def _localhost_certificate(
    *,
    not_before: datetime,
    not_after: datetime,
) -> tuple[bytes, str]:
    root_key = ec.generate_private_key(ec.SECP256R1())
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "root-ca")])
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(root_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )
    spki = leaf_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return (
        certificate.public_bytes(serialization.Encoding.DER),
        "sha256:" + hashlib.sha256(spki).hexdigest(),
    )


def _run_postgres_certificate_probe(
    peer_der: bytes,
    expected_pin: str,
    *,
    now: datetime,
    ssl_response: bytes = b"S",
) -> tuple[dict, MagicMock, MagicMock]:
    raw_socket = MagicMock()
    raw_socket.recv.return_value = ssl_response
    raw_context = MagicMock()
    raw_context.__enter__.return_value = raw_socket

    tls_socket = MagicMock()
    tls_socket.version.return_value = "TLSv1.3"
    tls_socket.getpeercert.return_value = peer_der
    tls_context_manager = MagicMock()
    tls_context_manager.__enter__.return_value = tls_socket
    tls_context = MagicMock()
    tls_context.wrap_socket.return_value = tls_context_manager

    with patch(
        "ops.internal_e2e_runner.scripts.probe_connectivity.socket.create_connection",
        return_value=raw_context,
    ), patch(
        "ops.internal_e2e_runner.scripts.probe_connectivity.ssl.SSLContext",
        return_value=tls_context,
    ) as context_factory:
        result = postgres_ssl_probe(
            relay_host="172.30.0.11",
            relay_port=5432,
            tls_hostname="maglev.proxy.rlwy.net",
            expected_spki_sha256=expected_pin,
            now=now,
        )

    if ssl_response == b"S":
        context_factory.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
        assert tls_context.check_hostname is False
        assert tls_context.verify_mode == ssl.CERT_NONE
    else:
        context_factory.assert_not_called()
    return result, raw_socket, tls_context


def test_railway_localhost_certificate_passes_only_with_explicit_spki() -> None:
    now = datetime(2026, 7, 19, 16, 0, tzinfo=timezone.utc)
    peer_der, pin = _localhost_certificate(
        not_before=now - timedelta(hours=1),
        not_after=now + timedelta(days=1),
    )

    result, raw_socket, tls_context = _run_postgres_certificate_probe(
        peer_der,
        pin,
        now=now,
    )

    raw_socket.sendall.assert_called_once_with(struct.pack("!II", 8, 80877103))
    tls_context.wrap_socket.assert_called_once_with(
        raw_socket,
        server_hostname="maglev.proxy.rlwy.net",
    )
    assert result["sslrequest_accepted"] is True
    assert result["tls_ok"] is True
    assert result["identity_verification_mode"] == "spki_sha256"
    assert result["certificate_pin_verified"] is True
    assert result["certificate_validity_verified"] is True
    assert result["authentication_sent"] is False
    assert result["query_sent"] is False
    assert "hostname_verified" not in result
    encoded = json.dumps(result)
    assert "BEGIN CERTIFICATE" not in encoded
    assert peer_der.hex() not in encoded


def test_postgres_certificate_wrong_spki_fails_closed() -> None:
    now = datetime(2026, 7, 19, 16, 0, tzinfo=timezone.utc)
    peer_der, _ = _localhost_certificate(
        not_before=now - timedelta(hours=1),
        not_after=now + timedelta(days=1),
    )
    result, _, _ = _run_postgres_certificate_probe(
        peer_der,
        "sha256:" + ("f" * 64),
        now=now,
    )
    assert result["tls_ok"] is True
    assert result["certificate_validity_verified"] is True
    assert result["certificate_pin_verified"] is False
    assert result["error"] == "certificate_spki_mismatch"


@pytest.mark.parametrize(
    ("not_before", "not_after", "error"),
    [
        (
            datetime(2026, 7, 17, tzinfo=timezone.utc),
            datetime(2026, 7, 18, tzinfo=timezone.utc),
            "certificate_expired",
        ),
        (
            datetime(2026, 7, 20, tzinfo=timezone.utc),
            datetime(2026, 7, 21, tzinfo=timezone.utc),
            "certificate_not_yet_valid",
        ),
    ],
)
def test_postgres_certificate_invalid_time_fails_closed(
    not_before: datetime,
    not_after: datetime,
    error: str,
) -> None:
    now = datetime(2026, 7, 19, 16, 0, tzinfo=timezone.utc)
    peer_der, pin = _localhost_certificate(
        not_before=not_before,
        not_after=not_after,
    )
    result, _, _ = _run_postgres_certificate_probe(peer_der, pin, now=now)
    assert result["tls_ok"] is True
    assert result["certificate_validity_verified"] is False
    assert result["certificate_pin_verified"] is False
    assert result["error"] == error


def test_postgres_malformed_certificate_and_no_sslrequest_fail_closed() -> None:
    now = datetime(2026, 7, 19, 16, 0, tzinfo=timezone.utc)
    malformed, _, _ = _run_postgres_certificate_probe(
        b"not-a-certificate",
        "sha256:" + ("a" * 64),
        now=now,
    )
    assert malformed["error"] == "certificate_malformed"
    assert malformed["certificate_pin_verified"] is False

    rejected, _, tls_context = _run_postgres_certificate_probe(
        b"",
        "sha256:" + ("a" * 64),
        now=now,
        ssl_response=b"N",
    )
    assert rejected["sslrequest_accepted"] is False
    assert rejected["error"] == "postgres_ssl_not_supported"
    tls_context.wrap_socket.assert_not_called()


def _identity_complete_positive_probes() -> list[dict]:
    return [
        {
            "kind": "https_connect_tls",
            "tls_ok": True,
            "hostname_verified": True,
        },
        {
            "kind": "postgres_sslrequest_tls",
            "sslrequest_accepted": True,
            "tls_ok": True,
            "identity_verification_mode": "spki_sha256",
            "certificate_pin_verified": True,
            "certificate_validity_verified": True,
            "authentication_sent": False,
            "query_sent": False,
        },
    ]


def test_probe_readiness_requires_database_pin_and_validity_evidence() -> None:
    complete = _identity_complete_positive_probes()
    assert positive_probes_ready(complete) is True
    validate_positive_probe_identity(complete)

    for field in ("certificate_pin_verified", "certificate_validity_verified"):
        incomplete = json.loads(json.dumps(complete))
        incomplete[1][field] = False
        assert positive_probes_ready(incomplete) is False
        with pytest.raises(ValueError, match="positive_probe_identity_incomplete"):
            validate_positive_probe_identity(incomplete)


def test_negative_control_binding_requires_exact_multiplicity() -> None:
    expected = [
        "control-a.example|203.0.113.31|443",
        "control-b.example|203.0.113.32|8443",
    ]
    validate_negative_control_binding(expected, list(expected), list(expected))
    with pytest.raises(ValueError, match="negative_probe_control_binding_mismatch"):
        validate_negative_control_binding(expected, expected[:1], list(expected))
    with pytest.raises(ValueError, match="negative_probe_control_binding_mismatch"):
        validate_negative_control_binding(expected, list(expected), [expected[0]] * 2)


def test_negative_controls_come_only_from_config() -> None:
    root = Path(__file__).resolve().parents[2]
    launcher = (
        root / "ops/internal_e2e_runner/run-confined-e2e.ps1"
    ).read_text(encoding="utf-8")
    entrypoint = (
        root / "ops/internal_e2e_runner/scripts/entrypoint.sh"
    ).read_text(encoding="utf-8")
    assert "$config.negative_probe_targets" in launcher
    assert 'c["negative_probe_targets"]' in entrypoint
    assert "1.1.1.1" not in launcher + entrypoint
    assert "8.8.8.8" not in launcher + entrypoint


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
    assert "EXPECTED_RULES=" not in script
    assert "verify_iptables_output_rules" in script
    assert '"${LIVE_NFT}" != *"policy drop"*' in script
    assert '"${NFT_ACCEPT_COUNT}" -ne 4' in script


_IPTABLES_POLICY = """\
-P OUTPUT DROP
-A OUTPUT -o lo -j ACCEPT
-A OUTPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
"""
_PROXY_RULE = (
    "-A OUTPUT -d 172.30.0.10/32 -p tcp -m tcp --dport 3128 -j ACCEPT"
)
_RELAY_RULE = (
    "-A OUTPUT -d 172.30.0.11/32 -p tcp -m tcp --dport 5432 -j ACCEPT"
)
_PROXY_CHECK = "-p tcp -d 172.30.0.10 --dport 3128 -j ACCEPT"
_RELAY_CHECK = "-p tcp -d 172.30.0.11 --dport 5432 -j ACCEPT"


def _run_iptables_verifier(
    *,
    rules: list[str],
    matching_checks: list[str],
) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if os.name == "nt":
        for candidate in (
            Path(r"C:\Program Files\Git\bin\bash.exe"),
            Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
        ):
            if candidate.is_file():
                bash = str(candidate)
                break
    if bash is None:
        pytest.skip("bash unavailable")
    root = Path(__file__).resolve().parents[2]
    helper = (
        root
        / "ops/internal_e2e_runner/scripts/verify_iptables_output.sh"
    ).as_posix()
    command = r"""
source "$1"
iptables() {
  if [[ "$1" == "-S" && "$2" == "OUTPUT" ]]; then
    printf '%s\n' "${FAKE_RULES}"
    return 0
  fi
  if [[ "$1" == "-C" && "$2" == "OUTPUT" ]]; then
    shift 2
    printf '%s\n' "${MATCHING_CHECKS}" | grep -Fxq -- "$*"
    return
  fi
  return 2
}
verify_iptables_output_rules 172.30.0.10 3128 172.30.0.11 5432
"""
    env = {
        **os.environ,
        "FAKE_RULES": _IPTABLES_POLICY + "\n".join(rules),
        "MATCHING_CHECKS": "\n".join(matching_checks),
    }
    return subprocess.run(
        [bash, "-c", command, "_", helper],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_iptables_semantic_verification_accepts_canonical_rendering() -> None:
    result = _run_iptables_verifier(
        rules=[_PROXY_RULE, _RELAY_RULE],
        matching_checks=[_PROXY_CHECK, _RELAY_CHECK],
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("rules", "matching_checks", "expected_error"),
    [
        (
            [_RELAY_RULE],
            [_RELAY_CHECK],
            "firewall_verification_failed",
        ),
        (
            [
                "-A OUTPUT -p tcp -m tcp --dport 3128 -j ACCEPT",
                _RELAY_RULE,
            ],
            [_RELAY_CHECK],
            "firewall_verification_failed",
        ),
        (
            [
                "-A OUTPUT -d 172.30.0.10/32 -p tcp -m tcp "
                "--dport 3129 -j ACCEPT",
                _RELAY_RULE,
            ],
            [_RELAY_CHECK],
            "firewall_verification_failed",
        ),
        (
            [
                _PROXY_RULE,
                _RELAY_RULE,
                "-A OUTPUT -d 203.0.113.9/32 -p tcp -m tcp "
                "--dport 443 -j ACCEPT",
            ],
            [_PROXY_CHECK, _RELAY_CHECK],
            "firewall_unexpected_accept_rule",
        ),
    ],
    ids=["missing", "wider-destination", "wrong-port", "extra-accept"],
)
def test_iptables_semantic_verification_fails_closed(
    rules: list[str],
    matching_checks: list[str],
    expected_error: str,
) -> None:
    result = _run_iptables_verifier(
        rules=rules,
        matching_checks=matching_checks,
    )
    assert result.returncode != 0
    assert expected_error in result.stderr


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
