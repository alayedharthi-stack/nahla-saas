#!/usr/bin/env python3
"""Selective merchant-plane tenant clone operator (Tenant 33 acceptance).

Default: dry-run with sanitized counts/checksums only — no PII or content values.
Apply requires archived dry-run digest, exact Alembic heads, and confirmation token.
Cleanup deletes only rows recorded in a prior clone manifest.

Never executes against production source without an additional exact confirmation
token; production execution remains blocked pending separate owner approval.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import MetaData, Table, create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.pool import NullPool

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(_REPO_ROOT), str(_REPO_ROOT / "backend"), str(_REPO_ROOT / "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from scripts.operators import staging_migration_operator_gates as gates  # noqa: E402
from scripts.operators.tenant_merchant_clone_contract import (  # noqa: E402
    APPLY_CONFIRM_ENV,
    APPLY_CONFIRM_TOKEN,
    CANONICAL_STAGING_DATABASE_HOST,
    CLEANUP_CONFIRM_ENV,
    CLEANUP_CONFIRM_TOKEN,
    CLONE_EXECUTION_PURPOSE_ACCEPTANCE,
    CLONE_EXECUTION_PURPOSE_INTERNAL_E2E_DISPOSABLE,
    CLONE_PROFILE_SALLA_MINIMAL,
    DEFAULT_ACCEPTANCE_TENANT_ID,
    DENIED_TABLES,
    DISPOSABLE_TARGET_ATTESTATION_ENV,
    DISPOSABLE_TARGET_ATTESTATION_HMAC_KEY_ENV,
    DISPOSABLE_TARGET_ATTESTATION_MAX_FUTURE_SKEW_SECONDS,
    DISPOSABLE_TARGET_ATTESTATION_MAX_LIFETIME_SECONDS,
    DISPOSABLE_TARGET_ATTESTATION_MIN_HMAC_KEY_LENGTH,
    DISPOSABLE_TARGET_ATTESTATION_SCHEMA_VERSION,
    DRY_RUN_DIGEST_ENV,
    DRY_RUN_DIGEST_SCHEMA_VERSION,
    EXPECTED_SOURCE_ALEMBIC_HEADS,
    EXPECTED_TARGET_ALEMBIC_HEADS,
    FORBIDDEN_DISPOSABLE_TARGET_HOST_MARKERS,
    FORBIDDEN_DISPOSABLE_TARGET_HOSTS,
    FORBIDDEN_SOURCE_TENANT_IDS,
    GLOBAL_STRIP_COLUMNS,
    INTERNAL_E2E_DISPOSABLE_APPLY_CONFIRM_ENV,
    INTERNAL_E2E_DISPOSABLE_APPLY_CONFIRM_TOKEN,
    INTERNAL_E2E_DISPOSABLE_MASTER_ENABLE_ENV,
    INTERNAL_E2E_DISPOSABLE_TARGET_BOOTSTRAP_NAME,
    INTERNAL_E2E_DISPOSABLE_TARGET_TEST_SLUG_MARKERS,
    INTERNAL_E2E_STAGING_DUAL_HEAD_TOPOLOGY,
    KNOWN_ALEMBIC_REVISIONS,
    KNOWN_CLONE_EXECUTION_PURPOSES,
    KNOWN_CLONE_PROFILES,
    MANIFEST_SCHEMA_VERSION,
    MASTER_ENABLE_ENV,
    PHONE_SCRUB_PLACEHOLDER,
    PRESERVE_TENANT_IDENTITY_MODE,
    PRODUCTION_ENVIRONMENT_VALUE,
    PRODUCTION_IDENTITY_CLASS,
    PRODUCTION_SOURCE_CONFIRM_ENV,
    PRODUCTION_SOURCE_CONFIRM_TOKEN,
    RESET_COUNT_COLUMNS,
    REMAP_TENANT_IDENTITY_MODE,
    SCRUBBED_JSON_KEY_REPLACEMENTS,
    SOURCE_DATABASE_URL_ENV,
    SOURCE_ENVIRONMENT_ENV,
    SOURCE_PROJECT_ENV,
    STAGING_ENVIRONMENT_VALUE,
    STAGING_IDENTITY_CLASS,
    STAGING_PROJECT_VALUE,
    TARGET_ALLOWED_ENVIRONMENT_VALUES,
    TARGET_DATABASE_URL_ENV,
    TARGET_BOOTSTRAP_NAME,
    TARGET_ENVIRONMENT_ENV,
    TARGET_PROJECT_ENV,
    TARGET_TEST_SLUG_MARKERS,
    TENANT_COPY_COLUMNS,
    allowed_table_names_for_profile,
    excluded_operational_tables_for_profile,
    resolve_clone_profile,
    resolve_execution_purpose,
    table_specs_for_profile,
)
from scripts.operators.tenant_merchant_clone_scrubber import (  # noqa: E402
    _is_forbidden_key,
    _is_integration_email_field_key,
    _is_provider_ownership_key,
    _normalize_key,
    scrub_ai_settings,
    scrub_json_value,
    scrub_row_json_columns,
    scan_for_unhandled_forbidden_keys,
)

GateFailure = gates.GateFailure


def _reflected_table(conn: Connection, name: str) -> Table:
    cache = conn.info.setdefault("tenant_clone_reflected_tables", {})
    if name not in cache:
        cache[name] = Table(name, MetaData(), autoload_with=conn)
    return cache[name]


@dataclass(frozen=True)
class DisposableTargetAttestation:
    attestation_id: str
    purpose: str
    schema_version: str
    issued_at: datetime
    expires_at: datetime
    target_hostname: str
    target_database_fingerprint: str
    source_canonical_fingerprint: str
    disposable_database: bool


@dataclass(frozen=True)
class CloneRequest:
    source_tenant_id: int
    target_tenant_id: int
    source_database_url: str
    target_database_url: str
    mode: str
    profile: str
    execution_purpose: str
    clone_id: str
    dry_run_digest: str | None
    manifest_path: Path | None
    env: Mapping[str, str]


def truthy_env(env: Mapping[str, str], name: str) -> bool:
    return (env.get(name) or "").strip().lower() in ("1", "true", "yes")


def emit(payload: Mapping[str, Any]) -> int:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))
    return 0


def emit_failure(*, error_class: str, stage: str) -> int:
    return emit({"outcome": "failed", "error_class": error_class, "stage": stage})


def _parse_database_url(raw_url: str) -> Any:
    return make_url(raw_url)


def validate_database_url_scheme(raw_url: str, *, stage: str) -> GateFailure | None:
    if not raw_url.strip():
        return GateFailure("database_binding_rejected", f"{stage}_database_url_missing")
    try:
        parsed = _parse_database_url(raw_url)
    except (ArgumentError, ValueError, TypeError):
        return GateFailure("database_binding_rejected", f"{stage}_database_url_malformed")
    if parsed.drivername not in gates._POSTGRES_SCHEMES:
        return GateFailure("database_binding_rejected", f"{stage}_database_scheme_rejected")
    host = (parsed.host or "").lower()
    if not host:
        return GateFailure("database_binding_rejected", f"{stage}_database_host_missing")
    return None


def validate_source_target_distinct(request: CloneRequest) -> GateFailure | None:
    """Reject one database, while allowing tenant-id preservation across databases."""
    source = _parse_database_url(request.source_database_url)
    target = _parse_database_url(request.target_database_url)
    source_endpoint = (
        (source.host or "").lower(),
        source.port,
        source.database,
        tuple(sorted(source.query.items())),
    )
    target_endpoint = (
        (target.host or "").lower(),
        target.port,
        target.database,
        tuple(sorted(target.query.items())),
    )
    if source_endpoint == target_endpoint:
        return GateFailure("identity_rejected", "source_equals_target_database")
    return None


def identity_mode(request: CloneRequest) -> str:
    if request.source_tenant_id == request.target_tenant_id:
        return PRESERVE_TENANT_IDENTITY_MODE
    return REMAP_TENANT_IDENTITY_MODE


def validate_target_staging_identity(env: Mapping[str, str]) -> GateFailure | None:
    project = (env.get(TARGET_PROJECT_ENV) or "").strip()
    environment = (env.get(TARGET_ENVIRONMENT_ENV) or "").strip().lower()
    if not project:
        return GateFailure("identity_rejected", "target_project_missing")
    if project != STAGING_PROJECT_VALUE:
        return GateFailure("identity_rejected", "target_project_mismatch")
    if environment not in TARGET_ALLOWED_ENVIRONMENT_VALUES:
        return GateFailure("identity_rejected", "target_environment_not_experimental_staging")
    for marker in gates._FORBIDDEN_ENV_MARKERS:
        if marker in environment:
            return GateFailure("identity_rejected", "target_production_marker_detected")
    return None


def classify_source_identity(env: Mapping[str, str]) -> tuple[str, GateFailure | None]:
    project = (env.get(SOURCE_PROJECT_ENV) or "").strip()
    environment = (env.get(SOURCE_ENVIRONMENT_ENV) or "").strip().lower()
    if not project or not environment:
        return "", GateFailure("identity_rejected", "source_identity_incomplete")
    if project != STAGING_PROJECT_VALUE and project != "desirable-growth":
        return "", GateFailure("identity_rejected", "source_project_not_allowlisted")
    if environment == STAGING_ENVIRONMENT_VALUE:
        return STAGING_IDENTITY_CLASS, None
    if environment == PRODUCTION_ENVIRONMENT_VALUE:
        return PRODUCTION_IDENTITY_CLASS, None
    return "", GateFailure("identity_rejected", "source_environment_not_allowlisted")


def validate_production_source_gate(env: Mapping[str, str], source_class: str) -> GateFailure | None:
    if source_class != PRODUCTION_IDENTITY_CLASS:
        return None
    token = (env.get(PRODUCTION_SOURCE_CONFIRM_ENV) or "").strip()
    if token != PRODUCTION_SOURCE_CONFIRM_TOKEN:
        return GateFailure("confirmation_missing", "production_source_not_confirmed")
    return None


def validate_master_enable(env: Mapping[str, str], *, mode: str) -> GateFailure | None:
    if mode == "dry-run":
        return None
    if not truthy_env(env, MASTER_ENABLE_ENV):
        return GateFailure("execution_disabled", "master_enable_missing")
    return None


def validate_apply_confirmation(env: Mapping[str, str], *, mode: str) -> GateFailure | None:
    if mode != "apply":
        return None
    return gates.validate_confirmation(
        env,
        confirmation_env=APPLY_CONFIRM_ENV,
        confirmation_token=APPLY_CONFIRM_TOKEN,
    )


def validate_cleanup_confirmation(env: Mapping[str, str], *, mode: str) -> GateFailure | None:
    if mode != "cleanup":
        return None
    return gates.validate_confirmation(
        env,
        confirmation_env=CLEANUP_CONFIRM_ENV,
        confirmation_token=CLEANUP_CONFIRM_TOKEN,
    )


def validate_target_database_host(env: Mapping[str, str], target_url: str) -> GateFailure | None:
    failure = validate_database_url_scheme(target_url, stage="target")
    if failure:
        return failure
    host = (_parse_database_url(target_url).host or "").lower()
    if host != gates._ALLOWED_STAGING_DATABASE_HOST:
        return GateFailure("database_binding_rejected", "target_database_host_not_experimental_staging")
    return None


def is_internal_e2e_disposable_purpose(purpose: str) -> bool:
    return purpose == CLONE_EXECUTION_PURPOSE_INTERNAL_E2E_DISPOSABLE


def target_bootstrap_name_for_purpose(purpose: str) -> str:
    if is_internal_e2e_disposable_purpose(purpose):
        return INTERNAL_E2E_DISPOSABLE_TARGET_BOOTSTRAP_NAME
    return TARGET_BOOTSTRAP_NAME


def validate_internal_e2e_disposable_tenant_policy(request: CloneRequest) -> GateFailure | None:
    if request.source_tenant_id in FORBIDDEN_SOURCE_TENANT_IDS:
        return GateFailure("operator_rejected", "internal_e2e_forbidden_source_tenant")
    if request.source_tenant_id != request.target_tenant_id:
        return GateFailure("operator_rejected", "internal_e2e_tenant_ids_must_match")
    return None


def validate_internal_e2e_disposable_profile(request: CloneRequest) -> GateFailure | None:
    if request.profile != CLONE_PROFILE_SALLA_MINIMAL:
        return GateFailure("operator_rejected", "internal_e2e_profile_must_be_salla_acceptance_minimal")
    return None


def validate_internal_e2e_source_database_host(source_url: str) -> GateFailure | None:
    failure = validate_database_url_scheme(source_url, stage="source")
    if failure:
        return failure
    host = (_parse_database_url(source_url).host or "").lower()
    if host != CANONICAL_STAGING_DATABASE_HOST:
        return GateFailure("database_binding_rejected", "internal_e2e_source_host_not_canonical_staging")
    return None


def validate_internal_e2e_disposable_target_host(
    source_url: str,
    target_url: str,
) -> GateFailure | None:
    failure = validate_database_url_scheme(target_url, stage="target")
    if failure:
        return failure
    source_host = (_parse_database_url(source_url).host or "").lower()
    target_host = (_parse_database_url(target_url).host or "").lower()
    if target_host in FORBIDDEN_DISPOSABLE_TARGET_HOSTS:
        return GateFailure("database_binding_rejected", "internal_e2e_target_host_canonical_forbidden")
    for marker in FORBIDDEN_DISPOSABLE_TARGET_HOST_MARKERS:
        if marker in target_host:
            return GateFailure("database_binding_rejected", "internal_e2e_target_host_production_marker")
    if target_host == source_host:
        return GateFailure("identity_rejected", "internal_e2e_target_host_equals_source")
    return None


def _parse_iso8601_utc(raw: str) -> datetime:
    normalized = raw.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _disposable_attestation_signing_payload(attestation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": attestation["schema_version"],
        "attestation_id": attestation["attestation_id"],
        "purpose": attestation["purpose"],
        "issued_at": attestation["issued_at"],
        "expires_at": attestation["expires_at"],
        "target_hostname": attestation["target_hostname"],
        "target_database_fingerprint": attestation["target_database_fingerprint"],
        "source_canonical_fingerprint": attestation["source_canonical_fingerprint"],
        "disposable_database": attestation["disposable_database"],
    }


def compute_disposable_attestation_signature(
    attestation: Mapping[str, Any],
    *,
    hmac_key: str,
) -> str:
    material = json.dumps(
        _disposable_attestation_signing_payload(attestation),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    digest = hmac.new(hmac_key.encode("utf-8"), material, hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


def target_attestation_binding_fingerprint(attestation: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _disposable_attestation_signing_payload(attestation),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _parsed_disposable_attestation_payload(
    attestation: DisposableTargetAttestation,
) -> dict[str, Any]:
    return {
        "schema_version": attestation.schema_version,
        "attestation_id": attestation.attestation_id,
        "purpose": attestation.purpose,
        "issued_at": attestation.issued_at.isoformat(),
        "expires_at": attestation.expires_at.isoformat(),
        "target_hostname": attestation.target_hostname,
        "target_database_fingerprint": attestation.target_database_fingerprint,
        "source_canonical_fingerprint": attestation.source_canonical_fingerprint,
        "disposable_database": attestation.disposable_database,
    }


_ATTESTATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
_SHA256_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}\.?$)(?!-)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.?$"
)


def parse_disposable_target_attestation(
    env: Mapping[str, str],
    *,
    target_hostname: str,
) -> tuple[DisposableTargetAttestation | None, GateFailure | None]:
    raw = (env.get(DISPOSABLE_TARGET_ATTESTATION_ENV) or "").strip()
    hmac_key = (env.get(DISPOSABLE_TARGET_ATTESTATION_HMAC_KEY_ENV) or "").strip()
    if not raw:
        return None, GateFailure("attestation_missing", "disposable_target_attestation_missing")
    if not hmac_key:
        return None, GateFailure("attestation_missing", "disposable_target_attestation_hmac_key_missing")
    if len(hmac_key) < DISPOSABLE_TARGET_ATTESTATION_MIN_HMAC_KEY_LENGTH:
        return None, GateFailure("attestation_invalid", "disposable_target_attestation_hmac_key_too_short")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, GateFailure("attestation_invalid", "disposable_target_attestation_malformed")
    if not isinstance(payload, Mapping):
        return None, GateFailure("attestation_invalid", "disposable_target_attestation_not_object")

    required_fields = (
        "schema_version",
        "attestation_id",
        "purpose",
        "issued_at",
        "expires_at",
        "target_hostname",
        "target_database_fingerprint",
        "source_canonical_fingerprint",
        "disposable_database",
        "signature",
    )
    for field in required_fields:
        if field not in payload:
            return None, GateFailure("attestation_invalid", f"disposable_target_attestation_field_missing:{field}")

    if payload["schema_version"] != DISPOSABLE_TARGET_ATTESTATION_SCHEMA_VERSION:
        return None, GateFailure("attestation_invalid", "disposable_target_attestation_schema_mismatch")
    if payload["purpose"] != CLONE_EXECUTION_PURPOSE_INTERNAL_E2E_DISPOSABLE:
        return None, GateFailure("attestation_invalid", "disposable_target_attestation_purpose_mismatch")
    if payload["disposable_database"] is not True:
        return None, GateFailure("attestation_invalid", "disposable_target_attestation_not_disposable")
    attestation_id = str(payload["attestation_id"]).strip()
    if not _ATTESTATION_ID_RE.fullmatch(attestation_id):
        return None, GateFailure("attestation_invalid", "disposable_target_attestation_id_invalid")

    attested_host = str(payload["target_hostname"]).strip().lower()
    if not _HOSTNAME_RE.fullmatch(attested_host):
        return None, GateFailure("attestation_invalid", "disposable_target_attestation_hostname_invalid")
    if attested_host != target_hostname.lower():
        return None, GateFailure("attestation_invalid", "disposable_target_attestation_hostname_mismatch")
    for field in ("target_database_fingerprint", "source_canonical_fingerprint"):
        if not _SHA256_FINGERPRINT_RE.fullmatch(str(payload[field])):
            return None, GateFailure(
                "attestation_invalid",
                f"disposable_target_attestation_{field}_invalid",
            )

    expected_signature = compute_disposable_attestation_signature(payload, hmac_key=hmac_key)
    if not hmac.compare_digest(str(payload["signature"]), expected_signature):
        return None, GateFailure("attestation_invalid", "disposable_target_attestation_signature_mismatch")

    try:
        issued_at = _parse_iso8601_utc(str(payload["issued_at"]))
        expires_at = _parse_iso8601_utc(str(payload["expires_at"]))
    except ValueError:
        return None, GateFailure("attestation_invalid", "disposable_target_attestation_time_malformed")
    now = datetime.now(timezone.utc)
    if issued_at > now + timedelta(
        seconds=DISPOSABLE_TARGET_ATTESTATION_MAX_FUTURE_SKEW_SECONDS
    ):
        return None, GateFailure("attestation_invalid", "disposable_target_attestation_issued_in_future")
    if expires_at <= issued_at:
        return None, GateFailure("attestation_invalid", "disposable_target_attestation_time_order_invalid")
    if (
        expires_at - issued_at
        > timedelta(seconds=DISPOSABLE_TARGET_ATTESTATION_MAX_LIFETIME_SECONDS)
    ):
        return None, GateFailure("attestation_invalid", "disposable_target_attestation_lifetime_exceeded")
    if now < issued_at:
        return None, GateFailure("attestation_invalid", "disposable_target_attestation_not_yet_valid")
    if expires_at <= now:
        return None, GateFailure("attestation_invalid", "disposable_target_attestation_expired")

    return (
        DisposableTargetAttestation(
            attestation_id=attestation_id,
            purpose=str(payload["purpose"]),
            schema_version=str(payload["schema_version"]),
            issued_at=issued_at,
            expires_at=expires_at,
            target_hostname=attested_host,
            target_database_fingerprint=str(payload["target_database_fingerprint"]).strip(),
            source_canonical_fingerprint=str(payload["source_canonical_fingerprint"]).strip(),
            disposable_database=True,
        ),
        None,
    )


def validate_disposable_target_attestation_live(
    attestation: DisposableTargetAttestation,
    *,
    source_database_digest: str,
    target_database_digest: str,
) -> GateFailure | None:
    if attestation.target_database_fingerprint != target_database_digest:
        return GateFailure("attestation_invalid", "disposable_target_attestation_target_fingerprint_mismatch")
    if attestation.source_canonical_fingerprint != source_database_digest:
        return GateFailure("attestation_invalid", "disposable_target_attestation_source_fingerprint_mismatch")
    return None


def validate_internal_e2e_disposable_master_enable(
    env: Mapping[str, str],
    *,
    mode: str,
) -> GateFailure | None:
    if not truthy_env(env, INTERNAL_E2E_DISPOSABLE_MASTER_ENABLE_ENV):
        return GateFailure("execution_disabled", "internal_e2e_disposable_master_enable_missing")
    return None


def validate_internal_e2e_disposable_apply_confirmation(
    env: Mapping[str, str],
    *,
    mode: str,
) -> GateFailure | None:
    if mode != "apply":
        return None
    return gates.validate_confirmation(
        env,
        confirmation_env=INTERNAL_E2E_DISPOSABLE_APPLY_CONFIRM_ENV,
        confirmation_token=INTERNAL_E2E_DISPOSABLE_APPLY_CONFIRM_TOKEN,
    )


def validate_internal_e2e_staging_alembic_heads(conn: Connection) -> GateFailure | None:
    revisions = gates.read_alembic_revisions(conn)
    if not revisions:
        return GateFailure("wrong_revision", "alembic_version_missing")
    unknown = revisions - KNOWN_ALEMBIC_REVISIONS
    if unknown:
        return GateFailure("wrong_revision", "alembic_unknown_revision")
    if revisions != INTERNAL_E2E_STAGING_DUAL_HEAD_TOPOLOGY:
        return GateFailure("wrong_revision", "internal_e2e_staging_topology_mismatch")
    return None


def _validate_role_alembic_heads(
    revisions: frozenset[str],
    *,
    expected: frozenset[str],
    role: str,
) -> GateFailure | None:
    if not revisions:
        return GateFailure("wrong_revision", f"{role}_alembic_version_missing")
    unknown = revisions - KNOWN_ALEMBIC_REVISIONS
    if unknown:
        return GateFailure("wrong_revision", f"{role}_alembic_unknown_revision")
    if revisions == expected:
        return None
    if role == "source":
        if revisions in (frozenset({"0088", "0089"}), frozenset({"0088", "0091"})):
            return GateFailure("wrong_revision", "source_alembic_multi_head_drift")
        if revisions == frozenset({"0088"}):
            return GateFailure("wrong_revision", "source_alembic_revision_mismatch")
        return GateFailure("wrong_revision", "source_alembic_heads_mismatch")
    missing = expected - revisions
    extra = revisions - expected
    if missing:
        return GateFailure(
            "wrong_revision",
            f"target_alembic_revision_missing:{sorted(missing)[0]}",
        )
    if extra:
        return GateFailure("wrong_revision", "target_alembic_extra_revision")
    return GateFailure("wrong_revision", "target_alembic_heads_mismatch")


def validate_source_alembic_heads(conn: Connection) -> GateFailure | None:
    revisions = gates.read_alembic_revisions(conn)
    return _validate_role_alembic_heads(
        revisions,
        expected=EXPECTED_SOURCE_ALEMBIC_HEADS,
        role="source",
    )


def validate_target_alembic_heads(conn: Connection) -> GateFailure | None:
    revisions = gates.read_alembic_revisions(conn)
    return _validate_role_alembic_heads(
        revisions,
        expected=EXPECTED_TARGET_ALEMBIC_HEADS,
        role="target",
    )


def validate_schema_compatibility(
    source_conn: Connection,
    target_conn: Connection,
    *,
    profile: str,
) -> GateFailure | None:
    """Fail closed before row reads if source lacks target-required columns."""
    source_inspector = inspect(source_conn)
    target_inspector = inspect(target_conn)
    table_specs = table_specs_for_profile(profile)

    if "tenants" not in source_inspector.get_table_names():
        return GateFailure("preflight_failed", "schema_compat:tenants:source_table_missing")
    if "tenants" not in target_inspector.get_table_names():
        return GateFailure("preflight_failed", "schema_compat:tenants:target_table_missing")
    source_tenant_cols = {c["name"] for c in source_inspector.get_columns("tenants")}
    missing_tenant = sorted(set(TENANT_COPY_COLUMNS) - source_tenant_cols)
    if missing_tenant:
        return GateFailure(
            "preflight_failed",
            f"schema_compat:tenants:source_missing_columns:{missing_tenant[0]}",
        )

    for spec in table_specs:
        table = spec.name
        if table not in target_inspector.get_table_names():
            return GateFailure("preflight_failed", f"schema_compat:{table}:target_table_missing")
        if table not in source_inspector.get_table_names():
            return GateFailure("preflight_failed", f"schema_compat:{table}:source_table_missing")
        target_cols = {c["name"] for c in target_inspector.get_columns(table)}
        source_cols = {c["name"] for c in source_inspector.get_columns(table)}
        required = target_cols - spec.skip_columns - frozenset({"id"})
        missing = sorted(required - source_cols)
        if missing:
            return GateFailure(
                "preflight_failed",
                f"schema_compat:{table}:source_missing_columns:{missing[0]}",
            )
    return None


def compute_dry_run_digest(digest_payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def build_dry_run_digest_binding_payload(
    *,
    execution_purpose: str,
    profile: str,
    identity_mode_value: str,
    source_database_identity_digest: str,
    target_database_identity_digest: str,
    source_tenant_id: int,
    target_tenant_id: int,
    target_shell_state: str,
    table_counts: Mapping[str, int],
    source_checksums: Mapping[str, str],
    dependency_order: Sequence[str],
    target_denied_domain_counts: Mapping[str, int],
    source_alembic_heads: Sequence[str],
    target_alembic_heads: Sequence[str],
    target_attestation_id: str = "",
    target_attestation_fingerprint: str = "",
) -> dict[str, Any]:
    """Apply-binding digest payload — excludes observational source-only telemetry."""
    return {
        "schema_version": DRY_RUN_DIGEST_SCHEMA_VERSION,
        "execution_purpose": execution_purpose,
        "profile": profile,
        "identity_mode": identity_mode_value,
        "source_database_identity_digest": source_database_identity_digest,
        "target_database_identity_digest": target_database_identity_digest,
        "source_tenant_id": source_tenant_id,
        "target_tenant_id": target_tenant_id,
        "target_shell_state": target_shell_state,
        "table_counts": dict(table_counts),
        "source_checksums": dict(source_checksums),
        "dependency_order": list(dependency_order),
        "target_denied_domain_counts": dict(target_denied_domain_counts),
        "source_alembic_heads": list(source_alembic_heads),
        "target_alembic_heads": list(target_alembic_heads),
        "target_attestation_id": target_attestation_id,
        "target_attestation_fingerprint": target_attestation_fingerprint,
    }


def _database_identity_payload(conn: Connection) -> dict[str, str]:
    row = conn.execute(
        text(
            """
            SELECT
                current_database() AS database_name,
                COALESCE(inet_server_addr()::text, 'local') AS server_address,
                COALESCE(inet_server_port()::text, 'local') AS server_port
            """
        )
    ).mappings().one()
    return {
        "database_name": str(row["database_name"]),
        "server_address": str(row["server_address"]),
        "server_port": str(row["server_port"]),
    }


def database_identity_digest(conn: Connection) -> str:
    """Hash runtime connection identity; never expose DSNs or credentials."""
    encoded = json.dumps(
        _database_identity_payload(conn),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def validate_runtime_database_distinct(
    source_conn: Connection,
    target_conn: Connection,
) -> tuple[str, str, GateFailure | None]:
    source_digest = database_identity_digest(source_conn)
    target_digest = database_identity_digest(target_conn)
    if source_digest == target_digest:
        return (
            source_digest,
            target_digest,
            GateFailure("identity_rejected", "source_equals_target_database_runtime"),
        )
    return source_digest, target_digest, None


def validate_source_tenant_exists(conn: Connection, source_tenant_id: int) -> GateFailure | None:
    exists = conn.execute(
        text("SELECT 1 FROM tenants WHERE id = :tid"),
        {"tid": source_tenant_id},
    ).scalar()
    if not exists:
        return GateFailure("preflight_failed", "source_tenant_missing")
    return None


def connect_engine(url: str, *, read_only: bool = False) -> Engine:
    connect_args = {"options": "-c default_transaction_read_only=on"} if read_only else {}
    return create_engine(
        url,
        poolclass=NullPool,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


_INDIRECT_TENANT_FILTERS: dict[str, str] = {
    "product_group_items": (
        "group_id IN (SELECT id FROM product_groups WHERE tenant_id = :tid)"
    ),
    "merchant_knowledge_media": (
        "section_id IN (SELECT id FROM merchant_knowledge_sections WHERE tenant_id = :tid)"
    ),
    "merchant_knowledge_section_products": (
        "section_id IN (SELECT id FROM merchant_knowledge_sections WHERE tenant_id = :tid)"
    ),
    "coupon_rules": "coupon_id IN (SELECT id FROM coupons WHERE tenant_id = :tid)",
    "branch_contacts": (
        "branch_id IN (SELECT id FROM merchant_branches WHERE tenant_id = :tid)"
    ),
    "branch_escalation_steps": (
        "branch_id IN (SELECT id FROM merchant_branches WHERE tenant_id = :tid)"
    ),
    "branch_arrival_keywords": (
        "branch_id IN (SELECT id FROM merchant_branches WHERE tenant_id = :tid)"
    ),
}


def tenant_filter_sql(table: str) -> str:
    return _INDIRECT_TENANT_FILTERS.get(table, "tenant_id = :tid")


def allowed_table_count(conn: Connection, table: str, tenant_id: int) -> int:
    return int(
        conn.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE {tenant_filter_sql(table)}"),
            {"tid": tenant_id},
        ).scalar_one()
    )


def table_count_checksum(conn: Connection, table: str, tenant_id: int) -> str:
    count = allowed_table_count(conn, table, tenant_id)
    return hashlib.sha256(f"{table}:{tenant_id}:{count}".encode()).hexdigest()[:16]


def denied_domain_zero_proof(conn: Connection, tenant_id: int) -> dict[str, int]:
    proof: dict[str, int] = {}
    for table in sorted(DENIED_TABLES):
        if table not in inspect(conn).get_table_names():
            continue
        cols = {c["name"] for c in inspect(conn).get_columns(table)}
        if "tenant_id" not in cols:
            continue
        proof[table] = int(
            conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            ).scalar_one()
        )
    return proof


def _target_tenant_row(conn: Connection, tenant_id: int) -> Mapping[str, Any] | None:
    return conn.execute(
        text(
            """
            SELECT
                id, name, domain, is_active, is_platform_tenant,
                ai_blocked_numbers, billing_provider, stripe_customer_id,
                stripe_subscription_id, stripe_price_id, subscription_status,
                trial_started_at, trial_ends_at, first_whatsapp_connected_at,
                current_period_end, hyperpay_payment_id, billing_status,
                store_address, google_maps_link, apple_maps_link,
                same_day_delivery_enabled, pickup_enabled, branding,
                recommendation_controls, coupon_policy
            FROM tenants
            WHERE id = :tid
            """
        ),
        {"tid": tenant_id},
    ).mappings().first()


def excluded_operational_source_counts(
    conn: Connection,
    tenant_id: int,
    *,
    profile: str,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in sorted(excluded_operational_tables_for_profile(profile)):
        if table not in inspect(conn).get_table_names():
            continue
        counts[table] = allowed_table_count(conn, table, tenant_id)
    return counts


def validate_target_shell(
    conn: Connection,
    target_tenant_id: int,
    *,
    profile: str,
    execution_purpose: str = CLONE_EXECUTION_PURPOSE_ACCEPTANCE,
) -> tuple[str, GateFailure | None]:
    """Allow absent shell or a strictly empty, acceptance-marked existing shell."""
    row = _target_tenant_row(conn, target_tenant_id)
    if row is None:
        return "bootstrap_required", None

    haystack = f"{row['name'] or ''} {row['domain'] or ''}".lower()
    markers = (
        INTERNAL_E2E_DISPOSABLE_TARGET_TEST_SLUG_MARKERS
        if is_internal_e2e_disposable_purpose(execution_purpose)
        else TARGET_TEST_SLUG_MARKERS
    )
    if not any(marker in haystack for marker in markers):
        return "", GateFailure("preflight_failed", "target_tenant_not_test_marked")
    if row["is_platform_tenant"]:
        return "", GateFailure("preflight_failed", "target_tenant_is_platform")
    if not row["is_active"]:
        return "", GateFailure("preflight_failed", "target_tenant_not_active")

    sensitive_columns = (
        "ai_blocked_numbers",
        "stripe_customer_id",
        "stripe_subscription_id",
        "stripe_price_id",
        "subscription_status",
        "trial_started_at",
        "trial_ends_at",
        "first_whatsapp_connected_at",
        "current_period_end",
        "hyperpay_payment_id",
        "billing_status",
    )
    if any(row[column] not in (None, [], {}, "") for column in sensitive_columns):
        return "", GateFailure("preflight_failed", "target_tenant_sensitive_state_present")
    empty_nullable_public = (
        "store_address",
        "google_maps_link",
        "apple_maps_link",
        "branding",
        "recommendation_controls",
        "coupon_policy",
    )
    if any(row[column] is not None for column in empty_nullable_public):
        return "", GateFailure("preflight_failed", "target_tenant_shell_not_empty")
    if row["same_day_delivery_enabled"] not in (None, False):
        return "", GateFailure("preflight_failed", "target_tenant_shell_not_empty")
    if row["pickup_enabled"] not in (None, True):
        return "", GateFailure("preflight_failed", "target_tenant_shell_not_empty")

    for table in sorted(allowed_table_names_for_profile(profile)):
        if allowed_table_count(conn, table, target_tenant_id):
            return "", GateFailure("preflight_failed", f"target_allowed_rows_present:{table}")
    target_denied = denied_domain_zero_proof(conn, target_tenant_id)
    occupied = sorted(table for table, count in target_denied.items() if count)
    if occupied:
        return "", GateFailure(
            "preflight_failed",
            f"target_denied_rows_present:{occupied[0]}",
        )
    return "existing_safe_empty", None


def _source_tenant_scalars(
    source_conn: Connection,
    *,
    source_tenant_id: int,
) -> dict[str, Any]:
    row = source_conn.execute(
        text(
            "SELECT "
            + ", ".join(TENANT_COPY_COLUMNS)
            + " FROM tenants WHERE id = :tid"
        ),
        {"tid": source_tenant_id},
    ).mappings().one()
    values = {column: row[column] for column in TENANT_COPY_COLUMNS}
    for column in ("branding", "recommendation_controls", "coupon_policy"):
        if values[column] is None:
            continue
        cleaned, transforms = scrub_json_value(values[column], path=f"tenants.{column}")
        if any(item.startswith("unhandled_forbidden_key:") for item in transforms):
            raise ValueError(f"forbidden_json_keys:tenants.{column}")
        values[column] = cleaned
    return values


def _provider_config_post_scrub_violations(
    value: Any,
    *,
    path: str = "",
) -> list[str]:
    """Validate scrubbed provider config using path-only violations."""
    violations: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if _is_integration_email_field_key(key):
                if child != "":
                    violations.append(child_path)
                continue
            normalized = _normalize_key(key)
            if any(
                marker in normalized
                for marker in ("token", "secret", "password", "oauth", "api_key")
            ):
                if child != "":
                    violations.append(child_path)
                continue
            if _is_provider_ownership_key(key):
                if child != "":
                    violations.append(child_path)
                continue
            if _is_forbidden_key(key):
                if normalized not in SCRUBBED_JSON_KEY_REPLACEMENTS:
                    violations.append(child_path)
                elif child != SCRUBBED_JSON_KEY_REPLACEMENTS[normalized]:
                    violations.append(child_path)
                continue
            violations.extend(
                _provider_config_post_scrub_violations(child, path=child_path)
            )
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            violations.extend(
                _provider_config_post_scrub_violations(
                    item,
                    path=f"{path}[{idx}]",
                )
            )
    return violations


def _advance_tenant_sequence(conn: Connection) -> None:
    sequence_name = conn.execute(
        text("SELECT pg_get_serial_sequence('tenants', 'id')")
    ).scalar_one_or_none()
    if not sequence_name:
        raise ValueError("tenant_id_sequence_missing")
    conn.execute(
        text(
            """
            SELECT setval(
                CAST(:sequence_name AS regclass),
                GREATEST(
                    nextval(CAST(:sequence_name AS regclass)),
                    (SELECT COALESCE(MAX(id), 1) FROM tenants)
                ),
                true
            )
            """
        ),
        {"sequence_name": sequence_name},
    )


def _prepare_target_tenant_shell(
    source_conn: Connection,
    target_conn: Connection,
    *,
    source_tenant_id: int,
    target_tenant_id: int,
    shell_state: str,
    execution_purpose: str = CLONE_EXECUTION_PURPOSE_ACCEPTANCE,
) -> tuple[bool, list[str], dict[str, Any] | None]:
    values = _source_tenant_scalars(source_conn, source_tenant_id=source_tenant_id)
    if shell_state == "bootstrap_required":
        internal_disposable = is_internal_e2e_disposable_purpose(execution_purpose)
        params = {
            "id": target_tenant_id,
            "name": target_bootstrap_name_for_purpose(execution_purpose),
            "domain": None,
            "is_active": True,
            "is_platform_tenant": False,
            **values,
        }
        tenants = _reflected_table(target_conn, "tenants")
        target_conn.execute(tenants.insert().values(**params))
        _advance_tenant_sequence(target_conn)
        return (
            True,
            [
                "bootstrap:target_tenant_internal_e2e_disposable_shell"
                if internal_disposable
                else "bootstrap:target_tenant_acceptance_shell"
            ],
            None,
        )

    previous = target_conn.execute(
        text(
            "SELECT "
            + ", ".join(TENANT_COPY_COLUMNS)
            + " FROM tenants WHERE id = :tid"
        ),
        {"tid": target_tenant_id},
    ).mappings().one()
    params = dict(values)
    tenants = _reflected_table(target_conn, "tenants")
    target_conn.execute(
        tenants.update().where(tenants.c.id == target_tenant_id).values(**params)
    )
    return (
        False,
        [f"tenant_scalars:{col}" for col in TENANT_COPY_COLUMNS],
        {column: previous[column] for column in TENANT_COPY_COLUMNS},
    )


def _transform_row(
    row: Mapping[str, Any],
    *,
    spec_name: str,
    spec_json_columns: Sequence[str],
    target_tenant_id: int,
    id_maps: dict[str, dict[int, int]],
    remap_fk_columns: Sequence[str],
    scrub_phone_columns: Sequence[str],
    deferred_fk_columns: Sequence[str],
) -> tuple[dict[str, Any], list[str]]:
    out = dict(row)
    transformations: list[str] = []

    if "tenant_id" in out:
        out["tenant_id"] = target_tenant_id

    if "id" in out:
        del out["id"]

    for column in GLOBAL_STRIP_COLUMNS:
        if column in out:
            out[column] = None
            transformations.append(f"strip_global:{spec_name}.{column}")

    for column in RESET_COUNT_COLUMNS:
        if column in out:
            out[column] = 0
            transformations.append(f"reset_count:{spec_name}.{column}")

    for column in scrub_phone_columns:
        if column in out and out[column]:
            out[column] = PHONE_SCRUB_PLACEHOLDER
            transformations.append(f"scrub_phone:{spec_name}.{column}")

    for column in remap_fk_columns:
        if column not in out or out[column] is None:
            continue
        parent_table = {
            "product_id": "products",
            "group_id": "product_groups",
            "variant_id": "product_variants",
            "source_product_id": "products",
            "target_product_id": "products",
            "section_id": "merchant_knowledge_sections",
            "media_id": "ai_media_library",
            "coupon_id": "coupons",
            "template_id": "whatsapp_templates",
            "branch_id": "merchant_branches",
            "contact_id": "branch_contacts",
        }.get(column)
        if not parent_table:
            raise ValueError(f"unknown_fk_remap:{spec_name}.{column}")
        old_id = int(out[column])
        out[column] = id_maps[parent_table][old_id]
        transformations.append(f"remap_fk:{spec_name}.{column}")

    for column in deferred_fk_columns:
        if column in out:
            out[column] = None

    if spec_json_columns:
        out, json_transforms = scrub_row_json_columns(out, spec_json_columns, table=spec_name)
        transformations.extend(json_transforms)

    if spec_name == "integrations":
        out["enabled"] = False
        transformations.append("force:integrations.disabled_until_staging_credentials")

    for column in spec_json_columns:
        if column not in row or row[column] is None:
            continue
        if (spec_name, column) in {
            ("integrations", "config"),
            ("tenant_settings", "whatsapp_settings"),
        }:
            violations = _provider_config_post_scrub_violations(out[column])
            if violations:
                raise ValueError(f"forbidden_json_keys:{spec_name}.{column}")
            continue
        violations = scan_for_unhandled_forbidden_keys(out[column])
        if violations:
            raise ValueError(f"forbidden_json_keys:{spec_name}.{column}")

    return out, transformations


def _insert_row(conn: Connection, table: str, row: Mapping[str, Any]) -> int:
    reflected = _reflected_table(conn, table)
    statement = reflected.insert().values(**dict(row)).returning(reflected.c.id)
    return int(conn.execute(statement).scalar_one())


def build_plan(request: CloneRequest) -> dict[str, Any]:
    table_specs = table_specs_for_profile(request.profile)
    allowed_names = allowed_table_names_for_profile(request.profile)
    source_engine = connect_engine(request.source_database_url, read_only=True)
    target_engine = connect_engine(request.target_database_url)
    disposable_attestation: DisposableTargetAttestation | None = None
    try:
        with source_engine.connect() as source_conn, target_engine.connect() as target_conn:
            (
                source_database_digest,
                target_database_digest,
                identity_failure,
            ) = validate_runtime_database_distinct(source_conn, target_conn)
            if identity_failure:
                raise ValueError(identity_failure.stage)

            if is_internal_e2e_disposable_purpose(request.execution_purpose):
                target_host = (_parse_database_url(request.target_database_url).host or "").lower()
                disposable_attestation, attestation_failure = parse_disposable_target_attestation(
                    request.env,
                    target_hostname=target_host,
                )
                if attestation_failure:
                    raise ValueError(attestation_failure.stage)
                assert disposable_attestation is not None
                attestation_live_failure = validate_disposable_target_attestation_live(
                    disposable_attestation,
                    source_database_digest=source_database_digest,
                    target_database_digest=target_database_digest,
                )
                if attestation_live_failure:
                    raise ValueError(attestation_live_failure.stage)
                for conn, validator in (
                    (source_conn, validate_internal_e2e_staging_alembic_heads),
                    (target_conn, validate_internal_e2e_staging_alembic_heads),
                ):
                    failure = validator(conn)
                    if failure:
                        raise ValueError(failure.stage)
            else:
                for conn, label, validator in (
                    (source_conn, "source", validate_source_alembic_heads),
                    (target_conn, "target", validate_target_alembic_heads),
                ):
                    failure = validator(conn)
                    if failure:
                        raise ValueError(f"{label}:{failure.stage}")

            failure = validate_schema_compatibility(
                source_conn,
                target_conn,
                profile=request.profile,
            )
            if failure:
                raise ValueError(failure.stage)

            source_alembic_heads = sorted(
                gates.read_alembic_revisions(source_conn)
            )
            target_alembic_heads = sorted(
                gates.read_alembic_revisions(target_conn)
            )

            failure = validate_source_tenant_exists(source_conn, request.source_tenant_id)
            if failure:
                raise ValueError(failure.stage)
            target_shell_state, failure = validate_target_shell(
                target_conn,
                request.target_tenant_id,
                profile=request.profile,
                execution_purpose=request.execution_purpose,
            )
            if failure:
                raise ValueError(failure.stage)

            table_counts: dict[str, int] = {}
            dependency_order = [spec.name for spec in table_specs]
            transformations: list[str] = [
                "tenant_scalars",
                "force:tenant_settings.ai_settings.store_ai_mode=test",
                "force:tenant_settings.ai_settings.ai_test_allowed_numbers=[]",
                "strip:channel_credentials_and_bindings",
            ]
            if "integrations" in allowed_names:
                transformations.append(
                    "force:integrations.disabled_until_staging_credentials"
                )

            for spec in table_specs:
                table_counts[spec.name] = allowed_table_count(
                    source_conn,
                    spec.name,
                    request.source_tenant_id,
                )

            excluded_operational_counts = excluded_operational_source_counts(
                source_conn,
                request.source_tenant_id,
                profile=request.profile,
            )
            planned_copied_rows = sum(table_counts.values())

            source_checksums = {
                table: table_count_checksum(source_conn, table, request.source_tenant_id)
                for table in allowed_names
                if table != "tenant_settings"
            }
            target_before = {
                table: table_count_checksum(target_conn, table, request.target_tenant_id)
                for table in allowed_names
                if table != "tenant_settings"
            }
            denied_proof = denied_domain_zero_proof(source_conn, request.source_tenant_id)
            target_denied_proof = denied_domain_zero_proof(
                target_conn,
                request.target_tenant_id,
            )

            digest_payload = build_dry_run_digest_binding_payload(
                execution_purpose=request.execution_purpose,
                profile=request.profile,
                identity_mode_value=identity_mode(request),
                source_database_identity_digest=source_database_digest,
                target_database_identity_digest=target_database_digest,
                source_tenant_id=request.source_tenant_id,
                target_tenant_id=request.target_tenant_id,
                target_shell_state=target_shell_state,
                table_counts=table_counts,
                source_checksums=source_checksums,
                dependency_order=dependency_order,
                target_denied_domain_counts=target_denied_proof,
                source_alembic_heads=source_alembic_heads,
                target_alembic_heads=target_alembic_heads,
                target_attestation_id=(
                    disposable_attestation.attestation_id if disposable_attestation else ""
                ),
                target_attestation_fingerprint=(
                    target_attestation_binding_fingerprint(
                        _parsed_disposable_attestation_payload(disposable_attestation)
                    )
                    if disposable_attestation
                    else ""
                ),
            )
            digest = compute_dry_run_digest(digest_payload)

            plan_payload = {
                "outcome": "planned",
                "mode": request.mode,
                "clone_id": request.clone_id,
                "schema_version": DRY_RUN_DIGEST_SCHEMA_VERSION,
                "execution_purpose": request.execution_purpose,
                "profile": request.profile,
                "identity_mode": identity_mode(request),
                "source_database_identity_digest": source_database_digest,
                "target_database_identity_digest": target_database_digest,
                "database_identities_distinct": True,
                "source_tenant_id": request.source_tenant_id,
                "target_tenant_id": request.target_tenant_id,
                "target_shell_state": target_shell_state,
                "target_tenant_bootstrap_planned": target_shell_state == "bootstrap_required",
                "dependency_order": dependency_order,
                "table_counts": table_counts,
                "planned_copied_rows": planned_copied_rows,
                "excluded_operational_source_counts": excluded_operational_counts,
                "source_checksums": source_checksums,
                "target_checksums_before": target_before,
                "denied_domain_source_counts": denied_proof,
                "target_denied_domain_counts": target_denied_proof,
                "target_denied_domain_zero": not any(target_denied_proof.values()),
                "transformations": transformations,
                "dry_run_digest": digest,
                "source_alembic_heads": source_alembic_heads,
                "target_alembic_heads": target_alembic_heads,
            }
            if disposable_attestation is not None:
                plan_payload["target_attestation_id"] = disposable_attestation.attestation_id
                plan_payload["target_attestation_fingerprint"] = (
                    target_attestation_binding_fingerprint(
                        _parsed_disposable_attestation_payload(disposable_attestation)
                    )
                )
            return plan_payload
    finally:
        source_engine.dispose()
        target_engine.dispose()


def apply_clone(request: CloneRequest) -> dict[str, Any]:
    if not request.dry_run_digest:
        raise ValueError("dry_run_digest_missing")

    table_specs = table_specs_for_profile(request.profile)
    allowed_names = allowed_table_names_for_profile(request.profile)
    source_engine = connect_engine(request.source_database_url, read_only=True)
    target_engine = connect_engine(request.target_database_url)
    id_maps: dict[str, dict[int, int]] = {spec.name: {} for spec in table_specs}
    for parent in (
        "products",
        "product_variants",
        "product_groups",
        "merchant_knowledge_sections",
        "ai_media_library",
        "coupons",
        "whatsapp_templates",
        "merchant_branches",
        "branch_contacts",
    ):
        id_maps.setdefault(parent, {})
    manifest_rows: dict[str, list[int]] = {}
    transformations: list[str] = []
    unrelated_before: dict[str, str] = {}
    target_tenant_bootstrapped = False
    existing_shell_scalar_restore: dict[str, Any] | None = None

    try:
        with source_engine.connect() as source_conn:
            plan = build_plan(request)
            if plan.get("profile") != request.profile:
                raise ValueError("clone_profile_mismatch")
            if plan["dry_run_digest"] != request.dry_run_digest:
                raise ValueError("dry_run_digest_mismatch")

            unrelated_raw = (request.env.get("NAHLA_CLONE_UNRELATED_TENANT_ID") or "").strip()
            unrelated_tenant_id = int(unrelated_raw) if unrelated_raw else None
            if unrelated_tenant_id is not None:
                with target_engine.connect() as target_conn:
                    for table in allowed_names:
                        if table in inspect(target_conn).get_table_names():
                            unrelated_before[table] = table_count_checksum(
                                target_conn, table, unrelated_tenant_id
                            )

            with target_engine.begin() as target_conn:
                shell_state, shell_failure = validate_target_shell(
                    target_conn,
                    request.target_tenant_id,
                    profile=request.profile,
                    execution_purpose=request.execution_purpose,
                )
                if shell_failure:
                    raise ValueError(shell_failure.stage)
                if shell_state != plan["target_shell_state"]:
                    raise ValueError("target_shell_state_changed_since_dry_run")
                (
                    target_tenant_bootstrapped,
                    tenant_transforms,
                    existing_shell_scalar_restore,
                ) = _prepare_target_tenant_shell(
                    source_conn,
                    target_conn,
                    source_tenant_id=request.source_tenant_id,
                    target_tenant_id=request.target_tenant_id,
                    shell_state=shell_state,
                    execution_purpose=request.execution_purpose,
                )
                transformations.extend(tenant_transforms)

                for spec in table_specs:
                    rows = source_conn.execute(
                        text(
                            f"SELECT * FROM {spec.name} "
                            f"WHERE {tenant_filter_sql(spec.name)} ORDER BY id"
                        ),
                        {"tid": request.source_tenant_id},
                    ).mappings().all()
                    manifest_rows[spec.name] = []
                    for row in rows:
                        transformed, row_transforms = _transform_row(
                            row,
                            spec_name=spec.name,
                            spec_json_columns=spec.json_columns,
                            target_tenant_id=request.target_tenant_id,
                            id_maps=id_maps,
                            remap_fk_columns=spec.remap_fk_columns,
                            scrub_phone_columns=spec.scrub_phone_columns,
                            deferred_fk_columns=spec.deferred_fk_columns,
                        )
                        transformations.extend(row_transforms)
                        if spec.upsert_on_tenant:
                            existing = target_conn.execute(
                                text(
                                    f"SELECT id FROM {spec.name} "
                                    f"WHERE tenant_id = :tid LIMIT 1"
                                ),
                                {"tid": request.target_tenant_id},
                            ).scalar()
                            if existing:
                                raise ValueError(
                                    f"target_allowed_rows_present:{spec.name}"
                                )
                            else:
                                new_id = _insert_row(target_conn, spec.name, transformed)
                        else:
                            old_id = int(row["id"])
                            new_id = _insert_row(target_conn, spec.name, transformed)
                            id_maps.setdefault(spec.name, {})[old_id] = new_id
                        manifest_rows[spec.name].append(new_id)

                if not manifest_rows["tenant_settings"]:
                    settings_id = _insert_row(
                        target_conn,
                        "tenant_settings",
                        {
                            "tenant_id": request.target_tenant_id,
                            "show_nahla_branding": True,
                            "branding_text": "Powered by Nahla",
                            "whatsapp_settings": {},
                            "ai_settings": scrub_ai_settings({}),
                            "store_settings": {},
                            "notification_settings": {},
                        },
                    )
                    manifest_rows["tenant_settings"].append(settings_id)
                    transformations.append(
                        "bootstrap:tenant_settings_safe_acceptance_defaults"
                    )

                # Backfill products.default_variant_id after variants exist.
                product_rows = source_conn.execute(
                    text(
                        "SELECT id, default_variant_id FROM products "
                        "WHERE tenant_id = :tid AND default_variant_id IS NOT NULL"
                    ),
                    {"tid": request.source_tenant_id},
                ).mappings().all()
                for product_row in product_rows:
                    old_product_id = int(product_row["id"])
                    old_variant_id = int(product_row["default_variant_id"])
                    target_conn.execute(
                        text(
                            "UPDATE products SET default_variant_id = :variant_id "
                            "WHERE id = :product_id AND tenant_id = :tid"
                        ),
                        {
                            "variant_id": id_maps["product_variants"][old_variant_id],
                            "product_id": id_maps["products"][old_product_id],
                            "tid": request.target_tenant_id,
                        },
                    )
                    transformations.append("remap_fk:products.default_variant_id")

                if unrelated_tenant_id is not None:
                    for table, checksum in unrelated_before.items():
                        after = table_count_checksum(target_conn, table, unrelated_tenant_id)
                        if after != checksum:
                            raise ValueError(f"unrelated_tenant_checksum_changed:{table}")

                post_denied = denied_domain_zero_proof(
                    target_conn,
                    request.target_tenant_id,
                )
                post_occupied = sorted(
                    table for table, count in post_denied.items() if count
                )
                if post_occupied:
                    raise ValueError(f"post_clone_denied_rows_present:{post_occupied[0]}")

                settings = target_conn.execute(
                    text(
                        "SELECT ai_settings, whatsapp_settings "
                        "FROM tenant_settings WHERE tenant_id = :tid"
                    ),
                    {"tid": request.target_tenant_id},
                ).mappings().one()
                ai_settings = settings["ai_settings"] or {}
                if (
                    ai_settings.get("store_ai_mode") != "test"
                    or ai_settings.get("ai_test_allowed_numbers") != []
                ):
                    raise ValueError("post_clone_ai_test_safety_failed")

        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "clone_id": request.clone_id,
            "profile": request.profile,
            "identity_mode": identity_mode(request),
            "source_database_identity_digest": plan["source_database_identity_digest"],
            "target_database_identity_digest": plan["target_database_identity_digest"],
            "database_identities_distinct": True,
            "source_tenant_id": request.source_tenant_id,
            "target_tenant_id": request.target_tenant_id,
            "target_tenant_bootstrapped": target_tenant_bootstrapped,
            "target_bootstrap_name": (
                target_bootstrap_name_for_purpose(request.execution_purpose)
                if target_tenant_bootstrapped
                else None
            ),
            "existing_shell_scalar_restore": existing_shell_scalar_restore,
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "dry_run_digest": request.dry_run_digest,
            "source_alembic_heads": plan["source_alembic_heads"],
            "target_alembic_heads": plan["target_alembic_heads"],
            "manifest_rows": manifest_rows,
            "transformations": sorted(set(transformations)),
        }
        if request.manifest_path:
            request.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            request.manifest_path.write_text(
                json.dumps(manifest, sort_keys=True, indent=2),
                encoding="utf-8",
            )
        return {"outcome": "applied", **manifest}
    finally:
        source_engine.dispose()
        target_engine.dispose()


def cleanup_clone(request: CloneRequest) -> dict[str, Any]:
    if not request.manifest_path or not request.manifest_path.is_file():
        raise ValueError("manifest_missing")
    manifest = json.loads(request.manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("manifest_schema_mismatch")
    if manifest.get("clone_id") != request.clone_id:
        raise ValueError("clone_id_mismatch")
    manifest_profile = manifest.get("profile")
    if manifest_profile != request.profile:
        raise ValueError("clone_profile_mismatch")

    cleanup_specs = table_specs_for_profile(str(manifest_profile))

    target_engine = connect_engine(request.target_database_url)
    deleted: dict[str, int] = {}
    try:
        with target_engine.begin() as conn:
            if (
                database_identity_digest(conn)
                != manifest.get("target_database_identity_digest")
            ):
                raise ValueError("cleanup_target_database_identity_mismatch")
            if int(manifest.get("target_tenant_id", -1)) != request.target_tenant_id:
                raise ValueError("cleanup_target_tenant_mismatch")
            product_ids = manifest.get("manifest_rows", {}).get("products") or []
            if product_ids:
                conn.execute(
                    text(
                        "UPDATE products SET default_variant_id = NULL "
                        "WHERE id = ANY(:ids) AND tenant_id = :tid"
                    ),
                    {"ids": product_ids, "tid": request.target_tenant_id},
                )
            for spec in reversed(cleanup_specs):
                ids = manifest.get("manifest_rows", {}).get(spec.name) or []
                if not ids:
                    continue
                result = conn.execute(
                    text(f"DELETE FROM {spec.name} WHERE id = ANY(:ids)"),
                    {"ids": ids},
                )
                deleted[spec.name] = int(result.rowcount or 0)
            shell_deleted = False
            if manifest.get("target_tenant_bootstrapped") is True:
                target_denied = denied_domain_zero_proof(
                    conn,
                    request.target_tenant_id,
                )
                if any(target_denied.values()):
                    raise ValueError("cleanup_bootstrapped_shell_has_operational_rows")
                result = conn.execute(
                    text(
                        "DELETE FROM tenants "
                        "WHERE id = :tid AND name = :name AND domain IS NULL"
                    ),
                    {
                        "tid": request.target_tenant_id,
                        "name": manifest.get("target_bootstrap_name"),
                    },
                )
                if result.rowcount != 1:
                    raise ValueError("cleanup_bootstrapped_shell_identity_mismatch")
                shell_deleted = True
            else:
                restore = manifest.get("existing_shell_scalar_restore")
                if not isinstance(restore, dict) or set(restore) != set(TENANT_COPY_COLUMNS):
                    raise ValueError("cleanup_existing_shell_restore_missing")
                assignments = ", ".join(
                    f"{column} = :{column}" for column in TENANT_COPY_COLUMNS
                )
                params = dict(restore)
                params["tid"] = request.target_tenant_id
                result = conn.execute(
                    text(
                        f"UPDATE tenants SET {assignments} "
                        "WHERE id = :tid"
                    ),
                    params,
                )
                if result.rowcount != 1:
                    raise ValueError("cleanup_existing_shell_missing")
        return {
            "outcome": "cleaned",
            "clone_id": request.clone_id,
            "deleted_counts": deleted,
            "target_tenant_shell_deleted": shell_deleted,
        }
    finally:
        target_engine.dispose()


def validate_clone_profile(profile: str | None) -> GateFailure | None:
    try:
        resolve_clone_profile(profile)
    except ValueError as exc:
        return GateFailure("operator_rejected", str(exc))
    return None


def validate_execution_purpose(purpose: str | None) -> GateFailure | None:
    try:
        resolve_execution_purpose(purpose)
    except ValueError as exc:
        return GateFailure("operator_rejected", str(exc))
    return None


def run_acceptance_gates(request: CloneRequest) -> GateFailure | None:
    failure = validate_clone_profile(request.profile)
    if failure:
        return failure
    for validator in (
        lambda: validate_database_url_scheme(request.source_database_url, stage="source"),
        lambda: validate_database_url_scheme(request.target_database_url, stage="target"),
        lambda: validate_target_staging_identity(request.env),
        lambda: validate_target_database_host(request.env, request.target_database_url),
        lambda: validate_source_target_distinct(request),
        lambda: validate_master_enable(request.env, mode=request.mode),
        lambda: validate_apply_confirmation(request.env, mode=request.mode),
        lambda: validate_cleanup_confirmation(request.env, mode=request.mode),
    ):
        failure = validator()
        if failure:
            return failure

    source_class, failure = classify_source_identity(request.env)
    if failure:
        return failure
    failure = validate_production_source_gate(request.env, source_class)
    if failure:
        return failure
    return None


def run_internal_e2e_disposable_gates(request: CloneRequest) -> GateFailure | None:
    failure = validate_clone_profile(request.profile)
    if failure:
        return failure
    failure = validate_internal_e2e_disposable_profile(request)
    if failure:
        return failure
    failure = validate_internal_e2e_disposable_tenant_policy(request)
    if failure:
        return failure
    if request.mode == "cleanup":
        return GateFailure(
            "operator_rejected",
            "internal_e2e_disposable_cleanup_forbidden",
        )
    for validator in (
        lambda: validate_database_url_scheme(request.source_database_url, stage="source"),
        lambda: validate_database_url_scheme(request.target_database_url, stage="target"),
        lambda: validate_target_staging_identity(request.env),
        lambda: validate_internal_e2e_source_database_host(request.source_database_url),
        lambda: validate_internal_e2e_disposable_target_host(
            request.source_database_url,
            request.target_database_url,
        ),
        lambda: validate_source_target_distinct(request),
        lambda: validate_internal_e2e_disposable_master_enable(request.env, mode=request.mode),
        lambda: validate_internal_e2e_disposable_apply_confirmation(request.env, mode=request.mode),
    ):
        failure = validator()
        if failure:
            return failure

    _, failure = classify_source_identity(request.env)
    if failure:
        return failure
    if (request.env.get(SOURCE_ENVIRONMENT_ENV) or "").strip().lower() != STAGING_ENVIRONMENT_VALUE:
        return GateFailure("identity_rejected", "internal_e2e_source_environment_not_staging")

    target_host = (_parse_database_url(request.target_database_url).host or "").lower()
    _, attestation_failure = parse_disposable_target_attestation(
        request.env,
        target_hostname=target_host,
    )
    return attestation_failure


def run_gates(request: CloneRequest) -> GateFailure | None:
    failure = validate_execution_purpose(request.execution_purpose)
    if failure:
        return failure
    if is_internal_e2e_disposable_purpose(request.execution_purpose):
        return run_internal_e2e_disposable_gates(request)
    return run_acceptance_gates(request)


def build_request_from_env(
    *,
    mode: str,
    profile: str,
    source_tenant_id: int,
    target_tenant_id: int,
    clone_id: str | None,
    dry_run_digest: str | None,
    manifest_path: Path | None,
    env: Mapping[str, str] | None = None,
    execution_purpose: str | None = None,
) -> CloneRequest:
    env_map = dict(env or os.environ)
    source_url = (env_map.get(SOURCE_DATABASE_URL_ENV) or "").strip()
    target_url = (env_map.get(TARGET_DATABASE_URL_ENV) or "").strip()
    return CloneRequest(
        source_tenant_id=source_tenant_id,
        target_tenant_id=target_tenant_id,
        source_database_url=source_url,
        target_database_url=target_url,
        mode=mode,
        profile=resolve_clone_profile(profile),
        execution_purpose=resolve_execution_purpose(execution_purpose),
        clone_id=clone_id or str(uuid.uuid4()),
        dry_run_digest=dry_run_digest or (env_map.get(DRY_RUN_DIGEST_ENV) or "").strip() or None,
        manifest_path=manifest_path,
        env=env_map,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merchant-plane tenant clone operator")
    parser.add_argument("command", choices=["dry-run", "apply", "cleanup"])
    parser.add_argument(
        "--source-tenant-id",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--target-tenant-id",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--profile",
        required=True,
        choices=sorted(KNOWN_CLONE_PROFILES),
        help="Closed clone profile (required; no default)",
    )
    parser.add_argument(
        "--execution-purpose",
        choices=sorted(KNOWN_CLONE_EXECUTION_PURPOSES),
        default=None,
        help="Closed execution purpose (default: acceptance_cross_database)",
    )
    parser.add_argument("--clone-id", default="")
    parser.add_argument("--dry-run-digest", default="")
    parser.add_argument("--manifest-path", default="")
    args = parser.parse_args(list(argv) if argv is not None else None)

    mode = {"dry-run": "dry-run", "apply": "apply", "cleanup": "cleanup"}[args.command]
    execution_purpose = resolve_execution_purpose(args.execution_purpose)
    if is_internal_e2e_disposable_purpose(execution_purpose) and (
        args.source_tenant_id is None or args.target_tenant_id is None
    ):
        return emit_failure(
            error_class="operator_rejected",
            stage="internal_e2e_explicit_tenant_ids_required",
        )
    request = build_request_from_env(
        mode=mode,
        profile=args.profile,
        source_tenant_id=(
            args.source_tenant_id
            if args.source_tenant_id is not None
            else DEFAULT_ACCEPTANCE_TENANT_ID
        ),
        target_tenant_id=(
            args.target_tenant_id
            if args.target_tenant_id is not None
            else DEFAULT_ACCEPTANCE_TENANT_ID
        ),
        clone_id=args.clone_id or None,
        dry_run_digest=args.dry_run_digest or None,
        manifest_path=Path(args.manifest_path) if args.manifest_path else None,
        execution_purpose=execution_purpose,
    )

    failure = run_gates(request)
    if failure:
        return emit_failure(error_class=failure.error_class, stage=failure.stage)

    try:
        if mode == "dry-run":
            return emit(build_plan(request))
        if mode == "apply":
            return emit(apply_clone(request))
        return emit(cleanup_clone(request))
    except ValueError as exc:
        return emit_failure(error_class="operator_rejected", stage=str(exc))
    except SQLAlchemyError:
        return emit_failure(error_class="database_error", stage="sqlalchemy")


if __name__ == "__main__":
    raise SystemExit(main())
