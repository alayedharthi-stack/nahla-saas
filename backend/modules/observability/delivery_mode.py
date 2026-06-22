"""
modules/observability/delivery_mode.py
──────────────────────────────────────
Classify the result of one WhatsApp outbound turn into a small
closed enum so the dashboard, the alert system, and human
operators can answer one question quickly:

  "Did the customer actually receive useful content?"

Background — the production regression this module catches
─────────────────────────────────────────────────────────
A merchant tested the catalog wire-up by sending
``"أبغى أشوف صورة لعسل السمر"`` and received a one-line
acknowledgement (``"أبشر خالد 🍯"``) with no catalog card, no
image, no link, and no fallback. Each downstream layer logged
"success" — the text reply landed at the provider, no exception
was raised — so the operator only learned about the regression
from the merchant. Nothing in our logs flagged that the customer
asked for an IMAGE and got TEXT.

This module gives every turn a structured verdict at the end of
the dispatch loop:

  * ``catalog``     — at least one Meta WhatsApp Catalog card sent.
  * ``image_cta``   — at least one product / library image sent
                      AND a CTA URL button. The legacy "rich"
                      experience.
  * ``media_only``  — image sent but no clickable CTA. Rare, e.g.
                      a barcode or certificate with no follow-up.
  * ``cta_only``    — clickable URL sent but no image. Common for
                      KB-style replies that link to a help page.
  * ``text_only``   — only the plain reply body. For most intents
                      this is FINE; for product / image / catalog
                      intents this is the alarm condition.
  * ``failed``      — even the initial text send failed at the
                      provider. The customer received nothing.

Design notes
────────────
* Pure functions. No DB, no HTTP, no I/O. Every helper is
  deterministic given its inputs, so a hundred unit tests cost
  milliseconds and behaviour is auditable line by line.
* The audit dict is a small fixed-shape ``TypedDict``-style record
  ; callers stamp it as they go, then call
  :func:`compute_final_delivery_mode` once at the end. No mutation
  inside the helper.
* The intent classifier is intentionally CONSERVATIVE. False
  positives (alerting on a turn where the customer didn't really
  ask for an image) would create alarm fatigue and erode trust
  in the signal. We match only high-confidence Arabic phrasings
  and the brain's explicit product-discovery actions.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Mode constants — closed enum
# ─────────────────────────────────────────────────────────────────────────────

DELIVERY_MODE_CATALOG      = "catalog"
DELIVERY_MODE_IMAGE_CTA    = "image_cta"
DELIVERY_MODE_MEDIA_ONLY   = "media_only"
DELIVERY_MODE_CTA_ONLY     = "cta_only"
DELIVERY_MODE_TEXT_ONLY    = "text_only"
DELIVERY_MODE_FAILED       = "failed"

# Modes that satisfy a product / image / catalog intent. Anything
# OUTSIDE this set when the customer asked for product content is
# the alarm condition the [DELIVERY_GUARD_FAIL] log fires on.
#
# May 2026 #10 — ``cta_only`` is now treated as acceptable. The
# customer asked to see a product; if catalog AND legacy image both
# fell through but a CTA-URL with the buy-page link did land, the
# customer can still tap through and SEE the product on the store.
# Operators previously got fatigued by the guard firing on this
# perfectly-recoverable mode, so we kept it in the alarm set and
# silenced it manually. With the explicit hard-recovery (see
# whatsapp_webhook.py ``[VISUAL_FALLBACK_RECOVERED]``) the rescue
# path lands here on purpose. The guard now ONLY fires for
# ``text_only`` and ``failed`` — the actual UX regression cases.
_PRODUCT_INTENT_OK_MODES = frozenset({
    DELIVERY_MODE_CATALOG,
    DELIVERY_MODE_IMAGE_CTA,
    DELIVERY_MODE_MEDIA_ONLY,
    DELIVERY_MODE_CTA_ONLY,
})


# ─────────────────────────────────────────────────────────────────────────────
# Audit dataclass — stamped by the webhook as it dispatches
# ─────────────────────────────────────────────────────────────────────────────

DeliveryAudit = Dict[str, Any]
"""Shape contract (every key is required; counts default to 0):

    {
      "first_send_failed":         bool,   # initial reply send failed
      "text_sent":                 bool,   # plain text body sent
      "interactive_buttons_sent":  bool,   # initial reply used buttons
      "cta_url_sent_count":        int,    # cta_url interactive sends
      "catalog_card_sent_count":   int,    # successful catalog sends
      "legacy_media_sent_count":   int,    # legacy image/file/video sends
      "contacts_sent":             bool,   # contact-card message sent
    }
"""


def new_delivery_audit() -> DeliveryAudit:
    """Return a fresh zero-valued audit dict.

    Callers should treat the returned dict as the authoritative
    shape; missing keys cause :func:`compute_final_delivery_mode`
    to fall through to ``"failed"`` (deliberately conservative).
    """
    return {
        "first_send_failed":         False,
        "text_sent":                 False,
        "interactive_buttons_sent":  False,
        "cta_url_sent_count":        0,
        "catalog_card_sent_count":   0,
        "legacy_media_sent_count":   0,
        "contacts_sent":             False,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Mode computation — pure function on the audit dict
# ─────────────────────────────────────────────────────────────────────────────

def compute_final_delivery_mode(audit: DeliveryAudit) -> str:
    """Return one of the ``DELIVERY_MODE_*`` constants.

    Precedence (high → low):

      1. ``failed``      — initial send failed.
      2. ``catalog``     — any catalog card landed.
      3. ``image_cta``   — image AND at least one CTA URL.
      4. ``media_only``  — image but no CTA URL.
      5. ``cta_only``    — CTA URL but no image.
      6. ``text_only``   — plain text or buttons-only reply.
      7. ``failed``      — nothing in audit at all (e.g. empty dict).

    The function never raises. Missing keys are treated as their
    zero value so a partial audit still classifies safely (worst
    case → ``"failed"``).
    """
    if not isinstance(audit, dict):
        return DELIVERY_MODE_FAILED

    if audit.get("first_send_failed"):
        return DELIVERY_MODE_FAILED

    if int(audit.get("catalog_card_sent_count", 0) or 0) > 0:
        return DELIVERY_MODE_CATALOG

    has_media = int(audit.get("legacy_media_sent_count", 0) or 0) > 0
    has_cta_url = int(audit.get("cta_url_sent_count", 0) or 0) > 0

    if has_media and has_cta_url:
        return DELIVERY_MODE_IMAGE_CTA
    if has_media:
        return DELIVERY_MODE_MEDIA_ONLY
    if has_cta_url:
        return DELIVERY_MODE_CTA_ONLY

    if (
        audit.get("text_sent")
        or audit.get("interactive_buttons_sent")
    ):
        return DELIVERY_MODE_TEXT_ONLY

    # Nothing was successfully sent — treat as failure even though
    # ``first_send_failed`` wasn't stamped. Defensive: a caller that
    # forgets to set the flag still gets the right verdict.
    return DELIVERY_MODE_FAILED


# ─────────────────────────────────────────────────────────────────────────────
# Intent classifier — was this turn about a product / image?
# ─────────────────────────────────────────────────────────────────────────────

# Brain-decided actions that imply the customer asked for product
# content. These are the action constants the DecisionEngine emits.
# Keep this set tight: only actions whose successful outcome
# REQUIRES non-text content.
_PRODUCT_BRAIN_ACTIONS = frozenset({
    "search_products",
    "recommend_addon",
    "propose_draft_order",
})

# High-confidence Arabic phrasings the customer uses to ask for a
# product / image / catalog directly. Conservative — short phrases
# that ALSO appear in unrelated chitchat (e.g. "أبي" alone) are
# excluded. Each entry must be a literal substring with no
# punctuation; we lowercase the inbound (no effect on Arabic) and
# normalise whitespace before checking.
_PRODUCT_INBOUND_KEYWORDS = (
    # "I want to see / show me" — verbs.
    "أبي أشوف",
    "أبغى أشوف",
    "ودي أشوف",
    "خلني أشوف",
    "وريني",
    "ورني",
    "أرني",
    "ارني",
    "اعرض علي",
    "اعرضي علي",
    # Explicit image / catalog requests.
    "ابعث صورة",
    "ارسل صورة",
    "أرسل صورة",
    "صورة المنتج",
    "صور المنتج",
    "صور المنتجات",
    "كتالوج",
    "كتالوجك",
    "كتالوج المتجر",
    # Bare product / image nouns alongside a request verb. We
    # require the noun to appear because bare "صورة" can occur in
    # many unrelated contexts.
    "أبي منتج",
    "أبغى منتج",
    "ودي منتج",
    "أبي صورة",
    "أبغى صورة",
    "ودي صورة",
    # Deictic / follow-up visual asks (May 2026).
    "الصورة وينها",
    "الصورة فين",
    "صور العسل",
    "صورة الطلح",
    "صور الطلح",
    "ابي اشوف المنتج",
    "أبي أشوف المنتج",
    "ابي اشوف صور",
    "أبي أشوف صور",
    # ── May 2026 #6 — visual-product enforcement keyword pack ────────
    # Production gap: customers say "عندك صورة للضهيان؟" / "ورني
    # السمر" / "أرسل رابط السمر" / "أبي الكتالوج" and the previous
    # keyword set missed them, so the FINAL_DELIVERY guard never
    # tripped and the text-only reply slipped through. Each new
    # entry is a HIGH-confidence Arabic phrasing — we deliberately
    # avoid bare "صورة" / "رابط" / "شكل" because they appear in
    # neutral chitchat ("صورة العقد" / "رابط الموقع" / "شكلك
    # تعبان") that we must not flag.
    # "show me / see / display" — verb-anchored variants.
    "اعرض لي",
    "اعرضي لي",
    "اعرضو",
    "عرض المنتج",
    "عرض المنتجات",
    # "what does it look like" — noun-anchored shape questions.
    "شكل المنتج",
    "شكله ايش",
    "شكلها ايش",
    "كيف شكله",
    "كيف شكلها",
    # "do you have a picture of …?" — possessive image asks.
    "عندك صورة",
    "عندكم صورة",
    "فيه صورة",
    "صورة ل",
    "صورة لـ",
    "صورة عن",
    # Explicit product-link asks (resend the buy URL or a fresh one).
    "ارسل رابط",
    "أرسل رابط",
    "ابعث رابط",
    "ابعث لي رابط",
    "ابعثلي رابط",
    "ارسل الرابط",
    "أرسل الرابط",
    "ابعث الرابط",
    "ابعث لي الرابط",
    "ابعثلي الرابط",
    "ودي رابط",
    "أبي رابط",
    "أبغى رابط",
    "رابط المنتج",
    "رابط للمنتج",
)


_NEGATIVE_NON_PRODUCT_PHRASES = (
    # May 2026 #6 — anchored non-product nouns that the expanded
    # keyword pack ("أرسل رابط …" / "صورة …") would otherwise drag
    # into the visual-product bucket and trigger a false enforcement
    # alarm. Each phrase is a HIGH-confidence "this is NOT about a
    # catalog product" signal:
    #
    #   * Generic web / contact links — store website, IG handle,
    #     WhatsApp link, contact-us link, login link, etc.
    #   * Document-style images — contract, invoice, receipt, ID,
    #     bank-account screenshot. These are usually customer-side
    #     uploads, not catalog asks; even when the customer ASKS
    #     for one (e.g. the bank-transfer barcode), the [MEDIA_KEY:]
    #     marker path handles it and the visual enforcer must not
    #     also attach a product card.
    "رابط الموقع",
    "رابط موقع",
    "رابط الانستا",
    "رابط الإنستا",
    "رابط الانستجرام",
    "رابط الإنستجرام",
    "رابط انستجرام",
    "رابط الواتس",
    "رابط واتس",
    "رابط الواتساب",
    "رابط الفيس",
    "رابط فيسبوك",
    "رابط الفيسبوك",
    "رابط التواصل",
    "رابط تواصل",
    "رابط الدخول",
    "رابط دخول",
    "رابط الدفع",                 # handled by ACTION_SEND_PAYMENT_LINK
    "رابط الفاتورة",
    "صورة العقد",
    "صورة الايصال",
    "صورة الإيصال",
    "صورة الفاتورة",
    "صورة البطاقة",
    "صورة الهوية",
    "صورة الحوالة",
    "صورة التحويل",
    "صورة الايبان",
    "صورة الآيبان",
    "صورة البروفايل",
    "صورة الشخصية",
)


def customer_wants_product_or_image(
    *,
    inbound_text: str,
    brain_action: str = "",
) -> bool:
    """Return ``True`` when the turn looks like a product / image
    / catalog request.

    The classifier OR-combines two signals:

      * The brain's explicit action choice (the strongest signal —
        the decision engine already ran the full intent pipeline).
      * Inbound-text keyword match against a closed set of
        high-confidence Arabic phrasings, gated by a negative
        filter for non-product nouns (web links, documents, IDs).

    Either signal alone is enough — except when the inbound text
    explicitly names a non-product noun, in which case the keyword
    path is suppressed to avoid false positives that would attach
    a random catalog card to a "أرسل رابط الموقع" / "صورة العقد"
    turn. Brain-action precedence is preserved: a decision-engine
    ``search_products`` always wins.

    Pure, fast, and case-insensitive. It never raises and treats
    ``None`` inputs as empty strings.
    """
    if (brain_action or "").strip() in _PRODUCT_BRAIN_ACTIONS:
        return True

    raw = inbound_text or ""
    if not raw:
        return False
    norm = " ".join(raw.split()).lower()

    if any(neg in norm for neg in _NEGATIVE_NON_PRODUCT_PHRASES):
        return False

    try:
        from modules.ai.brain.commerce.product_visual import (  # noqa: PLC0415
            is_product_visual_request,
        )
        if is_product_visual_request(raw):
            return True
    except Exception:  # noqa: BLE001
        pass

    return any(kw in norm for kw in _PRODUCT_INBOUND_KEYWORDS)


# ─────────────────────────────────────────────────────────────────────────────
# Guard — was this an acceptable mode for the inferred intent?
# ─────────────────────────────────────────────────────────────────────────────

def is_acceptable_mode_for_product_intent(
    mode: str,
    *,
    audit: Optional[DeliveryAudit] = None,
    brain_action: str = "",
) -> bool:
    """``True`` when *mode* satisfies a product / image request.

    May 2026 #10 — ``catalog`` / ``image_cta`` / ``media_only`` /
    ``cta_only`` are all acceptable. The first three deliver actual
    product content; ``cta_only`` gives the customer a clickable
    buy-page link they can open in WhatsApp's in-app browser, which
    is the explicit fallback contract from the visual-product
    enforcement layer. ``text_only`` and ``failed`` are the only
    modes that flip the [DELIVERY_GUARD_FAIL] alarm.

    Browse product lists: interactive reply buttons listing selectable
    SKUs are rich enough when ``brain_action`` is a product-discovery
    action (``search_products``, etc.).
    """
    if mode in _PRODUCT_INTENT_OK_MODES:
        return True
    action = (brain_action or "").strip()
    if (
        mode == DELIVERY_MODE_TEXT_ONLY
        and isinstance(audit, dict)
        and audit.get("interactive_buttons_sent")
        and action in _PRODUCT_BRAIN_ACTIONS
    ):
        return True
    return False


__all__ = [
    "DELIVERY_MODE_CATALOG",
    "DELIVERY_MODE_CTA_ONLY",
    "DELIVERY_MODE_FAILED",
    "DELIVERY_MODE_IMAGE_CTA",
    "DELIVERY_MODE_MEDIA_ONLY",
    "DELIVERY_MODE_TEXT_ONLY",
    "DeliveryAudit",
    "compute_final_delivery_mode",
    "customer_wants_product_or_image",
    "is_acceptable_mode_for_product_intent",
    "new_delivery_audit",
]
