"""
backend/modules/ai/orchestrator/engine.py
──────────────────────────────────────────
Canonical AI orchestration engine for the modular monolith.

Responsibilities:
  - build the final prompt (via modules.ai.prompts.builder)
  - delegate provider execution using provider_chain order when available
  - return a normalised AIReplyPayload

Provider routing (now active):
  When provider_chain is supplied by the pipeline, the engine attempts
  providers in chain order:
    anthropic → openai_compatible → gemini
  The first provider that returns a non-empty reply wins.
  Unconfigured providers are skipped.  If all fail, the engine falls back
  to self._provider (Anthropic) to preserve existing fallback semantics.

  When provider_chain is absent, self._provider (Anthropic) is called
  directly — identical to the pre-activation behavior.

External API surface: unchanged.
Webhook / runtime paths: unchanged.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

from modules.ai.prompts.builder import build_system_prompt
from modules.ai.orchestrator.costing import estimate_call_cost
from modules.ai.orchestrator.observability import ChainObserver
from modules.ai.orchestrator.prompt_versioning import fingerprint_prompt
from modules.ai.orchestrator.provider_router import ProviderChainConfig
from modules.ai.orchestrator.providers.registry import get_provider
from modules.ai.orchestrator.providers.resilience import (
    DEFAULT_TIMEOUT,
    call_with_resilience,
)
from modules.ai.orchestrator.types import AIOrchestrationRequest, AIReplyPayload

# Per-provider call timeout used inside _call_with_chain.
# Can be overridden via AI_PROVIDER_TIMEOUT env var (resilience.py reads it).
_PROVIDER_TIMEOUT: float = DEFAULT_TIMEOUT

logger = logging.getLogger("nahla.ai.orchestrator.engine")


class AIOrchestratorEngine:
    """
    Single facade for LLM execution used by AIOrchestrationPipeline.

    Call chain:
      generate_reply(request)
          ↓
      build_prompt(request)      — via modules.ai.prompts.builder
          ↓
      call_provider(request, prompt) — delegates to self._provider.call()
          ↓
      AIReplyPayload

    The engine no longer contains provider-specific logic and no longer
    imports concrete provider classes directly.  It resolves the active
    provider through the provider registry at initialisation time.
    Currently "anthropic" is the only registered provider.
    """

    def __init__(self) -> None:
        provider = get_provider("anthropic")
        assert provider is not None, "AnthropicProvider not found in provider registry"
        self._provider = provider   # default / fallback provider (always Anthropic)

    # ── Provider chain execution ───────────────────────────────────────────────

    def _call_with_chain(
        self,
        request: AIOrchestrationRequest,
        prompt: str,
        provider_chain: ProviderChainConfig,
    ) -> Dict[str, Any]:
        """
        Attempt providers in provider_chain order, returning on first success.

        For each provider name in the chain:
          1. Resolve from registry — skip if not registered.
          2. Check is_configured() — skip if unconfigured.
          3. Call provider.call(message, prompt) or provider.call_with_tools(...)
             when tool_definitions are present and supported.
          4. If reply_text is non-empty, return immediately (success).
          5. If empty, log and continue to the next provider.

        If every provider in the chain fails or is unavailable, fall back to
        self._provider.call() (Anthropic) to preserve existing error semantics.

        Never raises.
        """
        observer = ChainObserver(provider_chain.providers)

        for provider_name in provider_chain.providers:
            provider = get_provider(provider_name)

            if provider is None:
                logger.debug(
                    "[engine] provider_chain: %s not in registry — skipping",
                    provider_name,
                )
                observer.record_skipped(provider_name, "skipped_not_registered")
                continue

            if not provider.is_configured():
                logger.debug(
                    "[engine] provider_chain: %s not configured — skipping",
                    provider_name,
                )
                observer.record_skipped(provider_name, "skipped_not_configured")
                continue

            logger.debug(
                "[engine] provider_chain: attempting %s (timeout=%.1fs)",
                provider_name, _PROVIDER_TIMEOUT,
            )
            _t0 = time.monotonic()
            raw = call_with_resilience(
                provider_name,
                (
                    lambda p=provider: p.call_with_tools(
                        message=request.message,
                        prompt=prompt,
                        tools=request.tool_definitions,
                        tool_choice="auto",
                    )
                    if request.tool_definitions and hasattr(p, "call_with_tools")
                    else p.call(request.message, prompt)
                ),
                timeout=_PROVIDER_TIMEOUT,
            )
            _duration_ms = (time.monotonic() - _t0) * 1000

            if raw is None:
                # Circuit open, timeout, or exception — already logged by resilience
                logger.info(
                    "[engine] provider_chain: %s skipped by resilience — "
                    "falling through",
                    provider_name,
                )
                observer.record_call(provider_name, _duration_ms, "failed")
                continue

            if raw.get("reply_text"):
                logger.info(
                    "[engine] provider_chain: %s succeeded | reply_len=%d",
                    provider_name, len(raw["reply_text"]),
                )
                observer.record_call(provider_name, _duration_ms, "succeeded")
                observer.finalize(final_provider=provider_name, fallback_used=False)
                return raw

            logger.info(
                "[engine] provider_chain: %s returned empty — falling through",
                provider_name,
            )
            observer.record_call(provider_name, _duration_ms, "empty_reply")

        # All chain providers exhausted — preserve existing fallback semantics
        logger.debug(
            "[engine] provider_chain: all providers exhausted — "
            "using default provider fallback (anthropic)"
        )
        _t0 = time.monotonic()
        if request.tool_definitions and hasattr(self._provider, "call_with_tools"):
            result = self._provider.call_with_tools(
                message=request.message,
                prompt=prompt,
                tools=request.tool_definitions,
                tool_choice="auto",
            )
        else:
            result = self._provider.call(request.message, prompt)
        _duration_ms = (time.monotonic() - _t0) * 1000
        _fallback_status = "succeeded" if result.get("reply_text") else "failed"
        observer.record_call(f"{self._provider.provider_name}(fallback)", _duration_ms, _fallback_status)
        observer.finalize(
            final_provider=self._provider.provider_name if result.get("reply_text") else None,
            fallback_used=True,
        )
        return result

    # ── Context adapter ───────────────────────────────────────────────────────

    def _request_to_prompt_context(self, request: AIOrchestrationRequest) -> Dict[str, Any]:
        """
        Convert AIOrchestrationRequest → context dict for build_system_prompt.

        Priority (lowest → highest):
          1. safe defaults from the prompt builder
          2. AIContext base fields (store_name, locale)
          3. AIContext.metadata — caller-supplied enrichment
          4. prompt_overrides  — explicit per-call overrides
        """
        ctx: Dict[str, Any] = {
            "store_name":         request.context.store_name or "our store",
            "preferred_language": request.context.locale or "ar",
        }
        ctx.update(request.context.metadata)
        ctx.update(request.prompt_overrides)
        return ctx

    # ── Prompt building ───────────────────────────────────────────────────────

    def build_prompt(self, request: AIOrchestrationRequest) -> str:
        """
        Build the final system prompt.

        Internal compatibility escape hatch:
        If prompt_overrides contains `__full_system_prompt`, that exact prompt
        text is used as-is. This lets transitional callers preserve legacy
        prompt semantics while still routing execution through the canonical
        adapter -> pipeline -> engine -> provider stack.
        """
        full_prompt = request.prompt_overrides.get("__full_system_prompt")
        if isinstance(full_prompt, str) and full_prompt.strip():
            return full_prompt

        ctx = self._request_to_prompt_context(request)
        return build_system_prompt(ctx)

    # ── Provider call ─────────────────────────────────────────────────────────

    def call_provider(
        self,
        request: AIOrchestrationRequest,
        prompt: str,
        provider_chain: Optional[ProviderChainConfig] = None,
    ) -> Dict[str, Any]:
        """
        Execute a provider call, with optional chain-based routing.

        When provider_chain is supplied: delegates to _call_with_chain(),
        which attempts providers in order and falls back to Anthropic.

        When provider_chain is absent: calls self._provider (Anthropic)
        directly — identical to the pre-activation behavior.

        Returns a dict with reply_text, provider, model, status.
        Never raises — empty reply_text triggers legacy fallback in ai-engine.
        """
        if provider_chain is not None:
            return self._call_with_chain(request, prompt, provider_chain)
        if request.tool_definitions and hasattr(self._provider, "call_with_tools"):
            return self._provider.call_with_tools(
                message=request.message,
                prompt=prompt,
                tools=request.tool_definitions,
                tool_choice="auto",
            )
        return self._provider.call(request.message, prompt)

    # ── Reply generation ──────────────────────────────────────────────────────

    def generate_reply(
        self,
        request: AIOrchestrationRequest,
        provider_chain: Optional[ProviderChainConfig] = None,
    ) -> AIReplyPayload:
        """
        Build prompt and call provider.

        Parameters
        ----------
        request        : canonical orchestration request
        provider_chain : optional routing config from the pipeline layer.
                         When present, providers are attempted in chain order.
                         When absent, self._provider (Anthropic) is used directly.

        Logging:
          - "[engine] provider_chain received: [...]" at DEBUG when chain present
          - "[engine] provider_chain: {name} skipped/attempted/succeeded/failed"
          - "[engine] generate_reply: real reply produced" when reply obtained
          - "[engine] generate_reply: empty reply_text" when fallback triggered
        """
        if provider_chain is not None:
            logger.debug(
                "[engine] provider_chain received: %s hint=%s — chain routing active",
                provider_chain.providers,
                provider_chain.hint,
            )

        prompt = self.build_prompt(request)
        raw    = self.call_provider(request, prompt, provider_chain=provider_chain)

        reply_text   = str(raw.get("reply_text", ""))
        provider_str = str(raw.get("provider", "unknown"))
        status       = raw.get("status", "unknown")

        cost_meta: Dict[str, Any] = {}
        prompt_meta_dict: Dict[str, Any] = {}

        # Fingerprint the prompt once — used for both success and empty-reply paths
        prompt_meta = fingerprint_prompt(prompt)

        if reply_text:
            logger.info(
                "[engine] generate_reply: real reply produced | "
                "provider=%s status=%s len=%d",
                provider_str, status, len(reply_text),
            )
            cost_meta = estimate_call_cost(
                provider=provider_str,
                model=raw.get("model", "unknown"),
                prompt_chars=len(prompt),
                reply_chars=len(reply_text),
            )
            logger.info(
                "[cost-est] provider=%s model=%s tokens~=%d "
                "est_cost=$%.6f bucket=%s",
                cost_meta["provider"],
                cost_meta["model"],
                cost_meta["est_total_tokens"],
                cost_meta["est_cost_usd"],
                cost_meta["cost_bucket"],
            )
            prompt_meta_dict = {
                "name":    prompt_meta.prompt_name,
                "version": prompt_meta.prompt_version,
                "hash":    prompt_meta.prompt_hash,
                "builder": prompt_meta.builder_source,
            }
            logger.info(
                "[prompt-meta] name=%s version=%s hash=%s",
                prompt_meta.prompt_name,
                prompt_meta.prompt_version,
                prompt_meta.prompt_hash,
            )
        else:
            logger.debug(
                "[engine] generate_reply: empty reply_text | "
                "provider=%s status=%s — caller fallback will run",
                provider_str, status,
            )

        return AIReplyPayload(
            reply_text=reply_text,
            provider_used=provider_str,   # type: ignore[arg-type]
            prompt_used=prompt,
            raw_model_output=raw,
            metadata={
                "status":         status,
                "prompt_builder": "modules.ai.prompts.builder.build_system_prompt",
                "provider":       provider_str,
                "model":          raw.get("model", "unknown"),
                "cost":           cost_meta,        # {} when no reply produced
                "prompt":         prompt_meta_dict, # {} when no reply produced
            },
        )
