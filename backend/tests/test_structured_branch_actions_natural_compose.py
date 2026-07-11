"""
Structured branch actions — LLM-grounded compose with CTA/vCard payloads (Jul 2026).
"""
from __future__ import annotations

import asyncio
import os
import sys
import types as _types
from typing import Any, List, Optional, Sequence, Type

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.branch_trigger_router import (  # noqa: E402
    evaluate_branch_trigger_routing,
)
from modules.ai.brain.persona.branch_action_compose import (  # noqa: E402
    ACTION_KIND_ARRIVAL_SOFT,
    ACTION_KIND_BRANCH_CONTACT,
    ACTION_KIND_LOCATION,
    BranchComposeFacts,
    guard_branch_action_body,
    minimal_emergency_fallback,
    plain_text_location_fallback_body,
    try_compose_branch_action,
)
from modules.ai.brain.persona.facts_bundle import PersonaFactsBundle  # noqa: E402
from modules.operations.branch_arrival_keyword_evidence import (  # noqa: E402
    TRIGGER_NO_RESPONSE,
    match_branch_trigger,
)

TENANT_A = 811
TENANT_B = 812
MAPS_A = "https://maps.example/branch-a"
MAPS_B = "https://maps.example/branch-b"
PHONE_A = "966511100011"
PHONE_B = "966522200022"
BRANCH_A = "معرض الرياض"
BRANCH_B = "فرع جدة"
CUSTOMER_PHONE = "966500009911"
MSG_LOCATION = "وين موقعكم"
MSG_ON_THE_WAY = "انا في الطريق"
MSG_NO_ONE = "مافيه احد"
_FORBIDDEN_NORMAL = (
    "أهلاً بك، في انتظارك",
    "حاضر، جرّب التواصل",
    "اضغط الزر",
    "هذا موقع",
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
    def __init__(self) -> None:
        self.extra_metadata = {"brain_state": {"turn": 1, "order_prep": {}}}
        self.id = 1


def _branch_db(
    tenant_id: int,
    *,
    branch_id: int,
    branch_name: str,
    maps_url: str,
    phone: str,
) -> _DB:
    return _DB(
        branches=[
            _Row(
                id=branch_id,
                tenant_id=tenant_id,
                name=branch_name,
                city="الرياض",
                district="",
                address="",
                maps_url=maps_url,
                sort_order=0,
                is_active=True,
                location_response_mode="location_only",
                arrival_response_mode="reception_only",
                location_instructions_text="",
            )
        ],
        contacts=[
            _Row(
                id=branch_id * 10,
                branch_id=branch_id,
                display_name="سالم",
                role="reception",
                phone_e164=phone,
                whatsapp_e164="",
                sort_order=0,
                is_active=True,
                is_default_reception=True,
            )
        ],
        steps=[
            _Row(
                id=branch_id * 100,
                branch_id=branch_id,
                escalation_level=1,
                display_name="سالم",
                role="reception",
                phone_e164=phone,
                sort_order=0,
                is_active=True,
            )
        ],
    )


def _install_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_STRUCTURED_BRANCH_CONTACTS", "1")
    monkeypatch.setenv("BRANCH_ACTION_COMPOSE_ENABLED", "1")

    call_stub = _types.ModuleType("services.call_resolver")

    class _CallTarget:
        def __init__(self, name: str, wa_id: str, phone_display: str, raw_phone: str) -> None:
            self.name = name
            self.wa_id = wa_id
            self.phone_display = phone_display
            self.raw_phone = raw_phone

    def _fake_normalize(phone: str) -> str:
        digits = "".join(c for c in phone if c.isdigit())
        return digits

    call_stub.CallTarget = _CallTarget  # type: ignore[attr-defined]
    call_stub._normalize_saudi_phone = _fake_normalize  # type: ignore[attr-defined]
    call_stub._pretty_phone = lambda w: w  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "services.call_resolver", call_stub)

    conv = _StubConv()
    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda _db, *, tenant_id, phone: (conv, {"turn": 1, "order_prep": {}}),
    )


def _stub_llm_factory(label: str):
    async def _llm(bundle: PersonaFactsBundle) -> str:
        action = str((bundle.verified_facts or {}).get("action_kind") or "")
        branch = str((bundle.verified_facts or {}).get("branch_name") or "")
        return f"{label}:{action}:{branch}"

    return _llm


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_env(monkeypatch)


class TestComposeSurface:
    def test_t1_location_cta_compose_omits_url(self) -> None:
        facts = BranchComposeFacts(
            action_kind=ACTION_KIND_LOCATION,
            customer_message=MSG_LOCATION,
            branch_name=BRANCH_A,
            maps_cta_available=True,
            maps_configured=True,
        )
        out = _run(
            try_compose_branch_action(
                facts,
                tenant_id=TENANT_A,
                customer_phone=CUSTOMER_PHONE,
                llm_callable=_stub_llm_factory("loc"),
            )
        )
        assert out.compose_source == "persona_llm"
        assert MAPS_A not in out.text
        assert len(out.text) < 120
        body = plain_text_location_fallback_body(out.text, MAPS_A, use_cta=True)
        assert MAPS_A not in body

    def test_t2_compose_failure_uses_minimal_fallback(self) -> None:
        async def _fail(_bundle: PersonaFactsBundle) -> str:
            return ""

        facts = BranchComposeFacts(
            action_kind=ACTION_KIND_LOCATION,
            customer_message=MSG_LOCATION,
            branch_name=BRANCH_A,
            maps_cta_available=True,
        )
        out = _run(
            try_compose_branch_action(
                facts,
                tenant_id=TENANT_A,
                llm_callable=_fail,
            )
        )
        assert out.compose_source == "fallback_deterministic"
        assert out.fallback_reason
        assert BRANCH_A in out.text or "📍" in out.text

    def test_t3_cta_unavailable_includes_trusted_url(self) -> None:
        body = plain_text_location_fallback_body(
            "loc:location:معرض",
            MAPS_A,
            use_cta=False,
        )
        assert MAPS_A in body
        assert "invented" not in body

    def test_t4_soft_arrival_differs_from_location(self) -> None:
        db = _branch_db(
            TENANT_A, branch_id=1, branch_name=BRANCH_A, maps_url=MAPS_A, phone=PHONE_A,
        )
        first = evaluate_branch_trigger_routing(
            db, tenant_id=TENANT_A, message=MSG_LOCATION,
        )
        second = evaluate_branch_trigger_routing(
            db, tenant_id=TENANT_A, message=MSG_ON_THE_WAY,
        )
        assert first is not None and second is not None
        assert first.compose_facts is not None
        assert second.compose_facts is not None
        loc_out = _run(
            try_compose_branch_action(
                first.compose_facts,
                tenant_id=TENANT_A,
                llm_callable=_stub_llm_factory("a"),
            )
        )
        arr_out = _run(
            try_compose_branch_action(
                second.compose_facts,
                tenant_id=TENANT_A,
                llm_callable=_stub_llm_factory("b"),
            )
        )
        assert loc_out.text != arr_out.text
        assert second.compose_facts.action_kind == ACTION_KIND_ARRIVAL_SOFT
        assert MAPS_A not in arr_out.text
        for phrase in _FORBIDDEN_NORMAL:
            assert phrase not in arr_out.text

    def test_t5_no_one_triggers_contact_route(self) -> None:
        db = _branch_db(
            TENANT_A, branch_id=1, branch_name=BRANCH_A, maps_url=MAPS_A, phone=PHONE_A,
        )
        match = match_branch_trigger(db, TENANT_A, MSG_NO_ONE)
        assert match is not None
        assert match.trigger_type == TRIGGER_NO_RESPONSE
        decision = evaluate_branch_trigger_routing(
            db,
            tenant_id=TENANT_A,
            message=MSG_NO_ONE,
            customer_phone=CUSTOMER_PHONE,
        )
        assert decision is not None
        assert decision.deliver_contact is True
        assert decision.call_target is not None
        assert decision.compose_facts is not None
        assert decision.compose_facts.action_kind == ACTION_KIND_BRANCH_CONTACT
        assert decision.compose_facts.contact_card_available is True

    def test_t5_contact_compose_strips_phone_when_vcard(self) -> None:
        facts = BranchComposeFacts(
            action_kind=ACTION_KIND_BRANCH_CONTACT,
            customer_message=MSG_NO_ONE,
            branch_name=BRANCH_A,
            contact_name="سالم",
            contact_role="reception",
            contact_card_available=True,
        )
        async def _phoney(_bundle: PersonaFactsBundle) -> str:
            return f"تواصل على {PHONE_A}"

        out = _run(
            try_compose_branch_action(
                facts,
                tenant_id=TENANT_A,
                llm_callable=_phoney,
            )
        )
        guarded = guard_branch_action_body(out.text, facts)
        assert PHONE_A not in guarded

    def test_t6_tenant_isolation(self) -> None:
        db_a = _branch_db(
            TENANT_A, branch_id=1, branch_name=BRANCH_A, maps_url=MAPS_A, phone=PHONE_A,
        )
        db_b = _branch_db(
            TENANT_B, branch_id=2, branch_name=BRANCH_B, maps_url=MAPS_B, phone=PHONE_B,
        )
        dec_a = evaluate_branch_trigger_routing(db_a, tenant_id=TENANT_A, message=MSG_LOCATION)
        dec_b = evaluate_branch_trigger_routing(db_b, tenant_id=TENANT_B, message=MSG_LOCATION)
        assert dec_a is not None and dec_b is not None
        assert dec_a.maps_url == MAPS_A
        assert dec_b.maps_url == MAPS_B
        assert dec_a.compose_facts.branch_name == BRANCH_A
        assert dec_b.compose_facts.branch_name == BRANCH_B

    def test_t8_no_new_normal_path_templates_in_router_decision(self) -> None:
        db = _branch_db(
            TENANT_A, branch_id=1, branch_name=BRANCH_A, maps_url=MAPS_A, phone=PHONE_A,
        )
        for msg in (MSG_LOCATION, MSG_ON_THE_WAY, MSG_NO_ONE):
            decision = evaluate_branch_trigger_routing(
                db,
                tenant_id=TENANT_A,
                message=msg,
                customer_phone=CUSTOMER_PHONE,
            )
            assert decision is not None
            assert decision.compose_facts is not None
            assert not (decision.reply_text or "").strip()
            for phrase in _FORBIDDEN_NORMAL:
                assert phrase not in (decision.reply_text or "")

    def test_emergency_fallback_is_minimal(self) -> None:
        fb = minimal_emergency_fallback(
            BranchComposeFacts(action_kind=ACTION_KIND_ARRIVAL_SOFT),
        )
        assert len(fb) <= 4
