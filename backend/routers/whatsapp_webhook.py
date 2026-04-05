"""
routers/whatsapp_webhook.py
────────────────────────────
Handles incoming WhatsApp messages from Meta Cloud API.

Routes
  GET  /webhook/whatsapp  — Meta verification challenge
  POST /webhook/whatsapp  — Receive incoming messages, route to AI
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict

import anthropic
import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../database")))
from models import Tenant, WhatsAppConnection  # noqa: E402

from core.config import (
    ORCHESTRATOR_URL,
    WA_PHONE_ID,
    WA_TOKEN,
    WA_VERIFY_TOKEN,
)
from core.database import get_db

_ANTHROPIC_KEY = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY", "")
_CLAUDE_MODEL  = "claude-haiku-4-5"

_SYSTEM_PROMPT = """أنت نحلة 🍯 — مساعد ذكي لمنصة نحلة AI، أكبر منصة مبيعات واتساب للمتاجر السعودية.
مهمتك: مساعدة التجار على فهم المنصة، الاشتراك، والاستخدام.

═══════════════════════════════
🏷️ عن منصة نحلة
═══════════════════════════════
نحلة AI هي منصة SaaS سعودية تحوّل واتساب لموظف مبيعات ذكي يعمل 24/7.
الموقع: https://nahlah.ai
لوحة التحكم: https://app.nahlah.ai
البريد: support@nahlah.ai

المميزات الأساسية:
- ردود ذكية بالعامية تفهم أسئلة العملاء وترد فوراً
- الطيار الآلي: يكمل الطلبات من أولها لآخرها بدون تدخل
- استرجاع السلات المتروكة: يراقب ويرسل تذكيرات ذكية
- إعادة الطلب التنبؤي: يتذكر كل عميل ويرسل في الوقت المناسب
- تكامل مع سلة وزد مباشرةً
- تحليلات ومتابعة المبيعات

═══════════════════════════════
💰 الباقات والأسعار
═══════════════════════════════
⚠️ عرض الإطلاق ساري حتى 30 يونيو 2026

باقة المبتدئ (Starter):
- السعر الأصلي: 899 ريال/شهر
- سعر الإطلاق: 449 ريال/شهر ✨
- حتى 1,000 محادثة/شهر
- 3 أتمتات فعّالة
- حملتان/شهر
- تحليلات أساسية
- تجربة مجانية 14 يوم

باقة النمو (Growth):
- السعر الأصلي: 1,699 ريال/شهر
- سعر الإطلاق: 849 ريال/شهر ✨
- حتى 5,000 محادثة/شهر
- أتمتات غير محدودة
- 10 حملات/شهر
- تحليلات متقدمة
- أولوية الدعم
- تجربة مجانية 14 يوم

باقة التوسع (Scale):
- السعر الأصلي: 2,999 ريال/شهر
- سعر الإطلاق: 1,499 ريال/شهر ✨
- محادثات غير محدودة
- أتمتات وحملات غير محدودة
- تقارير مخصصة
- دعم مخصص 24/7
- وصول API كامل

═══════════════════════════════
🔗 التكاملات
═══════════════════════════════
- سلة (Salla) ✅
- زد (Zid) ✅
- واتساب Business API عبر Meta ✅

═══════════════════════════════
💳 الدفع
═══════════════════════════════
- مدى، فيزا، ماستركارد (عبر Moyasar)
- للاشتراك: https://app.nahlah.ai/billing

═══════════════════════════════
🚀 البدء
═══════════════════════════════
1. سجّل حساب مجاني: https://app.nahlah.ai/register
2. اربط متجرك (سلة أو زد)
3. اربط واتساب
4. شغّل الطيار الآلي

═══════════════════════════════
📞 الدعم
═══════════════════════════════
واتساب: هذا الرقم مباشرة
البريد: support@nahlah.ai
لوحة التحكم: https://app.nahlah.ai

═══════════════════════════════
اللغة والأسلوب:
═══════════════════════════════
- تحدث بالعربية واللهجة السعودية دائماً كافتراضي
- إذا بدأ أحد بالإنجليزية أو طلبها، انتقل للإنجليزية فوراً
- استخدم: "وش تبي؟" "كيف أقدر أساعدك؟" "بكل سرور" "تفضل"
- لا تستخدم "شنو" أبداً — هي عراقية وليست سعودية
- لا تستخدم ** أو * — واتساب لا يعرضها صح
- ردودك قصيرة ومفيدة (3-5 جمل)
- لا تخترع معلومات — إذا ما تعرف شيء قل "تواصل مع فريق الدعم"
"""

logger = logging.getLogger("nahla-backend")

router = APIRouter(tags=["WhatsApp Webhook"])

# ── Meta verification ──────────────────────────────────────────────────────────

@router.get("/webhook/whatsapp")
async def whatsapp_verify(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """
    Meta sends a GET request to verify the webhook URL.
    Respond with hub.challenge if the verify token matches.
    """
    if hub_mode == "subscribe" and hub_verify_token == WA_VERIFY_TOKEN:
        logger.info("WhatsApp webhook verified by Meta.")
        return PlainTextResponse(hub_challenge)

    logger.warning(
        "WhatsApp webhook verification failed. "
        "mode=%s token_match=%s",
        hub_mode,
        hub_verify_token == WA_VERIFY_TOKEN,
    )
    raise HTTPException(status_code=403, detail="Verification failed")


# ── Incoming messages ──────────────────────────────────────────────────────────

@router.post("/webhook/whatsapp")
async def whatsapp_incoming(request: Request):
    """
    Receives incoming WhatsApp messages and events from Meta.
    Identifies tenant by phone_number_id, then forwards to AI orchestrator.
    """
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        return {"status": "ok"}

    # Confirm receipt to Meta immediately
    try:
        await _handle_whatsapp_body(body)
    except Exception as exc:
        logger.error("WhatsApp webhook processing error: %s", exc, exc_info=True)

    return {"status": "ok"}


# ── Processing helpers ─────────────────────────────────────────────────────────

async def _handle_whatsapp_body(body: Dict[str, Any]) -> None:
    """Parse Meta webhook body and dispatch each message to the AI."""
    entries = body.get("entry", [])
    for entry in entries:
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
            messages = value.get("messages", [])

            for msg in messages:
                await _dispatch_message(phone_number_id, msg, value)


async def _dispatch_message(
    phone_number_id: str,
    msg: Dict[str, Any],
    value: Dict[str, Any],
) -> None:
    """
    Route a single incoming WhatsApp message to the AI orchestrator,
    then send the AI reply back to the customer.
    """
    msg_type = msg.get("type")
    sender   = msg.get("from", "")
    msg_id   = msg.get("id", "")

    # Only handle text messages for now
    if msg_type != "text":
        logger.debug("Skipping non-text message type=%s from=%s", msg_type, sender)
        return

    text = msg.get("text", {}).get("body", "").strip()
    if not text:
        return

    logger.info("Incoming WhatsApp message from=%s phone_id=%s", sender, phone_number_id)

    # ── Resolve tenant ────────────────────────────────────────────────────────
    tenant_id = await _resolve_tenant_by_phone(phone_number_id)

    # ── Call AI orchestrator ──────────────────────────────────────────────────
    ai_reply = await _call_orchestrator(tenant_id, sender, text)

    # ── Send reply via WhatsApp ───────────────────────────────────────────────
    if ai_reply:
        await _send_whatsapp_message(
            phone_id=phone_number_id or WA_PHONE_ID,
            to=sender,
            text=ai_reply,
        )


async def _resolve_tenant_by_phone(phone_number_id: str) -> int | None:
    """
    Find the tenant whose WhatsApp phone_number_id matches.
    Falls back to tenant_id=1 for single-tenant / development setups.
    """
    if not phone_number_id:
        return 1

    try:
        db = next(get_db())
        try:
            conn = (
                db.query(WhatsAppConnection)
                .filter(WhatsAppConnection.phone_number_id == phone_number_id)
                .first()
            )
            return conn.tenant_id if conn else 1
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Could not resolve tenant by phone_id=%s: %s", phone_number_id, exc)
        return 1


async def _call_orchestrator(
    tenant_id: int | None,
    customer_phone: str,
    message: str,
) -> str | None:
    """Try external orchestrator first; fall back to direct Claude call."""
    # ── Try external orchestrator ─────────────────────────────────────────────
    if ORCHESTRATOR_URL:
        payload = {
            "tenant_id": tenant_id or 1,
            "customer_phone": customer_phone,
            "message": message,
        }
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(f"{ORCHESTRATOR_URL}/orchestrate", json=payload)
                resp.raise_for_status()
                data = resp.json()
                reply = data.get("reply") or data.get("response") or data.get("message")
                if reply:
                    return reply
        except Exception as exc:
            logger.warning("Orchestrator unavailable (%s) — falling back to Claude direct", exc)

    # ── Direct Claude call ────────────────────────────────────────────────────
    return await _call_claude_direct(message)


async def _call_claude_direct(message: str) -> str:
    """Call Claude API directly and return a text reply."""
    if not _ANTHROPIC_KEY:
        logger.error("ANTHROPIC_API_KEY not set — cannot generate AI reply")
        return "عذراً، الخدمة غير متاحة حالياً. يرجى المحاولة لاحقاً."
    try:
        client = anthropic.Anthropic(api_key=_ANTHROPIC_KEY)
        response = client.messages.create(
            model=_CLAUDE_MODEL,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": message}],
        )
        return response.content[0].text if response.content else "كيف يمكنني مساعدتك؟"
    except Exception as exc:
        logger.error("Claude direct call failed: %s", exc)
        return "عذراً، حدث خطأ مؤقت. يرجى المحاولة مرة أخرى."


async def _send_whatsapp_message(phone_id: str, to: str, text: str) -> None:
    """Send a WhatsApp text message via Meta Cloud API."""
    if not WA_TOKEN:
        logger.warning("WHATSAPP_TOKEN not set — cannot send reply.")
        return

    url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    headers = {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code not in (200, 201):
                logger.error("WhatsApp send failed: %s %s", resp.status_code, resp.text)
            else:
                logger.info("WhatsApp reply sent to=%s", to)
    except Exception as exc:
        logger.error("WhatsApp send error: %s", exc)
