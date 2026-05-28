"""Payment barcode image request detection + outbound routing."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


class _FakeMediaItem:
    def __init__(
        self, *, id, tenant_id, media_key,
        title="X", media_type="image", file_url="https://x/y",
        is_active=True,
    ):
        self.id = id
        self.tenant_id = tenant_id
        self.media_key = media_key
        self.title = title
        self.media_type = media_type
        self.file_url = file_url
        self.mime_type = "image/png"
        self.storage_kind = "external"
        self.storage_path = None
        self.file_size_bytes = None
        self.is_active = is_active


class _FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, n):
        self._rows = self._rows[: int(n)]
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, rows):
        self._rows = list(rows)

    def query(self, model):
        return _FakeQuery(self._rows)


@pytest.mark.parametrize(
    "message",
    [
        "الباركود",
        "ارسل باركود الراجحي",
        "QR",
        "صورة الباركود",
        "ارسل صورة التحويل",
    ],
)
def test_is_payment_barcode_image_request_detects_phrases(message: str) -> None:
    from modules.ai.brain.decision.payment_barcode_routing import (
        PAYMENT_BARCODE_IMAGE_REQUEST,
        classify_payment_request,
        is_payment_barcode_image_request,
    )

    assert is_payment_barcode_image_request(message) is True
    assert classify_payment_request(message) == PAYMENT_BARCODE_IMAGE_REQUEST


def test_classify_payment_request_distinguishes_generic_payment_info() -> None:
    from modules.ai.brain.decision.payment_barcode_routing import (
        ASK_PAYMENT_INFO,
        classify_payment_request,
        is_payment_barcode_image_request,
    )

    assert is_payment_barcode_image_request("رقم حساب الراجحي") is False
    assert classify_payment_request("رقم حساب الراجحي") == ASK_PAYMENT_INFO


@pytest.mark.parametrize(
    "message",
    ["الباركود", "ارسل باركود الراجحي", "QR"],
)
def test_apply_payment_barcode_image_route_queues_payment_rajhi_barcode(
    message: str,
) -> None:
    from modules.ai.brain.decision.payment_barcode_routing import (
        PAYMENT_BARCODE_IMAGE_REQUEST,
        apply_payment_barcode_image_route,
    )

    session = _FakeSession([
        _FakeMediaItem(id=42, tenant_id=33, media_key="payment_rajhi_barcode"),
    ])
    media_attachments: list = []
    phone_fallback_reply = "المتوفر حالياً رقم التحويل 0555906901"

    result = apply_payment_barcode_image_route(
        session,
        tenant_id=33,
        customer_msg=message,
        media_attachments=media_attachments,
        reply_text=phone_fallback_reply,
        conversation_id=999,
    )

    assert result.barcode_request_detected is True
    assert result.request_kind == PAYMENT_BARCODE_IMAGE_REQUEST
    assert result.asset_found is True
    assert result.media_key == "payment_rajhi_barcode"
    assert result.media_send_attempted is True
    assert result.fallback_used is False
    assert result.queued_attachment is True
    assert result.rewrote_reply is True
    assert len(media_attachments) == 1
    assert media_attachments[0]["media_key"] == "payment_rajhi_barcode"
    assert media_attachments[0]["payment_barcode_route"] is True


@pytest.mark.parametrize(
    "message",
    ["الباركود", "ارسل باركود الراجحي", "QR"],
)
def test_artifact_guard_skips_phone_fallback_when_barcode_queued(
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.brain.decision.payment_barcode_routing import (
        apply_payment_barcode_image_route,
    )
    from modules.ai.postprocess.safety_nets import apply_outbound_artifact_guard

    session = _FakeSession([
        _FakeMediaItem(id=42, tenant_id=33, media_key="payment_rajhi_barcode"),
    ])
    media_attachments: list = []
    phone_fallback_reply = "تفضل 🌷 الراجحي 0555906901"

    route = apply_payment_barcode_image_route(
        session,
        tenant_id=33,
        customer_msg=message,
        media_attachments=media_attachments,
        reply_text=phone_fallback_reply,
    )
    assert route.queued_attachment is True

    from tests.test_outbound_artifact_guard import _patch_url_lookups

    _patch_url_lookups(monkeypatch)

    guard = apply_outbound_artifact_guard(
        None,
        tenant_id=33,
        customer_msg=message,
        reply_text=phone_fallback_reply,
        media_attachments=media_attachments,
        call_targets=[],
    )

    assert guard.expected_artifact == "payment_barcode"
    assert guard.artifact_satisfied is True
    assert guard.fired is False
    assert guard.skipped_reason == "artifact_already_present"


def test_engine_routes_barcode_image_request_with_payment_request_kind() -> None:
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.decision.payment_barcode_routing import (
        PAYMENT_BARCODE_IMAGE_REQUEST,
    )
    from modules.ai.brain.types import (
        BrainContext,
        CommerceFacts,
        INTENT_ASK_PAYMENT_INFO,
        Intent,
        MerchantConversationState,
    )

    state = MerchantConversationState()
    state.greeted = True
    ctx = BrainContext(
        tenant_id=33,
        customer_phone="+966500000000",
        message="الباركود",
        intent=Intent(name=INTENT_ASK_PAYMENT_INFO, confidence=0.9, raw_message="الباركود"),
        state=state,
        facts=CommerceFacts(has_products=True, product_count=1, orderable=True),
    )

    decision = DefaultDecisionEngine().decide(ctx)

    assert decision.args["payment_request_kind"] == PAYMENT_BARCODE_IMAGE_REQUEST
    assert decision.args["topic"] == "payment_barcode_image"
