"""
backend/modules/ai/knowledge/improvement_advisor.py
───────────────────────────────────────────────────
Knowledge Improvement Suggestions (KB-Improve V1, May 2026 #24).

This module powers the "اقتراحات تحسين من نحلة ✨" button in the Smart
Store Knowledge Hub. It generates AT MOST 5 preview-only suggestions
that the merchant can either approve (→ creates a draft via the
existing approval flow) or dismiss.

V1 design notes
───────────────
* **Two layers, both optional**:
    1. ``audit()`` — deterministic auditor. Pure-Python, no LLM call.
       Produces ``ImprovementFinding`` rows from the current knowledge
       base rows + the tenant's catalog snapshot.
    2. ``polish_with_gpt()`` — optional Arabic copy refinement.
       Skipped silently when ``OPENAI_API_KEY`` is unset (so local +
       CI runs return clean deterministic copy).
* **No embeddings, no vector store, no retrieval rewrite.** Every
  signal here comes from cheap pattern matching on titles + bodies.
* **Never auto-applies.** Every suggestion ships with a ``proposed_body``
  the merchant can edit before approving — the actual write goes
  through ``MerchantKnowledgeDraft`` so the existing platform-conflict
  guard + per-op approval still apply.
* **Cap at 5 suggestions per call** so the dashboard list stays
  actionable. Findings are ranked by severity, expected impact, and
  confidence before truncation.
* **Platform-aware**: when the tenant is connected to Salla / Zid /
  Shopify, the advisor refuses to suggest knowledge facts that would
  overlap with the platform's authoritative columns (price, stock,
  product titles).

Why a separate module instead of extending ``repair_advisor.py``:
* ``repair_advisor`` operates on the EXISTING rows — its output is
  "move this row from kind A to kind B". It targets structural drift.
* ``improvement_advisor`` operates on what's MISSING or WEAK — its
  output is "you don't have a payment policy yet; here's a draft to
  approve". It targets coverage + quality.
The two are complementary and intentionally decoupled.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("nahla.ai.knowledge.improvement_advisor")


# ── Knobs ───────────────────────────────────────────────────────────────────


# Hard ceiling per spec; ranking + severity-weighted truncation runs
# inside ``audit()`` before this is enforced.
_MAX_SUGGESTIONS = 5

# Bodies shorter than this in a real-world commerce kind look "weak"
# (a one-line sentence rarely answers a customer well enough to keep
# Claude off escalation). 80 chars ≈ 12-15 short Arabic words.
_WEAK_BODY_THRESHOLD_CHARS = 80

# Jaccard token similarity used for duplicate suggestion. Slightly
# lower than the repair_advisor threshold (0.6) because we're hinting
# the merchant to review — not auto-flagging contamination.
_DUPLICATE_SIMILARITY = 0.55

# Default suppression TTL (per spec point 1: "لمدة 7 أيام أو حتى يتغير
# الـ KB فعلياً"). The KB-change reset is implicit — when a merchant
# fills the gap the suggestion targeted, the auditor stops generating
# it anyway, so we don't need to track KB hashes here.
SUPPRESSION_TTL_DAYS = 7

# Hard floor on confidence. Currently a no-op (the lowest finding emits
# 0.6) but locks the bottom so future advisor passes can't sneak
# low-quality suggestions through. The ranker still sorts secondaries
# by confidence on top of this floor.
_DEFAULT_MIN_CONFIDENCE = 0.5


# ── Severity / impact taxonomy ──────────────────────────────────────────────


_SEVERITY_RANK: Dict[str, int] = {"high": 3, "medium": 2, "low": 1}


def _severity_score(sev: str) -> int:
    return _SEVERITY_RANK.get((sev or "").strip().lower(), 0)


# ── Data shapes ─────────────────────────────────────────────────────────────


@dataclass
class CatalogSlice:
    """Just the catalog columns the auditor needs.

    Kept slim so unit tests can build a list without dragging the full
    Product ORM model in. ``has_description``/``has_image`` are
    pre-computed by the caller so this stays JSON-friendly.
    """
    id: int
    title: str
    has_description: bool = False
    has_image: bool = False


_FP_NORMALIZE_RE = re.compile(r"[\s\u00a0\u200f\u200e]+", re.UNICODE)


def _normalize_for_fingerprint(text: str) -> str:
    """Normalize a string before hashing it into a fingerprint.

    We strip:
      * Unicode bidi marks and zero-width chars (so "أضف سياسة دفع‎"
        and "أضف سياسة دفع" produce the same fp).
      * Repeated whitespace.
      * Leading/trailing whitespace.
      * Case (lower) for the Latin tail of the string — Arabic has no
        case, so this is a no-op for the dominant token in our titles.

    The normalization is intentionally conservative: stronger forms
    (Arabic letter normalization — أ→ا, ة→ه, etc.) would collapse
    too aggressively and let semantically-different suggestions share
    a fingerprint. The current scheme is enough to ride out the small
    cosmetic edits the GPT polisher might apply ("أضف **سياسة دفع**
    واضحة" vs "أضف سياسة دفع واضحة").
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _FP_NORMALIZE_RE.sub(" ", s).strip().lower()
    return s


def compute_fingerprint(
    *,
    type_: str,
    target_kind: str,
    title: str,
    related_section_ids: Sequence[int] = (),
) -> str:
    """Stable 16-char hash identifying a suggestion across re-runs.

    Inputs:
      * ``type_``               — e.g. ``"missing_required_knowledge"``.
      * ``target_kind``         — e.g. ``"payment_method"``.
      * ``title``               — normalized via ``_normalize_for_fingerprint``.
      * ``related_section_ids`` — sorted before hashing so two findings
        about sections (50, 51) and (51, 50) collide as intended.

    A finding whose title changes cosmetically (polish, whitespace,
    bidi marks) keeps its fingerprint — that's what lets the
    suppression/applied filter survive a polish pass.
    """
    parts = [
        (type_ or "").strip().lower(),
        (target_kind or "").strip().lower(),
        _normalize_for_fingerprint(title or ""),
        ",".join(str(int(s)) for s in sorted(int(x) for x in related_section_ids or [])),
    ]
    raw = "|".join(parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


@dataclass
class ImprovementFinding:
    """Output of ``audit()`` — one structured suggestion.

    ``rationale_keys`` is an internal list of stable markers used by
    tests and by the GPT polisher to know which finding it's looking
    at (e.g. ``["missing:payment_method"]``). It never reaches the
    dashboard.

    ``fingerprint`` (KB-Improve V1.1) is a 16-char hash that survives
    cosmetic polish edits and is the key both for ``reject`` (stored
    in ``TenantSettings.ai_settings.kb_improvement_state.dismissed``)
    and ``approve`` (stored on the resulting ``MerchantKnowledgeDraft``
    via ``proposal_json.suggestion_fingerprint``) suppression. Computed
    lazily in ``__post_init__`` so callers don't have to remember to
    set it.
    """
    id: str
    type: str
    severity: str
    title: str
    reason: str
    expected_impact: str
    target_kind: str
    proposed_body: str
    requires_media: bool
    confidence: float
    related_section_ids: List[int] = field(default_factory=list)
    rationale_keys: List[str] = field(default_factory=list)
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = compute_fingerprint(
                type_=self.type,
                target_kind=self.target_kind,
                title=self.title,
                related_section_ids=self.related_section_ids,
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "severity": self.severity,
            "title": self.title,
            "reason": self.reason,
            "expected_impact": self.expected_impact,
            "target_kind": self.target_kind,
            "proposed_body": self.proposed_body,
            "requires_media": self.requires_media,
            "confidence": round(float(self.confidence), 2),
            "related_section_ids": list(self.related_section_ids),
            "fingerprint": self.fingerprint,
        }


# ── Pattern libraries ───────────────────────────────────────────────────────
#
# Same shape as ``repair_advisor`` but the lists are oriented toward
# DETECTING the topic, not classifying it. We keep them inline so the
# module stays a single importable file.


_BEHAVIORAL_PATTERNS: List[re.Pattern[str]] = [
    re.compile(p) for p in (
        r"\bلا\s+تقل\b",
        r"\bممنوع\s+(?:تقول|الرد|كلمة)\b",
        r"\bتجن[ّب]?\s+(?:كلمة|قول)\b",
        r"\b(?:استخدم|اعتمد)\s+لهجة\b",
        r"\bنبرة\s+الرد\b",
        r"\bأسلوب\s+الرد\b",
        r"\b(?:حبيبي|قلبي|يا\s+غالي)\b",
        r"\bحوّل\s+(?:ل|إلى)\s*(?:موظف|بشري|الفريق)\b",
        r"\bإيموجي\b",
    )
]

_COMPLIANCE_RISK_PATTERNS: List[re.Pattern[str]] = [
    re.compile(p) for p in (
        # Medicinal claims we don't want stamped into the KB.
        r"\b(?:يعالج|يشفي|علاج|دواء)\b",
        r"\b(?:يقضي\s+على)\b",
        r"\bمضمون\s+الشفاء\b",
        r"\b100%\s*(?:شفاء|علاج)\b",
    )
]

_BANK_TRANSFER_KEYWORDS = (
    "تحويل", "آيبان", "iban", "الراجحي", "الأهلي", "بنك",
)

_LOCATION_KEYWORDS = (
    "فرع", "موقع", "عنوان", "خريطة", "محلنا", "في الرياض", "في جدة",
)


def _has_any(text: str, keywords: Sequence[str]) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(k in low for k in keywords)


def _has_behavioral_signal(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _BEHAVIORAL_PATTERNS)


def _has_compliance_risk(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _COMPLIANCE_RISK_PATTERNS)


_TOKEN_RE = re.compile(r"[\w\u0600-\u06ff]+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    if not text:
        return set()
    return {tok for tok in _TOKEN_RE.findall(text.lower()) if len(tok) >= 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return (inter / union) if union else 0.0


# ── Auditor (Layer 1) ───────────────────────────────────────────────────────


@dataclass
class _RowView:
    """Minimal view of a section row for the auditor.

    KB-Improve V1.2 (May 2026 #29): we now also expose the
    ``media_keys`` / ``media_roles`` / ``media_titles`` triples on
    every link. The missing-barcode pass needs to know whether ANY
    section in the same kind/group already has a barcode-shaped
    media attached (role='barcode', media_key in the registry
    payment family, or a title that mentions a barcode/QR). Without
    this we were emitting 5 identical "أضف باركود" suggestions for
    5 duplicate bank_transfer rows even when one of the sibling
    rows already carried the barcode.
    """
    id: int
    kind: str
    title: str
    body: str
    is_active: bool
    media_count: int
    # Per-link metadata, aligned by index. ``len(media_keys) ==
    # len(media_roles) == len(media_titles)`` and equals the number
    # of *active* media links. Inactive media are dropped earlier
    # so they don't accidentally satisfy the barcode check.
    media_keys: List[str] = field(default_factory=list)
    media_roles: List[str] = field(default_factory=list)
    media_titles: List[str] = field(default_factory=list)


def _row_view(r: Any) -> _RowView:
    keys: List[str] = []
    roles: List[str] = []
    titles: List[str] = []
    for lk in (getattr(r, "media_links", None) or []):
        media = getattr(lk, "media", None)
        if media is None:
            continue
        if not bool(getattr(media, "is_active", True)):
            continue
        keys.append((getattr(media, "media_key", None) or "").strip().lower())
        roles.append((getattr(lk, "link_role", None) or "").strip().lower())
        titles.append((getattr(media, "title", None) or "").strip().lower())
    return _RowView(
        id=int(getattr(r, "id", 0) or 0),
        kind=(getattr(r, "kind", "") or "").strip().lower(),
        title=(getattr(r, "title", None) or "").strip(),
        body=(getattr(r, "body", None) or "").strip(),
        is_active=bool(getattr(r, "is_active", True)),
        media_count=len(keys),
        media_keys=keys,
        media_roles=roles,
        media_titles=titles,
    )


# Substrings (already lowercased) that mark a media attachment as
# "this is the payment barcode the merchant wants the AI to send".
# Checked against ``media_key`` / ``link_role`` / ``title``. We
# stay conservative — the false-positive cost is "we don't suggest
# adding a barcode", which is fine (worst case the merchant misses
# a hint; never wrong information).
_BARCODE_MEDIA_KEY_HINTS: Tuple[str, ...] = (
    "barcode", "_qr", "qr_",
    "rajhi", "alahli", "ahli", "stcpay", "mobilypay", "barq",
)
_BARCODE_TITLE_HINTS: Tuple[str, ...] = (
    "barcode", "qr", "باركود", "بار كود", "كيوار", "كيو ار",
    "رمز الدفع", "رمز التحويل", "رمز السداد",
    "راجحي", "اهلي", "أهلي", "stc", "موبايلي",
)


def _link_looks_like_barcode(
    media_key: str, link_role: str, media_title: str,
) -> bool:
    """One link is a 'barcode' if the role/key/title hints agree.

    Order matters slightly for clarity but not correctness — any
    one of the three signals is enough. We accept ``link_role=='barcode'``
    on its own because that's the merchant's explicit declaration in
    the dashboard.
    """
    if (link_role or "").strip().lower() == "barcode":
        return True
    k = (media_key or "").strip().lower()
    if k:
        if k.startswith("payment_"):
            return True
        if any(h in k for h in _BARCODE_MEDIA_KEY_HINTS):
            return True
    t = (media_title or "").strip().lower()
    if t and any(h in t for h in _BARCODE_TITLE_HINTS):
        return True
    return False


def _row_has_barcode_media(row: _RowView) -> bool:
    """True iff at least one of this row's active media links looks
    like the payment barcode the AI would attach for a transfer."""
    for k, role, title in zip(row.media_keys, row.media_roles, row.media_titles):
        if _link_looks_like_barcode(k, role, title):
            return True
    return False


# ── Suggestion factory helpers ──────────────────────────────────────────────
#
# Each ``_suggest_*`` helper returns an ``ImprovementFinding`` whose
# ``proposed_body`` is intentionally generic — the merchant can edit
# before approving. We DELIBERATELY do not invent merchant-specific
# numbers (prices, opening times, branches) — instead the body asks the
# merchant to fill the slot, matching the user's rule:
# "إذا لا توجد معلومة كافية، اقترح اسأل التاجر لإكمالها وليس نصاً مؤلفاً".


def _suggest_missing_payment(idx: int) -> ImprovementFinding:
    return ImprovementFinding(
        id=f"sug-{idx}",
        type="missing_required_knowledge",
        severity="high",
        title="أضف سياسة دفع واضحة",
        reason="لم نجد قسماً واضحاً يشرح طرق الدفع المتاحة في متجرك.",
        expected_impact=(
            "يساعد العملاء على معرفة طرق الدفع المتاحة بسرعة، ويقلل "
            "تكرار سؤال «كيف أدفع؟» وتحويل المحادثة لموظف بشري."
        ),
        target_kind="payment_method",
        proposed_body=(
            "نوفر عدة طرق للدفع: [أضف الطرق المتاحة في متجرك — مثل مدى، "
            "فيزا، التحويل البنكي، Apple Pay، تابي، تمارا]. إذا رغبت "
            "بالتحويل البنكي، يمكننا إرسال بيانات التحويل عند الطلب."
        ),
        requires_media=False,
        confidence=0.9,
        rationale_keys=["missing:payment_method"],
    )


def _suggest_missing_shipping(idx: int) -> ImprovementFinding:
    return ImprovementFinding(
        id=f"sug-{idx}",
        type="missing_required_knowledge",
        severity="high",
        title="أضف سياسة شحن واضحة",
        reason=(
            "لم نجد قسماً يوضح شركات الشحن، المناطق المغطاة، أو مدة "
            "التوصيل."
        ),
        expected_impact=(
            "سؤال «كم تستغرق الشحنة؟» من أكثر الأسئلة المتكررة. وجود "
            "سياسة شحن واضحة يقلل التواصل لموظف بشري ويزيد ثقة العميل."
        ),
        target_kind="shipping_zones",
        proposed_body=(
            "نشحن إلى [أضف المناطق التي تخدمها — مثلاً جميع مدن المملكة] "
            "عبر [أضف شركات الشحن المعتمدة — مثل سمسا، أرامكس]. مدة "
            "التوصيل المتوقعة [أضف المدة — مثلاً 2 إلى 4 أيام عمل]."
        ),
        requires_media=False,
        confidence=0.9,
        rationale_keys=["missing:shipping"],
    )


def _suggest_missing_return(idx: int) -> ImprovementFinding:
    return ImprovementFinding(
        id=f"sug-{idx}",
        type="missing_required_knowledge",
        severity="high",
        title="أضف سياسة استرجاع وضمان",
        reason="لا توجد سياسة استرجاع/استبدال موثقة في قاعدة المعرفة.",
        expected_impact=(
            "العميل يطمئن قبل الشراء، وتقل النزاعات بعد البيع. كما أنها "
            "إلزامية بنظام التجارة الإلكترونية في السعودية."
        ),
        target_kind="return_policy",
        proposed_body=(
            "نقبل الاسترجاع/الاستبدال خلال [أضف المدة — مثلاً 14 يوماً] "
            "من تاريخ الاستلام إذا كان المنتج بحالته الأصلية. للتقديم، "
            "تواصل معنا عبر الواتساب وسنوضح الخطوات."
        ),
        requires_media=False,
        confidence=0.85,
        rationale_keys=["missing:return"],
    )


def _suggest_missing_working_hours(idx: int) -> ImprovementFinding:
    return ImprovementFinding(
        id=f"sug-{idx}",
        type="missing_required_knowledge",
        severity="medium",
        title="أضف أوقات العمل",
        reason="لا توجد أوقات عمل واضحة، والذكاء يصعب عليه إخبار العميل متى يصل الرد البشري.",
        expected_impact="رد أدق على «هل أنتم متاحون الآن؟» وتقليل الإحباط عند تأخر الرد.",
        target_kind="working_hours",
        proposed_body=(
            "أوقات العمل: [أضف الأيام والساعات، مثلاً السبت إلى الخميس "
            "من 9 صباحاً إلى 9 مساءً]. خارج هذه الأوقات نرد عبر "
            "الذكاء، ويتم تحويل الرسائل المهمة لموظف بشري في أول يوم عمل."
        ),
        requires_media=False,
        confidence=0.8,
        rationale_keys=["missing:working_hours"],
    )


def _suggest_missing_escalation(idx: int) -> ImprovementFinding:
    return ImprovementFinding(
        id=f"sug-{idx}",
        type="behavior_tone",
        severity="medium",
        title="أضف قاعدة التحويل لموظف بشري",
        reason=(
            "الذكاء لا يعرف متى يحوّل المحادثة لموظف — قد يتأخر في "
            "تحويل شكوى أو يحوّل سؤالاً بسيطاً بسبب غياب القاعدة."
        ),
        expected_impact=(
            "تجربة عميل أوضح: الذكاء يردّ على الشائع، ويحوّل بسرعة للحالات "
            "التي تستحق تدخل بشري (شكاوى، طلبات خاصة، تأخر شحنة)."
        ),
        target_kind="escalation_rules",
        proposed_body=(
            "حوّل المحادثة لموظف بشري عندما: (أ) العميل يقدّم شكوى، "
            "(ب) يطلب خصماً غير مدرج، (ج) يستفسر عن طلب متأخر أكثر من "
            "[أضف المدة]، (د) يطلب صراحةً التحدث مع موظف. خلاف ذلك، "
            "أكمل الرد بنفسك بأسلوب ودي."
        ),
        requires_media=False,
        confidence=0.75,
        rationale_keys=["missing:escalation"],
    )


def _suggest_missing_response_tone(idx: int) -> ImprovementFinding:
    return ImprovementFinding(
        id=f"sug-{idx}",
        type="behavior_tone",
        severity="medium",
        title="حدّد نبرة الرد المطلوبة",
        reason="لا توجد قاعدة واضحة للهجة أو أسلوب الرد — يستخدم الذكاء النبرة الافتراضية للمنصة.",
        expected_impact="ردود متسقة بنبرة متجرك بدل أن تختلف من رسالة لأخرى.",
        target_kind="response_tone",
        proposed_body=(
            "يردّ المساعد بلهجة [أضف اللهجة — سعودي عام / خليجي / فصحى] "
            "بأسلوب [ودي/رسمي/مختصر]. يحافظ على الاحترام، ويستخدم "
            "أمثلة قصيرة من حياة العميل عند الحاجة."
        ),
        requires_media=False,
        confidence=0.7,
        rationale_keys=["missing:response_tone"],
    )


def _suggest_missing_forbidden_phrases(idx: int) -> ImprovementFinding:
    return ImprovementFinding(
        id=f"sug-{idx}",
        type="behavior_tone",
        severity="low",
        title="حدّد قائمة كلمات ممنوعة في الرد",
        reason="لا توجد قائمة كلمات ممنوعة — قد يستخدم الذكاء عبارات مبالغة أو غير مناسبة لجمهورك.",
        expected_impact="رد أكثر احترافية ومطابقة لهوية متجرك.",
        target_kind="forbidden_phrases",
        proposed_body=(
            "لا يستخدم المساعد الكلمات التالية: [أضف القائمة — مثلاً "
            "«حبيبي»، «قلبي»، «يا غالي»، أو أي تعبير دلال مفرط]. "
            "النبرة تبقى محترمة ومحايدة."
        ),
        requires_media=False,
        confidence=0.6,
        rationale_keys=["missing:forbidden_phrases"],
    )


def _suggest_bank_transfer_needs_barcode(
    idx: int, row: _RowView,
) -> ImprovementFinding:
    return ImprovementFinding(
        id=f"sug-{idx}",
        type="missing_media",
        severity="high",
        title="أرفق باركود/صورة للتحويل البنكي",
        reason=(
            "قسم التحويل البنكي موجود لكن بدون صورة باركود أو لقطة "
            "للحساب — الذكاء يضطر لإرسال نص فقط بدلاً من صورة جاهزة."
        ),
        expected_impact=(
            "تجربة دفع أسرع للعميل: يفتح صورة الباركود ويحوّل مباشرةً "
            "من تطبيق البنك بدلاً من كتابة الآيبان يدوياً."
        ),
        target_kind="bank_transfer",
        proposed_body=(
            "نوفر التحويل البنكي عبر باركود الراجحي/الأهلي. عند الطلب "
            "نرسل صورة الباركود مباشرة لتسديد المبلغ من تطبيق البنك."
        ),
        requires_media=True,
        confidence=0.85,
        related_section_ids=[row.id],
        rationale_keys=[
            "missing_media:bank_transfer",
            "purpose:add_payment_barcode",
            f"section:{row.id}",
        ],
    )


def _suggest_bank_transfer_merge_and_barcode(
    idx: int, rows: List[_RowView],
) -> ImprovementFinding:
    """Single suggestion that replaces the per-row barcode add when
    multiple bank_transfer rows exist without a barcode.

    Spec (KB-Improve V1.2, May 2026 #29 problem 2):
    "إذا كانت عدة أقسام bank_transfer مكررة، لا تعطِ 5 اقتراحات
    media، بل أعطِ اقتراح واحد: «يوجد تكرار في أقسام التحويل
    البنكي، نقترح دمجها وربط الباركود بالقسم الموحد»."
    """
    ids = sorted({int(r.id) for r in rows})
    return ImprovementFinding(
        id=f"sug-{idx}",
        type="duplicate_merge",
        severity="high",
        title="ادمج أقسام التحويل البنكي المكررة وأرفق باركوداً واحداً",
        reason=(
            f"يوجد {len(ids)} أقسام للتحويل البنكي بدون باركود مرتبط. "
            "تعدد الأقسام يربك الذكاء عند الاستحضار، ويضاعف فرصة أن "
            "يجيب بنص بدل إرسال صورة الباركود."
        ),
        expected_impact=(
            "قسم تحويل بنكي واحد موحّد مع باركود مرتبط واحد = الذكاء "
            "يرسل الصورة مباشرة عند سؤال العميل «كيف أحوّل لكم؟» "
            "بدل سرد الخطوات نصياً."
        ),
        target_kind="bank_transfer",
        proposed_body=(
            "احتفظ بقسم تحويل بنكي واحد فقط، وادمج تفاصيل الأقسام "
            "الأخرى بداخله، ثم اربط صورة الباركود (الراجحي/الأهلي/إلخ) "
            "بهذا القسم الموحد. الذكاء يفضّل المصدر الواحد الواضح."
        ),
        requires_media=True,
        confidence=0.85,
        related_section_ids=ids,
        rationale_keys=[
            "missing_media:bank_transfer",
            "purpose:add_payment_barcode",
            "duplicate:bank_transfer",
        ] + [f"section:{i}" for i in ids],
    )


def _suggest_weak_section(idx: int, row: _RowView) -> ImprovementFinding:
    return ImprovementFinding(
        id=f"sug-{idx}",
        type="weak_section",
        severity="medium",
        title=f"حسّن قسم «{row.title or row.kind}»",
        reason=(
            f"القسم الحالي قصير جداً ({len(row.body)} حرف) ولا يكفي الذكاء "
            "للإجابة على تفاصيل العميل."
        ),
        expected_impact=(
            "ردود أكثر اكتمالاً، وأقل احتمالاً لطلب الذكاء توضيحاً من "
            "العميل أو تحويله لموظف بشري."
        ),
        target_kind=row.kind,
        proposed_body=(
            f"{row.body}\n\nأضف هنا تفاصيل أكثر: [مثل المدة الزمنية، "
            "الشروط، الاستثناءات، المدن المغطاة، أو أي معلومة يكررها "
            "العملاء كثيراً]."
        ),
        requires_media=False,
        confidence=0.65,
        related_section_ids=[row.id],
        rationale_keys=["weak:short_body", f"section:{row.id}"],
    )


def _suggest_contamination(idx: int, row: _RowView) -> ImprovementFinding:
    return ImprovementFinding(
        id=f"sug-{idx}",
        type="semantic_contamination",
        severity="high",
        title=f"انقل القواعد السلوكية من قسم «{row.title or row.kind}»",
        reason=(
            "هذا القسم يحتوي قواعد أسلوب/سلوك للمساعد (مثل ألفاظ ممنوعة "
            "أو نبرة) لكنه مصنّف ضمن المعرفة التجارية. الذكاء يستحضر "
            "هذا القسم عند سؤال العميل عن الدفع/الشحن فيستحضر معه "
            "القواعد السلوكية في غير محلها."
        ),
        expected_impact=(
            "ردود تجارية أنظف، وقواعد سلوكية تطبق على كل المحادثات وليس "
            "فقط في السياق الذي وضعت فيه بالخطأ."
        ),
        target_kind="forbidden_phrases",
        proposed_body=(
            "انقل الجزء السلوكي من نص القسم الحالي إلى قسم جديد في "
            "«سلوك المساعد»، واترك المعلومات التجارية فقط في القسم الأصلي."
        ),
        requires_media=False,
        confidence=0.8,
        related_section_ids=[row.id],
        rationale_keys=["contamination:behavior_in_commerce", f"section:{row.id}"],
    )


def _suggest_duplicate(
    idx: int, a: _RowView, b: _RowView, similarity: float,
) -> ImprovementFinding:
    return ImprovementFinding(
        id=f"sug-{idx}",
        type="duplicate_merge",
        severity="low",
        title=f"ادمج قسمين متشابهين من نوع «{a.kind}»",
        reason=(
            f"يوجد قسمان من نفس النوع تشابه نصيهما بنسبة {round(similarity * 100)}%."
        ),
        expected_impact=(
            "قاعدة معرفة أنظف، وذكاء يستحضر المعلومة من مصدر واحد بدلاً "
            "من سحب نسختين مكررتين في نفس الرد."
        ),
        target_kind=a.kind,
        proposed_body=(
            "احتفظ بالقسم الأشمل واحذف/ادمج الآخر. تأكد من ضم أي تفاصيل "
            "فريدة من القسم المحذوف إلى القسم المُحتفظ به."
        ),
        requires_media=False,
        confidence=0.6,
        related_section_ids=[a.id, b.id],
        rationale_keys=["duplicate:same_kind", f"section:{a.id}", f"section:{b.id}"],
    )


def _suggest_compliance_risk(idx: int, row: _RowView) -> ImprovementFinding:
    return ImprovementFinding(
        id=f"sug-{idx}",
        type="compliance",
        severity="high",
        title=f"راجع صياغة قسم «{row.title or row.kind}» — قد تبدو ادعاء علاجي",
        reason=(
            "النص يحتوي عبارات قد تُفهم كادعاءات علاجية مباشرة (يعالج/"
            "يشفي/يقضي على...). هذه صياغة مخالفة لقواعد المنصات "
            "الإعلانية وقد تعرّض المتجر لعقوبات."
        ),
        expected_impact=(
            "صياغة آمنة قانونياً + ردود يثق بها العميل أكثر لأنها لا "
            "تَعِد بما لا يمكن تقديمه."
        ),
        target_kind="compliance_rules",
        proposed_body=(
            "اعتمد صياغات عامة مثل «يدخل في الأنظمة الغذائية الصحية» أو "
            "«تستخدمه كثير من العائلات في وجبات الإفطار». تجنّب: «يعالج»، "
            "«يشفي»، «يقضي على»، أو أي وعد طبي مباشر."
        ),
        requires_media=False,
        confidence=0.75,
        related_section_ids=[row.id],
        rationale_keys=["compliance:medicinal_claim", f"section:{row.id}"],
    )


def _suggest_product_usage_gap(
    idx: int, products_without_usage: List[CatalogSlice],
) -> ImprovementFinding:
    sample = ", ".join(p.title for p in products_without_usage[:3])
    return ImprovementFinding(
        id=f"sug-{idx}",
        type="product_knowledge_gap",
        severity="medium",
        title="أضف طريقة استخدام لمنتجاتك",
        reason=(
            f"لديك {len(products_without_usage)} منتج بدون قسم «طريقة "
            f"الاستخدام» (مثلاً: {sample})."
        ),
        expected_impact=(
            "العميل يفهم كيف يستخدم المنتج → معدّل إرضاء أعلى وعودات أقل."
        ),
        target_kind="product_usage",
        proposed_body=(
            "طريقة الاستخدام: [أضف الجرعة/الكمية، التوقيت، المدة، الفئة "
            "المستهدفة، وأي تحذيرات]. ربط هذا القسم بالمنتج المعني من "
            "قائمة المنتجات."
        ),
        requires_media=False,
        confidence=0.6,
        rationale_keys=["product_gap:usage"],
    )


# ── Auditor passes ──────────────────────────────────────────────────────────


def _pass_missing_required(
    rows: List[_RowView], idx_start: int,
) -> List[ImprovementFinding]:
    """Detect missing payment / shipping / return / hours sections."""
    by_kind: Dict[str, List[_RowView]] = {}
    for r in rows:
        by_kind.setdefault(r.kind, []).append(r)

    out: List[ImprovementFinding] = []
    idx = idx_start

    has_payment = bool(by_kind.get("payment_method") or by_kind.get("bank_transfer")
                       or by_kind.get("cod"))
    if not has_payment:
        out.append(_suggest_missing_payment(idx)); idx += 1

    has_shipping = bool(by_kind.get("shipping_zones")
                        or by_kind.get("shipping_carrier"))
    if not has_shipping:
        out.append(_suggest_missing_shipping(idx)); idx += 1

    if not by_kind.get("return_policy"):
        out.append(_suggest_missing_return(idx)); idx += 1

    if not by_kind.get("working_hours"):
        out.append(_suggest_missing_working_hours(idx)); idx += 1

    return out


def _pass_behavior_tone(
    rows: List[_RowView], idx_start: int,
) -> List[ImprovementFinding]:
    """Detect missing behavioral knowledge (escalation, tone, forbidden)."""
    by_kind = {r.kind for r in rows}
    out: List[ImprovementFinding] = []
    idx = idx_start

    if "escalation_rules" not in by_kind:
        out.append(_suggest_missing_escalation(idx)); idx += 1
    if "response_tone" not in by_kind:
        out.append(_suggest_missing_response_tone(idx)); idx += 1
    if "forbidden_phrases" not in by_kind:
        out.append(_suggest_missing_forbidden_phrases(idx)); idx += 1

    return out


def _pass_weak_sections(
    rows: List[_RowView], idx_start: int,
) -> List[ImprovementFinding]:
    """Flag commerce sections with very short bodies.

    We only consider COMMERCE rows here (group 2..5 ish) because a
    behavioral row like "لا تقل حبيبي" is allowed to be short.
    """
    try:
        from services.knowledge_section_kinds import (  # noqa: PLC0415
            BEHAVIORAL_KINDS,
        )
    except Exception:
        BEHAVIORAL_KINDS = frozenset()

    out: List[ImprovementFinding] = []
    idx = idx_start
    for r in rows:
        if not r.is_active or r.kind in BEHAVIORAL_KINDS:
            continue
        if r.kind in ("quick_update", "custom"):
            continue
        if 0 < len(r.body) < _WEAK_BODY_THRESHOLD_CHARS:
            out.append(_suggest_weak_section(idx, r)); idx += 1
    return out


def _pass_contamination(
    rows: List[_RowView], idx_start: int,
) -> List[ImprovementFinding]:
    """Flag behavioral phrases inside commerce kinds."""
    try:
        from services.knowledge_section_kinds import (  # noqa: PLC0415
            BEHAVIORAL_KINDS,
        )
    except Exception:
        BEHAVIORAL_KINDS = frozenset()

    out: List[ImprovementFinding] = []
    idx = idx_start
    for r in rows:
        if r.kind in BEHAVIORAL_KINDS:
            continue
        joined = f"{r.title}\n{r.body}"
        if _has_behavioral_signal(joined):
            out.append(_suggest_contamination(idx, r)); idx += 1
    return out


def _pass_duplicates(
    rows: List[_RowView],
    idx_start: int,
    *,
    skip_section_ids: Optional[set] = None,
) -> List[ImprovementFinding]:
    """Same-kind duplicate detection (Jaccard ≥ ``_DUPLICATE_SIMILARITY``).

    ``skip_section_ids`` (KB-Improve V1.2, May 2026 #29): when the
    missing-media pass already emitted a merge suggestion covering
    a bank_transfer group, we don't also want generic ``duplicate_
    merge`` findings for the same pairs — those would re-introduce
    the "5 duplicate suggestions" bug from a different angle. Pairs
    where EITHER side is in ``skip_section_ids`` are dropped.
    """
    skip = set(skip_section_ids or [])
    out: List[ImprovementFinding] = []
    idx = idx_start
    seen_pairs: set = set()
    for i, a in enumerate(rows):
        if a.id in skip:
            continue
        a_tokens = _tokens(f"{a.title}\n{a.body}")
        if not a_tokens:
            continue
        for b in rows[i + 1:]:
            if b.id in skip:
                continue
            if a.kind != b.kind:
                continue
            b_tokens = _tokens(f"{b.title}\n{b.body}")
            if not b_tokens:
                continue
            sim = _jaccard(a_tokens, b_tokens)
            if sim < _DUPLICATE_SIMILARITY:
                continue
            key = (min(a.id, b.id), max(a.id, b.id))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            out.append(_suggest_duplicate(idx, a, b, sim)); idx += 1
    return out


def _pass_missing_media(
    rows: List[_RowView], idx_start: int,
) -> List[ImprovementFinding]:
    """Bank-transfer / payment sections without an attached barcode.

    KB-Improve V1.2 (May 2026 #29 problem 2) — the original per-row
    loop emitted N findings for N duplicate bank_transfer rows, each
    with a different ``related_section_ids=[row.id]`` so the
    fingerprint dedup couldn't catch them. Worse, it didn't notice
    when a SIBLING bank_transfer row in the same KB already had a
    barcode attached (which means the merchant is fine — they just
    have legacy duplicates). The fix is to look at the WHOLE group:

      1. Collect bank-shaped bank_transfer / payment_method rows.
      2. If ANY row in the group already has a barcode media
         (link_role='barcode', media_key matches the payment
         family, or title hints at a barcode/QR), emit nothing —
         the merchant's intent is satisfied at the group level.
      3. If exactly ONE row has no barcode → keep the existing
         single ``_suggest_bank_transfer_needs_barcode`` shape so
         the merchant gets the targeted suggestion.
      4. If TWO+ rows have no barcode → emit ONE merge suggestion
         pointing at all of them (instead of N copies). The
         related_section_ids becomes the full sorted set so the
         fingerprint is stable across reruns.
    """
    bank_rows: List[_RowView] = []
    for r in rows:
        if r.kind not in ("bank_transfer", "payment_method"):
            continue
        joined = f"{r.title}\n{r.body}".lower()
        if not _has_any(joined, _BANK_TRANSFER_KEYWORDS):
            continue
        bank_rows.append(r)

    if not bank_rows:
        return []

    # 2. Group-level barcode check: if any sibling has a barcode,
    #    every row in the group is implicitly covered.
    if any(_row_has_barcode_media(r) for r in bank_rows):
        return []

    rows_without_barcode = [r for r in bank_rows if not _row_has_barcode_media(r)]
    if not rows_without_barcode:
        return []

    if len(rows_without_barcode) == 1:
        return [_suggest_bank_transfer_needs_barcode(idx_start, rows_without_barcode[0])]

    # 3. Multiple bank_transfer rows without barcode → ONE merge
    #    suggestion covering them all. Severity stays "high" because
    #    the customer-facing impact (no barcode in transfer replies)
    #    is identical to the single-row case.
    return [_suggest_bank_transfer_merge_and_barcode(idx_start, rows_without_barcode)]


def _pass_compliance(
    rows: List[_RowView], idx_start: int,
) -> List[ImprovementFinding]:
    """Medicinal-claim style risks in commerce / product copy."""
    out: List[ImprovementFinding] = []
    idx = idx_start
    for r in rows:
        if _has_compliance_risk(f"{r.title}\n{r.body}"):
            out.append(_suggest_compliance_risk(idx, r)); idx += 1
    return out


def _pass_product_gaps(
    rows: List[_RowView],
    products: Sequence[CatalogSlice],
    idx_start: int,
) -> List[ImprovementFinding]:
    """High-level product-knowledge gap (no per-product spam — one row)."""
    if not products:
        return []
    has_any_product_usage = any(r.kind == "product_usage" for r in rows)
    if has_any_product_usage:
        # We avoid a per-product spam in V1 — one product-usage row is
        # enough signal that the merchant has started the workflow.
        return []
    missing = [p for p in products if not p.has_description]
    if not missing:
        return []
    return [_suggest_product_usage_gap(idx_start, missing)]


# ── Public auditor API ──────────────────────────────────────────────────────


def audit(
    rows: Sequence[Any],
    *,
    platform_connected: bool = False,
    products: Optional[Sequence[CatalogSlice]] = None,
    max_suggestions: int = _MAX_SUGGESTIONS,
    min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
    suppressed_fingerprints: Optional[Sequence[str]] = None,
) -> List[ImprovementFinding]:
    """Generate up to ``max_suggestions`` deterministic findings.

    Order of passes is intentional: missing-required runs first so high-
    severity gaps surface even when the merchant's KB also has lower-
    severity issues. Final ranking re-orders by severity + confidence
    before truncation, but starting with high-severity passes keeps the
    ID numbering predictable for tests.

    KB-Improve V1.1 suppression:
      * ``min_confidence``           — drop findings below this floor
        BEFORE ranking. Default 0.5 is a no-op for current findings.
      * ``suppressed_fingerprints``  — set of fingerprints the merchant
        has already approved (drafted) or dismissed within TTL. We
        filter them out here so the same suggestion can't reappear
        until either the TTL expires (dismiss path) or the merchant
        creates the resulting section (approve path naturally stops
        the auditor from emitting the gap again).
    """
    views = [_row_view(r) for r in rows if bool(getattr(r, "is_active", True))]
    products_list = list(products or [])
    suppressed = set(suppressed_fingerprints or [])

    findings: List[ImprovementFinding] = []
    next_idx = 1

    findings.extend(_pass_missing_required(views, next_idx))
    next_idx = len(findings) + 1
    findings.extend(_pass_behavior_tone(views, next_idx))
    next_idx = len(findings) + 1
    findings.extend(_pass_weak_sections(views, next_idx))
    next_idx = len(findings) + 1
    findings.extend(_pass_contamination(views, next_idx))
    next_idx = len(findings) + 1

    # Missing-media now runs BEFORE duplicate detection (KB-Improve
    # V1.2, May 2026 #29). When it emits a group-level merge for
    # bank_transfer rows, we collect those section ids and pass them
    # as ``skip_section_ids`` to ``_pass_duplicates`` so a separate
    # generic duplicate finding doesn't re-introduce the same advice
    # from a different angle.
    missing_media_findings = _pass_missing_media(views, next_idx)
    findings.extend(missing_media_findings)
    next_idx = len(findings) + 1

    skip_dup_ids: set = set()
    for mf in missing_media_findings:
        if mf.type == "duplicate_merge":
            skip_dup_ids.update(int(s) for s in mf.related_section_ids or [])

    findings.extend(_pass_duplicates(views, next_idx, skip_section_ids=skip_dup_ids))
    next_idx = len(findings) + 1
    findings.extend(_pass_compliance(views, next_idx))
    next_idx = len(findings) + 1
    findings.extend(_pass_product_gaps(views, products_list, next_idx))

    # ── Platform-conflict guard ─────────────────────────────────────────
    # When the platform is connected, drop any proposed body that looks
    # like it would write a price/stock claim into the KB. Per spec:
    # "إذا الاقتراح متعلق بالسعر أو المخزون، اجعله conflict أو warning فقط".
    if platform_connected:
        findings = [f for f in findings if not _is_platform_claim_body(f)]

    # ── Confidence floor (KB-Improve V1.1, spec point 3) ────────────────
    # The ranker still uses confidence as a secondary sort key — but a
    # finding below ``min_confidence`` is dropped outright rather than
    # just demoted, so we never surface "padding" suggestions when the
    # KB is already in decent shape.
    if min_confidence > 0:
        findings = [f for f in findings
                    if float(f.confidence or 0.0) >= float(min_confidence)]

    # ── Suppression (KB-Improve V1.1, spec points 1 + 2) ────────────────
    # Caller passes in fingerprints we should hide:
    #   * "dismissed" entries within TTL  (rejected by the merchant)
    #   * "applied" entries (drafted from a promote endpoint)
    # The auditor stays stateless — it doesn't know about TTL or DB —
    # but it honors the filter unconditionally.
    if suppressed:
        findings = [f for f in findings if f.fingerprint not in suppressed]

    # ── Purpose-level dedup (KB-Improve V1.2, May 2026 #29) ──────────────
    # Belt-and-braces on top of the per-pass group aggregation: if two
    # different passes accidentally emit findings with the same
    # ``(type, target_kind, purpose:*)`` triple, keep only the highest-
    # confidence one. ``purpose`` lives inside ``rationale_keys`` and
    # NEVER reaches the dashboard — it's a stable internal label
    # (e.g. ``purpose:add_payment_barcode``). This catches the
    # remaining "two facts of the same shape" duplication class
    # without touching the per-section findings whose target naturally
    # differs (e.g. weak_section on row 12 vs row 13 — those have
    # different rationale_keys and stay independent).
    findings = _dedup_by_purpose(findings)

    # ── Content cluster (KB-Improve V1.3, May 2026 #36) ─────────────────
    # The previous fingerprint dedup keyed on ``related_section_ids``,
    # so per-section findings that produced byte-identical UI text
    # (same title, same reason, same proposed_body) survived the dedup
    # because each row carried a different section id. The dashboard
    # then rendered the same card 5 times, which was the merchant
    # feedback we got after Phase 1 shipped.
    #
    # The cluster pass collapses findings on a content-only key
    # (excludes section ids) and aggregates the section ids of all
    # collapsed siblings into the surviving anchor. Findings whose
    # text is genuinely unique (e.g. weak_section on rows with
    # different bodies) stay separate. The fingerprint is
    # recomputed over the merged section list so dismissals hide
    # the entire cluster, not just one row.
    raw_count = len(findings)
    findings, collapsed_count = _cluster_findings(findings)
    if collapsed_count:
        logger.info(
            "[KB_IMPROVE_CLUSTER] raw=%d returned=%d collapsed=%d",
            raw_count, len(findings), collapsed_count,
        )

    # ── Rank + truncate ─────────────────────────────────────────────────
    ranked = sorted(
        findings,
        key=lambda f: (
            -_severity_score(f.severity),
            -float(f.confidence or 0),
        ),
    )
    return ranked[: max(0, int(max_suggestions))]


_PRICE_HINT_RE = re.compile(
    r"\d+\s*(?:ريال|ر\.?س|SAR|sar|درهم|aed|usd|\$)",
    re.IGNORECASE,
)
_STOCK_HINT_RE = re.compile(
    r"(?:متوفر|غير\s+متوفر|نفد|نفذ|in\s+stock|out\s+of\s+stock)",
    re.IGNORECASE,
)


def _is_platform_claim_body(f: ImprovementFinding) -> bool:
    body = f.proposed_body or ""
    return bool(_PRICE_HINT_RE.search(body) or _STOCK_HINT_RE.search(body))


def _purpose_of(f: ImprovementFinding) -> Optional[str]:
    """Extract the ``purpose:<slug>`` marker from ``rationale_keys``.

    Returns ``None`` for findings that don't declare a purpose —
    those are passed through the purpose-dedup unchanged.
    """
    for key in f.rationale_keys or []:
        if not key:
            continue
        s = str(key)
        if s.startswith("purpose:"):
            return s[len("purpose:"):]
    return None


def _content_cluster_key(f: ImprovementFinding) -> str:
    """Stable cluster key for a finding based purely on **content**.

    Excludes ``related_section_ids`` on purpose — that's the field
    that made the fingerprint dedup miss the May 2026 #36
    duplicate-suggestion bug. When five different sections of the
    same kind happen to share an empty/identical title, every per-
    section finding had a different fingerprint (because the section
    id was in the hash) but emitted byte-for-byte identical UI text:

      title:           "حسّن قسم «shipping_zones»"
      reason:          "القسم الحالي قصير جداً ..."
      expected_impact: "ردود أكثر اكتمالاً ..."
      proposed_body:   "<row.body>\n\nأضف هنا تفاصيل أكثر: ..."

    The dashboard then renders the same card N times. We fix that
    by collapsing on the content tuple (type, target_kind, title,
    reason, expected_impact, proposed_body) and aggregating the
    related_section_ids of every collapsed sibling so the merchant
    can still see which rows the cluster covers.

    NB: ``proposed_body`` is intentionally PART of the key —
    weak_section findings interpolate ``row.body`` into the body, so
    two rows with truly different bodies remain in distinct
    clusters. Only rows that produced byte-identical advice
    collapse.
    """
    parts = [
        (f.type or "").strip().lower(),
        (f.target_kind or "").strip().lower(),
        _normalize_for_fingerprint(f.title or ""),
        _normalize_for_fingerprint(f.reason or ""),
        _normalize_for_fingerprint(f.expected_impact or ""),
        _normalize_for_fingerprint(f.proposed_body or ""),
    ]
    raw = "|".join(parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _cluster_findings(
    findings: List[ImprovementFinding],
) -> Tuple[List[ImprovementFinding], int]:
    """Collapse byte-identical findings into one entry per content key.

    Returns ``(survivors, collapsed_count)`` where ``collapsed_count``
    is how many rows we squashed (so observability can chart it).

    Rules:
      * Iteration order is preserved — the first finding in each
        cluster anchors the surviving row's text; later siblings only
        contribute their ``related_section_ids`` and ``rationale_keys``.
      * The anchor's ``confidence`` and ``severity`` are kept as-is.
        We DON'T promote a cluster's severity just because it covers
        more rows — the cluster is "the same advice", not "more
        urgent advice". Severity is a property of the advice itself.
      * The anchor's ``fingerprint`` is recomputed from the merged
        ``related_section_ids`` so the suppression key reflects the
        cluster — when the merchant dismisses the merged card, the
        whole cluster stays hidden until TTL.

    Findings whose content key is unique pass through untouched —
    no allocation, no fingerprint recompute. Hot path stays cheap
    when nothing is duplicated.
    """
    if not findings:
        return [], 0
    by_key: Dict[str, ImprovementFinding] = {}
    section_ids_by_key: Dict[str, List[int]] = {}
    rationale_by_key: Dict[str, List[str]] = {}
    order: List[str] = []
    collapsed = 0
    for f in findings:
        key = _content_cluster_key(f)
        if key not in by_key:
            by_key[key] = f
            section_ids_by_key[key] = list(f.related_section_ids or [])
            rationale_by_key[key] = list(f.rationale_keys or [])
            order.append(key)
            continue
        # Dup → fold into the anchor.
        collapsed += 1
        for sid in (f.related_section_ids or []):
            try:
                sid_int = int(sid)
            except (TypeError, ValueError):
                continue
            if sid_int not in section_ids_by_key[key]:
                section_ids_by_key[key].append(sid_int)
        for rk in (f.rationale_keys or []):
            if rk and rk not in rationale_by_key[key]:
                rationale_by_key[key].append(rk)

    survivors: List[ImprovementFinding] = []
    for key in order:
        anchor = by_key[key]
        merged_ids = sorted(int(s) for s in section_ids_by_key[key])
        merged_rationales = list(rationale_by_key[key])
        if (
            list(anchor.related_section_ids or []) == merged_ids
            and list(anchor.rationale_keys or []) == merged_rationales
        ):
            survivors.append(anchor)
            continue
        # Build a NEW finding so the dataclass post-init recomputes
        # the fingerprint over the merged section list. Suppression
        # keyed on this fp will hide the whole cluster, not just
        # the anchor section.
        clustered = ImprovementFinding(
            id=anchor.id,
            type=anchor.type,
            severity=anchor.severity,
            title=anchor.title,
            reason=anchor.reason,
            expected_impact=anchor.expected_impact,
            target_kind=anchor.target_kind,
            proposed_body=anchor.proposed_body,
            requires_media=anchor.requires_media,
            confidence=anchor.confidence,
            related_section_ids=merged_ids,
            rationale_keys=merged_rationales,
        )
        survivors.append(clustered)
    return survivors, collapsed


def _dedup_by_purpose(
    findings: List[ImprovementFinding],
) -> List[ImprovementFinding]:
    """Collapse findings that share ``(type, target_kind, purpose)``.

    Per spec (KB-Improve V1.2, point 5):
    "لا تعرض أكثر من اقتراح واحد من نفس type + target_kind + purpose."

    Findings without a ``purpose:*`` rationale key pass through —
    only purposeful findings are touched, so per-section weak/contam
    suggestions (which intentionally stay independent) are unaffected.

    When a duplicate is found we keep the higher-confidence finding;
    ties break on severity score then on the lower id (deterministic).
    """
    by_purpose: Dict[Tuple[str, str, str], ImprovementFinding] = {}
    survivors: List[ImprovementFinding] = []
    for f in findings:
        purpose = _purpose_of(f)
        if not purpose:
            survivors.append(f)
            continue
        key = (
            (f.type or "").strip().lower(),
            (f.target_kind or "").strip().lower(),
            purpose,
        )
        existing = by_purpose.get(key)
        if existing is None:
            by_purpose[key] = f
            continue
        # Tie-break: confidence desc, severity desc, lower id first.
        existing_score = (
            float(existing.confidence or 0),
            _severity_score(existing.severity),
            -int((existing.id or "sug-0").split("-")[-1] or 0),
        )
        candidate_score = (
            float(f.confidence or 0),
            _severity_score(f.severity),
            -int((f.id or "sug-0").split("-")[-1] or 0),
        )
        if candidate_score > existing_score:
            by_purpose[key] = f
    return survivors + list(by_purpose.values())


# ── Layer 2 — Optional GPT polisher ─────────────────────────────────────────


_POLISHER_MODEL = os.environ.get(
    "NAHLA_KB_IMPROVEMENT_MODEL",
    os.environ.get("NAHLA_KB_CLASSIFIER_MODEL", "gpt-4.1"),
)


_POLISHER_SYSTEM_PROMPT = """\
أنت محرر عربي لمتجر سعودي على واتساب. ستحصل على قائمة اقتراحات لتحسين
قاعدة المعرفة بصياغات أولية. مهمتك: تحسين الصياغة العربية فقط دون
اختراع أي معلومات جديدة، ودون تغيير الـ ``type`` أو ``target_kind`` أو
``severity`` أو ``confidence``.

أعد JSON فقط بالشكل:
{
  "suggestions": [
    {
      "id": "sug-1",
      "title": "<عنوان عربي محسّن>",
      "reason": "<سبب أوضح للتاجر>",
      "expected_impact": "<أثر متوقع عملي>",
      "proposed_body": "<نص جاهز بصياغة لطيفة لكن دون اختراع أرقام أو فروع أو طرق دفع غير مذكورة أصلاً>"
    }
  ]
}

قواعد إلزامية:
- لا تضف اقتراحات جديدة ولا تحذف أياً منها — احتفظ بنفس قائمة الـ id.
- إذا كان الـ proposed_body الأصلي يحوي placeholder بين أقواس مربعة
  [مثال: «أضف المدة»]، أبقِه كما هو واطلب من التاجر تعبئته بدلاً من
  اختراع رقم.
- لا تكتب JSON خارج الكائن المطلوب.
- لا تتجاوز 500 حرف لكل proposed_body.
"""


def polish_with_gpt(
    findings: Sequence[ImprovementFinding],
    *,
    tenant_id: Optional[int] = None,
) -> List[ImprovementFinding]:
    """Optionally refine Arabic copy. Returns original findings on any failure.

    Skipped entirely (zero network) when ``OPENAI_API_KEY`` is unset.
    The function NEVER mutates ``type`` / ``target_kind`` / ``severity``
    / ``confidence`` / ``requires_media`` / ``related_section_ids`` —
    only the four free-form text fields the polisher is allowed to
    touch.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key or not findings:
        return list(findings)

    payload = {
        "suggestions": [
            {
                "id": f.id,
                "type": f.type,
                "severity": f.severity,
                "target_kind": f.target_kind,
                "title": f.title,
                "reason": f.reason,
                "expected_impact": f.expected_impact,
                "proposed_body": f.proposed_body,
            }
            for f in findings
        ]
    }

    try:
        import httpx  # noqa: PLC0415
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                os.environ.get("OPENAI_API_BASE",
                               "https://api.openai.com/v1") + "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": _POLISHER_MODEL,
                    "messages": [
                        {"role": "system", "content": _POLISHER_SYSTEM_PROMPT},
                        {"role": "user",
                         "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "max_tokens": 1500,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        raw = str(data["choices"][0]["message"]["content"] or "")
        parsed = json.loads(raw)
        polished_by_id: Dict[str, Dict[str, Any]] = {}
        for item in (parsed.get("suggestions") or []):
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id") or "").strip()
            if not sid:
                continue
            polished_by_id[sid] = item

        out: List[ImprovementFinding] = []
        for f in findings:
            patch = polished_by_id.get(f.id)
            if not patch:
                out.append(f); continue
            title = (patch.get("title") or "").strip() or f.title
            reason = (patch.get("reason") or "").strip() or f.reason
            impact = (patch.get("expected_impact") or "").strip() or f.expected_impact
            body = (patch.get("proposed_body") or "").strip() or f.proposed_body
            # Trim aggressively — the polisher is sometimes verbose.
            # CRITICAL: pass the ORIGINAL fingerprint through. The
            # polished title is a near-paraphrase and would compute to
            # a different fp via __post_init__, breaking suppression
            # for any merchant who already dismissed the finding.
            out.append(ImprovementFinding(
                id=f.id, type=f.type, severity=f.severity,
                title=title[:160],
                reason=reason[:400],
                expected_impact=impact[:400],
                target_kind=f.target_kind,
                proposed_body=body[:500],
                requires_media=f.requires_media,
                confidence=f.confidence,
                related_section_ids=list(f.related_section_ids),
                rationale_keys=list(f.rationale_keys),
                fingerprint=f.fingerprint,
            ))
        return out
    except Exception as exc:  # noqa: BLE001 — polishing is best-effort
        logger.info(
            "[KB_IMPROVEMENT] polish skipped tenant=%s err=%s",
            tenant_id, exc,
        )
        return list(findings)


# ── Suppression state (KB-Improve V1.1) ────────────────────────────────────


# Key inside ``TenantSettings.ai_settings`` JSONB where we stash the
# small per-tenant suppression list. Chosen to be obviously scoped to
# this feature so other ai_settings consumers don't accidentally clobber
# it. The value shape is::
#
#     {
#       "dismissed": [
#         {"fp": "abc123...", "ts": "2026-05-22T13:00:00Z",
#          "expires_at": "2026-05-29T13:00:00Z", "type": "...",
#          "target_kind": "..."}
#       ]
#     }
#
# We keep the list capped at 200 entries (FIFO eviction) — pruning
# expired entries on every write keeps it tiny in practice. The cap is
# pure belt-and-braces.
KB_IMPROVEMENT_STATE_KEY = "kb_improvement_state"

_MAX_DISMISSED_ENTRIES = 200


def _now_iso() -> str:
    """``datetime.now(timezone.utc).isoformat()`` — extracted so tests
    can monkeypatch a deterministic clock."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: str) -> Optional[float]:
    """Best-effort ISO-8601 → epoch seconds; ``None`` on garbage."""
    if not s:
        return None
    from datetime import datetime
    try:
        # Python's fromisoformat handles the "+00:00" suffix natively
        # since 3.7; the "Z" form is normalized first for safety.
        normalized = s.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except Exception:
        return None


def load_suppression_state(
    settings_ai_settings: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return the kb_improvement_state dict (always shaped).

    Pure function — accepts the raw ``ai_settings`` dict to keep this
    module decoupled from SQLAlchemy. The router passes it
    ``settings.ai_settings or {}``.
    """
    state = (settings_ai_settings or {}).get(KB_IMPROVEMENT_STATE_KEY) or {}
    if not isinstance(state, dict):
        return {"dismissed": []}
    out = {"dismissed": []}
    raw = state.get("dismissed")
    if isinstance(raw, list):
        out["dismissed"] = [e for e in raw if isinstance(e, dict)]
    return out


def active_dismissed_fingerprints(
    settings_ai_settings: Optional[Dict[str, Any]],
    *,
    now_epoch: Optional[float] = None,
) -> set[str]:
    """Set of fingerprints still inside their TTL window.

    Expired entries are NOT pruned here — pruning happens on write
    (``record_dismissal``). Doing the read fast keeps the hot GET path
    cheap.
    """
    if now_epoch is None:
        import time as _t
        now_epoch = _t.time()
    state = load_suppression_state(settings_ai_settings)
    out: set[str] = set()
    for entry in state.get("dismissed") or []:
        fp = (entry.get("fp") or "").strip()
        if not fp:
            continue
        exp = _parse_iso(entry.get("expires_at") or "")
        if exp is None or exp > now_epoch:
            out.add(fp)
    return out


def record_dismissal(
    settings_ai_settings: Optional[Dict[str, Any]],
    *,
    fingerprint: str,
    suggestion_type: str = "",
    target_kind: str = "",
    ttl_days: int = SUPPRESSION_TTL_DAYS,
) -> Dict[str, Any]:
    """Return a NEW ai_settings dict with the dismissal appended.

    Pure function (no DB write here) so the router controls the
    persistence boundary. Prunes expired entries on the way to keep
    the list bounded.
    """
    from datetime import datetime, timedelta, timezone

    base: Dict[str, Any] = dict(settings_ai_settings or {})
    raw_state = base.get(KB_IMPROVEMENT_STATE_KEY) or {}
    if not isinstance(raw_state, dict):
        raw_state = {}
    dismissed_raw = raw_state.get("dismissed") or []
    if not isinstance(dismissed_raw, list):
        dismissed_raw = []

    now = datetime.now(timezone.utc)
    now_epoch = now.timestamp()
    # 1. Prune expired + drop any earlier copy of the same fingerprint
    #    (rejecting twice resets the TTL, by design).
    fp = (fingerprint or "").strip()
    pruned: List[Dict[str, Any]] = []
    for entry in dismissed_raw:
        if not isinstance(entry, dict):
            continue
        if (entry.get("fp") or "").strip() == fp:
            continue
        exp = _parse_iso(entry.get("expires_at") or "")
        if exp is not None and exp <= now_epoch:
            continue
        pruned.append(entry)

    # 2. Append the new dismissal.
    pruned.append({
        "fp": fp,
        "ts": now.isoformat(),
        "expires_at": (now + timedelta(days=int(ttl_days))).isoformat(),
        "type": (suggestion_type or "").strip()[:64],
        "target_kind": (target_kind or "").strip().lower()[:64],
    })

    # 3. FIFO cap (belt-and-braces).
    if len(pruned) > _MAX_DISMISSED_ENTRIES:
        pruned = pruned[-_MAX_DISMISSED_ENTRIES:]

    raw_state["dismissed"] = pruned
    base[KB_IMPROVEMENT_STATE_KEY] = raw_state
    return base


# ── Observability ───────────────────────────────────────────────────────────


def emit_improvement_log(
    *,
    tenant_id: Optional[int],
    suggestions: Sequence[ImprovementFinding],
    started: float,
    model: str,
    fallback: bool,
) -> None:
    """Structured ``[KB_IMPROVEMENT_SUGGESTIONS]`` log line.

    Aggregates a few counts the platform-owner dashboard can chart over
    time (e.g. "merchants who ran the advisor but found nothing", "% of
    tenants missing a payment policy").
    """
    high = sum(1 for s in suggestions if s.severity == "high")
    types = [s.type for s in suggestions]
    missing_required = sum(1 for t in types if t == "missing_required_knowledge")
    contamination = sum(1 for t in types if t == "semantic_contamination")
    duplicates = sum(1 for t in types if t == "duplicate_merge")
    latency_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "[KB_IMPROVEMENT_SUGGESTIONS] tenant_id=%s suggestions_count=%d "
        "high_severity_count=%d missing_required_count=%d "
        "contamination_count=%d duplicates_count=%d latency_ms=%d "
        "model=%s fallback=%s",
        tenant_id if tenant_id is not None else "-",
        len(suggestions),
        high,
        missing_required,
        contamination,
        duplicates,
        latency_ms,
        model or "-",
        "true" if fallback else "false",
    )
