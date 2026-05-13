"""
backend/services/call_resolver.py
─────────────────────────────────
Resolves ``[CALL:<phone>|<label>]`` markers emitted by the LLM into
WhatsApp ``contacts`` messages.

Why a dedicated resolver?
─────────────────────────
WhatsApp Cloud API ``interactive.cta_url`` only accepts ``http(s)://``
URLs — there is **no** native "call button" in interactive non-template
messages. The closest UX is a ``contacts`` payload (vCard), which the
customer's WhatsApp client renders as a contact card with native
Call / Message / Save actions on tap. That's the "professional call
button" experience the merchant asked for.

Marker contract
───────────────
The LLM emits one or more::

    [CALL:0541690226|أمين]
    [CALL:+966555906901|هيثم — الإدارة]

Phone can be in any common Saudi shape (``05xxxxxxxx`` /
``9665xxxxxxxx`` / ``+9665xxxxxxxx``); we normalise to E.164
without the ``+`` (``9665xxxxxxxx``) because that's what the
WhatsApp Cloud API expects in the ``phones[].wa_id`` field.

Label is the visible name on the contact card. We clip it to 60
chars (well below the WhatsApp formatted_name guidance) and strip
any control characters so the vCard payload never throws an HTTP
400 from the WhatsApp endpoint.

Strictly DO NOT use for
──────────────────────
* Payment phone numbers (Rajhi / STC Pay / Mobily) — those must
  stay as plain copyable text so the customer can paste them
  into the bank's transfer screen. Sending a vCard there forces
  an extra tap and confuses the cashier UX.
* Generic store contact phones in the website footer — those go
  through ``[MEDIA_KEY:store_location_image]`` or stay in the
  natural-language reply.

The contract is enforced at the prompt layer (see
``_MARKER_PROTOCOL_PREAMBLE`` in ``core/ai_libraries.py``); this
module is a pure transport for whatever the LLM emits.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Tuple

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# Marker regex
# ──────────────────────────────────────────────────────────────────
#
# Permissive on the phone (digits / spaces / dashes / plus / parens)
# so we capture what the LLM emits, then normalise in
# ``_normalize_saudi_phone``. Label is greedy-but-non-newline and
# trimmed at extraction time.
_CALL_MARKER_RE = re.compile(
    r"\[CALL:\s*([0-9+\s\-()]{6,25})\s*\|\s*([^\]\n]{1,80})\s*\]",
    re.IGNORECASE,
)

# Fallback when the merchant omits the pipe — we still try to
# salvage a number-only payload so a typo doesn't strand the call.
# Pattern is tighter: digits-only after the colon, label defaults to
# a neutral "الإدارة".
_CALL_MARKER_FALLBACK_RE = re.compile(
    r"\[CALL:\s*([0-9+\s\-()]{6,25})\s*\]",
    re.IGNORECASE,
)


# Saudi country code (E.164 without the "+"). WhatsApp's wa_id
# field uses this form.
_SAUDI_CC = "966"


# Max number of contact cards we ever attach to a single reply.
# WhatsApp accepts more in one ``contacts`` message but flooding
# the customer with multiple cards is bad UX for a sales chat.
MAX_CALLS_PER_REPLY = 2


@dataclass(frozen=True)
class CallTarget:
    """One resolved call action — what to render on the contact card."""

    name: str          # Display name on the card.
    wa_id: str         # E.164 without "+", e.g. "966541690226".
    phone_display: str # Pretty form for logs (e.g. "+966 54 169 0226").
    raw_phone: str     # Whatever the LLM emitted, for traceability.


# ──────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────


def extract_call_markers(
    reply_text: str,
    *,
    max_calls: int = MAX_CALLS_PER_REPLY,
) -> Tuple[str, List[CallTarget]]:
    """Strip ``[CALL:...]`` markers from ``reply_text`` and resolve them.

    Returns ``(cleaned_text, calls)``:

      * ``cleaned_text`` — the reply with every ``[CALL:...]``
        token removed. Two-blank-lines collapsed to one so the
        customer never sees the gap where the marker used to be.
      * ``calls`` — the resolved targets in declaration order,
        deduped by ``wa_id`` and capped at ``max_calls``.

    The cleaned reply is the body the customer sees; the resolved
    calls are dispatched as a separate ``contacts`` message after
    the main reply (see the webhook integration).
    """
    text = reply_text or ""
    if not text or "[CALL:" not in text.upper():
        return text, []

    matches = list(_CALL_MARKER_RE.finditer(text))
    # Try the fallback (no pipe) only if the strict matcher found nothing.
    used_fallback = False
    if not matches:
        matches = list(_CALL_MARKER_FALLBACK_RE.finditer(text))
        used_fallback = True
        if not matches:
            return text, []

    targets: List[CallTarget] = []
    seen_ids: set = set()
    for m in matches:
        raw_phone = (m.group(1) or "").strip()
        if used_fallback:
            label = "الإدارة"
        else:
            label = (m.group(2) or "").strip()
        if not raw_phone:
            continue
        wa_id = _normalize_saudi_phone(raw_phone)
        if not wa_id:
            # Couldn't parse — drop the marker silently rather than
            # ship a broken contact card.
            logger.info(
                "call_resolver | unresolvable phone %r — dropping marker",
                raw_phone,
            )
            continue
        if wa_id in seen_ids:
            continue
        seen_ids.add(wa_id)
        # Clamp label length (WhatsApp formatted_name guidance).
        clean_label = re.sub(r"[\x00-\x1f]+", "", label).strip()[:60]
        if not clean_label:
            clean_label = "الإدارة"
        targets.append(
            CallTarget(
                name=clean_label,
                wa_id=wa_id,
                phone_display=_pretty_phone(wa_id),
                raw_phone=raw_phone,
            )
        )
        if len(targets) >= max_calls:
            break

    # Strip ALL [CALL:...] markers (strict + fallback) from the
    # body. We do strict first then fallback to avoid double-pass
    # leaving a stray fragment.
    cleaned = _CALL_MARKER_RE.sub("", text)
    cleaned = _CALL_MARKER_FALLBACK_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, targets


def build_contacts_payload(
    calls: List[CallTarget],
    *,
    to: str,
    messaging_product: str = "whatsapp",
) -> dict:
    """Build the WhatsApp Cloud API ``contacts`` message body.

    The shape mirrors the Meta-documented schema verbatim. We keep
    ``first_name`` == ``formatted_name`` because most Arabic names
    don't split cleanly and the customer only sees the
    ``formatted_name`` on the card preview anyway.

    Phone ``type`` is ``WORK`` for staff numbers — semantically
    closer to the merchant context than ``CELL`` and also what the
    contact card legend reads on first render.
    """
    contacts_arr = []
    for c in calls:
        contacts_arr.append({
            "name": {
                "formatted_name": c.name,
                "first_name": c.name,
            },
            "phones": [
                {
                    # "phone" is the human-pretty form; "wa_id" is
                    # the E.164-without-plus form WhatsApp uses to
                    # dedupe + initiate chats.
                    "phone": "+" + c.wa_id,
                    "wa_id": c.wa_id,
                    "type": "WORK",
                },
            ],
        })
    return {
        "messaging_product": messaging_product,
        "to": to,
        "type": "contacts",
        "contacts": contacts_arr,
    }


# ──────────────────────────────────────────────────────────────────
# Internals — phone normalisation
# ──────────────────────────────────────────────────────────────────


def _normalize_saudi_phone(raw: str) -> str:
    """Best-effort Saudi-phone → E.164 (digits only, no leading +).

    Accepts the four shapes the LLM realistically emits:

      * ``05xxxxxxxx``       → ``9665xxxxxxxx``
      * ``5xxxxxxxx``        → ``9665xxxxxxxx``
      * ``9665xxxxxxxx``     → ``9665xxxxxxxx``
      * ``+9665xxxxxxxx``    → ``9665xxxxxxxx``

    Anything else (international non-Saudi, too short / too long)
    returns ``""`` so the caller can drop the marker rather than
    ship a malformed contact card.
    """
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    # Strip leading zeros that aren't part of the country code
    # (e.g. "0541690226" → "541690226").
    if digits.startswith("00966"):
        digits = digits[2:]  # "00966..." → "966..."
    if digits.startswith("0"):
        digits = digits.lstrip("0")
    # Now we expect either "9665xxxxxxxx" (12 digits) or
    # "5xxxxxxxx" (9 digits — Saudi mobile prefix).
    if digits.startswith(_SAUDI_CC):
        if len(digits) == 12 and digits[3] == "5":
            return digits
        return ""
    if digits.startswith("5") and len(digits) == 9:
        return _SAUDI_CC + digits
    return ""


def _pretty_phone(wa_id: str) -> str:
    """Pretty-print a normalised ``9665xxxxxxxx`` for logs."""
    if not wa_id or len(wa_id) != 12:
        return wa_id or ""
    return f"+{wa_id[:3]} {wa_id[3:5]} {wa_id[5:8]} {wa_id[8:]}"


__all__ = [
    "CallTarget",
    "MAX_CALLS_PER_REPLY",
    "extract_call_markers",
    "build_contacts_payload",
    "_CALL_MARKER_RE",
]
