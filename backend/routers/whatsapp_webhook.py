"""
routers/whatsapp_webhook.py
────────────────────────────
Handles incoming WhatsApp messages from Meta Cloud API.

Routes
  GET  /webhook/whatsapp  — Meta verification challenge
  POST /webhook/whatsapp  — Receive incoming messages, route through Engine

Architecture (Platform Brain — selling Nahla to merchants):

  Message Received
       │
       ▼
  IntentEngine.classify()          ← rule-based, <1ms, no AI
       │
       ▼
  SlotUpdater.update()             ← fill platform/size slots
       │
       ▼
  DecisionEngine.decide()          ← next_best_action
       │
       ├── SEND_CHECKOUT_LINK      ← immediate, no AI needed
       ├── SEND_TRIAL_LINK         ← immediate, no AI needed
       ├── SHOW_PLANS              ← immediate, no AI needed
       ├── SHOW_WELCOME_MENU       ← immediate, no AI needed
       ├── SEND_FOUNDER_LINK       ← immediate, no AI needed
       └── GENERATE_AI_REPLY       ← call Claude WITH history + context
                │
                ▼
           StateManager.save()     ← persist state + messages
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

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
from core.conversation_engine import (
    ESCALATE_SUPPORT,
    FILL_SLOT_PLATFORM,
    FILL_SLOT_SIZE,
    GENERATE_AI_REPLY,
    SEND_CHECKOUT_LINK,
    SEND_FOUNDER_LINK,
    SEND_TRIAL_LINK,
    SHOW_PLANS,
    SHOW_WELCOME_MENU,
    ContextBuilder,
    DeduplicationGuard,
    DecisionEngine,
    IntentEngine,
    SlotUpdater,
    StateManager,
    recommend_plan,
)
from core.database import get_db
from core.nahla_knowledge import build_nahla_system_prompt

logger = logging.getLogger("nahla-backend")
router = APIRouter(tags=["WhatsApp Webhook"])


# ═══════════════════════════════════════════════════════════════════════════════
# VERIFICATION
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/webhook/whatsapp")
async def whatsapp_verify(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == WA_VERIFY_TOKEN:
        logger.info("WhatsApp webhook verified by Meta.")
        return PlainTextResponse(hub_challenge)
    logger.warning("WhatsApp webhook verification failed — mode=%s", hub_mode)
    raise HTTPException(status_code=403, detail="Verification failed")


# ═══════════════════════════════════════════════════════════════════════════════
# INCOMING MESSAGES
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/webhook/whatsapp")
async def whatsapp_incoming(request: Request):
    """Receive incoming WhatsApp messages from Meta and route through Engine."""
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        return {"status": "ok"}
    try:
        await _handle_whatsapp_body(body)
    except Exception as exc:
        logger.error("WhatsApp webhook error: %s", exc, exc_info=True)
    return {"status": "ok"}


async def _handle_whatsapp_body(body: Dict[str, Any]) -> None:
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
            for msg in value.get("messages", []):
                await _dispatch_message(phone_number_id, msg, value)


# ═══════════════════════════════════════════════════════════════════════════════
# CORE DISPATCH — The Engine Loop
# ═══════════════════════════════════════════════════════════════════════════════

async def _dispatch_message(
    phone_number_id: str,
    msg: Dict[str, Any],
    value: Dict[str, Any],
) -> None:
    """
    Main entry point for each WhatsApp message.
    Runs the full Engine pipeline:
      Intent → Slots → Decision → Action → Save
    """
    msg_type = msg.get("type")
    sender   = msg.get("from", "")
    used_pid = phone_number_id or WA_PHONE_ID

    # ── Handle interactive button replies ──────────────────────────────────────
    if msg_type == "interactive":
        interactive = msg.get("interactive", {})
        if interactive.get("type") == "button_reply":
            btn_id = interactive.get("button_reply", {}).get("id", "")
            await _handle_button_reply(btn_id=btn_id, phone_id=used_pid, to=sender)
        return

    if msg_type != "text":
        return

    text = msg.get("text", {}).get("body", "").strip()
    if not text:
        return

    logger.info("[Engine] Message from=%s text_len=%d", sender, len(text))

    # ── Open DB session ────────────────────────────────────────────────────────
    db = next(get_db(), None)
    if not db:
        logger.error("[Engine] Could not open DB session")
        return

    try:
        # ── 1. Load conversation state ─────────────────────────────────────────
        state = StateManager.load(db, phone=sender)
        state.turn += 1

        # ── 2. Detect intent ───────────────────────────────────────────────────
        intent = IntentEngine.classify(text, state)
        logger.info("[Engine] phone=%s turn=%d intent=%s stage=%s",
                    sender, state.turn, intent, state.stage)

        # ── 3. Update slots based on intent ───────────────────────────────────
        SlotUpdater.update(state, intent)

        # ── 4. Determine next action ───────────────────────────────────────────
        action = DecisionEngine.decide(intent, state)
        state.last_action = action
        logger.info("[Engine] action=%s", action)

        # ── 5. Execute action ──────────────────────────────────────────────────
        ai_reply: Optional[str] = None

        if action == SHOW_WELCOME_MENU:
            await _send_welcome_menu(phone_id=used_pid, to=sender)
            # Save inbound message (no outbound text — menu is interactive)
            StateManager.save_message(db, sender, text, "inbound")
            conv = StateManager.save(db, state)
            return

        elif action == SEND_CHECKOUT_LINK:
            state.stage = "checkout"
            state.purchase_score = 10
            await _send_checkout_cta(phone_id=used_pid, to=sender)

        elif action == SEND_TRIAL_LINK:
            await _send_trial_cta(phone_id=used_pid, to=sender)

        elif action == SHOW_PLANS:
            await _send_plans_message(phone_id=used_pid, to=sender, db=db)

        elif action == SEND_FOUNDER_LINK:
            await _send_whatsapp_message(
                phone_id=used_pid,
                to=sender,
                text="زين! تقدر تتواصل مع المؤسس مباشرةً على واتساب 👇\nhttps://wa.me/966555906901",
            )

        elif action == ESCALATE_SUPPORT:
            await _send_whatsapp_message(
                phone_id=used_pid,
                to=sender,
                text=(
                    "تواصل مع فريق الدعم:\n"
                    "📧 support@nahlah.ai\n"
                    "أو راسلنا هنا وراح نرد في أقرب وقت 🙏"
                ),
            )

        elif action in (FILL_SLOT_PLATFORM, FILL_SLOT_SIZE):
            # Slot was already updated in step 3.
            # Now decide what to ask next (if anything).
            plan = recommend_plan(state)
            state.recommended_plan = plan

            if DeduplicationGuard.should_ask_store_size(state) and action == FILL_SLOT_PLATFORM:
                # Just got platform — now ask store size
                DeduplicationGuard.mark_asked(state, "store_size")
                platform = state.slots.platform or "منصتك"
                await _send_interactive_reply(
                    phone_id=used_pid,
                    to=sender,
                    body_text=f"ممتاز! نحلة تتكامل مع {platform} مباشرةً 🔗\nمتجرك كبير ولا صغير؟",
                    buttons=[
                        {"type": "reply", "reply": {"id": "store_small", "title": "صغير / ناشئ"}},
                        {"type": "reply", "reply": {"id": "store_big",   "title": "متوسط / كبير"}},
                    ],
                )
            elif action == FILL_SLOT_SIZE:
                # Got store size — recommend plan + CTA
                state.stage = "recommendation"
                plan_text = {
                    "small": "باقة Starter — 899 ريال/شهر ✨",
                    "large": "باقة Pro أو Business 💎",
                }.get(state.slots.store_size or "small", "باقة Starter")
                await _send_cta_url(
                    phone_id=used_pid,
                    to=sender,
                    body_text=f"بناءً على متجرك الأنسب هي {plan_text}\nجرّبها 14 يوم مجاناً — بدون بطاقة.",
                    btn_label="شوف الباقات وسجّل",
                    btn_url="https://app.nahlah.ai/billing",
                )
            else:
                # Fall through to AI reply for natural conversation
                action = GENERATE_AI_REPLY

        # ── 6. Generate AI reply if needed ────────────────────────────────────
        if action == GENERATE_AI_REPLY:
            history = StateManager.load_history(db, phone=sender)
            context_prefix = ContextBuilder.build_context_prefix(state)
            messages = ContextBuilder.build_messages(history, text)
            ai_reply = await _call_claude_with_context(
                messages=messages,
                context_prefix=context_prefix,
                db=db,
            )
            if ai_reply:
                await _send_whatsapp_message(phone_id=used_pid, to=sender, text=ai_reply)
                # Auto-suggest platform question if not asked yet
                if DeduplicationGuard.should_ask_platform(state):
                    DeduplicationGuard.mark_asked(state, "platform")

        # ── 7. Persist messages + state ────────────────────────────────────────
        StateManager.save_message(db, sender, text, "inbound")
        if ai_reply:
            StateManager.save_message(db, sender, ai_reply, "outbound")
        StateManager.save(db, state)

    finally:
        try:
            db.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# CLAUDE — Context-Aware Call
# ═══════════════════════════════════════════════════════════════════════════════

async def _call_claude_with_context(
    messages: list,
    context_prefix: str,
    db=None,
) -> str:
    """
    Call Claude with full conversation history + known context.
    Falls back to orchestrator if configured.
    """
    # ── Try external orchestrator ─────────────────────────────────────────────
    if ORCHESTRATOR_URL and len(messages) > 0:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"{ORCHESTRATOR_URL}/orchestrate",
                    json={
                        "tenant_id": 1,
                        "customer_phone": "engine",
                        "message": messages[-1]["content"] if messages else "",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                reply = data.get("reply") or data.get("response") or data.get("message")
                if reply:
                    return reply
        except Exception as exc:
            logger.warning("Orchestrator unavailable (%s) — direct Claude call", exc)

    # ── Direct Claude call with history ──────────────────────────────────────
    if not ANTHROPIC_API_KEY:
        return "عذراً، الخدمة غير متاحة حالياً. يرجى المحاولة لاحقاً."

    try:
        base_system = build_nahla_system_prompt(db)
        system_prompt = context_prefix + base_system if context_prefix else base_system
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=600,
            system=system_prompt,
            messages=messages,
        )
        return response.content[0].text if response.content else "كيف أقدر أساعدك؟"
    except Exception as exc:
        logger.error("Claude call failed: %s", exc)
        return "عذراً، حدث خطأ مؤقت. يرجى المحاولة مرة أخرى."


# ═══════════════════════════════════════════════════════════════════════════════
# TENANT RESOLVER
# ═══════════════════════════════════════════════════════════════════════════════

async def _resolve_tenant_by_phone(phone_number_id: str) -> int:
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


# ═══════════════════════════════════════════════════════════════════════════════
# WHATSAPP SEND HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def _send_whatsapp_message(phone_id: str, to: str, text: str) -> None:
    """Send a plain text WhatsApp message."""
    if not WA_TOKEN:
        return
    await _post_wa(phone_id, {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    })


async def _send_interactive_reply(
    phone_id: str,
    to: str,
    body_text: str,
    buttons: list,
) -> None:
    """Send a WhatsApp interactive message with reply buttons (max 3)."""
    await _post_wa(phone_id, {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {"buttons": buttons[:3]},  # Meta allows max 3 buttons
        },
    })


async def _send_cta_url(
    phone_id: str,
    to: str,
    body_text: str,
    btn_label: str,
    btn_url: str,
) -> None:
    """Send a WhatsApp CTA URL button message."""
    await _post_wa(phone_id, {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {"text": body_text},
            "action": {
                "name": "cta_url",
                "parameters": {"display_text": btn_label, "url": btn_url},
            },
        },
    })


async def _send_welcome_menu(phone_id: str, to: str) -> None:
    """Send the initial welcome interactive menu."""
    await _send_interactive_reply(
        phone_id=phone_id,
        to=to,
        body_text="هلا! أنا نحلة 🍯\nأساعد أصحاب المتاجر يبيعون أكثر عبر واتساب.\n\nوش تبي تعرف؟",
        buttons=[
            {"type": "reply", "reply": {"id": "menu_how",   "title": "كيف تشتغل؟ 🤔"}},
            {"type": "reply", "reply": {"id": "menu_price", "title": "كم الأسعار؟ 💰"}},
            {"type": "reply", "reply": {"id": "menu_trial", "title": "أبي أجرب 🚀"}},
        ],
    )


async def _send_checkout_cta(phone_id: str, to: str) -> None:
    """Send checkout link when user is ready to subscribe."""
    await _send_cta_url(
        phone_id=phone_id,
        to=to,
        body_text="ممتاز! سجّل الحين وابدأ تجربتك المجانية 14 يوم 🎁\nبدون بطاقة ائتمان.",
        btn_label="سجّل مجاناً الآن",
        btn_url="https://app.nahlah.ai/register",
    )


async def _send_trial_cta(phone_id: str, to: str) -> None:
    """Send trial registration CTA."""
    await _send_cta_url(
        phone_id=phone_id,
        to=to,
        body_text="تقدر تبدأ تجربة 14 يوم مجانية — بدون بطاقة ائتمان 🎁",
        btn_label="ابدأ التجربة المجانية",
        btn_url="https://app.nahlah.ai/register",
    )


async def _send_plans_message(phone_id: str, to: str, db=None) -> None:
    """Show plans overview then offer CTA."""
    # Text summary of plans (from knowledge base or hardcoded fallback)
    plans_text = (
        "🐝 باقات نحلة AI:\n\n"
        "Starter   — 899 ريال/شهر\n"
        "Pro       — 1,499 ريال/شهر\n"
        "Business  — 2,499 ريال/شهر\n\n"
        "كل الباقات: تجربة مجانية 14 يوم — بدون بطاقة.\n\n"
        "متجرك صغير ولا كبير؟ أساعدك تختار الأنسب."
    )
    await _send_whatsapp_message(phone_id=phone_id, to=to, text=plans_text)
    # Follow up with CTA button
    await _send_cta_url(
        phone_id=phone_id,
        to=to,
        body_text="شوف كل التفاصيل والمقارنة بين الباقات 💎",
        btn_label="عرض الباقات كاملة",
        btn_url="https://app.nahlah.ai/billing",
    )


async def _post_wa(phone_id: str, payload: dict) -> None:
    """POST a payload to WhatsApp Cloud API."""
    if not WA_TOKEN:
        return
    url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code not in (200, 201):
                logger.warning("WA post failed: %s %s", resp.status_code, resp.text[:200])
            else:
                logger.debug("WA post ok: to=%s", payload.get("to"))
        except Exception as exc:
            logger.error("WA post error: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════════
# BUTTON REPLY HANDLER  (interactive button taps from menu)
# ═══════════════════════════════════════════════════════════════════════════════

async def _handle_button_reply(btn_id: str, phone_id: str, to: str) -> None:
    """
    Handle quick-reply button taps.
    These are deterministic — no AI needed.
    """
    db = next(get_db(), None)
    state = StateManager.load(db, phone=to) if db else None

    if btn_id == "contact_founder":
        await _send_whatsapp_message(
            phone_id=phone_id,
            to=to,
            text="زين! تقدر تتواصل مع المؤسس مباشرةً على واتساب 👇\nhttps://wa.me/966555906901",
        )

    elif btn_id == "menu_how":
        await _send_interactive_reply(
            phone_id=phone_id,
            to=to,
            body_text=(
                "نحلة ترد على عملاء متجرك في واتساب وتساعدهم يكملون طلباتهم لوحدها 🤖\n"
                "بدون ما تتدخل أنت — 24/7.\n\nمتجرك على أي منصة؟"
            ),
            buttons=[
                {"type": "reply", "reply": {"id": "store_salla", "title": "سلة 🛒"}},
                {"type": "reply", "reply": {"id": "store_zid",   "title": "زد 🛒"}},
                {"type": "reply", "reply": {"id": "store_other", "title": "منصة ثانية"}},
            ],
        )
        if state:
            DeduplicationGuard.mark_asked(state, "platform")

    elif btn_id == "menu_price":
        await _send_plans_message(phone_id=phone_id, to=to, db=db)

    elif btn_id == "menu_trial":
        await _send_trial_cta(phone_id=phone_id, to=to)
        if state:
            state.stage = "checkout"
            state.purchase_score = 10

    elif btn_id in ("store_salla", "store_zid"):
        platform = "سلة" if btn_id == "store_salla" else "زد"
        if state:
            state.slots.platform = platform
        await _send_interactive_reply(
            phone_id=phone_id,
            to=to,
            body_text=f"ممتاز! نحلة تتكامل مع {platform} مباشرةً 🔗\nعندك عملاء كثيرين يسألون على واتساب؟\n\nمتجرك كبير ولا صغير؟",
            buttons=[
                {"type": "reply", "reply": {"id": "store_small", "title": "صغير / ناشئ"}},
                {"type": "reply", "reply": {"id": "store_big",   "title": "متوسط / كبير"}},
            ],
        )
        if state:
            DeduplicationGuard.mark_asked(state, "store_size")

    elif btn_id == "store_other":
        await _send_whatsapp_message(
            phone_id=phone_id,
            to=to,
            text="حالياً نحلة تدعم سلة وزد بشكل كامل.\nأي منصة تستخدم؟ ممكن نشوف إذا في حل 🤝",
        )

    elif btn_id in ("store_small", "store_big"):
        size = "small" if btn_id == "store_small" else "large"
        if state:
            state.slots.store_size = size
            state.stage = "recommendation"
        plan_text = (
            "باقة Starter — 899 ريال/شهر ✨\nالأنسب للمتاجر الناشئة والصغيرة."
            if size == "small"
            else "باقة Pro أو Business 💎\nالأنسب للمتاجر المتوسطة والكبيرة."
        )
        await _send_cta_url(
            phone_id=phone_id,
            to=to,
            body_text=f"{plan_text}\nجرّبها 14 يوم مجاناً — بدون بطاقة.",
            btn_label="شوف الباقات وسجّل",
            btn_url="https://app.nahlah.ai/billing",
        )

    else:
        logger.debug("Unhandled button_reply id=%s", btn_id)

    # Persist state changes from button interaction
    if db and state:
        try:
            StateManager.save_message(db, to, f"[button:{btn_id}]", "inbound")
            StateManager.save(db, state)
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass
