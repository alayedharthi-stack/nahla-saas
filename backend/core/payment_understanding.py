"""
core/payment_understanding.py
─────────────────────────────
Tenant 33 #48 (May 2026) — payment understanding correction.

The Bug
───────
Customer mentioned a transfer or amount in plain text inside an
ongoing conversation, without ever sending a receipt to the store.
The bot assumed:

    * الدفع تم
    * والإيصال وصل
    * والطلب أصبح جاهزًا للتجهيز والشحن

Why the decision was wrong
──────────────────────────
Mere mention of one of:

    * مبلغ
    * تحويل
    * حولت
    * فلوس

does NOT prove that any payment evidence reached the store, and it
does NOT prove that what was transferred matches the merchant's
official account. The previous behaviour conflated "claimed" with
"verified" and silently flipped state into the awaiting-receipt /
under-review path without any actual proof.

The Philosophy (per merchant directive)
──────────────────────────────────────

    "أصلحوا الفهم والقرار، وليس الكلمات."

We do NOT:
    * write hardcoded ACK lines for the AI to repeat,
    * force any specific wording on the brain,
    * impose tenant-specific copy.

We DO:
    * separate "claimed" from "verified" in state,
    * make payment confirmation tenant-aware (compare evidence
      against the merchant's registered accounts),
    * give the brain a clear *understanding signal* it can use to
      compose its own natural reply,
    * refuse to flip ``payment_receipt_received=True`` /
      ``order_status='under_review'`` until the evidence is
      actually verified against tenant accounts.

What this module owns
─────────────────────
A single pure function, ``compute_payment_understanding``, that
takes:

    * a tenant's registered payment accounts (load via
      ``core.tenant_payment_accounts.load_tenant_payment_accounts``),
    * the current evidence blob (None for text-only claims, OCR /
      PDF text + caption for media inbounds),
    * an "is this a text-only claim?" flag,

and returns a typed verdict the caller can:

    1. attach to the inbound's metadata so it flows into the brain
       prompt as understanding context (NOT as forced wording),
    2. consult before flipping any payment state,
    3. log so on-call can grep "payment_understanding_status=" and
       answer "why did the bot ACK as paid?" in seconds.

The function never raises and is safe to call from any path.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

from core.tenant_payment_accounts import (
    TenantPaymentAccounts,
    receipt_matches_tenant_accounts,
)

logger = logging.getLogger("nahla.payment_understanding")


# ── Status taxonomy ─────────────────────────────────────────────────
# Stable string constants — these are also written to MessageEvent
# metadata and consumed by the brain prompt overlay, so renaming any
# of them breaks downstream readers.

# Customer hasn't mentioned anything payment-related; the inbound is
# unrelated to money / transfer.
PAYMENT_UNDERSTANDING_NO_SIGNAL = "no_signal"

# Customer SAID they paid / transferred ("حولت", "تم التحويل") but
# attached nothing. NOT enough to confirm. Brain should reply
# naturally — we do NOT tell it what to write.
PAYMENT_UNDERSTANDING_TEXT_CLAIM_UNVERIFIED = "text_claim_unverified"

# Customer attached an image / PDF that contains payment-context
# tokens (bank brand, IBAN-prefix, amount, beneficiary) but the
# tenant has no registered accounts to compare against. Caller
# falls back to legacy ``payment_evidence`` verdict.
PAYMENT_UNDERSTANDING_EVIDENCE_NO_TENANT_ACCOUNTS = (
    "evidence_received_no_tenant_accounts"
)

# Customer attached payment evidence and the OCR has IBAN /
# beneficiary tokens, but NONE of them match the merchant's
# registered accounts. State MUST NOT flip to paid.
PAYMENT_UNDERSTANDING_EVIDENCE_ACCOUNT_MISMATCH = (
    "evidence_account_mismatch"
)

# Customer attached payment evidence; the OCR matched at least one
# of the merchant's registered IBANs / beneficiaries. Caller may
# proceed to flip ``payment_receipt_received=True``.
PAYMENT_UNDERSTANDING_EVIDENCE_VERIFIED = "evidence_verified"

# Customer attached evidence that classifies as payment-context,
# but the OCR has neither IBANs nor beneficiary tokens (e.g. a
# blurry amount-only screenshot). Tenant has accounts on file.
# We can't claim a match, but we can't claim a mismatch either —
# treat as "unverified" so state stays neutral.
PAYMENT_UNDERSTANDING_EVIDENCE_UNVERIFIED = "evidence_unverified"


_ALL_STATUSES = frozenset({
    PAYMENT_UNDERSTANDING_NO_SIGNAL,
    PAYMENT_UNDERSTANDING_TEXT_CLAIM_UNVERIFIED,
    PAYMENT_UNDERSTANDING_EVIDENCE_NO_TENANT_ACCOUNTS,
    PAYMENT_UNDERSTANDING_EVIDENCE_ACCOUNT_MISMATCH,
    PAYMENT_UNDERSTANDING_EVIDENCE_VERIFIED,
    PAYMENT_UNDERSTANDING_EVIDENCE_UNVERIFIED,
})


# ── Decision rules ──────────────────────────────────────────────────
# These are the two binary state-impacting questions the caller
# needs answers to. Storing them on the verdict (rather than letting
# every caller re-derive them) keeps the policy in one place.

_STATUS_ALLOWS_RECEIPT_RECEIVED_FLIP = frozenset({
    PAYMENT_UNDERSTANDING_EVIDENCE_VERIFIED,
    # Tenants without registered accounts keep legacy behaviour.
    PAYMENT_UNDERSTANDING_EVIDENCE_NO_TENANT_ACCOUNTS,
})

_STATUS_BLOCKS_ORDER_PAID_FLOW = frozenset({
    PAYMENT_UNDERSTANDING_TEXT_CLAIM_UNVERIFIED,
    PAYMENT_UNDERSTANDING_EVIDENCE_ACCOUNT_MISMATCH,
    PAYMENT_UNDERSTANDING_EVIDENCE_UNVERIFIED,
})


# ── Brain advisories ────────────────────────────────────────────────
# These strings are NOT outbound copy. They are *understanding hints*
# the brain prompt overlay can paste into the system prompt so the
# LLM grasps the situation and composes an organic reply on its own.
#
# They deliberately avoid imperative wording ("say X", "reply with
# Y") and stay descriptive / factual — exactly the merchant directive
# "أصلحوا الفهم والقرار، وليس الكلمات".

_ADVISORY_BY_STATUS: Dict[str, str] = {
    PAYMENT_UNDERSTANDING_NO_SIGNAL: "",
    PAYMENT_UNDERSTANDING_TEXT_CLAIM_UNVERIFIED: (
        "Payment claim status: the customer mentioned a transfer or "
        "an amount in text only. No receipt image, PDF, or "
        "transaction reference has been received yet. Treat the "
        "payment as UNVERIFIED. Do not assume the order is paid, "
        "do not promise shipping, and do not state that the "
        "receipt has been received. Reply naturally in your own "
        "wording."
    ),
    PAYMENT_UNDERSTANDING_EVIDENCE_NO_TENANT_ACCOUNTS: (
        "Payment evidence received, but this merchant has no "
        "registered payment accounts on file to compare against. "
        "Use the existing payment-evidence verdict to decide how "
        "to reply."
    ),
    PAYMENT_UNDERSTANDING_EVIDENCE_ACCOUNT_MISMATCH: (
        "Payment evidence received, BUT the IBAN / beneficiary in "
        "the receipt does NOT match any of the merchant's "
        "registered official accounts. Do not consider the order "
        "paid. Do not enter the order_paid / shipping flow. Reply "
        "naturally in your own wording."
    ),
    PAYMENT_UNDERSTANDING_EVIDENCE_UNVERIFIED: (
        "Payment evidence received, but it carries no IBAN or "
        "beneficiary tokens that can be matched against the "
        "merchant's registered accounts. Do not consider the "
        "order paid. Reply naturally in your own wording."
    ),
    PAYMENT_UNDERSTANDING_EVIDENCE_VERIFIED: (
        "Payment evidence verified: the receipt matches one of the "
        "merchant's registered accounts. The order is allowed to "
        "proceed to under_review. Reply naturally in your own "
        "wording."
    ),
}


@dataclass(frozen=True)
class PaymentUnderstanding:
    """Verdict returned by ``compute_payment_understanding``.

    Fields
    ------
    status:
        One of the ``PAYMENT_UNDERSTANDING_*`` constants. Stable.
    can_flip_receipt_received:
        True iff the caller is allowed to set
        ``payment_receipt_received=True`` /
        ``order_status='under_review'`` based on this verdict.
    blocks_order_paid_flow:
        True iff the caller MUST NOT enter the order-paid /
        shipping-preparation pathway based on this verdict alone.
    matched_iban:
        Canonical IBAN string if a tenant-account match was found,
        otherwise empty string.
    matched_beneficiary:
        Normalised beneficiary string if matched, otherwise empty.
    receipt_ibans:
        IBANs extracted from the receipt blob (canonical form).
    receipt_beneficiaries:
        Beneficiary candidates extracted from the receipt blob.
    advisory_for_brain:
        Plain-English understanding hint the brain prompt overlay
        can include in the system prompt. NOT outbound copy.
    reason:
        Short stable token explaining why this verdict was reached
        (used in structured logs).
    """

    status: str
    can_flip_receipt_received: bool
    blocks_order_paid_flow: bool
    matched_iban: str = ""
    matched_beneficiary: str = ""
    receipt_ibans: tuple = field(default_factory=tuple)
    receipt_beneficiaries: tuple = field(default_factory=tuple)
    advisory_for_brain: str = ""
    reason: str = ""

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "payment_understanding_status":            self.status,
            "payment_understanding_reason":            self.reason,
            "payment_understanding_can_flip_receipt":  self.can_flip_receipt_received,
            "payment_understanding_blocks_order_paid": self.blocks_order_paid_flow,
            "payment_understanding_matched_iban":      bool(self.matched_iban),
            "payment_understanding_matched_benef":     bool(self.matched_beneficiary),
            "payment_understanding_receipt_iban_n":    len(self.receipt_ibans),
            "payment_understanding_receipt_benef_n":   len(self.receipt_beneficiaries),
        }

    def to_state_patch(self) -> Dict[str, Any]:
        """Return a minimal, additive state patch the caller can
        merge into the brain state. Deliberately does NOT touch
        ``payment_receipt_received`` / ``awaiting_payment_receipt``
        / ``order_status`` — those flips are decided by the
        receipt / claim handlers based on
        ``can_flip_receipt_received``."""
        return {
            "payment_understanding_status": self.status,
            "payment_understanding_reason": self.reason,
        }


# ── Public function ────────────────────────────────────────────────


def compute_payment_understanding(
    *,
    tenant_accounts: Optional[TenantPaymentAccounts],
    evidence_text: Optional[str] = None,
    has_text_only_claim: bool = False,
) -> PaymentUnderstanding:
    """Decide what level of confidence we have in a customer's
    payment for this turn.

    Parameters
    ----------
    tenant_accounts:
        Result of ``load_tenant_payment_accounts``. May be ``None``
        if the caller never loaded one — treated as
        ``TenantPaymentAccounts()`` (no accounts on file).
    evidence_text:
        Concatenated OCR / PDF text + filename + caption from any
        media the customer attached this turn. ``None`` or empty
        means no media evidence.
    has_text_only_claim:
        True when the customer's text inbound matches a
        payment-claim phrase (use
        ``core.payment_intent.detect_payment_confirmation_text``).
        Independent of ``evidence_text`` — both can be true if the
        customer typed "حولت" along with attaching an image.

    Returns
    -------
    PaymentUnderstanding
        Always a populated verdict, even on edge cases.

    Never raises.
    """
    accounts = tenant_accounts or TenantPaymentAccounts()
    has_evidence = bool(evidence_text and str(evidence_text).strip())

    # No evidence + no claim → no signal. Caller does nothing.
    if not has_evidence and not has_text_only_claim:
        return PaymentUnderstanding(
            status=PAYMENT_UNDERSTANDING_NO_SIGNAL,
            can_flip_receipt_received=False,
            blocks_order_paid_flow=False,
            advisory_for_brain="",
            reason="no_payment_signal",
        )

    # Text claim only (no media evidence). Always unverified — the
    # customer SAYING "حولت" cannot, by itself, prove anything to
    # the merchant. State must NOT flip; brain must compose its
    # own reply naturally.
    if not has_evidence and has_text_only_claim:
        return PaymentUnderstanding(
            status=PAYMENT_UNDERSTANDING_TEXT_CLAIM_UNVERIFIED,
            can_flip_receipt_received=False,
            blocks_order_paid_flow=True,
            advisory_for_brain=_ADVISORY_BY_STATUS[
                PAYMENT_UNDERSTANDING_TEXT_CLAIM_UNVERIFIED
            ],
            reason="text_claim_without_evidence",
        )

    # Media evidence present. Run the tenant-account match check.
    match = receipt_matches_tenant_accounts(
        accounts=accounts,
        receipt_text=evidence_text,
    )
    match_status = str(match.get("status") or "")

    if match_status == "no_tenant_accounts":
        # Nothing on file → fall back to legacy verdict. Caller
        # keeps existing behaviour (will normally flip receipt
        # if classify_payment_evidence said ``confirmed``).
        return PaymentUnderstanding(
            status=PAYMENT_UNDERSTANDING_EVIDENCE_NO_TENANT_ACCOUNTS,
            can_flip_receipt_received=True,
            blocks_order_paid_flow=False,
            advisory_for_brain=_ADVISORY_BY_STATUS[
                PAYMENT_UNDERSTANDING_EVIDENCE_NO_TENANT_ACCOUNTS
            ],
            reason="evidence_present_no_tenant_accounts",
            receipt_ibans=tuple(match.get("receipt_ibans") or ()),
            receipt_beneficiaries=tuple(match.get("receipt_beneficiaries") or ()),
        )

    if match_status == "match":
        return PaymentUnderstanding(
            status=PAYMENT_UNDERSTANDING_EVIDENCE_VERIFIED,
            can_flip_receipt_received=True,
            blocks_order_paid_flow=False,
            matched_iban=str(match.get("matched_iban") or ""),
            matched_beneficiary=str(match.get("matched_beneficiary") or ""),
            advisory_for_brain=_ADVISORY_BY_STATUS[
                PAYMENT_UNDERSTANDING_EVIDENCE_VERIFIED
            ],
            reason="receipt_iban_or_beneficiary_match",
            receipt_ibans=tuple(match.get("receipt_ibans") or ()),
            receipt_beneficiaries=tuple(match.get("receipt_beneficiaries") or ()),
        )

    if match_status == "mismatch":
        return PaymentUnderstanding(
            status=PAYMENT_UNDERSTANDING_EVIDENCE_ACCOUNT_MISMATCH,
            can_flip_receipt_received=False,
            blocks_order_paid_flow=True,
            advisory_for_brain=_ADVISORY_BY_STATUS[
                PAYMENT_UNDERSTANDING_EVIDENCE_ACCOUNT_MISMATCH
            ],
            reason="receipt_iban_or_beneficiary_mismatch",
            receipt_ibans=tuple(match.get("receipt_ibans") or ()),
            receipt_beneficiaries=tuple(match.get("receipt_beneficiaries") or ()),
        )

    # match_status == "no_signal_in_receipt" — evidence-shaped but
    # no IBAN / beneficiary tokens to compare. Refuse to flip.
    return PaymentUnderstanding(
        status=PAYMENT_UNDERSTANDING_EVIDENCE_UNVERIFIED,
        can_flip_receipt_received=False,
        blocks_order_paid_flow=True,
        advisory_for_brain=_ADVISORY_BY_STATUS[
            PAYMENT_UNDERSTANDING_EVIDENCE_UNVERIFIED
        ],
        reason="evidence_without_matchable_tokens",
        receipt_ibans=tuple(match.get("receipt_ibans") or ()),
        receipt_beneficiaries=tuple(match.get("receipt_beneficiaries") or ()),
    )


def log_payment_understanding(
    *,
    tenant_id: Any,
    phone: Optional[str],
    source: str,
    verdict: PaymentUnderstanding,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit a single structured log line per understanding call.

    Canonical grep target: ``[PAYMENT_UNDERSTANDING]
    payment_understanding_status=...`` so on-call can answer "why
    did the bot treat this as paid / unpaid?" in seconds.
    """
    try:
        masked_phone = ""
        if phone:
            try:
                masked_phone = "*" + str(phone)[-4:]
            except Exception:
                masked_phone = ""
        logger.info(
            "[PAYMENT_UNDERSTANDING] tenant=%s phone=%s source=%s "
            "%s extra=%s",
            tenant_id, masked_phone, source,
            " ".join(f"{k}={v}" for k, v in verdict.to_log_dict().items()),
            extra or {},
        )
    except Exception:
        pass


__all__ = [
    "PAYMENT_UNDERSTANDING_NO_SIGNAL",
    "PAYMENT_UNDERSTANDING_TEXT_CLAIM_UNVERIFIED",
    "PAYMENT_UNDERSTANDING_EVIDENCE_NO_TENANT_ACCOUNTS",
    "PAYMENT_UNDERSTANDING_EVIDENCE_ACCOUNT_MISMATCH",
    "PAYMENT_UNDERSTANDING_EVIDENCE_VERIFIED",
    "PAYMENT_UNDERSTANDING_EVIDENCE_UNVERIFIED",
    "PaymentUnderstanding",
    "compute_payment_understanding",
    "log_payment_understanding",
]
