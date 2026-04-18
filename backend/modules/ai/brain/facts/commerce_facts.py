"""
brain/facts/commerce_facts.py
──────────────────────────────
DefaultFactsLoader — Phase 2 enriched version.

Loads a CommerceFacts snapshot for a single decision turn.

Phase 1 scalars (cheap, always loaded):
  has_products, product_count, has_active_integration, has_coupons,
  store_name, store_url, snapshot_fresh

Phase 2 additions (slightly more work but still < 5ms):
  in_stock_count     — products with in_stock=True
  orderable          — integration active + in_stock > 0
  coupon_eligibility — best active coupon code (first match)
  top_products       — top 5 in-stock products (id, external_id, title, price)
  integration_platform — "salla" | "zid" | "manual" | "unknown"
  within_working_hours — None when no store_hours configured
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ..types import CommerceFacts

logger = logging.getLogger("nahla.brain.facts_loader")


class DefaultFactsLoader:
    """Implements FactsLoader protocol."""

    def load(self, db: Any, tenant_id: int) -> CommerceFacts:
        from database.models import (
            Coupon,
            Integration,
            Product,
            StoreKnowledgeSnapshot,
            TenantSettings,
        )
        from sqlalchemy import func

        facts = CommerceFacts()

        # ── 1. Integration ────────────────────────────────────────────────
        integration = (
            db.query(Integration)
            .filter(Integration.tenant_id == tenant_id)
            .first()
        )
        facts.has_active_integration = bool(
            integration and integration.access_token
        )
        if integration:
            platform = (integration.config or {}).get("platform", "")
            if not platform:
                # Infer from integration type
                ig_type = getattr(integration, "integration_type", "") or ""
                platform = ig_type.lower() if ig_type else "unknown"
            facts.integration_platform = platform or "unknown"

        # ── 2. Products ───────────────────────────────────────────────────
        product_count = (
            db.query(func.count(Product.id))
            .filter(Product.tenant_id == tenant_id)
            .scalar()
        ) or 0
        facts.product_count = product_count
        facts.has_products  = product_count > 0

        # In-stock count (Phase 2)
        in_stock_count = (
            db.query(func.count(Product.id))
            .filter(
                Product.tenant_id == tenant_id,
                Product.in_stock.is_(True),
            )
            .scalar()
        ) or 0
        facts.in_stock_count = in_stock_count
        facts.orderable = facts.has_active_integration and in_stock_count > 0

        # Top 5 in-stock products for greeting / discovery (Phase 2)
        top_rows = (
            db.query(Product)
            .filter(
                Product.tenant_id == tenant_id,
                Product.in_stock.is_(True),
            )
            .order_by(Product.id)
            .limit(5)
            .all()
        )
        facts.top_products = [
            {
                "id":          p.id,
                "external_id": p.external_id,
                "title":       p.title,
                "price":       p.price,
                "sku":         p.sku,
            }
            for p in top_rows
        ]

        # ── 3. Coupons ────────────────────────────────────────────────────
        now = datetime.now(timezone.utc)

        active_coupons = (
            db.query(Coupon)
            .filter(
                Coupon.tenant_id == tenant_id,
                (Coupon.expires_at == None) | (Coupon.expires_at > now),  # noqa: E711
            )
            .limit(5)
            .all()
        )
        facts.has_coupons = bool(active_coupons)

        # Best coupon eligibility (Phase 2): pick the first valid coupon code
        for c in active_coupons:
            code = getattr(c, "code", "") or ""
            if code:
                facts.coupon_eligibility = str(code)
                break

        # ── 4. Store metadata ─────────────────────────────────────────────
        snapshot = (
            db.query(StoreKnowledgeSnapshot)
            .filter(StoreKnowledgeSnapshot.tenant_id == tenant_id)
            .first()
        )
        if snapshot:
            facts.snapshot_fresh = True
            raw = snapshot.data or {}
            facts.store_name = raw.get("store_name", "")
            facts.store_url  = raw.get("store_url", "")

        # ── 5. Working hours (Phase 2) ─────────────────────────────────────
        try:
            settings = (
                db.query(TenantSettings)
                .filter(TenantSettings.tenant_id == tenant_id)
                .first()
            )
            if settings:
                store_hours = (settings.ai_settings or {}).get("store_hours")
                if store_hours:
                    facts.within_working_hours = _check_working_hours(store_hours)
        except Exception:
            pass   # working hours are optional — never block a turn

        logger.debug(
            "[FactsLoader] tenant=%s products=%d (in_stock=%d) orderable=%s coupons=%s platform=%s",
            tenant_id,
            facts.product_count,
            facts.in_stock_count,
            facts.orderable,
            facts.has_coupons,
            facts.integration_platform,
        )
        return facts


def _check_working_hours(store_hours: dict) -> bool:
    """
    Returns True if current UTC time falls inside any configured window.

    store_hours format expected in ai_settings:
      {
        "timezone": "Asia/Riyadh",
        "windows": [
          {"day": 0, "open": "09:00", "close": "22:00"},
          ...
        ]
      }
    Day 0 = Monday, 6 = Sunday (ISO weekday - 1).
    """
    try:
        import zoneinfo
        tz_name = store_hours.get("timezone", "Asia/Riyadh")
        tz = zoneinfo.ZoneInfo(tz_name)
        local_now = datetime.now(tz)
        day_idx = local_now.weekday()   # 0=Mon, 6=Sun
        time_str = local_now.strftime("%H:%M")

        for window in store_hours.get("windows", []):
            if window.get("day") == day_idx:
                if window.get("open", "00:00") <= time_str <= window.get("close", "23:59"):
                    return True
        return False
    except Exception:
        return True   # assume open on any error
