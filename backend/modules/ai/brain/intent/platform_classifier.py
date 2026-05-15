"""
brain/intent/platform_classifier.py
───────────────────────────────────
Deterministic detector for "this customer is asking about NAHLA (the
SaaS platform), not the merchant's products."

Why this layer exists
─────────────────────
A merchant's customer occasionally asks about the platform powering
the conversation: subscription tiers, API access, WhatsApp Business
linking, Meta connection, campaign tooling, dashboard, AI features.
Common Gulf-Arabic phrasings:

  * "كم اشتراك نحلة؟"
  * "وش الباقات؟"
  * "كيف أربط واتساب الأعمال؟"
  * "عندكم API؟"
  * "كيف أربط مع ميتا؟"
  * "وش يسوي الذكاء الاصطناعي؟"
  * "ودي أعرف عن المنصة"

The legacy intent chain matched several of these against the broad
``INTENT_ASK_PRODUCT`` regex (which catches ``أبغى/أريد/أبي`` +
anything) and routed them into the catalogue flow. The merchant
brain then tried to look up "الاشتراك" or "API" as products and
either improvised a wrong answer or attempted a sales redirect.
Worst-case (May 2026 voice-note incident): a customer's transcribed
audio about "الذكاء، الاشتراك، الربط" was parsed as a product order.

Public contract
───────────────
``classify_platform(message: str) -> PlatformMatch | None``

Returns ``PlatformMatch(topic=str, confidence=float)`` or ``None``.
Confidence: 0.93 — beats commerce intents (ASK_PRODUCT 0.82,
ASK_PRICE 0.90, START_ORDER 0.88) without overriding HIGHER-priority
deterministic signals like INTENT_WHO_ARE_YOU (0.98) or
INTENT_ASK_PAYMENT_INFO (0.95).

Disambiguation principle
────────────────────────
Several platform tokens are AMBIGUOUS in isolation:
  * "نحلة"            — could be the platform OR the merchant's bot persona
  * "الذكاء"          — could be platform AI OR "honey for memory/intelligence"
  * "الحملات"          — could be platform marketing OR generic Arabic word
  * "API" / "Webhook" — almost always platform; safe alone

To prevent false positives we require EITHER:
  (a) a strong platform token alone (api, waba, embedded signup, meta,
      المنصة, الباقات/الخطط, لوحة التحكم, dashboard, etc.), OR
  (b) two weaker signals co-occurring within the same message
      (e.g., "نحلة" + "اشتراك"، "الذكاء" + "نحلة", "الحملات" + "ربط").

This keeps "كم سعر العسل ياقات نحلة الفاخرة" (a merchant product
named "نحلة") OUT of platform inquiry while keeping "كم سعر اشتراك
نحلة" IN.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, Set


# ── Platform topic constants ─────────────────────────────────────────────────
PLATFORM_SUBSCRIPTION    = "subscription"
PLATFORM_INTEGRATION     = "integration"
PLATFORM_API             = "api"
PLATFORM_AI_CAPABILITIES = "ai_capabilities"
PLATFORM_CAMPAIGNS       = "campaigns"
PLATFORM_DASHBOARD       = "dashboard"
PLATFORM_META_CONNECTION = "meta_connection"
PLATFORM_GENERAL         = "general_platform"


@dataclass(frozen=True)
class PlatformMatch:
    topic: str
    confidence: float


# ── Arabic normaliser (kept private — same shape as scope_tiers /
#    social_classifier so all three modules speak the same language). ─────
_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_ZW_RE         = re.compile(r"[\u200B-\u200F\u2028-\u202F\u2060-\u206F]")


def _norm(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _ZW_RE.sub("", s)
    s = _DIACRITICS_RE.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return s.lower().strip()


# ── STRONG tokens — sufficient alone to classify as platform ─────────────────
# These tokens have essentially zero ambiguity with a honey shop's
# domain. A message containing any one of them gets classified.
_STRONG_PLATFORM_TOKENS: dict[str, str] = {
    # Subscription / packages — "اشتراك نحلة" / "باقات نحلة"
    "اشتراك في نحله":     PLATFORM_SUBSCRIPTION,
    "اشتراك نحله":         PLATFORM_SUBSCRIPTION,
    "اشتراك في المنصه":   PLATFORM_SUBSCRIPTION,
    "اشتراك المنصه":       PLATFORM_SUBSCRIPTION,
    "باقات نحله":          PLATFORM_SUBSCRIPTION,
    "باقات المنصه":        PLATFORM_SUBSCRIPTION,
    "خطط نحله":            PLATFORM_SUBSCRIPTION,
    "خطط المنصه":          PLATFORM_SUBSCRIPTION,
    "تجربه مجانيه":        PLATFORM_SUBSCRIPTION,
    "free trial":          PLATFORM_SUBSCRIPTION,
    # API / Webhook
    " api":                PLATFORM_API,
    "api ":                PLATFORM_API,
    "^api$":               PLATFORM_API,   # handled via regex below too
    "webhook":             PLATFORM_API,
    "rest api":            PLATFORM_API,
    # Meta / Embedded Signup
    "embedded signup":     PLATFORM_META_CONNECTION,
    "facebook login":      PLATFORM_META_CONNECTION,
    "meta business":       PLATFORM_META_CONNECTION,
    "ميتا للاعمال":         PLATFORM_META_CONNECTION,
    "ربط مع ميتا":          PLATFORM_META_CONNECTION,
    "ربط ميتا":             PLATFORM_META_CONNECTION,
    "تطبيق ميتا":           PLATFORM_META_CONNECTION,
    # WhatsApp Business platform
    "whatsapp business":   PLATFORM_INTEGRATION,
    "واتساب الاعمال":       PLATFORM_INTEGRATION,
    "واتس اب الاعمال":     PLATFORM_INTEGRATION,
    "واتس الاعمال":         PLATFORM_INTEGRATION,
    "ربط الواتساب":         PLATFORM_INTEGRATION,
    "ربط واتساب":           PLATFORM_INTEGRATION,
    "اربط واتساب":          PLATFORM_INTEGRATION,
    "تربط واتساب":          PLATFORM_INTEGRATION,
    "ربط الرقم":             PLATFORM_INTEGRATION,
    "waba":                PLATFORM_INTEGRATION,
    "360dialog":           PLATFORM_INTEGRATION,
    "360 dialog":          PLATFORM_INTEGRATION,
    "ثري سيكستي":           PLATFORM_INTEGRATION,
    # Dashboard
    "لوحه التحكم":          PLATFORM_DASHBOARD,
    "لوحه تحكم":            PLATFORM_DASHBOARD,
    "dashboard":           PLATFORM_DASHBOARD,
    "بانل":                PLATFORM_DASHBOARD,
    # Direct platform name + clear platform context
    "منصه نحله":           PLATFORM_GENERAL,
    "نحله saas":           PLATFORM_GENERAL,
    "نحله ساس":            PLATFORM_GENERAL,
    "تطبيق نحله":          PLATFORM_GENERAL,
    "خدمات نحله":          PLATFORM_GENERAL,
    "تسعير نحله":          PLATFORM_SUBSCRIPTION,
    "سعر نحله":            PLATFORM_SUBSCRIPTION,
    "كم نحله":             PLATFORM_SUBSCRIPTION,
    "كم تكلف نحله":        PLATFORM_SUBSCRIPTION,
}


# Regex for tokens that need word-boundary matching (e.g. ``api``
# alone, otherwise we'd false-positive on Arabic strings containing
# the letters).
_STRONG_REGEX_RULES = [
    (re.compile(r"\bapi\b", re.IGNORECASE),       PLATFORM_API),
    (re.compile(r"\bwebhook\b", re.IGNORECASE),   PLATFORM_API),
    (re.compile(r"\bwaba\b", re.IGNORECASE),      PLATFORM_INTEGRATION),
    (re.compile(r"\bcrm\b", re.IGNORECASE),       PLATFORM_INTEGRATION),
]


# ── WEAK tokens — need at least TWO to fire ──────────────────────────────────
# These can appear in non-platform contexts (a customer's bot persona,
# a marketing offer, etc.) so we only treat them as platform signals
# when two or more co-occur OR they co-occur with the platform name.
_WEAK_PLATFORM_TOKENS: Set[str] = {
    "نحله",                # ambiguous with bot persona / merchant naming
    "المنصه",              # could be a generic word
    "الذكاء الاصطناعي",     # could be a customer asking about AI generally
    "الذكاء",              # very ambiguous alone
    "الحملات",             # could be ad campaigns from any store
    "الاشتراك",            # could be "اشتراك المتجر" — meh; usually platform
    "الباقات",             # could be product bundles in some stores
    "الخطط",               # also ambiguous
    "ربط",                 # too generic alone
    "نظام",
    "automation",
    "اتمته",
    "اتمتة",
    "روبوت",
    "بوت",
    "chatbot",
}

# Mapping from weak token → topic when the token participates in a
# 2-of-N match. The first matching token's topic wins.
_WEAK_TOKEN_TOPICS: dict[str, str] = {
    "الاشتراك":            PLATFORM_SUBSCRIPTION,
    "الباقات":             PLATFORM_SUBSCRIPTION,
    "الخطط":               PLATFORM_SUBSCRIPTION,
    "الذكاء الاصطناعي":     PLATFORM_AI_CAPABILITIES,
    "الذكاء":              PLATFORM_AI_CAPABILITIES,
    "الحملات":             PLATFORM_CAMPAIGNS,
    "automation":          PLATFORM_AI_CAPABILITIES,
    "اتمته":               PLATFORM_AI_CAPABILITIES,
    "اتمتة":               PLATFORM_AI_CAPABILITIES,
    "روبوت":               PLATFORM_AI_CAPABILITIES,
    "بوت":                 PLATFORM_AI_CAPABILITIES,
    "chatbot":             PLATFORM_AI_CAPABILITIES,
    "ربط":                 PLATFORM_INTEGRATION,
    "نظام":                PLATFORM_GENERAL,
    "نحله":                PLATFORM_GENERAL,
    "المنصه":              PLATFORM_GENERAL,
}


# ── Disqualifiers ────────────────────────────────────────────────────────────
# When the message clearly references a HONEY product context, we
# don't classify as platform even if a weak token co-occurs. This
# protects against "كم الذكاء اللي يخليني أتذكر" being misread.
_HONEY_DISQUALIFIERS = (
    "عسل", "السدر", "السمر", "الطلح", "المنجم", "الضهيان",
    "كيلو", "نصف كيلو", "علبه", "علب",
    "قرص", "شمع", "نحلات",   # nahla bees (the literal insect)
    "غذاء ملكات", "حبه البركه", "الزنجبيل", "الجنسنج",
)


def _has_honey_context(norm: str) -> bool:
    return any(kw in norm for kw in _HONEY_DISQUALIFIERS)


# ── Public entry point ───────────────────────────────────────────────────────
def classify_platform(message: str) -> Optional[PlatformMatch]:
    """Detect platform-inquiry messages.

    Always returns either a ``PlatformMatch`` or ``None``. Never
    raises. The function is O(N) in the number of registered tokens
    (small constant) and O(1) in message length for the strong-token
    path, O(N) for the weak-token cooccurrence path. Both well under
    100µs in practice — safe to call on the synchronous critical path.
    """
    if not message or not isinstance(message, str):
        return None
    norm = _norm(message)
    if not norm:
        return None

    # 1. STRONG regex rules first — covers "api", "webhook", "waba"
    #    which need word boundaries to avoid false-positives on Arabic
    #    text containing the latin letters.
    for pattern, topic in _STRONG_REGEX_RULES:
        if pattern.search(message):
            return PlatformMatch(topic=topic, confidence=0.95)

    # 2. STRONG substring tokens — multi-word phrases that are
    #    essentially unambiguous on their own.
    for token, topic in _STRONG_PLATFORM_TOKENS.items():
        # Skip the pseudo-regex entries (they were duplicates from
        # the regex section, kept for documentation).
        if token.startswith("^") or token.endswith("$"):
            continue
        if token in norm:
            return PlatformMatch(topic=topic, confidence=0.95)

    # 3. WEAK token co-occurrence path.
    #    Skip when the message clearly speaks honey — protects against
    #    "حملة عسل" / "عسل النحلة" / similar merchant phrasing.
    if _has_honey_context(norm):
        return None

    hits = [tok for tok in _WEAK_PLATFORM_TOKENS if tok in norm]
    if len(hits) >= 2:
        # Pick the first registered topic in priority order:
        # SUBSCRIPTION > INTEGRATION > AI > CAMPAIGNS > GENERAL.
        for priority_topic in (
            PLATFORM_SUBSCRIPTION,
            PLATFORM_INTEGRATION,
            PLATFORM_AI_CAPABILITIES,
            PLATFORM_CAMPAIGNS,
            PLATFORM_DASHBOARD,
            PLATFORM_META_CONNECTION,
            PLATFORM_GENERAL,
        ):
            for tok in hits:
                if _WEAK_TOKEN_TOPICS.get(tok) == priority_topic:
                    return PlatformMatch(topic=priority_topic, confidence=0.93)

    return None


__all__ = [
    "PLATFORM_SUBSCRIPTION",
    "PLATFORM_INTEGRATION",
    "PLATFORM_API",
    "PLATFORM_AI_CAPABILITIES",
    "PLATFORM_CAMPAIGNS",
    "PLATFORM_DASHBOARD",
    "PLATFORM_META_CONNECTION",
    "PLATFORM_GENERAL",
    "PlatformMatch",
    "classify_platform",
]
