"""Salla Tenant 1 owner test-store compatibility (feature-flagged, server-side only).

NOT general merchant_id routing. Authorizes only when:
  - feature flag + explicit config are present
  - merchant_account_id equals the configured trusted identity
  - Integration.external_store_id equals trusted identity exactly (PR #857 canonical)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from core.config import (
    SALLA_CLIENT_ID,
    SALLA_TEST_COMPAT_ENABLED,
    SALLA_TEST_COMPAT_TENANT_ID,
    SALLA_TEST_COMPAT_TRUSTED_IDENTITY,
)
from services.salla_store_identity import _str_id, find_salla_integration_by_identity

logger = logging.getLogger("nahla.salla_test_compat")


@dataclass(frozen=True)
class SallaTestCompatMatch:
    tenant_id: int
    integration_id: int
    external_store_id: str
    matched_via: str


def salla_test_compat_config_ready() -> bool:
    """True only when flag is on and required server-side config is explicitly set."""
    if not SALLA_TEST_COMPAT_ENABLED:
        return False
    if not _str_id(SALLA_TEST_COMPAT_TRUSTED_IDENTITY):
        return False
    if SALLA_TEST_COMPAT_TENANT_ID <= 0:
        return False
    return True


def resolve_salla_test_compat_match(
    db: Session,
    *,
    merchant_account_id: str,
    app_id: str = "",
) -> Optional[SallaTestCompatMatch]:
    """Return exact proven owner integration for the flagged test store, else None."""
    if not salla_test_compat_config_ready():
        return None

    mid = _str_id(merchant_account_id)
    trusted = _str_id(SALLA_TEST_COMPAT_TRUSTED_IDENTITY)
    if not mid or mid != trusted:
        return None

    expected_app = _str_id(SALLA_CLIENT_ID)
    supplied_app = _str_id(app_id)
    if expected_app and supplied_app and supplied_app != expected_app:
        logger.warning(
            "[SallaTestCompat] rejected — app_id mismatch | supplied=%s expected=%s",
            supplied_app[:8] + "***",
            expected_app[:8] + "***",
        )
        return None

    owner_row, via = find_salla_integration_by_identity(
        db,
        trusted,
        include_disabled=True,
        allow_alias_match=False,
    )
    if owner_row is None or via != "external_store_id":
        logger.warning(
            "[SallaTestCompat] rejected — no canonical external_store_id owner | trusted_identity=%s",
            trusted,
        )
        return None
    if owner_row.tenant_id != SALLA_TEST_COMPAT_TENANT_ID:
        logger.warning(
            "[SallaTestCompat] rejected — owner tenant mismatch | owner=%s expected=%s trusted_identity=%s",
            owner_row.tenant_id,
            SALLA_TEST_COMPAT_TENANT_ID,
            trusted,
        )
        return None
    if _str_id(owner_row.external_store_id) != trusted:
        logger.warning(
            "[SallaTestCompat] rejected — external_store_id mismatch | row=%s trusted=%s",
            _str_id(owner_row.external_store_id),
            trusted,
        )
        return None

    logger.info(
        "[SallaTestCompat] authorized | tenant_id=%s integration_id=%s external_store_id=%s matched_via=%s",
        owner_row.tenant_id,
        owner_row.id,
        trusted,
        via,
    )
    return SallaTestCompatMatch(
        tenant_id=owner_row.tenant_id,
        integration_id=owner_row.id,
        external_store_id=trusted,
        matched_via=via,
    )
