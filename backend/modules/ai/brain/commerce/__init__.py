"""Brain commerce helpers — recommendation breadth / option-limit policy."""

from .product_breadth_policy import (
    ProductBreadthDecision,
    apply_display_slice,
    clamp_product_attachments,
    explicit_broad_browse_requested,
    limit_initial_product_options_enabled,
    limit_recommendation_breadth_enabled,
    resolve_breadth_for_inbound,
    resolve_catalog_card_limit,
    resolve_product_breadth,
)
from .solution_seeking import (
    SolutionSeekingMatch,
    classify_solution_seeking_commerce,
    intelligent_need_clarification,
)

__all__ = [
    "ProductBreadthDecision",
    "SolutionSeekingMatch",
    "apply_display_slice",
    "clamp_product_attachments",
    "classify_solution_seeking_commerce",
    "explicit_broad_browse_requested",
    "intelligent_need_clarification",
    "limit_initial_product_options_enabled",
    "limit_recommendation_breadth_enabled",
    "resolve_breadth_for_inbound",
    "resolve_catalog_card_limit",
    "resolve_product_breadth",
]
