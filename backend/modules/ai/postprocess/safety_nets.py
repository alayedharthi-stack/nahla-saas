"""
modules/ai/postprocess/safety_nets.py
─────────────────────────────────────
Deterministic "Post-LLM Safety Nets" — backstops that recover the
intended interactive WhatsApp experience when Claude **forgets** to
emit the right marker.

Why this module exists
──────────────────────
The marker contract in ``core/ai_libraries._MARKER_PROTOCOL_PREAMBLE``
makes ``[PRODUCT:...]`` / ``[MEDIA_KEY:...]`` / ``[CALL:...]``
mandatory in well-defined situations. The prompt is strict, the
few-shot examples are direct, and the supervisor models comply
most of the time. But "most of the time" is not 100% — production
on 2026-05-13 surfaced three failure modes in a single session:

  * Customer: "أبي أشوف عسل الطلح" → Claude wrote a text reply
    with a CTA link but no ``[PRODUCT:...]`` → customer got the
    link, not the product card.
  * Customer: "أرسل باركود الراجحي" → Claude wrote "تفضل باركود
    الراجحي 🌷 امسحه من تطبيق الراجحي" with no
    ``[MEDIA_KEY:...]`` → customer got prose with no image.
  * Customer: "أبي أكلم أمين" → Claude wrote the phone as plain
    text with no ``[CALL:...]`` → customer got a number, not a
    contact card.

Tightening the prompt further has diminishing returns; the
production fix is to **not rely on a single layer**. After Claude
returns, we run three pure functions over the customer message +
reply and **add** the resolved attachments / contact cards the
LLM forgot. We never DELETE what Claude emitted — only fill gaps.

Design contract
───────────────
* Pure inputs / outputs. Each function returns a small, typed
  result; the caller (webhook) merges those into its existing
  ``_product_attachments`` / ``_media_attachments`` / ``_call_targets``
  lists.
* Idempotent. Running a net twice yields the same result.
* No DELETION of Claude-emitted markers. If Claude DID emit
  ``[PRODUCT:عسل الطلح]`` and we'd also resolve "عسل الطلح" via the
  safety net, we skip — the marker pipeline already handled it.
* Independent feature flags per net (kill-switch granularity for
  the rollout):

  * ``PRODUCT_SAFETY_NET_ENABLED``      (default ON)
  * ``MEDIA_KEY_SAFETY_NET_ENABLED``    (default ON)
  * ``STAFF_CONTACT_SAFETY_NET_ENABLED`` (default ON)

* Structured ``[SAFETY_NET:<kind>]`` logs on every fire so the
  team can grep production for "how often did the LLM fail to
  emit a marker that we recovered".

Adding a new safety net
───────────────────────
1. Define the trigger lexicon in a private ``_TRIGGER_*`` set
   (keep it Arabic-first, lowercase, normalised).
2. Add the ``apply_<kind>_safety_net`` function with a
   ``Result`` dataclass.
3. Wire it in ``backend/routers/whatsapp_webhook.py`` right
   after the existing marker extraction phase, BEFORE the
   ``[MARKER_RESOLUTION]`` log so the counters reflect the
   final state.
4. Emit a ``[SAFETY_NET:<kind>]`` JSON log on every fire.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    # Some webhook unit tests import this module without a real DB
    # session in scope. The DB type is hint-only here.
    from sqlalchemy.orm import Session  # noqa: F401
except Exception:  # pragma: no cover
    Session = Any  # type: ignore[misc,assignment]


logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# Feature flags
# ──────────────────────────────────────────────────────────────────


_FLAG_FALSY = {"0", "false", "no", "off", "disabled"}


def _flag(env_name: str) -> bool:
    """ON unless explicitly disabled. Read on every call so a hot
    kill-switch needs no restart."""
    raw = os.getenv(env_name)
    if raw is None:
        return True
    return raw.strip().lower() not in _FLAG_FALSY


def product_net_enabled() -> bool:
    return _flag("PRODUCT_SAFETY_NET_ENABLED")


def media_key_net_enabled() -> bool:
    return _flag("MEDIA_KEY_SAFETY_NET_ENABLED")


def staff_contact_net_enabled() -> bool:
    return _flag("STAFF_CONTACT_SAFETY_NET_ENABLED")


def store_link_net_enabled() -> bool:
    return _flag("STORE_LINK_SAFETY_NET_ENABLED")


def location_link_net_enabled() -> bool:
    """Toggle for the May 2026 #36 maps URL safety net.

    Defaults to ON via :func:`_flag` (which treats unset env vars as
    enabled) — same convention as :func:`store_link_net_enabled`.
    Set ``LOCATION_LINK_SAFETY_NET_ENABLED=0`` to kill-switch the
    maps stack at the platform level if a regression slips through.
    """
    return _flag("LOCATION_LINK_SAFETY_NET_ENABLED")


def clear_intent_fallback_net_enabled() -> bool:
    return _flag("CLEAR_INTENT_FALLBACK_NET_ENABLED")


def delivery_info_context_net_enabled() -> bool:
    return _flag("DELIVERY_INFO_CONTEXT_NET_ENABLED")


# ──────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────


# Arabic diacritics to strip for trigger matching.
_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_DIA_RE = re.compile(f"[{_DIA}]+")


def _normalise_for_match(text: str) -> str:
    """Lower-case + strip Arabic diacritics + collapse whitespace.

    We DON'T do alif/yaa folding here — the prompt teaches the LLM
    explicit forms and the trigger lists below include the common
    variants. Folding would cost us nothing for matching but would
    make the production logs harder to grep.
    """
    if not text:
        return ""
    t = text.strip().lower()
    t = _DIA_RE.sub("", t)
    t = re.sub(r"\s+", " ", t)
    return t


def _has_any(needles: set, haystack: str) -> bool:
    return any(n in haystack for n in needles)


# ──────────────────────────────────────────────────────────────────
# 1. Product Safety Net
# ──────────────────────────────────────────────────────────────────
#
# Trigger lexicon: verbs the customer uses when they want to *see*
# a specific product. Deliberately a small handful — false positives
# here mean we'd send an unrelated product card.

_PRODUCT_INTENT_VERBS: set = {
    "ابي اشوف",
    "أبي أشوف",
    "ابغى اشوف",
    "أبغى أشوف",
    "ابي اشوف",
    "ودي اشوف",
    "أرسل",
    "ارسل",
    "ابعث",
    "أبعث",
    "ابغى",
    "أبغى",
    "ابي",
    "أبي",
    "اعطني",
    "أعطني",
    "ودي",
    "اوريني",
    "ابي صور",
    "تشوف",
    "أبيك ترسل",
    "ابيك ترسل",
}


# Product-class hints in Arabic; presence raises confidence that
# the customer is asking for a catalog item (not a vague "صور" of
# the store). We keep this PURPOSELY narrow — adding too many
# would let any greeting hit the product resolver.

_PRODUCT_CLASS_HINTS: set = {
    "عسل", "العسل",
    "طلح", "الطلح",
    "سدر", "السدر",
    "سمر", "السمر",
    "مجرى", "المجرى",
    "ضومران", "الضومران",
    "زهر", "الزهر", "الزهور",
    "كشار", "الكشار",
    "غذاء", "غذاء الملكات", "ملكي",
    "حبة", "الحبة", "حبة البركة",
    "زنجبيل", "الزنجبيل",
    "زعتر", "الزعتر",
    "منتج", "المنتج",
}


@dataclass
class ProductSafetyNetResult:
    fired: bool = False
    reason: str = ""
    skipped_reason: str = ""
    customer_query: str = ""
    resolved_id: Optional[Any] = None
    resolved_title: str = ""
    confidence: str = ""
    extra_attachment: Optional[Dict[str, Any]] = None

    def to_log_dict(self) -> Dict[str, Any]:
        d = {
            "kind":             "product",
            "fired":            self.fired,
            "reason":           self.reason or self.skipped_reason,
            "customer_query":   self.customer_query[:120],
            "resolved_id":      self.resolved_id,
            "resolved_title":   self.resolved_title[:80],
            "confidence":       self.confidence,
        }
        return d


def apply_product_safety_net(
    db: Any,
    *,
    tenant_id: int,
    customer_msg: str,
    existing_product_attachments: List[Dict[str, Any]],
    detected_markers: int,
    customer_id: Optional[Any] = None,
) -> ProductSafetyNetResult:
    """Try to attach a product card when Claude forgot to emit
    ``[PRODUCT:...]`` but the customer clearly asked for one.

    Caller pattern::

        net = apply_product_safety_net(
            db,
            tenant_id=tenant_id,
            customer_msg=text,
            existing_product_attachments=_product_attachments,
            detected_markers=_marker_detected["product"],
            customer_id=getattr(convo, "customer_id", None),
        )
        if net.fired and net.extra_attachment:
            _product_attachments.append(net.extra_attachment)
            _marker_resolved["product"] += 1   # counts toward [MARKER_RESOLUTION]
    """
    result = ProductSafetyNetResult(customer_query=customer_msg or "")

    if not product_net_enabled():
        result.skipped_reason = "flag_disabled"
        return result

    # Already covered: either Claude emitted the marker (and we
    # resolved it) OR a previous net already added an attachment.
    if detected_markers > 0:
        result.skipped_reason = "claude_marker_present"
        return result
    if existing_product_attachments:
        result.skipped_reason = "already_attached"
        return result

    msg = _normalise_for_match(customer_msg)
    if not msg:
        result.skipped_reason = "empty_msg"
        return result

    has_intent = _has_any(_PRODUCT_INTENT_VERBS, msg)
    has_class = _has_any(_PRODUCT_CLASS_HINTS, msg)

    if not (has_intent and has_class):
        result.skipped_reason = "no_intent_or_class"
        return result

    # Delegate the actual lookup to the resolver. It already returns
    # ``None`` for too-short queries / no-match — we don't need to
    # second-guess it.
    try:
        from services.product_resolver import (  # noqa: PLC0415
            resolve_by_query as _resolve,
            format_product_card_caption as _caption,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("safety_nets.product | import failure: %s", exc)
        result.skipped_reason = "import_failure"
        return result

    try:
        resolution = _resolve(
            db, tenant_id, customer_msg or "",
            customer_id=customer_id, limit=5,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "safety_nets.product | resolve_by_query failed tenant=%s err=%s",
            tenant_id, exc,
        )
        result.skipped_reason = "resolver_error"
        return result

    if not resolution:
        result.skipped_reason = "no_match"
        return result

    # Build the SAME attachment shape the existing product-marker
    # pipeline produces so the downstream sender code is one branch.
    attachment = {
        "kind":         "product_card",
        "id":           resolution.id,
        "title":        resolution.title,
        "media_type":   "image",
        "file_url":     resolution.image_url,
        "caption":      _caption(resolution),
        "product_url":  resolution.product_url,
        "price":        resolution.price,
        "in_stock":     resolution.in_stock,
        "external_id":  resolution.external_id,
        "confidence":   resolution.confidence,
        "safety_net":   True,  # surfaces in logs + downstream metrics
    }

    result.fired = True
    result.reason = "intent_plus_class_hit"
    result.resolved_id = resolution.id
    result.resolved_title = resolution.title or ""
    result.confidence = resolution.confidence
    result.extra_attachment = attachment
    return result


# ──────────────────────────────────────────────────────────────────
# 2. Media Key Safety Net
# ──────────────────────────────────────────────────────────────────
#
# The media_key_registry already has a ``find_key_for_query``
# function that maps customer phrases to canonical keys (e.g.
# "باركود الراجحي" → "payment_rajhi_barcode"). We just need to
# wire it as a fallback when no ``[MEDIA_KEY:...]`` was emitted.


@dataclass
class MediaKeySafetyNetResult:
    fired: bool = False
    reason: str = ""
    skipped_reason: str = ""
    inferred_key: str = ""
    asset_available: bool = False
    extra_attachment: Optional[Dict[str, Any]] = None

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "kind":            "media_key",
            "fired":           self.fired,
            "reason":          self.reason or self.skipped_reason,
            "inferred_key":    self.inferred_key,
            "asset_available": self.asset_available,
        }


def apply_media_key_safety_net(
    db: Any,
    *,
    tenant_id: int,
    customer_msg: str,
    existing_media_attachments: List[Dict[str, Any]],
    detected_media_key_markers: int,
) -> MediaKeySafetyNetResult:
    """Try to attach an asset (image / PDF / video) when Claude
    forgot to emit ``[MEDIA_KEY:...]`` but the customer clearly
    asked for an asset we have on file (e.g. payment barcode).

    Caller pattern::

        net = apply_media_key_safety_net(
            db,
            tenant_id=tenant_id,
            customer_msg=text,
            existing_media_attachments=_media_attachments,
            detected_media_key_markers=_marker_detected["media_key"],
        )
        if net.fired and net.extra_attachment:
            _media_attachments.append(net.extra_attachment)
            _marker_resolved["media_key"] += 1
    """
    result = MediaKeySafetyNetResult()

    if not media_key_net_enabled():
        result.skipped_reason = "flag_disabled"
        return result
    if detected_media_key_markers > 0:
        # Claude DID emit the marker — the regular resolver handled it.
        result.skipped_reason = "claude_marker_present"
        return result

    # Don't double-attach: if the existing media attachments already
    # contain ANY media_key (added by a previous resolver), skip.
    for att in existing_media_attachments or []:
        if att.get("media_key"):
            result.skipped_reason = "already_has_media_key"
            return result

    if not (customer_msg or "").strip():
        result.skipped_reason = "empty_msg"
        return result

    try:
        from services.media_resolver import (  # noqa: PLC0415
            resolve_for_query as _resolve_media,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("safety_nets.media_key | import failure: %s", exc)
        result.skipped_reason = "import_failure"
        return result

    try:
        resolution, inferred = _resolve_media(db, tenant_id, customer_msg or "")
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "safety_nets.media_key | resolve_for_query failed tenant=%s err=%s",
            tenant_id, exc,
        )
        result.skipped_reason = "resolver_error"
        return result

    if not inferred:
        result.skipped_reason = "no_trigger_match"
        return result
    result.inferred_key = inferred

    if not resolution:
        # Key matched but the merchant hasn't uploaded the asset —
        # the prompt path will (separately) append the registry's
        # ``fallback_text``. Here we just report and bail.
        result.asset_available = False
        result.skipped_reason = "asset_missing"
        return result

    result.asset_available = True
    result.fired = True
    result.reason = "trigger_to_active_asset"
    result.extra_attachment = resolution.to_attachment()
    # Mark so downstream logs can tell this was a recovery.
    result.extra_attachment["safety_net"] = True
    return result


# ──────────────────────────────────────────────────────────────────
# 3. Staff Contact Safety Net
# ──────────────────────────────────────────────────────────────────
#
# When the customer asks to contact a staff member by name AND
# Claude emitted the phone number as plain text (not ``[CALL:...]``),
# we extract the phone from the reply and build a CallTarget
# manually.
#
# We deliberately use the AI-emitted phone as the source of truth
# (not a tenant staff directory). Reason: that directory doesn't
# exist yet, and Claude already has access to the KB context
# where staff phones live — if it WROTE the number, it almost
# certainly wrote the right number.

# Verbs/nouns that signal "I want to talk to a person":
_STAFF_INTENT_TRIGGERS: set = {
    "اكلم",
    "أكلم",
    "ابي اكلم",
    "أبي أكلم",
    "اتصل ب",
    "اتصل ع",
    "أتصل",
    "تواصل مع",
    "ابي اتواصل",
    "ابغى اتواصل",
    "اكلم احد",
    "كلم",
    "أكلم",
    "احتاج اكلم",
    "محتاج اكلم",
    "رقم",      # "ابي رقم أمين" / "ابي رقم الإدارة"
}

# Staff name candidates. The customer will reference one of these
# when they want a contact card. The tenant-specific list lives
# (for now) in the prompt KB; we hardcode the common Saudi staff
# names to catch the common requests. Adding here is cheap.
_STAFF_NAME_CANDIDATES: List[str] = [
    "أمين", "امين",
    "هشام",
    "هيثم",
    "أحمد", "احمد",
    "محمد",
    "سعد",
    "خالد",
    "عبدالله",
    "عبدالعزيز",
    "تركي",
    "أبو هشام", "ابو هشام",
    "الإدارة", "الادارة",
    "المالك",
    "صاحب المتجر",
    "المسؤول",
    "الموظف",
    "المسؤولة",
]


# Saudi phone — captures the common shapes Claude emits in replies.
# We match strictly so we don't accidentally lift a price ("بسعر 99
# ريال") or order number as a phone. The patterns mirror the ones
# in ``call_resolver._normalize_saudi_phone`` but here we just
# capture; normalisation happens via the call_resolver helper.
_PHONE_REGEXES: List[re.Pattern[str]] = [
    re.compile(r"\b\+?\s*9665\d{8}\b"),
    re.compile(r"\b00\s*9665\d{8}\b"),
    re.compile(r"\b05\d{8}\b"),
    re.compile(r"\b5\d{8}\b"),
]


def _extract_phones(text: str) -> List[str]:
    if not text:
        return []
    seen: List[str] = []
    for pat in _PHONE_REGEXES:
        for m in pat.findall(text):
            cand = m.strip()
            if cand and cand not in seen:
                seen.append(cand)
    return seen


def _find_staff_name(customer_msg_norm: str) -> Optional[str]:
    """Pick the longest staff-name candidate found in the
    (already-normalised) customer message. Longest wins so
    "أبو هشام" beats "هشام"."""
    if not customer_msg_norm:
        return None
    hits = [n for n in _STAFF_NAME_CANDIDATES if n in customer_msg_norm]
    if not hits:
        return None
    hits.sort(key=len, reverse=True)
    return hits[0]


@dataclass
class StaffContactSafetyNetResult:
    fired: bool = False
    reason: str = ""
    skipped_reason: str = ""
    inferred_name: str = ""
    wa_id: str = ""
    extra_call_target: Any = None  # CallTarget when fired

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "kind":           "staff_contact",
            "fired":          self.fired,
            "reason":         self.reason or self.skipped_reason,
            "inferred_name":  self.inferred_name,
            "wa_id":          self.wa_id,
        }


def apply_staff_contact_safety_net(
    *,
    customer_msg: str,
    reply_text: str,
    existing_call_targets: List[Any],
    detected_call_markers: int,
) -> StaffContactSafetyNetResult:
    """Build a contact-card ``CallTarget`` when the customer asked
    to reach a staff member by name AND Claude wrote the phone as
    plain text in the reply.

    Returns a :class:`StaffContactSafetyNetResult`. When ``fired``
    is true, the caller should append ``extra_call_target`` to
    ``_call_targets`` and bump ``_marker_resolved["call"]``.
    """
    result = StaffContactSafetyNetResult()

    if not staff_contact_net_enabled():
        result.skipped_reason = "flag_disabled"
        return result
    if detected_call_markers > 0:
        result.skipped_reason = "claude_marker_present"
        return result
    if existing_call_targets:
        result.skipped_reason = "already_attached"
        return result

    msg_norm = _normalise_for_match(customer_msg)
    if not msg_norm:
        result.skipped_reason = "empty_msg"
        return result

    if not _has_any(_STAFF_INTENT_TRIGGERS, msg_norm):
        result.skipped_reason = "no_staff_intent"
        return result

    name = _find_staff_name(msg_norm)
    if not name:
        result.skipped_reason = "no_staff_name"
        return result
    result.inferred_name = name

    # We need a phone in the reply text — otherwise we have nothing
    # to put on the contact card. (We won't guess from the customer
    # message; the customer typed the request, not the phone.)
    phones = _extract_phones(reply_text or "")
    if not phones:
        result.skipped_reason = "no_phone_in_reply"
        return result

    raw_phone = phones[0]

    try:
        from services.call_resolver import (  # noqa: PLC0415
            CallTarget,
            _normalize_saudi_phone,
            _pretty_phone,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("safety_nets.staff | import failure: %s", exc)
        result.skipped_reason = "import_failure"
        return result

    wa_id = _normalize_saudi_phone(raw_phone)
    if not wa_id:
        result.skipped_reason = "phone_normalize_failed"
        return result
    result.wa_id = wa_id

    # Clean up the display name a bit — strip articles for prettier
    # cards. "الإدارة" → keeps as-is (institutional label); proper
    # names stay as the customer typed them so the card matches the
    # mental model.
    display_name = name if name not in {"الإدارة", "الادارة"} else "الإدارة"

    target = CallTarget(
        name=display_name,
        wa_id=wa_id,
        phone_display=_pretty_phone(wa_id),
        raw_phone=raw_phone,
    )
    result.extra_call_target = target
    result.fired = True
    result.reason = "intent_plus_name_plus_phone_in_reply"
    return result


# ──────────────────────────────────────────────────────────────────
# 4. Store-Link Safety Net
# ──────────────────────────────────────────────────────────────────
#
# Production complaint (May 2026, see attached screenshot): customer
# typed "رابط المتجر"; the LLM replied "هذا متجرنا 🌷" with NO URL
# attached. The merchant looked unprofessional and the conversion
# died on the spot.
#
# The high-priority prompt already includes the rule
# "أرسل الرابط فقط بدون سؤال متابعة عن المنتج" — but rules alone
# don't bind a generative model. This deterministic safety net
# guarantees the URL lands whenever the customer's intent is
# unambiguously "give me the store link" AND we actually have the
# URL stored on the tenant.
#
# Contract
# ────────
# * Trigger lexicon is intentionally narrow (8 phrases) so we never
#   pre-empt a legitimate product/category question that happens to
#   include the word "متجر".
# * We NEVER hallucinate a URL — if ``store_url`` isn't set on
#   the tenant, we return a polite "أبشر — أرسل لك الرابط بعد
#   التأكد منه" fallback so the merchant is reminded to fill in
#   their settings (visible to the customer as a non-alarming
#   "I'll send it shortly").
# * We NEVER overwrite a reply that ALREADY contains a URL — that
#   path is fine; the LLM already complied with the prompt.
# * The fix is text-level only; no marker, no attachment, no
#   product / media / order side-effects. The webhook merges the
#   rewritten reply into the outbound payload exactly like it
#   does for ``reasoning_scrub``.
#
# Adding new triggers
# ───────────────────
# Keep the lexicon narrow. A trigger like "متجر" alone would fire
# on any product question ("في عسل في المتجر؟"). The current set
# requires either "رابط"/"link"/"موقع"/"website" or a self-contained
# noun like "المتجر الإلكتروني".

# Phrases that mean "send me the *online* store URL".
#
# May 2026 #36 carve-out: bare-"موقعكم" / "رابط الموقع" /
# "رابط موقعكم" phrasings used to live here, which made the
# store-link safety net rewrite "وين موقعكم؟" with the
# e-commerce URL even when the customer wanted Google Maps.
# Those phrases have been moved into :data:`_LOCATION_LINK_TRIGGERS_PHRASE`
# below — the location safety net handles them now.
_STORE_LINK_TRIGGERS_PHRASE: set = {
    # Arabic — direct
    "رابط المتجر",
    "رابط المتجرر",
    "رابط متجركم",
    "رابط متجرك",
    "رابط متجرنا",
    "موقع المتجر",
    "ارسل رابط المتجر",
    "أرسل رابط المتجر",
    "ابعث رابط المتجر",
    "أبعث رابط المتجر",
    "ابغى رابط المتجر",
    "أبغى رابط المتجر",
    "ابي رابط المتجر",
    "أبي رابط المتجر",
    "ودي رابط المتجر",
    "اعطني رابط المتجر",
    "أعطني رابط المتجر",
    "وين رابط المتجر",
    "وش رابط المتجر",
    "ايش رابط المتجر",
    "إيش رابط المتجر",
    "اللينك",
    "الينك",
    "ارسل اللينك",
    "أرسل اللينك",
    "ابعث اللينك",
    "أبعث اللينك",
    "ابي اللينك",
    "أبي اللينك",
    "ابغى اللينك",
    "ارسل الرابط",
    "أرسل الرابط",
    "ابعث الرابط",
    "أبعث الرابط",
    "ارسلي الرابط",
    "أرسلي الرابط",
    "ابعثلي الرابط",
    "ابي الرابط",
    "أبي الرابط",
    "ابغى الرابط",
    "أبغى الرابط",
    "ودي الرابط",
    "اعطني الرابط",
    "أعطني الرابط",
    "وين الرابط",
    "ابي رابط",
    "أبي رابط",
    "ابغى رابط",
    "أبغى رابط",
    "ارسل رابط",
    "أرسل رابط",
    "ابعث رابط",
    "ابعثلي رابط",
    "ودي رابط",
    "المتجر الالكتروني",
    "المتجر الإلكتروني",
    "موقعكم الالكتروني",
    "موقعكم الإلكتروني",
    # English — direct
    "store link",
    "store url",
    "website link",
    "website url",
    "send the link",
    "send link",
    "send the website",
    "send your website",
    "your website",
    "your store",
    "shop link",
    "shop url",
}


# URL-presence detector. Any of these substrings in the reply means
# the LLM already shipped a link — the safety net stays out of the
# way. We tolerate http://, https://, www.* and bare domains with a
# common TLD because Saudi stores frequently sit on .sa / .store /
# .shop / .com / .net / salla.sa / mysalla.com etc.
_URL_PRESENT_RE = re.compile(
    r"(?:https?://|www\.)\S+|\b[a-z0-9][a-z0-9-]*\."
    r"(?:com|net|sa|store|shop|me|io|co|app)\b",
    re.IGNORECASE,
)


# Empty/placeholder reply markers — these are the texts the LLM
# ships when it forgets the URL. Detecting them helps us decide
# whether to REPLACE the reply (very short / generic) or APPEND
# the URL (longer reply with context the customer asked for).
_GENERIC_HERE_IS_THE_STORE_MARKERS: tuple = (
    "هذا متجرنا",
    "هذي متجرنا",
    "متجرنا هنا",
    "هذا هو متجرنا",
    "هذا الرابط",
    "تفضل المتجر",
    "تفضلي المتجر",
    "تفضل الرابط",
    "هذا الموقع",
)


@dataclass
class StoreLinkSafetyNetResult:
    fired: bool = False
    reason: str = ""
    skipped_reason: str = ""
    rewrote_reply: bool = False
    store_url: str = ""
    new_reply: str = ""

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "kind":           "store_link",
            "fired":          self.fired,
            "reason":         self.reason or self.skipped_reason,
            "rewrote_reply":  self.rewrote_reply,
            "store_url_present": bool(self.store_url),
        }


def _looks_like_store_link_request(customer_msg: str) -> bool:
    msg = _normalise_for_match(customer_msg)
    if not msg:
        return False
    # Drop punctuation that fragments the phrase match.
    msg_compact = re.sub(r"[؟?,،.!:;\-\u060c]+", " ", msg)
    msg_compact = re.sub(r"\s+", " ", msg_compact).strip()
    for phrase in _STORE_LINK_TRIGGERS_PHRASE:
        if phrase in msg_compact:
            return True
    return False


def _normalise_url(url: str) -> str:
    """Trim whitespace, drop trailing slash, promote bare domains to
    ``https://``. Returns an empty string for falsy input.

    Used by every link source below so callers can stay tiny and the
    callsite logs are exact (we know the URL was already normalised).
    """
    s = str(url or "").strip().rstrip("/")
    if not s:
        return ""
    if not s.lower().startswith(("http://", "https://")):
        s = "https://" + s.lstrip("/")
    return s


def _lookup_tenant_store_url(db: Any, tenant_id: int) -> str:
    """Return the canonical store URL configured for ``tenant_id``,
    or an empty string when none is stored.

    Resolution chain (May 2026 #35 — platform-wide button-URL fallback):

      1. ``StoreKnowledgeSnapshot.store_profile["store_url"]`` — the
         canonical synced source, written by the platform-sync job
         (Salla/Zid/Shopify webhooks → snapshot table). When the
         merchant connects a platform this is populated automatically
         and is the freshest signal we have.
      2. ``TenantSettings.store_settings["store_url"]`` — manual
         entry from the dashboard's "Store" tab. Used by merchants
         who don't connect a platform but still want the AI to
         surface a link (custom Shopify, WooCommerce, Zid SDK, …).
      3. ``TenantSettings.whatsapp_settings["store_button_url"]`` —
         manual entry from the dashboard's "WhatsApp" tab; the URL
         the merchant types into the "Visit Store" CTA-button slot.
         Same intent class as (2) but a different field. Many
         Nahla-native shops fill ONLY this slot because that's what
         their template-builder UI exposes most prominently. Without
         this layer the AI cannot deliver "ابي رابط المتجر" for
         those tenants even though the URL is sitting one column
         away. Default value (``""``) means an empty merchant entry
         simply falls through to the next layer — no behaviour
         change for tenants who were already resolving via 1/2/4.
      4. ``Integration.config["store_url"|"storefront_url"|"domain"
         |"shop_domain"]`` for ANY provider — Salla, Zid, Shopify,
         WooCommerce. The previous implementation only checked Salla,
         which silently failed for tenants on other platforms.

    Never raises. Every step is wrapped — a DB hiccup degrades to
    "try the next source" rather than taking down the safety net.

    Logs a single ``[STORE_LINK_RESOLVER]`` INFO line per call with
    the chosen source and url length so production traffic can be
    audited via grep without enabling DEBUG.
    """
    if db is None or not tenant_id:
        return ""
    tenant_id = int(tenant_id)

    # ── 1) Synced store profile (StoreKnowledgeSnapshot) ────────────
    try:
        from core.store_knowledge import StoreKnowledgeLoader  # noqa: PLC0415
        loader = StoreKnowledgeLoader(db, tenant_id)
        profile = loader.store_profile() or {}
        url = _normalise_url(profile.get("store_url"))
        if url:
            logger.info(
                "[STORE_LINK_RESOLVER] tenant_id=%s source=snapshot url_len=%d",
                tenant_id, len(url),
            )
            return url
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "safety_nets.store_link | snapshot lookup failed tenant=%s "
            "err=%s", tenant_id, exc,
        )

    # ── 2) Tenant settings — store tab (manual entry) ────────────────
    # AND
    # ── 3) Tenant settings — whatsapp tab (CTA button URL slot) ─────
    # Combined under one DB read so we don't re-fetch the same row.
    settings = None
    try:
        from core.tenant import (  # noqa: PLC0415
            DEFAULT_STORE, DEFAULT_WHATSAPP,
            get_or_create_settings, merge_defaults,
        )
        settings = get_or_create_settings(db, tenant_id)

        store_cfg = merge_defaults(settings.store_settings, DEFAULT_STORE)
        url = _normalise_url(store_cfg.get("store_url"))
        if url:
            logger.info(
                "[STORE_LINK_RESOLVER] tenant_id=%s source=store_settings url_len=%d",
                tenant_id, len(url),
            )
            return url

        wa_cfg = merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
        url = _normalise_url(wa_cfg.get("store_button_url"))
        if url:
            logger.info(
                "[STORE_LINK_RESOLVER] tenant_id=%s source=whatsapp_button url_len=%d",
                tenant_id, len(url),
            )
            return url
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "safety_nets.store_link | settings lookup failed tenant=%s "
            "err=%s", tenant_id, exc,
        )

    # ── 4) Any platform Integration (broaden beyond Salla) ───────────
    try:
        from models import Integration  # noqa: PLC0415
        # Provider order kept stable so test output is predictable.
        # Add new providers HERE — every entry automatically becomes
        # an additional source-of-truth checkpoint.
        for provider in ("salla", "zid", "shopify", "woocommerce"):
            integration = db.query(Integration).filter(
                Integration.tenant_id == tenant_id,
                Integration.provider  == provider,
            ).first()
            if not integration:
                continue
            cfg = integration.config or {}
            url = _normalise_url(
                cfg.get("store_url")
                or cfg.get("storefront_url")
                or cfg.get("domain")
                or cfg.get("shop_domain")
            )
            if url:
                logger.info(
                    "[STORE_LINK_RESOLVER] tenant_id=%s source=integration:%s url_len=%d",
                    tenant_id, provider, len(url),
                )
                return url
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "safety_nets.store_link | integration lookup failed tenant=%s "
            "err=%s", tenant_id, exc,
        )

    logger.info(
        "[STORE_LINK_RESOLVER] tenant_id=%s source=none url_len=0 "
        "reason=no_source_configured",
        tenant_id,
    )
    return ""


def _looks_like_bare_store_intro(reply: str) -> bool:
    """True when the reply is a short generic "here is our store"
    line with no actual URL — e.g. "هذا متجرنا 🌷". We use this
    to decide between REPLACING the reply (very generic) and
    APPENDING the URL (long contextual reply)."""
    if not reply:
        return True
    trimmed = (reply or "").strip()
    # Anything under ~24 chars + a generic marker = a stub.
    short = len(trimmed) <= 60
    norm = _normalise_for_match(trimmed)
    has_marker = any(m in norm for m in _GENERIC_HERE_IS_THE_STORE_MARKERS)
    return short and has_marker


# ── No-URL fallback (revised May 2026 #31) ──────────────────────────
# The old fallback ("أبشر — أرسل لك الرابط بعد التأكد منه") was itself
# a broken promise: it said "I'll send the link" while no link was on
# file. That tripped the new ``maybe_scrub_unkept_asset_promise``
# guard, producing the awkward concatenated text the Tenant 33 owner
# flagged. The new copy is honest — it asks the customer ONE
# clarifying question instead of restating a promise we can't keep,
# and it does NOT contain any "أرسل لك الرابط" / "أرسل لك" phrase
# that the asset-promise sanitizer would rewrite again.
_FALLBACK_NO_URL_REPLY_AR = (
    "تأمر أمر 🌷 خبّرنا أي قسم أو منتج تبحث عنه وسنرسل تفاصيله "
    "مباشرة."
)


def _build_store_link_reply(store_url: str) -> str:
    """Canonical Arabic reply when we DO have a URL. Kept short so
    WhatsApp renders the link as a tappable preview."""
    return f"تفضل رابط متجرنا 🌷\n{store_url}"


def apply_store_link_safety_net(
    db: Any,
    *,
    tenant_id: int,
    customer_msg: str,
    reply_text: str,
) -> StoreLinkSafetyNetResult:
    """Guarantee the store URL lands when the customer asked for it.

    Behaviour matrix:

    +-------------------------------+--------------------------------+
    | Customer intent = store link? | URL already in reply?          |
    +================+==============+===============+================+
    |                | yes          | yes           | no-op (skip)    |
    |  store link    +--------------+---------------+----------------+
    |  intent?       | yes          | no            | rewrite reply  |
    +----------------+--------------+---------------+----------------+
    |                | no           | -             | no-op (skip)    |
    +----------------+--------------+----------------+---------------+

    When we rewrite:

    * If ``store_url`` is configured → use the canonical reply
      ``"تفضل رابط متجرنا 🌷\\n{store_url}"``. If the original
      reply was a non-generic longer message (e.g. answered another
      question first), we APPEND the link on a new line instead of
      replacing the body so we don't lose the LLM's other content.
    * If ``store_url`` is NOT configured → return the polite
      placeholder ``"أبشر 🌷 أرسل لك الرابط بعد التأكد منه."``.
      We never invent a URL.

    Caller pattern (in webhook)::

        net = apply_store_link_safety_net(
            db, tenant_id=tenant_id,
            customer_msg=text, reply_text=reply,
        )
        if net.fired and net.rewrote_reply:
            reply = net.new_reply
    """
    result = StoreLinkSafetyNetResult()

    if not store_link_net_enabled():
        result.skipped_reason = "flag_disabled"
        return result

    if not _looks_like_store_link_request(customer_msg or ""):
        result.skipped_reason = "no_store_link_intent"
        return result
    result.reason = "intent_detected"

    if reply_text and _URL_PRESENT_RE.search(reply_text):
        # The LLM already shipped a URL — nothing to fix.
        result.skipped_reason = "url_already_in_reply"
        return result

    store_url = _lookup_tenant_store_url(db, tenant_id)
    result.store_url = store_url

    if store_url:
        if _looks_like_bare_store_intro(reply_text or ""):
            # Short generic stub → replace with the canonical short
            # reply that includes the URL.
            result.new_reply = _build_store_link_reply(store_url)
        elif reply_text and reply_text.strip():
            # Longer reply (LLM may have answered something else
            # first) → keep its content and APPEND the link on a
            # new line so the URL still lands.
            sep = "\n" if reply_text.endswith("\n") else "\n\n"
            result.new_reply = (
                reply_text.rstrip() + sep + _build_store_link_reply(store_url)
            )
        else:
            # Empty reply (rare) — just send the canonical line.
            result.new_reply = _build_store_link_reply(store_url)
        result.fired = True
        result.rewrote_reply = True
        result.reason = "url_injected"
        return result

    # No URL on file. Avoid hallucinating one — return the polite
    # placeholder so the customer doesn't see "هذا متجرنا 🌷" alone.
    result.new_reply = _FALLBACK_NO_URL_REPLY_AR
    result.fired = True
    result.rewrote_reply = True
    result.reason = "fallback_no_url_configured"
    return result


# ──────────────────────────────────────────────────────────────────
# 4b. Location / Google Maps Safety Net (May 2026 #36)
# ──────────────────────────────────────────────────────────────────
#
# Mirror of the store-link stack above for **physical-location** /
# Google-Maps questions. The two paths are deliberately kept
# separate even though they share the same skeleton:
#
#   * ``store_url`` answers "where is your *online* shop" — it's an
#     e-commerce CTA URL.
#   * ``maps_url`` answers "where is your *physical* shop" — it's a
#     Google / Apple / Waze maps deep link.
#
# Bug class this safety net closes: a customer asks "وين موقعكم؟"
# and the LLM ships the e-commerce ``store_url`` because that's
# the only deterministic asset it knows how to resolve. The
# maps URL is right there in ``store_settings.google_maps_location``
# and frequently in a free-form KB section under ``kind=branches``
# — but no resolver was looking. This safety net fires only after
# intent detection (so it never overrides a payment / order flow)
# and only when the LLM's reply does NOT already carry a maps URL.

# Phrases that mean "send me the *physical* location URL". Kept
# disjoint from :data:`_STORE_LINK_TRIGGERS_PHRASE` so a single
# inbound message can never fire both nets at once.
_LOCATION_LINK_TRIGGERS_PHRASE: set = {
    # Arabic — direct location asks
    "موقعكم",
    "موقعك",
    "وين موقعكم",
    "أين موقعكم",
    "وين الموقع",
    "أين الموقع",
    "وين موقع",
    "وين مقركم",
    "أين مقركم",
    "مقر شركتكم",
    "وين المحل",
    "أين المحل",
    "وين فرعكم",
    "أين فرعكم",
    "وين الفرع",
    "أين الفرع",
    "عندكم فرع",
    "فروعكم",
    "وين فروعكم",
    "أين فروعكم",
    "أبي أزوركم",
    "أبي أزوركم",
    "أبي أجي للمحل",
    "ابي ازوركم",
    "ابي اجي للمحل",
    "نزور المحل",
    "نزوركم",
    "عنوانكم",
    "عنوان المحل",
    "عنوان الفرع",
    "وين عنوانكم",
    # Arabic — maps phrasings
    "خرايط",
    "الخرايط",
    "خريطة",
    "الخريطة",
    "على الخريطة",
    "على الخرايط",
    "رابط الموقع",
    "رابط موقعكم",
    "رابط موقعك",
    "رابط الخريطة",
    "رابط الخرايط",
    "رابط اللوكيشن",
    "ارسل لي اللوكيشن",
    "أرسل لي اللوكيشن",
    "ابعث اللوكيشن",
    "أبعث اللوكيشن",
    "ابي اللوكيشن",
    "أبي اللوكيشن",
    "ابغى اللوكيشن",
    "أبغى اللوكيشن",
    "لوكيشن",
    "اللوكيشن",
    "لوكيشن المحل",
    "لوكيشن المتجر",
    "لوكيشن الفرع",
    # English — maps phrasings
    "google maps",
    "google map",
    "map link",
    "map url",
    "your location",
    "your address",
    "store location",
    "branch location",
    "physical store",
    "where is your shop",
    "where is your branch",
    "where are you located",
}


# Generic "here is the location" markers — used to decide whether to
# REPLACE the LLM reply (short stub) or APPEND the URL (longer body
# the LLM already wrote). Mirrors the store-link version.
_GENERIC_HERE_IS_THE_LOCATION_MARKERS: tuple = (
    "هذا موقعنا",
    "هذي موقعنا",
    "موقعنا هنا",
    "هذا هو موقعنا",
    "هذا الموقع",
    "تفضل الموقع",
    "تفضل اللوكيشن",
)


# Free-form KB section kinds we sweep for a maps URL when neither
# the snapshot nor TenantSettings has one. ``branches`` is the
# canonical location bucket; we also peek at ``store_story`` and
# ``custom`` because a sizeable share of merchants paste their
# Google Maps link there in early onboarding.
_MAPS_KB_FALLBACK_KINDS: tuple = ("branches", "store_story", "custom")


# Recognised maps host fragments. We require one of these to appear
# in a candidate URL extracted from a KB section so we don't
# accidentally promote a random Salla product link to the maps slot.
_MAPS_HOST_HINTS: tuple = (
    "google.com/maps",
    "google.com.sa/maps",
    "maps.google.",
    "maps.app.goo.gl",
    "goo.gl/maps",
    "apple.com/maps",
    "maps.apple.com",
    "waze.com",
    "what3words.com",
    "what3words.",
    "/maps/",
)


_KB_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass
class LocationLinkSafetyNetResult:
    fired: bool = False
    reason: str = ""
    skipped_reason: str = ""
    rewrote_reply: bool = False
    maps_url: str = ""
    source: str = ""           # snapshot | store_settings | kb:<kind> | none
    new_reply: str = ""

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "kind":             "location_link",
            "fired":            self.fired,
            "reason":           self.reason or self.skipped_reason,
            "rewrote_reply":    self.rewrote_reply,
            "maps_url_present": bool(self.maps_url),
            "source":           self.source,
        }


def _looks_like_location_request(customer_msg: str) -> bool:
    """True when the inbound message is a physical-location ask.

    Sibling of :func:`_looks_like_store_link_request` — uses the same
    normalise + phrase-match strategy so behaviour is predictable.
    The trigger sets are disjoint, so a single message can fire AT
    MOST one of the two nets per turn.
    """
    msg = _normalise_for_match(customer_msg)
    if not msg:
        return False
    msg_compact = re.sub(r"[؟?,،.!:;\-\u060c]+", " ", msg)
    msg_compact = re.sub(r"\s+", " ", msg_compact).strip()
    for phrase in _LOCATION_LINK_TRIGGERS_PHRASE:
        if phrase in msg_compact:
            return True
    return False


def _looks_like_bare_location_intro(reply: str) -> bool:
    """True when the LLM emitted a short generic "here is our location"
    line with no URL — same heuristic as
    :func:`_looks_like_bare_store_intro` but with location markers."""
    if not reply:
        return True
    trimmed = (reply or "").strip()
    short = len(trimmed) <= 60
    norm = _normalise_for_match(trimmed)
    has_marker = any(m in norm for m in _GENERIC_HERE_IS_THE_LOCATION_MARKERS)
    return short and has_marker


def _extract_maps_url_from_text(body: str) -> str:
    """Return the first plausible maps URL in ``body`` or empty string.

    Used by the KB-section fallback layer of the resolver. We
    intentionally do NOT promote any old URL — only those whose
    host segment matches :data:`_MAPS_HOST_HINTS`. This avoids
    accidentally promoting a Salla product link or a YouTube embed
    to the maps slot just because it happens to live in the same KB
    section.
    """
    if not body:
        return ""
    for raw in _KB_URL_RE.findall(body):
        candidate = raw.strip(" \t\n.,،;:)\"]'>")
        low = candidate.lower()
        for hint in _MAPS_HOST_HINTS:
            if hint in low:
                return candidate
    return ""


def _lookup_tenant_maps_url(db: Any, tenant_id: int) -> Tuple[str, str]:
    """Resolve the canonical maps URL for ``tenant_id``.

    Resolution chain (May 2026 #36 — platform-wide maps stack):

      1. ``StoreKnowledgeSnapshot.store_profile["maps_url"]`` —
         the synced source mirrored from
         ``store_settings.google_maps_location`` by
         :func:`services.store_sync._rebuild_snapshot`.
      2. ``TenantSettings.store_settings["google_maps_location"]`` —
         the dashboard's "Store" tab. Fallback for tenants who
         filled the maps slot but whose snapshot has not been
         rebuilt since (or who don't have a snapshot at all
         because they're on the Nahla-native shop without an
         outside integration — see Phase-1 audit, May 2026 #36).
      3. Free-form KB sections in :data:`_MAPS_KB_FALLBACK_KINDS`
         (``branches`` / ``store_story`` / ``custom``). We scan
         their ``body`` for the first URL whose host matches
         :data:`_MAPS_HOST_HINTS`. This closes the gap for
         merchants who never touched a structured field but did
         paste their Google Maps link into the "الفروع" bucket
         in onboarding — the URL is sitting there but no
         resolver was looking.

    Returns a ``(url, source)`` tuple where ``source`` ∈
    ``{"snapshot", "store_settings", "kb:<kind>", "none"}``. Never
    raises; degrade to the next layer on any failure.

    Logs a single ``[MAPS_LINK_RESOLVER]`` INFO line per call so
    production traffic can be audited via grep without enabling
    DEBUG. Mirrors the ``[STORE_LINK_RESOLVER]`` shape.
    """
    if db is None or not tenant_id:
        return "", "none"
    tenant_id = int(tenant_id)

    # ── 1) Synced store profile (StoreKnowledgeSnapshot) ────────────
    try:
        from core.store_knowledge import StoreKnowledgeLoader  # noqa: PLC0415
        loader = StoreKnowledgeLoader(db, tenant_id)
        profile = loader.store_profile() or {}
        url = _normalise_url(profile.get("maps_url"))
        if url:
            logger.info(
                "[MAPS_LINK_RESOLVER] tenant_id=%s source=snapshot url_len=%d",
                tenant_id, len(url),
            )
            return url, "snapshot"
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "safety_nets.maps_link | snapshot lookup failed tenant=%s "
            "err=%s", tenant_id, exc,
        )

    # ── 2) TenantSettings.store_settings.google_maps_location ───────
    try:
        from core.tenant import (  # noqa: PLC0415
            DEFAULT_STORE,
            get_or_create_settings, merge_defaults,
        )
        settings = get_or_create_settings(db, tenant_id)
        store_cfg = merge_defaults(settings.store_settings, DEFAULT_STORE)
        url = _normalise_url(store_cfg.get("google_maps_location"))
        if url:
            logger.info(
                "[MAPS_LINK_RESOLVER] tenant_id=%s source=store_settings "
                "url_len=%d", tenant_id, len(url),
            )
            return url, "store_settings"
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "safety_nets.maps_link | settings lookup failed tenant=%s "
            "err=%s", tenant_id, exc,
        )

    # ── 3) KB free-form sections (branches / store_story / custom) ─
    try:
        from models import MerchantKnowledgeSection  # noqa: PLC0415
        rows = (
            db.query(MerchantKnowledgeSection)
            .filter(
                MerchantKnowledgeSection.tenant_id == tenant_id,
                MerchantKnowledgeSection.is_active.is_(True),
                MerchantKnowledgeSection.kind.in_(_MAPS_KB_FALLBACK_KINDS),
            )
            .order_by(
                MerchantKnowledgeSection.priority.asc(),
                MerchantKnowledgeSection.updated_at.desc(),
            )
            .limit(20)
            .all()
        )
        for row in rows:
            body = getattr(row, "body", "") or ""
            url = _extract_maps_url_from_text(body)
            if not url:
                continue
            url = _normalise_url(url)
            if url:
                logger.info(
                    "[MAPS_LINK_RESOLVER] tenant_id=%s source=kb:%s "
                    "section_id=%s url_len=%d",
                    tenant_id, row.kind, row.id, len(url),
                )
                return url, f"kb:{row.kind}"
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "safety_nets.maps_link | KB lookup failed tenant=%s err=%s",
            tenant_id, exc,
        )

    logger.info(
        "[MAPS_LINK_RESOLVER] tenant_id=%s source=none url_len=0 "
        "reason=no_source_configured",
        tenant_id,
    )
    return "", "none"


# Honest fallback when no maps URL is configured anywhere. We do
# NOT swap in the e-commerce ``store_url`` (the original bug) and
# we do NOT promise to "send the location later" — that would
# trip ``maybe_scrub_unkept_asset_promise``. Instead we ask one
# clarifying question that the merchant can answer manually.
_FALLBACK_NO_MAPS_URL_REPLY_AR = (
    "تأمر أمر 🌷 خبّرنا أي فرع أو مدينة تبحث عنها وسنرسل العنوان "
    "والتفاصيل."
)


def _build_location_reply(maps_url: str) -> str:
    """Canonical Arabic reply when we DO have a maps URL. Kept short
    so WhatsApp renders the link as a tappable preview / lifts it
    into a CTA button."""
    return f"موقعنا 📍\n{maps_url}"


def apply_location_safety_net(
    db: Any,
    *,
    tenant_id: int,
    customer_msg: str,
    reply_text: str,
) -> LocationLinkSafetyNetResult:
    """Guarantee the maps URL lands when the customer asked for it.

    Behaviour matrix mirrors :func:`apply_store_link_safety_net`:

    +-----------------------------+--------------------------------+
    | location intent? | URL in reply? | action                    |
    +==================+===============+============================+
    | yes              | yes (maps)    | no-op (skip)              |
    | yes              | no            | rewrite / append URL      |
    | no               | -             | no-op (skip)              |
    +------------------+---------------+----------------------------+

    "URL in reply" is intentionally STRICT — we only skip when the
    reply contains a URL whose host hints look like a maps URL.
    A reply that contains *only* the e-commerce store URL still
    fires this net so we can append the maps URL; that's the
    exact fix for "وين موقعكم → سترسل رابط المتجر" feedback.
    """
    result = LocationLinkSafetyNetResult()

    if not location_link_net_enabled():
        result.skipped_reason = "flag_disabled"
        return result

    if not _looks_like_location_request(customer_msg or ""):
        result.skipped_reason = "no_location_intent"
        return result
    result.reason = "intent_detected"

    if reply_text:
        # Only skip when the LLM already shipped a *maps* URL — a
        # generic store URL must not block the maps net.
        for raw in _KB_URL_RE.findall(reply_text):
            low = raw.lower()
            if any(h in low for h in _MAPS_HOST_HINTS):
                result.skipped_reason = "maps_url_already_in_reply"
                return result

    maps_url, source = _lookup_tenant_maps_url(db, tenant_id)
    result.maps_url = maps_url
    result.source = source

    if maps_url:
        if _looks_like_bare_location_intro(reply_text or ""):
            result.new_reply = _build_location_reply(maps_url)
        elif reply_text and reply_text.strip():
            sep = "\n" if reply_text.endswith("\n") else "\n\n"
            result.new_reply = (
                reply_text.rstrip() + sep + _build_location_reply(maps_url)
            )
        else:
            result.new_reply = _build_location_reply(maps_url)
        result.fired = True
        result.rewrote_reply = True
        result.reason = f"maps_url_injected:{source}"
        return result

    result.new_reply = _FALLBACK_NO_MAPS_URL_REPLY_AR
    result.fired = True
    result.rewrote_reply = True
    result.reason = "fallback_no_maps_url_configured"
    return result


# ──────────────────────────────────────────────────────────────────
# 5. Clear-Intent Fallback Safety Net
# ──────────────────────────────────────────────────────────────────
#
# Production complaints (May 2026, two attached screenshots):
#
#   Customer: "سلام عليكم هل يوجد عروض على العسل"
#   Bot:      "عذراً، تأخّر الرد قليلاً. هل يمكنك إعادة سؤالك؟
#              أو يمكنني مساعدتك في البحث عن منتج أو إنشاء طلب."
#
# This is the literal LLM-timeout copy in
# ``modules/ai/brain/compose/responder._llm_compose``. It ships
# whenever the upstream Anthropic call exceeds the 15s timeout.
# It's also the generic "I don't understand" fallback the LLM
# sometimes generates when context retrieval failed.
#
# Both cases are embarrassing because the customer's question was
# CRYSTAL CLEAR. The bot just dropped the ball on retrieval and
# blamed the customer for it ("هل يمكنك إعادة سؤالك؟").
#
# This safety net runs AFTER the LLM reply is composed and:
#
#   1. Detects the canonical "ask the customer to repeat" / "I
#      don't understand" / timeout phrases inside the reply.
#   2. Detects whether the customer's message had a CLEAR intent
#      (offers / price / product / store_link / shipping / payment /
#      ordering verb / honey product class).
#   3. When BOTH conditions hold, REPLACES the reply with a short
#      helpful intent-aware reply that keeps the conversation
#      moving forward.
#
# "Repeat your question" remains the right response only for truly
# ambiguous inputs (symbol-only, off-topic, single character) —
# in those cases neither intent detection nor signal scan fires,
# so the safety net stays out of the way.
#
# The net never touches the order / payment / product flows; it
# only rewrites the OUTGOING TEXT when the LLM produced a
# self-deprecating fallback for a clear question.


# Phrases that mean "the bot is asking the customer to repeat /
# is confused / didn't understand". We compare against the
# normalised reply.
_LLM_FALLBACK_MARKERS: tuple = (
    "اعد سؤالك",
    "أعد سؤالك",
    "اعادة سؤالك",
    "إعادة سؤالك",
    "اعيد",
    "تأخر الرد",
    "تأخّر الرد",
    "تاخر الرد",
    "تأخّر الرّد",
    "تاخر الرّد",
    "لم افهم",
    "لم أفهم",
    "ما فهمت",
    "وضح اكثر",
    "وضح أكثر",
    "وضحي اكثر",
    "وضحي أكثر",
    "ممكن توضح اكثر",
    "ممكن توضح أكثر",
    "ممكن توضحي اكثر",
    "ممكن توضحي أكثر",
    "اعطني تفاصيل اكثر",
    "أعطني تفاصيل أكثر",
    "ممكن تعيد",
    "ممكن تعيدي",
    # English equivalents that the LLM sometimes emits.
    "could you repeat",
    "could you rephrase",
    "i didn't understand",
    "i don't understand",
    "please rephrase",
    "please repeat",
)


# Intent → canonical safe rewrite. The replies are intentionally
# SHORT, calm, and stop short of inventing prices / promo details
# the bot may not actually have. Each opens a productive next
# step ("تحب أعرض لك الأنواع والأسعار؟") so the customer doesn't
# feel dismissed.
_INTENT_OFFERS = "offers"
_INTENT_PRICE = "price"
_INTENT_PRODUCT = "product"
_INTENT_STORE_LINK = "store_link"
_INTENT_SHIPPING = "shipping"
_INTENT_PAYMENT = "payment"
_INTENT_ORDER = "order"

# Trigger lexicons. Each tuple member is a NORMALISED Arabic /
# Latin substring. We match on the normalised inbound message
# (lower + Arabic-diacritic strip).
#
# Priority order is intentional: the FIRST hit wins, so we list
# specific-action intents (store_link / offers / order / payment /
# shipping / price) BEFORE the broad "product noun" bucket.
# Without this, "ودي اطلب كيلو عسل" would be classified as
# ``product`` because of "عسل" — but the customer's actual intent
# is to PLACE AN ORDER.
_CLEAR_INTENT_LEXICON: Dict[str, tuple] = {
    _INTENT_STORE_LINK: (
        # NOTE (May 2026 #36): "رابط الموقع" / "موقعكم" used to live
        # here, which let the clear-intent fallback substitute the
        # e-commerce store URL for physical-location asks. The
        # location safety net (``apply_location_safety_net``) handles
        # those phrasings now; this lexicon is intentionally narrowed
        # to UNAMBIGUOUS online-store mentions.
        "رابط المتجر", "اللينك", "المتجر الالكتروني",
        "store link", "store url", "website link",
    ),
    _INTENT_OFFERS: (
        # NOTE: we deliberately do NOT include the singular "عرض"
        # — it collides with the imperative verb "اعرض/اعرضي"
        # (display/show) which is a PRODUCT-browse signal, not an
        # offers question. The plural "عروض" and the unambiguous
        # discount nouns below cover the real "any promotions?"
        # phrasings without false positives.
        "عروض", "العروض", "تخفيض", "تخفيضات", "خصم", "خصومات",
        "كوبون", "كوبونات", "كود خصم", "كود تخفيض",
        "promo", "promotion", "discount", "offer", "sale",
    ),
    _INTENT_ORDER: (
        "اطلب", "أطلب", "ابي اطلب", "أبي أطلب", "ودي اطلب",
        "احجز", "أحجز", "اشتري", "أشتري", "ابغى اشتري",
        "اسوي طلب", "أسوي طلب", "انشاء طلب", "إنشاء طلب",
        "i want to order", "place order", "buy", "purchase",
    ),
    _INTENT_PAYMENT: (
        "وسائل الدفع", "طرق الدفع", "كيف ادفع", "كيف أدفع",
        "حساب بنكي", "ايبان", "آيبان", "تحويل بنكي",
        "payment", "iban", "bank transfer",
    ),
    _INTENT_SHIPPING: (
        "شحن", "الشحن", "توصيل", "التوصيل", "كم ياخذ التوصيل",
        "متى يوصل", "shipping", "delivery",
    ),
    _INTENT_PRICE: (
        "سعر", "اسعار", "أسعار", "كم سعر", "كم يكلف", "بكم",
        "price", "prices", "how much",
    ),
    _INTENT_PRODUCT: (
        # MULTI-TENANT NOTE: this is a CROSS-MERCHANT safety net, so
        # we do NOT hard-code honey / perfume / electronics nouns
        # here. The merchant brain (with its catalogue + KB) is the
        # only layer that knows what the tenant actually sells.
        #
        # This lexicon catches the GENERIC product-discovery
        # phrasings that work for any merchant:
        #   • "إيش عندكم؟" / "وش المتوفر" / "اعرض المنتجات"
        #   • "ابي منتج / صنف / نوع"
        #   • "كتالوج / لائحة المنتجات"
        # When any of these match AND the LLM produced a generic
        # fallback, the safety net rewrites with a polite "تحب أعرض
        # لك المتوفر والأسعار؟" line that opens the catalogue
        # without naming a category.
        "منتج", "منتجات", "المنتج", "المنتجات",
        "بضاعه", "بضاعة", "البضاعه", "البضاعة",
        "صنف", "صنوف", "الصنف", "اصناف", "أصناف", "الاصناف", "الأصناف",
        "نوع", "انواع", "أنواع", "الانواع", "الأنواع", "النوع",
        "موديل", "موديلات", "ماركه", "ماركة", "ماركات",
        "كتالوج", "الكتالوج", "كاتالوج", "كاتلوج",
        "لائحه", "لائحة", "قائمه", "قائمة", "لستة", "ليستة",
        "متوفر", "المتوفر", "متوفره", "متوفرة", "متوفرات",
        "موجود", "الموجود", "موجوده", "موجودة",
        "ايش عندكم", "إيش عندكم", "وش عندكم",
        "ايش المتوفر", "إيش المتوفر", "وش المتوفر",
        "ايش متوفر", "إيش متوفر", "وش متوفر",
        "اعرض لي", "أعرض لي", "اعرضي لي", "أعرضي لي",
        "ورني", "وريني", "اوريني", "أوريني",
        "ايش عندك", "إيش عندك", "وش عندك",
        # English equivalents.
        "product", "products", "catalog", "catalogue",
        "what do you have", "what do you sell", "show me",
        "available", "in stock",
    ),
}


# Canonical replies — picked to be informative without inventing
# data and WITHOUT naming a product class (this module runs for
# ALL merchants — honey, perfume, electronics, clothing, food …).
#
# Each line uses a generic "المتوفر / المنتجات" wording so the
# next turn (when the LLM / retrieval recovers) can deliver the
# tenant-specific catalogue without contradicting the safety
# net's reply. We deliberately AVOID promising a specific
# discount, price, payment method, or shipping cost — those
# numbers must come from the merchant's KB / catalogue, not from
# this fallback.
_CLEAR_INTENT_REPLIES: Dict[str, str] = {
    _INTENT_OFFERS: (
        "أبشر 🌷 لو فيه عروض حالية أعرضها لك مباشرة — "
        "تحب أعرض لك المتوفر والأسعار؟"
    ),
    _INTENT_PRICE: (
        "تحت أمرك 🌷 لو تخبرني المنتج المحدد أعرض لك السعر الحالي مباشرة."
    ),
    _INTENT_PRODUCT: (
        "أبشر 🌷 تحب أعرض لك المنتجات المتوفرة والأسعار؟"
    ),
    _INTENT_STORE_LINK: (
        # The dedicated store-link safety net handles the URL
        # injection itself. Here we only supply a fallback line
        # for the rare case it's disabled.
        "أبشر 🌷 أرسل لك رابط المتجر الحين."
    ),
    _INTENT_SHIPPING: (
        "أبشر 🌷 التوصيل متاح حسب موقعك — لو تخبرني المدينة "
        "أرتب لك الشحن."
    ),
    _INTENT_PAYMENT: (
        "أبشر 🌷 وسائل الدفع متاحة. تحب أرسل لك طرق الدفع المتوفرة؟"
    ),
    _INTENT_ORDER: (
        "أبشر 🌷 لو تخبرني المنتج المطلوب أبدأ معك الطلب فورًا."
    ),
}


@dataclass
class ClearIntentFallbackResult:
    fired: bool = False
    reason: str = ""
    skipped_reason: str = ""
    customer_intent: str = ""
    new_reply: str = ""

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "kind":            "clear_intent_fallback",
            "fired":           self.fired,
            "reason":          self.reason or self.skipped_reason,
            "customer_intent": self.customer_intent,
        }


def _detect_clear_customer_intent(customer_msg_norm: str) -> str:
    """Return the FIRST matching intent label, or empty string.

    Priority is the order of ``_CLEAR_INTENT_LEXICON`` keys. We
    deliberately stop at the first hit — if the customer says
    "عروض على العسل" we want "offers" (more actionable than
    "product").
    """
    if not customer_msg_norm:
        return ""
    for intent_name, triggers in _CLEAR_INTENT_LEXICON.items():
        for t in triggers:
            if t and t in customer_msg_norm:
                return intent_name
    return ""


def _reply_looks_like_generic_fallback(reply_norm: str) -> bool:
    if not reply_norm:
        return False
    return any(m in reply_norm for m in _LLM_FALLBACK_MARKERS)


def apply_clear_intent_fallback_net(
    *,
    customer_msg: str,
    reply_text: str,
) -> ClearIntentFallbackResult:
    """Replace a "please repeat" / timeout-apology reply with a
    short intent-aware reply when the customer's message was
    clearly understandable.

    The net is text-only. It NEVER mutates order state, never
    invents prices, and never touches the marker / attachment
    pipelines. When fired, the caller substitutes ``new_reply``
    for the outbound text.

    Returns ``ClearIntentFallbackResult`` with ``fired=True`` and
    ``new_reply`` populated when the rewrite should happen.
    """
    result = ClearIntentFallbackResult()

    if not clear_intent_fallback_net_enabled():
        result.skipped_reason = "flag_disabled"
        return result

    if not reply_text or not reply_text.strip():
        result.skipped_reason = "empty_reply"
        return result

    reply_norm = _normalise_for_match(reply_text)
    if not _reply_looks_like_generic_fallback(reply_norm):
        result.skipped_reason = "reply_not_generic_fallback"
        return result
    result.reason = "reply_asked_to_repeat_clear_question"

    msg_norm = _normalise_for_match(customer_msg or "")
    if not msg_norm:
        result.skipped_reason = "empty_customer_msg"
        return result

    intent = _detect_clear_customer_intent(msg_norm)
    if not intent:
        # No clear intent → the original "please repeat" reply is
        # appropriate. Stay out of the way.
        result.skipped_reason = "no_clear_intent"
        return result

    result.customer_intent = intent
    new_reply = _CLEAR_INTENT_REPLIES.get(intent)
    if not new_reply:
        result.skipped_reason = "no_template_for_intent"
        return result

    result.new_reply = new_reply
    result.fired = True
    return result


# ──────────────────────────────────────────────────────────────────
# 6. Delivery-Info Context-Aware Safety Net
# ──────────────────────────────────────────────────────────────────
#
# Production complaint (April 2026 screenshot):
#
#   Bot:      "ممكن ترسل لي عنوان الشحن أو المدينة عشان نرتب لك
#              التوصيل؟"
#   Customer: "خالد محيل صالح الحربي
#              0552375813
#              المدينة المنورة
#              الحمراء حي الصناعية قرط بن ربيعة
#              رقم المبنى ٤٣٦٥"
#   Bot:      "أعتذر، هذا خارج تخصصي. لو تحب أساعدك في شي يخص
#              العسل أو الطلب، أنا جاهزة 🌷"      ← out_of_scope!
#
# Architectural fix: BEFORE classifying a message as out_of_scope
# (or shipping any generic "outside my scope" line), we must check
# the conversation state. If the bot's last outbound asked for
# delivery info (address, city, name, phone, …) and the customer's
# new message contains ANY delivery signal, treat it as
# ``delivery_info_response`` and acknowledge — even if the
# decision engine routed the turn to out_of_scope.
#
# We do NOT mutate the order state in this safety net (that's the
# decision engine's job on the NEXT turn — we just unblock the
# conversation). The next inbound (after the customer sees our
# ACK) flows through the normal pipeline with the right state.
#
# Implementation uses the existing deterministic
# ``modules.ai.brain.intent.ordering_extractor.extract_ordering_slots``
# helper so we don't duplicate Arabic-name / city / address-code
# detection.


# Markers in the bot's most-recent outbound that mean "I am
# waiting for delivery info from the customer".
_BOT_AWAITING_DELIVERY_MARKERS: tuple = (
    "عنوان الشحن",
    "عنوان التوصيل",
    "العنوان الوطني",
    "عنوانك",
    "عنوانكم",
    "موقعك",
    "موقعكم",
    "بيانات التوصيل",
    "بيانات الشحن",
    "بيانات التوصيله",
    "بيانات الشحنه",
    "اين تحب التوصيل",
    "أين تحب التوصيل",
    "وين تحب التوصيل",
    "وين تبي نوصلك",
    "ايش المدينة",
    "أي مدينة",
    "وش المدينة",
    "ارسل المدينة",
    "أرسل المدينة",
    "ارسل العنوان",
    "أرسل العنوان",
    "ابعث العنوان",
    "أبعث العنوان",
    "ارسل لي عنوان",
    "أرسل لي عنوان",
    "ارسل لي عنوانك",
    "أرسل لي عنوانك",
    "ارسل لي العنوان",
    "أرسل لي العنوان",
    "ابعث لي العنوان",
    "ابعث لي عنوانك",
    "ابعث لي موقعك",
    "ارسل لي موقعك",
    "أرسل لي موقعك",
    "الاسم والجوال",
    "الاسم الكامل",
    "اسمك الكريم",
    "اسمك ورقم جوالك",
    "اسمك ورقم الجوال",
    "ايش الحي",
    "أي حي",
    "وش الحي",
    "نرتب لك التوصيل",
    "اكمل الطلب",
    "إكمال الطلب",
    "نكمل الطلب",
)


# Markers in the bot's CURRENT reply that mean "I'm dismissing
# this as out-of-scope / I didn't understand". When the bot was
# clearly awaiting delivery info, ANY of these on a message that
# contains delivery signals = embarrassment → rewrite.
_REPLY_LOOKS_DISMISSIVE_MARKERS: tuple = (
    "خارج تخصصي",
    "خارج نطاق",
    "ما اقدر اساعدك",
    "ما أقدر أساعدك",
    "ما يخص العسل",
    "في شي يخص العسل",
    "لو تحب اساعدك",
    "لو تحب أساعدك",
    "هذا خارج",
    "خارج النطاق",
    # "Please repeat" markers — also dismissive when we're awaiting
    # delivery data and the customer just provided it.
    "اعد سؤالك",
    "أعد سؤالك",
    "اعادة سؤالك",
    "إعادة سؤالك",
    "لم افهم",
    "لم أفهم",
    "ما فهمت",
    # Generic fallback we also catch elsewhere.
    "تأخر الرد",
    "تأخّر الرد",
    "تاخر الرد",
)


# Saudi mobile / phone pattern — used as one of the delivery
# signals so a "0552375813" alone (or with a name) counts as
# delivery info.
_SAUDI_PHONE_RE = re.compile(
    r"\b(?:\+?9665\d{8}|009665\d{8}|05\d{8}|5\d{8})\b"
)


@dataclass
class DeliveryInfoContextResult:
    fired: bool = False
    reason: str = ""
    skipped_reason: str = ""
    extracted_slots: Dict[str, Any] = field(default_factory=dict)
    has_phone: bool = False
    new_reply: str = ""

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "kind":            "delivery_info_context",
            "fired":           self.fired,
            "reason":          self.reason or self.skipped_reason,
            "slots_extracted": sorted(self.extracted_slots.keys()),
            "has_phone":       self.has_phone,
        }


_DELIVERY_INFO_ACK_FULL = (
    "وصلتني بيانات التوصيل 🌷 جاري ترتيب الطلب بإذن الله."
)

# Friendly per-missing-field nudges. The bot already knows what
# the customer JUST sent — we only ask for the field that's still
# missing so the conversation doesn't loop on the data the
# customer already provided.
_DELIVERY_INFO_ACK_PARTIAL_NUDGES: Dict[str, str] = {
    "customer_name":   "وصلتني المعلومات 🌷 ينقص الاسم — ممكن ترسل لي اسمك الكامل؟",
    "phone":           "وصلتني المعلومات 🌷 ينقص رقم الجوال — ممكن ترسل لي رقم جوالك؟",
    "city":            "وصلتني المعلومات 🌷 ينقص اسم المدينة — ممكن ترسل لي المدينة؟",
    "address":         "وصلتني المعلومات 🌷 ينقص العنوان التفصيلي أو العنوان الوطني — ممكن ترسل لي العنوان؟",
}


def _bot_was_awaiting_delivery(history: Optional[List[Dict[str, Any]]]) -> bool:
    """True when the last 1-2 outbound messages contain markers
    that mean "I'm waiting for delivery info from the customer".

    We look at the LAST few outbound turns (not just the very
    last) because the bot sometimes ships a card or follow-up
    line after the address question that pushes the original
    question one slot back.
    """
    if not history:
        return False
    outbound_seen = 0
    try:
        for turn in reversed(history):
            direction = str((turn or {}).get("direction") or "").lower()
            if direction not in ("out", "outbound"):
                continue
            outbound_seen += 1
            body = str((turn or {}).get("body") or "")
            if not body:
                if outbound_seen >= 3:
                    break
                continue
            norm = _normalise_for_match(body)
            if any(m in norm for m in _BOT_AWAITING_DELIVERY_MARKERS):
                return True
            if outbound_seen >= 3:
                break
    except Exception:  # noqa: BLE001
        return False
    return False


def _reply_looks_dismissive(reply_text: str) -> bool:
    if not reply_text:
        return False
    norm = _normalise_for_match(reply_text)
    return any(m in norm for m in _REPLY_LOOKS_DISMISSIVE_MARKERS)


def _extract_delivery_signals(customer_msg: str) -> Dict[str, Any]:
    """Run the deterministic ordering-slot extractor + a phone
    scan and return whatever delivery-flavoured fields we got.
    Empty dict when nothing matched. Never raises."""
    slots: Dict[str, Any] = {}
    if not customer_msg:
        return slots
    # Phone is checked here (not in ordering_extractor) since the
    # latter focuses on name/city/address.
    phone_match = _SAUDI_PHONE_RE.search(customer_msg)
    if phone_match:
        slots["phone"] = phone_match.group(0)
    try:
        from modules.ai.brain.intent.ordering_extractor import (  # noqa: PLC0415
            extract_ordering_slots,
        )
        ord_slots = extract_ordering_slots(customer_msg) or {}
        # Only carry over keys we'd recognise as delivery-related.
        for k in (
            "customer_name", "customer_first_name", "customer_last_name",
            "city", "short_address_code", "google_maps_url",
            "latitude", "longitude",
            "street", "district", "building_number",
            "additional_number", "postal_code", "address_line",
        ):
            v = ord_slots.get(k)
            if v not in (None, "", {}, []):
                slots[k] = v
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "safety_nets.delivery_info | extract_ordering_slots failed: %s",
            exc,
        )
    return slots


def _compose_delivery_info_ack(slots: Dict[str, Any]) -> str:
    """Decide between the "got everything" ACK and a "still need
    X" nudge. We treat the customer as "complete enough" when
    they sent (name OR phone) AND (city OR address signal). This
    is intentionally generous — the next turn (with the real
    order pipeline) will validate the precise shape; we just
    need to STOP the dismissive reply."""
    has_name = bool(
        slots.get("customer_name")
        or slots.get("customer_first_name")
        or slots.get("customer_last_name")
    )
    has_phone = bool(slots.get("phone"))
    has_city = bool(slots.get("city"))
    has_address = bool(
        slots.get("short_address_code")
        or slots.get("google_maps_url")
        or slots.get("address_line")
        or slots.get("street")
        or slots.get("building_number")
        or (slots.get("latitude") is not None and slots.get("longitude") is not None)
    )

    # "Good enough" — full ACK that doesn't promise shipping yet,
    # only acknowledges receipt of the data.
    if (has_name or has_phone) and (has_city or has_address):
        return _DELIVERY_INFO_ACK_FULL

    # Partial — nudge for the most important missing piece.
    if not has_name:
        return _DELIVERY_INFO_ACK_PARTIAL_NUDGES["customer_name"]
    if not has_phone and not has_address:
        return _DELIVERY_INFO_ACK_PARTIAL_NUDGES["phone"]
    if not has_city:
        return _DELIVERY_INFO_ACK_PARTIAL_NUDGES["city"]
    if not has_address:
        return _DELIVERY_INFO_ACK_PARTIAL_NUDGES["address"]
    return _DELIVERY_INFO_ACK_FULL


def apply_delivery_info_context_net(
    *,
    customer_msg: str,
    reply_text: str,
    history: Optional[List[Dict[str, Any]]] = None,
) -> DeliveryInfoContextResult:
    """When the bot was clearly waiting for delivery info AND the
    customer's message contains any delivery signal AND the LLM
    replied dismissively (out_of_scope / "didn't understand"),
    rewrite the reply with an acknowledgement.

    This is the architectural fix for the
    "customer typed full address → bot said 'outside my scope'"
    bug. Pure text rewrite — no state mutation, no order flow
    side-effects. The decision engine handles the actual order
    progression on the NEXT inbound (where state is now correctly
    populated by the regular slot extractor).
    """
    result = DeliveryInfoContextResult()

    if not delivery_info_context_net_enabled():
        result.skipped_reason = "flag_disabled"
        return result

    if not _bot_was_awaiting_delivery(history):
        result.skipped_reason = "bot_not_awaiting_delivery"
        return result
    result.reason = "bot_was_awaiting_delivery_info"

    if not _reply_looks_dismissive(reply_text):
        # Bot was waiting, but its reply isn't dismissive →
        # leave the LLM's reply alone (it may be doing the right
        # thing already, e.g. acknowledging the partial address).
        result.skipped_reason = "reply_not_dismissive"
        return result

    slots = _extract_delivery_signals(customer_msg or "")
    if not slots:
        result.skipped_reason = "no_delivery_signals_in_msg"
        return result

    result.extracted_slots = slots
    result.has_phone = bool(slots.get("phone"))
    result.new_reply = _compose_delivery_info_ack(slots)
    result.fired = True
    return result


__all__ = [
    "ProductSafetyNetResult",
    "MediaKeySafetyNetResult",
    "StaffContactSafetyNetResult",
    "StoreLinkSafetyNetResult",
    "LocationLinkSafetyNetResult",
    "ClearIntentFallbackResult",
    "DeliveryInfoContextResult",
    "apply_product_safety_net",
    "apply_media_key_safety_net",
    "apply_staff_contact_safety_net",
    "apply_store_link_safety_net",
    "apply_location_safety_net",
    "apply_clear_intent_fallback_net",
    "apply_delivery_info_context_net",
    "product_net_enabled",
    "media_key_net_enabled",
    "staff_contact_net_enabled",
    "store_link_net_enabled",
    "location_link_net_enabled",
    "clear_intent_fallback_net_enabled",
    "delivery_info_context_net_enabled",
]
