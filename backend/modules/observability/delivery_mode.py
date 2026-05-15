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

from typing import Any, Dict


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
_PRODUCT_INTENT_OK_MODES = frozenset({
    DELIVERY_MODE_CATALOG,
    DELIVERY_MODE_IMAGE_CTA,
    DELIVERY_MODE_MEDIA_ONLY,
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
        high-confidence Arabic phrasings.

    Either signal alone is enough. The function is pure, fast, and
    case-insensitive. It never raises and treats ``None`` inputs as
    empty strings.
    """
    if (brain_action or "").strip() in _PRODUCT_BRAIN_ACTIONS:
        return True

    raw = inbound_text or ""
    if not raw:
        return False
    # Normalise whitespace + lowercase (the latter is a no-op for
    # Arabic but cheap insurance for any Latin tail like product
    # SKUs).
    norm = " ".join(raw.split()).lower()
    return any(kw in norm for kw in _PRODUCT_INBOUND_KEYWORDS)


# ─────────────────────────────────────────────────────────────────────────────
# Guard — was this an acceptable mode for the inferred intent?
# ─────────────────────────────────────────────────────────────────────────────

def is_acceptable_mode_for_product_intent(mode: str) -> bool:
    """``True`` when *mode* satisfies a product / image request.

    We treat ``catalog`` / ``image_cta`` / ``media_only`` as
    acceptable because they all deliver actual product content.
    ``cta_only`` is intentionally EXCLUDED — a URL with no image
    looks like a generic link, not a product card; the customer
    asked to "see", so they should see something.
    """
    return mode in _PRODUCT_INTENT_OK_MODES


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
