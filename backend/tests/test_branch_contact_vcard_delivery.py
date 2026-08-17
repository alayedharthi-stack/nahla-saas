"""
Branch-contact vCard delivery — no-response escalation path (Jul 2026).

Regression for production smoke C: ``مافيه احد`` routed to branch_contact with
``branch_vcard_sent=false`` because the contact-delivery gate blocked vCard send
when ``escalation_reason`` was not wired from the branch-trigger decision.
"""
from __future__ import annotations

import asyncio
import os
import sys
import types as _types
from typing import Any, List, Optional, Sequence, Type
from unittest.mock import AsyncMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.branch_trigger_router import (  # noqa: E402
    evaluate_branch_trigger_routing,
)
from modules.ai.brain.commerce.contact_delivery_gate import (  # noqa: E402
    evaluate_contact_delivery_gate,
)
from modules.operations.branch_arrival_keyword_evidence import (  # noqa: E402
    TRIGGER_NO_RESPONSE,
    match_branch_trigger,
)
from modules.operations.branch_contact_evidence import (  # noqa: E402
    load_branch_contacts,
)
from services.call_resolver import CallTarget, build_contacts_payload  # noqa: E402

TENANT_A = 821
TENANT_B = 822
BRANCH_A = "معرض الرياض"
BRANCH_B = "فرع جدة"
MAPS_A = "https://maps.example/branch-a"
MAPS_B = "https://maps.example/branch-b"
PHONE_A = "966511100011"
PHONE_B = "966522200022"
CUSTOMER_PHONE = "966500009921"
MSG_NO_ONE = "مافيه احد"
MSG_LOCATION = "وين موقعكم"
MSG_ON_THE_WAY = "انا في الطريق"
ESCALATION_REASON = "no_response_escalation_advance"


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


def _branch_db(
    tenant_id: int,
    *,
    branch_id: int,
    branch_name: str,
    maps_url: str,
    phone: str,
    contact_name: str = "أحمد سالم",
    role: str = "showroom",
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
                display_name=contact_name,
                role=role,
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
                display_name=contact_name,
                role=role,
                phone_e164=phone,
                sort_order=0,
                is_active=True,
            )
        ],
    )


def _install_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_STRUCTURED_BRANCH_CONTACTS", "1")

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


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_env(monkeypatch)


class TestNoResponseRoute:
    def test_t1_trigger_and_branch_contact_decision(self) -> None:
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
            inbound_metadata={"source_type": "text", "normalized_type": "text"},
        )
        # Customer NL stays Brain-owned; keyword match is evidence only.
        assert decision is None
        contacts = load_branch_contacts(db, 1)
        assert contacts
        assert contacts[0].phone_e164 == PHONE_A


class TestContactDeliveryGate:
    def test_gate_blocks_no_response_without_escalation_reason(self) -> None:
        gate = evaluate_contact_delivery_gate(
            customer_message=MSG_NO_ONE,
            delivery_path="branch_trigger_router",
            policy_deliver_contact=True,
        )
        assert gate.allow is False
        assert gate.reason == "no_explicit_contact_intent"

    def test_gate_allows_no_response_with_escalation_reason(self) -> None:
        gate = evaluate_contact_delivery_gate(
            customer_message=MSG_NO_ONE,
            delivery_path="branch_trigger_router",
            policy_deliver_contact=True,
            escalation_reason=ESCALATION_REASON,
        )
        assert gate.allow is True
        assert gate.reason == "policy_path:branch_trigger_router"

    def test_t7_location_regression_unchanged(self) -> None:
        gate = evaluate_contact_delivery_gate(
            customer_message=MSG_LOCATION,
            delivery_path="branch_trigger_router",
            policy_deliver_contact=True,
            escalation_reason=ESCALATION_REASON,
        )
        assert gate.allow is False
        assert gate.reason == "branch_location_only"


class TestContactsPayload:
    def test_t3_payload_shape(self) -> None:
        target = CallTarget(
            name="أحمد سالم",
            wa_id=PHONE_A,
            phone_display=f"+{PHONE_A}",
            raw_phone=PHONE_A,
        )
        payload = build_contacts_payload([target], to=CUSTOMER_PHONE)
        assert payload["type"] == "contacts"
        assert payload["to"] == CUSTOMER_PHONE
        contact = payload["contacts"][0]
        assert contact["name"]["formatted_name"] == "أحمد سالم"
        assert contact["phones"][0]["phone"] == f"+{PHONE_A}"
        assert contact["phones"][0]["wa_id"] == PHONE_A


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class TestSendContactsMessage:
    def test_t2_vcard_send_called_once_with_escalation_reason(self) -> None:
        async def _case() -> bool:
            from routers.whatsapp_webhook import _send_contacts_message  # noqa: PLC0415

            target = CallTarget(
                name="أحمد سالم",
                wa_id=PHONE_A,
                phone_display=f"+{PHONE_A}",
                raw_phone=PHONE_A,
            )
            payload = build_contacts_payload([target], to=CUSTOMER_PHONE)

            with patch(
                "routers.whatsapp_webhook._post_wa",
                new_callable=AsyncMock,
                return_value=True,
            ) as post_wa:
                ok = await _send_contacts_message(
                    phone_id="pid",
                    to=CUSTOMER_PHONE,
                    payload=payload,
                    customer_message=MSG_NO_ONE,
                    delivery_path="branch_trigger_router",
                    escalation_reason=ESCALATION_REASON,
                    policy_deliver_contact=True,
                )
                assert ok is True
                post_wa.assert_awaited_once()
                sent_payload = post_wa.await_args.args[1]
                assert sent_payload["type"] == "contacts"
                assert sent_payload["contacts"][0]["name"]["formatted_name"] == "أحمد سالم"
            return ok

        _run(_case())

    def test_t4_provider_failure_returns_false_without_post(self) -> None:
        async def _case() -> None:
            from routers.whatsapp_webhook import _send_contacts_message  # noqa: PLC0415

            target = CallTarget(
                name="أحمد سالم",
                wa_id=PHONE_A,
                phone_display=f"+{PHONE_A}",
                raw_phone=PHONE_A,
            )
            payload = build_contacts_payload([target], to=CUSTOMER_PHONE)

            with patch(
                "routers.whatsapp_webhook._post_wa",
                new_callable=AsyncMock,
                return_value=False,
            ) as post_wa:
                ok = await _send_contacts_message(
                    phone_id="pid",
                    to=CUSTOMER_PHONE,
                    payload=payload,
                    customer_message=MSG_NO_ONE,
                    delivery_path="branch_trigger_router",
                    escalation_reason=ESCALATION_REASON,
                    policy_deliver_contact=True,
                )
                assert ok is False
                post_wa.assert_awaited_once()

        _run(_case())

    def test_missing_escalation_reason_skips_provider_post(self) -> None:
        async def _case() -> None:
            from routers.whatsapp_webhook import _send_contacts_message  # noqa: PLC0415

            target = CallTarget(
                name="أحمد سالم",
                wa_id=PHONE_A,
                phone_display=f"+{PHONE_A}",
                raw_phone=PHONE_A,
            )
            payload = build_contacts_payload([target], to=CUSTOMER_PHONE)

            with patch(
                "routers.whatsapp_webhook._post_wa",
                new_callable=AsyncMock,
                return_value=True,
            ) as post_wa:
                ok = await _send_contacts_message(
                    phone_id="pid",
                    to=CUSTOMER_PHONE,
                    payload=payload,
                    customer_message=MSG_NO_ONE,
                    delivery_path="branch_trigger_router",
                    policy_deliver_contact=True,
                )
                assert ok is False
                post_wa.assert_not_awaited()

        _run(_case())


class TestTenantIsolation:
    def test_t6_distinct_contacts_per_tenant(self) -> None:
        db_a = _branch_db(
            TENANT_A,
            branch_id=1,
            branch_name=BRANCH_A,
            maps_url=MAPS_A,
            phone=PHONE_A,
            contact_name="أحمد سالم",
        )
        db_b = _branch_db(
            TENANT_B,
            branch_id=2,
            branch_name=BRANCH_B,
            maps_url=MAPS_B,
            phone=PHONE_B,
            contact_name="نورة عبدالله",
        )
        dec_a = evaluate_branch_trigger_routing(
            db_a, tenant_id=TENANT_A, message=MSG_NO_ONE, customer_phone=CUSTOMER_PHONE,
            inbound_metadata={"source_type": "text", "normalized_type": "text"},
        )
        dec_b = evaluate_branch_trigger_routing(
            db_b, tenant_id=TENANT_B, message=MSG_NO_ONE, customer_phone=CUSTOMER_PHONE,
            inbound_metadata={"source_type": "text", "normalized_type": "text"},
        )
        assert dec_a is None and dec_b is None
        contacts_a = load_branch_contacts(db_a, 1)
        contacts_b = load_branch_contacts(db_b, 2)
        assert contacts_a[0].phone_e164 == PHONE_A
        assert contacts_b[0].phone_e164 == PHONE_B
        assert contacts_a[0].display_name == "أحمد سالم"
        assert contacts_b[0].display_name == "نورة عبدالله"


class TestRegressionRoutes:
    def test_t7_location_and_arrival_routes_still_resolve(self) -> None:
        db = _branch_db(
            TENANT_A, branch_id=1, branch_name=BRANCH_A, maps_url=MAPS_A, phone=PHONE_A,
        )
        loc = evaluate_branch_trigger_routing(
            db, tenant_id=TENANT_A, message=MSG_LOCATION,
            inbound_metadata={"source_type": "text", "normalized_type": "text"},
        )
        arr = evaluate_branch_trigger_routing(
            db, tenant_id=TENANT_A, message=MSG_ON_THE_WAY,
            inbound_metadata={"source_type": "text", "normalized_type": "text"},
        )
        assert loc is None and arr is None
        contacts = load_branch_contacts(db, 1)
        assert contacts[0].phone_e164 == PHONE_A
