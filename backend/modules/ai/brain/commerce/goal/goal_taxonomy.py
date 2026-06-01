"""
Closed goal taxonomy for goal-based commerce.

Merchant KB ``goal_tags`` MUST map into these enums — never the reverse.
"""
from __future__ import annotations

from enum import Enum
from typing import FrozenSet, Optional


class GoalTag(str, Enum):
    FERTILITY_VITALITY = "fertility_vitality"
    IMMUNITY_SUPPORT = "immunity_support"
    ENERGY_DAILY = "energy_daily"
    SLEEP_RELAXATION = "sleep_relaxation"
    GIFTING_LUXURY = "gifting_luxury"
    FITNESS_PERFORMANCE = "fitness_performance"
    DAILY_WELLNESS = "daily_wellness"
    SKIN_SENSITIVE = "skin_sensitive"
    WEIGHT_DIET = "weight_diet"
    GENERAL_WELLNESS = "general_wellness"


GOAL_TAXONOMY: FrozenSet[str] = frozenset(g.value for g in GoalTag)

# Aliases merchants might typo in KB — normalized at ingest/retrieval.
_GOAL_ALIASES = {
    "fertility": GoalTag.FERTILITY_VITALITY.value,
    "vitality": GoalTag.FERTILITY_VITALITY.value,
    "خصوبة": GoalTag.FERTILITY_VITALITY.value,
    "immunity": GoalTag.IMMUNITY_SUPPORT.value,
    "مناعه": GoalTag.IMMUNITY_SUPPORT.value,
    "energy": GoalTag.ENERGY_DAILY.value,
    "طاقه": GoalTag.ENERGY_DAILY.value,
    "sleep": GoalTag.SLEEP_RELAXATION.value,
    "نوم": GoalTag.SLEEP_RELAXATION.value,
    "gift": GoalTag.GIFTING_LUXURY.value,
    "هديه": GoalTag.GIFTING_LUXURY.value,
    "fitness": GoalTag.FITNESS_PERFORMANCE.value,
    "daily_wellness": GoalTag.DAILY_WELLNESS.value,
    "wellness": GoalTag.GENERAL_WELLNESS.value,
}


def normalize_goal_tag(raw: str) -> Optional[str]:
    """Return canonical goal tag or ``None`` if unknown."""
    s = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not s:
        return None
    if s in GOAL_TAXONOMY:
        return s
    if s in _GOAL_ALIASES:
        return _GOAL_ALIASES[s]
    return None


def normalize_goal_tags(tags: list) -> list[str]:
    out: list[str] = []
    for t in tags or []:
        canon = normalize_goal_tag(str(t))
        if canon and canon not in out:
            out.append(canon)
    return out
