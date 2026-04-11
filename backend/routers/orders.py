"""
routers/orders.py
─────────────────
Tenant-scoped order list endpoints for the merchant dashboard.

Backed by the real `Order` table.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id
from models import Order

router = APIRouter(prefix="/orders", tags=["Orders"])


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

    now = datetime.now(timezone.utc)
    orders: List[Dict[str, Any]] = []
    pending_count = 0
    completed_today = 0
    today_revenue = 0.0

    for order in rows:
        created_at = getattr(order, "created_at", None) or now
        if getattr(created_at, "tzinfo", None) is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        status_raw = str(order.status or "").lower()
        if status_raw in {"paid", "confirmed", "completed"}:
            status = "paid"
        elif status_raw in {"pending", "pending_payment", "pending_confirmation"}:
            status = "pending"
            pending_count += 1
        elif status_raw in {"failed"}:
            status = "failed"
        else:
            status = "cancelled"

        amount_value = _to_float_sar(order.total)
        if created_at.date() == now.date():
            today_revenue += amount_value
            if status == "paid":
                completed_today += 1

        customer_info = order.customer_info or {}
        line_items = order.line_items or []
        item_titles = []
        for item in line_items:
            name = item.get("product_name") or item.get("title") or "منتج"
            qty = int(item.get("quantity") or 1)
            item_titles.append(f"{name} ×{qty}")

        source = "AI" if (order.extra_metadata or {}).get("source") == "ai_sales_agent" else "manual"

        orders.append({
            "id": str(order.id),
            "customer": customer_info.get("name") or "—",
            "phone": customer_info.get("phone") or "—",
            "items": "، ".join(item_titles) if item_titles else "—",
            "amount": order.total or "0 ر.س",
            "amount_sar": round(amount_value, 2),
            "status": status,
            "source": source,
            "paymentLink": order.checkout_url,
            "createdAt": created_at.isoformat(),
        })

    return {
        "summary": {
            "total_orders": len(orders),
            "today_revenue_sar": round(today_revenue, 2),
            "pending_orders": pending_count,
            "completed_today": completed_today,
        },
        "orders": orders,
    }
