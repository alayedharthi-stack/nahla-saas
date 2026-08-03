"""
Canonical tenant commerce permission loader for MerchantBrain and orchestrator.

Loads ``CommercePermissions`` rows via an existing SQLAlchemy session when
provided; never opens a nested SessionLocal in that path.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Optional

from modules.ai.commerce.permissions import CommercePermissionSet

logger = logging.getLogger("nahla.ai.commerce.permission_loader")

PermissionSource = Literal["db_row", "defaults_missing_row", "load_failed"]


@dataclass(frozen=True)
class PermissionLoadResult:
    permissions: CommercePermissionSet
    source: PermissionSource
    ok: bool = True


def _default_permissions(tenant_id: int) -> CommercePermissionSet:
    """Mirror CommercePermissionSet server defaults when no DB row exists."""
    return CommercePermissionSet(tenant_id=tenant_id)


def _fail_closed_permissions(tenant_id: int) -> CommercePermissionSet:
    """Deny all sensitive commerce flags; read/search/track remain allowed."""
    return CommercePermissionSet(
        tenant_id=tenant_id,
        can_create_orders=False,
        can_create_checkout_links=False,
        can_send_payment_links=False,
        can_apply_coupons=False,
        can_auto_generate_coupons=False,
        can_cancel_orders=False,
    )


def load_tenant_commerce_permissions(
    db: Any,
    tenant_id: int,
) -> PermissionLoadResult:
    """
    Load tenant commerce permissions from ``commerce_permissions``.

    Returns:
      - ``db_row`` when a row exists
      - ``defaults_missing_row`` when no row (preserves legacy all-True defaults)
      - ``load_failed`` on exception (fail-closed sensitive flags)
    """
    try:
        from database.models import CommercePermissions

        row = (
            db.query(CommercePermissions)
            .filter(CommercePermissions.tenant_id == tenant_id)
            .first()
        )
        if not row:
            logger.info(
                "[commerce_permissions] tenant=%s source=defaults_missing_row",
                tenant_id,
            )
            return PermissionLoadResult(
                permissions=_default_permissions(tenant_id),
                source="defaults_missing_row",
                ok=True,
            )

        permissions = CommercePermissionSet(
            tenant_id=tenant_id,
            can_create_orders=bool(row.can_create_orders),
            can_create_checkout_links=bool(row.can_create_checkout_links),
            can_send_payment_links=bool(row.can_send_payment_links),
            can_apply_coupons=bool(row.can_apply_coupons),
            can_auto_generate_coupons=bool(row.can_auto_generate_coupons),
            can_cancel_orders=bool(row.can_cancel_orders),
        )
        logger.info(
            "[commerce_permissions] tenant=%s source=db_row flags=%s",
            tenant_id,
            permissions.to_dict(),
        )
        return PermissionLoadResult(
            permissions=permissions,
            source="db_row",
            ok=True,
        )
    except Exception as exc:
        logger.warning(
            "[commerce_permissions] tenant=%s source=load_failed error=%s",
            tenant_id,
            exc,
        )
        return PermissionLoadResult(
            permissions=_fail_closed_permissions(tenant_id),
            source="load_failed",
            ok=False,
        )


def load_tenant_commerce_permissions_standalone(tenant_id: int) -> PermissionLoadResult:
    """Legacy/orchestrator entry: opens and closes its own DB session."""
    from database.session import SessionLocal

    db = SessionLocal()
    try:
        return load_tenant_commerce_permissions(db, tenant_id)
    finally:
        db.close()
