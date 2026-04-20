"""
tests/test_abandoned_cart_recovery.py
──────────────────────────────────────
Coverage for the four-stage abandoned cart recovery workflow.

Architectural contract under test:

    Stage 1 — 30 minutes,    template,           NO coupon, URL→cart button
    Stage 2 — 6  hours,      interactive,        NO coupon, 3 dynamic buttons
    Stage 3 — 8  hours,      AI recovery,        OPTIONAL — disabled by default
    Stage 4 — 23h50m,        coupon CTA-URL,     auto-coupon ON, fires before
                                                 the 24-hour service window
                                                 closes so we don't open a
                                                 fresh paid marketing thread
                                                 to deliver a discount.

Stage 1 is emitted by the storefront snippet at abandonment time and
processed by the engine after its 30-minute delay. Stages 2-4 are
re-emitted by `scan_abandoned_cart_followups`, which writes a fresh
`cart_abandoned` AutomationEvent carrying `payload.step_idx` so the
engine picks the right step + delivery mode + coupon decision.

These tests pin the contract end-to-end at the data-shape level (no
WhatsApp send, no LLM call) so the user-facing promises — "no coupon at
30 minutes", "stay inside the 24h window", "respect Saudi quiet hours" —
cannot regress silently.
"""
from __future__ import annotations

import sys
from datetime import datetime, time, timedelta, timezone
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


def _seed_cart_automation(
    db, tenant_id: int, *, enabled: bool = True,
    config_override=None,
) -> SmartAutomation:
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
        config=config_override if config_override is not None else seed["config"],
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
            "source":       "storefront_snippet",
            "cart_total":   "199.00",
            "items":        2,
            "phone":        "+966555000111",
            "cart_id":      "cart-42",
            "checkout_url": "https://shop.example.sa/checkout/42",
        },
        processed=True,
        created_at=created,
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


# ═════════════════════════════════════════════════════════════════════════════
# 1. Seed shape — pins the user-facing 4-stage contract
# ═════════════════════════════════════════════════════════════════════════════

def _cart_seed():
    return next(
        s for s in SEED_AUTOMATIONS if s["automation_type"] == "abandoned_cart"
    )


def test_seed_has_four_stages_with_correct_delays() -> None:
    steps = _cart_seed()["config"]["steps"]
    assert len(steps) == 4
    assert steps[0]["delay_minutes"] == 30
    assert steps[1]["delay_minutes"] == 360    # 6 hours
    assert steps[2]["delay_minutes"] == 480    # 8 hours (AI recovery)
    assert steps[3]["delay_minutes"] == 1430   # 23h 50m — inside 24h window


def test_stage_four_fires_before_meta_24h_window_closes() -> None:
    """The whole point of stage 4 is to deliver the coupon push as a
    free in-window interactive instead of as a paid new marketing
    conversation. Anything ≥1440 would tip past the boundary."""
    steps = _cart_seed()["config"]["steps"]
    assert steps[3]["delay_minutes"] < 1440


def test_stage_one_uses_template_delivery_with_no_coupon() -> None:
    step = _cart_seed()["config"]["steps"][0]
    assert step["delivery_mode"] == "template"
    assert step.get("auto_coupon") is not True
    assert step.get("message_type") != "coupon"
    assert step["template_name"] == "abandoned_cart_recovery_ar"


def test_stage_two_uses_interactive_delivery_with_no_coupon() -> None:
    step = _cart_seed()["config"]["steps"][1]
    assert step["delivery_mode"] == "interactive"
    assert step.get("auto_coupon") is not True
    assert step.get("message_type") != "coupon"
    # Stage 2 always has a body so the in-window send has something
    # to render even when the merchant hasn't authored Arabic copy.
    assert step.get("body_text_ar")


def test_stage_three_is_optional_ai_recovery_disabled_by_default() -> None:
    """We never burn AI tokens unless the merchant explicitly opts in."""
    step = _cart_seed()["config"]["steps"][2]
    assert step["delivery_mode"] == "ai_recovery"
    assert step.get("enabled") is False
    assert step.get("ai_recovery_enabled") is False


def test_stage_four_is_coupon_cta_inside_window() -> None:
    step = _cart_seed()["config"]["steps"][3]
    assert step["delivery_mode"] == "interactive"
    assert step.get("auto_coupon") is True
    assert step.get("message_type") == "coupon"
    assert step["template_name"] == "abandoned_cart_final_offer_ar"
    # The "use the discount" button MUST be the primary CTA — that's
    # the visual the customer is meant to tap.
    assert "apply_coupon" in (step.get("buttons") or [])


def test_stage_four_template_carries_a_discount_slot() -> None:
    spec = DEFAULT_AUTOMATION_TEMPLATES["abandoned_cart_final_offer"]
    for lang in ("ar", "en"):
        slots = spec["languages"][lang]["slots"]
        assert "discount_code" in slots, (
            f"abandoned_cart_final_offer_{lang} must include a "
            f"discount_code slot — got {slots}"
        )


def test_stage_two_template_does_not_carry_a_discount_slot() -> None:
    spec = DEFAULT_AUTOMATION_TEMPLATES["abandoned_cart_followup"]
    for lang in ("ar", "en"):
        slots = spec["languages"][lang]["slots"]
        assert "discount_code" not in slots, (
            f"abandoned_cart_followup_{lang} should not carry a discount "
            f"slot — got {slots}"
        )


def test_global_saudi_quiet_hours_default_on() -> None:
    """Every Nahla merchant is in KSA — defaulting OFF would have us
    pinging customers at 3 a.m. on day one."""
    cfg = _cart_seed()["config"]
    assert cfg.get("respect_saudi_quiet_hours") is True


# ═════════════════════════════════════════════════════════════════════════════
# 2. Engine: step-idx awareness + delivery routing
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
    the configured 30 min."""
    cfg = _cart_seed()["config"]
    assert _resolve_delay(cfg, event=_StubEvent({})) == 30
    assert _resolve_delay(cfg, event=_StubEvent({"step_idx": 0})) == 30


def test_active_step_picks_coupon_stage_by_explicit_idx() -> None:
    """When the sweeper says `step_idx=3`, the engine must trust it
    even though the event is brand new (age 0). Otherwise stage 4
    would render the stage-1 template and lose the coupon contract."""
    cfg = _cart_seed()["config"]
    ev = _StubEvent({"step_idx": 3}, age=timedelta(0))
    step = _active_step_for_event(ev, cfg)
    assert step["template_name"] == "abandoned_cart_final_offer_ar"
    assert step.get("auto_coupon") is True
    assert step["delivery_mode"] == "interactive"


def test_active_step_picks_interactive_stage_two_by_explicit_idx() -> None:
    cfg = _cart_seed()["config"]
    ev = _StubEvent({"step_idx": 1}, age=timedelta(0))
    step = _active_step_for_event(ev, cfg)
    assert step["delivery_mode"] == "interactive"
    assert step["template_name"] == "abandoned_cart_followup_ar"


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


def test_stage_two_and_four_emitted_after_24_hours_skipping_disabled_ai() -> None:
    """A cart abandoned ~24 h ago should emit stage 2 + stage 4 on the
    same sweep, while stage 3 stays silent because `enabled=False`
    in the seed (no AI tokens, no noisy AutomationExecution row)."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        _seed_cart_automation(db, tenant.id)
        _emit_stage_one_event(
            db, tenant_id=tenant.id, customer_id=customer.id,
            age=timedelta(hours=24, minutes=5),
        )

        emitted = automation_emitters.scan_abandoned_cart_followups(
            db, tenant.id,
        )
        # stage 2 + stage 4 (stage 3 is disabled by default and skipped)
        assert emitted == 2

        followups = (
            db.query(AutomationEvent)
            .filter(AutomationEvent.processed.is_(False))
            .all()
        )
        step_ids = sorted((f.payload or {}).get("step_idx") for f in followups)
        assert step_ids == [1, 3]
    finally:
        db.close(); engine.dispose()


def test_stage_three_emitted_when_ai_recovery_explicitly_enabled() -> None:
    """If the merchant flips the AI step on in the editor, the sweeper
    must include it in the next sweep."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)

        seed_cfg = _cart_seed()["config"]
        cfg = {**seed_cfg, "steps": [dict(s) for s in seed_cfg["steps"]]}
        cfg["steps"][2]["enabled"] = True
        cfg["steps"][2]["ai_recovery_enabled"] = True
        cfg["ai_recovery_enabled"] = True

        _seed_cart_automation(db, tenant.id, config_override=cfg)
        _emit_stage_one_event(
            db, tenant_id=tenant.id, customer_id=customer.id,
            age=timedelta(hours=9),
        )

        emitted = automation_emitters.scan_abandoned_cart_followups(
            db, tenant.id,
        )
        # stage 2 (6h) + stage 3 (8h) — both due, stage 4 not yet
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
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        _seed_cart_automation(db, tenant.id)
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
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id, phone="+966555000111")
        _seed_cart_automation(db, tenant.id)
        original = _emit_stage_one_event(
            db, tenant_id=tenant.id, customer_id=customer.id,
            age=timedelta(hours=7),
        )

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


def test_followup_sweeper_ignores_events_older_than_48_hours() -> None:
    """Old, unattended cart events should not start emitting follow-ups
    when the merchant turns the toggle on weeks later."""
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


def test_per_step_disabled_does_not_block_subsequent_stages() -> None:
    """When stage 2 is disabled in the editor, stage 4 must still
    fire when its delay elapses — disable means "skip me", not
    "stop the workflow"."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)

        seed_cfg = _cart_seed()["config"]
        cfg = {**seed_cfg, "steps": [dict(s) for s in seed_cfg["steps"]]}
        cfg["steps"][1]["enabled"] = False        # turn off stage 2

        _seed_cart_automation(db, tenant.id, config_override=cfg)
        _emit_stage_one_event(
            db, tenant_id=tenant.id, customer_id=customer.id,
            age=timedelta(hours=24, minutes=5),
        )

        emitted = automation_emitters.scan_abandoned_cart_followups(
            db, tenant.id,
        )
        # Only stage 4 is emitted; stage 2 is recorded as skipped
        # in `recovery_followups` but no event is created for it.
        assert emitted == 1
        followups = (
            db.query(AutomationEvent)
            .filter(AutomationEvent.processed.is_(False))
            .all()
        )
        step_ids = sorted((f.payload or {}).get("step_idx") for f in followups)
        assert step_ids == [3]
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 4. Saudi-time guard
# ═════════════════════════════════════════════════════════════════════════════

from core.saudi_time_guard import (  # noqa: E402
    adjust_for_saudi_sleep_window,
    is_inside_quiet_hours,
)


def _utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_saudi_quiet_hours_detection() -> None:
    # 02:00 UTC → 05:00 KSA → INSIDE quiet window
    assert is_inside_quiet_hours(_utc(2026, 4, 20, 2, 0)) is True
    # 05:30 UTC → 08:30 KSA → OUTSIDE
    assert is_inside_quiet_hours(_utc(2026, 4, 20, 5, 30)) is False
    # 23:00 UTC → 02:00 KSA next day → INSIDE
    assert is_inside_quiet_hours(_utc(2026, 4, 20, 23, 0)) is True


def test_saudi_guard_defers_to_0830_ksa() -> None:
    """A 03:00 KSA scheduled send must defer to 08:30 KSA on the same day."""
    # 00:00 UTC = 03:00 KSA
    scheduled = _utc(2026, 4, 20, 0, 0)
    deferred  = adjust_for_saudi_sleep_window(scheduled)

    # 08:30 KSA = 05:30 UTC same day
    assert deferred == _utc(2026, 4, 20, 5, 30)


def test_saudi_guard_no_op_outside_quiet_window() -> None:
    scheduled = _utc(2026, 4, 20, 12, 0)    # 15:00 KSA, well clear
    assert adjust_for_saudi_sleep_window(scheduled) == scheduled


# ═════════════════════════════════════════════════════════════════════════════
# 5. Dynamic button id codec + Meta payload builders
# ═════════════════════════════════════════════════════════════════════════════

from services.cart_recovery_buttons import (  # noqa: E402
    ACTION_APPLY_COUPON,
    ACTION_RESUME_CART,
    attach_coupon_to_url,
    build_cta_url_payload,
    build_interactive_payload,
    decode_button_id,
    encode_button_id,
)


def test_button_id_round_trips_full_context() -> None:
    encoded = encode_button_id(
        ACTION_RESUME_CART,
        cart_id="cart-42", coupon_code="SAVE10",
        stage=2, automation_id=7,
    )
    assert encoded.startswith("cart:resume_cart:")
    decoded = decode_button_id(encoded)
    assert decoded == {
        "action":        "resume_cart",
        "cart_id":       "cart-42",
        "coupon_code":   "SAVE10",
        "stage":         2,
        "automation_id": 7,
    }


def test_button_id_decode_rejects_unknown_action() -> None:
    assert decode_button_id("cart:nuke_planet:c=1") is None
    assert decode_button_id("menu_price") is None        # legacy non-cart id
    assert decode_button_id("") is None


def test_interactive_payload_caps_at_three_buttons() -> None:
    payload = build_interactive_payload(
        to_phone="+966555000111",
        body_text="Hi",
        actions=[
            "resume_cart", "ask_question", "human_help",
            "postpone", "apply_coupon",
        ],
        cart_id="cart-42", stage=1,
    )
    btns = payload["interactive"]["action"]["buttons"]
    assert len(btns) == 3
    assert payload["type"] == "interactive"
    # Every rendered button must carry our dynamic id encoding so the
    # webhook can route the tap back to the right cart.
    for b in btns:
        assert b["reply"]["id"].startswith("cart:")


def test_cta_url_payload_uses_meta_cta_url_shape() -> None:
    payload = build_cta_url_payload(
        to_phone="+966555000111",
        body_text="Tap to use your discount",
        cta_label="استخدم الخصم",
        cta_url="https://shop.example.sa/checkout/42?coupon=SAVE10",
    )
    interactive = payload["interactive"]
    assert interactive["type"] == "cta_url"
    params = interactive["action"]["parameters"]
    assert params["url"].endswith("coupon=SAVE10")
    assert params["display_text"] == "استخدم الخصم"


def test_attach_coupon_to_url_appends_query_param() -> None:
    base = "https://shop.example.sa/checkout/42"
    assert attach_coupon_to_url(base, "SAVE10") == base + "?coupon=SAVE10"
    assert attach_coupon_to_url(base + "?ref=ad", "SAVE10") == (
        base + "?ref=ad&coupon=SAVE10"
    )
    # No coupon → URL unchanged.
    assert attach_coupon_to_url(base, None) == base
    # No URL → return as-is.
    assert attach_coupon_to_url("", "SAVE10") == ""


# ═════════════════════════════════════════════════════════════════════════════
# 6. Webhook button dispatcher (Rule-First, no AI)
# ═════════════════════════════════════════════════════════════════════════════

import asyncio  # noqa: E402

from services.cart_recovery_actions import (  # noqa: E402
    handle_cart_recovery_button,
    is_cart_recovery_button,
)


class _Recorder:
    """Captures the calls the dispatcher would make to the webhook senders."""
    def __init__(self):
        self.cta_calls:  list[dict] = []
        self.text_calls: list[dict] = []
        self.btn_calls:  list[dict] = []

    async def send_cta(self, **kw): self.cta_calls.append(kw)
    async def send_text(self, **kw): self.text_calls.append(kw)
    async def send_btn(self, **kw):  self.btn_calls.append(kw)


def test_is_cart_recovery_button_only_matches_cart_prefix() -> None:
    assert is_cart_recovery_button("cart:resume_cart:c=42") is True
    assert is_cart_recovery_button("contact_founder") is False
    assert is_cart_recovery_button("") is False
    assert is_cart_recovery_button(None) is False


def test_resume_cart_tap_replies_with_cart_url_cta() -> None:
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        auto = _seed_cart_automation(db, tenant.id)
        original = _emit_stage_one_event(
            db, tenant_id=tenant.id, customer_id=customer.id,
            age=timedelta(hours=7),
        )

        button = encode_button_id(
            ACTION_RESUME_CART, cart_id="cart-42",
            stage=1, automation_id=auto.id,
        )
        rec = _Recorder()
        handled = asyncio.run(handle_cart_recovery_button(
            db=db, button_id=button,
            phone_id="PHONE_ID", to_phone="+966555000111",
            tenant_id=tenant.id,
            send_cta_url=rec.send_cta, send_text=rec.send_text,
            send_buttons=rec.send_btn,
        ))
        assert handled is True
        assert len(rec.cta_calls) == 1
        cta = rec.cta_calls[0]
        # The CTA must point at the actual cart URL we baked into the
        # original event — that's the whole "no fake URL" promise.
        assert cta["btn_url"].startswith("https://shop.example.sa/checkout/42")

        # Outcome must be stamped on the parent event so the dashboard
        # funnel shows the tap.
        db.refresh(original)
        taps = (original.payload or {}).get("recovery_taps") or []
        assert taps and taps[-1]["outcome"] == "resume_cart"
    finally:
        db.close(); engine.dispose()


def test_apply_coupon_tap_attaches_code_to_url() -> None:
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        auto = _seed_cart_automation(db, tenant.id)
        _emit_stage_one_event(
            db, tenant_id=tenant.id, customer_id=customer.id,
            age=timedelta(hours=24),
        )

        button = encode_button_id(
            ACTION_APPLY_COUPON, cart_id="cart-42",
            coupon_code="CART10AUTO", stage=3, automation_id=auto.id,
        )
        rec = _Recorder()
        handled = asyncio.run(handle_cart_recovery_button(
            db=db, button_id=button,
            phone_id="PHONE_ID", to_phone="+966555000111",
            tenant_id=tenant.id,
            send_cta_url=rec.send_cta, send_text=rec.send_text,
            send_buttons=rec.send_btn,
        ))
        assert handled is True
        cta = rec.cta_calls[0]
        assert "coupon=CART10AUTO" in cta["btn_url"]
        assert "CART10AUTO" in cta["body_text"]
    finally:
        db.close(); engine.dispose()


def test_postpone_tap_reschedules_next_stage_only() -> None:
    """Postpone is a *snooze*, not a kill. The conversion-layer contract
    (see `services/cart_recovery_actions.py::_handle_postpone`) says the
    NEXT stage is rescheduled to fire `postpone_reschedule_minutes` in
    the future (default 12h), and a fresh AutomationEvent with a future
    `created_at` is written so the engine's wait-loop quietly holds it
    until the clock catches up. Stages after that one keep their
    original timings."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        auto = _seed_cart_automation(db, tenant.id)
        original = _emit_stage_one_event(
            db, tenant_id=tenant.id, customer_id=customer.id,
            age=timedelta(hours=2),
        )
        # The reschedule path reads automation_id from the event payload
        # to look up the configured delay.
        original.payload = {**(original.payload or {}), "automation_id": auto.id}
        db.commit(); db.refresh(original)

        button = encode_button_id(
            "postpone", cart_id="cart-42",
            stage=0, automation_id=auto.id,
        )
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        rec = _Recorder()
        handled = asyncio.run(handle_cart_recovery_button(
            db=db, button_id=button,
            phone_id="PHONE_ID", to_phone="+966555000111",
            tenant_id=tenant.id,
            send_cta_url=rec.send_cta, send_text=rec.send_text,
            send_buttons=rec.send_btn,
        ))
        assert handled is True
        # Reply is a calm acknowledgement — no CTA, no buttons.
        assert len(rec.text_calls) == 1
        assert not rec.cta_calls

        db.refresh(original)
        followups = (original.payload or {}).get("recovery_followups") or []
        rescheduled = [
            f for f in followups
            if f.get("reason") == "customer_postponed_rescheduled"
        ]
        # Only ONE stage is rescheduled (the next one), not all remaining.
        assert len(rescheduled) == 1
        assert rescheduled[0]["step_idx"] == 1

        # A brand-new AutomationEvent exists for that stage, with a
        # future created_at near the default 12h window.
        future_events = (
            db.query(AutomationEvent)
            .filter(AutomationEvent.id != original.id)
            .all()
        )
        assert len(future_events) == 1
        new_ev = future_events[0]
        assert (new_ev.payload or {}).get("step_idx") == 1
        assert (new_ev.payload or {}).get("reschedule_reason") == "customer_postponed"
        delta_minutes = (new_ev.created_at - before).total_seconds() / 60
        # Seed default is 720 minutes (12h). Tolerance for scheduling jitter.
        assert 715 <= delta_minutes <= 725
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 7. Trigger registration
# ═════════════════════════════════════════════════════════════════════════════

def test_cart_abandoned_trigger_is_canonical() -> None:
    """All re-emitted follow-ups must use the same trigger name as
    stage 1 so the engine matches them against the same SmartAutomation
    row. This is what makes the workflow "one automation, many stages"
    instead of one-automation-per-stage."""
    assert AutomationTrigger.CART_ABANDONED.value == "cart_abandoned"
