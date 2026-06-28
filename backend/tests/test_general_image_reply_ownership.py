"""
PR-D6B — general image reply ownership + identity_collaboration OCR guard.
"""
from __future__ import annotations

import os
import sys
import types as _types
from typing import Any, List
from unittest.mock import MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_STORE_URL = "https://shop.example.sa"
_MAPS_URL = "https://maps.app.goo.gl/test-branch"

_LONG_SOCIAL_VISION = (
    "[وصف الصورة المرسلة] لقطة شاشة من منشور اجتماعي قصير تظهر مقطع "
    "Get ready with us skincare edition with creator handles and hashtags"
)

_PRODUCT_VISION = (
    "[وصف الصورة] عبوة منتج على رف خشبي مع بطاقة سعر."
)


def _brain_decide(message: str, *, intent_name: str = "general", intent_confidence: float = 0.55):
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.intent.rules import match
    from modules.ai.brain.types import (
        BrainContext,
        CommerceFacts,
        Intent,
        MerchantConversationState,
    )

    intent = match(message)
    if intent is None:
        intent = Intent(
            name=intent_name,
            confidence=intent_confidence,
            slots={},
            raw_message=message,
        )
    ctx = BrainContext(
        tenant_id=33,
        customer_phone="966500000000",
        message=message,
        intent=intent,
        state=MerchantConversationState(greeted=True),
        facts=CommerceFacts(
            has_products=True,
            orderable=True,
            store_url=_STORE_URL,
            maps_url=_MAPS_URL,
        ),
    )
    return DefaultDecisionEngine().decide(ctx)


class _Section:
    def __init__(self, *, body: str) -> None:
        self.id = 1
        self.kind = "custom"
        self.body = body
        self.title = ""
        self.metadata = {}
        self.metadata_json = {}
        self.updated_at = 1


class _StubDB:
    def __init__(self, sections: List[_Section]) -> None:
        self._sections = sections

    def query(self, _model: Any) -> "_Query":
        return _Query(self._sections)


class _Query:
    def __init__(self, sections: List[_Section]) -> None:
        self._sections = list(sections)

    def filter(self, *args: Any, **kwargs: Any) -> "_Query":
        return self

    def order_by(self, *args: Any, **kwargs: Any) -> "_Query":
        return self

    def limit(self, _n: int) -> "_Query":
        return self

    def all(self) -> List[_Section]:
        return self._sections

    def first(self) -> None:
        return None


def _install_call_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    call_stub = _types.ModuleType("services.call_resolver")

    class _CallTarget:
        def __init__(self, name: str, wa_id: str, phone_display: str, raw_phone: str) -> None:
            self.name = name
            self.wa_id = wa_id
            self.phone_display = phone_display
            self.raw_phone = raw_phone

    def _fake_normalize(phone: str) -> str:
        digits = "".join(c for c in phone if c.isdigit())
        if digits.startswith("966"):
            return digits
        if digits.startswith("0") and len(digits) >= 10:
            return "966" + digits[1:]
        if len(digits) == 9 and digits.startswith("5"):
            return "966" + digits
        return digits

    call_stub.CallTarget = _CallTarget  # type: ignore[attr-defined]
    call_stub._normalize_saudi_phone = _fake_normalize  # type: ignore[attr-defined]
    call_stub._pretty_phone = lambda w: w  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "services.call_resolver", call_stub)
    monkeypatch.setenv("STAFF_CONTACT_POLICY_ENABLED", "1")


class TestIdentityCollaborationMediaGuard:
    def test_vision_only_long_ocr_not_identity_collaboration(self) -> None:
        from modules.ai.brain.commerce.entity_extraction_guard import (
            is_identity_collaboration_without_purchase,
        )
        from modules.ai.brain.commerce.identity_collaboration_guard import (
            TOPIC_IDENTITY_COLLABORATION,
            try_identity_collaboration_decision,
        )

        assert is_identity_collaboration_without_purchase(_LONG_SOCIAL_VISION) is True
        ctx = MagicMock()
        ctx.message = _LONG_SOCIAL_VISION
        ctx.tenant_id = 33
        assert try_identity_collaboration_decision(ctx) is None

    def test_authored_identity_caption_still_triggers(self) -> None:
        from modules.ai.brain.commerce.identity_collaboration_guard import (
            try_identity_collaboration_decision,
        )

        ctx = MagicMock()
        ctx.message = "أنا معلم في النحل"
        ctx.tenant_id = 33
        decision = try_identity_collaboration_decision(ctx)
        assert decision is not None
        assert decision.args.get("topic") == "identity_collaboration"


class TestGeneralImageReplyOwnership:
    def test_vision_only_not_identity_collaboration_route(self) -> None:
        from modules.ai.brain.commerce.identity_collaboration_guard import (
            TOPIC_IDENTITY_COLLABORATION,
        )
        from modules.ai.brain.decision.actions import ACTION_LLM_REPLY

        decision = _brain_decide(_LONG_SOCIAL_VISION)
        assert not (
            decision.action == ACTION_LLM_REPLY
            and decision.args.get("topic") == TOPIC_IDENTITY_COLLABORATION
        )

    def test_vision_only_not_generic_refusal_goal(self) -> None:
        from modules.ai.brain.commerce.general_media_reply_guard import (
            TOPIC_IMAGE_ACK_OR_CLARIFY,
        )
        from modules.ai.brain.decision.actions import ACTION_LLM_REPLY

        decision = _brain_decide(_LONG_SOCIAL_VISION)
        goal = str(decision.args.get("response_goal") or "")
        assert "ما أقدر" not in goal
        assert "آسف، ما أقدر" not in goal
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_IMAGE_ACK_OR_CLARIFY

    def test_vision_only_routes_image_ack_or_clarify(self) -> None:
        from modules.ai.brain.commerce.general_media_reply_guard import (
            TOPIC_IMAGE_ACK_OR_CLARIFY,
        )
        from modules.ai.brain.decision.actions import ACTION_LLM_REPLY

        decision = _brain_decide(_LONG_SOCIAL_VISION)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_IMAGE_ACK_OR_CLARIFY
        assert decision.args.get("block_commerce_escalation") is True

    def test_social_screenshot_no_staff_storefront_or_identity(self) -> None:
        from modules.ai.brain.commerce.identity_collaboration_guard import (
            TOPIC_IDENTITY_COLLABORATION,
        )
        from modules.ai.brain.commerce.link_intent import (
            LinkIntentType,
            resolve_inbound_link_intent,
        )
        from modules.ai.brain.commerce.staff_contact_evidence import (
            classify_staff_contact_request,
            compile_staff_contact_registry,
        )
        from modules.ai.brain.execution.faq import TOPIC_STORE_INFO

        msg = _LONG_SOCIAL_VISION
        reg = compile_staff_contact_registry([
            _Section(body="موظف: 0501111111"),
        ])
        assert classify_staff_contact_request(msg, registry=reg).kind == "none"
        assert resolve_inbound_link_intent(msg) == LinkIntentType.UNKNOWN_LINK
        decision = _brain_decide(msg)
        assert decision.args.get("topic") != TOPIC_IDENTITY_COLLABORATION
        assert decision.args.get("topic") != TOPIC_STORE_INFO

    def test_image_caption_staff_contact_still_works(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.brain.commerce.staff_contact_policy import (
            evaluate_staff_contact_policy,
        )

        _install_call_resolver(monkeypatch)
        db = _StubDB([_Section(body="موظف: 0549815590")])
        msg = f"أبي رقم موظف\n\n{_LONG_SOCIAL_VISION}"
        decision = evaluate_staff_contact_policy(
            db, tenant_id=33, message=msg, customer_phone="966500000000",
        )
        assert decision is not None
        assert decision.deliver_contact is True

    def test_image_caption_storefront_still_works(self) -> None:
        from modules.ai.brain.decision.actions import ACTION_FAQ_REPLY
        from modules.ai.brain.execution.faq import TOPIC_STORE_INFO

        msg = f"رابط المتجر الإلكتروني\n\n{_LONG_SOCIAL_VISION}"
        decision = _brain_decide(msg)
        assert decision.action == ACTION_FAQ_REPLY
        assert decision.args.get("topic") == TOPIC_STORE_INFO

    def test_image_caption_product_inquiry_not_image_ack(self) -> None:
        from modules.ai.brain.commerce.general_media_reply_guard import (
            TOPIC_IMAGE_ACK_OR_CLARIFY,
        )
        from modules.ai.brain.commerce.product_media import detect_product_media_turn

        msg = f"وش هذا المنتج؟\n\n{_PRODUCT_VISION}"
        verdict = detect_product_media_turn(msg, intent_name="general")
        assert verdict.matched is True
        decision = _brain_decide(msg)
        assert decision.args.get("topic") != TOPIC_IMAGE_ACK_OR_CLARIFY


class TestD5D6ARegression:
    def test_d5_ocr_name_not_contact_target(self) -> None:
        from modules.ai.brain.commerce.staff_contact_evidence import (
            classify_staff_contact_request,
            compile_staff_contact_registry,
        )
        from modules.ai.brain.commerce.staff_contact_media_source_guard import (
            staff_contact_intent_message,
        )

        vision = (
            "[وصف الصورة المرسلة] screenshot with creator handle in visible text"
        )
        reg = compile_staff_contact_registry([
            _Section(body="موظف: 0501111111"),
        ])
        assert staff_contact_intent_message(vision) == ""
        assert classify_staff_contact_request(vision, registry=reg).kind == "none"

    def test_d6a_ocr_storefront_not_website_route(self) -> None:
        from modules.ai.brain.commerce.link_intent import (
            LinkIntentType,
            resolve_inbound_link_intent,
        )
        from modules.ai.brain.execution.faq import TOPIC_STORE_INFO

        msg = (
            "[وصف الصورة] محتوى يذكر رابط المتجر الإلكتروني والموقع الإلكتروني"
        )
        assert resolve_inbound_link_intent(msg) == LinkIntentType.UNKNOWN_LINK
        decision = _brain_decide(msg)
        assert decision.args.get("topic") != TOPIC_STORE_INFO
