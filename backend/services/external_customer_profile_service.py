"""
ExternalCustomerProfile upsert/lookup (A1-v3.7).

No Customer canonical creation. No salla_customer_id. No phone/name merge.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from services.order_customer_identity_contract import (
    EXTERNAL_PROVIDER_SALLA_V1,
    PROFILE_SOURCE_SALLA_CUSTOMER_SYNC,
    PROFILE_SOURCE_SALLA_ORDER_REF,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def upsert_external_customer_profile(
    db: Session,
    *,
    tenant_id: int,
    integration_connection_id: int,
    external_customer_ref: str,
    identity_namespace: str = EXTERNAL_PROVIDER_SALLA_V1,
    demographics: Optional[Dict[str, Any]] = None,
    profile_source: str = PROFILE_SOURCE_SALLA_ORDER_REF,
) -> Any:
    from models import ExternalCustomerProfile  # noqa: PLC0415

    ref = str(external_customer_ref or "").strip()
    if not ref:
        raise ValueError("external_customer_ref_required")

    row = (
        db.query(ExternalCustomerProfile)
        .filter(
            ExternalCustomerProfile.tenant_id == int(tenant_id),
            ExternalCustomerProfile.identity_namespace == identity_namespace,
            ExternalCustomerProfile.integration_connection_id == int(integration_connection_id),
            ExternalCustomerProfile.external_customer_ref == ref,
        )
        .first()
    )
    now = _utcnow()
    demo = dict(demographics or {})
    if row is None:
        row = ExternalCustomerProfile(
            tenant_id=int(tenant_id),
            identity_namespace=identity_namespace,
            integration_connection_id=int(integration_connection_id),
            external_customer_ref=ref,
            profile_state="active",
            profile_source=profile_source,
            demographics=demo or None,
            provider_snapshot_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        if demo:
            merged = dict(row.demographics or {})
            merged.update(demo)
            row.demographics = merged
        row.profile_source = profile_source or row.profile_source
        row.provider_snapshot_at = now
        row.updated_at = now
        if row.profile_state != "active":
            row.profile_state = "active"
    db.flush()
    return row


def demographics_from_salla_customer_payload(payload: dict) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    name = (payload.get("first_name", "") + " " + payload.get("last_name", "")).strip()
    if not name:
        name = str(payload.get("name") or "").strip()
    return {
        k: v
        for k, v in {
            "name": name or None,
            "email": payload.get("email"),
            "mobile": payload.get("mobile") or payload.get("phone"),
        }.items()
        if v not in (None, "")
    }


def upsert_profile_from_salla_customer_sync(
    db: Session,
    *,
    tenant_id: int,
    integration_connection_id: int,
    payload: dict,
) -> Optional[Any]:
    ref = str(payload.get("id") or "").strip()
    if not ref:
        return None
    return upsert_external_customer_profile(
        db,
        tenant_id=tenant_id,
        integration_connection_id=integration_connection_id,
        external_customer_ref=ref,
        demographics=demographics_from_salla_customer_payload(payload),
        profile_source=PROFILE_SOURCE_SALLA_CUSTOMER_SYNC,
    )


__all__ = [
    "demographics_from_salla_customer_payload",
    "upsert_external_customer_profile",
    "upsert_profile_from_salla_customer_sync",
]
