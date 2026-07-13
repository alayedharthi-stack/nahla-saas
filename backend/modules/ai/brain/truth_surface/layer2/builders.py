"""
Pure Layer 2 shadow builders — trigger detection and coverage compare only.

No DB, network, webhook, Brain, Compose, loader invocation, or lifecycle imports.
"""
from __future__ import annotations

import re
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from ..contract import TrustedContextSnapshot, TrustedDomain
from .decision_plan_shadow import DecisionPlanShadow, ProposedActionKind
from .domain_registry import domains_for_triggers
from .intent_evidence import AmbiguityState, IntentEvidence

_COUPON_TRIGGERS = frozenset({"coupon_intent", "discount_intent", "cart_discount"})
_OFFER_TRIGGERS = frozenset({"offer_intent", "promotion_intent"})
_ORDER_TRIGGERS = frozenset({"order_ref", "order_status", "checkout_active"})
_PAYMENT_TRIGGERS = frozenset({"payment_query", "receipt"})
_SHIPMENT_TRIGGERS = frozenset({"tracking_query", "shipping_query"})
_CATALOG_TRIGGERS = frozenset({"product_query", "catalog_browse", "price_query"})

_COUPON_PATTERNS = (
    re.compile(r"coupon|discount", re.IGNORECASE),
    re.compile(r"\u0643\u0648\u0628\u0648\u0646|\u062e\u0635\u0645", re.IGNORECASE),
)
_OFFER_PATTERNS = (
    re.compile(r"offer|promotion", re.IGNORECASE),
    re.compile(r"\u0639\u0631\u0636(?:\u0627\u062a)?", re.IGNORECASE),
)
_ORDER_REF_PATTERN = re.compile(r"\b\d{6,}\b")
_TRACKING_PATTERNS = (
    re.compile(r"track|shipping", re.IGNORECASE),
    re.compile(r"\u062a\u062a\u0628\u0639|\u0634\u062d\u0646", re.IGNORECASE),
)
_PAYMENT_PATTERNS = (
    re.compile(r"payment|receipt", re.IGNORECASE),
    re.compile(r"\u062f\u0641\u0639|\u0625\u064a\u0635\u0627\u0644", re.IGNORECASE),
)


def _text_from_history(history: Optional[List[Any]]) -> str:
    if not history:
        return ""
    parts: List[str] = []
    for item in history[-6:]:
        if isinstance(item, dict):
            parts.append(str(item.get("content") or item.get("text") or ""))
        else:
            parts.append(str(item))
    return " ".join(parts)


def _prep_dict(brain_state: Any) -> Dict[str, Any]:
    prep = getattr(brain_state, "order_prep", None) if brain_state else None
    if prep is None:
        return {}
    if isinstance(prep, dict):
        return dict(prep)
    out: Dict[str, Any] = {}
    for key in (
        "order_ref",
        "draft_order_id",
        "coupon_code",
        "applied_coupon_code",
        "discount_code",
        "line_items",
        "product_id",
    ):
        if hasattr(prep, key):
            value = getattr(prep, key, None)
            if value not in (None, ""):
                out[key] = value
    return out


def _domain_fact_key(domain: str) -> str:
    return f"domain:{domain}"


def build_intent_evidence(
    *,
    message: str = "",
    history: Optional[List[Any]] = None,
    brain_state: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    source_turn_ref: str = "",
) -> IntentEvidence:
    text = (message or "").strip()
    combined = f"{text} {_text_from_history(history)}".strip()
    metadata = inbound_metadata or {}
    prep = _prep_dict(brain_state)
    trigger_ids: set[str] = {"always_base"}
    entities: List[Dict[str, str]] = []

    if any(pattern.search(combined) for pattern in _COUPON_PATTERNS):
        trigger_ids.update(_COUPON_TRIGGERS)
    if any(pattern.search(combined) for pattern in _OFFER_PATTERNS):
        trigger_ids.update(_OFFER_TRIGGERS)
    if _ORDER_REF_PATTERN.search(combined) or prep.get("order_ref") or prep.get("draft_order_id"):
        trigger_ids.update(_ORDER_TRIGGERS)
    if any(pattern.search(combined) for pattern in _TRACKING_PATTERNS):
        trigger_ids.update(_SHIPMENT_TRIGGERS)
    if any(pattern.search(combined) for pattern in _PAYMENT_PATTERNS):
        trigger_ids.update(_PAYMENT_TRIGGERS)
    if prep.get("line_items") or prep.get("product_id"):
        trigger_ids.update(_CATALOG_TRIGGERS)
        trigger_ids.add("checkout_active")

    for key in ("coupon_code", "discount_code", "applied_coupon_code", "promotion_id"):
        if metadata.get(key) or prep.get(key):
            trigger_ids.update(_COUPON_TRIGGERS | _OFFER_TRIGGERS)
            entities.append({"entity_kind": key})

    if prep.get("product_id"):
        entities.append({"entity_kind": "product_id"})

    frozen_triggers = frozenset(trigger_ids)
    required_domains = tuple(
        domain.value for domain in domains_for_triggers(frozen_triggers)
    )
    evidence_refs = tuple(
        f"trigger:{trigger}" for trigger in sorted(frozen_triggers)
    )
    ambiguity = AmbiguityState.CLEAR if text else AmbiguityState.AMBIGUOUS

    return IntentEvidence(
        confidence=1.0 if text else 0.7,
        entities=tuple(entities),
        required_domains=required_domains,
        evidence_refs=evidence_refs,
        ambiguity_state=ambiguity,
        trigger_ids=tuple(sorted(frozen_triggers)),
        source_turn_ref=source_turn_ref,
    )


def build_decision_plan_shadow(
    *,
    evidence: IntentEvidence,
    snapshot: Optional[TrustedContextSnapshot] = None,
) -> DecisionPlanShadow:
    required_facts = tuple(_domain_fact_key(domain) for domain in evidence.required_domains)

    if snapshot is None:
        return DecisionPlanShadow(
            proposed_action=ProposedActionKind.DEFER_UNAVAILABLE,
            required_facts=required_facts,
            missing_facts=required_facts,
            reason_codes=("snapshot_missing",),
        )

    loaded_coverage = tuple(snapshot.loaded_domains or [])
    loaded_set = set(loaded_coverage)
    missing_domains = [
        domain for domain in evidence.required_domains if domain not in loaded_set
    ]
    missing_facts = tuple(_domain_fact_key(domain) for domain in missing_domains)

    if missing_facts:
        return DecisionPlanShadow(
            proposed_action=ProposedActionKind.CLARIFY_MISSING,
            required_facts=required_facts,
            missing_facts=missing_facts,
            loaded_coverage=loaded_coverage,
            reason_codes=("missing_required_domains",),
            snapshot_ref=snapshot.snapshot_id or "",
        )

    if (
        TrustedDomain.COUPONS.value in evidence.required_domains
        or TrustedDomain.PROMOTIONS.value in evidence.required_domains
    ):
        coupon_facts = snapshot.facts_for_domain(TrustedDomain.COUPONS)
        promo_facts = snapshot.facts_for_domain(TrustedDomain.PROMOTIONS)
        if not coupon_facts and not promo_facts:
            return DecisionPlanShadow(
                proposed_action=ProposedActionKind.ANSWER_FROM_FACTS,
                required_facts=required_facts,
                loaded_coverage=loaded_coverage,
                reason_codes=("eligible_empty_ok",),
                snapshot_ref=snapshot.snapshot_id or "",
            )

    return DecisionPlanShadow(
        proposed_action=ProposedActionKind.ANSWER_FROM_FACTS,
        required_facts=required_facts,
        loaded_coverage=loaded_coverage,
        reason_codes=("facts_available",),
        snapshot_ref=snapshot.snapshot_id or "",
    )


__all__ = ["build_decision_plan_shadow", "build_intent_evidence"]
