"""Religious آمين must not trigger showroom staff «أمين» routing."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.staff_ameen_disambiguation import (  # noqa: E402
    has_explicit_staff_ameen_intent,
    is_religious_ameen_context,
    staff_name_token_allowed,
)
from test_staff_contact_kb_scan import (  # noqa: E402
    _StubKBSection,
    _install_stubs,
)


class TestReligiousAmeenDetection:
    @pytest.mark.parametrize(
        "message",
        (
            "آمين",
            "آمين يا رب",
            "اللهم آمين",
            "جزاك الله خير، آمين",
            "امين يا رب",
        ),
    )
    def test_religious_phrases_detected(self, message):
        assert is_religious_ameen_context(message) is True
        assert has_explicit_staff_ameen_intent(message) is False

    @pytest.mark.parametrize(
        "message",
        (
            "أبغى أمين",
            "كلم أمين",
            "رقم أمين",
            "أمين المعرض",
            "بائع المعرض",
            "ابي اكلم أمين",
            "أمين مايرد",
        ),
    )
    def test_explicit_staff_intent(self, message):
        assert is_religious_ameen_context(message) is False
        assert has_explicit_staff_ameen_intent(message) is True

    @pytest.mark.parametrize(
        "message",
        (
            "آمين يا رب",
            "اللهم آمين",
        ),
    )
    def test_ameen_staff_token_blocked(self, message):
        assert staff_name_token_allowed(message, "أمين") is False
        assert staff_name_token_allowed(message, "امين") is False

    def test_staff_token_allowed_with_explicit_ask(self):
        assert staff_name_token_allowed("ابي اكلم أمين", "أمين") is True


class TestReligiousAmeenSafetyNet:
    def test_ameen_yarabb_never_routes_showroom_contact(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

        db = _install_stubs(
            monkeypatch,
            sections=[
                _StubKBSection(
                    section_id=1,
                    kind="branches",
                    body="أمين بائع المعرض: 0541690226",
                ),
            ],
        )
        result = apply_staff_contact_safety_net(
            customer_msg="آمين يا رب",
            reply_text="تقدر تتواصل مع أمين بائع المعرض",
            existing_call_targets=[],
            detected_call_markers=0,
            db=db,
            tenant_id=33,
        )
        assert result.fired is False
        assert result.skipped_reason == "religious_ameen_context"

    @pytest.mark.parametrize(
        "customer_msg",
        (
            "آمين",
            "اللهم آمين",
            "جزاك الله خير، آمين",
        ),
    )
    def test_religious_variants_skip_staff_net(
        self,
        customer_msg: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

        db = _install_stubs(
            monkeypatch,
            sections=[
                _StubKBSection(
                    section_id=2,
                    kind="branches",
                    body="أمين - 0541690226",
                ),
            ],
        )
        result = apply_staff_contact_safety_net(
            customer_msg=customer_msg,
            reply_text="تواصل مع أمين بائع المعرض",
            existing_call_targets=[],
            detected_call_markers=0,
            db=db,
            tenant_id=33,
        )
        assert result.fired is False
        assert result.skipped_reason == "religious_ameen_context"

    def test_explicit_staff_ask_still_routes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

        db = _install_stubs(
            monkeypatch,
            sections=[
                _StubKBSection(
                    section_id=3,
                    kind="branches",
                    body="أمين بائع المعرض: 0541690226",
                ),
            ],
        )
        result = apply_staff_contact_safety_net(
            customer_msg="ابي اكلم أمين",
            reply_text="تواصل مع أمين بائع المعرض",
            existing_call_targets=[],
            detected_call_markers=0,
            db=db,
            tenant_id=33,
        )
        assert result.fired is True
