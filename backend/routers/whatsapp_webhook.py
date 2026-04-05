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
from typing import Any, Dict

import anthropic
import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from models import Tenant, WhatsAppConnection  # noqa: E402

from core.config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    ORCHESTRATOR_URL,
    WA_PHONE_ID,
    WA_TOKEN,
    WA_VERIFY_TOKEN,
)
from core.database import get_db
from core.nahla_knowledge import build_nahla_system_prompt


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
    db = next(get_db(), None)
    ai_reply = await _call_orchestrator(tenant_id, sender, text, db=db)
    if db:
        try:
            db.close()
        except Exception:
            pass

    # ── Send reply via WhatsApp ───────────────────────────────────────────────
    if ai_reply:
        used_phone_id = phone_number_id or WA_PHONE_ID
        await _send_whatsapp_message(phone_id=used_phone_id, to=sender, text=ai_reply)

        # Send CTA buttons when reply is about pricing, plans, or registration
        if _should_send_cta_buttons(text, ai_reply):
            await _send_cta_buttons(phone_id=used_phone_id, to=sender)


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
    db=None,
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
    return await _call_claude_direct(message, db=db)


async def _call_claude_direct(message: str, db=None) -> str:
    """Call Claude API directly and return a text reply."""
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set — cannot generate AI reply")
        return "عذراً، الخدمة غير متاحة حالياً. يرجى المحاولة لاحقاً."
    try:
        system_prompt = build_nahla_system_prompt(db)
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=512,
            system=system_prompt,
            messages=[{"role": "user", "content": message}],
        )
        return response.content[0].text if response.content else "كيف أقدر أساعدك؟"
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


# ── CTA Button helpers ─────────────────────────────────────────────────────────

# Keywords that signal CLEAR BUYING INTENT — only then send CTA buttons
_BUYING_INTENT_KEYWORDS = (
    # Arabic intent signals
    "أبي أجرب", "ابي اجرب", "أبي أبدأ", "ابي ابدا", "أبي أشترك", "ابي اشترك",
    "كيف أسجل", "كيف اسجل", "وين الرابط", "ارسل الرابط", "أرسل الرابط",
    "أبي أشوف الباقات", "ابدأ التجربة", "ابدا التجربة", "جاهز", "أبدأ الآن",
    "سجّلني", "سجلني", "اشتراك", "أشترك",
    # English intent signals
    "sign me up", "register now", "let's start", "how do i start",
    "send the link", "i want to try", "start trial", "sign up",
)


def _should_send_cta_buttons(user_text: str, ai_reply: str) -> bool:
    """
    Return True only when the user shows CLEAR buying intent.
    Avoids sending buttons on general questions about pricing/features.
    """
    user_lower = user_text.lower()
    # Also trigger if the AI reply itself contains the registration link
    if "app.nahlah.ai/register" in ai_reply:
        return True
    return any(kw in user_lower for kw in _BUYING_INTENT_KEYWORDS)


async def _send_cta_buttons(phone_id: str, to: str) -> None:
    """
    Send a CTA URL button (register) + a follow-up text with other links.
    WhatsApp Cloud API supports one URL button per interactive message (cta_url type).
    """
    if not WA_TOKEN:
        return

    url_endpoint = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json",
    }

    def _cta(body_text: str, btn_label: str, btn_url: str) -> dict:
        return {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "cta_url",
                "body": {"text": body_text},
                "action": {
                    "name": "cta_url",
                    "parameters": {
                        "display_text": btn_label,
                        "url": btn_url,
                    },
                },
            },
        }

    messages = [
        ("register", _cta(
            "جرّب نحلة مجاناً 14 يوم — بدون بطاقة ائتمان 🎁",
            "سجّل مجاناً الآن",
            "https://app.nahlah.ai/register",
        )),
        ("founder", _cta(
            "تواصل مع المؤسس والمدير التنفيذي مباشرةً 👋",
            "تواصل مع المؤسس",
            "https://wa.me/966555906901",
        )),
        ("billing", _cta(
            "شوف كل الباقات والأسعار بالتفصيل 💎",
            "عرض الباقات",
            "https://app.nahlah.ai/billing",
        )),
    ]

    async with httpx.AsyncClient(timeout=15) as client:
        for label, payload in messages:
            try:
                resp = await client.post(url_endpoint, json=payload, headers=headers)
                if resp.status_code not in (200, 201):
                    logger.warning("CTA %s send failed: %s %s", label, resp.status_code, resp.text)
                else:
                    logger.info("CTA %s sent to=%s", label, to)
            except Exception as exc:
                logger.error("CTA %s send error: %s", label, exc)
