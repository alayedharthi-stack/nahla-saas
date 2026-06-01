"""
Validated metadata schema for ``goal_based_recommendation`` KB sections.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .goal_taxonomy import normalize_goal_tags

VALID_PRODUCT_ROLES = frozenset({
    "primary",
    "complement",
    "upsell",
    "optional",
})


@dataclass
class GoalProductRef:
    ref: str = ""
    role: str = "primary"
    note: str = ""
    product_id: Optional[int] = None

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["GoalProductRef"]:
        if not isinstance(raw, dict):
            return None
        ref = str(raw.get("ref") or raw.get("title") or "").strip()
        pid = raw.get("product_id")
        try:
            product_id = int(pid) if pid not in (None, "") else None
        except (TypeError, ValueError):
            product_id = None
        if not ref and product_id is None:
            return None
        role = str(raw.get("role") or "primary").strip().lower()
        if role not in VALID_PRODUCT_ROLES:
            role = "primary"
        return cls(
            ref=ref,
            role=role,
            note=str(raw.get("note") or "").strip(),
            product_id=product_id,
        )


@dataclass
class GoalKBMetadata:
    goal_tags: List[str] = field(default_factory=list)
    products: List[GoalProductRef] = field(default_factory=list)
    usage_guidance: List[str] = field(default_factory=list)
    soft_claims: List[str] = field(default_factory=list)
    followup_questions: List[str] = field(default_factory=list)
    compliance: List[str] = field(default_factory=list)

    @classmethod
    def from_metadata_json(cls, meta: Optional[Dict[str, Any]]) -> Optional["GoalKBMetadata"]:
        if not isinstance(meta, dict):
            return None
        tags = normalize_goal_tags(list(meta.get("goal_tags") or []))
        if not tags:
            return None
        products: List[GoalProductRef] = []
        for item in meta.get("products") or []:
            parsed = GoalProductRef.from_dict(item)
            if parsed:
                products.append(parsed)
        if not products:
            return None

        def _str_list(key: str) -> List[str]:
            raw = meta.get(key) or []
            if isinstance(raw, str):
                raw = [raw]
            return [str(x).strip() for x in raw if str(x).strip()]

        return cls(
            goal_tags=tags,
            products=products,
            usage_guidance=_str_list("usage_guidance"),
            soft_claims=_str_list("soft_claims"),
            followup_questions=_str_list("followup_questions"),
            compliance=_str_list("compliance"),
        )
