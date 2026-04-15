"""
services/store_sync.py
───────────────────────
Store Knowledge Sync Service.

Responsibilities
  • Initial full sync — fetch everything from the store adapter after connection
  • Incremental sync  — called by platform webhooks for individual entity updates
  • Snapshot update   — maintain StoreKnowledgeSnapshot so AI always has fresh data
  • Job tracking      — write StoreSyncJob rows so dashboard can show progress

Usage
  svc = StoreSyncService(db, tenant_id)
  await svc.full_sync()                    # full historical sync (first time)
  await svc.full_sync(incremental=True)    # incremental sync (subsequent times)
  await svc.sync_products()                # triggered by product webhook
  status = svc.get_status()
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# A job stuck in "running" for longer than this is considered timed out
_SYNC_JOB_TIMEOUT_MINUTES = 10

from sqlalchemy.orm import Session

# Allow importing from project root
_THIS = os.path.dirname(os.path.abspath(__file__))
_DB   = os.path.abspath(os.path.join(_THIS, "../../database"))
for _p in (_THIS, _DB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import (  # noqa: E402
    Coupon,
    Customer,
    CustomerProfile,
    Order,
    Product,
    StoreSyncJob,
    StoreKnowledgeSnapshot,
    TenantSettings,
)
from services.customer_intelligence import (  # noqa: E402
    CustomerIntelligenceService,
    extract_order_datetime as intelligence_extract_order_datetime,
    normalize_phone as intelligence_normalize_phone,
)

logger = logging.getLogger("nahla-backend")


# ── Data normalisation helpers ────────────────────────────────────────────────

import re as _re

def _normalize_phone(raw_phone) -> str:
    return intelligence_normalize_phone(raw_phone)


def _extract_order_datetime(raw: Any) -> Optional[datetime]:
    return intelligence_extract_order_datetime(raw)


def _normalise_product(raw: Any) -> Dict:
    """Convert a store-adapter product object/dict to a normalised internal dict."""
    if hasattr(raw, "dict"):
        raw = raw.dict()
    return {
        "external_id":   str(raw.get("id", raw.get("external_id", ""))),
        "sku":           raw.get("sku", ""),
        "title":         raw.get("title", raw.get("name", "")),
        "description":   raw.get("description", ""),
        "price":         str(raw.get("price", raw.get("regular_price", ""))),
        "sale_price":    str(raw.get("sale_price", raw.get("promo_price", "")) or ""),
        "status":        raw.get("status", "active"),
        "category":      raw.get("category", raw.get("main_category", "")),
        "brand":         raw.get("brand", ""),
        "image_url":     raw.get("image", raw.get("thumbnail", "")),
        "in_stock":      raw.get("in_stock", True),
        "stock_qty":     raw.get("quantity", raw.get("stock_quantity", None)),
        "tags":          raw.get("tags", []),
        "variants":      raw.get("variants", []),
        "metadata":      raw.get("metadata", {}),
    }


def _normalise_order(raw: Any) -> Dict:
    if hasattr(raw, "dict"):
        raw = raw.dict()
    customer_info = raw.get("customer") or raw.get("customer_info") or {}
    if not customer_info:
        customer_name = raw.get("customer_name", "")
        customer_phone = raw.get("customer_phone", "")
        if customer_name or customer_phone:
            customer_info = {
                "name": customer_name,
                "mobile": _normalize_phone(customer_phone),
                "phone": _normalize_phone(customer_phone),
            }
    else:
        customer_info = dict(customer_info)
        normalized_phone = _normalize_phone(customer_info.get("mobile", customer_info.get("phone", "")))
        if normalized_phone:
            customer_info["mobile"] = normalized_phone
            customer_info["phone"] = normalized_phone
    order_dt = _extract_order_datetime(raw)
    return {
        "external_id":   str(raw.get("id", raw.get("external_id", ""))),
        "status":        raw.get("status", "unknown"),
        "total":         str(raw.get("total", raw.get("sub_total", ""))),
        "customer_info": customer_info,
        "line_items":    raw.get("items", raw.get("line_items", [])),
        "checkout_url":  raw.get("checkout_url", ""),
        "is_abandoned":  raw.get("is_abandoned", raw.get("abandoned", False)),
        "created_at":    order_dt.isoformat() if order_dt else raw.get("created_at"),
    }


def _normalise_coupon(raw: Any) -> Dict:
    if hasattr(raw, "dict"):
        raw = raw.dict()
    discount_val = raw.get("amount", raw.get("percent", raw.get("value", "")))
    status = raw.get("status", "active")
    return {
        "code":           raw.get("code", ""),
        "description":    raw.get("description", raw.get("name", "")),
        "discount_type":  raw.get("type", raw.get("discount_type", "percentage")),
        "discount_value": str(discount_val) if discount_val else "",
        "expires_at":     raw.get("expire_date", raw.get("expiry_date", raw.get("expires_at", None))),
        "active":         status == "active" if isinstance(status, str) else raw.get("active", True),
        "minimum_order":  raw.get("minimum_amount", raw.get("minimum_order", None)),
        "maximum_uses":   raw.get("maximum_uses", None),
    }


def _compute_segment(total_orders: int, total_spend: float, days_inactive: int):
    """Classify a customer into a segment and compute churn risk."""
    if days_inactive <= 14:
        churn_risk = max(0.02, days_inactive * 0.005)
    elif days_inactive <= 30:
        churn_risk = 0.10 + (days_inactive - 14) * 0.008
    elif days_inactive <= 60:
        churn_risk = 0.23 + (days_inactive - 30) * 0.01
    elif days_inactive <= 90:
        churn_risk = 0.53 + (days_inactive - 60) * 0.008
    else:
        churn_risk = 0.77 + min((days_inactive - 90) * 0.002, 0.23)
    churn_risk = round(min(churn_risk, 1.0), 3)

    if days_inactive > 90:
        segment = "churned"
    elif days_inactive > 60:
        segment = "at_risk"
    elif total_orders <= 1:
        segment = "new"
    elif (total_spend >= 2000 and total_orders >= 5) or total_spend >= 3000:
        segment = "vip"
    else:
        segment = "active"

    return segment, churn_risk


# ── Sync service ──────────────────────────────────────────────────────────────

class StoreSyncService:
    """
    Orchestrates syncing a tenant's store data into Nahla's DB and
    building the AI-ready StoreKnowledgeSnapshot.
    """

    def __init__(self, db: Session, tenant_id: int):
        self.db        = db
        self.tenant_id = tenant_id
        self._adapter  = None   # lazy-loaded
        self._customer_intelligence = CustomerIntelligenceService(db, tenant_id)

    # ── Adapter access ─────────────────────────────────────────────────────────

    def _get_adapter(self):
        if self._adapter is None:
            try:
                sys.path.insert(0, os.path.abspath(os.path.join(_THIS, "..")))
                from store_integration.registry import get_adapter  # noqa: PLC0415
                self._adapter = get_adapter(self.tenant_id)
            except Exception as exc:
                logger.warning("tenant=%s store adapter unavailable: %s", self.tenant_id, exc)
        return self._adapter

    # ── Job helpers ────────────────────────────────────────────────────────────

    def _start_job(self, sync_type: str, triggered_by: str = "system") -> StoreSyncJob:
        job = StoreSyncJob(
            tenant_id    = self.tenant_id,
            status       = "running",
            sync_type    = sync_type,
            triggered_by = triggered_by,
            started_at   = datetime.now(timezone.utc),
        )
        self.db.add(job)
        self.db.flush()
        return job

    def _finish_job(self, job: StoreSyncJob, **counts):
        job.status       = "completed"
        job.completed_at = datetime.now(timezone.utc)
        for k, v in counts.items():
            if hasattr(job, k):
                setattr(job, k, v)
        self.db.commit()

    def _fail_job(self, job: StoreSyncJob, error: str):
        job.status        = "failed"
        job.completed_at  = datetime.now(timezone.utc)
        job.error_message = error[:2000]
        self.db.commit()

    # ── Snapshot builder ───────────────────────────────────────────────────────

    def _rebuild_snapshot(self, products_count: int, orders_count: int, coupons_count: int):
        """Rebuild the AI-ready knowledge snapshot from DB contents."""
        snap = (
            self.db.query(StoreKnowledgeSnapshot)
            .filter_by(tenant_id=self.tenant_id)
            .first()
        )
        if not snap:
            snap = StoreKnowledgeSnapshot(tenant_id=self.tenant_id)
            self.db.add(snap)

        # Build catalog summary (top 50 in-stock products for AI context)
        top_products = (
            self.db.query(Product)
            .filter_by(tenant_id=self.tenant_id)
            .limit(50)
            .all()
        )
        catalog_items = []
        categories: set = set()
        for p in top_products:
            meta = p.extra_metadata or {}
            catalog_items.append({
                "id":        p.id,
                "external_id": p.external_id,
                "title":     p.title,
                "sku":       p.sku,
                "price":     p.price,
                "sale_price": meta.get("sale_price"),
                "in_stock":  meta.get("in_stock", True),
                "category":  meta.get("category", ""),
                "brand":     meta.get("brand", ""),
                "image_url": meta.get("image_url", ""),
            })
            if meta.get("category"):
                categories.add(meta["category"])

        # Active coupons
        active_coupons = (
            self.db.query(Coupon)
            .filter(
                Coupon.tenant_id == self.tenant_id,
                (Coupon.expires_at == None) | (Coupon.expires_at > datetime.now(timezone.utc)),  # noqa: E711
            )
            .all()
        )
        coupon_list = [
            {
                "code":           c.code,
                "description":    c.description,
                "discount_type":  c.discount_type,
                "discount_value": c.discount_value,
                "expires_at":     c.expires_at.isoformat() if c.expires_at else None,
            }
            for c in active_coupons
        ]

        # Store profile from TenantSettings
        settings = (
            self.db.query(TenantSettings)
            .filter_by(tenant_id=self.tenant_id)
            .first()
        )
        store_cfg  = (settings.store_settings or {}) if settings else {}
        wa_cfg     = (settings.whatsapp_settings or {}) if settings else {}

        snap.store_profile = {
            "store_name":    store_cfg.get("store_name", ""),
            "store_url":     store_cfg.get("store_url", ""),
            "logo_url":      store_cfg.get("logo_url", ""),
            "description":   store_cfg.get("store_description", ""),
            "contact_phone": wa_cfg.get("owner_whatsapp_number", ""),
            "contact_email": store_cfg.get("contact_email", ""),
        }
        snap.catalog_summary = {
            "total_products": products_count,
            "categories":     list(categories)[:30],
            "top_products":   catalog_items,
        }
        snap.coupon_summary = {
            "active_count": len(coupon_list),
            "coupons":      coupon_list[:20],
        }

        # Shipping from store settings
        snap.shipping_summary = {
            "methods": store_cfg.get("shipping_methods", []),
            "notes":   store_cfg.get("delivery_notes", ""),
        }

        # Policy
        snap.policy_summary = {
            "return_policy":   store_cfg.get("return_policy", ""),
            "shipping_policy": store_cfg.get("shipping_policy", ""),
            "payment_methods": store_cfg.get("payment_methods", []),
            "support_hours":   store_cfg.get("support_hours", ""),
        }

        snap.product_count  = products_count
        snap.order_count    = orders_count
        snap.coupon_count   = len(coupon_list)
        snap.customer_count = self.db.query(Customer).filter_by(tenant_id=self.tenant_id).count()
        snap.category_count = len(categories)
        snap.sync_version   = (snap.sync_version or 0) + 1
        snap.last_full_sync_at = datetime.now(timezone.utc)
        snap.updated_at        = datetime.now(timezone.utc)
        self.db.commit()

    # ── Incremental timestamp helper ─────────────────────────────────────────

    def _last_sync_timestamp(self) -> Optional[str]:
        """Return ISO timestamp of the last successful full sync, or None if never synced."""
        snap = (
            self.db.query(StoreKnowledgeSnapshot)
            .filter_by(tenant_id=self.tenant_id)
            .first()
        )
        if snap and snap.last_full_sync_at:
            return snap.last_full_sync_at.isoformat()
        return None

    # ── Products sync ──────────────────────────────────────────────────────────

    async def sync_products(self, incremental: bool = False) -> int:
        """Fetch products from the store adapter and upsert into DB.

        If incremental=True and a previous full sync exists, only fetch
        products updated since that timestamp.
        """
        adapter = self._get_adapter()
        if not adapter:
            return 0

        updated_since = None
        if incremental:
            updated_since = self._last_sync_timestamp()

        try:
            raw_list = await adapter.get_products(updated_since=updated_since)
        except Exception as exc:
            logger.warning("tenant=%s product sync failed: %s", self.tenant_id, exc)
            return 0

        logger.info(
            "tenant=%s syncing %d products (incremental=%s, since=%s)",
            self.tenant_id, len(raw_list), incremental, updated_since or "beginning",
        )

        created = 0
        updated = 0
        for raw in raw_list:
            normalised = _normalise_product(raw)
            ext_id = normalised["external_id"]
            existing = (
                self.db.query(Product)
                .filter_by(tenant_id=self.tenant_id, external_id=ext_id)
                .first()
            )
            if existing:
                existing.title       = normalised["title"]
                existing.description = normalised["description"]
                existing.price       = normalised["price"]
                existing.sku         = normalised["sku"]
                existing.extra_metadata = normalised
                updated += 1
            else:
                self.db.add(Product(
                    tenant_id    = self.tenant_id,
                    external_id  = ext_id,
                    title        = normalised["title"],
                    description  = normalised["description"],
                    price        = normalised["price"],
                    sku          = normalised["sku"],
                    extra_metadata = normalised,
                ))
                created += 1
        self.db.flush()
        logger.info(
            "tenant=%s products sync done — created=%d updated=%d total_upserted=%d",
            self.tenant_id, created, updated, created + updated,
        )
        return created + updated

    # ── Orders sync ────────────────────────────────────────────────────────────

    async def sync_orders(self, incremental: bool = False) -> int:
        adapter = self._get_adapter()
        if not adapter:
            return 0

        updated_since = None
        has_local_orders = self.db.query(Order).filter(Order.tenant_id == self.tenant_id).first() is not None
        if incremental and has_local_orders:
            updated_since = self._last_sync_timestamp()

        try:
            raw_list = await adapter.get_orders(updated_since=updated_since)
        except Exception as exc:
            logger.warning("tenant=%s orders sync failed: %s", self.tenant_id, exc)
            raise

        logger.info(
            "tenant=%s syncing %d orders (incremental=%s, since=%s)",
            self.tenant_id, len(raw_list), incremental and has_local_orders, updated_since or "beginning",
        )

        created = 0
        updated = 0
        for raw in raw_list:
            normalised = _normalise_order(raw)
            ext_id = normalised["external_id"]
            existing = (
                self.db.query(Order)
                .filter_by(tenant_id=self.tenant_id, external_id=ext_id)
                .first()
            )
            if existing:
                existing.status        = normalised["status"]
                existing.total         = normalised["total"]
                existing.customer_info = normalised["customer_info"]
                existing.line_items    = normalised["line_items"]
                existing.is_abandoned  = normalised["is_abandoned"]
                existing.extra_metadata = {
                    **(existing.extra_metadata or {}),
                    "created_at": normalised.get("created_at"),
                }
                updated += 1
            else:
                self.db.add(Order(
                    tenant_id    = self.tenant_id,
                    external_id  = ext_id,
                    status       = normalised["status"],
                    total        = normalised["total"],
                    customer_info = normalised["customer_info"],
                    line_items   = normalised["line_items"],
                    checkout_url = normalised["checkout_url"],
                    is_abandoned = normalised["is_abandoned"],
                    extra_metadata = {"created_at": normalised.get("created_at")},
                ))
                created += 1
        logger.info(
            "tenant=%s orders sync done — created=%d updated=%d total_upserted=%d",
            self.tenant_id, created, updated, created + updated,
        )
        self.db.flush()
        return created + updated

    # ── Coupons sync ───────────────────────────────────────────────────────────

    async def sync_coupons(self) -> int:
        adapter = self._get_adapter()
        if not adapter or not hasattr(adapter, "get_coupons"):
            return 0

        try:
            raw_list = await adapter.get_coupons()
        except Exception as exc:
            logger.warning("tenant=%s coupons sync failed: %s", self.tenant_id, exc)
            return 0

        logger.info("tenant=%s syncing %d coupons", self.tenant_id, len(raw_list))

        created = 0
        updated = 0
        for raw in raw_list:
            normalised = _normalise_coupon(raw)
            code = normalised["code"]
            if not code:
                continue
            existing = (
                self.db.query(Coupon)
                .filter_by(tenant_id=self.tenant_id, code=code)
                .first()
            )
            exp = None
            if normalised["expires_at"]:
                try:
                    exp = datetime.fromisoformat(str(normalised["expires_at"]).replace("Z", "+00:00"))
                except Exception:
                    pass

            if existing:
                existing.description    = normalised["description"]
                existing.discount_type  = normalised["discount_type"]
                existing.discount_value = normalised["discount_value"]
                existing.expires_at     = exp
                updated += 1
            else:
                self.db.add(Coupon(
                    tenant_id      = self.tenant_id,
                    code           = code,
                    description    = normalised["description"],
                    discount_type  = normalised["discount_type"],
                    discount_value = normalised["discount_value"],
                    expires_at     = exp,
                    extra_metadata = normalised,
                ))
                created += 1
        self.db.flush()
        logger.info(
            "tenant=%s coupons sync done — created=%d updated=%d",
            self.tenant_id, created, updated,
        )
        return created + updated

    # ── Customers sync ─────────────────────────────────────────────────────────

    async def sync_customers(self, incremental: bool = False) -> int:
        adapter = self._get_adapter()
        if not adapter or not hasattr(adapter, "get_customers"):
            return 0

        updated_since = None
        if incremental:
            updated_since = self._last_sync_timestamp()

        try:
            raw_list = await adapter.get_customers(updated_since=updated_since)
        except Exception as exc:
            logger.warning("tenant=%s customers sync failed: %s", self.tenant_id, exc)
            return 0

        logger.info(
            "tenant=%s syncing %d customers (incremental=%s, since=%s)",
            self.tenant_id, len(raw_list), incremental, updated_since or "beginning",
        )

        created = 0
        updated = 0
        for raw in raw_list:
            ext_id = str(raw.get("id", ""))
            if not ext_id:
                continue
            name = (raw.get("first_name", "") + " " + raw.get("last_name", "")).strip()
            if not name:
                name = raw.get("name", "")
            email = raw.get("email", "")
            phone = _normalize_phone(raw.get("mobile", raw.get("phone", "")))

            # 1. Try by salla_id first
            existing = (
                self.db.query(Customer)
                .filter(
                    Customer.tenant_id == self.tenant_id,
                    Customer.extra_metadata["salla_id"].astext == ext_id,
                )
                .first()
            )

            # 2. If not found by salla_id, try by normalized phone to avoid duplicates
            if not existing and phone:
                existing = (
                    self.db.query(Customer)
                    .filter(
                        Customer.tenant_id == self.tenant_id,
                        Customer.phone == phone,
                    )
                    .first()
                )

            meta = {"salla_id": ext_id, "source": "salla",
                    "city": raw.get("city", ""), "country": raw.get("country", "SA")}

            if existing:
                if name:
                    existing.name = name
                if email:
                    existing.email = email
                if phone:
                    existing.phone = phone
                existing.extra_metadata = {**(existing.extra_metadata or {}), **meta}
                updated += 1
            else:
                self.db.add(Customer(
                    tenant_id=self.tenant_id,
                    name=name or None,
                    email=email or None,
                    phone=phone or None,
                    extra_metadata=meta,
                ))
                created += 1
        self.db.flush()
        logger.info(
            "tenant=%s customers sync done — created=%d updated=%d",
            self.tenant_id, created, updated,
        )
        return created + updated

    # ── Customer profile builder ─────────────────────────────────────────────

    def _build_customer_profiles(self) -> int:
        """Create/update CustomerProfile for every customer using unified intelligence rules."""
        return self._customer_intelligence.rebuild_profiles_for_tenant(
            reason="store_sync_build_profiles",
            commit=True,
            emit_event=True,
        )

    # ── Full sync ──────────────────────────────────────────────────────────────

    async def full_sync(self, triggered_by: str = "merchant", incremental: bool = False) -> Dict:
        """Sync store data into the local DB.

        Args:
            triggered_by: who initiated the sync (merchant / scheduler / oauth_connect).
            incremental: if True, only fetch items updated since last full sync.
                         First sync is always full regardless of this flag.
        """
        # ── Pre-sync guard: refuse if binding is invalid ──────────────────
        try:
            from services.salla_guard import validate_before_sync  # noqa: PLC0415
            ok, reason = validate_before_sync(self.db, self.tenant_id)
            if not ok:
                logger.warning(
                    "tenant=%s ⛔ SYNC_BLOCKED — %s (triggered_by=%s)",
                    self.tenant_id, reason, triggered_by,
                )
                return {"status": "blocked", "message": reason}
        except Exception as guard_exc:
            logger.warning("tenant=%s salla_guard check failed (non-fatal): %s", self.tenant_id, guard_exc)

        has_previous = self._last_sync_timestamp() is not None
        is_incremental = incremental and has_previous

        sync_type = "incremental" if is_incremental else "full"
        job = self._start_job(sync_type, triggered_by)
        logger.info(
            "tenant=%s ▶ %s sync started (triggered_by=%s, has_previous=%s)",
            self.tenant_id, sync_type.upper(), triggered_by, has_previous,
        )

        try:
            products_n  = await self.sync_products(incremental=is_incremental)
            orders_n    = await self.sync_orders(incremental=is_incremental)
            coupons_n   = await self.sync_coupons()
            customers_n = await self.sync_customers(incremental=is_incremental)
            profiles_n  = self._customer_intelligence.rebuild_profiles_for_tenant(
                reason=f"full_sync:{triggered_by}",
                commit=True,
                emit_event=True,
            )

            self._rebuild_snapshot(products_n, orders_n, coupons_n)
            self._finish_job(
                job,
                products_synced   = products_n,
                orders_synced     = orders_n,
                coupons_synced    = coupons_n,
                customers_synced  = customers_n,
            )
            total_items = products_n + orders_n + coupons_n + customers_n
            if total_items == 0:
                logger.warning(
                    "tenant=%s ⚠️ %s sync completed but ALL counts are ZERO — "
                    "store may be empty or token may lack permissions",
                    self.tenant_id, sync_type.upper(),
                )
            else:
                logger.info(
                    "tenant=%s ✅ %s sync completed — products=%d orders=%d coupons=%d customers=%d profiles=%d",
                    self.tenant_id, sync_type.upper(), products_n, orders_n, coupons_n, customers_n, profiles_n,
                )

            result = {
                "status":           "completed",
                "sync_type":        sync_type,
                "products_synced":  products_n,
                "orders_synced":    orders_n,
                "coupons_synced":   coupons_n,
                "customers_synced": customers_n,
                "profiles_updated": profiles_n,
                "job_id":           job.id,
            }
            if total_items == 0:
                result["message"] = (
                    "تم الربط بنجاح لكن المتجر لا يحتوي على بيانات قابلة للمزامنة حالياً. "
                    "أضف منتجات في سلة ثم أعد المزامنة."
                )
            return result
        except Exception as exc:
            self._fail_job(job, str(exc))
            logger.error("tenant=%s ❌ %s sync error: %s", self.tenant_id, sync_type.upper(), exc)
            return {"status": "failed", "error": str(exc), "job_id": job.id}

    # ── Incremental product update (called by webhook) ─────────────────────────

    async def handle_product_webhook(self, payload: Dict) -> None:
        """Process a single product update from a platform webhook."""
        normalised = _normalise_product(payload)
        ext_id     = normalised["external_id"]
        if not ext_id:
            return

        existing = (
            self.db.query(Product)
            .filter_by(tenant_id=self.tenant_id, external_id=ext_id)
            .first()
        )
        if existing:
            existing.title         = normalised["title"]
            existing.price         = normalised["price"]
            existing.extra_metadata = normalised
        else:
            self.db.add(Product(
                tenant_id      = self.tenant_id,
                external_id    = ext_id,
                title          = normalised["title"],
                description    = normalised["description"],
                price          = normalised["price"],
                sku            = normalised["sku"],
                extra_metadata = normalised,
            ))
        self.db.commit()

        # Rebuild snapshot counts (lightweight)
        snap = (
            self.db.query(StoreKnowledgeSnapshot)
            .filter_by(tenant_id=self.tenant_id)
            .first()
        )
        if snap:
            snap.product_count             = (
                self.db.query(Product).filter_by(tenant_id=self.tenant_id).count()
            )
            snap.last_incremental_sync_at  = datetime.now(timezone.utc)
            snap.updated_at                = datetime.now(timezone.utc)
            self.db.commit()

    # ── Incremental order update (called by webhook) ────────────────────────

    async def handle_order_webhook(self, payload: Dict) -> None:
        """Process a single order create/update from a platform webhook."""
        normalised = _normalise_order(payload)
        ext_id     = normalised["external_id"]
        if not ext_id:
            return

        existing = (
            self.db.query(Order)
            .filter_by(tenant_id=self.tenant_id, external_id=ext_id)
            .first()
        )
        if existing:
            existing.status        = normalised["status"]
            existing.total         = normalised["total"]
            existing.customer_info = normalised["customer_info"]
            existing.line_items    = normalised["line_items"]
            existing.is_abandoned  = normalised["is_abandoned"]
            existing.extra_metadata = {
                **(existing.extra_metadata or {}),
                "created_at": normalised.get("created_at"),
            }
        else:
            self.db.add(Order(
                tenant_id     = self.tenant_id,
                external_id   = ext_id,
                status        = normalised["status"],
                total         = normalised["total"],
                customer_info = normalised["customer_info"],
                line_items    = normalised["line_items"],
                checkout_url  = normalised["checkout_url"],
                is_abandoned  = normalised["is_abandoned"],
                extra_metadata = {"created_at": normalised.get("created_at")},
            ))
        self.db.commit()

        customer = self._customer_intelligence.upsert_customer_from_order(
            normalised,
            source="order_webhook",
            commit=False,
        )
        if customer:
            self._customer_intelligence.recompute_profile_for_customer(
                customer.id,
                reason="order_webhook",
                commit=True,
                emit_event=True,
            )

        snap = (
            self.db.query(StoreKnowledgeSnapshot)
            .filter_by(tenant_id=self.tenant_id)
            .first()
        )
        if snap:
            snap.order_count              = (
                self.db.query(Order).filter_by(tenant_id=self.tenant_id).count()
            )
            snap.last_incremental_sync_at = datetime.now(timezone.utc)
            snap.updated_at               = datetime.now(timezone.utc)
            self.db.commit()

    def _update_customer_profile_from_order(self, normalised: Dict) -> None:
        """Backward-compatible wrapper around the unified customer intelligence service."""
        customer = self._customer_intelligence.upsert_customer_from_order(
            normalised,
            source="order_incremental",
            commit=False,
        )
        if customer:
            self._customer_intelligence.recompute_profile_for_customer(
                customer.id,
                reason="order_incremental",
                commit=True,
                emit_event=True,
            )

    # ── Incremental customer update (called by webhook) ───────────────────

    async def handle_customer_webhook(self, payload: Dict) -> None:
        """Process a single customer create/update from a platform webhook."""
        ext_id = str(payload.get("id", ""))
        name   = (payload.get("first_name", "") + " " + payload.get("last_name", "")).strip()
        if not name:
            name = payload.get("name", "")
        email  = payload.get("email", "")
        phone  = _normalize_phone(payload.get("mobile", payload.get("phone", "")))

        if not ext_id:
            return

        existing = self._customer_intelligence.upsert_customer_identity(
            phone=phone,
            name=name,
            email=email,
            external_id=ext_id,
            source="customer_webhook",
            extra_metadata=payload.get("metadata", {}) or {},
            seen_at=datetime.now(timezone.utc),
        )
        if existing and existing.id:
            self._customer_intelligence.recompute_profile_for_customer(
                existing.id,
                reason="customer_webhook",
                commit=True,
                emit_event=True,
            )
        else:
            self.db.commit()

        snap = (
            self.db.query(StoreKnowledgeSnapshot)
            .filter_by(tenant_id=self.tenant_id)
            .first()
        )
        if snap:
            snap.customer_count           = (
                self.db.query(Customer).filter_by(tenant_id=self.tenant_id).count()
            )
            snap.last_incremental_sync_at = datetime.now(timezone.utc)
            snap.updated_at               = datetime.now(timezone.utc)
            self.db.commit()

    # ── Product deletion (called by webhook) ──────────────────────────────

    async def handle_product_deleted(self, external_id: str) -> None:
        """Remove a product that was deleted in the store."""
        if not external_id:
            return
        deleted = (
            self.db.query(Product)
            .filter_by(tenant_id=self.tenant_id, external_id=external_id)
            .delete()
        )
        if deleted:
            self.db.commit()
            snap = (
                self.db.query(StoreKnowledgeSnapshot)
                .filter_by(tenant_id=self.tenant_id)
                .first()
            )
            if snap:
                snap.product_count            = (
                    self.db.query(Product).filter_by(tenant_id=self.tenant_id).count()
                )
                snap.last_incremental_sync_at = datetime.now(timezone.utc)
                snap.updated_at               = datetime.now(timezone.utc)
                self.db.commit()
            logger.info("tenant=%s product deleted | external_id=%s", self.tenant_id, external_id)

    # ── Status ─────────────────────────────────────────────────────────────────

    def get_status(self) -> Dict:
        """Return current sync status for the dashboard."""
        snap = (
            self.db.query(StoreKnowledgeSnapshot)
            .filter_by(tenant_id=self.tenant_id)
            .first()
        )
        last_job = (
            self.db.query(StoreSyncJob)
            .filter_by(tenant_id=self.tenant_id)
            .order_by(StoreSyncJob.id.desc())
            .first()
        )
        # Auto-expire stale "running" jobs so the UI never gets stuck forever.
        stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=_SYNC_JOB_TIMEOUT_MINUTES)
        stale_jobs = (
            self.db.query(StoreSyncJob)
            .filter(
                StoreSyncJob.tenant_id == self.tenant_id,
                StoreSyncJob.status    == "running",
                StoreSyncJob.created_at < stale_cutoff,
            )
            .all()
        )
        for stale in stale_jobs:
            stale.status        = "timed_out"
            stale.error_message = (
                f"تجاوز الحد الزمني ({_SYNC_JOB_TIMEOUT_MINUTES} دقيقة). "
                "قد يكون الخادم أُعيد تشغيله أثناء المزامنة."
            )
            stale.completed_at  = datetime.now(timezone.utc)
        if stale_jobs:
            self.db.commit()

        running_job = (
            self.db.query(StoreSyncJob)
            .filter_by(tenant_id=self.tenant_id, status="running")
            .first()
        )
        return {
            "has_snapshot":           snap is not None,
            "product_count":          snap.product_count  if snap else 0,
            "category_count":         snap.category_count if snap else 0,
            "order_count":            snap.order_count    if snap else 0,
            "coupon_count":           snap.coupon_count   if snap else 0,
            "customer_count":         snap.customer_count if snap else 0,
            "last_full_sync_at":      snap.last_full_sync_at.isoformat() if (snap and snap.last_full_sync_at) else None,
            "last_incremental_sync_at": snap.last_incremental_sync_at.isoformat() if (snap and snap.last_incremental_sync_at) else None,
            "sync_version":           snap.sync_version   if snap else 0,
            "sync_running":           running_job is not None,
            "last_job_status":        last_job.status     if last_job else None,
            "last_job_id":            last_job.id         if last_job else None,
            "last_job_error":         last_job.error_message if last_job else None,
        }
