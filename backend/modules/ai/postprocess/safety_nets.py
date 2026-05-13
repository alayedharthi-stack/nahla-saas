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
from typing import Any, Dict, List, Optional

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


__all__ = [
    "ProductSafetyNetResult",
    "MediaKeySafetyNetResult",
    "StaffContactSafetyNetResult",
    "apply_product_safety_net",
    "apply_media_key_safety_net",
    "apply_staff_contact_safety_net",
    "product_net_enabled",
    "media_key_net_enabled",
    "staff_contact_net_enabled",
]
