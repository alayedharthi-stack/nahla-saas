"""
core/payment_intent.py
──────────────────────
Detect text-only "I just paid / I transferred" claims and surface
a payment-understanding signal so the brain composes its own reply.

Tenant 33 #48 (May 2026) policy update
─────────────────────────────────────
Previous behaviour: this module would short-circuit the brain with
a hardcoded "وصل، يعطيك العافية" ACK whenever the customer typed
"حولت" or "تم التحويل", and would flip
``awaiting_payment_receipt=True`` + ``order_status='awaiting_receipt'``
in state. The merchant directive now is unambiguous:

    "أصلحوا الفهم والقرار، وليس الكلمات."

We are explicitly told NOT to:
    * write hardcoded ACK lines for the AI to repeat,
    * force any specific wording on the brain,
    * impose tenant-specific copy.

When ``PAYMENT_TEXT_CLAIM_BRAIN_DRIVEN_ENABLED`` is True (the new
default), text-only payment claims:
    1. set ``payment_claim_unverified=True`` and
       ``payment_claim_at`` in brain state (these are *understanding
       hints* the brain prompt overlay can use, NOT outbound copy),
    2. do NOT flip ``awaiting_payment_receipt`` or ``order_status``
       (no false implication that anything was received),
    3. let the brain run normally — it composes the reply itself.

The legacy hardcoded-ACK path is preserved behind the feature flag
for any operator who needs a temporary rollback.

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
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.payment_intent")


# ── Feature flag (Tenant 33 #48, May 2026) ──────────────────────────
# When True (the new default), text-only payment claims are NOT
# answered with the hardcoded ``_ACK_FIRST_CLAIM`` copy. The brain
# handles the reply naturally, with a ``payment_claim_unverified``
# understanding flag stamped on brain state so the prompt overlay
# knows the situation. Set the env var to "0"/"false" to roll back
# to the legacy hardcoded-ACK behaviour.
def _payment_text_claim_brain_driven_enabled() -> bool:
    """Read the feature flag at call time (so tests can monkey-patch
    the env var without re-importing the module). Defaults to True."""
    raw = os.environ.get("PAYMENT_TEXT_CLAIM_BRAIN_DRIVEN_ENABLED", "1")
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


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
        # ── Override phrases (May 2026 hotfix) ──────────────────────
        # Customer is correcting the bot after we mis-classified
        # a real receipt PDF as a "pre-transfer review screen".
        # Real production phrasings:
        #   "لا هذا ايصال مدفوع" / "هذا ايصال مدفوع" / "ايصال مدفوع"
        #   "هذا الايصال النهائي" / "هذا الايصال الاصلي"
        "ايصال مدفوع",
        "إيصال مدفوع",
        "هذا ايصال مدفوع",
        "هذا إيصال مدفوع",
        "لا هذا ايصال",
        "لا هذا إيصال",
        "لا هذا الايصال",
        "لا هذا الإيصال",
        "هذا الايصال النهائي",
        "هذا الإيصال النهائي",
        "هذا الايصال الاصلي",
        "هذا الإيصال الأصلي",
        "هذي مدفوعة",
        "هذي مدفوعه",
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


# ── Post-shipment delivery-confirmation gate (May 2026 #12) ─────────
# Real merchant screenshot: customer sent
#     "وصل الله يوصل في عمرك بوهشام اليوم اخذته"
# RIGHT after the merchant pushed a tracking-link / shipment update,
# and the bot replied with payment-receipt copy ("وصل الإيصال،
# وسيتم تجهيز الشحن…"). The customer was confirming the package
# arrived — not claiming a transfer.
#
# We solve this without new intents or templates: when the recent
# outbound history shows a shipment notice was already sent AND the
# inbound reads as a soft delivery confirmation (no explicit transfer
# / receipt / amount tokens), we suppress the payment-claim short
# circuit + the awaiting-receipt dedup re-prompt. The brain LLM then
# composes a natural "wishing you good health, glad it arrived"-style
# reply on its own.
#
# Soft-delivery tokens — "I got it", "it arrived", "I took it",
# "received today". Stay narrow: never include explicit
# transfer / payment / receipt language so a real "وصل التحويل"
# never gets disqualified.
_DELIVERY_CONFIRMATION_TOKENS = (
    "وصل اليوم", "وصلت اليوم", "وصلتني اليوم", "وصلني اليوم",
    "اخذت الطلب", "اخذته اليوم", "اخذته",
    "استلمت الطلب", "استلمته", "استلمناه", "استلمتها",
    "تسلمت الطلب", "تسلمته",
    "وصل الله يوصل",  # culturally-specific delivery blessing
    "وصل بوقته", "وصل بحاله", "وصل بسلامه", "وصل بسلامة",
    "وصل قبل شويه", "وصل قبل شوي", "وصل قبل قليل",
    "وصلني قبل قليل",
    "بوصل اليوم", "وصلتني الشحنه", "وصلتني الشحنة",
    "وصل البكج",
)

# When ANY of these appear, treat the message as explicitly
# transfer-related and DO NOT apply the delivery gate. Listed in
# normalised form (post ``_normalise_arabic``).
_EXPLICIT_PAYMENT_TOKENS = (
    "تحويل",   # تحويل / التحويل / تحويلك / حوالة-related
    "حواله", "حوالة",
    "ايصال", "إيصال",
    "دفعت", "ادفع", "السداد", "سددت",
    "بنك", "البنك", "حسابك",
    "ايبان", "iban",
    "مبلغ", "المبلغ",
    "ريال",  # often paired with a price in transfer claims
)


def looks_like_delivery_confirmation(text: Optional[str]) -> bool:
    """Return True when the inbound reads as a soft delivery / package
    arrival confirmation that is *not* a payment claim.

    Very conservative: any explicit transfer / receipt / bank /
    amount token disqualifies. The caller is expected to also check
    the recent outbound history for a shipment notification — both
    sides must align before we trust this signal.
    """
    if not text or not isinstance(text, str):
        return False
    raw = text.strip()
    if not raw or len(raw) > 240:
        return False
    norm = _normalise_arabic(raw)
    if not norm:
        return False
    # Must NOT carry any explicit payment vocabulary — prevents
    # hijacking real receipt claims like "وصل التحويل" / "ايصال
    # مدفوع" that share the "وصل" prefix.
    for tok in _EXPLICIT_PAYMENT_TOKENS:
        if tok in norm:
            return False
    for tok in _DELIVERY_CONFIRMATION_TOKENS:
        if tok in norm:
            return True
    return False


# Outbound text markers that prove we already pushed a shipment /
# tracking notice on this conversation. Drawn from the actual
# template / automation copy in the production code:
#   * services/whatsapp_templates/nahla_templates.py shipping_update,
#     order_out_for_delivery
#   * services/store_sync.py order_shipped automation
# Plus a few generic Arabic markers a merchant might type manually.
_SHIPMENT_OUTBOUND_MARKERS = (
    "تم شحن", "تم شحنه", "تم الشحن",
    "في طريقه إليك", "في طريقه اليك", "في طريقها إليك",
    "خارج للتوصيل", "تم التسليم",
    "متابعة حالة الشحن", "تتبع الشحن", "تتبع الشحنه", "تتبع الشحنة",
    "رقم التتبع", "رابط التتبع",
    "تجهيز الشحن", "جاهز للشحن", "تم التجهيز",
    # English equivalents merchants paste in (Aramex / SMSA tracking pages)
    "tracking", "out for delivery", "shipped", "delivered",
)


def _outbound_carries_shipment_marker(body: Any) -> bool:
    """Return True when an outbound message body contains any of the
    documented shipment / tracking markers. Pure helper so tests can
    exercise the marker list without spinning up a DB session."""
    if not body:
        return False
    body_str = body if isinstance(body, str) else str(body)
    for marker in _SHIPMENT_OUTBOUND_MARKERS:
        if marker in body_str:
            return True
    return False


def is_post_shipment_context(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    lookback: int = 12,
) -> bool:
    """Scan the last ``lookback`` outbound message events for shipment
    / tracking markers. Returns True when at least one matches.

    Read-only and exception-safe. We never raise into the caller —
    the gate degrades to "no shipment context detected" on any DB or
    import failure, which is the existing pre-fix behaviour.

    Conversation lookup reuses ``order_flow._find_conversation_by_phone``
    so the customer-phone resolution logic stays in one place
    (Customer.normalized_phone / Customer.phone JOIN with the legacy
    ``extra_metadata['customer_phone']`` fallback).
    """
    try:
        from database.models import (  # noqa: PLC0415
            Conversation as _Conv,
            Customer as _Customer,
            MessageEvent as _Msg,
        )
        from core.order_flow import _find_conversation_by_phone  # noqa: PLC0415
    except Exception:
        return False
    try:
        conv = _find_conversation_by_phone(
            db,
            tenant_id=int(tenant_id),
            phones=(phone,),
            Conversation=_Conv,
            Customer=_Customer,
        )
    except Exception:
        return False
    if conv is None:
        return False
    try:
        rows = (
            db.query(_Msg.body, _Msg.direction)
              .filter(_Msg.conversation_id == conv.id)
              .filter(_Msg.tenant_id == int(tenant_id))
              .order_by(_Msg.id.desc())
              .limit(lookback * 2)  # x2 because outbound + inbound interleave
              .all()
        )
    except Exception:
        return False
    seen_outbound = 0
    for row in rows:
        try:
            body = row[0]
            direction = row[1]
        except Exception:
            continue
        d = str(direction or "").lower()
        if d not in {"out", "outbound"}:
            continue
        if _outbound_carries_shipment_marker(body):
            return True
        seen_outbound += 1
        if seen_outbound >= lookback:
            break
    return False


def is_post_shipment_delivery_confirmation(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    inbound_text: str,
) -> bool:
    """Convenience combiner: True iff the inbound is a soft delivery
    confirmation AND the recent outbound history already carried a
    shipment notice. The caller can use this to suppress payment /
    receipt deterministic short-circuits.
    """
    if not looks_like_delivery_confirmation(inbound_text):
        return False
    return is_post_shipment_context(db, tenant_id=tenant_id, phone=phone)


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

    # ── Post-shipment delivery-confirmation gate (May 2026 #12) ──────
    # A recent outbound shipment notice + a soft "اخذته / استلمته /
    # وصل اليوم" inbound means the customer is confirming PACKAGE
    # delivery, NOT a money transfer. Suppress the deterministic
    # payment ACK so the brain composes a natural reply.
    if is_post_shipment_delivery_confirmation(
        db, tenant_id=tenant_id, phone=phone, inbound_text=inbound_text,
    ):
        logger.info(
            "[PAYMENT_INTENT] skip — post-shipment delivery confirmation "
            "tenant=%s phone=*%s text=%r",
            tenant_id, (phone or "")[-4:], (inbound_text or "")[:60],
        )
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

    # ── Receipt-override promotion (May 2026 hotfix) ─────────────────
    # When the customer's previous inbound was a PDF / image that we
    # *misclassified* as pre-transfer-review (or as ambiguous
    # payment-context data) and they now correct us with "هذا ايصال
    # مدفوع" / "لا هذا ايصال" / "تم التحويل" — promote the prior
    # evidence to confirmed: apply the same state_patch
    # ``maybe_handle_receipt_inbound`` would have applied, and reply
    # with the proper receipt ACK. Without this branch the customer
    # is stuck repeating themselves while the bot keeps asking for a
    # "final receipt" we already have.
    #
    # Safe-by-construction: we only promote when a recent inbound
    # message event actually carried payment evidence — never
    # speculatively. Any DB failure here silently falls through to
    # the legacy claim-ack branch below.
    _override_promotion = _maybe_promote_prior_evidence(
        db=db, tenant_id=tenant_id, phone=phone,
        selected_summary=s,
    )
    if _override_promotion is not None:
        logger.info(
            "[PAYMENT_INTENT] short_circuit=evidence_override "
            "tenant=%s phone=*%s prior_pe_status=%s",
            tenant_id, (phone or "")[-4:],
            _override_promotion.get("_prior_pe_status"),
        )
        return _override_promotion

    # ── Brain-driven text-claim policy (May 2026 #48) ────────────────
    # No prior receipt-shaped evidence to promote, no real media this
    # turn — the customer's "حولت" / "تم التحويل" is a pure verbal
    # claim. We do NOT short-circuit the brain with a hardcoded ACK.
    # We do NOT flip ``awaiting_payment_receipt`` or ``order_status``
    # — that would falsely imply something arrived at the merchant
    # when nothing has. Instead we stamp the lightweight understanding
    # flag ``payment_claim_unverified=True`` so the brain prompt can
    # see the situation and compose its own natural reply.
    if _payment_text_claim_brain_driven_enabled():
        try:
            patch = _stamp_text_claim_unverified_state(
                db, tenant_id=tenant_id, phone=phone,
                inbound_text=inbound_text,
            )
            logger.info(
                "[PAYMENT_INTENT] brain_driven=text_claim "
                "tenant=%s phone=*%s patch_keys=%s "
                "selected_product=%r awaiting_receipt=%s "
                "receipt_received=%s order_status=%r",
                tenant_id, (phone or "")[-4:],
                sorted(list((patch or {}).keys())),
                selected_product, awaiting_receipt, receipt_received,
                order_status,
            )
        except Exception as _stamp_exc:  # noqa: BLE001
            logger.debug(
                "[PAYMENT_INTENT] text-claim stamp failed (non-fatal) "
                "tenant=%s err=%s",
                tenant_id, _stamp_exc,
            )
        # Wave 1 W1.2 — receipt-verdict telemetry for the text-claim
        # path. Observation only; default OFF; never raises.
        try:
            from core.receipt_verdict import (  # noqa: PLC0415
                compute_receipt_verdict,
                is_receipt_verdict_telemetry_enabled,
                log_receipt_verdict,
            )
            if is_receipt_verdict_telemetry_enabled():
                _rv = compute_receipt_verdict(
                    payment_understanding=None,
                    payment_evidence_status=None,
                    has_attached_media=bool(has_attached_media),
                    has_text_only_claim=True,
                )
                log_receipt_verdict(
                    tenant_id=tenant_id, phone=phone,
                    source="text_claim_brain_driven",
                    verdict=_rv,
                )
        except Exception:
            pass
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
    # Brain-driven text-claim policy (May 2026 #48): when the new
    # behaviour is on, we do not substitute the brain's wording with
    # a hardcoded payment ACK. The brain owns the wording even when
    # it shipped a generic fallback — the ``payment_claim_unverified``
    # flag in state nudges the next turn naturally.
    if _payment_text_claim_brain_driven_enabled():
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
    try:
        from core.payment_relevance_gate import (  # noqa: PLC0415
            validate_payment_workflow_resume,
        )
        _prv = validate_payment_workflow_resume(
            message=inbound_text,
            state_summary=s,
            route="payment_context_rewrite",
        )
        if not _prv.allowed:
            return None
    except Exception:  # noqa: BLE001
        pass
    return compose_payment_claim_ack(
        selected_product=selected_product,
        awaiting_receipt=awaiting_receipt,
        receipt_received=receipt_received,
    )


def _utcnow_iso() -> str:
    from datetime import datetime, timezone   # noqa: PLC0415
    return datetime.now(timezone.utc).isoformat()


def _stamp_text_claim_unverified_state(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    inbound_text: str,
) -> Dict[str, Any]:
    """Stamp ``payment_claim_unverified=True`` + timestamp into
    brain state so the next-turn brain prompt overlay can include
    a payment-understanding advisory. Pure understanding signal —
    we do NOT flip ``awaiting_payment_receipt`` or ``order_status``.

    Returns the applied patch (mostly for logging). Never raises;
    a DB failure simply skips the stamp and the brain will still
    handle the turn normally.
    """
    patch: Dict[str, Any] = {
        "payment_claim_unverified":    True,
        "payment_claim_unverified_at": _utcnow_iso(),
        "payment_claim_text_preview":  (inbound_text or "")[:120],
    }
    try:
        from core.order_flow import apply_state_patch  # noqa: PLC0415
        apply_state_patch(
            db,
            tenant_id=tenant_id,
            phone=phone,
            state_patch=patch,
        )
    except Exception:
        # Try the alternate brain-state writer if the order_flow
        # helper isn't importable (e.g. in narrow unit-test
        # contexts). Failing silently is fine — the only effect
        # is the brain doesn't see the advisory hint this turn.
        try:
            from core.order_flow import _load_brain_state  # noqa: PLC0415
            conv, bs = _load_brain_state(
                db, tenant_id=tenant_id, phone=phone,
            )
            if conv is not None and isinstance(bs, dict):
                op = bs.setdefault("order_prep", {})
                op.update(patch)
        except Exception:
            return {}
    return patch


# ── Evidence-override promotion helper ──────────────────────────────
# When a customer's text override fires ("هذا ايصال مدفوع") we want
# to retroactively confirm a recent PDF / image they sent — but only
# when that media was actually classified as payment-context but
# non-confirmed. We read the customer's last few INBOUND
# ``MessageEvent`` rows (newest first) and inspect their
# ``extra_metadata.payment_evidence_status``.
#
# Returns the same shape as ``maybe_handle_payment_claim`` —
# ``{"reply_text": str, "state_patch": dict}`` — when promotion is
# warranted, else ``None``.


# Maximum age of the prior receipt-like inbound that the override is
# allowed to retroactively confirm. 6 hours is a safe window that
# covers a customer who sent the PDF, waited for the bot's reply,
# and then corrected the verdict (real cases sit at 1–10 minutes).
_EVIDENCE_OVERRIDE_LOOKBACK_HOURS = 6
# Look back at most this many inbound messages.
_EVIDENCE_OVERRIDE_LOOKBACK_LIMIT = 6


def _maybe_promote_prior_evidence(
    *,
    db: Any,
    tenant_id: int,
    phone: str,
    selected_summary: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Scan recent inbound messages for a payment-evidence PDF/image
    that we marked non-confirmed, and (if found) return the
    confirmed-receipt patch + ACK reply the customer should have
    gotten in the first place.

    Pure read on DB; never raises. Returns ``None`` on any error so
    the caller falls back to the legacy claim-ack branch.
    """
    if db is None or not tenant_id:
        return None
    try:
        from datetime import datetime, timedelta, timezone  # noqa: PLC0415
        from models import Conversation, Customer, MessageEvent  # noqa: PLC0415
        from core.order_flow import (  # noqa: PLC0415
            _find_conversation_by_phone,
            _normalize_e164,
        )

        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=_EVIDENCE_OVERRIDE_LOOKBACK_HOURS,
        )

        # Resolve conversation_id from the customer phone. The
        # MessageEvent row stores conversation_id, not phone, so we
        # need the join via Customer.normalized_phone first.
        e164 = _normalize_e164(phone) or phone
        conv = _find_conversation_by_phone(
            db, tenant_id=int(tenant_id), phones=(e164, phone),
            Conversation=Conversation, Customer=Customer,
        )
        # In unit tests / mocked DBs we may not have a conversation
        # row but still want to scan whatever events the fake exposes.
        # Build the query conditionally on having a conversation.
        q = (
            db.query(MessageEvent)
              .filter(MessageEvent.tenant_id == tenant_id)
              .filter(MessageEvent.direction == "inbound")
              .filter(MessageEvent.created_at >= cutoff)
        )
        if conv is not None:
            q = q.filter(MessageEvent.conversation_id == conv.id)
        events = (
            q.order_by(MessageEvent.created_at.desc())
             .limit(_EVIDENCE_OVERRIDE_LOOKBACK_LIMIT)
             .all()
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[PAYMENT_INTENT] evidence-override scan failed "
            "tenant=%s phone=*%s err=%s",
            tenant_id, (phone or "")[-4:], exc,
        )
        return None

    target = None
    for ev in events or []:
        md = getattr(ev, "extra_metadata", None) or {}
        if not isinstance(md, dict):
            continue
        pe_status = md.get("payment_evidence_status") or ""
        kind = md.get("pdf_kind") or md.get("image_kind") or ""
        if pe_status in ("pre_transfer_review", "needs_confirmation"):
            target = (ev, md, pe_status, kind)
            break
        if kind in ("payment_pre_review", "payment_pending_evidence"):
            target = (ev, md, pe_status, kind)
            break
    if target is None:
        return None
    ev, md, pe_status, kind = target

    # Build a receipt-confirmed state_patch mirroring
    # ``maybe_handle_receipt_inbound`` so downstream consumers (paid
    # filter, receipt analytics) treat it identically. The previous
    # inbound's metadata is carried through under
    # ``payment_receipt_metadata`` so support can deep-link the
    # original PDF/image even though it was promoted by a text reply.
    try:
        from core.order_flow import _receipt_text_fields  # noqa: PLC0415
        receipt_text = _receipt_text_fields(md)
    except Exception:  # noqa: BLE001
        receipt_text = {}

    state_patch: Dict[str, Any] = {
        "awaiting_payment_receipt": False,
        "payment_receipt_received": True,
        "payment_receipt_at":       _utcnow_iso(),
        "order_status":             "under_review",
        "payment_receipt_metadata": {
            "kind":            kind or "payment_receipt",
            "promoted_from":   pe_status or "evidence_override",
            "promoted_at":     _utcnow_iso(),
            "wa_message_id":   md.get("wa_message_id"),
            "filename":        md.get("filename"),
            "mime_type":       md.get("mime_type"),
            "storage_url":     md.get("storage_url"),
            "storage_sha256":  md.get("storage_sha256"),
            "original_received_at": getattr(ev, "created_at", None) and
                                    ev.created_at.isoformat(),
            **receipt_text,
        },
    }

    # Reply: use the dedicated receipt-ACK composer when available
    # (mirrors ``maybe_handle_receipt_inbound``'s wording), falling
    # back to the payment-claim ACK if for some reason that helper
    # can't be imported. The customer is acknowledged with the
    # product + price + address summary just like a clean receipt.
    s = selected_summary or {}
    selected_product = s.get("selected_product")
    try:
        from core.order_flow import _compose_receipt_ack  # noqa: PLC0415
        reply_text = _compose_receipt_ack(s)
    except Exception as _ack_exc:  # noqa: BLE001
        logger.debug(
            "[PAYMENT_INTENT] receipt-ack import failed tenant=%s err=%s",
            tenant_id, _ack_exc,
        )
        reply_text = compose_payment_claim_ack(
            selected_product=selected_product,
            awaiting_receipt=bool(s.get("awaiting_payment_receipt")),
            receipt_received=True,  # we're about to flip it
        )

    return {
        "reply_text":      reply_text,
        "state_patch":     state_patch,
        # Diagnostic key consumed by the webhook logger — never sent
        # to the customer because the webhook reads only the
        # ``reply_text`` field for the outbound message body.
        "_prior_pe_status": pe_status or kind or "unknown",
    }


__all__ = [
    "detect_payment_confirmation_text",
    "looks_like_generic_fallback_reply",
    "compose_payment_claim_ack",
    "maybe_handle_payment_claim",
    "rewrite_generic_reply_for_payment_context",
]
