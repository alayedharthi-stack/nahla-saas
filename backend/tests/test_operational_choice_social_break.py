"""
PR-D6C — media/social turns break stale operational choice replay.
"""
from __future__ import annotations

import os
import sys
import types as _types
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.branch_trigger_router import (  # noqa: E402
    MSG_PICKUP_PREFERENCE_ASK,
    evaluate_branch_trigger_routing,
)
from modules.ai.brain.commerce.pending_operational_choice import (  # noqa: E402
    PENDING_PICKUP_MAPS_OR_CONTACT,
)
from modules.ai.brain.intent.non_commerce_classifier import (  # noqa: E402
    NC_EID_GREETING,
    NON_COMMERCE_IMAGE_TAG,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY, ACTION_SOCIAL_REPLY  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    INTENT_GENERAL,
    INTENT_SOCIAL,
)

_STORE_URL = "https://shop.example.sa"
_MAPS_URL = "https://maps.app.goo.gl/test-branch"

_EID_VISION = (
    f"{NON_COMMERCE_IMAGE_TAG}\n"
    "[وصف الصورة المرسلة] تصميم تهنئة بمناسبة عيد الأضحى المبارك. "
    "كل عام وأنتم بخير. تقبل الله طاعتكم."
)

_SOCIAL_VISION = (
    "[وصف الصورة المر_sent] لقطة شاشة من منشور اجتماعي قصير skincare edition"
)


class _BranchRow:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _StructuredQuery:
    def __init__(self, rows: List[Any]) -> None:
        self._rows = list(rows)

    def filter(self, *args: Any, **kwargs: Any) -> "_StructuredQuery":
        return self

    def order_by(self, *args: Any, **kwargs: Any) -> "_StructuredQuery":
        return self

    def first(self) -> Optional[Any]:
        return self._rows[0] if self._rows else None

    def all(self) -> List[Any]:
        return list(self._rows)


class _StructuredDB:
    def __init__(self, *, branches: Optional[List[Any]] = None) -> None:
        self.branches = branches or []

    def query(self, model: Any) -> _StructuredQuery:
        name = getattr(model, "__name__", str(model))
        if name == "MerchantBranch":
            return _StructuredQuery(self.branches)
        if name in {"BranchContact", "BranchEscalationStep", "BranchArrivalKeyword"}:
            return _StructuredQuery([])
        return _StructuredQuery([])

    def add(self, obj: Any) -> None:
        pass

    def flush(self) -> None:
        pass


def _branch(**kwargs: Any) -> _BranchRow:
    defaults = dict(
        id=1,
        tenant_id=33,
        name="المعرض",
        city="",
        district="",
        address="",
        maps_url=_MAPS_URL,
        sort_order=0,
        is_active=True,
        location_response_mode="location_plus_reception",
        arrival_response_mode="reception_only",
        location_instructions_text="",
    )
    defaults.update(kwargs)
    return _BranchRow(**defaults)


def _brain_meta(*, block: bool = True) -> Dict[str, Any]:
    meta: Dict[str, Any] = {
        "brain_state": {
            "order_prep": {
                "pending_operational_choice": PENDING_PICKUP_MAPS_OR_CONTACT,
                "pending_operational_branch_id": 1,
            },
        },
    }
    return meta


def _brain_decide(
    message: str,
    *,
    intent_name: str = INTENT_GENERAL,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    block_commerce: bool = False,
):
    intent = Intent(
        name=intent_name,
        confidence=0.55,
        slots={"block_commerce_escalation": True, "social_category": NC_EID_GREETING}
        if block_commerce
        else {},
        raw_message=message,
    )
    ctx = BrainContext(
        tenant_id=33,
        customer_phone="966500000000",
        message=message,
        intent=intent,
        state=MerchantConversationState(
            greeted=True,
            last_question_asked=MSG_PICKUP_PREFERENCE_ASK,
            last_question_answered=False,
        ),
        facts=CommerceFacts(
            has_products=True,
            orderable=True,
            store_url=_STORE_URL,
            maps_url=_MAPS_URL,
        ),
        block_commerce_escalation=block_commerce,
        profile={"inbound_metadata": inbound_metadata or {}},
    )
    return DefaultDecisionEngine().decide(ctx)


class TestOperationalChoiceSocialBreak:
    @patch("core.order_flow._load_brain_state")
    def test_eid_image_with_stale_pending_no_branch_pickup_ask(
        self,
        mock_brain: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("USE_STRUCTURED_BRANCH_CONTACTS", "1")
        conv = MagicMock()
        conv.extra_metadata = _brain_meta()
        mock_brain.return_value = (conv, conv.extra_metadata["brain_state"])

        db = _StructuredDB(branches=[_branch()])
        decision = evaluate_branch_trigger_routing(
            db,
            tenant_id=33,
            message=_EID_VISION,
            customer_phone="966500000000",
            inbound_metadata={"source_type": "image", "caption": ""},
        )
        assert decision is None

    @patch("core.order_flow._load_brain_state")
    def test_social_vision_brain_routes_image_ack_not_pickup(
        self,
        mock_brain: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("USE_STRUCTURED_BRANCH_CONTACTS", "1")
        conv = MagicMock()
        conv.extra_metadata = _brain_meta()
        mock_brain.return_value = (conv, conv.extra_metadata["brain_state"])

        decision = _brain_decide(_SOCIAL_VISION)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "image_ack_or_clarify"
        assert MSG_PICKUP_PREFERENCE_ASK not in str(decision.args.get("response_goal") or "")

    def test_greeting_only_with_stale_last_question_routes_social(self) -> None:
        decision = _brain_decide("مرحبا")
        assert decision.action in {ACTION_SOCIAL_REPLY, ACTION_LLM_REPLY}
        assert decision.args.get("topic") != "execute_pending_offer"

    def test_dua_with_stale_last_question_routes_social(self) -> None:
        decision = _brain_decide("تقبل الله طاعتكم")
        assert decision.action in {ACTION_SOCIAL_REPLY, ACTION_LLM_REPLY}

    @patch("core.order_flow._load_brain_state")
    def test_explicit_send_location_still_routes(
        self,
        mock_brain: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("USE_STRUCTURED_BRANCH_CONTACTS", "1")
        conv = MagicMock()
        conv.extra_metadata = _brain_meta()
        mock_brain.return_value = (conv, conv.extra_metadata["brain_state"])

        db = _StructuredDB(branches=[_branch()])
        decision = evaluate_branch_trigger_routing(
            db,
            tenant_id=33,
            message="أرسل الموقع",
            customer_phone="966500000000",
        )
        assert decision is not None
        assert _MAPS_URL in (decision.maps_url or "") or MSG_PICKUP_PREFERENCE_ASK in (
            decision.reply_text or ""
        )

    def test_explicit_contact_data_not_blocked_by_social_break(self) -> None:
        from modules.ai.brain.commerce.operational_choice_turn_guard import (
            has_explicit_operational_intent,
            should_break_stale_operational_choice,
        )

        msg = "أرسل بيانات التواصل"
        assert has_explicit_operational_intent(msg) is True
        assert should_break_stale_operational_choice(msg) is False
        decision = _brain_decide(msg)
        assert decision.args.get("topic") != "image_ack_or_clarify"

    @patch("core.order_flow._load_brain_state")
    def test_image_caption_send_location_still_routes(
        self,
        mock_brain: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("USE_STRUCTURED_BRANCH_CONTACTS", "1")
        conv = MagicMock()
        conv.extra_metadata = _brain_meta()
        mock_brain.return_value = (conv, conv.extra_metadata["brain_state"])

        msg = f"أرسل الموقع\n\n{_EID_VISION}"
        db = _StructuredDB(branches=[_branch()])
        decision = evaluate_branch_trigger_routing(
            db,
            tenant_id=33,
            message=msg,
            customer_phone="966500000000",
            inbound_metadata={"source_type": "image", "caption": "أرسل الموقع"},
        )
        assert decision is not None
        assert decision.maps_url == _MAPS_URL or MSG_PICKUP_PREFERENCE_ASK in (
            decision.reply_text or ""
        )

    def test_should_replay_blocks_operational_last_question_on_eid(self) -> None:
        from modules.ai.brain.commerce.conversation_state_isolation import (
            should_replay_pending_question,
        )

        assert should_replay_pending_question(
            inbound_text=_EID_VISION,
            last_question=MSG_PICKUP_PREFERENCE_ASK,
            inbound_metadata={"source_type": "image"},
        ) is False


class TestD5D6ARegression:
    def test_d6b_general_image_not_identity(self) -> None:
        from modules.ai.brain.commerce.identity_collaboration_guard import (
            TOPIC_IDENTITY_COLLABORATION,
        )

        decision = _brain_decide(_SOCIAL_VISION)
        assert decision.args.get("topic") != TOPIC_IDENTITY_COLLABORATION

    def test_d6a_ocr_storefront_not_website(self) -> None:
        from modules.ai.brain.commerce.link_intent import (
            LinkIntentType,
            resolve_inbound_link_intent,
        )
        from modules.ai.brain.execution.faq import TOPIC_STORE_INFO

        msg = "[وصف الصورة] محتوى يذكر رابط المتجر الإلكتروني"
        assert resolve_inbound_link_intent(msg) == LinkIntentType.UNKNOWN_LINK
        decision = _brain_decide(msg)
        assert decision.args.get("topic") != TOPIC_STORE_INFO

    def test_d5_ocr_not_staff_contact(self) -> None:
        from modules.ai.brain.commerce.staff_contact_evidence import (
            classify_staff_contact_request,
            compile_staff_contact_registry,
        )

        class _Section:
            id = 1
            kind = "custom"
            body = "موظف: 0501111111"
            title = ""
            metadata = {}
            metadata_json = {}
            updated_at = 1

        reg = compile_staff_contact_registry([_Section()])
        vision = "[وصف الصورة] screenshot with creator handle visible"
        assert classify_staff_contact_request(vision, registry=reg).kind == "none"
