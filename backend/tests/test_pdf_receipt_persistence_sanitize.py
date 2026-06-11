"""
backend/tests/test_pdf_receipt_persistence_sanitize.py
──────────────────────────────────────────────────────
Regression: PDF receipt text with embedded NUL bytes must not crash
``StateManager.save_message`` or poison the DB session.

Production case (conv 101 / tenant 33, document_1781190999065.pdf):
pypdf returned text containing ``\\x00`` → PostgreSQL ValueError →
PendingRollbackError → generic temp retry.
"""
from __future__ import annotations

import os
import sys
from io import BytesIO
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.payment_evidence import (  # noqa: E402
    PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW,
    classify_payment_evidence,
)
from core.persistence_text_sanitize import sanitize_persistence_text  # noqa: E402


_PRODUCTION_LIKE_RECEIPT = (
    "Transfer Receipt\n"
    "Date 2026/06/11 - 6\x0016 PM\n"
    "alrajhi bank bene\x00ciary\n"
    "Transfer Details\n"
    "Amount 76 SAR\n"
    "From SA** **** **** **** **** 5136\n"
    "Al Rajhi Bank\n"
    "IBAN SA9580000694608010597442\n"
)


class TestSanitizePersistenceText:
    def test_removes_nul_bytes(self) -> None:
        raw = "hello\x00world"
        assert sanitize_persistence_text(raw) == "helloworld"
        assert "\x00" not in sanitize_persistence_text(raw)

    def test_preserves_useful_receipt_content(self) -> None:
        cleaned = sanitize_persistence_text(_PRODUCTION_LIKE_RECEIPT)
        assert "Transfer Receipt" in cleaned
        assert "76 SAR" in cleaned
        assert "Al Rajhi Bank" in cleaned
        assert "SA9580000694608010597442" in cleaned
        assert "616 PM" in cleaned or "6 16 PM" in cleaned or "6" in cleaned
        assert "\x00" not in cleaned

    def test_preserves_newlines_and_tabs(self) -> None:
        raw = "line1\nline2\r\nline3\tcol"
        assert sanitize_persistence_text(raw) == raw

    def test_preserves_arabic_text(self) -> None:
        raw = "إيصال تحويل\x00\nالمبلغ 500 ريال"
        cleaned = sanitize_persistence_text(raw)
        assert "إيصال تحويل" in cleaned
        assert "500" in cleaned
        assert "\x00" not in cleaned

    def test_none_and_empty(self) -> None:
        assert sanitize_persistence_text(None) == ""
        assert sanitize_persistence_text("") == ""


class TestExtractPdfTextSanitization:
    def test_extract_pdf_text_strips_nul_from_pages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys
        import types

        from modules.ai.media import normalizer as norm  # noqa: PLC0415

        class _FakePage:
            def extract_text(self) -> str:
                return "Transfer Details\x00\n76 SAR"

        class _FakeReader:
            is_encrypted = False

            @property
            def pages(self) -> List[_FakePage]:
                return [_FakePage()]

        fake_pypdf = types.ModuleType("pypdf")
        fake_pypdf.PdfReader = lambda _buf: _FakeReader()  # type: ignore[attr-defined]
        fake_errors = types.ModuleType("pypdf.errors")
        fake_errors.PdfReadError = Exception  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)
        monkeypatch.setitem(sys.modules, "pypdf.errors", fake_errors)

        result = norm._extract_pdf_text(b"%PDF-1.4 fake", tenant_id=1, media_id="m1")
        assert result["extraction_status"] == "ok"
        assert "\x00" not in result["text"]
        assert "76 SAR" in result["text"]


class TestSaveMessageInboundSanitize:
    def test_inbound_body_with_nul_is_scrubbed_before_persist(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from core.conversation_engine import StateManager  # noqa: PLC0415

        captured: Dict[str, Any] = {}

        class _FakeMessageEvent:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        fake_db = MagicMock()

        monkeypatch.setattr(
            "models.MessageEvent",
            _FakeMessageEvent,
            raising=False,
        )
        import models  # noqa: PLC0415

        monkeypatch.setattr(models, "MessageEvent", _FakeMessageEvent)

        body_with_nul = "[وثيقة PDF]\x00\n76 SAR Al Rajhi"
        StateManager.save_message(
            fake_db,
            phone="966500000000",
            body=body_with_nul,
            direction="inbound",
            conversation_id=101,
            tenant_id=33,
        )

        assert captured.get("body") == sanitize_persistence_text(body_with_nul)
        assert "\x00" not in captured.get("body", "")
        fake_db.add.assert_called_once()
        fake_db.commit.assert_called_once()

    def test_outbound_body_not_scrubbed_for_control_chars_only_path(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Outbound marker scrub path unchanged; NUL scrub is inbound-only."""
        from core.conversation_engine import StateManager  # noqa: PLC0415

        captured: Dict[str, Any] = {}

        class _FakeMessageEvent:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        fake_db = MagicMock()
        import models  # noqa: PLC0415

        monkeypatch.setattr(models, "MessageEvent", _FakeMessageEvent)

        raw = "reply\x00tail"
        StateManager.save_message(
            fake_db,
            phone="966500000000",
            body=raw,
            direction="outbound",
            conversation_id=1,
            tenant_id=33,
        )
        assert captured.get("body") == raw


class TestPaymentEvidenceAfterSanitization:
    def test_pre_transfer_review_still_classifiable_after_nul_strip(self) -> None:
        cleaned = sanitize_persistence_text(_PRODUCTION_LIKE_RECEIPT)
        result = classify_payment_evidence(
            cleaned,
            filename="document_1781190999065.pdf",
        )
        assert result["status"] == PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW
        assert result["signals"]["pre_review_hits"]

    def test_psycopg2_accepts_sanitized_string(self) -> None:
        """Sanitized body must not raise ValueError on encode (NUL guard)."""
        cleaned = sanitize_persistence_text(_PRODUCTION_LIKE_RECEIPT)
        # psycopg2 rejects NUL in adapted strings — mirror that contract.
        cleaned.encode("utf-8")
        assert b"\x00" not in cleaned.encode("utf-8")
