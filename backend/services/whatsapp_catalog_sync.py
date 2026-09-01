"""
services/whatsapp_catalog_sync.py
─────────────────────────────────
Tenant-scoped WhatsApp catalog publish: auto drain + manual enqueue.

Uses the same per-product orchestrator as native Meta sync. Does not
convert external-platform products into native rows.
"""
from __future__ import annotations

import hashlib
import logging
import os
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Set

from sqlalchemy.exc import SQLAlchemyError

from core.catalog import (
    CATALOG_STATUS_ACTIVE,
    META_EXISTING_SOURCES,
    OWNERSHIP_META_READONLY,
    OWNERSHIP_NAHLA_MANAGED_META,
    catalog_status_of,
    infer_ownership_mode,
    is_whatsapp_channel_publish_eligible,
    normalize_source,
)
from core.plan_entitlements import EntitlementLookupUnavailable, get_entitlements
from services.native_meta_sync_orchestrator import (
    CONTENT_LOOKUP_FIELDS,
    IDENTITY_LOOKUP_FIELDS,
    LOOKUP_UNVERIFIED_FIELDS,
    MAX_VERIFY_LAG_RETRIES,
    attempt_native_meta_sync,
    CatalogSyncSessionUnusable,
    classify_block_code,
    mark_native_meta_sync_pending,
    retry_is_due,
    sync_error_summary,
    verify_retry_is_due,
    _invalidate_sync_session,
    _syncing_is_stale,
)

logger = logging.getLogger(__name__)

_AUTO_SYNC_ENV = "NAHLA_WHATSAPP_CATALOG_AUTO_SYNC"
_GRAPH_PERMISSION_CODES = frozenset({"catalog_permission_denied"})
_PERMISSION_PROBE_MAX = 3
_PERMISSION_PROBE_COOLDOWN = timedelta(minutes=15)
_DRAIN_EXECUTOR: ThreadPoolExecutor | None = None
_DRAIN_EXECUTOR_WORKERS = 2
_DRAIN_COALESCER: Optional["TenantWorkCoalescer"] = None
DRAIN_QUEUE_MAX = 32
DRAIN_OVERFLOW_MAX = 32


def whatsapp_catalog_auto_sync_enabled() -> bool:
    """Explicit ops switch. Default off so a deploy does not start Graph pushes."""
    return os.environ.get(_AUTO_SYNC_ENV, "").strip().lower() in ("1", "true", "yes")

DRAIN_BATCH_SIZE = 25
PRODUCT_PAGE_SIZE = 500
FAILURE_SAMPLE_LIMIT = 10

_BLOCKERS_AR = {
    "feature_locked": (
        "مزامنة كتالوج واتساب غير مضمّنة في خطتك الحالية.",
        "رقِّ الخطة لتفعيل مزامنة الكتالوج مع واتساب.",
    ),
    "entitlement_unavailable": (
        "تعذّر التحقق من أهلية المزامنة مؤقتًا، وسيعاد المحاولة.",
        "انتظر لحظات ثم حدّث الحالة؛ لا حاجة لتعديل المنتج أو إعادة الربط.",
    ),
    "connection_not_found": (
        "لا يوجد ربط واتساب لهذا المتجر.",
        "اربط واتساب من صفحة الربط ثم أعد المحاولة.",
    ),
    "catalog_disabled": (
        "ربط الكتالوج بواتساب غير مفعّل.",
        "فعّل الكتالوج من إعدادات «ربط الكتالوج بواتساب وMeta».",
    ),
    "catalog_id_missing": (
        "معرّف كتالوج Meta غير مربوط.",
        "أدخل Meta Catalog ID من Commerce Manager ثم احفظ.",
    ),
    "access_token_missing": (
        "صلاحيات Meta غير مكتملة أو منتهية.",
        "أعد ربط واتساب لتجديد صلاحيات الكتالوج.",
    ),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _blocker(code: str) -> Dict[str, Any]:
    message_ar, action_ar = _BLOCKERS_AR.get(code, (code, ""))
    return {
        "ready": False,
        "blocker_code": code,
        "message_ar": message_ar,
        "action_ar": action_ar,
        "connection_fp": None,
    }


def _load_connection(db: Any, tenant_id: int) -> Any:
    from models import WhatsAppConnection  # noqa: PLC0415

    return (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == int(tenant_id))
        .first()
    )


def evaluate_whatsapp_catalog_sync_readiness(db: Any, tenant_id: int) -> Dict[str, Any]:
    """Local readiness for enqueue. Does not call Graph."""
    try:
        ent = get_entitlements(db, int(tenant_id), strict_lookup=True)
        if not ent.has_feature("meta_catalog_sync"):
            return _blocker("feature_locked")
    except EntitlementLookupUnavailable as exc:
        logger.warning(
            "[WA_CATALOG_SYNC] entitlement check unavailable tenant=%s source=%s",
            int(tenant_id),
            exc.source,
        )
        return _blocker("entitlement_unavailable")
    except (SQLAlchemyError, TimeoutError, OSError, ConnectionError) as exc:
        logger.warning(
            "[WA_CATALOG_SYNC] entitlement check unavailable tenant=%s err=%s",
            int(tenant_id),
            type(exc).__name__,
        )
        return _blocker("entitlement_unavailable")
    except Exception as exc:
        logger.warning(
            "[WA_CATALOG_SYNC] entitlement check failed tenant=%s err=%s",
            int(tenant_id),
            type(exc).__name__,
        )
        return _blocker("entitlement_unavailable")

    conn = _load_connection(db, tenant_id)
    if conn is None:
        return _blocker("connection_not_found")
    conn_fp = connection_sync_fingerprint(conn)
    if not bool(getattr(conn, "catalog_enabled", False)):
        blocked = _blocker("catalog_disabled")
        blocked["connection_fp"] = conn_fp
        return blocked
    if not str(getattr(conn, "meta_catalog_id", "") or "").strip():
        blocked = _blocker("catalog_id_missing")
        blocked["connection_fp"] = conn_fp
        return blocked
    token = str(getattr(conn, "access_token", "") or "").strip()
    if not token:
        meta = getattr(conn, "extra_metadata", None) or {}
        if not isinstance(meta, dict) or not str(meta.get("system_user_token") or "").strip():
            blocked = _blocker("access_token_missing")
            blocked["connection_fp"] = conn_fp
            return blocked
    return {
        "ready": True,
        "blocker_code": None,
        "message_ar": None,
        "action_ar": None,
        "catalog_enabled": True,
        "meta_catalog_id_present": True,
        "connection_fp": connection_sync_fingerprint(conn),
    }


def iter_tenant_products(db: Any, tenant_id: int) -> Iterable[Any]:
    from models import Product  # noqa: PLC0415

    offset = 0
    while True:
        batch = (
            db.query(Product)
            .filter(Product.tenant_id == int(tenant_id))
            .order_by(Product.id.asc())
            .offset(offset)
            .limit(PRODUCT_PAGE_SIZE)
            .all()
        )
        if not batch:
            break
        for row in batch:
            if int(getattr(row, "tenant_id", 0) or 0) != int(tenant_id):
                continue
            yield row
        if len(batch) < PRODUCT_PAGE_SIZE:
            break
        offset += PRODUCT_PAGE_SIZE


def _status_of(product: Any) -> str:
    return str(getattr(product, "sync_status", None) or "").strip().lower()


def _sync_meta(product: Any) -> Dict[str, Any]:
    meta = getattr(product, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        return {}
    sync_meta = meta.get("sync_meta")
    return dict(sync_meta) if isinstance(sync_meta, dict) else {}


def _readiness_fingerprint(readiness: Dict[str, Any]) -> str:
    return "|".join(
        [
            "1" if readiness.get("ready") else "0",
            str(readiness.get("blocker_code") or ""),
        ]
    )


def connection_sync_fingerprint(conn: Any) -> str:
    """Stable connection fingerprint. Hashes tokens; never logs them."""
    token = str(getattr(conn, "access_token", "") or "").strip()
    meta = getattr(conn, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    alt = str(meta.get("system_user_token") or "").strip()
    material = token or alt
    token_fp = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16] if material else "none"
    catalog_id = str(getattr(conn, "meta_catalog_id", "") or "").strip()
    enabled = "1" if getattr(conn, "catalog_enabled", False) else "0"
    status = str(getattr(conn, "status", "") or "")
    extras_raw = "|".join(
        [
            status,
            str(meta.get("catalog_management") or ""),
            str(
                meta.get("granted_scopes")
                or meta.get("scopes")
                or meta.get("token_scopes")
                or ""
            ),
            str(meta.get("token_status") or ""),
            str(meta.get("production_ready") if meta.get("production_ready") is not None else ""),
        ]
    )
    extras = hashlib.sha256(extras_raw.encode("utf-8")).hexdigest()[:12]
    return f"{enabled}|{catalog_id}|{token_fp}|{extras}"


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


def _permission_probe_due(sync_meta: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    count = int(sync_meta.get("permission_probe_count") or 0)
    if count >= _PERMISSION_PROBE_MAX:
        return False
    last = _parse_iso_dt(sync_meta.get("permission_probe_at"))
    if last is None:
        return True
    return (now or _now()) - last >= _PERMISSION_PROBE_COOLDOWN


def should_reconsider_blocked(product: Any, readiness: Dict[str, Any]) -> bool:
    """Re-evaluate blocked rows only when the blocking reason actually changed."""
    if _status_of(product) != "blocked":
        return False
    sm = _sync_meta(product)
    code = str(sm.get("last_error_code") or sm.get("block_code") or "")
    klass = str(sm.get("block_class") or classify_block_code(code))
    if klass == "permanent":
        return is_whatsapp_channel_publish_eligible(product)
    if klass == "readiness":
        if not readiness.get("ready"):
            return False
        stored_conn = str(sm.get("blocked_connection_fp") or "")
        current_conn = str(readiness.get("connection_fp") or "")
        if stored_conn and current_conn and stored_conn != current_conn:
            return True
        stored = str(sm.get("blocked_readiness_fp") or "")
        current = _readiness_fingerprint(readiness)
        if stored and stored == current:
            if code in _GRAPH_PERMISSION_CODES:
                return _permission_probe_due(sm)
            return False
        return True
    current_fp = channel_content_fingerprint(product)
    stored_fp = str(sm.get("blocked_fingerprint") or "")
    if stored_fp and stored_fp == current_fp:
        return False
    return True


def _stamp_readiness_on_block(product: Any, readiness: Dict[str, Any]) -> None:
    meta = dict(getattr(product, "extra_metadata", None) or {})
    sync_meta = dict(meta.get("sync_meta") or {})
    sync_meta["blocked_readiness_fp"] = _readiness_fingerprint(readiness)
    if readiness.get("connection_fp"):
        sync_meta["blocked_connection_fp"] = str(readiness.get("connection_fp"))
    meta["sync_meta"] = sync_meta
    product.extra_metadata = meta


def _belongs_to_tenant(product: Any, tenant_id: int) -> bool:
    return int(getattr(product, "tenant_id", 0) or 0) == int(tenant_id)


def _catalog_is_linked(conn: Any) -> bool:
    if conn is None:
        return False
    catalog_id = str(getattr(conn, "meta_catalog_id", "") or "").strip()
    return bool(getattr(conn, "catalog_enabled", False) and catalog_id)


def _is_meta_available_row(product: Any) -> bool:
    """Active local row that already exists as a WhatsApp/Meta catalog item."""
    if getattr(product, "merchant_hidden_at", None):
        return False
    if catalog_status_of(product) != CATALOG_STATUS_ACTIVE:
        return False
    mode = infer_ownership_mode(product)
    src = normalize_source(getattr(product, "source", None))
    if mode in (OWNERSHIP_META_READONLY, OWNERSHIP_NAHLA_MANAGED_META):
        return True
    if src in META_EXISTING_SOURCES:
        return True
    if str(getattr(product, "meta_item_id", None) or "").strip():
        return True
    return _status_of(product) == "synced"


def _empty_sync_counts() -> Dict[str, int]:
    return {
        "eligible": 0,
        "pending": 0,
        "syncing": 0,
        "synced": 0,
        "failed": 0,
        "blocked": 0,
        "pending_verification": 0,
        "skipped_ineligible": 0,
    }


def build_whatsapp_catalog_sync_status(db: Any, tenant_id: int) -> Dict[str, Any]:
    readiness = evaluate_whatsapp_catalog_sync_readiness(db, tenant_id)
    counts = _empty_sync_counts()
    last_success_at = None
    failures: List[Dict[str, Any]] = []
    meta_available_count = 0
    conn = None
    try:
        conn = _load_connection(db, tenant_id)
    except (SQLAlchemyError, AttributeError):
        conn = None
    catalog_linked = _catalog_is_linked(conn)

    if readiness.get("blocker_code") == "entitlement_unavailable":
        try:
            db.rollback()
        except SQLAlchemyError as rollback_exc:
            logger.warning(
                "[WA_CATALOG_SYNC] status rollback after entitlement outage tenant=%s err=%s",
                int(tenant_id),
                type(rollback_exc).__name__,
            )
    else:
        for row in iter_tenant_products(db, tenant_id):
            if not _belongs_to_tenant(row, tenant_id):
                continue
            if _is_meta_available_row(row):
                meta_available_count += 1
            if not is_whatsapp_channel_publish_eligible(row):
                counts["skipped_ineligible"] += 1
                continue
            counts["eligible"] += 1
            status = _status_of(row)
            sm = _sync_meta(row)
            verify_exhausted = (
                status == "pending_verification"
                and int(sm.get("verify_retry_count") or 0) >= MAX_VERIFY_LAG_RETRIES
            )
            if status == "pending" or status == "":
                counts["pending"] += 1
            elif status == "syncing":
                counts["syncing"] += 1
            elif status == "synced":
                counts["synced"] += 1
            elif status == "pending_verification" and not verify_exhausted:
                counts["pending_verification"] += 1
            elif status == "failed" or verify_exhausted:
                counts["failed"] += 1
            elif status == "blocked":
                counts["blocked"] += 1
            else:
                counts["pending"] += 1

            synced_at = getattr(row, "last_synced_at", None)
            if synced_at is not None:
                iso = synced_at.isoformat() if hasattr(synced_at, "isoformat") else str(synced_at)
                if last_success_at is None or iso > last_success_at:
                    last_success_at = iso

            if (status in ("failed", "blocked") or verify_exhausted) and len(failures) < FAILURE_SAMPLE_LIMIT:
                summary = sync_error_summary(row) or ("verification_exhausted" if verify_exhausted else status)
                failures.append({
                    "product_id": int(row.id),
                    "title": str(getattr(row, "title", "") or "")[:120],
                    "sync_status": status,
                    "error_summary": str(summary)[:240],
                })

    if counts["syncing"] > 0:
        phase = "syncing"
    elif counts["pending"] > 0:
        phase = "queued"
    elif counts["pending_verification"] > 0:
        phase = "pending_verification"
    elif counts["failed"] > 0 or counts["blocked"] > 0:
        phase = "needs_attention"
    elif counts["synced"] > 0:
        phase = "published"
    else:
        phase = "idle"

    auto_on = whatsapp_catalog_auto_sync_enabled()
    if readiness.get("ready"):
        status_phase = phase
    elif readiness.get("blocker_code") == "entitlement_unavailable":
        status_phase = "retrying"
    else:
        status_phase = "blocked"
    return {
        "ok": True,
        "tenant_id": int(tenant_id),
        "ready": bool(readiness.get("ready")),
        "blocker_code": readiness.get("blocker_code"),
        "message_ar": readiness.get("message_ar"),
        "action_ar": readiness.get("action_ar"),
        "phase": status_phase,
        "counts": counts,
        "queue_count": int(counts["pending"] + counts["syncing"] + counts["pending_verification"]),
        "meta_available_count": int(meta_available_count),
        "catalog_linked": bool(catalog_linked),
        "last_success_at": last_success_at,
        "failures": failures,
        "auto_sync_enabled": auto_on,
        "auto_sync_flag": _AUTO_SYNC_ENV,
        "verification": {
            "lookup_fields": list(IDENTITY_LOOKUP_FIELDS) + list(CONTENT_LOOKUP_FIELDS),
            "identity_fields": list(IDENTITY_LOOKUP_FIELDS),
            "content_fields": list(CONTENT_LOOKUP_FIELDS),
            "not_verified_fields": list(LOOKUP_UNVERIFIED_FIELDS),
            "note_ar": (
                "النشر يُختم فقط بعد تطابق السعر والعملة والتوفر مع Graph. "
                "وجود retailer_id وحده ليس إثبات مزامنة المحتوى، "
                "ولا يثبت ظهور المنتج في واجهة واتساب."
            ),
        },
    }


def enqueue_whatsapp_catalog_sync(
    db: Any,
    tenant_id: int,
    *,
    force: bool = False,
    trigger: str = "auto",
) -> Dict[str, Any]:
    """Mark eligible products pending. Does not claim Meta publish success."""
    readiness = evaluate_whatsapp_catalog_sync_readiness(db, tenant_id)
    if not readiness.get("ready"):
        phase = (
            "retrying"
            if readiness.get("blocker_code") == "entitlement_unavailable"
            else "blocked"
        )
        return {
            "ok": False,
            "queued": False,
            "phase": phase,
            "trigger": trigger,
            "enqueued": 0,
            "eligible": 0,
            "blocker_code": readiness.get("blocker_code"),
            "message_ar": readiness.get("message_ar"),
            "action_ar": readiness.get("action_ar"),
        }

    enqueued = 0
    eligible = 0
    for row in iter_tenant_products(db, tenant_id):
        if not _belongs_to_tenant(row, tenant_id):
            continue
        if not is_whatsapp_channel_publish_eligible(row):
            continue
        eligible += 1
        status = _status_of(row)
        from services.salla_variant_catalog_identity import is_salla_source  # noqa: PLC0415

        never_synced = getattr(row, "last_synced_at", None) is None
        if not is_salla_source(row):
            never_synced = never_synced and not getattr(row, "meta_item_id", None)
        if force or never_synced or status in ("pending", "failed", "blocked", "sync_failed", "pending_verification", ""):
            bump_content = status not in ("pending", "pending_verification")
            if mark_native_meta_sync_pending(db, row, bump_content=bump_content):
                enqueued += 1
    db.commit()
    return {
        "ok": True,
        "queued": True,
        "phase": "queued",
        "trigger": trigger,
        "enqueued": enqueued,
        "eligible": eligible,
        "blocker_code": None,
        "message_ar": None,
        "action_ar": None,
    }


def drain_whatsapp_catalog_sync(
    db: Any,
    tenant_id: int,
    *,
    limit: int = DRAIN_BATCH_SIZE,
    client: Any = None,
) -> Dict[str, Any]:
    """Process a batch of pending/failed products for one tenant.

    Product is the lock unit. Meta identities are sellable variant
    retailer_ids collected inside ``attempt_native_meta_sync`` — a Salla
    parent is never treated as a Graph identity.
    """
    readiness = evaluate_whatsapp_catalog_sync_readiness(db, tenant_id)
    out = {
        "ok": True,
        "tenant_id": int(tenant_id),
        "skipped": False,
        "processed": 0,
        "synced": 0,
        "failed": 0,
        "blocked": 0,
        "lock_skipped": 0,
        "pending_verification": 0,
        "blocker_code": None,
    }
    if not readiness.get("ready"):
        out["ok"] = False
        out["skipped"] = True
        out["blocker_code"] = readiness.get("blocker_code")
        return out

    now = _now()
    candidates: List[Any] = []
    for row in iter_tenant_products(db, tenant_id):
        if not _belongs_to_tenant(row, tenant_id):
            continue
        if not is_whatsapp_channel_publish_eligible(row):
            continue
        status = _status_of(row)
        if status == "synced":
            continue
        if status == "syncing" and not _syncing_is_stale(_sync_meta(row), now):
            continue
        if status == "blocked" and not should_reconsider_blocked(row, readiness):
            continue
        if status == "pending_verification" and not verify_retry_is_due(row, now):
            continue
        if status == "failed" and not retry_is_due(row, now):
            continue
        candidates.append(row)
        if len(candidates) >= int(limit):
            break

    for row in candidates:
        pid = int(row.id)
        try:
            result = attempt_native_meta_sync(
                db, int(tenant_id), pid, client=client,
            )
        except CatalogSyncSessionUnusable:
            logger.exception(
                "[WA_CATALOG_SYNC] session unusable tenant=%s product=%s; stopping drain",
                tenant_id,
                pid,
            )
            out["ok"] = False
            out["failed"] += 1
            out["processed"] += 1
            out["session_unusable"] = True
            raise
        except Exception:  # noqa: BLE001
            logger.exception(
                "[WA_CATALOG_SYNC] drain failed tenant=%s product=%s",
                tenant_id,
                pid,
            )
            out["failed"] += 1
            out["processed"] += 1
            continue
        out["processed"] += 1
        if result.get("ok"):
            out["synced"] += 1
        elif result.get("skipped"):
            out["lock_skipped"] += 1
        elif str(result.get("sync_status") or "") == "blocked":
            out["blocked"] += 1
            try:
                from models import Product  # noqa: PLC0415

                blocked_row = (
                    db.query(Product)
                    .filter(Product.id == pid, Product.tenant_id == int(tenant_id))
                    .first()
                )
                if blocked_row is not None:
                    _stamp_readiness_on_block(blocked_row, readiness)
                    db.commit()
            except Exception as exc:
                logger.warning(
                    "[WA_CATALOG_SYNC] readiness stamp failed tenant=%s product=%s err=%s",
                    int(tenant_id),
                    pid,
                    type(exc).__name__,
                )
                try:
                    db.rollback()
                except SQLAlchemyError as rollback_exc:
                    logger.warning(
                        "[WA_CATALOG_SYNC] readiness-stamp rollback failed tenant=%s product=%s err=%s",
                        int(tenant_id),
                        pid,
                        type(rollback_exc).__name__,
                    )
                    _invalidate_sync_session(db)
                    raise CatalogSyncSessionUnusable(
                        operation="readiness_stamp_rollback",
                        original_code="readiness_stamp_failed",
                        original_exc=exc,
                    ) from rollback_exc
        elif str(result.get("sync_status") or "") == "pending_verification":
            out["pending_verification"] = int(out.get("pending_verification") or 0) + 1
        else:
            out["failed"] += 1
    return out


def _variant_option_bits(item: Dict[str, Any]) -> str:
    opts = item.get("options") or item.get("variant_options") or {}
    if isinstance(opts, dict):
        pairs = [f"{k}={opts[k]}" for k in sorted(opts)]
        return ",".join(pairs)
    if isinstance(opts, list):
        return ",".join(str(x) for x in opts)
    return str(opts or "")


def channel_content_fingerprint(product: Any, *, extra_variants: Any = None) -> str:
    """Stable fingerprint of Meta-relevant catalog fields for one product."""
    meta = getattr(product, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    variants = extra_variants
    if variants is None:
        variants = meta.get("variants")
    variant_bits: List[str] = []
    if isinstance(variants, list):
        for item in variants:
            if not isinstance(item, dict):
                continue
            variant_bits.append(
                ":".join(
                    [
                        str(item.get("retailer_id") or item.get("id") or ""),
                        str(item.get("price") or ""),
                        str(item.get("sale_price") or ""),
                        str(item.get("stock_qty") or item.get("stock_quantity") or ""),
                        str(item.get("in_stock")),
                        str(item.get("image_url") or ""),
                        _variant_option_bits(item),
                    ]
                )
            )
    parts = [
        str(getattr(product, "title", "") or ""),
        str(getattr(product, "description", "") or meta.get("description") or ""),
        str(getattr(product, "price", "") or ""),
        str(meta.get("currency") or getattr(product, "currency", "") or ""),
        str(bool(getattr(product, "in_stock", True))),
        str(getattr(product, "stock_quantity", "") or ""),
        str(meta.get("image_url") or ""),
        str(meta.get("product_url") or ""),
        str(getattr(product, "catalog_status", "") or ""),
        "|".join(variant_bits),
    ]
    return "\n".join(parts)


def mark_product_pending_after_catalog_write(
    db: Any,
    product: Any,
    *,
    previous_fingerprint: Optional[str] = None,
) -> bool:
    """Best-effort pending mark after Salla/native catalog persistence.

    Skips when Meta-relevant fields are unchanged so the 120s drain
    does not resend an identical catalog.
    """
    try:
        if previous_fingerprint is not None:
            current = channel_content_fingerprint(product)
            if current == previous_fingerprint:
                return False
        return bool(mark_native_meta_sync_pending(db, product))
    except Exception:  # noqa: BLE001
        logger.exception(
            "[WA_CATALOG_SYNC] mark pending failed product=%s",
            getattr(product, "id", None),
        )
        return False


class TenantWorkCoalescer:
    """Bound executor queue with per-tenant coalescing and bounded overflow."""

    def __init__(
        self,
        *,
        max_queued: int,
        get_executor: Callable[[], ThreadPoolExecutor],
        runner: Callable[[int], None],
        max_overflow: Optional[int] = None,
    ) -> None:
        self._max_queued = max(1, int(max_queued))
        self._max_overflow = max(1, int(max_overflow if max_overflow is not None else max_queued))
        self._get_executor = get_executor
        self._runner = runner
        self._lock = Lock()
        self._pending: Set[int] = set()
        self._inflight: Set[int] = set()
        self._dirty: Set[int] = set()
        self._overflow: Deque[int] = deque()
        self._overflow_ids: Set[int] = set()

    def reset_for_tests(self) -> None:
        with self._lock:
            self._pending.clear()
            self._inflight.clear()
            self._dirty.clear()
            self._overflow.clear()
            self._overflow_ids.clear()

    def queue_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "pending": len(self._pending),
                "inflight": len(self._inflight),
                "dirty": len(self._dirty),
                "overflow": len(self._overflow),
                "max_queued": self._max_queued,
                "max_overflow": self._max_overflow,
                "overflow_ids": list(self._overflow),
            }

    def _enqueue_overflow_locked(self, tid: int) -> None:
        if tid in self._pending or tid in self._inflight:
            self._dirty.add(tid)
            return
        if tid in self._overflow_ids:
            return
        if len(self._overflow) >= self._max_overflow:
            return
        self._overflow.append(tid)
        self._overflow_ids.add(tid)

    def submit(self, tenant_id: int) -> None:
        tid = int(tenant_id)
        if tid <= 0:
            return
        with self._lock:
            if tid in self._inflight:
                self._dirty.add(tid)
                return
            if tid in self._pending:
                return
            queued = len(self._pending) + len(self._inflight)
            if queued >= self._max_queued:
                self._enqueue_overflow_locked(tid)
                return
            self._pending.add(tid)
        self._get_executor().submit(self._run, tid)

    def _run(self, tenant_id: int) -> None:
        with self._lock:
            self._pending.discard(tenant_id)
            self._inflight.add(tenant_id)
            self._dirty.discard(tenant_id)
        try:
            self._runner(tenant_id)
        finally:
            nxt: Optional[int] = None
            rerun = False
            with self._lock:
                self._inflight.discard(tenant_id)
                was_dirty = tenant_id in self._dirty
                self._dirty.discard(tenant_id)
                if self._overflow:
                    nxt = self._overflow.popleft()
                    self._overflow_ids.discard(nxt)
                    if was_dirty:
                        self._enqueue_overflow_locked(tenant_id)
                elif was_dirty:
                    rerun = True
            if nxt is not None:
                self.submit(nxt)
            elif rerun:
                self.submit(tenant_id)


def get_whatsapp_catalog_drain_executor() -> ThreadPoolExecutor:
    global _DRAIN_EXECUTOR
    if _DRAIN_EXECUTOR is None:
        _DRAIN_EXECUTOR = ThreadPoolExecutor(
            max_workers=_DRAIN_EXECUTOR_WORKERS,
            thread_name_prefix="wa-catalog-drain",
        )
    return _DRAIN_EXECUTOR


def run_whatsapp_catalog_drain_background(tenant_id: int) -> None:
    from core.database import SessionLocal  # noqa: PLC0415

    db = SessionLocal()
    try:
        drain_whatsapp_catalog_sync(db, int(tenant_id))
    except CatalogSyncSessionUnusable:
        logger.exception(
            "[WA_CATALOG_SYNC] background drain session unusable tenant=%s",
            tenant_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception("[WA_CATALOG_SYNC] background drain failed tenant=%s", tenant_id)
        try:
            db.rollback()
        except SQLAlchemyError as rollback_exc:
            logger.warning(
                "[WA_CATALOG_SYNC] background drain rollback failed tenant=%s err=%s",
                tenant_id,
                type(rollback_exc).__name__,
            )
            _invalidate_sync_session(db)
    finally:
        db.close()


def run_whatsapp_catalog_drain_tick() -> Dict[str, Any]:
    """Periodic worker entry: open and close the DB session inside the executor."""
    from core.database import SessionLocal  # noqa: PLC0415

    db = SessionLocal()
    try:
        return drain_ready_tenants(db)
    except CatalogSyncSessionUnusable:
        logger.exception("[WA_CATALOG_SYNC] periodic drain tick session unusable")
        return {
            "tenants": 0,
            "processed": 0,
            "synced": 0,
            "failed": 0,
            "error": True,
            "session_unusable": True,
        }
    except Exception:  # noqa: BLE001
        logger.exception("[WA_CATALOG_SYNC] periodic drain tick failed")
        try:
            db.rollback()
        except SQLAlchemyError as rollback_exc:
            logger.warning(
                "[WA_CATALOG_SYNC] drain-tick rollback failed err=%s",
                type(rollback_exc).__name__,
            )
            _invalidate_sync_session(db)
        return {"tenants": 0, "processed": 0, "synced": 0, "failed": 0, "error": True}
    finally:
        db.close()


def _whatsapp_catalog_drain_coalescer() -> TenantWorkCoalescer:
    global _DRAIN_COALESCER
    if _DRAIN_COALESCER is None:
        _DRAIN_COALESCER = TenantWorkCoalescer(
            max_queued=DRAIN_QUEUE_MAX,
            max_overflow=DRAIN_OVERFLOW_MAX,
            get_executor=get_whatsapp_catalog_drain_executor,
            runner=run_whatsapp_catalog_drain_background,
        )
    return _DRAIN_COALESCER


def reset_whatsapp_catalog_drain_scheduler_for_tests() -> None:
    global _DRAIN_COALESCER
    if _DRAIN_COALESCER is not None:
        _DRAIN_COALESCER.reset_for_tests()
    _DRAIN_COALESCER = None


def schedule_whatsapp_catalog_drain(
    tenant_id: int,
    *,
    allow_without_auto_flag: bool = False,
) -> None:
    if int(tenant_id) <= 0:
        return
    if not allow_without_auto_flag and not whatsapp_catalog_auto_sync_enabled():
        logger.info(
            "[WA_CATALOG_SYNC] auto drain skipped tenant=%s (%s!=1)",
            tenant_id,
            _AUTO_SYNC_ENV,
        )
        return
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get(
        "NAHLA_FORCE_WHATSAPP_CATALOG_DRAIN"
    ):
        return

    _whatsapp_catalog_drain_coalescer().submit(int(tenant_id))


def drain_ready_tenants(db: Any, *, limit_per_tenant: int = DRAIN_BATCH_SIZE) -> Dict[str, Any]:
    if not whatsapp_catalog_auto_sync_enabled():
        return {"tenants": 0, "processed": 0, "synced": 0, "failed": 0, "skipped": True}
    from models import WhatsAppConnection  # noqa: PLC0415

    conns = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.catalog_enabled.is_(True))
        .all()
    )
    tenants: List[int] = []
    seen = set()
    for conn in conns:
        tid = int(getattr(conn, "tenant_id", 0) or 0)
        if tid <= 0 or tid in seen:
            continue
        seen.add(tid)
        tenants.append(tid)

    summary = {"tenants": 0, "processed": 0, "synced": 0, "failed": 0}
    for tid in tenants:
        result = drain_whatsapp_catalog_sync(db, tid, limit=limit_per_tenant)
        if result.get("skipped"):
            continue
        summary["tenants"] += 1
        summary["processed"] += int(result.get("processed") or 0)
        summary["synced"] += int(result.get("synced") or 0)
        summary["failed"] += int(result.get("failed") or 0)
    return summary


__all__ = [
    "channel_content_fingerprint",
    "connection_sync_fingerprint",
    "build_whatsapp_catalog_sync_status",
    "drain_ready_tenants",
    "drain_whatsapp_catalog_sync",
    "enqueue_whatsapp_catalog_sync",
    "evaluate_whatsapp_catalog_sync_readiness",
    "get_whatsapp_catalog_drain_executor",
    "mark_product_pending_after_catalog_write",
    "reset_whatsapp_catalog_drain_scheduler_for_tests",
    "run_whatsapp_catalog_drain_background",
    "run_whatsapp_catalog_drain_tick",
    "schedule_whatsapp_catalog_drain",
    "TenantWorkCoalescer",
    "DRAIN_QUEUE_MAX",
    "DRAIN_OVERFLOW_MAX",
    "whatsapp_catalog_auto_sync_enabled",
    "should_reconsider_blocked",
]
