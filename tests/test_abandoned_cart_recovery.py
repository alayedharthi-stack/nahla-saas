"""
tests/test_abandoned_cart_recovery.py
──────────────────────────────────────
Coverage for the 3-stage abandoned cart recovery workflow.

Architectural contract under test:

    Stage 1 — 30 min, friendly reminder, NO coupon.
    Stage 2 — 6 hours, empathetic follow-up, NO coupon.
    Stage 3 — 24 hours, last-chance reminder, optional coupon.

Stage 1 is emitted by the storefront snippet and processed by the
existing engine cycle. Stages 2 and 3 are re-emitted by the new
`scan_abandoned_cart_followups` sweeper, which writes a fresh
`cart_abandoned` AutomationEvent carrying `payload.step_idx` so the
engine picks the right step + template + coupon decision.

These tests pin the contract end-to-end at the data-shape level
(no WhatsApp send, no LLM call) so the user-facing promise — "no
coupon at 30 minutes" — cannot regress silently.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
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
    AutomationEvent,
    Base,
    Customer,
    Order,
    SmartAutomation,
    Tenant,
)
from core import automation_emitters  # noqa: E402
from core.automation_engine import (  # noqa: E402
    _active_step_for_event,
    _resolve_delay,
)
from core.automation_triggers import AutomationTrigger  # noqa: E402
from core.automations_seed import SEED_AUTOMATIONS  # noqa: E402
from core.template_library import DEFAULT_AUTOMATION_TEMPLATES  # noqa: E402


# ── DB harness (mirrors tests/test_autopilot_engines.py) ─────────────────────

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


def _seed_tenant(db, name: str = "T") -> Tenant:
    t = Tenant(name=name, is_active=True)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _seed_customer(db, tenant_id: int, phone: str = "+966555000111") -> Customer:
    c = Customer(tenant_id=tenant_id, phone=phone, name="Sara")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _seed_cart_automation(db, tenant_id: int, *, enabled: bool = True) -> SmartAutomation:
    seed = next(
        s for s in SEED_AUTOMATIONS if s["automation_type"] == "abandoned_cart"
    )
    a = SmartAutomation(
        tenant_id=tenant_id,
        automation_type="abandoned_cart",
        engine="recovery",
        trigger_event=AutomationTrigger.CART_ABANDONED.value,
        name="Cart",
        enabled=enabled,
        config=seed["config"],
    )
    db.add(a)
    db.commit()
    return a


def _emit_stage_one_event(
    db, *, tenant_id: int, customer_id: int, age: timedelta,
) -> AutomationEvent:
    """Insert a processed stage-1 cart_abandoned event with the given age."""
    created = (datetime.now(timezone.utc) - age).replace(tzinfo=None)
    ev = AutomationEvent(
        tenant_id=tenant_id,
        event_type=AutomationTrigger.CART_ABANDONED.value,
        customer_id=customer_id,
        payload={
            "source":     "storefront_snippet",
            "cart_total": "199.00",
            "items":      2,
            "phone":      "+966555000111",
        },
        processed=True,
        created_at=created,
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


# ═════════════════════════════════════════════════════════════════════════════
# 1. Seed shape — pins the user-facing 3-stage contract
# ═════════════════════════════════════════════════════════════════════════════

def _cart_seed():
    return next(
        s for s in SEED_AUTOMATIONS if s["automation_type"] == "abandoned_cart"
    )


def test_seed_has_three_stages_with_correct_delays() -> None:
    steps = _cart_seed()["config"]["steps"]
    assert len(steps) == 3
    assert steps[0]["delay_minutes"] == 30
    assert steps[1]["delay_minutes"] == 360   # 6 hours
    assert steps[2]["delay_minutes"] == 1440  # 24 hours


def test_stage_one_has_no_coupon() -> None:
    """Friendly reminder only — no coupon at 30 minutes. This is the
    user-facing promise that broke the previous design."""
    step = _cart_seed()["config"]["steps"][0]
    assert step.get("auto_coupon") is not True
    assert step.get("message_type") != "coupon"
    # Per-step template must point at the plain reminder, not the
    # final-offer template (which carries a discount slot).
    assert step.get("template_name") == "abandoned_cart_recovery_ar"


def test_stage_two_has_no_coupon() -> None:
    """Empathetic follow-up — still no coupon at 6 hours."""
    step = _cart_seed()["config"]["steps"][1]
    assert step.get("auto_coupon") is not True
    assert step.get("message_type") != "coupon"
    assert step.get("template_name") == "abandoned_cart_followup_ar"


def test_stage_three_has_optional_coupon() -> None:
    """Stage 3 may attach a coupon. The auto_coupon flag is the legacy
    contract; tenants on OfferDecisionService ENFORCE/ADVISORY get the
    AI gate that decides whether to actually issue based on cart value
    or customer value (covered by tests/test_offer_decision.py)."""
    step = _cart_seed()["config"]["steps"][2]
    assert step.get("auto_coupon") is True
    assert step.get("message_type") == "coupon"
    assert step.get("template_name") == "abandoned_cart_final_offer_ar"


def test_stage_three_template_carries_a_discount_slot() -> None:
    """A coupon-bearing stage MUST have a discount slot in its template,
    otherwise the engine would resolve a coupon code and have nowhere
    to render it."""
    spec = DEFAULT_AUTOMATION_TEMPLATES["abandoned_cart_final_offer"]
    for lang in ("ar", "en"):
        slots = spec["languages"][lang]["slots"]
        assert "discount_code" in slots, (
            f"abandoned_cart_final_offer_{lang} must include a "
            f"discount_code slot — got {slots}"
        )


def test_stage_two_template_does_not_carry_a_discount_slot() -> None:
    """A no-coupon stage must NOT carry a discount slot — otherwise the
    template would render a literal `{{3}}` placeholder when the
    coupon resolver returns nothing for that stage."""
    spec = DEFAULT_AUTOMATION_TEMPLATES["abandoned_cart_followup"]
    for lang in ("ar", "en"):
        slots = spec["languages"][lang]["slots"]
        assert "discount_code" not in slots, (
            f"abandoned_cart_followup_{lang} should not carry a discount "
            f"slot — got {slots}"
        )


# ═════════════════════════════════════════════════════════════════════════════
# 2. Engine: step-idx awareness
# ═════════════════════════════════════════════════════════════════════════════

class _StubEvent:
    def __init__(self, payload, age=timedelta(0)):
        self.payload = payload
        self.created_at = datetime.now(timezone.utc) - age


def test_resolve_delay_returns_zero_when_payload_has_followup_step_idx() -> None:
    """A re-emitted event already paid the wait — the engine must NOT
    apply the stage-1 delay on top of it."""
    cfg = _cart_seed()["config"]
    ev = _StubEvent({"step_idx": 1})
    assert _resolve_delay(cfg, event=ev) == 0


def test_resolve_delay_keeps_legacy_behaviour_for_stage_one() -> None:
    """Stage-1 events (no step_idx, or step_idx == 0) must still wait
    the configured 30 min — that is the user-facing promise."""
    cfg = _cart_seed()["config"]
    assert _resolve_delay(cfg, event=_StubEvent({})) == 30
    assert _resolve_delay(cfg, event=_StubEvent({"step_idx": 0})) == 30


def test_active_step_picks_step_by_explicit_idx_not_age() -> None:
    """When the sweeper says `step_idx=2`, the engine must trust it
    even though the event is brand new (age 0). Otherwise stage 3
    would render the stage-1 template and coupon contract."""
    cfg = _cart_seed()["config"]
    ev = _StubEvent({"step_idx": 2}, age=timedelta(0))
    step = _active_step_for_event(ev, cfg)
    assert step["template_name"] == "abandoned_cart_final_offer_ar"
    assert step.get("auto_coupon") is True


def test_active_step_falls_back_to_age_when_payload_has_no_idx() -> None:
    """Legacy single-event automations (no step_idx) keep working."""
    cfg = _cart_seed()["config"]
    ev = _StubEvent({}, age=timedelta(minutes=45))
    step = _active_step_for_event(ev, cfg)
    assert step["template_name"] == "abandoned_cart_recovery_ar"


# ═════════════════════════════════════════════════════════════════════════════
# 3. scan_abandoned_cart_followups
# ═════════════════════════════════════════════════════════════════════════════

def test_no_followup_emitted_inside_six_hour_window() -> None:
    """Stage 1 just landed — nothing to do yet."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        _seed_cart_automation(db, tenant.id)
        _emit_stage_one_event(
            db, tenant_id=tenant.id, customer_id=customer.id,
            age=timedelta(hours=1),
        )

        emitted = automation_emitters.scan_abandoned_cart_followups(
            db, tenant.id,
        )
        assert emitted == 0
        assert db.query(AutomationEvent).count() == 1
    finally:
        db.close(); engine.dispose()


def test_stage_two_emitted_after_six_hours() -> None:
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        _seed_cart_automation(db, tenant.id)
        original = _emit_stage_one_event(
            db, tenant_id=tenant.id, customer_id=customer.id,
            age=timedelta(hours=7),
        )

        emitted = automation_emitters.scan_abandoned_cart_followups(
            db, tenant.id,
        )
        assert emitted == 1

        followups = (
            db.query(AutomationEvent)
            .filter(
                AutomationEvent.event_type == AutomationTrigger.CART_ABANDONED.value,
                AutomationEvent.processed.is_(False),
            )
            .all()
        )
        assert len(followups) == 1
        payload = followups[0].payload or {}
        assert payload["step_idx"] == 1
        assert payload["parent_event_id"] == original.id
        assert payload["source"] == "automation_emitters.cart_followups"
    finally:
        db.close(); engine.dispose()


def test_stage_two_and_three_emitted_after_twenty_four_hours() -> None:
    """A cart that has been abandoned 25 hours ago and never had any
    follow-up emitted should emit BOTH stages 2 and 3 in one sweep."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        _seed_cart_automation(db, tenant.id)
        _emit_stage_one_event(
            db, tenant_id=tenant.id, customer_id=customer.id,
            age=timedelta(hours=25),
        )

        emitted = automation_emitters.scan_abandoned_cart_followups(
            db, tenant.id,
        )
        assert emitted == 2

        followups = (
            db.query(AutomationEvent)
            .filter(AutomationEvent.processed.is_(False))
            .all()
        )
        step_ids = sorted((f.payload or {}).get("step_idx") for f in followups)
        assert step_ids == [1, 2]
    finally:
        db.close(); engine.dispose()


def test_followup_sweeper_is_idempotent() -> None:
    """Running the sweeper twice must not duplicate stage-2 events."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        _seed_cart_automation(db, tenant.id)
        _emit_stage_one_event(
            db, tenant_id=tenant.id, customer_id=customer.id,
            age=timedelta(hours=7),
        )

        first = automation_emitters.scan_abandoned_cart_followups(
            db, tenant.id,
        )
        second = automation_emitters.scan_abandoned_cart_followups(
            db, tenant.id,
        )
        assert (first, second) == (1, 0)
        assert db.query(AutomationEvent).count() == 2
    finally:
        db.close(); engine.dispose()


def test_followup_sweeper_does_not_recurse_on_followup_events() -> None:
    """A child event already carries `step_idx>0`. The sweeper must not
    treat that child as a parent — otherwise we'd cascade events
    forever once a stage-2 row exists."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        _seed_cart_automation(db, tenant.id)
        # Insert a stage-2 follow-up row directly (as if a previous sweep
        # already emitted it). It is NOT processed yet, but it is a
        # follow-up — so the sweeper must skip it as a "parent".
        ev = AutomationEvent(
            tenant_id=tenant.id,
            event_type=AutomationTrigger.CART_ABANDONED.value,
            customer_id=customer.id,
            payload={"step_idx": 1, "source": "automation_emitters.cart_followups"},
            processed=True,
            created_at=(datetime.now(timezone.utc) - timedelta(hours=20)).replace(tzinfo=None),
        )
        db.add(ev); db.commit()

        emitted = automation_emitters.scan_abandoned_cart_followups(
            db, tenant.id,
        )
        assert emitted == 0
    finally:
        db.close(); engine.dispose()


def test_followup_sweeper_stops_when_customer_completed_an_order() -> None:
    """Conversion guard: if the customer placed an order after
    abandoning the cart, the sweeper must NOT keep nagging them."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id, phone="+966555000111")
        _seed_cart_automation(db, tenant.id)
        original = _emit_stage_one_event(
            db, tenant_id=tenant.id, customer_id=customer.id,
            age=timedelta(hours=7),
        )

        # Customer came back and placed a real order 30 min after abandon.
        order = Order(
            tenant_id=tenant.id,
            external_id="O-RECOVERED",
            status="completed",
            total="199.00",
            customer_info={"phone": "+966555000111"},
            line_items=[],
            extra_metadata={
                "created_at": (
                    original.created_at + timedelta(minutes=30)
                ).isoformat(),
            },
        )
        db.add(order); db.commit()

        emitted = automation_emitters.scan_abandoned_cart_followups(
            db, tenant.id,
        )
        assert emitted == 0
    finally:
        db.close(); engine.dispose()


def test_followup_sweeper_disabled_automation_is_noop() -> None:
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        _seed_cart_automation(db, tenant.id, enabled=False)
        _emit_stage_one_event(
            db, tenant_id=tenant.id, customer_id=customer.id,
            age=timedelta(hours=25),
        )
        assert automation_emitters.scan_abandoned_cart_followups(
            db, tenant.id,
        ) == 0
    finally:
        db.close(); engine.dispose()


def test_followup_sweeper_ignores_events_older_than_thirty_six_hours() -> None:
    """Old, unattended cart events should not start emitting follow-ups
    when the merchant turns the toggle on weeks later — that would
    blast every dormant customer."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        _seed_cart_automation(db, tenant.id)
        _emit_stage_one_event(
            db, tenant_id=tenant.id, customer_id=customer.id,
            age=timedelta(days=7),
        )
        assert automation_emitters.scan_abandoned_cart_followups(
            db, tenant.id,
        ) == 0
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 4. Trigger registration
# ═════════════════════════════════════════════════════════════════════════════

def test_cart_abandoned_trigger_is_canonical() -> None:
    """All re-emitted follow-ups must use the same trigger name as
    stage 1 so the engine matches them against the same SmartAutomation
    row. This is what makes the workflow "one automation, many stages"
    instead of one-automation-per-stage."""
    assert AutomationTrigger.CART_ABANDONED.value == "cart_abandoned"
