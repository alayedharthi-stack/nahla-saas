"""Regression — customer ledger replies must not trip asset-promise phone scrubber."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.customer_commerce_answerer import (  # noqa: E402
    render_latest_order_summary_reply,
    render_order_history_count_reply,
)
from core.customer_commerce_ledger import (  # noqa: E402
    CustomerCommerceProfile,
    CustomerIdentity,
    EvidenceQuality,
    OrderCounts,
)
from core.local_order_resolver import LocalOrderSnapshot  # noqa: E402
from core.outbound_sanitizer import (  # noqa: E402
    ASSET_PHONE,
    contains_promised_asset,
    maybe_scrub_unkept_asset_promise,
)
from modules.ai.brain.decision.actions import ACTION_CUSTOMER_LEDGER_REPLY  # noqa: E402


def _profile_with_orders(*, total: int = 6, ref: str = "269866315") -> CustomerCommerceProfile:
    snap = LocalOrderSnapshot(
        order_id=95,
        external_id=ref,
        external_order_number=ref,
        status="pending_payment",
        source="salla",
        total="741.00",
        customer_name="أحمد سالم",
        line_items=[],
        tracking_number="",
    )
    return CustomerCommerceProfile(
        customer_identity=CustomerIdentity(customer_id=1, phone="966555906901"),
        order_counts=OrderCounts(total_orders=total, open_orders=1),
        latest_order=snap,
        latest_open_order=snap,
        evidence_quality=EvidenceQuality(),
    )


@pytest.mark.parametrize(
    "text",
    [
        "عندك 6 طلبات مسجلة عندنا على هذا الرقم. آخر طلب رقم 269866315 وحالته قيد إكمال الدفع.",
        "ما ظهر لي طلبات مسجلة على هذا الرقم.",
    ],
)
def test_customer_ledger_replies_not_classified_as_phone_promise(text: str) -> None:
    assert contains_promised_asset(text) is None


def test_customer_ledger_history_from_answerer_unchanged_by_scrubber() -> None:
    profile = _profile_with_orders()
    text = render_order_history_count_reply(profile)
    out, scrubbed, asset = maybe_scrub_unkept_asset_promise(
        text,
        has_url=False,
        has_media=False,
        has_phone=False,
        has_product_card=False,
    )
    assert scrubbed is False
    assert asset is None
    assert out == text
    assert "لا يوجد رقم تواصل" not in out


def test_customer_ledger_no_orders_from_answerer_unchanged_by_scrubber() -> None:
    profile = CustomerCommerceProfile(
        customer_identity=CustomerIdentity(phone="966500009429"),
        order_counts=OrderCounts(),
        evidence_quality=EvidenceQuality(),
    )
    text = render_order_history_count_reply(profile)
    assert text == "ما ظهر لي طلبات مسجلة على هذا الرقم."
    out, scrubbed, asset = maybe_scrub_unkept_asset_promise(
        text,
        has_url=False,
        has_media=False,
        has_phone=False,
    )
    assert scrubbed is False
    assert out == text


def test_customer_ledger_latest_summary_unchanged_by_scrubber() -> None:
    profile = _profile_with_orders()
    text = render_latest_order_summary_reply(profile)
    out, scrubbed, _ = maybe_scrub_unkept_asset_promise(
        text,
        has_url=False,
        has_media=False,
        has_phone=False,
    )
    assert scrubbed is False
    assert out == text


def test_staff_contact_phone_promise_still_scrubbed() -> None:
    text = "تفضل رقم أبو هشام، يخدمك بالتفصيل."
    assert contains_promised_asset(text) == ASSET_PHONE
    out, scrubbed, asset = maybe_scrub_unkept_asset_promise(
        text,
        has_url=False,
        has_media=False,
        has_phone=False,
    )
    assert scrubbed is True
    assert asset == ASSET_PHONE
    assert "تفضل رقم أبو هشام" not in out
    assert "لا يوجد رقم تواصل" in out


def test_track_order_reply_unaffected() -> None:
    text = (
        "حالة رقم الطلب 269866315: *قيد إكمال الدفع*\n"
        "الإجمالي: 741.00 SAR\n\n"
        "إذا تريد أساعدك في شيء آخر بخصوص الطلب أنا حاضر."
    )
    out, scrubbed, asset = maybe_scrub_unkept_asset_promise(
        text,
        has_url=False,
        has_media=False,
        has_phone=False,
    )
    assert scrubbed is False
    assert asset is None
    assert out == text


def test_catalog_product_option_reply_unaffected() -> None:
    text = "اختر رقم الخيار أو اسم المنتج وأكمل معك."
    out, scrubbed, asset = maybe_scrub_unkept_asset_promise(
        text,
        has_url=False,
        has_media=False,
        has_phone=False,
    )
    assert scrubbed is False
    assert asset is None
    assert out == text


def test_skip_asset_promise_scrub_flag_bypasses_scrubber() -> None:
    text = "تفضل رقم أبو هشام"
    out, scrubbed, asset = maybe_scrub_unkept_asset_promise(
        text,
        has_url=False,
        has_media=False,
        has_phone=False,
        skip_asset_promise_scrub=True,
    )
    assert scrubbed is False
    assert asset is None
    assert out == text


def test_customer_ledger_action_constant_matches_webhook_gate() -> None:
    assert ACTION_CUSTOMER_LEDGER_REPLY == "customer_ledger_reply"
