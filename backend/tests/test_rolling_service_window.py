"""Rolling 24h service window — last inbound refresh and conservative bound."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.wa_usage import (  # noqa: E402
    _open_new_window,
    has_open_service_window,
)
from models import ConversationLog, WaConversationWindow  # noqa: E402


def _make_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    saved = []
    for model in (WaConversationWindow, ConversationLog):
        table = model.__table__
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
        table.create(engine, checkfirst=True)
    for col, orig in saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session()


class TestConservativeTwentyFourHourBound:
    def _window(self, *, start: datetime):
        db = _make_db()
        phone = "+966500111222"
        db.add(
            WaConversationWindow(
                tenant_id=20,
                customer_phone=phone,
                window_start=start,
                category="service",
            )
        )
        db.commit()
        return db, phone

    def test_23_59_59_inside(self):
        now = datetime(2026, 9, 5, 12, 0, 0)
        db, phone = self._window(start=now - timedelta(hours=23, minutes=59, seconds=59))
        assert has_open_service_window(db, 20, phone, now=now) is True

    def test_exact_24_00_00_outside(self):
        now = datetime(2026, 9, 5, 12, 0, 0)
        db, phone = self._window(start=now - timedelta(hours=24))
        assert has_open_service_window(db, 20, phone, now=now) is False

    def test_24_00_01_outside(self):
        now = datetime(2026, 9, 5, 12, 0, 0)
        db, phone = self._window(start=now - timedelta(hours=24, seconds=1))
        assert has_open_service_window(db, 20, phone, now=now) is False


class TestInboundRefreshesRollingWindow:
    def test_later_inbound_extends_window_without_new_billable(self):
        db = _make_db()
        phone = "+966500222333"
        t0 = datetime(2026, 9, 5, 8, 0, 0)
        db.add(
            WaConversationWindow(
                tenant_id=20,
                customer_phone=phone,
                window_start=t0,
                category="service",
            )
        )
        db.commit()
        t_later = t0 + timedelta(hours=10)
        billed = _open_new_window(db, 20, phone, "service", "inbound", t_later)
        assert billed is False
        row = db.query(WaConversationWindow).one()
        assert row.window_start == t_later
        still_open_at = t_later + timedelta(hours=23, minutes=59, seconds=59)
        assert has_open_service_window(db, 20, phone, now=still_open_at) is True
        closed_at = t_later + timedelta(hours=24)
        assert has_open_service_window(db, 20, phone, now=closed_at) is False
        assert db.query(ConversationLog).count() == 0

    def test_template_source_does_not_refresh_service_anchor(self):
        db = _make_db()
        phone = "+966500333444"
        t0 = datetime(2026, 9, 5, 8, 0, 0)
        db.add(
            WaConversationWindow(
                tenant_id=20,
                customer_phone=phone,
                window_start=t0,
                category="service",
            )
        )
        db.commit()
        billed = _open_new_window(
            db, 20, phone, "marketing", "template", t0 + timedelta(hours=1)
        )
        assert billed is False
        row = db.query(WaConversationWindow).one()
        assert row.window_start == t0
        assert row.category == "service"
