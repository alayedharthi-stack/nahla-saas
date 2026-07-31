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


def resolve_model_from_audit(
    audit_context: Optional[Dict[str, Any]],
    *,
    default: str,
) -> str:
    """Per-call model override from audit/router metadata — never raises."""
    if not audit_context:
        return default
    override = str(
        audit_context.get("model_override") or audit_context.get("model") or ""
    ).strip()
    return override or default


def resolve_model_for_provider(
    audit_context: Optional[Dict[str, Any]],
    *,
    provider: str,
    default: str,
) -> str:
    """Map router tier model to a provider-native model name."""
    model = resolve_model_from_audit(audit_context, default=default)
    provider_key = str(provider or "").strip().lower()
    if provider_key == "openai_compatible" and model.lower().startswith("claude"):
        return os.environ.get("NAHLA_MODEL_CHEAP", "gpt-5.6-luna").strip() or "gpt-5.6-luna"
    return model


def emit_llm_cost_audit(**fields: Any) -> None:
    """Emit one ``[LLM_COST_AUDIT]`` line; never raises."""
    try:
        payload = {k: v for k, v in fields.items() if v is not None}
        _log.info("[LLM_COST_AUDIT] %s", json.dumps(payload, ensure_ascii=False))
        _emit_prompt_size_warnings(payload)
    except Exception as exc:  # noqa: BLE001 — audit must never break replies
        _log.warning(
            "[LLM_COST_AUDIT_ERROR] failed to emit llm cost audit payload: %s",
            type(exc).__name__,
        )


_PROMPT_SIZE_WARN_TOKENS = int(os.environ.get("NAHLA_PROMPT_SIZE_WARN_TOKENS", "30000"))


def _emit_prompt_size_warnings(payload: Dict[str, Any]) -> None:
    try:
        from modules.ai.brain.compose.prompt_payload_slim import (  # noqa: PLC0415
            ROUTINE_SOCIAL_INTENTS,
        )

        est = int(payload.get("estimated_input_tokens") or 0)
        if est > _PROMPT_SIZE_WARN_TOKENS:
            _log.warning(
                "[LLM_PROMPT_SIZE_WARN] %s",
                json.dumps(
                    {
                        "tenant_id": payload.get("tenant_id"),
                        "conversation_id": payload.get("conversation_id"),
                        "turn_id": payload.get("turn_id"),
                        "intent": payload.get("intent"),
                        "model": payload.get("model"),
                        "estimated_input_tokens": est,
                        "total_prompt_chars": payload.get("total_prompt_chars"),
                        "system_chars": payload.get("system_chars"),
                        "brain_state_json_chars": payload.get("brain_state_json_chars"),
                        "kb_chars": payload.get("kb_chars"),
                        "catalog_chars": payload.get("catalog_chars"),
                    },
                    ensure_ascii=False,
                ),
            )

        intent = str(payload.get("intent") or "").strip().lower()
        if intent in ROUTINE_SOCIAL_INTENTS:
            kb_chars = int(payload.get("kb_chars") or 0)
            catalog_chars = int(payload.get("catalog_chars") or 0)
            if kb_chars > 0 or catalog_chars > 0:
                _log.warning(
                    "[LLM_PROMPT_ROUTINE_BLOAT_WARN] %s",
                    json.dumps(
                        {
                            "tenant_id": payload.get("tenant_id"),
                            "conversation_id": payload.get("conversation_id"),
                            "turn_id": payload.get("turn_id"),
                            "intent": intent,
                            "kb_chars": kb_chars,
                            "catalog_chars": catalog_chars,
                            "estimated_input_tokens": est,
                        },
                        ensure_ascii=False,
                    ),
                )
    except Exception as exc:  # noqa: BLE001 — warnings must never break replies
        _log.warning(
            "[LLM_COST_AUDIT_ERROR] failed prompt size warning: %s",
            type(exc).__name__,
        )


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
    from dataclasses import asdict  # noqa: PLC0415

    from modules.ai.brain.compose.prompt_payload_slim import (  # noqa: PLC0415
        measure_prompt_layer_chars,
        resolve_kb_block_for_prompt,
        strip_state_dict_for_prompt,
    )
    from modules.ai.brain.compose.prompt_state_serializer import (  # noqa: PLC0415
        serialize_commerce_brain_state,
        should_apply_commerce_prompt_slim,
    )
    from modules.ai.prompts.tenant_overlay import build_tenant_overlay_split  # noqa: PLC0415

    mc = dict(getattr(reply_state, "merchant_context", None) or {})
    ai_settings = dict(mc.get("ai_settings") or {})
    overlay = build_tenant_overlay_split(ai_settings)
    structured_kb = str(mc.get("structured_facts_block") or "").strip()
    kb_block = resolve_kb_block_for_prompt(
        reply_state,
        structured_kb=structured_kb,
        overlay_facts=str(overlay.get("facts") or ""),
    )
    layer_sizes = measure_prompt_layer_chars(reply_state, kb_block=kb_block)
    kb_chars = layer_sizes["kb_chars"]
    catalog_chars = layer_sizes["catalog_chars"]
    product_context_chars = layer_sizes["product_context_chars"]
    tools_chars = layer_sizes["tools_chars"]
    ai_settings_chars = len(json.dumps(mc.get("ai_settings") or {}, ensure_ascii=False))

    # BrainStateJSON size proxy (matches prompt_builder serialization path).
    brain_state_json_chars = 0
    try:
        from modules.ai.brain.compose.brain_state_slim import (  # noqa: PLC0415
            prepare_brain_state_dict_with_telemetry,
        )

        state_dict = asdict(reply_state)
        state_dict.pop("tenant_overlay", None)
        if should_apply_commerce_prompt_slim(reply_state):
            state_dict = serialize_commerce_brain_state(
                state_dict,
                reply_state,
                kb_in_prompt_block=bool(kb_block),
            )
        else:
            state_dict = strip_state_dict_for_prompt(
                state_dict,
                reply_state,
                kb_in_prompt_block=bool(kb_block),
            )
        slim = prepare_brain_state_dict_with_telemetry(reply_state, state_dict)
        brain_state_json_chars = len(
            json.dumps(slim, ensure_ascii=False, indent=2)
        )
    except Exception as exc:  # noqa: BLE001 — audit must never break replies
        _log.warning(
            "[LLM_COST_AUDIT_ERROR] failed to build brain state json char count: %s",
            type(exc).__name__,
        )

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
        "commerce_prompt_slim": should_apply_commerce_prompt_slim(reply_state),
    }
