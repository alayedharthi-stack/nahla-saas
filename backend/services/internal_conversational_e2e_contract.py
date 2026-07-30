"""Closed contract for disposable internal conversational E2E execution."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


CONTRACT_VERSION = "internal_conversational_e2e_v1"
EVIDENCE_SCHEMA_VERSION = "internal_conversational_e2e_evidence_v2"
EVIDENCE_SIGNATURE_SCHEMA_VERSION = "internal_conversational_e2e_signature_v1"
EVIDENCE_CHANNEL = "direct_code_probe"

MASTER_ENABLE_ENV = "NAHLA_INTERNAL_E2E_ENABLED"
EXECUTION_CONFIRM_ENV = "NAHLA_INTERNAL_E2E_CONFIRM"
DATABASE_URL_ENV = "NAHLA_INTERNAL_E2E_DATABASE_URL"
TENANT_ALLOWLIST_ENV = "NAHLA_INTERNAL_E2E_TENANT_ALLOWLIST"
TEST_PHONE_ENV = "NAHLA_INTERNAL_E2E_TEST_PHONE"
PHONE_ALLOWLIST_ENV = "NAHLA_INTERNAL_E2E_PHONE_ALLOWLIST"
PINNED_REVISION_ENV = "NAHLA_INTERNAL_E2E_PINNED_REVISION"
EVIDENCE_HMAC_KEY_ENV = "NAHLA_INTERNAL_E2E_EVIDENCE_HMAC_KEY"
ATTESTATION_HMAC_KEY_ENV = "NAHLA_INTERNAL_E2E_ATTESTATION_HMAC_KEY"
ATTESTATION_JSON_ENV = "NAHLA_INTERNAL_E2E_ATTESTATION_JSON"
ATTESTATION_SIGNATURE_ENV = "NAHLA_INTERNAL_E2E_ATTESTATION_SIGNATURE"
NETWORK_FIREWALL_CONFIRM_ENV = "NAHLA_INTERNAL_E2E_NETWORK_FIREWALL_CONFIRM"
LLM_ENABLE_ENV = "NAHLA_INTERNAL_E2E_LLM_ENABLED"
LLM_HOST_ALLOWLIST_ENV = "NAHLA_INTERNAL_E2E_LLM_HOST_ALLOWLIST"
SESSION_DIR_ENV = "NAHLA_INTERNAL_E2E_SESSION_DIR"

CODE_DEFAULT_OFF = "internal_e2e_not_enabled"
CODE_EXECUTION_NOT_CONFIRMED = "internal_e2e_execution_not_confirmed"
CODE_ATTESTATION_MISSING = "sandbox_attestation_missing"
CODE_ATTESTATION_INVALID = "sandbox_attestation_invalid"
CODE_ATTESTATION_EXPIRED = "sandbox_attestation_expired"
CODE_EVIDENCE_KEY_MISSING = "evidence_hmac_key_missing"
CODE_DATABASE_IDENTITY_MISMATCH = "sandbox_database_identity_mismatch"
CODE_CANONICAL_DATABASE_REJECTED = "canonical_or_shared_database_rejected"
CODE_NETWORK_FIREWALL_UNATTESTED = "network_firewall_unattested"
CODE_RUNTIME_REVISION_MISMATCH = "runtime_revision_mismatch"
CODE_TENANT_REQUIRED = "tenant_id_required"
CODE_TENANT_1_DENIED = "tenant_1_hard_denied"
CODE_TENANT_NOT_ALLOWED = "tenant_not_allowlisted"
CODE_TENANT_MISSING = "tenant_missing"
CODE_TENANT_AMBIGUOUS = "tenant_ambiguous"
CODE_TENANT_MISMATCH = "tenant_identity_mismatch"
CODE_TENANT_ROLE_REJECTED = "tenant_role_rejected"
CODE_TENANT_ROLE_UNVERIFIABLE = "tenant_user_role_unverifiable"
CODE_STORE_AI_MODE_INVALID = "store_ai_mode_invalid"
CODE_PHONE_NOT_ALLOWLISTED = "phone_not_allowlisted"
CODE_LLM_DEFAULT_OFF = "llm_inference_not_explicitly_enabled"
CODE_LLM_HOST_ATTESTATION_INVALID = "llm_host_allowlist_attestation_invalid"
CODE_PROVENANCE_INCOMPLETE = "provenance_incomplete"

PROVENANCE_FIELDS = (
    "compose_source",
    "response_mode",
    "chosen_path",
    "llm_candidate_present",
    "final_text_transformed",
    "final_transform_reasons",
    "reply_source",
    "fallback_source",
    "fallback_reason",
    "fallback_action_type",
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_PHONE_RE = re.compile(r"^\d{10,15}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{7,40}$")
_HOST_RE = re.compile(r"^[a-z0-9.-]{3,253}$")
_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SAFE_AUDIT_VALUE_RE = re.compile(r"^[a-zA-Z0-9_.:-]{1,96}$")
SAFE_SCENARIO_ID_RE = SAFE_AUDIT_VALUE_RE
_REJECTED_USER_ROLES = frozenset({"admin", "superadmin", "platform", "platform_admin"})
USER_ROLE_UNVERIFIABLE = "__role_unverifiable__"
EGRESS_DENIAL_KINDS = frozenset(
    {
        "automation",
        "campaign",
        "external_tool",
    "financial",
    "salla_integration",
    "shipping",
    "whatsapp_provider",
    }
)
LIVE_TURN_STATUSES = frozenset(
    {
        "billing_denied",
        "brain_exception",
        "evaluated",
        "legacy_path",
        "outbound_locked",
        "suppressed",
    }
)


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def hmac_identifier(value: str, *, key: str) -> str:
    if not key:
        raise ValueError("evidence_hmac_key_missing")
    digest = hmac.new(key.encode(), str(value).encode(), hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest[:24]}"


def preliminary_environment_blockers(env: Mapping[str, str]) -> list[str]:
    """Return default-off blockers without performing any external I/O."""
    blockers: list[str] = []
    if str(env.get(MASTER_ENABLE_ENV) or "").strip().lower() not in _TRUTHY:
        blockers.append(CODE_DEFAULT_OFF)
    if str(env.get(EXECUTION_CONFIRM_ENV) or "").strip().lower() not in _TRUTHY:
        blockers.append(CODE_EXECUTION_NOT_CONFIRMED)
    if not str(env.get(ATTESTATION_HMAC_KEY_ENV) or ""):
        blockers.append(CODE_ATTESTATION_MISSING)
    if not str(env.get(EVIDENCE_HMAC_KEY_ENV) or ""):
        blockers.append(CODE_EVIDENCE_KEY_MISSING)
    return blockers


def database_identity_fingerprint(identity: Mapping[str, Any]) -> str:
    safe = {
        "database_name": str(identity.get("database_name") or ""),
        "server_address": str(identity.get("server_address") or ""),
        "server_port": str(identity.get("server_port") or ""),
    }
    return f"sha256:{hashlib.sha256(_canonical(safe).encode()).hexdigest()}"


def parse_int_allowlist(raw: str | None) -> frozenset[int]:
    values: set[int] = set()
    for token in str(raw or "").split(","):
        token = token.strip()
        if token.isdigit() and int(token) > 0:
            values.add(int(token))
    return frozenset(values)


def normalize_phone(raw: Any) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits if _PHONE_RE.fullmatch(digits) else ""


def parse_phone_allowlist(raw: str | None) -> frozenset[str]:
    return frozenset(
        phone
        for phone in (normalize_phone(token) for token in str(raw or "").split(","))
        if phone
    )


def parse_host_allowlist(raw: str | None) -> tuple[str, ...]:
    hosts = sorted(
        {
            str(token).strip().lower()
            for token in str(raw or "").split(",")
            if _HOST_RE.fullmatch(str(token).strip().lower())
        }
    )
    return tuple(hosts)


def validate_explicit_tenant_id(tenant_id: Any, allowed_tenants: Sequence[int]) -> list[str]:
    if type(tenant_id) is not int or tenant_id <= 0:
        return [CODE_TENANT_REQUIRED]
    if tenant_id == 1:
        return [CODE_TENANT_1_DENIED]
    if tenant_id not in set(allowed_tenants):
        return [CODE_TENANT_NOT_ALLOWED]
    return []


@dataclass(frozen=True)
class SandboxAttestation:
    contract_version: str
    attestation_id: str
    disposable_database: bool
    database_identity_fingerprint: str
    canonical_database_identity_fingerprint: str
    runtime_revision: str
    network_policy: str
    allowed_hosts: tuple[str, ...]
    expires_at_utc: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SandboxAttestation":
        return cls(
            contract_version=str(raw.get("contract_version") or ""),
            attestation_id=str(raw.get("attestation_id") or ""),
            disposable_database=raw.get("disposable_database") is True,
            database_identity_fingerprint=str(raw.get("database_identity_fingerprint") or ""),
            canonical_database_identity_fingerprint=str(
                raw.get("canonical_database_identity_fingerprint") or ""
            ),
            runtime_revision=str(raw.get("runtime_revision") or "").lower(),
            network_policy=str(raw.get("network_policy") or ""),
            allowed_hosts=tuple(sorted(str(v).lower() for v in (raw.get("allowed_hosts") or []))),
            expires_at_utc=str(raw.get("expires_at_utc") or ""),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "allowed_hosts": list(self.allowed_hosts),
            "attestation_id": self.attestation_id,
            "canonical_database_identity_fingerprint": (
                self.canonical_database_identity_fingerprint
            ),
            "contract_version": self.contract_version,
            "database_identity_fingerprint": self.database_identity_fingerprint,
            "disposable_database": self.disposable_database,
            "expires_at_utc": self.expires_at_utc,
            "network_policy": self.network_policy,
            "runtime_revision": self.runtime_revision,
        }


def sign_attestation(payload: Mapping[str, Any], *, key: str) -> str:
    return hmac.new(key.encode(), _canonical(payload).encode(), hashlib.sha256).hexdigest()


def sign_session_evidence(
    payload: Mapping[str, Any],
    *,
    key: str,
) -> dict[str, Any]:
    """Return a copy with a versioned canonical-HMAC integrity envelope."""
    if not key:
        raise ValueError(CODE_EVIDENCE_KEY_MISSING)
    signed = dict(payload)
    integrity = {
        "algorithm": "hmac-sha256",
        "key_purpose": "session_evidence",
        "schema_version": EVIDENCE_SIGNATURE_SCHEMA_VERSION,
    }
    signed["integrity"] = integrity
    signature = hmac.new(
        key.encode(),
        _canonical(signed).encode(),
        hashlib.sha256,
    ).hexdigest()
    signed["integrity"] = {**integrity, "signature": signature}
    return signed


def verify_session_evidence(payload: Mapping[str, Any], *, key: str) -> bool:
    """Verify an artifact without trusting any mutable payload field."""
    if not key or not isinstance(payload, Mapping):
        return False
    integrity = payload.get("integrity")
    if not isinstance(integrity, Mapping):
        return False
    signature = str(integrity.get("signature") or "")
    if (
        integrity.get("algorithm") != "hmac-sha256"
        or integrity.get("key_purpose") != "session_evidence"
        or integrity.get("schema_version") != EVIDENCE_SIGNATURE_SCHEMA_VERSION
        or not re.fullmatch(r"[0-9a-f]{64}", signature)
    ):
        return False
    unsigned = dict(payload)
    unsigned["integrity"] = {
        key_name: value
        for key_name, value in integrity.items()
        if key_name != "signature"
    }
    expected = hmac.new(
        key.encode(),
        _canonical(unsigned).encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def _attestation_blockers(
    *,
    env: Mapping[str, str],
    identity: Mapping[str, Any],
    attested_revision: str | None,
    now: datetime,
) -> tuple[SandboxAttestation | None, list[str]]:
    raw_json = str(env.get(ATTESTATION_JSON_ENV) or "")
    signature = str(env.get(ATTESTATION_SIGNATURE_ENV) or "")
    key = str(env.get(ATTESTATION_HMAC_KEY_ENV) or "")
    if not raw_json or not signature or not key:
        return None, [CODE_ATTESTATION_MISSING]
    try:
        raw = json.loads(raw_json)
        if not isinstance(raw, Mapping):
            raise ValueError
        expected = sign_attestation(raw, key=key)
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        attestation = SandboxAttestation.from_mapping(raw)
        uuid.UUID(attestation.attestation_id)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None, [CODE_ATTESTATION_INVALID]

    blockers: list[str] = []
    db_fingerprint = database_identity_fingerprint(identity)
    if (
        attestation.contract_version != CONTRACT_VERSION
        or not attestation.disposable_database
        or not _FINGERPRINT_RE.fullmatch(attestation.database_identity_fingerprint)
        or attestation.database_identity_fingerprint != db_fingerprint
    ):
        blockers.append(CODE_DATABASE_IDENTITY_MISMATCH)
    if (
        not _FINGERPRINT_RE.fullmatch(
            attestation.canonical_database_identity_fingerprint
        )
        or attestation.canonical_database_identity_fingerprint == db_fingerprint
    ):
        blockers.append(CODE_CANONICAL_DATABASE_REJECTED)
    if (
        attestation.network_policy != "default_deny"
        or str(env.get(NETWORK_FIREWALL_CONFIRM_ENV) or "").strip() != attestation.attestation_id
    ):
        blockers.append(CODE_NETWORK_FIREWALL_UNATTESTED)
    revision = str(attested_revision or "").lower()
    pinned = str(env.get(PINNED_REVISION_ENV) or "").strip().lower()
    if (
        not _REVISION_RE.fullmatch(revision)
        or not _REVISION_RE.fullmatch(pinned)
        or revision != pinned
        or attestation.runtime_revision != pinned
    ):
        blockers.append(CODE_RUNTIME_REVISION_MISMATCH)
    try:
        expiry = datetime.fromisoformat(attestation.expires_at_utc.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
            blockers.append(CODE_ATTESTATION_EXPIRED)
    except ValueError:
        blockers.append(CODE_ATTESTATION_INVALID)
    return attestation, blockers


def evaluate_preflight(
    *,
    env: Mapping[str, str],
    tenant_id: Any,
    identity: Mapping[str, Any],
    tenant_rows: Sequence[Mapping[str, Any]],
    attested_revision: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    blockers = preliminary_environment_blockers(env)
    if not str(env.get(EVIDENCE_HMAC_KEY_ENV) or ""):
        blockers.append(CODE_EVIDENCE_KEY_MISSING)

    allowed_tenants = parse_int_allowlist(env.get(TENANT_ALLOWLIST_ENV))
    blockers.extend(validate_explicit_tenant_id(tenant_id, allowed_tenants))

    attestation, attestation_blockers = _attestation_blockers(
        env=env,
        identity=identity,
        attested_revision=attested_revision,
        now=now or datetime.now(timezone.utc),
    )
    blockers.extend(attestation_blockers)

    if len(tenant_rows) == 0:
        blockers.append(CODE_TENANT_MISSING)
    elif len(tenant_rows) != 1:
        blockers.append(CODE_TENANT_AMBIGUOUS)
    else:
        row = tenant_rows[0]
        if row.get("id") != tenant_id:
            blockers.append(CODE_TENANT_MISMATCH)
        normalized_roles = {
            str(role or "").strip().lower()
            for role in (row.get("user_roles") or [])
        }
        rejected_roles = normalized_roles & _REJECTED_USER_ROLES
        if normalized_roles & {"", USER_ROLE_UNVERIFIABLE}:
            blockers.append(CODE_TENANT_ROLE_UNVERIFIABLE)
        if row.get("is_platform_tenant") is True or rejected_roles:
            blockers.append(CODE_TENANT_ROLE_REJECTED)
        ai_settings = row.get("ai_settings")
        if not isinstance(ai_settings, Mapping) or (
            str(ai_settings.get("store_ai_mode") or "") != "test"
            or ai_settings.get("store_ai_enabled", True) is not True
        ):
            blockers.append(CODE_STORE_AI_MODE_INVALID)
        else:
            phone = normalize_phone(env.get(TEST_PHONE_ENV))
            operator_allowlist = parse_phone_allowlist(env.get(PHONE_ALLOWLIST_ENV))
            db_allowlist = parse_phone_allowlist(
                ",".join(str(v) for v in (ai_settings.get("ai_test_allowed_numbers") or []))
            )
            if not phone or phone not in operator_allowlist or phone not in db_allowlist:
                blockers.append(CODE_PHONE_NOT_ALLOWLISTED)

    llm_enabled = str(env.get(LLM_ENABLE_ENV) or "").strip().lower() in _TRUTHY
    if not llm_enabled:
        blockers.append(CODE_LLM_DEFAULT_OFF)
    configured_hosts = parse_host_allowlist(env.get(LLM_HOST_ALLOWLIST_ENV))
    if (
        not llm_enabled
        or not configured_hosts
        or attestation is None
        or configured_hosts != attestation.allowed_hosts
    ):
        blockers.append(CODE_LLM_HOST_ATTESTATION_INVALID)

    return {
        "ok": not blockers,
        "blockers": sorted(set(blockers)),
        "database_identity_fingerprint": database_identity_fingerprint(identity),
        "evidence_channel": EVIDENCE_CHANNEL,
        "llm_inference_enabled": llm_enabled,
        "llm_allowed_hosts": list(configured_hosts),
        "tenant_id": tenant_id if type(tenant_id) is int else None,
        "runtime_revision": attested_revision,
        "attestation_id": attestation.attestation_id if attestation else None,
    }
