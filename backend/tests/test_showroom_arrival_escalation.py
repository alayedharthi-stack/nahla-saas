"""Platform-wide showroom arrival / location escalation regression tests."""
from __future__ import annotations

import os
import sys
import types as _types
from typing import Any, List, Optional, Sequence, Type
from unittest.mock import patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.branch_trigger_router import (  # noqa: E402
    MSG_NO_TRUSTED_BRANCH_CONTACT,
    evaluate_branch_trigger_routing,
)
from modules.ai.brain.commerce.location_link_policy import (  # noqa: E402
    evaluate_location_link_policy,
)
from modules.ai.brain.commerce.staff_contact_policy import (  # noqa: E402
    evaluate_staff_contact_policy,
)
from modules.ai.brain.commerce.staff_contact_target_continuity import (  # noqa: E402
    is_bare_who_to_call_followup,
)
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.current_turn_social_non_commerce import (  # noqa: E402
    resolve_current_turn_social_non_commerce,
)
from modules.ai.brain.persona.catalog_product_answer import (  # noqa: E402
    classify_catalog_question_kind,
)
from modules.ai.brain.types import INTENT_ASK_PRODUCT  # noqa: E402
from modules.operations.branch_arrival_keyword_evidence import (  # noqa: E402
    TRIGGER_NO_RESPONSE,
    match_branch_trigger,
)

GENERIC_TENANT_ID = 901
GENERIC_MAPS_URL = "https://maps.google.com/?q=generic-showroom-riyadh"
GENERIC_RECEPTION_NAME = "\u0633\u0627\u0644\u0645"  # سالم
GENERIC_RECEPTION_PHONE = "966511122233"
GENERIC_CUSTOMER_PHONE = "966500009901"
MSG_NO_ONE_HERE = "\u0645\u0627\u0644\u0642\u064a\u062a \u0627\u062d\u062f"  # مالقيت احد
MSG_ON_THE_WAY = "\u0627\u0646\u0627 \u0641\u064a \u0627\u0644\u0637\u0631\u064a\u0642"  # انا في الطريق
MSG_WHO_TO_CALL = "\u0627\u062a\u0635\u0644 \u0639\u0644\u0649 \u0645\u0646\u061f"  # اتصل على من؟
MSG_LOCATION_ASK = "\u0648\u064a\u0646 \u0645\u0648\u0642\u0639\u0643\u0645"  # وين موقعكم
_VAGUE_FALLBACK_PHRASES = (
    "أتوقع في بيانات طلبك",
    "دور الرقم في رسائل المتجر",
    "اتصل على رقم المتجر",
)


class _Row:
    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Q:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def filter(self, *args: Any, **kwargs: Any) -> "_Q":
        return self

    def order_by(self, *args: Any, **kwargs: Any) -> "_Q":
        return self

    def first(self) -> Optional[Any]:
        return self._rows[0] if self._rows else None

    def all(self) -> List[Any]:
        return list(self._rows)


class _DB:
    def __init__(self, **tables: List[Any]) -> None:
        self.branches = tables.get("branches", [])
        self.contacts = tables.get("contacts", [])
        self.steps = tables.get("steps", [])
        self.keywords = tables.get("keywords", [])

    def query(self, model: Type[Any]) -> _Q:
        name = getattr(model, "__name__", str(model))
        mapping = {
            "MerchantBranch": self.branches,
            "BranchContact": self.contacts,
            "BranchEscalationStep": self.steps,
            "BranchArrivalKeyword": self.keywords,
        }
        return _Q(mapping.get(name, []))


class _StubConv:
    def __init__(self, brain_state: dict) -> None:
        self.extra_metadata = {"brain_state": brain_state}
        self.id = 1


def _generic_branch_db(
    *,
    with_contacts: bool = True,
    with_escalation: bool = True,
) -> _DB:
    contacts: List[_Row] = []
    steps: List[_Row] = []
    if with_contacts:
        contacts.append(
            _Row(
                id=11,
                branch_id=1,
                display_name=GENERIC_RECEPTION_NAME,
                role="reception",
                phone_e164=GENERIC_RECEPTION_PHONE,
                whatsapp_e164="",
                sort_order=0,
                is_active=True,
                is_default_reception=True,
            )
        )
    if with_escalation:
        steps.append(
            _Row(
                id=101,
                branch_id=1,
                escalation_level=1,
                display_name=GENERIC_RECEPTION_NAME,
                role="showroom",
                phone_e164=GENERIC_RECEPTION_PHONE,
                sort_order=0,
                is_active=True,
            )
        )
    return _DB(
        branches=[
            _Row(
                id=1,
                tenant_id=GENERIC_TENANT_ID,
                name="معرض الرياض",
                city="الرياض",
                district="",
                address="",
                maps_url=GENERIC_MAPS_URL,
                sort_order=0,
                is_active=True,
                location_response_mode="location_only",
                arrival_response_mode="reception_only",
                location_instructions_text="",
            ),
        ],
        contacts=contacts,
        steps=steps,
        keywords=[],
    )


def _install_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_STRUCTURED_BRANCH_CONTACTS", "1")
    monkeypatch.setenv("LOCATION_LINK_POLICY_ENABLED", "1")
    monkeypatch.setenv("STAFF_CONTACT_POLICY_ENABLED", "1")

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


def _assert_no_commerce_hijack(text: str) -> None:
    low = (text or "").lower()
    for phrase in (
        "اختر رقم",
        "إنشاء طلب",
        "checkout",
        "الدفع",
        "اسمك",
        "عنوانك",
    ):
        assert phrase not in low


def _assert_no_vague_contact_fallback(text: str) -> None:
    for phrase in _VAGUE_FALLBACK_PHRASES:
        assert phrase not in (text or "")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_env(monkeypatch)


class TestLocationRequest:
    def test_structured_branch_location_compose_facts_and_cta(self) -> None:
        db = _generic_branch_db()
        decision = evaluate_branch_trigger_routing(
            db,
            tenant_id=GENERIC_TENANT_ID,
            message=MSG_LOCATION_ASK,
        )
        assert decision is not None
        assert decision.trigger_type == "location_request"
        assert decision.maps_url == GENERIC_MAPS_URL
        assert decision.use_cta is True
        assert decision.compose_facts is not None
        assert decision.compose_facts.maps_cta_available is True
        assert not (decision.reply_text or "").strip()
        _assert_no_commerce_hijack(decision.reply_text)

    def test_location_link_policy_transcript_safe_body(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "modules.ai.postprocess.safety_nets._lookup_tenant_maps_url",
            lambda _db, _tid: (GENERIC_MAPS_URL, "snapshot"),
        )
        decision = evaluate_location_link_policy(
            object(),
            tenant_id=GENERIC_TENANT_ID,
            message=MSG_LOCATION_ASK,
        )
        assert decision is not None
        assert decision.maps_url == GENERIC_MAPS_URL
        assert decision.reply_text.strip() != "موقعنا 📍"
        assert GENERIC_MAPS_URL in decision.reply_text
        assert decision.use_cta is True


class TestSoftArrival:
    def test_on_the_way_structured_compose_facts(self) -> None:
        db = _generic_branch_db()
        decision = evaluate_branch_trigger_routing(
            db,
            tenant_id=GENERIC_TENANT_ID,
            message=MSG_ON_THE_WAY,
        )
        assert decision is not None
        assert decision.trigger_type == "arrival_soft"
        assert decision.compose_facts is not None
        assert decision.compose_facts.action_kind == "arrival_soft"
        assert decision.compose_facts.location_already_sent is True
        assert decision.maps_url == GENERIC_MAPS_URL
        assert decision.resend_maps is True
        assert not (decision.reply_text or "").strip()
        _assert_no_commerce_hijack(decision.reply_text)


def _patch_empty_brain_state(monkeypatch: pytest.MonkeyPatch) -> None:
    conv = _StubConv({"turn": 1, "order_prep": {}})
    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda _db, *, tenant_id, phone: (conv, {"turn": 1, "order_prep": {}}),
    )


_CHECKOUT_SAVED_CHOICES = "\u0627\u062e\u062a\u064a\u0627\u0631\u0627\u062a\u0643 \u0645\u062d\u0641\u0648\u0638\u0629"


def _active_checkout_order_prep() -> dict:
    return {
        "line_items": [{"product_id": "generic-p1", "name": "\u062d\u0630\u0627\u0621 \u0631\u064a\u0627\u0636\u064a"}],
        "checkout_channel": "whatsapp_fast",
        "missing_fields": ["address", "short_address_code"],
        "customer_phone": GENERIC_CUSTOMER_PHONE,
    }


def _patch_brain_state(monkeypatch: pytest.MonkeyPatch, brain_state: dict) -> None:
    conv = _StubConv(brain_state)
    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda _db, *, tenant_id, phone: (conv, dict(brain_state)),
    )


def _assert_no_checkout_continuation(text: str) -> None:
    assert _CHECKOUT_SAVED_CHOICES not in (text or "")
    _assert_no_commerce_hijack(text)


class TestNoOneHereEscalation:
    def test_malagit_ahad_delivers_trusted_contact(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_empty_brain_state(monkeypatch)
        db = _generic_branch_db()
        decision = evaluate_branch_trigger_routing(
            db,
            tenant_id=GENERIC_TENANT_ID,
            message=MSG_NO_ONE_HERE,
            customer_phone=GENERIC_CUSTOMER_PHONE,
        )
        assert decision is not None
        assert decision.trigger_type == "no_response"
        assert decision.deliver_contact is True
        assert decision.call_target is not None
        assert GENERIC_RECEPTION_NAME in (decision.call_target.name or "")
        _assert_no_vague_contact_fallback(decision.reply_text)
        _assert_no_commerce_hijack(decision.reply_text)


class TestWhoToCallFollowUp:
    def test_who_to_call_after_arrival_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _generic_branch_db()
        brain_state = {
            "turn": 6,
            "recent_messages": [
                {
                    "direction": "inbound",
                    "body": MSG_ON_THE_WAY,
                    "turn": 5,
                },
            ],
        }
        conv = _StubConv(brain_state)
        monkeypatch.setattr(
            "core.order_flow._load_brain_state",
            lambda _db, *, tenant_id, phone: (conv, dict(brain_state)),
        )
        assert is_bare_who_to_call_followup(MSG_WHO_TO_CALL) is True
        decision = evaluate_staff_contact_policy(
            db,
            tenant_id=GENERIC_TENANT_ID,
            message=MSG_WHO_TO_CALL,
            customer_phone=GENERIC_CUSTOMER_PHONE,
        )
        assert decision is not None
        assert decision.deliver_contact is True
        assert decision.call_target is not None
        assert GENERIC_RECEPTION_NAME in (decision.call_target.name or "")
        _assert_no_vague_contact_fallback(decision.reply_text)


class TestCheckoutContinuityOverride:
    def test_stale_checkout_who_to_call_after_no_one_here(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _generic_branch_db()
        _patch_empty_brain_state(monkeypatch)
        turn_one = evaluate_branch_trigger_routing(
            db,
            tenant_id=GENERIC_TENANT_ID,
            message=MSG_NO_ONE_HERE,
            customer_phone=GENERIC_CUSTOMER_PHONE,
        )
        assert turn_one is not None
        assert turn_one.deliver_contact is True
        assert turn_one.call_target is not None
        assert GENERIC_RECEPTION_NAME in (turn_one.call_target.name or "")

        from modules.ai.brain.commerce.staff_contact_target_continuity import (  # noqa: PLC0415
            PENDING_CONTACT_TARGET_KEY,
            PendingContactTarget,
        )

        order_prep = _active_checkout_order_prep()
        order_prep[PENDING_CONTACT_TARGET_KEY] = PendingContactTarget(
            lookup_name=GENERIC_RECEPTION_NAME,
            display_name=GENERIC_RECEPTION_NAME,
            source="structured_branch_reception",
            confidence=0.96,
            created_turn=7,
        ).to_dict()
        brain_state = {
            "turn": 9,
            "stage": "ordering",
            "order_prep": order_prep,
            "recent_messages": [
                {"direction": "inbound", "body": MSG_NO_ONE_HERE, "turn": 7},
                {"direction": "outbound", "body": turn_one.reply_text, "turn": 8},
            ],
        }
        _patch_brain_state(monkeypatch, brain_state)

        turn_two = evaluate_staff_contact_policy(
            db,
            tenant_id=GENERIC_TENANT_ID,
            message=MSG_WHO_TO_CALL,
            customer_phone=GENERIC_CUSTOMER_PHONE,
        )
        assert turn_two is not None
        assert turn_two.skip_brain is True
        assert turn_two.deliver_contact is True
        assert turn_two.call_target is not None
        assert GENERIC_RECEPTION_NAME in (turn_two.call_target.name or "")
        _assert_no_checkout_continuation(turn_two.reply_text)
        _assert_no_vague_contact_fallback(turn_two.reply_text)

    def test_soft_arrival_who_to_call_beats_stale_checkout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _generic_branch_db()
        brain_state = {
            "turn": 7,
            "stage": "ordering",
            "order_prep": _active_checkout_order_prep(),
            "recent_messages": [
                {"direction": "inbound", "body": MSG_ON_THE_WAY, "turn": 6},
            ],
        }
        _patch_brain_state(monkeypatch, brain_state)

        decision = evaluate_staff_contact_policy(
            db,
            tenant_id=GENERIC_TENANT_ID,
            message=MSG_WHO_TO_CALL,
            customer_phone=GENERIC_CUSTOMER_PHONE,
        )
        assert decision is not None
        assert decision.skip_brain is True
        assert decision.deliver_contact is True
        assert decision.call_target is not None
        assert GENERIC_RECEPTION_NAME in (decision.call_target.name or "")
        _assert_no_checkout_continuation(decision.reply_text)

    def test_checkout_preservation_tamam_without_arrival_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from core.wa_draft_confirmation import compose_wa_order_flow_reply  # noqa: PLC0415

        db = _generic_branch_db()
        order_prep = _active_checkout_order_prep()
        brain_state = {
            "turn": 4,
            "stage": "ordering",
            "order_prep": order_prep,
            "recent_messages": [],
        }
        _patch_brain_state(monkeypatch, brain_state)

        decision = evaluate_staff_contact_policy(
            db,
            tenant_id=GENERIC_TENANT_ID,
            message="\u062a\u0645\u0627\u0645",
            customer_phone=GENERIC_CUSTOMER_PHONE,
        )
        assert decision is None

        checkout_reply = compose_wa_order_flow_reply(
            order_prep=order_prep,
            brain_state=brain_state,
            customer_message="\u062a\u0645\u0627\u0645",
        )
        assert checkout_reply is not None
        assert _CHECKOUT_SAVED_CHOICES in checkout_reply

    def test_bare_who_to_call_without_context_does_not_override_checkout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _generic_branch_db()
        brain_state = {
            "turn": 3,
            "stage": "ordering",
            "order_prep": _active_checkout_order_prep(),
            "recent_messages": [],
        }
        _patch_brain_state(monkeypatch, brain_state)

        decision = evaluate_staff_contact_policy(
            db,
            tenant_id=GENERIC_TENANT_ID,
            message=MSG_WHO_TO_CALL,
            customer_phone=GENERIC_CUSTOMER_PHONE,
        )
        assert decision is None

    def test_missing_trusted_contact_no_invented_phone(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _generic_branch_db(with_contacts=False, with_escalation=False)
        brain_state = {
            "turn": 6,
            "stage": "ordering",
            "order_prep": _active_checkout_order_prep(),
            "recent_messages": [
                {"direction": "inbound", "body": MSG_ON_THE_WAY, "turn": 5},
            ],
        }
        _patch_brain_state(monkeypatch, brain_state)

        decision = evaluate_staff_contact_policy(
            db,
            tenant_id=GENERIC_TENANT_ID,
            message=MSG_WHO_TO_CALL,
            customer_phone=GENERIC_CUSTOMER_PHONE,
        )
        if decision is not None:
            assert decision.deliver_contact is False
            assert "966" not in (decision.reply_text or "")
            _assert_no_checkout_continuation(decision.reply_text)
        else:
            assert decision is None


class TestNoResponseVariants:
    @pytest.mark.parametrize(
        "message",
        [
            "ما لقيت أحد",
            "مالقيت أحد",
            "مافي احد",
            "وينكم",
            "المعرض مقفل",
        ],
    )
    def test_variants_trigger_no_response(self, message: str) -> None:
        db = _generic_branch_db()
        match = match_branch_trigger(db, GENERIC_TENANT_ID, message=message)
        assert match is not None
        assert match.trigger_type == TRIGGER_NO_RESPONSE


class TestMissingContactSafety:
    def test_no_trusted_contact_safe_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_empty_brain_state(monkeypatch)
        db = _generic_branch_db(with_contacts=False, with_escalation=False)
        decision = evaluate_branch_trigger_routing(
            db,
            tenant_id=GENERIC_TENANT_ID,
            message=MSG_NO_ONE_HERE,
            customer_phone=GENERIC_CUSTOMER_PHONE,
        )
        assert decision is not None
        assert decision.trigger_type == "no_response"
        assert decision.deliver_contact is False
        assert decision.reply_text == MSG_NO_TRUSTED_BRANCH_CONTACT
        assert "966" not in decision.reply_text
        _assert_no_vague_contact_fallback(decision.reply_text)


class TestRegressionGuards:
    def test_class2_price_path_unchanged(self) -> None:
        assert classify_catalog_question_kind("كم سعر الطلح؟") == "price"

    def test_class4_order_status_not_showroom_escalation(self) -> None:
        intent = rules.match("طلبي رقم NHL-33-000016")
        assert intent is not None
        assert intent.name != "arrival_soft"
        assert intent.name != "location_request"

    def test_playful_social_not_showroom_escalation(self) -> None:
        msg = "وش عندك من سوالف؟"
        intent = rules.match(msg)
        assert intent is None or intent.name != INTENT_ASK_PRODUCT
        social = resolve_current_turn_social_non_commerce(msg, intent=intent)
        assert social.matched is True
