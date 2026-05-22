"""
backend/services/payment_media_autolink.py
──────────────────────────────────────────
Infer the canonical ``AIMediaItem.media_key`` when a merchant
links a media asset into a payment-flavoured knowledge section.

Why this module exists
──────────────────────
The Smart Store Knowledge Hub (Phase 1+) lets a merchant attach
any ``AIMediaItem`` to a section via a ``MerchantKnowledgeMedia``
link. The link records the ``(section, media, role)`` triple but
does **not** touch the media's canonical ``media_key`` column.

That's a problem for the existing payment-QR runtime path:

  1. Customer says "أبي باركود الراجحي".
  2. Claude SHOULD emit ``[MEDIA_KEY:payment_rajhi_barcode]``,
     but sometimes doesn't.
  3. :func:`modules.ai.postprocess.safety_nets.apply_media_key_safety_net`
     runs ``find_key_for_query``, which infers the canonical key
     from the customer's text.
  4. ``resolve_by_key(db, tenant_id, key)`` looks up an
     ``AIMediaItem`` with ``media_key='payment_rajhi_barcode'``.
  5. If the merchant uploaded the asset via the new KB Hub
     (linked to "التحويل البنكي" with role='barcode') the row
     has ``media_key=NULL`` → resolver returns ``None`` → the
     safety net silently bails and the customer never sees the
     QR.

This module closes that gap. When the link target is one of the
payment kinds + the role is ``barcode``, we sniff the text
context (section title/body + media title) for a bank name and
auto-bind the corresponding registry key. The merchant didn't
have to learn that "payment_rajhi_barcode" exists — they just
attached "Rajhi QR" to "التحويل البنكي" and the platform figured
out the rest.

Design constraints
──────────────────
* Pure function — no DB calls, no IO, no global state. Takes
  already-resolved text inputs. The caller decides when (and
  whether) to persist.
* Conservative — returns ``None`` on ambiguity (e.g. text
  mentions TWO banks). Better to leave ``media_key`` NULL than
  to bind it to the wrong bank and silently ship the wrong QR
  to a customer who's about to transfer real money.
* Tight gating — only fires for ``link_role='barcode'`` AND
  section ``kind`` in :data:`_VALID_PAYMENT_KINDS`. Other roles
  / kinds may legitimately use a different media slug we don't
  know about; we don't guess.
* Never overwrites — caller MUST skip when ``media.media_key``
  is already set. This module enforces nothing about that
  (it's a pure inferrer) but the docstring on
  :func:`detect_payment_media_key` flags the contract.
"""
from __future__ import annotations

from typing import Dict, Optional, Set, Tuple

from services.media_key_registry import normalize_text


# Section kinds that are unambiguously "payment". Other kinds —
# even ones that mention banks tangentially (e.g. ``shipping_zone``
# describing a COD area) — must NOT auto-bind, because the asset
# is probably not a transfer QR.
_VALID_PAYMENT_KINDS: frozenset[str] = frozenset({
    "payment_method",
    "bank_transfer",
})


# Which payment registry key each bank ID maps to. Keep in lock-step
# with the canonical slugs in
# :mod:`services.media_key_registry.REGISTRY` — if a slug there is
# renamed, this map breaks the auto-link silently and the safety net
# stops finding the asset. A small unit test in
# ``test_payment_media_autolink`` asserts every value here exists in
# the registry so the breakage shows up at CI time, not in production.
_BANK_KEY_MAP: Dict[str, str] = {
    "rajhi":     "payment_rajhi_barcode",
    "alahli":    "payment_alahli_barcode",
    "barq":      "payment_barq_barcode",
    "stcpay":    "payment_stcpay_qr",
    "mobilypay": "payment_mobilypay_qr",
    "iban":      "payment_bank_transfer_image",
}


# Tight bank-name detection. Each entry is a list of PRE-NORMALISED
# substrings — if ANY hits the combined text, the bank is detected.
# We deliberately exclude very-generic words ("بنك" alone, "حوالة"
# alone) so we don't false-positive on tenant policies that mention
# banking generically without naming a bank.
#
# Order does not matter for correctness — ambiguity is handled at the
# function level (multiple banks → None). The lists are kept short
# and content-bearing; the normaliser collapses ``ال`` / ``أ`` / ``ى``
# variants so we don't need to enumerate every Arabic spelling.
_BANK_PATTERNS: Dict[str, Tuple[str, ...]] = {
    "rajhi":     ("راجحي", "rajhi", "alrajhi", "alrahji",
                  "ar rajhi", "ar-rajhi", "ابو دانه"),
    "alahli":    ("اهلي", "ahli", "alahli", "al-ahli", "al ahli",
                  "snb", "saudi national"),
    "barq":      ("برق", "بارق", "barq"),
    "stcpay":    ("stc pay", "stcpay", "stc-pay",
                  "اس تي سي باي", "إس تي سي باي", "ستي سي باي",
                  "محفظة stc", "محفظة اس تي سي"),
    "mobilypay": ("mobily pay", "mobilypay", "mobily-pay",
                  "موبايلي باي", "موبايلي pay",
                  "محفظة موبايلي"),
    "iban":      ("ايبان", "iban", "آيبان"),
}


# Pre-normalise every pattern once at import time. Saves a per-call
# pass over the same strings on every link the merchant clicks.
_NORMALISED_PATTERNS: Dict[str, Tuple[str, ...]] = {
    bank: tuple(filter(None, (normalize_text(p) for p in patterns)))
    for bank, patterns in _BANK_PATTERNS.items()
}


def detect_payment_media_key(
    *,
    section_kind: str,
    section_title: str,
    section_body: str,
    media_title: str,
    link_role: str,
) -> Optional[str]:
    """Infer the canonical registry key for a payment-section media link.

    Parameters
    ----------
    section_kind
        The ``MerchantKnowledgeSection.kind`` slug
        (``"payment_method"`` / ``"bank_transfer"`` to qualify).
    section_title
        The section's display title (used for bank-name sniffing).
    section_body
        The section's body text (used for bank-name sniffing).
    media_title
        The ``AIMediaItem.title`` of the asset being linked
        (often the most specific signal — merchants name the
        upload "Rajhi QR" / "باركود الأهلي" / ...).
    link_role
        The ``MerchantKnowledgeMedia.link_role`` for this link.
        Only ``"barcode"`` qualifies — other roles describe
        non-canonical assets (evidence photos, tutorial videos, …)
        that should not auto-bind to a payment registry key.

    Returns
    -------
    str | None
        * The registry key when **exactly one** bank pattern
          matches across the combined text.
        * ``None`` when:
            - ``link_role`` is not ``"barcode"``;
            - ``section_kind`` is not in
              :data:`_VALID_PAYMENT_KINDS`;
            - no bank pattern matched at all;
            - more than one bank matched (ambiguous — caller
              should leave the merchant to set ``media_key``
              manually rather than risk binding to the wrong
              bank).

    Caller contract
    ---------------
    * MUST check that the target media's ``media_key`` is empty
      before persisting the returned value. This function does
      NOT enforce that — it's a pure inferrer.
    * SHOULD log the inferred key + the inputs that triggered it
      so a future "wrong QR sent" investigation can replay the
      decision.
    """
    role = (link_role or "").strip().lower()
    if role != "barcode":
        return None

    kind = (section_kind or "").strip().lower()
    if kind not in _VALID_PAYMENT_KINDS:
        return None

    combined = normalize_text(
        " ".join(p for p in (section_title, section_body, media_title) if p)
    )
    if not combined:
        return None

    hits: Set[str] = set()
    for bank_id, patterns in _NORMALISED_PATTERNS.items():
        for p in patterns:
            if p and p in combined:
                hits.add(bank_id)
                break  # one pattern per bank is sufficient

    if not hits:
        return None

    # Disambiguation: a specific-bank QR shadows a tenant's generic
    # IBAN image. Merchants who upload "تحويل بنكي - الراجحي" almost
    # always mean the Rajhi QR, not their generic IBAN screenshot.
    # Drop "iban" if it's the ONLY extra hit alongside a single
    # specific bank.
    if len(hits) > 1 and "iban" in hits and len(hits - {"iban"}) == 1:
        hits.discard("iban")

    if len(hits) != 1:
        # Two or more specific banks → genuinely ambiguous. Bail and
        # let the merchant pick. Better silent fallback than wrong QR.
        return None

    only = next(iter(hits))
    return _BANK_KEY_MAP[only]


__all__ = [
    "detect_payment_media_key",
]
