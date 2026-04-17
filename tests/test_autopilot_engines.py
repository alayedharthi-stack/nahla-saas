"""
tests/test_autopilot_engines.py
────────────────────────────────
Coverage for the 4-engine Autopilot redesign.

The product reorganises automations under 4 engines (recovery, growth,
experience, intelligence) and adds three new automations + their
emitters. This module locks down:

  1. Seed completeness — every new automation_type ships with engine,
     trigger_event, template_name, and lives in the canonical map.
  2. Master toggle — when TenantSettings.autopilot.enabled is False the
     engine writes one `skipped(autopilot_disabled)` execution per
     pending event and never calls _execute_action.
  3. Calendar windows — events_for_date returns the right CalendarEvent
     for each fixed Saudi date and is empty otherwise.
  4. Emitter idempotency — each scanner is safe to call multiple times
     in a row without duplicating events:
       • scan_unpaid_orders writes per-step progress to
         Order.extra_metadata and never re-emits the same step.
       • scan_predictive_reorders flips
         PredictiveReorderEstimate.notified after emitting.
       • _scan_seasonal records the year-per-slug it has already fired
         in TenantSettings.extra_metadata.
  5. Engines summary aggregator — sums per-automation KPIs into the
     four-engine payload and respects the windowing.
  6. Engine toggle endpoint — flips every automation in one engine in a
     single call and refuses unknown / unavailable engines.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, date as _date, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Tuple
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
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
    CustomerProfile,
    Order,
    PredictiveReorderEstimate,
    Product,
    SmartAutomation,
    Tenant,
    TenantSettings,
    WhatsAppConnection,
    WhatsAppTemplate,
)
from core.automation_engine import (  # noqa: E402
    emit_automation_event,
    process_pending_events,
)
from core.automations_seed import (  # noqa: E402
    ENGINE_BY_TYPE,
    SEED_AUTOMATIONS,
    ensure_engine_for_tenant,
    seed_automations_if_empty,
)
from core.automation_triggers import (  # noqa: E402
    AUTOMATION_TYPE_TO_TRIGGER,
    AutomationTrigger,
)
from core.calendar_events import (  # noqa: E402
    EID_AL_ADHA,
    EID_AL_FITR,
    FOUNDING_DAY,
    NATIONAL_DAY,
    RAMADAN_START,
    WHITE_FRIDAY,
    events_for_date,
)
from core.template_library import DEFAULT_AUTOMATION_TEMPLATES  # noqa: E402
from core import automation_emitters  # noqa: E402
from routers.automations import (  # noqa: E402
    ENGINE_DEFINITIONS,
    _aggregate_engine_kpis,
)


# ── DB harness (mirrors tests/test_automation_engine.py) ─────────────────────

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


def _set_autopilot(db, tenant_id: int, enabled: bool) -> None:
    settings = (
        db.query(TenantSettings)
        .filter(TenantSettings.tenant_id == tenant_id)
        .first()
    )
    if settings is None:
        settings = TenantSettings(tenant_id=tenant_id)
        db.add(settings)
    settings.extra_metadata = {"autopilot": {"enabled": bool(enabled)}}
    db.commit()


def _seed_customer(db, tenant_id: int, phone: str = "+966555000111") -> Customer:
    c = Customer(tenant_id=tenant_id, phone=phone, name="C")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _seed_template(db, tenant_id: int, name: str = "tpl") -> WhatsAppTemplate:
    t = WhatsAppTemplate(
        tenant_id=tenant_id, name=name, language="ar",
        category="MARKETING", status="APPROVED",
        components=[{"type": "BODY", "text": "مرحبا {{1}}"}],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _seed_wa_conn(db, tenant_id: int) -> WhatsAppConnection:
    c = WhatsAppConnection(
        tenant_id=tenant_id, status="connected",
        phone_number_id="PID", phone_number="+966500000000",
        sending_enabled=True, webhook_verified=True,
        connection_type="embedded", provider="meta",
    )
    db.add(c)
    db.commit()
    return c


# ═════════════════════════════════════════════════════════════════════════════
# 1. Seed completeness
# ═════════════════════════════════════════════════════════════════════════════

NEW_AUTOMATION_TYPES = (
    "unpaid_order_reminder",
    "seasonal_offer",
    "salary_payday_offer",
)


@pytest.mark.parametrize("automation_type", NEW_AUTOMATION_TYPES)
def test_new_automation_seeded_with_engine_and_trigger(automation_type: str) -> None:
    """Each new automation must be seeded with its engine + trigger_event."""
    seed = next(
        (s for s in SEED_AUTOMATIONS if s["automation_type"] == automation_type),
        None,
    )
    assert seed is not None, f"No seed for {automation_type}"
    assert seed["engine"] in {"recovery", "growth"}
    assert seed["engine"] == ENGINE_BY_TYPE[automation_type]
    assert seed["trigger_event"] == AUTOMATION_TYPE_TO_TRIGGER[automation_type].value
    # default off so a fresh tenant doesn't blast WhatsApp before opt-in.
    assert seed["enabled"] is False
    config = seed["config"]
    # Both AR and EN templates must point at real library entries.
    library_keys = set(DEFAULT_AUTOMATION_TEMPLATES.keys())
    assert automation_type in library_keys
    assert config["template_name"] == DEFAULT_AUTOMATION_TEMPLATES[automation_type]["languages"]["ar"]["template_name"]
    assert config["template_name_en"] == DEFAULT_AUTOMATION_TEMPLATES[automation_type]["languages"]["en"]["template_name"]


def test_engine_by_type_covers_every_seed_row() -> None:
    """ENGINE_BY_TYPE must cover every SEED_AUTOMATIONS automation_type.
    Drift here would land new automations under the wrong dashboard tab."""
    for seed in SEED_AUTOMATIONS:
        assert seed["automation_type"] in ENGINE_BY_TYPE, (
            f"Add `{seed['automation_type']}` → engine mapping in ENGINE_BY_TYPE"
        )
        assert seed["engine"] == ENGINE_BY_TYPE[seed["automation_type"]], (
            f"Engine mismatch for {seed['automation_type']}: "
            f"seed says {seed['engine']!r}, map says {ENGINE_BY_TYPE[seed['automation_type']]!r}"
        )


def test_seed_automations_persists_engine_for_new_tenant() -> None:
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        seed_automations_if_empty(db, tenant.id)
        db.commit()
        rows = db.query(SmartAutomation).all()
        # Every row should have a non-empty engine matching the canonical map.
        for r in rows:
            canonical = ENGINE_BY_TYPE.get(r.automation_type)
            if canonical is None:
                continue
            assert r.engine == canonical, (
                f"{r.automation_type}: stored {r.engine!r} != canonical {canonical!r}"
            )
        # And the three new automations are present.
        types = {r.automation_type for r in rows}
        for t in NEW_AUTOMATION_TYPES:
            assert t in types
    finally:
        db.close(); engine.dispose()


def test_ensure_engine_repairs_missing_engine() -> None:
    """If a row was created with the wrong engine (e.g. old schema), ensure_engine_for_tenant repairs it."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        # Insert a vip_upgrade row with WRONG engine = "recovery"
        a = SmartAutomation(
            tenant_id=tenant.id,
            automation_type="vip_upgrade",
            engine="recovery",   # wrong!
            trigger_event=AutomationTrigger.VIP_CUSTOMER_UPGRADE.value,
            name="VIP",
            enabled=False,
            config={},
        )
        db.add(a); db.commit()
        repaired = ensure_engine_for_tenant(db, tenant.id)
        db.commit()
        db.refresh(a)
        assert repaired == 1
        assert a.engine == "growth"
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 2. Master autopilot toggle
# ═════════════════════════════════════════════════════════════════════════════

def test_master_toggle_off_drains_pending_events_as_skipped() -> None:
    """When autopilot.enabled is False, every pending event becomes a `skipped`
    execution and `_execute_action` is never called."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        _set_autopilot(db, tenant.id, enabled=False)
        customer = _seed_customer(db, tenant.id)
        tpl = _seed_template(db, tenant.id)
        a = SmartAutomation(
            tenant_id=tenant.id,
            automation_type="abandoned_cart",
            engine="recovery",
            trigger_event="cart_abandoned",
            name="Cart",
            enabled=True,
            template_id=tpl.id,
            config={"template_name": "tpl"},
        )
        db.add(a); db.commit()

        emit_automation_event(
            db, tenant.id, "cart_abandoned",
            customer_id=customer.id, payload={}, commit=True,
        )

        with patch(
            "core.automation_engine._execute_action",
            new=AsyncMock(return_value=(True, {})),
        ) as mock_send:
            sent = asyncio.run(process_pending_events(db, tenant.id))

        assert sent == 0
        assert mock_send.call_count == 0
        # The pending event must be marked processed and have a `skipped` execution.
        ev = db.query(AutomationEvent).first()
        assert ev.processed is True
        execs = db.query(AutomationExecution).all()
        assert len(execs) == 1
        assert execs[0].status == "skipped"
        assert execs[0].skip_reason == "autopilot_disabled"
    finally:
        db.close(); engine.dispose()


def test_master_toggle_on_processes_normally() -> None:
    """Sanity check: with autopilot ON the engine still sends as before."""
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        _set_autopilot(db, tenant.id, enabled=True)
        customer = _seed_customer(db, tenant.id)
        # Profile so segment/conditions resolve cleanly.
        prof = CustomerProfile(
            tenant_id=tenant.id, customer_id=customer.id, segment="active",
            customer_status="active", total_orders=1, total_spend_sar=100.0,
            metrics_computed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            last_recomputed_reason="t",
        )
        db.add(prof); db.commit()
        tpl = _seed_template(db, tenant.id)
        _seed_wa_conn(db, tenant.id)
        a = SmartAutomation(
            tenant_id=tenant.id,
            automation_type="abandoned_cart",
            engine="recovery",
            trigger_event="cart_abandoned",
            name="Cart",
            enabled=True,
            template_id=tpl.id,
            config={"template_name": "tpl"},
        )
        db.add(a); db.commit()
        emit_automation_event(
            db, tenant.id, "cart_abandoned",
            customer_id=customer.id, payload={}, commit=True,
        )
        with patch(
            "core.automation_engine._execute_action",
            new=AsyncMock(return_value=(True, {})),
        ) as mock_send:
            asyncio.run(process_pending_events(db, tenant.id))
        assert mock_send.call_count == 1
        execs = db.query(AutomationExecution).all()
        assert len(execs) == 1
        assert execs[0].status == "sent"
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 3. Calendar event windows
# ═════════════════════════════════════════════════════════════════════════════

def test_national_day_match() -> None:
    matches = events_for_date(_date(2026, 9, 23))
    assert NATIONAL_DAY in matches


def test_founding_day_match() -> None:
    matches = events_for_date(_date(2026, 2, 22))
    assert FOUNDING_DAY in matches


def test_white_friday_is_last_friday_of_november() -> None:
    # November 2026: last Friday is Nov 27.
    matches = events_for_date(_date(2026, 11, 27))
    assert WHITE_FRIDAY in matches
    # The 26th (Thursday) must NOT match.
    assert WHITE_FRIDAY not in events_for_date(_date(2026, 11, 26))


def test_hijri_events_ramadan_and_eid_2026() -> None:
    assert RAMADAN_START in events_for_date(_date(2026, 2, 17))
    assert EID_AL_FITR  in events_for_date(_date(2026, 3, 19))
    assert EID_AL_ADHA  in events_for_date(_date(2026, 5, 26))


def test_no_event_returns_empty_list() -> None:
    assert events_for_date(_date(2026, 7, 1)) == []


def test_unknown_year_for_hijri_is_silently_empty() -> None:
    """Years not in the HIJRI table should never raise — they just yield nothing."""
    assert events_for_date(_date(2050, 3, 1)) == []


# ═════════════════════════════════════════════════════════════════════════════
# 4. Emitter idempotency
# ═════════════════════════════════════════════════════════════════════════════

def _seed_unpaid_automation(db, tenant_id: int) -> SmartAutomation:
    a = SmartAutomation(
        tenant_id=tenant_id,
        automation_type="unpaid_order_reminder",
        engine="recovery",
        trigger_event=AutomationTrigger.ORDER_PAYMENT_PENDING.value,
        name="Unpaid",
        enabled=True,
        config={
            "steps": [
                {"delay_minutes": 60,   "message_type": "reminder"},
                {"delay_minutes": 360,  "message_type": "reminder"},
                {"delay_minutes": 1440, "message_type": "final"},
            ],
        },
    )
    db.add(a); db.commit()
    return a


def test_unpaid_orders_emitter_emits_only_due_steps_and_is_idempotent() -> None:
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id, phone="+966500000001")
        _seed_unpaid_automation(db, tenant.id)

        # Order created 2 hours ago — only step 0 (60m) is due. Step 1 is at 6h.
        created = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(tzinfo=None)
        order = Order(
            tenant_id=tenant.id,
            external_id="O-1",
            status="pending",
            total="100",
            customer_info={"phone": "+966500000001"},
            line_items=[],
            extra_metadata={"created_at": created.isoformat()},
        )
        db.add(order); db.commit()

        emitted_first = automation_emitters.scan_unpaid_orders(db, tenant.id)
        assert emitted_first == 1
        # Second call without time passing must NOT re-emit step 0.
        emitted_second = automation_emitters.scan_unpaid_orders(db, tenant.id)
        assert emitted_second == 0

        events = db.query(AutomationEvent).filter_by(
            event_type=AutomationTrigger.ORDER_PAYMENT_PENDING.value,
        ).all()
        assert len(events) == 1
        assert events[0].payload["step_idx"] == 0
        assert events[0].payload["order_internal_id"] == order.id

        # extra_metadata.unpaid_reminders must record what we emitted.
        db.refresh(order)
        progress = (order.extra_metadata or {}).get("unpaid_reminders") or []
        assert len(progress) == 1
        assert progress[0]["step_idx"] == 0
    finally:
        db.close(); engine.dispose()


def test_unpaid_orders_emitter_skips_non_pending_orders() -> None:
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        _seed_customer(db, tenant.id, phone="+966500000002")
        _seed_unpaid_automation(db, tenant.id)
        created = (datetime.now(timezone.utc) - timedelta(hours=10)).replace(tzinfo=None)
        order = Order(
            tenant_id=tenant.id,
            external_id="O-2",
            status="completed",            # already paid → no reminder
            total="100",
            customer_info={"phone": "+966500000002"},
            line_items=[],
            extra_metadata={"created_at": created.isoformat()},
        )
        db.add(order); db.commit()
        assert automation_emitters.scan_unpaid_orders(db, tenant.id) == 0
        assert db.query(AutomationEvent).count() == 0
    finally:
        db.close(); engine.dispose()


def test_unpaid_orders_emitter_disabled_automation_is_noop() -> None:
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        _seed_customer(db, tenant.id, phone="+966500000003")
        a = _seed_unpaid_automation(db, tenant.id)
        a.enabled = False
        db.commit()
        created = (datetime.now(timezone.utc) - timedelta(hours=10)).replace(tzinfo=None)
        order = Order(
            tenant_id=tenant.id, external_id="O-3", status="pending",
            total="100", customer_info={"phone": "+966500000003"},
            line_items=[], extra_metadata={"created_at": created.isoformat()},
        )
        db.add(order); db.commit()
        assert automation_emitters.scan_unpaid_orders(db, tenant.id) == 0
        assert db.query(AutomationEvent).count() == 0
    finally:
        db.close(); engine.dispose()


def test_predictive_reorder_emitter_flips_notified_and_is_idempotent() -> None:
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        a = SmartAutomation(
            tenant_id=tenant.id,
            automation_type="predictive_reorder",
            engine="growth",
            trigger_event=AutomationTrigger.PREDICTIVE_REORDER_DUE.value,
            name="Reorder",
            enabled=True,
            config={"days_before": 3},
        )
        db.add(a); db.commit()
        # Product
        product = Product(
            tenant_id=tenant.id,
            external_id="P-1",
            title="عسل طلح",
        )
        db.add(product); db.commit()
        # Estimate due tomorrow → inside 3-day window.
        est = PredictiveReorderEstimate(
            tenant_id=tenant.id,
            customer_id=customer.id,
            product_id=product.id,
            predicted_reorder_date=(datetime.now(timezone.utc) + timedelta(days=1)).replace(tzinfo=None),
            notified=False,
        )
        db.add(est); db.commit()

        first = automation_emitters.scan_predictive_reorders(db, tenant.id)
        assert first == 1
        db.refresh(est)
        assert est.notified is True

        # Second pass must not emit again (notified flipped).
        assert automation_emitters.scan_predictive_reorders(db, tenant.id) == 0
        assert db.query(AutomationEvent).count() == 1
    finally:
        db.close(); engine.dispose()


def test_seasonal_emitter_dedupes_per_event_per_year() -> None:
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        # Customer + profile so they pass the audience filter.
        customer = _seed_customer(db, tenant.id)
        prof = CustomerProfile(
            tenant_id=tenant.id, customer_id=customer.id, segment="active",
            customer_status="active", total_orders=2, total_spend_sar=300.0,
            metrics_computed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            last_recomputed_reason="t",
        )
        db.add(prof); db.commit()
        # Settings row needed for the dedup log.
        db.add(TenantSettings(tenant_id=tenant.id, extra_metadata={}))
        db.commit()
        a = SmartAutomation(
            tenant_id=tenant.id,
            automation_type="seasonal_offer",
            engine="growth",
            trigger_event=AutomationTrigger.SEASONAL_EVENT_DUE.value,
            name="Seasonal",
            enabled=True,
            config={"audience": {"min_orders": 1}},
        )
        db.add(a); db.commit()

        # Pretend "today" is one day before national day — emitter targets
        # `target_day = today + 1`.
        national_day = _date(2026, 9, 23)
        first = automation_emitters._scan_seasonal(db, tenant.id, target_day=national_day)
        assert first == 1
        # Same call again must NOT re-emit for the same year.
        assert automation_emitters._scan_seasonal(db, tenant.id, target_day=national_day) == 0
        assert db.query(AutomationEvent).count() == 1
    finally:
        db.close(); engine.dispose()


def test_salary_emitter_only_fires_on_payday_and_dedupes_per_month() -> None:
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        prof = CustomerProfile(
            tenant_id=tenant.id, customer_id=customer.id, segment="active",
            customer_status="active", total_orders=2, total_spend_sar=300.0,
            metrics_computed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            last_recomputed_reason="t",
        )
        db.add(prof); db.commit()
        db.add(TenantSettings(tenant_id=tenant.id, extra_metadata={}))
        db.commit()
        a = SmartAutomation(
            tenant_id=tenant.id,
            automation_type="salary_payday_offer",
            engine="growth",
            trigger_event=AutomationTrigger.SALARY_PAYDAY_DUE.value,
            name="Payday",
            enabled=True,
            config={"payday_day": 27, "audience": {"min_orders": 1}},
        )
        db.add(a); db.commit()

        # Payday day = 27; target_day = 27 → fire. today = 26.
        today = _date(2026, 6, 26)
        target = _date(2026, 6, 27)
        first = automation_emitters._scan_salary(db, tenant.id, today=today, target_day=target)
        assert first == 1
        # Same month again → dedup.
        assert automation_emitters._scan_salary(db, tenant.id, today=today, target_day=target) == 0

        # Wrong day → no fire.
        wrong = _date(2026, 6, 15)
        assert automation_emitters._scan_salary(db, tenant.id, today=wrong, target_day=wrong) == 0
    finally:
        db.close(); engine.dispose()


# ═════════════════════════════════════════════════════════════════════════════
# 5. Engines summary aggregator
# ═════════════════════════════════════════════════════════════════════════════

def test_aggregate_engine_kpis_counts_only_recent_sent_executions() -> None:
    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        customer = _seed_customer(db, tenant.id)
        a = SmartAutomation(
            tenant_id=tenant.id,
            automation_type="abandoned_cart",
            engine="recovery",
            trigger_event="cart_abandoned",
            name="C",
            enabled=True,
            config={},
        )
        db.add(a); db.commit()

        # 1 recent sent execution + 1 stale (40d ago) + 1 skipped.
        now = datetime.now(timezone.utc)
        events = []
        for i in range(3):
            ev = AutomationEvent(
                tenant_id=tenant.id,
                event_type="cart_abandoned",
                customer_id=customer.id,
                payload={},
                processed=True,
                created_at=now - timedelta(days=2 + i),
            )
            db.add(ev)
            events.append(ev)
        db.commit()
        for e in events:
            db.refresh(e)
        db.add(AutomationExecution(
            tenant_id=tenant.id, automation_id=a.id, customer_id=customer.id,
            event_id=events[0].id, status="sent", executed_at=now - timedelta(days=2),
        ))
        db.add(AutomationExecution(
            tenant_id=tenant.id, automation_id=a.id, customer_id=customer.id,
            event_id=events[1].id, status="sent", executed_at=now - timedelta(days=40),
        ))
        db.add(AutomationExecution(
            tenant_id=tenant.id, automation_id=a.id, customer_id=customer.id,
            event_id=events[2].id, status="skipped", executed_at=now - timedelta(days=1),
        ))
        db.commit()

        result = _aggregate_engine_kpis(db, tenant.id, [a], days=30)
        bucket = result[a.id]
        # Only the 2-day-old `sent` row counts within the 30-day window.
        assert bucket["messages_sent"] == 1
        assert bucket["orders_attributed"] == 0
        assert bucket["revenue_sar"] == 0.0
    finally:
        db.close(); engine.dispose()


def test_engines_summary_definitions_cover_all_four_engines() -> None:
    keys = {d["engine"] for d in ENGINE_DEFINITIONS}
    assert keys == {"recovery", "growth", "experience", "intelligence"}
    available = {d["engine"] for d in ENGINE_DEFINITIONS if d["available"]}
    assert available == {"recovery", "growth"}


# ═════════════════════════════════════════════════════════════════════════════
# 6. Engine toggle endpoint behaviour
# ═════════════════════════════════════════════════════════════════════════════

class _StubRequestState:
    def __init__(self, tenant_id: int) -> None:
        self.tenant_id = tenant_id
        self.jwt_payload = {"tenant_id": tenant_id, "sub": "test", "role": "merchant"}


class _StubRequest:
    def __init__(self, tenant_id: int) -> None:
        self.state = _StubRequestState(tenant_id)
        # url.path is only read on the rejection branch; provide a stub.
        class _U:
            path = "/test"
        self.url = _U()


def test_toggle_engine_unknown_slug_raises_404() -> None:
    from routers.automations import EngineToggleIn, toggle_engine

    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(toggle_engine(
                engine="not_a_real_engine",
                body=EngineToggleIn(enabled=True),
                request=_StubRequest(tenant.id),
                db=db,
            ))
        assert exc.value.status_code == 404
    finally:
        db.close(); engine.dispose()


def test_toggle_engine_unavailable_engine_raises_409() -> None:
    """experience and intelligence are 'coming soon' — bulk toggle must refuse."""
    from routers.automations import EngineToggleIn, toggle_engine

    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(toggle_engine(
                engine="experience",
                body=EngineToggleIn(enabled=True),
                request=_StubRequest(tenant.id),
                db=db,
            ))
        assert exc.value.status_code == 409
    finally:
        db.close(); engine.dispose()


def test_toggle_engine_recovery_flips_all_recovery_automations() -> None:
    """Bulk toggle must flip every SmartAutomation row in that engine."""
    from routers.automations import EngineToggleIn, toggle_engine

    db, engine = _make_db()
    try:
        tenant = _seed_tenant(db)
        # Seed all default automations so we have multiple recovery rows.
        seed_automations_if_empty(db, tenant.id)
        db.commit()
        # All recovery automations start disabled per the seed.
        recovery_rows = db.query(SmartAutomation).filter(
            SmartAutomation.tenant_id == tenant.id,
            SmartAutomation.engine == "recovery",
        ).all()
        assert len(recovery_rows) >= 3
        assert all(not r.enabled for r in recovery_rows)

        # Bypass the billing gate.
        with patch("routers.automations.require_billing_access", return_value=None):
            result = asyncio.run(toggle_engine(
                engine="recovery",
                body=EngineToggleIn(enabled=True),
                request=_StubRequest(tenant.id),
                db=db,
            ))
        assert result["engine"] == "recovery"
        assert result["enabled"] is True
        assert result["automations_changed"] == len(recovery_rows)

        recovery_rows = db.query(SmartAutomation).filter(
            SmartAutomation.tenant_id == tenant.id,
            SmartAutomation.engine == "recovery",
        ).all()
        assert all(r.enabled for r in recovery_rows)

        # Growth rows must NOT have been touched.
        growth_rows = db.query(SmartAutomation).filter(
            SmartAutomation.tenant_id == tenant.id,
            SmartAutomation.engine == "growth",
        ).all()
        assert all(not r.enabled for r in growth_rows)
    finally:
        db.close(); engine.dispose()
