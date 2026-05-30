"""
core/active_order_context.py
────────────────────────────
Structured post-order commerce context on ``Conversation.extra_metadata``.

Phase A: additive persistence + structured-first reads. History heuristics
remain the resilience fallback layer.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Write-source telemetry (rollout debugging) ───────────────────────────────

WRITE_SOURCE_APPLY_STATE_PATCH = "apply_state_patch"
WRITE_SOURCE_STATE_STORE = "state_store"
WRITE_SOURCE_FUTURE_STORE_SYNC = "future_store_sync"

# ── Canonical order lifecycle (Phase A) ──────────────────────────────────────

CANONICAL_ORDER_STATUSES = frozenset({
    "pending_review",
    "confirmed",
    "preparing",
    "shipped",
    "delivered",
    "cancelled",
})

# Upstream slugs → canonical ``order_status``.
_RAW_TO_CANONICAL: Dict[str, str] = {
    "under_review":      "pending_review",
    "in_review":         "pending_review",
    "awaiting_review":   "pending_review",
    "review_pending":    "pending_review",
    "pending_review":    "pending_review",
    "payment_pending":   "pending_review",
    "pending_payment":   "pending_review",
    "awaiting_receipt":  "pending_review",
    "processing":        "preparing",
    "preparing":         "preparing",
    "ready":             "preparing",
    "in_progress":       "preparing",
    "confirmed":         "confirmed",
    "complete":          "confirmed",
    "completed":         "confirmed",
    "paid":              "confirmed",
    "shipped":           "shipped",
    "in_transit":        "shipped",
    "out_for_delivery":  "shipped",
    "delivering":        "shipped",
    "delivered":         "delivered",
    "cancelled":         "cancelled",
    "canceled":          "cancelled",
}

# Statuses that warrant persisting structured context on confirmation.
_PERSIST_CANONICAL = frozenset({
    "pending_review",
    "confirmed",
    "preparing",
    "shipped",
    "delivered",
})

PRE_SHIP_CANONICAL = frozenset({
    "pending_review",
    "confirmed",
    "preparing",
})

SHIPPED_CANONICAL = frozenset({
    "shipped",
    "delivered",
})

_ORDER_REF_RE = re.compile(
    r"(?:طلب(?:ك|كم)?\s*رقم|رقم\s*(?:ال)?طلب(?:ك|كم)?|order\s*(?:#|number)?)\s*[:#]?\s*(\d{4,})",
    re.IGNORECASE | re.UNICODE,
)

_HISTORY_STATUS_MARKERS: Tuple[Tuple[str, str], ...] = (
    ("بانتظار المراجعة", "pending_review"),
    ("بإنتظار المراجعة", "pending_review"),
    ("بمرحلة المراجعة", "pending_review"),
    ("مرحلة المراجعة", "pending_review"),
    ("pending review", "pending_review"),
    ("under review", "pending_review"),
    ("تم الشحن", "shipped"),
    ("في طريق", "shipped"),
    ("خارج للتوصيل", "shipped"),
    ("تم التسليم", "delivered"),
)

_SHIPPED_BODY_RE = re.compile(
    r"(?<![\u064a\u064a])تم\s+شحن(?:ه|ها|هم)?",
    re.UNICODE,
)


def _extract_order_reference_from_history(
    history: Optional[List[Dict[str, Any]]],
) -> str:
    if not history:
        return ""
    try:
        for turn in reversed(history):
            body = str((turn or {}).get("body") or "")
            if not body:
                continue
            match = _ORDER_REF_RE.search(body)
            if match:
                return match.group(1)
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _resolve_order_status_from_history(
    history: Optional[List[Dict[str, Any]]],
) -> str:
    if not history:
        return ""
    try:
        for turn in reversed(history):
            direction = str((turn or {}).get("direction") or "").lower()
            if direction not in ("out", "outbound"):
                continue
            body = str((turn or {}).get("body") or "").lower()
            if not body:
                continue
            for marker, slug in _HISTORY_STATUS_MARKERS:
                if marker in body:
                    return slug
            if _SHIPPED_BODY_RE.search(body):
                return "shipped"
    except Exception:  # noqa: BLE001
        return ""
    return ""


def normalize_order_status(raw: Optional[str]) -> Tuple[str, str]:
    """Return ``(canonical_order_status, raw_order_status)``."""
    raw_slug = str(raw or "").strip().lower()
    if not raw_slug:
        return "", ""
    canonical = _RAW_TO_CANONICAL.get(raw_slug, raw_slug)
    if canonical not in CANONICAL_ORDER_STATUSES:
        # Unknown upstream slug — keep as-is for canonical slot so reads
        # still work; raw preserves the upstream value for debugging.
        canonical = raw_slug
    return canonical, raw_slug


def is_pre_ship_canonical(order_status: str) -> bool:
    slug = str(order_status or "").strip().lower()
    if slug in SHIPPED_CANONICAL:
        return False
    if slug in PRE_SHIP_CANONICAL:
        return True
    # Legacy / unknown — treat non-shipped as pre-ship when ambiguous.
    return slug not in SHIPPED_CANONICAL


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _product_summary(brain_state: Dict[str, Any], order_prep: Dict[str, Any]) -> str:
    focus = brain_state.get("current_product_focus") or {}
    if not isinstance(focus, dict):
        focus = {}
    title = str(focus.get("title") or focus.get("name") or "").strip()
    if not title:
        return ""
    try:
        qty = int(order_prep.get("quantity") or 1)
    except (TypeError, ValueError):
        qty = 1
    if qty > 1:
        return f"{title} {qty}x".strip()
    return title


def _resolve_order_id(brain_state: Dict[str, Any], order_prep: Dict[str, Any]) -> str:
    for candidate in (
        brain_state.get("draft_order_id"),
        order_prep.get("draft_order_id"),
    ):
        oid = str(candidate or "").strip()
        if oid:
            return oid
    return ""


def build_active_order_context(
    *,
    order_id: str,
    brain_state: Dict[str, Any],
    order_prep: Dict[str, Any],
    external_id: Optional[str] = None,
    confirmed_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the ``active_order_context`` object (no top-level wrapper keys)."""
    raw_status = str(order_prep.get("order_status") or "").strip()
    if not raw_status and order_prep.get("payment_receipt_received"):
        raw_status = "under_review"
    canonical, raw_slug = normalize_order_status(raw_status)
    if not canonical and order_prep.get("payment_receipt_received"):
        canonical, raw_slug = "pending_review", raw_status or "under_review"

    focus = brain_state.get("current_product_focus") or {}
    if not isinstance(focus, dict):
        focus = {}
    ext = external_id
    if not ext:
        ext = focus.get("external_id")

    return {
        "order_id":           str(order_id),
        "external_id":        str(ext).strip() if ext else None,
        "order_status":       canonical,
        "raw_order_status":   raw_slug or None,
        "shipping_status":    "not_shipped",
        "tracking_url":       None,
        "tracking_number":    None,
        "confirmed_at":       confirmed_at or _iso_now(),
        "product_summary":    _product_summary(brain_state, order_prep) or None,
    }


def load_commerce_bundle(extra_metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Read commerce keys from conversation extra_metadata."""
    meta = extra_metadata or {}
    ctx = meta.get("active_order_context")
    return {
        "active_order_id":      str(meta.get("active_order_id") or "").strip() or None,
        "active_order_context": dict(ctx) if isinstance(ctx, dict) else None,
        "recent_order_ids":     list(meta.get("recent_order_ids") or []),
    }


def load_commerce_bundle_from_db(
    db: Any,
    tenant_id: int,
    customer_phone: str,
) -> Dict[str, Any]:
    """Load commerce bundle for a customer phone (brain pipeline read path)."""
    try:
        from core.order_flow import _find_conversation_by_phone, _normalize_e164  # noqa: PLC0415
        from models import Conversation, Customer  # noqa: PLC0415

        e164 = _normalize_e164(customer_phone) or customer_phone
        conv = _find_conversation_by_phone(
            db,
            tenant_id=int(tenant_id),
            phones=(e164, customer_phone),
            Conversation=Conversation,
            Customer=Customer,
        )
        if conv is None:
            return load_commerce_bundle(None)
        return load_commerce_bundle(dict(conv.extra_metadata or {}))
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[ACTIVE_ORDER_CONTEXT] load_from_db failed tenant=%s: %s",
            tenant_id, exc,
        )
        return load_commerce_bundle(None)


def structured_indicates_post_order(bundle: Optional[Dict[str, Any]]) -> bool:
    """True when structured ``active_order_context`` proves a post-order state."""
    if not bundle:
        return False
    ctx = bundle.get("active_order_context")
    if not isinstance(ctx, dict):
        return False
    order_id = str(ctx.get("order_id") or bundle.get("active_order_id") or "").strip()
    order_status = str(ctx.get("order_status") or "").strip()
    return bool(order_id and order_status)


def should_persist_from_patch(state_patch: Dict[str, Any]) -> bool:
    if state_patch.get("payment_receipt_received") is True:
        return True
    raw = str(state_patch.get("order_status") or "").strip()
    if not raw:
        return False
    canonical, _ = normalize_order_status(raw)
    return canonical in _PERSIST_CANONICAL


def should_persist_from_brain_state(
    brain_state: Dict[str, Any],
    order_prep: Dict[str, Any],
) -> bool:
    if order_prep.get("payment_receipt_received"):
        return True
    order_id = _resolve_order_id(brain_state, order_prep)
    if not order_id:
        return False
    raw = str(order_prep.get("order_status") or "").strip()
    if not raw:
        return False
    canonical, _ = normalize_order_status(raw)
    return canonical in _PERSIST_CANONICAL


def persist_active_order_context(
    meta: Dict[str, Any],
    *,
    brain_state: Dict[str, Any],
    order_prep: Dict[str, Any],
    write_source: str,
    external_id: Optional[str] = None,
) -> bool:
    """Merge structured commerce context into *meta* (in-place). Returns True if written."""
    order_id = _resolve_order_id(brain_state, order_prep)
    if not order_id:
        logger.info(
            "[ACTIVE_ORDER_CONTEXT] skip persist write_source=%s reason=no_order_id",
            write_source,
        )
        return False

    ctx = build_active_order_context(
        order_id=order_id,
        brain_state=brain_state,
        order_prep=order_prep,
        external_id=external_id,
    )
    if not ctx.get("order_status"):
        logger.info(
            "[ACTIVE_ORDER_CONTEXT] skip persist write_source=%s order_id=%s "
            "reason=no_order_status",
            write_source, order_id,
        )
        return False

    recent: List[str] = [
        str(x).strip()
        for x in (meta.get("recent_order_ids") or [])
        if str(x).strip()
    ]
    if order_id in recent:
        recent.remove(order_id)
    recent.insert(0, order_id)

    meta["active_order_id"] = order_id
    meta["active_order_context"] = ctx
    meta["recent_order_ids"] = recent

    logger.info(
        "[ACTIVE_ORDER_CONTEXT] persisted write_source=%s order_id=%s "
        "order_status=%s shipping_status=%s product_summary=%r "
        "recent_order_ids_count=%d",
        write_source,
        order_id,
        ctx.get("order_status"),
        ctx.get("shipping_status"),
        ctx.get("product_summary"),
        len(recent),
    )
    return True


def maybe_persist_from_patch(
    meta: Dict[str, Any],
    *,
    brain_state: Dict[str, Any],
    order_prep: Dict[str, Any],
    state_patch: Dict[str, Any],
) -> bool:
    if not should_persist_from_patch(state_patch):
        return False
    return persist_active_order_context(
        meta,
        brain_state=brain_state,
        order_prep=order_prep,
        write_source=WRITE_SOURCE_APPLY_STATE_PATCH,
    )


def maybe_persist_from_brain_save(
    meta: Dict[str, Any],
    *,
    brain_state: Dict[str, Any],
    order_prep: Dict[str, Any],
) -> bool:
    if not should_persist_from_brain_state(brain_state, order_prep):
        return False
    current_id = str(meta.get("active_order_id") or "").strip()
    new_id = _resolve_order_id(brain_state, order_prep)
    if current_id == new_id and meta.get("active_order_context"):
        return False
    return persist_active_order_context(
        meta,
        brain_state=brain_state,
        order_prep=order_prep,
        write_source=WRITE_SOURCE_STATE_STORE,
    )


# ── Structured-first resolution (fallback: brain_state → history) ────────────

def active_order_context_source(bundle: Optional[Dict[str, Any]]) -> str:
    return "structured" if structured_indicates_post_order(bundle) else "inferred"


def tracking_available_from_bundle(bundle: Optional[Dict[str, Any]]) -> bool:
    """Phase A: only structured ``tracking_url`` counts (no invention)."""
    if not bundle:
        return False
    ctx = bundle.get("active_order_context")
    if not isinstance(ctx, dict):
        return False
    url = str(ctx.get("tracking_url") or "").strip()
    return bool(url)


def resolve_order_reference(
    *,
    commerce_bundle: Optional[Dict[str, Any]] = None,
    state: Any = None,
    history: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, str]:
    """Return ``(order_reference, tracking_resolution_mode)``."""
    bundle = commerce_bundle or {}
    ctx = bundle.get("active_order_context")
    if isinstance(ctx, dict):
        ref = str(ctx.get("order_id") or bundle.get("active_order_id") or "").strip()
        if ref:
            return ref, "structured"

    prep = getattr(state, "order_prep", None) if state is not None else None
    for candidate in (
        str(getattr(state, "draft_order_id", "") or "").strip() if state else "",
        str(getattr(prep, "draft_order_id", "") or "").strip() if prep else "",
    ):
        if candidate:
            return candidate, "inferred_order_prep"

    hist_ref = _extract_order_reference_from_history(history)
    if hist_ref:
        return hist_ref, "inferred_history"
    return "", "inferred_history"


def resolve_order_status(
    *,
    commerce_bundle: Optional[Dict[str, Any]] = None,
    state: Any = None,
    order_prep: Any = None,
    history: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, str]:
    """Return ``(order_status, tracking_resolution_mode)``."""
    bundle = commerce_bundle or {}
    ctx = bundle.get("active_order_context")
    if isinstance(ctx, dict):
        status = str(ctx.get("order_status") or "").strip()
        if status:
            return status, "structured"

    prep = order_prep if order_prep is not None else getattr(state, "order_prep", None)
    try:
        raw = str(getattr(prep, "order_status", "") or "").strip().lower()
        if raw:
            canonical, _ = normalize_order_status(raw)
            return canonical or raw, "inferred_order_prep"
    except Exception:  # noqa: BLE001
        pass

    hist_status = _resolve_order_status_from_history(history)
    if hist_status:
        canonical, _ = normalize_order_status(hist_status)
        return canonical or hist_status, "inferred_history"
    return "", "inferred_history"


def resolve_shipping_status(
    commerce_bundle: Optional[Dict[str, Any]] = None,
) -> str:
    bundle = commerce_bundle or {}
    ctx = bundle.get("active_order_context")
    if isinstance(ctx, dict):
        ss = str(ctx.get("shipping_status") or "").strip()
        if ss:
            return ss
    return "not_shipped"


def log_tracking_resolution_telemetry(
    *,
    tenant_id: int,
    commerce_bundle: Optional[Dict[str, Any]] = None,
    order_id: str = "",
    shipping_status: str = "",
    tracking_available: bool = False,
    tracking_resolution_mode: str = "",
) -> None:
    ctx_source = active_order_context_source(commerce_bundle)
    logger.info(
        "[ACTIVE_ORDER_CONTEXT] telemetry tenant=%s "
        "active_order_context_source=%s tracking_resolution_mode=%s "
        "order_id=%s shipping_status=%s tracking_available=%s",
        tenant_id,
        ctx_source,
        tracking_resolution_mode or "unknown",
        order_id or "—",
        shipping_status or "—",
        bool(tracking_available),
    )


def prepare_tracking_follow_up_decision(ctx: Any) -> Dict[str, Any]:
    """Build tracking follow-up args + emit Phase A telemetry for *ctx*."""
    bundle = getattr(ctx, "commerce_bundle", None) or {}
    tracking_avail = tracking_available_from_bundle(bundle)
    order_ref, ref_mode = resolve_order_reference(
        commerce_bundle=bundle,
        state=getattr(ctx, "state", None),
        history=getattr(ctx, "history", None),
    )
    status, status_mode = resolve_order_status(
        commerce_bundle=bundle,
        state=getattr(ctx, "state", None),
        order_prep=getattr(getattr(ctx, "state", None), "order_prep", None),
        history=getattr(ctx, "history", None),
    )
    resolution_mode = ref_mode if ref_mode == "structured" else status_mode
    shipping_status = resolve_shipping_status(bundle)
    log_tracking_resolution_telemetry(
        tenant_id=int(getattr(ctx, "tenant_id", 0) or 0),
        commerce_bundle=bundle,
        order_id=order_ref,
        shipping_status=shipping_status,
        tracking_available=tracking_avail,
        tracking_resolution_mode=resolution_mode,
    )
    from modules.ai.brain.intent.link_disambiguation import (  # noqa: PLC0415
        build_tracking_follow_up_args,
    )

    return build_tracking_follow_up_args(
        state=getattr(ctx, "state", None),
        history=getattr(ctx, "history", None),
        commerce_bundle=bundle,
        tracking_available=tracking_avail,
    )
