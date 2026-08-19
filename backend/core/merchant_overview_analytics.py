"""
Canonical merchant Overview analytics.

Ownership: every KPI card and the revenue chart on /overview must come
from this module for one selected period. Store-sync status is a transport
surface only — it must not invent a second time window or a second total.

Period contract (merchant timezone, default Asia/Riyadh):
  today        — local calendar day 00:00 → now
  last_7_days  — local today + the previous 6 calendar days → now
  this_month   — local month start 00:00 → now

Comparisons are stored/queried as UTC (naive UTC for MessageEvent /
ConversationLog). Bounds are derived from the merchant timezone, never
the server's local zone.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from core.wa_usage import (
    count_conversations_in_window,
    get_merchant_timezone,
)

ALLOWED_PERIODS = ("today", "last_7_days", "this_month")

PERIOD_LABEL_AR = {
    "today": "اليوم",
    "last_7_days": "آخر 7 أيام",
    "this_month": "هذا الشهر",
}
PERIOD_LABEL_EN = {
    "today": "Today",
    "last_7_days": "Last 7 days",
    "this_month": "This month",
}

# Countable order statuses (merchant "orders" KPI). Drafts and abandoned
# carts are not orders the merchant would recognise in the list as placed.
EXCLUDED_ORDER_STATUSES = frozenset({
    "draft",
    "pending_customer_info",
    "abandoned",
})

# Revenue = gross paid/fulfilled totals. Cancelled/refunded/pending do not
# enter the card or the chart.
REVENUE_STATUSES = frozenset({
    "paid",
    "completed",
    "complete",
    "confirmed",
    "delivered",
    "delivering",
    "shipped",
    "out_for_delivery",
    "fulfilled",
    "processing",
})

WA_SOURCES = frozenset({"whatsapp", "ai_sales_agent", "ai_sales", "ai"})

_INBOUND_DIRECTIONS = frozenset({"inbound", "in", "customer", "user"})
_OUTBOUND_DIRECTIONS = frozenset({"outbound", "out", "ai", "assistant"})
_EXCLUDED_INBOUND_TYPES = frozenset({
    "coexistence_history",
    "smb_message_echo",
    "campaign",
    "status",
})


def _as_aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _as_naive_utc(dt: datetime) -> datetime:
    return _as_aware_utc(dt).replace(tzinfo=None)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_period(period: str) -> str:
    value = (period or "today").strip().lower()
    if value not in ALLOWED_PERIODS:
        return "today"
    return value


def resolve_period_bounds(
    db: Session,
    tenant_id: int,
    period: str,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return canonical [start, end] bounds in UTC for the Overview period."""
    period = normalize_period(period)
    now_utc = _as_aware_utc(now or _utcnow())
    tz = get_merchant_timezone(db, tenant_id)
    tz_name = getattr(tz, "key", None) or "Asia/Riyadh"
    local = now_utc.astimezone(tz)
    local_today_start = local.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == "last_7_days":
        start_local = local_today_start - timedelta(days=6)
    elif period == "this_month":
        start_local = local_today_start.replace(day=1)
    else:
        start_local = local_today_start

    start_utc = start_local.astimezone(timezone.utc)
    end_utc = now_utc
    return {
        "period": period,
        "period_label_ar": PERIOD_LABEL_AR[period],
        "period_label_en": PERIOD_LABEL_EN[period],
        "timezone": tz_name,
        "start_utc": start_utc,
        "end_utc": end_utc,
        "start_utc_naive": _as_naive_utc(start_utc),
        "end_utc_naive": _as_naive_utc(end_utc),
    }


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_aware_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    for variant in (text, text.replace(" ", "T", 1), text.split(".", 1)[0].replace(" ", "T", 1)):
        try:
            parsed = datetime.fromisoformat(variant)
            return _as_aware_utc(parsed)
        except Exception:
            continue
    return None


def order_created_at(order: Any) -> Optional[datetime]:
    """Canonical order time. Missing timestamps are excluded, never dated as now."""
    meta = getattr(order, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    catalog_meta = meta.get("catalog_order") if isinstance(meta.get("catalog_order"), dict) else {}
    candidates = [
        meta.get("created_at"),
        meta.get("draft_created_at"),
        meta.get("display_created_at"),
        meta.get("source_message_created_at"),
        meta.get("first_customer_message_at"),
        catalog_meta.get("source_message_at") if isinstance(catalog_meta, dict) else None,
        getattr(order, "created_at", None),
    ]
    for cand in candidates:
        parsed = _parse_timestamp(cand)
        if parsed is not None:
            return parsed
    return None


def _order_amount(order: Any) -> float:
    total = getattr(order, "total", None) or ""
    try:
        return float(str(total).replace(",", "").replace("ر.س", "").replace("SAR", "").split()[0])
    except Exception:
        return 0.0


def _order_status(order: Any) -> str:
    return str(getattr(order, "status", None) or "").strip().lower()


def _order_source(order: Any) -> str:
    raw = (getattr(order, "source", None) or "").strip().lower()
    if raw in WA_SOURCES:
        return "whatsapp"
    meta = getattr(order, "extra_metadata", None) or {}
    meta_src = str((meta or {}).get("source") or "").strip().lower()
    if meta_src in WA_SOURCES:
        return "whatsapp"
    return raw or "salla"


def _in_window(ts: Optional[datetime], start_utc: datetime, end_utc: datetime) -> bool:
    if ts is None:
        return False
    aware = _as_aware_utc(ts)
    return start_utc <= aware <= end_utc


def _is_countable_order(order: Any) -> bool:
    if bool(getattr(order, "is_abandoned", False)):
        return False
    status = _order_status(order)
    if status in EXCLUDED_ORDER_STATUSES:
        return False
    return True


def _is_ai_outbound(event: Any) -> bool:
    meta = getattr(event, "extra_metadata", None) or {}
    if isinstance(meta, dict) and meta.get("is_ai") is True:
        return True
    event_type = str(getattr(event, "event_type", None) or "").lower()
    return event_type in {"ai_fallback", "ai_handoff_ack"}


def _local_day_key(ts: datetime, tz) -> str:
    return _as_aware_utc(ts).astimezone(tz).date().isoformat()


def _chart_buckets(bounds: Dict[str, Any], tz) -> List[Tuple[str, str, str]]:
    """One bucket per local calendar day in [start, end]."""
    start_local = bounds["start_utc"].astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = bounds["end_utc"].astimezone(tz)
    days: List[Tuple[str, str, str]] = []
    cursor = start_local
    weekday_ar = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
    weekday_en = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    while cursor.date() <= end_local.date():
        key = cursor.date().isoformat()
        days.append((key, weekday_ar[cursor.weekday()], weekday_en[cursor.weekday()]))
        cursor = cursor + timedelta(days=1)
        if len(days) > 40:
            break
    return days


def compute_overview_kpis(
    db: Session,
    tenant_id: int,
    period: str = "today",
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    from models import (  # noqa: PLC0415
        CampaignSendLog,
        ConversationTrace,
        Customer,
        MessageEvent,
        Order,
    )
    from sqlalchemy import func  # noqa: PLC0415

    bounds = resolve_period_bounds(db, tenant_id, period, now=now)
    period = bounds["period"]
    start_utc = bounds["start_utc"]
    end_utc = bounds["end_utc"]
    start_naive = bounds["start_utc_naive"]
    end_naive = bounds["end_utc_naive"]
    tz = get_merchant_timezone(db, tenant_id)

    orders = (
        db.query(Order)
        .filter(Order.tenant_id == tenant_id)
        .all()
    )

    chart_spec = _chart_buckets(bounds, tz)
    revenue_by_day = {key: 0.0 for key, _ar, _en in chart_spec}

    revenue_period = 0.0
    orders_period = 0
    ai_revenue = 0.0
    ai_orders = 0
    recent_orders_out: List[Dict[str, Any]] = []
    window_orders: List[Any] = []

    for order in orders:
        if not _is_countable_order(order):
            continue
        created = order_created_at(order)
        if not _in_window(created, start_utc, end_utc):
            continue
        window_orders.append(order)
        orders_period += 1
        amt = _order_amount(order)
        status = _order_status(order)
        src = _order_source(order)
        if status in REVENUE_STATUSES:
            revenue_period += amt
            revenue_by_day[_local_day_key(created, tz)] = (
                revenue_by_day.get(_local_day_key(created, tz), 0.0) + amt
            )
            if src == "whatsapp":
                ai_revenue += amt
                ai_orders += 1

    window_orders.sort(key=lambda o: order_created_at(o) or start_utc, reverse=True)
    for order in window_orders[:5]:
        amt = _order_amount(order)
        customer_info = getattr(order, "customer_info", None) or {}
        customer_name = (
            getattr(order, "customer_name", None)
            or customer_info.get("name")
            or customer_info.get("phone")
            or "—"
        )
        order_num = (
            getattr(order, "external_order_number", None)
            or getattr(order, "external_id", None)
            or str(order.id)
        )
        recent_orders_out.append({
            "id": f"#{order_num}",
            "customer": customer_name,
            "amount": f"{amt:,.0f} ر.س",
            "status": "paid" if _order_status(order) in REVENUE_STATUSES else "pending",
            "source": "AI" if _order_source(order) == "whatsapp" else "salla",
        })

    revenue_chart = [
        {"day": ar_label, "day_en": en_label, "day_key": key, "revenue": round(revenue_by_day.get(key, 0.0), 2)}
        for key, ar_label, en_label in chart_spec
    ]
    chart_revenue_sum = round(sum(row["revenue"] for row in revenue_chart), 2)

    conversations_period = count_conversations_in_window(
        db, tenant_id, start_naive, end_naive + timedelta(microseconds=1),
    )

    messages_sent_period = 0
    try:
        messages_sent_period = (
            db.query(func.count(CampaignSendLog.id))
            .filter(
                CampaignSendLog.tenant_id == tenant_id,
                CampaignSendLog.status == "sent",
                CampaignSendLog.sent_at != None,  # noqa: E711
                CampaignSendLog.sent_at >= start_naive,
                CampaignSendLog.sent_at <= end_naive,
            )
            .scalar()
        ) or 0
    except Exception:
        messages_sent_period = 0

    new_customers = 0
    customers = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant_id)
        .all()
    )
    for cust in customers:
        first_seen = _parse_timestamp(getattr(cust, "first_seen_at", None))
        if _in_window(first_seen, start_utc, end_utc):
            new_customers += 1

    inbound_eligible = 0
    ai_outbound = 0
    events = (
        db.query(MessageEvent)
        .filter(
            MessageEvent.tenant_id == tenant_id,
            MessageEvent.created_at >= start_naive,
            MessageEvent.created_at <= end_naive,
        )
        .all()
    )
    for ev in events:
        direction = str(getattr(ev, "direction", None) or "").strip().lower()
        event_type = str(getattr(ev, "event_type", None) or "").strip().lower()
        if direction in _INBOUND_DIRECTIONS:
            if event_type in _EXCLUDED_INBOUND_TYPES:
                continue
            inbound_eligible += 1
        elif direction in _OUTBOUND_DIRECTIONS and _is_ai_outbound(ev):
            ai_outbound += 1

    if inbound_eligible == 0:
        ai_rate: Optional[float] = None
    else:
        ai_rate = round(min(ai_outbound, inbound_eligible) / inbound_eligible * 100.0, 1)

    recent_conversations_out: List[Dict[str, Any]] = []
    try:
        traces = (
            db.query(ConversationTrace)
            .filter(
                ConversationTrace.tenant_id == tenant_id,
                ConversationTrace.created_at >= start_naive,
                ConversationTrace.created_at <= end_naive,
            )
            .order_by(ConversationTrace.created_at.desc())
            .all()
        )
        seen_phones: set = set()
        for tr in traces:
            phone = tr.customer_phone or ""
            if not phone or phone in seen_phones:
                continue
            seen_phones.add(phone)
            recent_conversations_out.append({
                "id": str(tr.session_id or tr.id),
                "customer": phone,
                "phone": phone,
                "lastMsg": tr.message or "",
                "time": tr.created_at.isoformat() if tr.created_at else "",
                "isAI": True,
                "status": "active",
            })
            if len(recent_conversations_out) >= 5:
                break
    except Exception:
        recent_conversations_out = []

    return {
        "period": period,
        "period_label_ar": bounds["period_label_ar"],
        "period_label_en": bounds["period_label_en"],
        "analytics_timezone": bounds["timezone"],
        "window_start_utc": start_utc.isoformat(),
        "window_end_utc": end_utc.isoformat(),
        "revenue_today": round(revenue_period, 2),
        "orders_today": orders_period,
        "conversations_today": conversations_period,
        "revenue": round(revenue_period, 2),
        "orders": orders_period,
        "conversations": conversations_period,
        "new_customers": new_customers,
        "today_billable_conversations_count": conversations_period if period == "today" else None,
        "today_messages_count": inbound_eligible if period == "today" else inbound_eligible,
        "metric_kind_conversations": "billable_conversation_windows",
        "metric_kind_messages": "eligible_inbound_customer_messages",
        "metric_kind_orders": "created_at_in_window",
        "metric_kind_new_customers": "first_seen_at_in_window",
        "metric_kind_ai_rate": "ai_outbound_over_eligible_inbound",
        "messages_sent": int(messages_sent_period),
        "ai_rate": ai_rate,
        "ai_rate_numerator": ai_outbound,
        "ai_rate_denominator": inbound_eligible,
        "ai_revenue": round(ai_revenue, 2),
        "ai_orders": ai_orders,
        "recent_orders": recent_orders_out,
        "recent_conversations": recent_conversations_out,
        "revenue_chart": revenue_chart,
        "chart_revenue_sum": chart_revenue_sum,
        "card_chart_reconciled": chart_revenue_sum == round(revenue_period, 2),
    }
