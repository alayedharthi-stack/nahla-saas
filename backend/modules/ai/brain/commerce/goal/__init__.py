"""Goal-based commerce — platform reasoning + KB retrieval + bundle composition."""
from .bundle_composition import RegimenBundle, compose_regimen_bundle
from .goal_reasoning import GoalMatch, detect_customer_goal
from .goal_retrieval import GoalKBEntry, retrieve_goal_recommendations
from .goal_taxonomy import GOAL_TAXONOMY, GoalTag, normalize_goal_tag
from .telemetry import log_goal_commerce, log_goal_resolution_failed

__all__ = [
    "GOAL_TAXONOMY",
    "GoalKBEntry",
    "GoalMatch",
    "GoalTag",
    "RegimenBundle",
    "compose_regimen_bundle",
    "detect_customer_goal",
    "log_goal_commerce",
    "log_goal_resolution_failed",
    "normalize_goal_tag",
    "retrieve_goal_recommendations",
]
