"""
services/offer_decision_service.py
──────────────────────────────────
Single shared **decision layer** for every offer Nahla issues.

This module is the only place in the system that decides:

    "For this customer, on this surface, at this moment — should we issue
     a discount? If yes, which kind (promotion vs coupon), what value, and
     how long should it stay valid?"

It does NOT issue coupons or materialise promotions itself. The existing
primitives (`promotion_engine.materialise_for_customer` and
`coupon_generator.{pick_coupon_for_segment, create_on_demand}`) keep their
single responsibility — they execute the decision the policy made.

Why a separate layer
────────────────────
Before this module, three call sites independently picked a discount:

  1. `automation_engine._resolve_auto_coupon` (campaign-time)
  2. `orchestrator.adapter._execute_suggest_coupon` (chat-time)
  3. `customer_intelligence.recompute_profile_for_customer`
     (segment-change autogen)

Each one had its own precedence rules, its own clamping, its own — or
zero — awareness of merchant rules and price-sensitivity signals. That
made it impossible to:

  • apply one consistent merchant cap across all surfaces;
  • feed customer signals (price_sensitivity_score, recommended_discount_pct,
    coupon_usage_rate) into chat AND campaign decisions;
  • close the attribution loop on "which decision generated which order";
  • A/B test or layer a contextual bandit later.

This service is intentionally **deterministic** in v1. The policy is a
small list of explicit, ordered steps — no learning, no exploration. The
ledger writes (every decision recorded with its inputs, outputs, and
realised outcome via `OfferAttributionService`) give us the data needed
to swap in a bandit policy later **without changing any caller code**.

Surfaces
────────
    automation       — automation engine campaign sends
    chat             — conversational orchestrator suggest_coupon action
    segment_change   — customer_intelligence event-driven autogen

Public API
──────────
    OfferDecisionContext      input dataclass
    OfferDecisionSignals      nested dataclass for customer signals
    OfferDecision             output dataclass
    decide(db, ctx)           pure decision (writes one ledger row)
    apply_decision(...)       dispatch the decision to the right primitive
                              (promotion_engine / coupon_generator) and
                              stamp `decision_id` onto the resulting Coupon
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from models import (
    Coupon,
    CustomerProfile,
    OfferDecisionLedger,
    PriceSensitivityScore,
    Promotion,
    TenantSettings,
)


logger = logging.getLogger(__name__)


# Current deterministic policy version. Bumped when the algorithm changes
# in a way that should be visible in analytics ("v1.0 vs v1.1 breakdown").
POLICY_VERSION = "v1.0-deterministic"

# Allowed values — kept here so callers can import without fishing through
# string literals. Mirrors `DISCOUNT_SOURCE_*` constants in the automation
# engine (they share the wire-shape).
SURFACE_AUTOMATION       = "automation"
SURFACE_CHAT             = "chat"
SURFACE_SEGMENT_CHANGE   = "segment_change"
ALLOWED_SURFACES         = {SURFACE_AUTOMATION, SURFACE_CHAT, SURFACE_SEGMENT_CHANGE}

SOURCE_PROMOTION = "promotion"
SOURCE_COUPON    = "coupon"
SOURCE_NONE      = "none"


# ── Dataclasses ──────────────────────────────────────────────────────────

@dataclass
class OfferDecisionSignals:
    """
    Snapshot of every signal the policy is allowed to read.

    Filled by the caller (we don't fetch lazily inside `decide` — we want
    the snapshot to match exactly what the policy saw, which is what gets
    persisted to the ledger).
    """
    segment:                    Optional[str] = None    # CustomerProfile.segment
    customer_status:            Optional[str] = None    # CustomerProfile.customer_status
    rfm_segment:                Optional[str] = None
    total_orders:               int = 0
    total_spend_sar:            float = 0.0
    avg_order_value_sar:        float = 0.0
    last_order_at:              Optional[str] = None    # ISO string
    churn_risk_score:           float = 0.0
    is_returning:               bool = False
    # PriceSensitivityScore fields
    price_sensitivity_score:    float = 0.5
    recommended_discount_pct:   int = 0
    coupon_usage_rate:          float = 0.0
    # Cart context (chat / cart-abandoned only)
    cart_total:                 Optional[float] = None
    # How many offers this customer received in the last cap-window
    recent_offers_in_window:    int = 0


@dataclass
class OfferDecisionContext:
    tenant_id:          int
    surface:            str                                  # one of ALLOWED_SURFACES
    customer_id:        Optional[int] = None
    automation_id:      Optional[int] = None
    automation_type:    Optional[str] = None
    event_id:           Optional[int] = None
    # Caller hints — the policy reads these but is free to override:
    suggested_source:       Optional[str] = None             # promotion | coupon | none
    suggested_promotion_id: Optional[int] = None
    suggested_discount_pct: Optional[int] = None             # e.g. chat path: model said "20%"
    suggested_segment:      Optional[str] = None             # config.coupon_segment override
    # Snapshot
    signals:            OfferDecisionSignals = field(default_factory=OfferDecisionSignals)


@dataclass
class OfferDecision:
    decision_id:        str
    source:             str                          # promotion | coupon | none
    promotion_id:       Optional[int] = None
    discount_type:      Optional[str] = None         # percentage | fixed | free_shipping
    discount_value:     Optional[float] = None
    validity_days:      Optional[int] = None
    min_order_amount:   Optional[float] = None
    # Ordered short codes — explainability + future bandit features.
    reason_codes:       List[str] = field(default_factory=list)
    # The segment the policy ultimately picked (used by the coupon path).
    segment:            Optional[str] = None
    # Bandit-ready, always None in v1.
    experiment_arm:     Optional[str] = None


# ── Public entry points ──────────────────────────────────────────────────

def decide(db: Session, ctx: OfferDecisionContext) -> OfferDecision:
    """
    Run the deterministic policy and persist a ledger row.

    Never raises — a misconfigured tenant or missing signal must not block
    a WhatsApp send. On any internal failure, returns a `SOURCE_NONE`
    decision with reason_code=`policy_exception` so the caller falls
    through to the no-discount path.
    """
    if ctx.surface not in ALLOWED_SURFACES:
        # Defensive: caller passed a typo. Log and degrade to no-discount.
        logger.warning("[OfferDecisionService] unknown surface=%r — degrading to none", ctx.surface)
        return _no_offer_decision(reason="unknown_surface")

    try:
        decision = _run_policy(db, ctx)
    except Exception as exc:
        logger.exception(
            "[OfferDecisionService] policy raised tenant=%s surface=%s: %s",
            ctx.tenant_id, ctx.surface, exc,
        )
        decision = _no_offer_decision(reason="policy_exception")

    # Persist the ledger row even for `none` decisions — knowing how often
    # we *chose not* to send a discount is itself a useful signal.
    try:
        _write_ledger(db, ctx, decision)
    except Exception as exc:
        logger.exception(
            "[OfferDecisionService] ledger write failed (decision still returned) "
            "tenant=%s decision_id=%s: %s",
            ctx.tenant_id, decision.decision_id, exc,
        )
    return decision


async def apply_decision(
    db: Session,
    *,
    ctx: OfferDecisionContext,
    decision: OfferDecision,
    customer: Any = None,
) -> Dict[str, str]:
    """
    Materialise the decision via the existing primitives.

    Returns a `{"discount_code": ..., "vip_coupon": ..., "coupon_code": ...}`
    dict (matching the shape `_resolve_auto_coupon` has always returned)
    on success, or `{}` on any failure or `SOURCE_NONE` decision.

    Stamps `decision_id` onto the resulting `Coupon.extra_metadata` so the
    attribution service can join order → coupon → decision.
    """
    if decision.source == SOURCE_NONE:
        return {}

    coupon: Optional[Coupon] = None

    if decision.source == SOURCE_PROMOTION:
        coupon = await _apply_promotion(db, ctx=ctx, decision=decision, customer=customer)
    elif decision.source == SOURCE_COUPON:
        coupon = await _apply_coupon(db, ctx=ctx, decision=decision, customer=customer)
    else:  # pragma: no cover — guarded by the SOURCE_NONE check above
        logger.warning("[OfferDecisionService] unknown source=%r", decision.source)
        return {}

    if coupon is None or not getattr(coupon, "code", None):
        return {}

    _stamp_decision_id_on_coupon(db, coupon, decision.decision_id)
    _link_coupon_to_ledger(db, decision.decision_id, coupon.id)

    code = str(coupon.code).strip().upper()
    return {"discount_code": code, "vip_coupon": code, "coupon_code": code}


# ── Convenience: signal collection from CustomerProfile ──────────────────

def collect_signals(
    db: Session,
    *,
    tenant_id: int,
    customer_id: Optional[int],
    cart_total: Optional[float] = None,
    recent_offers_in_window: int = 0,
) -> OfferDecisionSignals:
    """
    Read CustomerProfile + PriceSensitivityScore and pack into a snapshot.

    Optional helper — callers may also build their own `OfferDecisionSignals`
    directly (e.g. the chat path which already loaded a memory dict).
    Missing rows just produce default values; never raises.
    """
    sig = OfferDecisionSignals(
        cart_total=cart_total,
        recent_offers_in_window=recent_offers_in_window,
    )
    if customer_id is None:
        return sig

    try:
        profile = (
            db.query(CustomerProfile)
            .filter(
                CustomerProfile.tenant_id == tenant_id,
                CustomerProfile.customer_id == customer_id,
            )
            .first()
        )
        if profile is not None:
            sig.segment             = profile.segment
            sig.customer_status     = profile.customer_status
            sig.rfm_segment         = profile.rfm_segment
            sig.total_orders        = int(profile.total_orders or 0)
            sig.total_spend_sar     = float(profile.total_spend_sar or 0.0)
            sig.avg_order_value_sar = float(profile.average_order_value_sar or 0.0)
            sig.churn_risk_score    = float(profile.churn_risk_score or 0.0)
            sig.is_returning        = bool(profile.is_returning)
            if profile.last_order_at:
                sig.last_order_at = profile.last_order_at.isoformat()
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("[OfferDecisionService] CustomerProfile lookup failed: %s", exc)

    try:
        pss = (
            db.query(PriceSensitivityScore)
            .filter(
                PriceSensitivityScore.tenant_id == tenant_id,
                PriceSensitivityScore.customer_id == customer_id,
            )
            .first()
        )
        if pss is not None:
            sig.price_sensitivity_score  = float(pss.score or 0.5)
            sig.recommended_discount_pct = int(pss.recommended_discount_pct or 0)
            sig.coupon_usage_rate        = float(pss.coupon_usage_rate or 0.0)
    except Exception as exc:  # pragma: no cover
        logger.debug("[OfferDecisionService] PriceSensitivityScore lookup failed: %s", exc)

    return sig


# ── Internal: deterministic policy ───────────────────────────────────────

def _run_policy(db: Session, ctx: OfferDecisionContext) -> OfferDecision:
    """
    Ordered policy steps. Each step appends to `reason_codes` so the
    rationale is explainable end-to-end.

    Order (first match wins for the *source*; later steps refine value):

      1. Caller's explicit suggestion (legacy parity for the automation
         engine: `config.discount_source` / step-level overrides).
      2. Eligible active promotion for this customer's segment.
      3. Merchant-edited rule for this automation_type.
      4. Segment defaults (only for the coupon path).
      5. Frequency cap — may downgrade the source to `none`.
      6. Hard merchant max-discount cap — clamps the value.
      7. Signal-driven nudge (price_sensitivity, recommended_discount).
    """
    decision_id = uuid.uuid4().hex
    reasons: List[str] = []

    # Step 1 — caller's explicit suggestion wins over policy nudges. This
    # gives 100% behavioural parity with `_resolve_discount_source` as it
    # behaved before this layer was introduced.
    suggested_src = (ctx.suggested_source or "").strip().lower() or None
    chosen_source: Optional[str] = None
    chosen_promo_id: Optional[int] = None

    if suggested_src in (SOURCE_PROMOTION, SOURCE_COUPON, SOURCE_NONE):
        chosen_source = suggested_src
        if chosen_source == SOURCE_PROMOTION and ctx.suggested_promotion_id:
            chosen_promo_id = int(ctx.suggested_promotion_id)
            reasons.append("legacy_step_promotion_override")
        elif chosen_source == SOURCE_PROMOTION:
            # The caller said "promotion" but didn't tell us which one.
            # Fall back to looking one up below.
            chosen_source = None
            reasons.append("explicit_promotion_no_id_fallback")
        elif chosen_source == SOURCE_COUPON:
            reasons.append("legacy_step_coupon_override")
        elif chosen_source == SOURCE_NONE:
            reasons.append("explicit_none")

    # Step 2 — eligible active promotion lookup (only if not already set).
    if chosen_source is None:
        promo = _find_eligible_promotion(db, ctx)
        if promo is not None:
            chosen_source = SOURCE_PROMOTION
            chosen_promo_id = promo.id
            reasons.append("auto_eligible_promotion")

    # Step 3 — merchant rule for this automation type → coupon path.
    rule = _lookup_merchant_rule(db, ctx)
    if chosen_source is None and rule is not None:
        chosen_source = SOURCE_COUPON
        reasons.append("merchant_rule_applied")
    elif chosen_source == SOURCE_COUPON and rule is not None:
        reasons.append("merchant_rule_applied")

    # If after all of the above we still have no source, send no offer.
    if chosen_source is None:
        chosen_source = SOURCE_NONE
        reasons.append("no_eligible_source")

    # Build the value/validity from the strongest available source.
    discount_type, discount_value, validity_days, min_order = _resolve_value_and_validity(
        db, ctx, chosen_source, chosen_promo_id, rule, reasons,
    )

    # Step 5 — frequency cap (downgrade to none).
    cap = _read_offer_frequency_cap(db, ctx.tenant_id)
    if cap is not None and ctx.signals.recent_offers_in_window >= cap and chosen_source != SOURCE_NONE:
        # Honour the cap unless the caller explicitly asked for a promotion
        # (campaigns the merchant hand-configured should still run).
        if suggested_src != SOURCE_PROMOTION:
            chosen_source     = SOURCE_NONE
            chosen_promo_id   = None
            discount_type     = None
            discount_value    = None
            validity_days     = None
            min_order         = None
            reasons.append("frequency_cap_hit")

    # Step 6 — hard merchant cap. Clamp percentages only, and ONLY for the
    # coupon path. A `Promotion` carries terms the merchant configured by
    # hand on the Promotions page; capping that on the way out would
    # silently override their explicit intent. Promotions are already
    # bounded by their own `discount_value` field.
    if chosen_source == SOURCE_COUPON and discount_type == "percentage" and discount_value is not None:
        max_pct = _read_max_discount_pct(db, ctx.tenant_id)
        if max_pct is not None and discount_value > max_pct:
            discount_value = float(max_pct)
            reasons.append("capped_by_max_discount")

    # Step 7 — signal nudges. We only nudge **upward** within the cap, and
    # only when the policy chose a coupon (we never tweak a merchant-defined
    # promotion's stored value — that is the merchant's prerogative).
    if chosen_source == SOURCE_COUPON and discount_type == "percentage" and discount_value is not None:
        nudged = _signal_nudge(discount_value, ctx.signals)
        if nudged != discount_value:
            max_pct = _read_max_discount_pct(db, ctx.tenant_id)
            if max_pct is not None:
                nudged = min(nudged, float(max_pct))
            if nudged != discount_value:
                discount_value = nudged
                reasons.append("price_sensitivity_nudge")

    return OfferDecision(
        decision_id      = decision_id,
        source           = chosen_source,
        promotion_id     = chosen_promo_id,
        discount_type    = discount_type,
        discount_value   = discount_value,
        validity_days    = validity_days,
        min_order_amount = min_order,
        reason_codes     = reasons,
        segment          = _resolve_segment_for_coupon(ctx),
        experiment_arm   = None,
    )


def _resolve_value_and_validity(
    db: Session,
    ctx: OfferDecisionContext,
    source: str,
    promo_id: Optional[int],
    rule: Optional[Dict[str, Any]],
    reasons: List[str],
) -> tuple[Optional[str], Optional[float], Optional[int], Optional[float]]:
    """
    Pick discount_type / discount_value / validity_days / min_order_amount.

    Order: promotion record → merchant rule → caller suggestion → segment
    catalogue defaults. We don't fail-loud here — missing fields just leave
    the corresponding outputs None and the caller handles them.
    """
    if source == SOURCE_PROMOTION and promo_id is not None:
        promo = db.query(Promotion).filter(Promotion.id == promo_id).first()
        if promo is not None:
            ptype = (promo.promotion_type or "").lower()
            dt = "percentage" if ptype == "percentage" else (
                 "fixed"     if ptype in ("fixed", "threshold_discount") else
                 "free_shipping" if ptype == "free_shipping" else
                 ptype)
            dv = float(promo.discount_value) if promo.discount_value is not None else None
            cond = dict(promo.conditions or {})
            mo  = cond.get("min_order_amount")
            return dt, dv, None, (float(mo) if mo is not None else None)

    if source == SOURCE_COUPON:
        # Merchant rule first — that's the editable surface in the dashboard.
        if rule is not None:
            dt = (rule.get("discount_type") or "percentage").lower()
            dv = rule.get("discount_value")
            vd = rule.get("validity_days")
            mo = rule.get("min_order_amount")
            return (
                dt,
                float(dv) if dv is not None else None,
                int(vd) if isinstance(vd, (int, float)) and int(vd) > 0 else None,
                float(mo) if mo not in (None, "", 0) else None,
            )
        # Fall back to the caller's suggestion (chat path passes the LLM's
        # %), then to the segment catalogue defaults from coupon_generator.
        if ctx.suggested_discount_pct is not None:
            return "percentage", float(ctx.suggested_discount_pct), None, None

        try:
            from services.coupon_generator import SEGMENT_DEFAULTS, _canonical_segment  # noqa: PLC0415
            seg = _canonical_segment(_resolve_segment_for_coupon(ctx) or "active")
            seg_def = SEGMENT_DEFAULTS.get(seg) or SEGMENT_DEFAULTS["active"]
            reasons.append("segment_default_applied")
            return "percentage", float(seg_def["discount_pct"]), int(seg_def["expiry_days"]), None
        except Exception:  # pragma: no cover — defensive
            pass

    return None, None, None, None


def _resolve_segment_for_coupon(ctx: OfferDecisionContext) -> Optional[str]:
    """
    Segment used by the coupon generator. Order:
      1. caller suggestion (config.coupon_segment)
      2. CustomerProfile.customer_status (live signal)
      3. CustomerProfile.segment
      4. None → caller picks a sensible default ("active").
    """
    if ctx.suggested_segment:
        return str(ctx.suggested_segment)
    return ctx.signals.customer_status or ctx.signals.segment


# ── Internal: side-tables (rules, promotions, settings) ─────────────────

def _lookup_merchant_rule(db: Session, ctx: OfferDecisionContext) -> Optional[Dict[str, Any]]:
    """Mirror of `routers.coupons.get_rule_for_automation` — kept here so
    the decision service has zero hard dependency on the router module
    (only an import inside the call to dodge circular imports)."""
    if not ctx.automation_type:
        return None
    try:
        from core.tenant import get_or_create_settings  # noqa: PLC0415
        from routers.coupons import get_rule_for_automation  # noqa: PLC0415

        ts = get_or_create_settings(db, ctx.tenant_id)
        return get_rule_for_automation(ts, ctx.automation_type)
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("[OfferDecisionService] rule lookup failed: %s", exc)
        return None


def _find_eligible_promotion(db: Session, ctx: OfferDecisionContext) -> Optional[Promotion]:
    """
    Return the first ACTIVE promotion this customer qualifies for, or None.

    Intentionally conservative in v1:
      • only promotions with `customer_segments` matching the customer's
        segment qualify (keeps merchant-defined targeting authoritative);
      • we never auto-pick an open ("everyone") promotion — those are
        better surfaced at checkout via the platform itself.
    """
    if ctx.signals.segment is None and ctx.signals.customer_status is None:
        return None
    seg = ctx.signals.customer_status or ctx.signals.segment
    try:
        from services.promotion_engine import is_promotion_active  # noqa: PLC0415

        candidates = (
            db.query(Promotion)
            .filter(Promotion.tenant_id == ctx.tenant_id, Promotion.status == "active")
            .all()
        )
        for promo in candidates:
            if not is_promotion_active(promo):
                continue
            cond = dict(promo.conditions or {})
            segments_required = cond.get("customer_segments") or []
            if not segments_required:
                continue  # universal promotions handled at checkout, not here
            if seg in segments_required:
                return promo
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("[OfferDecisionService] eligible promotion lookup failed: %s", exc)
    return None


def _read_offer_frequency_cap(db: Session, tenant_id: int) -> Optional[int]:
    """Read `TenantSettings.ai_settings.offer_frequency_cap`. None = uncapped."""
    try:
        ts = db.query(TenantSettings).filter_by(tenant_id=tenant_id).first()
        if ts and isinstance(ts.ai_settings, dict):
            raw = ts.ai_settings.get("offer_frequency_cap")
            if raw in (None, "", 0):
                return None
            return max(1, int(raw))
    except Exception:  # pragma: no cover
        pass
    return None


def _read_max_discount_pct(db: Session, tenant_id: int) -> Optional[int]:
    """Same source-of-truth used by `_get_merchant_limits`."""
    try:
        from services.coupon_generator import _get_merchant_limits  # noqa: PLC0415
        limits = _get_merchant_limits(db, tenant_id)
        return int(limits.get("max_discount") or 0) or None
    except Exception:  # pragma: no cover
        return None


def _signal_nudge(base_value: float, signals: OfferDecisionSignals) -> float:
    """
    Tiny deterministic nudge — kept simple on purpose. The real win comes
    from telemetry-driven future iterations (or a bandit). Today:

      • price_sensitivity_score >= 0.7 → +5 pp (the customer reliably
        responds to discounts; spending +5pp is a proven win).
      • recommended_discount_pct > base → bump to recommended (the
        per-customer signal already exists and is trusted by the chat
        orchestrator's prompt — coupon path now respects it too).
      • coupon_usage_rate < 0.10  → no negative nudge in v1; we leave
        the value alone because reducing discounts on low-redeemers
        is risky without first measuring lift.
    """
    nudged = base_value
    if signals.price_sensitivity_score >= 0.7:
        nudged = max(nudged, base_value + 5.0)
    if signals.recommended_discount_pct and signals.recommended_discount_pct > nudged:
        nudged = float(signals.recommended_discount_pct)
    return nudged


# ── Internal: ledger persistence ─────────────────────────────────────────

def _no_offer_decision(*, reason: str) -> OfferDecision:
    return OfferDecision(
        decision_id  = uuid.uuid4().hex,
        source       = SOURCE_NONE,
        reason_codes = [reason],
    )


def _write_ledger(db: Session, ctx: OfferDecisionContext, decision: OfferDecision) -> None:
    """Persist one ledger row for this decision (no commit — caller's
    transaction owns the lifecycle)."""
    row = OfferDecisionLedger(
        tenant_id           = ctx.tenant_id,
        decision_id         = decision.decision_id,
        surface             = ctx.surface,
        automation_id       = ctx.automation_id,
        event_id            = ctx.event_id,
        customer_id         = ctx.customer_id,
        signals_snapshot    = asdict(ctx.signals),
        chosen_source       = decision.source,
        chosen_promotion_id = decision.promotion_id,
        chosen_coupon_id    = None,                        # filled later
        discount_type       = decision.discount_type,
        discount_value      = (
            Decimal(str(decision.discount_value)) if decision.discount_value is not None else None
        ),
        validity_days       = decision.validity_days,
        reason_codes        = list(decision.reason_codes or []),
        policy_version      = POLICY_VERSION,
        experiment_arm      = decision.experiment_arm,
        attributed          = False,
        created_at          = datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(row)
    try:
        db.flush()
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("[OfferDecisionService] ledger flush failed: %s", exc)


def _stamp_decision_id_on_coupon(db: Session, coupon: Coupon, decision_id: str) -> None:
    meta = dict(coupon.extra_metadata or {})
    if meta.get("decision_id") == decision_id:
        return
    meta["decision_id"] = decision_id
    coupon.extra_metadata = meta
    flag_modified(coupon, "extra_metadata")
    try:
        db.flush()
    except Exception as exc:  # pragma: no cover
        logger.debug("[OfferDecisionService] stamp decision_id flush failed: %s", exc)


def _link_coupon_to_ledger(db: Session, decision_id: str, coupon_id: int) -> None:
    try:
        row = (
            db.query(OfferDecisionLedger)
            .filter(OfferDecisionLedger.decision_id == decision_id)
            .first()
        )
        if row is not None and row.chosen_coupon_id != coupon_id:
            row.chosen_coupon_id = coupon_id
            db.flush()
    except Exception as exc:  # pragma: no cover
        logger.debug("[OfferDecisionService] link coupon→ledger failed: %s", exc)


# ── Internal: per-source dispatchers (delegate to existing primitives) ──

async def _apply_promotion(
    db: Session,
    *,
    ctx: OfferDecisionContext,
    decision: OfferDecision,
    customer: Any,
) -> Optional[Coupon]:
    if not decision.promotion_id:
        return None
    try:
        from services.promotion_engine import materialise_for_customer  # noqa: PLC0415
        return await materialise_for_customer(
            db,
            promotion_id = int(decision.promotion_id),
            tenant_id    = ctx.tenant_id,
            customer_id  = getattr(customer, "id", None) if customer is not None else ctx.customer_id,
        )
    except Exception as exc:
        logger.warning(
            "[OfferDecisionService] promotion materialise failed tenant=%s promo=%s: %s",
            ctx.tenant_id, decision.promotion_id, exc,
        )
        return None


async def _apply_coupon(
    db: Session,
    *,
    ctx: OfferDecisionContext,
    decision: OfferDecision,
    customer: Any,
) -> Optional[Coupon]:
    try:
        from services.coupon_generator import CouponGeneratorService  # noqa: PLC0415

        svc = CouponGeneratorService(db, ctx.tenant_id)
        segment = decision.segment or "active"
        coupon = svc.pick_coupon_for_segment(segment)
        if coupon is not None:
            return coupon

        # Forward the merchant-edited overrides if present.
        requested_pct: Optional[int] = None
        if decision.discount_type == "percentage" and decision.discount_value is not None:
            requested_pct = int(round(float(decision.discount_value)))
        validity_override = (
            int(decision.validity_days) if decision.validity_days else None
        )
        return await svc.create_on_demand(
            segment,
            requested_discount_pct = requested_pct,
            validity_days_override = validity_override,
        )
    except Exception as exc:
        logger.warning(
            "[OfferDecisionService] coupon resolution failed tenant=%s segment=%s: %s",
            ctx.tenant_id, decision.segment, exc,
        )
        return None
