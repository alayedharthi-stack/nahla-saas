"""
Tenant-agnostic goal detection from customer messages.

Detects *outcomes* only — never products or merchant-specific regimens.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .goal_taxonomy import GoalTag, normalize_goal_tag

GOAL_CONFIDENCE = 0.91

_GOAL_PATTERNS: List[Tuple[str, List[str]]] = [
    (GoalTag.FERTILITY_VITALITY.value, [
        r"خصوب", r"للخصوب", r"تأخر\s*حمل", r"محاول(?:ه|ة)\s*حمل",
        r"vitality", r"fertility",
    ]),
    (GoalTag.IMMUNITY_SUPPORT.value, [
        r"مناع", r"immunity", r"immune",
    ]),
    (GoalTag.ENERGY_DAILY.value, [
        r"طاق(?:ه|ة)?", r"نشاط", r"energy", r"تعب", r"إرهاق",
    ]),
    (GoalTag.SLEEP_RELAXATION.value, [
        r"نوم", r"sleep", r"ارق", r"أرق", r"relax",
    ]),
    (GoalTag.GIFTING_LUXURY.value, [
        r"هد(?:يه|ية)", r"gift", r"هدية\s*فاخ", r"luxury",
    ]),
    (GoalTag.FITNESS_PERFORMANCE.value, [
        r"رياض", r"fitness", r"performance", r"تحمل",
    ]),
    (GoalTag.DAILY_WELLNESS.value, [
        r"يومي", r"daily\s*wellness", r"روتين\s*يوم",
    ]),
    (GoalTag.SKIN_SENSITIVE.value, [
        r"بشر(?:ه|ة)\s*حساس", r"sensitive\s*skin",
    ]),
    (GoalTag.WEIGHT_DIET.value, [
        r"دايت", r"رجيم", r"diet", r"weight\s*loss", r"تخسيس",
    ]),
    (GoalTag.GENERAL_WELLNESS.value, [
        r"صح(?:ه|ة)", r"wellness", r"عاف(?:ه|ة)",
    ]),
]

_OUTCOME_STRUCTURE_RE = re.compile(
    r"(?:"
    r"شي(?:ء)?\s*(?:ل|لـ|ال)?"
    r"|(?:اب(?:ي|غ(?:ى|a)?)|أ(?:بي|ب(?:غ(?:ى|a)?)?)|ودي|بدي)\s+"
    r"|(?:يناسب|مناسب\s*ل|ل(?:ل)?)"
    r"|(?:تنصحن|ترشح|وش\s+تنصح|ايش\s+تنصح)"
    r")",
    re.UNICODE | re.IGNORECASE,
)


@dataclass(frozen=True)
class GoalMatch:
    goal: str
    confidence: float = GOAL_CONFIDENCE
    source: str = "pattern"  # pattern | structure


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", (text or "").strip().lower())
    t = re.sub(r"[\u064B-\u065F\u0640]", "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", t).strip()


def detect_customer_goal(message: str) -> Optional[GoalMatch]:
    """Detect normalized customer outcome goal — no product assumptions."""
    raw = (message or "").strip()
    if not raw or len(raw) > 600:
        return None
    norm = _norm(raw)
    if len(norm) < 4:
        return None

    for goal, patterns in _GOAL_PATTERNS:
        if normalize_goal_tag(goal) is None:
            continue
        for pat in patterns:
            if re.search(pat, norm, re.IGNORECASE | re.UNICODE):
                if _OUTCOME_STRUCTURE_RE.search(norm) or len(norm.split()) >= 3:
                    return GoalMatch(goal=goal, source="pattern")
                return GoalMatch(goal=goal, confidence=0.84, source="pattern_weak")

    if _OUTCOME_STRUCTURE_RE.search(norm):
        return GoalMatch(goal=GoalTag.GENERAL_WELLNESS.value, confidence=0.72, source="structure")

    return None
