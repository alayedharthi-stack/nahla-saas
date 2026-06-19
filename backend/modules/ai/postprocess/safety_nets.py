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
    brain_state: Optional[Dict[str, Any]] = None,
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

    lookup_query = customer_msg or ""
    try:
        from modules.ai.brain.commerce.product_visual import (  # noqa: PLC0415
            attachment_matches_turn_request,
            extract_visual_product_query,
            is_deictic_visual_request,
        )
        explicit = extract_visual_product_query(customer_msg or "")
        if explicit:
            lookup_query = explicit
        elif is_deictic_visual_request(customer_msg or ""):
            focus = (brain_state or {}).get("current_product_focus") or {}
            focus_title = str(focus.get("title") or "").strip()
            if not focus_title:
                result.skipped_reason = "deictic_no_focus"
                return result
            lookup_query = focus_title
    except Exception:  # noqa: BLE001
        pass

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
            db, tenant_id, lookup_query or "",
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

    try:
        from modules.ai.brain.commerce.product_visual import (  # noqa: PLC0415
            attachment_matches_turn_request,
        )
        _ok, _why = attachment_matches_turn_request(
            inbound_message=customer_msg or "",
            attachment_title=str(resolution.title or ""),
            brain_state=brain_state,
        )
        if not _ok:
            result.skipped_reason = f"turn_mismatch:{_why}"
            return result
    except Exception:  # noqa: BLE001
        pass

    # Build the SAME attachment shape the existing product-marker
    # pipeline produces so the downstream sender code is one branch.
    attachment = {
        "kind":         "product_card",
        "id":           resolution.id,
        "title":        resolution.title,
        "media_type":   "image",
        "file_url":     resolution.image_url,
        "caption":      _caption(resolution, include_description=False),
        "product_url":  resolution.product_url,
        "price":        resolution.price,
        "in_stock":     resolution.in_stock,
        "external_id":  resolution.external_id,
        "confidence":   resolution.confidence,
        "safety_net":   True,
        "dispatch_source": "safety_net",
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
    inbound_metadata: Optional[Dict[str, Any]] = None,
    normalized_type: Optional[str] = None,
    conversation_id: Optional[int] = None,
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

    try:
        from modules.ai.brain.commerce.customer_origin_intent import (  # noqa: PLC0415
            customer_origin_has_payment_request,
            emit_payment_intent_telemetry,
            is_payment_media_key,
            split_inbound_text,
        )
        _split = split_inbound_text(
            customer_msg or "",
            inbound_metadata=inbound_metadata,
            normalized_type=normalized_type,
        )
        _origin = _split.customer_origin
    except Exception:  # noqa: BLE001
        _split = None
        _origin = (customer_msg or "").strip()

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

    if not (customer_msg or "").strip() and not (_origin or "").strip():
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

    # Payment media keys require customer-origin intent — never OCR alone.
    _probe_resolution, _probe_key = _resolve_media(db, tenant_id, customer_msg or "")
    if _probe_key and is_payment_media_key(_probe_key):
        if not customer_origin_has_payment_request(
            _origin,
            inbound_metadata=inbound_metadata,
            normalized_type=normalized_type,
        ):
            if _split is not None:
                emit_payment_intent_telemetry(
                    tenant_id=tenant_id,
                    route="media_key_safety_net",
                    split=_split,
                    allow_outbound=False,
                    reason="no_customer_origin_payment_intent",
                    conversation_id=conversation_id,
                )
            result.skipped_reason = "no_customer_origin_payment_intent"
            return result

    try:
        resolution, inferred = _resolve_media(db, tenant_id, _origin or "")
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

# Staff alias tokens are loaded per-tenant from KB evidence — see
# :func:`_staff_alias_candidates`.


# KB-section kinds we sweep when the LLM omitted the phone but the
# merchant has a name+phone pair sitting in free-form knowledge.
# We deliberately stay away from BEHAVIORAL_KINDS like ``response_tone``
# / ``forbidden_phrases`` (those don't carry contact directories) and
# product-bound kinds. After the structured KB rollout merchants started
# pasting "للتواصل المباشر: 05…" into commerce sections (payment_method,
# bank_transfer, working_hours, cod, shipping_*) so the resolver must
# scan those too — otherwise the migration silently lost contacts.
_STAFF_KB_FALLBACK_KINDS: tuple = (
    "branches",
    "store_story",
    "owner_identity",
    "quick_update",
    "custom",
    "faq",
    "escalation_rules",
    "payment_method",
    "bank_transfer",
    "cod",
    "working_hours",
    "shipping_carrier",
    "shipping_zones",
    "cold_shipping",
    "summer_note",
    "return_policy",
    "warranty",
    "reply_style",
    "dialect",
)


# Title prefixes the dashboard auto-generates for "improvement
# suggestion" cards — these read like data ("استكمال أرقام التواصل"
# / "أضف باركود التحويل") but are in fact prompts asking the
# merchant to add data, often containing example digits or
# placeholder text. Tenant 33 #38d trace showed sections 122-126,
# 136, 139 of this shape contributing 0 actual contacts but
# inflating the [STAFF_CONTACT_GRAPH] pair count and pushing the
# resolver to chase ghost names. We strip them at scan time so
# both the resolver and the graph trace reflect ACTUAL data.
_KB_SUGGESTION_TITLE_PREFIXES: tuple = (
    "أضف",
    "اضف",
    "أضيفي",
    "اضيفي",
    "إضافة",
    "اضافة",
    "استكمال",
    "تحسين",
    "تحديث",
    "حسّن",
    "حسن",
    "اقترح",
)


def _is_dashboard_suggestion_section(row: Any) -> bool:
    """Return True for sections whose title looks like a dashboard
    improvement-suggestion card rather than real merchant data.

    The check is title-prefix only — body text is sometimes the
    same Arabic verb in genuine content (e.g. "أضف العسل للحليب"
    in a usage tip), but improvement cards always START their
    title with one of the suggestion verbs. Keeping the check
    title-only avoids false-positives on legitimate prose.
    """
    title = str(getattr(row, "title", "") or "").strip()
    if not title:
        return False
    folded = _normalise_alif(title).lower()
    for prefix in _KB_SUGGESTION_TITLE_PREFIXES:
        if folded.startswith(_normalise_alif(prefix).lower()):
            return True
    return False


# Window (in characters) around a name match to consider for the
# nearest phone digits. Tight enough to avoid pairing the wrong
# number ("Manager 050... | Driver 053...") and wide enough to
# bridge a phone that lives a couple of bullet points away from
# the name.
#
# May 2026 #38 (post-D2 follow-up): bumped from 80 → 220 chars
# after the live trace showed أمين's name and phone landed in
# the same KB section but separated by a short paragraph
# (~120 chars). The narrower window made the resolver miss
# "بائع المعرض: أمين\n\nالتواصل المباشر: 0541690226" — exactly
# the merchant's setup. 220 chars covers the typical
# "name → role → contact" prose layout while still being tight
# enough to keep two distinct staff members in distinct windows
# (their entries are usually on separate KB sections, not glued
# into one paragraph).
_STAFF_KB_PROXIMITY_WINDOW = 220


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


def strip_embedded_phones_from_reply(reply_text: str) -> str:
    """Remove plain-text phone numbers when a vCard will carry the contact."""
    cleaned = reply_text or ""
    phones = _extract_phones(cleaned)
    if not phones:
        return cleaned
    for ph in phones:
        cleaned = cleaned.replace(ph, "")
    cleaned = re.sub(r"\s*على\s*$", "", cleaned.strip())
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned or (reply_text or "")


def _kb_contact_labels_only(db: Any, tenant_id: int) -> List[str]:
    """Full contact labels from KB ``label:phone`` lines (graph telemetry)."""
    if db is None or not tenant_id:
        return []
    _label_re = re.compile(
        r"^(.{2,48}?)\s*[:：\-–—]\s*(\+?\s*966?\s*5\d{8}|05\d{8}|5\d{8})\s*$",
        re.MULTILINE | re.UNICODE,
    )
    labels: List[str] = []
    try:
        from models import MerchantKnowledgeSection  # noqa: PLC0415
        from core.knowledge import apply_ai_visible_kb_query_filters  # noqa: PLC0415

        rows = (
            apply_ai_visible_kb_query_filters(
                db.query(MerchantKnowledgeSection)
            )
            .filter(
                MerchantKnowledgeSection.tenant_id == tenant_id,
                MerchantKnowledgeSection.kind.in_(_STAFF_KB_FALLBACK_KINDS),
            )
            .limit(40)
            .all()
        )
        for row in rows:
            if _is_dashboard_suggestion_section(row):
                continue
            body = getattr(row, "body", "") or ""
            for line in body.splitlines():
                m = _label_re.match(line.strip())
                if not m:
                    continue
                label = _normalise_for_match(m.group(1))
                if label and label not in labels:
                    labels.append(label)
    except Exception:  # noqa: BLE001
        return []
    return labels


def _kb_alias_tokens(db: Any, tenant_id: int) -> List[str]:
    """Extract contact labels from KB ``label:phone`` lines."""
    if db is None or not tenant_id:
        return []
    _label_re = re.compile(
        r"^(.{2,48}?)\s*[:：\-–—]\s*(\+?\s*966?\s*5\d{8}|05\d{8}|5\d{8})\s*$",
        re.MULTILINE | re.UNICODE,
    )
    tokens: List[str] = []
    try:
        from models import MerchantKnowledgeSection  # noqa: PLC0415
        from core.knowledge import apply_ai_visible_kb_query_filters  # noqa: PLC0415

        rows = (
            apply_ai_visible_kb_query_filters(
                db.query(MerchantKnowledgeSection)
            )
            .filter(
                MerchantKnowledgeSection.tenant_id == tenant_id,
                MerchantKnowledgeSection.kind.in_(_STAFF_KB_FALLBACK_KINDS),
            )
            .order_by(
                MerchantKnowledgeSection.priority.asc(),
                MerchantKnowledgeSection.updated_at.desc(),
            )
            .limit(40)
            .all()
        )
        for row in rows:
            if _is_dashboard_suggestion_section(row):
                continue
            body = getattr(row, "body", "") or ""
            for line in body.splitlines():
                m = _label_re.match(line.strip())
                if not m:
                    continue
                label = _normalise_for_match(m.group(1))
                if label and label not in tokens:
                    tokens.append(label)
                for part in label.split():
                    part_norm = _normalise_for_match(part)
                    if len(part_norm) >= 2 and part_norm not in tokens:
                        tokens.append(part_norm)
            body_norm = _normalise_alif(body).lower()
            for pat in _PHONE_REGEXES:
                for m in pat.finditer(body):
                    label = _extract_label_near_phone(body, m.start())
                    if label:
                        norm_label = _normalise_for_match(label)
                        if norm_label and norm_label not in tokens:
                            tokens.append(norm_label)
    except Exception:  # noqa: BLE001
        return []
    tokens.sort(key=len, reverse=True)
    return tokens


def _extract_label_near_phone(body: str, phone_start: int) -> str:
    line_start = body.rfind("\n", 0, phone_start) + 1
    line_end = body.find("\n", phone_start)
    if line_end < 0:
        line_end = len(body)
    line = body[line_start:line_end].strip()
    _label_re = re.compile(
        r"^(.{2,48}?)\s*[:：\-–—]\s*(\+?\s*966?\s*5\d{8}|05\d{8}|5\d{8})\s*$",
    )
    m = _label_re.match(line)
    if m:
        return m.group(1).strip()
    window = body[max(0, phone_start - 48):phone_start]
    window = re.sub(r"[\d+()\s\-]+$", "", window).strip()
    if window:
        parts = re.split(r"[:：\-–—]", window)
        if parts:
            return parts[-1].strip()
    return ""


def _staff_alias_candidates(db: Any, tenant_id: int) -> List[str]:
    """Return longest-first alias tokens configured for this tenant."""
    if db is None or not tenant_id:
        return []
    tokens: List[str] = []
    try:
        from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
            load_staff_contact_registry,
        )

        registry = load_staff_contact_registry(db, int(tenant_id))
        for rec in registry.records:
            if rec.is_owner:
                continue
            for token in rec.all_match_tokens():
                if token not in tokens:
                    tokens.append(token)
    except Exception:  # noqa: BLE001
        pass
    for token in _kb_alias_tokens(db, tenant_id):
        if token not in tokens:
            tokens.append(token)
    tokens.sort(key=len, reverse=True)
    return tokens


def _name_appears_in_kb_body(db: Any, tenant_id: int, name: str) -> bool:
    if not name or db is None or not tenant_id:
        return False
    target = _normalise_alif(name).lower()
    if not target:
        return False
    try:
        from models import MerchantKnowledgeSection  # noqa: PLC0415
        from core.knowledge import apply_ai_visible_kb_query_filters  # noqa: PLC0415

        rows = (
            apply_ai_visible_kb_query_filters(
                db.query(MerchantKnowledgeSection)
            )
            .filter(
                MerchantKnowledgeSection.tenant_id == tenant_id,
                MerchantKnowledgeSection.kind.in_(_STAFF_KB_FALLBACK_KINDS),
            )
            .limit(40)
            .all()
        )
        for row in rows:
            body = getattr(row, "body", "") or ""
            if target in _normalise_alif(body).lower():
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _evidence_backed_name_from_message(
    db: Any,
    tenant_id: int,
    msg_norm: str,
) -> str:
    """Extract a staff name from the inbound only when KB mentions it."""
    if not msg_norm or db is None or not tenant_id:
        return ""
    patterns = (
        r"رقم\s+([^\s]+)",
        r"اكلم\s+([^\s]+)",
        r"أكلم\s+([^\s]+)",
        r"اتصل\s+([^\s]+)",
        r"تواصل\s+مع\s+([^\s]+)",
    )
    for pat in patterns:
        m = re.search(pat, msg_norm)
        if not m:
            continue
        candidate = _normalise_for_match(m.group(1))
        if len(candidate) < 2:
            continue
        if _name_appears_in_kb_body(db, tenant_id, candidate):
            return candidate
    return ""


def _find_staff_name(
    customer_msg_norm: str,
    candidates: Optional[List[str]] = None,
    *,
    customer_msg_raw: str = "",
) -> Optional[str]:
    """Pick the longest configured alias found as a whole token in the message."""
    if not customer_msg_norm:
        return None
    raw = (customer_msg_raw or customer_msg_norm or "").strip()
    try:
        from modules.ai.brain.commerce.staff_ameen_disambiguation import (  # noqa: PLC0415
            is_religious_ameen_context,
            staff_name_token_allowed,
        )

        if is_religious_ameen_context(raw):
            return None
    except Exception:  # noqa: BLE001
        staff_name_token_allowed = None  # type: ignore[assignment,misc]
        is_religious_ameen_context = None  # type: ignore[assignment,misc]

    pool = candidates or []
    msg_fold = _normalise_alif(customer_msg_norm)
    hits = []
    for n in pool:
        if not n:
            continue
        if staff_name_token_allowed is not None and not staff_name_token_allowed(raw, n):
            continue
        if _candidate_token_present(msg_fold, _normalise_alif(n)):
            hits.append(n)
    if not hits:
        return None
    hits.sort(key=len, reverse=True)
    return hits[0]


def _candidate_token_present(haystack: str, candidate: str) -> bool:
    """True when *candidate* appears as its own token, not embedded in a word."""
    if not haystack or not candidate:
        return False
    if candidate not in haystack:
        return False
    idx = 0
    while True:
        pos = haystack.find(candidate, idx)
        if pos < 0:
            return False
        before = haystack[pos - 1] if pos > 0 else " "
        after_pos = pos + len(candidate)
        after = haystack[after_pos] if after_pos < len(haystack) else " "
        if before.isspace() and after.isspace():
            return True
        idx = pos + 1


# Inbound-direction tokens used by ``StateManager.load_history`` rows
# and any chat-style ``{"role": "user"}`` shape. Outbound tokens cover
# both wire shapes too. The webhook passes whichever load_history
# returns directly into the safety nets, so we MUST tolerate both.
_HISTORY_INBOUND_TOKENS = {
    "in", "inbound", "user", "customer", "client", "u",
}
_HISTORY_OUTBOUND_TOKENS = {
    "out", "outbound", "assistant", "bot", "ai", "system", "a",
}


def _extract_recent_history_norms(
    history: Optional[List[Any]],
) -> Tuple[str, str]:
    """Return ``(history_bot_norm, history_customer_norm)`` — the
    most-recent outbound and inbound bodies from a conversation
    history list, normalised via :func:`_normalise_for_match`.

    Tolerates the two wire shapes that flow through this codebase:

      * ``StateManager.load_history`` rows
        ``{"direction": "in"/"inbound" | "out"/"outbound",
            "body": "<text>"}``.
      * Chat-style messages
        ``{"role": "user"|"assistant"|"bot"|"ai"|"system",
            "content": "<text>"}``.

    Empty / unknown shapes contribute the empty string and the
    walker keeps looking. Critical: the staff-contact safety net's
    pronoun carry-forward relied on ``role/content`` only, and
    ``StateManager.load_history`` ships ``direction/body``. The
    pre-fix walker silently returned empty pools for every
    production turn — the May 2026 #38c live trace exposed this
    when the resolver kept missing أمين even though the prior
    bot turn clearly mentioned him.
    """
    bot_norm = ""
    customer_norm = ""
    if not isinstance(history, list):
        return bot_norm, customer_norm
    for entry in reversed(history):
        try:
            if isinstance(entry, dict):
                role_raw = entry.get("role") or entry.get("direction") or ""
                content_raw = entry.get("content") or entry.get("body") or ""
            else:
                role_raw = (
                    getattr(entry, "role", None)
                    or getattr(entry, "direction", None)
                    or ""
                )
                content_raw = (
                    getattr(entry, "content", None)
                    or getattr(entry, "body", None)
                    or ""
                )
        except Exception:  # noqa: BLE001
            continue
        role = str(role_raw or "").strip().lower()
        content = str(content_raw or "").strip()
        if not content:
            continue
        if not bot_norm and role in _HISTORY_OUTBOUND_TOKENS:
            bot_norm = _normalise_for_match(content)
        elif not customer_norm and role in _HISTORY_INBOUND_TOKENS:
            customer_norm = _normalise_for_match(content)
        if bot_norm and customer_norm:
            break
    return bot_norm, customer_norm


def _find_staff_name_in_pool(
    *texts: str,
    candidates: Optional[List[str]] = None,
) -> Tuple[str, str]:
    """Scan multiple already-normalised text candidates and return
    ``(name, source_label)`` for the first hit.

    Priority order is the argument order; pass the most authoritative
    text first (typically the customer message). Used to recover the
    intent target when the customer message itself is just a pronoun
    ("كم رقمه؟") but the LLM reply or the immediately preceding bot
    turn already mentioned the staff name (e.g.
    "تواصل مع أمين بائع المعرض").

    Reply-pulled names go through an extra contact-verb proximity
    check (Tenant 33 #38d): the bare "name appears in body" rule
    used to grab common Saudi names ("هشام") that the LLM dropped
    in unrelated paragraphs of the merchant's brand story. We
    only honour a reply hit when the name sits within ~60 chars
    of a contact verb (تواصل / كلم / اتصل …) — that's the same
    shape the trigger detector already uses, kept consistent so a
    name we accept here is also one we'd have triggered on.

    Returns ``("", "")`` when no candidate name appears in any pool.
    """
    labels = ("customer_msg", "reply", "history_bot", "history_customer", "extra")
    for idx, text in enumerate(texts):
        if not text:
            continue
        name = _find_staff_name(text, candidates)
        if not name:
            continue
        label = labels[idx] if idx < len(labels) else f"pool_{idx}"
        # Validate reply-source names against the contact-verb
        # proximity rule. customer_msg / history_customer are
        # trusted (the customer is asking explicitly), and
        # history_bot is also trusted since the prior bot turn
        # had its own contact-verb gating when it was emitted.
        # Only the CURRENT-reply path needs validation because
        # the LLM hallucinates names in non-contact prose more
        # often than any other source.
        if label == "reply":
            offer_verb, offer_name = _reply_offers_staff_contact(text)
            if not (offer_verb and offer_name):
                continue
            # Honour the offer's own pick instead of the bare
            # candidate match — they're often the same, but the
            # offer detector used the longest-match rule too,
            # and any divergence means the contact verb anchored
            # a different name nearby.
            return offer_name, label
        return name, label
    return "", ""


# Verbs the LLM uses when it offers a staff contact in its OWN reply.
# The pre-fix safety net only fired on customer-side triggers
# ("ابي رقم أمين"), so a reply-side offer like "تواصل مع أمين عند
# الوصول" — produced proactively when the customer says "وصلت" — never
# got a resolver pass. We now match these reply verbs to detect implicit
# contact offers and run the same KB scan.
#
# The two tuples below are searched separately because they carry
# different proximity expectations:
#
#   * Direct contact verbs ("تواصل مع X لخدمة العملاء", "اتصل على X")
#     allow up to 60 chars between the verb and the name — the LLM
#     often inserts a role description ("لخدمة العملاء", "بائع
#     المعرض") between them.
#   * Suggestion verbs ("جربي X", "حاولي مع X") are short, tight
#     escalation phrases. The customer says "أمين مايرد" and the
#     LLM suggests an alternative as a one-line answer — the name
#     is always within ~30 chars of the verb. The tighter proximity
#     keeps unrelated brand-story prose ("جربي هذا العسل البلدي ولا
#     يفوّت على الأكل اليوم … تواصل مع المتجر") from accidentally
#     pairing a stray name candidate with the suggestion verb.
_REPLY_STAFF_CONTACT_VERBS: tuple = (
    "تواصل مع",
    "تواصل ب",
    "تواصلي مع",
    "تواصلي ب",
    "اتصل ب",
    "اتصل على",
    "اتصلي ب",
    "اتصلي على",
    "كلم",
    "اكلم",
    "رقم",
    "اطلب",
    "اطلبي",
    "رتب مع",
    "رتبي مع",
)

# Suggestion verbs the LLM uses to escalate to an alternative staff
# member when the primary one is unavailable. Tenant 33 #38e: the
# reported regression chain "أمين مايرد → جربي هشام (card sent) →
# مايرد → جربي هيثم 🌷 (no card) → وين رقمه؟ (no card)". Step 2
# silently dropped because the trigger detector did not consider
# "جربي" a contact offer, so the LLM-suggested alternative was
# never resolved through the same KB scan that fed the first card.
#
# Substring matching (`verb in rn`) means short forms cover their
# inflected variants for free — "جرب" matches "جربي" / "جربه" /
# "جربها" / "جرّب" (after diacritic strip) and "حاول" matches
# "حاولي". We deliberately keep this set tight: every verb here
# MUST be one the LLM uses to nominate a person; broad recommendation
# verbs ("استخدم" / "اختار") would invite false positives because
# we already widened :data:`_STAFF_NAME_CANDIDATES` to cover role
# nouns ("البائع" / "بائع المعرض").
_REPLY_STAFF_SUGGESTION_VERBS: tuple = (
    "جرب",         # also matches "جربي" / "جربه" / "جربها" / "جرّب"
    "حاول",        # also matches "حاولي"
    "اسأل",        # also matches "اسألي"
    "اسال",        # alif-folded variant of "اسأل"
    "تقدر تكلم",
    "تقدري تكلمي",
    "ما رايك تكلم",
    "ما رايك تكلمي",
)

# Proximity windows the verb→name search uses, in characters. Direct
# contact verbs allow a longer trailing window because the LLM often
# describes the role between the verb and the name; suggestion verbs
# are tight one-liners where the name lands directly after.
_REPLY_STAFF_CONTACT_PROXIMITY = 60
_REPLY_STAFF_SUGGESTION_PROXIMITY = 30


def _scan_verb_name_pair(
    rn: str,
    verbs: tuple,
    proximity: int,
    candidates: List[str],
) -> Tuple[str, str]:
    """Find the first ``(verb, name)`` pair where one of *verbs*
    is followed within *proximity* characters by one of *candidates*.

    Helper extracted so the contact-verb and suggestion-verb scans
    share identical longest-first matching semantics. Returns an
    empty tuple on miss.
    """
    for verb in verbs:
        start = 0
        while True:
            idx = rn.find(verb, start)
            if idx < 0:
                break
            after = rn[idx + len(verb): idx + len(verb) + proximity]
            for name in candidates:
                if _candidate_token_present(after, name):
                    return verb, name
            start = idx + len(verb)
    return "", ""


def _reply_offers_staff_contact(
    reply_text: str,
    candidates: Optional[List[str]] = None,
) -> Tuple[str, str]:
    """Detect a proactive staff-contact offer in the bot reply.

    Returns ``(verb, name)`` for the first match where one of the
    direct contact verbs (:data:`_REPLY_STAFF_CONTACT_VERBS`) is
    followed within :data:`_REPLY_STAFF_CONTACT_PROXIMITY` chars by
    a known :data:`_STAFF_NAME_CANDIDATES` token, OR one of the
    suggestion verbs (:data:`_REPLY_STAFF_SUGGESTION_VERBS`) is
    followed within :data:`_REPLY_STAFF_SUGGESTION_PROXIMITY` chars
    by a candidate. Empty tuple on miss.

    Used as the secondary trigger for
    :func:`apply_staff_contact_safety_net` so an arrival-flow
    reply ("وصلت" → "تواصل مع أمين عند الوصول") OR an escalation
    reply ("أمين مايرد" → "جربي هشام 🌷") gets the same resolver
    pass as an explicit "ابي رقم أمين" ask. Without this the
    asset-promise sanitiser downstream rewrites the reply to the
    cold "الرقم غير مضاف" copy even though the KB has the contact.
    """
    if not reply_text:
        return "", ""
    rn = _normalise_for_match(reply_text)
    if not rn:
        return "", ""
    # Sort name candidates longest-first so "أبو هشام" beats "هشام"
    # and "بائع المعرض" beats "البائع".
    pool = sorted(candidates or [], key=len, reverse=True)
    # Direct contact verbs first — they carry the strongest semantic
    # weight ("تواصل مع X" is unambiguous) and the longer proximity
    # window beats suggestion verbs in the rare case the LLM stacks
    # both ("جربي التواصل مع هشام لخدمة العملاء"), so the resolver
    # logs `verb=تواصل مع` instead of `verb=جرب`.
    verb, name = _scan_verb_name_pair(
        rn, _REPLY_STAFF_CONTACT_VERBS,
        _REPLY_STAFF_CONTACT_PROXIMITY, pool,
    )
    if verb and name:
        return verb, name
    return _scan_verb_name_pair(
        rn, _REPLY_STAFF_SUGGESTION_VERBS,
        _REPLY_STAFF_SUGGESTION_PROXIMITY, pool,
    )


def _emit_staff_contact_graph_trace(
    db: Any,
    tenant_id: int,
) -> None:
    """Log a snapshot of the staff-contact graph the resolver can see.

    Runs at most once per turn from inside
    :func:`apply_staff_contact_safety_net`. Emits a single
    ``[STAFF_CONTACT_GRAPH]`` INFO line that lists every
    ``(candidate_name → phone)`` pair the resolver discovers in
    the KB sections covered by :data:`_STAFF_KB_FALLBACK_KINDS`,
    regardless of whether the safety net fires this turn.

    Production triage uses this to answer "does the resolver see
    أمين's number at all?" without enabling DEBUG and without
    reading code. When the trace shows the pair exists but the
    safety net still bails, the bug is in the trigger gating —
    not in KB ingestion. When the trace shows no pair exists,
    the merchant's KB genuinely doesn't carry the contact in a
    scanned kind and the next move is to expand
    :data:`_STAFF_KB_FALLBACK_KINDS` or add the contact to the
    KB.

    Never raises. Pure telemetry.
    """
    if db is None or not tenant_id:
        return
    try:
        from models import MerchantKnowledgeSection  # noqa: PLC0415
        from core.knowledge import apply_ai_visible_kb_query_filters  # noqa: PLC0415
        rows = (
            apply_ai_visible_kb_query_filters(
                db.query(MerchantKnowledgeSection)
            )
            .filter(
                MerchantKnowledgeSection.tenant_id == tenant_id,
                MerchantKnowledgeSection.kind.in_(_STAFF_KB_FALLBACK_KINDS),
            )
            .order_by(
                MerchantKnowledgeSection.priority.asc(),
                MerchantKnowledgeSection.updated_at.desc(),
            )
            .limit(40)
            .all()
        )
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "[STAFF_CONTACT_GRAPH] tenant_id=%s state=query_failed err=%s",
            tenant_id, exc,
        )
        return

    pairs: List[Dict[str, Any]] = []
    sections_scanned = 0
    sections_with_phone = 0
    suggestion_skipped = 0
    for row in rows:
        sections_scanned += 1
        if _is_dashboard_suggestion_section(row):
            suggestion_skipped += 1
            continue
        body = getattr(row, "body", "") or ""
        if not body:
            continue
        body_norm = _normalise_alif(body).lower()
        phones = _extract_phones(body)
        if phones:
            sections_with_phone += 1
        alias_tokens = _kb_contact_labels_only(db, tenant_id)
        for cand in alias_tokens:
            cand_norm = _normalise_alif(cand).lower()
            if cand_norm and cand_norm in body_norm:
                pairs.append({
                    "kind": getattr(row, "kind", ""),
                    "section_id": int(getattr(row, "id", 0) or 0),
                    "name_chars": len(cand),
                    "phones": len(phones),
                })

    # Compact summary — one line, easy to grep in production logs.
    # We deliberately don't ship the raw phone digits or full names
    # (PII discipline). The merchant's audit dashboard reads from
    # the same KB rows, so reconstruction is a one-click query.
    distinct_kinds = sorted({p["kind"] for p in pairs})
    pairs_with_phone = sum(1 for p in pairs if p["phones"] > 0)
    logger.info(
        "[STAFF_CONTACT_GRAPH] tenant_id=%s sections_scanned=%d "
        "sections_with_phone=%d suggestion_skipped=%d "
        "pairs_found=%d pairs_with_phone=%d kinds=%s",
        tenant_id, sections_scanned, sections_with_phone,
        suggestion_skipped, len(pairs), pairs_with_phone,
        ",".join(distinct_kinds) or "-",
    )

    # Per-pair detail — bounded to the first 12 pairs to avoid
    # log bloat on tenants with very busy KBs. Each line shows
    # exactly which section a candidate name landed in and
    # whether that section actually carries a phone, so a
    # production trace can answer "is the LLM hallucinating a
    # name from the brand story?" without dashboard access.
    for pair in pairs[:12]:
        logger.info(
            "[STAFF_CONTACT_GRAPH_PAIR] tenant_id=%s kind=%s "
            "section_id=%d name_chars=%d phones_in_section=%d",
            tenant_id,
            pair["kind"] or "-",
            pair["section_id"],
            pair["name_chars"],
            pair["phones"],
        )


@dataclass
class StaffContactSafetyNetResult:
    fired: bool = False
    reason: str = ""
    skipped_reason: str = ""
    inferred_name: str = ""
    wa_id: str = ""
    # Where the phone digits ultimately came from. Useful for
    # production telemetry when triaging "why did the AI say أمين's
    # name but not send his number?" — the answer is now visible
    # in a single grep.
    #
    # Values:
    #   ``"reply"``     — Claude wrote the phone as plain text and
    #                     we lifted it out of the LLM reply (the
    #                     classic path).
    #   ``"kb:<kind>"`` — extracted from a free-form KB section
    #                     where the merchant typed a name + phone
    #                     pair (May 2026 #36 KB-scan layer). The
    #                     ``<kind>`` segment tells you which KB
    #                     bucket carried the data.
    #   ``""``          — net did not fire.
    source: str = ""
    extra_call_target: Any = None  # CallTarget when fired
    strip_phones_from_reply: bool = False

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "kind":           "staff_contact",
            "fired":          self.fired,
            "reason":         self.reason or self.skipped_reason,
            "inferred_name":  self.inferred_name,
            "wa_id":          self.wa_id,
            "source":         self.source,
        }


def _normalise_alif(text: str) -> str:
    """Fold the four alif variants to a bare alif and the alif-maqsura
    to yaa. Used inside the KB scanner so a merchant who typed
    "أمين" can still be matched when the customer typed "امين"
    (and vice versa). We keep this fold LOCAL to the staff-name
    matcher — the rest of the file deliberately preserves the
    original Arabic shape so we don't garble unrelated content."""
    if not text:
        return ""
    return (
        text
        .replace("\u0623", "\u0627")   # أ
        .replace("\u0625", "\u0627")   # إ
        .replace("\u0622", "\u0627")   # آ
        .replace("\u0649", "\u064a")   # ى → ي
    )


def _lookup_staff_phone_in_kb(
    db: Any,
    tenant_id: int,
    name: str,
) -> Tuple[str, str, str]:
    """Scan free-form KB sections for a name+phone pair.

    Returns a ``(raw_phone, source_kind, section_id)`` tuple. Empty
    strings on miss / error. Never raises.

    Scoring:
      * For each candidate KB row (kinds in
        :data:`_STAFF_KB_FALLBACK_KINDS`), find every occurrence
        of ``name`` (alif-folded) inside the body.
      * For each occurrence we look at the surrounding window
        (:data:`_STAFF_KB_PROXIMITY_WINDOW` chars before/after)
        and pick the FIRST Saudi-shaped phone we see. Proximity
        wins over absolute body order so we don't pair "أمين"
        with the warehouse phone three paragraphs down.
      * Emits a single ``[STAFF_CONTACT_RESOLVER]`` INFO line per
        outcome (hit/miss). The webhook re-emits its own
        ``[SAFETY_NET:staff_contact]`` line as before.

    Platform-level: every merchant who typed
    "أمين - 0541690226" / "بائع المعرض: 0555906901" anywhere in a
    valid KB section now has the AI deliver that contact card —
    no schema migration, no dashboard change required.
    """
    if db is None or not tenant_id or not name:
        return "", "", ""
    target_norm = _normalise_alif(name).lower()
    if not target_norm:
        return "", "", ""

    try:
        from models import MerchantKnowledgeSection  # noqa: PLC0415
        from core.knowledge import apply_ai_visible_kb_query_filters  # noqa: PLC0415
        rows = (
            apply_ai_visible_kb_query_filters(
                db.query(MerchantKnowledgeSection)
            )
            .filter(
                MerchantKnowledgeSection.tenant_id == tenant_id,
                MerchantKnowledgeSection.kind.in_(_STAFF_KB_FALLBACK_KINDS),
            )
            .order_by(
                MerchantKnowledgeSection.priority.asc(),
                MerchantKnowledgeSection.updated_at.desc(),
            )
            .limit(40)
            .all()
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "safety_nets.staff_kb | query failed tenant=%s err=%s",
            tenant_id, exc,
        )
        return "", "", ""

    # Pass 1 — proximity match (the canonical, most-confident path).
    # We also remember the FIRST section that contained the name +
    # at least one phone, so we can fall back to a single-phone
    # bypass below when no proximity hit was found.
    fallback_single_phone: Optional[tuple] = None  # (kind, id, phone)
    name_seen_in_kinds: List[str] = []
    name_seen_in_section_ids: List[int] = []
    for row in rows:
        # Dashboard improvement-suggestion sections look like data
        # in the dashboard sidebar but their bodies are
        # placeholder prompts, not real contact entries. Skipping
        # at scan-time keeps both [STAFF_CONTACT_RESOLVER] and
        # the upstream graph trace free of ghost pairs.
        if _is_dashboard_suggestion_section(row):
            continue
        body = getattr(row, "body", "") or ""
        if not body:
            continue
        body_norm = _normalise_alif(body).lower()

        # Pre-compute every Saudi-phone span in the original body so
        # we can pick the one CLOSEST to a name hit. Using a single
        # pass keeps the inner loop O(name_hits + phone_count)
        # instead of O(name_hits * body_len).
        phone_spans: List[tuple] = []
        for pat in _PHONE_REGEXES:
            for m in pat.finditer(body):
                phone_spans.append((m.start(), m.end(), m.group(0)))
        # Track unique phones for the single-phone fallback below.
        unique_phones_in_section = sorted(
            {span[2] for span in phone_spans}
        )
        if not phone_spans:
            # Section has the name in it (we'll detect this below)
            # but no phones → record for telemetry only; the
            # cross-section fallback never reads this branch.
            if target_norm in body_norm:
                name_seen_in_kinds.append(getattr(row, "kind", ""))
                name_seen_in_section_ids.append(int(getattr(row, "id", 0) or 0))
            continue
        phone_spans.sort(key=lambda s: s[0])

        section_has_name = target_norm in body_norm
        if section_has_name:
            name_seen_in_kinds.append(getattr(row, "kind", ""))
            name_seen_in_section_ids.append(int(getattr(row, "id", 0) or 0))
            # Single-phone-in-section fallback: when the section
            # has the name AND exactly one phone, the most plausible
            # interpretation is "this section is about this person".
            # We hold this in reserve for after the proximity sweep.
            # Pre-fix (May 2026 #38), a section like
            #   "بائع المعرض: أمين"
            #   "<long product paragraph>"
            #   "للتواصل المباشر: 0541690226"
            # could exceed the proximity window. The single-phone
            # bypass rescues this layout — we never bypass on
            # multi-phone sections because that's where the
            # ambiguity lives.
            if (
                fallback_single_phone is None
                and len(unique_phones_in_section) == 1
            ):
                fallback_single_phone = (
                    getattr(row, "kind", ""),
                    str(getattr(row, "id", "")),
                    unique_phones_in_section[0],
                )

        idx = 0
        while True:
            pos = body_norm.find(target_norm, idx)
            if pos < 0:
                break
            name_end = pos + len(target_norm)
            # Two-pass selection:
            #   1. Prefer the closest phone AFTER the name within
            #      the window. Saudi free-form KB entries
            #      overwhelmingly write "<اسم>: <رقم>" — phone
            #      after name. Honouring that ordering keeps us
            #      from pairing "خالد" with the line above it
            #      ("أمين بائع المعرض: 054…") just because it
            #      happens to be one newline away.
            #   2. If nothing after, fall back to the closest
            #      phone BEFORE the name within the window.
            best_after: Optional[tuple] = None
            best_after_dist = _STAFF_KB_PROXIMITY_WINDOW + 1
            best_before: Optional[tuple] = None
            best_before_dist = _STAFF_KB_PROXIMITY_WINDOW + 1
            for ph_start, ph_end, ph_text in phone_spans:
                if ph_start >= name_end:
                    dist = ph_start - name_end
                    if dist <= _STAFF_KB_PROXIMITY_WINDOW and dist < best_after_dist:
                        best_after = (ph_start, ph_end, ph_text)
                        best_after_dist = dist
                elif ph_end <= pos:
                    dist = pos - ph_end
                    if dist <= _STAFF_KB_PROXIMITY_WINDOW and dist < best_before_dist:
                        best_before = (ph_start, ph_end, ph_text)
                        best_before_dist = dist
                else:
                    # Overlap — phone is inside the name span
                    # (extremely unlikely). Treat as zero distance.
                    if 0 < best_after_dist:
                        best_after = (ph_start, ph_end, ph_text)
                        best_after_dist = 0
            chosen = best_after or best_before
            if chosen is not None:
                used_dist = (
                    best_after_dist if best_after is not None
                    else best_before_dist
                )
                direction = "after" if best_after is not None else "before"
                logger.info(
                    "[STAFF_CONTACT_RESOLVER] tenant_id=%s source=kb:%s "
                    "section_id=%s name_len=%d phone_match=%s "
                    "direction=%s proximity=%d tier=proximity",
                    tenant_id, row.kind, row.id,
                    len(name), True, direction, used_dist,
                )
                return chosen[2], row.kind, str(row.id)
            idx = name_end

    # Pass 2 — single-phone-in-section bypass (May 2026 #38).
    # No proximity hit, but a section that mentions the name has
    # exactly one phone in its body. Treat that phone as the
    # contact for this name.
    if fallback_single_phone is not None:
        kind, sid, phone = fallback_single_phone
        logger.info(
            "[STAFF_CONTACT_RESOLVER] tenant_id=%s source=kb:%s "
            "section_id=%s name_len=%d phone_match=%s tier=single_phone "
            "reason=single_phone_in_section",
            tenant_id, kind, sid, len(name), True,
        )
        return phone, kind, sid

    # No phone found anywhere. Telemetry covers the four most
    # actionable failure modes so production triage can answer
    # "why didn't the bot send أمين's number?" without reading
    # source. ``no_phone_in_any_section`` means the merchant
    # genuinely has no phone in the staff KB — this should drive
    # a ``missing_staff_phone`` improvement suggestion.
    if not name_seen_in_kinds:
        miss_reason = "name_not_found_in_kb"
    elif name_seen_in_section_ids:
        # Name was found in a section, but that section had either
        # no phones or multiple phones (none within proximity).
        miss_reason = "name_found_no_proximity_match"
    else:
        miss_reason = "no_phone_in_any_section"
    logger.info(
        "[STAFF_CONTACT_RESOLVER] tenant_id=%s source=kb:miss name_len=%d "
        "phone_match=%s reason=%s name_in_kinds=%s name_in_section_ids=%s",
        tenant_id, len(name), False, miss_reason,
        ",".join(name_seen_in_kinds) or "-",
        ",".join(str(i) for i in name_seen_in_section_ids) or "-",
    )
    return "", "", ""


def apply_staff_contact_safety_net(
    *,
    customer_msg: str,
    reply_text: str,
    existing_call_targets: List[Any],
    detected_call_markers: int,
    db: Any = None,
    tenant_id: int = 0,
    history: Optional[List[Any]] = None,
    staff_contacts_sent: Optional[List[Dict[str, Any]]] = None,
    conversation_turn: int = 0,
    conversation_id: Optional[int] = None,
) -> StaffContactSafetyNetResult:
    """Build a contact-card ``CallTarget`` when the customer asked
    to reach a staff member by name.

    Resolution order (May 2026 #36 — platform-wide KB scan):
      1. **Reply scan**  — phones Claude wrote as plain text in
         the reply. Highest confidence: the LLM saw the KB and
         chose to mention this number for THIS request.
      2. **KB free-text scan** — when the LLM omitted the phone
         but the merchant has a name+phone pair sitting in a
         free-form KB section (``branches`` / ``store_story`` /
         ``owner_identity`` / ``quick_update`` / ``custom`` /
         ``faq`` / ``escalation_rules``), we lift it directly.
         This closes the gap that produced the May 2026 #36
         feedback: bot says "تواصل مع أمين بائع المعرض" but
         doesn't ship the number even though it's in the KB.

    Telemetry: the function emits a structured
    ``[STAFF_CONTACT_RESOLVER]`` INFO line per outcome (KB hit /
    miss / reply-only). The webhook re-emits the legacy
    ``[SAFETY_NET:staff_contact]`` line for backwards compat.

    Returns a :class:`StaffContactSafetyNetResult`. When ``fired``
    is true, the caller should append ``extra_call_target`` to
    ``_call_targets`` and bump ``_marker_resolved["call"]``. The
    ``source`` field tells you whether the phone came from the
    reply or which KB kind it was lifted from.

    ``db`` and ``tenant_id`` are optional for backwards
    compatibility — without them only the reply-scan layer runs,
    matching the pre-May-2026-#36 behaviour exactly.
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

    try:
        from modules.ai.brain.commerce.staff_ameen_disambiguation import (  # noqa: PLC0415
            is_religious_ameen_context,
        )

        if is_religious_ameen_context(customer_msg or ""):
            result.skipped_reason = "religious_ameen_context"
            return result
    except Exception:  # noqa: BLE001
        pass

    _alias_candidates = _staff_alias_candidates(db, int(tenant_id or 0))

    # Emit the structured contact-graph snapshot once per turn so
    # production triage can confirm "does the resolver see أمين's
    # number at all?" before debugging trigger gating. Cheap query,
    # bounded to 40 rows; the same shape we run for the resolver.
    _emit_staff_contact_graph_trace(db, int(tenant_id or 0))

    try:
        from modules.ai.brain.commerce.arrival_contact_policy import (  # noqa: PLC0415
            resolve_arrival_contact_policy,
        )
        from modules.ai.brain.commerce.contact_escalation import (  # noqa: PLC0415
            classify_employee_not_responding,
            classify_location_branch_failure,
            classify_store_arrival,
            contact_already_sent,
            log_contact_escalation,
            log_location_branch_failure,
            parse_staff_contacts_sent,
        )
        _history_list = history if isinstance(history, list) else None
        _arrival_policy = resolve_arrival_contact_policy(
            db, int(tenant_id or 0),
        )
        _store_arrival = classify_store_arrival(
            customer_msg or "",
            history=_history_list,
        )
        _employee_not_responding = classify_employee_not_responding(
            customer_msg or "",
        )
        _location_branch_failure = classify_location_branch_failure(
            customer_msg or "",
            history=_history_list,
        )
        _contacts_sent = parse_staff_contacts_sent(staff_contacts_sent)
        from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
            has_explicit_contact_intent,
            is_explicit_arrival_intent,
            should_defer_contact_policies_for_commerce,
        )
    except Exception:  # noqa: BLE001
        _arrival_policy = None
        _store_arrival = None
        _employee_not_responding = None
        _location_branch_failure = None
        _contacts_sent = []
        has_explicit_contact_intent = None  # type: ignore[assignment,misc]
        is_explicit_arrival_intent = None  # type: ignore[assignment,misc]
        should_defer_contact_policies_for_commerce = None  # type: ignore[assignment,misc]

    if (
        should_defer_contact_policies_for_commerce is not None
        and should_defer_contact_policies_for_commerce(customer_msg or "")
        and _employee_not_responding is None
        and not (
            has_explicit_contact_intent is not None
            and has_explicit_contact_intent(customer_msg or "")
        )
    ):
        logger.info(
            "[STAFF_CONTACT_TRACE] tenant_id=%s stage=trigger hit=False "
            "reason=commerce_deferred preview=%r",
            int(tenant_id or 0),
            (customer_msg or "")[:48],
        )
        result.skipped_reason = "commerce_deferred"
        return result

    _policy_allowed = bool(
        _arrival_policy is not None and _arrival_policy.allowed
    )
    _arrival_signal = bool(
        is_explicit_arrival_intent is not None
        and is_explicit_arrival_intent(customer_msg or "")
    )
    _arrival_gated_intent = _arrival_signal and _policy_allowed

    if _location_branch_failure is not None:
        log_location_branch_failure(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            trigger=_location_branch_failure.trigger,
            context=_location_branch_failure.context,
            matched=_location_branch_failure.pattern,
            preview=(customer_msg or "")[:80],
        )
        if _arrival_signal and not _policy_allowed:
            log_contact_escalation(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                trigger=_store_arrival.trigger if _store_arrival else "store_arrival",
                context=_store_arrival.context if _store_arrival else "-",
                name_source="-",
                already_sent=False,
                selected_contact="",
                contacts_sent_count=len(_contacts_sent),
                policy_allowed=False,
            )

    # Trigger gating: a turn fires the resolver when EITHER side of
    # the conversation surfaces staff-contact intent.
    #   * Customer-side: explicit ask ("ابي رقم أمين", "كم رقمه").
    #   * Customer-side (KB-gated): arrival / on-the-way / branch
    #     access ONLY when merchant KB opted in via
    #     ``merchant_allows_arrival_staff_contact``.
    #   * Reply-side:    bot proactively offers a contact
    #                    ("تواصل مع أمين عند الوصول") with no digits
    #                    in the reply yet — also KB-gated; without
    #                    opt-in the LLM text stands alone (no vCard).
    explicit_customer_intent = (
        _has_any(_STAFF_INTENT_TRIGGERS, msg_norm)
        or _employee_not_responding is not None
        or bool(_find_staff_name(
            msg_norm,
            _alias_candidates,
            customer_msg_raw=customer_msg or "",
        ))
    )
    customer_intent = explicit_customer_intent or _arrival_gated_intent

    reply_offer_verb, reply_offer_name = _reply_offers_staff_contact(
        reply_text or "", _alias_candidates,
    )
    reply_has_digits = bool(_extract_phones(reply_text or ""))
    reply_offer = bool(reply_offer_verb and reply_offer_name)
    if (
        reply_offer
        and should_defer_contact_policies_for_commerce is not None
        and should_defer_contact_policies_for_commerce(customer_msg or "")
        and _employee_not_responding is None
    ):
        reply_offer = False
        reply_offer_verb = ""
        reply_offer_name = ""
    if not customer_intent:
        if not reply_offer:
            if _employee_not_responding is not None:
                log_contact_escalation(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    trigger="employee_not_responding",
                    context="-",
                    name_source="-",
                    already_sent=False,
                    selected_contact="",
                    contacts_sent_count=len(_contacts_sent),
                )
            if _arrival_signal and not _policy_allowed:
                log_contact_escalation(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    trigger=_store_arrival.trigger if _store_arrival else "store_arrival",
                    context=_store_arrival.context if _store_arrival else "-",
                    name_source="-",
                    already_sent=False,
                    selected_contact="",
                    contacts_sent_count=len(_contacts_sent),
                    policy_allowed=False,
                )
            logger.info(
                "[STAFF_CONTACT_TRACE] tenant_id=%s stage=trigger hit=False "
                "customer_intent=False reply_offer_verb=%r "
                "reply_has_digits=%s arrival_signal=%s policy_allowed=%s",
                int(tenant_id or 0),
                reply_offer_verb or "", reply_has_digits,
                _arrival_signal, _policy_allowed,
            )
            result.skipped_reason = (
                "arrival_policy_denied"
                if _arrival_signal and not _policy_allowed and not reply_has_digits
                else "no_staff_intent"
            )
            return result
        if reply_has_digits:
            logger.info(
                "[STAFF_CONTACT_TRACE] tenant_id=%s stage=trigger hit=True "
                "source=reply_offer_with_digits verb=%r name_chars=%d",
                int(tenant_id or 0),
                reply_offer_verb,
                len(reply_offer_name),
            )
        elif not (_policy_allowed and _arrival_signal):
            log_contact_escalation(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                trigger="reply_offer",
                context=_store_arrival.context if _store_arrival else "-",
                name_source="-",
                already_sent=False,
                selected_contact="",
                contacts_sent_count=len(_contacts_sent),
                policy_allowed=_policy_allowed if _arrival_signal else False,
            )
            logger.info(
                "[STAFF_CONTACT_TRACE] tenant_id=%s stage=trigger hit=False "
                "source=reply_offer arrival_signal=%s policy_allowed=%s",
                int(tenant_id or 0),
                _arrival_signal,
                _policy_allowed,
            )
            result.skipped_reason = (
                "arrival_policy_denied"
                if _arrival_signal and not _policy_allowed
                else "no_staff_intent"
            )
            return result
        else:
            logger.info(
                "[STAFF_CONTACT_TRACE] tenant_id=%s stage=trigger hit=True "
                "source=reply_offer verb_chars=%d name_chars=%d policy_allowed=true",
                int(tenant_id or 0),
                len(reply_offer_verb), len(reply_offer_name),
            )
    else:
        logger.info(
            "[STAFF_CONTACT_TRACE] tenant_id=%s stage=trigger hit=True "
            "source=%s arrival_signal=%s policy_allowed=%s",
            int(tenant_id or 0),
            "arrival_gated" if _arrival_gated_intent else "customer_msg",
            _arrival_signal,
            _policy_allowed if _arrival_signal else "-",
        )

    # Layer 0: scan the customer message → the LLM reply →
    # the most recent bot/customer turns in history. Pronoun-only
    # asks ("كم رقمه؟") rely on the LLM having mentioned the staff
    # name in the same reply, or on the prior bot turn that the
    # customer is following up on. Without this carry-forward the
    # net misses every "كم رقمه" / "ايش رقمه" turn even when the
    # KB has the contact.
    reply_norm = _normalise_for_match(reply_text or "")
    history_bot_norm, history_customer_norm = _extract_recent_history_norms(history)

    _fallback_trigger = ""
    if _contacts_sent:
        if _employee_not_responding is not None:
            _fallback_trigger = "employee_not_responding"
        elif (
            _store_arrival is not None
            and getattr(_store_arrival, "trigger", "") == "branch_closed"
        ):
            _fallback_trigger = "branch_closed"

    name = ""
    name_source = ""
    _fallback_prefill_phone = ""
    _arrival_prefill_phone = ""
    _fallback_section_kind = ""
    _fallback_section_id = ""
    if _fallback_trigger:
        try:
            from modules.ai.brain.commerce.staff_contact_fallback_v0 import (  # noqa: PLC0415
                load_staff_chain_sections,
                resolve_staff_contact_fallback_v0,
            )
            _fb_sections = load_staff_chain_sections(db, int(tenant_id or 0))
            _fb = resolve_staff_contact_fallback_v0(
                _fb_sections,
                contacts_sent=_contacts_sent,
                customer_msg=customer_msg or "",
                trigger=_fallback_trigger,
                tenant_id=tenant_id,
                db=db,
            )
            if _fb.enabled and _fb.next_phone:
                name = _fb.next_lookup_name
                name_source = "staff_contact_fallback_v0"
                _fallback_prefill_phone = _fb.next_phone
                _fallback_section_id = str(_fb.section_id or "")
                _fallback_section_kind = "escalation_chain"
            elif not _fb.enabled:
                logger.info(
                    "[STAFF_CONTACT_TRACE] tenant_id=%s stage=fallback hit=False "
                    "trigger=%s reason=%s",
                    int(tenant_id or 0),
                    _fallback_trigger,
                    _fb.reason or "-",
                )
                result.skipped_reason = (
                    f"fallback_{_fb.reason}"
                    if _fb.reason
                    else "fallback_unresolved"
                )
                return result
        except Exception as _fb_exc:  # noqa: silent-ok - fallback_v0 must not block staff contact net
            logger.debug(
                "safety_nets.staff | fallback_v0 failed tenant=%s err=%s",
                tenant_id, _fb_exc,
            )

    # Arrival showroom evidence — must win over LLM reply_offer (CS names).
    if (
        not name
        and _arrival_gated_intent
        and not _fallback_trigger
        and db is not None
        and tenant_id
    ):
        try:
            from modules.ai.brain.commerce.arrival_contact_delivery_policy import (  # noqa: PLC0415
                resolve_arrival_contact_evidence,
            )

            _arrival_ev = resolve_arrival_contact_evidence(db, int(tenant_id))
            if _arrival_ev is not None and _arrival_ev.phone:
                name = _arrival_ev.lookup_name
                name_source = "arrival_evidence"
                _arrival_prefill_phone = _arrival_ev.phone
                logger.info(
                    "[STAFF_CONTACT_TRACE] tenant_id=%s stage=name_lookup hit=True "
                    "source=arrival_evidence name_chars=%d",
                    int(tenant_id or 0),
                    len(name),
                )
        except Exception as _arrival_ev_exc:  # noqa: BLE001
            logger.exception(
                "safety_nets.staff | arrival_evidence failed tenant=%s err=%s",
                tenant_id, _arrival_ev_exc,
            )

    if (
        not name
        and _arrival_gated_intent
        and not _fallback_trigger
        and _arrival_policy is not None
        and getattr(_arrival_policy, "policy_source", "") == "compiled_v0"
        and _arrival_policy.allowed
    ):
        _compiled_lookup = str(
            getattr(_arrival_policy, "contact_lookup_name", "") or ""
        ).strip()
        if _compiled_lookup:
            name = _compiled_lookup
            name_source = "compiled_v0_contact_hint"
            logger.info(
                "[STAFF_CONTACT_TRACE] tenant_id=%s stage=name_lookup hit=True "
                "source=compiled_v0_contact_hint name_chars=%d",
                int(tenant_id or 0),
                len(name),
            )

    # Reply-side LLM offers must NOT override arrival showroom evidence.
    if not name and reply_offer_name and not _arrival_gated_intent:
        name, name_source = reply_offer_name, "reply_offer"
    elif not name:
        name, name_source = _find_staff_name_in_pool(
            msg_norm, reply_norm,
            history_bot_norm, history_customer_norm,
            candidates=_alias_candidates,
        )
    if not name and db is not None and tenant_id:
        _ev_name = _evidence_backed_name_from_message(
            db, int(tenant_id), msg_norm,
        )
        if _ev_name:
            name, name_source = _ev_name, "customer_msg_kb_backed"

    if _arrival_gated_intent and not _fallback_trigger and not name:
        logger.info(
            "[STAFF_CONTACT_TRACE] tenant_id=%s stage=name_lookup hit=False "
            "source=arrival_gated reason=no_arrival_evidence",
            int(tenant_id or 0),
        )
        result.skipped_reason = "arrival_no_evidence"
        return result

    if not name:
        if _employee_not_responding is not None:
            log_contact_escalation(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                trigger="employee_not_responding",
                context="-",
                name_source=name_source or "-",
                already_sent=False,
                selected_contact="",
                contacts_sent_count=len(_contacts_sent),
            )
        logger.info(
            "[STAFF_CONTACT_TRACE] tenant_id=%s stage=name_lookup hit=False "
            "msg_chars=%d reply_chars=%d history_bot_chars=%d "
            "history_customer_chars=%d",
            int(tenant_id or 0),
            len(msg_norm), len(reply_norm),
            len(history_bot_norm), len(history_customer_norm),
        )
        result.skipped_reason = "no_staff_name"
        return result
    result.inferred_name = name
    logger.info(
        "[STAFF_CONTACT_TRACE] tenant_id=%s stage=name_lookup hit=True "
        "name_chars=%d source=%s",
        int(tenant_id or 0), len(name), name_source,
    )

    # ── Layer 1: reply scan (canonical path) ────────────────────
    raw_phone = _fallback_prefill_phone or _arrival_prefill_phone
    source = ""
    if raw_phone:
        if _fallback_section_kind:
            source = f"kb:{_fallback_section_kind}"
        elif _arrival_prefill_phone:
            source = "arrival_evidence"
        else:
            source = "fallback_v0"
        logger.info(
            "[STAFF_CONTACT_RESOLVER] tenant_id=%s source=%s "
            "name_len=%d phone_match=%s section_id=%s",
            int(tenant_id or 0),
            source,
            len(name),
            True,
            _fallback_section_id or "-",
        )
    phones = _extract_phones(reply_text or "")
    if not raw_phone and phones:
        raw_phone = phones[0]
        source = "reply"
        logger.info(
            "[STAFF_CONTACT_RESOLVER] tenant_id=%s source=reply "
            "name_len=%d phone_match=%s",
            int(tenant_id or 0), len(name), True,
        )

    # ── Layer 2: KB free-text scan (May 2026 #36) ───────────────
    kb_section_kind = ""
    kb_section_id = ""
    if not raw_phone and db is not None and tenant_id:
        raw_phone, kb_section_kind, kb_section_id = _lookup_staff_phone_in_kb(
            db, int(tenant_id), name,
        )
        if raw_phone:
            source = f"kb:{kb_section_kind}"

    if not raw_phone:
        # Telemetry: log the miss so production triage can see
        # tenants who could benefit from filling a structured
        # staff directory once we ship one.
        logger.info(
            "[STAFF_CONTACT_RESOLVER] tenant_id=%s source=none "
            "name_len=%d phone_match=%s reason=no_phone_in_reply_or_kb",
            int(tenant_id or 0), len(name), False,
        )
        # Tenant 33 #38e — escalation-chain gap signal. When the bot
        # PROACTIVELY suggested an alternative staff member but the
        # KB carries no phone for them, we want a single grep-able
        # line that says exactly which name fell through. This is
        # the actionable artefact for the merchant: "you suggested
        # هيثم but didn't add his number — fill it in the dashboard".
        # We emit only when the name came from the bot side
        # (reply_offer / history_bot) since a customer-typed name
        # is the customer's own ask, not an escalation gap.
        if name_source in ("reply_offer", "history_bot"):
            logger.info(
                "[STAFF_ESCALATION_GAP] tenant_id=%s name_chars=%d "
                "name_source=%s reason=suggested_but_no_kb_phone "
                "— merchant should add this contact to the KB",
                int(tenant_id or 0), len(name), name_source,
            )
        result.skipped_reason = "no_phone_in_reply"
        return result

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

    role = ""
    if db is not None and tenant_id:
        try:
            from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
                is_usable_display_name,
                load_staff_contact_registry,
            )

            registry = load_staff_contact_registry(db, int(tenant_id))
            phone_key = re.sub(r"\D", "", raw_phone)[-9:]
            for rec in registry.records:
                rec_key = re.sub(r"\D", "", rec.phone or "")[-9:]
                if rec_key and rec_key == phone_key:
                    if not is_usable_display_name(name):
                        name = rec.lookup_name
                    role = rec.role or role
                    break
        except Exception:
            logger.exception(
                "[SAFETY_NET:staff_contact] registry_role_lookup_failed tenant=%s",
                tenant_id,
            )

    from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
        build_staff_call_target,
    )

    target = build_staff_call_target(
        lookup_name=name,
        phone=raw_phone,
        role=role,
    )
    if target is None:
        result.skipped_reason = "phone_normalize_failed"
        return result

    result.extra_call_target = target
    result.fired = True
    result.source = source
    if source == "reply" or _extract_phones(reply_text or ""):
        result.strip_phones_from_reply = True
    if source == "reply":
        result.reason = "intent_plus_name_plus_phone_in_reply"
    else:
        # ``kb:<kind>``
        result.reason = f"intent_plus_name_plus_phone_in_{source}"

    _escalation_trigger = (
        "employee_not_responding"
        if _employee_not_responding is not None
        else (
            (_store_arrival.trigger if _store_arrival else "store_arrival")
            if _arrival_gated_intent
            else ("reply_offer" if reply_offer_name else "customer_staff_intent")
        )
    )
    _already_sent = contact_already_sent(
        _contacts_sent,
        name=target.name,
        phone=wa_id,
    )
    log_contact_escalation(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        trigger=_escalation_trigger,
        context=_store_arrival.context if _store_arrival else "-",
        name_source=name_source or "-",
        already_sent=_already_sent,
        selected_contact=target.name,
        contacts_sent_count=len(_contacts_sent),
        policy_allowed=_policy_allowed if _arrival_signal else None,
    )
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


def _looks_like_store_link_request(
    customer_msg: str,
    *,
    history: Optional[List[Dict[str, Any]]] = None,
    order_prep: Any = None,
) -> bool:
    msg = _normalise_for_match(customer_msg)
    if not msg:
        return False
    # Drop punctuation that fragments the phrase match.
    msg_compact = re.sub(r"[؟?,،.!:;\-\u060c]+", " ", msg)
    msg_compact = re.sub(r"\s+", " ", msg_compact).strip()
    try:
        from modules.ai.brain.intent.link_disambiguation import (  # noqa: PLC0415
            should_suppress_store_link_intent,
        )
        if should_suppress_store_link_intent(
            customer_msg or "",
            history=history,
            order_prep=order_prep,
        ):
            return False
    except Exception:  # noqa: BLE001
        pass
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
    history: Optional[List[Dict[str, Any]]] = None,
    order_prep: Any = None,
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

    if not _looks_like_store_link_request(
        customer_msg or "",
        history=history,
        order_prep=order_prep,
    ):
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
    "موقع المتجر",
    "موقع المعرض",
    "موقع المحل",
    "وين أنتم",
    "وين انتم",
    "وين المعرض",
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
    "الفروع",
    "ابغى الفروع",
    "أبغى الفروع",
    "ابي الفروع",
    "أبي الفروع",
    "branches",
    "فروع",
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
    "ارسل اللوكيشن",
    "أرسل اللوكيشن",
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
    try:
        from modules.ai.brain.intent.link_disambiguation import (  # noqa: PLC0415
            looks_like_physical_location_request,
        )
        if looks_like_physical_location_request(customer_msg or ""):
            return True
    except Exception:  # noqa: BLE001
        pass
    msg = _normalise_for_match(customer_msg)
    if not msg:
        return False
    msg_compact = re.sub(r"[؟?,،.!:;\-\u060c]+", " ", msg)
    msg_compact = re.sub(r"\s+", " ", msg_compact).strip()
    for phrase in _LOCATION_LINK_TRIGGERS_PHRASE:
        if phrase in msg_compact:
            return True
    return False


# Prose patterns that mean "the LLM didn't have a maps URL in
# context, so it's asking the customer for a branch / city /
# inquiry type". When the safety net is about to inject a URL,
# these prose-style replies need to be REPLACED, not appended to —
# otherwise the customer sees a contradictory message:
#
#   Bot: "أخبرنا أي فرع تبحث عنه لنرسل لك الموقع 🌷"
#        "موقعنا 📍"
#        "https://maps.app.goo.gl/…"
#
# The first sentence asks for a branch; the second sentence
# IS the branch's location. Customers find this jarring and
# the merchant gets blamed for "the bot doesn't know it sent
# the location". May 2026 #38 added these markers so the
# location safety net classifies the LLM's "prose fallback"
# as bare-intro-shaped and replaces it cleanly.
_LOCATION_REDUNDANT_PROSE_MARKERS: tuple = (
    "لنبعث لك",
    "لنرسل لك",
    "نرسل لك الموقع",
    "نبعث لك الموقع",
    "نرسلك الموقع",
    "اسم الفرع",
    "اسم المدينة",
    "اي فرع",
    "أي فرع",
    "ايش الفرع",
    "وش الفرع",
    "بنوع الاستفسار",
    "نوع الاستفسار",
    "اخبرنا بنوع",
    "أخبرنا بنوع",
    "اخبرنا بالفرع",
    "أخبرنا بالفرع",
    "خبرنا بنوع",
    "خبرنا بالفرع",
    "اعطنا اسم",
    "أعطنا اسم",
    "عطنا اسم",
    "اي مدينة تبحث",
    "أي مدينة تبحث",
)


def _looks_like_bare_location_intro(reply: str) -> bool:
    """True when the LLM emitted a generic "here is our location"
    line that the safety net should REPLACE rather than append to.

    Two patterns count as bare:
      1. Short replies (≤60 chars) carrying one of the canonical
         "هذا موقعنا" / "تفضل الموقع" markers — the legacy heuristic.
      2. Replies of any length that match one of
         :data:`_LOCATION_REDUNDANT_PROSE_MARKERS` — the "ask the
         customer for a branch" prose the LLM emits when it didn't
         have a maps URL in context. May 2026 #38: these used to
         survive the bare-intro check (they're > 60 chars), so the
         safety net appended the URL and the customer saw a
         contradictory two-message reply.
    """
    if not reply:
        return True
    trimmed = (reply or "").strip()
    norm = _normalise_for_match(trimmed)
    short = len(trimmed) <= 60
    has_legacy_marker = any(
        m in norm for m in _GENERIC_HERE_IS_THE_LOCATION_MARKERS
    )
    if short and has_legacy_marker:
        return True
    has_redundant_prose = any(
        m in norm for m in _LOCATION_REDUNDANT_PROSE_MARKERS
    )
    return has_redundant_prose


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
    ``{"structured_branch", "snapshot", "store_settings", "kb:<kind>", "none"}``. Never
    raises; degrade to the next layer on any failure.

    Logs a single ``[MAPS_LINK_RESOLVER]`` INFO line per call so
    production traffic can be audited via grep without enabling
    DEBUG. Mirrors the ``[STORE_LINK_RESOLVER]`` shape.
    """
    if db is None or not tenant_id:
        return "", "none"
    tenant_id = int(tenant_id)

    # ── 0) Structured branch maps (Operations Center PR-A) ───────────
    try:
        from modules.operations.branch_contact_evidence import (  # noqa: PLC0415
            lookup_structured_maps_url,
            structured_branch_contacts_enabled,
        )

        if structured_branch_contacts_enabled():
            url, src, branch_id = lookup_structured_maps_url(
                db, tenant_id, message="",
            )
            if url:
                logger.info(
                    "[MAPS_LINK_RESOLVER] tenant_id=%s source=%s "
                    "branch_id=%s url_len=%d",
                    tenant_id, src, branch_id or "-", len(url),
                )
                return url, src
    except Exception as exc:  # noqa: silent-ok - structured maps lookup must not block legacy resolver chain
        logger.debug(
            "safety_nets.maps_link | structured branch lookup failed "
            "tenant=%s err=%s", tenant_id, exc,
        )

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
        from core.knowledge import apply_ai_visible_kb_query_filters  # noqa: PLC0415
        rows = (
            apply_ai_visible_kb_query_filters(
                db.query(MerchantKnowledgeSection)
            )
            .filter(
                MerchantKnowledgeSection.tenant_id == tenant_id,
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


def _lookup_tenant_store_name(db: Any, tenant_id: int) -> str:
    if db is None or not tenant_id:
        return ""
    try:
        from core.store_knowledge import StoreKnowledgeLoader  # noqa: PLC0415

        profile = StoreKnowledgeLoader(db, int(tenant_id)).store_profile() or {}
        for key in ("store_name", "name", "title"):
            val = str(profile.get(key) or "").strip()
            if val:
                return val
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "safety_nets.maps_link | store_name lookup failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
    return ""


def _build_location_reply(
    maps_url: str,
    *,
    store_name: str = "",
    branch_name: str = "",
    city: str = "",
    district: str = "",
    address: str = "",
    has_branch_details: bool = False,
) -> str:
    """Canonical Arabic reply when a maps URL is available.

    When structured branch data exists, include branch context before
    the URL so WhatsApp can still lift the URL into a CTA button.
    """
    header_name = (branch_name or store_name or "").strip()
    lines: List[str] = []

    if has_branch_details and header_name:
        lines.append(f"📍 هذا موقع {header_name}")
    elif header_name:
        lines.append(f"📍 هذا موقع {header_name}")
    else:
        lines.append("📍 هذا موقعنا على خرائط Google")

    if has_branch_details:
        branch_loc_parts = [p for p in (city, district) if p]
        if branch_name and branch_loc_parts:
            lines.append(f"الفرع: {branch_name} – {' – '.join(branch_loc_parts)}")
        elif branch_loc_parts:
            lines.append(f"الفرع: {' – '.join(branch_loc_parts)}")
        elif branch_name:
            lines.append(f"الفرع: {branch_name}")

        if address:
            lines.append(f"العنوان: {address}")

        lines.append("")
        lines.append("اضغط الزر لفتح الموقع في خرائط Google.")
    else:
        lines.append("اضغط الزر لفتح الموقع.")

    lines.append(maps_url)
    return "\n".join(lines)


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

    branch_name = ""
    city = ""
    district = ""
    address = ""
    has_branch_details = False
    store_name = _lookup_tenant_store_name(db, tenant_id)

    if maps_url and source == "structured_branch":
        try:
            from modules.operations.branch_contact_evidence import (  # noqa: PLC0415
                resolve_branch_for_message,
                structured_branch_contacts_enabled,
            )

            if structured_branch_contacts_enabled():
                branch = resolve_branch_for_message(
                    db, tenant_id, customer_msg or "",
                )
                if branch is not None:
                    branch_name = branch.name or ""
                    city = branch.city or ""
                    district = branch.district or ""
                    address = branch.address or ""
                    has_branch_details = bool(
                        branch_name or city or district or address
                    )
        except Exception as exc:  # noqa: silent-ok - branch context is optional enrichment
            logger.debug(
                "safety_nets.maps_link | branch context lookup failed "
                "tenant=%s err=%s",
                tenant_id,
                exc,
            )

    if maps_url:
        location_block = _build_location_reply(
            maps_url,
            store_name=store_name,
            branch_name=branch_name,
            city=city,
            district=district,
            address=address,
            has_branch_details=has_branch_details,
        )
        if _looks_like_bare_location_intro(reply_text or ""):
            result.new_reply = location_block
        elif reply_text and reply_text.strip():
            sep = "\n" if reply_text.endswith("\n") else "\n\n"
            result.new_reply = reply_text.rstrip() + sep + location_block
        else:
            result.new_reply = location_block
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
    history: Optional[List[Dict[str, Any]]] = None,
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

    if intent == _INTENT_STORE_LINK:
        try:
            from modules.ai.brain.intent.link_disambiguation import (  # noqa: PLC0415
                should_suppress_store_link_intent,
            )
            if should_suppress_store_link_intent(
                customer_msg or "",
                history=history,
            ):
                result.skipped_reason = "tracking_link_not_store_link"
                return result
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[SAFETY_NET:store_link] tracking_link_suppression_failed err=%s",
                exc,
            )

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


# ── Active-order context markers (May 2026 #45) ────────────────
#
# Production complaint on Tenant 33 (May 25 KSA): customer ran
# through the full sales flow — picked the product, confirmed
# quantity, agreed to the price — and then sent their shipping
# data (name + phone + city + district) UNPROMPTED. The bot's
# last outbound was the price confirmation, NOT one of the
# explicit "أرسل لي العنوان" markers below, so
# ``_bot_was_awaiting_delivery`` returned False, the rewrite
# path stayed asleep, and the LLM dismissed the address as
# out_of_scope.
#
# Architectural fix: also treat the conversation as "ready for
# delivery info" when recent outbounds carry ACTIVE-ORDER context
# (price / currency / quantity / checkout language). The customer
# typing their address while the bot was just confirming a price
# is the natural next step — never out_of_scope.
#
# We keep this list narrow + high-signal so it can't accidentally
# fire on a generic catalogue browse. A real catalogue listing
# mentions products without committing the customer; an active
# order has either a currency-tagged price OR an explicit
# quantity + checkout cue paired with a single product focus.
_ACTIVE_ORDER_MARKERS: tuple = (
    # Currency / price tokens — always paired with digits in real
    # outbound text, but the substring scan is enough here.
    "ريال",
    "ر.س",
    "ر.س.",
    "ر س",
    "sar",
    # Order-progress phrases the bot uses after price confirmation
    "تأكيد الطلب", "تاكيد الطلب", "نأكد الطلب", "ناكد الطلب",
    "نكمل الطلب", "نكمل طلبك", "نكمل بعدها", "نكمل معك",
    "اكمل الطلب", "اكمال الطلب",
    "متابعة الطلب", "متابعه الطلب",
    "تأكيد الكمية", "تاكيد الكميه",
    "السعر الإجمالي", "السعر الاجمالي",
    "المبلغ الإجمالي", "المبلغ الاجمالي",
    "الإجمالي", "الاجمالي",
    "المجموع",
    # Quantity confirmation phrases
    "الكمية المطلوبة", "الكميه المطلوبه",
    "كم العدد", "العدد المطلوب",
    "وحدة", "وحده", "وحدتين", "وحدات",
    "زجاجة", "زجاجه", "زجاجتين",
    "علبة", "علبه", "علبتين",
    # Direct checkout cues
    "نرسل لك رابط الدفع", "نرسل رابط الدفع",
    "رابط الدفع", "رابط دفع",
    "بعد ما تأكد", "بعد ما تاكد",
)


def _history_in_active_order_context(
    history: Optional[List[Dict[str, Any]]],
) -> bool:
    """True when one of the last 3 outbounds contains an
    active-order marker — price + currency, quantity confirmation,
    or checkout cue.

    Used as a SECONDARY trigger for the delivery-info safety net so
    a customer typing "name + phone + city" right after a
    price-confirmation outbound (without the bot using one of the
    explicit "أرسل لي العنوان" markers) is still recognised as
    delivery info instead of a false out_of_scope.

    Conservative by design — never raises, and the threshold of 3
    outbounds is short enough that an old order from a previous
    session can't trigger it on a fresh discovery turn.
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
            if body:
                norm = _normalise_for_match(body)
                if any(m in norm for m in _ACTIVE_ORDER_MARKERS):
                    return True
            if outbound_seen >= 3:
                break
    except Exception:  # noqa: BLE001
        return False
    return False


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

    # ── Trigger condition (May 2026 #45 widened) ──────────────────
    # Two paths into the rewrite:
    #
    #   PATH A — explicit ask: bot's last 1-3 outbounds contained
    #            one of the "أرسل لي العنوان" markers. Same as
    #            April 2026 behaviour. Single delivery signal in
    #            the customer message is enough.
    #
    #   PATH B — active-order continuation: bot's last 3 outbounds
    #            contained an active-order marker (price+currency,
    #            quantity confirmation, checkout cue) AND the
    #            customer's message carries ≥ 2 distinct delivery
    #            fields. The stronger threshold guards against
    #            rewriting a bare "0552..." that's actually
    #            unrelated chatter from a customer who happens to
    #            be mid-order.
    #
    # Both paths still require ``_reply_looks_dismissive(reply_text)``
    # before we rewrite — when the LLM's reply is reasonable we
    # leave it alone.
    awaiting_explicit = _bot_was_awaiting_delivery(history)
    if awaiting_explicit:
        result.reason = "bot_was_awaiting_delivery_info"
    else:
        active_order = _history_in_active_order_context(history)
        if not active_order:
            result.skipped_reason = "bot_not_awaiting_delivery"
            return result
        result.reason = "active_order_continuation"

    if not _reply_looks_dismissive(reply_text):
        # Bot was waiting (or order is in flight), but the LLM's
        # reply isn't dismissive → the brain is handling it. Stay
        # out of the way — the merchant explicitly asked for
        # "احترام order context الجاري" without preventing
        # natural Brain responses.
        result.skipped_reason = "reply_not_dismissive"
        return result

    slots = _extract_delivery_signals(customer_msg or "")
    if not slots:
        result.skipped_reason = "no_delivery_signals_in_msg"
        return result

    # PATH B requires a STRONGER signal than PATH A — at least two
    # distinct delivery fields. This prevents rewriting on a bare
    # phone number or city name that may be unrelated to the order.
    if not awaiting_explicit:
        delivery_field_keys = (
            "phone", "city",
            "customer_name", "customer_first_name", "customer_last_name",
            "address_line", "short_address_code", "google_maps_url",
            "street", "district", "building_number",
        )
        signal_count = sum(1 for k in delivery_field_keys if slots.get(k))
        if signal_count < 2:
            result.skipped_reason = "active_order_context_but_weak_signal"
            return result
        result.reason = "active_order_continuation_strong_signal"

    result.extracted_slots = slots
    result.has_phone = bool(slots.get("phone"))
    result.new_reply = _compose_delivery_info_ack(slots)
    result.fired = True
    return result


# ══════════════════════════════════════════════════════════════════
# 6.5 Product Re-Ask Guard (May 2026 #47 — Tenant 33)
# ══════════════════════════════════════════════════════════════════
#
# Recurring regression on Tenant 33 (re-reported May 25 KSA after
# multiple "fixed-then-comes-back" cycles). The full transcript:
#
#   Customer: "أبي نص كيلو طلح بلدي"
#   Bot:      "نص كيلو طلح بلدي = 193 ريال"
#   Bot:      "أرسل لي موقعك على قوقل ماب أو الرمز الوطني المختصر
#              عشان نجهز الشحنة"
#   Customer: <Google Maps URL>
#   Bot:      "وصلني موقعك. قبل ما نكمل، اختر المنتج اللي تبغاه
#              من القائمة…"      ← BUG: product was already chosen!
#
# Why ``apply_delivery_info_context_net`` doesn't catch this on its
# own: the bot's reply ISN'T dismissive in the "خارج تخصصي" /
# "didn't understand" sense — it's actually a "re-ask the product"
# loop the brain emits when its in-turn slot extractor fails to
# carry the product across the address-collection turn. The
# delivery-info net only rewrites dismissive replies, so this
# regression slipped through the previous safety chain.
#
# Architectural fix (smallest possible): a dedicated narrow guard
# that fires ONLY when ALL three signals line up:
#
#   1. The bot's CURRENT reply contains a "re-ask product" phrase
#      ("اختر المنتج", "أي منتج", "حدد المنتج", "من القائمة", …).
#   2. The customer's CURRENT inbound carries a location signal
#      (Google Maps URL, geo coords, national short code,
#      "العنوان الوطني" / "موقعي" / "موقعك").
#   3. The recent history (last 3 outbounds) carries an
#      active-order marker (price+currency, quantity confirm,
#      checkout cue) — the existing ``_history_in_active_order_context``
#      helper. This is the proof that product+price+quantity were
#      already discussed.
#
# All three together is the ONLY combination that proves the
# brain is contradicting its own recent history. We deliberately
# do NOT mutate the order state — we only rewrite the outbound
# text into an order-continuation ACK so the customer doesn't see
# the contradictory "اختر المنتج" line. The next turn flows
# through the regular pipeline with the slot extractor working
# from the now-fresh history.
#
# This is the THIRD time the same class of bug has surfaced
# ("product context lost across address-collection turn") under
# slightly different brain behaviours — so we lock the regression
# down with a guard that doesn't depend on the brain's internal
# slot extractor working perfectly.

_PRODUCT_REASK_MARKERS: tuple = (
    # Direct "pick the product" re-asks. Normalised forms — the
    # caller passes the reply through ``_normalise_for_match``
    # before scanning so ة → ه, ي → ي, etc. are already collapsed.
    "اختر المنتج",
    "اختاري المنتج",
    "اختار المنتج",
    "اخترالمنتج",
    "حدد المنتج",
    "حددي المنتج",
    "حدد لي المنتج",
    "حددي لي المنتج",
    "اسم المنتج",
    "وش اسم المنتج",
    "ايش اسم المنتج",
    "وش المنتج",
    "ايش المنتج",
    "اي منتج",
    "أي منتج",
    "اي منتج تبغ",
    "أي منتج تبغ",
    "اي منتج تبي",
    "أي منتج تبي",
    "اي منتج تحب",
    "أي منتج تحب",
    "اي منتج تريد",
    "أي منتج تريد",
    # "من القائمة" / "من قائمة المنتجات" — this is the literal
    # phrase from the screenshot. Always paired with one of the
    # verbs above; we still match it stand-alone for safety.
    "من القائمه",
    "من قائمه",
    "من قائمه المنتجات",
    "اختر من القائمه",
    "اختار من القائمه",
    "اختر من المنتجات",
    "اختار من المنتجات",
    # "تبغى تطلب أيش" / "وش تبغى" with product hint — caught via
    # "وش المنتج" / "ايش المنتج" already.
)


# Location-signal markers in the customer's CURRENT inbound that
# prove they're answering an address/location ask — independent of
# whether the slot extractor caught structured fields. Used as a
# belt-and-braces check on top of ``_extract_delivery_signals``.
_CUSTOMER_LOCATION_INBOUND_MARKERS: tuple = (
    # URL fingerprints — these survive ``_normalise_for_match``
    # because the function normalises Arabic only.
    "maps.google",
    "google.com/maps",
    "goo.gl/maps",
    "maps.app.goo.gl",
    "maps.app.gl",
    "/maps/",
    "geo:",
    # National address code prefixes (Saudi short address is 4
    # letters + 4 digits, e.g. "RAKB1234" — we also match the
    # human prefix the merchant requests).
    "العنوان الوطني",
    "الرمز الوطني",
    "الرمز المختصر",
    "العنوان المختصر",
    # Self-references that are nearly always location-shaped when
    # they arrive after a "send me your location" ask.
    "موقعي",
    "موقعك",  # bot may quote it back — but appears in inbound too
    "هذا موقعي",
    "هذي موقعي",
    "هذا الموقع",
    "ذا موقعي",
)


def _customer_inbound_has_location(customer_msg: str) -> bool:
    """True when the customer's inbound carries a location signal —
    Maps URL, geo coords, national short code, or an explicit
    "موقعي / العنوان الوطني" mention.

    Two-layer check: first a substring scan over the inbound
    markers (catches the URL shapes), then the structured slot
    extractor used by the delivery-info net (catches normalised
    coords / short codes that the substring scan would miss).
    Never raises.
    """
    if not customer_msg:
        return False
    raw = str(customer_msg)
    raw_lower = raw.lower()
    norm = _normalise_for_match(raw)
    for m in _CUSTOMER_LOCATION_INBOUND_MARKERS:
        if m in norm or m.lower() in raw_lower:
            return True
    # Structured signals via the existing extractor.
    try:
        slots = _extract_delivery_signals(raw)
    except Exception:  # noqa: BLE001
        slots = {}
    return bool(
        slots.get("google_maps_url")
        or slots.get("short_address_code")
        or (slots.get("latitude") is not None and slots.get("longitude") is not None)
    )


def _reply_looks_like_product_reask(reply_text: str) -> bool:
    """True when the bot's reply is asking the customer to pick a
    product — either with an explicit "اختر المنتج" phrasing or a
    "من القائمة" cue. The marker list is intentionally narrow so
    we don't over-fire on legitimate up-sell prompts ("هل تحب
    تضيف منتج آخر؟" stays untouched)."""
    if not reply_text:
        return False
    norm = _normalise_for_match(reply_text)
    return any(m in norm for m in _PRODUCT_REASK_MARKERS)


@dataclass
class ProductReaskGuardResult:
    fired: bool = False
    reason: str = ""
    skipped_reason: str = ""
    has_maps_url: bool = False
    has_short_address: bool = False
    new_reply: str = ""

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "kind":               "product_reask_guard",
            "fired":              self.fired,
            "reason":             self.reason or self.skipped_reason,
            "has_maps_url":       self.has_maps_url,
            "has_short_address":  self.has_short_address,
        }


# Feature flag — defaults ON. Same convention as the other safety
# nets so ops can flip the switch without a redeploy if a hot
# regression emerges.
def product_reask_guard_enabled() -> bool:
    raw = os.environ.get("PRODUCT_REASK_GUARD_ENABLED", "true")
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


# Order-continuation ACK copies. Two variants — one when the
# customer also sent name+phone (data is complete enough), one
# generic when only the location arrived. Both NEVER promise a
# specific shipping carrier or price; that comes from the
# merchant's KB / catalogue on the next turn.
_ORDER_CONTINUATION_ACK_LOCATION_ONLY = (
    "وصلني موقعك 🌷 باقي نحتاج الاسم ورقم الجوال لو ما وصلوني، "
    "وبنجهز الطلب ونرسل لك طريقة الدفع."
)
_ORDER_CONTINUATION_ACK_LOCATION_FULL = (
    "وصلني موقعك 🌷 بيانات الشحن اكتملت، بنجهز الطلب ونرسل لك "
    "طريقة الدفع/التأكيد."
)


def apply_product_reask_guard(
    *,
    customer_msg: str,
    reply_text: str,
    history: Optional[List[Dict[str, Any]]] = None,
) -> ProductReaskGuardResult:
    """Block the brain from asking the customer to re-pick a product
    when (a) the brain's reply is a product re-ask, (b) the
    customer's inbound is a location/address signal, and
    (c) the recent history shows an active order (product +
    price/quantity already confirmed).

    Pure text rewrite — never mutates order state, never deletes
    markers. The next turn flows through the regular slot extractor
    with the brain's history caching freshly invalidated.

    Returns a :class:`ProductReaskGuardResult` with ``fired=True``
    and ``new_reply`` populated when the rewrite should happen. The
    webhook substitutes ``new_reply`` for the outbound text in that
    case; otherwise the LLM's reply ships unchanged.
    """
    result = ProductReaskGuardResult()

    if not product_reask_guard_enabled():
        result.skipped_reason = "flag_disabled"
        return result

    if not reply_text or not reply_text.strip():
        result.skipped_reason = "empty_reply"
        return result

    if not _reply_looks_like_product_reask(reply_text):
        result.skipped_reason = "reply_not_product_reask"
        return result

    if not _customer_inbound_has_location(customer_msg or ""):
        # Customer didn't actually send a location — the brain may
        # be legitimately asking which product they want. Stay
        # out of the way.
        result.skipped_reason = "inbound_not_location"
        return result

    if not _history_in_active_order_context(history):
        # No active-order context in recent outbounds — without
        # proof that product+price/quantity were just discussed
        # we can't claim the brain is contradicting itself.
        result.skipped_reason = "no_active_order_context"
        return result

    # All three guards lined up — the brain IS contradicting recent
    # context. Pick an ACK shape based on whether the customer also
    # sent name+phone alongside the location.
    try:
        slots = _extract_delivery_signals(customer_msg or "")
    except Exception:  # noqa: BLE001
        slots = {}

    has_name = bool(
        slots.get("customer_name")
        or slots.get("customer_first_name")
        or slots.get("customer_last_name")
    )
    has_phone = bool(slots.get("phone"))
    has_maps_url = bool(slots.get("google_maps_url"))
    has_short_address = bool(slots.get("short_address_code"))

    if has_name and has_phone:
        result.new_reply = _ORDER_CONTINUATION_ACK_LOCATION_FULL
    else:
        result.new_reply = _ORDER_CONTINUATION_ACK_LOCATION_ONLY

    result.fired = True
    result.reason = "product_reask_after_location_in_active_order"
    result.has_maps_url = has_maps_url
    result.has_short_address = has_short_address
    return result


# ══════════════════════════════════════════════════════════════════
# 7. Outbound Artifact Guard — hollow-affirmation rewriter
#    (May 2026 #37 — D2 / "guard outbound artifact promises")
# ══════════════════════════════════════════════════════════════════
#
# Final safety net. Runs AFTER every other net has had its turn.
# Catches the residual case where the customer asked for a
# concrete artifact (a phone number, a payment barcode, a maps
# URL, a store URL) and the LLM replied with a short
# affirmation that *sounds* like delivery — "أبشر", "تفضل",
# "تم", "حاضر" — but contains no actual artifact. Pre-fix
# (production trace, May 2026 #36 follow-up) the customer saw:
#
#     Customer: "عطني رقم أمين"
#     Bot:      "أبشر 🌷"
#     Customer: "هيا عطني"
#     Bot:      "تفضل أبو خلف 🌷"
#     Customer: "ما جاني شي"
#
# That is worse than honest unavailability — it implies a
# delivery the customer never receives.
#
# Guard contract:
#   1. Classify the customer's expected artifact (or "none").
#   2. Probe the post-net reply for actual delivery (phone digits
#      / maps URL / store URL / barcode media).
#   3. If satisfied → ``action="pass"`` and no rewrite.
#   4. If NOT satisfied AND the reply is a HOLLOW affirmation
#      (short, "أبشر"-shaped) → rewrite to either an injected
#      artifact (when resolvable) or an honest "غير مضاف
#      حاليًا" fallback.
#   5. If NOT satisfied AND the reply is natural prose (longer,
#      explanatory) → pass-through. We never override a
#      substantive reply.
#   6. If the reply ALREADY uses an honest "غير متوفر" /
#      "لم تتم إضافة" phrase → pass-through. Merchants who
#      coached the AI into honesty get to keep that voice.
#
# Platform-level: every merchant gets the guard with no opt-in.
# All resolutions go through the same chains the upstream nets
# use (``_lookup_tenant_store_url``, ``_lookup_tenant_maps_url``,
# ``_lookup_staff_phone_in_kb``) so configuration stays in one
# place.

# Hollow affirmation tokens — short standalone phrases that
# *promise* delivery without delivering. The order doesn't matter;
# we run a substring check against the alif-folded reply.
_HOLLOW_AFFIRMATION_TOKENS: Tuple[str, ...] = (
    "أبشر", "ابشر",
    "تفضل", "تفضلي",
    "تم",
    "حاضر",
    "أكيد", "اكيد",
    "أرسلت لك", "ارسلت لك",
    "أرسلتها", "ارسلتها",
    "خذ",
    "هذا هو", "هذي هي",
    "هذا الرقم", "هذا رقمه",
    "خذ الرقم",
)


# Honest "the asset isn't on file" tokens. When any one of
# these appears in the reply, the guard backs off — the merchant
# (or the merchant's coached prompt) is already telling the
# customer the truth, and we don't want to replace that with our
# canned line.
_HONEST_UNAVAILABLE_TOKENS: Tuple[str, ...] = (
    "غير متوفر", "غير متوفرة",
    "غير مضاف", "غير مضافة", "غير مضافه",
    "غير مدخل", "غير مدخلة",
    "غير مسجل", "غير مسجلة",
    "لم تتم إضافة", "لم يتم إضافة", "لم نضف",
    "لم تضاف", "لم يضاف",
    "ما تم إضافة", "ما تم اضافة",
    "لا يتوفر", "ما يوجد رقم", "ما عندنا رقم",
    "ما عندنا باركود", "ما توجد صورة باركود",
    "ليس لدينا رقم", "ليس عندنا رقم",
    "أحتاج إضافة", "احتاج إضافة", "احتاج اضافة",
)


# Customer-side lexicon — what does the customer want?
#
# Each artifact class has TWO axes:
#   * a "carrier" keyword set (رقم / جوال / باركود / موقع / رابط)
#   * a "subject" keyword set (اسم/دور موظف / بنك / متجر / موقع)
# The classifier requires BOTH axes to fire so a question like
# "وش رقم الطلب؟" doesn't get mistaken for a staff-phone ask.

_STAFF_PHONE_CARRIER_KEYWORDS: Tuple[str, ...] = (
    "رقم", "جوال", "موبايل", "واتساب", "وتساب",
    "اتصل", "اتصال", "تواصل", "كلمه",
)


# Role nouns — same surface area as the orphan-staff audit
# heuristic in :mod:`modules.ai.knowledge.improvement_advisor`.
# A staff role keyword + a phone-carrier keyword on the same
# inbound message is the strongest "the customer wants a
# specific person's contact" signal.
_STAFF_ROLE_KEYWORDS: Tuple[str, ...] = (
    "بائع المعرض", "بائع",
    "محاسب", "المحاسب",
    "كاشير", "الكاشير",
    "مسؤول", "المسؤول", "مسؤولة",
    "إدارة", "الإدارة", "الادارة",
    "خدمة العملاء", "الدعم",
    "موظف", "الموظف",
    "المالك", "صاحب المتجر",
)


_BARCODE_CARRIER_KEYWORDS: Tuple[str, ...] = (
    "باركود", "بار كود", "بار-كود",
    "qr", "كيوار", "كيو ار", "كيو-ار",
    "كود التحويل", "كود الدفع",
)


_BARCODE_BANK_KEYWORDS: Tuple[str, ...] = (
    "الراجحي", "الراجحى",
    "الأهلي", "الاهلي",
    "إنماء", "الإنماء", "الانماء",
    "البلاد", "الجزيرة", "الرياض",
    "ساب", "الفرنسي",
    "تحويل", "بنكي", "بنك", "حساب",
    "stcpay", "stc pay", "stc-pay", "اس تي سي",
)


# Delivery-complaint markers — short customer messages that
# essentially mean "the artifact you promised never arrived /
# I didn't get it / where is it?". When the CURRENT customer
# message is one of these AND the PRIOR customer message had an
# artifact intent (rقم/باركود/موقع/رابط), the artifact guard
# should treat the prior intent as carried over.
#
# Pre-fix (May 2026 #38) the guard only inspected the current
# inbound, so a sequence like:
#   Customer: "عطني رقم أمين"
#   Bot:      "أبشر 🌷"
#   Customer: "ما جاني شي"
#   Bot:      "خبّرنا بنوع الاستفسار وسنوصلك بالشخص المختص 🌷"
# left the misleading bot reply on the wire — the guard
# classified "ما جاني شي" as ``expected="none"`` and bailed.
_ARTIFACT_COMPLAINT_MARKERS: Tuple[str, ...] = (
    "ما جاني",
    "ما جاءني",
    "ما وصل",
    "ما وصلني",
    "ما استلمت",
    "ما استلمته",
    "وين الرقم",
    "وين رقم",
    "وين الرابط",
    "وين رابط",
    "وين الباركود",
    "وين باركود",
    "وين الموقع",
    "وين موقع",
    "أين الرقم",
    "أين الرابط",
    "أين الباركود",
    "أين الموقع",
    "هيا عطني",
    "هيا اعطني",
    "هيا أعطني",
    "ابعث",   # bare imperative — only fires when prior turn had artifact intent
    "ارسل",
    "أرسل",
    "ارسله",
    "أرسله",
    "وين هو",
    "ما شي",
    "مافي شي",
    "ما فيه شي",
    "لا شيء",
    "لاشيء",
)


def _is_artifact_complaint(customer_msg: str) -> bool:
    """True when the customer's message reads as a complaint that
    a previously-asked-for artifact didn't arrive. Short and
    imperative patterns only — we never use this as a primary
    classifier, only as a "carry the prior intent forward" hint.
    """
    if not customer_msg:
        return False
    norm = _normalise_for_match(customer_msg)
    norm = re.sub(r"[؟?,،.!:;\-\u060c]+", " ", norm)
    norm = re.sub(r"\s+", " ", norm).strip()
    if not norm:
        return False
    # Cap on length so a long explanation never accidentally
    # matches one of these markers.
    if len(norm) > 60:
        return False
    return any(m in norm for m in _ARTIFACT_COMPLAINT_MARKERS)


def _last_customer_msg_from_history(
    history: Optional[List[Dict[str, Any]]],
) -> str:
    """Return the most recent inbound (customer-side) message body
    from a conversation history list, skipping the message that
    triggered THIS turn (the latest user entry — the caller
    already has it as ``customer_msg``). Empty string when no
    such message exists.

    History shape mirrors the rest of the safety-nets module: a
    list of dicts with ``role`` and ``body`` (or ``content``)
    fields. We tolerate either key for resilience.
    """
    if not history or not isinstance(history, list):
        return ""
    seen_current = False
    for entry in reversed(history):
        if not isinstance(entry, dict):
            continue
        role = (entry.get("role") or "").lower()
        body = entry.get("body") or entry.get("content") or ""
        if not isinstance(body, str):
            continue
        if role not in {"user", "customer", "human", "in", "inbound"}:
            continue
        if not seen_current:
            # Skip the most recent user turn — that's the message
            # the guard is processing right now.
            seen_current = True
            continue
        if body.strip():
            return body
    return ""


# Reply-side delivery probes
#
# A Saudi mobile number in the reply body — the simplest
# "did the LLM actually send the number?" check. Same regex
# shape used by the audit pass and the staff KB scanner so
# behaviour stays consistent across modules.
_REPLY_PHONE_RE = re.compile(r"(?:\+?9665\d{8}|0?5\d{8})")


# wa.me / api.whatsapp.com / wa.link — these are tappable
# WhatsApp deep-links the LLM occasionally sends in lieu of
# raw digits. Counts as artifact-satisfied for staff phone.
_REPLY_WA_LINK_RE = re.compile(
    r"(?:wa\.me/|api\.whatsapp\.com/|wa\.link/)\d+",
    re.IGNORECASE,
)


# Maps URL hosts the LLM is allowed to ship as a "location"
# answer. Mirrors :data:`_MAPS_HOST_HINTS` used by the location
# net, but expressed as a single regex for the artifact probe.
_REPLY_MAPS_HOST_RE = re.compile(
    r"(?:google\.com/maps|goo\.gl/maps|maps\.app\.goo\.gl|maps\.google)",
    re.IGNORECASE,
)


# Any URL — used for the store-link probe. We deliberately
# subtract maps URLs at the call site so a maps URL doesn't
# accidentally satisfy a store-link request.
_REPLY_ANY_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass
class OutboundArtifactGuardResult:
    """Outcome of the hollow-affirmation guard.

    ``expected_artifact`` is one of:
      * ``"staff_phone"``     — customer asked for a staff/role phone
      * ``"payment_barcode"`` — customer asked for a payment QR / barcode
      * ``"maps_link"``       — customer asked for the physical location
      * ``"store_link"``      — customer asked for the e-commerce URL
      * ``"none"``            — no recognised artifact intent

    ``action`` is one of:
      * ``"pass"``                          — no rewrite, reply unchanged
      * ``"inject_staff_phone"``            — KB lookup hit, phone inserted
      * ``"rewrite_missing_staff_phone"``   — fallback "أحتاج إضافة رقم …"
      * ``"rewrite_missing_barcode"``       — fallback "صورة الباركود غير مضافة"
      * ``"inject_maps_link"``              — config has a maps URL, appended
      * ``"rewrite_missing_maps_link"``     — fallback "موقع المعرض غير مضاف"
      * ``"inject_store_link"``             — config has a store URL, replaced
      * ``"rewrite_missing_store_link"``    — fallback "رابط المتجر غير مضاف"
    """
    fired: bool = False
    expected_artifact: str = "none"
    artifact_satisfied: bool = False
    rewrote_reply: bool = False
    new_reply: str = ""
    action: str = "pass"
    skipped_reason: str = ""

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "kind": "outbound_artifact_guard",
            "fired": self.fired,
            "expected_artifact": self.expected_artifact,
            "artifact_satisfied": self.artifact_satisfied,
            "rewrote_reply": self.rewrote_reply,
            "action": self.action,
            "skipped_reason": self.skipped_reason,
        }


def _strip_decoration(text: str) -> str:
    """Drop emojis, punctuation, and digit runs so we can size the
    reply by the *meaningful* characters only. Used by the hollow
    detector — a reply like ``"تفضل 🌷🌷🌷"`` is hollow even
    though its raw length is > 8 chars."""
    if not text:
        return ""
    no_url = _REPLY_ANY_URL_RE.sub(" ", text)
    no_emoji = re.sub(r"[^\w\s\u0621-\u064a]", " ", no_url, flags=re.UNICODE)
    no_digits = re.sub(r"\d+", " ", no_emoji)
    return re.sub(r"\s+", " ", no_digits).strip()


def _is_hollow_affirmation(reply: str) -> bool:
    """True when the reply is short AND its meaningful content is
    dominated by a vague affirmation token. The ~80-char ceiling is
    intentional — anything longer is treated as natural prose
    that the merchant's prompt produced for a reason."""
    if not reply:
        return True
    stripped = _strip_decoration(reply)
    if len(stripped) == 0:
        return True
    if len(stripped) > 80:
        return False
    norm_reply = _normalise_alif(reply).lower()
    for tok in _HOLLOW_AFFIRMATION_TOKENS:
        if _normalise_alif(tok).lower() in norm_reply:
            return True
    return False


def _reply_already_honest(reply: str) -> bool:
    """True when the reply is already telling the customer the asset
    isn't on file. The guard backs off in this case so we don't
    overwrite a perfectly honest reply with a slightly different
    canned line (and we don't double-acknowledge unavailability)."""
    if not reply:
        return False
    norm = _normalise_alif(reply).lower()
    for tok in _HONEST_UNAVAILABLE_TOKENS:
        if _normalise_alif(tok).lower() in norm:
            return True
    return False


def _classify_expected_artifact(
    customer_msg: str,
    *,
    history: Optional[List[Dict[str, Any]]] = None,
    order_prep: Any = None,
) -> str:
    """Classify the inbound message into an artifact class.

    Order matters: we check the most specific intent first so a
    message like "وين موقع متجركم" lands on ``maps_link`` rather
    than ``store_link``. The two trigger sets are designed disjoint
    by :func:`_looks_like_store_link_request` /
    :func:`_looks_like_location_request`, so the order is just a
    safety belt.
    """
    if not customer_msg or not customer_msg.strip():
        return "none"

    norm_compact = _normalise_for_match(customer_msg)
    norm_compact = re.sub(r"[؟?,،.!:;\-\u060c]+", " ", norm_compact)
    norm_compact = re.sub(r"\s+", " ", norm_compact).strip()

    # 1. Maps-link intent (location / google maps / lookup)
    for phrase in _LOCATION_LINK_TRIGGERS_PHRASE:
        if phrase in norm_compact:
            return "maps_link"

    try:
        from modules.ai.brain.intent.link_disambiguation import (  # noqa: PLC0415
            should_suppress_store_link_intent,
        )
        if should_suppress_store_link_intent(
            customer_msg,
            history=history,
            order_prep=order_prep,
        ):
            return "none"
    except Exception:  # noqa: BLE001
        pass

    # 2. Store-link intent (online shop URL)
    for phrase in _STORE_LINK_TRIGGERS_PHRASE:
        if phrase in norm_compact:
            return "store_link"

    # 3. Payment barcode — needs a barcode-carrier keyword. Bank
    #    keyword strengthens the signal but isn't strictly required:
    #    "أبي الباركود" alone counts because barcodes only ever
    #    refer to payment delivery in this product.
    for kw in _BARCODE_CARRIER_KEYWORDS:
        if kw in norm_compact:
            return "payment_barcode"

    # 4. Staff-phone — requires a phone-carrier keyword AND either
    #    a recognised staff role or a recognised proper name. Pure
    #    "وش رقمكم" without role/name resolves to ``none`` so the
    #    upstream prompt (which knows the merchant's general
    #    contact) handles it.
    has_phone_carrier = any(
        kw in norm_compact for kw in _STAFF_PHONE_CARRIER_KEYWORDS
    )
    if has_phone_carrier:
        has_role = any(
            kw in norm_compact for kw in _STAFF_ROLE_KEYWORDS
        )
        has_name = _find_staff_name(norm_compact) is not None
        if has_role or has_name:
            return "staff_phone"

    return "none"


def _reply_has_phone(reply: str) -> bool:
    if not reply:
        return False
    if _REPLY_PHONE_RE.search(reply):
        return True
    if _REPLY_WA_LINK_RE.search(reply):
        return True
    return False


def _reply_has_maps_url(reply: str) -> bool:
    if not reply:
        return False
    return bool(_REPLY_MAPS_HOST_RE.search(reply))


def _reply_has_any_url(reply: str) -> bool:
    if not reply:
        return False
    return bool(_REPLY_ANY_URL_RE.search(reply))


def _media_attachments_have_barcode(
    media_attachments: Optional[List[Any]],
) -> bool:
    """Inspect the post-net media attachment list for a barcode/QR.

    A media item counts as a barcode when ANY of the following hold:
      * its ``link_role`` (or dict equivalent) is exactly ``"barcode"``;
      * its ``media_key`` contains "barcode" / "qr" / "payment";
      * its title contains "باركود" / "QR" / "كيوار".

    The check is lenient on shape so callers can pass either ORM
    rows (``media_link.media.media_key``) or pre-serialised dicts
    (``{"media": {"media_key": …}}``) without unwrapping first.
    """
    if not media_attachments:
        return False
    for att in media_attachments:
        link_role = ""
        media_key = ""
        title = ""
        try:
            if hasattr(att, "link_role"):
                link_role = (getattr(att, "link_role", "") or "").lower()
            elif isinstance(att, dict):
                link_role = (att.get("link_role") or "").lower()
            media_obj = (
                getattr(att, "media", None)
                if not isinstance(att, dict)
                else att.get("media")
            )
            if media_obj is not None:
                if isinstance(media_obj, dict):
                    media_key = (media_obj.get("media_key") or "").lower()
                    title = media_obj.get("title") or ""
                else:
                    media_key = (getattr(media_obj, "media_key", "") or "").lower()
                    title = getattr(media_obj, "title", "") or ""
            elif isinstance(att, dict):
                # ``MediaResolution.to_attachment()`` and the payment
                # barcode route emit flat dicts with top-level keys.
                media_key = (att.get("media_key") or "").lower()
                title = att.get("title") or ""
        except Exception:  # noqa: BLE001
            continue
        if link_role == "barcode":
            return True
        if any(hint in media_key for hint in ("barcode", "qr", "payment")):
            return True
        norm_title = _normalise_alif(title).lower()
        for hint in ("باركود", "بار كود", "qr", "كيوار", "كيو ار"):
            if _normalise_alif(hint).lower() in norm_title:
                return True
    return False


def _call_targets_have_phone(call_targets: Optional[List[Any]]) -> bool:
    """True when at least one resolved CallTarget carries a Saudi
    phone. Used as the second satisfaction probe for staff_phone —
    the reply might omit the digits because the LLM emitted a
    ``[CALL:…]`` marker that the marker-extractor already turned
    into a ``CallTarget``. Don't rewrite such replies."""
    if not call_targets:
        return False
    for ct in call_targets:
        phone = ""
        try:
            phone = (
                getattr(ct, "raw_phone", "")
                or getattr(ct, "phone_display", "")
                or getattr(ct, "wa_id", "")
                or ""
            )
            if not phone and isinstance(ct, dict):
                phone = (
                    ct.get("raw_phone")
                    or ct.get("phone_display")
                    or ct.get("wa_id")
                    or ""
                )
        except Exception:  # noqa: BLE001
            continue
        if phone and _REPLY_PHONE_RE.search(str(phone)):
            return True
    return False


def _detect_bank_label(customer_msg: str) -> str:
    """Return a short Arabic bank label found in the message, or ""
    if no recognised bank name appears. Used by the barcode
    rewrite so the canned line says
    ``"... صورة باركود الراجحي بعد"`` instead of a generic
    ``"... صورة الباركود ..."``.
    """
    if not customer_msg:
        return ""
    norm = _normalise_alif(customer_msg).lower()
    bank_pairs = (
        ("الراجحي", "الراجحي"),
        ("الراجحى", "الراجحي"),
        ("الاهلي", "الأهلي"),
        ("الأهلي", "الأهلي"),
        ("الانماء", "الإنماء"),
        ("الإنماء", "الإنماء"),
        ("انماء", "الإنماء"),
        ("البلاد", "البلاد"),
        ("الجزيرة", "الجزيرة"),
        ("الرياض", "الرياض"),
        ("ساب", "ساب"),
        ("stc pay", "STC Pay"),
        ("stcpay", "STC Pay"),
    )
    for needle, label in bank_pairs:
        if _normalise_alif(needle).lower() in norm:
            return label
    return ""


def apply_outbound_artifact_guard(
    db: Any,
    *,
    tenant_id: int,
    customer_msg: str,
    reply_text: str,
    media_attachments: Optional[List[Any]] = None,
    call_targets: Optional[List[Any]] = None,
    history: Optional[List[Dict[str, Any]]] = None,
    inbound_metadata: Optional[dict] = None,
    normalized_type: Optional[str] = None,
    conversation_id: Any = None,
) -> OutboundArtifactGuardResult:
    """Final hollow-affirmation guard.

    See module-level block "7. Outbound Artifact Guard" for the
    contract. The function never raises — every failure path
    returns a result object with ``skipped_reason`` set so the
    caller's structured log carries the diagnostic without
    propagating the exception into the webhook handler.

    ``history`` is optional but recommended — when the current
    customer message reads as a delivery complaint
    (``"ما جاني شي"`` / ``"وين الرقم"``) the guard inspects the
    PRIOR customer turn for artifact intent and carries it
    forward. Without history this fall-through is skipped and
    the guard behaves exactly like its pre-May-2026-#38 form.
    """
    result = OutboundArtifactGuardResult()

    origin_msg = (customer_msg or "").strip()
    try:
        from modules.ai.brain.commerce.customer_origin_intent import (  # noqa: PLC0415
            extract_customer_origin_text,
        )
        origin_msg, _ = extract_customer_origin_text(
            customer_msg or "",
            inbound_metadata=inbound_metadata,
            normalized_type=normalized_type,
        )
    except Exception:  # noqa: BLE001
        pass

    expected = _classify_expected_artifact(
        origin_msg or "",
        history=history,
    )

    # History-aware carry-forward (May 2026 #38).
    # If the current message classified as ``"none"`` AND it's
    # short / complaint-shaped AND the prior customer turn had
    # an artifact intent, carry that intent forward. This closes
    # the gap exposed by:
    #   Customer: "عطني رقم أمين"   ← classified=staff_phone
    #   Bot:      "أبشر 🌷"          ← guard fired correctly
    #   Customer: "ما جاني شي"      ← was classified=none, bailed
    # Without the carry-forward we leave the awkward
    # "خبّرنا بنوع الاستفسار وسنوصلك بالشخص المختص 🌷" reply
    # on the wire — exactly the production complaint.
    carryover = False
    if expected == "none" and _is_artifact_complaint(customer_msg or ""):
        prior_msg = _last_customer_msg_from_history(history)
        if prior_msg:
            prior_expected = _classify_expected_artifact(
                prior_msg,
                history=history,
            )
            if prior_expected != "none":
                expected = prior_expected
                carryover = True
                logger.info(
                    "[OUTBOUND_ARTIFACT_GUARD] tenant=%s "
                    "carryover=true prior_expected=%s "
                    "current_msg_len=%d prior_msg_len=%d",
                    int(tenant_id or 0), prior_expected,
                    len(customer_msg or ""), len(prior_msg),
                )
                # Use the prior message for downstream lookups
                # (name extraction, bank label) since the current
                # message is just a complaint.
                customer_msg = prior_msg

    result.expected_artifact = expected

    if expected == "payment_barcode":
        try:
            from core.payment_relevance_gate import (  # noqa: PLC0415
                PaymentRelevanceLogContext,
                validate_payment_outbound_artifact,
            )
            _prv = validate_payment_outbound_artifact(
                message=customer_msg or "",
                inbound_metadata=inbound_metadata,
                normalized_type=normalized_type,
                history=history,
                tenant_id=tenant_id,
                route="artifact_guard_barcode",
                log_context=PaymentRelevanceLogContext(
                    tenant_id=tenant_id,
                    message=customer_msg or "",
                    inbound_metadata=inbound_metadata,
                    normalized_type=normalized_type,
                    fallback_source="artifact_guard_barcode",
                    artifact=True,
                    final_action="dispatch_barcode",
                ),
            )
            if not _prv.allowed:
                result.skipped_reason = f"payment_relevance_gate:{_prv.reason}"
                return result
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[SAFETY_NET] payment_relevance_gate_failed err=%s",
                exc,
            )

    if expected == "none":
        result.skipped_reason = "no_artifact_intent"
        return result

    # When carryover fired AND the LLM's reply is the
    # asset-promise sanitizer's canned PHONE/LOCATION line, we
    # know that line is itself misleading (it promises an
    # escalation that never happens). Flag the reply as hollow
    # so the rewrite branch always runs in that case.
    if carryover and not _is_hollow_affirmation(reply_text or ""):
        norm_reply = _normalise_alif(reply_text or "").lower()
        misleading_handoff_markers = (
            "خبرنا بنوع الاستفسار",
            "خبرنا بالفرع",
            "وسنوصلك بالشخص المختص",
            "وسنوضح لك تفاصيل الموقع",
            "خبرنا بالمنطقة",
        )
        for m in misleading_handoff_markers:
            if _normalise_alif(m).lower() in norm_reply:
                # Force the hollow path — the guard's rewrite is
                # honest where this canned line was a soft
                # promise. The downstream rewrite branches still
                # try the resolver chain first, so a valid KB
                # entry still wins over the fallback copy.
                reply_text = ""
                break

    # Honest replies win — never rewrite "أحتاج إضافة الرقم …"
    # into our canned line. The merchant's coached prompt was
    # already doing the right thing.
    if _reply_already_honest(reply_text or ""):
        result.skipped_reason = "reply_already_honest"
        result.artifact_satisfied = True
        return result

    # Per-artifact satisfaction probe.
    satisfied = False
    if expected == "staff_phone":
        satisfied = (
            _reply_has_phone(reply_text or "")
            or _call_targets_have_phone(call_targets)
        )
    elif expected == "payment_barcode":
        # A barcode ask is satisfied ONLY by a barcode media —
        # NOT by a phone number in the reply (the customer
        # explicitly asked for the barcode, the transfer phone
        # is a different artifact).
        satisfied = _media_attachments_have_barcode(media_attachments)
    elif expected == "maps_link":
        satisfied = _reply_has_maps_url(reply_text or "")
    elif expected == "store_link":
        # A maps URL doesn't satisfy a store-link ask — the two
        # are different products. Subtract maps before declaring
        # "yes, the LLM shipped a URL".
        satisfied = (
            _reply_has_any_url(reply_text or "")
            and not _reply_has_maps_url(reply_text or "")
        )
    result.artifact_satisfied = satisfied

    if satisfied:
        result.skipped_reason = "artifact_already_present"
        return result

    # Reply lacks the artifact — rewrite ONLY when the reply is
    # actually hollow. A long natural-prose reply that explains
    # something else is left alone (we're a guard, not a
    # post-editor).
    if not _is_hollow_affirmation(reply_text or ""):
        result.skipped_reason = "reply_not_hollow"
        return result

    # ── Per-artifact rewrite branches ─────────────────────────────
    if expected == "staff_phone":
        norm_msg = _normalise_for_match(customer_msg or "")
        norm_reply_for_name = _normalise_for_match(reply_text or "")
        hist_bot_norm, hist_cust_norm = _extract_recent_history_norms(history)
        staff_name, _name_src = _find_staff_name_in_pool(
            norm_msg, norm_reply_for_name, hist_bot_norm, hist_cust_norm,
            candidates=_staff_alias_candidates(db, tenant_id),
        )
        if not staff_name:
            for kw in _staff_alias_candidates(db, tenant_id):
                if (
                    kw in norm_msg
                    or kw in norm_reply_for_name
                    or kw in hist_bot_norm
                    or kw in hist_cust_norm
                ):
                    staff_name = kw
                    break
        kb_phone, kb_kind, _kb_section = _lookup_staff_phone_in_kb(
            db, tenant_id, staff_name or "",
        )
        if kb_phone:
            label = staff_name or "الموظف"
            result.new_reply = f"تفضل رقم {label}: {kb_phone} 🌷"
            result.action = "inject_staff_phone"
            result.fired = True
            result.rewrote_reply = True
            return result
        label = staff_name or "الموظف المختص"
        result.new_reply = (
            f"أحتاج إضافة رقم {label} في بيانات المتجر "
            "حتى أرسله لك مباشرة 🌷"
        )
        result.action = "rewrite_missing_staff_phone"
        result.fired = True
        result.rewrote_reply = True
        return result

    if expected == "payment_barcode":
        bank_label = _detect_bank_label(customer_msg or "")
        bank_seg = f" {bank_label}" if bank_label else ""
        if _reply_has_phone(reply_text or ""):
            # The reply already carries the transfer phone — keep
            # the customer informed about what IS available.
            result.new_reply = (
                f"المتوفر حاليًا رقم التحويل، ولم تتم إضافة "
                f"صورة باركود{bank_seg} بعد 🌷"
            )
        else:
            result.new_reply = (
                f"صورة باركود{bank_seg} غير مضافة حاليًا "
                "في إعدادات الدفع 🌷"
            )
        result.action = "rewrite_missing_barcode"
        result.fired = True
        result.rewrote_reply = True
        return result

    if expected == "maps_link":
        try:
            maps_url, _src = _lookup_tenant_maps_url(db, tenant_id)
        except Exception:  # noqa: BLE001
            maps_url = ""
        if maps_url:
            result.new_reply = _build_location_reply(maps_url)
            result.action = "inject_maps_link"
        else:
            result.new_reply = (
                "موقع المعرض غير مضاف حاليًا في بيانات المتجر 🌷"
            )
            result.action = "rewrite_missing_maps_link"
        result.fired = True
        result.rewrote_reply = True
        return result

    if expected == "store_link":
        try:
            store_url = _lookup_tenant_store_url(db, tenant_id)
        except Exception:  # noqa: BLE001
            store_url = ""
        if store_url:
            result.new_reply = _build_store_link_reply(store_url)
            result.action = "inject_store_link"
        else:
            result.new_reply = "رابط المتجر غير مضاف حاليًا 🌷"
            result.action = "rewrite_missing_store_link"
        result.fired = True
        result.rewrote_reply = True
        return result

    # Should never reach here — the classifier returns one of the
    # four artifact types or "none" (handled above).
    result.skipped_reason = "unknown_artifact_class"
    return result


__all__ = [
    "ProductSafetyNetResult",
    "MediaKeySafetyNetResult",
    "StaffContactSafetyNetResult",
    "StoreLinkSafetyNetResult",
    "LocationLinkSafetyNetResult",
    "ClearIntentFallbackResult",
    "DeliveryInfoContextResult",
    "ProductReaskGuardResult",
    "OutboundArtifactGuardResult",
    "apply_product_safety_net",
    "apply_media_key_safety_net",
    "apply_staff_contact_safety_net",
    "apply_store_link_safety_net",
    "apply_location_safety_net",
    "apply_clear_intent_fallback_net",
    "apply_delivery_info_context_net",
    "apply_product_reask_guard",
    "apply_outbound_artifact_guard",
    "product_net_enabled",
    "media_key_net_enabled",
    "staff_contact_net_enabled",
    "store_link_net_enabled",
    "location_link_net_enabled",
    "clear_intent_fallback_net_enabled",
    "delivery_info_context_net_enabled",
    "product_reask_guard_enabled",
]
