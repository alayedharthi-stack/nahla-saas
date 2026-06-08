"""
ARCH-015-FIX Tests First — order_flow short-circuit / promotion (N-19..N-24).
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from arch015_helpers import (  # noqa: E402
    brain_state_active_awaiting,
    brain_state_not_awaiting,
    build_metadata_after_payment_gate,
    normalize_pdf,
)
from core import order_flow  # noqa: E402
from modules.ai.media.semantic_classifier import MEDIA_PRODUCT_IMAGE  # noqa: E402

PDF_PRE_TRANSFER = """مراجعة بيانات التحويل
اسم المستفيد: محمد علي
الآيبان: SA0380000000608010167519
المبلغ: 358 ر.س
تأكد من البيانات واضغط تحويل"""

PDF_CONFIRMED = """البنك الراجحي
اسم المستفيد: أحمد محمد
الآيبان: SA0380000000608010167519
المبلغ: 358.00 ر.س
تم التحويل بنجاح
رقم العملية: TXN-9981234"""


def _patch_brain(monkeypatch, brain_state):
    monkeypatch.setattr(
        order_flow,
        "_load_brain_state",
        lambda db, *, tenant_id, phone: (object(), brain_state),
    )


def _production_pre_review_metadata(isolated_storage, monkeypatch) -> dict:
    return normalize_pdf(
        monkeypatch,
        pdf_text=PDF_PRE_TRANSFER,
        filename="Transfer-Receipt.pdf",
        isolated_storage=isolated_storage,
    )


class TestArch015OrderFlowSoftReply:
    """N-19, N-20"""

    def test_n19_soft_reply_fires_pre_review_not_awaiting(
        self, isolated_storage, monkeypatch,
    ):
        _patch_brain(monkeypatch, brain_state_not_awaiting())
        md = _production_pre_review_metadata(isolated_storage, monkeypatch)
        decision = order_flow.maybe_handle_payment_evidence_inbound(
            db=MagicMock(),
            tenant_id=11,
            phone="+966500000001",
            inbound_normalized_type="document",
            inbound_metadata=md,
        )
        assert decision is not None
        assert decision["state_patch"] == {}
        assert "الإيصال" in decision["reply_text"]

    def test_n20_soft_reply_fires_needs_confirmation_not_awaiting(
        self, monkeypatch,
    ):
        _patch_brain(monkeypatch, brain_state_not_awaiting())
        md = build_metadata_after_payment_gate(
            """البنك الراجحي
اسم المستفيد: أحمد
الآيبان: SA0380000000608010167519
المبلغ: 358 ر.س""",
            normalized_type="image",
        )
        decision = order_flow.maybe_handle_payment_evidence_inbound(
            db=MagicMock(),
            tenant_id=11,
            phone="+966500000001",
            inbound_normalized_type="image",
            inbound_metadata=md,
        )
        assert decision is not None
        assert decision["state_patch"] == {}
        assert "الإيصال" in decision["reply_text"]


class TestArch015OrderFlowPromotion:
    """N-21..N-23"""

    def test_n21_promotion_fires_awaiting_active_pre_review_pdf(
        self, isolated_storage, monkeypatch,
    ):
        _patch_brain(monkeypatch, brain_state_active_awaiting())
        md = _production_pre_review_metadata(isolated_storage, monkeypatch)
        decision = order_flow.maybe_handle_payment_evidence_inbound(
            db=MagicMock(),
            tenant_id=11,
            phone="+966500000001",
            inbound_normalized_type="document",
            inbound_metadata=md,
        )
        assert decision is not None
        sp = decision["state_patch"]
        assert sp.get("payment_receipt_received") is True
        assert sp.get("order_status") == "under_review"

    def test_n22_promotion_blocked_product_semantic_contradiction(
        self, monkeypatch,
    ):
        _patch_brain(monkeypatch, brain_state_active_awaiting())
        md = {
            "payment_evidence_status": "needs_confirmation",
            "pdf_kind": "payment_pending_evidence",
            "media_semantic_category": MEDIA_PRODUCT_IMAGE,
        }
        decision = order_flow.maybe_handle_payment_evidence_inbound(
            db=MagicMock(),
            tenant_id=11,
            phone="+966500000001",
            inbound_normalized_type="document",
            inbound_metadata=md,
        )
        assert decision is None

    def test_n23_promotion_blocked_religious_semantic_contradiction(
        self, monkeypatch,
    ):
        _patch_brain(monkeypatch, brain_state_active_awaiting())
        md = build_metadata_after_payment_gate(
            """البنك الراجحي
اسم المستفيد: أحمد
الآيبان: SA0380000000608010167519
المبلغ: 358 ر.س""",
            normalized_type="image",
            non_commerce_category="eid_greeting",
        )
        decision = order_flow.maybe_handle_payment_evidence_inbound(
            db=MagicMock(),
            tenant_id=11,
            phone="+966500000001",
            inbound_normalized_type="image",
            inbound_metadata=md,
        )
        assert decision is None


class TestArch015ReceiptAckRegression:
    """N-24"""

    def test_n24_receipt_ack_still_fires_confirmed_pdf(
        self, isolated_storage, monkeypatch,
    ):
        _patch_brain(monkeypatch, brain_state_active_awaiting())
        md = normalize_pdf(
            monkeypatch,
            pdf_text=PDF_CONFIRMED,
            filename="document_1778.pdf",
            isolated_storage=isolated_storage,
        )
        decision = order_flow.maybe_handle_receipt_inbound(
            db=MagicMock(),
            tenant_id=11,
            phone="+966500000001",
            inbound_normalized_type="document",
            inbound_metadata=md,
        )
        assert decision is not None
        assert decision["state_patch"].get("payment_receipt_received") is True
