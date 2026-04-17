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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.database import get_db
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
    "pending_confirmation", "awaiting_confirmation",
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
    """
    display_name = order.customer_name → customer_info.name →
                   Customer table lookup by phone → phone → "—"
    Never returns blank so the merchant always has something to act on.
    Salla's per-order payload often omits the customer name even though
    the customer record itself has one — we cross-reference by phone.
    """
    direct = (getattr(order, "customer_name", None) or "").strip()
    if direct and not _looks_like_phone(direct):
        return direct
    info = order.customer_info or {}
    name = (info.get("name") or "").strip()
    if name and not _looks_like_phone(name):
        return name
    phone = (info.get("phone") or info.get("mobile") or "").strip()
    if customer_lookup and phone:
        looked_up = customer_lookup.get(phone)
        if looked_up:
            return looked_up
    return direct or name or phone or "—"


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


def _read_created_at(order: Order, fallback: datetime) -> datetime:
    """
    The Order model has no `created_at` column — the canonical timestamp
    lives in `extra_metadata['created_at']` (set during sync from the
    upstream `created_at` field). Fall through every plausible source so
    the dashboard never claims an old order is "today".
    """
    candidates: List[Any] = []
    meta = getattr(order, "extra_metadata", None) or {}
    candidates.extend([
        meta.get("created_at"),
        meta.get("updated_at"),
        getattr(order, "created_at", None),
        getattr(order, "updated_at", None),
    ])
    for cand in candidates:
        if isinstance(cand, datetime):
            return cand if cand.tzinfo else cand.replace(tzinfo=timezone.utc)
        if not cand:
            continue
        text = str(cand).strip()
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
    return fallback


def _build_customer_lookup(db: Session, tenant_id: int) -> Dict[str, str]:
    """phone → name map used to fill in missing names on order rows."""
    out: Dict[str, str] = {}
    for cust in (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant_id, Customer.name.isnot(None))
        .all()
    ):
        if not cust.name:
            continue
        if cust.phone:
            out[cust.phone.strip()] = cust.name.strip()
            digits = cust.phone.strip().lstrip("+").replace(" ", "").replace("-", "")
            if digits:
                out[digits] = cust.name.strip()
    return out


def _normalise_phone_key(phone: Optional[str]) -> str:
    """Digits-only phone, with leading + stripped — suitable for DB lookups."""
    return (phone or "").strip().lstrip("+").replace(" ", "").replace("-", "")


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
        meta_phone = (convo.extra_metadata or {}).get("customer_phone") or (convo.extra_metadata or {}).get("phone")
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

    if status == "pending":
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
    if source_key == "whatsapp" and is_ai_created and not has_open_conv:
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
    name = (customer_name or "عميلنا الكريم").strip() or "عميلنا الكريم"
    if name == "—":
        name = "عميلنا الكريم"

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


def _serialise_order(
    order: Order,
    *,
    customer_lookup: Dict[str, str],
    now: datetime,
    detailed: bool = False,
    vip_phones: Optional[set[str]] = None,
    unread_phones: Optional[set[str]] = None,
) -> Dict[str, Any]:
    """
    Render an order for the dashboard. ``detailed=True`` adds line-item
    breakdowns and the deep-link to the upstream store; the list endpoint
    keeps it lean for performance.
    """
    created_at  = _read_created_at(order, fallback=now)
    raw_status  = str(order.status or "")
    status      = _classify_status(raw_status)
    amount_value = _to_float_sar(order.total)

    customer_info = order.customer_info or {}
    line_items    = order.line_items or []

    item_titles: List[str] = []
    detailed_items: List[Dict[str, Any]] = []
    for item in line_items:
        name = item.get("product_name") or item.get("title") or item.get("name") or "منتج"
        qty  = int(item.get("quantity") or 1)
        item_titles.append(f"{name} ×{qty}")
        if detailed:
            unit_price = item.get("unit_price") or item.get("price")
            try:
                unit_price_f = float(unit_price) if unit_price is not None else None
            except Exception:
                unit_price_f = None
            detailed_items.append({
                "product_id":   str(item.get("product_id") or ""),
                "name":         name,
                "quantity":     qty,
                "variant_id":   str(item.get("variant_id") or "") or None,
                "unit_price":   unit_price_f,
                "image_url":    item.get("image_url") or item.get("image") or None,
            })

    source_key   = _resolve_source(order)
    source_label = SOURCE_LABELS_AR.get(source_key, source_key)
    order_number = _resolve_order_number(order)
    display_name = _resolve_customer_display(order, customer_lookup)
    phone        = (customer_info.get("phone") or customer_info.get("mobile") or "").strip()
    is_ai_created = source_key == "whatsapp" or (
        (order.extra_metadata or {}).get("source") in ("ai_sales_agent", "ai_sales", "ai")
    )

    is_vip_customer = bool(vip_phones    and phone and _is_vip(vip_phones, phone))
    has_open_conv   = bool(unread_phones and phone and _has_open_conversation(unread_phones, phone))

    needs_action = _compute_needs_action(
        status=status,
        source_key=source_key,
        payment_link=order.checkout_url,
        is_vip_customer=is_vip_customer,
        has_open_conv=has_open_conv,
        is_ai_created=is_ai_created,
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
        "items":         "، ".join(item_titles) if item_titles else "—",
        "amount":        _format_total(amount_value, order.total),
        "amount_sar":    round(amount_value, 2),
        "status":        status,
        "status_label":  STATUS_LABELS_AR.get(status, status),
        "raw_status":    _parse_corrupt_status(raw_status),
        "source":        source_key,
        "source_label":  source_label,
        "paymentLink":   order.checkout_url,
        "createdAt":     created_at.isoformat(),
        "is_ai_created": is_ai_created,
        "is_vip":        is_vip_customer,
        "has_open_conversation": has_open_conv,
        "needs_action":  needs_action,
    }

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
        store_url = _build_store_url(source_key, order.external_id, order.external_order_number)
        whatsapp_url = f"https://wa.me/{phone.lstrip('+').replace(' ', '').replace('-', '')}" if phone else None
        conversation_url = f"/conversations?phone={phone}" if phone else None
        payload["links"] = {
            "store":        store_url,
            "store_label":  f"فتح الطلب في {source_label}" if store_url else None,
            "whatsapp":     whatsapp_url,
            "conversation": conversation_url,
        }
        payload["payment_method"] = (order.extra_metadata or {}).get("payment_method")
        payload["notes"]          = (order.extra_metadata or {}).get("notes")
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

    return payload


@router.get("")
async def list_orders(request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    rows = (
        db.query(Order)
        .filter(Order.tenant_id == tenant_id)
        .order_by(Order.id.desc())
        .limit(200)
        .all()
    )

    customer_lookup = _build_customer_lookup(db, tenant_id)
    vip_phones      = _build_vip_phone_set(db, tenant_id)
    unread_phones   = _build_unread_phone_set(db, tenant_id)
    now             = datetime.now(timezone.utc)
    today           = now.date()

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
    return {
        "order": _serialise_order(
            order,
            customer_lookup=customer_lookup,
            now=datetime.now(timezone.utc),
            detailed=True,
            vip_phones=vip_phones,
            unread_phones=unread_phones,
        ),
    }


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
    phone = (customer_info.get("phone") or customer_info.get("mobile") or "").strip()
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
