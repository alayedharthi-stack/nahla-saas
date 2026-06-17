"""
clarification/deterministic.py
────────────────────────────────
Bounded clarification from structured slots only.

Used when the missing field is known and the answer space is enumerable.
Does not use axis template pools or message phrase matching.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..types import BrainContext
from .types import ClarificationSpec


def build_deterministic_question(
    spec: ClarificationSpec,
    *,
    ctx: Optional[BrainContext] = None,
) -> Optional[str]:
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

    if field == "product_order":
        if ctx is not None:
            from ..commerce.product_ordering_prompt import build_product_ordering_prompt  # noqa: PLC0415

            return build_product_ordering_prompt(ctx)
        return None

    if field == "variant":
        title = str(sp.get("product_title") or "").strip()
        if title:
            return f"أي حجم/خيار تقصد لـ «{title}»؟"
        return "أي حجم/خيار تقصد؟"

    if field == "quantity":
        title = str(sp.get("product_title") or "").strip()
        if ctx is not None:
            from ..commerce.product_ordering_prompt import build_product_ordering_prompt  # noqa: PLC0415

            prompt = build_product_ordering_prompt(ctx)
            if prompt and "كم" in prompt:
                return prompt
        if title:
            return f"تمام، كم الكمية اللي تبيها من «{title}»؟"
        return "تمام، كم الكمية اللي تبيها؟"

    if field == "address":
        return "تمام، وين التوصيل؟ أرسل المدينة أو رابط الموقع."

    if field == "payment":
        return "تمام، تبي الدفع تحويل بنكي ولا عند الاستلام؟"

    return None


__all__ = ["build_deterministic_question"]
