"""FactBoundPersonaComposer — verified-facts phrasing for social surfaces."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Sequence

from core.tenant import merge_ai_defaults
from modules.ai.brain.cost.model_router_audit import TIER_TINY, _env_tier_default

from .compose_guards import apply_guards_or_fallback, apply_persona_compose_guards
from .facts_bundle import (
    PHASE2_SOCIAL_SURFACES,
    PERSONA_COMPOSER_SURFACES,
    PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
    PERSONA_SURFACE_CUSTOMER_CONDITIONAL_COUPON_ANSWER,
    PERSONA_SURFACE_PAYMENT_MEDIA_INTRO,
    PersonaComposeResult,
    PersonaConstraints,
    PersonaFactsBundle,
)
from .fallback_catalog import deterministic_fallback
from .prompts import build_system_prompt, build_user_prompt

logger = logging.getLogger("nahla.brain.persona.fact_bound_composer")

_LLMCallable = Callable[[PersonaFactsBundle], Awaitable[str]]

_PERSONA_COMPOSE_CALL_SITE = "brain.persona.fact_bound_composer"

_PERSONA_COMPOSE_TIMEOUT_ENV = "NAHLA_PERSONA_COMPOSE_TIMEOUT_SECONDS"
# Platform default validated at 8.0s for confined catalog persona compose.
_PERSONA_COMPOSE_TIMEOUT_DEFAULT = 8.0
_PERSONA_COMPOSE_TIMEOUT_MIN = 0.5
_PERSONA_COMPOSE_TIMEOUT_MAX = 10.0


def resolve_persona_compose_timeout_seconds(
    override: Optional[float] = None,
) -> float:
    """Resolve persona LLM timeout from explicit override, env, or platform default."""
    if override is not None:
        return _clamp_persona_compose_timeout(float(override))
    raw = os.environ.get(_PERSONA_COMPOSE_TIMEOUT_ENV, "").strip()
    if not raw:
        return _PERSONA_COMPOSE_TIMEOUT_DEFAULT
    try:
        return _clamp_persona_compose_timeout(float(raw))
    except ValueError:
        return _PERSONA_COMPOSE_TIMEOUT_DEFAULT


def _clamp_persona_compose_timeout(value: float) -> float:
    return max(
        _PERSONA_COMPOSE_TIMEOUT_MIN,
        min(_PERSONA_COMPOSE_TIMEOUT_MAX, float(value)),
    )


COMPOSE_ATTEMPT_PROVIDER_CALL = "provider_call"
COMPOSE_ATTEMPT_SKIPPED_UNCONFIGURED = "skipped_unconfigured"
COMPOSE_ATTEMPT_SKIPPED_NO_ROUTE = "skipped_no_route"

CLOSED_PERSONA_COMPOSE_ATTEMPTS = frozenset({
    COMPOSE_ATTEMPT_PROVIDER_CALL,
    COMPOSE_ATTEMPT_SKIPPED_UNCONFIGURED,
    COMPOSE_ATTEMPT_SKIPPED_NO_ROUTE,
})

ROUTE_SOURCE_INJECTED_CALLABLE = "injected_callable"
INJECTED_CALLABLE_PROVIDER = "injected_callable"

_SUPPORTED_PERSONA_PROVIDERS = frozenset({"openai_compatible", "anthropic"})


@dataclass(frozen=True)
class PersonaComposeModelRoute:
    provider: str
    model: str
    tier: str
    source: str


@dataclass(frozen=True)
class PersonaComposeRouteResolution:
    route: PersonaComposeModelRoute
    provider_configured: bool
    compose_attempt: str


def _infer_provider_for_model(model: str) -> str:
    name = str(model or "").strip().lower()
    if name.startswith("claude"):
        return "anthropic"
    return "openai_compatible"


def is_persona_compose_provider_configured(provider: str) -> bool:
    """Return True when the provider has credentials configured (no network I/O)."""
    provider_key = str(provider or "").strip().lower()
    if provider_key == "openai_compatible":
        from modules.ai.orchestrator.providers.openai_compatible_provider import (  # noqa: PLC0415
            OpenAICompatibleProvider,
        )

        return OpenAICompatibleProvider().is_configured()
    if provider_key == "anthropic":
        from modules.ai.orchestrator.providers.anthropic_provider import (  # noqa: PLC0415
            AnthropicProvider,
        )

        return AnthropicProvider().is_configured()
    return False


def _platform_default_persona_candidates() -> list[tuple[str, str]]:
    """Cost-ordered tiny-tier persona compose candidates for platform default."""
    tiny = _env_tier_default(TIER_TINY)
    preferred_provider = str(tiny.suggested_provider or "openai_compatible").strip().lower()
    preferred_model = str(tiny.suggested_model or "gpt-4o-mini")
    from modules.ai.orchestrator.llm_cost_audit import resolve_anthropic_model  # noqa: PLC0415

    anthropic_model = resolve_anthropic_model()
    openai_model = "gpt-4o-mini"

    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(provider: str, model: str) -> None:
        key = (str(provider or "").strip().lower(), str(model or "").strip())
        if key[0] and key[1] and key not in seen:
            seen.add(key)
            candidates.append(key)

    _add(preferred_provider, preferred_model)
    if preferred_provider == "openai_compatible":
        _add("anthropic", anthropic_model)
    elif preferred_provider == "anthropic":
        _add("openai_compatible", openai_model)
    else:
        _add("openai_compatible", openai_model)
        _add("anthropic", anthropic_model)
    return candidates


def resolve_injected_callable_route_resolution() -> PersonaComposeRouteResolution:
    """Synthetic route for injected/custom LLM callables — no deployment provider lookup."""
    return PersonaComposeRouteResolution(
        route=PersonaComposeModelRoute(
            provider=INJECTED_CALLABLE_PROVIDER,
            model="",
            tier=TIER_TINY,
            source=ROUTE_SOURCE_INJECTED_CALLABLE,
        ),
        provider_configured=True,
        compose_attempt=COMPOSE_ATTEMPT_PROVIDER_CALL,
    )


def resolve_persona_compose_route_resolution(
    bundle: PersonaFactsBundle,
) -> PersonaComposeRouteResolution:
    """Resolve persona compose route with provider availability semantics."""
    env_model = os.environ.get("NAHLA_PERSONA_COMPOSE_MODEL", "").strip()
    if env_model:
        provider = (
            os.environ.get("NAHLA_PERSONA_COMPOSE_PROVIDER", "").strip()
            or _infer_provider_for_model(env_model)
        )
        provider_key = str(provider or "").strip().lower()
        route = PersonaComposeModelRoute(
            provider=provider,
            model=env_model,
            tier=TIER_TINY,
            source="env",
        )
        configured = (
            provider_key in _SUPPORTED_PERSONA_PROVIDERS
            and is_persona_compose_provider_configured(provider_key)
        )
        return PersonaComposeRouteResolution(
            route=route,
            provider_configured=configured,
            compose_attempt=(
                COMPOSE_ATTEMPT_PROVIDER_CALL
                if configured
                else COMPOSE_ATTEMPT_SKIPPED_UNCONFIGURED
            ),
        )

    settings = merge_ai_defaults(dict(bundle.merchant_persona or {}))
    tenant_model = str(settings.get("persona_composer_model") or "").strip()
    if tenant_model:
        provider = (
            str(settings.get("persona_composer_provider") or "").strip()
            or _infer_provider_for_model(tenant_model)
        )
        provider_key = str(provider or "").strip().lower()
        route = PersonaComposeModelRoute(
            provider=provider,
            model=tenant_model,
            tier=TIER_TINY,
            source="tenant_override",
        )
        configured = (
            provider_key in _SUPPORTED_PERSONA_PROVIDERS
            and is_persona_compose_provider_configured(provider_key)
        )
        return PersonaComposeRouteResolution(
            route=route,
            provider_configured=configured,
            compose_attempt=(
                COMPOSE_ATTEMPT_PROVIDER_CALL
                if configured
                else COMPOSE_ATTEMPT_SKIPPED_UNCONFIGURED
            ),
        )

    platform_candidates = _platform_default_persona_candidates()
    for provider, model in platform_candidates:
        if is_persona_compose_provider_configured(provider):
            return PersonaComposeRouteResolution(
                route=PersonaComposeModelRoute(
                    provider=provider,
                    model=model,
                    tier=TIER_TINY,
                    source="platform_default",
                ),
                provider_configured=True,
                compose_attempt=COMPOSE_ATTEMPT_PROVIDER_CALL,
            )

    preferred_provider, preferred_model = platform_candidates[0]
    return PersonaComposeRouteResolution(
        route=PersonaComposeModelRoute(
            provider=preferred_provider,
            model=preferred_model,
            tier=TIER_TINY,
            source="platform_default",
        ),
        provider_configured=False,
        compose_attempt=COMPOSE_ATTEMPT_SKIPPED_NO_ROUTE,
    )


def resolve_persona_compose_model_route(
    bundle: PersonaFactsBundle,
) -> PersonaComposeModelRoute:
    """Resolve provider/model for persona compose via env, tenant override, or platform tiny tier."""
    return resolve_persona_compose_route_resolution(bundle).route


def build_persona_route_metadata(
    resolution: PersonaComposeRouteResolution,
    *,
    llm_candidate: str = "",
) -> dict[str, Any]:
    """Bounded route/provenance fields for PersonaComposeResult and event metadata."""
    return {
        "route_provider": resolution.route.provider,
        "route_model": resolution.route.model,
        "route_tier": resolution.route.tier,
        "route_source": resolution.route.source,
        "route_provider_configured": resolution.provider_configured,
        "compose_attempt": resolution.compose_attempt,
        "llm_candidate_present": bool(str(llm_candidate or "").strip()),
    }


def call_persona_compose_provider_sync(
    *,
    route: PersonaComposeModelRoute,
    system: str,
    user: str,
    audit_context: dict[str, Any],
) -> tuple[str, str]:
    """Invoke exactly one persona provider; provider exceptions propagate."""
    provider_key = str(route.provider or "").strip().lower()
    if provider_key == "openai_compatible":
        from modules.ai.orchestrator.providers.openai_compatible_provider import (  # noqa: PLC0415
            OpenAICompatibleProvider,
        )

        provider = OpenAICompatibleProvider()
        if not provider.is_configured():
            return "", route.model
        result = provider.call(
            user,
            system,
            audit_context=audit_context,
        )
        return (
            str(result.get("reply_text") or "").strip(),
            str(result.get("model") or route.model),
        )
    if provider_key == "anthropic":
        from modules.ai.orchestrator.providers.anthropic_provider import (  # noqa: PLC0415
            AnthropicProvider,
        )

        provider = AnthropicProvider()
        if not provider.is_configured():
            return "", route.model
        result = provider.call(
            user,
            system,
            audit_context=audit_context,
        )
        return (
            str(result.get("reply_text") or "").strip(),
            str(result.get("model") or route.model),
        )
    return "", route.model

_CONSTITUTION_STUB_REPLIES: dict[str, tuple[str, ...]] = {
    "social_greeting": (
        "ياهلا ومرحبا! حياك الله 😊",
        "أهلًا وسهلًا، نورتنا 🌷",
        "هلا وغلا، أبشر",
        "حياك الله، تفضل 🤍",
        "يا هلا وسهلًا 😊",
    ),
    "social_checkin": (
        "بخير الله يسعدك، وش تحتاج؟",
        "تمام الحمد لله، أبشر 😊",
        "الحمد لله بخير، حياك 🌷",
        "بخير والحمد لله، تفضل",
        "تمام الله يعافيك 😊",
    ),
    "thanks": (
        "العفو، حياك الله 😊",
        "الله يعافيك 🤍",
        "أبشر، على الرحب والسعة",
        "ولا يهمك، حياك الله 🌷",
        "تسلم، الله يسعدك",
    ),
    "dua": (
        "الله يعافيك ويبارك فيك 🤍",
        "الله يجزاك خير 😊",
        "آمين، الله يسعدك 🌷",
        "الله يبارك فيك",
        "جزاك الله خيرًا 😊",
    ),
}


def canonical_facts_hash(verified_facts: dict[str, Any]) -> str:
    payload = json.dumps(verified_facts or {}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def detect_language(text: str) -> str:
    raw = str(text or "")
    if re.search(r"[\u0600-\u06FF]", raw):
        return "ar"
    if re.search(r"[A-Za-z]", raw):
        return "en"
    return "ar"


def build_social_facts_bundle(
    *,
    surface: str,
    inbound_text: str,
    tenant_id: int = 0,
    customer_phone: str = "",
    profile: Optional[dict[str, Any]] = None,
    merchant_persona: Optional[dict[str, Any]] = None,
    ctx: Any = None,
) -> PersonaFactsBundle:
    inbound = str(inbound_text or "").strip()
    language = detect_language(inbound)
    prof = dict(profile or {})
    pure_phatic = False
    try:
        from ..postprocess.social_checkout_pressure_guard import (  # noqa: PLC0415
            is_pure_phatic_bypass_turn,
        )

        pure_phatic = is_pure_phatic_bypass_turn(inbound)
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pure_phatic = False

    verified_facts = {
        "surface": surface,
        "inbound_text": inbound,
        "is_pure_phatic": pure_phatic,
        "allow_checkout_pressure": False,
        "allow_slot_prompts": False,
    }
    customer_context = {
        "first_name": prof.get("first_name") or prof.get("customer_first_name"),
        "full_name": prof.get("full_name") or prof.get("customer_name"),
        "has_verified_name": bool(
            str(prof.get("full_name") or prof.get("customer_name") or "").strip()
        ),
        "has_whatsapp_phone": bool(str(customer_phone or "").strip()),
        "has_saved_address": bool(prof.get("has_saved_address")),
    }
    persona = dict(merchant_persona or {})
    if ctx is not None:
        facts = getattr(ctx, "facts", None)
        if facts is not None:
            name = str(getattr(facts, "assistant_name", "") or "").strip()
            if name:
                persona.setdefault("assistant_name", name)
    return PersonaFactsBundle(
        surface=surface,
        inbound_text=inbound,
        language=language,
        dialect="saudi_arabic" if language == "ar" else None,
        verified_facts=verified_facts,
        customer_context=customer_context,
        merchant_persona=persona,
        constraints=PersonaConstraints(),
        tenant_id=int(tenant_id or 0),
        customer_phone=str(customer_phone or ""),
    )


class FactBoundPersonaComposer:
    """Text-only persona compose for verified social facts."""

    def __init__(
        self,
        *,
        enforce_gate: bool = True,
        llm_callable: Optional[_LLMCallable] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        self._enforce_gate = enforce_gate
        self._llm_callable = llm_callable
        self._timeout_seconds = resolve_persona_compose_timeout_seconds(timeout_seconds)

    async def compose(
        self,
        bundle: PersonaFactsBundle,
        *,
        ctx: Any = None,
        db: Any = None,
    ) -> PersonaComposeResult:
        t0 = time.monotonic()
        surface = str(bundle.surface or "").strip()
        if surface not in PERSONA_COMPOSER_SURFACES:
            fb = deterministic_fallback(bundle, ctx=ctx, reason="unsupported_surface")
            return self._result_from_fallback(bundle, fb, t0, reason="unsupported_surface")

        raw_text = ""
        source = "fallback_deterministic"
        model: Optional[str] = None
        fallback_reason = ""
        if self._llm_callable is not None:
            resolution = resolve_injected_callable_route_resolution()
        else:
            resolution = resolve_persona_compose_route_resolution(bundle)
        route_metadata = build_persona_route_metadata(resolution, llm_candidate="")
        model = resolution.route.model or None

        if self._llm_callable is not None:
            try:
                raw_text, model = await self._invoke_injected_callable(bundle)
                route_metadata = build_persona_route_metadata(
                    resolution,
                    llm_candidate=raw_text,
                )
                if (raw_text or "").strip():
                    source = "persona_llm"
                else:
                    fallback_reason = "empty_llm"
            except TimeoutError:
                fallback_reason = "timeout"
                logger.warning(
                    "[PERSONA_COMPOSE] timeout surface=%s tenant=%s",
                    surface,
                    bundle.tenant_id,
                )
            except Exception as exc:  # noqa: BLE001
                fallback_reason = f"llm_error:{type(exc).__name__}"
                logger.warning(
                    "[PERSONA_COMPOSE] llm_failed surface=%s tenant=%s err=%s",
                    surface,
                    bundle.tenant_id,
                    exc,
                )
        elif resolution.compose_attempt in {
            COMPOSE_ATTEMPT_SKIPPED_UNCONFIGURED,
            COMPOSE_ATTEMPT_SKIPPED_NO_ROUTE,
        }:
            fallback_reason = "route_unconfigured"
        else:
            try:
                raw_text, model = await self._invoke_provider_callable(
                    bundle,
                    resolution,
                )
                route_metadata = build_persona_route_metadata(
                    resolution,
                    llm_candidate=raw_text,
                )
                if (raw_text or "").strip():
                    source = "persona_llm"
                else:
                    fallback_reason = "empty_llm"
            except TimeoutError:
                fallback_reason = "timeout"
                logger.warning(
                    "[PERSONA_COMPOSE] timeout surface=%s tenant=%s",
                    surface,
                    bundle.tenant_id,
                )
            except Exception as exc:  # noqa: BLE001
                fallback_reason = f"llm_error:{type(exc).__name__}"
                logger.warning(
                    "[PERSONA_COMPOSE] llm_failed surface=%s tenant=%s err=%s",
                    surface,
                    bundle.tenant_id,
                    exc,
                )

        if not (raw_text or "").strip():
            if surface not in {
                PERSONA_SURFACE_CUSTOMER_CONDITIONAL_COUPON_ANSWER,
                PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
            }:
                raw_text = deterministic_fallback(
                    bundle,
                    ctx=ctx,
                    reason=fallback_reason or "empty_llm",
                )
                source = "fallback_deterministic"

        final_text, guard = apply_guards_or_fallback(
            raw_text,
            bundle,
            ctx=ctx,
            db=db,
            tenant_id=bundle.tenant_id,
        )
        if not guard.passed:
            source = "fallback_deterministic"
            if not fallback_reason:
                fallback_reason = guard.failed_reason or "guard_failed"

        latency_ms = int((time.monotonic() - t0) * 1000)
        emoji_count = sum(
            1 for ch in final_text if ch in {"😊", "🌷", "🤍"}
        )
        result = PersonaComposeResult(
            text=final_text,
            source=source,
            surface=surface,
            facts_hash=canonical_facts_hash(bundle.verified_facts),
            guard_passed=guard.passed and source == "persona_llm",
            guard_failed_reason="" if guard.passed else guard.failed_reason,
            fallback_reason=fallback_reason,
            language=bundle.language,
            dialect=bundle.dialect,
            emoji_count=emoji_count,
            latency_ms=latency_ms,
            model=model,
            metadata=dict(route_metadata),
        )
        logger.info(
            "[PERSONA_COMPOSE] surface=%s source=%s facts_hash=%s "
            "guard_passed=%s fallback_reason=%s latency_ms=%s tenant=%s",
            result.surface,
            result.source,
            result.facts_hash,
            result.guard_passed,
            result.fallback_reason or "-",
            result.latency_ms,
            bundle.tenant_id,
        )
        return result

    async def compose_samples(
        self,
        surface: str,
        inbound_text: str,
        *,
        samples: int = 5,
    ) -> list[str]:
        """Constitution probe — varied safe replies via stub LLM (no live API)."""
        out: list[str] = []
        bundle = build_social_facts_bundle(surface=surface, inbound_text=inbound_text)
        original_callable = self._llm_callable
        try:
            for i in range(max(1, samples)):
                self._llm_callable = self._constitution_stub_llm_factory(surface, seed=i)
                result = await self.compose(bundle)
                if result.text.strip():
                    out.append(result.text.strip())
        finally:
            self._llm_callable = original_callable
        return out

    def _constitution_stub_llm_factory(
        self,
        surface: str,
        *,
        seed: int = 0,
    ) -> _LLMCallable:
        pool = _CONSTITUTION_STUB_REPLIES.get(surface) or _CONSTITUTION_STUB_REPLIES["social_greeting"]

        async def _stub(_bundle: PersonaFactsBundle) -> str:
            return pool[seed % len(pool)]

        return _stub

    async def _invoke_injected_callable(
        self,
        bundle: PersonaFactsBundle,
    ) -> tuple[str, Optional[str]]:
        import asyncio  # noqa: PLC0415

        if self._llm_callable is None:
            return "", None
        text = (
            await asyncio.wait_for(
                self._llm_callable(bundle),
                timeout=self._timeout_seconds,
            )
        ).strip()
        return text, None

    async def _invoke_provider_callable(
        self,
        bundle: PersonaFactsBundle,
        resolution: PersonaComposeRouteResolution,
    ) -> tuple[str, Optional[str]]:
        route = resolution.route
        system = build_system_prompt(bundle)
        user = build_user_prompt(bundle)
        audit_context = {
            "reason": _PERSONA_COMPOSE_CALL_SITE,
            "surface": bundle.surface,
            "tenant_id": bundle.tenant_id,
            "model_override": route.model,
            "model_tier": route.tier,
        }

        import asyncio  # noqa: PLC0415

        def _call_provider_sync() -> tuple[str, str]:
            return call_persona_compose_provider_sync(
                route=route,
                system=system,
                user=user,
                audit_context=audit_context,
            )

        text, used_model = await asyncio.wait_for(
            asyncio.to_thread(_call_provider_sync),
            timeout=self._timeout_seconds,
        )
        return text, used_model or route.model or None

    def _result_from_fallback(
        self,
        bundle: PersonaFactsBundle,
        text: str,
        t0: float,
        *,
        reason: str,
    ) -> PersonaComposeResult:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return PersonaComposeResult(
            text=text,
            source="fallback_deterministic",
            surface=bundle.surface,
            facts_hash=canonical_facts_hash(bundle.verified_facts),
            guard_passed=False,
            fallback_reason=reason,
            language=bundle.language,
            dialect=bundle.dialect,
            latency_ms=latency_ms,
        )
