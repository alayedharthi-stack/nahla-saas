"""
tests/test_outcome_tracking.py
────────────────────────────────
Unit tests for services/outcome_tracker.py

All tests are pure unit tests — no real DB, no HTTP.
The DB session is replaced with a mock that returns pre-built stubs.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch, call

import pytest

REPO_ROOT   = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for _p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── Minimal stubs ─────────────────────────────────────────────────────────────

def _trace_stub(
    id: int = 1,
    tenant_id: int = 10,
    customer_phone: str = "966501234567",
    order_started: bool = True,
    order_confirmed: Optional[bool] = False,
    coupon_redeemed: Optional[bool] = False,
) -> MagicMock:
    t = MagicMock()
    t.id              = id
    t.tenant_id       = tenant_id
    t.customer_phone  = customer_phone
    t.order_started   = order_started
    t.order_confirmed = order_confirmed
    t.coupon_redeemed = coupon_redeemed
    return t


def _db(trace: Optional[MagicMock] = None) -> MagicMock:
    """Build a mock DB where query().filter().order_by().first() returns trace."""
    db = MagicMock()
    q  = MagicMock()
    f  = MagicMock()
    ob = MagicMock()
    ob.first.return_value = trace
    f.order_by.return_value = ob
    q.filter.return_value = f
    db.query.return_value = q
    return db


def _order(
    status: str = "confirmed",
    phone: str = "966501234567",
    coupon: Optional[str] = None,
) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "id": "ORD-999",
        "status": status,
        "customer": {"mobile": phone},
    }
    if coupon:
        data["coupon"] = {"code": coupon}
    return data


# ── Import under test (patching DB and store_sync.normalize) ──────────────────

def _import_tracker():
    from services import outcome_tracker as ot
    return ot


# ─────────────────────────────────────────────────────────────────────────────
# mark_order_confirmed
# ─────────────────────────────────────────────────────────────────────────────

class TestMarkOrderConfirmed:

    def _patch_phone(self, return_value="966501234567"):
        return patch("services.outcome_tracker._normalize_phone", return_value=return_value)

    def test_confirmed_order_sets_flag(self):
        trace = _trace_stub(order_confirmed=False)
        db = _db(trace=trace)
        ot = _import_tracker()

        with self._patch_phone():
            result = ot.mark_order_confirmed(db, tenant_id=10, order_data=_order())

        assert result is True
        assert trace.order_confirmed is True
        db.commit.assert_called_once()

    def test_no_phone_returns_false(self):
        db = _db(trace=None)
        ot = _import_tracker()
        order = {"id": "123", "status": "confirmed", "customer": {}}

        with self._patch_phone(return_value=""):
            result = ot.mark_order_confirmed(db, tenant_id=10, order_data=order)

        assert result is False
        db.commit.assert_not_called()

    def test_no_matching_trace_returns_false(self):
        db = _db(trace=None)
        ot = _import_tracker()

        with self._patch_phone():
            result = ot.mark_order_confirmed(db, tenant_id=10, order_data=_order())

        assert result is False
        db.commit.assert_not_called()

    def test_already_confirmed_not_updated(self):
        db = _db(trace=None)
        ot = _import_tracker()

        with self._patch_phone():
            result = ot.mark_order_confirmed(db, tenant_id=10, order_data=_order())

        assert result is False

    def test_db_error_returns_false_and_does_not_raise(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("DB offline")
        ot = _import_tracker()

        with self._patch_phone():
            result = ot.mark_order_confirmed(db, tenant_id=10, order_data=_order())

        assert result is False

    def test_phone_extracted_from_customer_mobile(self):
        trace = _trace_stub()
        db = _db(trace=trace)
        ot = _import_tracker()
        order = {"id": "X", "status": "confirmed", "customer": {"mobile": "0501234567"}}

        captured = []
        def fake_normalize(raw):
            captured.append(raw)
            return "966501234567"

        with patch("services.outcome_tracker._normalize_phone", side_effect=fake_normalize):
            ot.mark_order_confirmed(db, tenant_id=10, order_data=order)

        assert "0501234567" in captured

    def test_phone_fallback_to_order_customer_phone_key(self):
        trace = _trace_stub()
        db = _db(trace=trace)
        ot = _import_tracker()
        order = {"id": "X", "status": "confirmed", "customer_phone": "0509999999"}

        captured = []
        def fake_normalize(raw):
            captured.append(raw)
            return "966509999999"

        with patch("services.outcome_tracker._normalize_phone", side_effect=fake_normalize):
            ot.mark_order_confirmed(db, tenant_id=10, order_data=order)

        assert "0509999999" in captured


# ─────────────────────────────────────────────────────────────────────────────
# mark_coupon_redeemed
# ─────────────────────────────────────────────────────────────────────────────

class TestMarkCouponRedeemed:

    def _patch_phone(self, return_value="966501234567"):
        return patch("services.outcome_tracker._normalize_phone", return_value=return_value)

    def test_sets_coupon_redeemed_when_coupon_present(self):
        trace = _trace_stub(coupon_redeemed=False)
        db = _db(trace=trace)
        ot = _import_tracker()

        with self._patch_phone():
            result = ot.mark_coupon_redeemed(db, tenant_id=10, order_data=_order(coupon="SAVE20"))

        assert result is True
        assert trace.coupon_redeemed is True

    def test_returns_false_when_no_coupon_in_order(self):
        db = _db()
        ot = _import_tracker()

        with self._patch_phone():
            result = ot.mark_coupon_redeemed(db, tenant_id=10, order_data=_order())

        assert result is False
        db.commit.assert_not_called()

    def test_coupon_via_discount_code_key(self):
        trace = _trace_stub(coupon_redeemed=False)
        db = _db(trace=trace)
        ot = _import_tracker()
        order = _order()
        order["discount_code"] = "DISC10"

        with self._patch_phone():
            result = ot.mark_coupon_redeemed(db, tenant_id=10, order_data=order)

        assert result is True


# ─────────────────────────────────────────────────────────────────────────────
# record_order_outcome
# ─────────────────────────────────────────────────────────────────────────────

class TestRecordOrderOutcome:

    def test_confirmed_status_calls_mark_order_confirmed(self):
        ot = _import_tracker()
        db = MagicMock()

        with patch.object(ot, "mark_order_confirmed", return_value=True) as mock_conf:
            with patch.object(ot, "mark_coupon_redeemed", return_value=False):
                ot.record_order_outcome(db, tenant_id=10, order_data=_order(status="confirmed"))

        mock_conf.assert_called_once()

    def test_non_confirmed_status_does_not_call_mark_order_confirmed(self):
        ot = _import_tracker()
        db = MagicMock()

        with patch.object(ot, "mark_order_confirmed", return_value=False) as mock_conf:
            ot.record_order_outcome(db, tenant_id=10, order_data=_order(status="pending"))

        mock_conf.assert_not_called()

    @pytest.mark.parametrize("status", ["confirmed", "paid", "completed", "delivered", "in_progress"])
    def test_all_confirmed_statuses_trigger_tracking(self, status: str):
        ot = _import_tracker()
        db = MagicMock()

        with patch.object(ot, "mark_order_confirmed", return_value=True) as mock_conf:
            with patch.object(ot, "mark_coupon_redeemed", return_value=False):
                ot.record_order_outcome(db, tenant_id=10, order_data=_order(status=status))

        mock_conf.assert_called_once()

    def test_never_raises_even_on_exception(self):
        ot = _import_tracker()
        db = MagicMock()

        with patch.object(ot, "mark_order_confirmed", side_effect=RuntimeError("boom")):
            # Should not raise
            ot.record_order_outcome(db, tenant_id=10, order_data=_order(status="confirmed"))
