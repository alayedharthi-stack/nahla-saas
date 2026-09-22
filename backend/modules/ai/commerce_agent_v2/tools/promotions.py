"""Trusted tenant-scoped, read-only promotion tool.

The commerce runtime's model may tell a customer about a coupon only when the
merchant's own records say it is currently valid and shareable on this
channel, with this customer. ``promotion_truth.resolve_shareable_promotions``
is the platform's one resolver for that: it never invents a code, it leaves
out campaign-only, expired, disabled and exhausted codes, it hands a personal
code (one issued to a single customer) only to a conversation with that
customer, and it reports what it could not read. This module adds the
merchant's own dashboard policy — whether the AI may share coupons at all, and
which levels — and the one thing the resolver deliberately leaves open: whether
this customer has earned the level a code is conditioned on.

A valid code is not an entitled code. The merchant's loyalty ladder exists so
that a gold customer's discount is a gold customer's discount, and a tool that
hands every rung to every conversation has turned the reward into a public
price. So a level-conditioned coupon is projected only when
``coupon_entitlement_read`` says this customer reached that rung, read from
the platform's own authorities — the Customer Intelligence order count, the
level contract, and the merchant's saved ladder with its first-purchase rule
exactly as the merchant left it. Nothing is issued, assigned or generated here.

What is *not* conditioned on a level is not withheld for want of one. A coupon
the record ties to no rung, and every offer, is projected whether or not a
level could be resolved: a customer the platform could not classify still sees
what was never about classification. Equally, not knowing is not a level —
an unidentified conversation, an unreadable history and a known customer with
no purchases are three different states, the projection names which, and only
the third is a determination.

Every remaining condition stays unevaluated and is said so by name: the
projection claims the level question and no more.

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
from services.coupon_entitlement_read import LevelEntitlement, resolve_level_entitlement

MAX_PROMOTIONS = 8            # per kind: at most this many coupons and this many offers
MAX_CONDITION_IDS = 20        # ids kept per product/category condition; the rest become a count
_MAX_TEXT = 300
_RECORD_KINDS = ("coupon", "offer")
# What the projection says it settled about who may use a code.
LEVEL_ENTITLED = "entitled"                      # the record names a rung this customer reached
LEVEL_NOT_CONDITIONED = "not_conditioned_on_level"   # the record names no rung at all
# The conditions a projection carries but has not evaluated. Named rather than
# summarised, so a reader can see exactly what is still open.
_UNEVALUATED = "conditions_not_fully_evaluated"


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


def _unevaluated_conditions(conditions: Dict[str, Any]) -> Tuple[str, ...]:
    """The condition names this projection carries but did not check.

    The ``<key>_count`` companions ``_bounded_conditions`` adds are the same
    condition counted, not another one, so they are not listed twice.
    """
    names = [name for name in conditions if not str(name).endswith("_count")]
    return tuple(sorted(str(name) for name in names))


def _project(fact: Dict[str, Any], *, customer_id: Optional[int],
             allowed_levels: Optional[List[str]],
             entitlement: LevelEntitlement) -> Optional[Tuple[PromotionSnapshot, EvidenceRecord]]:
    """One resolver fact as a snapshot plus its evidence record, or ``None``
    when the fact cannot be cited here: no id, an unknown kind, a coupon
    without a code, a personal code that is someone else's, a level the
    merchant's policy keeps from the AI, or a level this customer has not
    earned. An offer never carries a code, and is never level-conditioned."""
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
    if kind == "coupon" and level:
        # Two separate gates, and both must open. The merchant's AI policy says
        # which rungs the assistant may ever mention in this store; the
        # entitlement says whether *this* customer reached this one. A store
        # that allows gold does not make every conversation gold.
        if allowed_levels is not None and level not in allowed_levels:
            return None
        if not entitlement.entitles(level):
            return None
        level_eligibility = LEVEL_ENTITLED
    else:
        # No rung on the record: nothing about a classification is being
        # claimed, so an unresolved level is no reason to withhold it.
        level_eligibility = LEVEL_NOT_CONDITIONED
    ref = f"promotion:{kind}:{promotion_id}"
    conditions = _bounded_conditions(fact.get("conditions"))
    unevaluated = _unevaluated_conditions(conditions)
    if kind == "offer":
        # An offer is terms, not a grant, and its record's empty ``conditions``
        # cannot be told apart from conditions that were never read. Nothing
        # about who may use it is settled here, and the note keeps the
        # resolver's own word for why.
        determined, note = False, _text(fact.get("eligibility_note"), 120)
    else:
        # Either this customer's standing was actually read, or the merchant's
        # own record names them: both settle *who* this is. Anything the record
        # still conditions on keeps the whole question open, by name.
        settled = entitlement.determined or bound_to_this_customer
        determined = settled and not unevaluated
        if unevaluated:
            note = _text(f"{_UNEVALUATED}:{','.join(unevaluated)}", 120)
        elif settled:
            note = ""
        else:
            note = _text(f"customer_standing_not_determined:{entitlement.reason}", 120)
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
        "conditions": conditions,
        "bound_to_this_customer": bound_to_this_customer,
        # What was settled about *who* may use this, and on what reading of the
        # customer. ``customer_level`` is the ladder rung the platform resolved,
        # empty when it resolved none; ``level_reason`` says whether that was a
        # determination or a failure to determine.
        "level_eligibility": level_eligibility,
        "customer_level": entitlement.resolved_level or "",
        "level_reason": entitlement.reason,
        # The whole eligibility question, not a part of it: true only when who
        # this customer is was settled *and* the record leaves no other
        # condition unchecked. A minimum basket or a usage limit nobody
        # evaluated keeps this false, an offer never sets it, and
        # ``eligibility_note`` names exactly what is still open.
        "eligibility_determined": determined,
        "eligibility_note": note,
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
            # The record says the promotion exists and is live. Who may use it
            # was settled somewhere else, and the evidence names where.
            "entitlement_service": "services.coupon_entitlement_read.resolve_level_entitlement",
            "entitlement_reason": entitlement.reason,
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
    merchant's AI coupon policy and this customer's own level entitlement, and
    registers each as evidence. ``denied`` says the merchant keeps coupons away
    from the AI; ``not_found`` is an honest empty answer; ``error`` says the
    records could not be read, which is never reported as "no promotions";
    ``partial`` marks a list a failed source may have left incomplete.

    The entitlement is resolved once, before any fact is projected, and travels
    with the result so the caller can see on what reading of the customer the
    list was built. It decides only which level-conditioned coupons may appear;
    a customer whose level could not be resolved still receives everything the
    records did not condition on one, and ``not_found`` after that is a real
    empty answer rather than a classification failure in disguise.
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
    # Read once per call, before anything is projected: every coupon in this
    # list is judged against the same reading of the customer, and a second
    # order landing mid-turn cannot make one rung's code appear beside
    # another's. Never raises — an unreadable history entitles nothing.
    entitlement = resolve_level_entitlement(context.db, int(context.tenant_id), customer_id)
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
            projected = _project(fact, customer_id=customer_id, allowed_levels=allowed_levels,
                                 entitlement=entitlement)
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
    entitlement_view = entitlement.as_dict()
    if not snapshots:
        if bool(getattr(truth, "query_failed", False)) or outcome == PROMOTION_QUERY_FAILED:
            return PromotionListResult(status="error", query_outcome=outcome or PROMOTION_QUERY_FAILED,
                                       partial=partial, failure_reason="promotion_query_failed",
                                       entitlement=entitlement_view)
        return PromotionListResult(status="not_found", query_outcome=outcome or NO_VALID_PROMOTIONS,
                                   partial=partial, failure_reason="no_valid_shareable_promotions",
                                   entitlement=entitlement_view)
    context.register_evidence(evidence)
    return PromotionListResult(status="ok", promotions=snapshots, evidence=evidence,
                               query_outcome=outcome, partial=partial, entitlement=entitlement_view)


__all__ = ["LEVEL_ENTITLED", "LEVEL_NOT_CONDITIONED", "MAX_CONDITION_IDS", "MAX_PROMOTIONS",
           "list_shareable_promotions_impl"]
