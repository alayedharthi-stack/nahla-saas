"""
backend/modules/ai/orchestrator/providers/openai_compatible_provider.py
────────────────────────────────────────────────────────────────────────
OpenAI-compatible provider implementation for the Nahla orchestration engine.

"OpenAI-compatible" means any endpoint that speaks the OpenAI Chat Completions
API (POST /chat/completions with the same request/response shape), including:
  - OpenAI API directly
  - Azure OpenAI
  - Local vLLM / llama.cpp servers
  - Any other compatible gateway

Configuration (via environment variables only — no hardcoded values):
  OPENAI_API_KEY      : bearer token for the endpoint
  OPENAI_API_BASE     : base URL (default: https://api.openai.com/v1)
  OPENAI_MODEL        : model name (default: gpt-5.6-luna)

Status:
  REGISTERED but NOT activated for runtime routing.
  The engine still uses AnthropicProvider exclusively.
  This provider is present in the registry so provider_chain
  routing can activate it without any further code changes.

No network call occurs at import time.
All I/O is deferred to call(...).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from modules.ai.orchestrator.ai_usage_ledger import record_ai_usage_from_openai_compatible
from modules.ai.orchestrator.llm_cost_audit import (
    approx_tokens_from_chars,
    emit_llm_cost_audit,
    resolve_model_from_audit,
    resolve_model_for_provider,
)
from modules.ai.orchestrator.providers.base import BaseAIProvider

logger = logging.getLogger("nahla.ai.orchestrator.engine")  # same logger as engine

# ── Configuration (read once at module import) ─────────────────────────────────
_API_KEY  = os.environ.get("OPENAI_API_KEY", "")
_API_BASE = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
_MODEL    = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna")
_TIMEOUT  = 25.0


def _build_chat_completion_body(
    model: str,
    messages: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build OpenAI chat completion body with model-specific constraints.

    GPT-5.6 family rejects non-default temperature (400). Omit temperature
    for those models; keep legacy temperature for older slugs.
    """
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": 1024,
    }
    if not str(model).startswith("gpt-5.6-"):
        body["temperature"] = 0.7
    return body


class OpenAICompatibleProvider(BaseAIProvider):
    """
    OpenAI Chat Completions-compatible provider.

    Stateless — safe to share a single instance across requests.
    Implements the same BaseAIProvider interface as AnthropicProvider.
    """

    @property
    def provider_name(self) -> str:
        return "openai_compatible"

    def is_configured(self) -> bool:
        """Return True when OPENAI_API_KEY is set."""
        return bool(_API_KEY)

    def call(
        self,
        message: str,
        prompt: str,
        *,
        history: Optional[List[Dict[str, Any]]] = None,
        audit_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Call an OpenAI-compatible chat completions endpoint synchronously.

        Returns a dict with reply_text, provider, model, status.
        Never raises — empty reply_text signals failure to the engine.
        """
        if not _API_KEY:
            logger.info(
                "[engine] OPENAI_API_KEY not set — openai_compatible provider "
                "returning empty reply_text"
            )
            return {
                "provider":   "openai_compatible",
                "model":      _MODEL,
                "reply_text": "",
                "status":     "no_api_key",
            }

        try:
            import httpx
        except ImportError:
            logger.warning(
                "[engine] httpx not available — openai_compatible provider "
                "returning empty reply_text (fallback triggered)"
            )
            return {
                "provider":   "openai_compatible",
                "model":      _MODEL,
                "reply_text": "",
                "status":     "no_http_client",
            }

        model = resolve_model_for_provider(
            audit_context,
            provider="openai_compatible",
            default=_MODEL,
        )

        try:
            headers = {
                "Authorization": f"Bearer {_API_KEY}",
                "Content-Type":  "application/json",
            }
            merged_messages = [{"role": "system", "content": prompt}, *_merge_history(history, message)]
            body = _build_chat_completion_body(model, merged_messages)
            audit_extra = dict(audit_context or {})
            messages_chars = sum(len(str(m.get("content") or "")) for m in merged_messages[1:])
            total_prompt_chars = len(prompt or "") + messages_chars
            emit_llm_cost_audit(
                tenant_id=audit_extra.get("tenant_id"),
                conversation_id=audit_extra.get("conversation_id"),
                turn_id=audit_extra.get("turn_id"),
                model=model,
                provider="openai_compatible",
                messages_count=len(merged_messages) - 1,
                system_chars=len(prompt or ""),
                messages_chars=messages_chars,
                total_prompt_chars=total_prompt_chars,
                estimated_input_tokens=int(
                    audit_extra.get("estimated_input_tokens")
                    or approx_tokens_from_chars(total_prompt_chars)
                ),
                reason=audit_extra.get("reason") or "openai_compatible_provider.call",
                intent=audit_extra.get("intent"),
                stage=audit_extra.get("stage"),
                channel=audit_extra.get("channel"),
                model_tier=audit_extra.get("model_tier"),
            )
            with httpx.Client(timeout=_TIMEOUT) as client:
                resp = client.post(
                    f"{_API_BASE}/chat/completions",
                    headers=headers,
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()

            reply = data["choices"][0]["message"]["content"].strip()
            logger.info(
                "[engine] Modular path used — OpenAI-compatible | "
                "provider=openai_compatible model=%s reply_len=%d",
                model, len(reply),
            )
            record_ai_usage_from_openai_compatible(
                audit_extra=audit_extra,
                model=model,
                httpx_data=data,
                reply_text=reply,
                total_prompt_chars=total_prompt_chars,
            )
            return {
                "provider":   "openai_compatible",
                "model":      model,
                "reply_text": reply,
                "status":     "ok",
            }

        except Exception as exc:
            logger.warning(
                "[engine] OpenAI-compatible: error %r — "
                "returning empty reply_text (fallback triggered)", exc
            )
            return {
                "provider":   "openai_compatible",
                "model":      _MODEL,
                "reply_text": "",
                "status":     "call_error",
            }


def _merge_history(
    history: Optional[List[Dict[str, Any]]],
    message: str,
) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    for item in history or []:
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        merged.append({"role": role, "content": content})
    if not merged or merged[-1]["role"] != "user":
        merged.append({"role": "user", "content": message})
    elif merged[-1]["content"] != message:
        merged.append({"role": "user", "content": message})
    return merged
