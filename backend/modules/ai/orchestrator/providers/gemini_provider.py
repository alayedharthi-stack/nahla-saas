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
from typing import Any, Dict

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

    def call(self, message: str, prompt: str) -> Dict[str, Any]:
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
            body: Dict[str, Any] = {
                "system_instruction": {
                    "parts": [{"text": prompt}]
                },
                "contents": [
                    {"role": "user", "parts": [{"text": message}]}
                ],
                "generationConfig": {
                    "maxOutputTokens": 1024,
                    "temperature":     0.7,
                },
            }
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
            return {
                "provider":   "gemini",
                "model":      _MODEL,
                "reply_text": reply,
                "status":     "ok",
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
