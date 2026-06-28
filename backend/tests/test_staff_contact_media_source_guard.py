"""PR-D5 — OCR/vision caption must not drive staff/contact routing."""
from __future__ import annotations

import sys
import types as _types
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock, patch

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
from modules.ai.brain.commerce.staff_contact_media_source_guard import (  # noqa: E402
    is_media_framed_inbound_message,
    staff_contact_intent_message,
)
from modules.ai.brain.commerce.staff_contact_policy import (  # noqa: E402
    evaluate_staff_contact_policy,
)
from modules.ai.brain.commerce.staff_contact_target_continuity import (  # noqa: E402
    capture_pending_target_from_inbound,
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


def _merchant_sections() -> List[_Section]:
    return [
        _Section(id=10, kind="custom", body="أمين: 0501111111"),
        _Section(id=20, kind="custom", body="هشام: 0549815590"),
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


_TIKTOK_VISION = (
    "[وصف الصورة المرسلة] لقطة شاشة من تيك توك تظهر Teddy&Abuk "
    "مع نص Get ready with us skincare edition"
)

_TIKTOK_VISION_WITH_CONTACT_WORDS = (
    "[وصف الصورة] محتوى تيك توك Teddy&Abuk تواصل مع Abuk "
    "Get ready with us skincare edition"
)


def test_media_framing_detected_for_image_vision_only() -> None:
    assert is_media_framed_inbound_message(_TIKTOK_VISION) is True
    assert staff_contact_intent_message(_TIKTOK_VISION) == ""


def test_image_caption_name_handle_does_not_trigger_staff_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB(_merchant_sections())
    reg = compile_staff_contact_registry(_merchant_sections())

    assert classify_staff_contact_request(_TIKTOK_VISION, registry=reg).kind == "none"
    assert evaluate_staff_contact_policy(
        db, tenant_id=33, message=_TIKTOK_VISION, customer_phone="966500000000",
    ) is None


def test_ocr_social_caption_does_not_reply_name_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB(_merchant_sections())
    for msg in (_TIKTOK_VISION, _TIKTOK_VISION_WITH_CONTACT_WORDS):
        decision = evaluate_staff_contact_policy(
            db, tenant_id=33, message=msg, customer_phone="966500000000",
        )
        if decision is not None:
            assert MSG_NAME_NOT_CONFIGURED not in decision.reply_text


def test_image_caption_alone_does_not_create_pending_contact_target() -> None:
    reg = compile_staff_contact_registry(_merchant_sections())
    assert capture_pending_target_from_inbound(_TIKTOK_VISION, registry=reg) is None
    assert capture_pending_target_from_inbound(
        _TIKTOK_VISION_WITH_CONTACT_WORDS,
        registry=reg,
    ) is None


def test_explicit_contact_in_customer_caption_after_vision_still_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB(_merchant_sections())
    msg = f"أبي رقم هشام\n\n{_TIKTOK_VISION}"
    decision = evaluate_staff_contact_policy(
        db, tenant_id=33, message=msg, customer_phone="966500000000",
    )
    assert decision is not None
    assert decision.deliver_contact is True
    assert decision.staff_target_tier == "named_person"
    assert MSG_NAME_NOT_CONFIGURED not in decision.reply_text


def test_known_staff_name_in_direct_text_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB(_merchant_sections())

    number_ask = evaluate_staff_contact_policy(
        db, tenant_id=33, message="أبي رقم هشام", customer_phone="966500000000",
    )
    assert number_ask is not None
    assert number_ask.deliver_contact is True
    assert number_ask.staff_target_tier == "named_person"

    from modules.ai.brain.commerce.staff_contact_product_label_guard import (  # noqa: PLC0415
        is_staff_or_contact_context,
    )

    assert is_staff_or_contact_context("من أمين؟") is True
    assert evaluate_staff_contact_policy(
        db, tenant_id=33, message="من أمين؟", customer_phone="966500000000",
    ) is None


def test_unknown_name_with_explicit_contact_gets_not_configured_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB(_merchant_sections())
    decision = evaluate_staff_contact_policy(
        db,
        tenant_id=33,
        message="أبي رقم شخص غير موجود",
        customer_phone="966500000000",
    )
    assert decision is not None
    assert decision.reply_text == MSG_NAME_NOT_CONFIGURED
    assert decision.staff_target_tier == "named_person"

    assert evaluate_staff_contact_policy(
        db,
        tenant_id=33,
        message=_TIKTOK_VISION,
        customer_phone="966500000000",
    ) is None


@patch("core.order_flow._load_brain_state")
def test_image_then_explicit_contact_followup_on_next_turn(
    mock_brain: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB(_merchant_sections())
    brain_state = {
        "turn": 5,
        "order_prep": {
            "pending_contact_target": {
                "lookup_name": "هشام",
                "display_name": "هشام",
                "role": "showroom",
                "source": "contact_delivered",
                "confidence": 0.98,
                "created_turn": 4,
                "expires_after_turns": 3,
            },
        },
    }
    conv = MagicMock()
    conv.extra_metadata = {"brain_state": brain_state}
    mock_brain.return_value = (conv, brain_state)

    decision = evaluate_staff_contact_policy(
        db,
        tenant_id=33,
        message="ارسل رقمه",
        customer_phone="966500000000",
    )
    assert decision is not None
    assert decision.deliver_contact is True
    assert MSG_NAME_NOT_CONFIGURED not in decision.reply_text
