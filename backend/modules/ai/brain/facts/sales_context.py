"""
brain/facts/sales_context.py
────────────────────────────
Build a unified SalesContextSnapshot for one merchant turn.

This layer intentionally reuses the existing store knowledge, customer
intelligence, offer-decision, and recommendation signals instead of inventing
another prompt-only context builder.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..types import MerchantConversationState, SalesContextSnapshot


class DefaultSalesContextLoader:
    """Build a turn-scoped SalesContextSnapshot from current DB state."""

    def load(
        self,
        db: Any,
        *,
        tenant_id: int,
        customer_phone: str,
        state: MerchantConversationState,
        history: List[Dict[str, Any]],
        profile: Dict[str, Any],
        customer_id: Optional[int] = None,
    ) -> SalesContextSnapshot:
        from core.store_knowledge import StoreKnowledgeLoader
        from services.offer_decision_service import collect_signals

        snapshot = SalesContextSnapshot()

        store_loader = StoreKnowledgeLoader(db, tenant_id)
        store_profile = store_loader.store_profile() or {}
        shipping_summary = store_loader.shipping_summary() or {}
        policy_summary = store_loader.policy_summary() or {}
        coupon_summary = store_loader.coupon_summary() or {}

        snapshot.store_profile = {
            "store_name": store_profile.get("store_name") or profile.get("store_name") or "",
            "store_url": store_profile.get("store_url") or "",
            "description": store_profile.get("description") or "",
            "city": store_profile.get("city") or "",
            "business_hours": policy_summary.get("support_hours") or "",
            "best_sellers": list((store_loader.catalog_summary() or {}).get("top_products") or []),
        }
        try:
            knowledge_fresh = bool(store_loader.is_fresh())
        except Exception:
            knowledge_fresh = False

        snapshot.store_policies = {
            "shipping": shipping_summary,
            "returns": policy_summary.get("return_policy") or "",
            "payments": policy_summary.get("payment_methods") or [],
            "coupons": coupon_summary,
            "knowledge_fresh": knowledge_fresh,
        }

        snapshot.customer_profile = {
            "id": customer_id or profile.get("id"),
            "name": profile.get("name", ""),
            "email": profile.get("email", ""),
            "segment": profile.get("segment", ""),
            "customer_status": profile.get("customer_status", ""),
            "rfm_segment": profile.get("rfm_segment", ""),
            "is_returning": bool(profile.get("is_returning")),
            "last_order_at": profile.get("last_order_at"),
            "total_orders": int(profile.get("total_orders") or 0),
            "total_spend_sar": float(profile.get("total_spend_sar") or 0.0),
        }
        snapshot.customer_preferences = {
            "preferred_categories": list(profile.get("preferred_categories") or []),
            "preferred_brands": list(profile.get("preferred_brands") or []),
            "price_range": dict(profile.get("price_range") or {}),
            "preferred_payment": profile.get("preferred_payment"),
            "preferred_delivery": profile.get("preferred_delivery"),
            "communication_style": profile.get("communication_style", "neutral"),
            "language": profile.get("preferred_language", "ar"),
        }

        recent_messages = list((history or [])[-20:])
        summary_text = str(
            state.conversation_summary
            or profile.get("history_summary")
            or ""
        ).strip()
        snapshot.conversation_memory = {
            "recent_messages": recent_messages,
            "conversation_summary": summary_text,
            "customer_goal": state.customer_goal,
            "stage": state.stage,
            "pending_action": state.pending_action,
            "selected_product": state.current_product_focus,
            "selected_variant": state.selected_variant,
            "cart_items": list(state.cart_items or []),
            "last_recommended_products": list(state.last_recommended_products or []),
            "checkout_progress": state.order_prep.to_dict(),
            "turn": state.turn,
        }

        try:
            signal_snapshot = collect_signals(
                db,
                tenant_id=tenant_id,
                customer_id=customer_id,
            )
            snapshot.offer_signals = {
                "segment": signal_snapshot.segment,
                "customer_status": signal_snapshot.customer_status,
                "rfm_segment": signal_snapshot.rfm_segment,
                "price_sensitivity_score": signal_snapshot.price_sensitivity_score,
                "recommended_discount_pct": signal_snapshot.recommended_discount_pct,
                "coupon_usage_rate": signal_snapshot.coupon_usage_rate,
                "recent_offers_in_window": signal_snapshot.recent_offers_in_window,
            }
        except Exception:
            snapshot.offer_signals = {}

        snapshot.recommendations = self._load_recommendations(
            db,
            tenant_id=tenant_id,
            customer_id=customer_id,
        )
        snapshot.repeat_purchase_candidates = self._load_repeat_purchase_candidates(
            db,
            tenant_id=tenant_id,
            customer_id=customer_id,
        )
        snapshot.web_search_policy = {
            "enabled": True,
            "use_only_when_store_knowledge_missing": True,
            "require_citations_for_claims": True,
            "blocked_topics": ["medical_diagnosis", "legal_advice", "financial_advice"],
        }

        return snapshot

    def _load_recommendations(
        self,
        db: Any,
        *,
        tenant_id: int,
        customer_id: Optional[int],
    ) -> List[Dict[str, Any]]:
        if not customer_id:
            return []

        try:
            from database.models import Product, ProductAffinity

            rows = (
                db.query(ProductAffinity, Product)
                .join(Product, Product.id == ProductAffinity.product_id)
                .filter(
                    ProductAffinity.tenant_id == tenant_id,
                    ProductAffinity.customer_id == customer_id,
                )
                .order_by(ProductAffinity.affinity_score.desc(), Product.id.desc())
                .limit(5)
                .all()
            )
            return [
                {
                    "id": product.id,
                    "title": product.title,
                    "price": product.price,
                    "affinity_score": affinity.affinity_score,
                    "reason": "high_affinity",
                }
                for affinity, product in rows
            ]
        except Exception:
            return []

    def _load_repeat_purchase_candidates(
        self,
        db: Any,
        *,
        tenant_id: int,
        customer_id: Optional[int],
    ) -> List[Dict[str, Any]]:
        if not customer_id:
            return []

        try:
            from database.models import PredictiveReorderEstimate, Product

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            rows = (
                db.query(PredictiveReorderEstimate, Product)
                .join(Product, Product.id == PredictiveReorderEstimate.product_id)
                .filter(
                    PredictiveReorderEstimate.tenant_id == tenant_id,
                    PredictiveReorderEstimate.customer_id == customer_id,
                    PredictiveReorderEstimate.predicted_reorder_date.isnot(None),
                    PredictiveReorderEstimate.predicted_reorder_date <= now,
                )
                .order_by(PredictiveReorderEstimate.predicted_reorder_date.asc())
                .limit(5)
                .all()
            )
            return [
                {
                    "product_id": product.id,
                    "title": product.title,
                    "price": product.price,
                    "predicted_reorder_date": (
                        estimate.predicted_reorder_date.isoformat()
                        if estimate.predicted_reorder_date
                        else None
                    ),
                    "confidence": float(getattr(estimate, "confidence_score", 0.0) or 0.0),
                }
                for estimate, product in rows
            ]
        except Exception:
            return []
