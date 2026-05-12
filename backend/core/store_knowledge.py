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
import re as _re
import sys
from datetime import datetime, timezone
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

from .store_display import clean_store_name

logger = logging.getLogger("nahla-backend")

# ── Description sanitisation ──────────────────────────────────────────────────
_DESC_SCRIPT_RE  = _re.compile(r"<(script|style)[^>]*>.*?</\1>", _re.DOTALL | _re.IGNORECASE)
_DESC_TAG_RE     = _re.compile(r"<[^>]+>", _re.DOTALL)
_DESC_SPACE_RE   = _re.compile(r"\s+")


def _format_variants_for_llm(variants: list, max_items: int = 6) -> str:
    """Summarise in-stock variant names for the LLM context.

    Salla stores each combination (e.g. "S / أحمر", "M / أبيض") as a separate
    variant row.  We surface only the in-stock ones so the AI can answer
    "ما المقاسات المتاحة؟" without fabricating values.

    Returns a comma-separated string like "S, M, L" or "" when empty.
    """
    if not variants or not isinstance(variants, list):
        return ""
    seen: list = []
    for v in variants:
        if not isinstance(v, dict):
            continue
        name = (v.get("name") or v.get("title") or "").strip()
        if not name:
            continue
        available = v.get("available", True)
        qty = v.get("quantity") or v.get("stock_quantity")
        in_stock = bool(available) and (qty is None or int(qty or 0) > 0)
        if in_stock and name not in seen:
            seen.append(name)
        if len(seen) >= max_items:
            break
    return "، ".join(seen)


def _clean_description(raw: str, max_length: int = 200) -> str:
    """Strip HTML from a product description and return a plain-text summary.

    Removes script/style blocks, strips all tags, collapses whitespace, and
    truncates to *max_length* characters so descriptions don't bloat the LLM
    context window.  Returns '' when raw is falsy.
    """
    if not raw:
        return ""
    text = _DESC_SCRIPT_RE.sub(" ", str(raw))
    text = _DESC_TAG_RE.sub(" ", text)
    text = _DESC_SPACE_RE.sub(" ", text).strip()
    if len(text) > max_length:
        # Cut at the last space before the limit so we don't break mid-word
        cut = text[:max_length].rsplit(" ", 1)[0]
        return cut + "…"
    return text


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
        # `last_full_sync_at` is loaded from PostgreSQL as an offset-naive
        # datetime (TIMESTAMP WITHOUT TIME ZONE), but `datetime.now(timezone.utc)`
        # is offset-aware — subtracting them raises
        # `TypeError: can't subtract offset-naive and offset-aware datetimes`
        # which kills the entire merchant AI reply path. We treat every
        # naive timestamp from the DB as UTC (which is how every writer
        # in this codebase persists them) and normalise before the math.
        last_sync = snap.last_full_sync_at
        if last_sync.tzinfo is None:
            last_sync = last_sync.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - last_sync).total_seconds() / 3600
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

    def search_products(self, query: str, limit: int = 10) -> List[Dict]:
        """
        Search synced products by keyword.

        Strategy (priority order):
          1. PostgreSQL full-text search on title + description (supports Arabic)
          2. ILIKE fallback on title for very short queries or FTS failures
        Only returns **orderable** products (has external_id + in_stock).
        """
        from sqlalchemy import text as sa_text  # noqa: PLC0415
        q_clean = query.strip()
        if not q_clean:
            return []

        # -- Full-text search (tsvector, works with Arabic tokens) ------------
        _results: List[Dict] = []
        try:
            fts_sql = sa_text("""
                SELECT id FROM products
                WHERE tenant_id = :tid
                  AND to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(description,''))
                      @@ plainto_tsquery('simple', :q)
                ORDER BY in_stock DESC, id
                LIMIT :lim
            """)
            result = self.db.execute(fts_sql, {"tid": self.tenant_id, "q": q_clean, "lim": limit})
            ids = [row[0] for row in result]
            if ids:
                rows = (
                    self.db.query(Product)
                    .filter(Product.id.in_(ids))
                    .all()
                )
                _raw_count = len(rows)
                _results = self._filter_orderable(rows, source="search")
                logger.info(
                    "[CATALOG SEARCH] tenant=%s query=%r method=fts "
                    "db_rows=%d returned=%d filtered_unsynced=%d",
                    self.tenant_id, q_clean, _raw_count, len(_results),
                    _raw_count - len(_results),
                )
                return _results
        except Exception:
            pass  # fall through to ILIKE

        # -- ILIKE fallback ---------------------------------------------------
        q_like = f"%{q_clean.lower()}%"
        rows = (
            self.db.query(Product)
            .filter(
                Product.tenant_id == self.tenant_id,
                Product.title.ilike(q_like),
            )
            .order_by(Product.in_stock.desc())
            .limit(limit)
            .all()
        )
        _raw_count = len(rows)
        _results = self._filter_orderable(rows, source="search_ilike")
        logger.info(
            "[CATALOG SEARCH] tenant=%s query=%r method=ilike "
            "db_rows=%d returned=%d filtered_unsynced=%d",
            self.tenant_id, q_clean, _raw_count, len(_results),
            _raw_count - len(_results),
        )
        return _results

    def get_by_external_id(self, ext_id: str) -> Optional[Dict]:
        p = (
            self.db.query(Product)
            .filter_by(tenant_id=self.tenant_id, external_id=ext_id)
            .first()
        )
        return self._format(p) if p else None

    def get_top_products(self, limit: int = 25) -> List[Dict]:
        """Return top **orderable** products."""
        rows = (
            self.db.query(Product)
            .filter_by(tenant_id=self.tenant_id)
            .order_by(Product.in_stock.desc(), Product.id)
            .limit(limit + 20)  # fetch extra to compensate for filtered-out rows
            .all()
        )
        _raw_count = len(rows)
        _results = self._filter_orderable(rows, source="top_products")[:limit]
        _unsynced = sum(
            1 for p in rows
            if not str(getattr(p, "external_id", "") or "").strip()
        )
        logger.info(
            "[CATALOG SEARCH] tenant=%s query='(top_products)' method=top "
            "db_rows=%d returned=%d filtered_unsynced=%d",
            self.tenant_id, _raw_count, len(_results), _unsynced,
        )
        return _results

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
        ext_id = (p.external_id or "").strip()
        stock_qty = meta.get("stock_qty", p.stock_quantity)
        in_stock_flag = meta.get("in_stock", p.in_stock)
        status = str(meta.get("status", "active") or "active").lower()

        # Variant awareness: if variants are synced, at least one must be
        # in-stock for the parent product to be orderable.
        variants = meta.get("variants") or []
        variants_ok = True
        if variants:
            variants_ok = any(
                _safe_int(v.get("stock_quantity") or v.get("quantity") or 0, 0) > 0
                for v in variants
                if isinstance(v, dict)
            )

        # ── Single source of truth for orderability ──────────────────────
        # `can_checkout` is the ONE authoritative flag that decides whether
        # a product may be shown in a numbered list AND accepted when the
        # customer picks it by number.  Every downstream check (decision
        # engine, order executor, debug endpoint) MUST read this field
        # instead of re-computing the logic.
        variants_in_stock = sum(
            1 for v in variants
            if isinstance(v, dict)
            and _safe_int(v.get("stock_quantity") or v.get("quantity") or 0, 0) > 0
        ) if variants else 0
        can_checkout = (
            bool(ext_id)
            and status == "active"
            and bool(in_stock_flag)
            and (stock_qty is None or _safe_int(stock_qty, 0) > 0)
            and variants_ok
        )
        # `orderable` is kept as an alias so existing code that reads it
        # continues to work without modification.
        orderable = can_checkout
        return {
            "id":              p.id,
            "external_id":     ext_id or None,
            "title":           p.title,
            "sku":             p.sku,
            "description":     _clean_description(p.description or meta.get("description", "")),
            "price":           p.price,
            "sale_price":      meta.get("sale_price"),
            "category":        meta.get("category", ""),
            "brand":           meta.get("brand", ""),
            "in_stock":        in_stock_flag,
            "stock_qty":       stock_qty,
            "image_url":       meta.get("image_url", ""),
            "orderable":       orderable,
            "can_checkout":    can_checkout,
            "status":          status,
            "variants_in_stock": variants_in_stock,
            # Variant/option names (e.g. "S, M, L" or "أحمر، أزرق") —
            # only in-stock combinations, max 6 entries.
            "variants_summary": _format_variants_for_llm(variants),
        }

    def _filter_orderable(
        self, rows: List[Product], *, source: str = ""
    ) -> List[Dict]:
        """Format product rows and drop non-orderable ones.

        Every product is logged with a [CATALOG] line for diagnostics.
        Products that pass are assigned a 1-based display_index matching
        the numbered list shown to the customer.
        """
        result: List[Dict] = []
        display_index = 0
        for p in rows:
            fmt = self._format(p)
            if fmt["can_checkout"]:
                display_index += 1
                fmt["display_index"] = display_index
                logger.info(
                    "[CATALOG] product listed | index=%d source=%s name=%r "
                    "external_id=%s stock_qty=%s in_stock=%s status=%s "
                    "can_checkout=True variants_in_stock=%s",
                    display_index, source, fmt["title"],
                    fmt["external_id"], fmt["stock_qty"],
                    fmt["in_stock"], fmt["status"],
                    fmt.get("variants_in_stock", 0),
                )
                result.append(fmt)
            else:
                # Log at WARNING so it surfaces in Railway/production logs
                # without needing debug level.
                logger.warning(
                    "[CATALOG] product SKIPPED (can_checkout=False) | "
                    "source=%s name=%r external_id=%s stock_qty=%s "
                    "in_stock=%s status=%s has_external_id=%s variants_in_stock=%s",
                    source, fmt["title"], fmt["external_id"],
                    fmt["stock_qty"], fmt["in_stock"], fmt["status"],
                    bool(fmt["external_id"]), fmt.get("variants_in_stock", 0),
                )
        return result

    def build_context_block(self, query: str = "") -> str:
        """Return a formatted text block for the AI prompt."""
        if query:
            products = self.search_products(query)
            # Keyword search matched nothing — show top products so the AI
            # always has catalogue context regardless of the query term.
            if not products:
                products = self.get_top_products(25)
        else:
            products = self.get_top_products(25)

        if not products:
            # Explicit instruction to the model — do NOT reveal coupons or
            # offer any discount when there is nothing to sell.
            return (
                "لا توجد منتجات مزامنة حالياً في قاعدة البيانات.\n"
                "تعليمات صارمة: لا تذكر أي كوبونات أو عروض للعميل.\n"
                "اعتذر بأدب وأخبر العميل أن المتجر سيتواصل معه قريباً."
            )

        lines = ["### المنتجات المتاحة للبيع (متوفرة فعلياً في المخزون):"]
        for p in products:
            price_str = f"{p['price']} ريال"
            if p.get("sale_price"):
                price_str = f"{p['sale_price']} ريال (بدلاً من {p['price']} ريال)"
            stock_str = ""
            if p.get("stock_qty") is not None:
                stock_str = f" ({p['stock_qty']} قطعة)"
            line = (
                f"- {p['title']} | السعر: {price_str} | متوفر{stock_str}"
                + (f" | التصنيف: {p['category']}" if p.get("category") else "")
            )
            if p.get("description"):
                line += f"\n  الوصف: {p['description']}"
            if p.get("variants_summary"):
                line += f"\n  الخيارات المتاحة: {p['variants_summary']}"
            lines.append(line)
        lines.append(
            "\nتنبيه: جميع المنتجات أعلاه تم التحقق من توفرها في المخزون."
            " لا تعرض أي منتج غير مذكور في هذه القائمة."
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
            "segment":        (profile.customer_status if profile and getattr(profile, "customer_status", None) else profile.segment if profile else "lead"),
            "rfm_segment":    (getattr(profile, "rfm_segment", None) if profile else None) or "lead",
            "is_returning":   profile.is_returning     if profile else False,
            "churn_risk":     profile.churn_risk_score if profile else 0,
        }

    def build_context_block(self, phone: str) -> str:
        p = self.get_profile(phone)
        if not p:
            return "عميل جديد — لا يوجد سجل مشتريات سابق."
        segment_label = {
            "lead": "محتمل",
            "new": "جديد",
            "active": "نشط",
            "at_risk": "معرض للمغادرة",
            "inactive": "غير نشط",
            "vip": "VIP",
        }.get(p["segment"], p["segment"])
        lines = [
            f"### معلومات العميل:",
            f"- الاسم: {p['name'] or 'غير معروف'}",
            f"- الشريحة: {segment_label}",
            f"- إجمالي الطلبات: {p['total_orders']}",
            f"- إجمالي الإنفاق: {p['total_spend']:.0f} ريال",
            f"- قطاع RFM: {p['rfm_segment']}",
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
# Main entry points — build_merchant_context() / build_ai_context()
# ─────────────────────────────────────────────────────────────────────────────

def build_merchant_context(
    db: Session,
    tenant_id: int,
    customer_phone: str = "",
    product_query: str = "",
    state: Optional[Any] = None,
    history: Optional[List[Dict[str, Any]]] = None,
    profile: Optional[Dict[str, Any]] = None,
    product_limit: int = 8,
) -> Dict[str, Any]:
    """Return a structured, fact-grounded merchant context for Brain/LLM use.

    This is intentionally built from existing sources only: store snapshots,
    synced products, customers, conversation state, and tenant settings JSON.
    No editable fact is invented here.
    """
    loader = StoreKnowledgeLoader(db, tenant_id)
    catalog = CatalogContextBuilder(db, tenant_id)
    customer_builder = CustomerContextBuilder(db, tenant_id)
    settings = (
        db.query(TenantSettings)
        .filter(TenantSettings.tenant_id == tenant_id)
        .first()
    )
    ai_settings = dict((settings.ai_settings if settings else None) or {})
    store_settings = dict((settings.store_settings if settings else None) or {})
    snap = loader.snapshot()

    # ── Context verbosity — A/B knob readable from ai_settings ───────────────
    # "compact" : fewer products, shorter policies, no suggested FAQ  (variant A)
    # "full"    : default, complete context                           (variant B)
    context_verbosity: str = str(ai_settings.get("context_verbosity") or "full").lower()
    is_compact = context_verbosity == "compact"

    # Bounded catalog limit — compact caps at 5, full keeps caller's choice.
    effective_limit = max(1, min(
        (5 if is_compact else product_limit)
        if isinstance(product_limit, int) and product_limit > 0
        else (5 if is_compact else 8),
        50,
    ))

    store_profile = dict(loader.store_profile() or {})
    if store_settings.get("store_name") and not store_profile.get("store_name"):
        store_profile["store_name"] = store_settings.get("store_name")
    if store_settings.get("store_url") and not store_profile.get("store_url"):
        store_profile["store_url"] = store_settings.get("store_url")
    if store_profile.get("store_name"):
        store_profile["store_name"] = clean_store_name(str(store_profile["store_name"]))

    # Match build_context_block: if search finds nothing, still surface orderable top products.
    pq = (product_query or "").strip()
    if pq:
        products = catalog.search_products(pq, limit=effective_limit)
        if not products:
            products = catalog.get_top_products(effective_limit)
    else:
        products = catalog.get_top_products(effective_limit)

    # Insights: one cheap COUNT + a small bounded sample for _format()-accurate orderable stats.
    # (Full-table Python _format loops are too heavy for large catalogs.)
    from sqlalchemy import func as sa_func  # noqa: PLC0415
    from sqlalchemy import or_ as sa_or_  # noqa: PLC0415

    total_product_count = int(
        db.query(sa_func.count(Product.id))
        .filter(Product.tenant_id == tenant_id)
        .scalar()
        or 0
    )
    insight_scan_limit = min(150, total_product_count)
    product_rows = (
        db.query(Product)
        .filter_by(tenant_id=tenant_id)
        .order_by(Product.in_stock.desc(), Product.id)
        .limit(insight_scan_limit)
        .all()
    )
    formatted_rows = [catalog._format(p) for p in product_rows]  # noqa: SLF001 - same module helper
    unavailable_count = sum(1 for p in formatted_rows if not p.get("orderable"))
    without_description_count = int(
        db.query(sa_func.count(Product.id))
        .filter(
            Product.tenant_id == tenant_id,
            sa_or_(
                Product.description.is_(None),
                sa_func.trim(Product.description) == "",
            ),
        )
        .scalar()
        or 0
    )

    policy_summary = dict(loader.policy_summary() or {})
    shipping_summary = dict(loader.shipping_summary() or {})
    policies = {
        "shipping_policy": (
            store_settings.get("shipping_policy")
            or policy_summary.get("shipping_policy")
            or shipping_summary.get("notes")
            or ""
        ),
        "payment_policy": store_settings.get("payment_policy") or "",
        "return_policy": store_settings.get("return_policy") or policy_summary.get("return_policy") or "",
        "warranty_policy": store_settings.get("warranty_policy") or "",
        "delivery_areas": store_settings.get("delivery_areas") or shipping_summary.get("delivery_areas") or "",
        "working_hours": (
            store_settings.get("working_hours")
            or policy_summary.get("support_hours")
            or store_profile.get("business_hours")
            or ""
        ),
        "payment_methods": policy_summary.get("payment_methods") or [],
        "shipping_methods": shipping_summary.get("methods") or [],
    }
    policy_presence = {
        key: bool(value)
        for key, value in policies.items()
        if key not in ("payment_methods", "shipping_methods")
    }

    customer_profile = {}
    if customer_phone:
        customer_profile = customer_builder.get_profile(customer_phone) or {
            "phone": customer_phone,
            "segment": "lead",
            "is_returning": False,
        }
    if profile:
        customer_profile = {**customer_profile, **profile}

    conversation = {
        "stage": getattr(state, "stage", ""),
        "customer_goal": getattr(state, "customer_goal", ""),
        "selected_product": getattr(state, "current_product_focus", None),
        "last_recommended_products": list(getattr(state, "last_recommended_products", []) or []),
        "conversation_summary": getattr(state, "conversation_summary", ""),
        "recent_messages": list((history or [])[-10:]),
    }

    approved_faq = list(store_settings.get("faq_approved") or ai_settings.get("faq_approved") or [])
    # compact mode skips suggested FAQ to shrink context size
    suggested_faq = (
        []
        if is_compact
        else list(store_settings.get("faq_suggested") or ai_settings.get("faq_suggested") or [])
    )
    # ── Policy rule values — read from ai_settings, surfaced to PolicyGate ──
    _cap_hours_raw = ai_settings.get("coupon_cap_hours", 24)
    _escalate_raw  = ai_settings.get("auto_escalate_after_n", 3)
    _max_order_raw = ai_settings.get("max_order_value", 0)
    # blocked_customers: list of phone numbers stored in store_settings (not ai_settings)
    _blocked_raw = store_settings.get("blocked_customers") or []
    _blocked_list: list = [str(p).strip() for p in _blocked_raw if p] if isinstance(_blocked_raw, list) else []

    # ── Autopilot mode (read once, default False) ─────────────────────────
    #
    # The merchant-curated libraries (manual coupons + AI media) are part of
    # *store intelligence*, not *automation*, so they MUST NOT be gated on
    # this flag. We surface it here purely so the prompt builder can adjust
    # priority guidance: when autopilot is ON, GPT prefers the automatic
    # coupon engine and treats manual coupons as a fallback; when autopilot
    # is OFF, manual coupons become the primary discount source the brain
    # may cite. The libraries themselves are loaded unconditionally below.
    try:
        from core.automation_engine import _is_autopilot_enabled  # noqa: PLC0415
        autopilot_enabled = bool(_is_autopilot_enabled(db, tenant_id))
    except Exception as _ap_exc:  # pragma: no cover — defensive
        logger.warning(
            "[MerchantContext] autopilot flag read failed tenant=%s err=%s",
            tenant_id, _ap_exc,
        )
        autopilot_enabled = False

    brain_profile = {
        "tone": ai_settings.get("reply_tone", "friendly"),
        "reply_length": ai_settings.get("reply_length", "medium"),
        "sales_style": ai_settings.get("sales_style", "consultative"),
        "coupon_strategy": ai_settings.get("coupon_strategy", "on_hesitation"),
        "emoji_usage": ai_settings.get("emoji_usage", "moderate"),
        "upsell_enabled": bool(ai_settings.get("upsell_enabled", ai_settings.get("recommendations_enabled", True))),
        "recommendations_enabled": bool(ai_settings.get("recommendations_enabled", True)),
        "owner_instructions": ai_settings.get("owner_instructions", ""),
        "coupon_rules": ai_settings.get("coupon_rules", ""),
        # Merchant-configurable policy knobs (Phase 11)
        "coupon_cap_hours": max(1, int(_cap_hours_raw)) if str(_cap_hours_raw).isdigit() or isinstance(_cap_hours_raw, (int, float)) else 24,
        "auto_escalate_after_n": max(1, int(_escalate_raw)) if str(_escalate_raw).isdigit() or isinstance(_escalate_raw, (int, float)) else 3,
        # Strict opt-in: PolicyGate's auto-escalate fires only when this is
        # explicitly True. Default False so a streak of GENERAL turns
        # (small talk, jokes, unusual product questions) never silently
        # promotes a conversation to handoff.
        "auto_escalate_enabled": bool(ai_settings.get("auto_escalate_enabled", False)),
        "max_order_value": float(_max_order_raw) if _max_order_raw and float(_max_order_raw) > 0 else None,
        "context_verbosity": context_verbosity,
        # Block list — customer phone numbers the merchant has flagged (Phase 12)
        "blocked_customers": _blocked_list,
        # Autopilot master switch — surfaced to the prompt builder so the
        # brain can prefer automatic coupons when ON and lean on the
        # merchant-curated manual_coupons list when OFF. The libraries
        # themselves are NEVER gated on this flag (see comment above).
        "autopilot_enabled": autopilot_enabled,
    }

    # Pages — synced from Salla via StoreSyncService.sync_pages() which writes to
    # store_settings["pages"] and snapshots them into StoreKnowledgeSnapshot.store_profile["pages"].
    # Primary source: store_settings (written directly by sync_pages after each full sync).
    # Fallback: snapshot store_profile (populated by _rebuild_snapshot, same underlying data).
    # Both are empty until the first full sync completes successfully.
    pages: List[Dict[str, Any]] = (
        list(store_settings.get("pages") or [])
        or list((loader.store_profile() or {}).get("pages") or [])
    )

    orderable_count = sum(1 for p in formatted_rows if p.get("orderable"))
    excluded_count = unavailable_count
    policies_count = sum(1 for v in policy_presence.values() if v)
    payment_methods_count = len(policies.get("payment_methods") or [])
    shipping_methods_count = len(policies.get("shipping_methods") or [])
    faq_count = len(approved_faq)
    pages_count = len(pages)

    insights = {
        "product_count": total_product_count,
        "orderable_count": orderable_count,
        "unavailable_count": excluded_count,
        "without_description_count": without_description_count,
        "last_sync_at": (
            snap.last_full_sync_at.isoformat()
            if snap and getattr(snap, "last_full_sync_at", None)
            else None
        ),
        "knowledge_fresh": loader.is_fresh(),
    }

    logger.info(
        "[MerchantContext] tenant=%s orderable=%d excluded=%d policies=%d "
        "payment_methods=%d shipping_methods=%d faq=%d pages=%d fresh=%s verbosity=%s",
        tenant_id,
        orderable_count,
        excluded_count,
        policies_count,
        payment_methods_count,
        shipping_methods_count,
        faq_count,
        pages_count,
        loader.is_fresh(),
        context_verbosity,
    )

    # Merchant-curated libraries (independent of Salla / automatic
    # coupons / autopilot) — surface the active rows so the brain can
    # cite a coupon code verbatim and attach a media file to its reply
    # via the ``[MEDIA:<id>]`` marker convention defined in
    # ``core.ai_libraries.extract_media_markers``.
    #
    # CONTRACT: this block runs unconditionally. The autopilot flag
    # above only changes prompt *priority guidance*, never visibility.
    # A merchant who sells manually over WhatsApp (autopilot=False, no
    # Salla, no automatic coupon engine) MUST still be able to ship
    # manual coupons and media library items through GPT.
    try:
        from core.ai_libraries import (  # noqa: PLC0415
            list_active_manual_coupons,
            list_active_ai_media,
        )
        # Build a small relevance query from the customer's last turn
        # so the cap (10 coupons / 15 media) tends to surface the most
        # contextually relevant rows when the merchant has many.
        _rel_query_parts: List[str] = []
        if product_query:
            _rel_query_parts.append(str(product_query))
        if history:
            for _msg in reversed(history):
                if not isinstance(_msg, dict):
                    continue
                if (_msg.get("role") or _msg.get("direction") or "").lower() in (
                    "user", "customer", "inbound",
                ):
                    _content = _msg.get("content") or _msg.get("text") or ""
                    if _content:
                        _rel_query_parts.append(str(_content))
                        break
        _relevance_query = " ".join(_rel_query_parts).strip() or None

        manual_coupons_active = list_active_manual_coupons(
            db, tenant_id, relevance_query=_relevance_query,
        )
        ai_media_active = list_active_ai_media(
            db, tenant_id, relevance_query=_relevance_query,
        )
    except Exception as _lib_exc:  # pragma: no cover — defensive
        logger.warning(
            "[MerchantContext] libraries fetch failed tenant=%s err=%s",
            tenant_id, _lib_exc,
        )
        manual_coupons_active = []
        ai_media_active = []

    return {
        "tenant_profile": store_profile,
        "customer": customer_profile,
        "conversation": conversation,
        "products": products,
        "policies": policies,
        "policy_presence": policy_presence,
        "faq": {
            "approved": approved_faq,
            "suggested": suggested_faq,
            "approved_only": True,
        },
        "pages": pages,
        "insights": insights,
        "brain_profile": brain_profile,
        "manual_coupons": manual_coupons_active,
        "ai_media_library": ai_media_active,
        "retrieval_rules": {
            "products_are_orderable_only": True,
            "do_not_invent_missing_policies": True,
            "faq_suggested_requires_approval": True,
            "short_whatsapp_reply": True,
            "manual_coupons_only_from_list": True,
            "media_attach_via_marker": True,
        },
        "context_verbosity": context_verbosity,
    }


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
        disp = clean_store_name(str(profile.get("store_name") or ""))
        if disp:
            parts.append(
                f"### المتجر:\n"
                f"- الاسم: {disp}\n"
                + (f"- الرابط: {profile['store_url']}\n" if profile.get("store_url") else "")
                + (f"- الوصف: {profile['description']}\n" if profile.get("description") else "")
                + (f"- للتواصل: {profile['contact_phone']}\n" if profile.get("contact_phone") else "")
            )

    # 2. Catalog — track whether real products exist
    catalog_has_products = False
    if "catalog" in include:
        catalog_builder = CatalogContextBuilder(db, tenant_id)
        block = catalog_builder.build_context_block(product_query)
        if block:
            parts.append(block)
            # A block without the "no products" sentinel means real rows were found
            catalog_has_products = "تعليمات صارمة" not in block

    # 3. Shipping
    if "shipping" in include:
        shipping_builder = ShippingContextBuilder(loader)
        block = shipping_builder.build_context_block()
        if block:
            parts.append(block)

    # 4. Coupons — ONLY when products are available.
    # Without products there is nothing to apply a discount to; exposing the
    # coupon list without a matching catalogue causes the AI to use the codes
    # as a "consolation prize", which is confusing and undesirable.
    if "coupons" in include and catalog_has_products:
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


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default
