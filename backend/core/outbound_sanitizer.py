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

# ── Internal sales-policy / prompt-instruction fingerprints (May 2026) ────────
# LLM occasionally quotes its own system prompt back to the customer —
# e.g. "حسب قواعد البيع التدريجي Progressive Selling…". These MUST
# never reach WhatsApp.
_POLICY_LEAK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("progressive_selling_en", re.compile(r"progressive\s+selling", re.IGNORECASE)),
    ("progressive_selling_ar", re.compile(r"البيع\s*التدريجي", re.UNICODE)),
    ("rules_prefix_ar",        re.compile(r"حسب\s*قواعد\s*(?:البيع\s*)?التدريجي", re.UNICODE)),
    ("rules_generic_ar",       re.compile(r"حسب\s*القواعد\b", re.UNICODE)),
    ("system_instructions_ar", re.compile(r"تعليمات\s*النظام", re.UNICODE)),
    ("reply_policy_ar",        re.compile(r"سياسة\s*الرد", re.UNICODE)),
    ("internal_policy_en",     re.compile(r"\binternal\s+policy\b", re.IGNORECASE)),
    ("decision_engine_en",     re.compile(r"\bdecision\s+engine\b", re.IGNORECASE)),
    ("routing_en",             re.compile(r"\brouting\b", re.IGNORECASE)),
    ("prompt_en",              re.compile(r"\bprompt\b", re.IGNORECASE)),
    ("classifier_en",          re.compile(r"\bclassifier\b", re.IGNORECASE)),
    ("high_priority_block",    re.compile(r"\bHIGH\s+PRIORITY\b", re.IGNORECASE)),
    ("brain_state_json",       re.compile(r"\bBrainStateJSON\b", re.IGNORECASE)),
    ("response_goal_field",    re.compile(r"\bresponse_goal\s*[:=]", re.IGNORECASE)),
]


def contains_policy_leak_markers(text: str) -> Optional[str]:
    """Return the first matching internal-policy fingerprint, or ``None``."""
    from core.outbound_leakage_firewall import contains_outbound_leak  # noqa: PLC0415

    hit = contains_outbound_leak(text)
    if hit and hit not in {
        "response_goal", "execute_pending_offer", "resolve_ambiguous_need",
        "action_token", "goal_constant", "fallback_kind_const",
        "intent_field", "decision_field", "relational_frame_field",
        "recommended_next_step", "fallback_kind_field",
        "internal_word", "debug_word", "planner_word",
    }:
        return hit
    # Legacy direct scan for policy-only patterns
    if not text or not isinstance(text, str):
        return None
    for name, pattern in _POLICY_LEAK_PATTERNS:
        if pattern.search(text):
            return name
    return None


def contains_internal_instruction_leak(text: str) -> Optional[str]:
    """Planner fields OR internal policy / prompt names in customer text."""
    from core.outbound_leakage_firewall import contains_outbound_leak  # noqa: PLC0415

    return contains_outbound_leak(text)


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


def _drop_leaky_sentences(text: str) -> str:
    """Remove sentences that contain internal instruction/policy leaks."""
    if not text:
        return text or ""
    chunks = re.split(r"(?<=[.!?؟\n])\s+", text.strip())
    clean = [
        c.strip()
        for c in chunks
        if c.strip() and contains_internal_instruction_leak(c) is None
    ]
    return " ".join(clean).strip()


def sanitize_outbound_text(
    text: str,
    *,
    tenant_id: Optional[int] = None,
    recipient: Optional[str] = None,
) -> Tuple[str, bool]:
    """Scrub internal planner/policy leakage from a plain-text reply."""
    from core.outbound_leakage_firewall import firewall_outbound_text  # noqa: PLC0415

    return firewall_outbound_text(
        text,
        tenant_id=tenant_id,
        recipient=recipient,
        fallback_text=SAFE_FALLBACK_TEXT,
    )


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
        if p.strip() and contains_internal_instruction_leak(p) is None
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
        if ln.strip() and contains_internal_instruction_leak(ln) is None
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

        # ── Internal planner / policy leak (June 2026) ──────────
        sanitized_body, was_internal_scrub = sanitize_outbound_text(
            body,
            tenant_id=tenant_id,
            recipient=recipient,
        )
        if was_internal_scrub:
            strip_buttons = sanitized_body == SAFE_FALLBACK_TEXT
            if _replace_body_in_payload(
                payload, sanitized_body, strip_buttons=strip_buttons
            ):
                return payload, True
            logger.warning(
                "[INTERNAL_POLICY_BLOCKED] tenant=%s could not rewrite "
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


# When scrubbing a false handoff promise, strip the claim only —
# never inject a generic ACK stub (P0 / Nahla Doctrine).
_HANDOFF_NEUTRAL_TEXT = ""


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

    stripped = text
    scrubbed = False
    while True:
        match = contains_handoff_promise(stripped)
        if not match:
            break
        if not scrubbed:
            logger.warning(
                "[HANDOFF_PROMISE_SCRUBBED] tenant=%s to=%s marker=%r "
                "original_len=%d preview=%r — handoff state NOT active, "
                "stripping promise (no generic ack injection)",
                tenant_id, recipient, match, len(text), text[:140],
            )
        stripped = stripped.replace(match, "").strip()
        stripped = re.sub(r"\s{2,}", " ", stripped)
        scrubbed = True
    return stripped, scrubbed


# ── Promised-asset leak (May 2026 P1, Tenant 33) ───────────────────────────
#
# Independent of all the above. Bug shape: the LLM, after the
# progressive-selling rewrite, started producing replies like:
#   - "أرسل لك الرابط بعد التأكد منه"   (no URL in the reply)
#   - "تفضل رقم أبو هشام"                 (no phone digits / call card)
#   - "امسح الباركود من تطبيق الراجحي"   (no [MEDIA_KEY:...] attached)
#   - "تفضل الموقع على الخريطة"          (no maps link)
#
# Each is a FALSE promise: the customer is told an asset is about
# to arrive but no asset is actually queued. Three upstream things
# already help (media_key safety net for barcodes, product safety
# net for store links, staff-contact safety net for phones), but
# they catch the cases where the customer's INBOUND is recognisable.
# When the LLM volunteers a promise out of nowhere ("سأرسل لك
# الرابط" inside an answer about working hours), none of those nets
# trigger because the customer never asked.
#
# This sanitizer is the wire-layer net: scan the outbound TEXT for a
# small library of promise patterns, ask the caller whether the
# matching asset class is actually present in the outbound dispatch,
# and rewrite the promise span when it's NOT. We do not attempt to
# SYNTHESISE the asset here — that's not our job; the upstream
# resolvers are responsible. Our job is to keep the AI honest.
#
# Pattern library is intentionally small (~25 patterns) and the
# matcher returns the first hit per asset class — false negatives
# are fine (the original promise stays), false positives are not
# (we'd over-edit a perfectly valid reply). The verbs are anchored
# on the imperative future ("سأرسل" / "أرسل لك" / "تفضل" / "هذا") so
# generic Arabic prose like "أرسلت لكم سابقًا الباركود" doesn't
# trip the rule.

# Asset classes we detect promises for.
ASSET_LINK     = "link"
ASSET_BARCODE  = "barcode"
ASSET_PHONE    = "phone"
ASSET_LOCATION = "location"


# Verbs that, taken together with an object noun below, signal an
# active promise ("I will send you …" / "here is …"). We allow
# small inflection windows (س + future / تفضل + ك + ل / هذا /
# أعطيك) without enumerating every possible morphological form.
_PROMISE_VERB = (
    r"(?:"
    r"(?:س|سـ|سأ|راح\s*[أا]?)?\s*[أا]?رسل\s*(?:ل|إل)ك(?:م)?|"
    r"(?:س|سـ)?[أا]?رفق\s*(?:ل|إل)ك(?:م)?|"
    r"[أا]?رسل\s*(?:ل|إل)ك(?:م)?|"
    r"(?:س|سـ)?[أا]?عطي?ك(?:م)?|"
    r"تفضّ?ل(?:ي)?|"
    r"هذا|هذه|"
    r"[أا]?بعث\s*(?:ل|إل)ك(?:م)?"
    r")"
)


# Object nouns per asset class. Each is checked WITH the promise
# verb in a short window (≤ 30 chars) so we anchor on actual
# imperative-future constructions.
_LINK_NOUN     = r"(?:ال)?(?:رابط|لينك|link|url|صفحة\s*المتجر|رابط\s*المتجر)"
_BARCODE_NOUN  = (
    r"(?:ال)?(?:باركود|بار\s*كود|qr|كيوار|كيو\s*ار|"
    r"رمز\s*(?:الدفع|التحويل|السداد))"
)
_PHONE_NOUN    = r"(?:ال)?(?:رقم|جوال|هاتف|تواصل)"
_LOCATION_NOUN = (
    r"(?:ال)?(?:موقع|عنوان|خريطة|location|map|"
    r"موقع\s*(?:الفرع|المتجر))"
)


def _compile_pair(noun: str) -> re.Pattern[str]:
    # Verb on the left within 0-15 chars before the noun.
    return re.compile(
        rf"{_PROMISE_VERB}[\s\S]{{0,30}}?{noun}",
        re.IGNORECASE | re.UNICODE,
    )


# Standalone shorthand: "الرابط:" / "الباركود:" / "الرقم:" / "الموقع:"
# on their own line introduces an asset. Without the verb context
# these are still promises ("here is X: __").
_STANDALONE_INTRO = re.compile(
    r"(?:^|\n)\s*"
    r"(?P<class>الرابط|الباركود|الرقم|الموقع|اللينك)"
    r"\s*[:：]\s*$",
    re.IGNORECASE | re.UNICODE | re.MULTILINE,
)


_PROMISE_PATTERNS: Dict[str, tuple] = {
    ASSET_LINK:     (_compile_pair(_LINK_NOUN),),
    ASSET_BARCODE:  (_compile_pair(_BARCODE_NOUN),),
    ASSET_PHONE:    (_compile_pair(_PHONE_NOUN),),
    ASSET_LOCATION: (_compile_pair(_LOCATION_NOUN),),
}

# Product-list prompts use «رقم الخيار/المنتج» — not contact-phone promises.
_PRODUCT_OPTION_NUMBER_RE = re.compile(
    r"(?:"
    r"اختر\s+رقم\s+(?:ال)?(?:خيار|منتج)"
    r"|اكتب\s+رقم\s+(?:ال)?(?:خيار|منتج)"
    r"|(?:^|[\s\n])رقم\s+(?:ال)?(?:خيار|منتج)"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def _is_product_option_number_context(text: str) -> bool:
    """True when «رقم» refers to a catalog option index, not a contact phone."""
    if not text:
        return False
    return bool(_PRODUCT_OPTION_NUMBER_RE.search(text))


# Customer commerce ledger replies refer to the customer's WhatsApp
# identity («على هذا الرقم») — not a staff-contact phone promise.
# Without this guard, ``_PROMISE_VERB``'s «هذا» + ``_PHONE_NOUN``'s
# «رقم» false-positive and rewrite the span to the staff-contact
# fallback («حالياً لا يوجد رقم تواصل مهيأ لإرساله.»).
_CUSTOMER_LEDGER_PHONE_CONTEXT_RE = re.compile(
    r"(?:"
    r"ما\s+ظهر\s+لي\s+طلبات\s+مسجلة\s+على\s+هذا\s+الرقم"
    r"|طلبات\s+مسجلة\s+عندنا\s+على\s+هذا\s+الرقم"
    r"|طلبات\s+مسجلة\s+على\s+هذا\s+الرقم"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def _is_customer_ledger_phone_context(text: str) -> bool:
    """True when «هذا الرقم» is the customer's identity, not a contact promise."""
    if not text:
        return False
    return bool(_CUSTOMER_LEDGER_PHONE_CONTEXT_RE.search(text))


_ORDER_REFERENCE_NUMBER_CONTEXT_RE = re.compile(
    r"(?:"
    r"بهذا\s+الرقم"
    r"|برقم\s+الطلب"
    r"|رقم\s+الطلب"
    r"|لم\s+أجد\s+طلب"
    r"|ما\s+قدرت\s+ألقى\s+طلب"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def _is_order_reference_phone_context(text: str) -> bool:
    """True when «رقم» refers to an order reference, not a staff-contact promise."""
    if not text:
        return False
    return bool(_ORDER_REFERENCE_NUMBER_CONTEXT_RE.search(text))


# Phone shape — Saudi mobile (05XXXXXXXX / +9665XXXXXXXX / 9665XXXXXXXX),
# plus generic international (+\d{7,15}) as a permissive fallback.
_PHONE_DIGITS_RE = re.compile(
    r"(?:\+?966|00966|0)?5\d{8}|\+\d{7,15}",
)


def _contains_url(text: str) -> bool:
    if not text:
        return False
    return bool(_URL_RE.search(text))


def _contains_phone(text: str) -> bool:
    if not text:
        return False
    return bool(_PHONE_DIGITS_RE.search(text))


# Neutral replacements per asset class. We rewrite the OFFENDING
# SPAN only (not the whole reply) so the customer still receives
# whatever else the LLM said in the same turn. Replacement copy is
# conservative — no clown tone, no automated escalation, no false
# bullet of "I will follow up". Each replacement either (a) admits
# the asset isn't ready and ASKS what the customer needs, or
# (b) acknowledges the request without committing to a delivery
# this turn.
# Neutral replacements per asset class.
#
# May 2026 #38 follow-up: the prior PHONE / LOCATION strings
# ("خبّرنا بنوع الاستفسار وسنوصلك بالشخص المختص" /
#  "خبّرنا بالفرع أو المنطقة وسنوضّح لك تفاصيل الموقع") were
# themselves false promises — they tell the customer "we'll
# connect you" or "we'll explain the location" while the system
# is in fact unable to honor either. The replacement copy is now
# OPENLY HONEST: it admits the asset isn't on file. The artifact
# guard upstream (``apply_outbound_artifact_guard``) takes the
# same line — when the asset is genuinely missing we say so,
# never escalate.
#
# Phrasing was revised across two production iterations:
#   • May 2026 #31 — first pass dropped "تكفي لحظة …" prefix.
#   • May 2026 #38 — current pass dropped "وسنوصلك" /
#     "وسنوضّح" promises that the wire layer can't fulfill.
_PROMISE_REPLACEMENTS: Dict[str, str] = {
    # LINK and BARCODE keep a soft "I'll get back to you" feel
    # because those assets ARE recoverable in many tenants
    # (a structured store URL or a mobile-app barcode picture).
    # PHONE and LOCATION default to a warm "we couldn't surface
    # it but we can still help" line — the cold "غير مضاف في
    # بيانات المتجر" copy used to leak a system-internal phrase
    # that the customer didn't need to see, and read as a
    # complaint against the merchant. The phrasing below stays
    # conversational and offers an actionable next step instead.
    ASSET_LINK:     "لحظة وأجيب لك التفاصيل 🌷",
    ASSET_BARCODE:  "خبّرنا بالمبلغ وسنوضّح لك طريقة الدفع المناسبة 🌷",
    ASSET_PHONE:    "حالياً لا يوجد رقم تواصل مهيأ لإرساله.",
    ASSET_LOCATION: "أبشر 🌷 الموقع ما طلع لي مباشرة، أقدر أرسل لك تفاصيل الفرع أو أساعدك بطريقة ثانية.",
}


def contains_promised_asset(text: str) -> Optional[str]:
    """Return the asset class of the first detected promise, or ``None``.

    Pure predicate. Useful for tests + log annotations. Order is
    deterministic — we check link → barcode → phone → location.
    """
    if not text or not isinstance(text, str):
        return None
    skip_phone = (
        _is_product_option_number_context(text)
        or _is_customer_ledger_phone_context(text)
        or _is_order_reference_phone_context(text)
    )
    for asset_class, patterns in _PROMISE_PATTERNS.items():
        if skip_phone and asset_class == ASSET_PHONE:
            continue
        for pattern in patterns:
            if pattern.search(text):
                return asset_class
    m = _STANDALONE_INTRO.search(text)
    if m and not skip_phone:
        token = (m.group("class") or "").strip()
        if token in ("الرابط", "اللينك"):
            return ASSET_LINK
        if token == "الباركود":
            return ASSET_BARCODE
        if token == "الرقم":
            return ASSET_PHONE
        if token == "الموقع":
            return ASSET_LOCATION
    return None


def maybe_scrub_unkept_asset_promise(
    text: str,
    *,
    has_url: bool,
    has_media: bool,
    has_phone: bool,
    has_product_card: bool = False,
    tenant_id: Optional[int] = None,
    recipient: Optional[str] = None,
    skip_asset_promise_scrub: bool = False,
) -> Tuple[str, bool, Optional[str]]:
    """Return ``(text_out, was_scrubbed, asset_class)``.

    Inputs:
      * ``text``       — final outbound text (post marker extraction).
      * ``has_url``    — at least one ``https?://`` URL in ``text``
                          OR a product card / CTA URL is queued.
      * ``has_media``  — at least one media attachment queued
                          (image / video / document).
      * ``has_phone``  — explicit phone digits in ``text`` OR a
                          ``[CALL:...]`` contact card is queued.
      * ``has_product_card`` — kept distinct because product cards
                          carry both a URL AND an image; useful for
                          the location class which wants either.

    Behaviour:
      * If no promise pattern matches → ``(text, False, None)``.
      * If promise matches AND the matching asset is present →
        ``(text, False, asset_class)``. We still report the class so
        upstream can log "promise honoured" if it wants.
      * If promise matches AND the asset is MISSING → we replace the
        offending SPAN (not the whole text) with the neutral copy
        and return ``(rewritten, True, asset_class)``. Logged as
        ``[ASSET_PROMISE_SCRUBBED]`` so production can audit.

    The caller is responsible for collecting ``has_url`` / ``has_media``
    / ``has_phone`` / ``has_product_card`` from the actual outbound
    state. We deliberately don't read the conversation model here —
    keeps the sanitizer pure and unit-testable.
    """
    if not text or not isinstance(text, str):
        return text or "", False, None

    if skip_asset_promise_scrub:
        return text, False, None

    asset_class = contains_promised_asset(text)
    if not asset_class:
        return text, False, None

    asset_present = {
        ASSET_LINK:     (has_url or has_product_card),
        ASSET_BARCODE:  has_media,
        ASSET_PHONE:    has_phone,
        ASSET_LOCATION: (has_url or has_product_card),
    }.get(asset_class, True)

    # Pre-scrub trace — emitted on every promise hit (honoured or
    # scrubbed). Lets production triage answer "did the resolver
    # ship the asset, or did the LLM just write a soft promise
    # that got rewritten?" without enabling DEBUG. Pair with
    # ``[STAFF_CONTACT_TRACE]`` / ``[STAFF_CONTACT_GRAPH]`` /
    # ``[STAFF_CONTACT_RESOLVER]`` to walk the whole chain in one
    # grep.
    logger.info(
        "[ASSET_PROMISE_TRACE] tenant=%s to=%s asset_class=%s "
        "asset_present=%s has_url=%s has_media=%s has_phone=%s "
        "has_product_card=%s text_len=%d",
        tenant_id, recipient, asset_class,
        bool(asset_present),
        bool(has_url), bool(has_media), bool(has_phone),
        bool(has_product_card), len(text),
    )

    if asset_present:
        # Honest promise — let it through. We don't log here on
        # purpose; the success path is the common case and the
        # existing ``[OUTBOUND_MEDIA_ATTACH]`` / CTA logs already
        # tell the operator the asset went out.
        return text, False, asset_class

    # Replace each matching span. We keep the rest of the reply
    # intact so a useful "thanks + ask question" turn isn't lost
    # just because one promise sentence wasn't honoured.
    replacement = _PROMISE_REPLACEMENTS.get(asset_class) or ""
    rewritten = text
    matched_any = False
    for pattern in _PROMISE_PATTERNS.get(asset_class, ()):
        if pattern.search(rewritten):
            rewritten = pattern.sub(replacement, rewritten)
            matched_any = True
    if not matched_any:
        # Standalone intro shape ("الرابط:") — replace the whole
        # intro+line.
        rewritten = _STANDALONE_INTRO.sub("\n" + replacement, rewritten)

    # Collapse any double-blank-lines that the substitution opened.
    rewritten = re.sub(r"\n{3,}", "\n\n", rewritten).strip()

    logger.warning(
        "[ASSET_PROMISE_SCRUBBED] tenant=%s to=%s asset_class=%s "
        "has_url=%s has_media=%s has_phone=%s has_product_card=%s "
        "original_len=%d preview=%r",
        tenant_id, recipient, asset_class,
        bool(has_url), bool(has_media), bool(has_phone), bool(has_product_card),
        len(text), text[:140],
    )
    return rewritten, True, asset_class


__all__ = [
    "SAFE_FALLBACK_TEXT",
    "ASSET_LINK",
    "ASSET_BARCODE",
    "ASSET_PHONE",
    "ASSET_LOCATION",
    "contains_leakage_markers",
    "contains_planner_markers",
    "contains_policy_leak_markers",
    "contains_internal_instruction_leak",
    "contains_handoff_promise",
    "contains_promised_asset",
    "extract_natural_segment",
    "maybe_scrub_handoff_promise",
    "maybe_scrub_unkept_asset_promise",
    "sanitize_outbound_payload",
    "sanitize_outbound_text",
]
