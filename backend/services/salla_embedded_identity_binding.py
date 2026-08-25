"""Durable OAuth-verified Salla embedded identity bindings."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from database.models import Integration, SallaEmbeddedIdentityBinding, User
from services.salla_reconciliation_challenge import ReconciliationChallenge

logger = logging.getLogger("nahla.salla_embedded_binding")

PROVIDER_SALLA = "salla"
VERIFIED_VIA_OAUTH_RECONCILE = "oauth_reconcile"
STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"


@dataclass(frozen=True)
class BindingReentryResult:
    ok: bool
    reason: str = ""
    tenant_id: int = 0
    canonical_store_id: str = ""
    integration: Optional[Integration] = None
    merchant_user: Optional[User] = None
    binding: Optional[SallaEmbeddedIdentityBinding] = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _str_id(value: object) -> str:
    return str(value or "").strip()


def find_active_binding(
    db: Session,
    *,
    provider: str,
    app_id: str,
    merchant_account_id: str,
) -> Optional[SallaEmbeddedIdentityBinding]:
    app = _str_id(app_id)
    merchant_id = _str_id(merchant_account_id)
    if not app or not merchant_id:
        return None
    return (
        db.query(SallaEmbeddedIdentityBinding)
        .filter(
            SallaEmbeddedIdentityBinding.provider == provider,
            SallaEmbeddedIdentityBinding.app_id == app,
            SallaEmbeddedIdentityBinding.merchant_account_id == merchant_id,
            SallaEmbeddedIdentityBinding.status == STATUS_ACTIVE,
        )
        .order_by(SallaEmbeddedIdentityBinding.id.desc())
        .first()
    )


def revoke_binding(
    db: Session,
    binding: SallaEmbeddedIdentityBinding,
    *,
    reason: str,
) -> None:
    if binding.status == STATUS_REVOKED:
        return
    now = _utc_now()
    binding.status = STATUS_REVOKED
    binding.revoked_at = now
    binding.revoked_reason = _str_id(reason) or "revoked"
    binding.updated_at = now
    logger.info(
        "[salla_embedded_binding] revoked | binding_id=%s tenant=%s merchant_account_id=%s reason=%s",
        binding.id,
        binding.tenant_id,
        binding.merchant_account_id,
        binding.revoked_reason,
    )


def validate_binding_for_reentry(
    db: Session,
    binding: SallaEmbeddedIdentityBinding,
    *,
    app_id: str,
) -> BindingReentryResult:
    """Re-prove live Integration + merchant user before token-login re-entry."""
    app = _str_id(app_id)
    if app and binding.app_id != app:
        revoke_binding(db, binding, reason="app_id_mismatch")
        db.flush()
        return BindingReentryResult(ok=False, reason="app_id_mismatch", binding=binding)

    integration = db.query(Integration).filter(Integration.id == binding.integration_id).first()
    if integration is None:
        revoke_binding(db, binding, reason="integration_missing")
        db.flush()
        return BindingReentryResult(ok=False, reason="integration_missing", binding=binding)

    cfg = integration.config or {}
    if integration.provider != PROVIDER_SALLA:
        revoke_binding(db, binding, reason="integration_provider_mismatch")
        db.flush()
        return BindingReentryResult(ok=False, reason="integration_provider_mismatch", binding=binding)

    if integration.tenant_id != binding.tenant_id:
        revoke_binding(db, binding, reason="integration_tenant_mismatch")
        db.flush()
        return BindingReentryResult(ok=False, reason="integration_tenant_mismatch", binding=binding)

    canonical_store_id = _str_id(integration.external_store_id)
    if not canonical_store_id:
        revoke_binding(db, binding, reason="integration_missing_canonical_store")
        db.flush()
        return BindingReentryResult(ok=False, reason="integration_missing_canonical_store", binding=binding)

    if canonical_store_id != _str_id(binding.canonical_store_id):
        revoke_binding(db, binding, reason="external_store_id_mismatch")
        db.flush()
        return BindingReentryResult(ok=False, reason="external_store_id_mismatch", binding=binding)

    if not integration.enabled:
        revoke_binding(db, binding, reason="integration_disabled")
        db.flush()
        return BindingReentryResult(ok=False, reason="integration_disabled", binding=binding)

    if cfg.get("needs_reauth"):
        revoke_binding(db, binding, reason="integration_needs_reauth")
        db.flush()
        return BindingReentryResult(ok=False, reason="integration_needs_reauth", binding=binding)

    merchant_user = (
        db.query(User)
        .filter(
            User.tenant_id == binding.tenant_id,
            User.role == "merchant",
            User.is_active.is_(True),
        )
        .order_by(User.id.asc())
        .first()
    )
    if merchant_user is None:
        return BindingReentryResult(ok=False, reason="inactive_merchant", binding=binding)

    return BindingReentryResult(
        ok=True,
        reason="ok",
        tenant_id=binding.tenant_id,
        canonical_store_id=canonical_store_id,
        integration=integration,
        merchant_user=merchant_user,
        binding=binding,
    )


def upsert_binding_from_oauth_reconcile(
    db: Session,
    *,
    challenge: ReconciliationChallenge,
    canonical_store_id: str,
    integration_id: int,
    tenant_id: int,
) -> SallaEmbeddedIdentityBinding:
    """Race-safe upsert: one active binding per embedded identity; preserve revoked history."""
    store_id = _str_id(canonical_store_id)
    app_id = _str_id(challenge.app_id)
    merchant_id = _str_id(challenge.merchant_account_id)
    if not store_id or not app_id or not merchant_id:
        raise ValueError("binding_upsert_invalid_inputs")
    if tenant_id <= 0 or integration_id <= 0:
        raise ValueError("binding_upsert_invalid_tenant_or_integration")

    integration = db.query(Integration).filter(Integration.id == integration_id).first()
    if integration is None:
        raise ValueError("binding_upsert_integration_missing")
    if integration.tenant_id != tenant_id:
        raise ValueError("binding_upsert_integration_tenant_mismatch")
    if _str_id(integration.external_store_id) != store_id:
        raise ValueError("binding_upsert_integration_store_mismatch")

    now = _utc_now()
    active = (
        db.query(SallaEmbeddedIdentityBinding)
        .filter(
            SallaEmbeddedIdentityBinding.provider == PROVIDER_SALLA,
            SallaEmbeddedIdentityBinding.app_id == app_id,
            SallaEmbeddedIdentityBinding.merchant_account_id == merchant_id,
            SallaEmbeddedIdentityBinding.status == STATUS_ACTIVE,
        )
        .with_for_update()
        .first()
    )

    if active is not None:
        same_target = (
            active.canonical_store_id == store_id
            and active.integration_id == integration_id
            and active.tenant_id == tenant_id
        )
        if same_target:
            active.verified_at = now
            active.updated_at = now
            active.verified_via = VERIFIED_VIA_OAUTH_RECONCILE
            logger.info(
                "[salla_embedded_binding] refreshed | binding_id=%s merchant_account_id=%s store_id=%s",
                active.id,
                merchant_id,
                store_id,
            )
            return active

        revoke_binding(db, active, reason="oauth_rebound")

    row = SallaEmbeddedIdentityBinding(
        provider=PROVIDER_SALLA,
        app_id=app_id,
        merchant_account_id=merchant_id,
        canonical_store_id=store_id,
        integration_id=integration_id,
        tenant_id=tenant_id,
        verified_via=VERIFIED_VIA_OAUTH_RECONCILE,
        verified_at=now,
        status=STATUS_ACTIVE,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    logger.info(
        "[salla_embedded_binding] created | binding_id=%s merchant_account_id=%s store_id=%s tenant=%s",
        row.id,
        merchant_id,
        store_id,
        tenant_id,
    )
    return row
