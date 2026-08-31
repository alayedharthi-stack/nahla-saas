"""IMAGE-only: trusted Vision evidence must survive Brain semantic routing.

Production RCA: a caption-less image with vision_status=ok was stripped
to empty by resolve_semantic_customer_message(), then webhook hit
empty_text_no_fallback and never invoked Brain.

Location / arrival remain caption-only via resolve_pre_brain_customer_message.
Audio, PDF, and video paths are not changed by this repair.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from modules.ai.brain.commerce.staff_contact_media_source_guard import (  # noqa: E402
    staff_contact_intent_message,
)
from modules.ai.media.routing_guard import (  # noqa: E402
    resolve_inbound_semantic_routing,
    resolve_pre_brain_customer_message,
    resolve_semantic_customer_message,
    trusted_vision_text_from_metadata,
)

VISION_CIPRALEX = (
    "نوع المحتوى: عبوة دواء.\n\n"
    'النص المرئي: "سيبراليكس 20 ملجم"، "Cipralex 20 mg"، "escitalopram".'
)
FRAMED_NO_CAPTION = f"[وصف الصورة المرسلة] {VISION_CIPRALEX}"
CUSTOMER_CAPTION = "هذا غلاف الدواء"
FRAMED_WITH_CAPTION = f"{CUSTOMER_CAPTION}\n\n[وصف الصورة] {VISION_CIPRALEX}"
OCR_VISIBLE = "سيبراليكس 20 ملجم"


def _image_meta(
    *,
    vision_status: str | None = "ok",
    vision_text: str | None = VISION_CIPRALEX,
    caption: str | None = None,
    extra: dict | None = None,
) -> dict:
    meta = {
        "source_type": "image",
        "normalized_type": "image",
        "caption": caption,
        "vision_status": vision_status,
        "vision_text": vision_text,
        "ai_used_image": vision_status == "ok" and bool(vision_text),
        "tenant_id": 1,
    }
    if extra:
        meta.update(extra)
    return meta


def _brain_eligible(semantic: str) -> bool:
    """Webhook Brain path is eligible when semantic text is non-empty."""
    return bool((semantic or "").strip())


class TestTrustedVisionHelper:
    def test_ok_status_and_text_is_trusted(self) -> None:
        assert trusted_vision_text_from_metadata(_image_meta()) == VISION_CIPRALEX

    def test_nested_normalized_inbound_is_trusted(self) -> None:
        wrapped = {"normalized_inbound": _image_meta()}
        assert trusted_vision_text_from_metadata(wrapped) == VISION_CIPRALEX

    def test_failed_status_is_not_trusted(self) -> None:
        assert trusted_vision_text_from_metadata(
            _image_meta(vision_status="failed", vision_text=VISION_CIPRALEX)
        ) == ""

    def test_empty_status_is_not_trusted(self) -> None:
        assert trusted_vision_text_from_metadata(
            _image_meta(vision_status="empty", vision_text="")
        ) == ""

    def test_skipped_status_is_not_trusted(self) -> None:
        assert trusted_vision_text_from_metadata(
            _image_meta(vision_status="skipped", vision_text=None)
        ) == ""

    def test_text_without_ok_status_is_not_trusted(self) -> None:
        assert trusted_vision_text_from_metadata(
            _image_meta(vision_status=None, vision_text=VISION_CIPRALEX)
        ) == ""


class TestImageSemanticRouting:
    def test_image_without_caption_preserves_vision_and_stays_brain_eligible(self) -> None:
        routing = resolve_inbound_semantic_routing(
            brain_text=FRAMED_NO_CAPTION,
            inbound_metadata=_image_meta(caption=None),
            inbound_normalized_type="image",
        )
        assert routing.semantic_text
        assert VISION_CIPRALEX in routing.semantic_text
        assert OCR_VISIBLE in routing.semantic_text
        assert _brain_eligible(routing.semantic_text)
        assert routing.route_unclear_audio_order_support is False

    def test_image_with_caption_keeps_caption_and_vision(self) -> None:
        semantic = resolve_semantic_customer_message(
            brain_text=FRAMED_WITH_CAPTION,
            inbound_metadata=_image_meta(caption=CUSTOMER_CAPTION),
            inbound_normalized_type="image",
        )
        assert CUSTOMER_CAPTION in semantic
        assert VISION_CIPRALEX in semantic
        assert OCR_VISIBLE in semantic
        assert _brain_eligible(semantic)

    def test_ocr_visible_text_survives_semantic_routing(self) -> None:
        semantic = resolve_semantic_customer_message(
            brain_text=FRAMED_NO_CAPTION,
            inbound_metadata=_image_meta(),
            inbound_normalized_type="image",
        )
        assert OCR_VISIBLE in semantic

    def test_empty_brain_text_falls_back_to_metadata_vision(self) -> None:
        semantic = resolve_semantic_customer_message(
            brain_text="",
            inbound_metadata=_image_meta(caption=None),
            inbound_normalized_type="image",
        )
        assert semantic == VISION_CIPRALEX
        assert _brain_eligible(semantic)

    def test_vision_failed_does_not_fabricate_evidence(self) -> None:
        semantic = resolve_semantic_customer_message(
            brain_text=FRAMED_NO_CAPTION,
            inbound_metadata=_image_meta(vision_status="failed", vision_text=None),
            inbound_normalized_type="image",
        )
        assert semantic == ""
        assert not _brain_eligible(semantic)

    def test_no_vision_metadata_fail_closed_to_caption_strip(self) -> None:
        framed = "[تصنيف صورة]\nوصف بصري للمنتج"
        semantic = resolve_semantic_customer_message(
            brain_text=framed,
            inbound_metadata={"source_type": "image"},
            inbound_normalized_type="image",
        )
        assert "تصنيف" not in semantic
        assert not _brain_eligible(semantic)


class TestAudioUnchanged:
    def test_audio_with_transcript_unchanged(self) -> None:
        spoken = "الطلب متأخر والشحن ما وصل"
        routing = resolve_inbound_semantic_routing(
            brain_text="",
            inbound_metadata={
                "source_type": "audio",
                "type": "audio",
                "transcript_text": spoken,
            },
            inbound_normalized_type="audio",
        )
        assert routing.semantic_text == spoken
        assert routing.route_unclear_audio_order_support is False

    def test_audio_without_transcript_unchanged(self) -> None:
        routing = resolve_inbound_semantic_routing(
            brain_text="",
            inbound_metadata={"source_type": "audio", "type": "audio"},
            inbound_normalized_type="audio",
        )
        assert routing.semantic_text == ""
        assert routing.route_unclear_audio_order_support is False


class TestDocumentAndVideoNotRepairedHere:
    def test_video_frame_vision_is_not_claimed_by_image_owner(self) -> None:
        framed = "[فيديو من العميل]\nالنص الظاهر/الوصف من الفيديو: كاميرا مراقبة"
        semantic = resolve_semantic_customer_message(
            brain_text=framed,
            inbound_metadata={
                "source_type": "video",
                "frame_vision_status": "ok",
                "frame_vision_text": "كاميرا مراقبة",
            },
            inbound_normalized_type="video",
        )
        assert "كاميرا" not in semantic

    def test_pdf_extracted_text_is_not_claimed_by_image_owner(self) -> None:
        framed = "[وثيقة PDF]\nنص الملف المستخرج:\nفاتورة تحويل"
        meta = {
            "source_type": "document",
            "pdf_text_full": "فاتورة تحويل",
        }
        assert trusted_vision_text_from_metadata(meta) == ""
        semantic = resolve_semantic_customer_message(
            brain_text=framed,
            inbound_metadata=meta,
            inbound_normalized_type="document",
        )
        # Document/PDF is out of scope: do not route it through the
        # image Vision helper. Existing document semantic behavior
        # is left unchanged.
        assert semantic == framed


class TestPreBrainCaptionOnlyPreserved:
    def test_image_pre_brain_is_caption_only_without_vision(self) -> None:
        msg = resolve_pre_brain_customer_message(
            brain_text=FRAMED_NO_CAPTION,
            inbound_metadata=_image_meta(caption=None),
        )
        assert msg == ""
        assert VISION_CIPRALEX not in msg

    def test_image_pre_brain_uses_customer_caption_not_vision(self) -> None:
        msg = resolve_pre_brain_customer_message(
            brain_text=FRAMED_WITH_CAPTION,
            inbound_metadata=_image_meta(caption=CUSTOMER_CAPTION),
        )
        assert msg == CUSTOMER_CAPTION
        assert VISION_CIPRALEX not in msg

    def test_location_guard_cannot_treat_vision_as_customer_authored(self) -> None:
        from modules.ai.brain.commerce.contact_route_policy import is_location_query

        vision_as_location = (
            "[وصف الصورة المرسلة] نوع المحتوى: لقطة خرائط. "
            "تظهر اتجاهات وموقع المتجر في الرياض."
        )
        pre_brain = resolve_pre_brain_customer_message(
            brain_text=vision_as_location,
            inbound_metadata=_image_meta(
                vision_text="تظهر اتجاهات وموقع المتجر في الرياض.",
                caption=None,
            ),
        )
        assert pre_brain == ""
        assert not is_location_query(pre_brain)
        assert staff_contact_intent_message(vision_as_location) == ""

        semantic = resolve_semantic_customer_message(
            brain_text=vision_as_location,
            inbound_metadata=_image_meta(
                vision_text="تظهر اتجاهات وموقع المتجر في الرياض.",
            ),
            inbound_normalized_type="image",
        )
        assert "الرياض" in semantic


class TestTenantIsolation:
    def test_foreign_metadata_cannot_confirm_this_image(self) -> None:
        foreign = {
            "source_type": "image",
            "tenant_id": 99,
            "vision_status": "ok",
            "vision_text": "منتج تاجر أجنبي يجب ألا يظهر",
        }
        local = _image_meta(vision_status="failed", vision_text=None, extra={"tenant_id": 1})
        semantic = resolve_semantic_customer_message(
            brain_text=FRAMED_NO_CAPTION,
            inbound_metadata=local,
            inbound_normalized_type="image",
        )
        assert "تاجر أجنبي" not in semantic
        assert trusted_vision_text_from_metadata(local) == ""
        assert trusted_vision_text_from_metadata(foreign) == "منتج تاجر أجنبي يجب ألا يظهر"

    def test_no_tenant_constant_in_routing_guard(self) -> None:
        src = (_BACKEND / "modules" / "ai" / "media" / "routing_guard.py").read_text(
            encoding="utf-8",
        )
        assert "tenant_id == 33" not in src
        assert "tenant_id==33" not in src
        assert "product 154" not in src
        assert "سيبراليكس" not in src


class TestNoSilentDropContract:
    def test_vision_failure_still_observable_as_empty_semantic(self) -> None:
        routing = resolve_inbound_semantic_routing(
            brain_text="",
            inbound_metadata=_image_meta(vision_status="failed", vision_text=None),
            inbound_normalized_type="image",
        )
        assert routing.semantic_text == ""
        assert routing.route_unclear_audio_order_support is False

    def test_successful_vision_is_not_empty_text_drop(self) -> None:
        routing = resolve_inbound_semantic_routing(
            brain_text=FRAMED_NO_CAPTION,
            inbound_metadata=_image_meta(),
            inbound_normalized_type="image",
        )
        assert routing.semantic_text
        assert not (
            not routing.semantic_text
            and not routing.route_unclear_audio_order_support
        )
