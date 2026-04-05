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
  await svc.full_sync()          # triggered by merchant "Sync Now" or after store connect
  await svc.sync_products()      # triggered by product webhook
  await svc.sync_orders(limit=50)
  status = svc.get_status()
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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
    Order,
    Product,
    StoreSyncJob,
    StoreKnowledgeSnapshot,
    TenantSettings,
)

logger = logging.getLogger("nahla-backend")


# ── Data normalisation helpers ────────────────────────────────────────────────

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
    return {
        "external_id":   str(raw.get("id", raw.get("external_id", ""))),
        "status":        raw.get("status", "unknown"),
        "total":         str(raw.get("total", raw.get("sub_total", ""))),
        "customer_info": raw.get("customer", raw.get("customer_info", {})),
        "line_items":    raw.get("items", raw.get("line_items", [])),
        "checkout_url":  raw.get("checkout_url", ""),
        "is_abandoned":  raw.get("is_abandoned", raw.get("abandoned", False)),
    }


def _normalise_coupon(raw: Any) -> Dict:
    if hasattr(raw, "dict"):
        raw = raw.dict()
    return {
        "code":           raw.get("code", ""),
        "description":    raw.get("description", raw.get("name", "")),
        "discount_type":  raw.get("type", raw.get("discount_type", "percentage")),
        "discount_value": str(raw.get("value", raw.get("discount_value", ""))),
        "expires_at":     raw.get("expires_at", raw.get("expire_date", None)),
        "active":         raw.get("active", raw.get("enabled", True)),
        "minimum_order":  raw.get("minimum_order", None),
        "maximum_uses":   raw.get("maximum_uses", None),
    }


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
        snap.category_count = len(categories)
        snap.sync_version   = (snap.sync_version or 0) + 1
        snap.last_full_sync_at = datetime.now(timezone.utc)
        snap.updated_at        = datetime.now(timezone.utc)
        self.db.commit()

    # ── Products sync ──────────────────────────────────────────────────────────

    async def sync_products(self) -> int:
        """Fetch all products from the store adapter and upsert into DB."""
        adapter = self._get_adapter()
        if not adapter:
            return 0

        try:
            raw_list = await adapter.get_products()
        except Exception as exc:
            logger.warning("tenant=%s product sync failed: %s", self.tenant_id, exc)
            return 0

        count = 0
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
            count += 1
        self.db.flush()
        return count

    # ── Orders sync ────────────────────────────────────────────────────────────

    async def sync_orders(self, limit: int = 200) -> int:
        adapter = self._get_adapter()
        if not adapter:
            return 0

        try:
            raw_list = await adapter.get_orders(limit=limit)
        except Exception as exc:
            logger.warning("tenant=%s orders sync failed: %s", self.tenant_id, exc)
            return 0

        count = 0
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
                ))
            count += 1
        self.db.flush()
        return count

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

        count = 0
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
            count += 1
        self.db.flush()
        return count

    # ── Full sync ──────────────────────────────────────────────────────────────

    async def full_sync(self, triggered_by: str = "merchant") -> Dict:
        """
        Run a complete initial sync:
          products → orders → coupons → rebuild snapshot

        Returns a summary dict.
        """
        job = self._start_job("full", triggered_by)
        try:
            products_n  = await self.sync_products()
            orders_n    = await self.sync_orders()
            coupons_n   = await self.sync_coupons()

            self._rebuild_snapshot(products_n, orders_n, coupons_n)
            self._finish_job(
                job,
                products_synced   = products_n,
                orders_synced     = orders_n,
                coupons_synced    = coupons_n,
            )
            logger.info(
                "tenant=%s full sync done — products=%d orders=%d coupons=%d",
                self.tenant_id, products_n, orders_n, coupons_n,
            )
            return {
                "status":           "completed",
                "products_synced":  products_n,
                "orders_synced":    orders_n,
                "coupons_synced":   coupons_n,
                "job_id":           job.id,
            }
        except Exception as exc:
            self._fail_job(job, str(exc))
            logger.error("tenant=%s full sync error: %s", self.tenant_id, exc)
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
