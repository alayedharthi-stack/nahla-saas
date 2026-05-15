"""
brain/knowledge_platform_slice.py
───────────────────────────────────
Intent-scoped excerpt of the merchant's *flat* manual knowledge base.

The dashboard stores one long ``manual_knowledge_base`` string — there are
no DB-level namespaces today. Platform questions ("كيف أربط واتساب؟",
"وش الباقات؟") must see *only* KB paragraphs that match platform
signals, otherwise the LLM floods the reply with honey catalogue text.

Algorithm (deterministic, O(chunks)):
  1. Split KB on blank lines; oversize blocks split by single newlines.
  2. Normalise Arabic for matching (same spirit as intent classifiers).
  3. Score each chunk = topic-keyword hits + generic platform hits +
     overlap with the customer message tokens (length ≥ 3).
  4. Penalise chunks that look like pure product pitches (عسل، كيلو،
     سعر، اطلب) when they lack platform anchors.
  5. Keep the top-scoring chunks up to ``max_chars``.

Empty return means "no KB slice" → caller falls back to canned deflection.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, FrozenSet, List, Set, Tuple

from .intent.platform_classifier import (
    PLATFORM_AI_CAPABILITIES,
    PLATFORM_API,
    PLATFORM_CAMPAIGNS,
    PLATFORM_DASHBOARD,
    PLATFORM_GENERAL,
    PLATFORM_INTEGRATION,
    PLATFORM_META_CONNECTION,
    PLATFORM_SUBSCRIPTION,
)

_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_ZW_RE = re.compile(r"[\u200B-\u200F\u2028-\u202F\u2060-\u206F]")


def _norm(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _ZW_RE.sub("", s)
    s = _DIACRITICS_RE.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return s.lower()


def _tokenise_message(msg: str) -> Set[str]:
    """Tokens from the user message (length ≥ 3) for light relevance."""
    n = _norm(msg)
    # strip URLs
    n = re.sub(r"https?://\S+", " ", n)
    parts = re.split(r"[^\w\u0600-\u06FF]+", n)
    return {p for p in parts if len(p) >= 3}


_TOPIC_KEYWORDS: Dict[str, FrozenSet[str]] = {
    PLATFORM_SUBSCRIPTION: frozenset({
        "اشتراك", "الاشتراك", "مشترك", "الباقات", "باقه", "باقات",
        "خطة", "خطط", "الخطط", "trial", "plan", "plans", "pricing",
        "التسعير", "مجاني", "تجربه", "فترة", "renew", "upgrade",
        "ترقيه", "تجديد",
    }),
    PLATFORM_INTEGRATION: frozenset({
        "واتساب", "واتس", "whatsapp", "waba", "ربط", "الربط", "تكامل",
        "360dialog", "ثري", "سيكستي", "cloud", "api", "webhook", "هاتف",
        "رقم", "تحقق", "otp",
    }),
    PLATFORM_API: frozenset({
        "api", "webhook", "rest", "endpoint", "token", "oauth", "json",
        "integration", "تكامل", "ويبهوك",
    }),
    PLATFORM_AI_CAPABILITIES: frozenset({
        "ذكاء", "اصطناعي", "ai", "بوت", "bot", "automation", "اتمتة",
        "رد", "تلقائي", "مساعد", "محادثه", "conversation",
    }),
    PLATFORM_CAMPAIGNS: frozenset({
        "حمله", "حملات", "الحملات", "قالب", "قوالب", "template", "broadcast",
        "رسائل", "جماعي", "segment",
    }),
    PLATFORM_DASHBOARD: frozenset({
        "لوحه", "تحكم", "dashboard", "بانل", "panel", "تقارير", "analytics",
    }),
    PLATFORM_META_CONNECTION: frozenset({
        "ميتا", "meta", "facebook", "فيس", "embedded", "signup", "waba",
        "business", "اعمال",
    }),
    PLATFORM_GENERAL: frozenset({
        "نحله", "منصه", "منصة", "المنصه", "المنصة", "تطبيق", "خدمه", "خدمة",
        "ساس", "saas", "نظام",
    }),
}

_GENERIC_PLATFORM: FrozenSet[str] = frozenset().union(*_TOPIC_KEYWORDS.values())

# Penalise paragraphs that look like catalogue copy, not platform docs.
_CATALOG_NOISE: FrozenSet[str] = frozenset({
    "عسل", "السدر", "سدر", "الطلح", "طلح", "الضهيان", "ضهيان", "كيلو",
    "نصف", "علبه", "قرص", "شمع", "خلطه", "خلطة",
})


def _split_chunks(kb: str) -> List[str]:
    raw = (kb or "").strip()
    if not raw:
        return []
    blocks = re.split(r"\n\s*\n+", raw)
    chunks: List[str] = []
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        if len(b) > 900:
            for line in b.split("\n"):
                line = line.strip()
                if line:
                    chunks.append(line)
        else:
            chunks.append(b)
    return chunks if chunks else [raw]


def _score_chunk(
    chunk_norm: str,
    topic: str,
    msg_tokens: Set[str],
) -> float:
    topic_kw = _TOPIC_KEYWORDS.get(topic) or _TOPIC_KEYWORDS[PLATFORM_GENERAL]
    score = 0.0
    for kw in topic_kw:
        if kw in chunk_norm:
            score += 3.0
    for kw in _GENERIC_PLATFORM:
        if kw in chunk_norm:
            score += 0.6
    # Message overlap (weak signal)
    for tok in msg_tokens:
        if len(tok) >= 3 and tok in chunk_norm:
            score += 1.2

    prod_hits = sum(1 for w in _CATALOG_NOISE if w in chunk_norm)
    plat_hits = sum(1 for w in _GENERIC_PLATFORM if w in chunk_norm)
    if prod_hits >= 2 and plat_hits == 0:
        score -= 4.0
    if prod_hits >= 4 and plat_hits <= 1:
        score -= 6.0
    return score


def extract_platform_kb_excerpt(
    manual_knowledge_base: str,
    platform_topic: str,
    customer_message: str,
    *,
    max_chars: int = 3400,
    min_score: float = 1.5,
) -> str:
    """
    Return a concatenated excerpt of KB paragraphs relevant to a platform
    inquiry, or "" when nothing scores above ``min_score``.
    """
    chunks = _split_chunks(manual_knowledge_base)
    if not chunks:
        return ""

    topic = (platform_topic or "").strip().lower() or PLATFORM_GENERAL
    if topic not in _TOPIC_KEYWORDS:
        topic = PLATFORM_GENERAL

    msg_tokens = _tokenise_message(customer_message or "")

    scored: List[Tuple[float, str]] = []
    for ch in chunks:
        ch_n = _norm(ch)
        if len(ch_n) < 8:
            continue
        sc = _score_chunk(ch_n, topic, msg_tokens)
        scored.append((sc, ch.strip()))

    scored.sort(key=lambda x: x[0], reverse=True)

    out_parts: List[str] = []
    total = 0
    for sc, text in scored:
        if sc < min_score:
            break
        if not text:
            continue
        piece = text if text in out_parts else text
        if total + len(piece) + 2 > max_chars:
            remain = max_chars - total - 50
            if remain < 80:
                break
            piece = piece[:remain].rsplit(" ", 1)[0] + "…"
        out_parts.append(piece)
        total += len(piece) + 2
        if total >= max_chars * 0.95:
            break

    return "\n\n".join(out_parts).strip()


__all__ = ["extract_platform_kb_excerpt"]
