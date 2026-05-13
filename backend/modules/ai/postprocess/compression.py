"""
modules/ai/postprocess/compression.py
─────────────────────────────────────
Conservative, rule-based response compression for WhatsApp replies.

Why this exists
───────────────
Phase 1 (HIGH-PRIORITY layer) and Phase 3 (Product Resolver in Brain)
fixed the *intent* side of Nahla's voice — the LLM now knows it must
write short, WhatsApp-y replies and knows how to emit
[PRODUCT:...]/[MEDIA_KEY:...] markers. But Claude still occasionally
slips into "brochure mode" on a single turn:

    "بكل تأكيد يا غالي 🌷 يسعدني جدًا أن أوضح لك كل التفاصيل…
    إن متجرنا يقدم لك تشكيلة من أجود أنواع العسل
    والمنتجات الطبيعية، حيث نحرص دائمًا على…
    والشحن سريع جدًا ومجاني فوق ٢٠٠ ريال…
    [٦ أسطر إضافية]"

This module is the safety net that strips that fluff *without* asking
another LLM to rewrite the reply.  Pure post-processing:

  * collapse 3+ consecutive blank lines to 2
  * drop empty filler openers at the start of lines
    ("بكل تأكيد، …" / "يسعدني جدًا …" / etc.) when they precede
    real content
  * collapse doubled greetings ("السلام عليكم … السلام عليكم")
  * cap emoji at 2 per reply (keeps Nahla's warmth, kills the
    "emoji on every line" disease)

It NEVER:
  * rewrites words
  * paraphrases
  * touches [PRODUCT:...] / [MEDIA_KEY:...] / [MEDIA:<id>] markers
  * touches URLs
  * truncates content (would risk losing a price or a product name)

Skip semantics
──────────────
Returns ``CompressionResult(text=..., skipped=True, ...)`` when the
input is already short and clean (≤ 6 lines, ≤ 320 chars, no filler
matches, ≤ 2 emojis). That short-circuit is by design: we want zero
work on already-good replies.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List


# ── Feature flag ─────────────────────────────────────────────────────────────
# Compression is enabled by default but gated by ``RESPONSE_COMPRESSION_ENABLED``
# so we can flip it off in production within seconds if it ever clips a
# [PRODUCT:...] marker or truncates a customer-facing sentence. The env
# value is re-read on every call (cost: ~1µs) so flipping the flag in
# Render/Heroku/etc. takes effect on the very next outbound reply —
# no service restart, no rollback, no deploy.
#
# Truthy values: "1", "true", "yes", "on" (case-insensitive).
# Anything else (including empty/missing) is treated as truthy because
# the default is ON for production rollout.
_FLAG_FALSY = {"0", "false", "no", "off", "disabled"}


def _compression_enabled() -> bool:
    raw = os.getenv("RESPONSE_COMPRESSION_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() not in _FLAG_FALSY


# ── Marker / URL protection ──────────────────────────────────────────────────
# We freeze these substrings BEFORE running any transformations and thaw
# them at the end. Every regex in this module is also written narrow
# enough not to match a marker accidentally, but the freeze pass is the
# belt-and-suspenders guarantee.
_MARKER_RE = re.compile(
    r"\[(?:PRODUCT|MEDIA|MEDIA_KEY|TEMPLATE|TRANSFER|HANDOFF)\s*:[^\]]*\]",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


# ── Filler openers (Arabic) ──────────────────────────────────────────────────
# Each pattern is anchored to "start of a line/sentence" so we don't
# scrub the same words from the middle of a clause. The replacement is
# always "" (we don't substitute a shorter synonym — that would be
# paraphrasing, which we explicitly opted out of).
#
# IMPORTANT: each pattern must end in a separator (\s+ or punctuation)
# so removing it doesn't glue two words together. Patterns are tested
# in declaration order; first match wins per line.
# Arabic diacritic character class — fatha/damma/kasra/shadda/sukun
# /tanwin variants. The fillers we want to scrub commonly carry "جدًا"
# which is the sequence ج + د + ◌ً + ا (4 codepoints), so any pattern
# that says "جد?" without permitting trailing diacritics + alif fails
# on natural Arabic input. We embed ``_DIA*`` in every filler pattern
# that has an ``-an`` ending.
_DIA = r"[\u064B-\u0652\u0640]*"   # any combination of harakat / tatweel


_FILLER_OPENER_PATTERNS: List[re.Pattern[str]] = [
    # "بكل تأكيد / سرور / حب" + optional "ولا يهمك" / comma + space
    re.compile(rf"^\s*بكل\s+تأكيد{_DIA}[،,!\.…]?\s+", re.UNICODE),
    re.compile(rf"^\s*بكل\s+سرور{_DIA}[،,!\.…]?\s+", re.UNICODE),
    re.compile(rf"^\s*بكل\s+حب{_DIA}[،,!\.…]?\s+", re.UNICODE),
    re.compile(rf"^\s*بالتأكيد{_DIA}[،,!\.…]?\s+", re.UNICODE),
    # "طبعًا أكيد" / "طبعًا،"
    re.compile(rf"^\s*طبع{_DIA}ا?\s+أكيد{_DIA}[،,!\.…]?\s+", re.UNICODE),
    re.compile(rf"^\s*طبع{_DIA}ا?\s*[،,!\.…]\s+", re.UNICODE),
    # "يسعدني" / "يسعدنا" with optional "جدًا" and optional "أن|ان"
    # connector. Single unified pattern so we don't leave behind a
    # stranded "أن" at the start of the kept clause.
    re.compile(
        rf"^\s*يسعدني(?:\s+جد{_DIA}ا?)?(?:\s+(?:أن|ان))?[،,!\.…]?\s+",
        re.UNICODE,
    ),
    re.compile(
        rf"^\s*يسعدنا(?:\s+جد{_DIA}ا?)?(?:\s+(?:أن|ان))?[،,!\.…]?\s+",
        re.UNICODE,
    ),
    re.compile(rf"^\s*اسمح\s+لي\s+(?:أن|ان)\s+أوضح{_DIA}[،,!\.…]?\s+", re.UNICODE),
    re.compile(rf"^\s*دعني\s+(?:أ|ا)وضح\s+لك{_DIA}[،,!\.…]?\s+", re.UNICODE),
    re.compile(r"^\s*اسمحوا\s+لي\s+(?:أن|ان)\s+", re.UNICODE),
    re.compile(rf"^\s*كما\s+تفضلت{_DIA}[،,!\.…]?\s+", re.UNICODE),
]


# ── Doubled-greeting detection ───────────────────────────────────────────────
# Patterns that appear twice in a single short reply almost always
# indicate the LLM repeated itself. We keep the FIRST occurrence (it's
# usually contextually right) and drop the second.
_GREETING_TOKENS: List[re.Pattern[str]] = [
    re.compile(r"\bالسلام\s+عليكم(?:\s+ورحمة\s+الله(?:\s+وبركاته)?)?\b", re.UNICODE),
    re.compile(r"\bمرحب[ًا]\b", re.UNICODE),
    re.compile(r"\bأهلًا\s+و?سهل[ًا]?\b", re.UNICODE),
    re.compile(r"\bأهلين\b", re.UNICODE),
    re.compile(r"\bحياك\s+الله\b", re.UNICODE),
    re.compile(r"\bشكر[ًا]\s+لك\b", re.UNICODE),
    re.compile(r"\bشكر[ًا]\s+جزيل[ًا]\b", re.UNICODE),
    re.compile(r"\bبارك\s+الله\s+فيك\b", re.UNICODE),
]


# ── Cold-medical disclaimers (Arabic) ────────────────────────────────────────
# These are the dry/clinical phrases that drift Nahla's voice towards
# "pharmacy bot" — they ALWAYS hurt trust in a honey-shop context. The
# new High-Priority rule asks the LLM not to write them, but rule-based
# scrubbing is still the safety net for replies that slip through.
#
# Patterns match the phrase + an optional trailing punctuation so the
# scrub doesn't leave a dangling comma in the middle of a clause.
# Each pattern starts with an OPTIONAL leading connector so we vacuum
# up "لأن X" / "حيث X" / "، لكن X" along with the disclaimer body.
# Without that, removing only "X" leaves a stranded "لأن ،" mid-line.
_COLD_DISCLAIMER_PATTERNS: List[re.Pattern[str]] = [
    re.compile(
        rf"(?:[،,]\s*)?(?:لأن|إذ|حيث|و?لكن)?\s*العسل\s+غذاء\s+طبيعي\s+"
        rf"وليس\s+علاج{_DIA}(?:\s+طبي)?[،,!\.…]?",
        re.UNICODE,
    ),
    re.compile(
        rf"(?:[،,]\s*)?(?:لأن|إذ|حيث|و?لكن)?\s*العسل\s+ليس\s+"
        rf"علاج{_DIA}ا?(?:\s+طبي)?[،,!\.…]?",
        re.UNICODE,
    ),
    re.compile(
        r"(?:[،,]\s*)?(?:لأن|إذ|حيث)?\s*(?:ما|لا)\s+(?:أ|ا)قدر\s+أعدك"
        r"(?:\s+بشي(?:\s+محدد)?)?[،,!\.…]?",
        re.UNICODE,
    ),
    re.compile(
        r"(?:[،,]\s*)?(?:ما|لا)\s+(?:أ|ا)ستطيع\s+(?:أن\s+)?أعدك[،,!\.…]?",
        re.UNICODE,
    ),
    re.compile(rf"(?:[،,]\s*)?لا\s+يعالج{_DIA}(?:\s+أي\s+مرض)?[،,!\.…]?", re.UNICODE),
    re.compile(rf"(?:[،,]\s*)?مجرد\s+غذاء\s+فقط{_DIA}[،,!\.…]?", re.UNICODE),
    re.compile(rf"(?:[،,]\s*)?لا\s+يوجد\s+(?:أي\s+)?فوائد\s+مثبتة{_DIA}[،,!\.…]?", re.UNICODE),
    # "لكن الأفضل دائمًا متابعة الطبيب حسب الحالة" — generic medical
    # disclaimer addendum. Eats the preceding ", لكن" if present.
    re.compile(
        rf"(?:[،,]\s*)?(?:لكن|و?لكن|و)?\s*الأفضل\s+(?:دائم{_DIA}ا?\s+)?"
        rf"متابعة\s+الطبيب(?:\s+حسب\s+الحالة)?[،,!\.…]?",
        re.UNICODE,
    ),
    re.compile(
        r"(?:[،,]\s*)?(?:يُنصح|ينصح|يُفضّل|يفضل)\s+بمراجعة\s+الطبيب[،,!\.…]?",
        re.UNICODE,
    ),
]


# ── Stock-phrase dedup (Arabic) ──────────────────────────────────────────────
# Phrases the LLM tends to over-use across paragraphs of the same
# reply. After ``_STOCK_PHRASE_KEEP_FIRST`` occurrences we strip later
# hits — leaving the surrounding sentence intact (we only consume the
# phrase + immediately-adjacent connector/comma).
_STOCK_PHRASE_KEEP_FIRST = 2
_STOCK_PHRASES: List[re.Pattern[str]] = [
    re.compile(r"بإذن\s+الله(?:\s+تعالى)?", re.UNICODE),
    re.compile(r"حسب\s+(?:تجارب|تجربة)\s+(?:كثير\s+من\s+)?عملائنا", re.UNICODE),
    re.compile(r"حسب\s+التجارب(?:\s+الشائعة)?", re.UNICODE),
    re.compile(r"كثير\s+من\s+(?:عملائنا|العملاء|الناس)", re.UNICODE),
    re.compile(r"ضمن\s+(?:روتين|نظام)\s+(?:غذائي|صحي)(?:\s+متوازن)?", re.UNICODE),
    re.compile(r"بشكل\s+عام", re.UNICODE),
    re.compile(r"منذ\s+القدم", re.UNICODE),
]


# ── Adaptive trigger: customer asked for detailed explanation ────────────────
# When the customer explicitly asks for detail, we soften the
# compression — keep multi-paragraph replies, don't trim sentences,
# only strip fillers / doubled greetings / emoji excess / cold
# disclaimers. The rationale: the customer signed up for the long
# answer, so handing them a 2-line reply would feel evasive.
_DETAILED_REQUEST_RE = re.compile(
    r"اشرح|فصّل|فصل\s+لي|بالتفصيل|بشكل\s+مفصل|كل\s+المعلومات|كل\s+التفاصيل|"
    r"كامل\s+التفاصيل|وضّح\s+لي\s+(?:كل|الكل)|أعطني\s+(?:كل|التفاصيل)",
    re.UNICODE,
)


# ── Product recommendation markers (for paragraph "keep" decisions) ──────────
# A paragraph is worth keeping when it contains a [PRODUCT:...] marker
# placeholder OR one of these recommendation verbs. We never trim a
# paragraph that has a recommendation. Patterns intentionally don't
# eat trailing whitespace — they're used as boolean detectors only.
_RECOMMENDATION_TOKENS: List[re.Pattern[str]] = [
    re.compile(r"\u0000M\u0000", re.UNICODE),         # frozen marker placeholder
    re.compile(rf"يفضّل(?:ون|ها|ه|ن)?{_DIA}", re.UNICODE),
    re.compile(rf"يفضلون{_DIA}", re.UNICODE),
    re.compile(rf"يستخدم(?:ون|ها|ه|ن)?{_DIA}", re.UNICODE),
    re.compile(rf"أرشّح(?:\s+لك)?{_DIA}", re.UNICODE),
    re.compile(rf"أرشح(?:\s+لك)?{_DIA}", re.UNICODE),
    re.compile(r"نرشح(?:\s+لك)?", re.UNICODE),
    re.compile(rf"أنصحك{_DIA}", re.UNICODE),
    re.compile(r"\bعسل\s+\S+", re.UNICODE),           # "عسل X" as a brand cue
]


# ── Emoji range (broad: covers most BMP + SMP emoji code points) ─────────────
# We cap *frequency* per reply, not per line — Nahla's voice still keeps
# a couple of emojis for warmth, just never one per sentence.
#
# The character class also swallows the trailing modifiers/joiners that
# come after a base emoji glyph — VS16 (U+FE0F), VS15 (U+FE0E), and the
# ZWJ (U+200D). Without those, removing "✅\uFE0F" left a stray VS16 in
# the output which renders as a tofu box in WhatsApp.
_EMOJI_RE = re.compile(
    "(?:"
    "["
    "\U0001F300-\U0001F6FF"   # symbols & pictographs / transport
    "\U0001F700-\U0001F77F"   # alchemical
    "\U0001F900-\U0001F9FF"   # supplemental symbols & pictographs
    "\U0001FA70-\U0001FAFF"   # extended-A
    "\U00002600-\U000026FF"   # misc symbols
    "\U00002700-\U000027BF"   # dingbats
    "\U0001F1E6-\U0001F1FF"   # regional indicators (flags)
    "\U0001F300-\U0001F5FF"
    "]"
    "[\uFE0E\uFE0F\u200D]*"   # optional variation selectors + ZWJ
    ")",
    flags=re.UNICODE,
)


# ── Tunables ─────────────────────────────────────────────────────────────────
DEFAULT_EMOJI_CAP = 2
DEFAULT_SHORT_LINES = 6
DEFAULT_SHORT_CHARS = 320
# Hard cap on the number of *paragraphs* (blocks separated by blank
# lines) we keep when the customer didn't ask for a detailed answer.
# 4 = greeting + up to 2 recommendation paragraphs + CTA.
DEFAULT_MAX_PARAGRAPHS = 4
# Soft cap used when the customer DID ask for detail.
DEFAULT_MAX_PARAGRAPHS_DETAILED = 8


@dataclass
class CompressionResult:
    """Outcome of one compression pass — both the new text and metrics."""
    text: str
    skipped: bool = False
    skip_reason: str = ""
    lines_before: int = 0
    lines_after: int = 0
    chars_before: int = 0
    chars_after: int = 0
    fillers_removed: int = 0
    greetings_deduped: int = 0
    blank_lines_collapsed: int = 0
    emojis_removed: int = 0
    cold_disclaimers_removed: int = 0
    stock_phrase_dedups: int = 0
    paragraphs_before: int = 0
    paragraphs_after: int = 0
    paragraphs_dropped: int = 0
    adaptive_mode: bool = False
    duration_ms: int = 0
    markers_preserved: int = 0
    urls_preserved: int = 0
    # Aggregated change indicator — true iff any rule actually changed text.
    any_change: bool = False

    def to_log_dict(self) -> Dict[str, object]:
        d = asdict(self)
        # Don't ship the full text in the log line — only sizes matter.
        d.pop("text", None)
        return d


def compress_for_whatsapp(
    text: str,
    *,
    customer_message: str = "",
    emoji_cap: int = DEFAULT_EMOJI_CAP,
    short_lines: int = DEFAULT_SHORT_LINES,
    short_chars: int = DEFAULT_SHORT_CHARS,
    max_paragraphs: int = DEFAULT_MAX_PARAGRAPHS,
    max_paragraphs_detailed: int = DEFAULT_MAX_PARAGRAPHS_DETAILED,
) -> CompressionResult:
    """Apply the conservative compression rules to ``text``.

    Pure function — no IO, no network, no model call. Safe to invoke
    on every outbound AI reply. Returns a CompressionResult.

    ``customer_message`` (the inbound text the LLM is replying to) is
    used for *adaptive* compression: when the customer asks explicitly
    for a detailed answer ("اشرح / فصّل / بالتفصيل / كل التفاصيل"),
    we soften the rules — keep multi-paragraph replies, don't trim
    sentences, only do the always-safe cleanups (fillers, doubled
    greetings, emoji cap, cold disclaimers). The customer signed up
    for the long answer in that case.

    Skip conditions (text returned untouched, ``skipped=True``):

    * empty / whitespace-only input
    * "already short and clean": ≤ short_lines visible lines AND
      ≤ short_chars chars AND no filler-opener hit AND no doubled
      greeting AND no cold disclaimer AND emoji_count within cap.

    Non-skip path:
      1. freeze markers + URLs to placeholders
      2. collapse 3+ blank lines → 2
      3. remove filler openers anchored at line starts
      4. strip cold-medical disclaimers (anywhere in text)
      5. dedupe doubled greetings
      6. dedupe over-used stock phrases (keep first 2 occurrences)
      7. cap emoji frequency
      8. trim oversized paragraphs (NON-adaptive only)
      9. drop now-empty / disclaimer-only paragraphs
     10. enforce paragraph cap if still > max_paragraphs
     11. whitespace cleanup
     12. thaw markers + URLs
    """
    t0 = time.perf_counter()
    original = text or ""
    result = CompressionResult(text=original)
    result.chars_before = len(original)
    result.lines_before = _count_visible_lines(original)
    result.paragraphs_before = _count_paragraphs(original)

    # ── Kill-switch: env flag ─────────────────────────────────────────────
    # Read on every call so flipping ``RESPONSE_COMPRESSION_ENABLED=false``
    # in the host environment kills compression on the next reply with
    # no restart / no rollback / no deploy. Skip-path still emits a
    # CompressionResult so the call-site logging stays uniform.
    if not _compression_enabled():
        result.skipped = True
        result.skip_reason = "disabled_by_flag"
        result.chars_after = result.chars_before
        result.lines_after = result.lines_before
        result.paragraphs_after = result.paragraphs_before
        result.duration_ms = int((time.perf_counter() - t0) * 1000)
        return result

    if not original.strip():
        result.skipped = True
        result.skip_reason = "empty"
        result.chars_after = result.chars_before
        result.lines_after = result.lines_before
        result.duration_ms = int((time.perf_counter() - t0) * 1000)
        return result

    # ── Adaptive mode detection ───────────────────────────────────────────
    # The signal lives in the *customer's* message, not the LLM reply.
    result.adaptive_mode = bool(
        customer_message
        and _DETAILED_REQUEST_RE.search(customer_message or "")
    )
    effective_paragraph_cap = (
        max_paragraphs_detailed if result.adaptive_mode else max_paragraphs
    )

    # ── Cheap pre-flight: is the reply already short and clean? ───────────
    # Skip iff EVERY signal we know how to fix is absent. The check is
    # cheap (one pass per pattern family) so a short clean reply still
    # exits in <1ms.
    visible = original.strip()
    emoji_count_initial = len(_EMOJI_RE.findall(visible))
    has_filler_initial = any(
        p.search("\n" + visible) for p in _FILLER_OPENER_PATTERNS
    )
    has_doubled_greeting_initial = any(
        len(p.findall(visible)) >= 2 for p in _GREETING_TOKENS
    )
    has_cold_disclaimer = any(
        p.search(visible) for p in _COLD_DISCLAIMER_PATTERNS
    )
    has_stock_overuse = any(
        len(p.findall(visible)) > _STOCK_PHRASE_KEEP_FIRST for p in _STOCK_PHRASES
    )
    over_paragraphs = result.paragraphs_before > effective_paragraph_cap
    if (
        result.lines_before <= short_lines
        and len(visible) <= short_chars
        and not has_filler_initial
        and not has_doubled_greeting_initial
        and not has_cold_disclaimer
        and not has_stock_overuse
        and not over_paragraphs
        and emoji_count_initial <= emoji_cap
        and "\n\n\n" not in original
    ):
        result.skipped = True
        result.skip_reason = "already_short"
        result.chars_after = result.chars_before
        result.lines_after = result.lines_before
        result.paragraphs_after = result.paragraphs_before
        result.duration_ms = int((time.perf_counter() - t0) * 1000)
        return result

    # ── 1. Freeze markers + URLs ──────────────────────────────────────────
    frozen_markers: List[str] = _MARKER_RE.findall(original)
    text_work = _MARKER_RE.sub("\u0000M\u0000", original)
    frozen_urls: List[str] = _URL_RE.findall(text_work)
    text_work = _URL_RE.sub("\u0000U\u0000", text_work)
    result.markers_preserved = len(frozen_markers)
    result.urls_preserved = len(frozen_urls)

    # ── 2. Collapse 3+ blank lines to exactly 2 (one empty line) ──────────
    before_blank = text_work
    text_work = re.sub(r"\n{3,}", "\n\n", text_work)
    if text_work != before_blank:
        result.blank_lines_collapsed = before_blank.count("\n\n\n")

    # ── 3. Remove filler openers per line ─────────────────────────────────
    # Claude stacks fillers ("بكل تأكيد، يسعدني جدًا، طبعًا أكيد …") —
    # so we keep iterating per line until no more openers match. A
    # safety cap of 4 prevents pathological inputs from stalling.
    new_lines: List[str] = []
    fillers_removed = 0
    for line in text_work.split("\n"):
        stripped = line
        for _ in range(4):
            matched_any = False
            for pat in _FILLER_OPENER_PATTERNS:
                new_stripped, n = pat.subn("", stripped, count=1)
                if n:
                    stripped = new_stripped
                    fillers_removed += 1
                    matched_any = True
                    break
            if not matched_any:
                break
        new_lines.append(stripped)
    text_work = "\n".join(new_lines)
    result.fillers_removed = fillers_removed

    # ── 3.5. Strip cold-medical disclaimers ───────────────────────────────
    # These almost always sit mid-sentence as a comma-separated clause.
    # We don't try to preserve the surrounding comma — the next
    # whitespace-cleanup pass will smooth things out.
    cold_removed = 0
    for pat in _COLD_DISCLAIMER_PATTERNS:
        text_work, n = pat.subn("", text_work)
        cold_removed += n
    result.cold_disclaimers_removed = cold_removed

    # ── 4. Dedupe doubled greetings (within the whole reply) ──────────────
    greetings_removed = 0
    for pat in _GREETING_TOKENS:
        hits = list(pat.finditer(text_work))
        if len(hits) >= 2:
            # Keep the first hit, blank out the rest by clipping each
            # later occurrence + its immediate trailing punctuation.
            for h in hits[1:]:
                start, end = h.span()
                trail = text_work[end:end + 2]
                trim = end
                # Eat a comma/exclamation right after the duplicate if present.
                if trail and trail[0] in "،,!.…":
                    trim += 1
                    if len(trail) > 1 and trail[1] == " ":
                        trim += 1
                text_work = text_work[:start] + text_work[trim:]
                greetings_removed += 1
                # finditer holds stale positions — re-run the loop from
                # scratch on the mutated text to avoid offset drift.
                break
            # After one removal the spans we cached are invalid; re-scan
            # the same pattern on the fresh text. Bounded by total
            # original occurrences so we always terminate.
            while True:
                new_hits = list(pat.finditer(text_work))
                if len(new_hits) < 2:
                    break
                h = new_hits[-1]
                start, end = h.span()
                trail = text_work[end:end + 2]
                trim = end
                if trail and trail[0] in "،,!.…":
                    trim += 1
                    if len(trail) > 1 and trail[1] == " ":
                        trim += 1
                text_work = text_work[:start] + text_work[trim:]
                greetings_removed += 1
    result.greetings_deduped = greetings_removed

    # ── 4.5. Dedupe over-used stock phrases ──────────────────────────────
    # Each pattern is searched independently. After
    # ``_STOCK_PHRASE_KEEP_FIRST`` occurrences we strip later hits
    # along with the immediately-trailing comma/connector so we don't
    # leave dangling "،" mid-line.
    stock_dedups = 0
    for pat in _STOCK_PHRASES:
        hits = list(pat.finditer(text_work))
        if len(hits) <= _STOCK_PHRASE_KEEP_FIRST:
            continue
        # Walk later hits in reverse so we don't invalidate offsets.
        for h in reversed(hits[_STOCK_PHRASE_KEEP_FIRST:]):
            start, end = h.span()
            # Eat one trailing punctuation + space if present.
            trail = text_work[end:end + 2]
            tail_cut = 0
            if trail and trail[0] in "،,!.…":
                tail_cut += 1
                if len(trail) > 1 and trail[1] == " ":
                    tail_cut += 1
            # Eat one leading "، " before the phrase if present.
            head_cut = 0
            if start >= 2 and text_work[start - 1] == " " and text_work[start - 2] in "،,":
                head_cut = 2
            text_work = text_work[:start - head_cut] + text_work[end + tail_cut:]
            stock_dedups += 1
    result.stock_phrase_dedups = stock_dedups

    # ── 5. Cap emoji frequency ────────────────────────────────────────────
    emoji_hits = list(_EMOJI_RE.finditer(text_work))
    emojis_removed = 0
    if len(emoji_hits) > emoji_cap:
        # Keep the first ``emoji_cap`` emojis, strip the rest (in
        # reverse order so we don't invalidate earlier spans).
        for h in reversed(emoji_hits[emoji_cap:]):
            start, end = h.span()
            text_work = text_work[:start] + text_work[end:]
            emojis_removed += 1
    result.emojis_removed = emojis_removed

    # ── 6. Whitespace cleanup pass 1 (before paragraph operations) ───────
    text_work = re.sub(r"[ \t]{2,}", " ", text_work)
    text_work = re.sub(r"\n[ \t]+\n", "\n\n", text_work)
    text_work = re.sub(r"\n{3,}", "\n\n", text_work)
    # Drop dangling commas that disclaimer/stock-phrase removal leaves.
    text_work = re.sub(r"\s+[،,]\s*$", "", text_work, flags=re.MULTILINE)
    text_work = re.sub(r"\s+[،,]\s*\n", "\n", text_work)
    text_work = re.sub(r"^[،,]\s+", "", text_work, flags=re.MULTILINE)
    # The Arabic em-dash divider "— ،" after a removed disclaimer reads
    # as a leftover scar — collapse "— ،" to "—" and "—\s*،" to "—".
    text_work = re.sub(r"—\s*[،,]\s*", "— ", text_work)

    # ── 7. Paragraph-level pruning ────────────────────────────────────────
    # Only kicks in for *non-adaptive* replies. We:
    #   (a) drop paragraphs that are now empty or shorter than 12 chars
    #   (b) drop paragraphs that lost their recommendation (i.e. had
    #       a cold disclaimer ripped out and nothing actionable remains)
    #   (c) cap total paragraphs at ``effective_paragraph_cap`` keeping
    #       the highest-value ones (greeting + markers + CTA first)
    paragraphs = _split_paragraphs(text_work)
    paragraphs_before_prune = len(paragraphs)
    pruned: List[str] = []
    for idx, para in enumerate(paragraphs):
        stripped = para.strip()
        if not stripped:
            continue
        if _is_broken_stub(stripped):
            # Paragraph either: started with a topic dash ("X — لأن …")
            # whose actionable content was ripped out by disclaimer
            # removal, OR starts with a stranded connector. Either way
            # showing it to the customer reads as a half-thought.
            continue
        pruned.append(stripped)
    if not result.adaptive_mode and len(pruned) > effective_paragraph_cap:
        # Score each paragraph; keep the highest-scoring ones, but
        # preserve original order in the rendered output.
        scored = [(_paragraph_score(p, idx, len(pruned)), idx, p) for idx, p in enumerate(pruned)]
        scored.sort(key=lambda t: (-t[0], t[1]))
        keep_idx = sorted(t[1] for t in scored[:effective_paragraph_cap])
        pruned = [pruned[i] for i in keep_idx]
    result.paragraphs_after = len(pruned)
    result.paragraphs_dropped = max(0, paragraphs_before_prune - len(pruned))
    text_work = "\n\n".join(pruned)

    # ── 8. Whitespace cleanup pass 2 (after paragraph operations) ────────
    text_work = "\n".join(line.rstrip() for line in text_work.split("\n"))
    text_work = re.sub(r"\n{3,}", "\n\n", text_work)
    text_work = text_work.strip("\n")

    # ── 9. Thaw URLs then markers ─────────────────────────────────────────
    for u in frozen_urls:
        text_work = text_work.replace("\u0000U\u0000", u, 1)
    for m in frozen_markers:
        text_work = text_work.replace("\u0000M\u0000", m, 1)

    result.text = text_work
    result.chars_after = len(text_work)
    result.lines_after = _count_visible_lines(text_work)
    result.any_change = (text_work != original)
    result.duration_ms = int((time.perf_counter() - t0) * 1000)

    # Edge case: every rule was a no-op. Tag with skip_reason=no_changes
    # so the log captures "we looked but didn't need to do anything".
    if not result.any_change:
        result.skipped = True
        result.skip_reason = "no_changes"
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _count_visible_lines(text: str) -> int:
    """Count non-blank lines — the metric merchants actually care about."""
    if not text:
        return 0
    return sum(1 for line in text.split("\n") if line.strip())


def _count_paragraphs(text: str) -> int:
    """Count paragraphs (blocks separated by blank lines)."""
    if not text:
        return 0
    return sum(1 for p in _split_paragraphs(text) if p.strip())


def _split_paragraphs(text: str) -> List[str]:
    """Split on blank-line boundaries (one or more empty lines)."""
    if not text:
        return []
    return re.split(r"\n\s*\n", text)


_BROKEN_STUB_CONNECTORS_RE = re.compile(
    r"^\s*(?:لأن|إذ|حيث|و?لكن|و|عند)\b",
    re.UNICODE,
)


def _is_broken_stub(paragraph: str) -> bool:
    """True if a paragraph reads like a cold-disclaimer leftover.

    Stub detection requires evidence that the paragraph WAS a topic
    explanation that lost its body — either an explicit "X —"
    structure with empty/connector-led residue, OR a paragraph that
    starts with a stranded connector ("لأن X / لكن X"). A short
    plain greeting like "حياك الله" or "يا هلا يا ام ابراهيم" is
    NOT a stub — it stands on its own — so the no-dash branch
    requires a connector at the start.
    """
    if not paragraph:
        return False
    if _looks_actionable(paragraph):
        return False
    has_dash = "—" in paragraph
    if has_dash:
        after_dash = paragraph.rsplit("—", 1)[-1].strip(" \t،,!.…")
        if not after_dash:
            return True
        if _BROKEN_STUB_CONNECTORS_RE.search(after_dash):
            return True
        # Filler-only residue AFTER a dash, short and not actionable
        # ("X — بشكل عام يدخل في الروتين الصحي عند").
        if len(after_dash) < 80:
            return True
        return False
    # No dash — flag only when the paragraph itself starts with a
    # stranded connector (the head was ripped off by a disclaimer).
    return bool(_BROKEN_STUB_CONNECTORS_RE.search(paragraph))


def _looks_actionable(paragraph: str) -> bool:
    """True if the paragraph has a marker / recommendation / question.

    Used as the gate that prevents the paragraph-pruning pass from
    dropping the only line that actually advances the sale.
    """
    if not paragraph:
        return False
    # Frozen marker placeholder OR explicit question mark counts as
    # actionable on its own.
    if "\u0000M\u0000" in paragraph:
        return True
    if "؟" in paragraph or "?" in paragraph:
        return True
    for pat in _RECOMMENDATION_TOKENS:
        if pat.search(paragraph):
            return True
    return False


def _paragraph_score(paragraph: str, idx: int, total: int) -> int:
    """Rank paragraphs for retention when we exceed the paragraph cap.

    Higher score = more likely to be kept. The ranking is intentionally
    coarse — we only need to distinguish "must keep" (greeting / CTA /
    product paragraphs) from "fillers".
    """
    score = 0
    # Markers — the LLM emitted a product/media reference; we MUST keep.
    if "\u0000M\u0000" in paragraph:
        score += 100
    # Questions (CTA) — also must keep.
    if "؟" in paragraph or "?" in paragraph:
        score += 80
    # Recommendation verbs / product cues.
    for pat in _RECOMMENDATION_TOKENS[1:]:
        if pat.search(paragraph):
            score += 40
            break
    # Greeting position bonus (first paragraph).
    if idx == 0:
        score += 30
    # CTA position bonus (last paragraph).
    if idx == total - 1:
        score += 30
    # Length penalty — long pure-prose paragraphs without any of the
    # above are the most likely to be filler.
    if len(paragraph) > 180 and score < 40:
        score -= 20
    return score
