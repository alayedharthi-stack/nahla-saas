"""
core/webhook_enforcement.py
───────────────────────────
Per-tenant webhook signature enforcement flags.

Phase 1B rolls Salla enforcement out merchant-by-merchant: an operator
verifies the merchant's Partner Portal config, watches three days of
clean audit-mode telemetry, then flips the per-tenant flag here. Only
once every live merchant is flipped do we promote the global
``SALLA_WEBHOOK_ENFORCE_SIGNATURE`` env flag to ``true``.

We piggy-back on the existing ``TenantSettings.extra_metadata`` JSONB
column (no migration needed). The shape lives under a stable namespace::

    extra_metadata = {
      ...,
      "webhook_enforcement": {
        "salla":       {"enforce": true,  "updated_at": "...", "updated_by": "ops@..."},
        "salla_oauth": {"enforce": false, "updated_at": "...", "updated_by": "ops@..."},
        "meta":        {"enforce": false, "updated_at": "...", "updated_by": "ops@..."},
      }
    }

Resolution order (most specific wins):

  1. Per-tenant flag in ``extra_metadata.webhook_enforcement.<provider>.enforce``
  2. Global env flag (e.g. ``SALLA_WEBHOOK_ENFORCE_SIGNATURE``)
  3. Default ``False`` (audit-only)

Public API
──────────
* ``get_tenant_enforcement(db, tenant_id, provider)``        — Optional[bool]
* ``set_tenant_enforcement(db, tenant_id, provider, enable)``— upsert helper
* ``resolve_enforce(db, tenant_id, provider, *, global_default)`` — final bool

Lookups are cheap (one indexed query) and cached per request inside the
caller — we deliberately keep the helper stateless to avoid stale-cache
bugs after operator flips.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger("nahla.webhook_enforcement")

_VALID_PROVIDERS = frozenset({"salla", "salla_oauth", "meta", "zid"})
_NAMESPACE_KEY = "webhook_enforcement"


def _coerce_bool(value: object) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
    return None


def get_tenant_enforcement(
    db: Session,
    tenant_id: Optional[int],
    provider: str,
) -> Optional[bool]:
    """Return the per-tenant override, or ``None`` if unset / invalid.

    Never raises — DB hiccups produce ``None`` and the caller falls back
    to the global default.
    """
    if not tenant_id or provider not in _VALID_PROVIDERS:
        return None

    try:
        from database.models import TenantSettings  # noqa: PLC0415
        row = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == tenant_id)
            .first()
        )
    except Exception as exc:  # noqa: BLE001 — never break webhook ingress on settings lookup
        logger.warning(
            "[webhook_enforcement] TenantSettings lookup failed for tenant=%s provider=%s: %s",
            tenant_id, provider, exc,
        )
        return None

    if row is None or not isinstance(row.extra_metadata, dict):
        return None
    block = row.extra_metadata.get(_NAMESPACE_KEY)
    if not isinstance(block, dict):
        return None
    entry = block.get(provider)
    if not isinstance(entry, dict):
        return None
    return _coerce_bool(entry.get("enforce"))


def set_tenant_enforcement(
    db: Session,
    tenant_id: int,
    provider: str,
    enable: bool,
    *,
    actor: Optional[str] = None,
) -> bool:
    """Persist a per-tenant enforcement flip. Returns ``True`` on success.

    Idempotent: writing the same value twice is a no-op metadata-wise but
    still bumps ``updated_at``. Caller is expected to be an admin route
    or an offline ops script — we do not enforce that here.
    """
    if provider not in _VALID_PROVIDERS:
        raise ValueError(f"Unsupported webhook enforcement provider: {provider}")
    if not tenant_id:
        raise ValueError("tenant_id is required")

    from database.models import TenantSettings  # noqa: PLC0415

    row = (
        db.query(TenantSettings)
        .filter(TenantSettings.tenant_id == tenant_id)
        .first()
    )
    if row is None:
        row = TenantSettings(tenant_id=tenant_id)
        db.add(row)

    meta = dict(row.extra_metadata or {})
    block = dict(meta.get(_NAMESPACE_KEY) or {})
    block[provider] = {
        "enforce": bool(enable),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": actor or "system",
    }
    meta[_NAMESPACE_KEY] = block
    row.extra_metadata = meta

    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception(
            "[webhook_enforcement] failed to commit flip tenant=%s provider=%s: %s",
            tenant_id, provider, exc,
        )
        return False

    logger.info(
        "[webhook_enforcement] tenant=%s provider=%s enforce=%s actor=%s",
        tenant_id, provider, enable, actor or "system",
    )
    return True


def resolve_enforce(
    db: Session,
    tenant_id: Optional[int],
    provider: str,
    *,
    global_default: bool,
) -> bool:
    """Return the effective enforce decision after applying overrides.

    Per-tenant flag, when present, takes precedence. Otherwise we fall
    back to ``global_default`` (typically the env-var flag).
    """
    override = get_tenant_enforcement(db, tenant_id, provider)
    if override is None:
        return bool(global_default)
    return override
