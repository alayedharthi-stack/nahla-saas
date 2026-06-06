"""
tests/test_receipt_text_quality.py
──────────────────────────────────
P0 Bank Receipt Extraction Reliability — measurement layer tests.

Guarantees:
  * Pure functions never raise on garbage input.
  * Garbled bank-export patterns score lower than clean Arabic text.
  * Shadow OCR escalation never implies live OCR was invoked.
  * Telemetry kill switch is default OFF and independent.
  * No payment / order-flow behaviour is touched by this module.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


GARBLED_SAMPLE = (
    "Transfer Receipt\nDate ...\nSARAmount 193 ...\n"
    "Al Rajhi Bank\n...\nكيلوبه%لاالإيصال"
)
CLEAN_SAMPLE = (
    "إيصال التحويل\nالتاريخ: 2026-01-15\nالمبلغ: 193 ريال\n"
    "مصرف الراجحي\nالمستفيد: تركي بن عايد\n"
    "SA0380000000608010167519\nتم التحويل بنجاح"
)


def test_compute_text_quality_never_raises() -> None:
    from core.receipt_text_quality import compute_text_quality

    for blob in (None, "", "\x00\x01", "x" * 5000):
        snap = compute_text_quality(blob)
        assert 0.0 <= snap.quality_score <= 1.0


def test_garbled_scores_lower_than_clean() -> None:
    from core.receipt_text_quality import compute_text_quality

    garbled = compute_text_quality(GARBLED_SAMPLE)
    clean = compute_text_quality(CLEAN_SAMPLE)
    assert garbled.quality_score < clean.quality_score
    assert garbled.is_garbled is True
    assert clean.is_garbled is False


def test_is_garbled_text_wrapper() -> None:
    from core.receipt_text_quality import is_garbled_text

    assert is_garbled_text(GARBLED_SAMPLE) is True
    assert is_garbled_text(CLEAN_SAMPLE) is False


def test_glued_token_triggers_garble_reason() -> None:
    from core.receipt_text_quality import compute_text_quality

    snap = compute_text_quality("Transfer Receipt\nSARAmount 193")
    assert "glued_tokens" in snap.garble_reasons
    assert snap.glued_token_count >= 1


def test_shadow_escalation_for_garbled_payment_pdf() -> None:
    from core.receipt_text_quality import compute_ocr_escalation_shadow

    shadow = compute_ocr_escalation_shadow(
        text=GARBLED_SAMPLE,
        pdf_kind="payment_receipt",
        pdf_text_status="ok",
        metadata={
            "filename": "Transfer-Receipt.pdf",
            "pdf_text_full": GARBLED_SAMPLE,
        },
    )
    assert shadow.would_escalate is True
    assert shadow.pypdf_succeeded is True
    assert shadow.shadow_reason in {
        "garbled_text",
        "garbled_and_core_fields_unreliable",
        "low_quality_and_core_fields_unreliable",
    }
    assert shadow.to_log_dict()["ocr_not_invoked"] is True


def test_shadow_no_escalation_for_clean_payment_pdf() -> None:
    from core.receipt_text_quality import compute_ocr_escalation_shadow

    shadow = compute_ocr_escalation_shadow(
        text=CLEAN_SAMPLE,
        pdf_kind="payment_receipt",
        pdf_text_status="ok",
        metadata={
            "filename": "receipt.pdf",
            "pdf_text_full": CLEAN_SAMPLE,
        },
    )
    assert shadow.would_escalate is False
    assert shadow.shadow_reason == "quality_acceptable"


def test_shadow_not_applicable_for_non_payment_kind() -> None:
    from core.receipt_text_quality import compute_ocr_escalation_shadow

    shadow = compute_ocr_escalation_shadow(
        text=GARBLED_SAMPLE,
        pdf_kind="invoice",
        pdf_text_status="ok",
    )
    assert shadow.would_escalate is False
    assert shadow.shadow_reason == "not_payment_candidate"


def test_shadow_empty_pypdf_would_escalate() -> None:
    from core.receipt_text_quality import compute_ocr_escalation_shadow

    shadow = compute_ocr_escalation_shadow(
        text="",
        pdf_kind="payment_pending_evidence",
        pdf_text_status="empty",
    )
    assert shadow.would_escalate is True
    assert shadow.shadow_reason == "empty_pypdf_text"


def test_telemetry_kill_switch_default_off(monkeypatch) -> None:
    from core.receipt_text_quality import (
        is_receipt_text_quality_telemetry_enabled,
        log_text_quality,
        compute_text_quality,
    )

    monkeypatch.delenv("RECEIPT_TEXT_QUALITY_TELEMETRY_ENABLED", raising=False)
    assert is_receipt_text_quality_telemetry_enabled() is False

    caplog = []
    import logging as _logging

    class _H(_logging.Handler):
        def emit(self, record):
            caplog.append(record.getMessage())

    h = _H()
    logger = _logging.getLogger("nahla.receipt_text_quality")
    logger.addHandler(h)
    logger.setLevel(_logging.INFO)
    try:
        log_text_quality(
            tenant_id=1,
            source="test",
            snapshot=compute_text_quality("x"),
        )
    finally:
        logger.removeHandler(h)
    assert not any("[RECEIPT_TEXT_QUALITY]" in m for m in caplog)


def test_telemetry_emits_when_enabled(monkeypatch, caplog) -> None:
    from core.receipt_text_quality import (
        compute_text_quality,
        log_text_quality,
    )

    monkeypatch.setenv("RECEIPT_TEXT_QUALITY_TELEMETRY_ENABLED", "1")
    caplog.set_level(logging.INFO, logger="nahla.receipt_text_quality")
    log_text_quality(
        tenant_id=7,
        media_id="m1",
        source="test",
        snapshot=compute_text_quality(GARBLED_SAMPLE),
        pdf_text_status="ok",
    )
    assert any("[RECEIPT_TEXT_QUALITY]" in r.message for r in caplog.records)
    assert any("is_garbled=True" in r.message for r in caplog.records)


def test_stamp_measurement_metadata_only_when_flag_on(
    monkeypatch,
) -> None:
    from core.receipt_text_quality import stamp_measurement_metadata

    meta: dict = {}
    monkeypatch.delenv("RECEIPT_TEXT_QUALITY_TELEMETRY_ENABLED", raising=False)
    stamp_measurement_metadata(meta, pypdf_text=GARBLED_SAMPLE, pypdf_status="ok")
    assert "receipt_text_quality_score" not in meta

    monkeypatch.setenv("RECEIPT_TEXT_QUALITY_TELEMETRY_ENABLED", "1")
    stamp_measurement_metadata(meta, pypdf_text=GARBLED_SAMPLE, pypdf_status="ok")
    assert "receipt_text_quality_score" in meta
    assert meta["receipt_text_is_garbled"] is True


def test_shadow_already_ocr_path_does_not_re_escalate() -> None:
    from core.receipt_text_quality import compute_ocr_escalation_shadow

    shadow = compute_ocr_escalation_shadow(
        text=GARBLED_SAMPLE,
        pdf_kind="payment_receipt",
        pdf_text_status="ocr",
    )
    assert shadow.would_escalate is False
    assert shadow.shadow_reason == "already_ocr_path"

