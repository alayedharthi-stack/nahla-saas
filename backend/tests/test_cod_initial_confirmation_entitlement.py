import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from core.automation_engine import (
    _is_initial_cod_confirmation_order_update,
    _try_execute,
)
from core.send_governor import check as governor_check
from core.send_governor import record_sent
from models import (
    AutomationEvent,
    AutomationExecution,
    Base,
    GovernorSendLog,
    SmartAutomation,
)


def _event(*, event_type="order_cod_pending", message_type=None):
    payload = {}
    if message_type is not None:
        payload["message_type"] = message_type
    return SimpleNamespace(event_type=event_type, payload=payload)


def test_initial_cod_prompt_is_a_transactional_order_update():
    assert _is_initial_cod_confirmation_order_update(
        "cod_confirmation",
        _event(message_type="initial_confirmation"),
    )


def test_cod_reminders_remain_growth_autopilot():
    assert not _is_initial_cod_confirmation_order_update(
        "cod_confirmation",
        _event(message_type="reminder"),
    )
    assert not _is_initial_cod_confirmation_order_update(
        "cod_confirmation",
        _event(),
    )


def test_unrelated_order_events_do_not_bypass_entitlements():
    assert not _is_initial_cod_confirmation_order_update(
        "cod_confirmation",
        _event(event_type="order_created", message_type="initial_confirmation"),
    )
    assert not _is_initial_cod_confirmation_order_update(
        "customer_winback",
        _event(message_type="initial_confirmation"),
    )


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    saved = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, JSONB):
                saved.append((column, column.type))
                column.type = JSON()
    Base.metadata.create_all(engine)
    for column, original in saved:
        column.type = original
    return sessionmaker(bind=engine)(), engine


def _stored_cod_event(db, *, message_type, step_idx=0):
    automation = SmartAutomation(
        tenant_id=1,
        automation_type="cod_confirmation",
        name="COD",
        enabled=True,
        trigger_event="order_cod_pending",
        config={
            "steps": [
                {"message_type": "reminder", "delay_minutes": 120},
                {"message_type": "reminder", "delay_minutes": 360},
            ],
        },
    )
    event = AutomationEvent(
        tenant_id=1,
        event_type="order_cod_pending",
        customer_id=6001,
        payload={
            "external_id": "COD-INITIAL-REGRESSION",
            "order_id": 7001,
            "order_internal_id": 7001,
            "message_type": message_type,
            "step_idx": step_idx,
        },
        processed=False,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add_all([automation, event])
    db.commit()
    return automation, event


def test_starter_initial_cod_bypasses_reminder_delay_and_executes_once():
    """The transactional prompt is immediate even though automation 34's
    first configured reminder is delayed by 120 minutes.
    """
    db, engine = _make_db()
    try:
        automation, event = _stored_cod_event(
            db,
            message_type="initial_confirmation",
        )
        record_sent(db, 1, event.customer_id, "abandoned_cart")
        db.commit()
        starter = SimpleNamespace(plan_slug="starter", has_feature=lambda _: False)
        sender = AsyncMock(return_value=(True, {"template": "nahla_cod_confirmation"}))

        with patch(
            "core.plan_entitlements.get_entitlements",
            return_value=starter,
        ) as get_entitlements, patch(
            "core.automation_engine._execute_action",
            new=sender,
        ):
            first = asyncio.run(
                _try_execute(db, 1, event, automation, event.created_at)
            )
            second = asyncio.run(
                _try_execute(db, 1, event, automation, event.created_at)
            )

        assert first == "sent"
        assert second == "sent"
        get_entitlements.assert_not_called()
        sender.assert_awaited_once()
        execution = db.query(AutomationExecution).one()
        assert execution.status == "sent"
        assert execution.event_id == event.id
        assert execution.automation_id == automation.id
        assert db.query(GovernorSendLog).filter_by(
            tenant_id=1,
            customer_id=event.customer_id,
        ).count() == 2
    finally:
        db.close()
        engine.dispose()


def test_initial_cod_exemption_requires_order_identity_and_bypasses_limits():
    db, engine = _make_db()
    try:
        record_sent(db, 1, 6001, "abandoned_cart")
        record_sent(db, 1, 6001, "customer_winback")
        db.commit()

        allowed = governor_check(
            db,
            tenant_id=1,
            customer_id=6001,
            automation_type="cod_confirmation",
            order_id=7001,
            initial_cod_confirmation=True,
        )
        missing_order = governor_check(
            db,
            tenant_id=1,
            customer_id=6001,
            automation_type="cod_confirmation",
            initial_cod_confirmation=True,
        )
        marketing = governor_check(
            db,
            tenant_id=1,
            customer_id=6001,
            automation_type="new_product_alert",
            order_id=7001,
            initial_cod_confirmation=True,
        )
        with patch(
            "core.send_governor._last_sent_any_service",
            return_value=None,
        ):
            missing_order_daily = governor_check(
                db,
                tenant_id=1,
                customer_id=6001,
                automation_type="cod_confirmation",
                initial_cod_confirmation=True,
            )

        assert allowed.allowed is True
        assert allowed.reason_code == "allowed"
        assert missing_order.allowed is False
        assert missing_order.reason_code == "blocked_by_6h_limit"
        assert marketing.allowed is False
        assert marketing.reason_code == "blocked_by_6h_limit"
        assert missing_order_daily.allowed is False
        assert missing_order_daily.reason_code == "blocked_by_daily_limit"
    finally:
        db.close()
        engine.dispose()


def test_growth_cod_reminder_inside_governor_window_remains_delayed():
    db, engine = _make_db()
    try:
        automation, event = _stored_cod_event(
            db,
            message_type="reminder",
            step_idx=1,
        )
        record_sent(db, 1, event.customer_id, "abandoned_cart")
        db.commit()
        growth = SimpleNamespace(plan_slug="growth", has_feature=lambda _: True)
        sender = AsyncMock(return_value=(True, {}))

        with patch(
            "core.plan_entitlements.get_entitlements",
            return_value=growth,
        ) as get_entitlements, patch(
            "core.automation_engine._execute_action",
            new=sender,
        ):
            result = asyncio.run(
                _try_execute(db, 1, event, automation, event.created_at)
            )

        assert result == "delay"
        get_entitlements.assert_called_once_with(db, 1)
        sender.assert_not_awaited()
        assert db.query(AutomationExecution).count() == 0
        assert event.processed is False
    finally:
        db.close()
        engine.dispose()


def test_starter_cod_reminder_remains_growth_locked():
    db, engine = _make_db()
    try:
        automation, event = _stored_cod_event(
            db,
            message_type="reminder",
            step_idx=1,
        )
        starter = SimpleNamespace(plan_slug="starter", has_feature=lambda _: False)
        sender = AsyncMock(return_value=(True, {}))

        with patch(
            "core.plan_entitlements.get_entitlements",
            return_value=starter,
        ) as get_entitlements, patch(
            "core.automation_engine._execute_action",
            new=sender,
        ):
            result = asyncio.run(
                _try_execute(db, 1, event, automation, event.created_at)
            )

        assert result == "skipped"
        get_entitlements.assert_called_once_with(db, 1)
        sender.assert_not_awaited()
        execution = db.query(AutomationExecution).one()
        assert execution.skip_reason == "plan_locked:autopilot_cod_confirmation:starter"
    finally:
        db.close()
        engine.dispose()
