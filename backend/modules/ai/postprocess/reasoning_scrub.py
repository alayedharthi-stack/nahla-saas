"""
modules/ai/postprocess/reasoning_scrub.py
─────────────────────────────────────────
Detects and strips LLM-internal reasoning prose that accidentally
leaks into the customer-facing reply.

Why this exists
───────────────
Production observation (May 2026): a customer asked "أبي أكلم أمين"
and Claude replied with::

    هيثم، العميل يطلب التواصل مع أمين. بناءً على السياق
    (هيثم الحارثي - رقم بديل ثاني في قاعدة المعرفة)، هذا يبدو
    أنه شخص من الفريق الداخلي وليس عميلًا عاديًا.

    تفضل رقم أمين 🌷
    0541690226

    تقدر تتواصل معه مباشرة على الواتساب 👍

The customer received the reasoning paragraph as if it were part
of the merchant's reply. That paragraph is:

  * a meta-address to the merchant ("هيثم،")
  * 3rd-person narration of the customer's request
  * an explicit reference to KB internals ("رقم بديل ثاني في
    قاعدة المعرفة")
  * a meta-classification of the contact ("شخص من الفريق
    الداخلي وليس عميلًا عاديًا")

All of those are signals that Claude is "thinking out loud" instead
of speaking AS the merchant. The prompt forbids this but the model
still slips occasionally — so we add a deterministic post-process
that drops any line containing one of these signals BEFORE the
reply hits the WhatsApp send code.

Rules
─────
1. **Line-level granularity.** We drop ENTIRE LINES that match a
   leak pattern, not just the matching span — because the leakage
   usually pollutes the whole sentence, not a sub-clause.
2. **Conservative patterns only.** Every pattern in
   ``_LEAK_PATTERNS`` is a high-confidence "this is reasoning, not
   speech" signal. False positives here mean the customer sees a
   broken reply — so we only ship a pattern after seeing it in
   real Claude output.
3. **Whitespace tidy.** After dropping lines we collapse 3+ blank
   lines to 1 so the reply doesn't have huge holes.
4. **Markers never touched.** ``[PRODUCT:...]``, ``[MEDIA_KEY:...]``,
   ``[CALL:...]`` are passed through verbatim — they're consumed by
   the resolvers, not by this module.

Feature flag
────────────
``REASONING_SCRUB_ENABLED`` (env, default ON). Read on every call
so the kill-switch is instant. Truthy: ``1/true/yes/on``. Falsy:
``0/false/no/off/disabled``.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, asdict
from typing import Dict, List


# ── Feature flag ─────────────────────────────────────────────────────────────
_FLAG_FALSY = {"0", "false", "no", "off", "disabled"}


def _scrub_enabled() -> bool:
    raw = os.getenv("REASONING_SCRUB_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() not in _FLAG_FALSY


# ── Leak patterns ────────────────────────────────────────────────────────────
# Each pattern is anchored on a phrase that ONLY appears when the
# LLM is narrating its own reasoning instead of speaking to the
# customer. We deliberately keep this list small — adding a noisy
# pattern can silently truncate a legitimate reply.
#
# Adding a new pattern: paste a real production reply line into a
# unit test first, confirm the regex matches that line and ONLY
# that line, then add it here. Do NOT add patterns based on
# theoretical leaks — Claude doesn't write what we imagine, it
# writes what it actually writes.

_LEAK_PATTERNS: List[re.Pattern[str]] = [
    # "بناءً على السياق" / "بناء على السياق" / "بناءً على التعليمات"
    re.compile(
        r"بناء?\s*[ًٍ]?\s*على\s+(?:السياق|التعليمات|قاعدة\s+المعرفة|المعلومات)",
        re.UNICODE,
    ),
    # Direct KB references
    re.compile(r"في\s+قاعدة\s+المعرفة", re.UNICODE),
    re.compile(r"من\s+قاعدة\s+المعرفة", re.UNICODE),
    re.compile(r"حسب\s+(?:التعليمات|السياق|قاعدة\s+المعرفة)", re.UNICODE),
    # The exact "أنه شخص من الفريق الداخلي" leak
    re.compile(
        r"يبدو\s+أنه\s+(?:شخص\s+من\s+)?الفريق\s+(?:الداخلي|الإداري)",
        re.UNICODE,
    ),
    re.compile(r"شخص\s+من\s+الفريق\s+(?:الداخلي|الإداري)", re.UNICODE),
    # "وليس عميلًا عاديًا" — classification leak
    re.compile(r"وليس\s+عميل[ًاٍ]+\s+عادي[ًاٍ]+", re.UNICODE),
    re.compile(r"ليس\s+عميل[ًاٍ]+\s+عادي[ًاٍ]+", re.UNICODE),
    # 3rd-person narration of the customer's request
    # ("العميل يطلب / يسأل / يريد / يحتاج")
    re.compile(
        r"(?:^|\s|،,)(?:العميل|الزبون|المستخدم)\s+(?:يطلب|يسأل|يريد|يحتاج|يبحث)",
        re.UNICODE,
    ),
    # KB-internal labels
    re.compile(r"رقم\s+بديل\s+(?:ثاني|أول|ثاني[ةى]?)", re.UNICODE),
    re.compile(r"بديل\s+(?:ثاني|أول)\s+في\s+قاعدة", re.UNICODE),
    # Meta-instructions Claude sometimes echoes back
    re.compile(r"كما\s+ذُكر\s+في\s+(?:التعليمات|السياق)", re.UNICODE),
    re.compile(r"وفق[ًاٍ]?\s+(?:للتعليمات|للسياق)", re.UNICODE),
]


# Meta-address patterns: line starts with "<MerchantName>،" followed
# by reasoning prose. We detect these only when the line ALSO
# matches one of the leak patterns above — that prevents us from
# eating a legitimate greeting like "هيثم، أهلًا بك!".
_MERCHANT_ADDRESS_RE = re.compile(
    r"^\s*(?:هيثم|أمين|هشام|سعد|محمد|أبو\s+\w+)\s*[،,]",
    re.UNICODE,
)


# ──────────────────────────────────────────────────────────────────
# Result type
# ──────────────────────────────────────────────────────────────────


@dataclass
class ScrubResult:
    """Outcome of one scrub pass."""
    text: str
    skipped: bool = False
    skip_reason: str = ""
    lines_before: int = 0
    lines_after: int = 0
    lines_dropped: int = 0
    pattern_hits: Dict[str, int] = None  # pattern_index -> hits
    duration_ms: int = 0
    any_change: bool = False

    def __post_init__(self):
        if self.pattern_hits is None:
            self.pattern_hits = {}

    def to_log_dict(self) -> Dict[str, object]:
        d = asdict(self)
        d.pop("text", None)
        return d


# ──────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────


def scrub_reasoning_leaks(text: str) -> ScrubResult:
    """Drop lines containing LLM-reasoning prose.

    Pure function — no IO, no LLM, no network. Always-on when the
    flag is enabled; safe to call on every outbound reply.

    Returns a :class:`ScrubResult` whose ``text`` field is the
    cleaned reply. When no patterns matched, ``text == input`` and
    ``any_change=False``. Empty input is returned unchanged with
    ``skipped=True``.
    """
    t0 = time.perf_counter()
    original = text or ""
    result = ScrubResult(text=original)
    result.lines_before = _count_lines(original)

    if not _scrub_enabled():
        result.skipped = True
        result.skip_reason = "disabled_by_flag"
        result.lines_after = result.lines_before
        result.duration_ms = int((time.perf_counter() - t0) * 1000)
        return result

    if not original.strip():
        result.skipped = True
        result.skip_reason = "empty"
        result.lines_after = result.lines_before
        result.duration_ms = int((time.perf_counter() - t0) * 1000)
        return result

    new_lines: List[str] = []
    dropped = 0
    hits: Dict[str, int] = {}
    for line in original.split("\n"):
        stripped = line.strip()
        if not stripped:
            new_lines.append(line)
            continue

        matched_idx = _line_matches_leak(stripped)
        if matched_idx is None:
            new_lines.append(line)
            continue

        # Drop the line. Tally which pattern fired (for [REASONING_SCRUB] log).
        dropped += 1
        key = f"pattern_{matched_idx}"
        hits[key] = hits.get(key, 0) + 1

    cleaned = "\n".join(new_lines)
    # Collapse 3+ blank lines to 1 (the dropped lines left holes).
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip("\n")

    result.text = cleaned
    result.lines_after = _count_lines(cleaned)
    result.lines_dropped = dropped
    result.pattern_hits = hits
    result.any_change = (cleaned != original)
    result.duration_ms = int((time.perf_counter() - t0) * 1000)
    if not result.any_change:
        result.skipped = True
        result.skip_reason = "no_changes"
    return result


# ──────────────────────────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────────────────────────


def _line_matches_leak(line: str) -> int | None:
    """Return the index of the first pattern that matches ``line``,
    else ``None``. Order is preserved so high-value patterns can
    short-circuit cheaper ones."""
    if not line:
        return None
    for idx, pat in enumerate(_LEAK_PATTERNS):
        if pat.search(line):
            return idx
    return None


def _count_lines(text: str) -> int:
    if not text:
        return 0
    return sum(1 for line in text.split("\n") if line.strip())


__all__ = [
    "ScrubResult",
    "scrub_reasoning_leaks",
]
