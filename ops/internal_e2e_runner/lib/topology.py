"""Closed topology and invocation contract for the confined E2E runner."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_SECRET_FILES = {
    "database_url": "NAHLA_INTERNAL_E2E_DATABASE_URL",
    "evidence_hmac_key": "NAHLA_INTERNAL_E2E_EVIDENCE_HMAC_KEY",
    "attestation_hmac_key": "NAHLA_INTERNAL_E2E_ATTESTATION_HMAC_KEY",
    "attestation_json": "NAHLA_INTERNAL_E2E_ATTESTATION_JSON",
    "attestation_signature": "NAHLA_INTERNAL_E2E_ATTESTATION_SIGNATURE",
    "network_confirm": "NAHLA_INTERNAL_E2E_NETWORK_FIREWALL_CONFIRM",
    "llm_api_key": "ANTHROPIC_API_KEY",
    "tenant_allowlist": "NAHLA_INTERNAL_E2E_TENANT_ALLOWLIST",
    "test_phone": "NAHLA_INTERNAL_E2E_TEST_PHONE",
    "phone_allowlist": "NAHLA_INTERNAL_E2E_PHONE_ALLOWLIST",
}
OTHER_PROVIDER_KEYS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "MISTRAL_API_KEY",
    "COHERE_API_KEY",
    "GROQ_API_KEY",
)


@dataclass(frozen=True)
class OperatorCommand:
    mode: str
    scenarios: str | None = None

    def argv(self, tenant_id: int) -> list[str]:
        result = [self.mode, "--tenant-id", str(tenant_id)]
        if self.scenarios is not None:
            result.extend(["--scenarios", self.scenarios])
        return result

    def evidence(self) -> dict[str, str]:
        result = {"mode": self.mode}
        if self.scenarios is not None:
            result["scenarios_sha256"] = sha256_file(self.scenarios)
        return result


def normalize_operator_command(argv: Sequence[str] | None) -> OperatorCommand:
    tokens = list(argv or ())
    if not tokens:
        return OperatorCommand("preflight")
    if tokens == ["preflight"]:
        return OperatorCommand("preflight")
    if len(tokens) == 3 and tokens[0] == "run" and tokens[1] == "--scenarios":
        scenario = Path(tokens[2])
        if not scenario.is_file():
            raise ValueError("operator_scenarios_file_missing")
        return OperatorCommand("run", str(scenario))
    raise ValueError("operator_command_invalid")


def validate_revision_binding(
    *, config_sha: str, checkout_sha: str, image_label_sha: str, baked_sha: str
) -> list[str]:
    values = {
        "config": config_sha,
        "checkout": checkout_sha,
        "image_label": image_label_sha,
        "baked": baked_sha,
    }
    blockers = [
        f"{name}_revision_invalid"
        for name, value in values.items()
        if not FULL_SHA_RE.fullmatch(str(value or "").lower())
    ]
    if not blockers and len({value.lower() for value in values.values()}) != 1:
        blockers.append("revision_binding_mismatch")
    return blockers


def validate_topology(
    *,
    runner_networks: Sequence[str],
    proxy_networks: Sequence[str],
    relay_networks: Sequence[str],
    internal_network: str,
    egress_network: str,
) -> list[str]:
    blockers: list[str] = []
    if set(runner_networks) != {internal_network}:
        blockers.append("runner_must_only_attach_internal_network")
    expected_sidecar = {internal_network, egress_network}
    if set(proxy_networks) != expected_sidecar:
        blockers.append("connect_proxy_topology_invalid")
    if set(relay_networks) != expected_sidecar:
        blockers.append("db_relay_topology_invalid")
    return blockers


def validate_secret_files(paths: Mapping[str, str]) -> list[str]:
    blockers: list[str] = []
    if set(paths) != set(REQUIRED_SECRET_FILES):
        blockers.append("required_secret_file_set_invalid")
    for name in REQUIRED_SECRET_FILES:
        path = Path(paths.get(name, ""))
        if not path.is_file():
            blockers.append(f"secret_file_missing:{name}")
        elif path.stat().st_size == 0:
            blockers.append(f"secret_file_empty:{name}")
    return blockers


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_inspect(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
