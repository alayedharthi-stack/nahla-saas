"""
services/meta_catalog_claim.py
──────────────────────────────
Cross-tenant isolation for the Meta catalog binding
(``whatsapp_connections.meta_catalog_id``).

One Commerce Manager catalog id must be adopted by **one** tenant. Every
write path that stamps ``meta_catalog_id`` (merchant PATCH, admin PATCH,
admin debug POST, the automatic onboarding) must refuse an id that another
tenant's connection already carries, and two tenants must not be able to
adopt the same id in two concurrent requests.

Mechanism (same as ``meta_catalog_onboarding``, shared lock namespace):

  1. ``pg_advisory_xact_lock(_CATALOG_CLAIM_LOCK_KEY, hashtext(catalog_id))``
     serialises all claims of one catalog id across sessions for the rest
     of the current transaction (released at COMMIT/ROLLBACK). A waiter
     resumes only after the holder committed, so its check below sees the
     holder's row. On SQLite (unit tests) the lock is a no-op.
  2. ``SELECT tenant_id FROM whatsapp_connections WHERE meta_catalog_id = :cid
     AND tenant_id <> :tenant`` — any row means the id is claimed elsewhere.
     A verified catalog-only consent row (``meta_catalog_authorizations``) for
     the same catalog counts as a claim too.

The guard never writes; the caller writes and commits inside the same
transaction so the lock covers the write.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger("nahla.meta_catalog_claim")

ERROR_CATALOG_CLAIMED_BY_OTHER_TENANT = "catalog_claimed_by_other_tenant"
ERROR_CATALOG_CLAIM_LOCK_FAILED = "catalog_claim_lock_failed"

# Shared with services.meta_catalog_onboarding._CATALOG_CLAIM_LOCK_KEY so the
# automatic onboarding path and the manual PATCH paths serialise together.
CATALOG_CLAIM_LOCK_KEY = 904222


class CatalogClaimError(RuntimeError):
    """Raised when a catalog id is (or may be) claimed by another tenant."""

    def __init__(self, code: str, detail: Dict[str, Any]):
        super().__init__(code)
        self.code = code
        self.detail = detail


def is_postgres(db: Any) -> bool:
    bind = db.get_bind() if hasattr(db, "get_bind") else None
    return str(getattr(getattr(bind, "dialect", None), "name", "") or "") == "postgresql"


def acquire_catalog_claim_lock(db: Any, catalog_id: str) -> None:
    """Serialise claims of one catalog id across tenants for this transaction.

    PostgreSQL only; SQLite/unit callers skip. A lock failure raises — the
    caller must fail closed rather than stamp without serialisation.
    """
    from sqlalchemy import text  # noqa: PLC0415

    cid = str(catalog_id or "").strip()
    if not cid or not is_postgres(db):
        return
    try:
        db.execute(
            text("SELECT pg_advisory_xact_lock(:k, hashtext(:c))"),
            {"k": CATALOG_CLAIM_LOCK_KEY, "c": cid},
        )
    except Exception as exc:
        logger.error("[META_CATALOG_CLAIM] advisory lock failed catalog=%s", cid, exc_info=True)
        raise CatalogClaimError(
            ERROR_CATALOG_CLAIM_LOCK_FAILED,
            {"error": ERROR_CATALOG_CLAIM_LOCK_FAILED, "catalog_id": cid},
        ) from exc


def other_tenants_claiming(db: Any, tenant_id: int, catalog_id: str) -> List[int]:
    """Tenant ids (other than *tenant_id*) whose connection carries *catalog_id*."""
    from sqlalchemy import text  # noqa: PLC0415

    cid = str(catalog_id or "").strip()
    if not cid:
        return []
    rows = db.execute(
        text(
            "SELECT tenant_id FROM whatsapp_connections "
            "WHERE meta_catalog_id = :cid AND tenant_id <> :tid "
            "ORDER BY tenant_id"
        ),
        {"cid": cid, "tid": int(tenant_id)},
    ).fetchall()
    tenants = {int(r[0]) for r in rows if r and r[0] is not None}
    # A verified catalog-only consent (``meta_catalog_authorizations``) claims
    # its catalog exactly like a WhatsApp connection binding does.
    from services.meta_catalog_consent import authorization_table_exists  # noqa: PLC0415

    if authorization_table_exists(db):
        consent_rows = db.execute(
            text(
                "SELECT tenant_id FROM meta_catalog_authorizations "
                "WHERE catalog_id = :cid AND tenant_id <> :tid"
            ),
            {"cid": cid, "tid": int(tenant_id)},
        ).fetchall()
        tenants |= {int(r[0]) for r in consent_rows if r and r[0] is not None}
    return sorted(tenants)


def guard_catalog_claim(db: Any, tenant_id: int, catalog_id: str) -> None:
    """Refuse adopting *catalog_id* for *tenant_id* when another tenant holds it.

    Takes the per-catalog advisory lock first so that two concurrent claims
    of the same id are serialised: the second one re-checks after the first
    committed and is refused. Raises ``CatalogClaimError``; never writes.
    """
    cid = str(catalog_id or "").strip()
    if not cid:
        return
    acquire_catalog_claim_lock(db, cid)
    others = other_tenants_claiming(db, tenant_id, cid)
    if others:
        logger.warning(
            "[META_CATALOG_CLAIM] refused tenant=%s catalog=%s claimed_by=%s",
            tenant_id, cid, others,
        )
        raise CatalogClaimError(
            ERROR_CATALOG_CLAIMED_BY_OTHER_TENANT,
            {
                "error": ERROR_CATALOG_CLAIMED_BY_OTHER_TENANT,
                "message_ar": (
                    "هذا الكتالوج مرتبط بحساب تاجر آخر ولا يمكن اعتماده هنا. "
                    "أنشئ كتالوجًا مستقلًا لمتجرك أو تواصل مع الدعم."
                ),
                "catalog_id": cid,
                "claimed_by_tenant_count": len(others),
            },
        )


__all__ = [
    "CATALOG_CLAIM_LOCK_KEY",
    "CatalogClaimError",
    "ERROR_CATALOG_CLAIMED_BY_OTHER_TENANT",
    "ERROR_CATALOG_CLAIM_LOCK_FAILED",
    "acquire_catalog_claim_lock",
    "guard_catalog_claim",
    "is_postgres",
    "other_tenants_claiming",
]
