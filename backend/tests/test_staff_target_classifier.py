"""Regression tests — staff target classification boundary (named vs generic role)."""
from __future__ import annotations

import sys
import types as _types
from pathlib import Path
from typing import Any, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: E402
    MSG_NAME_NOT_CONFIGURED,
    classify_staff_contact_request,
    compile_staff_contact_registry,
)
from modules.ai.brain.commerce.staff_contact_policy import (  # noqa: E402
    evaluate_staff_contact_policy,
)
from modules.ai.brain.commerce.staff_target_classifier import (  # noqa: E402
    classify_staff_target,
)


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

    def query(self, _model: Any) -> Any:
        return _Query(self._sections)


class _Query:
    def __init__(self, sections: List[_Section]) -> None:
        self._sections = list(sections)

    def filter(self, *args: Any, **kwargs: Any) -> Any:
        return self

    def order_by(self, *args: Any, **kwargs: Any) -> Any:
        return self

    def limit(self, _n: int) -> Any:
        return self

    def all(self) -> List[_Section]:
        return list(self._sections)

    def first(self) -> None:
        return None


def _merchant_sections() -> List[_Section]:
    return [
        _Section(
            id=10,
            kind="escalation_rules",
            body="عند الوصول للمعرض يُنسّق مع البائع.",
        ),
        _Section(
            id=20,
            kind="custom",
            body="بائع المعرض — 0541690226",
        ),
        _Section(
            id=30,
            kind="escalation_rules",
            body="هشام: 0549815590",
        ),
        _Section(
            id=40,
            kind="custom",
            body="أمين: 0501111111",
        ),
    ]


def _install_call_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    call_stub = _types.ModuleType("services.call_resolver")

    class _CallTarget:
        def __init__(self, name: str, wa_id: str, phone_display: str, raw_phone: str) -> None:
            self.name = name
            self.wa_id = wa_id
            self.phone_display = phone_display
            self.raw_phone = raw_phone

    def _fake_normalize(phone: str) -> str:
        digits = "".join(c for c in str(phone or "") if c.isdigit())
        if digits.startswith("966"):
            return digits
        if digits.startswith("0"):
            return "966" + digits[1:]
        return digits

    call_stub.CallTarget = _CallTarget  # type: ignore[attr-defined]
    call_stub._normalize_saudi_phone = _fake_normalize  # type: ignore[attr-defined]
    call_stub._pretty_phone = lambda w: w  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "services.call_resolver", call_stub)
    monkeypatch.setenv("STAFF_CONTACT_POLICY_ENABLED", "1")
    monkeypatch.setenv("USE_STRUCTURED_BRANCH_CONTACTS", "0")


class TestStaffTargetClassifier:
    @pytest.mark.parametrize(
        ("message", "expected_tier"),
        [
            ("أبي رقم العامل", "generic_role"),
            ("أبي رقم البائع", "generic_role"),
            ("أبي رقم أمين", "named_person"),
            ("أبي رقم هشام", "named_person"),
            ("أبي رقم شخص غير موجود", "named_person"),
        ],
    )
    def test_span_tier_classification(self, message: str, expected_tier: str) -> None:
        from modules.ai.brain.commerce.entity_extraction_guard import (  # noqa: PLC0415
            extract_staff_name_candidate,
        )

        span = extract_staff_name_candidate(message)
        assert span
        verdict = classify_staff_target(message, raw_span=span)
        assert verdict.tier == expected_tier

    def test_collective_message_is_generic_role(self) -> None:
        msg = "أكلم واحد من العاملين"
        verdict = classify_staff_target(msg)
        assert verdict.tier == "generic_role"
        assert verdict.reason == "structure:collective_reference"


class TestStaffContactRequestMapping:
    def test_role_number_ask_maps_to_generic_staff(self) -> None:
        req = classify_staff_contact_request("أبي رقم العامل")
        assert req.kind == "generic_staff"
        assert req.target_tier == "generic_role"

    def test_named_person_maps_to_named(self) -> None:
        req = classify_staff_contact_request("أبي رقم هشام")
        assert req.kind == "named"
        assert req.target_tier == "named_person"

    def test_verbal_role_still_generic_staff(self) -> None:
        req = classify_staff_contact_request("ابي أكلم البائع")
        assert req.kind == "generic_staff"

    def test_collective_phrase_generic_staff(self) -> None:
        req = classify_staff_contact_request("أكلم واحد من العاملين")
        assert req.kind == "generic_staff"
        assert req.target_tier == "generic_role"


class TestStaffContactPolicyRegression:
    @pytest.mark.parametrize(
        "message",
        [
            "أبي رقم العامل",
            "أبي رقم البائع",
            "ابي أكلم البائع",
            "أكلم واحد من العاملين",
        ],
    )
    def test_generic_staff_never_name_stub(
        self,
        message: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_call_resolver(monkeypatch)
        db = _StubDB(_merchant_sections())
        decision = evaluate_staff_contact_policy(db, tenant_id=33, message=message)
        assert decision is not None
        assert MSG_NAME_NOT_CONFIGURED not in decision.reply_text

    def test_generic_role_delivers_showroom_contact(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_call_resolver(monkeypatch)
        db = _StubDB(_merchant_sections())
        decision = evaluate_staff_contact_policy(
            db, tenant_id=33, message="أبي رقم العامل",
        )
        assert decision is not None
        assert decision.deliver_contact is True
        assert decision.call_target is not None
        assert decision.staff_target_tier == "generic_role"
        assert decision.staff_target_reason == "structure:numbered_role_slot"

    def test_configured_named_contact_delivers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_call_resolver(monkeypatch)
        db = _StubDB(_merchant_sections())
        decision = evaluate_staff_contact_policy(
            db, tenant_id=33, message="أبي رقم أمين",
        )
        assert decision is not None
        assert decision.deliver_contact is True
        assert decision.staff_target_tier == "named_person"

    def test_unknown_named_person_gets_name_stub(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_call_resolver(monkeypatch)
        db = _StubDB(_merchant_sections())
        decision = evaluate_staff_contact_policy(
            db, tenant_id=33, message="أبي رقم شخص غير موجود",
        )
        assert decision is not None
        assert decision.reply_text == MSG_NAME_NOT_CONFIGURED
        assert decision.staff_target_tier == "named_person"
        assert not decision.deliver_contact

    def test_configured_showroom_label_path_preserved(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_call_resolver(monkeypatch)
        db = _StubDB(_merchant_sections())
        decision = evaluate_staff_contact_policy(
            db, tenant_id=33, message="رقم بائع المعرض",
        )
        assert decision is not None
        assert decision.deliver_contact is True
        assert decision.reason == "named_match"

    def test_named_miss_hisham_still_name_stub(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_call_resolver(monkeypatch)
        db = _StubDB([
            _Section(id=1, kind="custom", body="أمين: 0501111111"),
        ])
        decision = evaluate_staff_contact_policy(
            db, tenant_id=33, message="أبي رقم هشام",
        )
        assert decision is not None
        assert decision.reply_text == MSG_NAME_NOT_CONFIGURED
        assert decision.staff_target_tier == "named_person"

    def test_arrival_defers_staff_classify(self) -> None:
        req = classify_staff_contact_request("أنا جاي")
        assert req.kind == "none"
