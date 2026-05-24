"""
tests/test_receipt_tenant_account_verification.py
─────────────────────────────────────────────────
Tenant 33 #48 (May 2026) — payment understanding correction.

These tests pin the new tenant-account verification gate inside
``core.order_flow.maybe_handle_receipt_inbound`` and
``core.order_flow.maybe_handle_payment_evidence_inbound``.

Behaviour summary
─────────────────

  * When the deterministic ``payment_evidence`` classifier said
    ``confirmed`` AND the merchant has registered payment accounts
    in the KB, the receipt handler must verify the OCR's IBAN /
    beneficiary against those accounts BEFORE flipping
    ``payment_receipt_received=True`` /
    ``order_status='under_review'``.

  * On a mismatch, the handler must return ``None`` so the brain
    composes its own natural reply (no hardcoded copy).

  * Tenants without registered accounts keep legacy behaviour —
    the gate is purely additive, never regressive.

Coverage map
────────────
1. Tenant has no accounts → legacy behaviour preserved.
2. Tenant has accounts + receipt IBAN matches → flip allowed.
3. Tenant has accounts + receipt IBAN mismatches → flip BLOCKED.
4. Tenant has accounts + receipt has no IBAN tokens → flip BLOCKED.
5. Active-order promotion branch in
   ``maybe_handle_payment_evidence_inbound`` is also gated.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest


_BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


_TENANT_IBAN = "SA0380000000608010167519"


# ── Shared fixtures ────────────────────────────────────────────────


def _stub_brain_state(monkeypatch, *, summary: Optional[Dict[str, Any]] = None):
    summary = summary or {
        "selected_product": "عسل سدر",
        "price": 360,
        "currency": "SAR",
        "awaiting_payment_receipt": True,
        "payment_receipt_received": False,
    }
    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda *_a, **_k: (None, {}),
    )
    monkeypatch.setattr(
        "core.order_flow._focus_summary",
        lambda _bs: summary,
    )


def _stub_tenant_accounts(monkeypatch, *, ibans=(), beneficiaries=()):
    from core.tenant_payment_accounts import TenantPaymentAccounts
    accts = TenantPaymentAccounts(
        ibans=tuple(ibans),
        beneficiaries=tuple(beneficiaries),
    )
    monkeypatch.setattr(
        "core.tenant_payment_accounts.load_tenant_payment_accounts",
        lambda *_a, **_k: accts,
    )
    # Patch the bound name inside order_flow's import path too.
    import core.order_flow as _of
    if hasattr(_of, "load_tenant_payment_accounts"):
        monkeypatch.setattr(
            _of, "load_tenant_payment_accounts",
            lambda *_a, **_k: accts,
        )


# ── 1. No tenant accounts → legacy behaviour ────────────────────────


def test_no_tenant_accounts_falls_back_to_legacy(monkeypatch):
    """A tenant who has not configured ``bank_transfer`` /
    ``payment_method`` KB sections keeps the existing behaviour:
    a confirmed receipt flips ``payment_receipt_received=True``."""
    from core import order_flow as of

    _stub_brain_state(monkeypatch)
    _stub_tenant_accounts(monkeypatch, ibans=(), beneficiaries=())

    result = of.maybe_handle_receipt_inbound(
        db=object(),
        tenant_id=33,
        phone="+966500000099",
        inbound_normalized_type="document",
        inbound_metadata={
            "pdf_kind": "payment_receipt",
            "payment_evidence_status": "confirmed",
            "pdf_text_preview": "تم التحويل بنجاح\nالمبلغ: 360 ريال",
            "filename": "Transaction-Receipt.pdf",
        },
    )
    assert result is not None
    assert result["state_patch"]["payment_receipt_received"] is True
    assert result["state_patch"]["order_status"] == "under_review"


# ── 2. Matching IBAN → flip allowed ────────────────────────────────


def test_matching_iban_allows_flip(monkeypatch):
    from core import order_flow as of

    _stub_brain_state(monkeypatch)
    _stub_tenant_accounts(monkeypatch, ibans=(_TENANT_IBAN,))

    result = of.maybe_handle_receipt_inbound(
        db=object(),
        tenant_id=33,
        phone="+966500000099",
        inbound_normalized_type="document",
        inbound_metadata={
            "pdf_kind": "payment_receipt",
            "payment_evidence_status": "confirmed",
            "pdf_text_preview": (
                "تم التحويل بنجاح\n"
                f"IBAN: {_TENANT_IBAN}\nالمبلغ: 360 ريال"
            ),
            "filename": "Transaction-Receipt.pdf",
        },
    )
    assert result is not None
    assert result["state_patch"]["payment_receipt_received"] is True
    md = result["state_patch"]["payment_receipt_metadata"]
    assert md.get("tenant_account_match", {}).get("status") == "evidence_verified"


# ── 3. Mismatched IBAN → flip blocked ──────────────────────────────


def test_mismatched_iban_blocks_flip(monkeypatch):
    """The headline regression of #48 in the media branch: a real
    confirmed receipt that was sent to the WRONG account must not
    flip the order to ``payment_receipt_received=True``."""
    from core import order_flow as of

    _stub_brain_state(monkeypatch)
    _stub_tenant_accounts(monkeypatch, ibans=(_TENANT_IBAN,))

    result = of.maybe_handle_receipt_inbound(
        db=object(),
        tenant_id=33,
        phone="+966500000099",
        inbound_normalized_type="document",
        inbound_metadata={
            "pdf_kind": "payment_receipt",
            "payment_evidence_status": "confirmed",
            "pdf_text_preview": (
                "تم التحويل بنجاح\n"
                "IBAN: SA9999999999999999999999\nالمبلغ: 360 ريال"
            ),
            "filename": "Transaction-Receipt.pdf",
        },
    )
    assert result is None, (
        "receipt with mismatched IBAN must NOT short-circuit the "
        "brain or flip payment state"
    )


# ── 4. Receipt without matchable tokens → flip blocked ─────────────


def test_receipt_without_iban_tokens_blocks_flip_when_accounts_configured(
    monkeypatch,
):
    """Tenant has accounts on file. The receipt OCR carried no
    IBAN / beneficiary token (perhaps a low-quality screenshot).
    We can't verify a match → caller must NOT flip state."""
    from core import order_flow as of

    _stub_brain_state(monkeypatch)
    _stub_tenant_accounts(monkeypatch, ibans=(_TENANT_IBAN,))

    result = of.maybe_handle_receipt_inbound(
        db=object(),
        tenant_id=33,
        phone="+966500000099",
        inbound_normalized_type="document",
        inbound_metadata={
            "pdf_kind": "payment_receipt",
            "payment_evidence_status": "confirmed",
            "pdf_text_preview": "تم التحويل بنجاح\n",
            "filename": "Transaction-Receipt.pdf",
        },
    )
    assert result is None


# ── 5. Active-order promotion branch is gated too ──────────────────


def test_active_order_promotion_blocked_on_mismatch(monkeypatch):
    """``maybe_handle_payment_evidence_inbound`` has an aggressive
    auto-confirm path: when the customer is in ``awaiting_receipt``
    and ANY bank-related document arrives, it promotes to
    confirmed. Under #48 this branch must also verify against
    tenant accounts before flipping."""
    from core import order_flow as of

    _stub_brain_state(monkeypatch)
    _stub_tenant_accounts(monkeypatch, ibans=(_TENANT_IBAN,))

    result = of.maybe_handle_payment_evidence_inbound(
        db=object(),
        tenant_id=33,
        phone="+966500000099",
        inbound_normalized_type="document",
        inbound_metadata={
            "pdf_kind": "payment_pre_review",
            "payment_evidence_status": "needs_confirmation",
            "pdf_text_preview": "إلى SA9999999999999999999999",
            "filename": "screenshot.pdf",
        },
    )
    assert result is None, (
        "active-order promotion must NOT flip state when the "
        "receipt's IBAN doesn't match the merchant's accounts"
    )


def test_active_order_promotion_allowed_on_match(monkeypatch):
    from core import order_flow as of

    _stub_brain_state(monkeypatch)
    _stub_tenant_accounts(monkeypatch, ibans=(_TENANT_IBAN,))

    result = of.maybe_handle_payment_evidence_inbound(
        db=object(),
        tenant_id=33,
        phone="+966500000099",
        inbound_normalized_type="document",
        inbound_metadata={
            "pdf_kind": "payment_pre_review",
            "payment_evidence_status": "needs_confirmation",
            "pdf_text_preview": f"إلى الحساب {_TENANT_IBAN}",
            "filename": "screenshot.pdf",
        },
    )
    assert result is not None
    assert result["state_patch"]["payment_receipt_received"] is True
    assert (
        result["state_patch"]["payment_receipt_metadata"]
              .get("tenant_account_match", {}).get("status")
        == "evidence_verified"
    )
