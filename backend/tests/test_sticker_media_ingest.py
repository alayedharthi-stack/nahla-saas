"""
Regression: WhatsApp stickers must flow through vision + social routing.

Before May 2026 stickers hit ``INBOUND_IGNORED_UNSUPPORTED`` at the
webhook allow-list and never reached the brain — even when the sticker
carried readable gratitude text like «جزاك الله خيراً».
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_SEARCH_PRODUCTS,
    ACTION_SOCIAL_REPLY,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent.non_commerce_classifier import (  # noqa: E402
    NON_COMMERCE_STICKER_TAG,
    classify_non_commerce,
    commerce_escalation_allowed,
)
from modules.ai.brain.intent.social_classifier import SOCIAL_THANKS  # noqa: E402
from modules.ai.brain.intent import rules as intent_rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    INTENT_SOCIAL,
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)
from modules.ai.media.routing_guard import (  # noqa: E402
    should_skip_contact_routing_for_media,
)
from services.catalog_product_orchestrator import (  # noqa: E402
    REASON_NON_COMMERCE_BLOCKED,
    evaluate_product_card_send,
)


def _run(coro):
    return asyncio.run(coro)


_GRATITUDE_VISION = "النص المرئي: جزاك الله خيراً"
_EXPRESSIVE_VISION = "ملصق تعبيري (وجه مبتسم) بدون نص مكتوب"


@pytest.fixture
def sticker_mocks(monkeypatch: pytest.MonkeyPatch):
    download = AsyncMock(
        return_value={
            "bytes": b"RIFF....WEBP",
            "mime_type": "image/webp",
        },
    )
    stored = MagicMock()
    stored.storage_url = "/media/inbound/33/sticker.webp"
    stored.storage_sha256 = "abc"
    stored.byte_size = 42
    stored.mime_type = "image/webp"
    save = MagicMock(return_value=stored)
    vision = AsyncMock()

    monkeypatch.setattr(
        "modules.ai.media.normalizer._download_meta_media",
        download,
    )
    monkeypatch.setattr(
        "services.inbound_media_storage.save_inbound_media",
        save,
    )
    monkeypatch.setattr(
        "modules.ai.media.normalizer._describe_sticker_with_openai",
        vision,
    )
    monkeypatch.setattr(
        "modules.ai.media.normalizer._runtime_openai_key",
        lambda: "test-key",
    )
    return {"download": download, "save": save, "vision": vision}


def _normalize_sticker(vision_text: str, *, sticker_mocks) -> Any:
    from modules.ai.media.normalizer import normalize_whatsapp_inbound

    sticker_mocks["vision"].return_value = vision_text
    return _run(
        normalize_whatsapp_inbound(
            db=MagicMock(),
            wa_conn=MagicMock(),
            tenant_id=33,
            message={
                "id": "wamid.sticker.1",
                "type": "sticker",
                "timestamp": "1710000000",
                "sticker": {
                    "id": "MEDIA_STICKER_1",
                    "mime_type": "image/webp",
                    "sha256": "abc",
                    "animated": False,
                },
            },
        ),
    )


def _brain_ctx(message: str, *, metadata: Dict[str, Any] | None = None) -> BrainContext:
    intent = intent_rules.match(message) or Intent(
        name=INTENT_SOCIAL,
        confidence=0.94,
        slots={"social_category": SOCIAL_THANKS},
        raw_message=message,
    )
    meta = dict(metadata or {})
    ctx = BrainContext(
        tenant_id=33,
        customer_phone="966500000000",
        message=message,
        intent=intent,
        state=MerchantConversationState(greeted=True),
        facts=CommerceFacts(has_products=True, orderable=True, store_name="Test"),
        block_commerce_escalation=bool(meta.get("block_commerce_escalation")),
        non_commerce_category=str(meta.get("non_commerce_category") or ""),
    )
    if meta.get("block_commerce_escalation") and not ctx.non_commerce_category:
        ctx.non_commerce_category = SOCIAL_THANKS
    return ctx


class TestStickerNormalizer:
    def test_sticker_downloads_webp_and_reaches_brain(self, sticker_mocks) -> None:
        result = _normalize_sticker(_GRATITUDE_VISION, sticker_mocks=sticker_mocks)

        assert result.normalized_type == "sticker"
        assert result.should_process is True
        assert "جزاك الله خير" in result.text
        assert result.metadata["source_type"] == "sticker"
        assert result.metadata["mime_type"] == "image/webp"
        assert result.metadata["sticker_download_status"] == "ok"
        assert result.metadata["sticker_kind"] == "text"
        sticker_mocks["download"].assert_awaited_once()
        sticker_mocks["vision"].assert_awaited_once()

    def test_expressive_sticker_blocks_commerce_without_product_signal(
        self, sticker_mocks,
    ) -> None:
        result = _normalize_sticker(_EXPRESSIVE_VISION, sticker_mocks=sticker_mocks)

        assert result.normalized_type == "sticker"
        assert result.should_process is True
        assert result.metadata["sticker_kind"] == "expressive_only"
        assert result.metadata["block_commerce_escalation"] is True
        assert classify_non_commerce(result.text, media_type="sticker") is not None
        assert not commerce_escalation_allowed(
            result.text,
            inbound_metadata=result.metadata,
        )


class TestStickerSocialRouting:
    def test_gratitude_sticker_routes_to_social_reply(self, sticker_mocks) -> None:
        result = _normalize_sticker(_GRATITUDE_VISION, sticker_mocks=sticker_mocks)
        nc = classify_non_commerce(result.text, media_type="sticker")
        assert nc is not None
        assert nc.category in {SOCIAL_THANKS, "religious_media", "dua"}

        intent = intent_rules.match(result.text)
        assert intent is not None
        assert intent.name == INTENT_SOCIAL

        ctx = _brain_ctx(result.text, metadata=result.metadata)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SOCIAL_REPLY
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_expressive_sticker_no_product_browse(self, sticker_mocks) -> None:
        result = _normalize_sticker(_EXPRESSIVE_VISION, sticker_mocks=sticker_mocks)
        ctx = _brain_ctx(result.text, metadata=result.metadata)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_SEARCH_PRODUCTS

        card = evaluate_product_card_send(
            tenant_id=33,
            connection=MagicMock(
                status="connected",
                sending_enabled=True,
                phone_number_id="123",
                catalog_enabled=True,
                meta_catalog_id="CAT",
                provider="meta",
            ),
            attachment={
                "kind": "product_card",
                "id": 1,
                "title": "عسل",
                "external_id": "SKU1",
                "confidence": "strong",
                "file_url": "https://example.com/p.jpg",
            },
            block_commerce_escalation=True,
        )
        assert card.reason == REASON_NON_COMMERCE_BLOCKED

    def test_sticker_skips_staff_contact_routing(self, sticker_mocks) -> None:
        result = _normalize_sticker(_GRATITUDE_VISION, sticker_mocks=sticker_mocks)
        assert should_skip_contact_routing_for_media(result.metadata) is True

    def test_gratitude_sticker_single_social_decision_not_duplicate(
        self, sticker_mocks,
    ) -> None:
        """One sticker turn → one social decision path (no search fallback)."""
        result = _normalize_sticker(_GRATITUDE_VISION, sticker_mocks=sticker_mocks)
        ctx = _brain_ctx(result.text, metadata=result.metadata)
        engine = DefaultDecisionEngine()
        d1 = engine.decide(ctx)
        d2 = engine.decide(ctx)
        assert d1.action == d2.action
        assert d1.action == ACTION_SOCIAL_REPLY
        assert NON_COMMERCE_STICKER_TAG in result.text


class TestStickerWebpConversion:
    def test_prepare_sticker_passes_webp_when_no_pillow(self) -> None:
        from modules.ai.media.normalizer import _prepare_sticker_vision_bytes

        raw = b"fake-webp-bytes"
        out_bytes, out_mime = _prepare_sticker_vision_bytes(raw, "image/webp")
        assert out_bytes == raw
        assert out_mime == "image/webp"
