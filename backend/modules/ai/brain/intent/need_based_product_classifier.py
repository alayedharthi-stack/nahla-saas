"""
brain/intent/need_based_product_classifier.py
──────────────────────────────────────────────
Backward-compatible shim — delegates to the global
``brain.commerce.solution_seeking`` intelligence layer.
"""
from __future__ import annotations

from typing import Optional

from ..commerce.solution_seeking import (
    SolutionSeekingMatch,
    classify_solution_seeking_commerce,
)

NeedBasedMatch = SolutionSeekingMatch

# Legacy constant names (tests / old imports)
NEED_DIABETES = "health_diet"
NEED_COLON = "health_diet"
NEED_STOMACH = "health_diet"
NEED_CHILDREN = "audience_age"
NEED_SLEEP = "health_diet"
NEED_GENERAL = "general_attribute"


def classify_need_based_product_advice(message: str) -> Optional[SolutionSeekingMatch]:
    return classify_solution_seeking_commerce(message)


__all__ = [
    "NEED_CHILDREN",
    "NEED_COLON",
    "NEED_DIABETES",
    "NEED_GENERAL",
    "NEED_SLEEP",
    "NEED_STOMACH",
    "NeedBasedMatch",
    "classify_need_based_product_advice",
]
