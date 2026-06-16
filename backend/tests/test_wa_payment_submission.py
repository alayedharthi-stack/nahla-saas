"""PR-2 — WhatsApp payment submission linking tests."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.wa_order_lifecycle import (  # noqa: E402
    STATUS_PAID,
    STATUS_PAYMENT_SUBMITTED,
    STATUS_PENDING_PAYMENT,
    has_payment_submission,
    is_payment_verified,
    resolve_wa_order_status,
)
from core.wa_order_linking import (  # noqa: E402
    MSG_WA_PAYMENT_UNLINKED,
    find_linkable_wa_order,
    is_linkable_wa_order_status,
    is_terminal_wa_order_status,
)
from core.wa_payment_submission import (  # noqa: E402
    apply_wa_payment_submission,
    build_payment_submission_prep_patch,
)
from services.nahla_order_bridge import sync_nahla_wa_order, upsert_nahla_paid_order  # noqa: E402


def _conv(**kwargs):
    defaults = {
        "id": 9063,
        "tenant_id": 33,
        "customer_id": 1,
        "customer": SimpleNamespace(
            id=1, tenant_id=33, phone="966551308005", name="Customer", extra_metadata={},
        ),
        "extra_metadata": {},
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _order(**kwargs):
    defaults = {
        "id": 501,
        "tenant_id": 33,
        "external_id": "nahla-wa-33-9063",
        "status": STATUS_PENDING_PAYMENT,
        "source": "whatsapp",
        "customer_info": {"phone": "966551308005"},
        "extra_metadata": {"created_via": "nahla_order_bridge", "conversation_id": 9063},
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestPaymentSubmissionLifecycle:
    def test_text_submission_not_paid(self) -> None:
        prep = {
            "product_id": "p1",
            "customer_first_name": "A",
            "customer_last_name": "B",
            "city": "Riyadh",
            "google_maps_url": "https://maps.google.com/?q=1,2",
            "payment_submission_received": True,
            "payment_confirmed": False,
        }
        status, _, _ = resolve_wa_order_status(prep, {})
        assert status == STATUS_PAYMENT_SUBMITTED

    def test_receipt_without_confirm_not_paid(self) -> None:
        prep = {
            "product_id": "p1",
            "customer_first_name": "A",
            "customer_last_name": "B",
            "city": "Riyadh",
            "short_address_code": "RIYD1234",
            "payment_receipt_received": True,
            "payment_confirmed": False,
        }
        status, _, _ = resolve_wa_order_status(prep, {})
        assert status == STATUS_PAYMENT_SUBMITTED
        assert not is_payment_verified(prep)

    def test_confirmed_allows_paid_with_address(self) -> None:
        prep = {
            "product_id": "p1",
            "customer_first_name": "A",
            "customer_last_name": "B",
            "city": "Riyadh",
            "google_maps_url": "https://maps.google.com/?q=1,2",
            "payment_receipt_received": True,
            "payment_confirmed": True,
        }
        status, _, _ = resolve_wa_order_status(prep, {}, payment_verified=True)
        assert status == STATUS_PAID

    def test_confirmed_blocked_without_address(self) -> None:
        prep = {
            "product_id": "p1",
            "customer_first_name": "A",
            "customer_last_name": "B",
            "city": "Riyadh",
            "payment_receipt_received": True,
            "payment_confirmed": True,
        }
        status, missing, _ = resolve_wa_order_status(prep, {}, payment_verified=True)
        assert status == STATUS_PAYMENT_SUBMITTED
        assert "delivery_address" in missing

    def test_has_payment_submission_union(self) -> None:
        assert has_payment_submission({"payment_submission_received": True})
        assert has_payment_submission({"payment_receipt_received": True})
        assert not has_payment_submission({})


class TestOrderLinking:
    def test_linkable_statuses(self) -> None:
        assert is_linkable_wa_order_status(STATUS_PENDING_PAYMENT)
        assert is_linkable_wa_order_status(STATUS_PAYMENT_SUBMITTED)
        assert not is_linkable_wa_order_status(STATUS_PAID)
        assert is_terminal_wa_order_status(STATUS_PAID)
        assert is_terminal_wa_order_status("cancelled")

    def test_find_by_conversation_external_id(self) -> None:
        db = MagicMock()
        row = _order()
        db.query.return_value.filter_by.return_value.first.return_value = row
        found = find_linkable_wa_order(
            db, tenant_id=33, conversation=_conv(), phone_candidates=("966551308005",),
        )
        assert found is row

    def test_does_not_link_paid_order(self) -> None:
        db = MagicMock()
        paid = _order(status=STATUS_PAID)
        db.query.return_value.filter_by.return_value.first.return_value = paid
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [paid]
        found = find_linkable_wa_order(
            db, tenant_id=33, conversation=_conv(), phone_candidates=("966551308005",),
        )
        assert found is None


class TestBridgePaymentSubmitted:
    def _enable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAHLA_ORDER_DRAFT_BRIDGE_ENABLED", "1")

    def test_pending_payment_text_claim_becomes_submitted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._enable(monkeypatch)
        existing = _order(status=STATUS_PENDING_PAYMENT)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = existing

        result = sync_nahla_wa_order(
            db,
            tenant_id=33,
            conversation=_conv(),
            brain_state={"current_product_focus": {"title": "Honey", "price": "320", "id": 9}},
            order_prep={
                "product_id": "prod-99",
                "customer_first_name": "Ahmad",
                "customer_last_name": "Ali",
                "city": "Riyadh",
                "google_maps_url": "https://maps.google.com/?q=24.7,46.6",
                "payment_submission_received": True,
                "payment_submission_type": "text_claim",
                "payment_confirmed": False,
            },
            trigger="text_claim",
        )
        assert result is existing
        assert existing.status == STATUS_PAYMENT_SUBMITTED
        assert existing.extra_metadata["payment_confirmed"] is False
        assert existing.extra_metadata["payment_verification_status"] == "pending"
        assert existing.extra_metadata["payment_method"] == "bank_transfer"
        assert existing.extra_metadata["payment_status"] == "pending_verification"

    def test_receipt_does_not_auto_paid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._enable(monkeypatch)
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None

        class _Order:
            def __init__(self, **kwargs):
                for k, v in kwargs.items():
                    setattr(self, k, v)
                self.id = 900

        import models  # noqa: WPS433

        monkeypatch.setattr(models, "Order", _Order)
        monkeypatch.setattr(
            "services.nahla_order_bridge._allocate_nhl_number",
            lambda _db, _tid: "NHL-33-000099",
        )

        result = sync_nahla_wa_order(
            db,
            tenant_id=33,
            conversation=_conv(),
            brain_state={"current_product_focus": {"title": "Honey", "price": "320"}},
            order_prep={
                "product_id": "prod-99",
                "customer_first_name": "Ahmad",
                "customer_last_name": "Ali",
                "city": "Riyadh",
                "google_maps_url": "https://maps.google.com/?q=24.7,46.6",
                "payment_receipt_received": True,
                "payment_confirmed": False,
            },
            trigger="receipt",
        )
        assert result.status == STATUS_PAYMENT_SUBMITTED
        assert result.extra_metadata["payment_confirmed"] is False


class TestUnlinkedClaim:
    def test_no_order_returns_unlinked_message(self) -> None:
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        conv = _conv()
        with patch("core.order_flow.apply_state_patch") as mock_apply:
            result = apply_wa_payment_submission(
                db,
                tenant_id=33,
                phone="966551308005",
                submission_type="text_claim",
                conversation=conv,
            )
        assert result["linked"] is False
        assert MSG_WA_PAYMENT_UNLINKED in result["reply_text"]
        mock_apply.assert_not_called()


class TestPrepPatch:
    def test_text_claim_patch(self) -> None:
        patch = build_payment_submission_prep_patch(submission_type="text_claim")
        assert patch["payment_submission_received"] is True
        assert patch["payment_confirmed"] is False
        assert patch["order_status"] == "payment_submitted"
        assert patch.get("payment_submission_at")
        from datetime import datetime
        submitted_at = datetime.fromisoformat(patch["payment_submission_at"])
        assert submitted_at.tzinfo is not None

    def test_missing_fields_never_include_phone(self) -> None:
        from core.wa_order_lifecycle import compute_wa_missing_fields  # noqa: PLC0415

        missing = compute_wa_missing_fields(
            {"product_id": "1", "payment_submission_received": True},
            whatsapp_phone="966551308005",
        )
        assert "customer_phone" not in missing


class TestPaymentIntentIntegration:
    def test_text_claim_without_order_returns_unlinked_reply(self) -> None:
        from core.payment_intent import maybe_handle_payment_claim  # noqa: PLC0415

        conv = _conv()
        db = MagicMock()
        db.query.return_value.filter_by.return_value.first.return_value = None
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []

        with patch("core.order_flow._load_brain_state", return_value=(conv, {})):
            result = maybe_handle_payment_claim(
                db,
                tenant_id=33,
                phone="966551308005",
                inbound_text="تم الدفع",
                has_attached_media=False,
            )
        assert result is not None
        assert MSG_WA_PAYMENT_UNLINKED in result["reply_text"]


class TestUpsertPaidGuard:
    def test_upsert_requires_explicit_confirm(self) -> None:
        db = MagicMock()
        assert upsert_nahla_paid_order(
            db,
            tenant_id=33,
            conversation=_conv(),
            brain_state={},
            order_prep={"payment_receipt_received": True},
        ) is None
