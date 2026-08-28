"""
routers/orders.py
─────────────────
Tenant-scoped order list endpoints for the merchant dashboard.

Backed by the real `Order` table.

Status mapping (Salla → dashboard buckets):
    paid        — order is fully paid / completed / delivered
    pending     — awaiting payment, review, fulfillment, or shipment
    failed      — payment explicitly failed
    cancelled   — explicitly cancelled / refunded / returned

Anything not recognised is treated as `pending` (NOT cancelled) so that
unknown merchant-customised Salla statuses never silently appear as ملغي.
"""
from __future__ import annotations

import ast
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from core.database import get_db
from core.phone_coerce import coerce_customer_info_phone, coerce_phone_str as _coerce_phone_str
from core.tenant import get_or_create_tenant, resolve_tenant_id
from models import (
    Conversation,
    Customer,
    CustomerProfile,
    MessageEvent,
    Order,
    WhatsAppConnection,
)

router = APIRouter(prefix="/orders", tags=["Orders"])
logger = logging.getLogger("nahla.orders")


# ── Salla status → UI bucket ────────────────────────────────────────────────
# Source: real Salla status slugs observed in production + customised store
# slugs returned by /admin/v2/orders. We classify into 4 visual buckets so
# the dashboard renders meaningful badges instead of an avalanche of "ملغي".
PAID_STATUSES = frozenset({
    "paid", "completed", "complete", "confirmed",
    "delivered", "delivering",
    "shipped", "out_for_delivery",
    "fulfilled",
})
PENDING_STATUSES = frozenset({
    "pending",
    "pending_payment", "payment_pending", "awaiting_payment",
    "payment_submitted",
    "pending_confirmation", "awaiting_confirmation",
    "cod_pending",
    "under_review", "in_review",
    "in_progress", "processing",
    "preparing", "in_preparation",
    "ready_for_pickup", "ready_for_shipment",
    "restored",
    "on_hold",
    "draft",
    "new",
})
FAILED_STATUSES = frozenset({
    "failed", "payment_failed", "expired",
})
CANCELLED_STATUSES = frozenset({
    "cancelled", "canceled",
    "refunded",
    "returned", "return",
    "voided",
})

STATUS_LABELS_AR: Dict[str, str] = {
    "paid":      "مدفوع",
    "pending":   "قيد المعالجة",
    "failed":    "فشل الدفع",
    "cancelled": "ملغي",
}

RAW_STATUS_LABELS_AR: Dict[str, str] = {
    "draft":                 "مسودة طلب",
    "pending_customer_info": "ناقص بيانات",
    "pending_payment":       "بانتظار الدفع",
    "payment_submitted":     "دفع مرسل — يحتاج تحقق",
    "cod_pending":           "دفع عند الاستلام",
    "paid":                  "مدفوع",
    "processing":            "قيد التجهيز",
    "ready_to_ship":         "جاهز للشحن",
    "shipment_created":      "تم إنشاء الشحنة",
    "label_generated":       "تم توليد البوليصة",
    "shipped":               "تم الشحن",
    "delivered":             "تم التسليم",
    "completed":             "مكتمل",
    "cancelled":             "ملغي",
    "abandoned":             "متروك",
}

# Origin platform → Arabic label for the "المصدر" column.
SOURCE_LABELS_AR: Dict[str, str] = {
    "salla":    "سلة",
    "zid":      "زد",
    "shopify":  "Shopify",
    "whatsapp": "واتساب",
    "manual":   "يدوي",
}


def _resolve_source(order: Order) -> str:
    """
    Pick the canonical origin for an order. Order of precedence:
      1. The dedicated `source` column set by adapters / ai_sales.
      2. extra_metadata.source — supports legacy `ai_sales_agent` rows.
      3. Default to "salla" so historical syncs (which only ever ran
         against Salla) don't render a blank المصدر cell.
    """
    raw = (getattr(order, "source", None) or "").strip().lower()
    if raw in SOURCE_LABELS_AR:
        return raw
    meta_src = ((order.extra_metadata or {}).get("source") or "").strip().lower()
    if meta_src in ("ai_sales_agent", "ai_sales", "whatsapp", "ai"):
        return "whatsapp"
    if meta_src in SOURCE_LABELS_AR:
        return meta_src
    return "salla"


def _looks_like_phone(text: str) -> bool:
    """A 'name' that's actually just a phone number — common when Salla's
    order payload only ships the customer mobile and no name."""
    if not text:
        return False
    digits = text.lstrip("+").replace(" ", "").replace("-", "")
    return digits.isdigit() and len(digits) >= 7


def _resolve_customer_display(order: Order, customer_lookup: Optional[Dict[str, str]] = None) -> str:
    from core.order_customer_display import resolve_order_customer_display_name  # noqa: PLC0415

    return resolve_order_customer_display_name(
        order,
        customer_lookup,
        normalise_phone_key=_normalise_phone_key,
    )


def _resolve_order_number(order: Order) -> str:
    """
    Display "#<external_order_number>" so the merchant sees the same
    number their store dashboard shows, not Nahla's internal pk.
    """
    raw = (
        (getattr(order, "external_order_number", None) or "").strip()
        or (order.external_id or "").strip()
    )
    if not raw:
        return f"#{order.id}"
    return raw if raw.startswith("#") else f"#{raw}"


# ── External store URLs ───────────────────────────────────────────────────
# Each entry is a callable (raw_external_id, raw_order_number) → URL or None.
# Salla's merchant dashboard accepts the internal id under /orders/<id>.
# Zid uses its merchant panel; Shopify uses the human "name" (the # number).
def _store_url_salla(external_id: Optional[str], order_number: Optional[str]) -> Optional[str]:
    target = (external_id or order_number or "").strip().lstrip("#")
    if not target:
        return None
    return f"https://salla.sa/dashboard/orders/{target}"


def _store_url_zid(external_id: Optional[str], order_number: Optional[str]) -> Optional[str]:
    target = (external_id or order_number or "").strip().lstrip("#")
    if not target:
        return None
    return f"https://web.zid.sa/orders/{target}"


def _store_url_shopify(external_id: Optional[str], order_number: Optional[str]) -> Optional[str]:
    target = (external_id or order_number or "").strip().lstrip("#")
    if not target:
        return None
    return f"https://admin.shopify.com/orders/{target}"


_STORE_URL_BUILDERS = {
    "salla":   _store_url_salla,
    "zid":     _store_url_zid,
    "shopify": _store_url_shopify,
}


def _build_store_url(source_key: str, external_id: Optional[str], order_number: Optional[str]) -> Optional[str]:
    """Return the deep-link to the order page in the upstream store
    dashboard, or None for sources that don't have one (whatsapp/manual)."""
    builder = _STORE_URL_BUILDERS.get(source_key)
    if not builder:
        return None
    return builder(external_id, order_number)


def _parse_corrupt_status(raw: str) -> str:
    """
    Legacy rows synced before the salla_adapter fix stored a Python repr of
    the Salla status dict (e.g. "{'id': 566146469, 'name': '...',
    'slug': 'under_review'}"). When we see one in DB, recover the slug at
    READ time so the dashboard isn't broken until backfill runs.
    """
    text = (raw or "").strip()
    if not text.startswith("{"):
        return text
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text
    if isinstance(parsed, dict):
        return str(parsed.get("slug") or parsed.get("name") or parsed.get("code") or text)
    return text


def _classify_status(raw: Any) -> str:
    """Map a stored DB status into one of {paid, pending, failed, cancelled}."""
    text = _parse_corrupt_status(str(raw or "")).strip().lower()
    if not text:
        return "pending"
    if text in PAID_STATUSES:
        return "paid"
    if text in PENDING_STATUSES:
        return "pending"
    if text in FAILED_STATUSES:
        return "failed"
    if text in CANCELLED_STATUSES:
        return "cancelled"
    # Unknown / merchant-customised slug → keep it visible as pending
    # rather than silently classifying as cancelled. Log so we can extend
    # the maps if a new common slug shows up.
    logger.info(
        "[orders] unrecognised order status %r — defaulting bucket=pending",
        text,
    )
    return "pending"


def _to_float_sar(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        return _to_float_sar(value.get("amount") or value.get("value") or 0)
    text = str(value).replace("ر.س", "").replace(",", "").replace("SAR", "").strip()
    try:
        return float(text)
    except Exception:
        return 0.0


def _format_total(amount_sar: float, raw: Any) -> str:
    """Pretty-print the order amount for the table cell."""
    if amount_sar > 0:
        return f"{amount_sar:.2f} ر.س"
    text = str(raw or "").strip()
    return text or "0.00 ر.س"


def _parse_order_timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    text = str(value).strip()
    for variant in (
        text.replace("Z", "+00:00"),
        text.replace(" ", "T", 1),
        text.split(".", 1)[0].replace(" ", "T", 1),
    ):
        try:
            dt = datetime.fromisoformat(variant)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _read_created_at(order: Order, fallback: datetime) -> datetime:
    """
    The Order model has no `created_at` column — the canonical timestamp
    lives in `extra_metadata['created_at']` (set during sync from the
    upstream `created_at` field). Fall through every plausible source so
    the dashboard never claims an old order is "today".
    """
    meta = getattr(order, "extra_metadata", None) or {}
    catalog_meta = meta.get("catalog_order") if isinstance(meta.get("catalog_order"), dict) else {}
    candidates: List[Any] = [
        meta.get("created_at"),
        meta.get("draft_created_at"),
        meta.get("display_created_at"),
        meta.get("source_message_created_at"),
        meta.get("first_customer_message_at"),
        catalog_meta.get("source_message_at") if isinstance(catalog_meta, dict) else None,
    ]
    for cand in candidates:
        parsed = _parse_order_timestamp(cand)
        if parsed is not None:
            return parsed
    for cand in (
        getattr(order, "created_at", None),
        meta.get("updated_at"),
        getattr(order, "updated_at", None),
        meta.get("last_synced_at"),
    ):
        parsed = _parse_order_timestamp(cand)
        if parsed is not None:
            return parsed
    return fallback


def _list_sort_created_at(order: Order) -> datetime:
    """Actual order-creation time for /orders ranking.

    Ignores last_updated/sync timestamps so a status or import touch cannot
    promote an older order. Missing created_at sorts last, then by id.
    """
    meta = getattr(order, "extra_metadata", None) or {}
    catalog_meta = meta.get("catalog_order") if isinstance(meta.get("catalog_order"), dict) else {}
    candidates: List[Any] = [
        meta.get("created_at"),
        meta.get("draft_created_at"),
        meta.get("display_created_at"),
        meta.get("source_message_created_at"),
        meta.get("first_customer_message_at"),
        catalog_meta.get("source_message_at") if isinstance(catalog_meta, dict) else None,
        getattr(order, "created_at", None),
    ]
    for cand in candidates:
        parsed = _parse_order_timestamp(cand)
        if parsed is not None:
            return parsed
    return datetime.min.replace(tzinfo=timezone.utc)


def _read_last_updated_at(order: Order, *, created_at: datetime) -> datetime:
    """Last operational sync or status change — not the list display date."""
    meta = getattr(order, "extra_metadata", None) or {}
    for cand in (
        meta.get("last_updated_at"),
        meta.get("status_changed_at"),
        meta.get("updated_at"),
        meta.get("last_synced_at"),
    ):
        parsed = _parse_order_timestamp(cand)
        if parsed is not None:
            return parsed
    return created_at


def _build_customer_lookup(db: Session, tenant_id: int) -> Dict[str, str]:
    """phone → display name map used to fill in missing names on order rows."""
    out: Dict[str, str] = {}
    for cust in (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant_id)
        .all()
    ):
        phone = _coerce_phone_str(cust.phone)
        if not phone:
            continue
        meta = cust.extra_metadata or {}
        candidates = []
        if isinstance(meta, dict):
            for key in ("wa_profile_name", "profile_name", "whatsapp_name", "display_name"):
                candidates.append(str(meta.get(key) or "").strip())
        if cust.name:
            candidates.append(str(cust.name).strip())
        display = ""
        for raw in candidates:
            if raw and not _looks_like_phone(raw):
                display = raw
                break
        if not display:
            continue
        out[phone] = display
        digits = _normalise_phone_key(phone)
        if digits:
            out[digits] = display
    return out


def _normalise_phone_key(phone: Optional[str]) -> str:
    """Digits-only phone, with leading + stripped — suitable for DB lookups."""
    return _coerce_phone_str(phone).lstrip("+").replace(" ", "").replace("-", "")


def _build_vip_phone_set(db: Session, tenant_id: int) -> set[str]:
    """
    Phones (digits-only) of customers whose CustomerProfile marks them as
    high-value. Used so the orders list can flag a row as `vip` without
    re-running the full customer-intelligence pipeline per request.
    Two sources are union-ed:
      1. CustomerProfile.segment == 'vip'
      2. CustomerProfile.rfm_segment in {'champion', 'loyal'}
    """
    out: set[str] = set()
    rows = (
        db.query(CustomerProfile, Customer.phone)
        .join(Customer, Customer.id == CustomerProfile.customer_id)
        .filter(
            CustomerProfile.tenant_id == tenant_id,
            Customer.phone.isnot(None),
        )
        .all()
    )
    for prof, phone in rows:
        is_vip = (
            (prof.segment or "").lower() == "vip"
            or (prof.rfm_segment or "").lower() in {"champion", "loyal", "champions", "loyal_customers"}
        )
        if not is_vip:
            continue
        digits = _normalise_phone_key(phone)
        if digits:
            out.add(digits)
    return out


def _build_unread_phone_set(db: Session, tenant_id: int) -> set[str]:
    """
    Phones (digits-only) of customers with at least one inbound WhatsApp
    message in a conversation that is NOT closed. Used so the orders list
    can flag rows that have an open thread the merchant should look at.
    """
    out: set[str] = set()
    convos = (
        db.query(Conversation, Customer.phone)
        .join(Customer, Customer.id == Conversation.customer_id, isouter=True)
        .filter(
            Conversation.tenant_id == tenant_id,
            Conversation.status != "closed",
        )
        .all()
    )
    for convo, phone in convos:
        meta_phone = (
            _coerce_phone_str((convo.extra_metadata or {}).get("customer_phone"))
            or _coerce_phone_str((convo.extra_metadata or {}).get("phone"))
        )
        digits = _normalise_phone_key(phone or meta_phone)
        if digits:
            out.add(digits)
    return out


def _has_open_conversation(unread_phones: set[str], phone: str) -> bool:
    return _normalise_phone_key(phone) in unread_phones


def _is_vip(vip_phones: set[str], phone: str) -> bool:
    return _normalise_phone_key(phone) in vip_phones


def _compute_needs_action(
    *,
    status: str,
    source_key: str,
    payment_link: Optional[str],
    is_vip_customer: bool,
    has_open_conv: bool,
    is_ai_created: bool,
    order: Optional[Order] = None,
    parsed_raw_status: str = "",
) -> List[Dict[str, str]]:
    """
    Build the list of "needs action" reasons for an order. Each reason is a
    dict the frontend can render as a colored chip:
        {
          "key":   "awaiting_payment",
          "label": "بانتظار الدفع",
          "level": "amber" | "red" | "blue" | "purple",
        }
    Empty list → the order is fine, no chip should be rendered.
    """
    reasons: List[Dict[str, str]] = []

    # WhatsApp lifecycle — operational chips from persisted state.
    if source_key == "whatsapp" and order is not None:
        from core.wa_order_dashboard import compute_wa_needs_action  # noqa: PLC0415

        _, wa_items = compute_wa_needs_action(order)
        for item in wa_items:
            reasons.append({
                "key":   item["key"],
                "label": item["label"],
                "level": item["level"],
            })

    elif status == "pending":
        reasons.append({
            "key":   "awaiting_payment",
            "label": "بانتظار الدفع",
            "level": "amber",
        })
        if not payment_link:
            reasons.append({
                "key":   "no_payment_link",
                "label": "لا يوجد رابط دفع",
                "level": "red",
            })

    if is_vip_customer:
        reasons.append({
            "key":   "vip",
            "label": "عميل VIP",
            "level": "purple",
        })

    if has_open_conv:
        reasons.append({
            "key":   "open_conversation",
            "label": "محادثة مفتوحة",
            "level": "blue",
        })

    # Whatsapp-originated, AI-created order with no follow-up conversation
    # opened means the merchant should at least confirm with the customer.
    if (
        source_key == "whatsapp"
        and is_ai_created
        and not has_open_conv
        and parsed_raw_status not in ("abandoned", "cancelled", "completed", "paid")
    ):
        reasons.append({
            "key":   "whatsapp_unfollowed",
            "label": "طلب من واتساب بدون متابعة",
            "level": "amber",
        })

    return reasons


def _build_timeline(order: Order, *, has_open_conv: bool, source_label: str) -> List[Dict[str, Any]]:
    """
    A best-effort, monotonic activity log for an order. Today the data lives
    in three places: Order columns, Order.extra_metadata, and (eventually)
    MessageEvent rows linked via metadata.order_id. We surface what we know.
    """
    meta = order.extra_metadata or {}
    events: List[Dict[str, Any]] = []

    created_at = _read_created_at(order, fallback=datetime.now(timezone.utc))
    is_ai = (
        (getattr(order, "source", None) == "whatsapp")
        or (meta.get("source") in ("ai_sales_agent", "ai_sales", "ai"))
    )
    creator = "أنشأه الذكاء" if is_ai else "أُنشئ من المتجر"
    events.append({
        "key":        "created",
        "label":      f"تم إنشاء الطلب — {creator} ({source_label})",
        "at":         created_at.isoformat(),
        "icon":       "package",
    })

    if order.checkout_url:
        events.append({
            "key":   "payment_link_attached",
            "label": "تم إنشاء رابط الدفع للطلب",
            "at":    created_at.isoformat(),
            "icon":  "link",
        })

    # Each payment reminder push appends a record to extra_metadata.payment_reminders.
    for reminder in (meta.get("payment_reminders") or []):
        events.append({
            "key":   "payment_reminder_sent",
            "label": "تم إرسال تذكير دفع للعميل",
            "at":    reminder.get("sent_at") or "",
            "icon":  "bell",
        })

    # Last status change tracked by sync layer.
    if meta.get("status_changed_at"):
        events.append({
            "key":   "status_updated",
            "label": f"آخر تحديث للحالة: {_parse_corrupt_status(str(order.status or ''))}",
            "at":    str(meta.get("status_changed_at")),
            "icon":  "refresh",
        })

    for pt_event in (meta.get("payment_timeline") or []):
        if not isinstance(pt_event, dict):
            continue
        ev_key = str(pt_event.get("event") or "")
        if ev_key == "payment_confirmed":
            events.append({
                "key":   "payment_confirmed",
                "label": "تم تأكيد وصول التحويل البنكي من التاجر",
                "at":    str(pt_event.get("verified_at") or ""),
                "icon":  "refresh",
            })
        elif ev_key == "payment_submitted":
            events.append({
                "key":   "payment_submitted",
                "label": "أرسل العميل إثبات الدفع — بانتظار التحقق",
                "at":    str(pt_event.get("at") or ""),
                "icon":  "bell",
            })

    if meta.get("address_received_at") or meta.get("delivery_address_received_at"):
        events.append({
            "key":   "address_received",
            "label": "استُلم موقع/عنوان التوصيل من العميل",
            "at":    str(meta.get("address_received_at") or meta.get("delivery_address_received_at") or ""),
            "icon":  "package",
        })

    for ship_ev in (meta.get("shipment_timeline") or []):
        if not isinstance(ship_ev, dict):
            continue
        ev_name = str(ship_ev.get("event") or "").strip().lower()
        at = str(ship_ev.get("at") or "")
        if ev_name == "shipment_created":
            events.append({
                "key":   "shipment_created",
                "label": "تم إنشاء الشحنة داخل نحلة",
                "at":    at,
                "icon":  "package",
            })
        elif ev_name == "label_generated":
            events.append({
                "key":   "label_generated",
                "label": "تم توليد بيانات البوليصة",
                "at":    at,
                "icon":  "link",
            })

    for st_event in (meta.get("status_timeline") or []):
        if not isinstance(st_event, dict):
            continue
        to_status = str(st_event.get("to") or st_event.get("event") or "").strip().lower()
        at = str(st_event.get("at") or "")
        if to_status == "pending_payment":
            events.append({
                "key":   "pending_payment",
                "label": "انتقل الطلب إلى بانتظار الدفع",
                "at":    at,
                "icon":  "bell",
            })
        elif to_status == "payment_submitted":
            events.append({
                "key":   "payment_submitted",
                "label": "أرسل العميل إثبات الدفع — بانتظار التحقق",
                "at":    at,
                "icon":  "bell",
            })
        elif to_status == "paid":
            events.append({
                "key":   "payment_confirmed",
                "label": "تم تأكيد الدفع",
                "at":    at,
                "icon":  "refresh",
            })

    if has_open_conv:
        events.append({
            "key":   "conversation_open",
            "label": "للعميل محادثة واتساب مفتوحة",
            "at":    "",
            "icon":  "message",
        })

    # Deduplicate exact (key, at) pairs and sort by timestamp so the UI
    # renders a clean chronological list.
    seen: set[tuple[str, str]] = set()
    unique: List[Dict[str, Any]] = []
    for ev in events:
        k = (ev["key"], ev["at"])
        if k in seen:
            continue
        seen.add(k)
        unique.append(ev)
    unique.sort(key=lambda e: e.get("at") or "")
    return unique


def _build_payment_reminder_text(
    *,
    customer_name: Optional[str],
    order_number: str,
    payment_url: Optional[str],
    phone: Optional[str] = None,  # noqa: ARG001 — kept for forward signature
) -> str:
    """
    Friendly Arabic reminder body. Always includes the order number; only
    references the payment link when the merchant actually has one. Kept
    short so it fits cleanly in WhatsApp's free-text body.
    """
    # Use the central greeting fallback so reminders speak the same
    # voice as campaigns and templates (``"عميلنا الغالي"``).
    from core.customer_display import (  # noqa: PLC0415
        DEFAULT_FALLBACK_NAME as _FALLBACK_GREETING,
        display_name_passthrough_or_fallback as _greet_name,
    )
    name = _greet_name(customer_name)
    if name == "—" or not name.strip():
        name = _FALLBACK_GREETING

    lines = [
        f"مرحباً {name} 👋",
        f"تذكير بطلبك رقم {order_number}.",
    ]
    if payment_url:
        lines.append(f"يمكنك إتمام الدفع من هنا: {payment_url}")
    else:
        lines.append("سعداء بخدمتك — متى ما رغبت بإتمام طلبك تواصل معنا 🌟")
    lines.append("شكراً لاختيارك متجرنا 🙏")
    return "\n".join(lines)


def _attach_missing_fields_engine_detail_payload(
    payload: Dict[str, Any],
    order: Order,
    order_meta: Dict[str, Any],
    *,
    db: Session,
    tenant_id: int,
) -> None:
    """Always set ``missing_fields_engine`` for Nahla WhatsApp orders on detail."""
    from core.order_context_builder import build_order_context_for_order  # noqa: PLC0415
    from core.order_context_prefill import build_order_context_api_payload  # noqa: PLC0415
    from core.order_missing_fields_engine import (  # noqa: PLC0415
        augment_divergence_with_confirm_blockers,
        log_missing_fields_engine_detail,
        missing_fields_engine_unavailable_dict,
        missing_fields_result_to_api_dict,
    )
    from core.wa_order_editor import is_wa_whatsapp_order  # noqa: PLC0415

    order_id = getattr(order, "id", None)
    legacy_missing = list(order_meta.get("missing_fields") or [])
    confirm_blockers = list(payload.get("confirm_blockers") or [])

    if not is_wa_whatsapp_order(order):
        log_missing_fields_engine_detail(
            order_id=order_id,
            tenant_id=int(tenant_id),
            available=False,
            reason="not_whatsapp_order",
            legacy_missing=legacy_missing,
            confirm_blockers=confirm_blockers,
            build_source="orders_api_detail",
        )
        return

    try:
        ctx = build_order_context_for_order(
            db,
            tenant_id=int(tenant_id),
            order=order,
        )
        payload["order_context_prefill"] = build_order_context_api_payload(ctx)
        if ctx.missing_fields_result is None:
            payload["missing_fields_engine"] = missing_fields_engine_unavailable_dict(
                "engine_result_none"
            )
            log_missing_fields_engine_detail(
                order_id=order_id,
                tenant_id=int(tenant_id),
                available=False,
                reason="engine_result_none",
                legacy_missing=legacy_missing,
                confirm_blockers=confirm_blockers,
                build_source="orders_api_detail",
            )
            return

        engine_result = augment_divergence_with_confirm_blockers(
            ctx.missing_fields_result,
            confirm_blockers=confirm_blockers,
        )
        payload["missing_fields_engine"] = missing_fields_result_to_api_dict(engine_result)
        log_missing_fields_engine_detail(
            order_id=order_id,
            tenant_id=int(tenant_id),
            available=True,
            result=engine_result,
            legacy_missing=legacy_missing,
            confirm_blockers=confirm_blockers,
            build_source="orders_api_detail",
        )
    except Exception as exc:  # noqa: BLE001
        payload["missing_fields_engine"] = missing_fields_engine_unavailable_dict(
            f"build_failed:{type(exc).__name__}"
        )
        payload["order_context_prefill"] = None
        logger.exception(
            "[ORDERS_API] missing_fields_engine detail failed order_id=%s tenant=%s",
            order_id,
            tenant_id,
        )
        log_missing_fields_engine_detail(
            order_id=order_id,
            tenant_id=int(tenant_id),
            available=False,
            reason=f"build_failed:{type(exc).__name__}",
            legacy_missing=legacy_missing,
            confirm_blockers=confirm_blockers,
            build_source="orders_api_detail",
        )


def _serialise_order(
    order: Order,
    *,
    customer_lookup: Dict[str, str],
    now: datetime,
    detailed: bool = False,
    vip_phones: Optional[set[str]] = None,
    unread_phones: Optional[set[str]] = None,
    db: Optional[Session] = None,
    tenant_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Render an order for the dashboard. ``detailed=True`` adds line-item
    breakdowns and the deep-link to the upstream store; the list endpoint
    keeps it lean for performance.
    """
    created_at  = _read_created_at(order, fallback=now)
    last_updated_at = _read_last_updated_at(order, created_at=created_at)
    raw_status  = str(order.status or "")
    status      = _classify_status(raw_status)
    customer_info = order.customer_info or {}
    line_items    = order.line_items or []
    order_meta    = order.extra_metadata or {}
    source_key    = _resolve_source(order)

    from core.order_amount_display import resolve_display_amount_sar  # noqa: PLC0415

    amount_value, persisted_amount_value, persisted_amount_stale = resolve_display_amount_sar(
        source=source_key,
        line_items=line_items,
        persisted_total=order.total,
    )
    meta_product  = str(order_meta.get("product_title") or "").strip()

    item_titles: List[str] = []
    detailed_items: List[Dict[str, Any]] = []
    for item in line_items:
        name = (
            item.get("product_name")
            or item.get("title")
            or item.get("name")
            or meta_product
            or "منتج"
        )
        qty  = int(item.get("quantity") or 1)
        item_titles.append(f"{name} ×{qty}")

    if detailed:
        if db is not None and tenant_id is not None and source_key == "whatsapp":
            from core.wa_order_line_item_evidence import (  # noqa: PLC0415
                enrich_order_line_items_for_dashboard,
            )

            detailed_items = enrich_order_line_items_for_dashboard(
                db, tenant_id, line_items,
            )
        else:
            from core.wa_order_line_item_evidence import (  # noqa: PLC0415
                MATCH_STATUS_CONFIRMED,
                MATCH_STATUS_CUSTOM_UNMATCHED,
                MATCH_STATUS_NEEDS_REVIEW,
                parse_unit_price,
                sanitize_line_item_without_db,
            )

            for item in line_items:
                row = sanitize_line_item_without_db(dict(item or {}))
                name = (
                    row.get("product_name")
                    or row.get("title")
                    or row.get("name")
                    or meta_product
                    or "منتج"
                )
                qty = int(row.get("quantity") or 1)
                unit_price_f = parse_unit_price(row.get("unit_price") or row.get("price"))
                status = str(row.get("match_status") or "").strip()
                if not status:
                    status = (
                        MATCH_STATUS_CUSTOM_UNMATCHED
                        if not row.get("product_id")
                        else MATCH_STATUS_NEEDS_REVIEW
                    )
                detailed_items.append({
                    "product_id":   str(row.get("product_id") or ""),
                    "name":         name,
                    "quantity":     qty,
                    "variant_id":   str(row.get("variant_id") or "") or None,
                    "variant_label": str(
                        row.get("variant_label")
                        or row.get("variant")
                        or row.get("size")
                        or ""
                    ).strip() or None,
                    "edition":      str(row.get("edition") or row.get("production") or "").strip() or None,
                    "unit_price":   unit_price_f,
                    "line_total":   round(unit_price_f * qty, 2) if unit_price_f is not None else None,
                    "image_url":    row.get("image_url") or row.get("image") or None,
                    "match_status": status,
                    "is_catalog_matched": status == MATCH_STATUS_CONFIRMED,
                    "query_hint":   row.get("query_hint"),
                })

    source_label = SOURCE_LABELS_AR.get(source_key, source_key)
    order_number = _resolve_order_number(order)
    display_name = _resolve_customer_display(order, customer_lookup)
    phone        = coerce_customer_info_phone(customer_info)
    is_ai_created = source_key == "whatsapp" or (
        (order.extra_metadata or {}).get("source") in ("ai_sales_agent", "ai_sales", "ai")
    )

    is_vip_customer = bool(vip_phones    and phone and _is_vip(vip_phones, phone))
    has_open_conv   = bool(unread_phones and phone and _has_open_conversation(unread_phones, phone))

    parsed_raw = _parse_corrupt_status(raw_status).strip().lower()

    needs_action = _compute_needs_action(
        status=status,
        source_key=source_key,
        payment_link=order.checkout_url,
        is_vip_customer=is_vip_customer,
        has_open_conv=has_open_conv,
        is_ai_created=is_ai_created,
        order=order,
        parsed_raw_status=parsed_raw,
    )

    from core.merchant_payment_confirmation import (  # noqa: PLC0415
        can_show_confirm_bank_transfer_button,
    )
    from core.order_payment_policy import (  # noqa: PLC0415
        PAYMENT_METHOD_LABELS_AR,
        build_merchant_payment_alerts,
    )
    from core.wa_order_dashboard import (  # noqa: PLC0415
        BANK_TRANSFER_VERIFY_BANNER,
        build_action_chips,
        build_city_line,
        build_delivery_location_display,
        build_list_summary,
        compute_wa_needs_action,
        resolve_address_status_label_ar,
        resolve_lifecycle_filter_key,
        resolve_payment_status_label_ar,
        resolve_payment_verification_label_ar,
        resolve_wa_status_label_ar,
    )

    payment_alerts = build_merchant_payment_alerts(
        raw_status=parsed_raw,
        meta=order_meta,
    )
    if (
        payment_alerts
        and payment_alerts[0].get("key") == "bank_transfer_verify_before_ship"
    ):
        payment_alerts[0]["label"] = BANK_TRANSFER_VERIFY_BANNER
        payment_alerts[0]["message"] = BANK_TRANSFER_VERIFY_BANNER

    existing_keys = {r["key"] for r in needs_action}
    for alert in payment_alerts:
        if alert["key"] in existing_keys:
            continue
        needs_action.append({
            "key":   alert["key"],
            "label": alert.get("label") or alert.get("message", ""),
            "level": alert["level"],
        })

    payment_method = str(order_meta.get("payment_method") or "").strip().lower() or None
    payment_method_label = (
        PAYMENT_METHOD_LABELS_AR.get(payment_method, payment_method)
        if payment_method else None
    )

    status_label_ar = resolve_wa_status_label_ar(parsed_raw, order=order)
    address_status_label_ar = resolve_address_status_label_ar(order)
    payment_status_label_ar = resolve_payment_status_label_ar(order_meta)
    payment_verification_label_ar = resolve_payment_verification_label_ar(order_meta)
    action_chips = build_action_chips(order) if source_key == "whatsapp" else []
    wa_needs_action_flag, _ = (
        compute_wa_needs_action(order) if source_key == "whatsapp" else (bool(needs_action), needs_action)
    )
    lifecycle_filter = resolve_lifecycle_filter_key(order)
    city_line = build_city_line(order)
    list_summary = build_list_summary(
        customer_name=display_name,
        items_text="، ".join(item_titles) if item_titles else "—",
        amount_text=_format_total(amount_value, order.total),
        city_line=city_line,
        payment_label=payment_status_label_ar or status_label_ar,
        address_label=address_status_label_ar,
    )

    payload: Dict[str, Any] = {
        # `id` is kept as the human-visible order number so existing
        # frontend key/search/filter code shows the platform reference
        # instead of the DB pk. The internal pk is also exposed for
        # routing (the detail page is keyed on it).
        "id":           order_number,
        "order_number": order_number,
        "internal_id":  str(order.id),
        "external_id":  order.external_id,
        "customer":      display_name,
        "customer_name": display_name,
        "phone":         phone or "—",
        "items":         (
            f"{len(item_titles)} منتجات — {('، '.join(item_titles))}"
            if len(item_titles) > 1
            else ("، ".join(item_titles) if item_titles else "—")
        ),
        "amount":        _format_total(amount_value, order.total),
        "amount_sar":    round(amount_value, 2),
        "persisted_amount_sar": round(persisted_amount_value, 2),
        "persisted_amount_stale": persisted_amount_stale,
        "status":        status,
        "status_label":  status_label_ar or RAW_STATUS_LABELS_AR.get(parsed_raw) or STATUS_LABELS_AR.get(status, status),
        "status_label_ar": status_label_ar,
        "raw_status":    _parse_corrupt_status(raw_status),
        "raw_status_label": status_label_ar or RAW_STATUS_LABELS_AR.get(parsed_raw, parsed_raw or raw_status),
        "source":        source_key,
        "source_label":  source_label,
        "paymentLink":   order.checkout_url,
        "createdAt":     created_at.isoformat(),
        "display_created_at": created_at.isoformat(),
        "last_updated_at": last_updated_at.isoformat(),
        "updated_at":    last_updated_at.isoformat(),
        "is_ai_created": is_ai_created,
        "is_vip":        is_vip_customer,
        "has_open_conversation": has_open_conv,
        "needs_action":  needs_action,
        "needs_action_flag": wa_needs_action_flag or bool(needs_action),
        "action_chips":  action_chips,
        "lifecycle_filter": lifecycle_filter,
        "city_line":     city_line,
        "list_summary":  list_summary,
        "address_status_label_ar": address_status_label_ar,
        "payment_method": payment_method,
        "payment_method_label": payment_method_label,
        "payment_method_label_ar": payment_method_label,
        "payment_status": order_meta.get("payment_status"),
        "payment_status_label_ar": payment_status_label_ar,
        "payment_confirmed": bool(order_meta.get("payment_confirmed")),
        "payment_verification_status": order_meta.get("payment_verification_status"),
        "payment_verification_label_ar": payment_verification_label_ar,
        "payment_verified_at": order_meta.get("payment_verified_at"),
        "payment_verified_by": order_meta.get("payment_verified_by"),
        "merchant_payment_alert": payment_alerts[0] if payment_alerts else None,
        "merchant_payment_alerts": payment_alerts,
        "can_confirm_bank_transfer": can_show_confirm_bank_transfer_button(order),
        "merchant_post_confirm_notice": order_meta.get("merchant_post_confirm_notice"),
        "payment_receipt_received": bool(order_meta.get("payment_receipt_received")),
        "payment_receipt_parsed": order_meta.get("payment_receipt_parsed"),
        "shipping_blocked_reason": order_meta.get("shipping_blocked_reason"),
        "parsed_receipt_fields": order_meta.get("parsed_receipt_fields")
        or (order_meta.get("payment_receipt_metadata") or {}).get("parsed_receipt_fields"),
    }
    salla_amounts = order_meta.get("salla_amounts")
    if isinstance(salla_amounts, dict) and salla_amounts:
        payload["salla_amounts"] = salla_amounts
        payload["currency"] = salla_amounts.get("currency") or "SAR"

    if detailed:
        payload["line_items"] = detailed_items
        payload["customer_address"] = {
            "city":            customer_info.get("city"),
            "district":        customer_info.get("district"),
            "street":          customer_info.get("street"),
            "building_number": customer_info.get("building_number"),
            "postal_code":     customer_info.get("postal_code"),
            "address":         customer_info.get("address"),
        }
        payload["delivery_location"] = build_delivery_location_display(order)
        payload["address_status_label_ar"] = address_status_label_ar
        store_url = _build_store_url(source_key, order.external_id, order.external_order_number)
        whatsapp_url = f"https://wa.me/{phone.lstrip('+').replace(' ', '').replace('-', '')}" if phone else None
        conversation_url = f"/conversations?phone={phone}" if phone else None
        payload["links"] = {
            "store":        store_url,
            "store_label":  f"فتح الطلب في {source_label}" if store_url else None,
            "whatsapp":     whatsapp_url,
            "conversation": conversation_url,
        }
        payload["payment_method"] = payment_method
        payload["payment_method_label"] = payment_method_label
        payload["payment_status"] = order_meta.get("payment_status")
        payload["payment_confirmed"] = bool(order_meta.get("payment_confirmed"))
        payload["merchant_payment_alert"] = payment_alerts[0] if payment_alerts else None
        payload["merchant_payment_alerts"] = payment_alerts
        payload["can_confirm_bank_transfer"] = can_show_confirm_bank_transfer_button(order)
        payload["merchant_post_confirm_notice"] = order_meta.get("merchant_post_confirm_notice")
        payload["notes"]          = order_meta.get("notes")
        payload["timeline"]       = _build_timeline(
            order, has_open_conv=has_open_conv, source_label=source_label,
        )

        # Pre-built draft of the payment-reminder text the merchant can send
        # with one tap. The frontend uses this both for the in-Nahla send
        # and as the prefilled body if it falls back to the conversation
        # composer or wa.me.
        if status == "pending":
            payload["payment_reminder_draft"] = _build_payment_reminder_text(
                customer_name=display_name,
                order_number=order_number,
                payment_url=order.checkout_url,
                phone=phone,
            )
        else:
            payload["payment_reminder_draft"] = None

        from core.wa_order_editor import order_edit_capabilities  # noqa: PLC0415

        caps = order_edit_capabilities(
            order,
            enriched_line_items=detailed_items if detailed else None,
        )
        payload.update(caps)
        payload["customer_first_name"] = caps.get("customer_first_name")
        payload["customer_last_name"] = caps.get("customer_last_name")
        payload["internal_note"] = caps.get("internal_note")
        payload["google_maps_url"] = (
            order_meta.get("google_maps_url")
            or order_meta.get("delivery_address_url")
            or customer_info.get("google_maps_url")
        )
        payload["short_address_code"] = (
            order_meta.get("short_address_code")
            or order_meta.get("national_short_address")
            or customer_info.get("short_address_code")
        )
        payload["shipping_meta"] = {
            "shipping_provider": caps.get("shipping_provider") or "manual",
            "shipping_cost": caps.get("shipping_cost"),
            "tracking_number": caps.get("tracking_number"),
            "shipping_status": caps.get("shipping_status"),
            "national_short_address": caps.get("national_short_address"),
            "delivery_notes": caps.get("delivery_notes"),
        }
        if db is not None and tenant_id is not None:
            _attach_missing_fields_engine_detail_payload(
                payload,
                order,
                order_meta,
                db=db,
                tenant_id=int(tenant_id),
            )
        from core.order_shipping_snapshot import build_order_shipping_snapshot  # noqa: PLC0415
        from core.wa_order_editor import _order_prep_from_order  # noqa: PLC0415

        payload["shipping_snapshot"] = build_order_shipping_snapshot(
            order_prep=_order_prep_from_order(order),
            customer_info=customer_info,
            extra_metadata=order_meta,
            last_sync_snapshot=dict(order_meta.get("last_sync_snapshot") or {}),
        )
        payload["customer_address_persisted"] = bool(order_meta.get("customer_address_persisted"))
        if payload.get("order_context_prefill"):
            payload["known_previous_address"] = payload["order_context_prefill"].get(
                "known_previous_address"
            )

    return payload


def _apply_lifecycle_db_filter(query, lifecycle_filter: Optional[str]):
    """
    Narrow the SQL query for status-backed lifecycle tabs.
    ``needs_action`` and ``missing_location`` use a wider fetch + Python refine.
    """
    from core.wa_order_dashboard import (  # noqa: PLC0415
        LIFECYCLE_FILTER_ABANDONED,
        LIFECYCLE_FILTER_CANCELLED,
        LIFECYCLE_FILTER_COMPLETED,
        LIFECYCLE_FILTER_MISSING_LOCATION,
        LIFECYCLE_FILTER_NEEDS_ACTION,
        LIFECYCLE_FILTER_PAID,
        LIFECYCLE_FILTER_PAYMENT_SUBMITTED,
        LIFECYCLE_FILTER_PENDING_PAYMENT,
    )

    filt = (lifecycle_filter or "").strip().lower()
    if not filt or filt == "all":
        return query

    if filt == LIFECYCLE_FILTER_PENDING_PAYMENT:
        return query.filter(Order.status == "pending_payment")

    if filt == LIFECYCLE_FILTER_PAYMENT_SUBMITTED:
        return query.filter(Order.status == "payment_submitted")

    if filt == LIFECYCLE_FILTER_PAID:
        return query.filter(
            or_(
                Order.status == "paid",
                Order.extra_metadata["payment_confirmed"].astext == "true",
            )
        )

    if filt == LIFECYCLE_FILTER_ABANDONED:
        return query.filter(
            or_(Order.status == "abandoned", Order.is_abandoned.is_(True))
        )

    if filt == LIFECYCLE_FILTER_COMPLETED:
        return query.filter(Order.status.in_(("completed", "complete")))

    if filt == LIFECYCLE_FILTER_CANCELLED:
        return query.filter(Order.status.in_(("cancelled", "canceled")))

    if filt == LIFECYCLE_FILTER_MISSING_LOCATION:
        return query.filter(Order.status == "pending_customer_info")

    if filt == LIFECYCLE_FILTER_NEEDS_ACTION:
        return query.filter(
            Order.status.in_(
                (
                    "draft",
                    "pending_customer_info",
                    "pending_payment",
                    "payment_submitted",
                    "paid",
                )
            )
        )

    return query


@router.get("")
async def list_orders(
    request: Request,
    db: Session = Depends(get_db),
    lifecycle_filter: Optional[str] = Query(None, alias="lifecycle_filter"),
    source: Optional[str] = Query(None),
):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    from core.wa_order_dashboard import (  # noqa: PLC0415
        LIFECYCLE_FILTER_MISSING_LOCATION,
        LIFECYCLE_FILTER_NEEDS_ACTION,
        order_matches_lifecycle_filter,
    )

    q = db.query(Order).filter(Order.tenant_id == tenant_id)
    if source:
        src = source.strip().lower()
        if src == "whatsapp":
            q = q.filter(Order.source == "whatsapp")
        elif src in SOURCE_LABELS_AR:
            q = q.filter(Order.source == src)

    q = _apply_lifecycle_db_filter(q, lifecycle_filter)
    # Rank the full filtered set by created_at before capping the page.
    # Limiting by id first dropped newer-created rows that happened to have older pks.
    rows = q.all()
    now             = datetime.now(timezone.utc)
    today           = now.date()
    rows.sort(
        key=lambda o: (
            _list_sort_created_at(o),
            int(getattr(o, "id", 0) or 0),
        ),
        reverse=True,
    )

    filt = (lifecycle_filter or "").strip().lower()
    if filt in (LIFECYCLE_FILTER_NEEDS_ACTION, LIFECYCLE_FILTER_MISSING_LOCATION):
        rows = [r for r in rows if order_matches_lifecycle_filter(r, filt)]
        rows = rows[:200]
    elif len(rows) > 200:
        rows = rows[:200]

    customer_lookup = _build_customer_lookup(db, tenant_id)
    vip_phones      = _build_vip_phone_set(db, tenant_id)
    unread_phones   = _build_unread_phone_set(db, tenant_id)

    orders: List[Dict[str, Any]] = []
    pending_count   = 0
    completed_today = 0
    today_revenue   = 0.0
    whatsapp_today_count   = 0
    whatsapp_today_revenue = 0.0
    needs_action_count     = 0

    for order in rows:
        item = _serialise_order(
            order,
            customer_lookup=customer_lookup,
            now=now,
            vip_phones=vip_phones,
            unread_phones=unread_phones,
        )
        orders.append(item)

        if item["status"] == "pending":
            pending_count += 1
        if item["needs_action"]:
            needs_action_count += 1

        try:
            row_date = datetime.fromisoformat(item["createdAt"]).date()
        except Exception:
            row_date = today

        if row_date == today:
            today_revenue += item["amount_sar"]
            if item["status"] == "paid":
                completed_today += 1
            if item["source"] == "whatsapp":
                whatsapp_today_count += 1
                whatsapp_today_revenue += item["amount_sar"]

    return {
        "summary": {
            "total_orders":            len(orders),
            "today_revenue_sar":       round(today_revenue, 2),
            "pending_orders":          pending_count,
            "completed_today":         completed_today,
            # Nahla-specific KPIs so the merchant sees the value of
            # WhatsApp + AI-driven sales at a glance.
            "whatsapp_orders_today":   whatsapp_today_count,
            "whatsapp_revenue_today":  round(whatsapp_today_revenue, 2),
            # Operational KPI: how many of the listed orders have at least
            # one open action (awaiting payment, no link, VIP, etc).
            "orders_needing_action":   needs_action_count,
        },
        "orders": orders,
    }


def _lookup_order(db: Session, tenant_id: int, order_id: str) -> Optional[Order]:
    """
    Find an order by either:
      • its internal Nahla pk (numeric)
      • its platform external_id
      • its human-visible external_order_number (with or without leading "#")
    so the frontend can route /orders/<anything-it-knows> safely.
    """
    raw = (order_id or "").strip().lstrip("#")
    if not raw:
        return None

    q = db.query(Order).filter(Order.tenant_id == tenant_id)

    if raw.isdigit():
        hit = q.filter(Order.id == int(raw)).first()
        if hit:
            return hit

    return (
        q.filter(
            (Order.external_order_number == raw)
            | (Order.external_id == raw)
        ).first()
    )


@router.get("/{order_id}")
async def get_order_detail(order_id: str, request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    order = _lookup_order(db, tenant_id, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="order_not_found")

    customer_lookup = _build_customer_lookup(db, tenant_id)
    vip_phones      = _build_vip_phone_set(db, tenant_id)
    unread_phones   = _build_unread_phone_set(db, tenant_id)

    from core.order_shipment_service import (  # noqa: PLC0415
        evaluate_create_shipment,
        get_order_shipment,
        resolve_tenant_cod_enabled,
        serialise_shipment,
    )
    from core.order_shipping_policy import can_generate_label  # noqa: PLC0415

    cod_enabled = resolve_tenant_cod_enabled(db, tenant_id)
    shipment_row = get_order_shipment(db, tenant_id, order.id)
    create_gate = evaluate_create_shipment(
        order,
        cod_enabled=cod_enabled,
        existing_shipment=shipment_row,
    )
    label_gate = (
        can_generate_label(order, shipment_row, cod_enabled=cod_enabled)
        if shipment_row else None
    )

    payload = _serialise_order(
        order,
        customer_lookup=customer_lookup,
        now=datetime.now(timezone.utc),
        detailed=True,
        vip_phones=vip_phones,
        unread_phones=unread_phones,
        db=db,
        tenant_id=tenant_id,
    )
    payload["shipping"] = {
        "can_create_shipment": create_gate.allowed,
        "blocked_reason_key": create_gate.reason_key,
        "blocked_reason_ar": create_gate.message_ar,
        "can_generate_label": bool(label_gate and label_gate.allowed),
        "label_blocked_reason_key": label_gate.reason_key if label_gate else None,
        "label_blocked_reason_ar": label_gate.message_ar if label_gate else None,
        "shipment": serialise_shipment(shipment_row) if shipment_row else None,
    }
    return {"order": payload}


# ── Payment reminder ───────────────────────────────────────────────────────

class PaymentReminderIn(BaseModel):
    # Optional: merchant-edited message overriding the default draft.
    message: Optional[str] = None


@router.post("/{order_id}/send-payment-reminder")
async def send_payment_reminder(
    order_id: str,
    body: PaymentReminderIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Send a WhatsApp payment-reminder text for a pending order.

    Uses the same merchant-initiated send path as POST /conversations/reply
    (no new direct provider_send_message call) so:
      • the 24-hour service-window guard is enforced consistently,
      • a MessageEvent row is logged for the conversation history,
      • the unified automation/engine guardrail is preserved.

    Always succeeds in returning the prepared draft + a /conversations
    deep-link so the merchant can send manually if the WhatsApp window is
    closed (i.e. the customer hasn't messaged us in the last 24h).
    """
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    # Payment reminder is an outbound action — blocked when no active billing
    from core.billing import require_outbound_access  # noqa: PLC0415
    require_outbound_access(db, tenant_id)

    order = _lookup_order(db, tenant_id, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="order_not_found")

    status = _classify_status(order.status)
    if status not in {"pending", "failed"}:
        raise HTTPException(
            status_code=409,
            detail="order_not_eligible_for_payment_reminder",
        )

    customer_info = order.customer_info or {}
    phone = coerce_customer_info_phone(customer_info)
    if not phone:
        raise HTTPException(status_code=409, detail="customer_phone_missing")

    customer_lookup = _build_customer_lookup(db, tenant_id)
    customer_name   = _resolve_customer_display(order, customer_lookup)
    order_number    = _resolve_order_number(order)

    text = (body.message or "").strip() or _build_payment_reminder_text(
        customer_name=customer_name,
        order_number=order_number,
        payment_url=order.checkout_url,
        phone=phone,
    )

    conversation_url = f"/conversations?phone={phone}"

    # Try to send through the existing merchant-reply path. We import lazily
    # because conversations.py also imports from us at startup in some test
    # configurations.
    try:
        from core.wa_usage import has_open_service_window  # noqa: PLC0415
        from services.customer_intelligence import normalize_phone  # noqa: PLC0415
        from routers.conversations import _get_or_create_conversation  # noqa: PLC0415
        from routers.whatsapp_webhook import _send_whatsapp_message  # noqa: PLC0415
    except Exception as exc:
        logger.error("[orders.reminder] dependency import failed: %s", exc)
        return {
            "sent":             False,
            "reason":            "dependency_unavailable",
            "message":           text,
            "conversation_url":  conversation_url,
        }

    customer_phone = normalize_phone(phone) or phone

    wa_conn = (
        db.query(WhatsAppConnection)
        .filter(
            WhatsAppConnection.tenant_id       == tenant_id,
            WhatsAppConnection.status          == "connected",
            WhatsAppConnection.sending_enabled == True,  # noqa: E712
        )
        .first()
    )
    if not wa_conn or not wa_conn.phone_number_id:
        return {
            "sent":             False,
            "reason":            "whatsapp_not_connected",
            "message":           text,
            "conversation_url":  conversation_url,
        }

    if not has_open_service_window(db, tenant_id, customer_phone):
        return {
            "sent":             False,
            "reason":            "service_window_closed",
            "message":           text,
            "conversation_url":  conversation_url,
        }

    convo = _get_or_create_conversation(db, tenant_id, customer_phone, customer_name)

    try:
        await _send_whatsapp_message(
            phone_id=wa_conn.phone_number_id,
            to=customer_phone,
            text=text,
            _tenant_id=tenant_id,
            _db=db,
        )
    except Exception as exc:
        logger.exception(
            "[orders.reminder] tenant=%s order=%s send failed", tenant_id, order.id,
        )
        return {
            "sent":             False,
            "reason":            "send_failed",
            "error":             str(exc)[:200],
            "message":           text,
            "conversation_url":  conversation_url,
        }

    sent_at = datetime.now(timezone.utc).isoformat()
    db.add(MessageEvent(
        conversation_id=convo.id,
        tenant_id=tenant_id,
        direction="outbound",
        body=text,
        event_type="payment_reminder",
        extra_metadata={
            "customer_phone": customer_phone,
            "order_id":       order.id,
            "order_number":   order_number,
            "is_ai":          False,
            "via":            "orders_dashboard",
        },
    ))

    # Persist on the order so the timeline has a permanent breadcrumb.
    meta = dict(order.extra_metadata or {})
    reminders = list(meta.get("payment_reminders") or [])
    reminders.append({"sent_at": sent_at, "channel": "whatsapp"})
    meta["payment_reminders"]    = reminders
    meta["last_reminder_at"]     = sent_at
    order.extra_metadata         = meta

    convo.status               = "active"
    convo.is_human_handoff     = False
    convo.paused_by_human      = False
    db.add(convo)
    db.add(order)
    db.commit()

    return {
        "sent":             True,
        "message":          text,
        "conversation_url": conversation_url,
        "sent_at":          sent_at,
    }


# ── Merchant bank-transfer confirmation (PR-2B) ─────────────────────────────

@router.post("/{order_id}/confirm-payment")
async def confirm_order_payment(
    order_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Merchant confirms bank-transfer funds after manual verification.

    Only ``payment_submitted`` + ``bank_transfer`` + ``payment_confirmed=false``.
    """
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    order = _lookup_order(db, tenant_id, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="order_not_found")

    from core.merchant_payment_confirmation import (  # noqa: PLC0415
        apply_merchant_payment_confirmation,
        can_merchant_confirm_bank_transfer,
    )

    allowed, reason = can_merchant_confirm_bank_transfer(order)
    if not allowed:
        raise HTTPException(
            status_code=409,
            detail=reason,
        )

    verified_by = str(
        request.headers.get("X-Staff-User")
        or request.headers.get("X-Merchant-User")
        or f"tenant:{tenant_id}"
    )

    try:
        result = apply_merchant_payment_confirmation(
            order,
            verified_by=verified_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    db.add(order)
    db.commit()
    db.refresh(order)

    customer_lookup = _build_customer_lookup(db, tenant_id)
    vip_phones      = _build_vip_phone_set(db, tenant_id)
    unread_phones   = _build_unread_phone_set(db, tenant_id)

    return {
        "ok":     True,
        "result": result,
        "order":  _serialise_order(
            order,
            customer_lookup=customer_lookup,
            now=datetime.now(timezone.utc),
            detailed=True,
            vip_phones=vip_phones,
            unread_phones=unread_phones,
            db=db,
            tenant_id=tenant_id,
        ),
    }


# ── Shipments (foundation — internal only, no carrier API) ─────────────────

def _shipment_verified_by(request: Request, tenant_id: int) -> str:
    return str(
        request.headers.get("X-Staff-User")
        or request.headers.get("X-Merchant-User")
        or f"tenant:{tenant_id}"
    )


@router.post("/{order_id}/shipments")
async def create_order_shipment_endpoint(
    order_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Create an internal shipment record when payment + address gates pass."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    order = _lookup_order(db, tenant_id, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="order_not_found")

    from core.order_shipment_service import (  # noqa: PLC0415
        create_order_shipment,
        evaluate_create_shipment,
        get_order_shipment,
        resolve_tenant_cod_enabled,
        serialise_shipment,
    )

    cod_enabled = resolve_tenant_cod_enabled(db, tenant_id)
    existing = get_order_shipment(db, tenant_id, order.id)
    gate = evaluate_create_shipment(
        order,
        cod_enabled=cod_enabled,
        existing_shipment=existing,
    )
    if not gate.allowed:
        raise HTTPException(
            status_code=400,
            detail={
                "reason": gate.reason_key,
                "message_ar": gate.message_ar,
            },
        )

    try:
        shipment, shipment_payload = create_order_shipment(
            db,
            tenant_id=tenant_id,
            order=order,
            verified_by=_shipment_verified_by(request, tenant_id),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"reason": str(exc), "message_ar": gate.message_ar},
        ) from exc

    db.add(order)
    db.commit()
    db.refresh(order)
    db.refresh(shipment)

    customer_lookup = _build_customer_lookup(db, tenant_id)
    vip_phones      = _build_vip_phone_set(db, tenant_id)
    unread_phones   = _build_unread_phone_set(db, tenant_id)

    order_payload = _serialise_order(
        order,
        customer_lookup=customer_lookup,
        now=datetime.now(timezone.utc),
        detailed=True,
        vip_phones=vip_phones,
        unread_phones=unread_phones,
        db=db,
        tenant_id=tenant_id,
    )
    from core.order_shipping_policy import MSG_SHIPMENT_EXISTS  # noqa: PLC0415

    order_payload["shipping"] = {
        "can_create_shipment": False,
        "blocked_reason_key": "shipment_exists",
        "blocked_reason_ar": MSG_SHIPMENT_EXISTS,
        "can_generate_label": True,
        "shipment": shipment_payload,
    }

    return {
        "ok": True,
        "shipment": shipment_payload,
        "order": order_payload,
    }


@router.post("/{order_id}/shipments/{shipment_id}/generate-label")
async def generate_shipment_label_endpoint(
    order_id: str,
    shipment_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Generate placeholder label metadata for an existing shipment."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    order = _lookup_order(db, tenant_id, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="order_not_found")

    from models import OrderShipment  # noqa: PLC0415
    from core.order_shipment_service import (  # noqa: PLC0415
        generate_shipment_label,
        resolve_tenant_cod_enabled,
    )
    from core.order_shipping_policy import can_generate_label  # noqa: PLC0415

    shipment = (
        db.query(OrderShipment)
        .filter(
            OrderShipment.id == shipment_id,
            OrderShipment.tenant_id == tenant_id,
            OrderShipment.order_id == order.id,
        )
        .first()
    )
    if not shipment:
        raise HTTPException(status_code=404, detail="shipment_not_found")

    cod_enabled = resolve_tenant_cod_enabled(db, tenant_id)
    gate = can_generate_label(order, shipment, cod_enabled=cod_enabled)
    if not gate.allowed:
        raise HTTPException(
            status_code=400,
            detail={
                "reason": gate.reason_key,
                "message_ar": gate.message_ar,
            },
        )

    try:
        shipment_payload = generate_shipment_label(
            db,
            tenant_id=tenant_id,
            order=order,
            shipment=shipment,
            verified_by=_shipment_verified_by(request, tenant_id),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"reason": str(exc), "message_ar": gate.message_ar},
        ) from exc

    db.add(order)
    db.add(shipment)
    db.commit()
    db.refresh(order)

    customer_lookup = _build_customer_lookup(db, tenant_id)
    vip_phones      = _build_vip_phone_set(db, tenant_id)
    unread_phones   = _build_unread_phone_set(db, tenant_id)

    order_payload = _serialise_order(
        order,
        customer_lookup=customer_lookup,
        now=datetime.now(timezone.utc),
        detailed=True,
        vip_phones=vip_phones,
        unread_phones=unread_phones,
        db=db,
        tenant_id=tenant_id,
    )
    order_payload["shipping"] = {
        "can_create_shipment": False,
        "blocked_reason_key": "shipment_exists",
        "blocked_reason_ar": None,
        "can_generate_label": False,
        "shipment": shipment_payload,
    }

    return {
        "ok": True,
        "shipment": shipment_payload,
        "order": order_payload,
    }


# ── P1 — Merchant draft order editing (WhatsApp / Nahla-native) ─────────────

class OrderCustomerPatch(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    internal_note: Optional[str] = None


class OrderAddressPatch(BaseModel):
    city: Optional[str] = None
    district: Optional[str] = None
    street: Optional[str] = None
    address: Optional[str] = None
    short_address_code: Optional[str] = None
    google_maps_url: Optional[str] = None
    delivery_notes: Optional[str] = None


class OrderShippingMetaPatch(BaseModel):
    shipping_provider: Optional[str] = None
    shipping_cost: Optional[float] = None
    tracking_number: Optional[str] = None
    shipping_status: Optional[str] = None
    delivery_notes: Optional[str] = None


class OrderLineItemAdd(BaseModel):
    product_id: Optional[str] = None
    variant_id: Optional[str] = None
    quantity: int = 1
    product_name: Optional[str] = None
    unit_price: Optional[float] = None


class OrderLineItemPatch(BaseModel):
    product_id: Optional[str] = None
    variant_id: Optional[str] = None
    quantity: Optional[int] = None
    product_name: Optional[str] = None
    unit_price: Optional[float] = None


class OrderCancelIn(BaseModel):
    reason: Optional[str] = None


def _merchant_actor(request: Request, tenant_id: int) -> str:
    return str(
        request.headers.get("X-Staff-User")
        or request.headers.get("X-Merchant-User")
        or f"tenant:{tenant_id}"
    )


def _detail_order_payload(
    db: Session,
    tenant_id: int,
    order: Order,
) -> Dict[str, Any]:
    customer_lookup = _build_customer_lookup(db, tenant_id)
    vip_phones      = _build_vip_phone_set(db, tenant_id)
    unread_phones   = _build_unread_phone_set(db, tenant_id)

    from core.order_shipment_service import (  # noqa: PLC0415
        evaluate_create_shipment,
        get_order_shipment,
        resolve_tenant_cod_enabled,
        serialise_shipment,
    )
    from core.order_shipping_policy import can_generate_label  # noqa: PLC0415

    cod_enabled = resolve_tenant_cod_enabled(db, tenant_id)
    shipment_row = get_order_shipment(db, tenant_id, order.id)
    create_gate = evaluate_create_shipment(
        order,
        cod_enabled=cod_enabled,
        existing_shipment=shipment_row,
    )
    label_gate = (
        can_generate_label(order, shipment_row, cod_enabled=cod_enabled)
        if shipment_row else None
    )

    payload = _serialise_order(
        order,
        customer_lookup=customer_lookup,
        now=datetime.now(timezone.utc),
        detailed=True,
        vip_phones=vip_phones,
        unread_phones=unread_phones,
        db=db,
        tenant_id=tenant_id,
    )
    payload["shipping"] = {
        "can_create_shipment": create_gate.allowed,
        "blocked_reason_key": create_gate.reason_key,
        "blocked_reason_ar": create_gate.message_ar,
        "can_generate_label": bool(label_gate and label_gate.allowed),
        "label_blocked_reason_key": label_gate.reason_key if label_gate else None,
        "label_blocked_reason_ar": label_gate.message_ar if label_gate else None,
        "shipment": serialise_shipment(shipment_row) if shipment_row else None,
    }
    return payload


def _handle_order_edit_error(exc: Exception) -> HTTPException:
    raw = str(exc)
    code = raw.split(":", 1)[-1].strip() if raw.startswith("catalog_evidence_incomplete:") else raw.strip()
    messages = {
        "catalog_variant_required": "اختر الحجم أولًا",
        "catalog_variant_not_found": "الحجم المختار غير موجود في الكتالوج",
        "catalog_product_not_found": "المنتج غير موجود في الكتالوج",
        "product_id_required": "اختر منتجًا من الكتالوج",
        "confirmed_item_requires_price": "السعر غير متوفر في الكتالوج",
        "confirmed_item_requires_variant": "اختر الحجم أولًا",
        "needs_variant": "اختر الحجم أولًا",
        "needs_review": "تعذّر إضافة المنتج — بيانات الكتالوج غير مكتملة",
    }
    if raw.startswith("catalog_evidence_incomplete:"):
        detail = messages.get(code, messages["needs_review"])
    else:
        detail = messages.get(code, raw)
    return HTTPException(status_code=409, detail=detail)


def _commit_edited_order(
    db: Session,
    tenant_id: int,
    order: Order,
) -> Dict[str, Any]:
    db.add(order)
    db.commit()
    db.refresh(order)
    return {"ok": True, "order": _detail_order_payload(db, tenant_id, order)}


@router.patch("/{order_id}/customer")
async def patch_order_customer(
    order_id: str,
    body: OrderCustomerPatch,
    request: Request,
    db: Session = Depends(get_db),
):
    from core.wa_order_editor import OrderEditError, update_order_customer  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    order = _lookup_order(db, tenant_id, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="order_not_found")

    actor = _merchant_actor(request, tenant_id)
    try:
        update_order_customer(
            order,
            first_name=body.first_name,
            last_name=body.last_name,
            phone=body.phone,
            internal_note=body.internal_note,
            actor=actor,
        )
        from core.order_customer_display import sync_order_customer_identity  # noqa: PLC0415

        sync_order_customer_identity(db, tenant_id, order)
    except OrderEditError as exc:
        raise _handle_order_edit_error(exc) from exc

    return _commit_edited_order(db, tenant_id, order)


@router.patch("/{order_id}/address")
async def patch_order_address(
    order_id: str,
    body: OrderAddressPatch,
    request: Request,
    db: Session = Depends(get_db),
):
    from core.wa_order_editor import OrderEditError, update_order_address  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    order = _lookup_order(db, tenant_id, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="order_not_found")

    actor = _merchant_actor(request, tenant_id)
    try:
        update_order_address(
            order,
            city=body.city,
            district=body.district,
            street=body.street,
            address=body.address,
            short_address_code=body.short_address_code,
            google_maps_url=body.google_maps_url,
            delivery_notes=body.delivery_notes,
            actor=actor,
            db=db,
            tenant_id=tenant_id,
        )
    except OrderEditError as exc:
        raise _handle_order_edit_error(exc) from exc

    return _commit_edited_order(db, tenant_id, order)


@router.patch("/{order_id}/shipping-meta")
async def patch_order_shipping_meta(
    order_id: str,
    body: OrderShippingMetaPatch,
    request: Request,
    db: Session = Depends(get_db),
):
    from core.wa_order_editor import OrderEditError, update_order_shipping_meta  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    order = _lookup_order(db, tenant_id, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="order_not_found")

    actor = _merchant_actor(request, tenant_id)
    try:
        update_order_shipping_meta(
            order,
            shipping_provider=body.shipping_provider,
            shipping_cost=body.shipping_cost,
            tracking_number=body.tracking_number,
            shipping_status=body.shipping_status,
            delivery_notes=body.delivery_notes,
            actor=actor,
        )
    except OrderEditError as exc:
        raise _handle_order_edit_error(exc) from exc

    return _commit_edited_order(db, tenant_id, order)


@router.post("/{order_id}/line-items")
async def add_order_line_item_endpoint(
    order_id: str,
    body: OrderLineItemAdd,
    request: Request,
    db: Session = Depends(get_db),
):
    from core.wa_order_editor import OrderEditError, add_order_line_item  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    order = _lookup_order(db, tenant_id, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="order_not_found")

    actor = _merchant_actor(request, tenant_id)
    try:
        add_order_line_item(
            order,
            body.model_dump(exclude_none=True),
            db=db,
            tenant_id=tenant_id,
            actor=actor,
        )
    except OrderEditError as exc:
        raise _handle_order_edit_error(exc) from exc

    return _commit_edited_order(db, tenant_id, order)


@router.patch("/{order_id}/line-items/{item_index}")
async def patch_order_line_item_endpoint(
    order_id: str,
    item_index: int,
    body: OrderLineItemPatch,
    request: Request,
    db: Session = Depends(get_db),
):
    from core.wa_order_editor import OrderEditError, update_order_line_item  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    order = _lookup_order(db, tenant_id, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="order_not_found")

    actor = _merchant_actor(request, tenant_id)
    try:
        update_order_line_item(
            order,
            item_index,
            body.model_dump(exclude_none=True),
            db=db,
            tenant_id=tenant_id,
            actor=actor,
        )
    except OrderEditError as exc:
        raise _handle_order_edit_error(exc) from exc

    return _commit_edited_order(db, tenant_id, order)


@router.delete("/{order_id}/line-items/{item_index}")
async def delete_order_line_item_endpoint(
    order_id: str,
    item_index: int,
    request: Request,
    db: Session = Depends(get_db),
):
    from core.wa_order_editor import OrderEditError, delete_order_line_item  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    order = _lookup_order(db, tenant_id, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="order_not_found")

    actor = _merchant_actor(request, tenant_id)
    try:
        delete_order_line_item(order, item_index, actor=actor)
    except OrderEditError as exc:
        raise _handle_order_edit_error(exc) from exc

    return _commit_edited_order(db, tenant_id, order)


@router.post("/{order_id}/confirm-ready")
async def confirm_order_ready_endpoint(
    order_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    from core.wa_order_editor import OrderEditError, confirm_order_ready  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    order = _lookup_order(db, tenant_id, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="order_not_found")

    actor = _merchant_actor(request, tenant_id)
    try:
        confirm_order_ready(order, actor=actor, db=db, tenant_id=tenant_id)
    except OrderEditError as exc:
        raise _handle_order_edit_error(exc) from exc

    return _commit_edited_order(db, tenant_id, order)


@router.post("/{order_id}/cancel")
async def cancel_order_endpoint(
    order_id: str,
    body: OrderCancelIn,
    request: Request,
    db: Session = Depends(get_db),
):
    from core.wa_order_editor import OrderEditError, cancel_order  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    order = _lookup_order(db, tenant_id, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="order_not_found")

    actor = _merchant_actor(request, tenant_id)
    try:
        cancel_order(order, actor=actor, reason=body.reason or "")
    except OrderEditError as exc:
        raise _handle_order_edit_error(exc) from exc

    return _commit_edited_order(db, tenant_id, order)


@router.delete("/{order_id}")
async def delete_draft_order_endpoint(
    order_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    from core.wa_order_editor import (  # noqa: PLC0415
        OrderEditError,
        delete_draft_order,
        log_draft_delete_audit,
    )

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    order = _lookup_order(db, tenant_id, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="order_not_found")

    actor = _merchant_actor(request, tenant_id)
    try:
        delete_draft_order(order, actor=actor)
    except OrderEditError as exc:
        raise _handle_order_edit_error(exc) from exc

    log_draft_delete_audit(order, actor=actor)
    db.add(order)
    db.flush()
    db.delete(order)
    db.commit()

    return {"ok": True, "deleted": True, "order_id": order_id}
