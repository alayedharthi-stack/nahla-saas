"""
backend/modules/ai/orchestrator/providers/gemini_provider.py
─────────────────────────────────────────────────────────────
Google Gemini provider implementation for the Nahla orchestration engine.

Uses the Gemini generateContent REST API (v1beta).
No SDK dependency — pure httpx call consistent with the other providers.

Configuration (via environment variables only — no hardcoded values):
  GEMINI_API_KEY  : Google AI API key
  GEMINI_MODEL    : model name (default: gemini-1.5-flash)

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

from modules.ai.orchestrator.ai_usage_ledger import (
    extract_provider_completion_telemetry,
    record_ai_usage_from_gemini,
)
from modules.ai.orchestrator.llm_cost_audit import approx_tokens_from_chars, emit_llm_cost_audit
from modules.ai.orchestrator.providers.base import BaseAIProvider

logger = logging.getLogger("nahla.ai.orchestrator.engine")  # same logger as engine

# ── Configuration (read once at module import) ─────────────────────────────────
_API_KEY = os.environ.get("GEMINI_API_KEY", "")
_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_TIMEOUT  = 25.0


class GeminiProvider(BaseAIProvider):
    """
    Google Gemini provider via the generateContent REST API.

    Stateless — safe to share a single instance across requests.
    Implements the same BaseAIProvider interface as AnthropicProvider
    and OpenAICompatibleProvider.
    """

    @property
    def provider_name(self) -> str:
        return "gemini"

    def is_configured(self) -> bool:
        """Return True when GEMINI_API_KEY is set."""
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
        Call the Gemini generateContent API synchronously.

        Returns a dict with reply_text, provider, model, status.
        Never raises — empty reply_text signals failure to the engine.

        The system prompt is passed as a system_instruction and the
        customer message as the user turn, matching Gemini's content format.
        """
        if not _API_KEY:
            logger.info(
                "[engine] GEMINI_API_KEY not set — gemini provider "
                "returning empty reply_text"
            )
            return {
                "provider":   "gemini",
                "model":      _MODEL,
                "reply_text": "",
                "status":     "no_api_key",
            }

        try:
            import httpx
        except ImportError:
            logger.warning(
                "[engine] httpx not available — gemini provider "
                "returning empty reply_text (fallback triggered)"
            )
            return {
                "provider":   "gemini",
                "model":      _MODEL,
                "reply_text": "",
                "status":     "no_http_client",
            }

        try:
            url = f"{_API_BASE}/{_MODEL}:generateContent?key={_API_KEY}"
            contents = _merge_history(history, message)
            body: Dict[str, Any] = {
                "system_instruction": {
                    "parts": [{"text": prompt}]
                },
                "contents": contents,
                "generationConfig": {
                    "maxOutputTokens": 1024,
                    "temperature":     0.7,
                },
            }
            audit_extra = dict(audit_context or {})
            messages_chars = sum(
                len(str((part or {}).get("text") or ""))
                for item in contents
                for part in (item.get("parts") or [])
            )
            total_prompt_chars = len(prompt or "") + messages_chars
            emit_llm_cost_audit(
                tenant_id=audit_extra.get("tenant_id"),
                conversation_id=audit_extra.get("conversation_id"),
                turn_id=audit_extra.get("turn_id"),
                model=_MODEL,
                provider="gemini",
                messages_count=len(contents),
                system_chars=len(prompt or ""),
                messages_chars=messages_chars,
                total_prompt_chars=total_prompt_chars,
                estimated_input_tokens=int(
                    audit_extra.get("estimated_input_tokens")
                    or approx_tokens_from_chars(total_prompt_chars)
                ),
                reason=audit_extra.get("reason") or "gemini_provider.call",
                intent=audit_extra.get("intent"),
                stage=audit_extra.get("stage"),
                channel=audit_extra.get("channel"),
            )
            with httpx.Client(timeout=_TIMEOUT) as client:
                resp = client.post(url, json=body)
                resp.raise_for_status()
                data = resp.json()

            reply = (
                data["candidates"][0]["content"]["parts"][0]["text"].strip()
            )
            logger.info(
                "[engine] Modular path used — Gemini | "
                "provider=gemini model=%s reply_len=%d",
                _MODEL, len(reply),
            )
            record_ai_usage_from_gemini(
                audit_extra=audit_extra,
                model=_MODEL,
                httpx_data=data,
                reply_text=reply,
                total_prompt_chars=total_prompt_chars,
            )
            return {
                "provider":   "gemini",
                "model":      _MODEL,
                "reply_text": reply,
                "status":     "ok",
                "completion_telemetry": extract_provider_completion_telemetry(
                    reply_text=reply, httpx_data=data,
                ),
            }

        except Exception as exc:
            logger.warning(
                "[engine] Gemini: error %r — "
                "returning empty reply_text (fallback triggered)", exc
            )
            return {
                "provider":   "gemini",
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
        merged.append({"role": role, "parts": [{"text": content}]})
    if not merged or merged[-1]["role"] != "user":
        merged.append({"role": "user", "parts": [{"text": message}]})
    elif (merged[-1].get("parts") or [{}])[0].get("text") != message:
        merged.append({"role": "user", "parts": [{"text": message}]})
    return merged
