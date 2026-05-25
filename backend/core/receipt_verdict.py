"""
core/receipt_verdict.py
───────────────────────
Wave 1 W1.2 — Payment / Receipt Integrity Stabilization, second
phase. **Telemetry only**.

What this module owns
─────────────────────
A single closed enum of payment-verification outcomes plus a pure
function that consolidates the existing decision signals into one
canonical verdict for telemetry. NO state writes. NO behavioural
change. NO new OCR / extraction. NO Order record creation. NO new
paid flow. NO brain wording.

Why a separate verdict layer
────────────────────────────
The diagnostic for Wave 1 surfaced a vocabulary fragmentation:

    * ``payment_evidence_status`` (legacy classifier in
      ``core.payment_evidence``) returns the strings ``confirmed``,
      ``pre_transfer_review``, ``needs_confirmation``,
      ``not_payment``, ``empty_text``.
    * ``PaymentUnderstanding.status`` (from W1.0 — May 2026 #48)
      returns ``no_signal``, ``text_claim_unverified``,
      ``evidence_no_tenant_accounts``, ``evidence_account_mismatch``,
      ``evidence_verified``, ``evidence_unverified``.

Operators have to mentally cross-walk both vocabularies whenever
they triage a "why did the bot ACK as paid?" report. W1.2 solves
the vocabulary drift WITHOUT changing semantics: it folds both
inputs into a closed seven-state enum the merchant directive
asked for, emits a structured ``[PAYMENT_VERIFICATION_DECISION]``
log line, and stays out of the way.

W1.2 invariants (locked by tests)
─────────────────────────────────
1. The ``ReceiptVerdict`` enum is **closed**. The exact set is
   pinned by an architectural test that fails the build on drift.
2. ``compute_receipt_verdict`` is a **pure function**. It MUST
   NOT raise, MUST NOT mutate any input, MUST NOT touch any
   state, DB, or filesystem.
3. The kill switch ``RECEIPT_VERDICT_TELEMETRY_ENABLED`` (default
   OFF) gates **logging only**. The pure function is always safe
   to call — the flag controls whether call sites bother to emit
   the structured log line.
4. The verdict NEVER drives behaviour in W1.2. Wave 1 W1.4 is
   the commit that promotes the verdict from telemetry to
   policy. Tests pin "wiring is observation-only" by asserting
   that callers' return values do not depend on the flag.

Status mapping (informational; the source of truth lives in
:func:`compute_receipt_verdict`)::

    PaymentUnderstanding.status                  ReceiptVerdict
    ─────────────────────────────────────────    ───────────────────────
    evidence_verified                            verified_match
    evidence_account_mismatch                    account_mismatch
    evidence_unverified                          unclear_receipt
    text_claim_unverified                        text_claim_unverified
    evidence_no_tenant_accounts (+confirmed)     probable_match
    evidence_no_tenant_accounts (+partial)       unclear_receipt
    evidence_no_tenant_accounts (+other)         probable_match
    no_signal                                    (fall through)

    payment_evidence_status (without PU)         ReceiptVerdict
    ─────────────────────────────────────────    ───────────────────────
    confirmed     + has_attached_media           probable_match
    pre_transfer_review / needs_confirmation     unclear_receipt
    empty_text   + payment_kind                  fake_or_corrupted
    not_payment / empty_text / unset             not_payment | text_claim_unverified | not_payment

    has_text_only_claim only                     text_claim_unverified
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, Optional, Tuple, Union

logger = logging.getLogger("nahla.receipt_verdict")


# ── 1. Closed verdict enum (architecturally pinned) ─────────────────


class ReceiptVerdict(str, Enum):
    """Closed enumeration of receipt-verification outcomes used by
    Wave 1 telemetry. The set is pinned by
    ``test_receipt_verdict_enum_is_closed`` — drift fails the build.

    Members
    -------
    VERIFIED_MATCH:
        Receipt evidence verified against the merchant's registered
        accounts. The only verdict that may, in W1.4, gate a
        ``payment_receipt_received=True`` flip. NEVER auto-flips
        anything in W1.2.
    PROBABLE_MATCH:
        Receipt evidence is present and the legacy classifier said
        ``confirmed``, but tenant-account verification cannot run
        (e.g. tenant has no registered accounts on file). Treat as
        a "high-confidence not-yet-verified" verdict — Wave 1 W1.4
        will require a stronger signal before flipping paid state.
    UNCLEAR_RECEIPT:
        Evidence-shaped media reached the bot, but key fields are
        missing / blurred / partial (e.g. ``pre_transfer_review``,
        ``needs_confirmation``, or ``evidence_unverified``). Cannot
        prove a match OR a mismatch — state must stay neutral.
    ACCOUNT_MISMATCH:
        Receipt evidence carries an IBAN / beneficiary token that
        explicitly does NOT match any of the merchant's registered
        accounts. The strongest "do not flip paid" signal.
    FAKE_OR_CORRUPTED:
        Inbound was attached AS payment-context (image_kind /
        pdf_kind = payment_*) but the OCR / vision text came back
        empty or unreadable. May indicate corrupted media, a
        screenshot of an unrelated UI, or an attempt to spoof a
        receipt with a non-bank image.
    TEXT_CLAIM_UNVERIFIED:
        Customer SAID they paid in plain text ("حولت" / "تم
        التحويل") with no media attached. The understanding flag
        from W1.1 is the brain-side counterpart.
    NOT_PAYMENT:
        No payment signal in this turn. Inbound is unrelated to
        money / transfer / receipt.
    """

    VERIFIED_MATCH         = "verified_match"
    PROBABLE_MATCH         = "probable_match"
    UNCLEAR_RECEIPT        = "unclear_receipt"
    ACCOUNT_MISMATCH       = "account_mismatch"
    FAKE_OR_CORRUPTED      = "fake_or_corrupted"
    TEXT_CLAIM_UNVERIFIED  = "text_claim_unverified"
    NOT_PAYMENT            = "not_payment"


# ── 2. Architectural pins ───────────────────────────────────────────
# Frozen sets exported for the architectural tests. Adding / renaming
# a verdict requires updating BOTH the enum and these pins, which
# means the build fails until the change is deliberate.
RECEIPT_VERDICTS_ALL: FrozenSet[ReceiptVerdict] = frozenset(ReceiptVerdict)

RECEIPT_VERDICTS_VALUES: FrozenSet[str] = frozenset({v.value for v in ReceiptVerdict})

# Verdicts that MAY (in W1.4 — NOT in W1.2) gate the paid flow.
# Pinned here so a future drift cannot silently widen the gate.
PAID_FLOW_ALLOWED_VERDICTS: FrozenSet[ReceiptVerdict] = frozenset({
    ReceiptVerdict.VERIFIED_MATCH,
})

# Verdicts that MUST always block the paid flow regardless of any
# other signal. ``NOT_PAYMENT`` is intentionally absent because
# inert traffic doesn't need a "block" — it just never enters the
# paid-flow gate to begin with.
PAID_FLOW_BLOCKED_VERDICTS: FrozenSet[ReceiptVerdict] = frozenset({
    ReceiptVerdict.UNCLEAR_RECEIPT,
    ReceiptVerdict.ACCOUNT_MISMATCH,
    ReceiptVerdict.FAKE_OR_CORRUPTED,
    ReceiptVerdict.TEXT_CLAIM_UNVERIFIED,
})


# ── 3. Inputs ───────────────────────────────────────────────────────
# Recognised legacy ``payment_evidence_status`` strings. We accept
# unknown values without raising — they collapse to NOT_PAYMENT.
_PE_STATUS_CONFIRMED          = "confirmed"
_PE_STATUS_PRE_TRANSFER       = "pre_transfer_review"
_PE_STATUS_NEEDS_CONFIRMATION = "needs_confirmation"
_PE_STATUS_NOT_PAYMENT        = "not_payment"
_PE_STATUS_EMPTY_TEXT         = "empty_text"

_PARTIAL_PE_STATUSES: FrozenSet[str] = frozenset({
    _PE_STATUS_PRE_TRANSFER,
    _PE_STATUS_NEEDS_CONFIRMATION,
})

# Recognised ``PaymentUnderstanding.status`` strings — copied here
# instead of imported so this module never circular-imports
# ``core.payment_understanding``. The shape is duck-typed: callers
# can pass either a real ``PaymentUnderstanding`` object or just
# the bare status string.
_PU_NO_SIGNAL                  = "no_signal"
_PU_TEXT_CLAIM_UNVERIFIED      = "text_claim_unverified"
_PU_EVIDENCE_NO_ACCOUNTS       = "evidence_received_no_tenant_accounts"
_PU_EVIDENCE_ACCOUNT_MISMATCH  = "evidence_account_mismatch"
_PU_EVIDENCE_VERIFIED          = "evidence_verified"
_PU_EVIDENCE_UNVERIFIED        = "evidence_unverified"

# Recognised payment-context kind tokens (``image_kind`` / ``pdf_kind``
# set by ``modules.ai.media.normalizer``). When the customer attached
# something normalized as payment-shaped, an empty OCR / vision blob
# becomes a corrupted-receipt signal rather than "no payment".
_PAYMENT_LIKE_KINDS: FrozenSet[str] = frozenset({
    "payment_receipt",
    "payment_pre_review",
    "payment_pending_evidence",
})


# ── 4. Result dataclass ─────────────────────────────────────────────


@dataclass(frozen=True)
class ReceiptVerdictResult:
    """Verdict + provenance returned by
    :func:`compute_receipt_verdict`. Frozen — never mutated by the
    caller. ``to_log_dict`` produces the exact key/value shape used
    by ``[PAYMENT_VERIFICATION_DECISION]``."""

    verdict: ReceiptVerdict
    reason: str = ""
    derived_from: str = ""           # "payment_understanding" | "payment_evidence_status" | "fallback"
    payment_understanding_status: str = ""
    payment_evidence_status: str = ""
    image_or_pdf_kind: str = ""
    has_attached_media: bool = False
    has_text_only_claim: bool = False
    matched_iban: str = ""
    matched_beneficiary: str = ""
    receipt_iban_count: int = 0
    receipt_beneficiary_count: int = 0

    @property
    def is_paid_flow_allowed(self) -> bool:
        """W1.2 telemetry-only convenience accessor. Real paid-flow
        gating arrives in W1.4; this property only restates the
        architectural pin and never imposes side effects."""
        return self.verdict in PAID_FLOW_ALLOWED_VERDICTS

    @property
    def is_paid_flow_blocked(self) -> bool:
        return self.verdict in PAID_FLOW_BLOCKED_VERDICTS

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "receipt_verdict":                  self.verdict.value,
            "receipt_verdict_reason":           self.reason,
            "receipt_verdict_derived_from":     self.derived_from,
            "payment_understanding_status":     self.payment_understanding_status,
            "payment_evidence_status":          self.payment_evidence_status,
            "image_or_pdf_kind":                self.image_or_pdf_kind,
            "has_attached_media":               self.has_attached_media,
            "has_text_only_claim":              self.has_text_only_claim,
            "matched_iban":                     bool(self.matched_iban),
            "matched_beneficiary":              bool(self.matched_beneficiary),
            "receipt_iban_count":               self.receipt_iban_count,
            "receipt_beneficiary_count":        self.receipt_beneficiary_count,
        }


# ── 5. Kill switch ──────────────────────────────────────────────────


def is_receipt_verdict_telemetry_enabled() -> bool:
    """Return ``True`` when ``RECEIPT_VERDICT_TELEMETRY_ENABLED`` is
    set to a truthy value. Default OFF — staged rollout per merchant
    directive. Independent from
    ``PAYMENT_CONTRADICTION_GUARD_ENABLED`` (W1.1) and any other
    Wave 1 flag."""
    raw = (
        os.environ.get("RECEIPT_VERDICT_TELEMETRY_ENABLED") or ""
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


# ── 6. Pure verdict computation ─────────────────────────────────────


def compute_receipt_verdict(
    *,
    payment_understanding: Any = None,
    payment_evidence_status: Optional[str] = None,
    image_kind: Optional[str] = None,
    pdf_kind: Optional[str] = None,
    has_attached_media: bool = False,
    has_text_only_claim: bool = False,
) -> ReceiptVerdictResult:
    """Consolidate the existing payment signals into a single closed
    verdict. Pure; never raises; never mutates inputs.

    Parameters
    ----------
    payment_understanding:
        Either a ``PaymentUnderstanding`` instance (duck-typed —
        we read ``.status`` / ``.matched_iban`` /
        ``.matched_beneficiary`` / ``.receipt_ibans`` /
        ``.receipt_beneficiaries`` if present) or a bare status
        string. ``None`` means the caller did not run the W1.0
        understanding layer.
    payment_evidence_status:
        Legacy classifier string from ``core.payment_evidence``.
        ``None`` / unknown collapses to "no signal".
    image_kind / pdf_kind:
        Normalizer-set kind tokens. We use these only to detect
        the ``fake_or_corrupted`` case — a payment-shaped
        attachment with empty OCR text.
    has_attached_media:
        ``True`` when this turn carries an inbound image / PDF /
        document. Independent of ``image_kind`` so callers can
        signal "media arrived but normalizer didn't classify it".
    has_text_only_claim:
        ``True`` when the inbound matches a payment-claim phrase
        per ``core.payment_intent.detect_payment_confirmation_text``.

    Returns
    -------
    ReceiptVerdictResult
        Always populated, even on garbage / partial inputs.
    """
    try:
        return _compute_unsafe(
            payment_understanding=payment_understanding,
            payment_evidence_status=payment_evidence_status,
            image_kind=image_kind,
            pdf_kind=pdf_kind,
            has_attached_media=bool(has_attached_media),
            has_text_only_claim=bool(has_text_only_claim),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[PAYMENT_VERIFICATION_DECISION] compute failed (returning "
            "NOT_PAYMENT): %s", exc,
        )
        return ReceiptVerdictResult(
            verdict=ReceiptVerdict.NOT_PAYMENT,
            reason="exception_in_compute",
            derived_from="fallback",
        )


def _coerce_pu(
    payment_understanding: Any,
) -> Tuple[str, str, str, Tuple[str, ...], Tuple[str, ...]]:
    """Extract ``(status, matched_iban, matched_beneficiary,
    receipt_ibans, receipt_beneficiaries)`` from a
    ``PaymentUnderstanding`` object OR a bare status string. Never
    raises."""
    if payment_understanding is None:
        return "", "", "", (), ()
    if isinstance(payment_understanding, str):
        return payment_understanding.strip(), "", "", (), ()
    status = str(getattr(payment_understanding, "status", "") or "").strip()
    matched_iban = str(getattr(payment_understanding, "matched_iban", "") or "").strip()
    matched_benef = str(getattr(payment_understanding, "matched_beneficiary", "") or "").strip()
    receipt_ibans = tuple(getattr(payment_understanding, "receipt_ibans", ()) or ())
    receipt_benefs = tuple(getattr(payment_understanding, "receipt_beneficiaries", ()) or ())
    return status, matched_iban, matched_benef, receipt_ibans, receipt_benefs


def _resolve_kind(
    image_kind: Optional[str], pdf_kind: Optional[str],
) -> str:
    return str(image_kind or pdf_kind or "").strip()


def _compute_unsafe(
    *,
    payment_understanding: Any,
    payment_evidence_status: Optional[str],
    image_kind: Optional[str],
    pdf_kind: Optional[str],
    has_attached_media: bool,
    has_text_only_claim: bool,
) -> ReceiptVerdictResult:
    """Real implementation. ``compute_receipt_verdict`` wraps this
    with a top-level except to guarantee the function never raises
    on garbage inputs."""

    pu_status, matched_iban, matched_benef, receipt_ibans, receipt_benefs = (
        _coerce_pu(payment_understanding)
    )
    pe_status = str(payment_evidence_status or "").strip()
    kind = _resolve_kind(image_kind, pdf_kind)

    # ── Layer 1: PaymentUnderstanding has the authoritative answer.
    if pu_status == _PU_EVIDENCE_VERIFIED:
        return _result(
            ReceiptVerdict.VERIFIED_MATCH,
            reason="evidence_iban_or_beneficiary_match",
            derived_from="payment_understanding",
            pu_status=pu_status, pe_status=pe_status, kind=kind,
            has_attached_media=has_attached_media,
            has_text_only_claim=has_text_only_claim,
            matched_iban=matched_iban, matched_benef=matched_benef,
            receipt_ibans=receipt_ibans, receipt_benefs=receipt_benefs,
        )

    if pu_status == _PU_EVIDENCE_ACCOUNT_MISMATCH:
        return _result(
            ReceiptVerdict.ACCOUNT_MISMATCH,
            reason="evidence_iban_or_beneficiary_mismatch",
            derived_from="payment_understanding",
            pu_status=pu_status, pe_status=pe_status, kind=kind,
            has_attached_media=has_attached_media,
            has_text_only_claim=has_text_only_claim,
            matched_iban=matched_iban, matched_benef=matched_benef,
            receipt_ibans=receipt_ibans, receipt_benefs=receipt_benefs,
        )

    if pu_status == _PU_EVIDENCE_UNVERIFIED:
        return _result(
            ReceiptVerdict.UNCLEAR_RECEIPT,
            reason="evidence_without_matchable_tokens",
            derived_from="payment_understanding",
            pu_status=pu_status, pe_status=pe_status, kind=kind,
            has_attached_media=has_attached_media,
            has_text_only_claim=has_text_only_claim,
            matched_iban=matched_iban, matched_benef=matched_benef,
            receipt_ibans=receipt_ibans, receipt_benefs=receipt_benefs,
        )

    if pu_status == _PU_TEXT_CLAIM_UNVERIFIED:
        return _result(
            ReceiptVerdict.TEXT_CLAIM_UNVERIFIED,
            reason="text_claim_without_evidence",
            derived_from="payment_understanding",
            pu_status=pu_status, pe_status=pe_status, kind=kind,
            has_attached_media=has_attached_media,
            has_text_only_claim=has_text_only_claim,
            matched_iban=matched_iban, matched_benef=matched_benef,
            receipt_ibans=receipt_ibans, receipt_benefs=receipt_benefs,
        )

    if pu_status == _PU_EVIDENCE_NO_ACCOUNTS:
        # Tenant has no registered payment accounts on file — we
        # cannot run the strict tenant-account verification, so the
        # best we can say is "probable" when the legacy classifier
        # also says ``confirmed``, otherwise unclear.
        if pe_status == _PE_STATUS_CONFIRMED:
            return _result(
                ReceiptVerdict.PROBABLE_MATCH,
                reason="legacy_confirmed_no_tenant_accounts",
                derived_from="payment_understanding",
                pu_status=pu_status, pe_status=pe_status, kind=kind,
                has_attached_media=has_attached_media,
                has_text_only_claim=has_text_only_claim,
                matched_iban=matched_iban, matched_benef=matched_benef,
                receipt_ibans=receipt_ibans, receipt_benefs=receipt_benefs,
            )
        if pe_status in _PARTIAL_PE_STATUSES:
            return _result(
                ReceiptVerdict.UNCLEAR_RECEIPT,
                reason=f"legacy_{pe_status}_no_tenant_accounts",
                derived_from="payment_understanding",
                pu_status=pu_status, pe_status=pe_status, kind=kind,
                has_attached_media=has_attached_media,
                has_text_only_claim=has_text_only_claim,
                matched_iban=matched_iban, matched_benef=matched_benef,
                receipt_ibans=receipt_ibans, receipt_benefs=receipt_benefs,
            )
        # Default fall-through within this PU state: media reached
        # the bot, payment-shaped enough to flag PU=evidence_no_accounts
        # but legacy classifier was vague. Surface as PROBABLE so it's
        # distinguishable from the "no signal at all" case.
        return _result(
            ReceiptVerdict.PROBABLE_MATCH,
            reason="evidence_no_tenant_accounts",
            derived_from="payment_understanding",
            pu_status=pu_status, pe_status=pe_status, kind=kind,
            has_attached_media=has_attached_media,
            has_text_only_claim=has_text_only_claim,
            matched_iban=matched_iban, matched_benef=matched_benef,
            receipt_ibans=receipt_ibans, receipt_benefs=receipt_benefs,
        )

    # ``no_signal`` and any unknown PU status fall through to layer 2.

    # ── Layer 2: payment_evidence_status fallback when no PU verdict.
    if pe_status == _PE_STATUS_CONFIRMED and (has_attached_media or kind in _PAYMENT_LIKE_KINDS):
        return _result(
            ReceiptVerdict.PROBABLE_MATCH,
            reason="legacy_confirmed_without_understanding",
            derived_from="payment_evidence_status",
            pu_status=pu_status, pe_status=pe_status, kind=kind,
            has_attached_media=has_attached_media,
            has_text_only_claim=has_text_only_claim,
        )

    if pe_status in _PARTIAL_PE_STATUSES and (has_attached_media or kind in _PAYMENT_LIKE_KINDS):
        return _result(
            ReceiptVerdict.UNCLEAR_RECEIPT,
            reason=f"legacy_{pe_status}",
            derived_from="payment_evidence_status",
            pu_status=pu_status, pe_status=pe_status, kind=kind,
            has_attached_media=has_attached_media,
            has_text_only_claim=has_text_only_claim,
        )

    # ── Layer 3: corrupted / fake heuristic.
    # The customer attached something the normalizer flagged as
    # payment-shaped, but the OCR / vision text came back empty.
    # Could be a non-bank screenshot, a corrupted PDF, or a spoof
    # attempt. Either way the strongest defensive verdict is
    # FAKE_OR_CORRUPTED — never enters paid flow under any future
    # rule.
    if (
        kind in _PAYMENT_LIKE_KINDS
        and (
            pe_status == _PE_STATUS_EMPTY_TEXT
            or (has_attached_media and pe_status in ("", _PE_STATUS_NOT_PAYMENT))
        )
    ):
        return _result(
            ReceiptVerdict.FAKE_OR_CORRUPTED,
            reason="payment_kind_attached_but_no_readable_text",
            derived_from="payment_evidence_status",
            pu_status=pu_status, pe_status=pe_status, kind=kind,
            has_attached_media=has_attached_media,
            has_text_only_claim=has_text_only_claim,
        )

    # ── Layer 4: text-only claim (no media at all).
    if has_text_only_claim and not has_attached_media:
        return _result(
            ReceiptVerdict.TEXT_CLAIM_UNVERIFIED,
            reason="text_only_claim_no_media",
            derived_from="fallback",
            pu_status=pu_status, pe_status=pe_status, kind=kind,
            has_attached_media=has_attached_media,
            has_text_only_claim=has_text_only_claim,
        )

    # ── Layer 5: nothing payment-related.
    return _result(
        ReceiptVerdict.NOT_PAYMENT,
        reason="no_payment_signal",
        derived_from="fallback",
        pu_status=pu_status, pe_status=pe_status, kind=kind,
        has_attached_media=has_attached_media,
        has_text_only_claim=has_text_only_claim,
    )


def _result(
    verdict: ReceiptVerdict,
    *,
    reason: str,
    derived_from: str,
    pu_status: str,
    pe_status: str,
    kind: str,
    has_attached_media: bool,
    has_text_only_claim: bool,
    matched_iban: str = "",
    matched_benef: str = "",
    receipt_ibans: Tuple[str, ...] = (),
    receipt_benefs: Tuple[str, ...] = (),
) -> ReceiptVerdictResult:
    return ReceiptVerdictResult(
        verdict=verdict,
        reason=reason,
        derived_from=derived_from,
        payment_understanding_status=pu_status,
        payment_evidence_status=pe_status,
        image_or_pdf_kind=kind,
        has_attached_media=has_attached_media,
        has_text_only_claim=has_text_only_claim,
        matched_iban=matched_iban,
        matched_beneficiary=matched_benef,
        receipt_iban_count=len(receipt_ibans),
        receipt_beneficiary_count=len(receipt_benefs),
    )


# ── 7. Log emission helper ──────────────────────────────────────────


def log_receipt_verdict(
    *,
    tenant_id: Any,
    phone: Optional[str] = None,
    conversation_id: Any = None,
    message_id: Any = None,
    source: str,
    verdict: ReceiptVerdictResult,
) -> None:
    """Emit the canonical
    ``[PAYMENT_VERIFICATION_DECISION]`` line. Operators grep this
    token to answer "which verdict did the bot reach for this
    receipt and why?". Never raises.

    Field shape locked by ``test_log_line_carries_all_canonical_fields``
    so future renames break loudly.
    """
    if not is_receipt_verdict_telemetry_enabled():
        return
    try:
        masked_phone = ""
        if phone:
            try:
                masked_phone = "*" + str(phone)[-4:]
            except Exception:
                masked_phone = ""
        payload = verdict.to_log_dict()
        body = " ".join(f"{k}={v}" for k, v in payload.items())
        logger.info(
            "[PAYMENT_VERIFICATION_DECISION] "
            "tenant_id=%s conversation_id=%s message_id=%s "
            "phone=%s source=%s %s",
            tenant_id, conversation_id, message_id,
            masked_phone, source, body,
        )
    except Exception:
        pass


__all__ = [
    "ReceiptVerdict",
    "ReceiptVerdictResult",
    "RECEIPT_VERDICTS_ALL",
    "RECEIPT_VERDICTS_VALUES",
    "PAID_FLOW_ALLOWED_VERDICTS",
    "PAID_FLOW_BLOCKED_VERDICTS",
    "compute_receipt_verdict",
    "log_receipt_verdict",
    "is_receipt_verdict_telemetry_enabled",
]
