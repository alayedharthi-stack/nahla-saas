"""
services/unsubscribe.py
───────────────────────
Unsubscribe / opt-out management for Nahla.

Lifecycle (3 states stored in `Customer.extra_metadata` — no migration needed):

    ┌─ ORDINARY ──────────────────────────────────────────┐
    │   nothing in metadata, customer reachable           │
    └─────────────┬───────────────────────────────────────┘
                  │ customer sends unsubscribe keyword
                  ▼
    ┌─ PENDING_UNSUBSCRIBE ──────────────────────────────┐
    │   pending_unsubscribe = true                        │
    │   pending_unsubscribe_at = ISO timestamp            │
    │   • System sends interactive confirmation (2 buttons)
    │   • All campaigns / automations / AI are PAUSED      │
    └─────────────┬───────────────────────────────────────┘
                  │
       ┌──────────┴──────────┐
       │                     │
   "نعم متأكد"            "تراجع"
       ▼                     ▼
    ┌─ UNSUBSCRIBED ──┐   ┌─ ORDINARY again ─┐
    │ is_unsubscribed │   │ (pending cleared) │
    │ = true          │   │                   │
    └────────┬────────┘   └───────────────────┘
             │
             │ customer sends ANY new inbound message
             ▼
        ┌─ ORDINARY again (auto re-subscribe) ─┐
        └──────────────────────────────────────┘

Metadata keys written by this module:

    pending_unsubscribe      bool   True while waiting for button reply
    pending_unsubscribe_at   str    ISO-8601 timestamp
    is_unsubscribed          bool   True after final confirmation
    unsubscribed_at          str    ISO-8601 timestamp
    resubscribed_at          str    ISO-8601 timestamp of last re-sub event

Button IDs sent to WhatsApp (used by the webhook to route the reply):

    UNSUB_CONFIRM_BUTTON_ID = "unsub_confirm"
    UNSUB_CANCEL_BUTTON_ID  = "unsub_cancel"
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

logger = logging.getLogger("nahla.unsubscribe")

# ── Button identifiers ───────────────────────────────────────────────────────
UNSUB_CONFIRM_BUTTON_ID = "unsub_confirm"
UNSUB_CANCEL_BUTTON_ID  = "unsub_cancel"

# ── Standard message copy ────────────────────────────────────────────────────
CONFIRMATION_BODY_AR = (
    "هل أنت متأكد من إلغاء الاشتراك من رسائل المتجر؟\n\n"
    "سنُوقف عنك جميع الحملات والتذكيرات الآلية فور تأكيدك."
)
CONFIRMATION_BTN_CONFIRM_AR = "نعم متأكد"
CONFIRMATION_BTN_CANCEL_AR  = "تراجع"
CONFIRMATION_NUMERIC_CONFIRM = {"1", "١"}
CONFIRMATION_NUMERIC_CANCEL  = {"2", "٢"}
PENDING_UNSUBSCRIBE_TIMEOUT_HOURS = 24
PENDING_PROMPT_RESEND_MINUTES = 5

FINAL_UNSUBSCRIBED_MSG_AR = (
    "تم إلغاء اشتراكك بنجاح. ✅\n\n"
    "إذا كنت ترغب في العودة لاستقبال العروض والحملات، أرسل أي رسالة "
    "لهذا الرقم وسيتم تفعيلك تلقائياً."
)
CANCELLED_UNSUB_MSG_AR = "تم إلغاء طلب إلغاء الاشتراك 👍"
CONFIRMATION_FALLBACK_MSG_AR = (
    "هل أنت متأكد من إلغاء الاشتراك من رسائل المتجر؟\n\n"
    "للإلغاء النهائي رد بالرقم:\n"
    "1 = نعم متأكد\n"
    "2 = تراجع"
)

# ── Marketing-template footer (appended automatically to MARKETING templates) ─
# Long form is shown to merchants in the dashboard; short form is what we
# actually submit to Meta because WhatsApp footers are capped at 60 chars.
MARKETING_FOOTER_AR_FULL  = (
    "إذا كنت لا ترغب في استقبال مثل هذه الرسائل، اكتب إلغاء الاشتراك"
)
MARKETING_FOOTER_AR       = "للإيقاف اكتب: إلغاء الاشتراك"   # ≤ 60 chars, Meta-safe

# ── Keyword registry ─────────────────────────────────────────────────────────
# Two tiers:
#   1. STRICT  — full opt-out phrases (always trigger pending)
#   2. SOLO    — single word "إلغاء" / "stop" — also trigger, but only
#               when the message is short enough to be unambiguous
#
# Matching is case-insensitive, ignores leading/trailing whitespace, and is
# unicode-aware. We normalise common Arabic alef variants before matching
# so that "إلغاء" / "الغاء" / "ألغاء" are all caught.

_STRICT_PATTERNS: tuple[str, ...] = (
    r"إلغاء\s*الاشتراك",
    r"إلغ\s*الاشتراك",
    r"الغ\s*الاشتراك",        # colloquial without hamza: "الغ الاشتراك"
    r"الغاء\s*الاشتراك",
    r"إلغاء\s*اشتراك",
    r"الغاء\s*اشتراك",
    r"أوقف\s*الرسائل?",
    r"اوقف\s*الرسائل?",
    r"إيقاف\s*الرسائل?",
    r"ايقاف\s*الرسائل?",
    r"لا\s*أريد\s*رسائل?",
    r"لا\s*اريد\s*رسائل?",
    r"لا\s*ترسلوا?\s*لي",
    r"لا\s*ترسلي\s*لي",
    r"لا\s*تراسلني",
    r"لا\s*عاد\s*ترسل",      # "لاعاد ترسل" / "لا عاد ترسل" — don't send anymore
    r"أزعجتني",               # "أزعجتني / ازعجتني" — you bothered me (normalisation catches ا/أ)
    r"ازعجتني",
    r"توقف\s*عن\s*الإرسال",
    r"توقف\s*عن\s*الارسال",
    r"أوقف\s*التواصل",
    r"اوقف\s*التواصل",
    r"^\s*unsubscribe\s*$",
    r"^\s*opt\s*[-_ ]?out\s*$",
)

# "Solo" keywords are accepted only when the message is short (≤3 words)
# so that a sentence like "هل تريد إلغاء طلبي؟" doesn't trigger an opt-out.
_SOLO_PATTERNS: tuple[str, ...] = (
    r"^\s*إلغاء\s*$",
    r"^\s*الغاء\s*$",
    r"^\s*ألغاء\s*$",
    r"^\s*الغ\s*$",           # colloquial imperative without hamza
    r"^\s*لا\s*ترسل\s*$",    # "لاترسل" / "لا ترسل" standalone
    r"^\s*stop\s*$",
)


def _normalise_pattern(p: str) -> str:
    """Collapse Arabic alef variants inside a regex *pattern* string.

    Only the Arabic letter characters are replaced — regex metacharacters
    (\\s, ?, ^, $, *, …) are untouched, so the result is still valid regex.
    """
    return (
        p.replace("أ", "ا")
         .replace("إ", "ا")
         .replace("آ", "ا")
         .replace("ٱ", "ا")
    )


_STRICT_RX      = [re.compile(p,                    re.IGNORECASE | re.UNICODE) for p in _STRICT_PATTERNS]
_STRICT_RX_NORM = [re.compile(_normalise_pattern(p), re.IGNORECASE | re.UNICODE) for p in _STRICT_PATTERNS]
_SOLO_RX        = [re.compile(p,                    re.IGNORECASE | re.UNICODE) for p in _SOLO_PATTERNS]
_SOLO_RX_NORM   = [re.compile(_normalise_pattern(p), re.IGNORECASE | re.UNICODE) for p in _SOLO_PATTERNS]


# ── Public API ───────────────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    """Lowercase + collapse Arabic alef variants for resilient matching."""
    if not text:
        return ""
    out = text.strip().lower()
    # Common Arabic spelling variants that should be treated as identical
    out = (
        out.replace("أ", "ا")
           .replace("إ", "ا")
           .replace("آ", "ا")
           .replace("ٱ", "ا")
    )
    return out


def is_unsubscribe_request(text: str) -> bool:
    """Return True if *text* should trigger the unsubscribe confirmation flow.

    Strict phrases match anywhere in the message; solo keywords only match
    when the message is ≤3 words (to avoid false positives on natural
    sentences that happen to contain the word "إلغاء").
    """
    if not text:
        return False

    raw = text.strip()
    if not raw:
        return False

    norm = _normalise(raw)

    # Apply each pattern against raw text (exact spelling) AND the same pattern
    # with normalised alef variants against the normalised text.  This catches
    # colloquial forms like "الغ الاشتراك" (no hamza) that the raw regex would
    # miss even when applied to the normalised text, because the pattern itself
    # still contains "إ".
    for rx, rx_norm in zip(_STRICT_RX, _STRICT_RX_NORM):
        if rx.search(raw) or rx_norm.search(norm):
            return True

    word_count = len(re.findall(r"\S+", raw))
    if word_count <= 3:
        for rx, rx_norm in zip(_SOLO_RX, _SOLO_RX_NORM):
            if rx.search(raw) or rx_norm.search(norm):
                return True

    return False


def classify_confirmation_text(text: str) -> Literal["confirm", "cancel"] | None:
    """Fallback classifier for text replies when quick-reply buttons fail."""
    cleaned = _normalise(text or "")
    if not cleaned:
        return None
    if cleaned in CONFIRMATION_NUMERIC_CONFIRM or cleaned in {
        "نعم",
        "نعم متاكد",
        "متاكد",
        "اكيد",
        "تاكيد",
        "اكد",
    }:
        return "confirm"
    if cleaned in CONFIRMATION_NUMERIC_CANCEL or cleaned in {
        "تراجع",
        "الغاء الطلب",
        "لا",
        "رجوع",
    }:
        return "cancel"
    return None


# ── State helpers ────────────────────────────────────────────────────────────

def is_customer_unsubscribed(customer: Any) -> bool:
    """Final, confirmed unsubscribe."""
    meta = getattr(customer, "extra_metadata", None) or {}
    return bool(meta.get("is_unsubscribed"))


def is_customer_pending_unsubscribe(customer: Any, *, now: Optional[datetime] = None) -> bool:
    """Customer asked to opt-out and is awaiting button confirmation."""
    meta = getattr(customer, "extra_metadata", None) or {}
    if not meta.get("pending_unsubscribe"):
        return False
    return not is_pending_expired(customer, now=now)


def is_silenced(customer: Any) -> bool:
    """Convenience: customer should not receive ANY system-initiated message
    (campaigns, automations, AI replies). True for both PENDING and FINAL.
    """
    return is_customer_unsubscribed(customer) or is_customer_pending_unsubscribe(customer)


def is_pending_expired(customer: Any, *, now: Optional[datetime] = None) -> bool:
    """Pending requests expire after 24h so customers never stay suspended forever."""
    meta = getattr(customer, "extra_metadata", None) or {}
    if not meta.get("pending_unsubscribe"):
        return False
    expires_at = _parse_dt(meta.get("pending_unsubscribe_expires_at"))
    if expires_at is None:
        created_at = _parse_dt(meta.get("pending_unsubscribe_at"))
        if created_at is None:
            return False
        expires_at = created_at + timedelta(hours=PENDING_UNSUBSCRIBE_TIMEOUT_HOURS)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current >= expires_at


def expire_pending_if_needed(
    db: Any,
    customer: Any,
    *,
    now: Optional[datetime] = None,
    commit: bool = True,
) -> bool:
    """Clear expired pending state. Returns True when state changed."""
    if not is_pending_expired(customer, now=now):
        return False
    clear_pending_unsubscribe(db, customer, commit=commit)
    logger.info(
        "customer %s (phone=%s) pending_unsubscribe expired → ORDINARY",
        getattr(customer, "id", "?"), getattr(customer, "phone", "?"),
    )
    return True


def should_send_pending_prompt(
    customer: Any,
    *,
    now: Optional[datetime] = None,
    min_interval_minutes: int = PENDING_PROMPT_RESEND_MINUTES,
) -> bool:
    """Throttle repeated confirmation prompts while pending."""
    meta = getattr(customer, "extra_metadata", None) or {}
    last_sent = _parse_dt(meta.get("pending_unsubscribe_prompt_sent_at"))
    if last_sent is None:
        return True
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current - last_sent >= timedelta(minutes=min_interval_minutes)


def mark_pending_prompt_sent(db: Any, customer: Any, *, commit: bool = True) -> None:
    meta = dict(getattr(customer, "extra_metadata", None) or {})
    meta["pending_unsubscribe_prompt_sent_at"] = datetime.now(timezone.utc).isoformat()
    customer.extra_metadata = meta
    _flag(customer)
    db.add(customer)
    if commit:
        db.commit()


# ── State transitions ───────────────────────────────────────────────────────

def mark_pending_unsubscribe(db: Any, customer: Any, *, commit: bool = True) -> None:
    """Move customer into PENDING state (awaiting button confirmation)."""
    now = datetime.now(timezone.utc)
    meta = dict(getattr(customer, "extra_metadata", None) or {})
    meta["pending_unsubscribe"]    = True
    meta["pending_unsubscribe_at"] = now.isoformat()
    meta["pending_unsubscribe_expires_at"] = (
        now + timedelta(hours=PENDING_UNSUBSCRIBE_TIMEOUT_HOURS)
    ).isoformat()
    customer.extra_metadata = meta
    _flag(customer)
    db.add(customer)
    if commit:
        db.commit()
    logger.info(
        "customer %s (phone=%s) → PENDING_UNSUBSCRIBE",
        getattr(customer, "id", "?"), getattr(customer, "phone", "?"),
    )


def clear_pending_unsubscribe(db: Any, customer: Any, *, commit: bool = True) -> None:
    """Remove the pending flag (used both on confirmation and on cancellation)."""
    meta = dict(getattr(customer, "extra_metadata", None) or {})
    if any(k in meta for k in (
        "pending_unsubscribe",
        "pending_unsubscribe_at",
        "pending_unsubscribe_expires_at",
        "pending_unsubscribe_prompt_sent_at",
    )):
        meta.pop("pending_unsubscribe", None)
        meta.pop("pending_unsubscribe_at", None)
        meta.pop("pending_unsubscribe_expires_at", None)
        meta.pop("pending_unsubscribe_prompt_sent_at", None)
        customer.extra_metadata = meta
        _flag(customer)
        db.add(customer)
        if commit:
            db.commit()


def mark_unsubscribed(db: Any, customer: Any, *, commit: bool = True) -> None:
    """Final confirmation — customer is fully opted out."""
    meta = dict(getattr(customer, "extra_metadata", None) or {})
    meta["is_unsubscribed"] = True
    meta["unsubscribed_at"] = datetime.now(timezone.utc).isoformat()
    meta.pop("pending_unsubscribe", None)
    meta.pop("pending_unsubscribe_at", None)
    meta.pop("pending_unsubscribe_expires_at", None)
    meta.pop("pending_unsubscribe_prompt_sent_at", None)
    meta.pop("resubscribed_at", None)

    customer.extra_metadata = meta
    _flag(customer)
    db.add(customer)
    if commit:
        db.commit()
    logger.info(
        "customer %s (phone=%s) → UNSUBSCRIBED (confirmed)",
        getattr(customer, "id", "?"), getattr(customer, "phone", "?"),
    )


def mark_resubscribed(db: Any, customer: Any, *, commit: bool = True) -> None:
    """Customer sent a new message after a final unsubscribe → bring them back."""
    meta = dict(getattr(customer, "extra_metadata", None) or {})
    meta["is_unsubscribed"]    = False
    meta["resubscribed_at"]    = datetime.now(timezone.utc).isoformat()
    meta.pop("pending_unsubscribe", None)
    meta.pop("pending_unsubscribe_at", None)
    meta.pop("pending_unsubscribe_expires_at", None)
    meta.pop("pending_unsubscribe_prompt_sent_at", None)
    customer.extra_metadata = meta
    _flag(customer)
    db.add(customer)
    if commit:
        db.commit()
    logger.info(
        "customer %s (phone=%s) → RE-SUBSCRIBED automatically",
        getattr(customer, "id", "?"), getattr(customer, "phone", "?"),
    )


# ── Interactive WhatsApp payload builders ───────────────────────────────────

def build_confirmation_payload(to_phone: str) -> Dict[str, Any]:
    """Quick-reply interactive message asking the customer to confirm
    or cancel their opt-out request. Pure data — no I/O."""
    return {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                to_phone,
        "type":              "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": CONFIRMATION_BODY_AR},
            "footer": {"text": "نحلة • مساعدك التسويقي"},
            "action": {
                "buttons": [
                    {
                        "type":  "reply",
                        "reply": {
                            "id":    UNSUB_CONFIRM_BUTTON_ID,
                            "title": CONFIRMATION_BTN_CONFIRM_AR,
                        },
                    },
                    {
                        "type":  "reply",
                        "reply": {
                            "id":    UNSUB_CANCEL_BUTTON_ID,
                            "title": CONFIRMATION_BTN_CANCEL_AR,
                        },
                    },
                ],
            },
        },
    }


def build_text_payload(to_phone: str, body: str) -> Dict[str, Any]:
    """Plain text message used for the final 'goodbye' or 'cancelled' notice."""
    return {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                to_phone,
        "type":              "text",
        "text": {"body": body, "preview_url": False},
    }


def build_confirmation_fallback_payload(to_phone: str) -> Dict[str, Any]:
    """Plain-text fallback if interactive quick replies fail to send."""
    return build_text_payload(to_phone, CONFIRMATION_FALLBACK_MSG_AR)


# ── Marketing-footer helper for templates ───────────────────────────────────

def ensure_marketing_footer(components: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Append the Nahla unsubscribe footer to a template's components list,
    only if no FOOTER component already exists. Returns a new list (never
    mutates the input).

    Used by the AI template generator and the Nahla preset library so
    every MARKETING template that we submit to Meta carries the standard
    opt-out hint without breaking Meta policy (footer ≤ 60 chars, no
    variables).
    """
    out = list(components or [])

    has_footer = any(
        (c.get("type") or "").upper() == "FOOTER"
        for c in out
    )
    if has_footer:
        return out

    out.append({"type": "FOOTER", "text": MARKETING_FOOTER_AR})
    return out


# ── Internal helpers ────────────────────────────────────────────────────────

def _flag(customer: Any) -> None:
    """Nudge SQLAlchemy to emit an UPDATE for the JSONB column."""
    try:
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
        flag_modified(customer, "extra_metadata")
    except Exception:
        pass


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None
