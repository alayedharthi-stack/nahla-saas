"""Salla Tenant 1 owner test-store compatibility (feature-flagged, server-side only).

NOT general merchant_id routing. Runs only after canonical resolution fails and
only when the trusted identity matches an explicit allowlist anchored to tenant 1.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from core.config import (
    SALLA_TEST_COMPAT_ENABLED,
    SALLA_TEST_COMPAT_TENANT_ID,
    SALLA_TEST_COMPAT_TRUSTED_IDENTITY,
)
from services.salla_store_identity import _str_id, find_salla_integration_by_identity

logger = logging.getLogger("nahla.salla_test_compat")


def _tenant1_integration_anchor(db: Session, trusted_id: str) -> bool:
    """True when tenant 1's Salla integration is explicitly anchored to ``trusted_id``."""
    from models import Integration  # noqa: PLC0415

    rows = (
        db.query(Integration)
        .filter(
            Integration.provider == "salla",
            Integration.tenant_id == SALLA_TEST_COMPAT_TENANT_ID,
        )
        .all()
    )
    for row in rows:
        if _str_id(row.external_store_id) == trusted_id:
            return True
        cfg = row.config or {}
        if _str_id(cfg.get("store_id")) == trusted_id:
            return True
    return False


def resolve_salla_test_compat_tenant(
    db: Session,
    *,
    merchant_account_id: str,
    app_id: str = "",
) -> Optional[int]:
    """Return tenant id for the authorized owner test store, else None."""
    if not SALLA_TEST_COMPAT_ENABLED:
        return None

    mid = _str_id(merchant_account_id)
    trusted = _str_id(SALLA_TEST_COMPAT_TRUSTED_IDENTITY)
    if not mid or not trusted or mid != trusted:
        return None

    owner_row, via = find_salla_integration_by_identity(
        db,
        trusted,
        include_disabled=True,
        allow_alias_match=False,
    )
    if owner_row is not None and owner_row.tenant_id == SALLA_TEST_COMPAT_TENANT_ID and via == "external_store_id":
        logger.info(
            "[SallaTestCompat] authorized via canonical external_store_id | "
            "tenant_id=%s trusted_identity=%s app_id=%s",
            owner_row.tenant_id,
            trusted,
            (app_id[:8] + "***") if app_id else "-",
        )
        return owner_row.tenant_id

    if not _tenant1_integration_anchor(db, trusted):
        logger.warning(
            "[SallaTestCompat] rejected — tenant 1 anchor missing | trusted_identity=%s",
            trusted,
        )
        return None

    logger.info(
        "[SallaTestCompat] authorized via tenant-1 anchor | tenant_id=%s trusted_identity=%s app_id=%s",
        SALLA_TEST_COMPAT_TENANT_ID,
        trusted,
        (app_id[:8] + "***") if app_id else "-",
    )
    return SALLA_TEST_COMPAT_TENANT_ID
