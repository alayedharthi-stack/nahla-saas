"""
CustomerMemoryLoader
────────────────────
Loads all customer intelligence records for a single customer in one pass.
Returns a flat dict that PromptBuilder and PolicyGuard can consume directly.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from database.models import (
    Coupon,
    ConversationHistorySummary,
    Customer,
    CustomerPreferences,
    CustomerProfile,
    KnowledgePolicy,
    Order,
    Product,
    ProductAffinity,
    PriceSensitivityScore,
    Tenant,
    TenantSettings,
    WhatsAppNumber,
)
from database.session import SessionLocal

logger = logging.getLogger("ai-orchestrator.memory")


def load_customer_memory(
    tenant_id: int,
    customer_phone: str,
) -> Dict[str, Any]:
    """
    Load everything the orchestrator needs to personalise a response:
      - customer record (or create stub if first contact)
      - profile, preferences, price sensitivity, conversation summary
      - top-affinity products (up to 10)
      - store context: products, active coupons, policy, branding
    """
    db = SessionLocal()
    try:
        # ── Tenant ────────────────────────────────────────────────────────────
        tenant: Optional[Tenant] = db.query(Tenant).filter(
            Tenant.id == tenant_id, Tenant.is_active == True
        ).first()

        if not tenant:
            return _empty_context()

        # ── Resolve or stub customer ──────────────────────────────────────────
        customer: Optional[Customer] = db.query(Customer).filter(
            Customer.tenant_id == tenant_id,
            Customer.phone == customer_phone,
        ).first()

        if not customer:
            # First time this number contacts the store — return minimal context
            return _new_customer_context(tenant, customer_phone, db)

        customer_id = customer.id

        # ── Customer intelligence records ─────────────────────────────────────
        profile: Optional[CustomerProfile] = db.query(CustomerProfile).filter(
            CustomerProfile.customer_id == customer_id,
            CustomerProfile.tenant_id == tenant_id,
        ).first()

        prefs: Optional[CustomerPreferences] = db.query(CustomerPreferences).filter(
            CustomerPreferences.customer_id == customer_id,
            CustomerPreferences.tenant_id == tenant_id,
        ).first()

        sensitivity: Optional[PriceSensitivityScore] = db.query(PriceSensitivityScore).filter(
            PriceSensitivityScore.customer_id == customer_id,
            PriceSensitivityScore.tenant_id == tenant_id,
        ).first()

        history: Optional[ConversationHistorySummary] = db.query(ConversationHistorySummary).filter(
            ConversationHistorySummary.customer_id == customer_id,
            ConversationHistorySummary.tenant_id == tenant_id,
        ).first()

        # ── Top-affinity products for this customer ───────────────────────────
        affinity_rows: List[ProductAffinity] = (
            db.query(ProductAffinity)
            .filter(
                ProductAffinity.customer_id == customer_id,
                ProductAffinity.tenant_id == tenant_id,
            )
            .order_by(ProductAffinity.affinity_score.desc())
            .limit(10)
            .all()
        )
        affinity_product_ids = [r.product_id for r in affinity_rows]

        # ── Recent orders (last 5) ─────────────────────────────────────────────
        recent_orders: List[Order] = (
            db.query(Order)
            .filter(Order.tenant_id == tenant_id)
            .filter(Order.customer_info.op("->>")(  # JSONB field lookup
                "phone"
            ) == customer_phone)
            .order_by(Order.id.desc())
            .limit(5)
            .all()
        )

        # ── Store products — in-stock first, then by affinity ─────────────────
        all_products: List[Product] = (
            db.query(Product)
            .filter(Product.tenant_id == tenant_id)
            .order_by(Product.in_stock.desc(), Product.id)
            .limit(50)
            .all()
        )

        # Annotate each product with this customer's affinity score
        affinity_map = {r.product_id: r.affinity_score for r in affinity_rows}
        product_lines = [
            {
                "id": p.id,
                "title": p.title,
                "price_sar": p.price,
                "sku": p.sku,
                "in_stock": p.in_stock,
                "affinity_score": affinity_map.get(p.id, 0.0),
                "tags": p.recommendation_tags or [],
            }
            for p in all_products
        ]
        # Sort: high-affinity products first, then in-stock
        product_lines.sort(
            key=lambda x: (x["affinity_score"], 1 if x.get("in_stock") else 0),
            reverse=True,
        )

        # ── Active coupons ────────────────────────────────────────────────────
        coupons: List[Coupon] = (
            db.query(Coupon)
            .filter(Coupon.tenant_id == tenant_id)
            .filter(
                (Coupon.expires_at == None) | (Coupon.expires_at > datetime.utcnow())
            )
            .limit(10)
            .all()
        )
        coupon_lines = [
            {
                "code": c.code,
                "discount_type": c.discount_type,
                "discount_value": c.discount_value,
                "description": c.description,
            }
            for c in coupons
        ]

        # ── Store policy ──────────────────────────────────────────────────────
        knowledge_policy: Optional[KnowledgePolicy] = (
            db.query(KnowledgePolicy).filter(KnowledgePolicy.tenant_id == tenant_id).first()
        )
        settings: Optional[TenantSettings] = (
            db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).first()
        )

        return {
            # Store
            "store_name": tenant.name,
            "store_address": tenant.store_address or "",
            "coupon_policy": tenant.coupon_policy or {},
            "recommendation_controls": tenant.recommendation_controls or {},
            "branding": (settings.branding_text if settings else "Powered by Nahla"),
            # Knowledge policy
            "allowed_categories": (knowledge_policy.allowed_categories or []) if knowledge_policy else [],
            "blocked_categories": (knowledge_policy.blocked_categories or []) if knowledge_policy else [],
            "escalation_rules": (knowledge_policy.escalation_rules or {}) if knowledge_policy else {},
            # Customer
            "customer_id": customer_id,
            "customer_name": customer.name or "",
            "customer_phone": customer_phone,
            "is_returning": profile.is_returning if profile else False,
            "segment": profile.segment if profile else "new",
            "preferred_language": (prefs.language if prefs else (profile.preferred_language if profile else "ar")),
            "communication_style": prefs.communication_style if prefs else "neutral",
            # Profile summary
            "total_orders": profile.total_orders if profile else 0,
            "total_spend_sar": profile.total_spend_sar if profile else 0.0,
            "avg_order_value_sar": profile.average_order_value_sar if profile else 0.0,
            "last_order_at": profile.last_order_at.isoformat() if (profile and profile.last_order_at) else None,
            # Preferences
            "preferred_categories": prefs.preferred_categories if prefs else [],
            "preferred_brands": prefs.preferred_brands if prefs else [],
            "price_range": {
                "min": prefs.price_range_min_sar if prefs else None,
                "max": prefs.price_range_max_sar if prefs else None,
            },
            "preferred_payment": prefs.preferred_payment_method if prefs else None,
            "preferred_delivery": prefs.preferred_delivery_type if prefs else None,
            # Price sensitivity
            "price_sensitivity_score": sensitivity.score if sensitivity else 0.5,
            "recommended_discount_pct": sensitivity.recommended_discount_pct if sensitivity else 0,
            "coupon_usage_rate": sensitivity.coupon_usage_rate if sensitivity else 0.0,
            # Conversation history
            "history_summary": history.summary_text if history else "",
            "past_topics": history.topics_discussed if history else [],
            "past_products_mentioned": history.products_mentioned if history else [],
            "last_intent": history.last_intent if history else None,
            "sentiment": history.sentiment if history else "neutral",
            "escalation_count": history.escalation_count if history else 0,
            # Products and catalog
            "products": product_lines[:30],   # top 30 after affinity sort
            "high_affinity_product_ids": affinity_product_ids[:5],
            # Coupons are only passed to the prompt when products exist.
            # Without a catalogue the model uses coupons as a "consolation
            # prize" — confusing and commercially harmful.
            "coupons": coupon_lines if product_lines else [],
            # Recent order context
            "recent_orders": [
                {
                    "status": o.status,
                    "total": o.total,
                    "items": o.line_items,
                }
                for o in recent_orders
            ],
        }

    finally:
        db.close()


def _new_customer_context(tenant: Tenant, phone: str, db) -> Dict[str, Any]:
    """Minimal context returned for a customer making first contact."""
    products = db.query(Product).filter(Product.tenant_id == tenant.id).limit(30).all()
    coupons = db.query(Coupon).filter(
        Coupon.tenant_id == tenant.id,
        (Coupon.expires_at == None) | (Coupon.expires_at > datetime.utcnow()),
    ).limit(10).all()
    knowledge_policy = db.query(KnowledgePolicy).filter(
        KnowledgePolicy.tenant_id == tenant.id
    ).first()

    return {
        "store_name": tenant.name,
        "store_address": tenant.store_address or "",
        "coupon_policy": tenant.coupon_policy or {},
        "recommendation_controls": tenant.recommendation_controls or {},
        "branding": "Powered by Nahla",
        "allowed_categories": (knowledge_policy.allowed_categories or []) if knowledge_policy else [],
        "blocked_categories": (knowledge_policy.blocked_categories or []) if knowledge_policy else [],
        "escalation_rules": (knowledge_policy.escalation_rules or {}) if knowledge_policy else {},
        "customer_id": None,
        "customer_name": "",
        "customer_phone": phone,
        "is_returning": False,
        "segment": "new",
        "preferred_language": "ar",
        "communication_style": "neutral",
        "total_orders": 0,
        "total_spend_sar": 0.0,
        "avg_order_value_sar": 0.0,
        "last_order_at": None,
        "preferred_categories": [],
        "preferred_brands": [],
        "price_range": {"min": None, "max": None},
        "preferred_payment": None,
        "preferred_delivery": None,
        "price_sensitivity_score": 0.5,
        "recommended_discount_pct": 0,
        "coupon_usage_rate": 0.0,
        "history_summary": "",
        "past_topics": [],
        "past_products_mentioned": [],
        "last_intent": None,
        "sentiment": "neutral",
        "escalation_count": 0,
        "products": [
            {"id": p.id, "title": p.title, "price_sar": p.price, "sku": p.sku,
             "affinity_score": 0.0, "tags": p.recommendation_tags or []}
            for p in products
        ],
        "high_affinity_product_ids": [],
        "coupons": [
            {"code": c.code, "discount_type": c.discount_type,
             "discount_value": c.discount_value, "description": c.description}
            for c in coupons
        ],
        "recent_orders": [],
    }


def _empty_context() -> Dict[str, Any]:
    return {
        "store_name": "our store", "store_address": "",
        "coupon_policy": {}, "recommendation_controls": {}, "branding": "",
        "allowed_categories": [], "blocked_categories": [], "escalation_rules": {},
        "customer_id": None, "customer_name": "", "customer_phone": "",
        "is_returning": False, "segment": "new", "preferred_language": "ar",
        "communication_style": "neutral", "total_orders": 0, "total_spend_sar": 0.0,
        "avg_order_value_sar": 0.0, "last_order_at": None,
        "preferred_categories": [], "preferred_brands": [],
        "price_range": {"min": None, "max": None},
        "preferred_payment": None, "preferred_delivery": None,
        "price_sensitivity_score": 0.5, "recommended_discount_pct": 0,
        "coupon_usage_rate": 0.0, "history_summary": "", "past_topics": [],
        "past_products_mentioned": [], "last_intent": None,
        "sentiment": "neutral", "escalation_count": 0,
        "products": [], "high_affinity_product_ids": [],
        "coupons": [], "recent_orders": [],
    }
