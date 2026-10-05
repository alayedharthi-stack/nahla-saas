"""
services/whatsapp_catalog_retirement.py
───────────────────────────────────────
Withdraw channel copies when a product stops being sellable.

A product that is hidden by the merchant, hidden or deleted in its source
store (Salla), or deleted in Nahla must not stay live in the Meta catalog
that WhatsApp shows to customers. This module owns that transition:

* ``mark_product_channel_retire_pending`` — the product row stays; its
  ``sync_meta`` carries the retirement request and the drain executes it.
* ``enqueue_channel_retirement_ledger`` — for rows that are about to be
  deleted the identities are written to a tenant-scoped ledger on the
  WhatsApp connection row, so the Graph write survives the product delete.
* ``attempt_product_channel_retirement`` / ``drain_channel_retirement_ledger``
  — executed by the same WhatsApp catalog drain, with the same flag, the
  same tenant readiness and the same bounded retry budget.

The channel write is ``availability=out of stock`` + ``visibility=staging``
on the existing Graph item, verified by a Graph read. Nothing is deleted on
Meta; nothing is created. Items this path never published are never touched:
an identity is retired only with the publish path's own publication evidence
(a ``meta_catalog_memberships`` row with a publication provenance and the
Graph item id), checked again against the live item before every write. The
ledger keeps a copy of that evidence for rows that are deleted, because the
membership row is deleted with the product.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.attributes import flag_modified

from core.catalog import (
    META_EXISTING_SOURCES,
    OWNERSHIP_META_READONLY,
    OWNERSHIP_NAHLA_MANAGED_META,
    SOURCE_NAHLA_MANAGED_META,
    infer_ownership_mode,
    is_hidden_at_source,
    is_whatsapp_channel_publish_eligible,
    normalize_source,
)

logger = logging.getLogger("nahla.wa_catalog_retirement")

RETIRE_MAX_ATTEMPTS = 5
RETIRE_BACKOFF_SECONDS = (60, 300, 900, 1800, 3600)

REASON_MERCHANT_HIDDEN = "merchant_hidden"
REASON_SOURCE_HIDDEN = "source_hidden"
REASON_SOURCE_DELETED = "source_deleted"
REASON_MANUAL_DELETED = "manual_deleted"
REASON_CATALOG_INACTIVE = "catalog_inactive"

SYNC_STATUS_RETIRED = "retired"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _strip(value: Any) -> str:
    return str(value or "").strip()


def _parse_iso_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _read_sync_meta(product: Any) -> Dict[str, Any]:
    meta = getattr(product, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        return {}
    sync_meta = meta.get("sync_meta")
    return dict(sync_meta) if isinstance(sync_meta, dict) else {}


def _write_sync_meta(product: Any, **updates: Any) -> Dict[str, Any]:
    meta = dict(getattr(product, "extra_metadata", None) or {})
    sync_meta = dict(meta.get("sync_meta") or {})
    sync_meta.update(updates)
    meta["sync_meta"] = sync_meta
    product.extra_metadata = meta
    if getattr(product, "_sa_instance_state", None) is not None:
        flag_modified(product, "extra_metadata")
    return sync_meta


def _is_postgres(db: Any) -> bool:
    try:
        bind = db.get_bind() if hasattr(db, "get_bind") else None
    except Exception:  # noqa: BLE001
        return False
    return str(getattr(getattr(bind, "dialect", None), "name", "") or "") == "postgresql"


def _load_connection(db: Any, tenant_id: int, *, for_update: bool = False) -> Any:
    """Load the tenant connection with committed column values.

    ``populate_existing`` refreshes an identity the session already holds,
    so a JSON merge never starts from a stale ``extra_metadata`` snapshot.
    ``for_update`` adds a row lock on PostgreSQL for read-modify-write.
    """
    from models import WhatsAppConnection  # noqa: PLC0415

    query = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == int(tenant_id))
        .populate_existing()
    )
    if for_update and _is_postgres(db):
        query = query.with_for_update()
    return query.first()


def load_connection_for_metadata_write(db: Any, tenant_id: int) -> Any:
    """Locked, refreshed connection row for any ``extra_metadata`` merge."""
    return _load_connection(db, tenant_id, for_update=True)


# ── Ownership ─────────────────────────────────────────────────────────────

def is_channel_copy_owned_by_publish_path(product: Any) -> bool:
    """Only items this publish path created may be withdrawn.

    Rows imported from an existing Meta catalog (``meta_readonly``) or
    managed through Meta (``nahla_managed_meta``) are not ours to touch.
    """
    if product is None:
        return False
    mode = infer_ownership_mode(product)
    if mode in (OWNERSHIP_META_READONLY, OWNERSHIP_NAHLA_MANAGED_META):
        return False
    src = normalize_source(getattr(product, "source", None))
    if src in META_EXISTING_SOURCES or src == SOURCE_NAHLA_MANAGED_META:
        return False
    return True


# ── Identities ────────────────────────────────────────────────────────────

def channel_identities_for_product(db: Any, product: Any) -> List[Dict[str, Any]]:
    """Every Graph identity this path can prove it published for *product*.

    The only accepted proof is the publish path's: a ``meta_catalog_memberships``
    row for this tenant and product with a publication provenance and a Graph
    item id. Reconcile-derived memberships, the legacy ``Product.meta_item_id``
    stamp, ``sync_meta`` expectations and variant retailer ids prove presence or
    intent, never publication, so they yield no identity and nothing on Meta is
    touched for them. Identities are deduplicated by (catalog, retailer_id).
    """
    if product is None or not is_channel_copy_owned_by_publish_path(product):
        return []
    tenant_id = int(getattr(product, "tenant_id", 0) or 0)
    product_id = int(getattr(product, "id", 0) or 0)
    if tenant_id <= 0 or product_id <= 0:
        return []
    from core.meta_catalog_membership import PUBLICATION_PROVENANCES  # noqa: PLC0415

    try:
        from models import MetaCatalogMembership  # noqa: PLC0415

        rows = (
            db.query(MetaCatalogMembership)
            .filter(
                MetaCatalogMembership.tenant_id == tenant_id,
                MetaCatalogMembership.product_id == product_id,
            )
            .all()
        )
    except (SQLAlchemyError, AttributeError, TypeError):
        rows = []
    out: List[Dict[str, Any]] = []
    seen: set[tuple] = set()
    for row in rows or []:
        rid = _strip(getattr(row, "retailer_id", None))
        mid = _strip(getattr(row, "meta_item_id", None))
        cid = _strip(getattr(row, "catalog_id", None))
        prov = _strip(getattr(row, "provenance", None))
        if not rid or not mid or not cid or prov not in PUBLICATION_PROVENANCES:
            continue
        if (cid, rid) in seen:
            continue
        seen.add((cid, rid))
        out.append(
            {
                "retailer_id": rid,
                "meta_item_id": mid,
                "catalog_id": cid,
                "product_id": product_id,
                "source": "membership",
                "publication_provenance": prov,
            }
        )
    return out


def ledger_publication_evidence(row: Any) -> Dict[str, Any]:
    """The ledger copy of the publication evidence for one deleted identity.

    Ledger rows are written only from ``channel_identities_for_product`` (see
    ``enqueue_channel_retirement_ledger``), in the transaction that deletes the
    product and with it the membership row. A row without a Graph item id or a
    catalog carries no evidence and is never retired.
    """
    mid = _strip(getattr(row, "meta_item_id", None))
    cid = _strip(getattr(row, "catalog_id", None))
    rid = _strip(getattr(row, "retailer_id", None))
    owned = bool(mid and cid and rid)
    return {
        "owned": owned,
        "source": "ledger:publication_membership" if owned else None,
        "meta_product_id": mid or None,
        "catalog_id": cid or None,
        "retailer_id": rid,
        "reasons": [] if owned else ["ledger_row_without_publication_evidence"],
    }


# ── Product-row retirement request ────────────────────────────────────────

def retirement_reason_for(product: Any) -> Optional[str]:
    """Why a non-eligible row needs its channel copy withdrawn, or None."""
    if product is None:
        return None
    if getattr(product, "merchant_hidden_at", None):
        return REASON_MERCHANT_HIDDEN
    if is_hidden_at_source(product):
        return REASON_SOURCE_HIDDEN
    from core.catalog import CATALOG_STATUS_ACTIVE, catalog_status_of  # noqa: PLC0415

    if catalog_status_of(product) != CATALOG_STATUS_ACTIVE:
        return REASON_CATALOG_INACTIVE
    return None


def product_has_channel_copy(product: Any) -> bool:
    """Local evidence that a channel copy exists (or may exist) for the row."""
    if product is None:
        return False
    sync_meta = _read_sync_meta(product)
    if sync_meta.get("channel_retired_at") and not sync_meta.get("retire_pending"):
        return False
    status = _strip(getattr(product, "sync_status", None)).lower()
    return bool(
        _strip(getattr(product, "meta_item_id", None))
        or getattr(product, "last_synced_at", None) is not None
        or sync_meta.get("expected_payloads_by_retailer_id")
        or status in ("synced", "pending_verification", "syncing")
    )


def mark_product_channel_retire_pending(
    db: Any,
    product: Any,
    *,
    reason: Optional[str] = None,
) -> bool:
    """Request withdrawal of the product's channel copy. Idempotent.

    Returns False when the row is still publish-eligible, or never had a
    channel copy, or already carries the same pending request.
    """
    if product is None or not is_channel_copy_owned_by_publish_path(product):
        return False
    if is_whatsapp_channel_publish_eligible(product):
        return False
    if not product_has_channel_copy(product):
        return False
    if retirement_reason_for(product) is None:
        # Not eligible for an ownership reason, not because it was withdrawn.
        return False
    sync_meta = _read_sync_meta(product)
    why = reason or retirement_reason_for(product) or REASON_CATALOG_INACTIVE
    if (
        sync_meta.get("retire_pending")
        and sync_meta.get("retire_reason") == why
        and not sync_meta.get("retire_exhausted")
    ):
        return True
    now = _now().isoformat()
    _write_sync_meta(
        product,
        retire_pending=True,
        retire_reason=why,
        retire_requested_at=now,
        retire_attempts=0,
        next_retire_at=None,
        retire_exhausted=False,
        retire_last_error=None,
        dirty=False,
    )
    try:
        db.flush()
    except SQLAlchemyError:
        logger.exception(
            "[WA_CATALOG_RETIRE] flush failed product=%s",
            getattr(product, "id", None),
        )
        return False
    return True


def retirement_is_due(product: Any, now: Optional[datetime] = None) -> bool:
    sync_meta = _read_sync_meta(product)
    if not sync_meta.get("retire_pending"):
        return False
    if sync_meta.get("retire_exhausted"):
        return False
    nxt = _parse_iso_dt(sync_meta.get("next_retire_at"))
    if nxt is None:
        return True
    return (now or _now()) >= nxt


def _backoff(attempts: int) -> Optional[str]:
    if attempts >= RETIRE_MAX_ATTEMPTS:
        return None
    delay = RETIRE_BACKOFF_SECONDS[min(attempts - 1, len(RETIRE_BACKOFF_SECONDS) - 1)]
    return (_now() + timedelta(seconds=delay)).isoformat()


def attempt_product_channel_retirement(
    db: Any,
    tenant_id: int,
    product_id: int,
    *,
    client: Any = None,
) -> Dict[str, Any]:
    """Execute one pending product retirement. Idempotent Graph writes."""
    from models import Product  # noqa: PLC0415
    from services.meta_catalog_push import (  # noqa: PLC0415
        MetaCatalogPushError,
        _resolve_connection,
        live_item_publication_evidence,
        retire_meta_catalog_item,
    )

    out: Dict[str, Any] = {
        "ok": False,
        "tenant_id": int(tenant_id),
        "product_id": int(product_id),
        "identities": 0,
        "retired": 0,
        "absent": 0,
        "failed": 0,
        "refused": 0,
        "skipped": False,
        "error_code": None,
    }
    from services.whatsapp_catalog_sync_scope import SCOPE_BLOCKER_CODE, product_in_sync_scope  # noqa: PLC0415

    if not product_in_sync_scope(tenant_id, product_id):
        # Outside the trial scope: leave the request pending, touch nothing.
        out["skipped"] = True
        out["error_code"] = SCOPE_BLOCKER_CODE
        return out
    product = (
        db.query(Product)
        .filter(Product.id == int(product_id), Product.tenant_id == int(tenant_id))
        .first()
    )
    if product is None:
        out["skipped"] = True
        out["error_code"] = "product_not_found"
        return out
    sync_meta = _read_sync_meta(product)
    if not sync_meta.get("retire_pending"):
        out["skipped"] = True
        out["error_code"] = "not_pending"
        return out
    if is_whatsapp_channel_publish_eligible(product):
        # Restored before the drain ran: the publish path owns it again.
        _write_sync_meta(product, retire_pending=False, retire_reason=None, next_retire_at=None)
        db.commit()
        out["ok"] = True
        out["skipped"] = True
        out["error_code"] = "eligible_again"
        return out

    try:
        conn = _resolve_connection(db, int(tenant_id))
    except MetaCatalogPushError as exc:
        return _stamp_retire_failure(db, product, out, exc.code)
    identities = channel_identities_for_product(db, product)
    out["identities"] = len(identities)
    if not identities:
        # Nothing this path can prove it published: no Graph read or write,
        # and the row is not marked retired, because nothing was withdrawn.
        _write_sync_meta(
            product,
            retire_pending=False,
            next_retire_at=None,
            retire_last_error=None,
            retire_blocked="no_publication_evidence",
        )
        db.commit()
        out["ok"] = True
        out["skipped"] = True
        out["error_code"] = "no_publication_evidence"
        return out
    errors: List[str] = []
    results: Dict[str, Any] = {}
    for ident in identities:
        catalog_id = ident["catalog_id"]
        evidence = live_item_publication_evidence(
            db,
            tenant_id=int(tenant_id),
            catalog_id=catalog_id,
            retailer_id=ident["retailer_id"],
            meta_product_id=ident["meta_item_id"],
            parent=product,
        )
        try:
            res = retire_meta_catalog_item(
                conn,
                catalog_id,
                ident["retailer_id"],
                ident.get("meta_item_id"),
                publication_evidence=evidence,
                client=client,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[WA_CATALOG_RETIRE] graph call failed tenant=%s product=%s rid=%s",
                tenant_id,
                product_id,
                ident["retailer_id"],
            )
            res = {"ok": False, "error": f"{type(exc).__name__}"}
        results[ident["retailer_id"]] = {
            "ok": bool(res.get("ok")),
            "action": res.get("action"),
            "error": res.get("error"),
            "visibility_applied": res.get("visibility_applied"),
        }
        if res.get("ok"):
            if res.get("action") == "absent":
                out["absent"] += 1
            else:
                out["retired"] += 1
        elif res.get("action") == "block_ownership_unverified" or res.get("error") == "catalog_not_current":
            # Not provably ours (or no longer in this tenant's catalog): never
            # written, never retried.
            out["refused"] += 1
        else:
            out["failed"] += 1
            errors.append(str(res.get("error") or "retire_failed"))

    if out["failed"]:
        return _stamp_retire_failure(db, product, out, errors[0], results=results)
    if out["refused"]:
        # Some identity was refused: the row is not reported as retired,
        # because a channel copy this path cannot prove it owns may remain.
        _write_sync_meta(
            product,
            retire_pending=False,
            next_retire_at=None,
            retire_results=results,
            retire_last_error=None,
            retire_blocked="no_publication_evidence",
        )
        db.commit()
        out["ok"] = True
        out["error_code"] = "no_publication_evidence"
        return out

    now = _now().isoformat()
    product.sync_status = SYNC_STATUS_RETIRED
    product.sync_error = None
    _write_sync_meta(
        product,
        retire_pending=False,
        retire_blocked=None,
        channel_retired_at=now,
        retire_results=results,
        retire_last_error=None,
        next_retire_at=None,
        retire_exhausted=False,
        content_verified=False,
        last_error_code=None,
        last_error_summary=None,
    )
    db.commit()
    out["ok"] = True
    logger.info(
        "[WA_CATALOG_RETIRE] tenant=%s product=%s identities=%s retired=%s absent=%s reason=%s",
        tenant_id,
        product_id,
        out["identities"],
        out["retired"],
        out["absent"],
        sync_meta.get("retire_reason"),
    )
    return out


def _stamp_retire_failure(
    db: Any,
    product: Any,
    out: Dict[str, Any],
    error_code: str,
    *,
    results: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    sync_meta = _read_sync_meta(product)
    attempts = int(sync_meta.get("retire_attempts") or 0) + 1
    next_at = _backoff(attempts)
    _write_sync_meta(
        product,
        retire_attempts=attempts,
        next_retire_at=next_at,
        retire_exhausted=next_at is None,
        retire_last_error=str(error_code)[:200],
        retire_results=results or sync_meta.get("retire_results"),
    )
    try:
        db.commit()
    except SQLAlchemyError:
        logger.exception("[WA_CATALOG_RETIRE] failure stamp commit failed product=%s", product.id)
        db.rollback()
    out["ok"] = False
    out["error_code"] = str(error_code)
    out["attempts"] = attempts
    out["exhausted"] = next_at is None
    return out


# ── Durable retirement ledger (rows that are deleted) ─────────────────────
#
# ``catalog_channel_retirements`` rows are written in the same transaction
# as the product delete. They have no FK to ``products`` and live outside
# ``whatsapp_connections.extra_metadata``, so neither the delete nor any
# concurrent JSON writer (reconnect bind, onboarding ensure, token refresh,
# reconcile snapshot) can erase them. There is no cap: every identity of a
# deleted product is recorded or the delete does not happen.

LEDGER_STATUS_PENDING = "pending"
LEDGER_STATUS_DONE = "done"
LEDGER_STATUS_EXHAUSTED = "exhausted"
# Terminal: the row carries no publication evidence, so it is never retired.
LEDGER_STATUS_REFUSED = "refused"


def _ledger_model():
    from models import CatalogChannelRetirement  # noqa: PLC0415

    return CatalogChannelRetirement


def enqueue_channel_retirement_ledger(
    db: Any,
    tenant_id: int,
    identities: Iterable[Dict[str, Any]],
    *,
    reason: str,
    catalog_id: Optional[str] = None,
) -> int:
    """Record identities whose product row is about to disappear.

    Must be called in the same transaction as the delete so a crash never
    leaves a deleted row with a live channel copy and no record. Returns
    the number of new ledger rows; an existing row for the same retailer_id
    is re-opened as ``pending`` with a fresh budget (duplicates merge).
    Raises when a row cannot be written, so the caller's delete rolls back.

    Only identities that carry the publish path's publication evidence (as
    returned by ``channel_identities_for_product``: publication provenance,
    Graph item id and catalog) are recorded; the ledger row is the copy of
    that evidence that outlives the deleted membership. Anything else is
    skipped, so nothing unproven is ever queued for a Graph write.
    """
    from core.meta_catalog_membership import PUBLICATION_PROVENANCES  # noqa: PLC0415

    model = _ledger_model()
    items = [
        dict(i)
        for i in identities
        if _strip((i or {}).get("retailer_id"))
        and _strip((i or {}).get("meta_item_id"))
        and _strip((i or {}).get("catalog_id"))
        and _strip((i or {}).get("publication_provenance")) in PUBLICATION_PROVENANCES
    ]
    if not items:
        return 0
    now = _now()
    added = 0
    for item in items:
        rid = _strip(item.get("retailer_id"))
        cid = _strip(item.get("catalog_id"))
        query = db.query(model).filter(
            model.tenant_id == int(tenant_id),
            model.retailer_id == rid,
        )
        existing = query.filter(model.catalog_id == cid).first()
        if existing is not None:
            existing.reason = str(reason)
            existing.status = LEDGER_STATUS_PENDING
            existing.attempts = 0
            existing.next_attempt_at = None
            existing.last_error = None
            existing.updated_at = now
            existing.done_at = None
            # the evidence just read wins: it names the item this path published
            existing.meta_item_id = _strip(item.get("meta_item_id"))
            if existing.product_id is None and item.get("product_id") is not None:
                existing.product_id = int(item["product_id"])
            continue
        db.add(
            model(
                tenant_id=int(tenant_id),
                catalog_id=cid,
                retailer_id=rid,
                meta_item_id=_strip(item.get("meta_item_id")),
                product_id=int(item["product_id"]) if item.get("product_id") is not None else None,
                reason=str(reason),
                status=LEDGER_STATUS_PENDING,
                attempts=0,
                next_attempt_at=None,
                last_error=None,
                created_at=now,
                updated_at=now,
            )
        )
        added += 1
    db.flush()
    return added


def ledger_snapshot(db: Any, tenant_id: int) -> Dict[str, Any]:
    model = _ledger_model()
    rows = db.query(model).filter(model.tenant_id == int(tenant_id)).all()
    pending = [r for r in rows if r.status == LEDGER_STATUS_PENDING]
    exhausted = [r for r in rows if r.status == LEDGER_STATUS_EXHAUSTED]
    refused = [r for r in rows if r.status == LEDGER_STATUS_REFUSED]
    done = [r for r in rows if r.status == LEDGER_STATUS_DONE]
    last_done = max((r.done_at for r in done if r.done_at is not None), default=None)
    last_error = None
    errored = [r for r in rows if r.last_error and r.status != LEDGER_STATUS_DONE]
    if errored:
        errored.sort(key=lambda r: r.updated_at or r.created_at)
        last_error = errored[-1].last_error
    return {
        "pending": len(pending),
        "exhausted": len(exhausted),
        "refused": len(refused),
        "done_total": len(done),
        "last_done_at": last_done.isoformat() if last_done is not None else None,
        "last_error": last_error,
    }


def reset_exhausted_ledger_entries(db: Any, tenant_id: int) -> int:
    """Give exhausted ledger rows a fresh budget (used by reconciliation)."""
    model = _ledger_model()
    rows = (
        db.query(model)
        .filter(model.tenant_id == int(tenant_id), model.status == LEDGER_STATUS_EXHAUSTED)
        .all()
    )
    now = _now()
    for row in rows:
        row.status = LEDGER_STATUS_PENDING
        row.attempts = 0
        row.next_attempt_at = None
        row.updated_at = now
    if rows:
        db.flush()
    return len(rows)


def _ledger_row_due(row: Any, now: datetime) -> bool:
    if row.status != LEDGER_STATUS_PENDING:
        return False
    nxt = row.next_attempt_at
    if nxt is None:
        return True
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=timezone.utc)
    return now >= nxt


def drain_channel_retirement_ledger(
    db: Any,
    tenant_id: int,
    *,
    limit: int = 25,
    client: Any = None,
) -> Dict[str, Any]:
    """Process due ledger rows for one tenant.

    Graph calls run first against a plain read; each row is then updated by
    primary key, so rows inserted by a concurrent delete are never touched.
    Out-of-scope tenants and product-limited trials are refused here as
    well as in the Graph helper itself.
    """
    from services.meta_catalog_push import retire_meta_catalog_item  # noqa: PLC0415
    from services.whatsapp_catalog_sync_scope import product_in_sync_scope, tenant_in_sync_scope  # noqa: PLC0415

    out: Dict[str, Any] = {
        "processed": 0, "retired": 0, "absent": 0, "failed": 0, "refused": 0,
        "remaining": 0, "skipped_scope": 0,
    }
    if not tenant_in_sync_scope(tenant_id):
        out["skipped_scope"] = 1
        return out
    model = _ledger_model()
    conn = _load_connection(db, tenant_id)
    if conn is None:
        return out
    now = _now()
    rows = (
        db.query(model)
        .filter(model.tenant_id == int(tenant_id), model.status == LEDGER_STATUS_PENDING)
        .order_by(model.id.asc())
        .all()
    )
    outcomes: Dict[int, Dict[str, Any]] = {}
    observed: Dict[int, tuple] = {}
    for row in rows:
        if not _ledger_row_due(row, now):
            continue
        if not product_in_sync_scope(tenant_id, row.product_id):
            out["skipped_scope"] += 1
            continue
        if out["processed"] >= int(limit):
            break
        out["processed"] += 1
        observed[int(row.id)] = (int(row.attempts or 0), row.updated_at, row.reason)
        evidence = ledger_publication_evidence(row)
        if not evidence["owned"]:
            res = {"ok": False, "error": "no_publication_evidence", "refused": True}
        else:
            try:
                res = retire_meta_catalog_item(
                    conn,
                    _strip(row.catalog_id),
                    row.retailer_id,
                    row.meta_item_id,
                    publication_evidence=evidence,
                    client=client,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "[WA_CATALOG_RETIRE] ledger graph call failed tenant=%s rid=%s",
                    tenant_id,
                    row.retailer_id,
                )
                res = {"ok": False, "error": type(exc).__name__}
        outcomes[int(row.id)] = res

    db.expire_all()
    for row_id, res in outcomes.items():
        query = db.query(model).filter(model.id == int(row_id), model.tenant_id == int(tenant_id))
        if _is_postgres(db):
            query = query.with_for_update()
        row = query.first()
        if row is None:
            continue
        if (int(row.attempts or 0), row.updated_at, row.reason) != observed.get(int(row_id)):
            # Re-opened (product re-created and re-deleted) while Graph ran:
            # the new request stands; this outcome belongs to the old one.
            out["reopened"] = int(out.get("reopened") or 0) + 1
            continue
        row.updated_at = now
        if res.get("ok"):
            row.status = LEDGER_STATUS_DONE
            row.done_at = now
            row.last_error = None
            if res.get("action") == "absent":
                out["absent"] += 1
            else:
                out["retired"] += 1
            continue
        if res.get("refused") or res.get("action") == "block_ownership_unverified" or res.get("error") == "catalog_not_current":
            # No publication evidence for this catalog: never retried, never written.
            row.status = LEDGER_STATUS_REFUSED
            row.last_error = str(res.get("error") or "no_publication_evidence")[:255]
            row.next_attempt_at = None
            out["refused"] += 1
            continue
        attempts = int(row.attempts or 0) + 1
        row.attempts = attempts
        row.last_error = str(res.get("error") or "retire_failed")[:255]
        next_at = _backoff(attempts)
        row.next_attempt_at = _parse_iso_dt(next_at) if next_at else None
        row.status = LEDGER_STATUS_EXHAUSTED if next_at is None else LEDGER_STATUS_PENDING
        out["failed"] += 1
    if outcomes:
        db.commit()
    out["remaining"] = (
        db.query(model)
        .filter(model.tenant_id == int(tenant_id), model.status == LEDGER_STATUS_PENDING)
        .count()
    )
    return out


__all__ = [
    "LEDGER_STATUS_DONE",
    "LEDGER_STATUS_EXHAUSTED",
    "LEDGER_STATUS_PENDING",
    "LEDGER_STATUS_REFUSED",
    "REASON_CATALOG_INACTIVE",
    "REASON_MANUAL_DELETED",
    "REASON_MERCHANT_HIDDEN",
    "REASON_SOURCE_DELETED",
    "REASON_SOURCE_HIDDEN",
    "RETIRE_MAX_ATTEMPTS",
    "SYNC_STATUS_RETIRED",
    "attempt_product_channel_retirement",
    "channel_identities_for_product",
    "drain_channel_retirement_ledger",
    "enqueue_channel_retirement_ledger",
    "ledger_publication_evidence",
    "is_channel_copy_owned_by_publish_path",
    "ledger_snapshot",
    "load_connection_for_metadata_write",
    "mark_product_channel_retire_pending",
    "product_has_channel_copy",
    "reset_exhausted_ledger_entries",
    "retirement_is_due",
    "retirement_reason_for",
]
