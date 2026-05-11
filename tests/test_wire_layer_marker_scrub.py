"""
tests/test_wire_layer_marker_scrub.py
─────────────────────────────────────
Locks the contract that every outbound WhatsApp payload passing
through ``provider_send_message`` has internal markers
(``[TRANSFER]``, ``[DEBUG]``, ``[ACTION]``, ``[INTERNAL]``,
``[MEDIA:5]`` etc.) stripped from every text-bearing slot before
the request leaves this process.

Why this matters
────────────────
Merchants reported customers receiving ``[TRANSFER]`` literally
in WhatsApp. The original fix was an inline scrub in the AI
reply path of ``whatsapp_webhook.py`` — but every OTHER outbound
caller (manual `/conversations/reply`, automation engine,
order notifications, cart recovery, admin direct-send, fallback
replies in the webhook itself) bypassed it.

The wire-layer scrub at ``provider_send_message`` is the
defense-in-depth fix: a future caller that forgets to sanitize
CANNOT leak markers, because the bytes literally cannot reach
Meta / 360dialog without passing through this chokepoint.

Tests in this file exercise the helper directly with the full
range of Meta Graph "messages" payload shapes (text, interactive
button / list / cta_url, image / video / document captions) and
assert that:
  1. All bracketed markers are stripped.
  2. The merchant's legitimate Arabic content (which may contain
     square brackets around regular words) is preserved.
  3. Templates pass through untouched (their text is pre-approved
     by Meta — never AI-generated).
  4. Edge cases (empty, non-string, missing keys, exceptions) do
     not break the send.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)


def _scrub(payload):
    from services.whatsapp_platform.service import _scrub_outbound_payload
    return _scrub_outbound_payload(payload)


# ──────────────────────────────────────────────────────────────────────
# Text type
# ──────────────────────────────────────────────────────────────────────


class TestTextPayload:
    def test_transfer_marker_stripped_from_text_body(self):
        p = _scrub({
            "messaging_product": "whatsapp",
            "to": "+966500000111",
            "type": "text",
            "text": {"body": "أهلاً [TRANSFER] سيتم تحويلك للمختص."},
        })
        assert "[TRANSFER]" not in p["text"]["body"]
        assert "أهلاً" in p["text"]["body"]
        assert "سيتم تحويلك للمختص" in p["text"]["body"]

    def test_all_four_marker_names_stripped(self):
        body = (
            "بداية [TRANSFER] [DEBUG] وسط "
            "[ACTION] [INTERNAL] نهاية"
        )
        p = _scrub({"type": "text", "text": {"body": body}})
        out = p["text"]["body"]
        for tok in ("[TRANSFER]", "[DEBUG]", "[ACTION]", "[INTERNAL]"):
            assert tok not in out, f"{tok} survived: {out!r}"
        assert "بداية" in out and "نهاية" in out

    def test_media_marker_with_payload_stripped(self):
        p = _scrub({
            "type": "text",
            "text": {"body": "تفضل الصورة [MEDIA:7] انتهى."},
        })
        assert "[MEDIA:7]" not in p["text"]["body"]
        assert "تفضل الصورة" in p["text"]["body"]
        assert "انتهى" in p["text"]["body"]

    def test_template_marker_with_payload_stripped(self):
        p = _scrub({
            "type": "text",
            "text": {"body": "مرحبا [TEMPLATE:contact_owner] شكرا"},
        })
        assert "[TEMPLATE:contact_owner]" not in p["text"]["body"]

    def test_arabic_brackets_are_preserved(self):
        """Merchants sometimes wrap Arabic notes in brackets like
        ``[ملاحظة]``. The regex matches ASCII uppercase only, so
        Arabic-bracketed content must pass through unchanged."""
        body = "[ملاحظة] هذا تنبيه مهم [إشعار]"
        p = _scrub({"type": "text", "text": {"body": body}})
        assert p["text"]["body"] == body

    def test_lowercase_brackets_are_preserved(self):
        """The marker regex requires uppercase. ``[debug]`` is not
        an internal marker — leave it alone (could be merchant
        content)."""
        body = "ملاحظة [debug] الأمر يدوي"
        p = _scrub({"type": "text", "text": {"body": body}})
        assert p["text"]["body"] == body

    def test_clean_text_passes_through_unchanged(self):
        body = "مرحباً، كيف يمكنني خدمتك اليوم؟"
        p = _scrub({"type": "text", "text": {"body": body}})
        assert p["text"]["body"] == body

    def test_does_not_mutate_input(self):
        original = {"type": "text", "text": {"body": "أهلاً [TRANSFER]"}}
        _ = _scrub(original)
        assert original["text"]["body"] == "أهلاً [TRANSFER]"


# ──────────────────────────────────────────────────────────────────────
# Interactive types
# ──────────────────────────────────────────────────────────────────────


class TestInteractiveButton:
    def test_body_text_scrubbed(self):
        p = _scrub({
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": "اختر [TRANSFER] أحد الخيارات"},
                "action": {"buttons": [
                    {"type": "reply", "reply": {"id": "y", "title": "نعم"}},
                ]},
            },
        })
        assert "[TRANSFER]" not in p["interactive"]["body"]["text"]

    def test_header_text_scrubbed(self):
        p = _scrub({
            "type": "interactive",
            "interactive": {
                "type": "button",
                "header": {"type": "text", "text": "[ACTION] العنوان"},
                "body": {"text": "النص"},
                "action": {"buttons": []},
            },
        })
        assert "[ACTION]" not in p["interactive"]["header"]["text"]
        assert "العنوان" in p["interactive"]["header"]["text"]

    def test_footer_text_scrubbed(self):
        p = _scrub({
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": "النص"},
                "footer": {"text": "[INTERNAL] حقوق محفوظة"},
                "action": {"buttons": []},
            },
        })
        assert "[INTERNAL]" not in p["interactive"]["footer"]["text"]

    def test_image_header_not_scrubbed(self):
        """Only text-type headers are scrubbed. Image headers carry
        a `link`, not a `text` — must pass through untouched."""
        p = _scrub({
            "type": "interactive",
            "interactive": {
                "type": "button",
                "header": {"type": "image", "image": {"link": "https://x/y.png"}},
                "body": {"text": "النص"},
                "action": {"buttons": []},
            },
        })
        assert p["interactive"]["header"]["image"]["link"] == "https://x/y.png"

    def test_button_titles_scrubbed(self):
        p = _scrub({
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": "النص"},
                "action": {"buttons": [
                    {"type": "reply", "reply": {"id": "a", "title": "[DEBUG] نعم"}},
                    {"type": "reply", "reply": {"id": "b", "title": "لا"}},
                ]},
            },
        })
        btns = p["interactive"]["action"]["buttons"]
        assert "[DEBUG]" not in btns[0]["reply"]["title"]
        assert "نعم" in btns[0]["reply"]["title"]
        assert btns[1]["reply"]["title"] == "لا"


class TestInteractiveList:
    def test_section_and_row_titles_scrubbed(self):
        p = _scrub({
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": "اختر"},
                "action": {"sections": [
                    {
                        "title": "[TRANSFER] القسم الأول",
                        "rows": [
                            {"id": "r1", "title": "[ACTION] الصف الأول", "description": "[DEBUG] تفصيل"},
                            {"id": "r2", "title": "الصف الثاني", "description": "تفصيل آخر"},
                        ],
                    },
                ]},
            },
        })
        sec = p["interactive"]["action"]["sections"][0]
        assert "[TRANSFER]" not in sec["title"]
        assert "[ACTION]" not in sec["rows"][0]["title"]
        assert "[DEBUG]" not in sec["rows"][0]["description"]
        assert sec["rows"][1]["title"] == "الصف الثاني"


class TestInteractiveCtaUrl:
    def test_cta_display_text_scrubbed(self):
        p = _scrub({
            "type": "interactive",
            "interactive": {
                "type": "cta_url",
                "body": {"text": "اضغط للمتابعة"},
                "action": {
                    "name": "cta_url",
                    "parameters": {
                        "display_text": "[ACTION] افتح الرابط",
                        "url": "https://example.com/order/123",
                    },
                },
            },
        })
        params = p["interactive"]["action"]["parameters"]
        assert "[ACTION]" not in params["display_text"]
        assert "افتح الرابط" in params["display_text"]
        # URL is NOT a text slot — must pass through verbatim.
        assert params["url"] == "https://example.com/order/123"


# ──────────────────────────────────────────────────────────────────────
# Media types
# ──────────────────────────────────────────────────────────────────────


class TestMediaCaption:
    def test_image_caption_scrubbed(self):
        p = _scrub({
            "type": "image",
            "image": {
                "link": "https://x/y.jpg",
                "caption": "[TRANSFER] منتج جديد",
            },
        })
        assert "[TRANSFER]" not in p["image"]["caption"]
        assert "منتج جديد" in p["image"]["caption"]
        assert p["image"]["link"] == "https://x/y.jpg"

    def test_video_caption_scrubbed(self):
        p = _scrub({
            "type": "video",
            "video": {"link": "https://x/y.mp4", "caption": "[DEBUG] تجربة"},
        })
        assert "[DEBUG]" not in p["video"]["caption"]

    def test_document_caption_scrubbed(self):
        p = _scrub({
            "type": "document",
            "document": {
                "link": "https://x/y.pdf",
                "caption": "[INTERNAL] الفاتورة",
                "filename": "invoice.pdf",
            },
        })
        assert "[INTERNAL]" not in p["document"]["caption"]
        # filename is an identifier — not scrubbed.
        assert p["document"]["filename"] == "invoice.pdf"

    def test_image_without_caption_passes(self):
        p = _scrub({
            "type": "image",
            "image": {"link": "https://x/y.jpg"},
        })
        assert p["image"]["link"] == "https://x/y.jpg"
        # caption was absent → still absent (or None after _clean)
        assert p["image"].get("caption") in (None, "")


# ──────────────────────────────────────────────────────────────────────
# Template type — deliberately NOT scrubbed
# ──────────────────────────────────────────────────────────────────────


class TestTemplateNotScrubbed:
    def test_template_payload_passes_through_unchanged(self):
        """Template bodies are pre-approved by Meta. Their parameter
        values come from DB (customer name, coupon code) — never from
        GPT. Scrubbing them would be a no-op at best and at worst
        could mangle parameter values that legitimately contain
        bracketed text (e.g. a coupon code ``[VIP10]``)."""
        original = {
            "type": "template",
            "template": {
                "name": "order_confirmation",
                "language": {"code": "ar"},
                "components": [
                    {"type": "body", "parameters": [
                        {"type": "text", "text": "[VIP10]"},
                        {"type": "text", "text": "أحمد"},
                    ]},
                ],
            },
        }
        p = _scrub(original)
        # The bracketed param survives.
        assert p["template"]["components"][0]["parameters"][0]["text"] == "[VIP10]"
        assert p["template"]["name"] == "order_confirmation"


# ──────────────────────────────────────────────────────────────────────
# Robustness — non-dict, missing fields, weird shapes
# ──────────────────────────────────────────────────────────────────────


class TestRobustness:
    def test_non_dict_input_passes_through(self):
        assert _scrub(None) is None
        assert _scrub("hello") == "hello"
        assert _scrub([]) == []

    def test_unknown_type_passes_through(self):
        p = _scrub({"type": "sticker", "sticker": {"id": "abc"}})
        assert p["sticker"]["id"] == "abc"

    def test_missing_text_slot_does_not_crash(self):
        p = _scrub({"type": "text"})
        # No "text" key — nothing to scrub, no exception.
        assert p["type"] == "text"

    def test_text_body_is_non_string(self):
        """If body is somehow not a string (None / number / dict),
        pass it through unchanged."""
        for v in [None, 123, {"unexpected": "shape"}]:
            p = _scrub({"type": "text", "text": {"body": v}})
            assert p["text"]["body"] == v

    def test_empty_string_body_passes(self):
        p = _scrub({"type": "text", "text": {"body": ""}})
        assert p["text"]["body"] == ""

    def test_idempotent(self):
        """Running the scrub twice produces the same output as once."""
        p1 = _scrub({"type": "text", "text": {"body": "أهلاً [TRANSFER]"}})
        p2 = _scrub(p1)
        assert p1["text"]["body"] == p2["text"]["body"]


# ──────────────────────────────────────────────────────────────────────
# Integration — provider_send_message actually calls the scrub
# ──────────────────────────────────────────────────────────────────────


class TestProviderSendIntegration:
    def test_provider_send_message_scrubs_before_post(self, monkeypatch):
        """End-to-end: verify that the scrubbed payload (NOT the
        original) is what gets handed to the HTTP layer."""
        import asyncio
        from services.whatsapp_platform import service as wa_service

        captured = {}

        async def _fake_get_token(*args, **kwargs):
            class _Ctx:
                token = "TKN"
                source = "test"
            return _Ctx()

        async def _fake_post(conn, ctx, *, tenant_id, operation, path, json, timeout):
            captured["json"] = json
            return {"messages": [{"id": "wamid.x"}]}

        def _fake_wa_provider(conn):
            return "meta"

        monkeypatch.setattr(wa_service, "get_token_for_operation", _fake_get_token)
        monkeypatch.setattr(wa_service, "provider_post_with_context", _fake_post)
        monkeypatch.setattr(wa_service, "wa_provider", _fake_wa_provider)

        async def _run():
            return await wa_service.provider_send_message(
                db=None,
                conn=object(),
                tenant_id=1,
                operation="send_message",
                phone_id="123",
                payload={
                    "messaging_product": "whatsapp",
                    "to": "+966500000111",
                    "type": "text",
                    "text": {"body": "أهلاً [TRANSFER] العميل العزيز"},
                },
            )

        asyncio.run(_run())

        sent = captured["json"]
        assert "[TRANSFER]" not in sent["text"]["body"], (
            "wire-layer scrub did not run — marker reached HTTP layer"
        )
        assert "العميل العزيز" in sent["text"]["body"]
