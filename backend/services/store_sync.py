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
    ProductInterest,
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
        "status":        _extract_status_string(raw.get("status"), fallback="active"),
        "category":      raw.get("category", raw.get("main_category", "")),
        "brand":         raw.get("brand", ""),
        "image_url":     raw.get("image", raw.get("thumbnail", "")),
        "in_stock":      raw.get("in_stock", True),
        "stock_qty":     raw.get("quantity", raw.get("stock_quantity", None)),
        "tags":          raw.get("tags", []),
        "variants":      raw.get("variants", []),
        "metadata":      raw.get("metadata", {}),
    }


def _coerce_int(value: Any) -> Optional[int]:
    """Best-effort conversion of stock_qty (str/int/None) to a real int."""
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _extract_status_string(status: Any, fallback: str = "unknown") -> str:
    """
    Salla (and some other platforms) return order/product status as either:
      • a plain string  e.g. "under_review"
      • a dict          e.g. {"id": 566146469, "name": "بإنتظار المراجعة",
                               "slug": "under_review", "customized": {...}}

    The DB column is VARCHAR — always return a plain string.
    Priority: slug → name → str(fallback)
    """
    if isinstance(status, dict):
        return str(status.get("slug") or status.get("name") or fallback)
    if status is None:
        return fallback
    s = str(status).strip()
    return s if s else fallback


def _extract_amount_string(value: Any) -> str:
    """
    Salla sometimes sends monetary fields as:
      • a plain number/string  → return as-is
      • a dict {"amount": 100, "currency": "SAR"} → extract amount

    Always returns a string safe for the VARCHAR `total` column.
    """
    if isinstance(value, dict):
        amount = value.get("amount") or value.get("value") or ""
        return str(amount)
    return str(value) if value is not None else ""


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
    raw_total = raw.get("total") or raw.get("sub_total") or raw.get("amounts", {})

    external_id = str(raw.get("id", raw.get("external_id", ""))).strip()
    # Human-visible order number — prefer the platform's explicit
    # reference_id (Salla), fall back to a few common synonyms (Zid uses
    # `code`, Shopify uses `name`/`order_number`), and finally to the
    # external_id so the column is never blank.
    external_order_number = str(
        raw.get("reference_id")
        or raw.get("order_number")
        or raw.get("number")
        or raw.get("code")
        or raw.get("name")
        or external_id
    ).strip() or external_id

    customer_name = (
        (customer_info.get("name") if isinstance(customer_info, dict) else None)
        or raw.get("customer_name")
        or ""
    )
    customer_name = str(customer_name).strip()

    return {
        "external_id":           external_id,
        "external_order_number": external_order_number,
        "status":                _extract_status_string(raw.get("status"), fallback="unknown"),
        "total":                 _extract_amount_string(raw_total),
        "customer_name":         customer_name,
        "customer_info":         customer_info,
        "line_items":            raw.get("items", raw.get("line_items", [])),
        "checkout_url":          raw.get("checkout_url", ""),
        "is_abandoned":          raw.get("is_abandoned", raw.get("abandoned", False)),
        "source":                str(raw.get("source") or "").strip().lower() or None,
        "created_at":            order_dt.isoformat() if order_dt else raw.get("created_at"),
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


# NOTE: The segment classifier used to live here as ``_compute_segment`` with
# a different label set (``churned|new|vip|active``) than the authoritative
# one in ``services/customer_intelligence.compute_customer_status`` (``lead|
# new|active|vip|at_risk|inactive``). It was never called from anywhere but
# caused confusion during refactors, so it was deleted as part of the
# 2026-04-16 root-cause fix. Use ``CustomerIntelligenceService`` for ALL
# classification decisions.


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
        # (product_id, external_id, title) for products that just transitioned
        # from out-of-stock → in-stock. Fan-out is performed once after the
        # loop so we don't slow down each iteration with ProductInterest queries
        # for products no one is waiting on.
        restocked: List[Dict[str, Any]] = []
        for raw in raw_list:
            normalised = _normalise_product(raw)
            ext_id = normalised["external_id"]
            new_qty = _coerce_int(normalised.get("stock_qty"))
            new_in_stock = bool(normalised.get("in_stock", True))
            new_available = new_in_stock and (new_qty is None or new_qty > 0)

            existing = (
                self.db.query(Product)
                .filter_by(tenant_id=self.tenant_id, external_id=ext_id)
                .first()
            )
            if existing:
                # Detect 0 → >0 transition BEFORE we overwrite the columns.
                # We treat (in_stock=false) OR (stock_quantity<=0) as "was zero".
                was_unavailable = (
                    (getattr(existing, "in_stock", True) is False)
                    or (existing.stock_quantity is not None and existing.stock_quantity <= 0)
                )
                existing.title       = normalised["title"]
                existing.description = normalised["description"]
                existing.price       = normalised["price"]
                existing.sku         = normalised["sku"]
                existing.in_stock    = new_in_stock
                existing.stock_quantity = new_qty
                existing.extra_metadata = normalised
                updated += 1

                if was_unavailable and new_available:
                    restocked.append({
                        "product_id":  existing.id,
                        "external_id": ext_id,
                        "title":       normalised["title"],
                    })
            else:
                p = Product(
                    tenant_id    = self.tenant_id,
                    external_id  = ext_id,
                    title        = normalised["title"],
                    description  = normalised["description"],
                    price        = normalised["price"],
                    sku          = normalised["sku"],
                    in_stock     = new_in_stock,
                    stock_quantity = new_qty,
                    extra_metadata = normalised,
                )
                self.db.add(p)
                created += 1
        self.db.flush()

        # ── Back-in-stock fan-out ─────────────────────────────────────────────
        # For each product that just came back, emit one
        # `product_back_in_stock` AutomationEvent per pending ProductInterest
        # row. The engine then processes each event as a normal single-customer
        # send, so all the existing idempotency/delay/condition machinery
        # continues to apply (including per-execution metrics).
        if restocked:
            self._fan_out_back_in_stock(restocked)

        logger.info(
            "tenant=%s products sync done — created=%d updated=%d total_upserted=%d restocked=%d",
            self.tenant_id, created, updated, created + updated, len(restocked),
        )
        return created + updated

    def _fan_out_back_in_stock(self, restocked: List[Dict[str, Any]]) -> None:
        """
        Emit one product_back_in_stock event per pending ProductInterest row
        for each restocked product. Called from sync_products after the
        upsert loop has flushed the new stock state.
        """
        from core.automation_engine import emit_automation_event  # noqa: PLC0415

        # Look up the merchant's store URL once so we can synthesize a
        # clickable product URL in the event payload — the named slot
        # `product_url` is the contract every back_in_stock_* template uses.
        store_cfg = (
            self.db.query(TenantSettings)
            .filter_by(tenant_id=self.tenant_id)
            .first()
        )
        store_url_root = ""
        if store_cfg and store_cfg.store_settings:
            store_url_root = str(store_cfg.store_settings.get("store_url") or "").rstrip("/")

        emitted = 0
        for prod in restocked:
            interests: List[ProductInterest] = (
                self.db.query(ProductInterest)
                .filter(
                    ProductInterest.tenant_id  == self.tenant_id,
                    ProductInterest.product_id == prod["product_id"],
                    ProductInterest.notified   == False,  # noqa: E712
                )
                .all()
            )
            if not interests:
                continue
            now = datetime.now(timezone.utc)
            for interest in interests:
                product_url = ""
                if store_url_root and prod["external_id"]:
                    product_url = f"{store_url_root}/p/{prod['external_id']}"
                emit_automation_event(
                    self.db,
                    self.tenant_id,
                    "product_back_in_stock",
                    customer_id=interest.customer_id,
                    payload={
                        "product_id":          prod["product_id"],
                        "product_external_id": prod["external_id"],
                        "product_name":        prod["title"],
                        "product_url":         product_url,
                        "store_url":           store_url_root,
                        "interest_id":         interest.id,
                    },
                    commit=False,
                )
                # Mark the interest as notified up-front. If the engine fails
                # to actually send (no template, no WA connection), the
                # AutomationExecution row records the failure — re-arming the
                # waitlist on every restock would double-spam customers.
                interest.notified = True
                interest.notified_at = now
                emitted += 1
        if emitted:
            self.db.flush()
            logger.info(
                "tenant=%s back-in-stock fan-out — products=%d events=%d",
                self.tenant_id, len(restocked), emitted,
            )

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
        status_counter: Dict[str, int] = {}
        zero_total_count = 0
        for raw in raw_list:
            # Capture the upstream status BEFORE normalisation so we can audit
            # any future divergence between Salla's slug and our DB string.
            raw_status = None
            try:
                raw_status = (
                    raw.status if hasattr(raw, "status")
                    else (raw.get("status") if isinstance(raw, dict) else None)
                )
            except Exception:
                raw_status = None

            normalised = _normalise_order(raw)
            ext_id = normalised["external_id"]
            normalised_status = normalised["status"]
            status_counter[normalised_status] = status_counter.get(normalised_status, 0) + 1

            # If the normalised status looks like a Python repr, that means
            # the upstream was a dict the adapter failed to unwrap. Surface
            # loudly — historically this corruption silently mapped every
            # order to "ملغي" in the dashboard.
            if normalised_status.startswith("{"):
                logger.warning(
                    "tenant=%s order=%s status looks like a repr (%r) — adapter "
                    "failed to extract slug from raw=%r",
                    self.tenant_id, ext_id, normalised_status, raw_status,
                )

            try:
                _amount = float(normalised["total"] or 0)
            except (TypeError, ValueError):
                _amount = 0.0
            if _amount == 0.0:
                zero_total_count += 1

            # Resolve the order's source: prefer what the adapter put on
            # the normalised row, else fall back to the registered adapter
            # platform name (salla/zid/shopify). Never leave it blank for a
            # platform-synced order.
            adapter_source = (
                normalised.get("source")
                or getattr(adapter, "platform", None)
                or "salla"
            )

            existing = (
                self.db.query(Order)
                .filter_by(tenant_id=self.tenant_id, external_id=ext_id)
                .first()
            )
            if existing:
                existing.status                = normalised_status
                existing.total                 = normalised["total"]
                existing.customer_info         = normalised["customer_info"]
                existing.line_items            = normalised["line_items"]
                existing.is_abandoned          = normalised["is_abandoned"]
                existing.external_order_number = normalised["external_order_number"]
                if normalised["customer_name"]:
                    existing.customer_name = normalised["customer_name"]
                existing.source = adapter_source
                existing.extra_metadata = {
                    **(existing.extra_metadata or {}),
                    "created_at": normalised.get("created_at"),
                }
                updated += 1
            else:
                self.db.add(Order(
                    tenant_id             = self.tenant_id,
                    external_id           = ext_id,
                    external_order_number = normalised["external_order_number"],
                    status                = normalised_status,
                    total                 = normalised["total"],
                    customer_name         = normalised["customer_name"] or None,
                    customer_info         = normalised["customer_info"],
                    line_items            = normalised["line_items"],
                    checkout_url          = normalised["checkout_url"],
                    is_abandoned          = normalised["is_abandoned"],
                    source                = adapter_source,
                    extra_metadata        = {"created_at": normalised.get("created_at")},
                ))
                created += 1
        logger.info(
            "tenant=%s orders sync done — created=%d updated=%d total_upserted=%d "
            "status_distribution=%s zero_total=%d",
            self.tenant_id, created, updated, created + updated,
            status_counter, zero_total_count,
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
        """
        Process a single order create/update from a platform webhook.

        Idempotent by ``(tenant_id, external_id)`` — the DB enforces this via
        the partial unique index ``uq_orders_tenant_external_id`` added in
        migration 0023. Concurrent webhooks can no longer double-insert.
        """
        from core.obs import EVENTS, log_event  # noqa: PLC0415
        from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

        normalised = _normalise_order(payload)
        ext_id     = normalised["external_id"]
        if not ext_id:
            log_event(
                EVENTS.ORDER_UPSERT_ERROR,
                tenant_id=self.tenant_id,
                reason="missing_external_id",
            )
            return

        is_new = False
        order_row = (
            self.db.query(Order)
            .filter_by(tenant_id=self.tenant_id, external_id=ext_id)
            .first()
        )
        # Webhook payload doesn't always tell us which adapter it came
        # from; resolve from the registered adapter for this tenant so the
        # source column stays accurate (salla/zid/shopify).
        webhook_source = (
            normalised.get("source")
            or getattr(self._get_adapter(), "platform", None)
            or "salla"
        )

        if order_row is not None:
            order_row.status                = normalised["status"]
            order_row.total                 = normalised["total"]
            order_row.customer_info         = normalised["customer_info"]
            order_row.line_items            = normalised["line_items"]
            order_row.is_abandoned          = normalised["is_abandoned"]
            order_row.external_order_number = normalised["external_order_number"]
            if normalised["customer_name"]:
                order_row.customer_name = normalised["customer_name"]
            order_row.source = webhook_source
            order_row.extra_metadata = {
                **(order_row.extra_metadata or {}),
                "created_at": normalised.get("created_at"),
            }
            try:
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        else:
            order_row = Order(
                tenant_id             = self.tenant_id,
                external_id           = ext_id,
                external_order_number = normalised["external_order_number"],
                status                = normalised["status"],
                total                 = normalised["total"],
                customer_name         = normalised["customer_name"] or None,
                customer_info         = normalised["customer_info"],
                line_items            = normalised["line_items"],
                checkout_url          = normalised["checkout_url"],
                is_abandoned          = normalised["is_abandoned"],
                source                = webhook_source,
                extra_metadata        = {"created_at": normalised.get("created_at")},
            )
            self.db.add(order_row)
            try:
                self.db.commit()
                is_new = True
            except IntegrityError:
                # Concurrent writer beat us to it — fall back to UPDATE path.
                self.db.rollback()
                log_event(
                    EVENTS.ORDER_UPSERT_CONFLICT,
                    tenant_id=self.tenant_id,
                    external_id=ext_id,
                )
                order_row = (
                    self.db.query(Order)
                    .filter_by(tenant_id=self.tenant_id, external_id=ext_id)
                    .first()
                )
                if order_row is None:
                    # Should be impossible, but fail loudly rather than silently.
                    raise
                order_row.status                = normalised["status"]
                order_row.total                 = normalised["total"]
                order_row.customer_info         = normalised["customer_info"]
                order_row.line_items            = normalised["line_items"]
                order_row.is_abandoned          = normalised["is_abandoned"]
                order_row.external_order_number = normalised["external_order_number"]
                if normalised["customer_name"]:
                    order_row.customer_name = normalised["customer_name"]
                order_row.source = webhook_source
                try:
                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    raise

        log_event(
            EVENTS.ORDER_UPSERT_SUCCESS,
            tenant_id=self.tenant_id,
            external_id=ext_id,
            order_id=order_row.id,
            is_new=is_new,
            status=normalised["status"],
        )

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

        if is_new:
            try:
                from core.automation_engine import emit_automation_event  # noqa: PLC0415
                emit_automation_event(
                    self.db,
                    self.tenant_id,
                    "order_created",
                    customer_id=customer.id if customer else None,
                    payload={
                        "external_id": ext_id,
                        "order_id": order_row.id,
                        "status": normalised.get("status"),
                        "total": normalised.get("total"),
                    },
                    commit=True,
                )
            except Exception as exc:
                # Automation failures are logged at ERROR so they are visible,
                # but do not fail the whole webhook — the order is already
                # durably stored and the dispatcher will retry this webhook
                # if we re-raise, potentially double-inserting automation rows.
                logger.exception(
                    "[StoreSync] emit order_created failed tenant=%s order=%s: %s",
                    self.tenant_id, order_row.id, exc,
                )

            # Close the offer-decision attribution loop. We do this on every
            # *new* order — not only on `order_paid` — because Salla orders
            # frequently arrive already paid, and waiting for `order_paid`
            # would miss them. Idempotent on re-runs.
            try:
                from services.offer_attribution_service import (  # noqa: PLC0415
                    attribute_order_to_decision,
                )
                attribute_order_to_decision(
                    self.db,
                    tenant_id=self.tenant_id,
                    order_id=order_row.id,
                    payload={
                        "total":  normalised.get("total"),
                        "status": normalised.get("status"),
                    },
                )
            except Exception as exc:
                logger.debug(
                    "[StoreSync] offer attribution failed tenant=%s order=%s: %s",
                    self.tenant_id, order_row.id, exc,
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
