"""
Orchestrator API routes
────────────────────────
POST /orchestrate           — main intelligence + safety pipeline
GET  /health                — liveness check
GET  /customer/{tenant}/{phone} — inspect stored customer memory (debug)
GET  /permissions/{tenant_id}   — inspect tenant commerce permissions (debug)

Pipeline (9 stages):
  1  Load customer memory
  2  Load commerce permissions
  3  Build Claude system prompt (grounding instructions included)
  4  Call Claude (reply text + tool-use actions)
  5  FactGuard          — vet reply TEXT for 11 ungrounded claim types
  6  PolicyGuard        — validate ACTION PARAMS (discounts, categories, count)
  7  CommercePermissionGuard — filter by tenant-level action type flags
  8  ActionExecutionGuard    — final synthesis: executable + blocked_reason
  9  Async memory update + return OrchestrateResponse
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_ORCH_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ORCH_ROOT not in sys.path:
    sys.path.insert(0, _ORCH_ROOT)

from memory.loader import load_customer_memory
from memory.updater import update_customer_memory
from policy.guard import validate_actions
from prompt.builder import build_system_prompt
from engine.claude_client import call_claude
from fact_guard.data_fetcher import fetch_grounding_data
from fact_guard.checker import vet_reply, extract_coupon_codes_from_text
from commerce.permission_guard import gate as permission_gate, load_permissions
from commerce.permissions import CommercePermissionSet
from execution.action_execution_guard import decide as execution_decide

logger = logging.getLogger("ai-orchestrator.routes")
router = APIRouter()


# ── Request / Response schemas ────────────────────────────────────────────────

class OrchestrateRequest(BaseModel):
    tenant_id: int
    customer_phone: str
    message: str
    conversation_id: Optional[int] = None


class GatedAction(BaseModel):
    type: str
    # PolicyGuard
    policy_result: str              # approved | modified | blocked
    policy_notes: str
    # CommercePermissionGuard
    permission_result: str          # permitted | denied | n/a
    permission_notes: str
    # ActionExecutionGuard — final synthesis
    executable: bool                # True only when all gates pass
    blocked_reason: Optional[str]   # None when executable=True; first failing gate's message
    # Final payload — only present when executable=True
    final_payload: Optional[Dict[str, Any]] = None


class FactGuardResult(BaseModel):
    was_modified: bool
    claims_detected: List[str]


class OrchestrateResponse(BaseModel):
    reply: str
    fact_guard: FactGuardResult
    actions: List[GatedAction]
    customer_segment: str
    is_returning: bool
    model_used: str


# ── Main orchestration endpoint ───────────────────────────────────────────────

@router.post("/orchestrate", response_model=OrchestrateResponse)
async def orchestrate(req: OrchestrateRequest):
    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="message cannot be empty")

    logger.info(
        f"Orchestrate | tenant={req.tenant_id} | phone={req.customer_phone} | "
        f"msg={req.message[:80]}"
    )

    # ── Stage 1: Load customer memory ─────────────────────────────────────────
    try:
        ctx = load_customer_memory(req.tenant_id, req.customer_phone)
    except Exception as exc:
        logger.warning(f"Memory load failed: {exc} — proceeding with empty context")
        ctx = {}

    customer_segment = ctx.get("segment", "new")
    is_returning     = ctx.get("is_returning", False)
    customer_id      = ctx.get("customer_id")

    # ── Stage 2: Load commerce permissions ────────────────────────────────────
    try:
        permissions: CommercePermissionSet = load_permissions(req.tenant_id)
    except Exception as exc:
        logger.warning(f"Permissions load failed: {exc} — using safe defaults")
        permissions = CommercePermissionSet(tenant_id=req.tenant_id)

    # ── Stage 3: Build system prompt ──────────────────────────────────────────
    system_prompt = build_system_prompt(ctx)

    # ── Stage 4: Call Claude ──────────────────────────────────────────────────
    try:
        engine_result = await call_claude(system_prompt, req.message)
    except Exception as exc:
        logger.error(f"Claude call failed: {exc}")
        raise HTTPException(status_code=502, detail="AI engine unavailable")

    raw_reply   = engine_result.get("reply", "")
    raw_actions = engine_result.get("actions", [])
    model_used  = engine_result.get("model", "unknown")

    # ── Stage 5: Fact Guard ───────────────────────────────────────────────────
    # Fetch grounding data (DB reads, once per request)
    try:
        grounding = fetch_grounding_data(req.tenant_id, req.customer_phone)
    except Exception as exc:
        logger.warning(f"Grounding data fetch failed: {exc} — fact guard will reject all claims")
        from fact_guard.data_fetcher import GroundingData
        grounding = GroundingData()

    # Extract coupon codes mentioned in the AI reply so the checker can verify them
    mentioned_coupons = extract_coupon_codes_from_text(raw_reply)

    vetted = vet_reply(raw_reply, grounding, mentioned_coupon_codes=mentioned_coupons)
    vetted_reply = vetted.vetted_text

    fact_guard_result = FactGuardResult(
        was_modified=vetted.was_modified,
        claims_detected=[c.claim_type for c in vetted.claims if c.detected],
    )

    if vetted.was_modified:
        logger.info(
            f"FactGuard modified reply | tenant={req.tenant_id} | "
            f"claims={fact_guard_result.claims_detected}"
        )

    # ── Stage 6: Policy Guard ─────────────────────────────────────────────────
    policy_gated = validate_actions(raw_actions, ctx)

    # ── Stage 7: Commerce Permission Guard ────────────────────────────────────
    permission_gated = permission_gate(policy_gated, permissions)

    # ── Stage 8: Action Execution Guard ───────────────────────────────────────
    fully_gated = execution_decide(permission_gated, req.tenant_id)

    # ── Stage 5b: Append branding footer ──────────────────────────────────────
    branding = ctx.get("branding", "")
    if branding and vetted_reply:
        final_reply = f"{vetted_reply}\n\n_{branding}_"
    else:
        final_reply = vetted_reply

    # ── Stage 9a: Collect executable product IDs for memory update ───────────
    approved_product_ids: List[int] = []
    for action in fully_gated:
        if action.get("executable") and action.get("final_payload"):
            fp = action["final_payload"]
            if action["type"] == "suggest_product":
                pid = fp.get("product_id")
                if pid:
                    approved_product_ids.append(pid)
            elif action["type"] in ("suggest_bundle", "propose_order", "create_draft_order"):
                approved_product_ids.extend(fp.get("product_ids", []))

    # ── Stage 9b: Fire-and-forget memory update ───────────────────────────────
    turn_data = {
        "intent":                _infer_intent(req.message),
        "sentiment":             _infer_sentiment(req.message),
        "suggested_product_ids": approved_product_ids,
        "inferred_preferences":  _infer_preferences(req.message),
        "approved_actions": [
            {
                "type":              a["type"],
                "proposed_payload":  a.get("proposed_payload"),
                "policy_result":     a["policy_result"],
                "policy_notes":      a.get("policy_notes", ""),
                "permission_result": a.get("permission_result", "n/a"),
                "permission_notes":  a.get("permission_notes", ""),
                "final_payload":     a.get("final_payload"),
            }
            for a in fully_gated
        ],
        "summary_snippet": f"Customer: {req.message[:200]}",
    }

    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        None,
        update_customer_memory,
        req.tenant_id,
        customer_id,
        req.customer_phone,
        turn_data,
    )

    # ── Return ────────────────────────────────────────────────────────────────
    return OrchestrateResponse(
        reply=final_reply or _default_reply(),
        fact_guard=fact_guard_result,
        actions=[
            GatedAction(
                type=a["type"],
                policy_result=a["policy_result"],
                policy_notes=a.get("policy_notes", ""),
                permission_result=a.get("permission_result", "n/a"),
                permission_notes=a.get("permission_notes", ""),
                executable=a.get("executable", False),
                blocked_reason=a.get("blocked_reason"),
                final_payload=a.get("final_payload"),
            )
            for a in fully_gated
        ],
        customer_segment=customer_segment,
        is_returning=is_returning,
        model_used=model_used,
    )


# ── Health ────────────────────────────────────────────────────────────────────

@router.get("/health")
async def health():
    from engine.claude_client import CLAUDE_API_KEY, CLAUDE_MODEL
    return {
        "service":          "ai-orchestrator",
        "status":           "ok",
        "claude_configured": bool(CLAUDE_API_KEY),
        "model":            CLAUDE_MODEL,
        "pipeline_stages": [
            "customer_memory",
            "commerce_permissions",
            "claude_generate",
            "fact_guard",
            "policy_guard",
            "commerce_permission_guard",
            "action_execution_guard",
            "memory_update",
        ],
    }


# ── Debug endpoints ───────────────────────────────────────────────────────────

@router.get("/customer/{tenant_id}/{phone}")
async def get_customer_memory(tenant_id: int, phone: str):
    try:
        ctx = load_customer_memory(tenant_id, phone)
        ctx.pop("products", None)   # strip large list from debug output
        return ctx
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/permissions/{tenant_id}")
async def get_permissions(tenant_id: int):
    try:
        perms = load_permissions(tenant_id)
        return {
            "tenant_id": tenant_id,
            "permissions": perms.to_dict(),
            "hardcoded_forbidden": [
                "delete_order", "delete_customer", "delete_product",
                "delete_coupon", "cancel_paid_order",
                "hard_delete_draft_order", "modify_paid_order", "bulk_delete",
            ],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Lightweight intent / sentiment heuristics ─────────────────────────────────

def _infer_intent(message: str) -> str:
    text = message.lower()
    if any(w in text for w in ("buy", "order", "اشتري", "اطلب", "طلب")):
        return "order"
    if any(w in text for w in ("discount", "coupon", "خصم", "كوبون", "offer", "عرض")):
        return "discount_inquiry"
    if any(w in text for w in ("product", "price", "سعر", "منتج", "catalog")):
        return "browse"
    if any(w in text for w in ("complaint", "wrong", "broken", "مشكلة", "خطأ", "شكوى")):
        return "complaint"
    if any(w in text for w in ("delivery", "ship", "توصيل", "شحن")):
        return "delivery_inquiry"
    if any(w in text for w in ("track", "where", "status", "تتبع", "وين")):
        return "order_tracking"
    return "general_inquiry"


def _infer_sentiment(message: str) -> str:
    text = message.lower()
    frustrated = {"again", "still", "always", "مجدداً", "كمان", "دائما"}
    negative   = {"angry", "terrible", "worst", "hate", "مزعج", "سيء", "مشكلة"}
    positive   = {"great", "love", "perfect", "ممتاز", "رائع", "شكراً"}
    if any(w in text for w in frustrated): return "frustrated"
    if any(w in text for w in negative):   return "negative"
    if any(w in text for w in positive):   return "positive"
    return "neutral"


def _infer_preferences(message: str) -> Dict[str, Any]:
    text = message.lower()
    prefs: Dict[str, Any] = {}
    if "english" in text or "بالانجليزي" in text: prefs["language"] = "en"
    elif "arabic" in text or "عربي" in text:       prefs["language"] = "ar"
    return {"notes": prefs} if prefs else {}


def _default_reply() -> str:
    return "شكراً لتواصلك معنا! 😊 كيف أقدر أساعدك؟\n\nThanks for reaching out! 😊 How can I assist you?"
