"""
tests/test_payment_evidence.py
──────────────────────────────
Coverage for the universal payment-evidence classifier introduced
in May 2026 to stop the production bug where the bot treated
pre-transfer review screens as completed payments and shipped
"thanks, order under review" ACKs (sometimes with an internal phone
number) before the customer had actually transferred anything.

The classifier lives at ``core.payment_evidence`` and is wired in
two places:

  * ``modules.ai.media.normalizer._process_image`` — runs after
    OpenAI Vision describes the image.
  * ``modules.ai.media.normalizer._process_document`` — runs after
    pypdf extracts text from the PDF (with a vision-OCR fallback
    for scanned PDFs).

These tests cover:

  * Pure-function matrix of the classifier itself
    (confirmed / pre_transfer_review / needs_confirmation /
    not_payment + edge cases).
  * The PDF text-extraction helper (pypdf happy path, encrypted
    PDF, empty bytes, library-missing fallback).
  * The document normalizer end-to-end: a real Saudi-bank-style
    receipt text produces ``pdf_kind=payment_receipt`` AND
    ``payment_evidence_status=confirmed``; a pre-transfer review
    screen produces ``pdf_kind=payment_pre_review``.
  * The ``order_flow.maybe_handle_receipt_inbound`` gate refuses
    to fire when ``payment_evidence_status != "confirmed"``.
  * The ``order_flow.maybe_handle_payment_evidence_inbound``
    soft-reply fires for pre-transfer review.

Tests follow the same conventions as ``tests/test_inbound_media.py``:
direct unit calls (no TestClient), in-memory mocks for the OpenAI
endpoints, and ``MagicMock`` for the SQLAlchemy session where DB
access is required.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in [str(REPO_ROOT), str(BACKEND_DIR), str(DATABASE_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def isolated_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "services.inbound_media_storage._STORAGE_ROOT",
        Path(tmp_path).resolve(),
    )
    yield tmp_path


def _run(coro):
    return asyncio.run(coro)


# ──────────────────────────────────────────────────────────────────────
# 1. Pure classifier matrix
# ──────────────────────────────────────────────────────────────────────


class TestPaymentEvidenceClassifier:
    """Stress-tests for ``classify_payment_evidence``. The classifier
    is the universal gate — false positives here would let the bot
    confirm payment for screens that never actually committed."""

    def test_empty_input_is_not_payment(self):
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_NOT_PAYMENT,
        )
        v = classify_payment_evidence("")
        assert v["status"] == PAYMENT_EVIDENCE_NOT_PAYMENT
        assert v["reason"] == "empty_text"

        v = classify_payment_evidence(None)
        assert v["status"] == PAYMENT_EVIDENCE_NOT_PAYMENT

    def test_strong_success_phrase_confirmed_arabic(self):
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_CONFIRMED,
        )
        for text in (
            "تم التحويل بنجاح إلى المستفيد",
            "تمت العملية بنجاح",
            "تم خصم المبلغ من حسابك",
            "حالة العملية: ناجحة",
            "إيصال تحويل نهائي\nمبلغ 358 ر.س",
        ):
            v = classify_payment_evidence(text)
            assert v["status"] == PAYMENT_EVIDENCE_CONFIRMED, text
            assert v["reason"] == "strong_success_phrase"

    def test_strong_success_phrase_confirmed_english(self):
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_CONFIRMED,
        )
        for text in (
            "Transfer Successful\nAmount: 358 SAR",
            "Status: Completed\nReference No: TXN-9981234",
            "Transaction approved",
            "Payment Successful",
        ):
            v = classify_payment_evidence(text)
            assert v["status"] == PAYMENT_EVIDENCE_CONFIRMED, text

    def test_pre_transfer_review_screens_arabic(self):
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW,
        )
        for text in (
            "مراجعة بيانات التحويل\n"
            "اسم المستفيد: محمد علي\n"
            "الآيبان: SA0380000000608010167519\n"
            "المبلغ: 358 ر.س\n"
            "تأكد من البيانات واضغط تحويل",
            "تأكيد التحويل\nاضغط على تأكيد لإتمام العملية",
            "مراجعة المستفيد قبل التحويل",
        ):
            v = classify_payment_evidence(text)
            assert v["status"] == PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW, text
            assert v["reason"] == "pre_transfer_review_phrase"

    def test_pre_transfer_review_screens_english(self):
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW,
        )
        for text in (
            "Review Transfer\nBeneficiary: Ahmed\nIBAN: SA03 8000 0000 6080 1016 7519\n"
            "Press confirm to send.",
            "Review and confirm\nAmount: 358 SAR",
            "Verify beneficiary name",
        ):
            v = classify_payment_evidence(text)
            assert v["status"] == PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW

    def test_pre_transfer_review_outranks_completion_tokens(self):
        """If the screen says both 'تأكيد التحويل' (button label) and
        bank context, we must NOT promote to confirmed just because
        a weak success token ('تم' in 'تم استلام البيانات') exists.
        The pre-review phrase wins."""
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW,
        )
        text = (
            "تأكيد التحويل\n"
            "اسم المستفيد: محمد\n"
            "الآيبان: SA0380000000608010167519\n"
            "تم استلام البيانات\n"
            "اضغط على تأكيد"
        )
        v = classify_payment_evidence(text)
        assert v["status"] == PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW

    def test_strong_success_outranks_pre_review(self):
        """If BOTH a pre-review phrase AND a strong success marker
        are present (e.g. customer sent a multi-step screenshot
        scrolled past confirmation), the confirmed verdict wins."""
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_CONFIRMED,
        )
        text = (
            "تأكيد التحويل\n"
            "تم التحويل بنجاح\n"
            "رقم العملية: TXN-9981234"
        )
        v = classify_payment_evidence(text)
        assert v["status"] == PAYMENT_EVIDENCE_CONFIRMED

    def test_payment_context_only_needs_confirmation(self):
        """Bank brand + amount + IBAN without any completion marker
        → needs_confirmation. Do NOT promote to confirmed."""
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_NEEDS_CONFIRMATION,
        )
        text = (
            "البنك الراجحي\n"
            "اسم المستفيد: أحمد محمد\n"
            "رقم الحساب: SA0380000000608010167519\n"
            "المبلغ: 358 ر.س"
        )
        v = classify_payment_evidence(text)
        assert v["status"] == PAYMENT_EVIDENCE_NEEDS_CONFIRMATION
        assert v["reason"] == "payment_context_no_success_marker"

    def test_iban_alone_is_needs_confirmation(self):
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_NEEDS_CONFIRMATION,
        )
        text = "حسابي: SA0380000000608010167519"
        v = classify_payment_evidence(text)
        assert v["status"] == PAYMENT_EVIDENCE_NEEDS_CONFIRMATION
        assert v["signals"]["iban_present"] is True

    def test_weak_success_with_context_promotes_to_confirmed(self):
        """A short receipt that says only 'Successful' alongside
        bank brand + amount should still classify as confirmed."""
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_CONFIRMED,
        )
        text = (
            "Al Rajhi Bank\n"
            "Amount: 358 SAR\n"
            "Status: Successful\n"
            "IBAN: SA0380000000608010167519"
        )
        v = classify_payment_evidence(text)
        assert v["status"] == PAYMENT_EVIDENCE_CONFIRMED

    def test_weak_success_without_context_is_not_payment(self):
        """The word 'successful' alone (e.g. in a screenshot of a
        game / app) must NOT classify as a payment."""
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_NOT_PAYMENT,
        )
        v = classify_payment_evidence("Successful! You won the round.")
        assert v["status"] == PAYMENT_EVIDENCE_NOT_PAYMENT

    def test_reference_number_with_context_stays_needs_confirmation_without_success(self):
        from core.payment_evidence import (
            classify_payment_evidence,
            PAYMENT_EVIDENCE_CONFIRMED,
            PAYMENT_EVIDENCE_NEEDS_CONFIRMATION,
        )
        text = (
            "Al Rajhi\n"
            "Beneficiary: Ahmed\n"
            "Amount: 358 SAR\n"
            "Reference Number: TXN-9981234"
        )
        v = classify_payment_evidence(text)
        assert v["status"] == PAYMENT_EVIDENCE_NEEDS_CONFIRMATION
        assert v["status"] != PAYMENT_EVIDENCE_CONFIRMED
        assert v["reason"] == "payment_context_no_success_marker"

    def test_two_generic_hints_with_awaiting_context_is_needs_confirmation(self):
        """Tightened May 2026: a SINGLE generic word ("تحويل") on its
        own is too noisy — could be a customer typing the word over
        an unrelated screenshot. We now require ≥2 distinct generic
        hints OR a discriminating signal (IBAN / bank brand / amount)
        before classifying as needs_confirmation."""
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_NEEDS_CONFIRMATION,
        )
        v = classify_payment_evidence(
            "تحويل إيصال",
            extra_context={"awaiting_payment_receipt": True},
        )
        assert v["status"] == PAYMENT_EVIDENCE_NEEDS_CONFIRMATION

    def test_single_generic_hint_is_not_payment_even_when_awaiting(self):
        """Hotfix lock-in: one bare word ("تحويل") MUST stay
        not_payment even when the bot is awaiting a receipt — the
        brain should still get the image and reply normally."""
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_NOT_PAYMENT,
        )
        v = classify_payment_evidence(
            "تحويل",
            extra_context={"awaiting_payment_receipt": True},
        )
        assert v["status"] == PAYMENT_EVIDENCE_NOT_PAYMENT

    def test_generic_hint_without_context_is_not_payment(self):
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_NOT_PAYMENT,
        )
        v = classify_payment_evidence("تحويل")
        assert v["status"] == PAYMENT_EVIDENCE_NOT_PAYMENT

    def test_bare_sa_token_does_not_count_as_iban(self):
        """Production false-positive: customer name "Salma" or word
        "salla" must not trigger IBAN signal."""
        from core.payment_evidence import classify_payment_evidence
        v = classify_payment_evidence("Salma من سلة (salla) تحب المنتج")
        assert v["signals"]["iban_present"] is False

    def test_diacritics_normalised(self):
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_CONFIRMED,
        )
        # Same as "تم التحويل" but with explicit fatha + sukun.
        v = classify_payment_evidence("تَمَّ التَّحْوِيلُ بنجاح")
        assert v["status"] == PAYMENT_EVIDENCE_CONFIRMED


# ──────────────────────────────────────────────────────────────────────
# 1c. Hard-negative gate (greeting / social images)
#
# Production regression May 2026: a Kaaba/Hajj greeting card sent in
# the middle of a conversation was being misclassified as payment
# evidence, derailing the bot into shipping/product prompts. The
# greeting gate runs BEFORE any payment scoring and forces
# NOT_PAYMENT for unambiguous greeting / social content.
# ──────────────────────────────────────────────────────────────────────


class TestHardNegativeGreetingGate:
    """Greeting / social images must never become payment evidence,
    regardless of context."""

    def test_hajj_greeting_card_is_not_payment(self):
        """The exact image from the user-supplied production report:
        Kaaba + 'أهنئكم بقدوم عشر ذي الحجة' + 'تقبل الله منا ومنكم'."""
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_NOT_PAYMENT,
        )
        ocr = (
            "أهنئكم بقدوم عشر ذي الحجة، خير الأيام عند الله، "
            "تقبل الله منا ومنكم صالح الأعمال، وكتب لكم الأجر، "
            "ويبلغكم يوم النحر، وأسعدكم طول الدهر، كل عام وأنتم إلى الله أقرب"
        )
        v = classify_payment_evidence(ocr)
        assert v["status"] == PAYMENT_EVIDENCE_NOT_PAYMENT
        assert v["reason"] == "greeting_or_social_content"

    def test_eid_card_is_not_payment(self):
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_NOT_PAYMENT,
        )
        v = classify_payment_evidence("عيد مبارك وكل عام وأنتم بخير")
        assert v["status"] == PAYMENT_EVIDENCE_NOT_PAYMENT

    def test_ramadan_card_is_not_payment(self):
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_NOT_PAYMENT,
        )
        v = classify_payment_evidence("رمضان مبارك تقبل الله طاعتكم")
        assert v["status"] == PAYMENT_EVIDENCE_NOT_PAYMENT

    def test_greeting_with_payment_noise_token_still_not_payment(self):
        """Even when a greeting card text happens to contain a
        generic payment word like 'إيصال صدقة', the greeting gate
        forces NOT_PAYMENT — the card is a greeting, not a receipt."""
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_NOT_PAYMENT,
        )
        v = classify_payment_evidence(
            "تقبل الله منا ومنكم وجعلها صدقة إيصال خير لكم"
        )
        assert v["status"] == PAYMENT_EVIDENCE_NOT_PAYMENT
        assert v["reason"] == "greeting_or_social_content"

    def test_friday_greeting_is_not_payment(self):
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_NOT_PAYMENT,
        )
        v = classify_payment_evidence("جمعة مباركة وأسعدكم الله")
        assert v["status"] == PAYMENT_EVIDENCE_NOT_PAYMENT

    def test_morning_greeting_is_not_payment(self):
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_NOT_PAYMENT,
        )
        v = classify_payment_evidence("صباح الخير وسعادة لكم")
        assert v["status"] == PAYMENT_EVIDENCE_NOT_PAYMENT

    def test_english_eid_greeting_is_not_payment(self):
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_NOT_PAYMENT,
        )
        v = classify_payment_evidence("Eid Mubarak! May your day be blessed.")
        assert v["status"] == PAYMENT_EVIDENCE_NOT_PAYMENT

    def test_condolences_is_not_payment(self):
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_NOT_PAYMENT,
        )
        v = classify_payment_evidence(
            "إنا لله وإنا إليه راجعون، أحسن الله عزاكم"
        )
        assert v["status"] == PAYMENT_EVIDENCE_NOT_PAYMENT

    def test_generic_product_photo_caption_is_not_payment(self):
        """Random product photo / advertising caption MUST stay
        not_payment so the brain replies to the actual content."""
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_NOT_PAYMENT,
        )
        v = classify_payment_evidence(
            "صورة عسل سدر طبيعي من مناحلنا الجبلية، السعر ٣٦٠ ريال"
        )
        # The price tag alone is a SINGLE generic context hit + one
        # currency token → not enough to fire under the tightened
        # thresholds.
        assert v["status"] == PAYMENT_EVIDENCE_NOT_PAYMENT

    def test_real_completed_receipt_still_fires_after_greeting_gate(self):
        """Sanity: the greeting gate must NOT swallow a real receipt
        text just because it shares a noun with a greeting (e.g.
        bank confirmation that says 'مبروك' is fine — we still want
        confirmed)."""
        from core.payment_evidence import (
            classify_payment_evidence, PAYMENT_EVIDENCE_CONFIRMED,
        )
        # No greeting markers — pure receipt body.
        v = classify_payment_evidence(
            "تم التحويل بنجاح\n"
            "المبلغ: 360 ريال\n"
            "إلى: متجر نهلة - الراجحي\n"
            "رقم العملية: 9981234"
        )
        assert v["status"] == PAYMENT_EVIDENCE_CONFIRMED


# ──────────────────────────────────────────────────────────────────────
# 2. Soft reply composer
# ──────────────────────────────────────────────────────────────────────


class TestPaymentEvidenceReply:
    def test_pre_transfer_review_reply_is_short_and_tone_safe(self):
        from core.payment_evidence import (
            compose_payment_evidence_reply,
            PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW,
        )
        reply = compose_payment_evidence_reply(
            PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW,
        )
        assert reply is not None
        # Must mention "مراجعة" + "تحويل" but NOT promise shipping
        # or include any internal phone number.
        assert "مراجعة" in reply
        assert "تحويل" in reply
        assert "شحن" not in reply
        assert "أمين" not in reply
        # Must invite the customer to send the final receipt.
        assert "الإيصال" in reply

    def test_needs_confirmation_reply_is_short_and_tone_safe(self):
        from core.payment_evidence import (
            compose_payment_evidence_reply,
            PAYMENT_EVIDENCE_NEEDS_CONFIRMATION,
        )
        reply = compose_payment_evidence_reply(
            PAYMENT_EVIDENCE_NEEDS_CONFIRMATION,
        )
        assert reply is not None
        assert "شحن" not in reply
        assert "أمين" not in reply

    def test_confirmed_returns_none(self):
        """Confirmed payments are handled by ``order_flow`` — the
        soft-reply composer must NOT pre-empt that copy."""
        from core.payment_evidence import (
            compose_payment_evidence_reply,
            PAYMENT_EVIDENCE_CONFIRMED, PAYMENT_EVIDENCE_NOT_PAYMENT,
        )
        assert compose_payment_evidence_reply(
            PAYMENT_EVIDENCE_CONFIRMED) is None
        assert compose_payment_evidence_reply(
            PAYMENT_EVIDENCE_NOT_PAYMENT) is None


# ──────────────────────────────────────────────────────────────────────
# 3. PDF text extraction helper
# ──────────────────────────────────────────────────────────────────────


def _make_text_pdf(text: str) -> bytes:
    """Build a minimal text-bearing PDF using pypdf so we can test
    the extractor on a real document. Returns the in-memory bytes.
    Skips the test if pypdf is unavailable in the runtime."""
    pypdf = pytest.importorskip("pypdf")
    from io import BytesIO
    writer = pypdf.PdfWriter()
    # Add a single blank page and overlay text via the simplest API
    # pypdf exposes — ``add_blank_page`` + a content stream. We use
    # the very compact ``page.merge_page`` technique not because
    # it's the prettiest path but because it works without
    # reportlab.
    page = writer.add_blank_page(width=595, height=842)
    # Inject a basic content stream containing the text.
    from pypdf.generic import (  # type: ignore[import]
        ContentStream, NameObject,
    )
    # Build a tiny content stream that writes the text at (50, 750)
    # using the standard Helvetica font.
    stream_bytes = (
        b"BT /F1 12 Tf 50 750 Td (" +
        text.encode("latin-1", errors="replace") +
        b") Tj ET"
    )
    cs = ContentStream(None, writer)
    cs._data = stream_bytes
    page[NameObject("/Contents")] = writer._add_object(cs)
    # Register a Helvetica font resource on the page.
    from pypdf.generic import DictionaryObject, ArrayObject  # noqa: PLC0415
    font_dict = DictionaryObject({
        NameObject("/Type"):     NameObject("/Font"),
        NameObject("/Subtype"):  NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_ref = writer._add_object(font_dict)
    resources = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): font_ref,
        }),
    })
    page[NameObject("/Resources")] = resources

    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestExtractPDFText:
    def test_extracts_text_from_simple_pdf(self):
        from modules.ai.media.normalizer import _extract_pdf_text
        pdf_bytes = _make_text_pdf("Transfer Successful TXN-9981234")
        result = _extract_pdf_text(pdf_bytes, tenant_id=1, media_id="x")
        # We don't care about whitespace; just that the text we
        # injected shows up.
        assert result["extraction_status"] == "ok"
        assert result["page_count"] == 1
        assert "Transfer" in result["text"] or "TXN" in result["text"]

    def test_empty_bytes_returns_empty(self):
        from modules.ai.media.normalizer import _extract_pdf_text
        result = _extract_pdf_text(b"", tenant_id=1, media_id="x")
        assert result["extraction_status"] == "empty"
        assert result["text"] == ""

    def test_garbage_bytes_returns_corrupt(self):
        from modules.ai.media.normalizer import _extract_pdf_text
        result = _extract_pdf_text(
            b"this is not a pdf at all", tenant_id=1, media_id="x",
        )
        # pypdf raises PdfReadError → status=corrupt + no OCR needed.
        assert result["extraction_status"] in {"corrupt", "empty"}
        assert result["text"] == ""

    def test_library_missing_returns_fallback(self, monkeypatch):
        """Simulate environments where pypdf isn't installed — the
        extractor must NOT raise; it returns library_missing."""
        from modules.ai.media import normalizer
        real_import = __builtins__["__import__"] if isinstance(
            __builtins__, dict) else __builtins__.__import__

        def fake_import(name, *args, **kwargs):
            if name == "pypdf" or name.startswith("pypdf."):
                raise ImportError("pypdf not installed")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            result = normalizer._extract_pdf_text(
                b"%PDF-1.4 fake", tenant_id=1, media_id="x",
            )
        assert result["extraction_status"] == "library_missing"
        assert result["text"] == ""


# ──────────────────────────────────────────────────────────────────────
# 4. Document normalizer end-to-end
# ──────────────────────────────────────────────────────────────────────


class TestDocumentNormalizerPaymentEvidence:
    def _doc_message(
        self,
        *, filename: str = "doc.pdf", mime_type: str = "application/pdf",
        caption: str = "",
    ):
        return {
            "type": "document",
            "document": {
                "id": "wa-doc-001",
                "mime_type": mime_type,
                "filename": filename,
                "caption": caption,
            },
            "timestamp": "1700000000",
            "id": "wa-msg-doc",
        }

    def _patch_io(
        self, monkeypatch, *, downloaded_bytes: bytes,
        pdf_text: str = "",
    ):
        """Patch the download helper to return canned bytes, patch
        ``_extract_pdf_text`` to return the provided text, and
        ensure persistence is mocked so disk isn't touched."""
        from modules.ai.media import normalizer
        monkeypatch.setattr(
            normalizer,
            "_download_meta_media",
            AsyncMock(return_value={
                "bytes": downloaded_bytes,
                "mime_type": "application/pdf",
            }),
        )
        monkeypatch.setattr(
            normalizer,
            "_extract_pdf_text",
            lambda *a, **kw: {
                "text": pdf_text,
                "page_count": 1 if pdf_text else 0,
                "extraction_status": "ok" if pdf_text else "empty",
                "ocr_required": False,
            },
        )
        # Make persistence a no-op (returns None — caller handles it).
        monkeypatch.setattr(
            normalizer,
            "_try_persist",
            lambda **kw: None,
        )

    def test_real_receipt_text_promotes_to_confirmed(
        self, isolated_storage, monkeypatch,
    ):
        """A PDF that pypdf extracts as 'تم التحويل بنجاح ... رقم
        العملية' must classify as ``pdf_kind=payment_receipt`` AND
        ``payment_evidence_status=confirmed``."""
        from modules.ai.media import normalizer
        self._patch_io(
            monkeypatch,
            downloaded_bytes=b"%PDF-1.4 fake bytes",
            pdf_text=(
                "البنك الراجحي\n"
                "اسم المستفيد: أحمد محمد\n"
                "الآيبان: SA0380000000608010167519\n"
                "المبلغ: 358.00 ر.س\n"
                "تم التحويل بنجاح\n"
                "رقم العملية: TXN-9981234\n"
                "وقت تنفيذ العملية: 2026-05-17 13:45"
            ),
        )
        result = _run(normalizer.normalize_whatsapp_inbound(
            db=MagicMock(), wa_conn=MagicMock(), tenant_id=11,
            message=self._doc_message(filename="document_1778.pdf"),
        ))
        assert result.normalized_type == "document"
        assert result.metadata["pdf_kind"] == "payment_receipt"
        assert result.metadata["payment_evidence_status"] == "confirmed"
        # The brain-facing text must contain the extracted PDF text
        # so the LLM never apologises for "not being able to open
        # PDFs".
        assert "تم التحويل" in result.text
        assert "نص الملف المستخرج" in result.text

    def test_pre_transfer_review_demotes_to_pre_review(
        self, isolated_storage, monkeypatch,
    ):
        """A PDF whose body is the review-before-transfer screen
        must NOT be classified as payment_receipt — instead
        ``payment_pre_review`` so the deterministic ACK does not
        fire."""
        from modules.ai.media import normalizer
        self._patch_io(
            monkeypatch,
            downloaded_bytes=b"%PDF-1.4 fake",
            pdf_text=(
                "مراجعة بيانات التحويل\n"
                "اسم المستفيد: محمد علي\n"
                "الآيبان: SA0380000000608010167519\n"
                "المبلغ: 358 ر.س\n"
                "تأكد من البيانات واضغط تحويل"
            ),
        )
        result = _run(normalizer.normalize_whatsapp_inbound(
            db=MagicMock(), wa_conn=MagicMock(), tenant_id=11,
            message=self._doc_message(filename="Transfer-Receipt.pdf"),
        ))
        assert result.metadata["pdf_kind"] == "payment_pre_review"
        assert result.metadata["payment_evidence_status"] == \
            "pre_transfer_review"
        # The brain-facing marker must SAY this is a review screen
        # so the LLM (if it runs) doesn't say "thanks, we got your
        # receipt".
        assert "شاشة مراجعة" in result.text or "مراجعة قبل" in result.text

    def test_iban_only_screenshot_demotes_to_pending_evidence(
        self, isolated_storage, monkeypatch,
    ):
        """A PDF that's just bank/IBAN/amount with no completion
        marker → ``payment_pending_evidence``, not payment_receipt."""
        from modules.ai.media import normalizer
        self._patch_io(
            monkeypatch,
            downloaded_bytes=b"%PDF-1.4 fake",
            pdf_text=(
                "البنك الراجحي\n"
                "اسم المستفيد: أحمد\n"
                "الآيبان: SA0380000000608010167519\n"
                "المبلغ: 358 ر.س"
            ),
        )
        result = _run(normalizer.normalize_whatsapp_inbound(
            db=MagicMock(), wa_conn=MagicMock(), tenant_id=11,
            message=self._doc_message(filename="receipt.pdf"),
        ))
        assert result.metadata["pdf_kind"] == "payment_pending_evidence"
        assert result.metadata["payment_evidence_status"] == \
            "needs_confirmation"

    def test_text_extraction_failure_does_not_break_pipeline(
        self, isolated_storage, monkeypatch,
    ):
        """If pypdf returns empty text and OCR is unavailable, the
        normalizer should still produce a usable document result —
        the filename/caption heuristics take over."""
        from modules.ai.media import normalizer
        self._patch_io(
            monkeypatch,
            downloaded_bytes=b"%PDF-1.4 scanned",
            pdf_text="",
        )
        # Ensure no OPENAI_API_KEY → OCR fallback is silently skipped.
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            result = _run(normalizer.normalize_whatsapp_inbound(
                db=MagicMock(), wa_conn=MagicMock(), tenant_id=11,
                message=self._doc_message(
                    filename="إيصال_التحويل.pdf",
                    caption="هذا إيصال التحويل",
                ),
            ))
        assert result.normalized_type == "document"
        # No body text → evidence is not_payment, but filename
        # contains "إيصال" so the legacy heuristic still marks the
        # doc as payment_receipt. The webhook's deterministic ACK
        # is then guarded by the evidence status (verified in the
        # order_flow tests below).
        assert result.metadata["pdf_kind"] in {
            "payment_receipt", "payment_pending_evidence",
        }


# ──────────────────────────────────────────────────────────────────────
# 5. order_flow gate
# ──────────────────────────────────────────────────────────────────────


class TestOrderFlowReceiptGate:
    """Exercise the order_flow gates with a patched
    ``_load_brain_state`` so we don't need a real DB."""

    def _bs(self, **overrides):
        op = {
            "awaiting_payment_receipt": True,
            "order_status": "awaiting_receipt",
        }
        op.update(overrides.get("order_prep", {}) or {})
        bs = {
            "order_prep": op,
            "current_product_focus": {
                "id": "p1", "title": "عسل سدر", "price": 358,
                "currency": "SAR",
            },
        }
        return bs

    def _patch_state(self, monkeypatch, brain_state):
        """Patch order_flow._load_brain_state so the gates see the
        provided brain_state without hitting the DB."""
        from core import order_flow
        monkeypatch.setattr(
            order_flow, "_load_brain_state",
            lambda db, *, tenant_id, phone: (object(), brain_state),
        )

    def test_confirmed_receipt_fires_ack(self, monkeypatch):
        from core.order_flow import maybe_handle_receipt_inbound
        self._patch_state(monkeypatch, self._bs())
        decision = maybe_handle_receipt_inbound(
            db=MagicMock(), tenant_id=11, phone="+966500000001",
            inbound_normalized_type="document",
            inbound_metadata={
                "pdf_kind": "payment_receipt",
                "payment_evidence_status": "confirmed",
                "payment_evidence_reason": "strong_success_phrase",
            },
        )
        assert decision is not None
        assert "إيصال" in decision["reply_text"] or "وصل" in decision["reply_text"]
        sp = decision["state_patch"]
        assert sp.get("payment_receipt_received") is True
        assert sp.get("order_status") == "payment_submitted"

    def test_pre_transfer_review_does_not_fire_ack(self, monkeypatch):
        from core.order_flow import maybe_handle_receipt_inbound
        self._patch_state(monkeypatch, self._bs())
        decision = maybe_handle_receipt_inbound(
            db=MagicMock(), tenant_id=11, phone="+966500000001",
            inbound_normalized_type="document",
            inbound_metadata={
                "pdf_kind": "payment_receipt",
                "payment_evidence_status": "pre_transfer_review",
                "payment_evidence_reason": "pre_transfer_review_phrase",
            },
        )
        assert decision is None

    def test_pdf_kind_pre_review_does_not_fire_ack(self, monkeypatch):
        from core.order_flow import maybe_handle_receipt_inbound
        self._patch_state(monkeypatch, self._bs())
        decision = maybe_handle_receipt_inbound(
            db=MagicMock(), tenant_id=11, phone="+966500000001",
            inbound_normalized_type="document",
            inbound_metadata={
                "pdf_kind": "payment_pre_review",
                "payment_evidence_status": "pre_transfer_review",
            },
        )
        assert decision is None

    def test_evidence_soft_reply_fires_for_pre_review(self, monkeypatch):
        """The soft reply fires when the conversation does NOT have
        an active order awaiting a receipt — i.e. the customer sent
        a bank screenshot without an open order context. The
        active-order + awaiting_receipt path is now promoted to
        confirmed (see ``test_evidence_promotes_for_pre_review_when_active_order``).
        """
        from core.order_flow import maybe_handle_payment_evidence_inbound
        # Remove the active-order trigger: no awaiting_receipt flag.
        self._patch_state(
            monkeypatch,
            self._bs(order_prep={"awaiting_payment_receipt": False}),
        )
        decision = maybe_handle_payment_evidence_inbound(
            db=MagicMock(), tenant_id=11, phone="+966500000001",
            inbound_normalized_type="document",
            inbound_metadata={
                "pdf_kind": "payment_pre_review",
                "payment_evidence_status": "pre_transfer_review",
                "payment_evidence_reason": "pre_transfer_review_phrase",
            },
        )
        assert decision is not None
        assert decision["state_patch"] == {}, "must NOT mutate order state"
        assert "مراجعة" in decision["reply_text"]
        assert "الإيصال" in decision["reply_text"]
        assert "أمين" not in decision["reply_text"]

    def test_evidence_soft_reply_fires_for_needs_confirmation(self, monkeypatch):
        from core.order_flow import maybe_handle_payment_evidence_inbound
        # Remove the active-order trigger: no awaiting_receipt flag.
        self._patch_state(
            monkeypatch,
            self._bs(order_prep={"awaiting_payment_receipt": False}),
        )
        decision = maybe_handle_payment_evidence_inbound(
            db=MagicMock(), tenant_id=11, phone="+966500000001",
            inbound_normalized_type="image",
            inbound_metadata={
                "image_kind": "payment_pending_evidence",
                "payment_evidence_status": "needs_confirmation",
                "payment_evidence_reason": "payment_context_no_success_marker",
            },
        )
        assert decision is not None
        assert decision["state_patch"] == {}

    def test_evidence_promotes_for_pre_review_when_active_order(self, monkeypatch):
        """Hotfix coverage (May 2026): when a customer with an
        ACTIVE order + ``awaiting_payment_receipt=True`` sends a
        payment-context PDF that the classifier marked as
        ``pre_transfer_review`` (e.g. a real receipt that the bank
        printed with a "تأكيد التحويل" header), the helper must
        promote to confirmed instead of re-asking for the receipt."""
        from core.order_flow import maybe_handle_payment_evidence_inbound
        self._patch_state(monkeypatch, self._bs())  # has awaiting + product
        decision = maybe_handle_payment_evidence_inbound(
            db=MagicMock(), tenant_id=11, phone="+966500000001",
            inbound_normalized_type="document",
            inbound_metadata={
                "pdf_kind": "payment_pre_review",
                "payment_evidence_status": "pre_transfer_review",
                "payment_evidence_reason": "pre_transfer_review_phrase",
            },
        )
        assert decision is not None
        sp = decision["state_patch"]
        assert sp.get("payment_receipt_received") is True
        assert sp.get("awaiting_payment_receipt") is False
        assert sp.get("order_status") == "payment_submitted"
        # Reply should be the receipt ACK, NOT the "send me the
        # final receipt" soft reply.
        assert "مراجعة قبل التحويل" not in decision["reply_text"]
        assert (
            "وصلنا" in decision["reply_text"]
            or "إيصال" in decision["reply_text"]
        )

    def test_evidence_soft_reply_skips_unrelated_inbound(self, monkeypatch):
        from core.order_flow import maybe_handle_payment_evidence_inbound
        self._patch_state(monkeypatch, self._bs())
        decision = maybe_handle_payment_evidence_inbound(
            db=MagicMock(), tenant_id=11, phone="+966500000001",
            inbound_normalized_type="document",
            inbound_metadata={
                "pdf_kind": "unknown",
                "payment_evidence_status": "not_payment",
            },
        )
        assert decision is None
