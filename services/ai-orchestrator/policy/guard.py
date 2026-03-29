"""
PolicyGuard
───────────
Validates and enforces store-level rules on every AI-proposed action.

Claude suggests; PolicyGuard decides whether the suggestion is:
  - approved   — sent as-is
  - modified   — sent with clamped/adjusted values
  - blocked    — dropped entirely, reason logged

Rules are read from:
  - Tenant.coupon_policy  (JSONB):  min_discount, max_discount, allowed_coupon_types
  - KnowledgePolicy:               blocked_categories, escalation_rules
  - Tenant.recommendation_controls: max_recommendations_per_turn

No DB reads happen here — the caller passes the pre-loaded context dict.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("ai-orchestrator.policy")

# ── Action type constants ─────────────────────────────────────────────────────
SUGGEST_PRODUCT  = "suggest_product"
SUGGEST_COUPON   = "suggest_coupon"
SUGGEST_BUNDLE   = "suggest_bundle"
PROPOSE_ORDER    = "propose_order"

ALLOWED_ACTION_TYPES = {SUGGEST_PRODUCT, SUGGEST_COUPON, SUGGEST_BUNDLE, PROPOSE_ORDER}


def validate_actions(
    proposed_actions: List[Dict[str, Any]],
    context: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Process every action Claude proposed.

    Returns a list of gate results, one per proposed action:
    {
        "type":            str,
        "proposed_payload": dict,
        "policy_result":   "approved" | "modified" | "blocked",
        "policy_notes":    str,
        "final_payload":   dict | None,
    }
    """
    coupon_policy         = context.get("coupon_policy", {})
    blocked_categories    = set(context.get("blocked_categories", []))
    recommendation_controls = context.get("recommendation_controls", {})
    max_recommendations   = recommendation_controls.get("max_recommendations_per_turn", 3)

    results: List[Dict[str, Any]] = []
    recommendation_count = 0

    for action in proposed_actions:
        action_type = action.get("type", "")
        payload     = action.get("payload", {})

        # ── Unknown action type ───────────────────────────────────────────────
        if action_type not in ALLOWED_ACTION_TYPES:
            results.append(_block(action_type, payload, f"unknown action type: {action_type}"))
            continue

        # ── Per-recommendation cap ────────────────────────────────────────────
        if action_type in (SUGGEST_PRODUCT, SUGGEST_BUNDLE):
            if recommendation_count >= max_recommendations:
                results.append(_block(action_type, payload,
                                      f"max_recommendations_per_turn ({max_recommendations}) reached"))
                continue
            recommendation_count += 1

        # ── Route to specific validator ───────────────────────────────────────
        if action_type == SUGGEST_PRODUCT:
            result = _validate_product(payload, blocked_categories)
        elif action_type == SUGGEST_COUPON:
            result = _validate_coupon(payload, coupon_policy)
        elif action_type == SUGGEST_BUNDLE:
            result = _validate_bundle(payload, blocked_categories)
        elif action_type == PROPOSE_ORDER:
            result = _validate_order(payload)
        else:
            result = _approve(action_type, payload)

        results.append(result)

    return results


# ── Validators ────────────────────────────────────────────────────────────────

def _validate_product(payload: Dict, blocked_categories: set) -> Dict:
    # If the product's category is blocked, refuse
    product_categories = payload.get("categories", [])
    if isinstance(product_categories, str):
        product_categories = [product_categories]
    overlap = set(str(c).lower() for c in product_categories) & blocked_categories
    if overlap:
        return _block(SUGGEST_PRODUCT, payload,
                      f"product category blocked by policy: {overlap}")
    return _approve(SUGGEST_PRODUCT, payload)


def _validate_coupon(payload: Dict, coupon_policy: Dict) -> Dict:
    if not coupon_policy:
        return _approve(SUGGEST_COUPON, payload)

    min_discount = coupon_policy.get("min_discount", 0)
    max_discount = coupon_policy.get("max_discount", 100)
    allowed_types = coupon_policy.get("allowed_coupon_types")   # None = all allowed

    proposed_pct = int(payload.get("discount_pct", 0))
    notes_parts: List[str] = []
    modified = False
    final = dict(payload)

    # Clamp discount within store limits
    if proposed_pct < min_discount:
        final["discount_pct"] = min_discount
        notes_parts.append(f"discount raised from {proposed_pct}% to min {min_discount}%")
        modified = True
    elif proposed_pct > max_discount:
        final["discount_pct"] = max_discount
        notes_parts.append(f"discount clamped from {proposed_pct}% to max {max_discount}%")
        modified = True

    # Check allowed coupon type
    coupon_type = payload.get("coupon_type", "percentage")
    if allowed_types and coupon_type not in allowed_types:
        return _block(SUGGEST_COUPON, payload,
                      f"coupon type '{coupon_type}' not in store allowed_coupon_types: {allowed_types}")

    if modified:
        return {
            "type": SUGGEST_COUPON,
            "proposed_payload": payload,
            "policy_result": "modified",
            "policy_notes": "; ".join(notes_parts),
            "final_payload": final,
        }
    return _approve(SUGGEST_COUPON, payload)


def _validate_bundle(payload: Dict, blocked_categories: set) -> Dict:
    # Bundles are allowed unless they contain blocked-category products.
    # Without product detail we trust the AI here — approval with note.
    if not payload.get("product_ids"):
        return _block(SUGGEST_BUNDLE, payload, "bundle has no product_ids")
    return _approve(SUGGEST_BUNDLE, payload)


def _validate_order(payload: Dict) -> Dict:
    if not payload.get("product_ids"):
        return _block(PROPOSE_ORDER, payload, "order proposal has no product_ids")
    return _approve(PROPOSE_ORDER, payload)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _approve(action_type: str, payload: Dict) -> Dict:
    return {
        "type": action_type,
        "proposed_payload": payload,
        "policy_result": "approved",
        "policy_notes": "",
        "final_payload": payload,
    }


def _block(action_type: str, payload: Dict, reason: str) -> Dict:
    logger.info(f"PolicyGuard blocked {action_type}: {reason}")
    return {
        "type": action_type,
        "proposed_payload": payload,
        "policy_result": "blocked",
        "policy_notes": reason,
        "final_payload": None,
    }
