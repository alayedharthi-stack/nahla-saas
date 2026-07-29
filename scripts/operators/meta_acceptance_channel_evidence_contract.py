"""Bounded Meta acceptance channel evidence contract (default-off, no mutations).

Distinguishes env-key ``meta_config_present`` from operator-attested preflight
readiness ``operator_attested_channel_ready``. Post-send provider/device evidence
remains ``actual_provider_channel`` elsewhere in the acceptance program.

Operator webhook observation is not provider-cryptographic proof — it is a closed,
HMAC-signed operator attestation with observation provenance.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from scripts.operators.staging_acceptance_config_consolidation_contract import (
    ACCEPTANCE_CUTOVER_LABEL,
    ACCEPTANCE_CUTOVER_SCOPE,
    ACCEPTANCE_CUTOVER_SNAPSHOT_COMPONENTS,
    ACCEPTANCE_TARGET_PROVIDER_PATH,
    D360_LEGACY_DETECTION_KEYS,
    CHANNEL_READINESS_REQUIRED_KEYS,
    META_DIRECT_WEBHOOK_ROUTE,
    SNAPSHOT_SCHEMA_VERSION,
    is_d360_legacy_present,
    is_d360_only_legacy_path,
    is_meta_credential_complete,
    meta_signature_mode_gaps,
)

ARTIFACT_SCHEMA_VERSION = "meta_acceptance_operator_webhook_observation_v2"
WEBHOOK_ATTESTATION_ARTIFACT_ENV = "NAHLA_META_ACCEPTANCE_WEBHOOK_ATTESTATION_ARTIFACT"
WEBHOOK_ATTESTATION_HMAC_KEY_ENV = "NAHLA_META_ACCEPTANCE_WEBHOOK_ATTESTATION_HMAC_KEY"

CODE_WEBHOOK_ATTESTATION_MISSING = "webhook_attestation_missing"
CODE_WEBHOOK_ATTESTATION_INVALID = "webhook_attestation_invalid"
CODE_WEBHOOK_ATTESTATION_STALE = "webhook_attestation_stale"
CODE_WEBHOOK_ATTESTATION_FORGED = "webhook_attestation_forged"
CODE_WEBHOOK_ATTESTATION_REVISION_MISMATCH = "webhook_attestation_revision_mismatch"
CODE_WEBHOOK_ATTESTATION_DEPLOYMENT_MISMATCH = "webhook_attestation_deployment_mismatch"
CODE_WEBHOOK_ATTESTATION_BACKEND_MISMATCH = "webhook_attestation_backend_mismatch"
CODE_WEBHOOK_ATTESTATION_TENANT_MISMATCH = "webhook_attestation_tenant_mismatch"
CODE_WEBHOOK_ATTESTATION_ROUTE_UNOBSERVED = "webhook_attestation_route_unobserved"
CODE_WEBHOOK_OBSERVATION_INVALID = "webhook_observation_invalid"
CODE_WEBHOOK_OBSERVATION_STALE = "webhook_observation_stale"
CODE_ROLLBACK_SNAPSHOT_MISSING = "rollback_snapshot_missing"
CODE_ROLLBACK_SNAPSHOT_INVALID = "rollback_snapshot_invalid"
CODE_ROLLBACK_SNAPSHOT_STALE = "rollback_snapshot_stale"
CODE_DB_WA_BINDING_MISSING = "db_wa_binding_missing"
CODE_DB_WA_BINDING_INVALID = "db_wa_binding_invalid"
CODE_DB_WA_BINDING_MISMATCH = "db_wa_binding_fingerprint_mismatch"

EVIDENCE_CLASS_OPERATOR_OBSERVED_META_WEBHOOK = "operator_observed_meta_webhook"

META_PROVIDER_VALUE = "meta"
D360_PROVIDER_VALUE = "360dialog"
CONNECTED_STATUS_VALUES = frozenset({"connected", "active", "enabled"})
HMAC_DOMAIN_PREFIX = "META_ACCEPTANCE_CHANNEL_EVIDENCE\0"
ARTIFACT_MAX_AGE_SECONDS = 24 * 60 * 60
OBSERVATION_MAX_AGE_SECONDS = 4 * 60 * 60
CLOCK_SKEW_ALLOWANCE_SECONDS = 5 * 60

ALLOWED_OBSERVATION_SOURCES = frozenset(
    {
        "meta_developer_console_manual_review",
        "meta_graph_api_read",
    }
)

_FINGERPRINT_RE = re.compile(r"^hmac-sha256:[0-9a-f]{32}$")
_CONTENT_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_OBSERVER_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")

REQUIRED_ATTESTATION_FIELDS: tuple[str, ...] = (
    "artifact_schema_version",
    "provider",
    "tenant_id",
    "backend_url_fingerprint",
    "pinned_revision",
    "deployment_id",
    "observation_source",
    "observer_id",
    "observed_at_utc",
    "observation_evidence_digest",
    "observation_evidence_ref",
    "observed_callback_route",
    "waba_id_fingerprint",
    "phone_number_id_fingerprint",
    "issued_at_utc",
    "expires_at_utc",
    "rollback_snapshot_evidence",
    "forbidden_unlocks_respected",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_utc(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def fingerprint_value(*, material: str, label: str, hmac_key: str) -> str:
    if not hmac_key:
        raise ValueError("hmac_key_missing")
    payload = f"{HMAC_DOMAIN_PREFIX}{label}\0{material}".encode("utf-8")
    digest = hmac.new(hmac_key.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest[:32]}"


def meta_config_readiness_gaps(variables: Mapping[str, str]) -> list[str]:
    """Env-key readiness only. Never includes webhook route attestation."""
    gaps: list[str] = []
    for key in CHANNEL_READINESS_REQUIRED_KEYS:
        if not str(variables.get(key) or "").strip():
            gaps.append(key)
    gaps.extend(meta_signature_mode_gaps(variables))
    return gaps


def evaluate_meta_config_present(variables: Mapping[str, str]) -> dict[str, Any]:
    d360_only = is_d360_only_legacy_path(variables)
    gaps = meta_config_readiness_gaps(variables)
    return {
        "meta_config_present": (not d360_only) and not gaps,
        "d360_only_legacy_path": d360_only,
        "d360_legacy_present": is_d360_legacy_present(variables),
        "meta_credential_complete": is_meta_credential_complete(variables),
        "meta_config_gaps": gaps,
    }


def _attestation_signature_material(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: payload[key] for key in sorted(payload) if key != "signature"}


def sign_webhook_attestation_artifact(payload: Mapping[str, Any], *, hmac_key: str) -> str:
    material = json.dumps(_attestation_signature_material(payload), sort_keys=True, separators=(",", ":"))
    return hmac.new(hmac_key.encode("utf-8"), material.encode("utf-8"), hashlib.sha256).hexdigest()


def load_webhook_attestation_artifact(path: str | Path) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(CODE_WEBHOOK_ATTESTATION_INVALID)
    return raw


def verify_webhook_attestation_signature(
    payload: Mapping[str, Any],
    *,
    hmac_key: str,
) -> bool:
    signature = str(payload.get("signature") or "").strip()
    if not signature or not hmac_key:
        return False
    expected = sign_webhook_attestation_artifact(payload, hmac_key=hmac_key)
    return hmac.compare_digest(signature, expected)


def rollback_snapshot_evidence_gaps(
    snapshot: Mapping[str, Any] | None,
    *,
    observed_at: datetime | None,
) -> list[str]:
    if snapshot is None:
        return ["rollback_snapshot_evidence"]
    gaps: list[str] = []
    if str(snapshot.get("snapshot_schema_version") or "").strip() != SNAPSHOT_SCHEMA_VERSION:
        gaps.append("rollback_snapshot_evidence.snapshot_schema_version")
    if snapshot.get("rollback_required") is not True:
        gaps.append("rollback_snapshot_evidence.rollback_required")
    fingerprint = str(snapshot.get("snapshot_fingerprint") or "").strip()
    if not _FINGERPRINT_RE.fullmatch(fingerprint):
        gaps.append("rollback_snapshot_evidence.snapshot_fingerprint")
    if str(snapshot.get("label") or "").strip() != ACCEPTANCE_CUTOVER_LABEL:
        gaps.append("rollback_snapshot_evidence.label")
    if str(snapshot.get("scope") or "").strip() != ACCEPTANCE_CUTOVER_SCOPE:
        gaps.append("rollback_snapshot_evidence.scope")
    if snapshot.get("forbidden_unlocks_respected") is not True:
        gaps.append("rollback_snapshot_evidence.forbidden_unlocks_respected")
    components = snapshot.get("components")
    if not isinstance(components, Mapping):
        gaps.append("rollback_snapshot_evidence.components")
        return gaps
    for component in ACCEPTANCE_CUTOVER_SNAPSHOT_COMPONENTS:
        entry = components.get(component)
        if not isinstance(entry, Mapping):
            gaps.append(f"rollback_snapshot_evidence.components.{component}")
            continue
        component_fingerprint = str(entry.get("fingerprint") or "").strip()
        if not _FINGERPRINT_RE.fullmatch(component_fingerprint):
            gaps.append(f"rollback_snapshot_evidence.components.{component}.fingerprint")
    captured = _parse_utc(str(snapshot.get("captured_at_utc") or ""))
    if captured is None:
        gaps.append("rollback_snapshot_evidence.captured_at_utc")
    elif observed_at is not None and captured >= observed_at:
        gaps.append("rollback_snapshot_evidence.captured_after_observation")
    return gaps


def webhook_attestation_gaps(
    artifact: Mapping[str, Any] | None,
    *,
    tenant_id: int,
    backend_url: str,
    pinned_revision: str,
    deployment_id: str,
    hmac_key: str,
    now: datetime | None = None,
) -> list[str]:
    if artifact is None:
        return ["webhook_attestation_artifact"]
    gaps: list[str] = []
    if not verify_webhook_attestation_signature(artifact, hmac_key=hmac_key):
        gaps.append("webhook_attestation_signature")
    if artifact.get("artifact_schema_version") != ARTIFACT_SCHEMA_VERSION:
        gaps.append("webhook_attestation_schema_version")
    if str(artifact.get("provider") or "").strip() != ACCEPTANCE_TARGET_PROVIDER_PATH:
        gaps.append("webhook_attestation_provider")
    if int(artifact.get("tenant_id") or -1) != tenant_id:
        gaps.append("webhook_attestation.tenant_id")
    for field in REQUIRED_ATTESTATION_FIELDS:
        if field not in artifact:
            gaps.append(f"webhook_attestation.{field}")
            continue
        value = artifact.get(field)
        if field == "rollback_snapshot_evidence":
            if not isinstance(value, Mapping):
                gaps.append(f"webhook_attestation.{field}")
            continue
        if not str(value or "").strip():
            gaps.append(f"webhook_attestation.{field}")
    if gaps:
        return gaps
    if artifact.get("forbidden_unlocks_respected") is not True:
        gaps.append("webhook_attestation.forbidden_unlocks_respected")

    observation_source = str(artifact.get("observation_source") or "").strip()
    if observation_source not in ALLOWED_OBSERVATION_SOURCES:
        gaps.append("webhook_attestation.observation_source")
    observer_id = str(artifact.get("observer_id") or "").strip()
    if not observer_id or not _OBSERVER_ID_RE.fullmatch(observer_id):
        gaps.append("webhook_attestation.observer_id")
    evidence_digest = str(artifact.get("observation_evidence_digest") or "").strip()
    if not _CONTENT_DIGEST_RE.fullmatch(evidence_digest):
        gaps.append("webhook_attestation.observation_evidence_digest")
    evidence_ref = str(artifact.get("observation_evidence_ref") or "").strip()
    if not _FINGERPRINT_RE.fullmatch(evidence_ref):
        gaps.append("webhook_attestation.observation_evidence_ref")

    observed_route = str(artifact.get("observed_callback_route") or "").strip()
    if observed_route != META_DIRECT_WEBHOOK_ROUTE:
        gaps.append("webhook_attestation.observed_callback_route")
    if str(artifact.get("pinned_revision") or "").strip() != pinned_revision.strip():
        gaps.append("webhook_attestation.pinned_revision")
    if str(artifact.get("deployment_id") or "").strip() != deployment_id.strip():
        gaps.append("webhook_attestation.deployment_id")
    expected_backend = fingerprint_value(
        material=backend_url.strip(),
        label="backend_url",
        hmac_key=hmac_key,
    )
    if str(artifact.get("backend_url_fingerprint") or "").strip() != expected_backend:
        gaps.append("webhook_attestation.backend_url_fingerprint")

    issued = _parse_utc(str(artifact.get("issued_at_utc") or ""))
    expires = _parse_utc(str(artifact.get("expires_at_utc") or ""))
    observed = _parse_utc(str(artifact.get("observed_at_utc") or ""))
    current = (now or _utc_now()).astimezone(timezone.utc).replace(microsecond=0)
    if issued is None or expires is None or observed is None:
        gaps.append("webhook_attestation.validity_window")
    else:
        if observed > issued:
            gaps.append("webhook_attestation.observation_after_issued")
        if (issued - observed).total_seconds() > OBSERVATION_MAX_AGE_SECONDS + CLOCK_SKEW_ALLOWANCE_SECONDS:
            gaps.append("webhook_attestation.observation_stale")
        if issued > current:
            gaps.append("webhook_attestation.not_yet_valid")
        if expires < current:
            gaps.append("webhook_attestation.expired")
        if (expires - issued).total_seconds() > ARTIFACT_MAX_AGE_SECONDS + CLOCK_SKEW_ALLOWANCE_SECONDS:
            gaps.append("webhook_attestation.validity_window_too_long")

    rollback = artifact.get("rollback_snapshot_evidence")
    rollback_gaps = rollback_snapshot_evidence_gaps(
        rollback if isinstance(rollback, Mapping) else None,
        observed_at=observed,
    )
    gaps.extend(rollback_gaps)
    return gaps


def whatsapp_connection_binding_gaps(
    row: Mapping[str, Any] | None,
    *,
    tenant_id: int,
    artifact: Mapping[str, Any],
    hmac_key: str,
) -> list[str]:
    if row is None:
        return ["db_wa_binding.row_missing"]
    gaps: list[str] = []
    if int(row.get("tenant_id") or -1) != tenant_id:
        gaps.append("db_wa_binding.tenant_id")
    provider = str(row.get("provider") or "").strip().lower()
    if provider == D360_PROVIDER_VALUE:
        gaps.append("db_wa_binding.d360_provider_rejected")
    if provider != META_PROVIDER_VALUE:
        gaps.append("db_wa_binding.provider")
    status = str(row.get("status") or "").strip().lower()
    if status not in CONNECTED_STATUS_VALUES:
        gaps.append("db_wa_binding.status")
    if row.get("sending_enabled") is not True:
        gaps.append("db_wa_binding.sending_enabled")
    phone_number_id = str(row.get("phone_number_id") or "").strip()
    waba_id = str(
        row.get("whatsapp_business_account_id") or row.get("waba_id") or ""
    ).strip()
    if not phone_number_id:
        gaps.append("db_wa_binding.phone_number_id")
    if not waba_id:
        gaps.append("db_wa_binding.waba_id")
    if gaps:
        return gaps
    expected_phone = fingerprint_value(
        material=phone_number_id,
        label="phone_number_id",
        hmac_key=hmac_key,
    )
    expected_waba = fingerprint_value(
        material=waba_id,
        label="waba_id",
        hmac_key=hmac_key,
    )
    if str(artifact.get("phone_number_id_fingerprint") or "").strip() != expected_phone:
        gaps.append("db_wa_binding.phone_number_id_fingerprint")
    if str(artifact.get("waba_id_fingerprint") or "").strip() != expected_waba:
        gaps.append("db_wa_binding.waba_id_fingerprint")
    return gaps


def evaluate_operator_attested_channel_ready(
    *,
    variables: Mapping[str, str],
    tenant_id: int,
    artifact: Mapping[str, Any] | None,
    hmac_key: str,
    backend_url: str,
    pinned_revision: str,
    deployment_id: str,
    db_row: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    config = evaluate_meta_config_present(variables)
    attestation_gaps = webhook_attestation_gaps(
        artifact,
        tenant_id=tenant_id,
        backend_url=backend_url,
        pinned_revision=pinned_revision,
        deployment_id=deployment_id,
        hmac_key=hmac_key,
        now=now,
    )
    binding_gaps: list[str] = []
    if artifact is not None and not attestation_gaps:
        binding_gaps = whatsapp_connection_binding_gaps(
            db_row,
            tenant_id=tenant_id,
            artifact=artifact,
            hmac_key=hmac_key,
        )
    all_gaps = list(config["meta_config_gaps"]) + attestation_gaps + binding_gaps
    if config["d360_only_legacy_path"]:
        all_gaps.append("d360_only_legacy_path")
    ready = (
        bool(config["meta_config_present"])
        and not attestation_gaps
        and not binding_gaps
        and artifact is not None
    )
    return {
        "operator_attested_channel_ready": ready,
        "channel_evidence_class": EVIDENCE_CLASS_OPERATOR_OBSERVED_META_WEBHOOK,
        "meta_config_present": bool(config["meta_config_present"]),
        "webhook_attestation_gaps": attestation_gaps,
        "db_wa_binding_gaps": binding_gaps,
        "channel_evidence_gaps": all_gaps,
        "observed_callback_route": (
            str(artifact.get("observed_callback_route") or "").strip() if artifact else None
        ),
        "observation_source": (
            str(artifact.get("observation_source") or "").strip() if artifact else None
        ),
    }


def build_rollback_snapshot_evidence(
    *,
    snapshot_fingerprint: str,
    captured_at_utc: str,
    component_fingerprints: Mapping[str, str],
    observed_at_utc: str | None = None,
) -> dict[str, Any]:
    """Assemble caller-supplied consolidation rollback evidence only."""
    components: dict[str, Any] = {}
    for component in ACCEPTANCE_CUTOVER_SNAPSHOT_COMPONENTS:
        fingerprint = str(component_fingerprints.get(component) or "").strip()
        components[component] = {"fingerprint": fingerprint}
    snapshot: dict[str, Any] = {
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_fingerprint": str(snapshot_fingerprint or "").strip(),
        "captured_at_utc": str(captured_at_utc or "").strip(),
        "rollback_required": True,
        "label": ACCEPTANCE_CUTOVER_LABEL,
        "scope": ACCEPTANCE_CUTOVER_SCOPE,
        "forbidden_unlocks_respected": True,
        "components": components,
    }
    observed_at = _parse_utc(observed_at_utc or "") if observed_at_utc else None
    gaps = rollback_snapshot_evidence_gaps(snapshot, observed_at=observed_at)
    if gaps:
        raise ValueError(CODE_ROLLBACK_SNAPSHOT_INVALID)
    return snapshot


def build_webhook_attestation_artifact(
    *,
    tenant_id: int,
    backend_url: str,
    pinned_revision: str,
    deployment_id: str,
    observed_callback_route: str,
    waba_id: str,
    phone_number_id: str,
    hmac_key: str,
    observation_source: str,
    observer_id: str,
    observed_at_utc: str,
    observation_evidence_digest: str,
    observation_evidence_ref: str,
    rollback_snapshot_evidence: Mapping[str, Any],
    issued_at_utc: str,
    expires_at_utc: str,
) -> dict[str, Any]:
    """Assemble operator attestation from caller-supplied external evidence only."""
    if not str(observation_source or "").strip():
        raise ValueError(CODE_WEBHOOK_OBSERVATION_INVALID)
    if not str(observer_id or "").strip():
        raise ValueError(CODE_WEBHOOK_OBSERVATION_INVALID)
    if not str(observed_at_utc or "").strip():
        raise ValueError(CODE_WEBHOOK_OBSERVATION_INVALID)
    if not _CONTENT_DIGEST_RE.fullmatch(str(observation_evidence_digest or "").strip()):
        raise ValueError(CODE_WEBHOOK_OBSERVATION_INVALID)
    if not _FINGERPRINT_RE.fullmatch(str(observation_evidence_ref or "").strip()):
        raise ValueError(CODE_WEBHOOK_OBSERVATION_INVALID)
    if not isinstance(rollback_snapshot_evidence, Mapping):
        raise ValueError(CODE_ROLLBACK_SNAPSHOT_MISSING)

    issued = _parse_utc(issued_at_utc)
    observed = _parse_utc(observed_at_utc)
    expires = _parse_utc(expires_at_utc)
    if issued is None or observed is None or expires is None:
        raise ValueError(CODE_WEBHOOK_ATTESTATION_INVALID)

    rollback = dict(rollback_snapshot_evidence)
    rollback_gaps = rollback_snapshot_evidence_gaps(rollback, observed_at=observed)
    if rollback_gaps:
        raise ValueError(CODE_ROLLBACK_SNAPSHOT_INVALID)

    payload: dict[str, Any] = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "provider": ACCEPTANCE_TARGET_PROVIDER_PATH,
        "tenant_id": tenant_id,
        "backend_url_fingerprint": fingerprint_value(
            material=backend_url.strip(),
            label="backend_url",
            hmac_key=hmac_key,
        ),
        "pinned_revision": pinned_revision.strip(),
        "deployment_id": deployment_id.strip(),
        "observation_source": observation_source.strip(),
        "observer_id": observer_id.strip(),
        "observed_at_utc": observed.isoformat(),
        "observation_evidence_digest": observation_evidence_digest.strip(),
        "observation_evidence_ref": observation_evidence_ref.strip(),
        "observed_callback_route": observed_callback_route.strip(),
        "waba_id_fingerprint": fingerprint_value(
            material=waba_id.strip(),
            label="waba_id",
            hmac_key=hmac_key,
        ),
        "phone_number_id_fingerprint": fingerprint_value(
            material=phone_number_id.strip(),
            label="phone_number_id",
            hmac_key=hmac_key,
        ),
        "issued_at_utc": issued.isoformat(),
        "expires_at_utc": expires.isoformat(),
        "rollback_snapshot_evidence": rollback,
        "forbidden_unlocks_respected": True,
    }
    payload["signature"] = sign_webhook_attestation_artifact(payload, hmac_key=hmac_key)
    return payload


def build_unsigned_webhook_attestation_template() -> dict[str, Any]:
    """Human-signable operator webhook attestation skeleton (unsigned).

    Contains no fabricated signature or approval defaults. Operators must
    supply observation evidence and sign with ``sign_webhook_attestation_artifact``.
    """
    rollback_components = {
        component: {"fingerprint": ""}
        for component in ACCEPTANCE_CUTOVER_SNAPSHOT_COMPONENTS
    }
    return {
        "template_schema_version": "meta_acceptance_operator_webhook_observation_unsigned_v1",
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "provider": ACCEPTANCE_TARGET_PROVIDER_PATH,
        "tenant_id": None,
        "backend_url_fingerprint": "",
        "pinned_revision": "",
        "deployment_id": "",
        "observation_source": "",
        "observer_id": "",
        "observed_at_utc": "",
        "observation_evidence_digest": "",
        "observation_evidence_ref": "",
        "observed_callback_route": META_DIRECT_WEBHOOK_ROUTE,
        "waba_id_fingerprint": "",
        "phone_number_id_fingerprint": "",
        "issued_at_utc": "",
        "expires_at_utc": "",
        "rollback_snapshot_evidence": {
            "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
            "snapshot_fingerprint": "",
            "captured_at_utc": "",
            "label": ACCEPTANCE_CUTOVER_LABEL,
            "scope": ACCEPTANCE_CUTOVER_SCOPE,
            "components": rollback_components,
        },
        "signature": None,
        "human_signoff_required": True,
        "note": (
            "Populate all fields from operator observation; set "
            "forbidden_unlocks_respected and rollback_required to true only "
            "after human verification; sign before use"
        ),
    }


__all__ = [
    "ALLOWED_OBSERVATION_SOURCES",
    "ARTIFACT_MAX_AGE_SECONDS",
    "ARTIFACT_SCHEMA_VERSION",
    "CODE_DB_WA_BINDING_INVALID",
    "CODE_DB_WA_BINDING_MISMATCH",
    "CODE_DB_WA_BINDING_MISSING",
    "CODE_ROLLBACK_SNAPSHOT_INVALID",
    "CODE_ROLLBACK_SNAPSHOT_MISSING",
    "CODE_ROLLBACK_SNAPSHOT_STALE",
    "CODE_WEBHOOK_ATTESTATION_BACKEND_MISMATCH",
    "CODE_WEBHOOK_ATTESTATION_DEPLOYMENT_MISMATCH",
    "CODE_WEBHOOK_ATTESTATION_FORGED",
    "CODE_WEBHOOK_ATTESTATION_INVALID",
    "CODE_WEBHOOK_ATTESTATION_MISSING",
    "CODE_WEBHOOK_ATTESTATION_REVISION_MISMATCH",
    "CODE_WEBHOOK_ATTESTATION_ROUTE_UNOBSERVED",
    "CODE_WEBHOOK_ATTESTATION_STALE",
    "CODE_WEBHOOK_ATTESTATION_TENANT_MISMATCH",
    "CODE_WEBHOOK_OBSERVATION_INVALID",
    "CODE_WEBHOOK_OBSERVATION_STALE",
    "D360_LEGACY_DETECTION_KEYS",
    "EVIDENCE_CLASS_OPERATOR_OBSERVED_META_WEBHOOK",
    "OBSERVATION_MAX_AGE_SECONDS",
    "REQUIRED_ATTESTATION_FIELDS",
    "WEBHOOK_ATTESTATION_ARTIFACT_ENV",
    "WEBHOOK_ATTESTATION_HMAC_KEY_ENV",
    "build_rollback_snapshot_evidence",
    "build_unsigned_webhook_attestation_template",
    "build_webhook_attestation_artifact",
    "evaluate_meta_config_present",
    "evaluate_operator_attested_channel_ready",
    "fingerprint_value",
    "load_webhook_attestation_artifact",
    "meta_config_readiness_gaps",
    "rollback_snapshot_evidence_gaps",
    "sign_webhook_attestation_artifact",
    "verify_webhook_attestation_signature",
    "webhook_attestation_gaps",
    "whatsapp_connection_binding_gaps",
]
