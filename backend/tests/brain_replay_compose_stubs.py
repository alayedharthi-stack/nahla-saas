"""
brain_replay_compose_stubs.py
─────────────────────────────
Recorded compose stubs for BrainReplayRunner — CI-safe, no external LLM.

Routing stays brain-owned; only final LLM compose is stubbed.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from modules.ai.brain.types import BrainContext, Decision

_STUB_MARKER = "[brain-replay-stub]"


def _order_prep_from_ctx(ctx: BrainContext) -> Dict[str, Any]:
    state = getattr(ctx, "state", None)
    prep = getattr(state, "order_prep", None) if state is not None else None
    if isinstance(prep, dict):
        return prep
    bundle = dict(getattr(ctx, "commerce_bundle", None) or {})
    nested = bundle.get("order_prep")
    return dict(nested) if isinstance(nested, dict) else {}


def _topic(decision: Optional[Decision]) -> str:
    if decision is None:
        return ""
    return str((decision.args or {}).get("topic") or "")


def stub_llm_reply(
    ctx: BrainContext,
    *,
    decision: Optional[Decision] = None,
) -> str:
    """Context-aware compose stub — mirrors live canary shapes without store runtime logic."""
    message = (ctx.message or "").strip()
    topic = _topic(decision)
    prep = _order_prep_from_ctx(ctx)
    catalog_total = float(
        prep.get("order_flow_v2_catalog_total")
        or prep.get("catalog_total")
        or 365.5
    )

    if "اخترت" in message:
        return ""

    if "منتجات" in message or topic in {"catalog_browse", "native_catalog_browse"}:
        return "اختر المنتجات المناسبة من القائمة التالية 👇"

    if message in {"شفيك", "شلونك"} or topic == "checkout_delivery_mode":
        return "كيف تبين نستلم منك الطلب؟ توصيل أو استلام من المتجر؟"

    if any(tok in message for tok in ("ودوه", "وديه", "لعنواني")):
        return f"تمام، نوصل لعنوانك. المجموع {catalog_total:.2f} ريال."

    bank_tokens = ("الراجحي", "راجحي", "تحويل الراجحي", "الأهلي", "اهلي")
    if any(tok in message for tok in bank_tokens) or topic in {
        "payment_info",
        "payment_method",
        "bank_transfer",
    }:
        if "الأهلي" in message or "اهلي" in message:
            return "أكيد، هذه بيانات التحويل البنكي للأهلي."
        return "أكيد، هذه بيانات التحويل البنكي للراجحي."

    # Name-like during checkout can escape to catalog browse in live canary.
    if len(message.split()) >= 2 and not any(ch.isdigit() for ch in message):
        stage = str(getattr(getattr(ctx, "state", None), "stage", "") or "")
        if stage in {"ordering", "checkout", "deciding"}:
            return (
                "أقدر أعرض لك الخيارات المؤكدة من الكتالوج — اختر من القائمة "
                "أو اذكر اسم المنتج 🍯"
            )

    if topic:
        return f"{_STUB_MARKER} topic={topic}"

    if message:
        return f"{_STUB_MARKER} msg={message[:60]}"

    return f"{_STUB_MARKER} empty"


async def stub_llm_compose(
    _composer_self: Any,
    ctx: BrainContext,
    _result: Any,
    *,
    decision: Optional[Decision] = None,
    **_kwargs: Any,
) -> str:
    return stub_llm_reply(ctx, decision=decision)


async def stub_legacy_llm_compose(
    _composer_self: Any,
    ctx: BrainContext,
    _result: Any,
    *,
    decision: Optional[Decision] = None,
    **_kwargs: Any,
) -> str:
    return stub_llm_reply(ctx, decision=decision)


async def stub_extract_slots(_message: str, _history: Optional[list] = None) -> Dict[str, Any]:
    """Skip external slot LLM — ordering heuristics still run in classifier."""
    return {}


__all__ = [
    "stub_extract_slots",
    "stub_legacy_llm_compose",
    "stub_llm_compose",
    "stub_llm_reply",
]
