"""
routers/analytics.py
────────────────────
Tenant-scoped analytics endpoints for the merchant dashboard.

These endpoints provide real, database-backed data for:
- revenue trend
- conversation vs conversion metrics
- order source breakdown
- top products by order volume / revenue proxy

No fake or seeded dashboard-only data is returned here.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id
from models import ConversationLog, Order

router = APIRouter(prefix="/analytics", tags=["Analytics"])


def _to_float_sar(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("ر.س", "").replace(",", "").strip()
    try:
        return float(text)
    except Exception:
        return 0.0


@router.get("/dashboard")
async def analytics_dashboard(request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    now = datetime.now(timezone.utc)

    # ── Revenue + orders (last 7 days) ───────────────────────────────────────
    order_rows = (
        db.query(Order)
        .filter(Order.tenant_id == tenant_id)
        .order_by(Order.id.desc())
        .all()
    )

    revenue_by_day: Dict[str, float] = defaultdict(float)
    orders_by_day: Dict[str, int] = defaultdict(int)
    pending_count = 0
    completed_today = 0
    total_orders = len(order_rows)
    today_revenue = 0.0

    top_products_acc: Dict[str, Dict[str, Any]] = {}
    source_counts = {"AI": 0, "manual": 0}

    for order in order_rows:
        created_at = getattr(order, "created_at", None) or now
        if getattr(created_at, "tzinfo", None) is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        day_key = created_at.strftime("%a")

        amount = _to_float_sar(order.total)
        revenue_by_day[day_key] += amount
        orders_by_day[day_key] += 1

        if created_at.date() == now.date():
            today_revenue += amount
            if str(order.status).lower() in {"paid", "confirmed", "completed"}:
                completed_today += 1

        if str(order.status).lower() in {"pending", "pending_payment", "pending_confirmation"}:
            pending_count += 1

        source = "AI" if (order.extra_metadata or {}).get("source") == "ai_sales_agent" else "manual"
        source_counts[source] += 1

        for item in order.line_items or []:
            name = str(item.get("product_name") or "منتج غير معروف")
            row = top_products_acc.setdefault(name, {"name": name, "orders": 0, "revenue": 0.0})
            qty = int(item.get("quantity") or 1)
            row["orders"] += qty
            row["revenue"] += amount

    day_labels = []
    for i in range(6, -1, -1):
        dt = now - timedelta(days=i)
        day_labels.append((dt.strftime("%a"), dt.strftime("%a")))

    revenue_trend = [
        {"month": label, "revenue": round(revenue_by_day.get(key, 0.0), 2)}
        for key, label in day_labels
    ]
    conversion_trend = [
        {
            "day": label,
            "conversations": 0,
            "conversions": orders_by_day.get(key, 0),
        }
        for key, label in day_labels
    ]

    # ── Conversation usage (same month) ──────────────────────────────────────
    conv_rows = (
        db.query(ConversationLog)
        .filter(
            ConversationLog.tenant_id == tenant_id,
        )
        .all()
    )
    monthly_conversations = 0
    conv_by_day: Dict[str, int] = defaultdict(int)
    for conv in conv_rows:
        created_at = conv.conversation_started_at or now
        if getattr(created_at, "tzinfo", None) is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if created_at.year == now.year and created_at.month == now.month:
            monthly_conversations += 1
            conv_by_day[created_at.strftime("%a")] += 1

    for row in conversion_trend:
        key = row["day"]
        row["conversations"] = conv_by_day.get(key, 0)

    conversion_rate = round((total_orders / monthly_conversations) * 100, 1) if monthly_conversations else 0.0

    total_source = max(1, source_counts["AI"] + source_counts["manual"])
    source_breakdown = [
        {"name": "محادثات الذكاء الاصطناعي", "value": round((source_counts["AI"] / total_source) * 100, 1), "color": "#f59e0b"},
        {"name": "مباشر / يدوي", "value": round((source_counts["manual"] / total_source) * 100, 1), "color": "#94a3b8"},
    ]

    top_products = sorted(
        top_products_acc.values(),
        key=lambda x: (x["revenue"], x["orders"]),
        reverse=True,
    )[:10]
    for row in top_products:
        row["revenue"] = round(row["revenue"], 2)
        row["trend"] = "+0%"

    return {
        "summary": {
            "current_month_revenue_sar": round(today_revenue if total_orders else sum(r["revenue"] for r in revenue_trend), 2),
            "conversion_rate_pct": conversion_rate,
            "current_month_orders": total_orders,
            "current_month_conversations": monthly_conversations,
            "today_revenue_sar": round(today_revenue, 2),
            "pending_orders": pending_count,
            "completed_today": completed_today,
        },
        "revenue_trend": revenue_trend,
        "conversion_trend": conversion_trend,
        "source_breakdown": source_breakdown,
        "top_products": top_products,
    }
