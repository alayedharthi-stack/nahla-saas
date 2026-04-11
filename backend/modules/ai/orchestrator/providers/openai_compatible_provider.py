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
  OPENAI_MODEL        : model name (default: gpt-4o-mini)

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
from typing import Any, Dict

from modules.ai.orchestrator.providers.base import BaseAIProvider

logger = logging.getLogger("nahla.ai.orchestrator.engine")  # same logger as engine

# ── Configuration (read once at module import) ─────────────────────────────────
_API_KEY  = os.environ.get("OPENAI_API_KEY", "")
_API_BASE = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
_MODEL    = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
_TIMEOUT  = 25.0


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

    def call(self, message: str, prompt: str) -> Dict[str, Any]:
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

        try:
            headers = {
                "Authorization": f"Bearer {_API_KEY}",
                "Content-Type":  "application/json",
            }
            body = {
                "model": _MODEL,
                "messages": [
                    {"role": "system",  "content": prompt},
                    {"role": "user",    "content": message},
                ],
                "max_tokens":  1024,
                "temperature": 0.7,
            }
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
                _MODEL, len(reply),
            )
            return {
                "provider":   "openai_compatible",
                "model":      _MODEL,
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
