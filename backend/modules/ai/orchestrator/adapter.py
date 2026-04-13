"""
backend/modules/ai/orchestrator/adapter.py
───────────────────────────────────────────
Compatibility adapter between the new canonical AI orchestrator and all
legacy request structures in the Nahla codebase.

Public surface:
  generate_ai_reply(**kwargs)         — primary entry point, keyword args
  from_orchestrate_request(req_dict)  — convert OrchestrateRequest-style dict
  from_ai_engine_request(req_dict)    — convert AIRequest-style dict (ai-engine)
  from_whatsapp_client(...)           — convert whatsapp-service call signature

Rules:
  - Does NOT modify any legacy service (ai-engine, services/ai-orchestrator,
    whatsapp-service).
  - Does NOT change any webhook or runtime entrypoint.
  - Does NOT call any external API or database directly.
  - This file is a pure bridge layer: it converts legacy inputs to canonical
    types, then delegates to AIOrchestrationPipeline.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Dict, List, Optional

from modules.ai.orchestrator.engine import AIOrchestratorEngine
from modules.ai.orchestrator.pipeline import AIOrchestrationPipeline
from modules.ai.orchestrator.types import (
    AIChannel,
    AIContext,
    AIOrchestrationRequest,
    AIProvider,
    AIReplyPayload,
)

logger = logging.getLogger("nahla.ai.orchestrator.adapter")

# Shared pipeline instance — stateless, safe to reuse across calls
_pipeline = AIOrchestrationPipeline(engine=AIOrchestratorEngine())


# ══════════════════════════════════════════════════════════════════════════════
# Primary entry point
# ══════════════════════════════════════════════════════════════════════════════

def generate_ai_reply(
    *,
    tenant_id: Optional[int] = None,
    customer_phone: str = "",
    message: str = "",
    store_name: str = "",
    channel: str = "system",
    locale: str = "ar",
    context_metadata: Optional[Dict[str, Any]] = None,
    tools_requested: Optional[List[str]] = None,
    tool_definitions: Optional[List[Dict[str, Any]]] = None,
    prompt_overrides: Optional[Dict[str, Any]] = None,
    provider_hint: Optional[str] = None,
) -> AIReplyPayload:
    """
    Primary adapter entry point for AI reply generation.

    Accepts the lowest-common-denominator set of keyword arguments that covers
    all legacy request shapes (OrchestrateRequest, AIRequest, ai_client call).

    Converts them into an AIOrchestrationRequest and delegates to the
    canonical AIOrchestrationPipeline.

    Parameters
    ----------
    tenant_id        : numeric DB tenant id
    customer_phone   : customer's phone number
    message          : inbound customer message text
    store_name       : display name of the merchant's store
    channel          : caller surface — one of whatsapp | campaigns | conversations | widgets | system
    locale           : preferred reply language (default: ar)
    context_metadata : optional enrichment dict forwarded as AIContext.metadata
                       (customer profile, products, coupons, history, etc.)
    tools_requested  : list of action/tool names to gate through commerce permissions
    tool_definitions : optional tool schema definitions used by providers that
                       support native tool use (currently Anthropic)
    prompt_overrides : key/value overrides injected at highest priority into prompt context
    provider_hint    : optional LLM provider preference
                       (anthropic | openai_compatible | gemini | mock)

    Returns
    -------
    AIReplyPayload with reply_text, allowed/blocked actions, policy notes,
    permission snapshot, and observability metadata.
    """
    _channel: AIChannel = _coerce_channel(channel)
    _provider: Optional[AIProvider] = _coerce_provider(provider_hint)

    request = AIOrchestrationRequest(
        context=AIContext(
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            store_name=store_name,
            channel=_channel,
            locale=locale,
            metadata=context_metadata or {},
        ),
        message=message,
        tools_requested=tools_requested or [],
        tool_definitions=tool_definitions or [],
        prompt_overrides=prompt_overrides or {},
        provider_hint=_provider,
    )

    logger.debug(
        "[adapter.generate_ai_reply] tenant=%s channel=%s phone=%s",
        tenant_id, _channel, customer_phone,
    )

    return _pipeline.run(request)


async def generate_orchestrate_response(
    *,
    tenant_id: int,
    customer_phone: str,
    message: str,
    conversation_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Canonical compatibility entry point for the legacy `/orchestrate` HTTP API.

    This function preserves the old response shape while moving execution
    ownership into backend/modules/ai/orchestrator.

    It intentionally reuses transitional memory / guard helpers from
    services/ai-orchestrator so that:
      - `/orchestrate` clients keep the same response contract
      - runtime provider execution still goes through the canonical adapter
        -> pipeline -> engine -> provider registry/provider classes
      - the service route itself becomes a thin shell
    """
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
    orch_root = os.path.join(repo_root, "services", "ai-orchestrator")
    for _p in (repo_root, orch_root):
        if _p not in sys.path:
            sys.path.insert(0, _p)

    from memory.loader import load_customer_memory  # type: ignore[import-not-found]
    from memory.updater import update_customer_memory  # type: ignore[import-not-found]
    from fact_guard.data_fetcher import GroundingData, fetch_grounding_data  # type: ignore[import-not-found]
    from fact_guard.checker import extract_coupon_codes_from_text, vet_reply  # type: ignore[import-not-found]
    from policy.guard import validate_actions  # type: ignore[import-not-found]
    from commerce.permission_guard import gate as permission_gate, load_permissions  # type: ignore[import-not-found]
    from commerce.permissions import CommercePermissionSet  # type: ignore[import-not-found]
    from execution.action_execution_guard import decide as execution_decide  # type: ignore[import-not-found]
    from engine.claude_client import _TOOLS as ORCHESTRATOR_TOOLS  # type: ignore[import-not-found]
    from modules.ai.prompts.builder import build_system_prompt

    try:
        ctx = load_customer_memory(tenant_id, customer_phone)
    except Exception as exc:
        logger.warning("[adapter.generate_orchestrate_response] memory load failed: %s", exc)
        ctx = {}

    customer_segment = ctx.get("segment", "new")
    is_returning = ctx.get("is_returning", False)
    customer_id = ctx.get("customer_id")

    try:
        permissions: CommercePermissionSet = load_permissions(tenant_id)
    except Exception as exc:
        logger.warning("[adapter.generate_orchestrate_response] permissions load failed: %s", exc)
        permissions = CommercePermissionSet(tenant_id=tenant_id)

    full_system_prompt = build_system_prompt({
        **ctx,
        "store_name": ctx.get("store_name", "our store"),
        "preferred_language": ctx.get("preferred_language", "ar"),
    })

    payload = generate_ai_reply(
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        message=message,
        store_name=ctx.get("store_name", ""),
        channel="whatsapp",
        locale=ctx.get("preferred_language", "ar"),
        context_metadata={
            **ctx,
            "conversation_id": conversation_id,
        },
        tool_definitions=ORCHESTRATOR_TOOLS,
        prompt_overrides={
            "__full_system_prompt": full_system_prompt,
        },
        provider_hint="anthropic",
    )

    raw_reply = payload.reply_text
    raw_actions = payload.raw_model_output.get("actions", []) or []
    model_used = payload.metadata.get("model", "unknown")

    try:
        grounding = fetch_grounding_data(tenant_id, customer_phone)
    except Exception as exc:
        logger.warning("[adapter.generate_orchestrate_response] grounding fetch failed: %s", exc)
        grounding = GroundingData()

    mentioned_coupons = extract_coupon_codes_from_text(raw_reply)
    vetted = vet_reply(raw_reply, grounding, mentioned_coupon_codes=mentioned_coupons)
    vetted_reply = vetted.vetted_text
    fact_guard = {
        "was_modified": vetted.was_modified,
        "claims_detected": [c.claim_type for c in vetted.claims if c.detected],
    }

    policy_gated = validate_actions(raw_actions, ctx)
    permission_gated = permission_gate(policy_gated, permissions)
    fully_gated = execution_decide(permission_gated, tenant_id)

    approved_product_ids: List[int] = []
    coupon_code_to_inject: Optional[str] = None
    coupon_expiry_text_to_inject: Optional[str] = None
    for action in fully_gated:
        if action.get("executable") and action.get("final_payload"):
            fp = action["final_payload"]
            if action["type"] == "suggest_product":
                pid = fp.get("product_id")
                if pid:
                    approved_product_ids.append(pid)
            elif action["type"] in ("suggest_bundle", "propose_order", "create_draft_order"):
                approved_product_ids.extend(fp.get("product_ids", []))
            elif action["type"] == "suggest_coupon":
                coupon_payload = await _execute_suggest_coupon(
                    tenant_id, customer_segment, fp,
                )
                if coupon_payload:
                    coupon_code_to_inject = coupon_payload.get("code")
                    coupon_expiry_text_to_inject = coupon_payload.get("expires_text")
                    fp["coupon_code"] = coupon_code_to_inject
                    if coupon_payload.get("expires_at"):
                        fp["coupon_expires_at"] = coupon_payload["expires_at"]
                    if coupon_expiry_text_to_inject:
                        fp["coupon_expires_text"] = coupon_expiry_text_to_inject

    if coupon_code_to_inject and coupon_code_to_inject not in (vetted_reply or ""):
        vetted_reply = (vetted_reply or "") + f"\n\nكود الخصم الخاص بك: {coupon_code_to_inject}"
    if coupon_expiry_text_to_inject and coupon_expiry_text_to_inject not in (vetted_reply or ""):
        vetted_reply = (vetted_reply or "") + f"\nينتهي الكوبون بتاريخ: {coupon_expiry_text_to_inject}"

    branding = ctx.get("branding", "")
    final_reply = f"{vetted_reply}\n\n_{branding}_" if branding and vetted_reply else vetted_reply

    turn_data = {
        "intent": "order" if any(w in message.lower() for w in ("buy", "order", "اشتري", "اطلب", "طلب")) else "general_inquiry",
        "sentiment": "neutral",
        "suggested_product_ids": approved_product_ids,
        "inferred_preferences": {},
        "approved_actions": [
            {
                "type": a["type"],
                "proposed_payload": a.get("proposed_payload"),
                "policy_result": a["policy_result"],
                "policy_notes": a.get("policy_notes", ""),
                "permission_result": a.get("permission_result", "n/a"),
                "permission_notes": a.get("permission_notes", ""),
                "final_payload": a.get("final_payload"),
            }
            for a in fully_gated
        ],
        "summary_snippet": f"Customer: {message[:200]}",
    }
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(
            None,
            update_customer_memory,
            tenant_id,
            customer_id,
            customer_phone,
            turn_data,
        )
    except RuntimeError:
        pass

    return {
        "reply": final_reply or "شكراً لتواصلك معنا! 😊 كيف أقدر أساعدك؟\n\nThanks for reaching out! 😊 How can I assist you?",
        "fact_guard": fact_guard,
        "actions": [
            {
                "type": a["type"],
                "policy_result": a["policy_result"],
                "policy_notes": a.get("policy_notes", ""),
                "permission_result": a.get("permission_result", "n/a"),
                "permission_notes": a.get("permission_notes", ""),
                "executable": a.get("executable", False),
                "blocked_reason": a.get("blocked_reason"),
                "final_payload": a.get("final_payload"),
            }
            for a in fully_gated
        ],
        "customer_segment": customer_segment,
        "is_returning": is_returning,
        "model_used": model_used,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Legacy-specific converters
# ══════════════════════════════════════════════════════════════════════════════

def from_orchestrate_request(req_dict: Dict[str, Any]) -> AIOrchestrationRequest:
    """
    Convert a services/ai-orchestrator OrchestrateRequest-shaped dict into
    an AIOrchestrationRequest.

    Expected input keys (matches OrchestrateRequest schema in
    services/ai-orchestrator/api/routes.py):
      tenant_id      : int
      customer_phone : str
      message        : str
      conversation_id: int | None  (optional, preserved in metadata)

    Any extra keys are forwarded into context.metadata so callers can pass
    enrichment data without changing the function signature.
    """
    known_keys = {"tenant_id", "customer_phone", "message", "conversation_id"}
    extra = {k: v for k, v in req_dict.items() if k not in known_keys}

    metadata: Dict[str, Any] = {}
    if req_dict.get("conversation_id") is not None:
        metadata["conversation_id"] = req_dict["conversation_id"]
    metadata.update(extra)

    return AIOrchestrationRequest(
        context=AIContext(
            tenant_id=req_dict.get("tenant_id"),
            customer_phone=req_dict.get("customer_phone", ""),
            channel="whatsapp",
            metadata=metadata,
        ),
        message=req_dict.get("message", ""),
    )


def from_ai_engine_request(req_dict: Dict[str, Any]) -> AIOrchestrationRequest:
    """
    Convert an ai-engine AIRequest-shaped dict into an AIOrchestrationRequest.

    Expected input keys (matches AIRequest schema in ai-engine/main.py):
      tenant    : str   — display_phone_number or store identifier
      phone     : str   — customer phone number
      message   : str
      tenant_id : int | None  (optional numeric id)

    The legacy `tenant` field (a string identifier rather than an int) is
    stored in context.metadata["legacy_tenant_identifier"] so it is
    preserved without being promoted to a typed field.
    """
    tenant_str = req_dict.get("tenant", "")
    metadata: Dict[str, Any] = {}
    if tenant_str:
        metadata["legacy_tenant_identifier"] = tenant_str

    return AIOrchestrationRequest(
        context=AIContext(
            tenant_id=req_dict.get("tenant_id"),
            customer_phone=req_dict.get("phone", ""),
            channel="whatsapp",
            metadata=metadata,
        ),
        message=req_dict.get("message", ""),
    )


def from_whatsapp_client(
    tenant: str,
    phone: str,
    message: str,
    tenant_id: Optional[int] = None,
) -> AIOrchestrationRequest:
    """
    Convert the whatsapp-service/ai_client.get_ai_response() call signature
    into an AIOrchestrationRequest.

    Matches the exact parameter names used in:
      whatsapp-service/ai_client.py → get_ai_response(tenant, phone, message, tenant_id)

    This converter exists so that a future whatsapp-service migration can
    replace the HTTP call to the legacy orchestrator with a direct in-process
    call to generate_ai_reply() via this adapter — without changing the
    calling code signature.
    """
    metadata: Dict[str, Any] = {}
    if tenant:
        metadata["legacy_tenant_identifier"] = tenant

    return AIOrchestrationRequest(
        context=AIContext(
            tenant_id=tenant_id,
            customer_phone=phone,
            channel="whatsapp",
            metadata=metadata,
        ),
        message=message,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Convenience: run pipeline directly from pre-built request
# ══════════════════════════════════════════════════════════════════════════════

def run_pipeline(request: AIOrchestrationRequest) -> AIReplyPayload:
    """
    Execute the canonical pipeline for an already-built AIOrchestrationRequest.

    Use this when you have already converted a legacy request via one of the
    from_* converters and want to hand control directly to the pipeline.
    """
    return _pipeline.run(request)


# ══════════════════════════════════════════════════════════════════════════════
# Internal coercion helpers
# ══════════════════════════════════════════════════════════════════════════════

_VALID_CHANNELS = frozenset({"whatsapp", "campaigns", "conversations", "widgets", "system"})
_VALID_PROVIDERS = frozenset({"anthropic", "openai_compatible", "gemini", "mock", "unknown"})


def _coerce_channel(value: str) -> AIChannel:
    """
    Map any reasonable channel string to a valid AIChannel literal.

    Unknown values fall back to 'system' so callers are never forced to know
    the exact literal values.
    """
    normalized = (value or "system").lower().strip()
    return normalized if normalized in _VALID_CHANNELS else "system"  # type: ignore[return-value]


def _coerce_provider(value: Optional[str]) -> Optional[AIProvider]:
    """
    Map an optional provider string to a valid AIProvider literal or None.
    """
    if not value:
        return None
    normalized = value.lower().strip()
    return normalized if normalized in _VALID_PROVIDERS else None  # type: ignore[return-value]


async def _execute_suggest_coupon(
    tenant_id: int,
    customer_segment: str,
    payload: Dict[str, Any],
) -> Optional[Dict[str, str]]:
    """Pick or create a real coupon for the customer segment."""
    try:
        db_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        if db_root not in sys.path:
            sys.path.insert(0, db_root)

        from core.database import SessionLocal
        from services.coupon_generator import CouponGeneratorService, build_coupon_send_payload

        db = SessionLocal()
        try:
            svc = CouponGeneratorService(db, tenant_id)
            coupon = svc.pick_coupon_for_segment(customer_segment)
            if coupon:
                logger.info(
                    "suggest_coupon: picked pool coupon %s for tenant=%s segment=%s",
                    coupon.code, tenant_id, customer_segment,
                )
                return build_coupon_send_payload(coupon)

            requested_discount = payload.get("discount_pct")
            coupon = await svc.create_on_demand(
                customer_segment,
                requested_discount_pct=requested_discount,
            )
            if coupon:
                logger.info(
                    "suggest_coupon: created on-demand coupon %s for tenant=%s segment=%s",
                    coupon.code, tenant_id, customer_segment,
                )
                return build_coupon_send_payload(coupon)
        finally:
            db.close()
    except Exception as exc:
        logger.error("suggest_coupon execution failed: %s", exc, exc_info=True)
    return None
