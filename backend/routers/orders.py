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

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id
from models import Order

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
        created_at = _read_created_at(order, fallback=now)

        raw_status = str(order.status or "")
        status = _classify_status(raw_status)
        if status == "pending":
            pending_count += 1

        amount_value = _to_float_sar(order.total)
        if created_at.date() == now.date():
            today_revenue += amount_value
            if status == "paid":
                completed_today += 1

        customer_info = order.customer_info or {}
        line_items = order.line_items or []
        item_titles = []
        for item in line_items:
            name = item.get("product_name") or item.get("title") or item.get("name") or "منتج"
            qty = int(item.get("quantity") or 1)
            item_titles.append(f"{name} ×{qty}")

        source = "AI" if (order.extra_metadata or {}).get("source") == "ai_sales_agent" else "manual"

        orders.append({
            "id": str(order.id),
            "external_id": order.external_id,
            "customer": customer_info.get("name") or "—",
            "phone": customer_info.get("phone") or customer_info.get("mobile") or "—",
            "items": "، ".join(item_titles) if item_titles else "—",
            "amount": _format_total(amount_value, order.total),
            "amount_sar": round(amount_value, 2),
            "status": status,
            "status_label": STATUS_LABELS_AR.get(status, status),
            "raw_status": _parse_corrupt_status(raw_status),
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
