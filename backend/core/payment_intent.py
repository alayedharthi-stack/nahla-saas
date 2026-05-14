"""
core/payment_intent.py
──────────────────────
Detect text-only "I just paid / I transferred" claims and short-circuit
the AI brain with a deterministic acknowledgement.

Why this module exists
──────────────────────
``core/order_flow.maybe_handle_receipt_inbound`` already short-circuits
the brain when a PDF / image arrives in an active-order context — the
customer literally attached the receipt. But Saudi customers also
routinely SAY they paid without attaching anything:

    Bot @ 7:50 PM : "تمام، بانتظار الإيصال وقت ما تحوّل وأكمّل لك …"
    Customer @ 10:08 PM : "تم التحويل"
    Bot @ 10:08 PM : "أنا هنا — قول وش تحتاج وأكمّل معك."    ← BUG

The bot lost the funnel and shipped the generic dedup fallback because
"تم التحويل" is two tokens and matches no high-confidence intent in the
brain. From the customer's perspective the store just FORGOT the order
mid-checkout.

This module owns the deterministic recovery for that single class of
inbounds:

  1. ``detect_payment_confirmation_text(text)`` — does the message
     CLAIM a transfer / payment / deposit was made?
  2. ``maybe_handle_payment_claim(...)`` — when the claim arrives in
     an active-order or awaiting-receipt context, return a
     deterministic ACK + state patch the webhook can apply, bypassing
     the brain entirely.

We do NOT trust the claim. The state remains ``awaiting_payment_receipt``
unless the customer also attaches a receipt — at which point the
existing media short-circuit takes over. The ACK simply asks for the
receipt politely so verification can complete.

Categorisation priority (May 2026 policy)
─────────────────────────────────────────
``payment_confirmation`` and ``bank_transfer_context`` outrank
``fallback`` / ``smalltalk`` / ``general_assistant`` / ``unknown_message``.
If THIS detector fires the brain MUST NOT run; if the brain DID run
and produced a generic fallback line, ``rewrite_generic_reply_for_payment_context``
swaps it for a payment-aware sentence.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.payment_intent")


# ── Payment-confirmation tokens (Arabic) ────────────────────────────
# Each entry is the *normalised* form (alef variants collapsed to ``ا``,
# ta-marbuta to ``ه``, ya-with-dots to ``ي``, diacritics + tatweel
# stripped). The matcher applies the same normalisation to inbound
# tokens before checking, so a single entry catches the obvious
# orthographic variants without an explosion of permutations.
#
# We deliberately keep this list TIGHT — only phrases that almost
# always carry a "transfer completed" semantic. Ambiguous lone tokens
# like ``"دفع"`` (could be a question: "كيف الدفع؟") are NOT here;
# they show up via ``_PAYMENT_VERBS`` only when paired with the
# completion marker ``"تم"`` / ``"خلصت"`` / past-tense conjugations.

_NORMALISE_AR_RE = re.compile(r"[\u064B-\u065F\u0670]")  # diacritics


def _normalise_arabic(text: str) -> str:
    """Light Arabic normalisation — matches the convention used in
    ``core/customer_name_cleanup`` and ``core/customer_name_extractor``
    so the same orthographic mapping is applied everywhere."""
    if not text:
        return ""
    t = _NORMALISE_AR_RE.sub("", text)
    t = t.replace("ـ", "")
    t = (
        t.replace("أ", "ا")
         .replace("إ", "ا")
         .replace("آ", "ا")
         .replace("ى", "ي")
         .replace("ة", "ه")
    )
    return t.lower()


# Substring phrases (NOT word boundaries) — we match these as the
# customer often writes one of them as the entire message body, or
# embedded inside a sentence. The list is conservative enough that
# substring matching does not generate false positives on conversation
# text. Each entry is stored pre-normalised.
_PAYMENT_CONFIRMATION_PHRASES: Tuple[str, ...] = tuple(
    _normalise_arabic(p) for p in (
        # ── Direct "completed" verbs ───────────────────────────────
        "تم التحويل",
        "تم الدفع",
        "تم السداد",
        "تم الإيداع",
        "تم الايداع",
        "تم ارسال الحواله",
        "تم إرسال الحوالة",
        "تم تحويل المبلغ",
        "تم تحويل",
        "تم سداد",
        "تم ايداع",
        "تم تحويل الفلوس",
        "تم تحويل المال",
        "خلصت التحويل",
        "خلصت الدفع",
        "اكملت الدفع",
        "أكملت الدفع",
        "اكملت التحويل",
        "ارسلت الايصال",
        "أرسلت الإيصال",
        "ارسلت إيصال",
        "ارسلت لك الايصال",
        "هذا الإيصال",
        "هذا الايصال",
        "هذي الحوالة",
        "هذي الحواله",
        "ايصال التحويل",
        "إيصال التحويل",
        "وصل التحويل",
        "وصل الدفع",
        # ── First-person past-tense claims ────────────────────────
        "حولت لك",
        "حولت المبلغ",
        "حولت الفلوس",
        "حولت المال",
        "دفعت لك",
        "دفعت المبلغ",
        "ابشرك حولت",
        "أبشرك حولت",
        "ابشر حولت",
        "ودعت لك",
        "اودعت",
        "أودعت",
        "سددت",
        "سددت لك",
        # ── Reassurance phrasings ─────────────────────────────────
        "تم التحويل بنجاح",
        "وصلتك الحوالة",
        "وصلتك الحواله",
        "شيك التحويل",
        "تأكيد التحويل",
        "تاكيد التحويل",
        "تأكيد الدفع",
        "تاكيد الدفع",
        # ── Very short claims (entire message is just one of these) ─
        # These are handled by the EXACT short-message matcher below
        # so they don't trip on incidental occurrences inside longer
        # support / question text.
    )
)


# Whole-message exact tokens — fires ONLY when the inbound (after
# trim + Arabic normalisation) IS one of these. A 4-letter "تم" in
# the middle of a question must not match here.
_PAYMENT_CONFIRMATION_EXACT: frozenset = frozenset(
    _normalise_arabic(p) for p in (
        "تم",
        "تمام",
        "حولت",
        "دفعت",
        "سددت",
        "اودعت", "أودعت",
        "ارسلت", "أرسلت",
        "اكملت", "أكملت",
        "خلصت",
        "تم التحويل",
        "تم الدفع",
        "تم السداد",
        "تم الايداع",
        "تم الإيداع",
        "حوالة",
        "حواله",
        "ايصال",
        "إيصال",
        "تحويل",
        "تحويل بنكي",
        "تحويل بنكى",
    )
)


# Generic fallback phrases the brain occasionally ships when it does
# not know how to answer. We detect these AS-WRITTEN (after a soft
# normalisation) so we can REWRITE them when the inbound was payment-
# context. Adding entries here is the safest way to suppress new
# generic openers — the rewriter never deletes a reply, it only
# replaces the canned ones.
_GENERIC_FALLBACK_MARKERS: Tuple[str, ...] = (
    "انا هنا",
    "أنا هنا",
    "وش اقدر اخدمك",
    "وش أقدر أخدمك",
    "وش تحتاج",
    "تامر بشي",
    "تأمر بشي",
    "كيف اقدر اخدمك",
    "كيف أقدر أخدمك",
    "كيف أقدر أساعدك",
    "كيف اقدر اساعدك",
    "هل تحتاج شي",
)


def detect_payment_confirmation_text(text: Optional[str]) -> bool:
    """Return True when the inbound message reads as a "I paid /
    I transferred / here is the receipt" claim.

    Never raises. The check is intentionally tight — we'd rather
    miss an ambiguous claim (the brain handles it normally) than
    swallow an unrelated message and reply about a non-existent
    transfer.
    """
    if not text or not isinstance(text, str):
        return False
    raw = text.strip()
    if not raw:
        return False
    if len(raw) > 240:
        # Long messages are rarely pure payment claims; we accept the
        # missed-positive in exchange for skipping the substring pass
        # on essays.
        return False
    normalised = _normalise_arabic(raw)
    if not normalised:
        return False

    # 1) Exact-message match (short claims like "تم التحويل").
    if normalised in _PAYMENT_CONFIRMATION_EXACT:
        return True

    # 2) Substring match against the high-precision phrase list.
    for phrase in _PAYMENT_CONFIRMATION_PHRASES:
        if phrase and phrase in normalised:
            return True

    return False


def looks_like_generic_fallback_reply(reply_text: Optional[str]) -> bool:
    """Return True when the brain's reply text matches one of the
    canned fallback phrases. Used by the post-brain rewriter so we
    can swap a generic line for a payment-aware one whenever the
    inbound was clearly payment-related."""
    if not reply_text or not isinstance(reply_text, str):
        return False
    normalised = _normalise_arabic(reply_text)
    if not normalised:
        return False
    for marker in _GENERIC_FALLBACK_MARKERS:
        if marker in normalised:
            return True
    return False


# ── Deterministic ACK copy ──────────────────────────────────────────
# Two variants — choose by state so the customer doesn't see the
# same sentence twice when they say "تم التحويل" then a minute later
# send an actual receipt PDF.

_ACK_FIRST_CLAIM = (
    "وصل، يعطيك العافية 🌷\n"
    "لو تكرّمت أرسل لي صورة الإيصال أو PDF التحويل عشان نتحقق "
    "ونكمل تجهيز الطلب بإذن الله."
)

_ACK_RECEIPT_REMINDER = (
    "تمام، بإذن الله نتحقق من التحويل ونكمل الطلب 🌷\n"
    "لو ما أرسلت الإيصال بعد، أرسله هنا (صورة أو PDF) "
    "عشان نسرّع التحقق."
)

_ACK_REVIEW_IN_PROGRESS = (
    "تم استلام الإيصال، وبإذن الله يتم إكمال الطلب حال التحقق 🌷\n"
    "بنتواصل معك أول ما يتجهز للشحن."
)


def compose_payment_claim_ack(
    *,
    selected_product: Optional[str],
    awaiting_receipt: bool,
    receipt_received: bool,
) -> str:
    """Choose the right deterministic ACK for a text-only payment
    claim, based on the current order state."""
    if receipt_received:
        # Customer already sent a receipt — they're now nudging us
        # for an update. Reassure with the "under review" copy.
        return _ACK_REVIEW_IN_PROGRESS
    if awaiting_receipt:
        # We already asked once. Don't re-ask aggressively — gentle
        # nudge with a stronger "we trust you, just need the proof".
        return _ACK_RECEIPT_REMINDER
    base = _ACK_FIRST_CLAIM
    if selected_product:
        # Mention the product so the customer knows the bot didn't
        # forget the funnel.
        base = (
            f"وصل، يعطيك العافية على طلب ({selected_product}) 🌷\n"
            "لو تكرّمت أرسل لي صورة الإيصال أو PDF التحويل عشان "
            "نتحقق ونكمل تجهيز الطلب بإذن الله."
        )
    return base


def maybe_handle_payment_claim(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    inbound_text: str,
    has_attached_media: bool,
) -> Optional[Dict[str, Any]]:
    """Decide whether to short-circuit the brain with a payment-claim
    ACK. Returns a dict with ``reply_text`` + ``state_patch`` when
    the short-circuit should fire, or ``None`` when the brain should
    handle the inbound normally.

    Rules:
      1. Inbound must be a recognised payment-confirmation phrase.
      2. No attached media — actual receipts go through the existing
         ``maybe_handle_receipt_inbound`` short-circuit.
      3. Conversation must have an ACTIVE order context. We define
         "active" as any of:
            * ``selected_product`` set in brain state,
            * ``awaiting_payment_receipt = True``,
            * ``payment_receipt_received = True`` (customer is
              following up on an already-submitted receipt),
            * ``order_status`` in {awaiting_receipt, under_review,
              processing, payment_pending}.

    Never raises. Returns ``None`` on any unexpected DB / state
    error so the brain can still take over.
    """
    if has_attached_media:
        return None
    if not detect_payment_confirmation_text(inbound_text):
        return None

    try:
        from core.order_flow import _focus_summary, _load_brain_state  # noqa: PLC0415
        _conv, bs = _load_brain_state(db, tenant_id=tenant_id, phone=phone)
        s = _focus_summary(bs)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[PAYMENT_INTENT] state load failed tenant=%s phone=%s err=%s",
            tenant_id, phone, exc,
        )
        return None

    selected_product   = s.get("selected_product")
    awaiting_receipt   = bool(s.get("awaiting_payment_receipt"))
    receipt_received   = bool(s.get("payment_receipt_received"))
    order_status       = str(s.get("order_status") or "").lower()
    active_statuses    = {
        "awaiting_receipt", "under_review",
        "processing", "payment_pending",
    }
    has_active_context = bool(
        selected_product
        or awaiting_receipt
        or receipt_received
        or order_status in active_statuses
    )
    if not has_active_context:
        # No order in flight → let the brain handle it; the customer
        # might be asking a question that just happens to share
        # vocabulary with payment confirmations.
        logger.info(
            "[PAYMENT_INTENT] skip — no active order context "
            "tenant=%s phone=*%s text=%r",
            tenant_id, (phone or "")[-4:], (inbound_text or "")[:40],
        )
        return None

    reply_text = compose_payment_claim_ack(
        selected_product=selected_product,
        awaiting_receipt=awaiting_receipt,
        receipt_received=receipt_received,
    )
    state_patch: Dict[str, Any] = {
        "payment_claim_at": _utcnow_iso(),
    }
    # Only flip ``awaiting_payment_receipt`` ON if it wasn't already
    # set AND no receipt has been received yet. If a receipt is
    # already in, we don't want to undo the "review" state.
    if not awaiting_receipt and not receipt_received:
        state_patch["awaiting_payment_receipt"] = True
        state_patch["order_status"]             = "awaiting_receipt"
    logger.info(
        "[PAYMENT_INTENT] short_circuit=payment_claim tenant=%s phone=*%s "
        "selected_product=%r awaiting_receipt=%s receipt_received=%s "
        "order_status=%r",
        tenant_id, (phone or "")[-4:],
        selected_product, awaiting_receipt, receipt_received,
        order_status,
    )
    return {
        "reply_text":  reply_text,
        "state_patch": state_patch,
    }


def rewrite_generic_reply_for_payment_context(
    *,
    inbound_text: str,
    brain_reply: str,
    state_summary: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Post-brain safety net. If the brain insisted on shipping a
    generic fallback line ("أنا هنا — قول وش تحتاج") AND the inbound
    was clearly payment-context AND we have an active order, return
    a payment-aware replacement; otherwise return ``None`` so the
    caller keeps the brain's text.

    This is the LAST defence against the bug from the screenshot —
    even if the pre-brain short-circuit somehow misses the claim,
    we catch it here before the reply hits ``_post_wa``.
    """
    if not detect_payment_confirmation_text(inbound_text):
        return None
    if not looks_like_generic_fallback_reply(brain_reply):
        return None
    s = state_summary or {}
    selected_product = s.get("selected_product")
    awaiting_receipt = bool(s.get("awaiting_payment_receipt"))
    receipt_received = bool(s.get("payment_receipt_received"))
    has_active_context = bool(
        selected_product or awaiting_receipt or receipt_received
        or str(s.get("order_status") or "").lower() in {
            "awaiting_receipt", "under_review",
            "processing", "payment_pending",
        }
    )
    if not has_active_context:
        return None
    return compose_payment_claim_ack(
        selected_product=selected_product,
        awaiting_receipt=awaiting_receipt,
        receipt_received=receipt_received,
    )


def _utcnow_iso() -> str:
    from datetime import datetime, timezone   # noqa: PLC0415
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "detect_payment_confirmation_text",
    "looks_like_generic_fallback_reply",
    "compose_payment_claim_ack",
    "maybe_handle_payment_claim",
    "rewrite_generic_reply_for_payment_context",
]
