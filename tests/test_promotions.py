"""
tests/test_promotions.py
─────────────────────────
Coverage for the Promotions ↔ Coupons split + automation glue.

Promotions are *automatic* discount rules (no code required) that the
merchant manages from the new "العروض" page. When an automation fires,
`services.promotion_engine.materialise_for_customer` issues a personal
`Coupon` row carrying the promotion's terms — so the same flow works
across every store backend.

What this module locks down:

  1. Engine purity:
       • is_promotion_active honours status + dates + usage_limit.
       • compute_effective_status reflects the calendar even when the
         merchant left status='active' on an expired row.
       • evaluate_conditions checks segments + min_order_amount and
         passes when conditions are absent.

  2. Materialisation:
       • Issues an NHxxx personal coupon bound to the promotion +
         customer with `source='promotion'` metadata.
       • Idempotent: a second call for the same (promo, customer)
         returns the existing live coupon instead of a duplicate.
       • Inactive promotion → returns None silently.
       • Failing condition (segment mismatch) → returns None silently.
       • Expired promotion → returns None and is swept by sweep_expired.

  3. Automation glue:
       • _resolve_discount_source picks 'promotion' when configured,
         falls back to 'coupon' on the legacy `auto_coupon` shape so
         existing seeds keep working.
       • _materialise_promotion_for_send produces the same return
         shape as the coupon path so templates render identically.

  4. Seed migration:
       • The seasonal_offer + salary_payday_offer seeds carry
         discount_source='promotion' + a default_promotion_slug.
       • ensure_default_promotions_for_tenant creates the default
         Promotion rows and wires `config.promotion_id` on first call;
         is idempotent on the second call.

  5. Router contract:
       • ENGINE_DEFINITIONS / SEED_AUTOMATIONS round-trip the new
         fields without dropping discount_source.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
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
    Promotion,
    SmartAutomation,
    Tenant,
)
from services.promotion_engine import (  # noqa: E402
    ACTIVE_STATUS,
    DRAFT_STATUS,
    EXPIRED_STATUS,
    PAUSED_STATUS,
    SCHEDULED_STATUS,
    compute_effective_status,
    evaluate_conditions,
    is_promotion_active,
    materialise_for_customer,
    sweep_expired,
)
from core.automation_engine import (  # noqa: E402
    _materialise_promotion_for_send,
    _pick_promotion_id_for_event,
    _resolve_auto_coupon,
    _resolve_discount_source,
)
from core.automations_seed import (  # noqa: E402
    DEFAULT_PROMOTIONS,
    SEASONAL_OCCASIONS,
    SEED_AUTOMATIONS,
    ensure_default_promotions_for_tenant,
    seed_automations_if_empty,
)


# ── DB harness ────────────────────────────────────────────────────────────────

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


def _seed_customer(db, tenant_id: int, phone: str = "+966555000111") -> Customer:
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
) -> CustomerProfile:
    p = CustomerProfile(
        tenant_id=tenant_id,
        customer_id=customer_id,
        segment=segment,
        customer_status=segment,
        total_orders=2,
        total_spend_sar=300.0,
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
    name: str = "Test Promotion",
    promotion_type: str = "percentage",
    discount_value: float | None = 15.0,
    status: str = ACTIVE_STATUS,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    conditions: dict | None = None,
    usage_limit: int | None = None,
) -> Promotion:
    promo = Promotion(
        tenant_id=tenant_id,
        name=name,
        promotion_type=promotion_type,
        discount_value=Decimal(str(discount_value)) if discount_value is not None else None,
        status=status,
        starts_at=starts_at.replace(tzinfo=None) if starts_at and starts_at.tzinfo else starts_at,
        ends_at=ends_at.replace(tzinfo=None) if ends_at and ends_at.tzinfo else ends_at,
        conditions=conditions or {},
        usage_count=0,
        usage_limit=usage_limit,
        extra_metadata={},
    )
    db.add(promo)
    db.commit()
    db.refresh(promo)
    return promo


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ═════════════════════════════════════════════════════════════════════════════
# 1. Pure helpers — is_promotion_active / compute_effective_status
# ═════════════════════════════════════════════════════════════════════════════

class TestIsPromotionActive:
    def test_status_active_no_dates_is_live(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            promo = _seed_promotion(db, t.id, status=ACTIVE_STATUS)
            assert is_promotion_active(promo) is True
        finally:
            db.close(); engine.dispose()

    def test_paused_is_not_live(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            promo = _seed_promotion(db, t.id, status=PAUSED_STATUS)
            assert is_promotion_active(promo) is False
        finally:
            db.close(); engine.dispose()

    def test_future_starts_at_is_not_live(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            future = datetime.now(timezone.utc) + timedelta(hours=2)
            promo = _seed_promotion(db, t.id, starts_at=future)
            assert is_promotion_active(promo) is False
        finally:
            db.close(); engine.dispose()

    def test_past_ends_at_is_not_live(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            past = datetime.now(timezone.utc) - timedelta(hours=2)
            promo = _seed_promotion(db, t.id, ends_at=past)
            assert is_promotion_active(promo) is False
        finally:
            db.close(); engine.dispose()

    def test_usage_limit_reached_is_not_live(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            promo = _seed_promotion(db, t.id, usage_limit=3)
            promo.usage_count = 3
            db.commit()
            assert is_promotion_active(promo) is False
        finally:
            db.close(); engine.dispose()


class TestComputeEffectiveStatus:
    def test_paused_round_trips(self) -> None:
        promo = Promotion(name="x", promotion_type="percentage", status=PAUSED_STATUS)
        assert compute_effective_status(promo) == PAUSED_STATUS

    def test_draft_round_trips(self) -> None:
        promo = Promotion(name="x", promotion_type="percentage", status=DRAFT_STATUS)
        assert compute_effective_status(promo) == DRAFT_STATUS

    def test_active_after_ends_at_is_expired(self) -> None:
        past = datetime.now(timezone.utc) - timedelta(days=1)
        promo = Promotion(
            name="x", promotion_type="percentage",
            status=ACTIVE_STATUS, ends_at=past.replace(tzinfo=None),
        )
        assert compute_effective_status(promo) == EXPIRED_STATUS

    def test_active_before_starts_at_is_scheduled(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=2)
        promo = Promotion(
            name="x", promotion_type="percentage",
            status=ACTIVE_STATUS, starts_at=future.replace(tzinfo=None),
        )
        assert compute_effective_status(promo) == SCHEDULED_STATUS

    def test_active_within_window_is_active(self) -> None:
        now = datetime.now(timezone.utc)
        promo = Promotion(
            name="x", promotion_type="percentage",
            status=ACTIVE_STATUS,
            starts_at=(now - timedelta(hours=1)).replace(tzinfo=None),
            ends_at=(now + timedelta(hours=1)).replace(tzinfo=None),
        )
        assert compute_effective_status(promo) == ACTIVE_STATUS


# ═════════════════════════════════════════════════════════════════════════════
# 2. evaluate_conditions
# ═════════════════════════════════════════════════════════════════════════════

class TestEvaluateConditions:
    def test_no_conditions_passes(self) -> None:
        promo = Promotion(name="x", promotion_type="percentage", conditions={})
        ok, reason = evaluate_conditions(promo)
        assert ok is True and reason is None

    def test_segment_match_passes(self) -> None:
        promo = Promotion(
            name="x", promotion_type="percentage",
            conditions={"customer_segments": ["vip", "loyal"]},
        )
        profile = CustomerProfile(tenant_id=1, customer_id=1, segment="vip")
        ok, _ = evaluate_conditions(promo, customer_profile=profile)
        assert ok is True

    def test_segment_mismatch_fails(self) -> None:
        promo = Promotion(
            name="x", promotion_type="percentage",
            conditions={"customer_segments": ["vip"]},
        )
        profile = CustomerProfile(tenant_id=1, customer_id=1, segment="active")
        ok, reason = evaluate_conditions(promo, customer_profile=profile)
        assert ok is False
        assert "segment_mismatch" in (reason or "")

    def test_min_order_amount_below_fails(self) -> None:
        promo = Promotion(
            name="x", promotion_type="percentage",
            conditions={"min_order_amount": 200},
        )
        ok, reason = evaluate_conditions(promo, cart_total=Decimal("150"))
        assert ok is False
        assert "below_min_order_amount" in (reason or "")

    def test_min_order_amount_at_or_above_passes(self) -> None:
        promo = Promotion(
            name="x", promotion_type="percentage",
            conditions={"min_order_amount": 200},
        )
        ok, _ = evaluate_conditions(promo, cart_total=Decimal("250"))
        assert ok is True


# ═════════════════════════════════════════════════════════════════════════════
# 3. materialise_for_customer
# ═════════════════════════════════════════════════════════════════════════════

class TestMaterialiseForCustomer:
    def test_issues_personal_coupon_bound_to_promotion(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            customer = _seed_customer(db, t.id)
            promo = _seed_promotion(db, t.id, discount_value=20.0)

            coupon = asyncio.run(materialise_for_customer(
                db, promotion_id=promo.id, tenant_id=t.id, customer_id=customer.id,
            ))

            assert coupon is not None
            assert coupon.code.startswith("NH")
            assert coupon.discount_type == "percentage"
            assert Decimal(str(coupon.discount_value)) == Decimal("20")
            meta = coupon.extra_metadata or {}
            assert meta["source"] == "promotion"
            assert meta["promotion_id"] == promo.id
            assert meta["customer_id"] == customer.id

            db.refresh(promo)
            assert promo.usage_count == 1
        finally:
            db.close(); engine.dispose()

    def test_idempotent_returns_existing_live_code(self) -> None:
        """A second call for the same (promo, customer) reuses the live code."""
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            customer = _seed_customer(db, t.id)
            promo = _seed_promotion(db, t.id)

            first = asyncio.run(materialise_for_customer(
                db, promotion_id=promo.id, tenant_id=t.id, customer_id=customer.id,
            ))
            second = asyncio.run(materialise_for_customer(
                db, promotion_id=promo.id, tenant_id=t.id, customer_id=customer.id,
            ))

            assert first is not None and second is not None
            assert first.id == second.id
            assert first.code == second.code

            # Only one coupon row created across the two calls.
            promo_coupons = (
                db.query(Coupon)
                .filter(Coupon.tenant_id == t.id)
                .all()
            )
            sourced = [c for c in promo_coupons
                       if (c.extra_metadata or {}).get("source") == "promotion"
                       and (c.extra_metadata or {}).get("promotion_id") == promo.id]
            assert len(sourced) == 1
        finally:
            db.close(); engine.dispose()

    def test_inactive_promotion_returns_none(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            customer = _seed_customer(db, t.id)
            promo = _seed_promotion(db, t.id, status=PAUSED_STATUS)

            coupon = asyncio.run(materialise_for_customer(
                db, promotion_id=promo.id, tenant_id=t.id, customer_id=customer.id,
            ))
            assert coupon is None
        finally:
            db.close(); engine.dispose()

    def test_segment_condition_failure_returns_none(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            customer = _seed_customer(db, t.id)
            _seed_profile(db, t.id, customer.id, segment="active")
            promo = _seed_promotion(db, t.id, conditions={"customer_segments": ["vip"]})

            coupon = asyncio.run(materialise_for_customer(
                db, promotion_id=promo.id, tenant_id=t.id, customer_id=customer.id,
            ))
            assert coupon is None
        finally:
            db.close(); engine.dispose()

    def test_missing_promotion_returns_none(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            customer = _seed_customer(db, t.id)
            coupon = asyncio.run(materialise_for_customer(
                db, promotion_id=99999, tenant_id=t.id, customer_id=customer.id,
            ))
            assert coupon is None
        finally:
            db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 4. sweep_expired
# ═════════════════════════════════════════════════════════════════════════════

def test_sweep_expired_flips_active_promos_past_their_end() -> None:
    db, engine = _make_db()
    try:
        t = _seed_tenant(db)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        expired_one = _seed_promotion(db, t.id, name="A", ends_at=past)
        live_one = _seed_promotion(db, t.id, name="B", ends_at=future)

        flipped = sweep_expired(db, t.id)
        assert flipped == 1

        db.refresh(expired_one); db.refresh(live_one)
        assert expired_one.status == EXPIRED_STATUS
        assert live_one.status == ACTIVE_STATUS
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 5. _resolve_discount_source — automation glue
# ═════════════════════════════════════════════════════════════════════════════

class TestResolveDiscountSource:
    def test_explicit_promotion_in_config_wins(self) -> None:
        src = _resolve_discount_source(
            config={"discount_source": "promotion", "promotion_id": 42},
            active_step={},
        )
        assert src == "promotion"

    def test_explicit_step_overrides_config(self) -> None:
        src = _resolve_discount_source(
            config={"discount_source": "coupon"},
            active_step={"discount_source": "promotion"},
        )
        assert src == "promotion"

    def test_legacy_auto_coupon_in_config_resolves_to_coupon(self) -> None:
        """Backward compat: existing seeds use auto_coupon=True."""
        src = _resolve_discount_source(
            config={"auto_coupon": True}, active_step={},
        )
        assert src == "coupon"

    def test_legacy_step_message_type_coupon_resolves_to_coupon(self) -> None:
        src = _resolve_discount_source(
            config={}, active_step={"message_type": "coupon"},
        )
        assert src == "coupon"

    def test_no_signal_resolves_to_none(self) -> None:
        src = _resolve_discount_source(config={}, active_step={})
        assert src == "none"


# ═════════════════════════════════════════════════════════════════════════════
# 6. _materialise_promotion_for_send (engine path)
# ═════════════════════════════════════════════════════════════════════════════

class TestMaterialisePromotionForSend:
    def test_returns_template_friendly_dict(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            customer = _seed_customer(db, t.id)
            promo = _seed_promotion(db, t.id, discount_value=10.0)

            extras = asyncio.run(_materialise_promotion_for_send(
                db, tenant_id=t.id, customer=customer, promotion_id=promo.id,
            ))

            assert "discount_code" in extras
            assert extras["discount_code"].startswith("NH")
            assert extras["discount_code"] == extras["coupon_code"]
            assert extras["discount_code"] == extras["vip_coupon"]
        finally:
            db.close(); engine.dispose()

    def test_missing_promotion_id_returns_empty(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            customer = _seed_customer(db, t.id)
            extras = asyncio.run(_materialise_promotion_for_send(
                db, tenant_id=t.id, customer=customer, promotion_id=None,
            ))
            assert extras == {}
        finally:
            db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 7. _resolve_auto_coupon dispatcher honours promotion source
# ═════════════════════════════════════════════════════════════════════════════

def test_resolve_auto_coupon_dispatches_to_promotion_path() -> None:
    db, engine = _make_db()
    try:
        t = _seed_tenant(db)
        customer = _seed_customer(db, t.id)
        promo = _seed_promotion(db, t.id, discount_value=12.0)

        extras = asyncio.run(_resolve_auto_coupon(
            db,
            tenant_id=t.id,
            customer=customer,
            config={"discount_source": "promotion", "promotion_id": promo.id},
            active_step={},
        ))

        assert "discount_code" in extras
        assert extras["discount_code"].startswith("NH")
    finally:
        db.close(); engine.dispose()


def test_resolve_auto_coupon_legacy_path_still_works() -> None:
    """Backward compat: a config with `auto_coupon=True` and no
    discount_source must still go through the coupon pool path.
    Either it succeeds (returning a code) or fails gracefully ({}),
    but it must NOT raise — otherwise existing seeds break."""
    db, engine = _make_db()
    try:
        t = _seed_tenant(db)
        customer = _seed_customer(db, t.id)

        extras = asyncio.run(_resolve_auto_coupon(
            db,
            tenant_id=t.id,
            customer=customer,
            config={"auto_coupon": True},
            active_step={},
        ))

        # The legacy path returns a dict — either populated (code issued)
        # or empty (pool empty). Crucially: never raises and never
        # silently dispatches to the promotion path.
        assert isinstance(extras, dict)
        if extras:
            assert "discount_code" in extras
            # The legacy path must not stamp source=promotion on its codes.
            issued = extras["discount_code"]
            row = (
                db.query(Coupon)
                .filter(Coupon.tenant_id == t.id, Coupon.code == issued)
                .first()
            )
            assert row is not None
            assert (row.extra_metadata or {}).get("source") != "promotion"
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 8. Seed migration — seasonal_offer / salary_payday now use promotions
# ═════════════════════════════════════════════════════════════════════════════

PROMOTION_BACKED_AUTOMATIONS = (
    "seasonal_offer",
    "salary_payday_offer",
)


@pytest.mark.parametrize("automation_type", PROMOTION_BACKED_AUTOMATIONS)
def test_seed_uses_discount_source_promotion(automation_type: str) -> None:
    seed = next(s for s in SEED_AUTOMATIONS if s["automation_type"] == automation_type)
    cfg = seed["config"]
    assert cfg["discount_source"] == "promotion"
    assert cfg.get("default_promotion_slug") in DEFAULT_PROMOTIONS
    # The legacy `auto_coupon` flag must be removed so the dispatcher
    # picks the promotion path.
    assert "auto_coupon" not in cfg


COUPON_BACKED_AUTOMATIONS = (
    "abandoned_cart",
    "vip_upgrade",
    "customer_winback",
    "back_in_stock",
)


@pytest.mark.parametrize("automation_type", COUPON_BACKED_AUTOMATIONS)
def test_seed_keeps_coupon_for_personal_recovery_flows(automation_type: str) -> None:
    """cart_abandoned/winback/vip/back_in_stock stay on the coupon pool —
    those flows hand a code directly to one customer in WhatsApp, which
    is the original semantic of the coupon primitive."""
    seed = next(s for s in SEED_AUTOMATIONS if s["automation_type"] == automation_type)
    cfg = seed["config"]
    assert cfg.get("discount_source") != "promotion"


def test_ensure_default_promotions_seeds_and_links() -> None:
    db, engine = _make_db()
    try:
        t = _seed_tenant(db)
        seed_automations_if_empty(db, t.id)

        # Pre-condition: seasonal/salary automations exist with no promotion_id.
        rows = (
            db.query(SmartAutomation)
            .filter(
                SmartAutomation.tenant_id == t.id,
                SmartAutomation.automation_type.in_(PROMOTION_BACKED_AUTOMATIONS),
            )
            .all()
        )
        for r in rows:
            assert (r.config or {}).get("promotion_id") is None

        mutations = ensure_default_promotions_for_tenant(db, t.id)
        # 2 promotions + 2 automations linked = 4 mutations on first call.
        assert mutations == len(DEFAULT_PROMOTIONS) + len(PROMOTION_BACKED_AUTOMATIONS)
        db.commit()

        # Post-condition: every promotion-backed automation now has a
        # promotion_id pointing at its default promotion.
        rows = (
            db.query(SmartAutomation)
            .filter(
                SmartAutomation.tenant_id == t.id,
                SmartAutomation.automation_type.in_(PROMOTION_BACKED_AUTOMATIONS),
            )
            .all()
        )
        for r in rows:
            assert isinstance((r.config or {}).get("promotion_id"), int)

        # Promotions actually exist with the right slugs.
        promos = db.query(Promotion).filter(Promotion.tenant_id == t.id).all()
        slugs = {(p.extra_metadata or {}).get("slug") for p in promos}
        for slug in DEFAULT_PROMOTIONS:
            assert slug in slugs

        # Idempotent: second call is a no-op.
        mutations_2 = ensure_default_promotions_for_tenant(db, t.id)
        assert mutations_2 == 0
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 9. Single-engine guardrail still green — no new outbound provider
# ═════════════════════════════════════════════════════════════════════════════

# ═════════════════════════════════════════════════════════════════════════════
# 10. Router contract — CRUD + activate/pause + summary
# ═════════════════════════════════════════════════════════════════════════════

class _StubRequestState:
    def __init__(self, tenant_id: int) -> None:
        self.tenant_id = tenant_id
        self.jwt_payload = {"tenant_id": tenant_id, "sub": "test", "role": "merchant"}


class _StubRequest:
    def __init__(self, tenant_id: int) -> None:
        self.state = _StubRequestState(tenant_id)
        class _U:
            path = "/test"
        self.url = _U()


class TestPromotionsRouter:
    def test_create_then_get(self) -> None:
        from routers.promotions import (
            PromotionConditionsIn,
            PromotionCreateIn,
            create_promotion,
            get_promotion,
        )
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            body = PromotionCreateIn(
                name="Ramadan 15%",
                promotion_type="percentage",
                discount_value=15.0,
                conditions=PromotionConditionsIn(min_order_amount=200.0),
                status="active",
            )
            created = asyncio.run(create_promotion(
                body=body, request=_StubRequest(t.id), db=db,
            ))
            assert created["name"] == "Ramadan 15%"
            assert created["promotion_type"] == "percentage"
            assert created["discount_value"] == 15.0
            assert created["effective_status"] == "active"
            assert created["is_live"] is True

            fetched = asyncio.run(get_promotion(
                promotion_id=created["id"], request=_StubRequest(t.id), db=db,
            ))
            assert fetched["id"] == created["id"]
            assert fetched["conditions"]["min_order_amount"] == 200.0
        finally:
            db.close(); engine.dispose()

    def test_list_and_summary(self) -> None:
        from routers.promotions import list_promotions, promotions_summary
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            _seed_promotion(db, t.id, name="A", status=ACTIVE_STATUS)
            _seed_promotion(db, t.id, name="B", status=PAUSED_STATUS)
            _seed_promotion(db, t.id, name="C", status=DRAFT_STATUS)

            listed = asyncio.run(list_promotions(
                request=_StubRequest(t.id), status=None, promotion_type=None, db=db,
            ))
            assert len(listed["promotions"]) == 3

            active_only = asyncio.run(list_promotions(
                request=_StubRequest(t.id), status="active", promotion_type=None, db=db,
            ))
            assert len(active_only["promotions"]) == 1
            assert active_only["promotions"][0]["name"] == "A"

            summary = asyncio.run(promotions_summary(
                request=_StubRequest(t.id), db=db,
            ))
            assert summary["total"] == 3
            assert summary["active"] == 1
            assert summary["paused"] == 1
            assert summary["draft"] == 1

        finally:
            db.close(); engine.dispose()

    def test_pause_and_activate_round_trip(self) -> None:
        from routers.promotions import activate_promotion, pause_promotion
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            promo = _seed_promotion(db, t.id, status=ACTIVE_STATUS)

            paused = asyncio.run(pause_promotion(
                promotion_id=promo.id, request=_StubRequest(t.id), db=db,
            ))
            assert paused["status"] == PAUSED_STATUS

            reactivated = asyncio.run(activate_promotion(
                promotion_id=promo.id, request=_StubRequest(t.id), db=db,
            ))
            assert reactivated["status"] == ACTIVE_STATUS
        finally:
            db.close(); engine.dispose()

    def test_activate_expired_returns_409(self) -> None:
        from fastapi import HTTPException

        from routers.promotions import activate_promotion
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            past = datetime.now(timezone.utc) - timedelta(days=1)
            # SCHEDULED+past-end → effective=expired (draft is exempt by design,
            # so the activate guard only kicks in for previously-live rows).
            promo = _seed_promotion(db, t.id, status=SCHEDULED_STATUS, ends_at=past)
            with pytest.raises(HTTPException) as exc:
                asyncio.run(activate_promotion(
                    promotion_id=promo.id, request=_StubRequest(t.id), db=db,
                ))
            assert exc.value.status_code == 409
        finally:
            db.close(); engine.dispose()

    def test_invalid_promotion_type_returns_422(self) -> None:
        from fastapi import HTTPException

        from routers.promotions import PromotionCreateIn, create_promotion
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            body = PromotionCreateIn(
                name="bad",
                promotion_type="bogus_type",
                discount_value=5.0,
            )
            with pytest.raises(HTTPException) as exc:
                asyncio.run(create_promotion(
                    body=body, request=_StubRequest(t.id), db=db,
                ))
            assert exc.value.status_code == 422
        finally:
            db.close(); engine.dispose()

    def test_get_unknown_promotion_returns_404(self) -> None:
        from fastapi import HTTPException

        from routers.promotions import get_promotion
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            with pytest.raises(HTTPException) as exc:
                asyncio.run(get_promotion(
                    promotion_id=99999, request=_StubRequest(t.id), db=db,
                ))
            assert exc.value.status_code == 404
        finally:
            db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 11. Seasonal calendar — per-occasion Promotion rows + endpoint
# ═════════════════════════════════════════════════════════════════════════════
#
# `seasonal_offer` automation fans out across every entry in the
# Saudi calendar (founding_day, national_day, white_friday,
# ramadan_start, eid_al_fitr, eid_al_adha). Each occasion is now a
# first-class merchant-editable Promotion (tagged via
# `extra_metadata.occasion_slug`) so the merchant can set a different
# discount for Founding Day vs White Friday without touching the
# automation config. The engine prefers the per-occasion row at
# materialisation time and falls back to the generic
# `seasonal_default_15` Promotion that the automation's
# `config.promotion_id` points at.

EXPECTED_OCCASION_SLUGS = {
    "founding_day", "national_day", "white_friday",
    "ramadan_start", "eid_al_fitr", "eid_al_adha",
    "salary_payday",
}


class TestSeasonalOccasionCatalogue:
    def test_every_seasonal_occasion_has_a_default_promotion(self) -> None:
        # Each entry in SEASONAL_OCCASIONS must point at a Promotion
        # slug that is actually seeded by ensure_default_promotions_for_tenant
        # — otherwise the dashboard would render a card whose `promotion`
        # field never resolves.
        for spec in SEASONAL_OCCASIONS:
            assert spec["promotion_slug"] in DEFAULT_PROMOTIONS, (
                f"Seasonal occasion {spec['occasion_slug']!r} references "
                f"unknown promotion slug {spec['promotion_slug']!r}"
            )

    def test_calendar_covers_known_saudi_occasions(self) -> None:
        slugs = {spec["occasion_slug"] for spec in SEASONAL_OCCASIONS}
        assert slugs == EXPECTED_OCCASION_SLUGS

    def test_per_occasion_promotions_carry_occasion_slug_metadata(self) -> None:
        # The engine matches Promotion → CalendarEvent via the
        # `occasion_slug` key in extra_metadata. If it's missing, the
        # per-occasion routing silently falls back to the generic row,
        # which would defeat the merchant's per-occasion edits.
        for spec in SEASONAL_OCCASIONS:
            if spec["occasion_slug"] == "salary_payday":
                # Salary payday is NOT calendar-event-driven — it lives
                # under salary_payday_offer and is selected by the
                # automation's config.promotion_id alone.
                continue
            promo_spec = DEFAULT_PROMOTIONS[spec["promotion_slug"]]
            extras = promo_spec.get("extra_metadata") or {}
            assert extras.get("occasion_slug") == spec["occasion_slug"]


class TestPickPromotionIdForEvent:
    def test_returns_fallback_when_no_event_payload(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            picked = _pick_promotion_id_for_event(
                db,
                tenant_id=t.id,
                event_payload=None,
                fallback_promotion_id=42,
            )
            assert picked == 42
        finally:
            db.close(); engine.dispose()

    def test_returns_fallback_when_no_matching_occasion_promotion(self) -> None:
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            # Generic seasonal promotion exists but has no occasion_slug.
            _seed_promotion(db, t.id, status=ACTIVE_STATUS)
            picked = _pick_promotion_id_for_event(
                db,
                tenant_id=t.id,
                event_payload={"event_slug": "founding_day"},
                fallback_promotion_id=999,
            )
            assert picked == 999
        finally:
            db.close(); engine.dispose()

    def test_prefers_active_per_occasion_promotion(self) -> None:
        from sqlalchemy.orm.attributes import flag_modified

        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            generic = _seed_promotion(db, t.id, status=ACTIVE_STATUS, name="Generic")
            specific = _seed_promotion(db, t.id, status=ACTIVE_STATUS, name="Founding")
            specific.extra_metadata = {"occasion_slug": "founding_day"}
            flag_modified(specific, "extra_metadata")
            db.commit()

            picked = _pick_promotion_id_for_event(
                db,
                tenant_id=t.id,
                event_payload={"event_slug": "founding_day"},
                fallback_promotion_id=generic.id,
            )
            assert picked == specific.id
        finally:
            db.close(); engine.dispose()

    def test_skips_paused_per_occasion_promotion(self) -> None:
        # If the merchant paused the per-occasion row, the engine must
        # fall back to the configured automation-level promotion rather
        # than silently using the paused one.
        from sqlalchemy.orm.attributes import flag_modified

        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            paused = _seed_promotion(db, t.id, status=PAUSED_STATUS, name="PausedFD")
            paused.extra_metadata = {"occasion_slug": "founding_day"}
            flag_modified(paused, "extra_metadata")
            db.commit()

            picked = _pick_promotion_id_for_event(
                db,
                tenant_id=t.id,
                event_payload={"event_slug": "founding_day"},
                fallback_promotion_id=777,
            )
            assert picked == 777
        finally:
            db.close(); engine.dispose()


class TestSeasonalCalendarEndpoint:
    def test_returns_one_entry_per_known_occasion(self) -> None:
        from routers.promotions import seasonal_calendar
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            seed_automations_if_empty(db, t.id)
            payload = asyncio.run(seasonal_calendar(
                request=_StubRequest(t.id), db=db,
            ))
            slugs = {o["occasion_slug"] for o in payload["occasions"]}
            assert slugs == EXPECTED_OCCASION_SLUGS
        finally:
            db.close(); engine.dispose()

    def test_each_occasion_has_a_seeded_promotion_after_call(self) -> None:
        from routers.promotions import seasonal_calendar
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            seed_automations_if_empty(db, t.id)
            payload = asyncio.run(seasonal_calendar(
                request=_StubRequest(t.id), db=db,
            ))
            # `seasonal_calendar` calls ensure_default_promotions_for_tenant
            # so every occasion entry must come back with a non-None
            # `promotion` payload.
            for o in payload["occasions"]:
                assert o["promotion"] is not None, (
                    f"occasion {o['occasion_slug']} has no Promotion linked"
                )
                assert o["ai_summary"]
                assert o["automation_type"] in {"seasonal_offer", "salary_payday_offer"}
        finally:
            db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 12. Single-engine guardrail still green — no new outbound provider
# ═════════════════════════════════════════════════════════════════════════════

def test_promotion_path_does_not_send_directly() -> None:
    """The promotion engine never calls a WhatsApp provider directly —
    it only issues a Coupon row that the existing automation engine
    consumes via _resolve_auto_coupon. Asserts no `provider_send_message`
    import inside the new module."""
    promotion_engine_src = (REPO_ROOT / "backend" / "services" / "promotion_engine.py").read_text(encoding="utf-8")
    assert "provider_send_message" not in promotion_engine_src
    assert "send_template" not in promotion_engine_src
