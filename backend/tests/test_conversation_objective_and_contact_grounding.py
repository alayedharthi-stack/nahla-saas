"""Tests for conversation objective + generic contact + availability fallback guards."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.commerce.entity_extraction_guard import (  # noqa: E402
    extract_staff_name_candidate,
    is_generic_store_contact_phrase,
    is_store_channel_phone_phrase,
)
from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: E402
    MSG_NAME_NOT_CONFIGURED,
    StaffContactRequest,
    classify_staff_contact_request,
    compile_staff_contact_registry,
    resolve_staff_contact,
)
from modules.ai.brain.intent.agent_distributor_classifier import (  # noqa: E402
    is_agent_distributor_inquiry,
)
from modules.ai.brain.intent.conversation_objective_guard import (  # noqa: E402
    OBJECTIVE_PRODUCT_ORIGIN,
    OBJECTIVE_TTL_TURNS,
    is_product_origin_objective_active,
    refresh_conversation_objective,
)
from modules.ai.brain.intent_priority.types import GOAL_PRODUCT_AVAILABILITY  # noqa: E402
from modules.ai.brain.postprocess.commerce_reply_quality_guard import (  # noqa: E402
    apply_commerce_reply_quality_guard,
    select_arabic_commerce_fallback,
)
from modules.ai.brain.types import MerchantConversationState  # noqa: E402


class _Section:
    def __init__(self, *, id: int, kind: str, body: str, title: str = "") -> None:
        self.id = id
        self.kind = kind
        self.body = body
        self.title = title
        self.metadata = {}
        self.metadata_json = {}
        self.updated_at = id


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

    def first(self) -> Any:
        return None

    def all(self) -> List[_Section]:
        return list(self._sections)


def _empty_registry() -> Any:
    return compile_staff_contact_registry([], store_contact_phone="")


def _named_staff_resolution(message: str) -> str:
    request = classify_staff_contact_request(message)
    resolution = resolve_staff_contact(_empty_registry(), request, message=message)
    if resolution.unknown_name:
        return MSG_NAME_NOT_CONFIGURED
    return request.kind


def _fallback(
    inbound: str,
    *,
    intent: str = "ask_product",
    goal: str = GOAL_PRODUCT_AVAILABILITY,
    objective: str = "",
) -> str:
    return select_arabic_commerce_fallback(
        intent_name=intent,
        primary_customer_goal=goal,
        inbound_text=inbound,
        conversation_objective=objective,
    )[0]


def _state(*, turn: int = 0, **kwargs: Any) -> MerchantConversationState:
    base = MerchantConversationState(turn=turn)
    for key, value in kwargs.items():
        setattr(base, key, value)
    return base


class TestGenericContactGuard:
    def test_phone_pronoun_is_general_channel_not_named(self) -> None:
        msg = "عليه رقم تليفونكم"
        assert is_store_channel_phone_phrase(msg)
        assert extract_staff_name_candidate(msg) == ""
        assert classify_staff_contact_request(msg).kind == "general_channel"
        assert _named_staff_resolution(msg) != MSG_NAME_NOT_CONFIGURED

    @pytest.mark.parametrize(
        "msg",
        [
            "رقمكم",
            "رقم الهاتف",
            "رقم الجوال",
            "تليفونكم",
            "هاتفكم",
        ],
    )
    def test_store_channel_phone_variants(self, msg: str) -> None:
        assert is_store_channel_phone_phrase(msg) or is_generic_store_contact_phrase(msg)
        assert classify_staff_contact_request(msg).kind == "general_channel"
        assert _named_staff_resolution(msg) != MSG_NAME_NOT_CONFIGURED

    def test_named_staff_still_works(self) -> None:
        msg = "رقم هشام"
        assert classify_staff_contact_request(msg).kind == "named"
        assert _named_staff_resolution(msg) == MSG_NAME_NOT_CONFIGURED


class TestAgentDistributorAndAvailabilityFallback:
    def test_agent_inquiry_detected(self) -> None:
        assert is_agent_distributor_inquiry("مين وكيلكم في مصر؟")

    def test_distributor_inquiry_detected(self) -> None:
        assert is_agent_distributor_inquiry("عندكم موزع في القاهرة؟")

    def test_agent_empty_reply_not_availability_fallback(self) -> None:
        out = apply_commerce_reply_quality_guard(
            "",
            inbound_text="مين وكيلكم في مصر؟",
            intent_name="ask_product",
            primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
        ).reply
        assert out != "التوفر قيد التحقق."

    def test_real_availability_still_uses_availability_fallback(self) -> None:
        out = _fallback("هل عندكم منتج X؟")
        assert out == "التوفر قيد التحقق."


class TestConversationObjectiveGuard:
    def test_image_plus_ownership_starts_objective(self) -> None:
        st = _state(turn=0)
        result = refresh_conversation_objective(
            st,
            "ده تبعكم؟",
            {"inbound_metadata": {"normalized_type": "image"}},
        )
        assert result.active
        assert st.active_conversation_objective == OBJECTIVE_PRODUCT_ORIGIN
        assert st.objective_evidence.get("has_inbound_image") is True

    def test_followup_agent_inherits_objective(self) -> None:
        st = _state(
            turn=1,
            active_conversation_objective=OBJECTIVE_PRODUCT_ORIGIN,
            objective_started_turn=1,
            objective_last_reinforced_turn=1,
            objective_evidence={"has_inbound_image": True},
        )
        result = refresh_conversation_objective(st, "مين وكيلكم في مصر؟", {})
        assert result.active
        assert is_product_origin_objective_active(st)
        assert st.objective_last_reinforced_turn == 2

    def test_followup_phone_not_named_staff_under_objective(self) -> None:
        st = _state(
            turn=2,
            active_conversation_objective=OBJECTIVE_PRODUCT_ORIGIN,
            objective_started_turn=1,
            objective_last_reinforced_turn=2,
        )
        refresh_conversation_objective(st, "عليه رقم تليفونكم", {})
        assert classify_staff_contact_request("عليه رقم تليفونكم").kind == "general_channel"
        assert _named_staff_resolution("عليه رقم تليفونكم") != MSG_NAME_NOT_CONFIGURED
        out = _fallback(
            "عليه رقم تليفونكم",
            objective=st.active_conversation_objective,
        )
        assert out != "التوفر قيد التحقق."

    def test_supply_chain_stays_in_objective(self) -> None:
        st = _state(
            turn=3,
            active_conversation_objective=OBJECTIVE_PRODUCT_ORIGIN,
            objective_started_turn=1,
            objective_last_reinforced_turn=3,
        )
        result = refresh_conversation_objective(
            st,
            "وصلني ازاي من عندكم؟",
            {},
        )
        assert result.active
        assert st.active_conversation_objective == OBJECTIVE_PRODUCT_ORIGIN
        out = _fallback(
            "وصلني ازاي من عندكم؟",
            objective=st.active_conversation_objective,
        )
        assert out != "التوفر قيد التحقق."

    def test_explicit_purchase_clears_objective(self) -> None:
        st = _state(
            turn=4,
            active_conversation_objective=OBJECTIVE_PRODUCT_ORIGIN,
            objective_started_turn=1,
            objective_last_reinforced_turn=4,
        )
        result = refresh_conversation_objective(st, "أبي أطلب عسل طلح", {})
        assert result.cleared
        assert st.active_conversation_objective == ""
        assert not is_product_origin_objective_active(st)

    def test_ttl_expires_objective(self) -> None:
        st = _state(
            turn=10,
            active_conversation_objective=OBJECTIVE_PRODUCT_ORIGIN,
            objective_started_turn=1,
            objective_last_reinforced_turn=3,
        )
        # Next turn is 11; age = 11 - 3 = 8 > TTL(6)
        result = refresh_conversation_objective(st, "تمام", {})
        assert not result.active
        assert st.active_conversation_objective == ""
        assert not is_product_origin_objective_active(st, current_turn=11)

    def test_received_order_starts_objective(self) -> None:
        st = _state(turn=0)
        result = refresh_conversation_objective(st, "لو سمحت انا جالي اوردر", {})
        assert result.active
        assert st.active_conversation_objective == OBJECTIVE_PRODUCT_ORIGIN

    def test_objective_blocks_availability_for_agent_followup(self) -> None:
        st = _state(
            turn=2,
            active_conversation_objective=OBJECTIVE_PRODUCT_ORIGIN,
            objective_started_turn=1,
            objective_last_reinforced_turn=2,
        )
        out = apply_commerce_reply_quality_guard(
            "",
            inbound_text="مين وكيلكم في مصر؟",
            intent_name="ask_product",
            primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
            conversation_objective=st.active_conversation_objective,
        ).reply
        assert out != "التوفر قيد التحقق."

    def test_ttl_constant_matches_spec(self) -> None:
        assert OBJECTIVE_TTL_TURNS == 6
