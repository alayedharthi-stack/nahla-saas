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

A second incident (June 2026) leaked raw planner-field identifiers
(``response_goal``, ``execute_pending_offer``,
``resolve_ambiguous_need``) into a customer WhatsApp thread. The
LLM had quoted its own prompt scaffolding inside an otherwise
natural Arabic reply — a class of leak that the bracketed-marker
scrubber (``core.ai_libraries.scrub_internal_markers``) does not
catch because the identifiers are bare words, not ``[FOO]``-shaped
tokens.

Defence in depth
────────────────
Layers protecting outbound replies from leakage:

  1. ``modules/ai/tools/web_search.search_web`` is hard-gated by
     ``MERCHANT_EXTERNAL_RESEARCH_ENABLED`` (default OFF). Even if a
     legacy code path calls it, nothing goes to the network.
  2. The decision engine never proposes ``ACTION_WEB_SEARCH`` unless
     the env is opted in; out-of-scope questions route to a canned
     deflection that never calls the LLM.
  3. ``core.ai_libraries.scrub_internal_markers`` strips bracketed
     tokens (``[TRANSFER]``, ``[TEMPLATE:foo]``, …) at the brain
     boundary, persistence layer and wire layer.
  4. *This module*. Right before ``_post_wa`` ships a payload to
     360dialog / Cloud API, we scan the outbound text for known
     leakage fingerprints and either rewrite to a clean segment
     (planner leak) or replace with a safe fallback (search leak).
     Logged as ``[EXTERNAL_RESEARCH_BLOCKED]`` /
     ``[INTERNAL_PLANNER_BLOCKED]``.

The wire-level guard is what makes the guarantee airtight: ANY
future code path that produces a leaky reply still gets caught here.

Public surface
──────────────
* ``sanitize_outbound_payload(payload, *, tenant_id=None)`` — mutates
  the WhatsApp Cloud API payload in place and returns ``(payload,
  was_sanitised)``. Handles ``text``, ``interactive.button`` and
  ``interactive.cta_url`` body fields. Anything else passes through
  unchanged.
* ``contains_leakage_markers(text)`` — predicate for the search-leak
  fingerprints (returns name or ``None``).
* ``contains_planner_markers(text)`` — predicate for the
  internal-planner fingerprints (returns name or ``None``).
* ``extract_natural_segment(text)`` — best-effort recovery of the
  clean Arabic part of a reply that was contaminated with planner
  text. Returns the recovered string or ``None`` when nothing
  recoverable remains.

The sanitiser is intentionally CONSERVATIVE for the search case:
  * Single-host store links like ``mystore.salla.sa/product/123`` are
    fine — only the patterns associated with external-search dumps
    trip the rule.
  * The DuckDuckGo bridge (``html.duckduckgo.com/l/?uddg=…``) is the
    canonical leak source we observed in production; that alone is
    enough to drop the reply.

For the planner case the strategy is RECOVERY-FIRST: we try to keep
the natural Arabic message the customer was meant to see and only
fall back to a generic apology when no clean segment can be salvaged.
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

# ── Internal planner / debug-field fingerprints ──────────────────────────────
#
# These are bare identifier tokens copy-pasted out of the brain
# scaffolding (prompts, decision engine, fallback policy) that have
# no business appearing in a customer-facing reply. They are NOT
# bracketed, so ``scrub_internal_markers`` (ASCII-uppercase brackets
# only) cannot catch them.
#
# The identifiers are matched as standalone words / assignment
# fragments so we don't accidentally strip a customer's own English
# noun. ``\b`` boundaries + the very specific snake_case shape of
# planner identifiers keeps false positives low: a customer would
# not naturally type ``execute_pending_offer`` in Arabic.
_PLANNER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("response_goal",          re.compile(r"\bresponse_goal\b",                 re.IGNORECASE)),
    ("execute_pending_offer",  re.compile(r"\bexecute_pending_offer\b",         re.IGNORECASE)),
    ("resolve_ambiguous_need", re.compile(r"\bresolve_ambiguous_need\b",        re.IGNORECASE)),
    # ACTION_LLM_REPLY, ACTION_WEB_SEARCH, ACTION_HANDOFF, …
    ("action_token",           re.compile(r"\bACTION_[A-Z][A-Z0-9_]*\b")),
    # GOAL_ANSWER, GOAL_RETRY, GOAL_ACK, GOAL_HANDOFF
    ("goal_constant",          re.compile(r"\bGOAL_[A-Z][A-Z0-9_]*\b")),
    # FALLBACK_KIND_NEUTRAL_RETRY, …
    ("fallback_kind_const",    re.compile(r"\bFALLBACK_KIND_[A-Z][A-Z0-9_]*\b")),
    # Field-style assignments ``intent=...`` / ``decision=...``
    # / ``stage:exploring``. Both ``=`` and ``:`` are treated as
    # assignment because the prompt formats it as ``key: value``.
    ("intent_field",           re.compile(r"\bintent\s*[:=]",                   re.IGNORECASE)),
    ("decision_field",         re.compile(r"\bdecision\s*[:=]",                 re.IGNORECASE)),
    ("relational_frame_field", re.compile(r"\brelational_frame\s*[:=]",         re.IGNORECASE)),
    ("recommended_next_step",  re.compile(r"\brecommended_next_step\s*[:=]",    re.IGNORECASE)),
    ("fallback_kind_field",    re.compile(r"\bfallback_kind\s*[:=]",            re.IGNORECASE)),
    # Bare English diagnostic words. A customer-facing Arabic reply
    # never contains these — their presence is a strong leak signal.
    ("internal_word",          re.compile(r"\binternal\b",                      re.IGNORECASE)),
    ("debug_word",             re.compile(r"\bdebug\b",                         re.IGNORECASE)),
    ("planner_word",           re.compile(r"\bplanner\b",                       re.IGNORECASE)),
]


def contains_planner_markers(text: str) -> Optional[str]:
    """Return the name of the first matching planner-field
    fingerprint, or ``None`` when ``text`` is clean. Pure function;
    safe to call from tests."""
    if not text or not isinstance(text, str):
        return None
    for name, pattern in _PLANNER_PATTERNS:
        if pattern.search(text):
            return name
    return None


def extract_natural_segment(text: str) -> Optional[str]:
    """Best-effort recovery of the clean customer-facing portion of
    a reply that was contaminated with planner identifiers.

    Strategy
    ────────
    1. Split ``text`` on blank lines (``\\n\\n+``). Drop every
       paragraph that contains a planner marker. If anything
       survives, return it (joined back with one blank line).
    2. Otherwise split on single newlines and drop every line that
       contains a planner marker. If a non-trivial remainder
       survives, return it.
    3. Otherwise return ``None``.

    The first strategy handles the common LLM failure mode (one
    "thinking out loud" paragraph followed by a clean Arabic reply
    paragraph — exactly the June 2026 leak shape). The second is a
    fallback for replies that were not double-newline separated.
    """
    if not text or not isinstance(text, str):
        return None

    # Strategy 1: paragraph-level filtering.
    paragraphs = re.split(r"\n\s*\n+", text.strip())
    clean_paragraphs = [
        p.strip()
        for p in paragraphs
        if p.strip() and contains_planner_markers(p) is None
    ]
    if clean_paragraphs:
        recovered = "\n\n".join(clean_paragraphs).strip()
        if recovered:
            return recovered

    # Strategy 2: line-level filtering.
    lines = text.splitlines()
    clean_lines = [
        ln.strip()
        for ln in lines
        if ln.strip() and contains_planner_markers(ln) is None
    ]
    if clean_lines:
        recovered = "\n".join(clean_lines).strip()
        # Require at least 3 chars so we don't keep a stray single
        # punctuation mark that happens to sit on its own line.
        if len(recovered) >= 3:
            return recovered

    return None

# ── Safe fallback ────────────────────────────────────────────────────────────
#
# Returned to the customer when we drop a leaky reply (URLs, search
# dumps, etc.). Stays calm and short — May 2026 #2 merchant feedback
# explicitly asked us NOT to use clown-tone fallbacks ("لا نريد شخصية
# مهرج"). One 🌷, no laughter, no funnel-opener. We also avoid any
# phrasing that sounds like an order confirmation ("استلمنا طلبك" /
# "وصل") since customers read those literally.
SAFE_FALLBACK_TEXT = "أعتذر، حصل خلل بسيط في الرد. لو تكرر معك، أعد السؤال وأنا معك 🌷"


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


def _replace_body_in_payload(
    payload: Dict[str, Any],
    new_text: str,
    *,
    strip_buttons: bool = True,
) -> bool:
    """Replace the customer-facing body of a WhatsApp Cloud API
    payload with ``new_text``. Returns True if a replacement was
    performed (we matched a known shape) or False if the payload
    doesn't carry an editable text body.

    ``strip_buttons`` controls what happens to interactive payloads:

    * ``True`` (default, used for search-dump scrubs) drops every
      ``buttons`` / ``cta_url`` action because those almost certainly
      pointed at the leaky URL and are no longer meaningful once
      the body has been replaced with a generic apology.
    * ``False`` (used for planner-identifier scrubs where we
      RECOVERED the natural Arabic body) keeps the existing buttons
      since they were authored for the same reply turn — the buttons
      reflect the author's intent and stripping them would degrade
      a perfectly valid customer message.
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
                if strip_buttons:
                    action = interactive.get("action")
                    if isinstance(action, dict) and "buttons" in action:
                        action["buttons"] = []
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

        # ── Internal-planner leak (June 2026) ───────────────────
        # Try to recover the natural Arabic part of the reply
        # rather than dropping the whole message. Only fall back
        # to the generic apology when nothing recoverable remains.
        planner_match = contains_planner_markers(body)
        if planner_match:
            recovered = extract_natural_segment(body)
            if (
                recovered
                and recovered != body
                and contains_planner_markers(recovered) is None
            ):
                new_text = recovered
                outcome  = "recovered_natural_segment"
                strip_buttons = False
            else:
                new_text = SAFE_FALLBACK_TEXT
                outcome  = "fallback_no_clean_segment"
                strip_buttons = True
            logger.warning(
                "[INTERNAL_PLANNER_BLOCKED] tenant=%s to=%s marker=%s "
                "outcome=%s original_len=%d preview=%r",
                tenant_id,
                recipient,
                planner_match,
                outcome,
                len(body),
                body[:140],
            )
            if _replace_body_in_payload(
                payload, new_text, strip_buttons=strip_buttons
            ):
                return payload, True
            logger.warning(
                "[INTERNAL_PLANNER_BLOCKED] tenant=%s could not rewrite "
                "payload type=%r — blanking body",
                tenant_id, payload.get("type"),
            )
            _replace_body_in_payload(payload, "")
            return payload, True

        # ── External-research leak (May 2026) ───────────────────
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
            "[OUTBOUND_SANITIZER] crashed tenant=%s err=%s — "
            "letting payload through",
            tenant_id, exc,
        )
        return payload, False


# ── Handoff-promise leak (May 2026 P1, Tenant 33) ───────────────────────────
#
# Independent of search / planner leaks. The bug shape: the LLM (or a
# defensive code path like the loop-guard pre-send branch) produces
# Arabic text claiming "I will transfer you to a human" while the
# canonical handoff state flags (``Conversation.is_human_handoff`` /
# ``needs_human`` / ``handoff_active`` / ``status='human'``) stay
# False, so the conversation never enters the dashboard's "طلب موظف"
# inbox and the AI silently resumes on the next inbound. From the
# customer's point of view: a false promise.
#
# Two upstream fixes already shipped (loop-guard branch now flips
# every flag + persona prompt no longer ENCOURAGES the LLM to emit
# this language). This module is the wire-layer SAFETY NET — even if
# a future code path produces a handoff promise, this scrubber
# either lets it through (when state IS active, the promise is
# genuine) or rewrites it (when state is NOT active, so we don't lie
# to the customer).
#
# Patterns reuse the canonical list maintained at
# ``whatsapp_webhook.py`` ``_looks_like_owner_fallback`` (lines 6409-6440)
# so both detectors stay in sync.
# The verb root "حوّل" appears with many morphological variants in
# colloquial Saudi Arabic:
#   * Tense markers: س / سأ / راح / بـ
#   * Person markers: ك (you-singular) / كم (you-plural)
#   * Spelling: with or without أ, with or without shadda
# Rather than enumerate every combination, we anchor on the verb
# CORE (``ح?وّ?لك?``) plus a short window of optional pronoun /
# tense / preposition characters, then look for an audience noun
# (فريق / موظف / متجر) within ~12 characters. This catches the
# canonical forms + their paraphrases without false-positives on
# unrelated occurrences of "حول" (which is more commonly part of
# "حول السعر" / "تحوّل إلى" idioms — those don't end in an
# audience noun).
_TRANSFER_VERB_BODY = r"(?:س|سـ|سأ|راح\s*أ|بـ)?[أا]?حو(?:ّ|ـ)?لك?(?:م|نا)?"
_AUDIENCE_NOUN = r"(?:ال)?(?:فريق|موظف|موظفين|المتجر|متجر)"

_HANDOFF_PROMISE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.UNICODE) for p in (
        # "I will transfer YOU to …" — verb + recipient pronoun + audience.
        rf"{_TRANSFER_VERB_BODY}\s*(?:ل|لل|إلى|الى)\s*{_AUDIENCE_NOUN}",
        # "I will transfer the conversation …" — verb + المحادثة.
        rf"{_TRANSFER_VERB_BODY}\s*(?:ال)?محادثة",
        # "The team will reach out / contact you" family.
        r"الفريق\s*(?:راح|سـ?ي?|سوف)\s*(?:يتواصل|يرد|يتابع)",
        r"(?:راح|سـ?ي?|سوف)\s*(?:يتواصل|يرد|يتابع)\s*معك\s*(?:الفريق|أحد|احد)?",
        r"سيتواصل\s*معك\s*الفريق",
        r"سيتابع\s*معك\s*الفريق",
        r"(?:راح|سـ?ي?|سوف|بـ)?(?:يرد|يجاوب|يجاوبك)\s*عليك\s*(?:أحد|احد)\s*"
        r"(?:الموظفين|الموظفات|من\s*الفريق)",
        # "Your message reached the team" — implicit handoff.
        r"تم\s*تحويل\s*(?:ال)?محادثة",
        r"سيتم\s*تحويلك",
    )
)


# Neutral replacement when we strip the promise. Conservative copy
# (Tenant 33 owner explicitly said the AI must NOT clown-tone or
# escalate falsely) — we acknowledge receipt without promising any
# automated transfer.
_HANDOFF_NEUTRAL_TEXT = (
    "تمام 🌷 وصلت رسالتك، وسأخبر فريق المتجر ليتواصل معك في أقرب وقت ممكن."
)


def contains_handoff_promise(text: str) -> Optional[str]:
    """Return the first matching handoff-promise pattern, or ``None``.

    Pure predicate — does not touch state. Used by both unit tests and
    the wire-layer scrub (``maybe_scrub_handoff_promise``) so the
    detection stays in one place.
    """
    if not text or not isinstance(text, str):
        return None
    for pattern in _HANDOFF_PROMISE_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(0)
    return None


def maybe_scrub_handoff_promise(
    text: str,
    *,
    handoff_state_active: bool,
    tenant_id: Optional[int] = None,
    recipient: Optional[str] = None,
) -> Tuple[str, bool]:
    """Return ``(text_out, was_scrubbed)``.

    When ``handoff_state_active`` is True we let the promise through —
    the customer is being told the truth. When it's False AND the
    text contains a handoff promise, we replace the offending text
    with a neutral acknowledgement so the AI doesn't make a promise
    the system can't keep.

    Caller is responsible for figuring out ``handoff_state_active`` —
    typically by checking ``Conversation.is_human_handoff`` /
    ``needs_human`` / ``handoff_active`` / ``status == 'human'``.
    """
    if not text or not isinstance(text, str):
        return text or "", False

    if handoff_state_active:
        # Honest promise — let it through.
        return text, False

    match = contains_handoff_promise(text)
    if not match:
        return text, False

    logger.warning(
        "[HANDOFF_PROMISE_SCRUBBED] tenant=%s to=%s marker=%r "
        "original_len=%d preview=%r — handoff state NOT active, "
        "replacing with neutral ack",
        tenant_id, recipient, match, len(text), text[:140],
    )
    return _HANDOFF_NEUTRAL_TEXT, True


__all__ = [
    "SAFE_FALLBACK_TEXT",
    "contains_leakage_markers",
    "contains_planner_markers",
    "contains_handoff_promise",
    "extract_natural_segment",
    "maybe_scrub_handoff_promise",
    "sanitize_outbound_payload",
]
