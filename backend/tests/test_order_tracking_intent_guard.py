"""Regression tests for order-tracking three-layer guard (Phase 2)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: E402
    boost_track_order_intent,
    is_order_tracking_follow_up,
    is_pre_order_shipping_inquiry,
    resolve_order_tracking_guard_reply,
)
from modules.ai.brain.commerce.product_label_hygiene import is_non_product_label  # noqa: E402
from modules.ai.brain.compose import templates as T  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.postprocess.availability_guard_policy import (  # noqa: E402
    inbound_exempt_from_availability_rewrite,
)
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    apply_product_availability_truth_guard,
)
from modules.ai.brain.postprocess.staff_escalation_truth_guard import (  # noqa: E402
    SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR,
    apply_staff_escalation_truth_guard,
)
from modules.ai.brain.types import INTENT_ASK_PRODUCT, INTENT_TRACK_ORDER  # noqa: E402


TRACKING_MESSAGES = [
    "متى يوصل الطلب",
    "وين طلبي",
    "طلبت طلب وابي اعرف متى يوصل لي",
    "الشحنة وينها",
    "رقم التتبع",
    "حالة الطلب",
]

NON_TRACKING_MESSAGES = [
    "متى يوصل عسل الطلح إذا طلبته؟",
    "أبي عسل طلح",
    "وش الخيارات؟",
]


class TestOrderTrackingDetection:
    @pytest.mark.parametrize("message", TRACKING_MESSAGES)
    def test_existing_order_follow_up_detected(self, message: str) -> None:
        assert is_order_tracking_follow_up(message) is True
        assert is_pre_order_shipping_inquiry(message) is False

    @pytest.mark.parametrize("message", NON_TRACKING_MESSAGES)
    def test_browse_and_preorder_not_tracking(self, message: str) -> None:
        assert is_order_tracking_follow_up(message) is False

    def test_preorder_shipping_flagged(self) -> None:
        msg = "متى يوصل عسل الطلح إذا طلبته؟"
        assert is_pre_order_shipping_inquiry(msg) is True
        assert is_order_tracking_follow_up(msg) is False


class TestLayer1IntentGuard:
    @pytest.mark.parametrize("message", TRACKING_MESSAGES)
    def test_rules_match_track_order(self, message: str) -> None:
        intent = rules.match(message)
        assert intent is not None
        assert intent.name == INTENT_TRACK_ORDER
        assert intent.confidence >= 0.96

    @pytest.mark.parametrize("message", TRACKING_MESSAGES)
    def test_boost_returns_track_order(self, message: str) -> None:
        boosted = boost_track_order_intent(message)
        assert boosted is not None
        assert boosted.name == INTENT_TRACK_ORDER
        assert boosted.extraction_method == "order_tracking_guard"

    def test_preorder_not_boosted_to_track(self) -> None:
        msg = "متى يوصل عسل الطلح إذا طلبته؟"
        assert boost_track_order_intent(msg) is None
        intent = rules.match(msg)
        assert intent is None or intent.name != INTENT_TRACK_ORDER

    def test_browse_not_boosted_to_track(self) -> None:
        for msg in ("أبي عسل طلح", "وش الخيارات؟"):
            assert boost_track_order_intent(msg) is None

    def test_boost_overrides_wrong_ask_product(self) -> None:
        wrong = rules.match("طلبت طلب وابي اعرف متى يوصل لي")
        assert wrong is not None
        assert wrong.name == INTENT_TRACK_ORDER

    def test_shipping_duration_no_longer_steals_track_phrase(self) -> None:
        intent = rules.match("متى يوصل الطلب")
        assert intent is not None
        assert intent.name == INTENT_TRACK_ORDER
        assert intent.name != "ask_shipping"


class TestLayer2AvailabilityRewriteGuard:
    @pytest.mark.parametrize("message", TRACKING_MESSAGES)
    def test_inbound_exempt_from_availability_rewrite(self, message: str) -> None:
        assert inbound_exempt_from_availability_rewrite(message) is True

    @pytest.mark.parametrize("message", NON_TRACKING_MESSAGES)
    def test_browse_not_exempt_from_availability(self, message: str) -> None:
        assert inbound_exempt_from_availability_rewrite(message) is False

    @pytest.mark.parametrize("message", TRACKING_MESSAGES)
    def test_tracking_phrase_not_product_label(self, message: str) -> None:
        assert is_non_product_label(message) is True

    def test_tracking_reply_not_rewritten_in_enforce_mode(self) -> None:
        prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"
        try:
            bad_reply = "متوفر متى يوصل الطلب بعدة خيارات 🛒 / وش نوع تبيه؟"
            result = apply_product_availability_truth_guard(
                reply=bad_reply,
                availability_context={
                    "catalog_skus": [],
                    "focus_product": None,
                    "recommended_product_ids": [],
                    "kb_signals": [],
                    "kb_links": [],
                },
                inbound_text="متى يوصل الطلب",
                tenant_id=1,
            )
            assert result.replaced is False
            assert result.reply == bad_reply
            assert "بعدة خيارات" not in result.reply or result.reply == bad_reply
        finally:
            if prev is None:
                os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
            else:
                os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = prev


class TestLayer3StaffEscalationStubGuard:
    def test_tracking_follow_up_gets_identifiers_not_stub(self) -> None:
        llm_reply = "تم تحويلك لفريق الدعم، راح يتواصلون معك قريباً 🌷"
        result = apply_staff_escalation_truth_guard(
            reply=llm_reply,
            inbound_text="طلبت طلب وابي اعرف متى يوصل لي",
            conversation_flags={},
        )
        assert result.replaced is True
        assert result.reply != SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR
        assert "رقم الطلب" in result.reply or "الجوال" in result.reply

    def test_non_tracking_still_gets_generic_stub(self) -> None:
        result = apply_staff_escalation_truth_guard(
            reply="تم تحويلك للدعم",
            inbound_text="أبي عسل طلح",
            conversation_flags={},
        )
        assert result.replaced is True
        assert result.reply == SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR

    def test_resolve_guard_reply_asks_identifiers_when_no_evidence(self) -> None:
        reply = resolve_order_tracking_guard_reply(state=None, history=[])
        assert reply == T.track_order_need_identifiers()
        assert "رقم الطلب" in reply
        assert "الجوال" in reply
