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

What is not conditioned on a level is not withheld *for want of one* — but it
still needs the merchant to have said so. The absence of a rung on a record
says only that the record does not name one; it is not evidence that the
merchant meant the code for everyone. A coupon carrying no rung is therefore
projected only when the merchant published it to an AI surface themselves
(``_merchant_published_generally``), and every offer is projected because an
active store promotion the merchant created *is* that authorisation and carries
no code. A customer the platform could not classify still sees what was never
about classification, and nothing else.

Equally, not knowing is not a level — an unidentified conversation, an
unreadable history and a known customer with no purchases are three different
states, the projection names which, and only the third is a determination.

Every remaining condition stays unevaluated and is said so by name: the
projection claims the level question and no more.

There is no Agents-SDK wrapper: the legacy path keeps its own promotion
policy. The commerce runtime's loop passes the trusted context directly.
"""
from __future__ import annotations

import logging
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
from services.coupon_level_contract import (
    CANONICAL_COUPON_LEVEL_IDS,
    ladder_is_readable,
    policy_served_level,
    unreadable_rungs,
)
from services.native_ai_coupon_eligibility import NATIVE_AI_CHANNELS

logger = logging.getLogger(__name__)

MAX_PROMOTIONS = 8            # per kind: at most this many coupons and this many offers
MAX_CONDITION_IDS = 20        # ids kept per product/category condition; the rest become a count
_MAX_TEXT = 300
_RECORD_KINDS = ("coupon", "offer")
# What the projection says it settled about who may use a code.
LEVEL_ENTITLED = "entitled"                      # the record names the rung this customer stands on
GENERAL_AUTHORIZED = "merchant_authorized_general"   # no rung, and the merchant published it anyway
# A coupon carrying no rung is a general offer only when the merchant performed
# two acts that say so. ``source_type`` "manual" is the merchant's own dashboard
# — the single path where the AI fields are set at all; the warm pool writes
# "system" and always stamps a rung, and a Salla import expresses no Nahla AI
# intent. ``allocation_channel`` is left empty when the merchant does not choose
# one, and only the pool generator defaults it to "shared", which is why
# "shared" on its own proves nothing. Both together are a deliberate placement.
MERCHANT_AUTHORED_SOURCE_TYPES = frozenset({"manual"})

# Why a currently valid promotion did not reach this customer. An empty list is
# an answer, and these say which answer it is: without them "no codes for you"
# and "this store has no codes" and "we could not work out who you are" arrive
# identically, and the difference is the whole question a merchant asks first.
WITHHELD_UNUSABLE_RECORD = "unusable_record"
WITHHELD_PERSONAL_TO_ANOTHER = "personal_to_another_customer"
WITHHELD_LEVEL_NOT_ALLOWED = "level_not_allowed_by_store_policy"
WITHHELD_LEVEL_NOT_EARNED = "level_not_earned_by_customer"
# Permitted by the store and not above this customer's standing, but not the
# one rung being served. A gold customer served silver still sees one rung.
WITHHELD_LEVEL_NOT_SERVED = "level_not_served_for_customer"
# The merchant's ladder could not be read at all, so no rung can be shown to
# honour the gates they set on it. Counted separately because it is the one
# reason here that is a failure of ours rather than an answer about the
# customer or the store: an empty list carrying this is not an empty store.
WITHHELD_LEVEL_POLICY_UNREADABLE = "level_policy_unreadable"
WITHHELD_NOT_PUBLISHED = "not_published_for_general_use"
WITHHELD_EXPIRING_TOO_SOON = "expiring_before_store_minimum"
# The conditions a projection carries but has not evaluated. Named rather than
# summarised, so a reader can see exactly what is still open.
_UNEVALUATED = "conditions_not_fully_evaluated"
# Records that actually put the level question to the merchant's ladder. Zero
# of them means an unreadable ladder cost this customer nothing, and a list
# that lost nothing is never reported as short.
LEVEL_CONDITIONED = "level_conditioned"


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


def _merchant_published_generally(fact: Dict[str, Any]) -> bool:
    """Whether the merchant themselves put this unleveled coupon where the
    assistant can reach it.

    Always a positive act, never an absence. The coupon must first have been
    created in the merchant's own dashboard — ``source_type`` "manual", the one
    path that writes the AI fields; the warm pool writes "system" and always
    stamps a rung, and a store-platform import expresses no Nahla intent. Then
    one of two things the merchant did:

    * they chose an AI-reachable allocation channel, or
    * they created it as a general promotional coupon, which the dashboard
      describes to them as a code that «يبقى مشتركاً» — it stays shared, and is
      simply not auto-assigned to one customer. That is the merchant declaring
      a public code, and it reaches the assistant on the strength of the
      declaration rather than of the missing rung.

    A channel the merchant did choose is honoured either way: ``campaign`` and
    ``autopilot`` are placements on surfaces that are not this one, so a coupon
    sent there stays there. And ``merchant_authored`` is the create endpoint's
    own marker, which nothing else writes — a row that merely says nothing is
    still not a merchant act, which is the distinction this gate exists to keep.
    """
    source_type = _text(fact.get("source_type"), 32).lower()
    if source_type not in MERCHANT_AUTHORED_SOURCE_TYPES:
        return False
    channel = _text(fact.get("allocation_channel"), 32).lower()
    if channel:
        return channel in NATIVE_AI_CHANNELS
    return bool(fact.get("merchant_authored"))


def _is_above_standing(level: str, entitlement: LevelEntitlement) -> bool:
    """Whether a rung sits above what this customer's order history earned.

    A standing that could not be determined puts every rung above it: a failure
    to determine entitles nothing, and must never read as a lower tier handed
    out by default.
    """
    earned = str(getattr(entitlement, "resolved_level", "") or "").strip().lower()
    if earned not in CANONICAL_COUPON_LEVEL_IDS or level not in CANONICAL_COUPON_LEVEL_IDS:
        return True
    return (CANONICAL_COUPON_LEVEL_IDS.index(level)
            > CANONICAL_COUPON_LEVEL_IDS.index(earned))


def _project(fact: Dict[str, Any], *, customer_id: Optional[int],
             allowed_levels: Optional[List[str]], entitlement: LevelEntitlement,
             served_level: Optional[str] = None,
             level_policy_readable: bool = True,
             withheld: Optional[Dict[str, int]] = None,
             reached: Optional[Dict[str, int]] = None,
             ) -> Optional[Tuple[PromotionSnapshot, EvidenceRecord]]:
    """One resolver fact as a snapshot plus its evidence record, or ``None``
    when the fact cannot be cited here: no id, an unknown kind, a coupon
    without a code, a personal code that is someone else's, a level the
    merchant's policy keeps from the AI, a level this customer has not earned,
    or no rung and no merchant publication. An offer never carries a code, and
    is never level-conditioned.

    Every refusal is counted into ``withheld`` under the reason that caused it.
    A caller that only sees ``None`` cannot tell a store with nothing to give
    from a customer who earned nothing from a customer nobody could place, and
    those are three different conversations.
    """
    def _refuse(reason: str) -> None:
        if withheld is not None:
            withheld[reason] = withheld.get(reason, 0) + 1
    kind = str(fact.get("record_kind") or "")
    try:
        promotion_id = int(fact.get("id"))
    except (TypeError, ValueError):
        _refuse(WITHHELD_UNUSABLE_RECORD)
        return None
    if promotion_id <= 0 or kind not in _RECORD_KINDS:
        _refuse(WITHHELD_UNUSABLE_RECORD)
        return None
    code = _text(fact.get("code"), 64) if kind == "coupon" else ""
    if kind == "coupon" and not code:
        _refuse(WITHHELD_UNUSABLE_RECORD)
        return None
    bound_customer = fact.get("bound_customer_id")
    if bool(fact.get("customer_bound")) or bound_customer not in (None, "", 0):
        try:
            bound_customer = int(bound_customer)
        except (TypeError, ValueError):
            _refuse(WITHHELD_UNUSABLE_RECORD)
            return None
        if customer_id is None or int(customer_id) != bound_customer:
            _refuse(WITHHELD_PERSONAL_TO_ANOTHER)
            return None
        bound_to_this_customer = True
    else:
        bound_to_this_customer = False
    level = _text(fact.get("coupon_level"), 32).lower()
    if kind != "coupon":
        # An offer is a store promotion the merchant created and left running.
        # Being active *is* the authorisation, and it carries no code.
        level_eligibility = GENERAL_AUTHORIZED
    elif level:
        # This record asked the ladder a question. Counted here rather than by
        # the caller so that a rung-conditioned code refused *before* this point
        # — unusable, or personal to someone else — is not counted as one the
        # ladder was consulted about.
        if reached is not None:
            reached[LEVEL_CONDITIONED] = reached.get(LEVEL_CONDITIONED, 0) + 1
        # Two separate gates, and both must open. The merchant's AI policy says
        # which rungs the assistant may ever mention in this store; the
        # entitlement says whether this is the rung *this* customer stands on.
        # A store that allows gold does not make every conversation gold.
        if allowed_levels is not None and level not in allowed_levels:
            _refuse(WITHHELD_LEVEL_NOT_ALLOWED)
            return None
        if not level_policy_readable:
            # The rung the merchant would serve could not be worked out, and a
            # rung is never shown on the strength of a standing alone: what a
            # customer earned is not evidence that this channel may hand it
            # over. Withheld under its own reason, so the caller can say the
            # list is short because of us rather than answer for the store.
            _refuse(WITHHELD_LEVEL_POLICY_UNREADABLE)
            return None
        if level != (served_level or ""):
            # One rung reaches a conversation, and it is the rung the platform
            # would actually serve: the highest the merchant permits at or
            # below what this customer earned. Above their standing is not
            # theirs; at or below it but not the served rung would put two
            # rungs' codes in front of one customer, which is the failure the
            # entitlement module exists to prevent.
            _refuse(WITHHELD_LEVEL_NOT_EARNED if _is_above_standing(level, entitlement)
                    else WITHHELD_LEVEL_NOT_SERVED)
            return None
        level_eligibility = LEVEL_ENTITLED
    elif _merchant_published_generally(fact):
        # No rung, and the merchant published it to an AI surface themselves.
        # Nothing about a classification is being claimed, so an unresolved
        # level is no reason to withhold it.
        level_eligibility = GENERAL_AUTHORIZED
    else:
        # No rung and no authorisation. The absence of a level says only that
        # the record does not name one — never that the merchant meant this for
        # everyone — so it is withheld rather than guessed into a public offer.
        _refuse(WITHHELD_NOT_PUBLISHED)
        return None
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


def _unreadable_reason(ladder_failure: str, broken_rungs: List[str]) -> Optional[str]:
    """Why this list may be short, or ``None`` when nothing was unreadable.

    Two different failures, named apart: the whole ladder could not be read, or
    particular rungs carry permission fields that are not permission fields.
    Neither is a merchant decision, and neither may be reported as one.
    """
    if ladder_failure:
        return f"coupon_level_policy_unreadable:{ladder_failure}"
    if broken_rungs:
        return f"coupon_level_settings_unreadable:{','.join(broken_rungs)}"
    return None


def _merchant_levels(context: CommerceAgentContext) -> Any:
    """The merchant's saved coupon ladder, through the platform's own accessor.

    Read for one purpose: each rung's own ``enabled`` switch and
    ``allowed_channels``, so the rung chosen for a customer honours the gates
    the merchant set on it. Read-only, like everything else here — the
    generator is another scope's file and this runtime does not change it.
    """
    from services.coupon_generator import _get_coupon_dashboard_block  # noqa: PLC0415

    return (_get_coupon_dashboard_block(context.db, int(context.tenant_id)) or {}).get("levels")


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

    The merchant's ladder is the same kind of authority as the policy above it:
    when it cannot be read, no rung is served, every level-conditioned coupon is
    withheld under ``level_policy_unreadable``, and what the merchant published
    to everyone is unaffected because it never depended on a rung. A list
    shortened that way is returned ``partial``; one emptied that way is returned
    ``error``, never ``not_found`` — the store was not asked and cannot be
    reported as having nothing.
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
    # The one rung this store would actually serve this customer: the highest
    # it permits at or below what the order history earned. The issuance
    # service selects with the same contract, so what the assistant may mention
    # and what the platform would hand over cannot drift apart.
    #
    # Their standing is untouched by it. A gold customer in a store that keeps
    # gold from the assistant is still gold; silver is only what this channel
    # may offer them today.
    #
    # ``policy_served_level`` is total: malformed settings narrow what is
    # servable and never raise, so only the read itself can fail here. When it
    # does there is no rung to serve and every level-conditioned coupon is
    # withheld — a standing is not evidence that this channel may hand that
    # rung over, and falling back to the earned rung would serve the very one
    # the merchant may have closed.
    level_policy_unreadable = ""
    broken_rungs: List[str] = []
    served_level: Optional[str] = None
    try:
        merchant_levels = _merchant_levels(context)
    except Exception as exc:  # noqa: BLE001 - an unreadable ladder serves nothing
        level_policy_unreadable = type(exc).__name__
    else:
        broken_rungs = unreadable_rungs(merchant_levels)
        if not ladder_is_readable(merchant_levels):
            # The read succeeded and returned something that is not a ladder.
            # Indistinguishable, for our purposes, from not having read it:
            # neither tells us what the merchant permits.
            level_policy_unreadable = "not_a_ladder"
        else:
            served_level = policy_served_level(
                merchant_levels, entitlement.resolved_level,
                channel="ai", policy_levels=allowed_levels or ())
    if level_policy_unreadable:
        logger.info("[PROMOTION_PROJECTION] tenant=%s level_ladder_unreadable=%s",
                    int(context.tenant_id), level_policy_unreadable)
    truth = resolve_shareable_promotions(context.db, int(context.tenant_id), limit=bounded,
                                         customer_id=customer_id)
    min_hours = _min_remaining_hours(policy)
    cutoff = _now() + timedelta(hours=min_hours) if min_hours > 0 else None
    snapshots: List[PromotionSnapshot] = []
    evidence: List[EvidenceRecord] = []
    withheld: Dict[str, int] = {}
    considered = 0
    reached: Dict[str, int] = {}
    for facts in (list(getattr(truth, "shareable", None) or ()), list(getattr(truth, "offers", None) or ())):
        kept = 0
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            considered += 1
            if cutoff is not None and fact.get("record_kind") == "coupon" and _expires_before(fact, cutoff):
                withheld[WITHHELD_EXPIRING_TOO_SOON] = withheld.get(WITHHELD_EXPIRING_TOO_SOON, 0) + 1
                continue
            projected = _project(fact, customer_id=customer_id, allowed_levels=allowed_levels,
                                 entitlement=entitlement, served_level=served_level,
                                 level_policy_readable=not level_policy_unreadable,
                                 withheld=withheld, reached=reached)
            if projected is None:
                continue
            snapshot, record = projected
            snapshots.append(snapshot)
            evidence.append(record)
            kept += 1
            if kept >= bounded:
                break
    outcome = str(getattr(truth, "query_outcome", "") or "")
    # What could not be read, and only when it was actually asked. A rung whose
    # own permission fields are unreadable is refused rather than served — the
    # safe answer — but it is still a rung this list was judged against without
    # knowing what the merchant meant by it, and that makes the list incomplete
    # in a way the customer cannot see. When nothing here was conditioned on a
    # rung, neither failure cost anything and neither is claimed.
    unreadable = (_unreadable_reason(level_policy_unreadable, broken_rungs)
                  if reached.get(LEVEL_CONDITIONED, 0) > 0 else None)
    partial = outcome == PROMOTION_PARTIAL_FAILURE or unreadable is not None
    # The standing and the rung served are reported side by side: they differ
    # exactly when the store's policy capped this customer, and never silently.
    entitlement_view = {**entitlement.as_dict(), "served_level": served_level or ""}
    # One line that answers "why did nothing reach me?" without a person having
    # to reconstruct it from a transcript. The merchant's records said how many
    # promotions were live; this says how many survived each gate and which
    # gate took the rest. No code, no customer identifier, no secret.
    logger.info(
        "[PROMOTION_PROJECTION] tenant=%s customer=%s considered=%d kept=%d "
        "withheld=%s standing=%s level=%s served=%s determined=%s ladder_unreadable=%s "
        "unreadable_rungs=%s",
        int(context.tenant_id), "yes" if customer_id is not None else "none",
        considered, len(snapshots),
        ",".join(f"{reason}:{count}" for reason, count in sorted(withheld.items())) or "none",
        entitlement.reason, entitlement.resolved_level or "none", served_level or "none",
        entitlement.determined, level_policy_unreadable or "no",
        ",".join(broken_rungs) or "none",
    )
    if not snapshots:
        if bool(getattr(truth, "query_failed", False)) or outcome == PROMOTION_QUERY_FAILED:
            return PromotionListResult(status="error", query_outcome=outcome or PROMOTION_QUERY_FAILED,
                                       partial=partial, failure_reason="promotion_query_failed",
                                       entitlement=entitlement_view, withheld=dict(withheld))
        if unreadable is not None:
            # The store held codes conditioned on a rung and we could not read
            # the settings that say whether they may be shared. "This store has
            # nothing for you" would be an answer we did not reach, so the
            # failure is reported as the failure it is.
            return PromotionListResult(
                status="error", query_outcome=outcome or NO_VALID_PROMOTIONS, partial=True,
                failure_reason=unreadable, entitlement=entitlement_view, withheld=dict(withheld))
        return PromotionListResult(status="not_found", query_outcome=outcome or NO_VALID_PROMOTIONS,
                                   partial=partial, failure_reason="no_valid_shareable_promotions",
                                   entitlement=entitlement_view, withheld=dict(withheld))
    context.register_evidence(evidence)
    return PromotionListResult(status="ok", promotions=snapshots, evidence=evidence,
                               query_outcome=outcome, partial=partial, entitlement=entitlement_view,
                               withheld=dict(withheld),
                               # An offer the merchant published is still theirs to
                               # share; the rung-conditioned codes beside it are the
                               # ones we could not judge, and the list says so.
                               failure_reason=unreadable)


__all__ = ["GENERAL_AUTHORIZED", "LEVEL_CONDITIONED", "LEVEL_ENTITLED", "MAX_CONDITION_IDS",
           "MAX_PROMOTIONS",
           "MERCHANT_AUTHORED_SOURCE_TYPES", "WITHHELD_EXPIRING_TOO_SOON",
           "WITHHELD_LEVEL_NOT_ALLOWED", "WITHHELD_LEVEL_NOT_EARNED",
           "WITHHELD_LEVEL_NOT_SERVED", "WITHHELD_LEVEL_POLICY_UNREADABLE",
           "WITHHELD_NOT_PUBLISHED",
           "WITHHELD_PERSONAL_TO_ANOTHER", "WITHHELD_UNUSABLE_RECORD",
           "list_shareable_promotions_impl"]
