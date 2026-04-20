"""
tests/test_conversion_layer.py
──────────────────────────────
Unit coverage for `services.conversion_layer` — the Rule-First brain
that decides WHAT to send when the abandoned-cart workflow tries to
fire a step.

Contract under test:

    • AI recovery only fires when at least one of:
         interacted | buttons tapped | cart > threshold | soft signals
    • Coupon is granted only when the customer hasn't converted, didn't
      just tap resume_cart, and the cart exceeds the minimum threshold.
    • Coupon value slides down as cart value slides up (margin guard).
    • Kill switch (blocked / opted_out) stops everything immediately.
    • Active conversations defer the step by 15 min — they don't kill it.
    • resume_cart / apply_coupon taps flatten every remaining stage
      (so we don't keep nagging a customer who already bought in).
    • postpone reschedules the NEXT stage for 12h (default, configurable)
      and issues ONE fresh AutomationEvent with a future created_at.
    • Dual-CTA rendering contract: stage-4 renders [resume_cart +
      apply_coupon + ask_question] when a coupon was granted, and
      [resume_cart + ask_question + human_help] (no apply_coupon) when
      it wasn't — the coupon button never appears without an actual
      coupon.
"""
from __future__ import annotations

import asyncio
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
    AutomationExecution,
    Base,
    Customer,
    MessageEvent,
    SmartAutomation,
    Tenant,
)
from core.automation_triggers import AutomationTrigger  # noqa: E402
from services import conversion_layer  # noqa: E402
from services.conversion_layer import (  # noqa: E402
    ConversionContext,
    buttons_for_stage,
    decide,
    dynamic_coupon_engine,
    enrich_body_with_coupon,
    format_coupon_block,
    is_active_conversation,
    is_killed,
    should_send_coupon,
    should_trigger_ai_recovery,
)
from services import cart_recovery_actions  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────

def _make_db() -> Tuple[Any, Any]:
    engine = create_engine("sqlite:///:memory:")
    _saved: list = []
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


def _seed_customer(db, tenant_id: int, **kwargs) -> Customer:
    c = Customer(
        tenant_id=tenant_id,
        phone=kwargs.get("phone", "+966555000111"),
        name=kwargs.get("name", "Sara"),
        extra_metadata=kwargs.get("extra_metadata"),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _ctx(**overrides) -> ConversionContext:
    base = dict(
        customer_id=1, customer_phone="+966555000111", tenant_id=1,
        stage=0, cart_value=250.0, cart_items=2,
        cart_url="https://shop.example.sa/checkout/42",
    )
    base.update(overrides)
    return ConversionContext(**base)


# ═════════════════════════════════════════════════════════════════════════════
# 1. AI recovery gate
# ═════════════════════════════════════════════════════════════════════════════

def test_ai_recovery_skipped_when_no_signal() -> None:
    ctx = _ctx(cart_value=100.0, messages_count=0, buttons_clicked=[])
    assert should_trigger_ai_recovery(ctx) is False


def test_ai_recovery_fires_when_customer_interacted() -> None:
    ctx = _ctx(messages_count=1)
    assert should_trigger_ai_recovery(ctx) is True


def test_ai_recovery_fires_on_button_tap() -> None:
    ctx = _ctx(buttons_clicked=["ask_question"], last_action="ask_question")
    assert should_trigger_ai_recovery(ctx) is True


def test_ai_recovery_fires_for_high_value_cart() -> None:
    ctx = _ctx(cart_value=1500.0)
    assert should_trigger_ai_recovery(ctx, high_value_threshold=500.0) is True


def test_ai_recovery_fires_on_still_thinking_signal() -> None:
    ctx = _ctx(last_action="still_thinking")
    assert should_trigger_ai_recovery(ctx) is True


# ═════════════════════════════════════════════════════════════════════════════
# 2. Coupon decision
# ═════════════════════════════════════════════════════════════════════════════

def test_coupon_skipped_after_order_completed() -> None:
    ctx = _ctx(order_completed=True, cart_value=500.0)
    assert should_send_coupon(ctx) is False


def test_coupon_skipped_after_resume_cart_tap() -> None:
    ctx = _ctx(last_action="resume_cart", cart_value=500.0)
    assert should_send_coupon(ctx) is False


def test_coupon_skipped_for_below_threshold_cart() -> None:
    ctx = _ctx(cart_value=50.0)
    assert should_send_coupon(ctx, min_cart_value=100.0) is False


def test_coupon_granted_when_all_gates_pass() -> None:
    ctx = _ctx(cart_value=300.0, last_action="postpone")
    assert should_send_coupon(ctx) is True


# ═════════════════════════════════════════════════════════════════════════════
# 3. Dynamic coupon value — sliding scale
# ═════════════════════════════════════════════════════════════════════════════

def test_dynamic_coupon_big_cart_gets_smallest_discount() -> None:
    assert dynamic_coupon_engine(_ctx(cart_value=1000.0)) == 5.0


def test_dynamic_coupon_medium_cart_gets_mid_tier() -> None:
    assert dynamic_coupon_engine(_ctx(cart_value=400.0)) == 8.0


def test_dynamic_coupon_small_cart_gets_headline_discount() -> None:
    assert dynamic_coupon_engine(_ctx(cart_value=150.0)) == 10.0


# ═════════════════════════════════════════════════════════════════════════════
# 4. Button intelligence (by stage + coupon state)
# ═════════════════════════════════════════════════════════════════════════════

def test_stage_four_with_coupon_renders_dual_cta() -> None:
    buttons = buttons_for_stage(3, coupon_granted=True)
    assert "resume_cart" in buttons
    assert "apply_coupon" in buttons
    assert len(buttons) <= 3


def test_stage_four_without_coupon_drops_apply_coupon() -> None:
    buttons = buttons_for_stage(3, coupon_granted=False)
    assert "resume_cart" in buttons
    assert "apply_coupon" not in buttons


def test_stage_one_default_buttons() -> None:
    buttons = buttons_for_stage(0, coupon_granted=False)
    assert buttons[0] == "resume_cart"
    assert "ask_question" in buttons
    assert "postpone" in buttons


# ═════════════════════════════════════════════════════════════════════════════
# 5. Kill switch + active conversation
# ═════════════════════════════════════════════════════════════════════════════

def test_kill_switch_blocked_customer() -> None:
    ctx = _ctx(customer_blocked=True)
    assert is_killed(ctx) is True


def test_kill_switch_opted_out_customer() -> None:
    ctx = _ctx(customer_opted_out=True)
    assert is_killed(ctx) is True


def test_active_conversation_detects_recent_inbound() -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ctx = _ctx(last_inbound_at=now - timedelta(minutes=3))
    assert is_active_conversation(ctx) is True


def test_active_conversation_ignores_old_inbound() -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ctx = _ctx(last_inbound_at=now - timedelta(minutes=30))
    assert is_active_conversation(ctx) is False


# ═════════════════════════════════════════════════════════════════════════════
# 6. decide() — top-level routing
# ═════════════════════════════════════════════════════════════════════════════

def test_decide_kill_switch_does_not_reschedule() -> None:
    ctx = _ctx(customer_blocked=True)
    d = decide(ctx, active_step={"delivery_mode": "template"}, config={})
    assert d.proceed is False
    assert d.skip_reason == "customer_blocked"
    assert d.reschedule_minutes == 0


def test_decide_order_completed_hard_skip() -> None:
    ctx = _ctx(order_completed=True)
    d = decide(ctx, active_step={"delivery_mode": "template"}, config={})
    assert d.proceed is False
    assert d.skip_reason == "order_completed"


def test_decide_active_conversation_reschedules() -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ctx = _ctx(last_inbound_at=now - timedelta(minutes=4))
    d = decide(ctx, active_step={"delivery_mode": "interactive"}, config={})
    assert d.proceed is False
    assert d.skip_reason == "user_active"
    assert d.reschedule_minutes > 0


def test_decide_ai_gate_skips_when_no_signal() -> None:
    ctx = _ctx(cart_value=50.0, messages_count=0, buttons_clicked=[])
    step = {"delivery_mode": "ai_recovery", "message_type": "ai_recovery"}
    d = decide(ctx, active_step=step, config={})
    assert d.proceed is False
    assert d.skip_reason == "no_signal"


def test_decide_ai_gate_fires_when_cart_is_high_value() -> None:
    ctx = _ctx(cart_value=1500.0)
    step = {"delivery_mode": "ai_recovery", "message_type": "ai_recovery"}
    d = decide(ctx, active_step=step, config={})
    assert d.proceed is True


def test_decide_coupon_stage_grants_coupon_and_dual_cta() -> None:
    ctx = _ctx(stage=3, cart_value=300.0, last_action="postpone")
    step = {
        "delivery_mode": "interactive",
        "message_type":  "coupon",
        "auto_coupon":   True,
    }
    d = decide(ctx, active_step=step, config={})
    assert d.proceed is True
    assert d.coupon_granted is True
    assert d.coupon_percent in (5.0, 8.0, 10.0)
    assert d.buttons_override is not None
    assert "apply_coupon" in d.buttons_override
    assert "resume_cart" in d.buttons_override


def test_decide_coupon_stage_skips_coupon_below_threshold() -> None:
    ctx = _ctx(stage=3, cart_value=50.0)
    step = {
        "delivery_mode": "interactive",
        "message_type":  "coupon",
        "auto_coupon":   True,
    }
    d = decide(ctx, active_step=step, config={})
    assert d.proceed is True
    assert d.coupon_granted is False
    assert "apply_coupon" not in (d.buttons_override or [])


def test_decide_swaps_resume_cart_to_open_store_when_cart_url_missing() -> None:
    ctx = _ctx(cart_url=None, store_url="https://shop.example.sa/")
    step = {"delivery_mode": "interactive", "buttons": ["resume_cart", "ask_question"]}
    d = decide(ctx, active_step=step, config={})
    assert d.buttons_override is not None
    assert "open_store" in d.buttons_override
    assert "resume_cart" not in d.buttons_override


# ═════════════════════════════════════════════════════════════════════════════
# 7. Coupon presentation (premium UX)
# ═════════════════════════════════════════════════════════════════════════════

def test_format_coupon_block_has_backticks_and_percent() -> None:
    block = format_coupon_block("SAVE10", 10.0)
    assert "`SAVE10`" in block
    assert "10" in block


def test_enrich_body_replaces_placeholder_when_present() -> None:
    body = "مرحباً! استخدم {{discount_code}} الآن."
    enriched = enrich_body_with_coupon(body, "SAVE10", 10.0)
    assert "{{discount_code}}" not in enriched
    assert "`SAVE10`" in enriched


def test_enrich_body_appends_block_when_no_placeholder() -> None:
    body = "رسالة بدون placeholder."
    enriched = enrich_body_with_coupon(body, "SAVE10", 10.0)
    assert body in enriched
    assert "`SAVE10`" in enriched


# ═════════════════════════════════════════════════════════════════════════════
# 8. build_context — DB-backed
# ═════════════════════════════════════════════════════════════════════════════

def test_build_context_reads_inbound_count_and_last_tap() -> None:
    db, _eng = _make_db()
    tenant = _seed_tenant(db)
    customer = _seed_customer(db, tenant.id, extra_metadata={
        "marketing_opt_out": True,
    })
    auto = SmartAutomation(
        tenant_id=tenant.id,
        automation_type="abandoned_cart",
        engine="recovery",
        trigger_event=AutomationTrigger.CART_ABANDONED.value,
        name="Cart",
        enabled=True,
        config={},
    )
    db.add(auto)
    db.commit()

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ev = AutomationEvent(
        tenant_id=tenant.id,
        event_type=AutomationTrigger.CART_ABANDONED.value,
        customer_id=customer.id,
        payload={
            "cart_total":   "400.00",
            "items":        3,
            "cart_id":      "c42",
            "checkout_url": "https://shop.example.sa/c/42",
            "recovery_taps": [
                {"outcome": "ask_question", "stage": 0},
            ],
        },
        processed=True,
        created_at=now - timedelta(hours=2),
    )
    db.add(ev)

    msg = MessageEvent(
        tenant_id=tenant.id,
        direction="inbound",
        body="متى يوصل؟",
        event_type="whatsapp",
        extra_metadata={"phone": customer.phone},
        created_at=now - timedelta(minutes=30),
    )
    db.add(msg)
    db.commit()
    db.refresh(ev)

    ctx = conversion_layer.build_context(
        db, tenant_id=tenant.id, event=ev, customer=customer,
        automation=auto, active_step={}, config={},
    )

    assert ctx.cart_value == 400.0
    assert ctx.cart_items == 3
    assert ctx.cart_url == "https://shop.example.sa/c/42"
    assert ctx.messages_count == 1
    assert ctx.buttons_clicked == ["ask_question"]
    assert ctx.last_action == "ask_question"
    assert ctx.customer_opted_out is True
    assert ctx.customer_blocked is False


# ═════════════════════════════════════════════════════════════════════════════
# 9. Webhook — resume flattens funnel, postpone reschedules next stage
# ═════════════════════════════════════════════════════════════════════════════

async def _noop(*args, **kwargs):
    return None


def test_resume_cart_tap_marks_all_remaining_stages_skipped() -> None:
    db, _eng = _make_db()
    tenant = _seed_tenant(db)
    customer = _seed_customer(db, tenant.id)
    auto = SmartAutomation(
        tenant_id=tenant.id, automation_type="abandoned_cart",
        engine="recovery",
        trigger_event=AutomationTrigger.CART_ABANDONED.value,
        name="Cart", enabled=True, config={},
    )
    db.add(auto); db.commit(); db.refresh(auto)

    ev = AutomationEvent(
        tenant_id=tenant.id,
        event_type=AutomationTrigger.CART_ABANDONED.value,
        customer_id=customer.id,
        payload={
            "cart_id":      "cart-42",
            "checkout_url": "https://shop.example.sa/checkout/42",
        },
        processed=True,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1),
    )
    db.add(ev); db.commit(); db.refresh(ev)

    exe = AutomationExecution(
        tenant_id=tenant.id,
        automation_id=auto.id,
        event_id=ev.id,
        status="sent",
        action_taken={},
        executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(exe); db.commit()

    btn_id = f"cart:resume_cart:c=cart-42:s=0:a={auto.id}"

    async def run():
        ok = await cart_recovery_actions.handle_cart_recovery_button(
            db=db, button_id=btn_id, phone_id="p1", to_phone=customer.phone,
            tenant_id=tenant.id,
            send_cta_url=_noop, send_text=_noop, send_buttons=_noop,
        )
        assert ok is True

    asyncio.run(run())

    db.refresh(ev)
    followups = (ev.payload or {}).get("recovery_followups") or []
    future_stages_skipped = [
        f for f in followups if f.get("reason") == "user_resume_cart"
    ]
    assert len(future_stages_skipped) >= 3    # stages 1, 2, 3 flattened
    assert (ev.payload or {}).get("recovery_resumed_at")


def test_postpone_tap_reschedules_next_stage_not_all_remaining() -> None:
    db, _eng = _make_db()
    tenant = _seed_tenant(db)
    customer = _seed_customer(db, tenant.id)
    auto = SmartAutomation(
        tenant_id=tenant.id, automation_type="abandoned_cart",
        engine="recovery",
        trigger_event=AutomationTrigger.CART_ABANDONED.value,
        name="Cart", enabled=True,
        config={"postpone_reschedule_minutes": 720},
    )
    db.add(auto); db.commit(); db.refresh(auto)

    before_postpone = datetime.now(timezone.utc).replace(tzinfo=None)
    ev = AutomationEvent(
        tenant_id=tenant.id,
        event_type=AutomationTrigger.CART_ABANDONED.value,
        customer_id=customer.id,
        payload={
            "cart_id":      "cart-77",
            "checkout_url": "https://shop.example.sa/checkout/77",
            "automation_id": auto.id,
        },
        processed=True,
        created_at=before_postpone - timedelta(hours=6),
    )
    db.add(ev); db.commit(); db.refresh(ev)

    # Customer taps postpone on stage 1 (step_idx=1)
    btn_id = f"cart:postpone:c=cart-77:s=1:a={auto.id}"

    async def run():
        await cart_recovery_actions.handle_cart_recovery_button(
            db=db, button_id=btn_id, phone_id="p1", to_phone=customer.phone,
            tenant_id=tenant.id,
            send_cta_url=_noop, send_text=_noop, send_buttons=_noop,
        )

    asyncio.run(run())

    # 1. The parent event's recovery_followups must mark ONLY the
    # next stage (stage 2) as rescheduled — NOT every remaining stage.
    db.refresh(ev)
    followups = (ev.payload or {}).get("recovery_followups") or []
    rescheduled = [
        f for f in followups
        if f.get("reason") == "customer_postponed_rescheduled"
    ]
    assert len(rescheduled) == 1
    assert rescheduled[0]["step_idx"] == 2
    assert rescheduled[0]["rescheduled_for_minutes"] == 720

    # 2. A brand-new AutomationEvent must exist for that same stage
    # with a created_at ~12h in the future (within a minute of 720m).
    future_events = (
        db.query(AutomationEvent)
        .filter(AutomationEvent.id != ev.id)
        .all()
    )
    assert len(future_events) == 1
    new_ev = future_events[0]
    assert (new_ev.payload or {}).get("step_idx") == 2
    assert (new_ev.payload or {}).get("reschedule_reason") == "customer_postponed"
    delta_minutes = (new_ev.created_at - before_postpone).total_seconds() / 60
    assert 715 <= delta_minutes <= 725


def test_postpone_respects_merchant_configured_reschedule_minutes() -> None:
    db, _eng = _make_db()
    tenant = _seed_tenant(db)
    customer = _seed_customer(db, tenant.id)
    auto = SmartAutomation(
        tenant_id=tenant.id, automation_type="abandoned_cart",
        engine="recovery",
        trigger_event=AutomationTrigger.CART_ABANDONED.value,
        name="Cart", enabled=True,
        config={"postpone_reschedule_minutes": 240},   # merchant → 4h
    )
    db.add(auto); db.commit(); db.refresh(auto)

    ev = AutomationEvent(
        tenant_id=tenant.id,
        event_type=AutomationTrigger.CART_ABANDONED.value,
        customer_id=customer.id,
        payload={"cart_id": "c1", "automation_id": auto.id},
        processed=True,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(ev); db.commit(); db.refresh(ev)

    async def run():
        await cart_recovery_actions.handle_cart_recovery_button(
            db=db, button_id=f"cart:postpone:c=c1:s=0:a={auto.id}",
            phone_id="p", to_phone=customer.phone, tenant_id=tenant.id,
            send_cta_url=_noop, send_text=_noop, send_buttons=_noop,
        )

    asyncio.run(run())

    db.refresh(ev)
    rescheduled = [
        f for f in (ev.payload or {}).get("recovery_followups") or []
        if f.get("reason") == "customer_postponed_rescheduled"
    ]
    assert rescheduled[0]["rescheduled_for_minutes"] == 240


# ═════════════════════════════════════════════════════════════════════════════
# 10. Metrics stamped on action_taken for every tap
# ═════════════════════════════════════════════════════════════════════════════

def test_resume_cart_tap_bumps_converted_metric() -> None:
    db, _eng = _make_db()
    tenant = _seed_tenant(db)
    customer = _seed_customer(db, tenant.id)
    auto = SmartAutomation(
        tenant_id=tenant.id, automation_type="abandoned_cart",
        engine="recovery",
        trigger_event=AutomationTrigger.CART_ABANDONED.value,
        name="Cart", enabled=True, config={},
    )
    db.add(auto); db.commit(); db.refresh(auto)
    ev = AutomationEvent(
        tenant_id=tenant.id,
        event_type=AutomationTrigger.CART_ABANDONED.value,
        customer_id=customer.id,
        payload={"cart_id": "c9", "checkout_url": "https://x.example/c/9"},
        processed=True,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(ev); db.commit(); db.refresh(ev)
    exe = AutomationExecution(
        tenant_id=tenant.id, automation_id=auto.id, event_id=ev.id,
        status="sent", action_taken={"metrics": {"sent": 1}},
        executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(exe); db.commit()

    async def run():
        await cart_recovery_actions.handle_cart_recovery_button(
            db=db, button_id=f"cart:resume_cart:c=c9:s=0:a={auto.id}",
            phone_id="p", to_phone=customer.phone, tenant_id=tenant.id,
            send_cta_url=_noop, send_text=_noop, send_buttons=_noop,
        )

    asyncio.run(run())

    db.refresh(exe)
    metrics = (exe.action_taken or {}).get("metrics") or {}
    assert metrics.get("clicked", 0) >= 1
    assert metrics.get("resumed_cart", 0) == 1
    assert metrics.get("converted", 0) == 1


def test_postpone_tap_bumps_postponed_metric_not_converted() -> None:
    db, _eng = _make_db()
    tenant = _seed_tenant(db)
    customer = _seed_customer(db, tenant.id)
    auto = SmartAutomation(
        tenant_id=tenant.id, automation_type="abandoned_cart",
        engine="recovery",
        trigger_event=AutomationTrigger.CART_ABANDONED.value,
        name="Cart", enabled=True, config={},
    )
    db.add(auto); db.commit(); db.refresh(auto)
    ev = AutomationEvent(
        tenant_id=tenant.id,
        event_type=AutomationTrigger.CART_ABANDONED.value,
        customer_id=customer.id,
        payload={"cart_id": "c8", "automation_id": auto.id},
        processed=True,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(ev); db.commit(); db.refresh(ev)
    exe = AutomationExecution(
        tenant_id=tenant.id, automation_id=auto.id, event_id=ev.id,
        status="sent", action_taken={},
        executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(exe); db.commit()

    async def run():
        await cart_recovery_actions.handle_cart_recovery_button(
            db=db, button_id=f"cart:postpone:c=c8:s=0:a={auto.id}",
            phone_id="p", to_phone=customer.phone, tenant_id=tenant.id,
            send_cta_url=_noop, send_text=_noop, send_buttons=_noop,
        )

    asyncio.run(run())

    db.refresh(exe)
    metrics = (exe.action_taken or {}).get("metrics") or {}
    assert metrics.get("postponed", 0) == 1
    assert metrics.get("converted", 0) == 0
