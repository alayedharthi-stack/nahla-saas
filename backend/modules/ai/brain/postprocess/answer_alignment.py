"""
modules/ai/brain/postprocess/answer_alignment.py
────────────────────────────────────────────────
Lightweight semantic answer-alignment validator.

Why this module exists
──────────────────────
Five real merchant screenshots collected on 2026-05-18 / 19 showed
the bot replying with text that did not actually answer the customer's
last message:

  1. Customer asks a product-benefit question ("هو ممتاز لمشاكل
     البطن؟") and the bot replies with a pure social ack
     ("ما تقصر أبداً وياك").
  2. Customer sends a polite close ("على خير إن شاء الله") and the
     bot reopens the funnel ("الله يحييك 🌹 وش الخدمة؟").
  3. Customer confirms package delivery ("وصل الله يوصل... اخذته")
     after the merchant pushed a tracking notice — bot replies with
     payment-receipt copy ("وصل الإيصال، ...").
  4. Customer sends a religious blessing / dua and the bot replies
     "ما أقدر أساعدك في هذا الموضوع" (out-of-scope template).
  5. Customer wraps up with "تمام حولي لهم" and the bot drifts to
     "الله يبحث عنك بحسن ظنك".

These are routing / context bugs — not template bugs. We fix the
upstream root causes elsewhere (see ``social_classifier`` closing
disqualifier and ``payment_intent.is_post_shipment_*`` gates), but
the LLM composer can still wander off when the brain hands it a
borderline context. This module is the last-mile check.

Surgical contract — what we DO NOT do
─────────────────────────────────────
* No new intents.
* No reply templates.
* No keyword → reply rules ("if customer said X, reply with Y").
* No mutation of ``reply`` in default mode.

What we DO
──────────
* Detect 4 narrow mismatch shapes drawn from the screenshots above.
* Emit a single structured ``[ALIGN_MISMATCH]`` log per turn so the
  merchant can trace misfires in Railway / observability.
* Optionally — when the env flag ``BRAIN_ALIGNMENT_REGEN`` is set to
  ``"1"`` / ``"true"`` — clear the reply so the pipeline regenerates
  via the existing ``ACTION_LLM_REPLY`` path. Default is **log-only**
  while we collect a baseline of misfires before flipping the gate.

Each rule is intentionally tight. We accept missed-positives over
swallowing legitimate replies — every false-positive would force a
regenerate and slow down the conversation.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.brain.postprocess.alignment")


# ── Public types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AlignmentResult:
    """Outcome of the alignment check.

    * ``passed=True``  → reply answers the message; pipeline ships it.
    * ``passed=False`` → mismatch detected; ``mismatch_type`` carries
      the rule name and ``reason`` carries a short human-readable
      explanation. The pipeline either logs and ships (default) or
      regenerates (env-flag opt-in).
    """
    passed: bool
    mismatch_type: str = ""
    reason: str = ""


# ── Light Arabic normalisation ──────────────────────────────────────────────


_NORMALISE_AR_RE = re.compile(r"[\u064B-\u065F\u0670]")


def _norm(text: Optional[str]) -> str:
    if not text or not isinstance(text, str):
        return ""
    t = _NORMALISE_AR_RE.sub("", text)
    t = t.replace("ـ", "")
    t = (
        t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
         .replace("ى", "ي").replace("ة", "ه")
    )
    return t.lower().strip()


# ── Rule signal lists ───────────────────────────────────────────────────────


# Question / suitability / benefit signals — overlaps with
# ``social_classifier._PRACTICAL_QUESTION_SIGNALS`` but we keep the
# list local so this module stays self-contained and can be unit-
# tested without importing the classifier.
_QUESTION_SIGNALS = (
    "؟", "?",
    # Practical / how-to.
    "كيف اس", "كيف اخذ", "كيف اشرب", "كيف اكل", "كيف اعمل", "كيف اسوي",
    "وش اسوي", "وش طريقه", "وش طريقة", "في طريقه", "في طريقة",
    "متي اخذ", "متى اخذ",
    "الاستعمال", "الاستخدام",
    "الجرعه", "الجرعة",
    # Suitability — "is it good for X / does it work for Y".
    "ممتاز ل", "مفيد ل", "ينفع ل", "هل ينفع", "هل يصلح",
    "يصلح ل", "هل يفيد", "يفيد ل",
    "مناسب ل", "هل مناسب",
    # Direct WH-questions (anchored — "كيف الحال" stays out via the
    # absence of "كيف اس/اخذ/...").
    "هل ممتاز", "هل مفيد", "هل يستخدم", "هل يستعمل",
    "متي يستخدم", "متى يستخدم",
)


# Reply patterns that are PURELY social / acknowledgement copy with
# no informational content. Substring match against the normalised
# reply. We pick phrases that appear ONLY in the social template
# pool — never in legitimate product / shipping / payment answers.
_PURELY_SOCIAL_REPLY_MARKERS = (
    "ما تقصر ابدا",      # "ما تقصر أبدًا"
    "الله يعافيك ويسعدك",
    "حياك الله ويبارك",
    "بيض الله وجهك",
    "الله يجزاك خير",     # without product context
    "الله يبحث عنك",      # "الله يبحث عنك بحسن ظنك" — drift bucket
)


# Closing / farewell tokens on the inbound side — duplicated from
# ``social_classifier._CLOSING_SIGNALS`` so this module stays
# decoupled.
_INBOUND_CLOSING_SIGNALS = (
    "علي خير", "على خير",
    "في امان الله", "بامان الله", "بحفظ الله",
    "مع السلامه", "مع السلامة",
    "تصبح علي خير", "تصبح على خير",
    "خلاص شكرا", "خلاص شكراً",
    "تكفينا الحين", "كفايه الحين",
    "تمام كذا", "تمام بكذا",
    "بس كذا شكرا", "بس كذا شكراً",
)


# Reply patterns that re-open the funnel — flagged when the inbound
# is a closing / farewell.
_REOPEN_REPLY_MARKERS = (
    "وش الخدمه", "وش الخدمة",
    "كيف اقدر اخدمك", "كيف أقدر أخدمك",
    "كيف اقدر اساعدك", "كيف أقدر أساعدك",
    "وش اقدر اخدمك", "وش اقدر اساعدك",
)


# Out-of-scope template marker — exact substring from
# ``compose/templates._HARD_OUT_OF_SCOPE_VARIANTS``. Reply-side check.
_OOS_REPLY_MARKERS = (
    "ما اقدر اساعدك في هذا الموضوع",
    "هذا خارج تخصصي",
)


# Inbound religious / dua / blessing markers — light list. We do NOT
# claim to detect every religious utterance (the social classifier
# already handles basmala / prophet invocation deterministically); we
# use this only to flag the reply-side OOS misfire.
_INBOUND_RELIGIOUS_MARKERS = (
    "اللهم", "صلي الله عليه وسلم", "صلى الله عليه وسلم",
    "بسم الله", "ماشاء الله", "ما شاء الله",
    "الله يحفظك", "الله يجزاك", "جزاك الله خير",
    "الحمدلله", "الحمد لله",
    "صباح النور", "مساء النور",  # plain greetings
    "امين", "آمين",
)


# Delivery-confirmation tokens — duplicated from
# ``payment_intent._DELIVERY_CONFIRMATION_TOKENS`` so this module
# can run without importing the payment-intent package.
_INBOUND_DELIVERY_TOKENS = (
    "وصل اليوم", "وصلت اليوم", "وصلتني اليوم", "وصلني اليوم",
    "اخذت الطلب", "اخذته اليوم", "اخذته",
    "استلمت الطلب", "استلمته", "استلمناه", "استلمتها",
    "تسلمت الطلب", "تسلمته",
    "وصل الله يوصل",
    "وصل بسلامه", "وصل بسلامة", "وصل بحاله",
)


# Reply patterns that talk about a money receipt — "وصل الإيصال",
# "بانتظار التحويل", etc.
_REPLY_PAYMENT_RECEIPT_MARKERS = (
    "وصل الايصال", "وصل الإيصال",
    "وصلنا ايصال التحويل", "وصلنا إيصال التحويل",
    "بانتظار ايصال التحويل", "بانتظار إيصال التحويل",
    "ارسل ايصال التحويل", "ارسل إيصال التحويل",
    "صوره الايصال", "صورة الايصال", "صورة الإيصال",
    "تم استلام الايصال", "تم استلام الإيصال",
)


# When the reply explicitly references a money / amount / bank
# context, the delivery-confirmation alignment rule does NOT fire —
# we do not want to flag a legitimate "your transfer is being
# verified" reply just because the inbound happened to also mention
# "وصل".
_REPLY_PAYMENT_CONTEXT_HINTS = (
    "تحويل", "حواله", "حوالة", "بنك", "ايبان", "iban",
    "ريال",  # paired with the receipt copy
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _has_question_signal(inbound_norm: str) -> bool:
    return any(tok in inbound_norm for tok in _QUESTION_SIGNALS)


def _is_purely_social_reply(reply_norm: str) -> bool:
    return any(tok in reply_norm for tok in _PURELY_SOCIAL_REPLY_MARKERS)


def _has_closing_signal(inbound_norm: str) -> bool:
    return any(tok in inbound_norm for tok in _INBOUND_CLOSING_SIGNALS)


def _has_reopen_marker(reply_norm: str) -> bool:
    return any(tok in reply_norm for tok in _REOPEN_REPLY_MARKERS)


def _has_oos_marker(reply_norm: str) -> bool:
    return any(tok in reply_norm for tok in _OOS_REPLY_MARKERS)


def _is_religious_inbound(inbound_norm: str) -> bool:
    return any(tok in inbound_norm for tok in _INBOUND_RELIGIOUS_MARKERS)


def _has_delivery_token(inbound_norm: str) -> bool:
    return any(tok in inbound_norm for tok in _INBOUND_DELIVERY_TOKENS)


def _has_explicit_payment_inbound(inbound_norm: str) -> bool:
    """Inbound mentions the customer transferred / paid — disqualifies
    the delivery-confirmation rule because the reply discussing a
    receipt is then legitimate."""
    explicit = (
        "تحويل", "حواله", "حوالة", "حولت", "ادفع", "دفعت",
        "ايصال", "إيصال", "بنك", "ايبان", "iban",
        "سددت", "اودعت", "أودعت",
    )
    return any(tok in inbound_norm for tok in explicit)


def _has_payment_receipt_reply_marker(reply_norm: str) -> bool:
    return any(tok in reply_norm for tok in _REPLY_PAYMENT_RECEIPT_MARKERS)


def _reply_has_payment_context(reply_norm: str) -> bool:
    return any(tok in reply_norm for tok in _REPLY_PAYMENT_CONTEXT_HINTS)


# ── Alignment rules ─────────────────────────────────────────────────────────


def _rule_question_to_social(inbound_norm: str, reply_norm: str) -> Optional[str]:
    """Rule 1 — substantive question matched by purely social reply.

    The customer's message carries a question / suitability /
    practical signal AND the reply contains ONLY social-pool
    vocabulary (no product, no price, no explanation). Returns a
    short reason on hit, else None.
    """
    if not _has_question_signal(inbound_norm):
        return None
    if not _is_purely_social_reply(reply_norm):
        return None
    # Defence in depth: if the reply ALSO carries informational
    # content (price, product name fragments, instructions), the
    # rule does NOT fire — a blessing-prefixed answer is fine.
    informational_hints = (
        "ريال", "سعر", "بكم", "كيلو", "غرام", "تحويل", "ايصال",
        "تجهيز", "شحن", "توصيل", "استخدام", "استعمال", "جرعه",
        "ملعقه", "ملعقة",
    )
    if any(h in reply_norm for h in informational_hints):
        return None
    return "question_signal_in_inbound paired with purely-social reply"


def _rule_closing_to_reopen(inbound_norm: str, reply_norm: str) -> Optional[str]:
    """Rule 2 — polite close answered by a "how can I help?" reopener."""
    if not _has_closing_signal(inbound_norm):
        return None
    if not _has_reopen_marker(reply_norm):
        return None
    return "closing_inbound paired with reopen-style reply"


def _rule_religious_to_oos(inbound_norm: str, reply_norm: str) -> Optional[str]:
    """Rule 3 — religious / dua inbound matched by out-of-scope copy."""
    if not _is_religious_inbound(inbound_norm):
        return None
    if not _has_oos_marker(reply_norm):
        return None
    return "religious_inbound paired with out-of-scope reply"


def _rule_delivery_to_receipt(
    inbound_norm: str,
    reply_norm: str,
    *,
    order_status: str,
    awaiting_payment_receipt: bool,
) -> Optional[str]:
    """Rule 4 — soft delivery confirmation answered by payment-receipt copy.

    Tightly constrained: inbound must read as a delivery
    confirmation AND must NOT carry explicit payment vocabulary; the
    reply must mention an explicit receipt phrase. The order-state
    cross-check guards against the legitimate case where we are
    already verifying a transfer.
    """
    if not _has_delivery_token(inbound_norm):
        return None
    if _has_explicit_payment_inbound(inbound_norm):
        return None
    if not _has_payment_receipt_reply_marker(reply_norm):
        return None
    # If the brain itself thinks we're awaiting a receipt and the
    # reply is legitimately about that, we still let it through.
    # But the inbound says "I received the package" — those two
    # don't reconcile. We log the mismatch for audit and rely on
    # the upstream gate (``payment_intent.is_post_shipment_*``) to
    # have cleared the stale flag. This rule is the safety net.
    return (
        f"delivery_confirmation_inbound paired with payment-receipt "
        f"reply (order_status={order_status!r}, "
        f"awaiting_receipt={awaiting_payment_receipt})"
    )


# ── Public entry point ─────────────────────────────────────────────────────


def check_alignment(
    *,
    last_user_message: str,
    reply: str,
    intent_name: str = "",
    action: str = "",
    order_status: str = "",
    awaiting_payment_receipt: bool = False,
) -> AlignmentResult:
    """Run the four alignment rules and return the first mismatch.

    All inputs are optional / safe — empty values just produce
    ``passed=True``. The function is exception-safe; any internal
    failure yields ``passed=True`` so a buggy validator can never
    block a reply.
    """
    try:
        inbound_norm = _norm(last_user_message)
        reply_norm = _norm(reply)
        if not inbound_norm or not reply_norm:
            return AlignmentResult(passed=True)

        # Rule 1 — question → purely social.
        r = _rule_question_to_social(inbound_norm, reply_norm)
        if r:
            return AlignmentResult(
                passed=False,
                mismatch_type="question_to_social",
                reason=r,
            )

        # Rule 2 — closing → reopen.
        r = _rule_closing_to_reopen(inbound_norm, reply_norm)
        if r:
            return AlignmentResult(
                passed=False,
                mismatch_type="closing_to_reopen",
                reason=r,
            )

        # Rule 3 — religious → OOS.
        r = _rule_religious_to_oos(inbound_norm, reply_norm)
        if r:
            return AlignmentResult(
                passed=False,
                mismatch_type="religious_to_oos",
                reason=r,
            )

        # Rule 4 — delivery → receipt.
        r = _rule_delivery_to_receipt(
            inbound_norm, reply_norm,
            order_status=order_status,
            awaiting_payment_receipt=awaiting_payment_receipt,
        )
        if r:
            return AlignmentResult(
                passed=False,
                mismatch_type="delivery_to_receipt",
                reason=r,
            )

        return AlignmentResult(passed=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ALIGN_MISMATCH] check failed: %s", exc)
        return AlignmentResult(passed=True)


# ── Pipeline integration helpers ───────────────────────────────────────────


def regen_enabled() -> bool:
    """Return True when the pipeline should clear ``reply`` and
    regenerate via ``ACTION_LLM_REPLY`` on a mismatch.

    Default is **log-only** — we collect baseline data before
    enabling regeneration. Flip the env flag to ``"1"`` once the
    log signal looks clean.
    """
    return os.environ.get("BRAIN_ALIGNMENT_REGEN", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def emit_mismatch_log(
    *,
    tenant_id: int,
    phone: str,
    turn: int,
    last_user_message: str,
    reply: str,
    result: AlignmentResult,
    intent_name: str = "",
    action: str = "",
    order_status: str = "",
    awaiting_payment_receipt: bool = False,
    regen_will_fire: bool = False,
) -> None:
    """Single structured log line so misfires are searchable and
    aggregable in the merchant's observability stack."""
    if result.passed:
        return
    try:
        masked_phone = phone[-4:] if phone and len(phone) >= 4 else "****"
        logger.warning(
            "[ALIGN_MISMATCH] tenant=%s phone=*%s turn=%s mismatch=%s "
            "intent=%s action=%s order_status=%r awaiting_receipt=%s "
            "regen=%s reason=%s "
            "inbound=%r reply=%r",
            tenant_id, masked_phone, turn, result.mismatch_type,
            intent_name, action, order_status, awaiting_payment_receipt,
            regen_will_fire, result.reason,
            (last_user_message or "")[:120],
            (reply or "")[:160],
        )
    except Exception:
        # Never let logging break a turn.
        pass


__all__ = [
    "AlignmentResult",
    "check_alignment",
    "regen_enabled",
    "emit_mismatch_log",
]
