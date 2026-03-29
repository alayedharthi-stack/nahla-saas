"""
AI Client — routes messages through the AI Orchestrator (port 8016)
with automatic fallback to the simpler ai-engine (port 8002).

The orchestrator provides customer memory, personalisation, and policy guard.
The ai-engine provides a stateless fallback when the orchestrator is unavailable.
"""

import logging
import os
from typing import Any, Dict, Optional

import httpx

AI_ORCHESTRATOR_URL = os.getenv("AI_ORCHESTRATOR_URL", "http://localhost:8016/orchestrate")
AI_ENGINE_URL       = os.getenv("AI_ENGINE_URL",        "http://localhost:8002/ai/respond")

logger = logging.getLogger("whatsapp-service.ai_client")


async def get_ai_response(
    tenant: str,
    phone: str,
    message: str,
    tenant_id: Optional[int] = None,
) -> str:
    """
    Attempt orchestrator first (personalised, memory-aware).
    Fall back to ai-engine (stateless) on any error.
    """
    if tenant_id:
        reply = await _call_orchestrator(tenant_id, phone, message)
        if reply is not None:
            return reply

    # Fallback: stateless ai-engine
    return await _call_ai_engine(tenant, phone, message)


async def _call_orchestrator(tenant_id: int, phone: str, message: str) -> Optional[str]:
    payload: Dict[str, Any] = {
        "tenant_id":      tenant_id,
        "customer_phone": phone,
        "message":        message,
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(AI_ORCHESTRATOR_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("reply", "")
    except httpx.HTTPError as exc:
        logger.warning(f"Orchestrator unavailable: {exc} — falling back to ai-engine")
        return None


async def _call_ai_engine(tenant: str, phone: str, message: str) -> str:
    payload = {"tenant": tenant, "phone": phone, "message": message}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(AI_ENGINE_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("response", "Sorry, I couldn't process your message.")
    except httpx.HTTPError as exc:
        logger.error(f"ai-engine also unavailable: {exc}")
        return "عذراً، حدث خطأ تقني. سنتواصل معك قريباً.\n\nSorry, a technical error occurred. We'll reach out to you shortly."
