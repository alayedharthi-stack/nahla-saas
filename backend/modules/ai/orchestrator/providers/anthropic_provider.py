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

from modules.ai.orchestrator.anthropic_exception_diagnostics import (
    anthropic_exception_diagnostics,
)
from modules.ai.orchestrator.llm_cost_audit import (
    emit_llm_cost_audit,
    approx_tokens_from_chars,
    resolve_anthropic_model,
    resolve_model_from_audit,
    resolve_model_for_provider,
)
from modules.ai.orchestrator.ai_usage_ledger import record_ai_usage_from_anthropic
from modules.ai.orchestrator.providers.base import BaseAIProvider

logger = logging.getLogger("nahla.ai.orchestrator.engine")  # same logger as engine

# ── Provider configuration ─────────────────────────────────────────────────────
_API_KEY  = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY", "")
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

    def call_messages(
        self,
        messages: List[Dict[str, Any]],
        prompt: str,
        *,
        audit_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Call Claude with a full message history.

        This is used by legacy compatibility callers that still need multi-turn
        conversation continuity but should no longer own Anthropic execution
        logic directly.
        """
        return self._call_internal(
            messages=messages, prompt=prompt, audit_context=audit_context,
        )

    def call_with_tools(
        self,
        *,
        message: str,
        prompt: str,
        tools: List[Dict[str, Any]],
        tool_choice: str = "auto",
        history: Optional[List[Dict[str, Any]]] = None,
        audit_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Call Claude with native tool use enabled.

        Used by the legacy orchestrator compatibility shell so tool/action
        proposals stay compatible while Anthropic execution remains canonical
        under modules.ai.orchestrator.providers.
        """
        return self._call_internal(
            messages=_merge_history(history, message),
            prompt=prompt,
            tools=tools,
            tool_choice=tool_choice,
            audit_context=audit_context,
        )

    def call(
        self,
        message: str,
        prompt: str,
        *,
        history: Optional[List[Dict[str, Any]]] = None,
        audit_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Call Claude synchronously.

        Returns a dict with reply_text, provider, model, status.
        Never raises — empty reply_text signals failure to the engine.
        """
        return self._call_internal(
            messages=_merge_history(history, message),
            prompt=prompt,
            audit_context=audit_context,
        )

    def call_single_step(
        self,
        *,
        messages: List[Dict[str, Any]],
        system: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Dict[str, Any]] = None,
        max_tokens: int = 1024,
        timeout_seconds: Optional[float] = None,
        audit_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Run exactly one Anthropic inference step and report it structurally.

        This exists for callers that own their own loop and therefore need what
        :meth:`_call_internal` deliberately flattens away: the tool-use block
        ids, the stop reason and the usage, each reported as it arrived or as
        explicitly absent. It runs **one** request: SDK-level retries are turned
        off so the caller's own durable attempt accounting is the single
        authority on how often the model is asked, and the per-request timeout
        is the caller's, not the library default.

        Never raises. ``status`` is closed:
        ``ok``, ``no_api_key``, ``sdk_unavailable``, ``auth_error``,
        ``rate_limited``, ``overloaded``, ``timeout``, ``connection_error``,
        ``api_error``, ``sdk_error``.
        """
        if not _API_KEY:
            return {"provider": "none", "model": "none", "status": "no_api_key",
                    "stop_reason": None, "blocks": [], "usage": None, "error": None}
        if not _SDK_AVAILABLE:
            # A single structured step needs block ids and a stop reason; the raw
            # httpx path in this module is kept for the legacy flattened calls.
            return {"provider": "none", "model": "none", "status": "sdk_unavailable",
                    "stop_reason": None, "blocks": [], "usage": None, "error": None}

        # Deliberately not bound to a ``model``-named local: this module's single
        # fingerprinted model-selection statement is the one in ``_call_internal``,
        # and this call must resolve exactly what that one resolves rather than
        # become a second selection surface. The equality is asserted by
        # ``test_the_single_step_call_resolves_the_same_model_as_the_legacy_path``.
        resolved = resolve_model_for_provider(
            audit_context, provider="anthropic", default=resolve_anthropic_model(),
        )
        system_chars = len(system or "")
        messages_chars = sum(len(str(m.get("content") or "")) for m in messages)
        total_prompt_chars = system_chars + messages_chars
        audit_extra = dict(audit_context or {})
        emit_llm_cost_audit(
            tenant_id=audit_extra.get("tenant_id"),
            conversation_id=audit_extra.get("conversation_id"),
            turn_id=audit_extra.get("turn_id"),
            model=resolved,
            provider="anthropic",
            messages_count=len(messages),
            system_chars=system_chars,
            messages_chars=messages_chars,
            history_chars=audit_extra.get("history_chars", messages_chars),
            tools_chars=audit_extra.get("tools_chars"),
            total_prompt_chars=total_prompt_chars,
            estimated_input_tokens=approx_tokens_from_chars(total_prompt_chars),
            reason=audit_extra.get("reason") or "anthropic_provider.call_single_step",
            intent=audit_extra.get("intent"),
            stage=audit_extra.get("stage"),
            channel=audit_extra.get("channel"),
            model_tier=audit_extra.get("model_tier"),
        )

        def failure(status: str, exc: Optional[BaseException] = None) -> Dict[str, Any]:
            if exc is not None:
                logger.warning(
                    "[engine] Claude single step: %s diagnostics=%s", status,
                    anthropic_exception_diagnostics(exc),
                )
            return {"provider": "anthropic", "model": resolved, "status": status,
                    "stop_reason": None, "blocks": [], "usage": None,
                    "error": type(exc).__name__ if exc is not None else None}

        try:
            client_kwargs: Dict[str, Any] = {"api_key": _API_KEY, "max_retries": 0}
            if timeout_seconds is not None:
                client_kwargs["timeout"] = float(timeout_seconds)
            client = _anthropic_sdk.Anthropic(**client_kwargs)
            request_body: Dict[str, Any] = {
                "model": resolved,
                "max_tokens": int(max_tokens),
                "system": system,
                "messages": messages,
            }
            if tools:
                request_body["tools"] = tools
                if tool_choice:
                    request_body["tool_choice"] = dict(tool_choice)
            response = client.messages.create(**request_body)
        except _anthropic_sdk.AuthenticationError as exc:
            return failure("auth_error", exc)
        except _anthropic_sdk.RateLimitError as exc:
            return failure("rate_limited", exc)
        except _anthropic_sdk.APITimeoutError as exc:
            return failure("timeout", exc)
        except _anthropic_sdk.APIConnectionError as exc:
            return failure("connection_error", exc)
        except _anthropic_sdk.APIStatusError as exc:
            status = "overloaded" if getattr(exc, "status_code", None) == 529 else "api_error"
            return failure(status, exc)
        except Exception as exc:  # noqa: BLE001 - a step is an outcome, never a crash
            return failure("sdk_error", exc)

        blocks: List[Dict[str, Any]] = []
        reply_chars = 0
        for block in getattr(response, "content", None) or []:
            kind = getattr(block, "type", "")
            if kind == "tool_use":
                # The input is reported exactly as it arrived. Coercing a
                # falsy value to {} would turn None, [], "" or 0 into a
                # well-formed request to run the tool with its defaults; the
                # caller has to be able to tell malformed output apart from a
                # genuine no-argument call.
                blocks.append({
                    "type": "tool_use",
                    "id": getattr(block, "id", "") or "",
                    "name": getattr(block, "name", "") or "",
                    "input": getattr(block, "input", None),
                })
            elif kind == "text":
                text = getattr(block, "text", "") or ""
                reply_chars += len(text)
                blocks.append({"type": "text", "text": text})
            else:
                blocks.append({"type": str(kind or "unknown")})

        raw_usage = getattr(response, "usage", None)
        usage: Optional[Dict[str, Any]] = None
        if raw_usage is not None:
            usage = {
                "input_tokens": getattr(raw_usage, "input_tokens", None),
                "output_tokens": getattr(raw_usage, "output_tokens", None),
                "cache_read_input_tokens": getattr(raw_usage, "cache_read_input_tokens", None),
                "cache_creation_input_tokens": getattr(raw_usage, "cache_creation_input_tokens", None),
            }
        record_ai_usage_from_anthropic(
            audit_extra=audit_extra, model=resolved, response=response,
            reply_text="x" * reply_chars, total_prompt_chars=total_prompt_chars,
        )
        logger.info(
            "[engine] Claude single step | model=%s stop_reason=%s blocks=%d usage_present=%s",
            resolved, getattr(response, "stop_reason", None), len(blocks), usage is not None,
        )
        return {
            "provider": "anthropic",
            "model": resolved,
            "status": "ok",
            "stop_reason": getattr(response, "stop_reason", None),
            "blocks": blocks,
            "usage": usage,
            "request_id": getattr(response, "id", None),
            "error": None,
        }

    def _call_internal(
        self,
        *,
        messages: List[Dict[str, Any]],
        prompt: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        audit_context: Optional[Dict[str, Any]] = None,
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

        model = resolve_model_for_provider(
            audit_context,
            provider="anthropic",
            default=resolve_anthropic_model(),
        )
        system_chars = len(prompt or "")
        messages_chars = sum(len(str(m.get("content") or "")) for m in messages)
        total_prompt_chars = system_chars + messages_chars
        audit_extra = dict(audit_context or {})
        emit_llm_cost_audit(
            tenant_id=audit_extra.get("tenant_id"),
            conversation_id=audit_extra.get("conversation_id"),
            turn_id=audit_extra.get("turn_id"),
            model=model,
            provider="anthropic",
            messages_count=len(messages),
            system_chars=system_chars,
            messages_chars=messages_chars,
            brain_state_json_chars=audit_extra.get("brain_state_json_chars"),
            history_chars=audit_extra.get("history_chars", messages_chars),
            kb_chars=audit_extra.get("kb_chars"),
            catalog_chars=audit_extra.get("catalog_chars"),
            product_context_chars=audit_extra.get("product_context_chars"),
            tools_chars=audit_extra.get("tools_chars"),
            total_prompt_chars=total_prompt_chars,
            estimated_input_tokens=approx_tokens_from_chars(total_prompt_chars),
            reason=audit_extra.get("reason") or "anthropic_provider._call_internal",
            intent=audit_extra.get("intent"),
            stage=audit_extra.get("stage"),
            channel=audit_extra.get("channel"),
            model_tier=audit_extra.get("model_tier"),
        )

        # ── Path 1: Anthropic SDK (sync) ──────────────────────────────────────
        if _SDK_AVAILABLE:
            try:
                client = _anthropic_sdk.Anthropic(api_key=_API_KEY)
                request_body: Dict[str, Any] = {
                    "model":      model,
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
                    model, len(reply),
                )
                record_ai_usage_from_anthropic(
                    audit_extra=audit_extra,
                    model=model,
                    response=response,
                    reply_text=reply,
                    total_prompt_chars=total_prompt_chars,
                )
                return {
                    "provider":   "anthropic",
                    "model":      model,
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
                    "provider": "anthropic", "model": model,
                    "reply_text": "", "status": "auth_error", "actions": [],
                }
            except _anthropic_sdk.APIConnectionError as exc:
                logger.warning(
                    "[engine] Claude SDK: connection error diagnostics=%s — "
                    "returning empty reply_text (fallback triggered)",
                    anthropic_exception_diagnostics(exc),
                )
                return {
                    "provider": "anthropic", "model": model,
                    "reply_text": "", "status": "connection_error", "actions": [],
                }
            except Exception as exc:
                logger.warning(
                    "[engine] Claude SDK: unexpected error diagnostics=%s — "
                    "returning empty reply_text (fallback triggered)",
                    anthropic_exception_diagnostics(exc),
                )
                return {
                    "provider": "anthropic", "model": model,
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
                "model":      model,
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
                model, len(reply),
            )
            record_ai_usage_from_anthropic(
                audit_extra=audit_extra,
                model=model,
                httpx_data=data,
                reply_text=reply,
                total_prompt_chars=total_prompt_chars,
            )
            return {
                "provider":   "anthropic",
                "model":      model,
                "reply_text": reply,
                "status":     "ok",
                "actions":    actions,
            }

        except Exception as exc:
            logger.warning(
                "[engine] Claude httpx: error diagnostics=%s — "
                "returning empty reply_text (fallback triggered)",
                anthropic_exception_diagnostics(exc),
            )
            return {
                "provider": "anthropic", "model": model,
                "reply_text": "", "status": "httpx_error", "actions": [],
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
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += f"\n{content}"
        else:
            merged.append({"role": role, "content": content})

    if not merged or merged[-1]["role"] != "user":
        merged.append({"role": "user", "content": message})
    elif merged[-1]["content"] != message:
        merged.append({"role": "user", "content": message})
    return merged
