"""
tests/test_payment_understanding.py
───────────────────────────────────
Tenant 33 #48 (May 2026) — payment understanding correction.

Pinning the decision layer that lives in
``backend/core/payment_understanding.py``. Every assertion here
covers a concrete production scenario the merchant called out:

    "ذكر مبلغ أو كلمة 'حولت' لا يعني أن الدفع تأكد. ولا يجوز اعتبار
     الطلب مدفوعًا إلا عند وجود payment evidence حقيقي ومطابق
     لحساب التاجر الرسمي."

Coverage map
────────────
1. No signal → ``no_signal``, no state mutation allowed.
2. Text-only claim ("حولت") → ``text_claim_unverified``,
   ``can_flip_receipt_received=False``,
   ``blocks_order_paid_flow=True``.
3. Evidence + tenant has no accounts → fall back to legacy
   (``can_flip_receipt_received=True``).
4. Evidence + matching IBAN → ``evidence_verified``, flip allowed.
5. Evidence + matching beneficiary → ``evidence_verified``, flip allowed.
6. Evidence + mismatched IBAN → ``evidence_account_mismatch``, flip blocked.
7. Evidence + no IBAN/beneficiary tokens → ``evidence_unverified``, flip blocked.
8. Both text claim AND verified evidence → ``evidence_verified`` wins
   (claim does not regress the verdict).
9. Advisory text exists for every non-trivial status (NO outbound
   wording — these are understanding hints).
10. State patch is additive only — never touches
    ``payment_receipt_received`` / ``order_status``.
"""
from __future__ import annotations

import pytest

from core.payment_understanding import (
    PAYMENT_UNDERSTANDING_EVIDENCE_ACCOUNT_MISMATCH,
    PAYMENT_UNDERSTANDING_EVIDENCE_NO_TENANT_ACCOUNTS,
    PAYMENT_UNDERSTANDING_EVIDENCE_UNVERIFIED,
    PAYMENT_UNDERSTANDING_EVIDENCE_VERIFIED,
    PAYMENT_UNDERSTANDING_NO_SIGNAL,
    PAYMENT_UNDERSTANDING_TEXT_CLAIM_UNVERIFIED,
    PaymentUnderstanding,
    compute_payment_understanding,
)
from core.tenant_payment_accounts import TenantPaymentAccounts


_TENANT_IBAN = "SA0380000000608010167519"
_TENANT_BENEF = "نحله الفلاح"


def _accounts(*, ibans=(_TENANT_IBAN,), beneficiaries=()):
    return TenantPaymentAccounts(
        ibans=tuple(ibans),
        beneficiaries=tuple(beneficiaries),
    )


# ── 1. No signal ───────────────────────────────────────────────────


def test_no_signal_status():
    v = compute_payment_understanding(
        tenant_accounts=_accounts(),
        evidence_text=None,
        has_text_only_claim=False,
    )
    assert v.status == PAYMENT_UNDERSTANDING_NO_SIGNAL
    assert v.can_flip_receipt_received is False
    assert v.blocks_order_paid_flow is False
    assert v.advisory_for_brain == ""


# ── 2. Text-only claim ─────────────────────────────────────────────


def test_text_only_claim_does_not_allow_receipt_flip():
    """Customer typed 'حولت' / 'تم التحويل' with nothing attached.
    THIS IS THE CORE BUG OF #48: state must NOT flip on a verbal
    claim alone."""
    v = compute_payment_understanding(
        tenant_accounts=_accounts(),
        evidence_text=None,
        has_text_only_claim=True,
    )
    assert v.status == PAYMENT_UNDERSTANDING_TEXT_CLAIM_UNVERIFIED
    assert v.can_flip_receipt_received is False
    assert v.blocks_order_paid_flow is True
    assert v.advisory_for_brain  # advisory exists
    # Advisory does NOT contain any imperative outbound wording —
    # it should only describe the situation.
    assert "وصل" not in v.advisory_for_brain  # no Arabic outbound copy
    assert "أرسل لي الإيصال" not in v.advisory_for_brain


def test_text_only_claim_without_tenant_accounts_still_blocks_flip():
    """Even tenants without configured accounts must NOT flip
    receipt state from a text-only claim."""
    v = compute_payment_understanding(
        tenant_accounts=TenantPaymentAccounts(),
        evidence_text=None,
        has_text_only_claim=True,
    )
    assert v.status == PAYMENT_UNDERSTANDING_TEXT_CLAIM_UNVERIFIED
    assert v.can_flip_receipt_received is False


# ── 3. Evidence + no tenant accounts → legacy fallback ─────────────


def test_evidence_no_tenant_accounts_falls_back_to_legacy():
    v = compute_payment_understanding(
        tenant_accounts=TenantPaymentAccounts(),
        evidence_text="تم التحويل إلى SA0380000000608010167519",
        has_text_only_claim=False,
    )
    assert v.status == PAYMENT_UNDERSTANDING_EVIDENCE_NO_TENANT_ACCOUNTS
    # Legacy behaviour preserved for tenants without configured KB.
    assert v.can_flip_receipt_received is True
    assert v.blocks_order_paid_flow is False


# ── 4. Evidence + matching IBAN → verified ─────────────────────────


def test_evidence_iban_match_verified():
    v = compute_payment_understanding(
        tenant_accounts=_accounts(ibans=(_TENANT_IBAN,)),
        evidence_text=(
            "تم التحويل بنجاح إلى الحساب\n"
            "IBAN: SA0380000000608010167519\nالمبلغ: 250 ريال"
        ),
        has_text_only_claim=False,
    )
    assert v.status == PAYMENT_UNDERSTANDING_EVIDENCE_VERIFIED
    assert v.can_flip_receipt_received is True
    assert v.blocks_order_paid_flow is False
    assert v.matched_iban == _TENANT_IBAN


def test_evidence_iban_match_with_different_spacing():
    v = compute_payment_understanding(
        tenant_accounts=_accounts(ibans=(_TENANT_IBAN,)),
        evidence_text="IBAN: SA03 8000 0000 6080 1016 7519",
        has_text_only_claim=False,
    )
    assert v.status == PAYMENT_UNDERSTANDING_EVIDENCE_VERIFIED


# ── 5. Evidence + matching beneficiary ─────────────────────────────


def test_evidence_beneficiary_match_verified():
    v = compute_payment_understanding(
        tenant_accounts=_accounts(
            ibans=(),
            beneficiaries=(_TENANT_BENEF,),
        ),
        evidence_text="اسم المستفيد: نحله الفلاح للتجاره",
        has_text_only_claim=False,
    )
    assert v.status == PAYMENT_UNDERSTANDING_EVIDENCE_VERIFIED
    assert "نحله" in v.matched_beneficiary


# ── 6. Evidence + mismatched IBAN → blocked ────────────────────────


def test_evidence_iban_mismatch_blocks_flip():
    """The headline regression: customer attached a real receipt
    but to the wrong account (or someone else's account). State
    must NOT flip to 'paid'."""
    v = compute_payment_understanding(
        tenant_accounts=_accounts(ibans=(_TENANT_IBAN,)),
        evidence_text=(
            "تم التحويل إلى SA9999999999999999999999\nالمبلغ: 250"
        ),
        has_text_only_claim=False,
    )
    assert v.status == PAYMENT_UNDERSTANDING_EVIDENCE_ACCOUNT_MISMATCH
    assert v.can_flip_receipt_received is False
    assert v.blocks_order_paid_flow is True


# ── 7. Evidence with no matchable tokens ───────────────────────────


def test_evidence_without_matchable_tokens_blocks_flip():
    """OCR yielded amount/bank words but no IBAN or beneficiary
    label. Tenant has accounts on file → caller must NOT flip
    receipt state since we cannot prove the receipt belongs to
    THIS merchant."""
    v = compute_payment_understanding(
        tenant_accounts=_accounts(ibans=(_TENANT_IBAN,)),
        evidence_text="screenshot of an amount field 250 SAR",
        has_text_only_claim=False,
    )
    assert v.status == PAYMENT_UNDERSTANDING_EVIDENCE_UNVERIFIED
    assert v.can_flip_receipt_received is False


# ── 8. Verified evidence overrides text claim ──────────────────────


def test_verified_evidence_overrides_text_claim():
    """When the customer says 'حولت' AND attaches a matching receipt
    on the same turn, the verified branch wins — text claim does
    not regress the verdict."""
    v = compute_payment_understanding(
        tenant_accounts=_accounts(ibans=(_TENANT_IBAN,)),
        evidence_text="تم التحويل إلى SA0380000000608010167519",
        has_text_only_claim=True,
    )
    assert v.status == PAYMENT_UNDERSTANDING_EVIDENCE_VERIFIED
    assert v.can_flip_receipt_received is True


# ── 9. Advisory shape ───────────────────────────────────────────────


def test_advisory_for_brain_describes_not_dictates():
    """The advisory string must be a *description* the brain
    consumes for understanding — never imperative outbound copy.
    This is the core merchant directive: 'فهم وقرار، لا كلمات'."""
    v_text = compute_payment_understanding(
        tenant_accounts=_accounts(),
        has_text_only_claim=True,
    )
    v_mismatch = compute_payment_understanding(
        tenant_accounts=_accounts(ibans=(_TENANT_IBAN,)),
        evidence_text="إلى SA9999999999999999999999",
    )
    for v in (v_text, v_mismatch):
        a = v.advisory_for_brain.lower()
        # No Arabic copy that the brain might parrot back.
        assert "وصل" not in a
        assert "أرسل لي" not in a
        assert "أبشر" not in a
        # Must contain factual descriptors (English understanding).
        assert any(token in a for token in (
            "unverified", "do not", "match",
        ))


# ── 10. State patch shape ──────────────────────────────────────────


def test_state_patch_does_not_flip_payment_state():
    """The understanding verdict's state patch must never include
    keys that imply the order is paid / under-review. Those flips
    are owned by the receipt handler, gated by
    ``can_flip_receipt_received``."""
    v = compute_payment_understanding(
        tenant_accounts=_accounts(),
        has_text_only_claim=True,
    )
    patch = v.to_state_patch()
    forbidden = {
        "payment_receipt_received",
        "awaiting_payment_receipt",
        "order_status",
        "payment_receipt_at",
    }
    assert forbidden.isdisjoint(patch.keys())


def test_log_dict_has_stable_keys():
    v = compute_payment_understanding(
        tenant_accounts=_accounts(),
        has_text_only_claim=True,
    )
    log = v.to_log_dict()
    # On-call greps these tokens; pin them.
    for key in (
        "payment_understanding_status",
        "payment_understanding_reason",
        "payment_understanding_can_flip_receipt",
        "payment_understanding_blocks_order_paid",
    ):
        assert key in log
