"""
clear_intent_recompose.py
───────────────────────────
Post-safety-net LLM recompose when clear-intent facts require rephrasing.

Phase 2 follow-up to #280: facts/metadata only from
``apply_clear_intent_fallback_net``; this module turns those facts into
constrained LLM compose — never ``_CLEAR_INTENT_REPLIES`` templates.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Tuple

from core.reply_instruction import (
    CONSTRAINT_NO_FALSE_HANDOFF,
    CONSTRAINT_NO_PAYMENT_CONFIRM,
    CONSTRAINT_NO_PRICE_INVENTION,
    CONSTRAINT_NO_SHIPPING_PROMISE,
    DECISION_KIND_CLEAR_INTENT,
    PATH_CLEAR_INTENT_FALLBACK,
    ReplyInstruction,
    is_operational_constrained_compose_enabled,
)

logger = logging.getLogger("nahla.clear_intent_recompose")

_FLAG_RECOMPOSE = "CLEAR_INTENT_RECOMPOSE_ENABLED"


def clear_intent_recompose_enabled() -> bool:
    raw = (os.getenv(_FLAG_RECOMPOSE) or "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def should_recompose_clear_intent(clear_intent_result: Any) -> bool:
    """True when safety net detected a clear intent on a weak/generic reply."""
    if clear_intent_result is None:
        return False
    if not getattr(clear_intent_result, "fired", False):
        return False
    facts = getattr(clear_intent_result, "facts", None) or {}
    return str(facts.get("required_delivery") or "") == "llm_rephrase"


def build_clear_intent_instruction_from_result(
    clear_intent_result: Any,
    *,
    weak_reply: str,
    inbound_text: str,
) -> ReplyInstruction:
    facts: Dict[str, Any] = dict(getattr(clear_intent_result, "facts", None) or {})
    intent = str(
        facts.get("detected_intent")
        or getattr(clear_intent_result, "customer_intent", "")
        or ""
    ).strip()
    facts.setdefault("clear_intent", intent)
    facts.setdefault("required_delivery", "llm_rephrase")
    return ReplyInstruction(
        path=PATH_CLEAR_INTENT_FALLBACK,
        decision_kind=DECISION_KIND_CLEAR_INTENT,
        facts=facts,
        constraints=(
            CONSTRAINT_NO_PRICE_INVENTION,
            CONSTRAINT_NO_SHIPPING_PROMISE,
            CONSTRAINT_NO_PAYMENT_CONFIRM,
            CONSTRAINT_NO_FALSE_HANDOFF,
        ),
        forbidden_claims=(),
        legacy_copy=str(weak_reply or "").strip(),
        decision_owner="postprocess.safety_nets.clear_intent",
        inbound_text=inbound_text,
    )


async def maybe_recompose_clear_intent_reply(
    *,
    db: Any,
    tenant_id: int,
    phone: str,
    clear_intent_result: Any,
    inbound_text: str,
    weak_reply: str,
) -> Tuple[str, Dict[str, Any]]:
    """Return (reply_text, metadata). Keeps *weak_reply* when recompose skipped/fails."""
    weak = str(weak_reply or "").strip()
    base_meta: Dict[str, Any] = {
        "recomposed": False,
        "clear_intent_fallback_facts": dict(
            getattr(clear_intent_result, "facts", None) or {}
        ),
    }
    if not should_recompose_clear_intent(clear_intent_result):
        base_meta["skipped_reason"] = "not_required"
        return weak, base_meta

    if not clear_intent_recompose_enabled():
        base_meta["skipped_reason"] = "flag_disabled"
        return weak, base_meta

    if not is_operational_constrained_compose_enabled():
        base_meta["skipped_reason"] = "constrained_compose_disabled"
        return weak, base_meta

    instruction = build_clear_intent_instruction_from_result(
        clear_intent_result,
        weak_reply=weak,
        inbound_text=inbound_text,
    )

    from core.constrained_operational_compose import compose_constrained_operational_reply  # noqa: PLC0415

    try:
        text, compose_meta = await compose_constrained_operational_reply(
            db=db,
            tenant_id=tenant_id,
            phone=phone,
            instruction=instruction,
            inbound_text=inbound_text,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[CLEAR_INTENT_RECOMPOSE] failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        base_meta["skipped_reason"] = "compose_exception"
        return weak, base_meta

    out = str(text or "").strip()
    if not out or out == weak:
        base_meta["skipped_reason"] = "compose_empty_or_unchanged"
        base_meta.update(compose_meta or {})
        return weak, base_meta

    if (compose_meta or {}).get("copy_source") != "constrained_compose":
        base_meta["skipped_reason"] = "compose_used_legacy_fallback"
        base_meta.update(compose_meta or {})
        return weak, base_meta

    merged = {**base_meta, **(compose_meta or {})}
    merged["recomposed"] = True
    merged["reply_source"] = "clear_intent_recompose"
    logger.info(
        "[CLEAR_INTENT_RECOMPOSE] ok tenant=%s intent=%s len=%d",
        tenant_id,
        instruction.facts.get("clear_intent"),
        len(out),
    )
    return out, merged


__all__ = [
    "build_clear_intent_instruction_from_result",
    "clear_intent_recompose_enabled",
    "maybe_recompose_clear_intent_reply",
    "should_recompose_clear_intent",
]
