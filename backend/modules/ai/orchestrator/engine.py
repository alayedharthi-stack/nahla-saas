"""
backend/modules/ai/orchestrator/engine.py
──────────────────────────────────────────
Canonical AI orchestration engine for the modular monolith.

Responsibilities:
  - build the final prompt (via modules.ai.prompts.builder)
  - delegate provider execution using provider_chain order when available
  - return a normalised AIReplyPayload

Provider routing (OpenAI-only customer chat):
  When provider_chain is supplied, the engine attempts providers in chain order
  (default: openai_compatible only). On technical failure, escalates models
  Luna → Terra → Sol (Sol gated by ALLOW_PREMIUM_MODEL). No silent Anthropic
  or Gemini fallback.

External API surface: unchanged.
Webhook / runtime paths: unchanged.
"""
from __future__ import annotations

import inspect
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
from modules.ai.orchestrator.customer_chat_models import (
    customer_chat_provider,
    emit_customer_chat_model_telemetry,
    technical_escalation_models,
)
from modules.ai.orchestrator.types import AIOrchestrationRequest, AIReplyPayload

# Per-provider call timeout used inside _call_with_chain.
# Can be overridden via AI_PROVIDER_TIMEOUT env var (resilience.py reads it).
_PROVIDER_TIMEOUT: float = DEFAULT_TIMEOUT

logger = logging.getLogger("nahla.ai.orchestrator.engine")


def _supports_audit_context(provider: Any, method_name: str) -> bool:
    """True when ``provider.<method_name>`` accepts ``audit_context``."""
    method = getattr(provider, method_name, None)
    if method is None:
        return False
    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):
        return False
    return "audit_context" in sig.parameters


def _audit_kwargs(
    provider: Any,
    method_name: str,
    audit_context: Dict[str, Any],
) -> Dict[str, Any]:
    if audit_context and _supports_audit_context(provider, method_name):
        return {"audit_context": audit_context}
    return {}


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
    Currently resolves openai_compatible as the default provider for customer chat.
    """

    def __init__(self) -> None:
        provider = get_provider("openai_compatible")
        assert provider is not None, "OpenAICompatibleProvider not found in provider registry"
        self._provider = provider

    # ── Provider chain execution ───────────────────────────────────────────────

    def _call_with_chain(
        self,
        request: AIOrchestrationRequest | str,
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

        If every provider in the chain fails or is unavailable, attempt technical
        model escalation (Luna → Terra → gated Sol) within openai_compatible.
        Never falls back to Anthropic or Gemini.

        Never raises.
        """
        observer = ChainObserver(provider_chain.providers)
        request_obj = self._coerce_request(request)
        audit_context = self._audit_context_from_request(request_obj)
        router_meta = dict(request_obj.prompt_overrides.get("__model_router") or {})
        block_anthropic_fallback = bool(router_meta.get("block_anthropic_fallback"))
        router_tier = str(router_meta.get("tier") or "").strip().lower()
        if router_tier == "cheap":
            block_anthropic_fallback = True

        for provider_name in provider_chain.providers:
            if provider_name in {"anthropic", "gemini"}:
                logger.info(
                    "[engine] provider_chain: skipping %s — "
                    "excluded from customer chat path",
                    provider_name,
                )
                observer.record_skipped(provider_name, "excluded_customer_chat")
                continue
            if block_anthropic_fallback and provider_name == "anthropic":
                logger.info(
                    "[engine] provider_chain: skipping anthropic — "
                    "blocked for routine commerce"
                )
                observer.record_skipped(provider_name, "blocked_routine_commerce")
                continue

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
            _history = [
                {"role": msg.role, "content": msg.content}
                for msg in request_obj.history
            ]
            raw = call_with_resilience(
                provider_name,
                (
                    lambda p=provider: p.call_with_tools(
                        message=request_obj.message,
                        prompt=prompt,
                        tools=request_obj.tool_definitions,
                        tool_choice="auto",
                        history=_history,
                        **_audit_kwargs(p, "call_with_tools", audit_context),
                    )
                    if request_obj.tool_definitions and hasattr(p, "call_with_tools")
                    else p.call(
                        request_obj.message,
                        prompt,
                        history=_history,
                        **_audit_kwargs(p, "call", audit_context),
                    )
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
                if provider_chain.providers and provider_name != provider_chain.providers[0]:
                    raw["provider_chain_fallback_used"] = True
                    raw["provider_chain_primary"] = provider_chain.providers[0]
                else:
                    raw["provider_chain_fallback_used"] = False
                _actual_model = str(raw.get("model") or audit_context.get("model_override") or "")
                emit_customer_chat_model_telemetry(
                    provider=provider_name,
                    requested_model=str(
                        router_meta.get("model") or audit_context.get("model_override") or ""
                    ),
                    actual_model=_actual_model,
                    escalation_reason="",
                    tenant_id=audit_context.get("tenant_id"),
                    conversation_id=audit_context.get("conversation_id"),
                    turn_id=audit_context.get("turn_id"),
                )
                return raw

            logger.info(
                "[engine] provider_chain: %s returned empty — falling through",
                provider_name,
            )
            observer.record_call(provider_name, _duration_ms, "empty_reply")

        requested_model = str(
            audit_context.get("model_override")
            or router_meta.get("model")
            or ""
        ).strip()
        escalated = self._try_technical_model_escalation(
            request_obj=request_obj,
            prompt=prompt,
            observer=observer,
            audit_context=audit_context,
            requested_model=requested_model,
        )
        if escalated.get("reply_text"):
            return escalated

        logger.info(
            "[engine] provider_chain: all providers exhausted — "
            "no cross-provider fallback (OpenAI-only path)"
            if block_anthropic_fallback
            else "controlled failure (no Anthropic/Gemini fallback)"
        )
        observer.finalize(final_provider=None, fallback_used=False)
        emit_customer_chat_model_telemetry(
            provider=customer_chat_provider(),
            requested_model=requested_model,
            actual_model=requested_model or None,
            escalation_reason="openai_chain_exhausted",
            tenant_id=audit_context.get("tenant_id"),
            conversation_id=audit_context.get("conversation_id"),
            turn_id=audit_context.get("turn_id"),
            extra={"status": "openai_chain_exhausted"},
        )
        exhausted: Dict[str, Any] = {
            "reply_text": "",
            "provider": "" if block_anthropic_fallback else self._provider.provider_name,
            "model": requested_model,
            "status": "openai_chain_exhausted",
        }
        if block_anthropic_fallback:
            exhausted["anthropic_fallback_blocked"] = True
        return exhausted

    def _try_technical_model_escalation(
        self,
        *,
        request_obj: AIOrchestrationRequest,
        prompt: str,
        observer: ChainObserver,
        audit_context: Dict[str, Any],
        requested_model: str,
    ) -> Dict[str, Any]:
        """Luna → Terra → gated Sol on technical failure within openai_compatible."""
        from modules.ai.orchestrator.customer_chat_models import customer_chat_provider

        if not self._provider.is_configured():
            return {"reply_text": ""}

        for escalation_model in technical_escalation_models(requested_model):
            escalation_audit = dict(audit_context)
            escalation_audit["model_override"] = escalation_model
            logger.info(
                "[engine] technical model escalation: %s → %s",
                requested_model or "(default)",
                escalation_model,
            )
            _t0 = time.monotonic()
            raw = self._invoke_provider_call(
                request_obj,
                prompt,
                provider=self._provider,
                audit_context=escalation_audit,
            )
            _duration_ms = (time.monotonic() - _t0) * 1000
            if raw and raw.get("reply_text"):
                observer.record_call(
                    f"{self._provider.provider_name}({escalation_model})",
                    _duration_ms,
                    "succeeded",
                )
                observer.finalize(
                    final_provider=self._provider.provider_name,
                    fallback_used=True,
                )
                raw["provider_chain_fallback_used"] = True
                raw["model_escalation"] = True
                raw["requested_model"] = requested_model
                raw["actual_model"] = str(raw.get("model") or escalation_model)
                emit_customer_chat_model_telemetry(
                    provider=customer_chat_provider(),
                    requested_model=requested_model,
                    actual_model=raw["actual_model"],
                    escalation_reason="technical_failure",
                    tenant_id=audit_context.get("tenant_id"),
                    conversation_id=audit_context.get("conversation_id"),
                    turn_id=audit_context.get("turn_id"),
                )
                return raw
            observer.record_call(
                f"{self._provider.provider_name}({escalation_model})",
                _duration_ms,
                "failed",
            )
        return {"reply_text": ""}

    def _invoke_provider_call(
        self,
        request_obj: AIOrchestrationRequest,
        prompt: str,
        *,
        provider: Any,
        audit_context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        _history = [
            {"role": msg.role, "content": msg.content}
            for msg in request_obj.history
        ]
        return call_with_resilience(
            provider.provider_name,
            (
                lambda p=provider: p.call_with_tools(
                    message=request_obj.message,
                    prompt=prompt,
                    tools=request_obj.tool_definitions,
                    tool_choice="auto",
                    history=_history,
                    **_audit_kwargs(p, "call_with_tools", audit_context),
                )
                if request_obj.tool_definitions and hasattr(p, "call_with_tools")
                else p.call(
                    request_obj.message,
                    prompt,
                    history=_history,
                    **_audit_kwargs(p, "call", audit_context),
                )
            ),
            timeout=_PROVIDER_TIMEOUT,
        )

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

    def _audit_context_from_request(
        self, request: AIOrchestrationRequest,
    ) -> Dict[str, Any]:
        audit = dict(request.prompt_overrides.get("__llm_cost_audit") or {})
        if not audit.get("tenant_id"):
            audit["tenant_id"] = request.context.tenant_id
        if not audit.get("channel"):
            audit["channel"] = request.context.channel
        return audit

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
        audit_context = self._audit_context_from_request(request)
        if request.tool_definitions and hasattr(self._provider, "call_with_tools"):
            return self._provider.call_with_tools(
                message=request.message,
                prompt=prompt,
                tools=request.tool_definitions,
                tool_choice="auto",
                history=[{"role": msg.role, "content": msg.content} for msg in request.history],
                **_audit_kwargs(self._provider, "call_with_tools", audit_context),
            )
        return self._provider.call(
            request.message,
            prompt,
            history=[{"role": msg.role, "content": msg.content} for msg in request.history],
            **_audit_kwargs(self._provider, "call", audit_context),
        )

    def _coerce_request(self, request: AIOrchestrationRequest | str) -> AIOrchestrationRequest:
        if isinstance(request, AIOrchestrationRequest):
            return request
        from modules.ai.orchestrator.types import AIContext

        return AIOrchestrationRequest(
            context=AIContext(channel="system"),
            message=str(request),
        )

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
                "provider_chain_fallback_used": bool(
                    raw.get("provider_chain_fallback_used")
                ),
                "finish_reason": (
                    (raw.get("completion_telemetry") or {}).get("finish_reason")
                    if isinstance(raw.get("completion_telemetry"), dict)
                    else None
                ),
                "output_tokens": (
                    (raw.get("completion_telemetry") or {}).get("output_tokens")
                    if isinstance(raw.get("completion_telemetry"), dict)
                    else None
                ),
                "raw_char_count": (
                    (raw.get("completion_telemetry") or {}).get("raw_char_count")
                    if isinstance(raw.get("completion_telemetry"), dict)
                    else len(reply_text)
                ),
                "completion_telemetry": (
                    dict(raw.get("completion_telemetry") or {})
                    if isinstance(raw.get("completion_telemetry"), dict)
                    else {}
                ),
            },
        )
