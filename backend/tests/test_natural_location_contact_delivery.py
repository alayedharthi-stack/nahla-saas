"""
Platform-wide regression — natural location CTA bodies, arrival dedup,
staff contact cards, and tenant isolation (Jul 2026).
"""
from __future__ import annotations

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
from modules.ai.brain.commerce.location_link_policy import (  # noqa: E402
    evaluate_location_link_policy,
)
from modules.ai.postprocess.safety_nets import _build_location_reply  # noqa: E402
from modules.operations.branch_arrival_keyword_evidence import (  # noqa: E402
    TRIGGER_NO_RESPONSE,
    match_branch_trigger,
)

TENANT_A = 801
TENANT_B = 802
MAPS_A = "https://maps.app.goo.gl/tenant-a-branch"
MAPS_B = "https://maps.app.goo.gl/tenant-b-branch"
PHONE_A = "966511100001"
PHONE_B = "966522200002"
BRANCH_A = "معرض الرياض"
BRANCH_B = "فرع جدة"
CUSTOMER_PHONE = "966500009901"
MSG_LOCATION = "وين موقعكم"
MSG_ON_THE_WAY = "انا في الطريق"
MSG_NO_ONE = "مافيه احد"
_LONG_LOCATION_MARKERS = (
    "اضغط الزر لفتح الموقع",
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


class _StubConv:
    def __init__(self) -> None:
        self.extra_metadata = {"brain_state": {"turn": 1, "order_prep": {}}}
        self.id = 1


def _install_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_STRUCTURED_BRANCH_CONTACTS", "1")
    monkeypatch.setenv("LOCATION_LINK_POLICY_ENABLED", "1")

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


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_env(monkeypatch)


def _assert_short_body_no_url_dup(body: str, maps_url: str) -> None:
    assert maps_url not in (body or "")
    assert len((body or "").strip()) < 120
    for marker in _LONG_LOCATION_MARKERS:
        assert marker not in (body or "")


class TestLocationCtaDelivery:
    def test_t1_location_cta_omits_raw_url(self) -> None:
        db = _branch_db(
            TENANT_A, branch_id=1, branch_name=BRANCH_A, maps_url=MAPS_A, phone=PHONE_A,
        )
        decision = evaluate_branch_trigger_routing(
            db, tenant_id=TENANT_A, message=MSG_LOCATION,
        )
        assert decision is not None
        assert decision.maps_url == MAPS_A
        assert decision.use_cta is True
        _assert_short_body_no_url_dup(decision.reply_text, MAPS_A)

    def test_t2_location_fallback_includes_trusted_url(self) -> None:
        text = _build_location_reply(
            MAPS_A,
            branch_name=BRANCH_A,
            has_branch_details=True,
            include_url_in_body=True,
        )
        assert MAPS_A in text
        assert "example" not in text

    def test_t3_soft_arrival_no_duplicate_location_paragraph(self) -> None:
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
        assert first.reply_text.strip() != second.reply_text.strip()
        _assert_short_body_no_url_dup(second.reply_text, MAPS_A)
        assert second.resend_maps is True
        assert second.maps_url == MAPS_A


class TestStaffContactDelivery:
    def test_t4_no_response_routes_contact_card(self) -> None:
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
        assert PHONE_A.replace("966", "") not in (decision.reply_text or "")
        assert "966" not in (decision.reply_text or "")

    def test_t5_contact_fallback_plain_path_when_no_cta(self) -> None:
        text = _build_location_reply(MAPS_A, include_url_in_body=True)
        assert MAPS_A in text


class TestTenantIsolation:
    def test_t6_no_cross_tenant_leakage(self) -> None:
        db_a = _branch_db(
            TENANT_A, branch_id=1, branch_name=BRANCH_A, maps_url=MAPS_A, phone=PHONE_A,
        )
        db_b = _branch_db(
            TENANT_B, branch_id=2, branch_name=BRANCH_B, maps_url=MAPS_B, phone=PHONE_B,
        )
        dec_a = evaluate_branch_trigger_routing(
            db_a, tenant_id=TENANT_A, message=MSG_LOCATION,
        )
        dec_b = evaluate_branch_trigger_routing(
            db_b, tenant_id=TENANT_B, message=MSG_LOCATION,
        )
        assert dec_a is not None and dec_b is not None
        assert dec_a.maps_url == MAPS_A
        assert dec_b.maps_url == MAPS_B
        assert BRANCH_B not in dec_a.reply_text
        assert BRANCH_A not in dec_b.reply_text
        assert MAPS_B not in dec_a.reply_text
        assert MAPS_A not in dec_b.reply_text


class TestRepeatedTurns:
    def test_t7_second_turn_shorter_than_first(self) -> None:
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
        assert BRANCH_A not in second.reply_text
        _assert_short_body_no_url_dup(second.reply_text, MAPS_A)
        for marker in _LONG_LOCATION_MARKERS:
            assert marker not in second.reply_text


class TestLocationLinkPolicyParity:
    def test_location_policy_cta_matches_branch_router(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "modules.ai.postprocess.safety_nets._lookup_tenant_maps_url",
            lambda _db, _tid: (MAPS_A, "snapshot"),
        )
        decision = evaluate_location_link_policy(
            object(), tenant_id=TENANT_A, message=MSG_LOCATION,
        )
        assert decision is not None
        assert decision.use_cta is True
        assert decision.maps_url == MAPS_A
        _assert_short_body_no_url_dup(decision.reply_text, MAPS_A)
