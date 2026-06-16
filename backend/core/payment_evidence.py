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

# ── Hard-negative lexicon (May 2026 hotfix) ─────────────────────────
# A regression in production (Kaaba/Hajj greeting card was classified
# as payment evidence → bot asked the customer for a missing product
# and shipping details instead of just replying to the greeting):
# the lexicon-based classifier was too permissive when an image was
# *clearly* religious / social / festive in nature. We now run a
# short, conservative greeting/social filter BEFORE any payment
# scoring. A hit here forces ``NOT_PAYMENT`` regardless of any other
# context hits — those generic words ("ايصال" in caption text, etc.)
# are noise once we know the image is a greeting card.
#
# This list intentionally captures only UNAMBIGUOUS greetings — the
# kind a bank statement / receipt / invoice would NEVER print:
#   * Eid / Ramadan / Hajj / Friday well-wishes,
#   * generic congratulations,
#   * sympathies / condolences,
#   * "good morning" / "good evening" / friendly invocations.
#
# It is NOT a catch-all for "any unrelated image". Unrelated images
# with no payment context already classify as NOT_PAYMENT via the
# existing decision tree. This filter exists only to protect against
# the case where a greeting card happens to share a token with the
# payment lexicon (e.g. someone wrote "إيصال صدقة" on it).
_GREETING_AND_SOCIAL_PHRASES: Tuple[str, ...] = tuple(_normalise(s) for s in (
    # Religious / festive greetings (Saudi-typical wording).
    "اهنئكم",
    "أهنئكم",
    "تهنئه",
    "تهنئة",
    "تقبل الله منا ومنكم",
    "تقبل الله",
    "كل عام وانتم بخير",
    "كل عام وأنتم بخير",
    "عيدكم مبارك",
    "عيد مبارك",
    "عيد سعيد",
    "عيد اضحى مبارك",
    "عيد فطر مبارك",
    "بقدوم عشر ذي الحجه",
    "بقدوم عشر ذي الحجة",
    "عشر ذي الحجه",
    "عشر ذي الحجة",
    "يوم النحر",
    "يوم عرفه",
    "يوم عرفة",
    "وقفة عرفه",
    "وقفة عرفة",
    "الحج المبرور",
    "حج مبرور",
    "اضحى مبارك",
    "اضحى سعيد",
    "رمضان مبارك",
    "رمضان كريم",
    "حلول شهر رمضان",
    "العام الهجري",
    "مولد النبوي",
    "ذكرى المولد",
    "اليوم الوطني",
    "يوم التاسيس",
    "يوم التأسيس",
    "جمعه مباركه",
    "جمعة مباركة",
    "صباح الخير",
    "مساء الخير",
    "صبحكم الله بالخير",
    "مساكم الله بالخير",
    "اسعد الله صباحكم",
    "أسعد الله صباحكم",
    # Generic congratulations / condolences (never on a receipt).
    "مبروك",
    "الف مبروك",
    "ألف مبروك",
    "تهانينا",
    "بمناسبة",
    "بالسلامه",
    "بالسلامة",
    "حمدا لله على السلامه",
    "حمداً لله على السلامة",
    "البقاء لله",
    "انا لله وانا اليه راجعون",
    "إنا لله وإنا إليه راجعون",
    "احسن الله عزاكم",
    "أحسن الله عزاكم",
    # Common Islamic supplications used in social cards.
    "اللهم صل وسلم",
    "اللهم صلي وسلم",
    "اللهم آمين",
    "اللهم امين",
    "جزاكم الله خير",
    "بارك الله فيكم",
    "اسعدكم الله",
    "أسعدكم الله",
    "يبلغكم الله",
    "بلغكم الله",
    "اسعدكم طول الدهر",
    "أسعدكم طول الدهر",
    # English equivalents that show up on bilingual cards.
    "eid mubarak",
    "ramadan kareem",
    "ramadan mubarak",
    "happy eid",
    "blessed eid",
    "blessed hajj",
    "happy new year",
    "happy friday",
    "good morning",
    "good evening",
    "congratulations",
    "condolences",
))


def _is_hard_negative(blob: str) -> Optional[str]:
    """Return the first matched greeting / social phrase if the blob
    is unmistakably a greeting card / social message. Returns
    ``None`` otherwise. Pure substring check on the normalised blob.

    Used as a HARD GATE in front of the payment-evidence classifier:
    when a hit fires, we immediately return ``NOT_PAYMENT`` so the
    image flows back to the general vision/brain path.
    """
    if not blob:
        return None
    for phrase in _GREETING_AND_SOCIAL_PHRASES:
        if phrase and phrase in blob:
            return phrase
    return None

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


# ── Filename hints (May 2026 hotfix #2) ────────────────────────────
# Production regression: a real bank-generated PDF named
# ``Transaction-Receipt.pdf`` was demoted from confirmed →
# pre_transfer_review because its body contained the section header
# "تأكيد التحويل" (which is ALSO a pre-review imperative on the
# pre-transfer page of some apps). The filename is a strong positive
# signal — banks don't call the *review* page a "Receipt". When the
# filename clearly indicates a receipt artifact AND the body lacks
# explicit pre-review IMPERATIVE verbs (like "اضغط تحويل" / "Tap
# to transfer"), we keep the verdict as CONFIRMED instead of
# demoting it.
#
# This list captures the canonical Saudi & GCC bank receipt
# filenames we've observed in production exports.
_RECEIPT_FILENAME_PATTERNS: Tuple[str, ...] = tuple(s.lower() for s in (
    "transaction-receipt",
    "transaction_receipt",
    "transactionreceipt",
    "transfer-receipt",
    "transfer_receipt",
    "transferreceipt",
    "wire-confirmation",
    "wire_confirmation",
    "payment-confirmation",
    "payment_confirmation",
    "payment-receipt",
    "payment_receipt",
    "receipt-",
    "_receipt",
    "ايصال",
    "إيصال",
    "ايصال_تحويل",
    "إيصال_تحويل",
    "ايصال-تحويل",
    "tahweel",
    "tahwil",
    "rajhi_receipt",
    "alahli_receipt",
    "alinma_receipt",
    "stcpay_receipt",
    "stcpay-receipt",
))

# Strict pre-review IMPERATIVES — these are call-to-action verbs that
# appear ONLY on the pre-transfer button screen, never on a completed
# transfer receipt. Hits here override the receipt-filename hint.
_PRE_TRANSFER_IMPERATIVES: Tuple[str, ...] = tuple(_normalise(s) for s in (
    "اضغط تحويل",
    "اضغط على تحويل",
    "اضغط تأكيد",
    "اضغط تاكيد",
    "اضغط على تأكيد",
    "اضغط لاتمام التحويل",
    "اضغط لإتمام التحويل",
    "اضغط لإتمام العمليه",
    "اضغط لاتمام العمليه",
    "اضغط للتحويل",
    "press transfer",
    "press confirm",
    "press to confirm",
    "tap transfer",
    "tap to transfer",
    "tap to confirm",
    "confirm and transfer",
    "review and confirm",
    "review & confirm",
    "are you sure you want to transfer",
    "do you want to confirm",
))


def _filename_signals_receipt(filename: Optional[str]) -> bool:
    """Return True when the filename matches the canonical
    bank-receipt naming convention. Pure substring check on a
    lowercased copy of the filename."""
    if not filename:
        return False
    fn = str(filename).lower()
    for pat in _RECEIPT_FILENAME_PATTERNS:
        if pat and pat in fn:
            return True
    return False


def _body_has_pre_review_imperative(blob: str) -> bool:
    """Return True when the (already-normalised) body contains an
    explicit pre-transfer button label ("Tap to transfer" /
    "اضغط تحويل") — distinct from a passive section header like
    "تأكيد التحويل" which is rendered on both the pre-review screen
    AND on completed receipts."""
    if not blob:
        return False
    for phrase in _PRE_TRANSFER_IMPERATIVES:
        if phrase and phrase in blob:
            return True
    return False


def classify_payment_evidence(
    text: Optional[str],
    *,
    extra_context: Optional[Dict[str, Any]] = None,
    filename: Optional[str] = None,
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

    # ── Hard-negative gate ─────────────────────────────────────────
    # Greeting cards / social messages immediately short-circuit to
    # NOT_PAYMENT, even if the OCR text also happens to contain a
    # noisy payment token. The image flows back to the general
    # vision/brain path so the bot replies to the actual content
    # (e.g. "تقبل الله منا ومنكم").
    _hn = _is_hard_negative(blob)
    if _hn is not None:
        return {
            "status":  PAYMENT_EVIDENCE_NOT_PAYMENT,
            "reason":  "greeting_or_social_content",
            "signals": {
                "success_hits": [],
                "pre_review_hits": [],
                "context_hits": [],
                "generic_payment_hits": [],
                "iban_present": False,
                "reference_number_present": False,
                "weak_success_present": False,
                "greeting_hit":  _hn,
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

    # Record filename-hint signal for trace logging.
    _fname_signals_receipt = _filename_signals_receipt(filename)
    _body_has_imperative   = _body_has_pre_review_imperative(blob)
    signals["filename_signals_receipt"]    = _fname_signals_receipt
    signals["pre_review_imperative_match"] = _body_has_imperative

    if success_hits:
        return {
            "status":  PAYMENT_EVIDENCE_CONFIRMED,
            "reason":  "strong_success_phrase",
            "signals": signals,
        }

    # ── Completed bank receipt override (P0) ────────────────────────
    # Rajhi / AlAhli final receipts often contain passive headers like
    # ``تأكيد التحويل`` that also appear on pre-transfer screens.
    # When amount + beneficiary/IBAN + bank are present WITHOUT a
    # tap-to-transfer imperative, treat as completed — not pre-review.
    try:
        from core.bank_transfer_receipt_resolver import (  # noqa: PLC0415
            extract_bank_receipt_fields,
        )

        _ext = extract_bank_receipt_fields(text, filename=filename)
        if (
            _ext.amount
            and (_ext.beneficiary_name or iban_present)
            and _ext.bank_name
            and not _ext.has_pre_review_imperative
            and _ext.receipt_type == "final_receipt"
        ):
            return {
                "status":  PAYMENT_EVIDENCE_CONFIRMED,
                "reason":  "bank_receipt_final_fields",
                "signals": {**signals, "bank_receipt_resolver": True},
            }
    except Exception:  # noqa: BLE001
        pass

    # Pre-review demotion is conditional. A bank-generated PDF named
    # "Transaction-Receipt.pdf" frequently contains the section
    # header "تأكيد التحويل" even though the transfer is complete —
    # that header sits in our pre_review lexicon. We only treat the
    # blob as pre-review when:
    #   * the filename does NOT signal a receipt artifact, OR
    #   * the body contains an explicit pre-review IMPERATIVE
    #     ("اضغط تحويل" / "Tap to transfer") that no completed
    #     receipt would ever print.
    if pre_review_hits and not _fname_signals_receipt:
        return {
            "status":  PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW,
            "reason":  "pre_transfer_review_phrase",
            "signals": signals,
        }
    if pre_review_hits and _fname_signals_receipt and _body_has_imperative:
        return {
            "status":  PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW,
            "reason":  "pre_transfer_imperative_with_receipt_filename",
            "signals": signals,
        }
    # ── Filename-only confirmation ────────────────────────────────
    # Filename is a real-receipt artifact name, no explicit
    # pre-review imperative, and the body has SOME payment context
    # (even just a section header like "تأكيد التحويل"). Treat as
    # confirmed so the customer's funnel proceeds. Without this
    # branch we'd loop the customer back through "send me the final
    # receipt" forever when the bank PDF body shares a noun with
    # the pre-review lexicon — exactly the production bug May 2026.
    if _fname_signals_receipt and (
        pre_review_hits or context_hits or iban_present
        or weak_success_hits or generic_hits
    ):
        return {
            "status":  PAYMENT_EVIDENCE_CONFIRMED,
            "reason":  "receipt_filename_with_payment_context",
            "signals": signals,
        }

    # ── Discriminating-context check ───────────────────────────────
    # A single weak hit like "ريال" / "sar" / "amount" appearing on
    # any random image (a product photo with a price tag, a salary
    # screenshot, etc.) used to be enough to push the verdict to
    # NEEDS_CONFIRMATION — which then short-circuited the
    # conversation into the receipt-pending flow. We now require
    # one of:
    #   * a Saudi IBAN (regex-confirmed),
    #   * OR ≥ 2 distinct payment-context hits (e.g. bank brand +
    #     amount, or beneficiary + IBAN-prefix-without-22-digits),
    #   * OR ≥ 2 distinct generic payment hints (e.g. "تحويل" AND
    #     "إيصال" — single hit alone is too noisy).
    # This keeps real bank screenshots well within the gate while
    # closing the false-positive door for product photos and chat
    # text that only mentions a price.
    has_payment_context = bool(
        iban_present
        or len(context_hits) >= 2
        or len(generic_hits) >= 2
        or (len(context_hits) >= 1 and len(generic_hits) >= 1)
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

    if len(generic_hits) >= 2:
        # Two or more generic payment hints (e.g. "تحويل" + "إيصال")
        # without IBAN / bank-brand / amount. Marginal evidence: only
        # treat as ``needs_confirmation`` when the brain state says
        # the bot was waiting for a receipt. Otherwise → not_payment.
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
    # Single generic hit (e.g. the lone word "ايصال" in caption text)
    # is intentionally treated as NOT_PAYMENT regardless of brain
    # state — too noisy to act on. The brain still sees the image and
    # can react to it via the normal vision/brain path.

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
    except Exception:  # noqa: silent-ok — telemetry must not block payment classify
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
