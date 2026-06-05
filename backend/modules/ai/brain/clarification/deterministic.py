"""
clarification/deterministic.py
────────────────────────────────
Bounded clarification from structured slots only.

Used when the missing field is known and the answer space is enumerable.
Does not use axis template pools or message phrase matching.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .types import ClarificationSpec


def build_deterministic_question(spec: ClarificationSpec) -> Optional[str]:
    """
    Build a clarify question from ``structured_prompt`` when options are known.

    Returns ``None`` when structured data is insufficient — caller must fall
    back to legacy or generative path.
    """
    sp: Dict[str, Any] = dict(spec.structured_prompt or {})
    field = str(sp.get("field") or "").strip().lower()
    if not field:
        return None

    if field == "list_pick":
        options: List[str] = [
            str(o).strip() for o in list(sp.get("options") or []) if str(o).strip()
        ]
        if not options:
            return None
        lines = ["أي خيار تقصد؟"]
        for idx, title in enumerate(options[:6], 1):
            lines.append(f"{idx}. {title}")
        lines.append("(اكتب رقم الخيار أو اسمه)")
        return "\n".join(lines)

    if field == "variant":
        title = str(sp.get("product_title") or "").strip()
        if title:
            return f"أي حجم/خيار تقصد لـ «{title}»؟"
        return "أي حجم/خيار تقصد؟"

    if field == "quantity":
        title = str(sp.get("product_title") or "").strip()
        unit = str(sp.get("unit") or "").strip()
        if title and unit:
            return f"كم {unit} تقصد من «{title}»؟"
        if unit:
            return f"كم {unit} تقصد؟"
        return None

    return None


__all__ = ["build_deterministic_question"]
