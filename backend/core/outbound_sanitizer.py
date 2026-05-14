"""
core/outbound_sanitizer.py
───────────────────────────
Final-line-of-defence scrubber for WhatsApp outbound payloads.

Why this exists
───────────────
A May 2026 incident shipped raw DuckDuckGo result dumps (encoded
URLs, ``uddg=…&rut=…`` fragments, Wikipedia citations) into customer
WhatsApp threads after an out-of-scope question
("ايهما حساب كهرباء الشقة"). That broke merchant trust instantly:
the AI looked like a search engine, not a sales assistant.

Defence in depth
────────────────
Three layers protect outbound replies from external-research leakage:

  1. ``modules/ai/tools/web_search.search_web`` is hard-gated by
     ``MERCHANT_EXTERNAL_RESEARCH_ENABLED`` (default OFF). Even if a
     legacy code path calls it, nothing goes to the network.
  2. The decision engine never proposes ``ACTION_WEB_SEARCH`` unless
     the env is opted in; out-of-scope questions route to a canned
     deflection that never calls the LLM.
  3. *This module*. Right before ``_post_wa`` ships a payload to
     360dialog / Cloud API, we scan the outbound text for any of the
     known leakage fingerprints and replace the body with a safe
     fallback if we find one. Logged as ``[EXTERNAL_RESEARCH_BLOCKED]``.

The third layer is what makes the guarantee airtight: ANY future code
path that somehow produces a search-y reply still gets caught here.

Public surface
──────────────
* ``sanitize_outbound_payload(payload, *, tenant_id=None)`` — mutates
  the WhatsApp Cloud API payload in place and returns ``(payload,
  was_sanitised)``. Handles ``text``, ``interactive.button`` and
  ``interactive.cta_url`` body fields. Anything else passes through
  unchanged.
* ``contains_leakage_markers(text)`` — pure-function predicate for
  unit tests and ad-hoc checks.

The sanitiser is intentionally CONSERVATIVE:
  * Single-host store links like ``mystore.salla.sa/product/123`` are
    fine — only the patterns associated with external-search dumps
    trip the rule.
  * The DuckDuckGo bridge (``html.duckduckgo.com/l/?uddg=…``) is the
    canonical leak source we observed in production; that alone is
    enough to drop the reply.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("nahla.security.outbound_sanitizer")

# ── Leakage fingerprints ─────────────────────────────────────────────────────
#
# These are the patterns observed in the live incident plus the close
# variants a slightly different LLM run could emit. Order matters: the
# DuckDuckGo bridge is the canonical leak, so it leads the list and
# its reason is named explicitly in the log line.
_LEAK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("duckduckgo_bridge",   re.compile(r"duckduckgo\.com",                  re.IGNORECASE)),
    ("duckduckgo_redirect", re.compile(r"\b(?:uddg|rut)\s*=",               re.IGNORECASE)),
    ("bing_search",         re.compile(r"bing\.com/search\b",               re.IGNORECASE)),
    ("google_search",       re.compile(r"google\.com/search\b",             re.IGNORECASE)),
    ("wikipedia_citation",  re.compile(r"\bwikipedia\.org/",                re.IGNORECASE)),
    # URL-double-encoded Arabic — the dead giveaway of a DDG "sources:"
    # dump that we observed in the incident. Plain ``%D8`` (single-
    # encoded Arabic) is FINE — that's just a normal Salla / Zid
    # product URL with Arabic slugs. Double-encoded ``%25D8`` only
    # appears when an already-encoded URL is wrapped inside another
    # encoded query string (which is exactly the ``html.duckduckgo
    # .com/l/?uddg=https%3A%2F%2F…%25D8%25A7…`` shape).
    ("double_encoded_url",  re.compile(r"%25[0-9A-Fa-f]{2}", )),
    # "المصادر:" header followed by URL markers is the giveaway of
    # the old ``web_search_summary`` template output.
    ("sources_header",      re.compile(r"المصادر\s*:\s*\n?\s*[-•]?\s*(?:https?://|//)", )),
]

# How many independent URLs in a single message constitute a "dump"?
# A normal merchant reply rarely sends more than ONE clickable link
# (payment link OR product page OR tracking page). Two is suspicious;
# three or more is almost certainly a search citation block.
_MAX_URLS_PER_MESSAGE = 2

_URL_RE = re.compile(r"https?://\S+|(?<![A-Za-z0-9])//\S+", re.IGNORECASE)

# ── Safe fallback ────────────────────────────────────────────────────────────
#
# Returned to the customer when we drop a leaky reply. Kept playful
# and on-brand per the May 2026 merchant feedback ("don't sound
# corporate") — the dry "هذا خارج نطاق متجرنا" wording was banned in
# that round and must not be reintroduced here. We avoid any phrasing
# that sounds like an order confirmation ("استلمنا طلبك" / "وصل")
# for the same reason the handoff template avoids it — customers
# read those literally.
SAFE_FALLBACK_TEXT = "معليش 🌷 خلّينا في العسل والطلبات — وش تحب نشوف لك اليوم؟ 😄🍯"


def contains_leakage_markers(text: str) -> Optional[str]:
    """Return the NAME of the first matching leakage fingerprint, or
    ``None`` when the text is clean. Useful for unit tests + log
    annotations. The returned name is one of the keys in
    ``_LEAK_PATTERNS`` plus the synthetic ``too_many_urls`` bucket.
    """
    if not text or not isinstance(text, str):
        return None
    for name, pattern in _LEAK_PATTERNS:
        if pattern.search(text):
            return name
    urls = _URL_RE.findall(text)
    if len(urls) > _MAX_URLS_PER_MESSAGE:
        return "too_many_urls"
    return None


def _replace_body_in_payload(payload: Dict[str, Any], new_text: str) -> bool:
    """Replace the customer-facing body of a WhatsApp Cloud API
    payload with ``new_text``. Returns True if a replacement was
    performed (we matched a known shape) or False if the payload
    doesn't carry an editable text body.
    """
    if not isinstance(payload, dict):
        return False

    msg_type = str(payload.get("type") or "").lower()

    if msg_type == "text":
        text_block = payload.setdefault("text", {})
        if isinstance(text_block, dict):
            text_block["body"] = new_text
            return True

    if msg_type == "interactive":
        interactive = payload.get("interactive") or {}
        if isinstance(interactive, dict):
            body_block = interactive.setdefault("body", {})
            if isinstance(body_block, dict):
                body_block["text"] = new_text
                # When we scrub a search dump we also drop any
                # buttons that came with it — there's no clean way
                # to know if a "اطلب الآن" button still makes sense
                # when the body has been rewritten.
                action = interactive.get("action")
                if isinstance(action, dict) and "buttons" in action:
                    action["buttons"] = []
                # Same logic for the cta_url variant — strip the
                # link so we don't ship a button pointing to a search
                # result page.
                if isinstance(action, dict) and action.get("name") == "cta_url":
                    interactive["type"] = "button"
                    interactive["action"] = {"buttons": []}
                return True

    return False


def _extract_existing_body(payload: Dict[str, Any]) -> str:
    """Pull the customer-facing body out of a Cloud API payload so we
    can scan it. Empty string for unknown shapes — caller treats that
    as "nothing to check"."""
    if not isinstance(payload, dict):
        return ""
    msg_type = str(payload.get("type") or "").lower()
    if msg_type == "text":
        return str((payload.get("text") or {}).get("body") or "")
    if msg_type == "interactive":
        interactive = payload.get("interactive") or {}
        body = (interactive.get("body") or {}).get("text") or ""
        return str(body)
    return ""


def sanitize_outbound_payload(
    payload: Dict[str, Any],
    *,
    tenant_id: Optional[int] = None,
    recipient: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Inspect & maybe-rewrite a Cloud API payload.

    Returns the (possibly mutated) payload and a boolean indicating
    whether a sanitisation occurred. Callers that care about whether
    the message was scrubbed can branch on the boolean — e.g. to
    persist the original text in a debug field for audit. Never raises.
    """
    try:
        body = _extract_existing_body(payload)
        if not body:
            return payload, False
        match = contains_leakage_markers(body)
        if not match:
            return payload, False
        logger.warning(
            "[EXTERNAL_RESEARCH_BLOCKED] tenant=%s to=%s marker=%s original_len=%d "
            "preview=%r",
            tenant_id,
            recipient,
            match,
            len(body),
            body[:140],
        )
        if _replace_body_in_payload(payload, SAFE_FALLBACK_TEXT):
            return payload, True
        # We detected a leak but couldn't rewrite the payload shape —
        # fail closed by clearing the body so the message goes out
        # empty rather than leaking. The caller will most likely
        # surface a delivery error (preferable to the leak).
        logger.warning(
            "[EXTERNAL_RESEARCH_BLOCKED] tenant=%s could not rewrite payload type=%r — "
            "blanking body",
            tenant_id, payload.get("type"),
        )
        _replace_body_in_payload(payload, "")
        return payload, True
    except Exception as exc:  # noqa: BLE001
        # The sanitiser MUST NOT take the send path down. Worst case:
        # we log the exception and let the original payload through.
        logger.exception(
            "[EXTERNAL_RESEARCH_BLOCKED] sanitizer crashed tenant=%s err=%s — "
            "letting payload through",
            tenant_id, exc,
        )
        return payload, False


__all__ = [
    "SAFE_FALLBACK_TEXT",
    "contains_leakage_markers",
    "sanitize_outbound_payload",
]
