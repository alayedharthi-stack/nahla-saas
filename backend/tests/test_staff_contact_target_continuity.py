"""PR-C2 — staff/contact target continuity across turns."""
from __future__ import annotations

import os
import sys
import types as _types
from typing import Any, List

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.entity_extraction_guard import (  # noqa: E402
    is_general_contact_numbers_request,
)
from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: E402
    classify_staff_contact_request,
)
from modules.ai.brain.commerce.staff_contact_policy import (  # noqa: E402
    evaluate_staff_contact_policy,
)
from modules.ai.brain.commerce.staff_contact_target_continuity import (  # noqa: E402
    DEFAULT_EXPIRES_AFTER_TURNS,
    PendingContactTarget,
    is_contact_target_followup,
    is_stale_pending_target,
    load_pending_contact_target,
    should_clear_pending_on_topic_switch,
)

CONFIGURED_STAFF = "خالد"
CONFIGURED_PHONE = "0503334455"


class _Section:
    def __init__(self, *, id: int, kind: str, body: str, title: str = "") -> None:
        self.id = id
        self.kind = kind
        self.body = body
        self.title = title
        self.metadata = {}
        self.metadata_json = {}
        self.updated_at = id


class _StubConv:
    def __init__(self, brain_state: dict) -> None:
        self.extra_metadata: dict = {"brain_state": brain_state}
        self.id = 1


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


class _StubDB:
    def __init__(
        self,
        sections: List[_Section] | None = None,
        brain_state: dict | None = None,
    ) -> None:
        self._sections = list(sections or [])
        self._brain_state = dict(brain_state or {})
        self._conv = _StubConv(self._brain_state)

    def query(self, _model: Any) -> "_Query":
        return _Query(self._sections)

    def add(self, _obj: object) -> None:
        pass

    def flush(self) -> None:
        pass

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def _staff_sections(
    *,
    staff_name: str = CONFIGURED_STAFF,
    phone: str = CONFIGURED_PHONE,
    role_label: str = "",
) -> List[_Section]:
    sections = [
        _Section(
            id=1,
            kind="custom",
            body=f"{staff_name}: {phone}",
        ),
    ]
    if role_label:
        sections.append(
            _Section(
                id=2,
                kind="custom",
                body=f"{role_label}: {phone}",
            ),
        )
    return sections


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


def _make_db(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sections: List[_Section],
    brain_state: dict,
) -> _StubDB:
    db = _StubDB(sections, brain_state)
    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda _db, *, tenant_id, phone: (db._conv, dict(db._brain_state)),
    )
    return db


def _pending_target(
    *,
    lookup: str = CONFIGURED_STAFF,
    display: str = CONFIGURED_STAFF,
    role: str = "showroom",
    created_turn: int = 5,
    expires: int = DEFAULT_EXPIRES_AFTER_TURNS,
) -> dict:
    return PendingContactTarget(
        lookup_name=lookup,
        display_name=display,
        role=role,
        source="test_fixture",
        confidence=0.95,
        created_turn=created_turn,
        expires_after_turns=expires,
    ).to_dict()


class TestStaffContactTargetContinuity:
    def test_t1_pronoun_resolves_to_previous_staff_target(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_call_resolver(monkeypatch)
        db = _make_db(
            monkeypatch,
            sections=_staff_sections(),
            brain_state={
                "turn": 7,
                "order_prep": {"pending_contact_target": _pending_target()},
            },
        )
        decision = evaluate_staff_contact_policy(
            db,
            tenant_id=10,
            message="ارسل رقمه",
            customer_phone="966500000001",
        )
        assert decision is not None
        assert decision.request_kind == "named"
        assert decision.request_kind != "general_channel"
        assert decision.deliver_contact is True
        assert decision.call_target is not None
        assert CONFIGURED_STAFF in (decision.call_target.name or "")

    def test_t2_waineh_resolves_target(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_call_resolver(monkeypatch)
        db = _make_db(
            monkeypatch,
            sections=_staff_sections(),
            brain_state={
                "turn": 4,
                "order_prep": {"pending_contact_target": _pending_target(created_turn=3)},
            },
        )
        assert is_contact_target_followup("وينه؟") is True
        decision = evaluate_staff_contact_policy(
            db,
            tenant_id=10,
            message="وينه؟",
            customer_phone="966500000001",
        )
        assert decision is not None
        assert decision.request_kind == "named"
        assert decision.request_kind != "general_channel"

    def test_t3_named_staff_request_without_pending(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_call_resolver(monkeypatch)
        db = _make_db(
            monkeypatch,
            sections=_staff_sections(),
            brain_state={"turn": 2, "order_prep": {}},
        )
        msg = f"أرسل رقم {CONFIGURED_STAFF}"
        decision = evaluate_staff_contact_policy(
            db,
            tenant_id=10,
            message=msg,
            customer_phone="966500000001",
        )
        assert decision is not None
        assert decision.request_kind == "named"
        assert decision.deliver_contact is True
        assert decision.call_target is not None

    def test_t4_general_contact_stays_general_without_target(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_call_resolver(monkeypatch)
        db = _make_db(
            monkeypatch,
            sections=_staff_sections(),
            brain_state={"turn": 2, "order_prep": {}},
        )
        msg = "ارسل الأرقام لاهنت"
        assert is_general_contact_numbers_request(msg) is True
        req = classify_staff_contact_request(msg)
        assert req.kind == "general_channel"
        decision = evaluate_staff_contact_policy(
            db,
            tenant_id=10,
            message=msg,
            customer_phone="966500000001",
        )
        assert decision is not None
        assert decision.request_kind == "general_channel"
        assert decision.deliver_contact is False
        assert decision.call_target is None

    def test_t5_stale_target_does_not_resolve(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_call_resolver(monkeypatch)
        stale = _pending_target(created_turn=1, expires=3)
        brain = {"turn": 10, "order_prep": {"pending_contact_target": stale}}
        _make_db(monkeypatch, sections=_staff_sections(), brain_state=brain)
        target = load_pending_contact_target(
            brain["order_prep"],
            current_turn=10,
            brain_state=brain,
            registry=None,
        )
        assert target is None
        parsed = PendingContactTarget.from_dict(stale)
        assert parsed is not None
        assert is_stale_pending_target(parsed, current_turn=10) is True
        db = _StubDB(_staff_sections(), brain)
        monkeypatch.setattr(
            "core.order_flow._load_brain_state",
            lambda _db, *, tenant_id, phone: (db._conv, dict(db._brain_state)),
        )
        decision = evaluate_staff_contact_policy(
            db,
            tenant_id=10,
            message="ارسل رقمه",
            customer_phone="966500000001",
        )
        assert decision is None or decision.request_kind == "general_channel"
        if decision is not None:
            assert decision.deliver_contact is False

    def test_t6_configured_staff_name_fixture(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_call_resolver(monkeypatch)
        staff = "سالم"
        db = _make_db(
            monkeypatch,
            sections=_staff_sections(staff_name=staff),
            brain_state={
                "turn": 6,
                "order_prep": {
                    "pending_contact_target": _pending_target(
                        lookup=staff,
                        display=staff,
                    ),
                },
            },
        )
        decision = evaluate_staff_contact_policy(
            db,
            tenant_id=10,
            message="ارسل رقمه",
            customer_phone="966500000001",
        )
        assert decision is not None
        assert decision.request_kind == "named"
        assert staff in (getattr(decision.call_target, "name", "") or "")

    def test_t7_no_configured_contact_missing_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_call_resolver(monkeypatch)
        ghost = "غير_موجود"
        db = _make_db(
            monkeypatch,
            sections=[],
            brain_state={
                "turn": 3,
                "order_prep": {
                    "pending_contact_target": _pending_target(
                        lookup=ghost,
                        display=ghost,
                    ),
                },
            },
        )
        decision = evaluate_staff_contact_policy(
            db,
            tenant_id=10,
            message="ارسل رقمه",
            customer_phone="966500000001",
        )
        assert decision is not None
        assert decision.deliver_contact is False
        assert decision.call_target is None

    def test_t6_role_target_from_showroom_label(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_call_resolver(monkeypatch)
        db = _make_db(
            monkeypatch,
            sections=[
                _Section(
                    id=1,
                    kind="custom",
                    body=f"بائع المعرض: {CONFIGURED_PHONE}",
                ),
            ],
            brain_state={
                "turn": 5,
                "order_prep": {
                    "pending_contact_target": _pending_target(
                        lookup="بائع المعرض",
                        display="بائع المعرض",
                        role="showroom",
                    ),
                },
            },
        )
        decision = evaluate_staff_contact_policy(
            db,
            tenant_id=10,
            message="ارسل رقمه",
            customer_phone="966500000001",
        )
        assert decision is not None
        assert decision.request_kind == "named"
        assert decision.deliver_contact is True

    def test_t8_topic_switch_clears_target_and_allows_browse(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        msg = "وش أنواع العسل المتوفرة؟"
        assert should_clear_pending_on_topic_switch(msg) is True
        assert is_contact_target_followup(msg) is False
        req = classify_staff_contact_request(msg)
        assert req.kind != "general_channel"
        assert req.kind == "none"

    def test_pronoun_not_general_channel(self) -> None:
        assert is_general_contact_numbers_request("ارسل رقمه") is False
        assert is_contact_target_followup("ارسل رقمه") is True
