"""
tests/test_offer_decision.py
─────────────────────────────
Coverage for the new shared offer-decision layer (Phase 1: foundation).

Two responsibilities, two layers of tests:

  1. **OfferDecisionService.decide** — pure deterministic policy. We lock
     down the ordered behaviour: caller-suggested override > auto promo
     lookup > merchant rule > segment defaults; plus the cross-cutting
     guards (frequency cap, hard merchant cap, signal nudge). Every
     decision must persist a ledger row with the snapshot it saw.

  2. **OfferAttributionService.attribute_order_to_decision** — the
     close-the-loop hook. Given a paid order whose coupon was issued by
     a decision, the ledger row must flip to attributed=True with the
     order_id, revenue_amount, and redeemed_at populated. Idempotent on
     re-runs.

  3. **Seed parity** — every seed automation routed through
     `OfferDecisionService.decide` (with its config injected as the
     caller hint) must yield a decision compatible with what the
     legacy `_resolve_discount_source` would have picked. This is the
     guardrail that lets Phase 2 swap the call site without behavioural
     drift.

Bandit-readiness invariants (light-touch — actual implementation is
Phase 6): every persisted row carries a non-null `policy_version` and
its `experiment_arm` defaults to None.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Tuple

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import (  # noqa: E402
    Base,
    Coupon,
    Customer,
    CustomerProfile,
    OfferDecisionLedger,
    Order,
    PriceSensitivityScore,
    Promotion,
    Tenant,
    TenantSettings,
)
from services.offer_decision_service import (  # noqa: E402
    POLICY_VERSION,
    SOURCE_COUPON,
    SOURCE_NONE,
    SOURCE_PROMOTION,
    SURFACE_AUTOMATION,
    SURFACE_CHAT,
    SURFACE_SEGMENT_CHANGE,
    OfferDecisionContext,
    OfferDecisionSignals,
    apply_decision,
    collect_signals,
    decide,
)
from services.offer_attribution_service import (  # noqa: E402
    attribute_order_to_decision,
)
from services.promotion_engine import ACTIVE_STATUS  # noqa: E402
from core.automations_seed import SEED_AUTOMATIONS  # noqa: E402


# ── DB harness (shared shape with test_promotions / test_coupon_rules) ──

def _make_db() -> Tuple[Any, Any]:
    engine = create_engine("sqlite:///:memory:")
    _saved: list[tuple] = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig_type in _saved:
        col.type = orig_type
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed_tenant(db) -> Tenant:
    t = Tenant(name="T", is_active=True)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _seed_settings(
    db,
    tenant_id: int,
    *,
    max_discount: int | None = None,
    offer_frequency_cap: int | None = None,
    coupon_rules: dict | None = None,
) -> TenantSettings:
    ai = {}
    if max_discount is not None:
        # `_get_merchant_limits` reads this as an int (max value) — see
        # services/coupon_generator.py:_get_merchant_limits.
        ai["allowed_discount_levels"] = int(max_discount)
    if offer_frequency_cap is not None:
        ai["offer_frequency_cap"] = offer_frequency_cap

    extra: dict = {}
    if coupon_rules is not None:
        extra["coupons_dashboard"] = {"rules": coupon_rules}

    ts = TenantSettings(
        tenant_id=tenant_id,
        ai_settings=ai or None,
        extra_metadata=extra or None,
    )
    db.add(ts)
    db.commit()
    db.refresh(ts)
    return ts


def _seed_customer(db, tenant_id: int, phone: str = "+966555111000") -> Customer:
    c = Customer(tenant_id=tenant_id, phone=phone, name="C")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _seed_profile(
    db,
    tenant_id: int,
    customer_id: int,
    *,
    segment: str = "active",
    status: str | None = None,
) -> CustomerProfile:
    p = CustomerProfile(
        tenant_id=tenant_id,
        customer_id=customer_id,
        segment=segment,
        customer_status=status or segment,
        total_orders=2,
        total_spend_sar=300.0,
        average_order_value_sar=150.0,
        churn_risk_score=0.2,
        is_returning=True,
        last_order_at=datetime.now(timezone.utc).replace(tzinfo=None),
        metrics_computed_at=datetime.now(timezone.utc).replace(tzinfo=None),
        last_recomputed_reason="test",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _seed_promotion(
    db,
    tenant_id: int,
    *,
    customer_segments: list[str],
    discount_value: float = 25.0,
) -> Promotion:
    promo = Promotion(
        tenant_id=tenant_id,
        name="Auto eligible promo",
        promotion_type="percentage",
        discount_value=Decimal(str(discount_value)),
        status=ACTIVE_STATUS,
        conditions={"customer_segments": customer_segments},
        usage_count=0,
        extra_metadata={},
    )
    db.add(promo)
    db.commit()
    db.refresh(promo)
    return promo


class _NoCloseSession:
    """Proxy a real Session but make `.close()` a no-op so tests can keep
    using objects after the code under test calls close. Necessary
    because the orchestrator adapter calls db.close() in a finally block,
    which detaches every test object from the session."""
    def __init__(self, real_session) -> None:
        self._s = real_session

    def __getattr__(self, name):
        return getattr(self._s, name)

    def close(self) -> None:  # no-op — test owns the lifecycle
        return None


def _ctx(tenant_id: int, **overrides) -> OfferDecisionContext:
    base: dict = {
        "tenant_id": tenant_id,
        "surface":   SURFACE_AUTOMATION,
        "signals":   OfferDecisionSignals(segment="active", customer_status="active"),
    }
    base.update(overrides)
    return OfferDecisionContext(**base)


# ═════════════════════════════════════════════════════════════════════════
# 1. Pure policy decisions
# ═════════════════════════════════════════════════════════════════════════

class TestDeciderPolicy:
    def test_explicit_promotion_id_wins(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            promo = _seed_promotion(db, t.id, customer_segments=["active"])
            decision = decide(
                db,
                _ctx(
                    t.id,
                    suggested_source="promotion",
                    suggested_promotion_id=promo.id,
                    automation_type="seasonal_offer",
                ),
            )
            assert decision.source == SOURCE_PROMOTION
            assert decision.promotion_id == promo.id
            assert "legacy_step_promotion_override" in decision.reason_codes
            assert decision.discount_type == "percentage"
            assert decision.discount_value == 25.0
        finally:
            db.close(); engine.dispose()

    def test_explicit_none_short_circuits(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            decision = decide(db, _ctx(t.id, suggested_source="none"))
            assert decision.source == SOURCE_NONE
            assert "explicit_none" in decision.reason_codes
        finally:
            db.close(); engine.dispose()

    def test_auto_promotion_lookup_when_segment_eligible(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            promo = _seed_promotion(db, t.id, customer_segments=["vip"])
            decision = decide(
                db,
                _ctx(
                    t.id,
                    signals=OfferDecisionSignals(segment="vip", customer_status="vip"),
                ),
            )
            assert decision.source == SOURCE_PROMOTION
            assert decision.promotion_id == promo.id
            assert "auto_eligible_promotion" in decision.reason_codes
        finally:
            db.close(); engine.dispose()

    def test_no_eligible_source_returns_none(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            # No promotion seeded; no merchant rule injected; no caller hint.
            decision = decide(
                db,
                _ctx(
                    t.id,
                    signals=OfferDecisionSignals(segment="active", customer_status="active"),
                ),
            )
            assert decision.source == SOURCE_NONE
            assert "no_eligible_source" in decision.reason_codes
        finally:
            db.close(); engine.dispose()

    def test_explicit_coupon_with_segment_default(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            _seed_settings(db, t.id, max_discount=30)
            decision = decide(
                db,
                _ctx(
                    t.id,
                    suggested_source="coupon",
                    suggested_segment="vip",
                    signals=OfferDecisionSignals(segment="vip", customer_status="vip"),
                ),
            )
            assert decision.source == SOURCE_COUPON
            assert "legacy_step_coupon_override" in decision.reason_codes
            assert decision.segment == "vip"
            # Segment default applied (SEGMENT_DEFAULTS catalogue).
            assert decision.discount_type == "percentage"
            assert decision.discount_value == 20.0   # vip default
            assert "segment_default_applied" in decision.reason_codes
        finally:
            db.close(); engine.dispose()

    def test_caller_suggested_pct_used_for_chat(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            _seed_settings(db, t.id, max_discount=20)
            decision = decide(
                db,
                _ctx(
                    t.id,
                    surface=SURFACE_CHAT,
                    suggested_source="coupon",
                    suggested_discount_pct=12,
                    suggested_segment="active",
                ),
            )
            assert decision.source == SOURCE_COUPON
            assert decision.discount_type == "percentage"
            assert decision.discount_value == 12.0
        finally:
            db.close(); engine.dispose()


class TestDeciderGuards:
    def test_frequency_cap_hits_downgrades_to_none(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            _seed_settings(db, t.id, offer_frequency_cap=2)
            sig = OfferDecisionSignals(
                segment="active", customer_status="active", recent_offers_in_window=3
            )
            decision = decide(
                db,
                _ctx(t.id, suggested_source="coupon", signals=sig),
            )
            assert decision.source == SOURCE_NONE
            assert "frequency_cap_hit" in decision.reason_codes
        finally:
            db.close(); engine.dispose()

    def test_frequency_cap_does_not_drop_explicit_promotion(self) -> None:
        """Merchant-configured promotions are authoritative — the cap is
        an AI guard, not a merchant kill-switch."""
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            _seed_settings(db, t.id, offer_frequency_cap=1)
            promo = _seed_promotion(db, t.id, customer_segments=["active"])
            sig = OfferDecisionSignals(
                segment="active", customer_status="active", recent_offers_in_window=10
            )
            decision = decide(
                db,
                _ctx(
                    t.id,
                    suggested_source="promotion",
                    suggested_promotion_id=promo.id,
                    signals=sig,
                ),
            )
            assert decision.source == SOURCE_PROMOTION
            assert "frequency_cap_hit" not in decision.reason_codes
        finally:
            db.close(); engine.dispose()

    def test_max_discount_cap_clamps_percentage(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            _seed_settings(db, t.id, max_discount=15)
            decision = decide(
                db,
                _ctx(
                    t.id,
                    surface=SURFACE_CHAT,
                    suggested_source="coupon",
                    suggested_discount_pct=40,
                    suggested_segment="active",
                ),
            )
            assert decision.source == SOURCE_COUPON
            assert decision.discount_value == 15.0
            assert "capped_by_max_discount" in decision.reason_codes
        finally:
            db.close(); engine.dispose()

    def test_signal_nudge_bumps_when_price_sensitive(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            _seed_settings(db, t.id, max_discount=30)
            sig = OfferDecisionSignals(
                segment="active",
                customer_status="active",
                price_sensitivity_score=0.85,
            )
            decision = decide(
                db,
                _ctx(
                    t.id,
                    surface=SURFACE_CHAT,
                    suggested_source="coupon",
                    suggested_discount_pct=10,
                    suggested_segment="active",
                    signals=sig,
                ),
            )
            assert decision.source == SOURCE_COUPON
            assert decision.discount_value == 15.0
            assert "price_sensitivity_nudge" in decision.reason_codes
        finally:
            db.close(); engine.dispose()

    def test_signal_nudge_respects_cap(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            _seed_settings(db, t.id, max_discount=12)
            sig = OfferDecisionSignals(
                segment="active",
                customer_status="active",
                price_sensitivity_score=0.95,
                recommended_discount_pct=25,
            )
            decision = decide(
                db,
                _ctx(
                    t.id,
                    surface=SURFACE_CHAT,
                    suggested_source="coupon",
                    suggested_discount_pct=10,
                    suggested_segment="active",
                    signals=sig,
                ),
            )
            assert decision.discount_value == 12.0
            assert "capped_by_max_discount" in decision.reason_codes or \
                   "price_sensitivity_nudge" in decision.reason_codes
        finally:
            db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════
# 2. Ledger persistence
# ═════════════════════════════════════════════════════════════════════════

class TestLedger:
    def test_every_decision_writes_one_row(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            decide(db, _ctx(t.id, suggested_source="none"))
            db.commit()
            rows = db.query(OfferDecisionLedger).filter_by(tenant_id=t.id).all()
            assert len(rows) == 1
            row = rows[0]
            assert row.surface == SURFACE_AUTOMATION
            assert row.chosen_source == SOURCE_NONE
            assert row.policy_version == POLICY_VERSION
            assert row.experiment_arm is None
            assert row.attributed is False
            # Snapshot persisted with the segment we passed in.
            assert isinstance(row.signals_snapshot, dict)
            assert row.signals_snapshot.get("segment") == "active"
        finally:
            db.close(); engine.dispose()

    def test_promotion_decision_persists_promotion_id(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            promo = _seed_promotion(db, t.id, customer_segments=["active"])
            decision = decide(
                db,
                _ctx(
                    t.id,
                    suggested_source="promotion",
                    suggested_promotion_id=promo.id,
                ),
            )
            db.commit()
            row = (
                db.query(OfferDecisionLedger)
                .filter_by(decision_id=decision.decision_id)
                .one()
            )
            assert row.chosen_source == SOURCE_PROMOTION
            assert row.chosen_promotion_id == promo.id
            assert row.discount_type == "percentage"
            assert Decimal(str(row.discount_value)) == Decimal("25")
        finally:
            db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════
# 3. Apply decision → coupon stamping
# ═════════════════════════════════════════════════════════════════════════

class TestApplyDecisionStampsCoupon:
    def test_promotion_apply_stamps_decision_id_on_coupon(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            customer = _seed_customer(db, t.id)
            _seed_profile(db, t.id, customer.id, segment="vip")
            promo = _seed_promotion(db, t.id, customer_segments=["vip"])

            ctx = _ctx(
                t.id,
                customer_id=customer.id,
                suggested_source="promotion",
                suggested_promotion_id=promo.id,
                signals=OfferDecisionSignals(segment="vip", customer_status="vip"),
            )
            decision = decide(db, ctx)
            extras = asyncio.run(apply_decision(db, ctx=ctx, decision=decision, customer=customer))

            assert extras.get("coupon_code")
            coupon = (
                db.query(Coupon)
                .filter_by(tenant_id=t.id, code=extras["coupon_code"])
                .one()
            )
            assert (coupon.extra_metadata or {}).get("decision_id") == decision.decision_id

            # Ledger row was back-linked to the issued coupon.
            row = db.query(OfferDecisionLedger).filter_by(decision_id=decision.decision_id).one()
            assert row.chosen_coupon_id == coupon.id
        finally:
            db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════
# 4. Attribution
# ═════════════════════════════════════════════════════════════════════════

class TestAttribution:
    def _seed_paid_order(self, db, tenant_id: int, *, coupon_code: str, total: float = 250.0) -> Order:
        o = Order(
            tenant_id=tenant_id,
            external_id="ext-1",
            status="paid",
            total=total,
            customer_info={"phone": "+966555111000"},
            extra_metadata={"coupon_code": coupon_code},
        )
        db.add(o)
        db.commit()
        db.refresh(o)
        return o

    def test_attributes_paid_order_to_its_decision(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            customer = _seed_customer(db, t.id)
            _seed_profile(db, t.id, customer.id, segment="vip")
            promo = _seed_promotion(db, t.id, customer_segments=["vip"])

            ctx = _ctx(
                t.id,
                customer_id=customer.id,
                suggested_source="promotion",
                suggested_promotion_id=promo.id,
                signals=OfferDecisionSignals(segment="vip", customer_status="vip"),
            )
            decision = decide(db, ctx)
            extras = asyncio.run(apply_decision(db, ctx=ctx, decision=decision, customer=customer))
            db.commit()

            order = self._seed_paid_order(db, t.id, coupon_code=extras["coupon_code"])

            attributed = attribute_order_to_decision(
                db,
                tenant_id=t.id,
                order_id=order.id,
                payload={"amount": float(order.total)},
            )
            assert attributed is not None
            db.commit()

            row = db.query(OfferDecisionLedger).filter_by(decision_id=decision.decision_id).one()
            assert row.attributed is True
            assert row.order_id == order.id
            assert row.revenue_amount is not None and Decimal(str(row.revenue_amount)) == Decimal("250")
            assert row.redeemed_at is not None
        finally:
            db.close(); engine.dispose()

    def test_attribution_is_idempotent(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            customer = _seed_customer(db, t.id)
            _seed_profile(db, t.id, customer.id, segment="vip")
            promo = _seed_promotion(db, t.id, customer_segments=["vip"])

            ctx = _ctx(
                t.id,
                customer_id=customer.id,
                suggested_source="promotion",
                suggested_promotion_id=promo.id,
                signals=OfferDecisionSignals(segment="vip", customer_status="vip"),
            )
            decision = decide(db, ctx)
            extras = asyncio.run(apply_decision(db, ctx=ctx, decision=decision, customer=customer))
            db.commit()

            order = self._seed_paid_order(db, t.id, coupon_code=extras["coupon_code"])
            r1 = attribute_order_to_decision(db, tenant_id=t.id, order_id=order.id)
            r2 = attribute_order_to_decision(db, tenant_id=t.id, order_id=order.id)
            assert r1 is not None and r2 is not None
            assert r1.id == r2.id
        finally:
            db.close(); engine.dispose()

    def test_order_with_no_coupon_returns_none(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            o = Order(
                tenant_id=t.id, external_id="x", status="paid", total=100.0,
                customer_info={}, extra_metadata={},
            )
            db.add(o)
            db.commit()
            assert attribute_order_to_decision(db, tenant_id=t.id, order_id=o.id) is None
        finally:
            db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════
# 5. Signal collection
# ═════════════════════════════════════════════════════════════════════════

class TestCollectSignals:
    def test_packs_profile_and_price_sensitivity(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            customer = _seed_customer(db, t.id)
            _seed_profile(db, t.id, customer.id, segment="vip")
            db.add(PriceSensitivityScore(
                tenant_id=t.id, customer_id=customer.id,
                score=0.8, coupon_usage_rate=0.2, recommended_discount_pct=15,
            ))
            db.commit()

            sig = collect_signals(
                db, tenant_id=t.id, customer_id=customer.id, cart_total=199.0,
            )
            assert sig.segment == "vip"
            assert sig.price_sensitivity_score == pytest.approx(0.8)
            assert sig.recommended_discount_pct == 15
            assert sig.coupon_usage_rate == pytest.approx(0.2)
            assert sig.cart_total == 199.0
        finally:
            db.close(); engine.dispose()

    def test_missing_customer_returns_defaults(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            sig = collect_signals(db, tenant_id=t.id, customer_id=None)
            assert sig.segment is None
            assert sig.recommended_discount_pct == 0
        finally:
            db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════
# 6. Seed parity — every seed automation routes cleanly through the policy
# ═════════════════════════════════════════════════════════════════════════

# Mapping seed config → expected policy outcome. Locks down Phase 1 →
# Phase 2 invariant: when the engine starts calling decide(...) with the
# step's discount_source, the chosen source must match what the legacy
# `_resolve_discount_source` would have picked.
SEED_EXPECTED_SOURCE: dict[str, str] = {
    "abandoned_cart":        SOURCE_COUPON,       # legacy auto_coupon=true on stage 3
    "predictive_reorder":    SOURCE_NONE,         # no discount in seed
    "customer_winback":      SOURCE_COUPON,       # auto_coupon=true
    "vip_upgrade":           SOURCE_COUPON,       # auto_coupon=true
    "new_product_alert":     SOURCE_NONE,
    "back_in_stock":         SOURCE_NONE,         # informational only
    "unpaid_order_reminder": SOURCE_NONE,
    "cod_confirmation":      SOURCE_NONE,         # transactional reminder, no discount
    "seasonal_offer":        SOURCE_PROMOTION,    # discount_source=promotion
    "salary_payday_offer":   SOURCE_PROMOTION,
}


@pytest.mark.parametrize(
    "seed",
    SEED_AUTOMATIONS,
    ids=[s["automation_type"] for s in SEED_AUTOMATIONS],
)
def test_seed_parity_through_decision_service(seed: dict) -> None:
    """The decision the new policy returns for each seed config must
    match the legacy `_resolve_discount_source` outcome.

    This is the guardrail Phase 2 leans on: if it fails, swapping the
    automation engine's call site is unsafe."""
    db, engine = _make_db()
    try:
        t = _seed_tenant(db)
        cfg = seed["config"]

        # Build the same context the automation engine will build at call
        # time after Phase 2.
        suggested_source: str | None = None
        suggested_promo_id: int | None = None
        if cfg.get("discount_source"):
            suggested_source = str(cfg["discount_source"])
        elif cfg.get("auto_coupon") or any(
            (s.get("auto_coupon") or s.get("message_type") == "coupon")
            for s in cfg.get("steps", []) or []
        ):
            suggested_source = "coupon"

        # For promotion-backed seeds we also have to seed the matching
        # promotion so the policy can pick a value/validity from it.
        if suggested_source == "promotion":
            promo = _seed_promotion(db, t.id, customer_segments=["active"])
            suggested_promo_id = promo.id

        ctx = OfferDecisionContext(
            tenant_id=t.id,
            surface=SURFACE_AUTOMATION,
            automation_type=seed["automation_type"],
            suggested_source=suggested_source,
            suggested_promotion_id=suggested_promo_id,
            signals=OfferDecisionSignals(segment="active", customer_status="active"),
        )
        decision = decide(db, ctx)
        expected = SEED_EXPECTED_SOURCE[seed["automation_type"]]
        assert decision.source == expected, (
            f"seed={seed['automation_type']} expected {expected} got {decision.source} "
            f"reasons={decision.reason_codes}"
        )
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════
# 7. Phase-2 wiring — automation engine routes through the service when
#    the per-tenant `offer_decision_service` flag is on.
# ═════════════════════════════════════════════════════════════════════════

class TestAutomationEngineFlagWiring:
    """The automation engine's `_resolve_auto_coupon` must:
      (a) keep the legacy code path when the flag is off (default),
      (b) call the decision service when the flag is on, writing a
          ledger row and stamping `decision_id` on the issued coupon."""

    def _enable_flag(self, db, tenant_id: int) -> None:
        ts = db.query(TenantSettings).filter_by(tenant_id=tenant_id).first()
        if ts is None:
            ts = TenantSettings(tenant_id=tenant_id)
            db.add(ts)
        meta = dict(ts.extra_metadata or {})
        meta["tenant_features"] = {"offer_decision_service": True}
        ts.extra_metadata = meta
        db.commit()

    def test_flag_off_uses_legacy_path(self) -> None:
        from core.automation_engine import _resolve_auto_coupon
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            customer = _seed_customer(db, t.id)
            _seed_profile(db, t.id, customer.id, segment="vip")

            extras = asyncio.run(_resolve_auto_coupon(
                db,
                tenant_id=t.id,
                customer=customer,
                config={"auto_coupon": True, "automation_type": "vip_upgrade"},
                active_step={},
            ))
            db.commit()

            # Legacy path: no ledger row written.
            assert db.query(OfferDecisionLedger).count() == 0
            # If a code came back it must NOT carry decision_id (legacy
            # path doesn't stamp one).
            if extras.get("coupon_code"):
                row = (
                    db.query(Coupon)
                    .filter(Coupon.tenant_id == t.id, Coupon.code == extras["coupon_code"])
                    .first()
                )
                assert (row.extra_metadata or {}).get("decision_id") is None
        finally:
            db.close(); engine.dispose()

    def test_flag_on_writes_ledger_and_stamps_decision_id(self) -> None:
        from core.automation_engine import _resolve_auto_coupon
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            customer = _seed_customer(db, t.id)
            _seed_profile(db, t.id, customer.id, segment="vip")
            self._enable_flag(db, t.id)

            extras = asyncio.run(_resolve_auto_coupon(
                db,
                tenant_id=t.id,
                customer=customer,
                config={"auto_coupon": True, "automation_type": "vip_upgrade"},
                active_step={},
                automation_id=42,
                event_id=99,
            ))
            db.commit()

            # New path: exactly one ledger row, scoped to this tenant.
            rows = db.query(OfferDecisionLedger).filter_by(tenant_id=t.id).all()
            assert len(rows) == 1
            row = rows[0]
            assert row.surface == SURFACE_AUTOMATION
            assert row.automation_id == 42
            assert row.event_id == 99
            assert row.customer_id == customer.id
            assert row.policy_version == POLICY_VERSION

            # If a coupon was issued it should carry the same decision_id.
            if extras.get("coupon_code"):
                coupon = (
                    db.query(Coupon)
                    .filter(Coupon.tenant_id == t.id, Coupon.code == extras["coupon_code"])
                    .first()
                )
                assert (coupon.extra_metadata or {}).get("decision_id") == row.decision_id
                assert row.chosen_coupon_id == coupon.id
        finally:
            db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════
# 8. Phase-3 chat path — advisory mode + enforce mode
# ═════════════════════════════════════════════════════════════════════════

class TestChatAdvisoryAndEnforce:
    """The chat path must:
      • in advisory mode (default), still let the LLM-suggested discount
        through but ALSO write a ledger row capturing what the policy
        would have done — so we can measure parity before flipping;
      • in enforce mode (per-tenant flag on), let the policy override
        the LLM's value and stamp the resulting coupon with the
        decision_id."""

    def _enable_flag(self, db, tenant_id: int) -> None:
        ts = db.query(TenantSettings).filter_by(tenant_id=tenant_id).first()
        if ts is None:
            ts = TenantSettings(tenant_id=tenant_id)
            db.add(ts)
        meta = dict(ts.extra_metadata or {})
        meta["tenant_features"] = {"offer_decision_service": True}
        ts.extra_metadata = meta
        db.commit()

    def test_advisory_mode_logs_decision_without_overriding_llm(
        self, monkeypatch
    ) -> None:
        """The legacy code path is preserved; one ledger row is still
        appended carrying the policy's view."""
        from modules.ai.orchestrator import adapter as chat_adapter

        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            customer = _seed_customer(db, t.id)
            _seed_profile(db, t.id, customer.id, segment="active")

            # Make the adapter use OUR in-memory session — the import is
            # lazy (`from core.database import SessionLocal`) so we patch
            # the source module the adapter pulls from. We also no-op
            # `db.close()` inside the adapter so our test objects stay
            # attached when we re-query after the call returns.
            import core.database as core_db  # noqa: PLC0415
            monkeypatch.setattr(core_db, "SessionLocal", lambda: _NoCloseSession(db), raising=False)
            # Stub the legacy CouponGeneratorService — we don't need a real
            # Salla adapter; we just need a Coupon row to come back.
            from services import coupon_generator as cg  # noqa: PLC0415

            issued_coupon = Coupon(
                tenant_id=t.id,
                code="NHCH1",
                description="advisory test",
                discount_type="percentage",
                discount_value="20",
                extra_metadata={},
            )
            db.add(issued_coupon)
            db.commit()
            db.refresh(issued_coupon)
            tenant_id = t.id
            customer_pk = customer.id

            class _StubSvc:
                def __init__(self, *_a, **_kw): pass
                def pick_coupon_for_segment(self, _segment): return issued_coupon
            monkeypatch.setattr(cg, "CouponGeneratorService", _StubSvc)
            # build_coupon_send_payload reads coupon attrs only — keep real impl.

            payload = asyncio.run(chat_adapter._execute_suggest_coupon(
                tenant_id, "active", {"discount_pct": 20},
                customer_id=customer_pk,
            ))
            db.commit()

            assert payload is not None
            assert payload.get("code") == "NHCH1"

            # Advisory ledger row should exist for this chat call.
            rows = (
                db.query(OfferDecisionLedger)
                .filter_by(tenant_id=tenant_id, surface=SURFACE_CHAT)
                .all()
            )
            assert len(rows) == 1
            row = rows[0]
            assert row.policy_version == POLICY_VERSION
            # Coupon should be back-stamped with the advisory decision_id.
            db.refresh(issued_coupon)
            assert (issued_coupon.extra_metadata or {}).get("decision_id") == row.decision_id
            assert (issued_coupon.extra_metadata or {}).get("decision_mode") == "advisory"
        finally:
            db.close(); engine.dispose()

    def test_enforce_mode_lets_policy_clamp_and_stamps_decision_id(
        self, monkeypatch
    ) -> None:
        """When enforce mode is on, the chat path goes through
        OfferDecisionService.apply_decision and the LLM's suggested 40%
        is clamped down to the merchant cap."""
        from modules.ai.orchestrator import adapter as chat_adapter

        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            customer = _seed_customer(db, t.id)
            _seed_profile(db, t.id, customer.id, segment="active")
            _seed_settings(db, t.id, max_discount=15)
            self._enable_flag(db, t.id)
            tenant_id = t.id
            customer_pk = customer.id

            import core.database as core_db  # noqa: PLC0415
            monkeypatch.setattr(core_db, "SessionLocal", lambda: _NoCloseSession(db), raising=False)

            # Stub the coupon generator so apply_decision's coupon path
            # produces a real row without needing Salla.
            from services import coupon_generator as cg  # noqa: PLC0415

            class _StubSvc:
                def __init__(self, _db, _tenant_id):
                    self.db = _db
                    self.tenant_id = _tenant_id
                def pick_coupon_for_segment(self, _segment):
                    return None
                async def create_on_demand(self, _segment, *, requested_discount_pct=None,
                                          validity_days_override=None):
                    coupon = Coupon(
                        tenant_id=self.tenant_id,
                        code="NHCH2",
                        description="enforce test",
                        discount_type="percentage",
                        discount_value=str(requested_discount_pct or 10),
                        extra_metadata={},
                    )
                    self.db.add(coupon)
                    self.db.commit()
                    self.db.refresh(coupon)
                    return coupon
            monkeypatch.setattr(cg, "CouponGeneratorService", _StubSvc)

            payload = asyncio.run(chat_adapter._execute_suggest_coupon(
                tenant_id, "active", {"discount_pct": 40},
                customer_id=customer_pk,
            ))
            db.commit()

            assert payload is not None
            assert payload.get("code") == "NHCH2"

            rows = (
                db.query(OfferDecisionLedger)
                .filter_by(tenant_id=tenant_id, surface=SURFACE_CHAT)
                .all()
            )
            assert len(rows) == 1
            row = rows[0]
            assert Decimal(str(row.discount_value)) == Decimal("15")
            assert "capped_by_max_discount" in (row.reason_codes or [])

            coupon = db.query(Coupon).filter_by(tenant_id=tenant_id, code="NHCH2").one()
            assert (coupon.extra_metadata or {}).get("decision_id") == row.decision_id
        finally:
            db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════
# 9. Phase-4 — customer_intelligence segment-change routes through service
# ═════════════════════════════════════════════════════════════════════════

class TestSegmentChangeRouting:
    def test_flag_on_writes_segment_change_ledger_row(self, monkeypatch) -> None:
        """Calling the new helper directly produces a SURFACE_SEGMENT_CHANGE
        ledger row. (We test the helper instead of the full
        recompute_profile flow because that pulls in the whole metrics
        pipeline; the routing decision is what we care about here.)"""
        from services.customer_intelligence import CustomerIntelligenceService

        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            customer = _seed_customer(db, t.id)
            _seed_profile(db, t.id, customer.id, segment="vip")
            _seed_settings(db, t.id, max_discount=25)

            from services import coupon_generator as cg  # noqa: PLC0415

            class _StubSvc:
                def __init__(self, _db, _tenant_id):
                    self.db = _db
                    self.tenant_id = _tenant_id
                def pick_coupon_for_segment(self, _segment): return None
                async def create_on_demand(self, _segment, *, requested_discount_pct=None,
                                          validity_days_override=None):
                    coupon = Coupon(
                        tenant_id=self.tenant_id,
                        code="NHSC1",
                        description="seg-change",
                        discount_type="percentage",
                        discount_value=str(requested_discount_pct or 20),
                        extra_metadata={},
                    )
                    self.db.add(coupon)
                    self.db.commit()
                    self.db.refresh(coupon)
                    return coupon
            monkeypatch.setattr(cg, "CouponGeneratorService", _StubSvc)

            svc = CustomerIntelligenceService(db, t.id)
            asyncio.run(svc._segment_change_via_decision_service(
                customer_id=customer.id, segment="vip", reason="test",
            ))
            db.commit()

            rows = (
                db.query(OfferDecisionLedger)
                .filter_by(tenant_id=t.id, surface=SURFACE_SEGMENT_CHANGE)
                .all()
            )
            assert len(rows) == 1
            row = rows[0]
            assert row.customer_id == customer.id
            assert row.policy_version == POLICY_VERSION
            # Coupon row should exist & carry the decision_id.
            issued = db.query(Coupon).filter_by(tenant_id=t.id, code="NHSC1").one()
            assert (issued.extra_metadata or {}).get("decision_id") == row.decision_id
        finally:
            db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════
# 10. Defensive — unknown surface degrades to none
# ═════════════════════════════════════════════════════════════════════════

def test_unknown_surface_degrades_to_no_offer() -> None:
    db, engine = _make_db()
    try:
        t = _seed_tenant(db)
        ctx = OfferDecisionContext(tenant_id=t.id, surface="not-a-surface")
        decision = decide(db, ctx)
        assert decision.source == SOURCE_NONE
        assert "unknown_surface" in decision.reason_codes
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════
# 11. Rollout-mode truth table (services.offer_decision_flags)
# ═════════════════════════════════════════════════════════════════════════
#
# These tests pin down the per-tenant rollout mode resolver. Every other
# surface (chat, automation, segment-change) reads through this resolver,
# so a regression here would silently re-route traffic.

class TestDecisionModeTruthTable:
    """The truth table is documented at the top of
    `services/offer_decision_flags.py`. ADVISORY always wins; otherwise
    the legacy `offer_decision_service` flag controls ENFORCE; otherwise
    OFF."""

    def _set_flags(self, db, tenant_id, *, service=None, advisory=None) -> None:
        ts = db.query(TenantSettings).filter_by(tenant_id=tenant_id).first()
        if ts is None:
            ts = TenantSettings(tenant_id=tenant_id)
            db.add(ts)
        meta = dict(ts.extra_metadata or {})
        flags = dict(meta.get("tenant_features") or {})
        if service is not None:
            flags["offer_decision_service"] = service
        if advisory is not None:
            flags["offer_decision_service_advisory"] = advisory
        meta["tenant_features"] = flags
        ts.extra_metadata = meta
        db.commit()

    def test_no_flags_is_off(self) -> None:
        from services.offer_decision_flags import DecisionMode, tenant_decision_mode
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            assert tenant_decision_mode(db, t.id) is DecisionMode.OFF
        finally:
            db.close(); engine.dispose()

    def test_service_only_is_enforce(self) -> None:
        """Backward-compat: a tenant currently shipping with just
        `offer_decision_service:true` must continue to enforce."""
        from services.offer_decision_flags import DecisionMode, tenant_decision_mode
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            self._set_flags(db, t.id, service=True)
            assert tenant_decision_mode(db, t.id) is DecisionMode.ENFORCE
        finally:
            db.close(); engine.dispose()

    def test_advisory_only_is_advisory(self) -> None:
        """The new shadow-mode toggle works on its own — staff can opt
        a tenant into advisory without ever flipping the main flag."""
        from services.offer_decision_flags import DecisionMode, tenant_decision_mode
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            self._set_flags(db, t.id, advisory=True)
            assert tenant_decision_mode(db, t.id) is DecisionMode.ADVISORY
        finally:
            db.close(); engine.dispose()

    def test_advisory_wins_over_enforce(self) -> None:
        """Safety brake: setting advisory must downgrade an
        enforce-mode tenant back to advisory without un-setting the
        main flag."""
        from services.offer_decision_flags import DecisionMode, tenant_decision_mode
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            self._set_flags(db, t.id, service=True, advisory=True)
            assert tenant_decision_mode(db, t.id) is DecisionMode.ADVISORY
        finally:
            db.close(); engine.dispose()

    def test_missing_settings_row_is_off(self) -> None:
        """No TenantSettings row at all → OFF (legacy behaviour)."""
        from services.offer_decision_flags import DecisionMode, tenant_decision_mode
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            assert tenant_decision_mode(db, t.id) is DecisionMode.OFF
        finally:
            db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════
# 12. Advisory mode — automation engine
# ═════════════════════════════════════════════════════════════════════════
#
# In ADVISORY the automation engine must:
#   (a) write a SURFACE_AUTOMATION ledger row capturing the policy's
#       view of the world (so we can audit shadow runs),
#   (b) still return the **legacy** code path's discount — the policy
#       must not change behaviour during shadow rollout,
#   (c) back-stamp the legacy-issued coupon with the same decision_id
#       so attribution still closes the loop on redemption.

class TestAutomationEngineAdvisoryMode:
    def _enable_advisory(self, db, tenant_id: int) -> None:
        ts = db.query(TenantSettings).filter_by(tenant_id=tenant_id).first()
        if ts is None:
            ts = TenantSettings(tenant_id=tenant_id)
            db.add(ts)
        meta = dict(ts.extra_metadata or {})
        meta["tenant_features"] = {"offer_decision_service_advisory": True}
        ts.extra_metadata = meta
        db.commit()

    def test_advisory_writes_ledger_and_returns_legacy_coupon(self, monkeypatch) -> None:
        from core.automation_engine import _resolve_auto_coupon

        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            customer = _seed_customer(db, t.id)
            _seed_profile(db, t.id, customer.id, segment="vip")
            self._enable_advisory(db, t.id)

            # Stub the legacy coupon generator so the legacy path returns
            # a deterministic coupon code we can assert on.
            from services import coupon_generator as cg  # noqa: PLC0415

            issued = Coupon(
                tenant_id=t.id,
                code="LEGACYADV1",
                description="advisory automation",
                discount_type="percentage",
                discount_value="20",
                extra_metadata={},
            )
            db.add(issued); db.commit(); db.refresh(issued)

            class _StubSvc:
                def __init__(self, _db, _tenant_id):
                    self.db = _db; self.tenant_id = _tenant_id
                def pick_coupon_for_segment(self, _segment): return issued
                async def create_on_demand(self, *_a, **_kw): return issued
            monkeypatch.setattr(cg, "CouponGeneratorService", _StubSvc)

            extras = asyncio.run(_resolve_auto_coupon(
                db,
                tenant_id=t.id,
                customer=customer,
                config={"auto_coupon": True, "automation_type": "vip_upgrade"},
                active_step={},
                automation_id=77,
                event_id=88,
            ))
            db.commit()

            # (a) one ledger row, scoped to this tenant + automation surface.
            rows = (
                db.query(OfferDecisionLedger)
                .filter_by(tenant_id=t.id, surface=SURFACE_AUTOMATION)
                .all()
            )
            assert len(rows) == 1
            row = rows[0]
            assert row.automation_id == 77
            assert row.event_id == 88
            assert row.customer_id == customer.id
            assert row.policy_version == POLICY_VERSION

            # (b) legacy code's coupon is what came back — not whatever
            # the policy would have issued.
            assert extras.get("coupon_code") == "LEGACYADV1"

            # (c) the legacy coupon got back-stamped with the advisory
            # decision_id and labelled as advisory_automation.
            db.refresh(issued)
            meta = issued.extra_metadata or {}
            assert meta.get("decision_id") == row.decision_id
            assert meta.get("decision_mode") == "advisory_automation"
        finally:
            db.close(); engine.dispose()

    def test_advisory_with_main_flag_still_runs_advisory_branch(self, monkeypatch) -> None:
        """Truth-table sanity: when BOTH flags are on, advisory wins —
        the legacy resolver runs and the engine does NOT call
        OfferDecisionService.apply_decision (which would issue a
        different coupon). Captured by checking the issued code."""
        from core.automation_engine import _resolve_auto_coupon

        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            customer = _seed_customer(db, t.id)
            _seed_profile(db, t.id, customer.id, segment="vip")
            ts = db.query(TenantSettings).filter_by(tenant_id=t.id).first()
            if ts is None:
                ts = TenantSettings(tenant_id=t.id); db.add(ts)
            meta = dict(ts.extra_metadata or {})
            meta["tenant_features"] = {
                "offer_decision_service": True,
                "offer_decision_service_advisory": True,
            }
            ts.extra_metadata = meta
            db.commit()

            from services import coupon_generator as cg  # noqa: PLC0415
            issued = Coupon(
                tenant_id=t.id, code="LEGACYBOTH",
                description="both flags", discount_type="percentage",
                discount_value="20", extra_metadata={},
            )
            db.add(issued); db.commit(); db.refresh(issued)

            class _StubSvc:
                def __init__(self, _db, _tenant_id):
                    self.db = _db; self.tenant_id = _tenant_id
                def pick_coupon_for_segment(self, _segment): return issued
                async def create_on_demand(self, *_a, **_kw): return issued
            monkeypatch.setattr(cg, "CouponGeneratorService", _StubSvc)

            extras = asyncio.run(_resolve_auto_coupon(
                db,
                tenant_id=t.id,
                customer=customer,
                config={"auto_coupon": True, "automation_type": "vip_upgrade"},
                active_step={},
                automation_id=1,
                event_id=2,
            ))
            db.commit()

            assert extras.get("coupon_code") == "LEGACYBOTH"
            db.refresh(issued)
            assert (issued.extra_metadata or {}).get("decision_mode") == "advisory_automation"
        finally:
            db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════
# 13. Advisory mode — segment-change (customer_intelligence)
# ═════════════════════════════════════════════════════════════════════════

class TestSegmentChangeAdvisoryMode:
    def test_advisory_segment_change_writes_ledger_and_runs_legacy(self, monkeypatch) -> None:
        from services.customer_intelligence import CustomerIntelligenceService

        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            customer = _seed_customer(db, t.id)
            _seed_profile(db, t.id, customer.id, segment="vip")
            _seed_settings(db, t.id, max_discount=25)

            # Build a stub CouponGeneratorService.generate_for_customer
            # that returns a deterministic coupon row.
            from services import coupon_generator as cg  # noqa: PLC0415

            issued = Coupon(
                tenant_id=t.id,
                code="SEGADV1",
                description="advisory segment change",
                discount_type="percentage",
                discount_value="20",
                extra_metadata={},
            )
            db.add(issued); db.commit(); db.refresh(issued)

            class _StubSvc:
                def __init__(self, _db, _tenant_id):
                    self.db = _db; self.tenant_id = _tenant_id
                async def generate_for_customer(self, _cid, _segment, *, reason=""):
                    return issued
            monkeypatch.setattr(cg, "CouponGeneratorService", _StubSvc)

            svc = CustomerIntelligenceService(db, t.id)
            asyncio.run(svc._segment_change_advisory(
                customer_id=customer.id, segment="vip", reason="test",
            ))
            db.commit()

            # Ledger row written for the segment-change surface.
            rows = (
                db.query(OfferDecisionLedger)
                .filter_by(tenant_id=t.id, surface=SURFACE_SEGMENT_CHANGE)
                .all()
            )
            assert len(rows) == 1
            row = rows[0]
            assert row.customer_id == customer.id
            assert row.policy_version == POLICY_VERSION

            # Legacy coupon back-stamped with the advisory decision_id.
            db.refresh(issued)
            meta = issued.extra_metadata or {}
            assert meta.get("decision_id") == row.decision_id
            assert meta.get("decision_mode") == "advisory_segment_change"
        finally:
            db.close(); engine.dispose()
