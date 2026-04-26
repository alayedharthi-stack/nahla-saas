"""
tests/test_notification_throttle.py
──────────────────────────────────────
اختبارات منطق _should_notify_merchant_email:

1. عميل جديد (first_seen_at < 5 دقائق) → يُرسَل
2. عميل تواصل قبل 3 ساعات → لا يُرسَل (active conversation)
3. عميل عاد بعد 25 ساعة → يُرسَل (returning)
4. عميل عاد بعد 25 ساعة لكن أُرسل إشعار منذ ساعتين → لا يُرسَل (throttled)
5. لا يوجد عميل → لا يُرسَل
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ── path setup ────────────────────────────────────────────────────────────────
_BACKEND = Path(__file__).parent.parent / "backend"
_DB_DIR  = Path(__file__).parent.parent / "database"
for _p in (str(_BACKEND), str(_DB_DIR), str(_BACKEND.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# We import the helper directly from whatsapp_webhook; it's a module-level function.
from routers.whatsapp_webhook import _should_notify_merchant_email  # noqa: E402


def _customer(first_seen_at=None, last_interaction_at=None, cid=1):
    """Minimal Customer-like object."""
    return SimpleNamespace(
        id=cid,
        first_seen_at=first_seen_at,
        last_interaction_at=last_interaction_at,
    )


def _db_no_recent_notif():
    """Mock DB that returns no recent notification (no throttling)."""
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None
    return mock_db


def _db_with_recent_notif():
    """Mock DB that returns a recent notification (throttle applies)."""
    recent = SimpleNamespace(id=99)
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = recent
    return mock_db


NOW = datetime.now(timezone.utc)


class TestShouldNotifyMerchantEmail:

    def test_first_message_new_customer(self):
        """أول رسالة من عميل جديد (first_seen_at < 5 دقائق) → يُرسَل."""
        customer = _customer(
            first_seen_at=NOW - timedelta(minutes=2),
            last_interaction_at=NOW - timedelta(minutes=2),
        )
        result = _should_notify_merchant_email(
            db=_db_no_recent_notif(), tenant_id=1,
            customer=customer, silence_hours=24,
        )
        assert result["send"] is True
        assert result["reason"] == "first_message"

    def test_active_conversation_no_notify(self):
        """عميل تواصل منذ 3 ساعات فقط → لا يُرسَل (محادثة نشطة)."""
        customer = _customer(
            first_seen_at=NOW - timedelta(days=10),
            last_interaction_at=NOW - timedelta(hours=3),
        )
        result = _should_notify_merchant_email(
            db=_db_no_recent_notif(), tenant_id=1,
            customer=customer, silence_hours=24,
        )
        assert result["send"] is False
        assert result["reason"] == "active_conversation"

    def test_returning_customer_after_silence(self):
        """عميل عاد بعد 25 ساعة → يُرسَل."""
        customer = _customer(
            first_seen_at=NOW - timedelta(days=30),
            last_interaction_at=NOW - timedelta(hours=25),
        )
        result = _should_notify_merchant_email(
            db=_db_no_recent_notif(), tenant_id=1,
            customer=customer, silence_hours=24,
        )
        assert result["send"] is True
        assert result["reason"] == "returning_customer"

    def test_throttled_even_after_silence(self):
        """عميل عاد بعد 25 ساعة لكن أُرسل إشعار منذ ساعتين → لا يُرسَل (throttled)."""
        customer = _customer(
            first_seen_at=NOW - timedelta(days=30),
            last_interaction_at=NOW - timedelta(hours=25),
        )
        result = _should_notify_merchant_email(
            db=_db_with_recent_notif(), tenant_id=1,
            customer=customer, silence_hours=24,
        )
        assert result["send"] is False
        assert result["reason"] == "throttled"

    def test_no_customer_skip(self):
        """لا يوجد سجل عميل → لا يُرسَل."""
        result = _should_notify_merchant_email(
            db=_db_no_recent_notif(), tenant_id=1,
            customer=None, silence_hours=24,
        )
        assert result["send"] is False
        assert result["reason"] == "no_customer"

    def test_borderline_exactly_24h(self):
        """عميل تواصل منذ 24 ساعة بالضبط → يُرسَل (صمت كافٍ)."""
        customer = _customer(
            first_seen_at=NOW - timedelta(days=10),
            last_interaction_at=NOW - timedelta(hours=24),
        )
        result = _should_notify_merchant_email(
            db=_db_no_recent_notif(), tenant_id=1,
            customer=customer, silence_hours=24,
        )
        # timedelta(hours=24) >= timedelta(hours=24) → returning_customer
        assert result["send"] is True
        assert result["reason"] == "returning_customer"

    def test_borderline_just_over_24h(self):
        """عميل تواصل منذ 24 ساعة و1 دقيقة → يُرسَل."""
        customer = _customer(
            first_seen_at=NOW - timedelta(days=10),
            last_interaction_at=NOW - timedelta(hours=24, minutes=1),
        )
        result = _should_notify_merchant_email(
            db=_db_no_recent_notif(), tenant_id=1,
            customer=customer, silence_hours=24,
        )
        assert result["send"] is True
        assert result["reason"] == "returning_customer"
