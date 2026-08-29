"""
services/native_meta_sync_orchestrator.py
─────────────────────────────────────────
Automatic + retry Meta catalog sync for Nahla-native manual products.

Reuses preview / push / lookup helpers — no duplicate Graph client.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.attributes import flag_modified, get_history, set_committed_value

from core.catalog import (
    canonical_retailer_id,
    is_whatsapp_channel_publish_eligible,
    whatsapp_channel_publish_rejection_detail,
)
from services.meta_catalog_linking import get_waba_catalog_link_status
from services.meta_catalog_push import (
    MetaCatalogPushError,
    find_meta_catalog_item_by_retailer_id,
    push_one_meta_catalog_item,
    _resolve_connection,
)
from services.meta_catalog_sync_preview import preview_native_meta_sync

logger = logging.getLogger("nahla.native_meta_sync")


class VariantDiscoveryError(RuntimeError):
    """Variant query failed; parent retailer_id is not a known-empty catalog."""


class CatalogSyncSessionUnusable(RuntimeError):
    """Session rollback/cleanup failed; the current batch must stop."""

    def __init__(self, *, operation: str, original_code: str, original_exc: BaseException | None = None):
        self.operation = operation
        self.original_code = original_code
        self.original_exc = original_exc
        detail = f"{operation} rollback failed after {original_code}"
        super().__init__(detail)
        if original_exc is not None and hasattr(self, "add_note"):
            self.add_note(f"original {type(original_exc).__name__}: {original_exc}")

SYNC_STALE_TTL = timedelta(minutes=12)
MAX_AUTO_RETRIES = 5
RETRY_BACKOFF_SECONDS = (60, 300, 900, 1800, 3600)
TRANSIENT_ERROR_CODES = frozenset({
    "meta_http_error",
    "meta_push_failed",
    "meta_rate_limited",
    "lookup_failed",
    "verification_failed",
    "access_token_missing",
    "connection_not_found",
    "catalog_id_missing",
    "variant_discovery_failed",
})
IDENTITY_LOOKUP_FIELDS = ("id", "retailer_id", "name")
CONTENT_LOOKUP_FIELDS = ("price", "currency", "availability")
LOOKUP_VERIFIED_FIELDS = IDENTITY_LOOKUP_FIELDS + CONTENT_LOOKUP_FIELDS
LOOKUP_UNVERIFIED_FIELDS = (
    "image_url",
    "whatsapp_storefront_visibility",
)
MAX_VERIFY_LAG_RETRIES = 3
VERIFY_LAG_BACKOFF_SECONDS = (60, 300, 900)
READINESS_BLOCK_CODES = frozenset({
    "catalog_disabled",
    "catalog_id_missing",
    "access_token_missing",
    "connection_not_found",
    "feature_locked",
    "catalog_permission_denied",
})
PERMANENT_BLOCK_CODES = frozenset({
    "product_already_meta_managed",
    "product_not_channel_publish_eligible",
    "not_eligible",
})
PRODUCT_BLOCK_CODES = frozenset({
    "preview_fatal",
    "missing_retailer_id",
    "missing_price",
    "missing_image_url",
    "missing_url",
    "product_not_active_in_catalog",
    "product_not_meta_export_eligible",
})

META_RELEVANT_PATCH_KEYS: FrozenSet[str] = frozenset({
    "title",
    "description",
    "price",
    "currency",
    "in_stock",
    "stock_quantity",
    "image_url",
    "product_url",
    "meta_retailer_id",
    "availability",
})

_ACQUIRABLE_STATUSES = frozenset({
    "pending",
    "failed",
    "blocked",
    "sync_failed",
    "pending_verification",
    "",
})


def _sanitize_sync_error(push_result: Dict[str, Any]) -> str:
    code = str(push_result.get("error") or "meta_push_failed")
    meta = push_result.get("meta") or {}
    response = meta.get("response")
    if isinstance(response, dict):
        graph_err = response.get("error")
        if isinstance(graph_err, dict):
            message = (
                graph_err.get("error_user_msg")
                or graph_err.get("message")
                or graph_err.get("type")
                or ""
            )
            if message:
                return f"{code}: {str(message)[:480]}"
    return code[:500]


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


def _generation(sync_meta: Dict[str, Any], key: str) -> int:
    try:
        return int(sync_meta.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _content_baseline(sync_meta: Dict[str, Any]) -> int:
    """Generation this worker started with. Prefer acquire's sync_generation."""
    sync_gen = _generation(sync_meta, "sync_generation")
    if sync_gen > 0:
        return sync_gen
    return _generation(sync_meta, "content_generation")


def _refresh_sync_meta_from_db(db: Any, product: Any) -> Dict[str, Any]:
    """Reload extra_metadata so a concurrent dirty/generation flag is not lost.

    The syncing worker committed and released the row lock before Graph I/O.
    A newer catalog write may have set ``dirty`` / bumped ``content_generation``
    in another session. Finalizing from the stale in-memory JSON would
    overwrite that flag and skip the newer version.

    Returns ``lost_lease=True`` when another worker acquired a newer lock.
    In that case this worker must not stamp a terminal result.
    This helper only merges into the in-memory object; terminal writes go
    through ``_stamp_with_lease`` so the lease check and write are atomic.
    """
    snapshot = _read_sync_meta(product)
    lease = _generation(snapshot, "lock_generation")
    baseline = _content_baseline(snapshot)
    try:
        db.refresh(product, attribute_names=["extra_metadata", "sync_status"])
    except Exception:
        return {"ok": False, "lost_lease": False}
    sm = _read_sync_meta(product)
    current_lease = _generation(sm, "lock_generation")
    if lease and current_lease and current_lease != lease:
        return {"ok": False, "lost_lease": True, "lease": lease, "current_lease": current_lease}
    content_gen = _generation(sm, "content_generation")
    _write_sync_meta(
        product,
        dirty=bool(sm.get("dirty")) or content_gen > baseline,
        content_generation=content_gen,
        lock_generation=current_lease or lease,
        sync_generation=baseline,
    )
    return {"ok": True, "lost_lease": False}


def _copy_product_sync_fields(src: Any, dest: Any) -> None:
    dest.sync_status = src.sync_status
    dest.sync_error = getattr(src, "sync_error", None)
    dest.extra_metadata = src.extra_metadata
    dest.meta_item_id = getattr(src, "meta_item_id", None)
    dest.last_synced_at = getattr(src, "last_synced_at", None)
    dest.meta_catalog_published_at = getattr(src, "meta_catalog_published_at", None)


def _stamp_with_lease(db: Any, product: Any, lease: int, mutator: Callable[[Any], None]) -> bool:
    """Lock the row, confirm this worker still owns the lease, apply mutator, commit.

    Does not hold the row lock across Graph I/O. Call only after acquire+commit.
    Returns False when another worker reclaimed the lease.
    """
    from models import Product  # noqa: PLC0415

    pid = getattr(product, "id", None)
    tid = getattr(product, "tenant_id", None)
    row = product
    used_lock = False
    baseline = _content_baseline(_read_sync_meta(product))
    if pid is not None and tid is not None:
        try:
            locked = (
                db.query(Product)
                .filter(Product.id == int(pid), Product.tenant_id == int(tid))
                .with_for_update()
                .populate_existing()
                .first()
            )
            try:
                locked_id = int(getattr(locked, "id", 0) or 0)
            except (TypeError, ValueError):
                locked_id = 0
            if locked is not None and locked_id == int(pid):
                row = locked
                used_lock = True
        except Exception:
            row = product
            used_lock = False

    if not used_lock:
        result = _refresh_sync_meta_from_db(db, product)
        if result.get("lost_lease"):
            return False
        row = product
        current = _lease_of(row)
        if int(current or 0) != int(lease or 0):
            return False
    else:
        current = _lease_of(row)
        if int(current or 0) != int(lease or 0):
            return False
        sm = _read_sync_meta(row)
        if bool(sm.get("dirty")) or _generation(sm, "content_generation") > baseline:
            _write_sync_meta(
                row,
                dirty=True,
                content_generation=_generation(sm, "content_generation"),
            )

    mutator(row)
    if row is not product:
        _copy_product_sync_fields(row, product)
    db.commit()
    return True


def _read_sync_meta(product: Any) -> Dict[str, Any]:
    meta = getattr(product, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        return {}
    sync_meta = meta.get("sync_meta")
    return dict(sync_meta) if isinstance(sync_meta, dict) else {}


def _write_sync_meta(product: Any, **updates: Any) -> Dict[str, Any]:
    meta = dict(getattr(product, "extra_metadata", None) or {})
    sync_meta = dict(meta.get("sync_meta") or {})
    sync_meta.update({k: v for k, v in updates.items() if v is not None or k in updates})
    meta["sync_meta"] = sync_meta
    product.extra_metadata = meta
    if getattr(product, "_sa_instance_state", None) is not None:
        flag_modified(product, "extra_metadata")
    return sync_meta


def _content_payload_snapshot(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "price": payload.get("price"),
        "currency": payload.get("currency"),
        "availability": payload.get("availability"),
    }


def _expected_payloads_map(product: Any) -> Dict[str, Dict[str, Any]]:
    raw = _read_sync_meta(product).get("expected_payloads_by_retailer_id")
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, val in raw.items():
        if isinstance(val, dict):
            out[str(key)] = dict(val)
    return out


def _should_verify_without_push(sync_meta: Dict[str, Any]) -> bool:
    """Lookup-only only when the lease still matches the generation that stored expectations."""
    if bool(sync_meta.get("dirty")):
        return False
    content_gen = _generation(sync_meta, "content_generation")
    expected_gen = _generation(sync_meta, "expected_content_generation")
    if expected_gen <= 0 or content_gen != expected_gen:
        return False
    expected = sync_meta.get("expected_payloads_by_retailer_id")
    return isinstance(expected, dict) and bool(expected)


def _payload_for_verify(
    product: Any,
    retailer_id: str,
    push_result: Dict[str, Any],
    *,
    lookup_only: bool,
    retailer_count: int,
) -> Dict[str, Any]:
    if not lookup_only:
        payload = push_result.get("payload") if isinstance(push_result.get("payload"), dict) else {}
        return _content_payload_snapshot(payload) if payload else {}
    sync_meta = _read_sync_meta(product)
    if not _should_verify_without_push(sync_meta):
        return {}
    expected = _expected_payloads_map(product).get(str(retailer_id))
    if isinstance(expected, dict) and expected:
        return expected
    if retailer_count == 1:
        stored = sync_meta.get("last_pushed_payload")
        if isinstance(stored, dict) and stored:
            return _content_payload_snapshot(stored)
    return {}


def classify_block_code(code: Optional[str]) -> str:
    value = str(code or "").strip()
    if value in READINESS_BLOCK_CODES:
        return "readiness"
    if value in PERMANENT_BLOCK_CODES:
        return "permanent"
    if value in PRODUCT_BLOCK_CODES:
        return "product"
    if value.startswith("preview_fatal"):
        return "product"
    return "product"


_ARABIC_INDIC_DIGITS = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)
_BIDI_AND_SPACE_CHARS = (
    "\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
    "\u2066\u2067\u2068\u2069\xa0\u00a0\u202f\u2007"
)
_CURRENCY_MARKERS = re.compile(
    r"(?:SAR|USD|EUR|AED|﷼|ر\.?\s*س\.?)",
    re.IGNORECASE,
)
_NUMBER_TOKEN = re.compile(r"-?\d+(?:\.\d+)?")


def _price_minor(value: Any) -> Optional[int]:
    """Normalize a Meta price field to integer minor units.

    Raw ``int`` values stay minor units (Graph/payload contract).
    Formatted display strings are major units, including Arabic-Indic
    digits and Arabic decimal/thousands separators.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    if not isinstance(value, str):
        return None

    text = value.translate({ord(ch): " " for ch in _BIDI_AND_SPACE_CHARS})
    text = text.translate(_ARABIC_INDIC_DIGITS)
    text = text.replace("٫", ".").replace("٬", "")
    text = _CURRENCY_MARKERS.sub(" ", text)
    text = text.replace(",", "")
    text = " ".join(text.split())
    if not text:
        return None

    tokens = _NUMBER_TOKEN.findall(text)
    if len(tokens) != 1:
        return None
    leftover = _NUMBER_TOKEN.sub(" ", text).strip()
    leftover = leftover.replace(".", "").strip()
    if leftover:
        return None

    token = tokens[0]
    if token.startswith("-"):
        return None
    try:
        amount = Decimal(token)
    except InvalidOperation:
        return None
    if "." in token:
        minor = amount * Decimal(100)
        integral = minor.to_integral_value()
        if minor != integral:
            return None
        return int(integral)
    return int(amount)


def _norm_availability(value: Any) -> str:
    text = str(value or "").strip().lower().replace("_", " ")
    if text in {"in stock", "in stock.", "available"}:
        return "in stock"
    if text in {"out of stock", "out of stock.", "oos"}:
        return "out of stock"
    return text


def _norm_currency(value: Any) -> str:
    return str(value or "").strip().upper()


def compare_pushed_content_to_lookup(
    payload: Dict[str, Any],
    live: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compare pushed payload to Graph GET row. Identity match is not enough."""
    live = live if isinstance(live, dict) else {}
    missing: List[str] = []
    mismatched: List[str] = []
    checked: List[str] = []
    expected_price = _price_minor(payload.get("price"))
    live_price = _price_minor(live.get("price"))
    if expected_price is None:
        missing.append("price")
    elif live_price is None:
        missing.append("price")
    else:
        checked.append("price")
        if expected_price != live_price:
            mismatched.append("price")

    expected_cur = _norm_currency(payload.get("currency"))
    live_cur = _norm_currency(live.get("currency"))
    if not expected_cur:
        missing.append("currency")
    elif not live_cur:
        missing.append("currency")
    else:
        checked.append("currency")
        if expected_cur != live_cur:
            mismatched.append("currency")

    expected_av = _norm_availability(payload.get("availability"))
    live_av = _norm_availability(live.get("availability"))
    if not expected_av:
        missing.append("availability")
    elif not live_av:
        missing.append("availability")
    else:
        checked.append("availability")
        if expected_av != live_av:
            mismatched.append("availability")

    if missing and not mismatched:
        outcome = "incomplete"
    elif mismatched:
        outcome = "mismatch"
    elif checked:
        outcome = "matched"
    else:
        outcome = "incomplete"
    return {
        "outcome": outcome,
        "checked_fields": checked,
        "missing_fields": missing,
        "mismatched_fields": mismatched,
    }


def _lease_of(product: Any) -> int:
    return _generation(_read_sync_meta(product), "lock_generation")


def _lease_held(db: Any, product: Any, lease: int) -> bool:
    result = _refresh_sync_meta_from_db(db, product)
    if result.get("lost_lease"):
        return False
    current = _lease_of(product)
    if int(lease or 0) == 0:
        return True
    return current == int(lease or 0)


def _invalidate_sync_session(db: Any) -> None:
    for method_name in ("invalidate", "close"):
        method = getattr(db, method_name, None)
        if not callable(method):
            continue
        try:
            method()
        except SQLAlchemyError as exc:
            logger.warning(
                "[NATIVE_META_SYNC] session %s failed err=%s",
                method_name,
                type(exc).__name__,
            )


def _rollback_or_raise_unusable(
    db: Any,
    *,
    operation: str,
    original_code: str,
    original_exc: BaseException | None = None,
) -> None:
    try:
        db.rollback()
    except SQLAlchemyError as rollback_exc:
        logger.warning(
            "[NATIVE_META_SYNC] %s rollback failed err=%s original=%s",
            operation,
            type(rollback_exc).__name__,
            original_code,
        )
        _invalidate_sync_session(db)
        raise CatalogSyncSessionUnusable(
            operation=operation,
            original_code=original_code,
            original_exc=original_exc,
        ) from rollback_exc


def _abandon_stale_lease(db: Any) -> Dict[str, Any]:
    _rollback_or_raise_unusable(db, operation="abandon_stale_lease", original_code="stale_lease")
    return {"ok": False, "skipped": True, "error_code": "stale_lease"}


def _syncing_is_stale(sync_meta: Dict[str, Any], now: datetime) -> bool:
    started = _parse_iso_dt(
        sync_meta.get("syncing_started_at") or sync_meta.get("last_attempt_at"),
    )
    if started is None:
        return True
    return (now - started) >= SYNC_STALE_TTL


def meta_relevant_patch_keys(keys: Set[str]) -> bool:
    return bool(keys & META_RELEVANT_PATCH_KEYS)


def sync_error_summary(product: Any) -> Optional[str]:
    err = getattr(product, "sync_error", None)
    if err:
        return str(err)[:500]
    sync_meta = _read_sync_meta(product)
    summary = sync_meta.get("last_error_summary")
    return str(summary)[:500] if summary else None


def retry_allowed_for_status(sync_status: Optional[str]) -> bool:
    status = str(sync_status or "").strip().lower()
    if status in ("syncing",):
        return False
    if status == "synced":
        return False
    return True


def build_sync_response_fields(product: Any) -> Dict[str, Any]:
    sync_meta = _read_sync_meta(product)
    status = getattr(product, "sync_status", None)
    return {
        "sync_status": status,
        "sync_error_summary": sync_error_summary(product),
        "meta_item_id": getattr(product, "meta_item_id", None),
        "last_sync_attempt_at": sync_meta.get("last_attempt_at"),
        "last_synced_at": (
            product.last_synced_at.isoformat()
            if getattr(product, "last_synced_at", None) else None
        ),
        "retry_allowed": retry_allowed_for_status(status),
    }


def _merge_locked_sync_meta(product: Any, db_status: Any, db_sync_meta: Dict[str, Any]) -> None:
    """Keep local catalog fields; adopt a newer lease from the locked row."""
    local_extra = dict(getattr(product, "extra_metadata", None) or {})
    local_sm = (
        dict(local_extra.get("sync_meta") or {})
        if isinstance(local_extra.get("sync_meta"), dict)
        else {}
    )
    db_sm = dict(db_sync_meta or {})
    db_lease = _generation(db_sm, "lock_generation")
    local_lease = _generation(local_sm, "lock_generation")
    if db_lease > local_lease:
        merged = dict(db_sm)
        if local_sm.get("dirty"):
            merged["dirty"] = True
        merged["content_generation"] = max(
            _generation(local_sm, "content_generation"),
            _generation(db_sm, "content_generation"),
        )
        local_extra["sync_meta"] = merged
        product.extra_metadata = local_extra
        if getattr(product, "_sa_instance_state", None) is not None:
            flag_modified(product, "extra_metadata")
        if db_status is not None:
            product.sync_status = db_status


def _run_in_savepoint(db: Any, fn: Callable[[], Any]) -> Any:
    """Run ``fn`` inside a connection SAVEPOINT.

    ``Session.begin_nested()`` flushes pending ORM state even when
    ``autoflush=False``, which would persist stale ``sync_meta`` before the
    read. A Connection-level savepoint does not flush the Session.
    """
    nested = None
    try:
        conn = db.connection()
        nested = conn.begin_nested()
    except Exception:
        nested = None
    if nested is None:
        return fn()
    try:
        result = fn()
        nested.commit()
        return result
    except Exception:
        try:
            nested.rollback()
        except SQLAlchemyError as rollback_exc:
            logger.warning(
                "[NATIVE_META_SYNC] savepoint rollback failed err=%s",
                type(rollback_exc).__name__,
            )
        raise


def _read_sync_state_columns(db: Any, product: Any, *, for_update: bool) -> Optional[tuple[Any, Dict[str, Any]]]:
    pid = getattr(product, "id", None)
    tid = getattr(product, "tenant_id", None)
    if pid is None or tid is None:
        return None
    from sqlalchemy import text  # noqa: PLC0415

    lock_sql = " FOR UPDATE" if for_update else ""
    mapping = db.execute(
        text(
            "SELECT sync_status, metadata AS extra_metadata "
            f"FROM products WHERE id = :id AND tenant_id = :tid{lock_sql}"
        ),
        {"id": int(pid), "tid": int(tid)},
    ).mappings().first()
    if not mapping:
        return None
    extra = mapping.get("extra_metadata")
    if extra is None:
        extra = mapping.get("metadata")
    if isinstance(extra, (bytes, bytearray)):
        extra = extra.decode("utf-8")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except (TypeError, ValueError):
            return None
    if extra is None:
        extra = {}
    if not isinstance(extra, dict):
        return None
    db_sm = extra.get("sync_meta") if isinstance(extra.get("sync_meta"), dict) else {}
    return mapping.get("sync_status"), db_sm


def _lock_sync_state_keep_local_catalog(db: Any, product: Any) -> bool:
    """Load current sync lease without wiping unflushed catalog columns.

    Returns False when no current ``sync_meta`` could be read. Callers must
    not write sync fields from stale in-memory JSON in that case.
    """
    pid = getattr(product, "id", None)
    tid = getattr(product, "tenant_id", None)
    if pid is None or tid is None:
        return True
    dialect = ""
    try:
        bind = db.get_bind()
        dialect = str(getattr(getattr(bind, "dialect", None), "name", "") or "")
    except Exception:
        dialect = ""

    def _load_sql(*, for_update: bool) -> Optional[tuple[Any, Dict[str, Any]]]:
        return _run_in_savepoint(
            db,
            lambda: _read_sync_state_columns(db, product, for_update=for_update),
        )

    if dialect.startswith("postgres"):
        loaded = None
        for for_update in (True, False):
            try:
                loaded = _load_sql(for_update=for_update)
            except Exception:
                loaded = None
            if loaded is not None:
                db_status, db_sm = loaded
                _merge_locked_sync_meta(product, db_status, db_sm)
                return True
        try:
            locked = _run_in_savepoint(db, lambda: _orm_lock_product(db, int(pid), int(tid)))
            if locked is None:
                return False
            if locked is product:
                return True
            _merge_locked_sync_meta(
                product,
                getattr(locked, "sync_status", None),
                _read_sync_meta(locked),
            )
            return True
        except Exception:
            return False
    try:
        locked = _orm_lock_product(db, int(pid), int(tid))
        if locked is None or locked is product:
            return True
        _merge_locked_sync_meta(product, getattr(locked, "sync_status", None), _read_sync_meta(locked))
        return True
    except Exception:
        return False


def _orm_lock_product(db: Any, pid: int, tid: int) -> Any:
    from models import Product  # noqa: PLC0415

    return (
        db.query(Product)
        .filter(Product.id == int(pid), Product.tenant_id == int(tid))
        .with_for_update()
        .first()
    )


def _abandon_unflushed_sync_fields(product: Any) -> None:
    """Drop unflushed sync JSON without rolling back catalog column changes."""
    for attr in ("extra_metadata", "sync_status"):
        try:
            hist = get_history(product, attr)
            if not hist.has_changes():
                continue
            previous = hist.deleted[0] if hist.deleted else None
            set_committed_value(product, attr, previous)
        except Exception:
            continue


def _revalidate_sync_state_for_write(db: Any, product: Any) -> bool:
    """Take a row lock and merge current ``sync_meta`` immediately before write.

    An unlocked fallback read is not a write authorization. Another session
    may commit a newer lease between that read and our flush.
    """
    pid = getattr(product, "id", None)
    tid = getattr(product, "tenant_id", None)
    if pid is None or tid is None:
        return True
    dialect = ""
    try:
        bind = db.get_bind()
        dialect = str(getattr(getattr(bind, "dialect", None), "name", "") or "")
    except Exception:
        dialect = ""
    if not dialect.startswith("postgres"):
        return True
    try:
        loaded = _run_in_savepoint(
            db,
            lambda: _read_sync_state_columns(db, product, for_update=True),
        )
    except Exception:
        loaded = None
    if loaded is not None:
        db_status, db_sm = loaded
        _merge_locked_sync_meta(product, db_status, db_sm)
        return True
    try:
        locked = _run_in_savepoint(db, lambda: _orm_lock_product(db, int(pid), int(tid)))
        if locked is None:
            return False
        if locked is not product:
            _merge_locked_sync_meta(
                product,
                getattr(locked, "sync_status", None),
                _read_sync_meta(locked),
            )
        return True
    except Exception:
        return False


def mark_native_meta_sync_pending(db: Any, product: Any, *, bump_content: bool = True) -> bool:
    """Mark a channel-publish-eligible product pending for Meta sync.

    If a push is already in flight, set ``dirty`` and bump
    ``content_generation`` instead of clobbering ``syncing``.
    Reloads sync lease state when possible so a newer lock is not rewound,
    without reloading unflushed catalog columns.
    """
    if not is_whatsapp_channel_publish_eligible(product):
        return False
    if not _lock_sync_state_keep_local_catalog(db, product):
        _abandon_unflushed_sync_fields(product)
        return False
    if not _revalidate_sync_state_for_write(db, product):
        _abandon_unflushed_sync_fields(product)
        return False
    row = product

    now = _now()
    status = str(getattr(row, "sync_status", None) or "").strip().lower()
    sync_meta = _read_sync_meta(row)
    if (
        not bump_content
        and status in ("pending", "pending_verification")
        and not (status == "syncing")
    ):
        return True
    next_generation = _generation(sync_meta, "content_generation") + 1
    if status == "syncing" and not _syncing_is_stale(sync_meta, now):
        _write_sync_meta(
            row,
            dirty=True,
            pending_at=now.isoformat(),
            content_generation=next_generation,
            retry_count=sync_meta.get("retry_count"),
            next_retry_at=sync_meta.get("next_retry_at"),
            verify_retry_count=0,
            next_verify_at=None,
            verify_exhausted=False,
        )
        if row is not product:
            product.sync_status = row.sync_status
            product.extra_metadata = row.extra_metadata
        db.flush()
        return True
    row.sync_status = "pending"
    row.sync_error = None
    _write_sync_meta(
        row,
        pending_at=now.isoformat(),
        dirty=False,
        retry_count=0,
        next_retry_at=None,
        last_error_code=None,
        last_error_summary=None,
        content_generation=next_generation,
        verify_retry_count=0,
        next_verify_at=None,
        verify_exhausted=False,
    )
    if row is not product:
        product.sync_status = row.sync_status
        product.sync_error = row.sync_error
        product.extra_metadata = row.extra_metadata
    db.flush()
    return True


def _release_acquire_tx(db: Any) -> None:
    _rollback_or_raise_unusable(
        db,
        operation="release_acquire_tx",
        original_code="sync_lock_not_acquired",
    )


def _try_acquire_sync_lock(db: Any, tenant_id: int, product_id: int) -> Optional[Any]:
    """CAS: pending|failed|blocked|stale-syncing → syncing. Returns product or None."""
    from models import Product  # noqa: PLC0415

    now = _now()
    row = (
        db.query(Product)
        .filter(
            Product.id == int(product_id),
            Product.tenant_id == int(tenant_id),
        )
        .with_for_update(skip_locked=True)
        .populate_existing()
        .first()
    )
    if row is None:
        _release_acquire_tx(db)
        return None

    if not is_whatsapp_channel_publish_eligible(row):
        _release_acquire_tx(db)
        return None

    status = str(row.sync_status or "").strip().lower()
    sync_meta = _read_sync_meta(row)

    if status == "syncing" and not _syncing_is_stale(sync_meta, now):
        _rollback_or_raise_unusable(
            db, operation="release_live_syncing", original_code="sync_lock_not_acquired"
        )
        return None

    if status == "synced":
        _rollback_or_raise_unusable(
            db, operation="release_synced_row", original_code="sync_lock_not_acquired"
        )
        return None

    if status not in _ACQUIRABLE_STATUSES and status != "syncing":
        if row.sync_status is not None:
            _rollback_or_raise_unusable(
                db, operation="release_non_acquirable", original_code="sync_lock_not_acquired"
            )
            return None

    row.sync_status = "syncing"
    lock_generation = _generation(sync_meta, "lock_generation") + 1
    _write_sync_meta(
        row,
        syncing_started_at=now.isoformat(),
        last_attempt_at=now.isoformat(),
        lock_generation=lock_generation,
        sync_generation=_generation(sync_meta, "content_generation"),
    )
    db.commit()
    db.refresh(row)
    return row


def _mark_blocked(product: Any, *, error_code: str, summary: str) -> None:
    product.sync_status = "blocked"
    product.sync_error = summary[:2000]
    from services.whatsapp_catalog_sync import channel_content_fingerprint  # noqa: PLC0415

    try:
        blocked_fp = channel_content_fingerprint(product)
    except Exception:
        blocked_fp = None
    sync_meta = _read_sync_meta(product)
    klass = classify_block_code(error_code)
    probe_count = int(sync_meta.get("permission_probe_count") or 0)
    if klass == "readiness" and error_code == "catalog_permission_denied":
        probe_count += 1
    _write_sync_meta(
        product,
        last_error_code=error_code,
        last_error_summary=summary[:500],
        syncing_started_at=None,
        block_class=klass,
        blocked_fingerprint=blocked_fp,
        blocked_readiness_fp=(
            f"0|{error_code}" if klass == "readiness" else None
        ),
        permission_probe_count=probe_count if klass == "readiness" else sync_meta.get("permission_probe_count"),
        permission_probe_at=_now().isoformat() if klass == "readiness" else sync_meta.get("permission_probe_at"),
    )


def _error_consumes_retry_budget(error_code: str) -> bool:
    """Transient Graph failures and unclassified errors share a finite backoff budget."""
    if error_code in READINESS_BLOCK_CODES:
        return False
    if error_code in PERMANENT_BLOCK_CODES:
        return False
    if error_code in PRODUCT_BLOCK_CODES:
        return False
    if error_code == "preview_fatal":
        return False
    return True


def _mark_failed(product: Any, *, error_code: str, summary: str) -> None:
    product.sync_status = "failed"
    product.sync_error = summary[:2000]
    sync_meta = _read_sync_meta(product)
    retry_count = int(sync_meta.get("retry_count") or 0)
    next_retry_at = None
    if _error_consumes_retry_budget(error_code) and retry_count < MAX_AUTO_RETRIES:
        retry_count += 1
        if retry_count >= MAX_AUTO_RETRIES:
            next_retry_at = None
        else:
            delay = RETRY_BACKOFF_SECONDS[min(retry_count - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
            next_retry_at = (_now() + timedelta(seconds=delay)).isoformat()
    _write_sync_meta(
        product,
        last_error_code=error_code,
        last_error_summary=summary[:500],
        syncing_started_at=None,
        retry_count=retry_count,
        next_retry_at=next_retry_at,
    )


def _requeue_if_dirty(product: Any) -> bool:
    sync_meta = _read_sync_meta(product)
    if not sync_meta.get("dirty"):
        return False
    product.sync_status = "pending"
    product.sync_error = None
    _write_sync_meta(
        product,
        dirty=False,
        pending_at=_now().isoformat(),
        syncing_started_at=None,
        next_retry_at=None,
    )
    return True


def retry_is_due(product: Any, now: Optional[datetime] = None) -> bool:
    """True when a failed product may be auto-retried (push backoff)."""
    status = str(getattr(product, "sync_status", None) or "").strip().lower()
    if status in ("pending", ""):
        return True
    if status == "pending_verification":
        return verify_retry_is_due(product, now)
    if status == "blocked":
        return False
    if status != "failed":
        return False
    sync_meta = _read_sync_meta(product)
    retry_count = int(sync_meta.get("retry_count") or 0)
    if retry_count >= MAX_AUTO_RETRIES:
        return False
    nxt = _parse_iso_dt(sync_meta.get("next_retry_at"))
    if nxt is None:
        return True
    return (now or _now()) >= nxt


def verify_retry_is_due(product: Any, now: Optional[datetime] = None) -> bool:
    """True when a content-verification lag check may run (no re-push)."""
    status = str(getattr(product, "sync_status", None) or "").strip().lower()
    if status != "pending_verification":
        return False
    sync_meta = _read_sync_meta(product)
    retry_count = int(sync_meta.get("verify_retry_count") or 0)
    if retry_count >= MAX_VERIFY_LAG_RETRIES:
        return False
    nxt = _parse_iso_dt(sync_meta.get("next_verify_at"))
    if nxt is None:
        return True
    return (now or _now()) >= nxt


def _raise_variant_discovery_failed(db: Any, parent: Any, exc: BaseException) -> None:
    logger.warning(
        "[NATIVE_META_SYNC] variant discovery failed product=%s err=%s",
        getattr(parent, "id", None),
        type(exc).__name__,
    )
    try:
        db.rollback()
    except SQLAlchemyError as rollback_exc:
        logger.warning(
            "[NATIVE_META_SYNC] variant-discovery rollback failed product=%s err=%s",
            getattr(parent, "id", None),
            type(rollback_exc).__name__,
        )
        _invalidate_sync_session(db)
        raise CatalogSyncSessionUnusable(
            operation="variant_discovery_rollback",
            original_code="variant_discovery_failed",
            original_exc=exc,
        ) from rollback_exc
    raise VariantDiscoveryError(type(exc).__name__) from exc


def _collect_retailer_ids(db: Any, parent: Any, fallback: Optional[str]) -> list[str]:
    ids: list[str] = []
    try:
        from models import ProductVariant  # noqa: PLC0415

        rows = (
            db.query(ProductVariant)
            .filter(
                ProductVariant.tenant_id == int(parent.tenant_id),
                ProductVariant.product_id == int(parent.id),
            )
            .all()
        )
        if isinstance(rows, list):
            for row in rows:
                rid = str(getattr(row, "retailer_id", "") or "").strip()
                if rid:
                    ids.append(rid)
    except (SQLAlchemyError, AttributeError, TypeError, ValueError) as exc:
        _raise_variant_discovery_failed(db, parent, exc)
    fb = str(fallback or "").strip()
    if fb and fb not in ids:
        ids.append(fb)
    seen: Set[str] = set()
    out: list[str] = []
    for rid in ids:
        if rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def _mark_pending_verification(
    product: Any,
    *,
    meta_item_id: str,
    comparison: Dict[str, Any],
    waba_linked: Optional[bool],
) -> None:
    now = _now()
    sync_meta = _read_sync_meta(product)
    content_gen = _generation(sync_meta, "content_generation")
    verify_gen = _generation(sync_meta, "verify_generation")
    expected_gen = _generation(sync_meta, "expected_content_generation")
    prior_retries = int(sync_meta.get("verify_retry_count") or 0)
    if verify_gen == 0:
        # Legacy rows predating verify_generation: never treat 0 as the
        # current content generation (that would inherit an exhausted budget).
        verify_gen = expected_gen if expected_gen > 0 else 0
    if verify_gen != content_gen:
        retry_count = 1
    else:
        retry_count = prior_retries + 1
    delay = VERIFY_LAG_BACKOFF_SECONDS[min(retry_count - 1, len(VERIFY_LAG_BACKOFF_SECONDS) - 1)]
    next_at = None
    exhausted = retry_count >= MAX_VERIFY_LAG_RETRIES
    if not exhausted:
        next_at = (now + timedelta(seconds=delay)).isoformat()
    product.sync_status = "pending_verification"
    product.sync_error = None
    if meta_item_id:
        product.meta_item_id = meta_item_id
    _write_sync_meta(
        product,
        syncing_started_at=None,
        verified_at=None,
        content_verified=False,
        verify_retry_count=retry_count,
        verify_generation=content_gen,
        next_verify_at=next_at,
        verify_exhausted=exhausted,
        verify_outcome=comparison.get("outcome"),
        verify_missing_fields=list(comparison.get("missing_fields") or []),
        verify_mismatched_fields=list(comparison.get("mismatched_fields") or []),
        waba_catalog_linked=waba_linked,
        lookup_verified_fields=list(IDENTITY_LOOKUP_FIELDS),
        lookup_unverified_fields=list(LOOKUP_UNVERIFIED_FIELDS),
        last_error_code="verification_exhausted" if exhausted else "pending_content_verification",
        last_error_summary=(
            "content verification retries exhausted; needs attention"
            if exhausted
            else "retailer_id matched; content not yet verified against Graph"
        ),
    )


def _mark_synced(
    product: Any,
    *,
    meta_item_id: str,
    waba_linked: Optional[bool],
) -> None:
    now = _now()
    product.sync_status = "synced"
    product.sync_error = None
    product.meta_item_id = meta_item_id
    product.last_synced_at = now
    product.meta_catalog_published_at = now
    _write_sync_meta(
        product,
        last_error_code=None,
        last_error_summary=None,
        syncing_started_at=None,
        retry_count=0,
        next_retry_at=None,
        waba_catalog_linked=waba_linked,
        verified_at=now.isoformat(),
        content_verified=True,
        verify_retry_count=0,
        next_verify_at=None,
    )


def _waba_linked_flag(waba_status: Dict[str, Any]) -> Optional[bool]:
    if not waba_status:
        return None
    if waba_status.get("ok"):
        linked = waba_status.get("expected_catalog_linked")
        return bool(linked) if linked is not None else None
    return None


def attempt_native_meta_sync(
    db: Any,
    tenant_id: int,
    product_id: int,
    *,
    client: Any = None,
    allow_synced_retry: bool = False,
) -> Dict[str, Any]:
    """Run one Meta sync attempt. Caller must not pass request-scoped ORM objects."""
    if allow_synced_retry:
        parent = _load_product(db, tenant_id, product_id)
        if parent is None:
            return {"ok": False, "skipped": True, "error_code": "product_not_found"}
        if not mark_native_meta_sync_pending(db, parent):
            return {"ok": False, "skipped": True, "error_code": "not_eligible"}
        db.commit()

    parent = _try_acquire_sync_lock(db, tenant_id, product_id)
    if parent is None:
        return {"ok": False, "skipped": True, "error_code": "sync_lock_not_acquired"}

    lease = _lease_of(parent)
    lookup_only = _should_verify_without_push(_read_sync_meta(parent))

    def _fail(error_code: str, summary: str, **extra: Any) -> Dict[str, Any]:
        extra_clean = {k: v for k, v in extra.items() if k != "error_code"}

        def _mutate(row: Any) -> None:
            if (
                error_code in READINESS_BLOCK_CODES
                or error_code in PERMANENT_BLOCK_CODES
                or error_code in PRODUCT_BLOCK_CODES
                or error_code == "preview_fatal"
            ):
                _mark_blocked(row, error_code=error_code, summary=summary)
            else:
                _mark_failed(row, error_code=error_code, summary=summary)
            _requeue_if_dirty(row)

        if not _stamp_with_lease(db, parent, lease, _mutate):
            return _abandon_stale_lease(db)
        return {
            "ok": False,
            "sync_status": parent.sync_status,
            "error_code": error_code,
            **extra_clean,
        }

    try:
        return _attempt_acquired_body(
            db,
            tenant_id,
            product_id,
            parent,
            lease,
            lookup_only=lookup_only,
            client=client,
            fail=_fail,
        )
    except CatalogSyncSessionUnusable:
        raise
    except TypeError as exc:
        logger.exception(
            "[NATIVE_META_SYNC] type error after acquire tenant=%s product=%s",
            tenant_id,
            product_id,
        )
        return _fail("unexpected_sync_error", f"TypeError: {exc}"[:500])
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[NATIVE_META_SYNC] unexpected error after acquire tenant=%s product=%s",
            tenant_id,
            product_id,
        )
        return _fail("unexpected_sync_error", f"{type(exc).__name__}: {exc}"[:500])


def _attempt_acquired_body(
    db: Any,
    tenant_id: int,
    product_id: int,
    parent: Any,
    lease: int,
    *,
    lookup_only: bool,
    client: Any,
    fail: Callable[..., Dict[str, Any]],
) -> Dict[str, Any]:
    try:
        conn = _resolve_connection(db, tenant_id)
        if not bool(getattr(conn, "catalog_enabled", False)):
            def _pending_disabled(row: Any) -> None:
                row.sync_status = "pending"
                row.sync_error = None
                _write_sync_meta(row, last_error_code="catalog_disabled")

            if not _stamp_with_lease(db, parent, lease, _pending_disabled):
                return _abandon_stale_lease(db)
            return {
                "ok": False,
                "skipped": True,
                "error_code": "catalog_disabled",
                "sync_status": parent.sync_status,
            }
    except MetaCatalogPushError as exc:
        return fail(exc.code, exc.code)

    rejection = whatsapp_channel_publish_rejection_detail(parent)
    if rejection is not None:
        return fail(
            str(rejection.get("error_code") or "not_eligible"),
            rejection.get("message_ar", "not_eligible"),
            **{k: v for k, v in rejection.items() if k != "error_code"},
        )

    preview = preview_native_meta_sync(db, tenant_id, product_id, channel_publish=True)
    if not preview.get("eligible"):
        code = str(preview.get("error_code") or "preview_ineligible")
        summary = str(preview.get("message_ar") or code)
        preview_extra = {k: v for k, v in preview.items() if k != "error_code"}
        return fail(code, summary, **preview_extra)

    fatal_errors = list(preview.get("fatal_errors") or [])
    if fatal_errors:
        codes = ", ".join(e.get("code", "") for e in fatal_errors[:3])
        return fail("preview_fatal", f"preview_fatal: {codes}"[:500], fatal_errors=fatal_errors)

    from services.meta_catalog_sync_confirm import ensure_native_default_variant  # noqa: PLC0415

    try:
        try:
            _variant, _variant_created = ensure_native_default_variant(db, parent)
        except (SQLAlchemyError, AttributeError, TypeError, ValueError) as exc:
            _raise_variant_discovery_failed(db, parent, exc)
        fallback_rid = (
            getattr(_variant, "retailer_id", None)
            or preview.get("retailer_id")
            or canonical_retailer_id(parent, fallback_to_synthetic=True)
        )
        retailer_ids = _collect_retailer_ids(db, parent, fallback_rid)
    except VariantDiscoveryError as exc:
        return fail(
            "variant_discovery_failed",
            f"variant_discovery_failed: {exc}"[:500],
        )
    if not retailer_ids:
        return fail("missing_retailer_id", "missing_retailer_id")

    last_push: Dict[str, Any] = {}
    last_lookup: Dict[str, Any] = {}
    last_comparison: Dict[str, Any] = {}
    variant_results: Dict[str, Any] = {}
    expected_map = _expected_payloads_map(parent)
    verified_meta_item_id: Optional[str] = None
    retailer_id = retailer_ids[0]
    content_ok = True
    skipped_push = lookup_only
    content_generation = _generation(_read_sync_meta(parent), "content_generation")

    for retailer_id in retailer_ids:
        if lookup_only:
            push_result = {"ok": True, "skipped_push": True}
        else:
            try:
                push_result = push_one_meta_catalog_item(
                    db,
                    int(tenant_id),
                    str(retailer_id),
                    confirm=True,
                    client=client,
                )
            except MetaCatalogPushError as exc:
                return fail(exc.code, exc.code, retailer_id=retailer_id)

        last_push = push_result
        http_status = None
        meta_block = push_result.get("meta") if isinstance(push_result.get("meta"), dict) else {}
        try:
            http_status = int(meta_block.get("http_status") or 0)
        except (TypeError, ValueError):
            http_status = 0
        if not lookup_only and not push_result.get("ok"):
            err_msg = _sanitize_sync_error(push_result)
            code = str(push_result.get("error") or "meta_push_failed")
            if http_status == 429:
                code = "meta_rate_limited"
            return fail(code, err_msg, retailer_id=retailer_id)

        try:
            conn = _resolve_connection(db, tenant_id)
            catalog_id = str(getattr(conn, "meta_catalog_id", "") or "").strip()
            if not catalog_id:
                raise MetaCatalogPushError("catalog_id_missing", "meta_catalog_id is not set")
            meta_item_id, lookup = find_meta_catalog_item_by_retailer_id(
                conn, catalog_id, str(retailer_id), client=client,
            )
        except MetaCatalogPushError as exc:
            return fail(exc.code, exc.code, retailer_id=retailer_id)

        last_lookup = lookup or {}
        if not meta_item_id or not last_lookup.get("matched"):
            return fail(
                "verification_failed",
                "verification_failed: retailer_id not found after push",
                retailer_id=retailer_id,
            )
        if verified_meta_item_id is None:
            verified_meta_item_id = str(meta_item_id)

        payload = _payload_for_verify(
            parent,
            str(retailer_id),
            push_result,
            lookup_only=lookup_only,
            retailer_count=len(retailer_ids),
        )
        if not lookup_only and payload:
            expected_map[str(retailer_id)] = dict(payload)
        comparison = compare_pushed_content_to_lookup(payload, last_lookup.get("item"))
        variant_results[str(retailer_id)] = comparison
        last_comparison = comparison
        if comparison.get("outcome") != "matched":
            content_ok = False

    waba_status = get_waba_catalog_link_status(db, tenant_id)
    waba_linked = _waba_linked_flag(waba_status)

    pushed_payload = last_push.get("payload") if isinstance(last_push.get("payload"), dict) else None

    def _stamp_verify_lag(row: Any) -> None:
        _mark_pending_verification(
            row,
            meta_item_id=str(verified_meta_item_id),
            comparison=last_comparison,
            waba_linked=waba_linked,
        )
        updates = {
            "expected_payloads_by_retailer_id": expected_map,
            "expected_content_generation": content_generation,
            "variant_verify_results": variant_results,
        }
        if pushed_payload:
            updates["last_pushed_payload"] = pushed_payload
        _write_sync_meta(row, **updates)
        _requeue_if_dirty(row)

    def _stamp_success(row: Any) -> None:
        if skipped_push and not _should_verify_without_push(_read_sync_meta(row)):
            _stamp_verify_lag(row)
            return
        _mark_synced(row, meta_item_id=str(verified_meta_item_id), waba_linked=waba_linked)
        updates: Dict[str, Any] = {
            "lookup_verified_fields": list(LOOKUP_VERIFIED_FIELDS),
            "lookup_unverified_fields": list(LOOKUP_UNVERIFIED_FIELDS),
            "expected_payloads_by_retailer_id": expected_map,
            "expected_content_generation": content_generation,
            "variant_verify_results": variant_results,
        }
        if pushed_payload:
            updates["last_pushed_payload"] = pushed_payload
        _write_sync_meta(row, **updates)
        _requeue_if_dirty(row)

    if content_ok:
        if not _stamp_with_lease(db, parent, lease, _stamp_success):
            return _abandon_stale_lease(db)
        logger.info(
            "[NATIVE_META_SYNC] tenant=%s product=%s retailer_id=%s meta_item_id=%s variants=%s requeued=%s content=matched",
            tenant_id,
            product_id,
            retailer_id,
            verified_meta_item_id,
            len(retailer_ids),
            bool(_read_sync_meta(parent).get("dirty")) is False,
        )
        return {
            "ok": True,
            "sync_status": parent.sync_status,
            "meta_item_id": parent.meta_item_id,
            "retailer_id": retailer_id,
            "variant_count": len(retailer_ids),
            "last_synced_at": parent.last_synced_at.isoformat() if parent.last_synced_at else None,
            "waba_catalog_linked": waba_linked,
            "push": last_push,
            "lookup": last_lookup,
            "requeued": parent.sync_status == "pending",
            "skipped_push": skipped_push,
            "verification": {
                "matched_retailer_id": True,
                "content_matched": True,
                "lookup_fields": list(LOOKUP_VERIFIED_FIELDS),
                "not_verified_fields": list(LOOKUP_UNVERIFIED_FIELDS),
                "comparison": last_comparison,
                "variant_results": variant_results,
            },
        }

    if not _stamp_with_lease(db, parent, lease, _stamp_verify_lag):
        return _abandon_stale_lease(db)
    sm = _read_sync_meta(parent)
    exhausted = bool(sm.get("verify_exhausted"))
    return {
        "ok": False,
        "sync_status": parent.sync_status,
        "error_code": "verification_exhausted" if exhausted else "pending_content_verification",
        "meta_item_id": parent.meta_item_id,
        "retailer_id": retailer_id,
        "skipped_push": skipped_push,
        "requeued": parent.sync_status == "pending",
        "verification": {
            "matched_retailer_id": True,
            "content_matched": False,
            "lookup_fields": list(IDENTITY_LOOKUP_FIELDS),
            "content_fields": list(CONTENT_LOOKUP_FIELDS),
            "not_verified_fields": list(LOOKUP_UNVERIFIED_FIELDS),
            "comparison": last_comparison,
            "variant_results": variant_results,
            "exhausted": exhausted,
        },
    }


def _load_product(db: Any, tenant_id: int, product_id: int) -> Optional[Any]:
    from models import Product  # noqa: PLC0415

    return (
        db.query(Product)
        .filter(Product.id == int(product_id), Product.tenant_id == int(tenant_id))
        .first()
    )


def run_native_meta_sync_background(tenant_id: int, product_id: int) -> None:
    """Background entry — fresh DB session only."""
    from core.database import SessionLocal  # noqa: PLC0415

    db = SessionLocal()
    try:
        attempt_native_meta_sync(db, int(tenant_id), int(product_id))
    except Exception:  # noqa: BLE001
        logger.exception(
            "[NATIVE_META_SYNC] background failed tenant=%s product=%s",
            tenant_id,
            product_id,
        )
        db.rollback()
    finally:
        db.close()


def schedule_native_meta_sync(background_tasks: Any, tenant_id: int, product_id: int) -> None:
    background_tasks.add_task(run_native_meta_sync_background, int(tenant_id), int(product_id))


__all__ = [
    "META_RELEVANT_PATCH_KEYS",
    "attempt_native_meta_sync",
    "build_sync_response_fields",
    "mark_native_meta_sync_pending",
    "meta_relevant_patch_keys",
    "retry_allowed_for_status",
    "compare_pushed_content_to_lookup",
    "classify_block_code",
    "verify_retry_is_due",
    "run_native_meta_sync_background",
    "schedule_native_meta_sync",
    "sync_error_summary",
    "MAX_VERIFY_LAG_RETRIES",
    "SYNC_STALE_TTL",
]
