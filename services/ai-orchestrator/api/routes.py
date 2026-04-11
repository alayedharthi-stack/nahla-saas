"""
Orchestrator API routes
────────────────────────
Compatibility HTTP shell for the legacy `/orchestrate` contract.

Canonical runtime owner:
  backend/modules/ai/orchestrator/adapter.py

This file preserves the existing request/response shape so external callers
can continue using `POST /orchestrate`, but it no longer owns provider
execution directly. All AI execution now flows through the canonical adapter.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_BACKEND_DIR = os.path.join(_REPO_ROOT, "backend")
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

_ORCH_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ORCH_ROOT not in sys.path:
    sys.path.insert(0, _ORCH_ROOT)

logger = logging.getLogger("ai-orchestrator.routes")
router = APIRouter()

from memory.loader import load_customer_memory  # noqa: E402
from commerce.permission_guard import load_permissions  # noqa: E402
from modules.ai.orchestrator.adapter import generate_orchestrate_response


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

    result = await generate_orchestrate_response(
        tenant_id=req.tenant_id,
        customer_phone=req.customer_phone,
        message=req.message,
        conversation_id=req.conversation_id,
    )

    return OrchestrateResponse(**result)


# ── Health ────────────────────────────────────────────────────────────────────

@router.get("/health")
async def health():
    return {
        "service":          "ai-orchestrator",
        "status":           "ok",
        "canonical_owner":  "backend/modules/ai/orchestrator",
        "mode":             "compatibility_shell",
        "pipeline_stages": [
            "compat_request_mapping",
            "canonical_adapter",
            "canonical_pipeline",
            "canonical_engine",
            "compat_response_mapping",
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


