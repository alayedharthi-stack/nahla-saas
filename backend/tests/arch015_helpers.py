"""
arch015_helpers.py
──────────────────
Shared fixtures for ARCH-015-FIX Tests First phase.

Official system contract (Truth Regression if violated):
  INV-1: payment_* kind → pe ∈ {confirmed, pre_transfer_review, needs_confirmation}
  INV-2: pe == not_payment → no payment_* kind slot
  INV-3: canonical pairing (payment_pre_review ↔ pre_transfer_review, etc.)
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

PAYMENT_KIND_PE_PAIRS: Dict[str, str] = {
    "payment_pre_review": "pre_transfer_review",
    "payment_pending_evidence": "needs_confirmation",
    "payment_receipt": "confirmed",
}
PAYMENT_KINDS = frozenset(PAYMENT_KIND_PE_PAIRS)
WEAK_PE_STATUSES = frozenset({"pre_transfer_review", "needs_confirmation"})


def assert_truth_consistent(metadata: Dict[str, Any]) -> None:
    """INV-1 / INV-2 / INV-3 — raises AssertionError on contradiction."""
    md = metadata or {}
    pe = str(md.get("payment_evidence_status") or "").strip()
    pdf_kind = str(md.get("pdf_kind") or "").strip()
    image_kind = str(md.get("image_kind") or "").strip()
    kind = pdf_kind or image_kind

    if pe == "not_payment":
        assert pdf_kind not in PAYMENT_KINDS, (
            f"INV-2 violated: not_payment + pdf_kind={pdf_kind!r}"
        )
        assert image_kind not in PAYMENT_KINDS, (
            f"INV-2 violated: not_payment + image_kind={image_kind!r}"
        )
        return

    if kind in PAYMENT_KINDS:
        expected_pe = PAYMENT_KIND_PE_PAIRS[kind]
        assert pe == expected_pe, (
            f"INV-3 violated: {kind!r} requires pe={expected_pe!r}, got {pe!r}"
        )
        assert pe in WEAK_PE_STATUSES | {"confirmed"}, (
            f"INV-1 violated: kind={kind!r} with pe={pe!r}"
        )


def merge_metadata_production_semantics(
    base_meta: Dict[str, Any],
    overridden: Dict[str, Any],
) -> Dict[str, Any]:
    """Mirror normalizer merge including B3 stale-key removal."""
    out = deepcopy(base_meta)
    out.update(overridden)
    for key in ("image_kind", "pdf_kind"):
        if key not in overridden and key in out:
            out.pop(key, None)
    return out


def apply_semantic_layers_like_normalizer(
    base_meta: Dict[str, Any],
    *,
    text_blob: str = "",
    caption: str = "",
    filename: str = "",
    normalized_type: str = "document",
    tenant_id: int = 11,
) -> Dict[str, Any]:
    """Run production ``_apply_semantic_media_classification`` in-place."""
    from modules.ai.media import normalizer

    meta = deepcopy(base_meta)
    normalizer._apply_semantic_media_classification(
        base_meta=meta,
        text_blob=text_blob,
        caption=caption,
        filename=filename,
        normalized_type=normalized_type,
        tenant_id=tenant_id,
    )
    return meta


def build_metadata_after_payment_gate(
    text: str,
    *,
    filename: str = "",
    caption: str = "",
    normalized_type: str = "document",
    non_commerce_category: Optional[str] = None,
) -> Dict[str, Any]:
    """Payment-evidence gate + kind slot + semantic layers (image/document)."""
    from core.payment_evidence import classify_payment_evidence

    ev = classify_payment_evidence(
        "\n".join(filter(None, [filename, caption, text])),
        filename=filename or None,
    )
    meta: Dict[str, Any] = {
        "payment_evidence_status": ev["status"],
        "payment_evidence_reason": ev["reason"],
        "payment_evidence_signals": ev.get("signals") or {},
    }
    if non_commerce_category:
        meta["non_commerce_category"] = non_commerce_category

    pe = ev["status"]
    kind_key = "pdf_kind" if normalized_type == "document" else "image_kind"
    if pe == "confirmed":
        meta[kind_key] = "payment_receipt"
    elif pe == "pre_transfer_review":
        meta[kind_key] = "payment_pre_review"
    elif pe == "needs_confirmation":
        meta[kind_key] = "payment_pending_evidence"

    return apply_semantic_layers_like_normalizer(
        meta,
        text_blob=text,
        caption=caption,
        filename=filename,
        normalized_type=normalized_type,
    )


def _doc_message(*, filename: str = "doc.pdf", caption: str = ""):
    return {
        "type": "document",
        "document": {
            "id": "wa-doc-arch015",
            "mime_type": "application/pdf",
            "filename": filename,
            "caption": caption,
        },
        "timestamp": "1700000000",
        "id": "wa-msg-arch015",
    }


def patch_document_io(monkeypatch, *, downloaded_bytes: bytes, pdf_text: str):
    """Patch normalizer I/O for PDF E2E (same pattern as test_payment_evidence)."""
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
    monkeypatch.setattr(normalizer, "_try_persist", lambda **kw: None)


async def normalize_pdf_async(
    monkeypatch,
    *,
    pdf_text: str,
    filename: str,
    isolated_storage,
) -> Dict[str, Any]:
    from modules.ai.media import normalizer

    patch_document_io(
        monkeypatch,
        downloaded_bytes=b"%PDF-1.4 arch015",
        pdf_text=pdf_text,
    )
    result = await normalizer.normalize_whatsapp_inbound(
        db=MagicMock(),
        wa_conn=MagicMock(),
        tenant_id=11,
        message=_doc_message(filename=filename),
    )
    return dict(result.metadata or {})


def normalize_pdf(monkeypatch, *, pdf_text: str, filename: str, isolated_storage) -> Dict[str, Any]:
    return asyncio.run(
        normalize_pdf_async(
            monkeypatch,
            pdf_text=pdf_text,
            filename=filename,
            isolated_storage=isolated_storage,
        )
    )


def brain_state_active_awaiting(**order_prep_overrides):
    op = {
        "awaiting_payment_receipt": True,
        "order_status": "awaiting_receipt",
    }
    op.update(order_prep_overrides)
    return {
        "order_prep": op,
        "current_product_focus": {
            "id": "p1",
            "title": "عسل سدر",
            "price": 358,
            "currency": "SAR",
        },
    }


def brain_state_not_awaiting():
    return brain_state_active_awaiting(awaiting_payment_receipt=False)


@pytest.fixture
def isolated_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "services.inbound_media_storage._STORAGE_ROOT",
        __import__("pathlib").Path(tmp_path).resolve(),
    )
    yield tmp_path
