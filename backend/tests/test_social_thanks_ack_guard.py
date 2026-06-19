"""Social thanks/closing must not receive generic receipt ACK stubs."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.observability.order_flow_evidence import (  # noqa: E402
    _INPUT_TYPE_SOCIAL_THANKS,
    detect_input_types,
    is_generic_ack_stub,
)
from modules.ai.brain.postprocess.staff_escalation_truth_guard import (  # noqa: E402
    SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR,
    apply_staff_escalation_truth_guard,
)
from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: E402
    resolve_social_thanks_guard_reply,
    should_suppress_generic_stub_injection,
)


def _order_state_with_cart() -> SimpleNamespace:
    return SimpleNamespace(
        order_prep=SimpleNamespace(
            cart_items=[{"product_name": "عسل طلح", "quantity": 1}],
            line_items=[{"product_name": "عسل طلح", "quantity": 1}],
            missing_fields=["customer_first_name"],
            order_status="awaiting_address",
        ),
        awaiting_option_confirmation=False,
        last_question_asked="",
    )


class TestSocialThanksStubSuppression:
    @pytest.mark.parametrize(
        "message",
        (
            "الله يعطيك العافية ومشكور",
            "جزاك الله خير",
            "بيض الله وجهك",
            "يعطيكم العافية",
        ),
    )
    def test_should_suppress_generic_stub(self, message: str) -> None:
        assert should_suppress_generic_stub_injection(inbound_text=message) is True

    @pytest.mark.parametrize(
        "message,fragment",
        (
            ("الله يعطيك العافية ومشكور", "الله يعافيك"),
            ("جزاك الله خير", "وإياك"),
            ("بيض الله وجهك", "وجهك"),
        ),
    )
    def test_resolve_social_mirror(self, message: str, fragment: str) -> None:
        reply = resolve_social_thanks_guard_reply(message)
        assert reply
        assert SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR not in reply
        assert fragment in reply

    def test_detect_input_types_social_thanks(self) -> None:
        types = detect_input_types(message="الله يعطيك العافية ومشكور")
        assert _INPUT_TYPE_SOCIAL_THANKS in types


class TestStaffEscalationGuardSocialThanks:
    _FALSE_ESCALATION_LLM = "سيتواصل معك الفريق قريباً"

    @pytest.mark.parametrize(
        "message,fragment",
        (
            ("الله يعطيك العافية ومشكور", "الله يعافيك"),
            ("جزاك الله خير", "وإياك"),
            ("بيض الله وجهك", "وجهك"),
        ),
    )
    def test_false_escalation_returns_social_not_generic_ack(
        self,
        message: str,
        fragment: str,
    ) -> None:
        result = apply_staff_escalation_truth_guard(
            reply=self._FALSE_ESCALATION_LLM,
            inbound_text=message,
            conversation_flags={},
        )
        assert result.replaced is True
        assert result.reply != SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR
        assert "وصلت رسالتك" not in result.reply
        assert fragment in result.reply
        assert result.action.endswith("social_thanks")

    def test_active_order_commerce_unchanged(self) -> None:
        result = apply_staff_escalation_truth_guard(
            reply="تم تحويلك لفريق الدعم، أكمل الطلب الآن",
            inbound_text="نعم",
            state=_order_state_with_cart(),
            conversation_flags={},
        )
        assert result.reply != SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR
        assert "وصلت رسالتك" not in result.reply
        assert "تحويل" not in result.reply

    def test_production_heba_case(self) -> None:
        msg = "الله يعطيك العافية ومشكور"
        result = apply_staff_escalation_truth_guard(
            reply=self._FALSE_ESCALATION_LLM,
            inbound_text=msg,
            conversation_flags={},
        )
        assert not is_generic_ack_stub(result.reply)
        assert "الله يعافيك" in result.reply
