"""
core/payment_evidence.py
────────────────────────
Universal "is this *really* a completed payment?" classifier.

Why this module exists
──────────────────────
Multiple production complaints (May 2026) traced back to the same
class of bug across many tenants:

    Customer sends a screenshot/PDF that *looks* payment-related —
    bank logo, IBAN, amount, beneficiary name, even a "تأكيد التحويل"
    button — and the bot treats the conversation as ``paid`` /
    ``payment_confirmed`` / ``order_paid``. It mutates state, ships
    the deterministic "thank-you-receipt-received" ACK, sometimes
    sends an internal phone number ("أمين") and routes the customer
    as if shipping is approved.

But often the screenshot is the **review-before-transfer** screen the
Saudi banking apps (Rajhi, AlAhli, STC Pay, Alinma, …) show right
before the user taps the final "تحويل / Confirm" button. The transfer
has NOT been executed. Nothing has been debited.

This module owns the single decision:

    Given a blob of text extracted from a customer document/image
    (vision OCR result + caption + filename) — does it contain
    EXPLICIT evidence that the transfer/payment actually completed,
    or is it a pre-transfer review / data-entry / details-only
    screen?

It returns one of four statuses:

    "confirmed"         — clear success markers ("تم التحويل",
                          "Successful", reference number, execution
                          time + status, debit confirmation).
    "pre_transfer_review" — review/verify/confirmation screen the
                            customer sees BEFORE tapping transfer.
    "needs_confirmation" — payment-context evidence (bank, IBAN,
                          amount, beneficiary) but NO completion
                          marker; treat as data-verification chat,
                          do NOT mutate order state.
    "not_payment"       — no payment-evidence signals at all (the
                          caller's classifier may still treat it as
                          a non-payment document).

The classifier is conservative-by-default. When in doubt the verdict
is ``needs_confirmation`` — which keeps the customer's funnel intact
(no premature ACK / no internal phone number leak) while still
letting the brain reply naturally.

Wiring (see callers in this PR):
  * ``modules.ai.media.normalizer._process_image`` — runs after
    vision describes the image; sets ``image_kind`` only when the
    verdict is ``confirmed``.
  * ``modules.ai.media.normalizer._process_document`` — runs after
    we extract PDF text (pypdf) + after the filename/caption
    heuristic; sets ``pdf_kind=payment_receipt`` only when verdict
    is ``confirmed``.
  * ``core.order_flow.maybe_handle_receipt_inbound`` — fires the
    "thanks, order under review" ACK ONLY for ``confirmed``.
  * ``core.order_flow.maybe_handle_payment_evidence_inbound`` —
    sibling helper that returns a short polite reply for
    ``pre_transfer_review`` / ``needs_confirmation`` without
    mutating ``order_status`` / ``payment_receipt_received``.

The classifier is pure-Python, never raises, and never touches the
DB. It is safe to call from any path (webhook, scheduler, tests).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.payment_evidence")


# ── Public status constants ─────────────────────────────────────────
PAYMENT_EVIDENCE_CONFIRMED            = "confirmed"
PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW  = "pre_transfer_review"
PAYMENT_EVIDENCE_NEEDS_CONFIRMATION   = "needs_confirmation"
PAYMENT_EVIDENCE_NOT_PAYMENT          = "not_payment"

_ALL_STATUSES = frozenset({
    PAYMENT_EVIDENCE_CONFIRMED,
    PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW,
    PAYMENT_EVIDENCE_NEEDS_CONFIRMATION,
    PAYMENT_EVIDENCE_NOT_PAYMENT,
})


# ── Arabic normalisation (mirrors core.payment_intent) ──────────────
_AR_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670]")


def _normalise(text: Optional[str]) -> str:
    """Light Arabic normalisation: strip diacritics + tatweel,
    collapse alef/ya/ta-marbuta variants, lowercase the whole blob.
    Returns empty string for falsy input. Never raises."""
    if not text:
        return ""
    try:
        t = _AR_DIACRITICS.sub("", str(text))
        t = t.replace("ـ", "")
        t = (
            t.replace("أ", "ا")
             .replace("إ", "ا")
             .replace("آ", "ا")
             .replace("ى", "ي")
             .replace("ة", "ه")
        )
        return t.lower()
    except Exception:
        return ""


# ── Lexicons ────────────────────────────────────────────────────────
# Each lexicon is a tuple of *normalised* substrings. We match by
# plain ``in`` over the normalised blob, which is fast and side-effect
# free. Order inside a tuple does not matter for correctness — only
# tuple membership (whether a substring fires) feeds the decision.

# Strong "the transfer actually completed" markers. Hits here outrank
# every other signal.
_STRONG_SUCCESS_PHRASES: Tuple[str, ...] = tuple(_normalise(s) for s in (
    # Direct Arabic completion verbs (past tense, machine-generated by
    # Saudi banking apps and SMS notifications).
    "تم التحويل",
    "تم الدفع",
    "تم السداد",
    "تم الايداع",
    "تم الإيداع",
    "تم ارسال الحواله",
    "تم إرسال الحوالة",
    "تم تنفيذ العمليه",
    "تم تنفيذ العملية",
    "تمت العمليه",
    "تمت العملية",
    "تمت بنجاح",
    "تم الارسال",
    "تم الإرسال",
    "تم تحويل المبلغ",
    "تم خصم",
    "تم خصم المبلغ",
    "خصم المبلغ من حسابك",
    "خصم من حسابك",
    "حواله ناجحه",
    "حوالة ناجحة",
    "تحويل ناجح",
    "عمليه ناجحه",
    "عملية ناجحة",
    "نجحت العمليه",
    "نجحت العملية",
    "تم بنجاح",
    "ايصال تحويل نهائي",
    "إيصال تحويل نهائي",
    "تم استلام الحواله",
    "تم استلام الحوالة",
    "حالة العمليه: ناجحه",
    "حالة العملية: ناجحة",
    "حاله العمليه ناجحه",
    "الحاله ناجحه",
    "الحالة ناجحة",
    "status: success",
    "status: successful",
    "status: completed",
    "transaction successful",
    "transfer successful",
    "transfer completed",
    "transaction completed",
    "payment successful",
    "payment completed",
    "successfully transferred",
    "successfully sent",
    "successfully paid",
    "transaction approved",
    "approved transaction",
    # Reference / execution time fields rendered by banks only AFTER
    # the transfer is committed.
    "رقم مرجع العمليه",
    "رقم مرجع العملية",
    "رقم العمليه:",
    "رقم العملية:",
    "مرجع العمليه:",
    "مرجع العملية:",
    "رقم مرجع التحويل",
    "رقم التحويل:",
    "reference number",
    "reference no",
    "ref no",
    "ref number",
    "transaction reference",
    "txn ref",
    # Execution timestamps banks print only on the final receipt.
    "وقت تنفيذ العمليه",
    "وقت تنفيذ العملية",
    "تاريخ ووقت العمليه",
    "تاريخ ووقت العملية",
    "execution time",
    "executed on",
    "executed at",
))

# Weaker — single tokens that, on their own, only hint at success.
# Used to *boost* a needs_confirmation verdict to confirmed when one
# of these appears alongside payment-context signals.
_WEAK_SUCCESS_TOKENS: Tuple[str, ...] = tuple(_normalise(s) for s in (
    "successful",
    "completed",
    "success",
    "done",
    "approved",
    "ناجح",
    "ناجحه",
    "ناجحة",
    "تمت",
    "تم",
))

# Pre-transfer review / data-entry screen markers. If any of these
# fire AND no strong-success marker is present, the verdict is
# ``pre_transfer_review`` — explicitly NOT a confirmed payment.
_PRE_TRANSFER_REVIEW_PHRASES: Tuple[str, ...] = tuple(_normalise(s) for s in (
    # Arabic Saudi banking apps (Rajhi, AlAhli, Alinma, STC Pay,
    # Albilad, ANB, SAB/SABB) — all show one of these labels above
    # the final "تحويل" button.
    "مراجعه بيانات التحويل",
    "مراجعة بيانات التحويل",
    "تأكد من البيانات",
    "تاكد من البيانات",
    "تأكيد بيانات التحويل",
    "تاكيد بيانات التحويل",
    "تاكيد التحويل",
    "تأكيد التحويل",
    "مراجعه الحواله",
    "مراجعة الحوالة",
    "اضغط تحويل",
    "اضغط على تحويل",
    "اضغط تأكيد",
    "اضغط تاكيد",
    "اضغط على تأكيد",
    "اضغط لاتمام التحويل",
    "اضغط لإتمام التحويل",
    "اضغط لإتمام العمليه",
    "اضغط لاتمام العمليه",
    "تأكيد قبل التحويل",
    "تاكيد قبل التحويل",
    "مراجعه قبل التحويل",
    "مراجعة قبل التحويل",
    "ادخال بيانات التحويل",
    "إدخال بيانات التحويل",
    "ادخل المبلغ",
    "أدخل المبلغ",
    "مراجعه المستفيد",
    "مراجعة المستفيد",
    "مراجعه الايبان",
    "مراجعة الآيبان",
    "تحقق من اسم المستفيد",
    "تحقق من الآيبان",
    "تحقق من الايبان",
    "تأكيد العمليه",
    "تاكيد العمليه",
    # English equivalents shown by the SAR-bank apps when language=EN.
    "review transfer",
    "review the transfer",
    "review beneficiary",
    "verify beneficiary",
    "confirm transfer",
    "confirm the transfer",
    "confirm and transfer",
    "tap to transfer",
    "tap transfer",
    "press confirm",
    "press to confirm",
    "transfer details",
    "transfer summary",
    "transfer preview",
    "review your transfer",
    "review and confirm",
    "review & confirm",
    "transfer confirmation",
    "transfer review",
    "before transfer",
    "before you transfer",
    "verify details",
    "verify the details",
    "verify transfer details",
    "are you sure you want to transfer",
    "do you want to confirm",
))

# Payment-context signals — bank names, IBAN, beneficiary, amount.
# These alone DO NOT prove completion. They only mark the document
# as "payment-related" so we can decide between needs_confirmation
# and not_payment.
_PAYMENT_CONTEXT_PHRASES: Tuple[str, ...] = tuple(_normalise(s) for s in (
    # Beneficiary / account labels.
    "اسم المستفيد",
    "اسم المرسل اليه",
    "اسم المرسل إليه",
    "اسم المحول اليه",
    "اسم المحول إليه",
    "المستفيد:",
    "beneficiary",
    "beneficiary name",
    "to account",
    "from account",
    "اسم الحساب",
    "حساب المستفيد",
    "رقم الحساب",
    "account number",
    "from card",
    "to card",
    "ايبان",
    "آيبان",
    "iban",
    "sa",  # IBAN prefix — only counted when it precedes 22 digits (see code)
    # Bank brand names (Saudi). Hitting a bank name alone is a weak
    # payment-context signal; combined with amount/IBAN it pushes the
    # verdict to needs_confirmation (never to confirmed).
    "الراجحي",
    "الاهلي",
    "الأهلي",
    "الانماء",
    "الإنماء",
    "البلاد",
    "ساب",
    "العربي",
    "الفرنسي",
    "الرياض",
    "العربي الوطني",
    "rajhi",
    "alrajhi",
    "ncb",
    "snb",
    "alahli",
    "al ahli",
    "alinma",
    "albilad",
    "stcpay",
    "stc pay",
    "sab",
    "sabb",
    "anb",
    "barwa",
    # Amount / currency labels.
    "المبلغ",
    "مبلغ التحويل",
    "amount",
    "transfer amount",
    "ر.س",
    "ريال",
    "sar",
))

# Generic "payment-screen language" hints — used purely to inflate
# the payment-context score when bank-brand or amount keywords are
# absent. None of these promote a verdict to confirmed on their own.
_GENERIC_PAYMENT_HINTS: Tuple[str, ...] = tuple(_normalise(s) for s in (
    "تحويل",
    "حواله",
    "حوالة",
    "ايصال",
    "إيصال",
    "فاتوره",
    "فاتورة",
    "دفع",
    "سداد",
    "transfer",
    "payment",
    "remittance",
    "receipt",
    "invoice",
))

# Regex for the "SA + 22 digits" IBAN form (Saudi). When this matches
# we know the screen is payment-related even if no Arabic keywords
# fire (e.g. screenshot of a pure-numbers verification screen).
_SAUDI_IBAN_RE = re.compile(r"\bsa\s?\d{2}\s?(?:\d\s?){20}\b", re.IGNORECASE)

# Regex for "reference / transaction" numeric IDs — these only appear
# on the FINAL receipt, never on the review-before-transfer screen.
# We accept hyphens / spaces between digit groups.
_REF_NUMBER_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"رقم\s*(?:مرجع|العمليه|العملية|التحويل)\s*[:#]?\s*[A-Z0-9][A-Z0-9\-\s]{4,}", re.IGNORECASE),
    re.compile(r"\b(?:ref(?:erence)?|txn|transaction)\s*(?:number|no\.?|#)?\s*[:#]?\s*[A-Z0-9][A-Z0-9\-]{4,}", re.IGNORECASE),
)


def _classify_label(category: str) -> str:
    """Return a stable short label for the structured ``reason`` log
    field. Used so operators can grep a single token instead of a
    sentence."""
    return category


def _scan_phrases(blob: str, phrases: Tuple[str, ...]) -> List[str]:
    """Return the list of phrases (normalised) that appear inside
    ``blob`` (already normalised). Bounded by the cardinality of the
    lexicon — never returns a giant list."""
    if not blob or not phrases:
        return []
    hits: List[str] = []
    for p in phrases:
        if p and p in blob:
            hits.append(p)
    return hits


def classify_payment_evidence(
    text: Optional[str],
    *,
    extra_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Classify a blob of extracted text (vision OCR + caption +
    filename + PDF text) into one of the four payment-evidence
    statuses.

    Parameters
    ----------
    text:
        The combined text to inspect. Caller is responsible for
        concatenating whatever signals it has (caption, filename,
        OCR / vision result, PDF extracted text). Empty / None →
        ``not_payment`` with reason ``empty_text``.
    extra_context:
        Optional dict of conversation-state hints. Currently
        consumed keys:
            * ``awaiting_payment_receipt`` (bool): the bot just
              asked for a receipt. We do NOT use this to PROMOTE a
              verdict to ``confirmed`` — only to break ties between
              ``needs_confirmation`` and ``not_payment``.

    Return
    ------
    dict with keys::

        {
          "status":   <one of PAYMENT_EVIDENCE_*>,
          "reason":   <stable short machine label>,
          "signals": {
              "success_hits":          [...],
              "pre_review_hits":       [...],
              "context_hits":          [...],
              "generic_payment_hits":  [...],
              "iban_present":          bool,
              "reference_number_present": bool,
              "weak_success_present":  bool,
          },
        }

    Never raises.
    """
    blob = _normalise(text)

    # Empty input → fast exit. The caller will keep the original
    # downstream behaviour (no kind, no short-circuit).
    if not blob:
        return {
            "status":  PAYMENT_EVIDENCE_NOT_PAYMENT,
            "reason":  "empty_text",
            "signals": {
                "success_hits": [],
                "pre_review_hits": [],
                "context_hits": [],
                "generic_payment_hits": [],
                "iban_present": False,
                "reference_number_present": False,
                "weak_success_present": False,
            },
        }

    success_hits     = _scan_phrases(blob, _STRONG_SUCCESS_PHRASES)
    pre_review_hits  = _scan_phrases(blob, _PRE_TRANSFER_REVIEW_PHRASES)
    context_hits_all = _scan_phrases(blob, _PAYMENT_CONTEXT_PHRASES)
    generic_hits     = _scan_phrases(blob, _GENERIC_PAYMENT_HINTS)
    weak_success_hits = _scan_phrases(blob, _WEAK_SUCCESS_TOKENS)

    # The "sa" entry in _PAYMENT_CONTEXT_PHRASES is the IBAN prefix —
    # but as a plain substring it's a false-positive magnet (matches
    # "salla", "sa3a", "sar", "rasa"). We accept it ONLY when the
    # regex confirms a full SA+22-digit IBAN form anywhere in the
    # text. Drop the bare "sa" hits otherwise.
    iban_present = bool(_SAUDI_IBAN_RE.search(blob))
    context_hits = [h for h in context_hits_all if h != "sa"]
    if iban_present and "sa" not in context_hits:
        context_hits.append("sa")

    # Reference-number patterns: any hit OR an explicit "reference
    # number" / "رقم مرجع العملية" phrase already captured above.
    reference_number_present = any(
        p.search(blob) for p in _REF_NUMBER_PATTERNS
    ) or any(s in blob for s in (
        "رقم مرجع العمليه", "رقم مرجع العملية",
        "reference number", "transaction reference",
    ))

    signals = {
        "success_hits":             success_hits,
        "pre_review_hits":          pre_review_hits,
        "context_hits":             context_hits,
        "generic_payment_hits":     generic_hits,
        "iban_present":             iban_present,
        "reference_number_present": reference_number_present,
        "weak_success_present":     bool(weak_success_hits),
    }

    # ── Decision tree ───────────────────────────────────────────
    # Priority (top wins):
    #   1. ANY strong-success phrase → CONFIRMED. This is the only
    #      verdict that lets downstream code mutate order state.
    #   2. ANY pre-transfer-review phrase → PRE_TRANSFER_REVIEW.
    #      Banks NEVER print these labels on a completed-transfer
    #      receipt, so a hit here is high-confidence "NOT yet
    #      transferred".
    #   3. Weak-success token + payment context (IBAN / reference /
    #      bank brand + amount) → CONFIRMED. Catches the case where
    #      vision text only carries "Successful" or "ناجحة" without
    #      the longer phrase.
    #   4. Reference number rendered + payment context →
    #      CONFIRMED. Banks only print transaction IDs on the
    #      final receipt.
    #   5. Payment-context signals only → NEEDS_CONFIRMATION.
    #   6. Generic payment hints only → NEEDS_CONFIRMATION.
    #   7. Nothing → NOT_PAYMENT.

    if success_hits:
        return {
            "status":  PAYMENT_EVIDENCE_CONFIRMED,
            "reason":  "strong_success_phrase",
            "signals": signals,
        }

    if pre_review_hits:
        return {
            "status":  PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW,
            "reason":  "pre_transfer_review_phrase",
            "signals": signals,
        }

    has_payment_context = bool(
        context_hits or iban_present or len(generic_hits) >= 2
    )

    if weak_success_hits and has_payment_context:
        # Pair check: "ناجحة"/"Successful" on a screen that ALSO has
        # IBAN/bank/amount → real completed transfer. On its own
        # this token can mean anything ("ناجحة في الاختبار").
        return {
            "status":  PAYMENT_EVIDENCE_CONFIRMED,
            "reason":  "weak_success_with_context",
            "signals": signals,
        }

    if reference_number_present and has_payment_context:
        return {
            "status":  PAYMENT_EVIDENCE_CONFIRMED,
            "reason":  "reference_number_with_context",
            "signals": signals,
        }

    if has_payment_context:
        return {
            "status":  PAYMENT_EVIDENCE_NEEDS_CONFIRMATION,
            "reason":  "payment_context_no_success_marker",
            "signals": signals,
        }

    if generic_hits:
        # Single generic hint like the word "تحويل" without context.
        # Could be the customer typing the word ("هذا التحويل …") on
        # a screenshot of something else entirely. We mark it as
        # needs_confirmation only when the brain state says the bot
        # was waiting for a receipt; otherwise it's not_payment.
        if extra_context and bool(extra_context.get("awaiting_payment_receipt")):
            return {
                "status":  PAYMENT_EVIDENCE_NEEDS_CONFIRMATION,
                "reason":  "generic_payment_hint_with_awaiting_context",
                "signals": signals,
            }
        return {
            "status":  PAYMENT_EVIDENCE_NOT_PAYMENT,
            "reason":  "generic_payment_hint_only",
            "signals": signals,
        }

    return {
        "status":  PAYMENT_EVIDENCE_NOT_PAYMENT,
        "reason":  "no_payment_signals",
        "signals": signals,
    }


# ── Short polite replies for the non-confirmed branches ─────────────
# Kept here so every caller speaks the same tone. The copy is
# deliberately SHORT, friendly, and contains zero internal-routing
# claims (no shipping promises, no internal agent phone numbers).

_PRE_TRANSFER_REVIEW_REPLY_AR = (
    "هذي تبدو شاشة مراجعة قبل التحويل 🌷\n"
    "تأكد من البيانات واضغط تحويل، وأرسل لي الإيصال النهائي بعد إتمام "
    "العملية وأتابع طلبك بإذن الله."
)

_NEEDS_CONFIRMATION_REPLY_AR = (
    "وصلني الملف 👍\n"
    "بعد التحويل أرسل لي الإيصال النهائي وأتابع طلبك بإذن الله."
)


def compose_payment_evidence_reply(
    status: str,
    *,
    awaiting_receipt: bool = False,
) -> Optional[str]:
    """Return a short, tone-safe reply for the non-confirmed
    branches. Returns ``None`` for ``confirmed`` (the existing
    ``order_flow._compose_receipt_ack`` owns that copy) and for
    ``not_payment`` (caller should let the brain answer normally).

    The replies intentionally avoid:
      * promising shipping / order completion
      * leaking internal phone numbers (e.g. "أمين")
      * changing the conversation tone or merchant persona
    """
    if status == PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW:
        return _PRE_TRANSFER_REVIEW_REPLY_AR
    if status == PAYMENT_EVIDENCE_NEEDS_CONFIRMATION:
        return _NEEDS_CONFIRMATION_REPLY_AR
    return None


def log_payment_evidence_verdict(
    *,
    tenant_id: Any,
    phone: Optional[str],
    source: str,
    verdict: Dict[str, Any],
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit a single structured log line per classification call.

    The format ``[PAYMENT_EVIDENCE] payment_evidence_status=...
    payment_evidence_reason=...`` is the canonical grep target so
    on-call can answer the "why did the bot ACK this as paid?"
    question in seconds.
    """
    try:
        sig = verdict.get("signals") or {}
        masked_phone = ""
        if phone:
            try:
                masked_phone = "*" + str(phone)[-4:]
            except Exception:
                masked_phone = ""
        logger.info(
            "[PAYMENT_EVIDENCE] tenant=%s phone=%s source=%s "
            "payment_evidence_status=%s payment_evidence_reason=%s "
            "success_hits=%d pre_review_hits=%d context_hits=%d "
            "generic_hits=%d iban=%s ref=%s weak_success=%s "
            "extra=%s",
            tenant_id, masked_phone, source,
            verdict.get("status"), verdict.get("reason"),
            len(sig.get("success_hits") or []),
            len(sig.get("pre_review_hits") or []),
            len(sig.get("context_hits") or []),
            len(sig.get("generic_payment_hits") or []),
            bool(sig.get("iban_present")),
            bool(sig.get("reference_number_present")),
            bool(sig.get("weak_success_present")),
            extra or {},
        )
    except Exception:
        pass


__all__ = [
    "PAYMENT_EVIDENCE_CONFIRMED",
    "PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW",
    "PAYMENT_EVIDENCE_NEEDS_CONFIRMATION",
    "PAYMENT_EVIDENCE_NOT_PAYMENT",
    "classify_payment_evidence",
    "compose_payment_evidence_reply",
    "log_payment_evidence_verdict",
]
