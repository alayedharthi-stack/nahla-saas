"""
core/store_knowledge.py
────────────────────────
AI-Readiness Layer for Nahla.

Provides structured, fact-guarded context to the AI orchestrator so it can
answer customer questions using *real* store data — not guesses.

Classes
  StoreKnowledgeLoader     — loads the synced knowledge snapshot for a tenant
  CatalogContextBuilder    — product search, availability, price lookup
  OrderContextBuilder      — recent orders, status, customer order history
  ShippingContextBuilder   — shipping methods, zones, delivery estimates
  CustomerContextBuilder   — customer profile and purchase history
  PolicyContextBuilder     — return, payment, and support policies
  CouponContextBuilder     — active coupons and offer eligibility

Key function
  build_ai_context(db, tenant_id, query_context) → str
    — single entry point used by the AI orchestrator to assemble a context block
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

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
    StoreKnowledgeSnapshot,
    TenantSettings,
)

logger = logging.getLogger("nahla-backend")


# ─────────────────────────────────────────────────────────────────────────────
# StoreKnowledgeLoader — single access point for the snapshot
# ─────────────────────────────────────────────────────────────────────────────

class StoreKnowledgeLoader:
    """Load (and cache within a request) the StoreKnowledgeSnapshot for a tenant."""

    def __init__(self, db: Session, tenant_id: int):
        self.db        = db
        self.tenant_id = tenant_id
        self._snap: Optional[StoreKnowledgeSnapshot] = None

    def snapshot(self) -> Optional[StoreKnowledgeSnapshot]:
        if self._snap is None:
            self._snap = (
                self.db.query(StoreKnowledgeSnapshot)
                .filter_by(tenant_id=self.tenant_id)
                .first()
            )
        return self._snap

    def store_profile(self) -> Dict:
        snap = self.snapshot()
        return (snap.store_profile or {}) if snap else {}

    def catalog_summary(self) -> Dict:
        snap = self.snapshot()
        return (snap.catalog_summary or {}) if snap else {}

    def shipping_summary(self) -> Dict:
        snap = self.snapshot()
        return (snap.shipping_summary or {}) if snap else {}

    def policy_summary(self) -> Dict:
        snap = self.snapshot()
        return (snap.policy_summary or {}) if snap else {}

    def coupon_summary(self) -> Dict:
        snap = self.snapshot()
        return (snap.coupon_summary or {}) if snap else {}

    def is_fresh(self, max_age_hours: int = 6) -> bool:
        snap = self.snapshot()
        if not snap or not snap.last_full_sync_at:
            return False
        age = (datetime.now(timezone.utc) - snap.last_full_sync_at).total_seconds() / 3600
        return age < max_age_hours


# ─────────────────────────────────────────────────────────────────────────────
# CatalogContextBuilder
# ─────────────────────────────────────────────────────────────────────────────

class CatalogContextBuilder:
    """
    Answers questions about products.
    FACT RULE: only asserts prices, availability, and SKUs from synced DB data.
    """

    def __init__(self, db: Session, tenant_id: int):
        self.db        = db
        self.tenant_id = tenant_id

    def search_products(self, query: str, limit: int = 5) -> List[Dict]:
        """Full-text keyword search over synced products."""
        q = f"%{query.lower()}%"
        rows = (
            self.db.query(Product)
            .filter(
                Product.tenant_id == self.tenant_id,
                Product.title.ilike(q),
            )
            .limit(limit)
            .all()
        )
        return [self._format(p) for p in rows]

    def get_by_external_id(self, ext_id: str) -> Optional[Dict]:
        p = (
            self.db.query(Product)
            .filter_by(tenant_id=self.tenant_id, external_id=ext_id)
            .first()
        )
        return self._format(p) if p else None

    def get_top_products(self, limit: int = 10) -> List[Dict]:
        rows = (
            self.db.query(Product)
            .filter_by(tenant_id=self.tenant_id)
            .limit(limit)
            .all()
        )
        return [self._format(p) for p in rows]

    def check_availability(self, ext_id: str) -> Dict:
        """Return {'available': bool, 'stock_qty': int|None} from synced data."""
        p = (
            self.db.query(Product)
            .filter_by(tenant_id=self.tenant_id, external_id=ext_id)
            .first()
        )
        if not p:
            return {"available": None, "stock_qty": None, "source": "not_found"}
        meta = p.extra_metadata or {}
        return {
            "available":  meta.get("in_stock", True),
            "stock_qty":  meta.get("stock_qty"),
            "price":      p.price,
            "sale_price": meta.get("sale_price"),
            "source":     "synced",
        }

    def _format(self, p: Product) -> Dict:
        meta = p.extra_metadata or {}
        return {
            "id":          p.id,
            "external_id": p.external_id,
            "title":       p.title,
            "sku":         p.sku,
            "price":       p.price,
            "sale_price":  meta.get("sale_price"),
            "category":    meta.get("category", ""),
            "brand":       meta.get("brand", ""),
            "in_stock":    meta.get("in_stock", True),
            "stock_qty":   meta.get("stock_qty"),
            "image_url":   meta.get("image_url", ""),
        }

    def build_context_block(self, query: str = "") -> str:
        """Return a formatted text block for the AI prompt."""
        if query:
            products = self.search_products(query)
        else:
            products = self.get_top_products(10)

        if not products:
            return "لا توجد منتجات متاحة حالياً في قاعدة البيانات."

        lines = ["### المنتجات المتاحة (من البيانات الفعلية للمتجر):"]
        for p in products:
            price_str = f"{p['price']} ريال"
            if p.get("sale_price"):
                price_str = f"{p['sale_price']} ريال (بدلاً من {p['price']} ريال)"
            avail = "متوفر" if p.get("in_stock") else "غير متوفر"
            if p.get("stock_qty") is not None:
                avail += f" ({p['stock_qty']} قطعة)"
            lines.append(
                f"- {p['title']} | السعر: {price_str} | الحالة: {avail}"
                + (f" | التصنيف: {p['category']}" if p.get("category") else "")
            )
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# OrderContextBuilder
# ─────────────────────────────────────────────────────────────────────────────

class OrderContextBuilder:
    """Answers questions about order status and history."""

    def __init__(self, db: Session, tenant_id: int):
        self.db        = db
        self.tenant_id = tenant_id

    def get_customer_orders(self, customer_phone: str, limit: int = 5) -> List[Dict]:
        rows = (
            self.db.query(Order)
            .filter(
                Order.tenant_id == self.tenant_id,
                Order.customer_info["phone"].astext == customer_phone,
            )
            .order_by(Order.id.desc())
            .limit(limit)
            .all()
        )
        return [self._format(o) for o in rows]

    def get_by_external_id(self, ext_id: str) -> Optional[Dict]:
        o = (
            self.db.query(Order)
            .filter_by(tenant_id=self.tenant_id, external_id=ext_id)
            .first()
        )
        return self._format(o) if o else None

    def _format(self, o: Order) -> Dict:
        return {
            "id":           o.id,
            "external_id":  o.external_id,
            "status":       o.status,
            "total":        o.total,
            "is_abandoned": o.is_abandoned,
            "items_count":  len(o.line_items or []),
            "checkout_url": o.checkout_url,
        }

    def build_context_block(self, customer_phone: str = "") -> str:
        if not customer_phone:
            return ""
        orders = self.get_customer_orders(customer_phone)
        if not orders:
            return "لا توجد طلبات سابقة لهذا العميل."
        lines = ["### طلبات العميل الأخيرة (من بيانات المتجر):"]
        for o in orders:
            status_ar = {
                "pending": "قيد الانتظار", "processing": "قيد المعالجة",
                "shipped": "تم الشحن", "delivered": "تم التوصيل",
                "cancelled": "ملغي", "abandoned": "متروك",
            }.get(o["status"], o["status"])
            lines.append(
                f"- طلب #{o['external_id']} | الحالة: {status_ar} | المجموع: {o['total']} ريال"
            )
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# ShippingContextBuilder
# ─────────────────────────────────────────────────────────────────────────────

class ShippingContextBuilder:
    """Returns real shipping options stored in the snapshot."""

    def __init__(self, loader: StoreKnowledgeLoader):
        self.loader = loader

    def build_context_block(self) -> str:
        summary = self.loader.shipping_summary()
        if not summary:
            return ""
        methods = summary.get("methods", [])
        notes   = summary.get("notes", "")
        lines   = ["### معلومات الشحن والتوصيل (من إعدادات المتجر):"]
        if methods:
            for m in methods:
                if isinstance(m, dict):
                    name = m.get("name", "")
                    cost = m.get("cost", m.get("price", ""))
                    eta  = m.get("eta", m.get("delivery_days", ""))
                    lines.append(
                        f"- {name}"
                        + (f" | التكلفة: {cost}" if cost else "")
                        + (f" | المدة: {eta}" if eta else "")
                    )
                else:
                    lines.append(f"- {m}")
        if notes:
            lines.append(f"ملاحظات: {notes}")
        return "\n".join(lines) if len(lines) > 1 else ""


# ─────────────────────────────────────────────────────────────────────────────
# CustomerContextBuilder
# ─────────────────────────────────────────────────────────────────────────────

class CustomerContextBuilder:
    """Returns customer history and profile for personalised AI responses."""

    def __init__(self, db: Session, tenant_id: int):
        self.db        = db
        self.tenant_id = tenant_id

    def get_profile(self, phone: str) -> Optional[Dict]:
        customer = (
            self.db.query(Customer)
            .filter_by(tenant_id=self.tenant_id, phone=phone)
            .first()
        )
        if not customer:
            return None
        profile = (
            self.db.query(CustomerProfile)
            .filter_by(customer_id=customer.id)
            .first()
        )
        return {
            "name":           customer.name,
            "phone":          customer.phone,
            "total_orders":   profile.total_orders    if profile else 0,
            "total_spend":    profile.total_spend_sar  if profile else 0,
            "segment":        profile.segment          if profile else "new",
            "is_returning":   profile.is_returning     if profile else False,
            "churn_risk":     profile.churn_risk_score if profile else 0,
        }

    def build_context_block(self, phone: str) -> str:
        p = self.get_profile(phone)
        if not p:
            return "عميل جديد — لا يوجد سجل مشتريات سابق."
        segment_label = {
            "new": "جديد", "active": "نشط", "at_risk": "معرض للمغادرة",
            "churned": "خامل", "vip": "VIP",
        }.get(p["segment"], p["segment"])
        lines = [
            f"### معلومات العميل:",
            f"- الاسم: {p['name'] or 'غير معروف'}",
            f"- الشريحة: {segment_label}",
            f"- إجمالي الطلبات: {p['total_orders']}",
            f"- إجمالي الإنفاق: {p['total_spend']:.0f} ريال",
        ]
        if p["is_returning"]:
            lines.append("- عميل متكرر")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# CouponContextBuilder
# ─────────────────────────────────────────────────────────────────────────────

class CouponContextBuilder:
    """Returns active, non-expired coupons for AI to mention when appropriate."""

    def __init__(self, db: Session, tenant_id: int):
        self.db        = db
        self.tenant_id = tenant_id

    def get_active_coupons(self) -> List[Dict]:
        rows = (
            self.db.query(Coupon)
            .filter(
                Coupon.tenant_id == self.tenant_id,
                (Coupon.expires_at == None) | (Coupon.expires_at > datetime.now(timezone.utc)),  # noqa: E711
            )
            .limit(10)
            .all()
        )
        return [
            {
                "code":           r.code,
                "description":    r.description or "",
                "discount_type":  r.discount_type,
                "discount_value": r.discount_value,
                "expires_at":     r.expires_at.isoformat() if r.expires_at else None,
            }
            for r in rows
        ]

    def build_context_block(self) -> str:
        coupons = self.get_active_coupons()
        if not coupons:
            return ""
        lines = ["### الكوبونات والعروض الفعّالة حالياً (مؤكدة من قاعدة البيانات):"]
        for c in coupons:
            dtype = "خصم نسبي" if c["discount_type"] == "percentage" else "خصم ثابت"
            lines.append(
                f"- كود: {c['code']} | {dtype}: {c['discount_value']}"
                + (f" | ينتهي: {c['expires_at']}" if c.get("expires_at") else "")
            )
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# PolicyContextBuilder
# ─────────────────────────────────────────────────────────────────────────────

class PolicyContextBuilder:
    """Returns store policies for AI to cite accurately."""

    def __init__(self, loader: StoreKnowledgeLoader):
        self.loader = loader

    def build_context_block(self) -> str:
        policy = self.loader.policy_summary()
        if not policy:
            return ""
        lines = ["### سياسات المتجر:"]
        if policy.get("return_policy"):
            lines.append(f"- سياسة الإرجاع: {policy['return_policy']}")
        if policy.get("shipping_policy"):
            lines.append(f"- سياسة الشحن: {policy['shipping_policy']}")
        if policy.get("payment_methods"):
            methods = ", ".join(policy["payment_methods"]) if isinstance(policy["payment_methods"], list) else policy["payment_methods"]
            lines.append(f"- طرق الدفع: {methods}")
        if policy.get("support_hours"):
            lines.append(f"- ساعات الدعم: {policy['support_hours']}")
        return "\n".join(lines) if len(lines) > 1 else ""


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point — build_ai_context()
# ─────────────────────────────────────────────────────────────────────────────

def build_ai_context(
    db: Session,
    tenant_id: int,
    customer_phone: str = "",
    product_query: str  = "",
    include_sections: Optional[List[str]] = None,
) -> str:
    """
    Assemble a structured context string for the AI prompt.

    Sections available (pass None to include all):
      "store_profile", "catalog", "orders", "shipping", "coupons",
      "policies", "customer"

    FACT SAFETY GUARANTEE:
      Every fact in the returned string comes from a DB row or a synced snapshot.
      The AI must not add inventory, price, or coupon facts beyond what is here.
    """
    include = set(include_sections or ["store_profile", "catalog", "shipping", "coupons", "policies", "customer"])
    loader  = StoreKnowledgeLoader(db, tenant_id)
    parts: List[str] = []

    # 1. Store identity
    if "store_profile" in include:
        profile = loader.store_profile()
        if profile.get("store_name"):
            parts.append(
                f"### المتجر:\n"
                f"- الاسم: {profile['store_name']}\n"
                + (f"- الرابط: {profile['store_url']}\n" if profile.get("store_url") else "")
                + (f"- الوصف: {profile['description']}\n" if profile.get("description") else "")
                + (f"- للتواصل: {profile['contact_phone']}\n" if profile.get("contact_phone") else "")
            )

    # 2. Catalog
    if "catalog" in include:
        catalog_builder = CatalogContextBuilder(db, tenant_id)
        block = catalog_builder.build_context_block(product_query)
        if block:
            parts.append(block)

    # 3. Shipping
    if "shipping" in include:
        shipping_builder = ShippingContextBuilder(loader)
        block = shipping_builder.build_context_block()
        if block:
            parts.append(block)

    # 4. Coupons
    if "coupons" in include:
        coupon_builder = CouponContextBuilder(db, tenant_id)
        block = coupon_builder.build_context_block()
        if block:
            parts.append(block)

    # 5. Policies
    if "policies" in include:
        policy_builder = PolicyContextBuilder(loader)
        block = policy_builder.build_context_block()
        if block:
            parts.append(block)

    # 6. Customer history
    if "customer" in include and customer_phone:
        customer_builder = CustomerContextBuilder(db, tenant_id)
        block = customer_builder.build_context_block(customer_phone)
        if block:
            parts.append(block)

        order_builder = OrderContextBuilder(db, tenant_id)
        block = order_builder.build_context_block(customer_phone)
        if block:
            parts.append(block)

    # 7. Freshness warning
    if not loader.is_fresh():
        parts.append(
            "\n⚠️ تنبيه: بيانات المتجر قد تكون غير محدّثة. "
            "لا تؤكد أسعاراً أو توفراً دون التحقق عبر أداة المتجر."
        )

    return "\n\n".join(parts) if parts else "لا توجد بيانات متجر متاحة حالياً."
