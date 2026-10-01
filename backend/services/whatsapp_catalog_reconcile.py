"""
services/whatsapp_catalog_reconcile.py
──────────────────────────────────────
Periodic catch-up for the WhatsApp catalog publish path.

Event-driven publish (Salla webhook → pending → drain) stays the primary
path. This module is the safety net for what events cannot prove:

* an item Nahla believes is live but Graph no longer has, or shows with a
  different price / currency / availability (drift) → re-queued;
* a withdrawn item that is still ``in stock`` in Graph → retirement re-queued;
* the WABA ↔ catalog link, re-read from Graph and persisted as evidence;
* exhausted retirement ledger entries, given a fresh retry budget.

It never creates, deletes or edits Graph items itself: it only reads
Graph and marks local rows, and the same drain performs the writes. Reads
are bounded per tenant and at most one tenant is reconciled per tick.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.attributes import flag_modified

from core.catalog import is_whatsapp_channel_publish_eligible

logger = logging.getLogger("nahla.wa_catalog_reconcile")

RECONCILE_META_KEY = "wa_catalog_reconcile"
_RECONCILE_INTERVAL_ENV = "NAHLA_WHATSAPP_CATALOG_RECONCILE_SEC"
DEFAULT_RECONCILE_INTERVAL_SECONDS = 6 * 3600
RECONCILE_MAX_PRODUCTS = 2000
RECONCILE_LIVE_FIELDS = "id,retailer_id,name,price,currency,availability"


def reconcile_interval_seconds() -> int:
    try:
        value = int(os.environ.get(_RECONCILE_INTERVAL_ENV, "") or DEFAULT_RECONCILE_INTERVAL_SECONDS)
    except (TypeError, ValueError):
        value = DEFAULT_RECONCILE_INTERVAL_SECONDS
    return max(300, value)


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def _sync_meta(product: Any) -> Dict[str, Any]:
    meta = getattr(product, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        return {}
    sm = meta.get("sync_meta")
    return dict(sm) if isinstance(sm, dict) else {}


def _persist_snapshot(db: Any, tenant_id: int, payload: Dict[str, Any]) -> None:
    """Write the reconcile snapshot on a freshly loaded, locked connection row.

    The reconcile runs long Graph reads; merging into a row loaded before
    them would overwrite entries other writers (the retirement ledger)
    committed in the meantime.
    """
    from services.whatsapp_catalog_retirement import load_connection_for_metadata_write  # noqa: PLC0415

    conn = load_connection_for_metadata_write(db, tenant_id)
    if conn is None:
        return
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    meta[RECONCILE_META_KEY] = payload
    conn.extra_metadata = meta
    if getattr(conn, "_sa_instance_state", None) is not None:
        flag_modified(conn, "extra_metadata")


def reconcile_snapshot(conn: Any) -> Dict[str, Any]:
    meta = getattr(conn, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        return {}
    snap = meta.get(RECONCILE_META_KEY)
    return dict(snap) if isinstance(snap, dict) else {}


def reconcile_is_due(conn: Any, now: Optional[datetime] = None) -> bool:
    snap = reconcile_snapshot(conn)
    last = _parse_iso_dt(snap.get("at"))
    if last is None:
        return True
    return ((now or _now()) - last).total_seconds() >= reconcile_interval_seconds()


def _normalize_live_item(row: Dict[str, Any]) -> Dict[str, Any]:
    price = row.get("price")
    currency = row.get("currency")
    if not currency and isinstance(price, str):
        parts = price.strip().split()
        if len(parts) == 2 and parts[1].isalpha():
            currency = parts[1]
    return {
        "price": price,
        "currency": currency,
        "availability": row.get("availability"),
    }


def reconcile_tenant_channel_catalog(
    db: Any,
    tenant_id: int,
    *,
    client: Any = None,
    max_products: int = RECONCILE_MAX_PRODUCTS,
    refresh_link: bool = True,
) -> Dict[str, Any]:
    """Read Graph once, compare with local expectations, re-queue drift."""
    from models import WhatsAppConnection  # noqa: PLC0415
    from services.meta_catalog_reconcile import fetch_meta_catalog_live_products  # noqa: PLC0415
    from services.native_meta_sync_orchestrator import (  # noqa: PLC0415
        compare_pushed_content_to_lookup,
        mark_native_meta_sync_pending,
    )
    from services.whatsapp_catalog_retirement import (  # noqa: PLC0415
        mark_product_channel_retire_pending,
        reset_exhausted_ledger_entries,
    )
    from services.whatsapp_catalog_sync import (  # noqa: PLC0415
        evaluate_whatsapp_catalog_sync_readiness,
        iter_tenant_products,
    )

    out: Dict[str, Any] = {
        "ok": False,
        "tenant_id": int(tenant_id),
        "at": _now().isoformat(),
        "catalog_id": None,
        "skipped": False,
        "blocker_code": None,
        "live_items": 0,
        "live_complete": False,
        "checked_products": 0,
        "checked_identities": 0,
        "missing": 0,
        "drifted": 0,
        "requeued": 0,
        "retire_requeued": 0,
        "ledger_reset": 0,
        "waba_link_state": None,
        "error": None,
    }
    conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == int(tenant_id))
        .first()
    )
    if conn is None:
        out["skipped"] = True
        out["blocker_code"] = "connection_not_found"
        return out
    readiness = evaluate_whatsapp_catalog_sync_readiness(db, tenant_id)
    if not readiness.get("ready"):
        # Persist the skip so a blocked tenant does not stay "most due" and
        # starve every other tenant's turn until its blocker clears.
        out["skipped"] = True
        out["blocker_code"] = readiness.get("blocker_code")
        _persist_snapshot(db, tenant_id, out)
        _safe_commit(db)
        return out
    catalog_id = str(getattr(conn, "meta_catalog_id", "") or "").strip()
    out["catalog_id"] = catalog_id or None

    live, info = fetch_meta_catalog_live_products(
        conn, catalog_id, client=client, fields=RECONCILE_LIVE_FIELDS,
    )
    out["live_items"] = int(info.get("items") or 0)
    out["live_complete"] = bool(info.get("complete"))
    if info.get("error") or not info.get("complete"):
        # An incomplete read must never be mistaken for "items missing".
        out["error"] = str(info.get("error") or "live_read_incomplete")[:240]
        _persist_snapshot(db, tenant_id, out)
        _safe_commit(db)
        return out

    checked_products = 0
    for row in iter_tenant_products(db, tenant_id):
        if int(getattr(row, "tenant_id", 0) or 0) != int(tenant_id):
            continue
        if checked_products >= int(max_products):
            out["truncated"] = True
            break
        sm = _sync_meta(row)
        status = str(getattr(row, "sync_status", None) or "").strip().lower()
        eligible = is_whatsapp_channel_publish_eligible(row)

        if eligible and status == "synced":
            expected = sm.get("expected_payloads_by_retailer_id")
            if not isinstance(expected, dict) or not expected:
                continue
            checked_products += 1
            needs_push = False
            for rid, payload in expected.items():
                out["checked_identities"] += 1
                live_row = live.get(str(rid))
                if live_row is None:
                    out["missing"] += 1
                    needs_push = True
                    continue
                comparison = compare_pushed_content_to_lookup(
                    dict(payload or {}), _normalize_live_item(live_row),
                )
                if comparison.get("outcome") == "mismatch":
                    out["drifted"] += 1
                    needs_push = True
            if needs_push:
                try:
                    if mark_native_meta_sync_pending(db, row):
                        out["requeued"] += 1
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[WA_CATALOG_RECONCILE] requeue failed tenant=%s product=%s",
                        tenant_id,
                        getattr(row, "id", None),
                    )
            continue

        if not eligible and (status == "retired" or sm.get("channel_retired_at")) and not sm.get("retire_pending"):
            # Withdrawn rows: anything still sellable in Graph is drift too.
            retire_results = sm.get("retire_results") if isinstance(sm.get("retire_results"), dict) else {}
            rids = list(retire_results.keys()) or list(
                (sm.get("expected_payloads_by_retailer_id") or {}).keys()
                if isinstance(sm.get("expected_payloads_by_retailer_id"), dict) else []
            )
            if not rids:
                continue
            checked_products += 1
            still_live = False
            for rid in rids:
                out["checked_identities"] += 1
                live_row = live.get(str(rid))
                if live_row is None:
                    continue
                av = str(live_row.get("availability") or "").strip().lower().replace("_", " ")
                if av and av != "out of stock":
                    still_live = True
            if still_live:
                meta = dict(getattr(row, "extra_metadata", None) or {})
                sync_meta = dict(meta.get("sync_meta") or {})
                sync_meta["channel_retired_at"] = None
                meta["sync_meta"] = sync_meta
                row.extra_metadata = meta
                if mark_product_channel_retire_pending(db, row):
                    out["retire_requeued"] += 1

    out["checked_products"] = checked_products

    if refresh_link:
        try:
            from services.meta_catalog_linking import get_waba_catalog_link_status  # noqa: PLC0415

            link = get_waba_catalog_link_status(db, tenant_id)
            out["waba_link_state"] = link.get("link_status") if link.get("ok") else None
            out["waba_link_error"] = link.get("error")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[WA_CATALOG_RECONCILE] link status read failed tenant=%s err=%s",
                tenant_id,
                type(exc).__name__,
            )
            out["waba_link_error"] = type(exc).__name__

    out["ok"] = True
    from services.whatsapp_catalog_retirement import load_connection_for_metadata_write  # noqa: PLC0415

    fresh = load_connection_for_metadata_write(db, tenant_id)
    out["ledger_reset"] = reset_exhausted_ledger_entries(fresh) if fresh is not None else 0
    _persist_snapshot(db, tenant_id, out)
    _safe_commit(db)
    logger.info(
        "[WA_CATALOG_RECONCILE] tenant=%s live=%s checked=%s missing=%s drifted=%s requeued=%s retire_requeued=%s link=%s",
        tenant_id,
        out["live_items"],
        out["checked_products"],
        out["missing"],
        out["drifted"],
        out["requeued"],
        out["retire_requeued"],
        out["waba_link_state"],
    )
    return out


def _safe_commit(db: Any) -> None:
    try:
        db.commit()
    except SQLAlchemyError:
        logger.exception("[WA_CATALOG_RECONCILE] commit failed")
        try:
            db.rollback()
        except SQLAlchemyError:
            logger.exception("[WA_CATALOG_RECONCILE] rollback failed")


def reconcile_due_tenants(db: Any, *, max_tenants: int = 1, client: Any = None) -> Dict[str, Any]:
    """Reconcile at most ``max_tenants`` due, catalog-enabled tenants."""
    from models import WhatsAppConnection  # noqa: PLC0415
    from services.whatsapp_catalog_sync import whatsapp_catalog_auto_sync_enabled  # noqa: PLC0415

    summary: Dict[str, Any] = {"tenants": 0, "requeued": 0, "retire_requeued": 0, "errors": 0, "skipped": False}
    if not whatsapp_catalog_auto_sync_enabled():
        summary["skipped"] = True
        return summary
    conns = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.catalog_enabled.is_(True))
        .all()
    )
    now = _now()
    due: List[Any] = []
    for conn in conns:
        if reconcile_is_due(conn, now):
            due.append(conn)
    due.sort(key=lambda c: _parse_iso_dt(reconcile_snapshot(c).get("at")) or datetime.min.replace(tzinfo=timezone.utc))
    for conn in due[: max(1, int(max_tenants))]:
        tid = int(getattr(conn, "tenant_id", 0) or 0)
        if tid <= 0:
            continue
        try:
            result = reconcile_tenant_channel_catalog(db, tid, client=client)
        except Exception:  # noqa: BLE001
            logger.exception("[WA_CATALOG_RECONCILE] tenant reconcile crashed tenant=%s", tid)
            summary["errors"] += 1
            try:
                db.rollback()
            except SQLAlchemyError:
                logger.exception("[WA_CATALOG_RECONCILE] rollback failed tenant=%s", tid)
            continue
        summary["tenants"] += 1
        summary["requeued"] += int(result.get("requeued") or 0)
        summary["retire_requeued"] += int(result.get("retire_requeued") or 0)
        if result.get("error"):
            summary["errors"] += 1
    return summary


def run_whatsapp_catalog_reconcile_tick() -> Dict[str, Any]:
    """Periodic worker entry: own DB session, at most one tenant per tick."""
    from core.database import SessionLocal  # noqa: PLC0415

    db = SessionLocal()
    try:
        return reconcile_due_tenants(db, max_tenants=1)
    except Exception:  # noqa: BLE001
        logger.exception("[WA_CATALOG_RECONCILE] tick failed")
        try:
            db.rollback()
        except SQLAlchemyError:
            logger.exception("[WA_CATALOG_RECONCILE] tick rollback failed")
        return {"tenants": 0, "requeued": 0, "retire_requeued": 0, "errors": 1}
    finally:
        db.close()


__all__ = [
    "DEFAULT_RECONCILE_INTERVAL_SECONDS",
    "RECONCILE_META_KEY",
    "reconcile_due_tenants",
    "reconcile_interval_seconds",
    "reconcile_is_due",
    "reconcile_snapshot",
    "reconcile_tenant_channel_catalog",
    "run_whatsapp_catalog_reconcile_tick",
]
