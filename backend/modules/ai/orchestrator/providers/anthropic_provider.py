"""
backend/modules/ai/orchestrator/providers/anthropic_provider.py
────────────────────────────────────────────────────────────────
Anthropic (Claude) provider implementation for the Nahla orchestration engine.

This module contains the Anthropic-specific execution logic that was previously
inline in engine.call_provider(). It is the only provider implementation that
is currently active at runtime.

Execution path (same as before extraction):
  1. Anthropic SDK (sync client) — if SDK is installed AND ANTHROPIC_API_KEY set
  2. Raw httpx sync — if SDK missing but API key exists
  3. Returns reply_text="" — if no key or any call fails (engine falls back)

Log messages are kept identical to the pre-extraction engine.call_provider()
messages so observability is not disturbed.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from modules.ai.orchestrator.providers.base import BaseAIProvider

logger = logging.getLogger("nahla.ai.orchestrator.engine")  # same logger as engine

# ── Provider configuration ─────────────────────────────────────────────────────
_API_KEY  = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY", "")
_MODEL    = os.environ.get("CLAUDE_MODEL", "claude-opus-4-6")
_API_BASE = "https://api.anthropic.com/v1"

# Try to load the Anthropic SDK (sync client preferred)
try:
    import anthropic as _anthropic_sdk
    _SDK_AVAILABLE = True
    logger.info("[engine] Anthropic SDK available — sync client will be used")
except ImportError:
    _SDK_AVAILABLE = False
    logger.info("[engine] Anthropic SDK not installed — sync httpx will be used")

# httpx needed for the raw fallback path even when SDK is available
try:
    import httpx as _httpx
except ImportError:
    _httpx = None  # type: ignore[assignment]


class AnthropicProvider(BaseAIProvider):
    """
    Anthropic (Claude) provider — currently the only active provider.

    Wraps the exact Anthropic execution logic that previously lived in
    AIOrchestratorEngine.call_provider(), with identical behavior.
    """

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def is_configured(self) -> bool:
        """Return True when ANTHROPIC_API_KEY (or CLAUDE_API_KEY) is set."""
        return bool(_API_KEY)

    def call_messages(self, messages: List[Dict[str, Any]], prompt: str) -> Dict[str, Any]:
        """
        Call Claude with a full message history.

        This is used by legacy compatibility callers that still need multi-turn
        conversation continuity but should no longer own Anthropic execution
        logic directly.
        """
        return self._call_internal(messages=messages, prompt=prompt)

    def call_with_tools(
        self,
        *,
        message: str,
        prompt: str,
        tools: List[Dict[str, Any]],
        tool_choice: str = "auto",
    ) -> Dict[str, Any]:
        """
        Call Claude with native tool use enabled.

        Used by the legacy orchestrator compatibility shell so tool/action
        proposals stay compatible while Anthropic execution remains canonical
        under modules.ai.orchestrator.providers.
        """
        return self._call_internal(
            messages=[{"role": "user", "content": message}],
            prompt=prompt,
            tools=tools,
            tool_choice=tool_choice,
        )

    def call(self, message: str, prompt: str) -> Dict[str, Any]:
        """
        Call Claude synchronously.

        Returns a dict with reply_text, provider, model, status.
        Never raises — empty reply_text signals failure to the engine.
        """
        return self._call_internal(
            messages=[{"role": "user", "content": message}],
            prompt=prompt,
        )

    def _call_internal(
        self,
        *,
        messages: List[Dict[str, Any]],
        prompt: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Shared Anthropic execution path.

        Supports:
        - simple one-turn text generation (`call`)
        - multi-turn message history (`call_messages`)
        - native tool use (`call_with_tools`)

        Returns a dict with the canonical provider fields plus optional
        `actions` when tool_use blocks are present.
        """
        if not _API_KEY:
            logger.info(
                "[engine] ANTHROPIC_API_KEY not set — returning empty reply_text "
                "(legacy fallback will run)"
            )
            return {
                "provider":   "none",
                "model":      "none",
                "reply_text": "",
                "status":     "no_api_key",
                "actions":    [],
            }

        # ── Path 1: Anthropic SDK (sync) ──────────────────────────────────────
        if _SDK_AVAILABLE:
            try:
                client = _anthropic_sdk.Anthropic(api_key=_API_KEY)
                request_body: Dict[str, Any] = {
                    "model":      _MODEL,
                    "max_tokens": 1024,
                    "system":     prompt,
                    "messages":   messages,
                }
                if tools:
                    request_body["tools"] = tools
                    request_body["tool_choice"] = {"type": tool_choice or "auto"}
                response = client.messages.create(**request_body)
                reply = ""
                actions: List[Dict[str, Any]] = []
                for block in response.content:
                    if getattr(block, "type", "") == "tool_use":
                        actions.append({
                            "type": getattr(block, "name", ""),
                            "payload": getattr(block, "input", {}) or {},
                        })
                    elif hasattr(block, "text") and block.text:
                        reply = block.text

                logger.info(
                    "[engine] Modular path used — Claude SDK%s | "
                    "provider=anthropic model=%s reply_len=%d",
                    " + tools" if tools else "",
                    _MODEL, len(reply),
                )
                return {
                    "provider":   "anthropic",
                    "model":      _MODEL,
                    "reply_text": reply,
                    "status":     "ok",
                    "actions":    actions,
                }

            except _anthropic_sdk.AuthenticationError:
                logger.warning(
                    "[engine] Claude SDK: authentication error — "
                    "returning empty reply_text (fallback triggered)"
                )
                return {
                    "provider": "anthropic", "model": _MODEL,
                    "reply_text": "", "status": "auth_error", "actions": [],
                }
            except _anthropic_sdk.APIConnectionError as exc:
                logger.warning(
                    "[engine] Claude SDK: connection error %r — "
                    "returning empty reply_text (fallback triggered)", exc
                )
                return {
                    "provider": "anthropic", "model": _MODEL,
                    "reply_text": "", "status": "connection_error", "actions": [],
                }
            except Exception as exc:
                logger.warning(
                    "[engine] Claude SDK: unexpected error %r — "
                    "returning empty reply_text (fallback triggered)", exc
                )
                return {
                    "provider": "anthropic", "model": _MODEL,
                    "reply_text": "", "status": "sdk_error", "actions": [],
                }

        # ── Path 2: raw httpx sync (SDK not installed) ────────────────────────
        if _httpx is None:
            logger.warning(
                "[engine] httpx not available and SDK not installed — "
                "returning empty reply_text (fallback triggered)"
            )
            return {
                "provider": "none", "model": "none",
                "reply_text": "", "status": "no_http_client", "actions": [],
            }

        try:
            headers = {
                "x-api-key":         _API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            }
            body: Dict[str, Any] = {
                "model":      _MODEL,
                "max_tokens": 1024,
                "system":     prompt,
                "messages":   messages,
            }
            if tools:
                body["tools"] = tools
                body["tool_choice"] = {"type": tool_choice or "auto"}
            with _httpx.Client(timeout=25.0) as client:
                resp = client.post(f"{_API_BASE}/messages", headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()

            reply = ""
            actions: List[Dict[str, Any]] = []
            for block in data.get("content", []):
                if block.get("type") == "tool_use":
                    actions.append({
                        "type": block.get("name", ""),
                        "payload": block.get("input", {}) or {},
                    })
                elif block.get("type") == "text":
                    reply = block.get("text", "")

            logger.info(
                "[engine] Modular path used — Claude httpx%s | "
                "provider=anthropic model=%s reply_len=%d",
                " + tools" if tools else "",
                _MODEL, len(reply),
            )
            return {
                "provider":   "anthropic",
                "model":      _MODEL,
                "reply_text": reply,
                "status":     "ok",
                "actions":    actions,
            }

        except Exception as exc:
            logger.warning(
                "[engine] Claude httpx: error %r — "
                "returning empty reply_text (fallback triggered)", exc
            )
            return {
                "provider": "anthropic", "model": _MODEL,
                "reply_text": "", "status": "httpx_error", "actions": [],
            }
