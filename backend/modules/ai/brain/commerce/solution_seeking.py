"""
brain/commerce/solution_seeking.py
──────────────────────────────────
Tenant-agnostic commerce intelligence: distinguish **solution-seeking /
attribute-based / outcome-based** product intent from **unknown SKU /
bare price** intent.

This is NOT a honey-specific or tenant-33 patch. Any merchant category
(food, perfume, clothing, electronics, supplements, …) can receive
advisory commerce routing when the customer describes a *need* or
*attribute* rather than naming a product.

Semantic category (intent): ``solution_seeking_commerce``
Legacy alias: ``need_based_product_advice``

Bad routing:
  "أي منتج تقصد؟ اكتب اسمه…"

Good routing:
  LLM advisory using KB/attributes, optional 1–2 high-confidence picks,
  intelligent need clarification (never bare SKU name request).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

logger = logging.getLogger("nahla.brain.solution_seeking")

# Closed solution axes — category-agnostic outcome/attribute buckets.
AXIS_HEALTH_DIET = "health_diet"
AXIS_AUDIENCE = "audience_age"
AXIS_FORMALITY = "formality_occasion"
AXIS_SEASON = "season_climate"
AXIS_SIZE_FIT = "size_fit"
AXIS_PERFORMANCE = "performance_spec"
AXIS_DURABILITY = "durability_longevity"
AXIS_GENERAL = "general_attribute"

SOLUTION_SEEKING_CONFIDENCE = 0.94

# Commerce / catalog inquiry context — any vertical, not honey-only.
_COMMERCE_CONTEXT_RE = re.compile(
    r"(?:"
    r"منتج|شي(?:ء)?|بضاع|سلعه|صنف|نوع|ماركه|"
    r"عطر|perfume|fragrance|"
    r"جوال|موبايل|هاتف|phone|smartphone|"
    r"لابت|op|laptop|notebook|computer|"
    r"كامير|camera|"
    r"لبس|ملاب|ثوب|فستان|قمي|حذاء|"
    r"عسل|honey|supplement|"
    r"عند(?:ك|كم)|لد(?:يك|يكم)|يوجد|موجود|"
    r"تنصحن|ترشح|تنصح|ترشد|"
    r"افضل|أفضل|best|recommend"
    r")",
    re.IGNORECASE | re.UNICODE,
)

# Pure unit/price tail — not solution-seeking.
_PRICE_ONLY_TAIL_RE = re.compile(
    r"(?:كم\s*سعر|بكم|سعر\s*ال|قد\s*ايش|how\s*much)"
    r"[\s\u0020]*"
    r"(?:ال)?(?:كilo|كيلo|كيلو|كيلوغرام|kg|gram|جرام)?"
    r"\s*$",
    re.UNICODE | re.IGNORECASE,
)

# Explicit bare SKU / name lookup — NOT solution-seeking.
_BARE_NAME_LOOKUP_RE = re.compile(
    r"^(?:"
    r"(?:كم|بكم|سعر|price)\s+"
    r"|(?:ابغ|ابي|أبغ|أبي|اريد|أريد|ودي|بدي)\s+"
    r")?.{1,40}$",
    re.UNICODE | re.IGNORECASE,
)

_AXIS_RULES: List[Tuple[str, List[str]]] = [
    (AXIS_HEALTH_DIET, [
        r"ما\s*يرفع\s*السكر", r"لا\s*يرفع\s*السكر", r"بدون\s*سكر",
        r"مناسب\s*ل(?:ل)?(?:سكر|السكر|السكري|مرضى\s*السكر|دايت|رجيم|دايت)",
        r"ل(?:ل)?(?:سكر|السكر|السكري|مرضى\s*السكر|دايت|رجيم)",
        r"خفيف\s*على\s*المعد", r"ل(?:ل)?(?:معد|قولون|كح[ةه]|سعال)",
        r"diabetic|diabetes|diet|sugar\s*free|light\s*on\s*stomach",
    ]),
    (AXIS_AUDIENCE, [
        r"ل(?:ل)?(?:اطفال|الاطفال|أطفال|الأطفال|رضع|babies|kids|children)",
        r"يناسب\s*ال(?:اطفال|أطفال)",
        r"مقاس\s*يناسب",
    ]),
    (AXIS_FORMALITY, [
        r"رسمي", r"formal", r"مناسب\s*ل(?:ل)?(?:عمل|دوام|اجتماع|مناسبه|زفاف)",
        r"لبس\s*رسمي",
    ]),
    (AXIS_SEASON, [
        r"ل(?:ل)?(?:صيف|الصيف|شتاء|الشتاء|spring|summer|winter)",
        r"عطر\s*ل(?:ل)?صيف",
    ]),
    (AXIS_SIZE_FIT, [
        r"واسع", r"wide", r"loose", r"oversized", r"مقاس\s*كبير",
        r"مقاس\s*صغير", r"يناسب\s*مقاس",
    ]),
    (AXIS_PERFORMANCE, [
        r"بطار", r"battery", r"قوي(?:ة)?\s*(?:البطار|الاداء|الأداء)?",
        r"ل(?:ل)?(?:مونتاج|تصوير|gaming|العاب|الألعاب|مونتاج)",
        r"تصوير\s*ليل", r"night\s*(?:photo|shot|mode)",
        r"for\s*editing|video\s*edit",
    ]),
    (AXIS_DURABILITY, [
        r"ثابت", r"long\s*lasting", r"يفضل\s*طويل", r"ثبات",
    ]),
    (AXIS_GENERAL, [
        r"مناسب\s*ل", r"suitable\s*for", r"good\s*for",
        r"بدون\s+\w", r"without\s+\w",
    ]),
]

# Structural solution-seeking (attribute/outcome phrasing without SKU name).
_STRUCTURE_PATTERNS: List[str] = [
    r"(?:منتج|شي(?:ء)?)\s+(?:بدون|مناسب|ل(?:ل)?)",
    r"(?:عند(?:ك|كم)|هل\s+(?:عند|يوجد)).{0,35}(?:مناسب|بدون|ما\s*يرفع|ل(?:ل)?|ثابت|قوي|واسع|خفيف)",
    r"(?:تنصحن|ترشح|وش\s+تنصح|ايش\s+تنصح).{0,40}",
    r"(?:افضل|أفضل|best)\s+\S.{2,40}(?:ل(?:ل)?|for|بدون)",
    r"\S.{2,30}\s+(?:بدون|مناسب\s*ل|ل(?:ل)?(?:دايت|رسم|صيف|اطف|معد|نوم|سكر))",
]

# Negative gate — do NOT treat as solution-seeking when the turn is clearly
# delivery / payment / support / order / location (tenant-agnostic).
_NEGATIVE_GATE_RULES: List[Tuple[str, List[str]]] = [
    ("delivery_intent", [
        r"توصيل", r"شحن", r"مندوب", r"مناديب", r"استلام",
        r"يوصل", r"توصل", r"تشحن", r"تشحنون", r"توصلون",
        r"delivery", r"shipping", r"ship to",
        r"البيع(?:ه|ة)",
    ]),
    ("payment_intent", [
        r"دفع", r"الدفع", r"تحويل", r"حوال", r"فلوس", r"باركود",
        r"مدى", r"visa", r"apple pay", r"تابي", r"تمارا",
        r"payment", r"transfer", r"bank",
    ]),
    ("order_intent", [
        r"طلب(?:ي|ات)?", r"تتبع", r"tracking", r"وين\s*طلب",
        r"status", r"حالة\s*الطلب", r"وصل\s*الطلب",
    ]),
    ("location_intent", [
        r"موقع(?:ي|ك|نا)?", r"الموقع", r"فرع", r"فروع", r"عنوان(?:ي|ك)?",
        r"خريط", r"maps", r"location", r"branch",
    ]),
    ("support_intent", [
        r"شكوى", r"مشكله", r"مشكلة", r"تأخير", r"ما\s*وصل",
        r"مو\s*وصل", r"complaint", r"support", r"خدمة\s*العملاء",
    ]),
]

_RECOMMENDATION_ASK_RE = re.compile(
    r"(?:"
    r"تنصحن|ترشح|تنصح|ترشد|وش\s+تنصح|ايش\s+تنصح|"
    r"افضل|أفضل|best|recommend|اقترح|اقتراح"
    r")",
    re.IGNORECASE | re.UNICODE,
)


@dataclass(frozen=True)
class SolutionSeekingMatch:
    """Outcome of :func:`classify_solution_seeking_commerce`."""

    axis: str
    confidence: float = SOLUTION_SEEKING_CONFIDENCE
    source: str = "attribute_pattern"  # attribute_pattern | structure

    @property
    def category(self) -> str:
        """Backward-compat alias used by legacy ``need_category`` slots."""
        return self.axis


def _norm_ar(text: str) -> str:
    t = unicodedata.normalize("NFKC", (text or "").strip().lower())
    t = re.sub(r"[\u064B-\u065F\u0640]", "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    t = re.sub(r"[؟?!.,؛:]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _has_commerce_context(norm: str) -> bool:
    return bool(_COMMERCE_CONTEXT_RE.search(norm))


def _has_attribute_outcome_signal(norm: str) -> Optional[str]:
    for axis, patterns in _AXIS_RULES:
        for pat in patterns:
            if re.search(pat, norm, re.IGNORECASE | re.UNICODE):
                return axis
    return None


def _matches_solution_structure(norm: str) -> bool:
    return any(
        re.search(pat, norm, re.IGNORECASE | re.UNICODE)
        for pat in _STRUCTURE_PATTERNS
    )


def classify_solution_seeking_commerce(message: str) -> Optional[SolutionSeekingMatch]:
    """Detect attribute/outcome-based commerce intent (all verticals)."""
    raw = (message or "").strip()
    if not raw or len(raw) > 800:
        return None

    norm = _norm_ar(raw)
    if not norm or len(norm) < 4:
        return None

    if _PRICE_ONLY_TAIL_RE.search(norm):
        return None

    # Very short bare lookups ("عسل سدر", "بكم") are SKU/price — not advisory.
    if len(norm.split()) <= 3 and not _has_attribute_outcome_signal(norm):
        return None

    axis = _has_attribute_outcome_signal(norm)
    if axis:
        if _has_commerce_context(norm) or _matches_solution_structure(norm):
            return SolutionSeekingMatch(axis=axis, source="attribute_pattern")
        # Strong health/outcome phrases without explicit product noun still count.
        if axis in {AXIS_HEALTH_DIET, AXIS_AUDIENCE, AXIS_PERFORMANCE}:
            return SolutionSeekingMatch(axis=axis, source="attribute_pattern")

    if _matches_solution_structure(norm) and _has_commerce_context(norm):
        return SolutionSeekingMatch(axis=AXIS_GENERAL, source="structure")

    return None


def detect_solution_seeking_suppression(
    message: str,
    *,
    skip_recent_topic: bool = False,
    state: Any = None,
    history: Optional[List[Any]] = None,
) -> Optional[str]:
    """
    Return suppression topic when message is clearly non-advisory commerce.

    Topics: ``delivery_intent``, ``payment_intent``, ``order_intent``,
    ``location_intent``, ``support_intent`` — or ``None``.
    """
    raw = (message or "").strip()
    if not raw:
        return None
    norm = _norm_ar(raw)
    if not norm:
        return None

    for topic, patterns in _NEGATIVE_GATE_RULES:
        for pat in patterns:
            if re.search(pat, norm, re.IGNORECASE | re.UNICODE):
                return topic

    return None


def contextual_non_product_clarification(message: str) -> Optional[str]:
    """Short non-product clarify for payment/support — avoid long handoffs."""
    norm = _norm_ar(message or "")
    if not norm:
        return None
    if re.search(r"فلوس|دفع|تحويل|باركود|مدى|visa|payment|transfer", norm):
        return "تقصد طريقة الدفع أو إثبات التحويل؟ أرسل لك التفاصيل."
    if re.search(r"شكو|مشكل|تأخير|ما\s*وصل|complaint|support", norm):
        return "تقصد متابعة طلب أو مشكلة في التوصيل؟ وضّح لي رقم الطلب أو المشكلة."
    if re.search(r"موقع|فرع|خريط|location|branch", norm):
        return "تقصد موقع الفرع أو عنوان التوصيل؟"
    return None


def customer_explicit_recommendation_request(message: str) -> bool:
    """True when customer explicitly asks for a recommendation."""
    return bool(_RECOMMENDATION_ASK_RE.search(_norm_ar(message or "")))


def should_suppress_repeat_need_clarification(
    state: Any,
    axis: str,
    question: str,
) -> bool:
    """Avoid repeating the same need-clarification within a short window."""
    try:
        from .fallback_guard import should_block_fallback_repeat  # noqa: PLC0415

        return should_block_fallback_repeat(state, question or "")
    except Exception:  # noqa: BLE001
        return False


def log_solution_seeking_suppressed(
    *,
    tenant_id: Any = None,
    reason: str = "",
    preview: str = "",
) -> None:
    try:
        logger.info(
            "[SOLUTION_SEEKING_SUPPRESSED] tenant=%s reason=%s preview=%r",
            tenant_id,
            reason or "-",
            (preview or "")[:80],
        )
    except Exception:  # noqa: BLE001
        pass


def log_intelligent_need_clarification_suppressed(
    *,
    tenant_id: Any = None,
    axis: str = "",
    reason: str = "",
    preview: str = "",
) -> None:
    try:
        logger.info(
            "[INTELLIGENT_NEED_CLARIFICATION_SUPPRESSED] tenant=%s axis=%s "
            "reason=%s preview=%r",
            tenant_id,
            axis or "-",
            reason or "-",
            (preview or "")[:80],
        )
    except Exception:  # noqa: BLE001
        pass


def apply_post_repair_suppression(
    intent: Any,
    message: str,
    *,
    state: Any = None,
    history: Optional[List[Any]] = None,
) -> Any:
    """
    Re-run negative gates on semantically repaired text.

    Demotes advisory commerce intent when repair reveals delivery/payment/etc.
    """
    from ..types import (  # noqa: PLC0415
        INTENT_ASK_LOCATION,
        INTENT_ASK_PAYMENT_INFO,
        INTENT_ASK_SHIPPING,
        INTENT_NEED_BASED_PRODUCT_ADVICE,
        INTENT_TALK_HUMAN,
        INTENT_TRACK_ORDER,
        Intent,
    )

    topic = detect_solution_seeking_suppression(
        message or "",
        skip_recent_topic=True,
    )
    if not topic:
        try:
            from .fallback_guard import resolve_active_topic, stamp_recent_topic  # noqa: PLC0415

            topic = resolve_active_topic(message or "", state, history)
            if topic and state is not None:
                stamp_recent_topic(state, topic)
        except Exception:  # noqa: BLE001
            pass

    if not topic:
        return intent

    if intent.name not in {
        INTENT_NEED_BASED_PRODUCT_ADVICE,
        "need_based_product_advice",
        "solution_seeking_commerce",
        "general",
    }:
        return intent

    _map = {
        "delivery_intent": INTENT_ASK_SHIPPING,
        "payment_intent": INTENT_ASK_PAYMENT_INFO,
        "order_intent": INTENT_TRACK_ORDER,
        "location_intent": INTENT_ASK_LOCATION,
        "support_intent": INTENT_TALK_HUMAN,
    }
    mapped = _map.get(topic)
    if not mapped:
        return intent

    log_solution_seeking_suppressed(
        reason=f"post_repair_{topic}",
        preview=message or "",
    )
    return Intent(
        name=mapped,
        confidence=max(float(getattr(intent, "confidence", 0.5) or 0.5), 0.92),
        raw_message=getattr(intent, "raw_message", "") or message,
        slots=dict(getattr(intent, "slots", None) or {}),
    )


def intelligent_need_clarification(axis: str) -> str:
    """Customer-facing clarification about the *need*, not SKU name."""
    _templates = {
        AXIS_HEALTH_DIET: (
            "تقصد منتج مناسب لمرضى السكر أو بدون سكر؟ "
            "أقدر أوضح لك الأنسب عندنا، ومع الحالات الصحية الأفضل المتابعة مع الطبيب."
        ),
        AXIS_AUDIENCE: (
            "تقصد منتج مناسب للأطفال أو لفئة عمر معيّنة؟ "
            "وضّح لي العمر أو الاستخدام وأرشّح لك الأنسب."
        ),
        AXIS_FORMALITY: (
            "تقصد شي رسمي أو لمناسبة معيّنة؟ "
            "وضّح الاستخدام (عمل، مناسبة، …) وأقترح عليك الأنسب."
        ),
        AXIS_SEASON: (
            "تقصد شي مناسب لفصل أو جو معيّن؟ "
            "قلّي الصيف أو الشتاء أو الاستخدام وأوجّهك."
        ),
        AXIS_SIZE_FIT: (
            "تقصد مقاس أو قصة معيّنة (واسع، مقاس أطفال، …)؟ "
            "وضّح المقاس أو الشكل المطلوب."
        ),
        AXIS_PERFORMANCE: (
            "تقصد مواصفات أداء معيّنة (بطارية، مونتاج، تصوير ليلي، …)؟ "
            "وضّح الاستخدام الأساسي وأرشّح لك الأنسب."
        ),
        AXIS_DURABILITY: (
            "تقصد ثبات أو جودة معيّنة في الاستخدام؟ "
            "وضّح الاستخدام اليومي وأقترح عليك."
        ),
    }
    return _templates.get(
        axis,
        "تقصد حاجة أو مواصفة معيّنة؟ وضّح الاستخدام أو الصفة المطلوبة "
        "وأرشّح لك الأنسب من عندنا — بدون ما تحتاج تكتب اسم منتج.",
    )


def log_solution_seeking_commerce(
    *,
    tenant_id: Any = None,
    axis: str = "",
    source: str = "",
    route: str = "",
    preview: str = "",
) -> None:
    """Structured line for prod monitoring — grep ``[SOLUTION_SEEKING_COMMERCE]``."""
    try:
        logger.info(
            "[SOLUTION_SEEKING_COMMERCE] tenant=%s axis=%s source=%s route=%s preview=%r",
            tenant_id,
            axis or "-",
            source or "-",
            route or "-",
            (preview or "")[:80],
        )
    except Exception:  # noqa: BLE001
        pass


def log_intelligent_need_clarification(
    *,
    tenant_id: Any = None,
    axis: str = "",
    reason: str = "",
    preview: str = "",
) -> None:
    """Structured line — grep ``[INTELLIGENT_NEED_CLARIFICATION]``."""
    try:
        logger.info(
            "[INTELLIGENT_NEED_CLARIFICATION] tenant=%s axis=%s reason=%s preview=%r",
            tenant_id,
            axis or "-",
            reason or "-",
            (preview or "")[:80],
        )
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "AXIS_AUDIENCE",
    "AXIS_DURABILITY",
    "AXIS_FORMALITY",
    "AXIS_GENERAL",
    "AXIS_HEALTH_DIET",
    "AXIS_PERFORMANCE",
    "AXIS_SEASON",
    "AXIS_SIZE_FIT",
    "SOLUTION_SEEKING_CONFIDENCE",
    "SolutionSeekingMatch",
    "apply_post_repair_suppression",
    "classify_solution_seeking_commerce",
    "contextual_non_product_clarification",
    "customer_explicit_recommendation_request",
    "detect_solution_seeking_suppression",
    "intelligent_need_clarification",
    "log_intelligent_need_clarification",
    "log_intelligent_need_clarification_suppressed",
    "log_solution_seeking_commerce",
    "log_solution_seeking_suppressed",
    "should_suppress_repeat_need_clarification",
]
