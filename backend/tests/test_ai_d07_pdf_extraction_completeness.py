"""AI-D07 — generic PDF extraction completeness gate + OCR supplement path."""
from __future__ import annotations

import asyncio
import os
import sys
from io import BytesIO
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_BACKEND, os.path.join(_REPO, "database"), _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.bank_transfer_receipt_resolver import (  # noqa: E402
    build_receipt_data,
    extract_bank_receipt_fields,
)
from core.payment_evidence import classify_payment_evidence  # noqa: E402
from core.payment_receipt_field_parser import parse_payment_receipt_fields  # noqa: E402
from core.tenant_payment_accounts import (  # noqa: E402
    TenantPaymentAccounts,
    canonical_iban,
    receipt_matches_tenant_accounts,
)
from modules.ai.media import normalizer  # noqa: E402
from modules.ai.media.pdf_extraction_completeness import (  # noqa: E402
    assess_pdf_extraction_completeness,
    merge_primary_and_ocr_text,
)


GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_CUSTOMER = "أحمد سالم"
GENERIC_CITY = "الرياض"
TENANT_IBAN = "SA0310000000000000001234"
MISMATCH_IBAN = "SA0310000000000000009999"
GENERIC_BENEFICIARY = "شركة النور للتجارة"
GENERIC_BENEFICIARY_ASCII = "Noor Trading Company LLC"

COMPLETE_RECEIPT_TEXT = (
    f"Bank Transfer Receipt\n"
    f"Merchant: {GENERIC_MERCHANT}\n"
    f"Customer: {GENERIC_CUSTOMER}\n"
    f"City: {GENERIC_CITY}\n"
    f"Beneficiary: {GENERIC_BENEFICIARY_ASCII}\n"
    f"IBAN: {TENANT_IBAN}\n"
    f"Amount: 1500.00 SAR\n"
    f"Reference Number: REF-9876543210\n"
    f"Transfer Status: Completed Successfully\n"
    f"Transaction Date: 2026-08-21 14:30\n"
    f"From Account: SA1200000000000000005678\n"
    f"Bank Name: Example Bank\n"
    f"Authorization Code: AUTH1234567890\n"
    f"Service Fee: 0.00 SAR\n"
    f"Notes: Payment for order RRRD1234\n"
)

SPARSE_LABEL_TEXT = (
    f"Bank Transfer Receipt\n"
    f"Merchant Store: {GENERIC_MERCHANT}\n"
    f"Customer Name:\n"
    f"Beneficiary Name:\n"
    f"Destination IBAN:\n"
    f"Amount:\n"
    f"Reference Number:\n"
    f"Transfer Date:\n"
    f"Status:\n"
    f"Notes:\n"
    f"Customer City:\n"
    f"Authorization Code:\n"
    f"Transfer Channel:\n"
    f"Payment Purpose:\n"
)

RECOVERED_OCR_TEXT = (
    f"Beneficiary: {GENERIC_BENEFICIARY}\n"
    f"IBAN: {TENANT_IBAN}\n"
    f"Amount: 1500.00 SAR\n"
    f"Reference Number: REF-9876543210\n"
    f"Transfer Status: Completed\n"
    f"Customer: {GENERIC_CUSTOMER}\n"
    f"City: {GENERIC_CITY}\n"
)

MISMATCH_OCR_TEXT = RECOVERED_OCR_TEXT.replace(TENANT_IBAN, MISMATCH_IBAN)


def _run(coro):
    return asyncio.run(coro)


def _make_text_pdf(text: str) -> bytes:
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import (  # noqa: PLC0415
        ContentStream,
        DictionaryObject,
        NameObject,
    )

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=595, height=842)

    lines = (text or "").splitlines() or [""]
    parts = [b"BT /F1 12 Tf 50 750 Td"]
    for idx, line in enumerate(lines):
        if idx > 0:
            parts.append(b"0 -14 Td")
        safe = line.encode("latin-1", errors="replace")
        parts.append(b"(" + safe + b") Tj")
    parts.append(b"ET")
    stream_bytes = b"\n".join(parts)

    cs = ContentStream(None, writer)
    cs._data = stream_bytes
    page[NameObject("/Contents")] = writer._add_object(cs)

    font_dict = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_ref = writer._add_object(font_dict)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): font_ref,
        }),
    })

    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_sparse_overlay_pdf(label_text: str, *, byte_pad: int | None = None) -> bytes:
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import (  # noqa: PLC0415
        ContentStream,
        DictionaryObject,
        NameObject,
        NumberObject,
        StreamObject,
    )

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=595, height=842)

    lines = (label_text or "").splitlines() or [""]
    parts = [b"BT /F1 12 Tf 50 750 Td"]
    for idx, line in enumerate(lines):
        if idx > 0:
            parts.append(b"0 -14 Td")
        safe = line.encode("latin-1", errors="replace")
        parts.append(b"(" + safe + b") Tj")
    parts.append(b"ET")
    parts.append(b"q 1 0 0 1 0 0 cm /Im1 Do Q")
    stream_bytes = b"\n".join(parts)

    cs = ContentStream(None, writer)
    cs._data = stream_bytes
    page[NameObject("/Contents")] = writer._add_object(cs)

    font_dict = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_ref = writer._add_object(font_dict)

    image_dict = DictionaryObject({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(800),
        NameObject("/Height"): NumberObject(1100),
        NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
        NameObject("/BitsPerComponent"): NumberObject(8),
    })
    image_stream = StreamObject()
    image_stream._data = b"\x00" * 128
    image_dict[NameObject("/Length")] = NumberObject(len(image_stream._data))
    image_ref = writer._add_object(image_dict)

    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): font_ref,
        }),
        NameObject("/XObject"): DictionaryObject({
            NameObject("/Im1"): image_ref,
        }),
    })

    if byte_pad:
        pad_stream = StreamObject()
        pad_stream._data = b"0" * byte_pad
        writer._add_object(pad_stream)

    buf = BytesIO()
    writer.write(buf)
    data = buf.getvalue()
    if byte_pad and len(data) < 40_000:
        extra = StreamObject()
        extra._data = b"0" * max(byte_pad, 40_000 - len(data))
        writer._add_object(extra)
        buf = BytesIO()
        writer.write(buf)
        data = buf.getvalue()
    return data


def _tenant_accounts(*, ibans: tuple[str, ...]) -> TenantPaymentAccounts:
    return TenantPaymentAccounts(
        ibans=ibans,
        beneficiaries=(GENERIC_BENEFICIARY,),
        bank_brands=("Example Bank",),
        section_ids=(101,),
    )


class TestPdfExtractionCompletenessHelpers:
    def test_merge_primary_and_ocr_text_keeps_both(self) -> None:
        primary = "Amount:\nBeneficiary Name:"
        ocr = RECOVERED_OCR_TEXT
        merged = merge_primary_and_ocr_text(primary, ocr)
        assert "Amount:" in merged
        assert TENANT_IBAN in merged

    def test_merge_returns_ocr_when_primary_empty(self) -> None:
        assert merge_primary_and_ocr_text("", RECOVERED_OCR_TEXT) == RECOVERED_OCR_TEXT


class TestControlACompletePrimaryText:
    def test_complete_pdf_is_ok_without_ocr(self) -> None:
        pdf_bytes = _make_text_pdf(COMPLETE_RECEIPT_TEXT)
        result = normalizer._extract_pdf_text(pdf_bytes, tenant_id=1, media_id="a1")
        assert result["extraction_status"] == "ok"
        assert result["ocr_required"] is False
        assert result["completeness_reason"] == "complete"

        parsed = parse_payment_receipt_fields(result["text"])
        assert parsed.amount
        assert parsed.reference_number
        assert parsed.beneficiary_name
        assert TENANT_IBAN in result["text"]


class TestControlBSparseOverlayRecovery:
    def test_sparse_overlay_marks_incomplete_and_requires_ocr(self) -> None:
        pdf_bytes = _make_sparse_overlay_pdf(SPARSE_LABEL_TEXT, byte_pad=45_000)
        result = normalizer._extract_pdf_text(pdf_bytes, tenant_id=1, media_id="b1")
        assert result["extraction_status"] == "incomplete"
        assert result["ocr_required"] is True
        assert result["completeness_reason"] in {
            "sparse_overlay",
            "incomplete_value_coverage",
        }
        assert GENERIC_BENEFICIARY not in result["text"]
        assert TENANT_IBAN not in result["text"]

    def test_process_document_supplements_non_empty_primary_with_ocr(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pdf_bytes = _make_sparse_overlay_pdf(SPARSE_LABEL_TEXT, byte_pad=45_000)
        monkeypatch.setattr(
            normalizer,
            "_download_meta_media",
            AsyncMock(return_value={
                "bytes": pdf_bytes,
                "mime_type": "application/pdf",
            }),
        )
        monkeypatch.setattr(normalizer, "_try_persist", lambda **kw: None)
        monkeypatch.setattr(normalizer, "_runtime_openai_key", lambda: "sk-test")

        async def fake_ocr(*args: Any, **kwargs: Any) -> str:
            return RECOVERED_OCR_TEXT

        monkeypatch.setattr(normalizer, "_ocr_pdf_with_vision", fake_ocr)

        result = _run(normalizer._process_document(
            db=MagicMock(),
            wa_conn=MagicMock(),
            tenant_id=7,
            document_payload={
                "id": "doc-sparse",
                "mime_type": "application/pdf",
                "filename": "transfer.pdf",
                "caption": "",
            },
            ts_raw="1700000000",
            wa_msg_id="wa-sparse",
        ))

        meta = result.metadata
        assert meta["pdf_ocr_required"] is True
        assert meta["pdf_text_status"] == "ocr_supplemented"
        assert TENANT_IBAN in (meta.get("pdf_text_full") or "")
        assert GENERIC_BENEFICIARY in (meta.get("pdf_text_full") or "")


class TestControlCStructuredFieldsAfterRecovery:
    def test_recovered_text_populates_receipt_fields(self) -> None:
        merged = merge_primary_and_ocr_text(SPARSE_LABEL_TEXT, RECOVERED_OCR_TEXT)
        parsed = parse_payment_receipt_fields(merged)
        extraction = extract_bank_receipt_fields(merged, filename="transfer.pdf")
        receipt = build_receipt_data(extraction)

        assert parsed.amount
        assert parsed.reference_number
        assert parsed.beneficiary_name
        assert TENANT_IBAN in merged
        assert extraction.beneficiary_iban == TENANT_IBAN
        assert receipt["amount"]
        assert receipt["reference_number"]
        assert receipt["beneficiary_name"] or receipt.get("beneficiary_iban")


class TestControlDTenantMatch:
    def test_recovered_text_matches_tenant_accounts(self) -> None:
        merged = merge_primary_and_ocr_text(SPARSE_LABEL_TEXT, RECOVERED_OCR_TEXT)
        verdict = receipt_matches_tenant_accounts(
            accounts=_tenant_accounts(ibans=(canonical_iban(TENANT_IBAN),)),
            receipt_text=merged,
        )
        assert verdict["status"] == "match"
        assert verdict["matched_iban"] == canonical_iban(TENANT_IBAN)


class TestControlETenantMismatch:
    def test_recovered_mismatch_iban_does_not_match(self) -> None:
        merged = merge_primary_and_ocr_text(SPARSE_LABEL_TEXT, MISMATCH_OCR_TEXT)
        verdict = receipt_matches_tenant_accounts(
            accounts=_tenant_accounts(ibans=(canonical_iban(TENANT_IBAN),)),
            receipt_text=merged,
        )
        assert verdict["status"] == "mismatch"
        assert not verdict.get("matched_iban")


class TestControlFUnrecoverable:
    def test_empty_ocr_does_not_invent_fields(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pdf_bytes = _make_sparse_overlay_pdf(SPARSE_LABEL_TEXT, byte_pad=45_000)
        monkeypatch.setattr(
            normalizer,
            "_download_meta_media",
            AsyncMock(return_value={
                "bytes": pdf_bytes,
                "mime_type": "application/pdf",
            }),
        )
        monkeypatch.setattr(normalizer, "_try_persist", lambda **kw: None)
        monkeypatch.setattr(normalizer, "_runtime_openai_key", lambda: "sk-test")
        monkeypatch.setattr(normalizer, "_ocr_pdf_with_vision", AsyncMock(return_value=""))

        result = _run(normalizer._process_document(
            db=MagicMock(),
            wa_conn=MagicMock(),
            tenant_id=7,
            document_payload={
                "id": "doc-unrecoverable",
                "mime_type": "application/pdf",
                "filename": "transfer.pdf",
                "caption": "",
            },
            ts_raw="1700000000",
            wa_msg_id="wa-unrecoverable",
        ))

        meta = result.metadata
        assert meta["pdf_text_status"] == "incomplete"
        full_text = meta.get("pdf_text_full") or ""
        parsed = parse_payment_receipt_fields(full_text)
        assert not parsed.amount
        assert TENANT_IBAN not in full_text
        assert not extract_bank_receipt_fields(full_text).beneficiary_iban

        evidence = classify_payment_evidence(meta.get("pdf_text_full") or "")
        assert evidence.get("status") != "confirmed"


class TestControlGNonPaymentPdf:
    def test_dense_catalog_pdf_is_complete_despite_receipt_filename(self) -> None:
        catalog_text = (
            "Product Catalog Spring 2026\n"
            + "\n".join(
                f"Item {idx}: Generic product description with selectable text "
                f"and SKU-{idx:04d} for {GENERIC_MERCHANT}."
                for idx in range(1, 40)
            )
        )
        pdf_bytes = _make_text_pdf(catalog_text)
        result = normalizer._extract_pdf_text(
            pdf_bytes, tenant_id=1, media_id="g1",
        )
        assessment = assess_pdf_extraction_completeness(
            result["text"],
            pdf_bytes,
            result["page_count"],
        )
        assert assessment.complete is True
        assert assessment.ocr_required is False

    def test_small_memo_pdf_does_not_force_ocr_for_receipt_filename(self) -> None:
        memo = (
            f"Internal memo for {GENERIC_MERCHANT}\n"
            f"Customer {GENERIC_CUSTOMER} asked about shipping to {GENERIC_CITY}.\n"
            f"Follow up tomorrow. Ticket REF-1234567890."
        )
        pdf_bytes = _make_text_pdf(memo)
        result = normalizer._extract_pdf_text(
            pdf_bytes, tenant_id=1, media_id="g2",
        )
        assert result["ocr_required"] is False
        assert result["completeness_reason"] == "complete"


class TestControlHTenantIsolation:
    def test_tenant_a_iban_does_not_match_tenant_b_accounts(self) -> None:
        merged = merge_primary_and_ocr_text(SPARSE_LABEL_TEXT, RECOVERED_OCR_TEXT)
        tenant_a = TenantPaymentAccounts(
            ibans=(canonical_iban(TENANT_IBAN),),
            beneficiaries=(GENERIC_BENEFICIARY,),
        )
        tenant_b = TenantPaymentAccounts(
            ibans=(canonical_iban(MISMATCH_IBAN),),
            beneficiaries=("شركة أخرى للتجارة",),
        )
        match_a = receipt_matches_tenant_accounts(
            accounts=tenant_a, receipt_text=merged,
        )
        match_b = receipt_matches_tenant_accounts(
            accounts=tenant_b, receipt_text=merged,
        )
        assert match_a["status"] == "match"
        assert match_b["status"] == "mismatch"


class TestControlJOcrGatingWithNonEmptyPrimary:
    def test_process_document_invokes_ocr_when_required_and_text_present(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: Dict[str, int] = {"ocr": 0}

        monkeypatch.setattr(
            normalizer,
            "_download_meta_media",
            AsyncMock(return_value={
                "bytes": b"%PDF-1.4 fake",
                "mime_type": "application/pdf",
            }),
        )
        monkeypatch.setattr(normalizer, "_try_persist", lambda **kw: None)
        monkeypatch.setattr(normalizer, "_runtime_openai_key", lambda: "sk-test")
        monkeypatch.setattr(
            normalizer,
            "_extract_pdf_text",
            lambda *a, **kw: {
                "text": "Amount:\nBeneficiary Name:\nReference Number:",
                "page_count": 1,
                "extraction_status": "incomplete",
                "ocr_required": True,
                "completeness_reason": "incomplete_value_coverage",
                "completeness_signals": {"dangling_label_lines": 3},
            },
        )

        async def fake_ocr(*args: Any, **kwargs: Any) -> str:
            calls["ocr"] += 1
            return RECOVERED_OCR_TEXT

        monkeypatch.setattr(normalizer, "_ocr_pdf_with_vision", fake_ocr)

        result = _run(normalizer._process_document(
            db=MagicMock(),
            wa_conn=MagicMock(),
            tenant_id=9,
            document_payload={
                "id": "doc-gate",
                "mime_type": "application/pdf",
                "filename": "transfer.pdf",
                "caption": "",
            },
            ts_raw="1700000000",
            wa_msg_id="wa-gate",
        ))

        assert calls["ocr"] == 1
        assert result.metadata["pdf_text_status"] == "ocr_supplemented"
        assert TENANT_IBAN in (result.metadata.get("pdf_text_full") or "")
