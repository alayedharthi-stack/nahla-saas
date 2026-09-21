"""Trusted tenant-scoped, read-only promotion tool.

The commerce runtime's model may tell a customer about a coupon only when the
merchant's own records say it is currently valid and shareable on this
channel, with this customer. ``promotion_truth.resolve_shareable_promotions``
is the platform's one resolver for that: it never invents a code, it leaves
out campaign-only, expired, disabled and exhausted codes, it hands a personal
code (one issued to a single customer) only to a conversation with that
customer, and it reports what it could not read. This module adds the
merchant's own dashboard policy — whether the AI may share coupons at all, and
which levels — and projects the facts into evidence the loop verifies a reply
against. Whether the customer qualifies is not evaluated here, and every
projection says so.

There is no Agents-SDK wrapper: the legacy path keeps its own promotion
policy. The commerce runtime's loop passes the trusted context directly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from modules.ai.brain.commerce.promotion_truth import (
    NO_VALID_PROMOTIONS,
    PROMOTION_PARTIAL_FAILURE,
    PROMOTION_QUERY_FAILED,
    resolve_shareable_promotions,
)
from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.output import (
    EvidenceRecord,
    PromotionListResult,
    PromotionSnapshot,
)

MAX_PROMOTIONS = 8            # per kind: at most this many coupons and this many offers
MAX_CONDITION_IDS = 20        # ids kept per product/category condition; the rest become a count
_MAX_TEXT = 300
_RECORD_KINDS = ("coupon", "offer")


def _text(value: Any, limit: int = _MAX_TEXT) -> str:
    return str(value or "").strip()[:limit]


def _bounded_conditions(conditions: Any) -> Dict[str, Any]:
    """The coupon's conditions with every list cut to ``MAX_CONDITION_IDS`` and
    its full length kept as ``<key>_count``, so a product-scoped coupon with
    hundreds of ids cannot push the whole observation past the loop's bound."""
    if not isinstance(conditions, dict):
        return {}
    bounded: Dict[str, Any] = {}
    for key, value in list(conditions.items())[:12]:
        name = _text(key, 64)
        if isinstance(value, (list, tuple)):
            items = list(value)
            bounded[name] = [_text(item, 64) if not isinstance(item, (int, float, bool)) else item
                             for item in items[:MAX_CONDITION_IDS]]
            if len(items) > MAX_CONDITION_IDS:
                bounded[f"{name}_count"] = len(items)
        elif isinstance(value, (int, float, bool)) or value is None:
            bounded[name] = value
        else:
            bounded[name] = _text(value, 120)
    return bounded


def _project(fact: Dict[str, Any], *, customer_id: Optional[int],
             allowed_levels: Optional[List[str]]) -> Optional[Tuple[PromotionSnapshot, EvidenceRecord]]:
    """One resolver fact as a snapshot plus its evidence record, or ``None``
    when the fact cannot be cited here: no id, an unknown kind, a coupon
    without a code, a personal code that is someone else's, or a level the
    merchant's policy keeps from the AI. An offer never carries a code."""
    kind = str(fact.get("record_kind") or "")
    try:
        promotion_id = int(fact.get("id"))
    except (TypeError, ValueError):
        return None
    if promotion_id <= 0 or kind not in _RECORD_KINDS:
        return None
    code = _text(fact.get("code"), 64) if kind == "coupon" else ""
    if kind == "coupon" and not code:
        return None
    bound_customer = fact.get("bound_customer_id")
    if bool(fact.get("customer_bound")) or bound_customer not in (None, "", 0):
        try:
            bound_customer = int(bound_customer)
        except (TypeError, ValueError):
            return None
        if customer_id is None or int(customer_id) != bound_customer:
            return None
        bound_to_this_customer = True
    else:
        bound_to_this_customer = False
    level = _text(fact.get("coupon_level"), 32).lower()
    if kind == "coupon" and level and allowed_levels is not None and level not in allowed_levels:
        return None
    ref = f"promotion:{kind}:{promotion_id}"
    fields: Dict[str, Any] = {
        "promotion_id": promotion_id,
        "record_kind": kind,
        "code": code,
        "name": _text(fact.get("name"), 160),
        "description": _text(fact.get("description")),
        "discount_type": _text(fact.get("discount_type") or fact.get("promotion_type"), 64),
        "discount_value": _text(fact.get("discount_value"), 64),
        "discount": _text(fact.get("discount"), 64),
        "expires_at": _text(fact.get("expires_at") or fact.get("ends_at"), 64),
        "coupon_level": level,
        "conditions": _bounded_conditions(fact.get("conditions")),
        "bound_to_this_customer": bound_to_this_customer,
        "eligibility_determined": False,
        "eligibility_note": _text(fact.get("eligibility_note"), 120),
    }
    evidence = EvidenceRecord(
        ref=ref,
        source="promotion_coupon" if kind == "coupon" else "promotion_offer",
        source_id=str(promotion_id),
        facts=[],
        fields=dict(fields),
        provenance={
            "service": "modules.ai.brain.commerce.promotion_truth.resolve_shareable_promotions",
            "record": "coupons" if kind == "coupon" else "promotions",
            "freshness": "query_time",
        },
    )
    return PromotionSnapshot(**fields, evidence_ref=ref), evidence


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _min_remaining_hours(policy: Dict[str, Any]) -> int:
    """The merchant's ``min_remaining_hours``: a code with less life left than
    this is not handed out by the AI. Unreadable or negative reads as 0."""
    try:
        return max(0, int(policy.get("min_remaining_hours") or 0))
    except (TypeError, ValueError):
        return 0


def _expires_before(fact: Dict[str, Any], cutoff: datetime) -> bool:
    """Whether the coupon's expiry is readable and earlier than ``cutoff``. An
    unreadable expiry does not exclude: the resolver has already judged the
    code currently valid, and this rule only shortens that window."""
    raw = str(fact.get("expires_at") or "").strip()
    if not raw:
        return False
    try:
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires < cutoff


def _merchant_policy(context: CommerceAgentContext) -> Dict[str, Any]:
    """The merchant's AI coupon policy from the dashboard, read through the
    platform's own accessor — the one the customer-request coupon service
    reads, imported as is: the coupon generator is another scope's file and
    this runtime does not change it (``test_branch_diff_excludes_other_agent_scope_paths``).
    Raises when it cannot be read: a capability gate that cannot be read is
    closed, never assumed open."""
    from services.coupon_generator import _get_ai_policy  # noqa: PLC0415

    return _get_ai_policy(context.db, int(context.tenant_id))


async def list_shareable_promotions_impl(
    context: CommerceAgentContext,
    *,
    limit: int = MAX_PROMOTIONS,
) -> PromotionListResult:
    """SDK-free implementation of the ``list_shareable_promotions`` read tool.

    Reads the tenant's currently valid, shareable coupons and offers through
    the platform's promotion-truth resolver — for this conversation's customer,
    so a personal code issued to anyone else is never returned — under the
    merchant's AI coupon policy, and registers each as evidence. ``denied``
    says the merchant keeps coupons away from the AI; ``not_found`` is an
    honest empty answer; ``error`` says the records could not be read, which
    is never reported as "no promotions"; ``partial`` marks a list a failed
    source may have left incomplete.
    """
    context.assert_scope()
    try:
        policy = _merchant_policy(context)
    except Exception as exc:  # noqa: BLE001 - an unreadable gate is a closed gate, said as such
        return PromotionListResult(status="error", query_outcome=PROMOTION_QUERY_FAILED,
                                   failure_reason=f"merchant_ai_coupon_policy_unreadable:{type(exc).__name__}")
    if not bool(policy.get("enabled", True)):
        return PromotionListResult(status="denied", query_outcome=NO_VALID_PROMOTIONS,
                                   failure_reason="merchant_ai_coupon_policy_disabled")
    allowed_levels_raw = policy.get("allowed_levels")
    allowed_levels = ([str(level).lower() for level in allowed_levels_raw]
                      if isinstance(allowed_levels_raw, (list, tuple)) else None)
    bounded = max(1, min(int(limit or MAX_PROMOTIONS), MAX_PROMOTIONS))
    raw_customer = getattr(context, "customer_id", None)
    customer_id = int(raw_customer) if raw_customer not in (None, "", 0) else None
    truth = resolve_shareable_promotions(context.db, int(context.tenant_id), limit=bounded,
                                         customer_id=customer_id)
    min_hours = _min_remaining_hours(policy)
    cutoff = _now() + timedelta(hours=min_hours) if min_hours > 0 else None
    snapshots: List[PromotionSnapshot] = []
    evidence: List[EvidenceRecord] = []
    for facts in (list(getattr(truth, "shareable", None) or ()), list(getattr(truth, "offers", None) or ())):
        kept = 0
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            if cutoff is not None and fact.get("record_kind") == "coupon" and _expires_before(fact, cutoff):
                continue
            projected = _project(fact, customer_id=customer_id, allowed_levels=allowed_levels)
            if projected is None:
                continue
            snapshot, record = projected
            snapshots.append(snapshot)
            evidence.append(record)
            kept += 1
            if kept >= bounded:
                break
    outcome = str(getattr(truth, "query_outcome", "") or "")
    partial = outcome == PROMOTION_PARTIAL_FAILURE
    if not snapshots:
        if bool(getattr(truth, "query_failed", False)) or outcome == PROMOTION_QUERY_FAILED:
            return PromotionListResult(status="error", query_outcome=outcome or PROMOTION_QUERY_FAILED,
                                       partial=partial, failure_reason="promotion_query_failed")
        return PromotionListResult(status="not_found", query_outcome=outcome or NO_VALID_PROMOTIONS,
                                   partial=partial, failure_reason="no_valid_shareable_promotions")
    context.register_evidence(evidence)
    return PromotionListResult(status="ok", promotions=snapshots, evidence=evidence,
                               query_outcome=outcome, partial=partial)


__all__ = ["MAX_CONDITION_IDS", "MAX_PROMOTIONS", "list_shareable_promotions_impl"]
