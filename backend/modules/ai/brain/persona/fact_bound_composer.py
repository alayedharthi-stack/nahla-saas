"""FactBoundPersonaComposer — verified-facts phrasing for social surfaces."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Awaitable, Callable, Optional, Sequence

from .compose_guards import apply_guards_or_fallback, apply_persona_compose_guards
from .facts_bundle import (
    PHASE2_SOCIAL_SURFACES,
    PersonaComposeResult,
    PersonaConstraints,
    PersonaFactsBundle,
)
from .fallback_catalog import deterministic_fallback
from .prompts import build_system_prompt, build_user_prompt

logger = logging.getLogger("nahla.brain.persona.fact_bound_composer")

_LLMCallable = Callable[[PersonaFactsBundle], Awaitable[str]]

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
        timeout_seconds: float = 1.5,
    ) -> None:
        self._enforce_gate = enforce_gate
        self._llm_callable = llm_callable
        self._timeout_seconds = timeout_seconds

    async def compose(
        self,
        bundle: PersonaFactsBundle,
        *,
        ctx: Any = None,
        db: Any = None,
    ) -> PersonaComposeResult:
        t0 = time.monotonic()
        surface = str(bundle.surface or "").strip()
        if surface not in PHASE2_SOCIAL_SURFACES:
            fb = deterministic_fallback(bundle, ctx=ctx, reason="unsupported_surface")
            return self._result_from_fallback(bundle, fb, t0, reason="unsupported_surface")

        raw_text = ""
        source = "fallback_deterministic"
        model: Optional[str] = None
        fallback_reason = ""

        try:
            raw_text = await self._invoke_llm(bundle)
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

    async def _invoke_llm(self, bundle: PersonaFactsBundle) -> str:
        if self._llm_callable is not None:
            import asyncio  # noqa: PLC0415

            return (
                await asyncio.wait_for(
                    self._llm_callable(bundle),
                    timeout=self._timeout_seconds,
                )
            ).strip()

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return ""

        import asyncio  # noqa: PLC0415

        import anthropic  # noqa: PLC0415

        client = anthropic.AsyncAnthropic(
            api_key=api_key,
            timeout=self._timeout_seconds,
        )
        system = build_system_prompt(bundle)
        user = build_user_prompt(bundle)
        model = (
            os.environ.get("NAHLA_PERSONA_COMPOSE_MODEL")
            or os.environ.get("ANTHROPIC_SLOT_MODEL")
            or "claude-3-5-haiku-20241022"
        )

        from modules.ai.orchestrator.llm_cost_audit import emit_llm_cost_audit  # noqa: PLC0415

        emit_llm_cost_audit(
            model=model,
            provider="anthropic",
            messages_count=1,
            system_chars=len(system),
            messages_chars=len(user),
            total_prompt_chars=len(system) + len(user),
            estimated_input_tokens=(len(system) + len(user)) // 4,
            reason="brain.persona.fact_bound_composer",
        )

        response = await asyncio.wait_for(
            client.messages.create(
                model=model,
                max_tokens=120,
                temperature=0.9,
                system=system,
                messages=[{"role": "user", "content": user}],
            ),
            timeout=self._timeout_seconds,
        )
        from modules.ai.orchestrator.ai_usage_ledger import (  # noqa: PLC0415
            record_ai_usage_from_anthropic,
        )

        reply_text = response.content[0].text if response.content else ""
        record_ai_usage_from_anthropic(
            audit_extra={
                "reason": "brain.persona.fact_bound_composer",
                "surface": bundle.surface,
            },
            model=model,
            response=response,
            reply_text=reply_text,
            total_prompt_chars=len(system) + len(user),
        )
        return str(reply_text or "").strip()

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
