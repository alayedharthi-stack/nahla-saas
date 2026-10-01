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
Meta; nothing is created. Items this path never published are never touched.
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

RETIRE_LEDGER_KEY = "wa_catalog_retirements"
RETIRE_STATS_KEY = "wa_catalog_retirement_stats"
RETIRE_LEDGER_MAX = 500
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
    from models import WhatsAppConnection  # noqa: PLC0415

    query = db.query(WhatsAppConnection).filter(
        WhatsAppConnection.tenant_id == int(tenant_id)
    )
    if for_update and _is_postgres(db):
        query = query.with_for_update()
    return query.first()


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
    """Every Graph identity this path may have published for *product*.

    Sources, in order: ``meta_catalog_memberships`` rows (Salla SKUs and
    literal binds), the product's own ``meta_item_id`` with the retailer ids
    recorded in ``sync_meta.expected_payloads_by_retailer_id`` (native rows),
    and ``ProductVariant.retailer_id`` for native variants. Identities are
    deduplicated by retailer_id. A product that was never published yields
    an empty list, so nothing on Meta is touched for it.
    """
    if product is None or not is_channel_copy_owned_by_publish_path(product):
        return []
    tenant_id = int(getattr(product, "tenant_id", 0) or 0)
    product_id = int(getattr(product, "id", 0) or 0)
    if tenant_id <= 0 or product_id <= 0:
        return []
    sync_meta = _read_sync_meta(product)
    ever_published = bool(
        _strip(getattr(product, "meta_item_id", None))
        or getattr(product, "last_synced_at", None) is not None
        or sync_meta.get("expected_payloads_by_retailer_id")
        or _strip(getattr(product, "sync_status", None)).lower()
        in ("synced", "pending_verification", SYNC_STATUS_RETIRED)
    )
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def _add(retailer_id: Any, meta_item_id: Any, catalog_id: Any, source: str) -> None:
        rid = _strip(retailer_id)
        if not rid or rid in seen:
            return
        seen.add(rid)
        out.append(
            {
                "retailer_id": rid,
                "meta_item_id": _strip(meta_item_id) or None,
                "catalog_id": _strip(catalog_id) or None,
                "product_id": product_id,
                "source": source,
            }
        )

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
    for row in rows or []:
        if _strip(getattr(row, "meta_item_id", None)) or ever_published:
            _add(
                getattr(row, "retailer_id", None),
                getattr(row, "meta_item_id", None),
                getattr(row, "catalog_id", None),
                "membership",
            )

    if not ever_published:
        return out

    expected = sync_meta.get("expected_payloads_by_retailer_id")
    if isinstance(expected, dict):
        for rid in expected.keys():
            _add(rid, None, None, "expected_payload")

    product_meta_item = _strip(getattr(product, "meta_item_id", None))
    if product_meta_item:
        from core.catalog import canonical_retailer_id  # noqa: PLC0415

        try:
            rid = canonical_retailer_id(product, fallback_to_synthetic=True)
        except Exception:  # noqa: BLE001
            rid = ""
        if rid and rid not in seen:
            _add(rid, product_meta_item, None, "product_meta_item")
        else:
            for item in out:
                if item["retailer_id"] == rid and not item.get("meta_item_id"):
                    item["meta_item_id"] = product_meta_item

    try:
        from models import ProductVariant  # noqa: PLC0415

        variants = (
            db.query(ProductVariant)
            .filter(
                ProductVariant.tenant_id == tenant_id,
                ProductVariant.product_id == product_id,
            )
            .all()
        )
    except (SQLAlchemyError, AttributeError, TypeError):
        variants = []
    from services.salla_variant_catalog_identity import is_salla_source  # noqa: PLC0415

    if not is_salla_source(product):
        for row in variants or []:
            _add(getattr(row, "retailer_id", None), None, None, "variant")
    return out


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
    if sync_meta.get("retire_pending") and sync_meta.get("retire_reason") == why:
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
        "skipped": False,
        "error_code": None,
    }
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
    default_catalog = _strip(getattr(conn, "meta_catalog_id", None))

    identities = channel_identities_for_product(db, product)
    out["identities"] = len(identities)
    errors: List[str] = []
    results: Dict[str, Any] = {}
    for ident in identities:
        catalog_id = ident.get("catalog_id") or default_catalog
        try:
            res = retire_meta_catalog_item(
                conn,
                catalog_id,
                ident["retailer_id"],
                ident.get("meta_item_id"),
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
        else:
            out["failed"] += 1
            errors.append(str(res.get("error") or "retire_failed"))

    if out["failed"]:
        return _stamp_retire_failure(db, product, out, errors[0], results=results)

    now = _now().isoformat()
    product.sync_status = SYNC_STATUS_RETIRED
    product.sync_error = None
    _write_sync_meta(
        product,
        retire_pending=False,
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


# ── Tenant ledger (for rows that are deleted) ─────────────────────────────

def _ledger(conn: Any) -> List[Dict[str, Any]]:
    meta = getattr(conn, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        return []
    raw = meta.get(RETIRE_LEDGER_KEY)
    return [dict(x) for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []


def _write_ledger(conn: Any, entries: List[Dict[str, Any]], stats_update: Optional[Dict[str, Any]] = None) -> None:
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    meta[RETIRE_LEDGER_KEY] = entries[:RETIRE_LEDGER_MAX]
    if stats_update:
        stats = dict(meta.get(RETIRE_STATS_KEY) or {})
        stats.update(stats_update)
        meta[RETIRE_STATS_KEY] = stats
    conn.extra_metadata = meta
    if getattr(conn, "_sa_instance_state", None) is not None:
        flag_modified(conn, "extra_metadata")


def enqueue_channel_retirement_ledger(
    db: Any,
    tenant_id: int,
    identities: Iterable[Dict[str, Any]],
    *,
    reason: str,
) -> int:
    """Record identities whose product row is about to disappear.

    Must be called in the same transaction as the delete so a crash never
    leaves a deleted row with a live channel copy and no record. Returns
    the number of new ledger entries (duplicates by retailer_id are merged).
    """
    items = [dict(i) for i in identities if _strip((i or {}).get("retailer_id"))]
    if not items:
        return 0
    conn = _load_connection(db, tenant_id, for_update=True)
    if conn is None:
        logger.warning(
            "[WA_CATALOG_RETIRE] ledger skipped tenant=%s: no WhatsApp connection", tenant_id,
        )
        return 0
    entries = _ledger(conn)
    by_rid = {e.get("retailer_id"): e for e in entries}
    added = 0
    now = _now().isoformat()
    for item in items:
        rid = _strip(item.get("retailer_id"))
        if rid in by_rid:
            existing = by_rid[rid]
            existing["reason"] = reason
            if not existing.get("meta_item_id") and item.get("meta_item_id"):
                existing["meta_item_id"] = item.get("meta_item_id")
            continue
        if len(entries) >= RETIRE_LEDGER_MAX:
            logger.warning(
                "[WA_CATALOG_RETIRE] ledger full tenant=%s; retailer_id=%s left to reconciliation",
                tenant_id,
                rid,
            )
            continue
        entry = {
            "retailer_id": rid,
            "meta_item_id": _strip(item.get("meta_item_id")) or None,
            "catalog_id": _strip(item.get("catalog_id")) or None,
            "product_id": item.get("product_id"),
            "reason": reason,
            "created_at": now,
            "attempts": 0,
            "next_attempt_at": None,
            "last_error": None,
            "exhausted": False,
        }
        entries.append(entry)
        by_rid[rid] = entry
        added += 1
    _write_ledger(conn, entries)
    db.flush()
    return added


def ledger_snapshot(conn: Any) -> Dict[str, Any]:
    entries = _ledger(conn)
    meta = getattr(conn, "extra_metadata", None) or {}
    stats = meta.get(RETIRE_STATS_KEY) if isinstance(meta, dict) else {}
    pending = [e for e in entries if not e.get("exhausted")]
    exhausted = [e for e in entries if e.get("exhausted")]
    return {
        "pending": len(pending),
        "exhausted": len(exhausted),
        "done_total": int((stats or {}).get("done_total") or 0),
        "last_done_at": (stats or {}).get("last_done_at"),
        "last_error": (stats or {}).get("last_error"),
    }


def reset_exhausted_ledger_entries(conn: Any) -> int:
    """Give exhausted ledger entries a fresh budget (used by reconciliation)."""
    entries = _ledger(conn)
    reset = 0
    for e in entries:
        if e.get("exhausted"):
            e["exhausted"] = False
            e["attempts"] = 0
            e["next_attempt_at"] = None
            reset += 1
    if reset:
        _write_ledger(conn, entries)
    return reset


def drain_channel_retirement_ledger(
    db: Any,
    tenant_id: int,
    *,
    limit: int = 25,
    client: Any = None,
) -> Dict[str, Any]:
    """Process due ledger entries for one tenant."""
    from services.meta_catalog_push import retire_meta_catalog_item  # noqa: PLC0415

    out: Dict[str, Any] = {"processed": 0, "retired": 0, "absent": 0, "failed": 0, "remaining": 0}
    conn = _load_connection(db, tenant_id, for_update=True)
    if conn is None:
        return out
    entries = _ledger(conn)
    if not entries:
        return out
    now = _now()
    default_catalog = _strip(getattr(conn, "meta_catalog_id", None))
    keep: List[Dict[str, Any]] = []
    done = 0
    last_error: Optional[str] = None
    for entry in entries:
        due = (
            not entry.get("exhausted")
            and (
                _parse_iso_dt(entry.get("next_attempt_at")) is None
                or now >= _parse_iso_dt(entry.get("next_attempt_at"))
            )
        )
        if not due or out["processed"] >= int(limit):
            keep.append(entry)
            continue
        out["processed"] += 1
        try:
            res = retire_meta_catalog_item(
                conn,
                entry.get("catalog_id") or default_catalog,
                entry.get("retailer_id"),
                entry.get("meta_item_id"),
                client=client,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[WA_CATALOG_RETIRE] ledger graph call failed tenant=%s rid=%s",
                tenant_id,
                entry.get("retailer_id"),
            )
            res = {"ok": False, "error": type(exc).__name__}
        if res.get("ok"):
            done += 1
            if res.get("action") == "absent":
                out["absent"] += 1
            else:
                out["retired"] += 1
            continue
        attempts = int(entry.get("attempts") or 0) + 1
        entry["attempts"] = attempts
        entry["last_error"] = str(res.get("error") or "retire_failed")[:200]
        entry["next_attempt_at"] = _backoff(attempts)
        entry["exhausted"] = entry["next_attempt_at"] is None
        last_error = entry["last_error"]
        out["failed"] += 1
        keep.append(entry)
    out["remaining"] = len(keep)
    stats_update: Dict[str, Any] = {}
    if done:
        meta = getattr(conn, "extra_metadata", None) or {}
        stats = meta.get(RETIRE_STATS_KEY) if isinstance(meta, dict) else {}
        stats_update["done_total"] = int((stats or {}).get("done_total") or 0) + done
        stats_update["last_done_at"] = now.isoformat()
    if last_error is not None:
        stats_update["last_error"] = last_error
    _write_ledger(conn, keep, stats_update or None)
    db.commit()
    return out


__all__ = [
    "REASON_CATALOG_INACTIVE",
    "REASON_MANUAL_DELETED",
    "REASON_MERCHANT_HIDDEN",
    "REASON_SOURCE_DELETED",
    "REASON_SOURCE_HIDDEN",
    "RETIRE_LEDGER_KEY",
    "RETIRE_MAX_ATTEMPTS",
    "SYNC_STATUS_RETIRED",
    "attempt_product_channel_retirement",
    "channel_identities_for_product",
    "drain_channel_retirement_ledger",
    "enqueue_channel_retirement_ledger",
    "is_channel_copy_owned_by_publish_path",
    "ledger_snapshot",
    "mark_product_channel_retire_pending",
    "product_has_channel_copy",
    "reset_exhausted_ledger_entries",
    "retirement_is_due",
    "retirement_reason_for",
]
