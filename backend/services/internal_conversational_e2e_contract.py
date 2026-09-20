"""Closed contract for disposable internal conversational E2E execution."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence


CONTRACT_VERSION = "internal_conversational_e2e_v1"
EVIDENCE_SCHEMA_VERSION = "internal_conversational_e2e_evidence_v3"
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


# ── OrderFlowV2 address-turn evidence (schema v3) ─────────────────────────
#
# Kept HARNESS-SPECIFIC on purpose. The platform's shared reply provenance
# (``TextProvenance`` / ``PROVENANCE_FIELDS``) is consumed by every
# non-address caller and by the completeness check over stored artifacts;
# widening it to carry an address concern would turn a validation need into
# a platform contract change, with re-verification of every historical
# record behind it. Nothing here touches it. The signature already covers
# the whole canonical payload, so these fields are signed by construction
# and tampering with any of them fails ``verify_session_evidence``; and
# because that verifier never reads ``evidence_schema_version``, v2
# artifacts keep verifying unchanged.

EVIDENCE_SCHEMA_VERSIONS_SUPPORTED: tuple[str, ...] = (
    "internal_conversational_e2e_evidence_v2",
    "internal_conversational_e2e_evidence_v3",
)

SCENARIO_SCHEMA_VERSION_V1 = "internal_conversational_e2e_scenarios_v1"
SCENARIO_SCHEMA_VERSION_V2 = "internal_conversational_e2e_scenarios_v2"
SCENARIO_SCHEMA_VERSIONS_SUPPORTED: tuple[str, ...] = (
    SCENARIO_SCHEMA_VERSION_V1,
    SCENARIO_SCHEMA_VERSION_V2,
)

TURN_MODE_BRAIN = "brain"
TURN_MODE_OF2 = "of2"
TURN_MODES = frozenset({TURN_MODE_BRAIN, TURN_MODE_OF2})

# What a scenario says it did to this turn on purpose. ``none`` is the only
# value that may contribute to a NATURAL outcome rate; the others exist to
# confirm a mechanism and carry their own denominator.
FAILURE_INJECTION_NONE = "none"
FAILURE_INJECTIONS = frozenset(
    {
        FAILURE_INJECTION_NONE,
        "provider_error",
        "provider_timeout",
        "guard_boundary",
    }
)

# How the reply was executed. Derived from execution and provenance, never
# from whether the payload happened to carry choices.
PATH_ORDINARY = "ordinary"
PATH_RECOVERY = "recovery"
PATH_UNRESOLVED = "unresolved"
EXECUTION_PATHS = frozenset({PATH_ORDINARY, PATH_RECOVERY, PATH_UNRESOLVED})

# What the customer was actually offered. A SEPARATE dimension: an
# ordinary turn for a customer with no saved addresses legitimately
# carries no choices, and must not be read as a recovery because of it.
SURFACE_BUTTONS = "buttons"
SURFACE_LIST = "list"
SURFACE_TEXT = "text"
SURFACE_NONE = "none"
DELIVERED_SURFACES = frozenset(
    {SURFACE_BUTTONS, SURFACE_LIST, SURFACE_TEXT, SURFACE_NONE}
)

CODE_ADDRESS_EVIDENCE_MISSING = "address_turn_evidence_missing"
CODE_ADDRESS_EVIDENCE_INCOMPLETE = "address_turn_evidence_incomplete"
CODE_ADDRESS_EVIDENCE_UNBOUND = "address_turn_evidence_unbound"
CODE_MODEL_CALL_EVIDENCE_INCOMPLETE = "model_bound_call_evidence_incomplete"

ADDRESS_TURN_REQUIRED_FIELDS: tuple[str, ...] = (
    "captured_payload_digest",
    "collection_field",
    "compose_entered",
    "delivered_surface",
    "execution_path",
    "failure_injection",
    "model_bound_calls",
    "captured_payload_digest_verified",
    "outbound_message_id",
    "outbound_message_row_verified",
    "outbound_metadata_turn_ref",
    "outbound_provenance",
    "receipt_action_ids",
    "receipt_address_ids",
    "recorded_action_ids",
    "transport",
    "turn_ref",
)

MODEL_BOUND_CALL_REQUIRED_FIELDS: tuple[str, ...] = (
    "call_index",
    "collection_field",
    "delivery_address_status",
    "has_accepted_maps_reference",
    "missing_field",
    "observed_at",
    "response_goal",
    "stage",
    "turn_ref",
)


CODE_ADDRESS_EXPECTATION_UNMET = "address_turn_expectation_unmet"
CODE_ADDRESS_RECEIPT_MISMATCH = "address_turn_receipt_mismatch"
CODE_ADDRESS_INJECTION_NOT_EXECUTED = "address_turn_injection_not_executed"
CODE_ADDRESS_EXPECTATIONS_MISSING = "address_turn_expectations_missing"
CODE_ADDRESS_FINAL_SOURCE_UNESTABLISHED = "address_turn_final_source_unestablished"
CODE_ADDRESS_TIMING_UNCORRELATED = "address_turn_timing_uncorrelated"

CONSENT_ACTION_PREFIX = "nahla_addr_select"
_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CAPTURED_DELIVERY_ID = re.compile(r"^captured\.[0-9a-f]{32}$")

# The closed set the doctrine allows for a customer-facing reply.
COMPOSE_SOURCES = frozenset(
    {
        "llm",
        "persona_llm",
        "merchant_template",
        "meta_template",
        "legal_exact_text",
        "security_exact_text",
        "fallback_deterministic",
    }
)

# What a scenario must state about an address turn before its evidence
# means anything. Signing protects the bytes after they are written; it
# says nothing about whether they describe the turn the scenario asked
# for, which is why these are checked and not merely present.
ADDRESS_EXPECTATION_FIELDS: tuple[str, ...] = (
    "collection_field",
    "response_goal",
    "missing_field",
)


def _expected(expectations: Mapping[str, Any], key: str) -> str:
    return str(expectations.get(key) or "").strip()


def _model_call_blockers(
    record: Mapping[str, Any],
    expectations: Mapping[str, Any],
) -> list[str]:
    """Every model-bound call must match what the scenario asked for."""
    blockers: list[str] = []
    calls = record.get("model_bound_calls")
    calls = list(calls) if isinstance(calls, Sequence) and not isinstance(calls, (str, bytes)) else []

    provenance = dict(record.get("outbound_provenance") or {})
    compose_entered = bool(record.get("compose_entered"))
    # A turn where compose could not be ENTERED legitimately has no model
    # call. That is a truthful outcome, not thin evidence — but it must
    # then carry the fallback provenance that says so, rather than simply
    # omitting everything.
    if not calls:
        if compose_entered:
            return [CODE_MODEL_CALL_EVIDENCE_INCOMPLETE]
        if not str(provenance.get("fallback_reason") or "").strip():
            return [CODE_MODEL_CALL_EVIDENCE_INCOMPLETE]
        return []

    for call in calls:
        if not isinstance(call, Mapping) or any(
            field not in call for field in MODEL_BOUND_CALL_REQUIRED_FIELDS
        ):
            blockers.append(CODE_MODEL_CALL_EVIDENCE_INCOMPLETE)
            break
        if str(call.get("observed_at") or "") != "orchestrator_adapter":
            # Reconstructed from somewhere else is not observation.
            blockers.append(CODE_MODEL_CALL_EVIDENCE_INCOMPLETE)
            break
        if str(call.get("stage") or "") not in ("ordinary", "recovery"):
            # ``unspecified`` means no branch declared itself, so the
            # path this call belongs to was never established.
            blockers.append(CODE_MODEL_CALL_EVIDENCE_INCOMPLETE)
            break
        # An outcome, or a truthful "this call never returned". Silence
        # is neither.
        if not call.get("outcome_recorded") and not call.get("outcome_pending"):
            blockers.append(CODE_MODEL_CALL_EVIDENCE_INCOMPLETE)
            break
        if call.get("outcome_recorded"):
            if not isinstance(call.get("candidate_present"), bool):
                blockers.append(CODE_MODEL_CALL_EVIDENCE_INCOMPLETE)
                break
            # Where the text came from, or why there is none. One of the
            # two must be stated; "it returned something" is not a source.
            if not call.get("candidate_present") and not str(
                call.get("fallback_reason") or ""
            ).strip():
                blockers.append(CODE_MODEL_CALL_EVIDENCE_INCOMPLETE)
                break
            if call.get("candidate_present") and not str(
                call.get("compose_source") or ""
            ).strip():
                blockers.append(CODE_MODEL_CALL_EVIDENCE_INCOMPLETE)
                break

    for call in calls:
        if not isinstance(call, Mapping):
            continue
        for field in ADDRESS_EXPECTATION_FIELDS:
            want = _expected(expectations, field)
            if want and str(call.get(field) or "") != want:
                blockers.append(CODE_ADDRESS_EXPECTATION_UNMET)
        want_status = _expected(expectations, "delivery_address_status")
        if want_status and str(call.get("delivery_address_status") or "") != want_status:
            blockers.append(CODE_ADDRESS_EXPECTATION_UNMET)
        if "requires_accepted_maps_reference" in expectations:
            if bool(call.get("has_accepted_maps_reference")) != bool(
                expectations["requires_accepted_maps_reference"]
            ):
                blockers.append(CODE_ADDRESS_EXPECTATION_UNMET)
    return blockers


def _binding_blockers(record: Mapping[str, Any]) -> list[str]:
    """One turn, three artifacts, tied by VERIFIED identity.

    Checking that a digest starts with ``sha256:`` and that two ids are
    non-empty is a shape test, not a binding: a foreign row id, a foreign
    delivery id and a digest of nothing all passed it. Each one is now
    checked against the thing it is supposed to identify, and the
    producer states whether it could verify them at all.
    """
    blockers: list[str] = []
    turn_ref = str(record.get("turn_ref") or "")
    if not turn_ref or str(record.get("outbound_metadata_turn_ref") or "") != turn_ref:
        blockers.append(CODE_ADDRESS_EVIDENCE_UNBOUND)
    else:
        for call in record.get("model_bound_calls") or []:
            if isinstance(call, Mapping) and str(call.get("turn_ref") or "") != turn_ref:
                blockers.append(CODE_ADDRESS_EVIDENCE_UNBOUND)
                break

    if str(record.get("transport") or "") != "captured":
        return sorted(set(blockers))

    digest = str(record.get("captured_payload_digest") or "")
    if not _SHA256_DIGEST.fullmatch(digest):
        blockers.append(CODE_ADDRESS_EVIDENCE_UNBOUND)
    # The producer recomputed the digest from the payload it captured and
    # says whether it matched. A digest nobody checked binds nothing.
    if record.get("captured_payload_digest_verified") is not True:
        blockers.append(CODE_ADDRESS_EVIDENCE_UNBOUND)

    delivery_ids = record.get("delivery_ids")
    delivery_ids = (
        list(delivery_ids)
        if isinstance(delivery_ids, Sequence) and not isinstance(delivery_ids, (str, bytes))
        else []
    )
    if not delivery_ids or not all(
        _CAPTURED_DELIVERY_ID.fullmatch(str(v)) for v in delivery_ids
    ):
        blockers.append(CODE_ADDRESS_EVIDENCE_UNBOUND)

    # The persisted row was fetched and is this turn's row, not an
    # arbitrary non-empty string.
    if record.get("outbound_message_row_verified") is not True:
        blockers.append(CODE_ADDRESS_EVIDENCE_UNBOUND)
    if not str(record.get("outbound_message_id") or "").strip():
        blockers.append(CODE_ADDRESS_EVIDENCE_UNBOUND)

    # The recorded showing names the delivery it was recorded against.
    # When a showing exists at all, that reference must be one of the
    # delivery ids this turn actually captured.
    if record.get("recorded_action_ids"):
        offer_ref = str(record.get("recorded_offer_delivery_ref") or "")
        if not offer_ref or offer_ref not in {str(v) for v in delivery_ids}:
            blockers.append(CODE_ADDRESS_EVIDENCE_UNBOUND)

    # Latency is only acceptable when it is correlated to this turn.
    timing = record.get("turn_timing")
    timing = dict(timing) if isinstance(timing, Mapping) else {}
    if not timing:
        if record.get("turn_timing_unavailable") is not True:
            # Either correlated timing, or an explicit statement that the
            # measurement is unavailable. Silently absent is neither.
            blockers.append(CODE_ADDRESS_TIMING_UNCORRELATED)
    elif not str(timing.get("turn_id") or "").strip():
        blockers.append(CODE_ADDRESS_TIMING_UNCORRELATED)
    return sorted(set(blockers))


def _receipt_blockers(record: Mapping[str, Any]) -> list[str]:
    """What was offered on the wire is what was recorded as offered.

    Three lists have to agree, not two. ``receipt_action_ids`` are the ids
    that actually left; ``receipt_address_ids`` are what they resolve to;
    ``recorded_action_ids`` are the addresses the platform recorded as
    shown. Comparing only the last two let an action id naming a
    different offer and a different address pass, because nobody checked
    that the ids on the wire were the ones those addresses came from.
    """
    delivered_actions = record.get("receipt_action_ids")
    delivered = record.get("receipt_address_ids")
    recorded = record.get("recorded_action_ids")
    for value in (delivered_actions, delivered, recorded):
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return [CODE_ADDRESS_RECEIPT_MISMATCH]

    resolved: set[str] = set()
    offers: set[str] = set()
    for action_id in delivered_actions:
        parts = str(action_id or "").split(":")
        if len(parts) != 3 or parts[0] != CONSENT_ACTION_PREFIX:
            return [CODE_ADDRESS_RECEIPT_MISMATCH]
        offers.add(parts[1])
        resolved.add(parts[2])

    if resolved != {str(v) for v in delivered}:
        return [CODE_ADDRESS_RECEIPT_MISMATCH]
    if {str(v) for v in delivered} != {str(v) for v in recorded}:
        return [CODE_ADDRESS_RECEIPT_MISMATCH]
    # One showing answers one question: ids from two different offers on
    # one payload mean the receipt does not describe a single showing.
    if delivered_actions and len(offers) != 1:
        return [CODE_ADDRESS_RECEIPT_MISMATCH]
    recorded_offer = str(record.get("recorded_offer_id") or "")
    if offers and recorded_offer and recorded_offer not in offers:
        return [CODE_ADDRESS_RECEIPT_MISMATCH]
    return []


def address_turn_evidence_blockers(
    record: Any,
    *,
    expects_address_turn: bool,
    expectations: Optional[Mapping[str, Any]] = None,
) -> list[str]:
    """Why this turn's address evidence cannot be accepted.

    ``address_turn`` is optional in the SCHEMA so that a v3 artifact for
    a brain turn stays valid — but optional must not mean "absent is
    fine". A scenario turn that declares ``expects_address_turn`` and
    produces no record, an incomplete one, one that contradicts what the
    scenario asked for, or one whose artifacts cannot be tied together,
    is a failure of the run rather than a thinner artifact.
    """
    blockers: list[str] = []
    if not expects_address_turn:
        return blockers
    if not isinstance(record, Mapping) or not record:
        return [CODE_ADDRESS_EVIDENCE_MISSING]

    wants = dict(expectations or {})
    # An OrderFlowV2 scenario has to say what it expects. Without that,
    # every check below degrades to "some string is present", which is
    # how a city turn asserting a missing DELIVERY ADDRESS passed.
    # Every one of them, not any of them. A manifest stating only the
    # collection field leaves the goal and the missing field unasserted,
    # so a turn could contradict either and still be accepted.
    if not all(_expected(wants, field) for field in ADDRESS_EXPECTATION_FIELDS):
        blockers.append(CODE_ADDRESS_EXPECTATIONS_MISSING)

    missing = [f for f in ADDRESS_TURN_REQUIRED_FIELDS if f not in record]
    if missing:
        blockers.append(CODE_ADDRESS_EVIDENCE_INCOMPLETE)
    if str(record.get("execution_path") or "") not in EXECUTION_PATHS:
        blockers.append(CODE_ADDRESS_EVIDENCE_INCOMPLETE)
    if str(record.get("execution_path") or "") == PATH_UNRESOLVED:
        # Nothing established which path ran, so no outcome can be filed.
        blockers.append(CODE_ADDRESS_EVIDENCE_INCOMPLETE)
    if str(record.get("delivered_surface") or "") not in DELIVERED_SURFACES:
        blockers.append(CODE_ADDRESS_EVIDENCE_INCOMPLETE)
    if str(record.get("failure_injection") or "") not in FAILURE_INJECTIONS:
        blockers.append(CODE_ADDRESS_EVIDENCE_INCOMPLETE)
    if str(record.get("transport") or "") != "captured":
        blockers.append(CODE_ADDRESS_EVIDENCE_INCOMPLETE)

    # Final-text provenance: what the customer received and why.
    provenance = record.get("outbound_provenance")
    provenance = dict(provenance) if isinstance(provenance, Mapping) else {}
    # "Non-empty mapping" established nothing about the final text. The
    # source has to be named, and it has to be one of the closed values.
    source = str(provenance.get("compose_source") or "").strip()
    if source not in COMPOSE_SOURCES:
        blockers.append(CODE_ADDRESS_FINAL_SOURCE_UNESTABLISHED)
    elif source == "fallback_deterministic" and not str(
        provenance.get("fallback_reason") or ""
    ).strip():
        blockers.append(CODE_ADDRESS_FINAL_SOURCE_UNESTABLISHED)

    # An injection that was declared but never fired proves nothing about
    # the mechanism it named, and must not be filed as a passing check.
    injection = str(record.get("failure_injection") or FAILURE_INJECTION_NONE)
    if injection != FAILURE_INJECTION_NONE:
        state = record.get("injection_state")
        state = dict(state) if isinstance(state, Mapping) else {}
        if int(state.get("fired") or 0) < 1 or str(state.get("kind") or "") != injection:
            blockers.append(CODE_ADDRESS_INJECTION_NOT_EXECUTED)

    for want_key, record_key in (
        ("expected_execution_path", "execution_path"),
        ("expected_surface", "delivered_surface"),
    ):
        want = _expected(wants, want_key)
        if want and str(record.get(record_key) or "") != want:
            blockers.append(CODE_ADDRESS_EXPECTATION_UNMET)

    want_field = _expected(wants, "collection_field")
    if want_field and str(record.get("collection_field") or "") != want_field:
        blockers.append(CODE_ADDRESS_EXPECTATION_UNMET)

    blockers.extend(_model_call_blockers(record, wants))
    blockers.extend(_binding_blockers(record))
    blockers.extend(_receipt_blockers(record))
    return sorted(set(blockers))


def classify_execution_path(provenance: Any) -> str:
    """Ordinary or recovery, from what the run recorded about itself.

    Never from the delivered surface: an ordinary turn for a customer with
    no saved addresses has no choices to offer and is still ordinary.
    """
    meta = dict(provenance or {}) if isinstance(provenance, Mapping) else {}
    if meta.get("address_reply_recovered") or meta.get("address_claim_send_suppressed"):
        return PATH_RECOVERY
    if "address_reply_composed" in meta or meta.get("address_claim_compose_attempted") is not None:
        return PATH_ORDINARY
    return PATH_UNRESOLVED


def delivered_surface(payload: Any) -> str:
    """What the captured payload actually offered the customer."""
    body = dict(payload or {}) if isinstance(payload, Mapping) else {}
    interactive = body.get("interactive")
    interactive = dict(interactive) if isinstance(interactive, Mapping) else {}
    kind = str(interactive.get("type") or "").strip().lower()
    if kind == "button":
        return SURFACE_BUTTONS
    if kind == "list":
        return SURFACE_LIST
    if body.get("text"):
        return SURFACE_TEXT
    return SURFACE_NONE
