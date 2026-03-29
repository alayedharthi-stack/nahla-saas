"""
GroundingDataFetcher
────────────────────
Fetches verified system data for each claim type the FactGuard needs to check.

All lookups are against the local database — no service-to-service HTTP.
This keeps the fact guard fast and eliminates cross-service failure modes.

The returned GroundingData object is passed to fact_guard/checker.py.
It is loaded ONCE per request, not per claim.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Set

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from database.models import Coupon, Order, Product, SyncLog, Tenant
from database.session import SessionLocal


@dataclass
class GroundingData:
    """Verified facts for this tenant + customer, loaded once per request."""

    # Products that exist in the catalog (regardless of stock level)
    known_product_ids: Set[int] = field(default_factory=set)

    # Products where metadata explicitly marks them as in-stock
    explicitly_in_stock_ids: Set[int] = field(default_factory=set)

    # Products explicitly marked as low stock (stock_count defined and below threshold)
    low_stock_product_ids: Set[int] = field(default_factory=set)

    # Products that have an active sale price or discount_pct in metadata
    discounted_product_ids: Set[int] = field(default_factory=set)

    # Coupon codes that are confirmed active (exist and not expired)
    valid_coupon_codes: Set[str] = field(default_factory=set)

    # Customer's last known order status (None if no orders)
    customer_last_order_status: Optional[str] = None

    # Customer has at least one order with a checkout_url (payment link exists)
    customer_has_pending_payment_link: bool = False

    # Delivery configuration
    same_day_delivery_enabled: bool = False
    pickup_enabled: bool = False
    has_configured_shipping: bool = False   # True if any ShippingFee rows exist
    has_delivery_zones: bool = False        # True if any DeliveryZone rows exist

    # Whether any restock event exists in sync_logs for this tenant
    restock_events_exist: bool = False


def fetch_grounding_data(tenant_id: int, customer_phone: str) -> GroundingData:
    """
    Load all verification data needed by the FactGuard in a single DB session.
    """
    db = SessionLocal()
    try:
        tenant: Optional[Tenant] = db.query(Tenant).filter(
            Tenant.id == tenant_id
        ).first()

        if not tenant:
            return GroundingData()

        # ── Product catalog ───────────────────────────────────────────────────
        _LOW_STOCK_THRESHOLD = 5   # units — products at or below this are "low stock"

        products = db.query(Product).filter(Product.tenant_id == tenant_id).all()
        known_ids: Set[int] = set()
        in_stock_ids: Set[int] = set()
        low_stock_ids: Set[int] = set()
        discounted_ids: Set[int] = set()
        for p in products:
            known_ids.add(p.id)
            meta = p.metadata or {}
            if meta.get("in_stock") is True:
                in_stock_ids.add(p.id)
            stock_count = meta.get("stock_count")
            if isinstance(stock_count, (int, float)) and 0 < stock_count <= _LOW_STOCK_THRESHOLD:
                low_stock_ids.add(p.id)
            if meta.get("sale_price") or meta.get("discount_pct"):
                discounted_ids.add(p.id)

        # ── Valid coupons ─────────────────────────────────────────────────────
        now = datetime.utcnow()
        coupons = db.query(Coupon).filter(
            Coupon.tenant_id == tenant_id,
        ).all()
        valid_codes: Set[str] = set()
        for c in coupons:
            if c.expires_at is None or c.expires_at > now:
                valid_codes.add(c.code.upper())

        # ── Customer last order + payment link ───────────────────────────────
        last_order_status: Optional[str] = None
        has_pending_payment_link = False
        customer_orders = (
            db.query(Order)
            .filter(Order.tenant_id == tenant_id)
            .filter(
                Order.customer_info.op("->>")(  # JSONB lookup
                    "phone"
                ) == customer_phone
            )
            .order_by(Order.id.desc())
            .limit(5)
            .all()
        )
        for o in customer_orders:
            if last_order_status is None:
                last_order_status = o.status
            if o.checkout_url:
                has_pending_payment_link = True
                break

        # ── Delivery config ───────────────────────────────────────────────────
        from database.models import DeliveryZone, ShippingFee
        has_shipping = db.query(ShippingFee).filter(
            ShippingFee.tenant_id == tenant_id
        ).first() is not None

        has_zones = db.query(DeliveryZone).filter(
            DeliveryZone.tenant_id == tenant_id
        ).first() is not None

        # ── Restock events ────────────────────────────────────────────────────
        restock = db.query(SyncLog).filter(
            SyncLog.tenant_id == tenant_id,
            SyncLog.resource_type == "product",
            SyncLog.status == "restock",
        ).first()

        return GroundingData(
            known_product_ids=known_ids,
            explicitly_in_stock_ids=in_stock_ids,
            low_stock_product_ids=low_stock_ids,
            discounted_product_ids=discounted_ids,
            valid_coupon_codes=valid_codes,
            customer_last_order_status=last_order_status,
            customer_has_pending_payment_link=has_pending_payment_link,
            same_day_delivery_enabled=bool(tenant.same_day_delivery_enabled),
            pickup_enabled=bool(tenant.pickup_enabled),
            has_configured_shipping=has_shipping,
            has_delivery_zones=has_zones,
            restock_events_exist=restock is not None,
        )

    finally:
        db.close()
