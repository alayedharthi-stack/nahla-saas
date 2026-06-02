"""Pre-deploy gate: payment artifact filtering + explicit text asks."""
from __future__ import annotations

import pytest

from modules.ai.brain.commerce.conversational_priority import (
    has_payment_outbound_consent,
)
from modules.ai.brain.commerce.customer_origin_intent import (
    attachment_is_payment_artifact,
    customer_origin_has_payment_request,
    filter_payment_media_attachments,
    split_inbound_text,
)
from modules.ai.brain.decision.payment_barcode_routing import (
    apply_payment_barcode_image_route,
)


_PAYMENT_RAJHI = {"id": 42, "media_key": "payment_rajhi_barcode", "title": "باركود الراجحي"}
_STORE_CERT = {"id": 7, "media_key": "store_certificate", "title": "شهادة المتجر"}


class _FakeMediaItem:
    def __init__(self, *, id, tenant_id, media_key, title="X"):
        self.id = id
        self.tenant_id = tenant_id
        self.media_key = media_key
        self.title = title
        self.media_type = "image"
        self.file_url = "https://x/y"
        self.mime_type = "image/png"
        self.storage_kind = "external"
        self.storage_path = None
        self.file_size_bytes = None
        self.is_active = True


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


def _ocr_only_meta() -> dict:
    return {
        "source_type": "image",
        "vision_text": "تحويل الراجحي iban باركود",
        "image_kind": "payment_pending_evidence",
    }


def _simulate_media_key_filter(customer_text: str, meta: dict) -> list:
    """Mirror whatsapp_webhook MEDIA_KEY post-extract gate."""
    split = split_inbound_text(customer_text, inbound_metadata=meta, normalized_type="image")
    allow = customer_origin_has_payment_request(
        split.customer_origin,
        inbound_metadata=meta,
        normalized_type="image",
    )
    resolved = [_PAYMENT_RAJHI, _STORE_CERT]
    return filter_payment_media_attachments(resolved, allow_payment=allow)


def _simulate_media_id_filter(customer_text: str, meta: dict) -> list:
    """Mirror whatsapp_webhook legacy [MEDIA:id] post-extract gate."""
    split = split_inbound_text(customer_text, inbound_metadata=meta, normalized_type="image")
    allow = customer_origin_has_payment_request(
        split.customer_origin,
        inbound_metadata=meta,
        normalized_type="image",
    )
    legacy = [
        {
            "id": 99,
            "title": "باركود التحويل البنكي الراجحي",
            "media_key": "payment_rajhi_barcode",
        },
        {"id": 8, "title": "صورة المنتج", "media_key": "product_photo"},
    ]
    return filter_payment_media_attachments(legacy, allow_payment=allow)


class TestPaymentArtifactFiltering:
    def test_media_key_path_blocks_payment_rajhi_barcode_on_ocr_only(self):
        meta = _ocr_only_meta()
        msg = "[وصف الصورة المرسلة] تحويل الراجحي iban باركود"
        out = _simulate_media_key_filter(msg, meta)
        keys = [a.get("media_key") for a in out]
        assert "payment_rajhi_barcode" not in keys
        assert attachment_is_payment_artifact(_PAYMENT_RAJHI)

    def test_media_id_path_blocks_payment_rajhi_barcode_on_ocr_only(self):
        meta = _ocr_only_meta()
        msg = "[وصف الصورة المرسلة] تحويل الراجحي iban باركود"
        out = _simulate_media_id_filter(msg, meta)
        keys = [a.get("media_key") for a in out]
        assert "payment_rajhi_barcode" not in keys

    def test_media_id_blocks_title_only_payment_asset_without_media_key(self):
        meta = _ocr_only_meta()
        msg = "[وصف الصورة المرسلة] iban الراجحي"
        split = split_inbound_text(msg, inbound_metadata=meta, normalized_type="image")
        allow = customer_origin_has_payment_request(split.customer_origin, inbound_metadata=meta)
        legacy = [{"id": 99, "title": "باركود التحويل البنكي الراجحي"}]
        out = filter_payment_media_attachments(legacy, allow_payment=allow)
        assert out == []


@pytest.mark.parametrize(
    "customer_text",
    [
        "كيف أدفع؟",
        "أرسل الباركود",
        "أرسل الحساب",
    ],
)
class TestExplicitTextRequestsStillDispatch:
    def test_consent_granted(self, customer_text: str):
        assert has_payment_outbound_consent(
            customer_text,
            inbound_metadata={"source_type": "text"},
            normalized_type="text",
            tenant_id=33,
            route="predeploy_gate",
        )

    def test_media_key_filter_keeps_payment_rajhi_barcode(self, customer_text: str):
        meta = {"source_type": "text"}
        split = split_inbound_text(customer_text, inbound_metadata=meta, normalized_type="text")
        allow = customer_origin_has_payment_request(split.customer_origin)
        out = filter_payment_media_attachments([_PAYMENT_RAJHI], allow_payment=allow)
        assert len(out) == 1
        assert out[0]["media_key"] == "payment_rajhi_barcode"

    def test_barcode_route_queues_asset_for_barcode_ask(self, customer_text: str):
        if customer_text != "أرسل الباركود":
            pytest.skip("barcode image route is barcode-specific")
        session = _FakeSession([
            _FakeMediaItem(id=42, tenant_id=33, media_key="payment_rajhi_barcode"),
        ])
        media_attachments: list = []
        result = apply_payment_barcode_image_route(
            session,
            tenant_id=33,
            customer_msg=customer_text,
            media_attachments=media_attachments,
            reply_text="تفضل",
        )
        assert result.asset_found is True
        assert result.media_key == "payment_rajhi_barcode"
        assert media_attachments[0]["media_key"] == "payment_rajhi_barcode"

    def test_find_best_payment_asset_for_account_asks(self, customer_text: str):
        if customer_text not in {"كيف أدفع؟", "أرسل الحساب"}:
            pytest.skip("asset finder check for account/pay asks")
        from core.ai_libraries import find_best_payment_asset, is_payment_query

        assert is_payment_query(customer_text)
        session = _FakeSession([
            _FakeMediaItem(
                id=42,
                tenant_id=33,
                media_key="payment_rajhi_barcode",
                title="باركود التحويل البنكي الراجحي",
            ),
        ])
        # AIMediaItem duck-type: add tags/usage_context attrs
        row = session._rows[0]
        row.tags = ["راجحي", "باركود", "تحويل"]
        row.usage_context = "payment"
        row.priority = 10
        asset = find_best_payment_asset(session, 33, customer_text)
        assert asset is not None
        assert asset.get("id") == 42
