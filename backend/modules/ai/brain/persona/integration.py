"""Integration hooks for FactBoundPersonaComposer in Brain compose."""
from __future__ import annotations

import logging
from typing import Any, Optional

from core.tenant import merge_ai_defaults

from ..types import BrainContext
from .fact_bound_composer import FactBoundPersonaComposer, build_social_facts_bundle
from .facts_bundle import PersonaComposeResult
from .flags import is_persona_composer_enforce_enabled, persona_composer_allowlist_result
from .surface_resolver import is_allowed_phase2_surface

logger = logging.getLogger("nahla.brain.persona.integration")


def _ai_settings_from_ctx(ctx: BrainContext) -> dict[str, Any]:
    mc = dict(getattr(ctx, "merchant_context", None) or {})
    ai = mc.get("ai_settings")
    if isinstance(ai, dict) and ai:
        return merge_ai_defaults(ai)
    tc = getattr(ctx, "tenant_context", None)
    if tc is not None:
        stored = getattr(tc, "ai_settings", None)
        if isinstance(stored, dict) and stored:
            return merge_ai_defaults(stored)
    return merge_ai_defaults({})


def _surface_allowed(ai_settings: dict[str, Any], surface: str) -> bool:
    if not is_allowed_phase2_surface(surface):
        return False
    raw = ai_settings.get("persona_composer_surfaces")
    if isinstance(raw, list) and raw:
        allowed = {str(s).strip() for s in raw if str(s).strip()}
        return surface in allowed
    return True


def should_enforce_persona_compose(ctx: BrainContext, *, surface: str) -> bool:
    if getattr(ctx, "human_priority", False):
        return False
    ai = _ai_settings_from_ctx(ctx)
    if not _surface_allowed(ai, surface):
        return False
    return is_persona_composer_enforce_enabled(
        tenant_id=int(getattr(ctx, "tenant_id", 0) or 0),
        customer_phone=str(getattr(ctx, "customer_phone", "") or ""),
        ai_settings=ai,
    )


def persona_compose_metadata(result: PersonaComposeResult) -> dict[str, Any]:
    """Nested persona_compose block for message event persistence."""
    return {
        "surface": result.surface,
        "source": result.source,
        "guard_passed": result.guard_passed,
        "guard_failed_reason": result.guard_failed_reason or "",
        "fallback_reason": result.fallback_reason or "",
        "language": result.language,
        "dialect": result.dialect or "",
        "emoji_count": result.emoji_count,
        "latency_ms": result.latency_ms,
        "model": result.model or "",
        "facts_hash": result.facts_hash,
    }


def build_persona_compose_event_metadata(
    result: PersonaComposeResult,
    *,
    tenant_id: int,
    allowlist_result: str,
) -> dict[str, Any]:
    """Outbound ``message_events.extra_metadata`` payload when composer is used."""
    return {
        "chosen_path": "fact_bound_persona_compose",
        "persona_compose": {
            **persona_compose_metadata(result),
            "tenant_id": int(tenant_id),
            "allowlist_result": str(allowlist_result or ""),
        },
    }


def merge_persona_compose_into_extra_metadata(
    extra: Optional[dict[str, Any]],
    event_meta: Optional[dict[str, Any]],
) -> dict[str, Any]:
    merged = dict(extra or {})
    if not isinstance(event_meta, dict):
        return merged
    chosen = str(event_meta.get("chosen_path") or "").strip()
    if chosen:
        merged["chosen_path"] = chosen
    pc = event_meta.get("persona_compose")
    if isinstance(pc, dict) and pc:
        merged["persona_compose"] = dict(pc)
    return merged


async def try_enforce_phatic_llm_persona_compose(
    ctx: BrainContext,
    *,
    decision: Any = None,
    action_result: Optional[Any] = None,
    db: Any = None,
) -> Optional[PersonaComposeResult]:
    """Intercept ACTION_LLM_REPLY phatic turns for test-mode FactBoundPersonaComposer."""
    from .surface_resolver import resolve_phatic_llm_surface  # noqa: PLC0415

    surface = resolve_phatic_llm_surface(ctx, decision=decision)
    if not surface:
        return None
    return await try_enforce_persona_compose(
        ctx,
        surface=surface,
        action_result=action_result,
        db=db,
    )


async def try_enforce_persona_compose(
    ctx: BrainContext,
    *,
    surface: str,
    action_result: Optional[Any] = None,
    db: Any = None,
) -> Optional[PersonaComposeResult]:
    """Compose social phrasing when test-mode gate passes; else None (legacy path)."""
    if not should_enforce_persona_compose(ctx, surface=surface):
        return None

    ai = _ai_settings_from_ctx(ctx)
    bundle = build_social_facts_bundle(
        surface=surface,
        inbound_text=str(getattr(ctx, "message", "") or ""),
        tenant_id=int(getattr(ctx, "tenant_id", 0) or 0),
        customer_phone=str(getattr(ctx, "customer_phone", "") or ""),
        profile=dict(getattr(ctx, "profile", None) or {}),
        merchant_persona=_ai_settings_from_ctx(ctx),
        ctx=ctx,
    )
    composer = FactBoundPersonaComposer(enforce_gate=False)
    result = await composer.compose(bundle, ctx=ctx, db=db)
    if action_result is not None:
        data = getattr(action_result, "data", None)
        if isinstance(data, dict):
            allowlist_result = persona_composer_allowlist_result(
                tenant_id=int(getattr(ctx, "tenant_id", 0) or 0),
                customer_phone=str(getattr(ctx, "customer_phone", "") or ""),
                ai_settings=ai,
            )
            event_meta = build_persona_compose_event_metadata(
                result,
                tenant_id=int(getattr(ctx, "tenant_id", 0) or 0),
                allowlist_result=allowlist_result,
            )
            data.update(event_meta)
    return result
