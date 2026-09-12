"""Regression tests for canonical Order Updates enablement in the event engine."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Tuple
from unittest.mock import AsyncMock, patch

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core.automation_engine import (  # noqa: E402
    _is_automation_effectively_enabled,
    _process_event,
)
from core.commerce_lifecycle.order_updates import (  # noqa: E402
    set_order_update_flags,
    sync_order_confirmation_automation,
)
from models import AutomationEvent, SmartAutomation, TenantSettings  # noqa: E402


def _make_db(*models) -> Tuple[Any, Any]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    saved = []
    for model in models:
        table = model.__table__
        for column in table.columns:
            if isinstance(column.type, JSONB):
                saved.append((column, column.type))
                column.type = JSON()
        table.create(engine, checkfirst=True)
    for column, original in saved:
        column.type = original
    return sessionmaker(bind=engine)(), engine


def _automation(*, enabled: bool) -> SmartAutomation:
    return SmartAutomation(
        tenant_id=9,
        automation_type="order_notifications",
        engine="recovery",
        trigger_event="order_notifications",
        name="إشعارات الطلبات",
        enabled=enabled,
        config={},
    )


def test_runtime_uses_canonical_on_even_when_legacy_row_is_off():
    db, _ = _make_db(TenantSettings)
    set_order_update_flags(
        db,
        9,
        {"order_confirmation": True},
        master_enabled=True,
        commit=True,
    )
    automation = _automation(enabled=False)

    assert _is_automation_effectively_enabled(db, 9, automation) is True
    assert automation.enabled is True


def test_runtime_fails_closed_when_canonical_setting_is_off():
    db, _ = _make_db(TenantSettings)
    set_order_update_flags(
        db,
        9,
        {"order_confirmation": True},
        master_enabled=False,
        commit=True,
    )
    automation = _automation(enabled=True)

    assert _is_automation_effectively_enabled(db, 9, automation) is False
    assert automation.enabled is False


def test_settings_write_synchronizes_legacy_automation_row():
    db, _ = _make_db(TenantSettings, SmartAutomation)
    automation = _automation(enabled=False)
    db.add(automation)
    db.commit()
    set_order_update_flags(
        db,
        9,
        {"order_confirmation": True},
        master_enabled=True,
        commit=False,
    )

    assert sync_order_confirmation_automation(db, 9, commit=True) is True
    assert automation.enabled is True


def test_process_event_executes_when_canonical_on_and_legacy_row_off():
    db, _ = _make_db(TenantSettings, SmartAutomation, AutomationEvent)
    set_order_update_flags(
        db,
        9,
        {"order_confirmation": True},
        master_enabled=True,
        commit=True,
    )
    automation = _automation(enabled=False)
    event = AutomationEvent(
        tenant_id=9,
        event_type="order_notifications",
        customer_id=65,
        payload={"order_id": 141},
        processed=False,
    )
    db.add_all([automation, event])
    db.commit()

    with patch("core.automation_engine._try_execute", new=AsyncMock(return_value="sent")) as execute:
        sent = asyncio.run(
            _process_event(db, 9, event, datetime.now(timezone.utc))
        )

    assert sent == 1
    assert event.processed is True
    assert event.automation_id == automation.id
    assert automation.enabled is True
    execute.assert_awaited_once()
