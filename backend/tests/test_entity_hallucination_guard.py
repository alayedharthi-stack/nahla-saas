"""P0 — entity hallucination guard (product labels, staff names, identity/collaboration)."""
from __future__ import annotations

import os
import sys
import types as _types
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
    has_explicit_purchase_intent,
    is_generic_store_contact_phrase,
    is_identity_collaboration_without_purchase,
)
from modules.ai.brain.commerce.identity_collaboration_guard import (  # noqa: E402
    try_identity_collaboration_decision,
)
from modules.ai.brain.commerce.product_label_hygiene import (  # noqa: E402
    is_non_product_label,
)
from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: E402
    MSG_NAME_NOT_CONFIGURED,
    StaffContactRequest,
    classify_staff_contact_request,
    compile_staff_contact_registry,
    resolve_staff_contact,
)
from modules.ai.brain.commerce.staff_contact_policy import (  # noqa: E402
    evaluate_staff_contact_policy,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.postprocess.availability_guard_policy import (  # noqa: E402
    inbound_exempt_from_availability_rewrite,
)
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    _label_from_inbound_availability_ask,
    apply_product_availability_truth_guard,
    build_operational_availability_conflict_reply,
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


class TestProductLabelGuard:
    @pytest.mark.parametrize(
        "message",
        [
            "انا معلم في النحل وحبيت ادوم معاكم",
            "عندي خبره",
            "متى يوصل الطلب",
            "وين الشحنة",
            "أتواصل معكم",
            "كيف أتواصل معاكم؟",
        ],
    )
    def test_non_catalog_inbound_not_product_label(self, message: str) -> None:
        assert is_non_product_label(message) is True
        assert _label_from_inbound_availability_ask(message) == ""

    @pytest.mark.parametrize(
        "message",
        ["ابي عسل طلح", "عسل سمر ١ كيلو"],
    )
    def test_valid_product_inquiry_still_allowed(self, message: str) -> None:
        assert is_non_product_label(message) is False

    def test_incident_availability_reply_has_no_full_inbound_label(self) -> None:
        inbound = "انا معلم في النحل وحبيت ادوم معاكم"
        ev = MagicMock()
        ev.entity.product_id = None
        ev.entity.family_key = "inbound:x"
        reply = build_operational_availability_conflict_reply(
            ev, availability_context={"focus_product": {}, "catalog_skus": []},
            inbound_text=inbound,
        )
        assert inbound not in reply

    def test_shipping_phrase_exempt_from_availability_rewrite(self) -> None:
        assert inbound_exempt_from_availability_rewrite("متى يوصل الطلب") is True


class TestStaffContactGuard:
    @pytest.mark.parametrize(
        "message",
        [
            "أتواصل معكم",
            "أتواصل معاك",
            "أرجع أتواصل معكم بعدين",
            "كيف أتواصل معاكم؟",
            "شكراً بتواصل معكم",
        ],
    )
    def test_generic_contact_not_named(self, message: str) -> None:
        req = classify_staff_contact_request(message)
        assert req.kind in {"none", "general_channel"}
        assert req.kind != "named"

    @pytest.mark.parametrize(
        "message",
        [
            "أتواصل معكم",
            "أتواصل معاك",
            "كيف أتواصل معاكم؟",
        ],
    )
    def test_generic_contact_policy_no_name_not_configured(
        self, message: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_call_resolver(monkeypatch)
        decision = evaluate_staff_contact_policy(
            _StubDB([]), tenant_id=10, message=message,
        )
        if decision is not None:
            assert MSG_NAME_NOT_CONFIGURED not in decision.reply_text

    def test_named_staff_with_candidate(self) -> None:
        assert extract_staff_name_candidate("ابي رقم هشام") == "هشام"

    def test_named_staff_configured(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_call_resolver(monkeypatch)
        db = _StubDB([
            _Section(id=1, kind="custom", body="هشام: 0503333333"),
        ])
        decision = evaluate_staff_contact_policy(
            db, tenant_id=10, message="ارسل رقم هشام",
        )
        assert decision is not None
        assert decision.deliver_contact is True

    def test_named_staff_not_configured_only_with_candidate(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_call_resolver(monkeypatch)
        db = _StubDB([])
        decision = evaluate_staff_contact_policy(
            db, tenant_id=10, message="ارسل رقم هشام",
        )
        assert decision is not None
        assert decision.reply_text == MSG_NAME_NOT_CONFIGURED

    def test_role_contact_routes_generic(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_call_resolver(monkeypatch)
        req = classify_staff_contact_request("ابي أكلم البائع")
        assert req.kind == "generic_staff"
        db = _StubDB([
            _Section(id=1, kind="custom", body="بائع المعرض: 0504444444"),
        ])
        decision = evaluate_staff_contact_policy(
            db, tenant_id=10, message="ابي أكلم البائع",
        )
        assert decision is not None
        assert decision.deliver_contact is True

    def test_general_channel_reply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_call_resolver(monkeypatch)
        decision = evaluate_staff_contact_policy(
            _StubDB([]), tenant_id=10, message="كيف أتواصل معاكم؟",
        )
        assert decision is not None
        assert "تقدر" in decision.reply_text
        assert MSG_NAME_NOT_CONFIGURED not in decision.reply_text


class TestIdentityCollaborationGuard:
    @pytest.mark.parametrize(
        "message",
        [
            "أنا معلم في النحل",
            "عندي خبرة وحاب أداوم معاكم",
        ],
    )
    def test_identity_without_purchase_detected(self, message: str) -> None:
        assert is_identity_collaboration_without_purchase(message) is True

    def test_explicit_purchase_overrides_identity_guard(self) -> None:
        msg = "أنا نحال وأبي أشتري طرود نحل"
        assert has_explicit_purchase_intent(msg) is True
        assert is_identity_collaboration_without_purchase(msg) is False

    def test_decision_engine_blocks_commerce_escalation(self) -> None:
        ctx = MagicMock()
        ctx.message = "أنا معلم في النحل"
        ctx.tenant_id = 1
        decision = try_identity_collaboration_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("block_commerce_escalation") is True
        assert "buy" in str(decision.args.get("response_goal") or "").lower()


class TestOutboundGuard:
    def test_availability_rewrite_blocked_for_identity_inbound(self) -> None:
        prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"
        try:
            inbound = "انا معلم في النحل وحبيت ادوم معاكم"
            bad = f"متوفر {inbound} بعدة خيارات."
            result = apply_product_availability_truth_guard(
                reply=bad,
                availability_context={
                    "catalog_skus": [],
                    "focus_product": None,
                    "kb_signals": [],
                    "kb_links": [],
                },
                inbound_text=inbound,
                tenant_id=1,
            )
            assert result.replaced is False
        finally:
            if prev is None:
                os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
            else:
                os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = prev

    def test_name_not_configured_requires_candidate(self) -> None:
        registry = compile_staff_contact_registry([])
        resolution = resolve_staff_contact(
            registry,
            StaffContactRequest(kind="named"),
            message="أتواصل معكم",
        )
        assert resolution.unknown_name is False
        assert resolution.reason == "no_named_intent"
