"""
llm_cost_audit.py
─────────────────
Structured, PII-free telemetry emitted immediately before Anthropic calls.

Logs character counts and routing metadata only — never message bodies or KB text.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Mapping, Optional

_log = logging.getLogger("nahla.ai.llm_cost_audit")

_CHARS_PER_TOKEN = int(os.environ.get("AI_CHARS_PER_TOKEN", "4"))


def approx_tokens_from_chars(char_count: int) -> int:
    return max(0, int(char_count) // _CHARS_PER_TOKEN)


def resolve_anthropic_model() -> str:
    """Single source for the runtime Claude model name."""
    explicit = os.environ.get("CLAUDE_MODEL", "").strip()
    if explicit:
        return explicit
    try:
        from core.config import CLAUDE_MODEL as _cfg_default  # noqa: PLC0415

        return str(_cfg_default)
    except Exception:  # noqa: BLE001
        return "claude-haiku-4-5"


def emit_llm_cost_audit(**fields: Any) -> None:
    """Emit one ``[LLM_COST_AUDIT]`` line; never raises."""
    try:
        payload = {k: v for k, v in fields.items() if v is not None}
        _log.info("[LLM_COST_AUDIT] %s", json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


def _messages_char_count(messages: List[Mapping[str, Any]]) -> int:
    total = 0
    for item in messages:
        total += len(str(item.get("content") or ""))
    return total


def audit_from_orchestration_request(
    *,
    request: Any,
    prompt: str,
    model: str,
    provider: str = "anthropic",
    source: str = "orchestrator.engine",
) -> None:
    """Audit hook for the canonical adapter → pipeline → engine path."""
    ctx = getattr(request, "context", None)
    meta: Dict[str, Any] = dict(getattr(ctx, "metadata", None) or {})
    overrides: Dict[str, Any] = dict(getattr(request, "prompt_overrides", None) or {})
    extra: Dict[str, Any] = dict(overrides.get("__llm_cost_audit") or {})

    history_msgs = [
        {"role": m.role, "content": m.content}
        for m in (getattr(request, "history", None) or [])
    ]
    if not history_msgs and getattr(request, "message", ""):
        history_msgs = [{"role": "user", "content": str(request.message)}]

    system_chars = len(prompt or "")
    messages_chars = _messages_char_count(history_msgs)
    total_prompt_chars = system_chars + messages_chars

    emit_llm_cost_audit(
        tenant_id=getattr(ctx, "tenant_id", None) if ctx else extra.get("tenant_id"),
        conversation_id=extra.get("conversation_id") or meta.get("conversation_id"),
        turn_id=extra.get("turn_id") or meta.get("turn_id"),
        model=model,
        provider=provider,
        messages_count=len(history_msgs),
        system_chars=system_chars,
        messages_chars=messages_chars,
        brain_state_json_chars=extra.get("brain_state_json_chars"),
        history_chars=extra.get("history_chars", messages_chars),
        kb_chars=extra.get("kb_chars"),
        catalog_chars=extra.get("catalog_chars"),
        product_context_chars=extra.get("product_context_chars"),
        tools_chars=extra.get("tools_chars"),
        total_prompt_chars=total_prompt_chars,
        estimated_input_tokens=approx_tokens_from_chars(total_prompt_chars),
        reason=extra.get("reason") or source,
        intent=extra.get("intent"),
        stage=extra.get("stage"),
        channel=getattr(ctx, "channel", None) if ctx else extra.get("channel"),
    )


def build_brain_compose_audit_extra(
    *,
    reply_state: Any,
    prompt: str,
    history_messages: List[Dict[str, str]],
    tenant_id: Optional[int],
    conversation_id: Optional[int],
    turn_id: Optional[int],
    source: str = "brain.compose._llm_compose",
) -> Dict[str, Any]:
    """Size breakdown for MerchantBrain compose — no message content."""
    mc = dict(getattr(reply_state, "merchant_context", None) or {})
    kb_chars = len(str(mc.get("structured_facts_block") or ""))
    catalog_chars = len(json.dumps(mc.get("products") or [], ensure_ascii=False))
    product_context_chars = len(
        json.dumps(getattr(reply_state, "selected_product", None) or {}, ensure_ascii=False)
    )
    tools_chars = len(str(mc.get("resolver_overlay") or ""))
    ai_settings_chars = len(json.dumps(mc.get("ai_settings") or {}, ensure_ascii=False))

    # BrainStateJSON size proxy (matches prompt_builder serialization path).
    brain_state_json_chars = 0
    try:
        from dataclasses import asdict  # noqa: PLC0415
        from modules.ai.brain.compose.brain_state_slim import (  # noqa: PLC0415
            prepare_brain_state_dict_with_telemetry,
        )

        state_dict = asdict(reply_state)
        state_dict.pop("tenant_overlay", None)
        slim = prepare_brain_state_dict_with_telemetry(reply_state, state_dict)
        brain_state_json_chars = len(
            json.dumps(slim, ensure_ascii=False, indent=2)
        )
    except Exception:  # noqa: BLE001
        pass

    history_chars = _messages_char_count(history_messages)
    system_chars = len(prompt or "")

    return {
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "intent": getattr(reply_state, "intent_name", None),
        "stage": getattr(reply_state, "stage", None),
        "kb_chars": kb_chars,
        "catalog_chars": catalog_chars,
        "product_context_chars": product_context_chars,
        "tools_chars": tools_chars,
        "brain_state_json_chars": brain_state_json_chars,
        "ai_settings_json_chars": ai_settings_chars,
        "history_chars": history_chars,
        "system_chars": system_chars,
        "total_prompt_chars": system_chars + history_chars,
        "reason": source,
    }
