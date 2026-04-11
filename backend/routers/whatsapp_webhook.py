"""
routers/whatsapp_webhook.py  v2
────────────────────────────────
Platform Brain — WhatsApp webhook with full Engine integration.

Engine pipeline per message:
  ① Idempotency check        (skip duplicate webhooks)
  ② Load ConversationState   (from PostgreSQL)
  ③ IntentEngine.classify()  (rule-based, <1ms)
  ④ SlotUpdater.update()     (fill platform/size slots)
  ⑤ StageTransitionEngine    (advance stage if criteria met)
  ⑥ DecisionEngine.decide()  (returns action + decision_reason)
  ⑦ Execute action           (deterministic — Claude only for GENERATE_AI_REPLY)
  ⑧ FactGuard.verify_reply() (scan for hallucinations)
  ⑨ StateManager.save()      (persist state + messages)
  ⑩ ObservabilityLogger.log()(write full trace to DB)
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import anthropic
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from models import WhatsAppConnection

from core.config import ANTHROPIC_API_KEY, CLAUDE_MODEL, ORCHESTRATOR_URL, WA_PHONE_ID, WA_TOKEN, WA_VERIFY_TOKEN
from core.conversation_engine import (
    # Actions
    DETERMINISTIC_ACTIONS,
    ESCALATE_SUPPORT,
    FILL_SLOT_PLATFORM,
    FILL_SLOT_SIZE,
    GENERATE_AI_REPLY,
    SEND_CHECKOUT_LINK,
    SEND_FOUNDER_LINK,
    SEND_TRIAL_LINK,
    SHOW_PLANS,
    SHOW_WELCOME_MENU,
    # Classes
    ContextBuilder,
    DeduplicationGuard,
    DecisionEngine,
    FactGuard,
    IdempotencyGuard,
    IntentEngine,
    ObservabilityLogger,
    SlotUpdater,
    StateManager,
    StageTransitionEngine,
    TurnLog,
    recommend_plan,
)
from core.database import get_db
from core.nahla_knowledge import build_nahla_system_prompt
from modules.ai.orchestrator.adapter import generate_ai_reply

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
    db=Depends(get_db),
):
    if hub_mode != "subscribe" or not hub_verify_token:
        raise HTTPException(status_code=403, detail="Verification failed")

    # 1) Check platform-level token (Nahla's own WhatsApp)
    if hub_verify_token == WA_VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge)

    # 2) Check per-tenant tokens (merchant WhatsApp connections)
    from models import TenantSettings  # noqa: PLC0415
    try:
        matches = (
            db.query(TenantSettings)
            .filter(TenantSettings.whatsapp_settings.op("->>")("verify_token") == hub_verify_token)
            .first()
        )
        if matches:
            logger.info("[Webhook] Verified tenant webhook token for tenant_id=%s", matches.tenant_id)
            return PlainTextResponse(hub_challenge)
    except Exception as exc:
        logger.warning("[Webhook] Per-tenant token lookup failed: %s", exc)

    raise HTTPException(status_code=403, detail="Verification failed")


# ═══════════════════════════════════════════════════════════════════════════════
# INCOMING MESSAGES
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/webhook/whatsapp")
async def whatsapp_incoming(request: Request):
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        return {"status": "ok"}
    try:
        await _handle_whatsapp_body(body)
    except Exception as exc:
        logger.error("[Webhook] Unhandled error: %s", exc, exc_info=True)
    return {"status": "ok"}


async def _handle_whatsapp_body(body: Dict[str, Any]) -> None:
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
            for msg in value.get("messages", []):
                await _dispatch_message(phone_number_id, msg, value)


# ═══════════════════════════════════════════════════════════════════════════════
# CORE DISPATCH — Full Engine Pipeline
# ═══════════════════════════════════════════════════════════════════════════════

async def _dispatch_message(
    phone_number_id: str,
    msg: Dict[str, Any],
    value: Dict[str, Any],
) -> None:
    t_start  = time.monotonic()
    msg_type = msg.get("type")
    sender   = msg.get("from", "")
    msg_id   = msg.get("id", "")

    # ── TRACE: log every incoming webhook ─────────────────────────────────────
    logger.info(
        "[TRACE][1/6] INCOMING_WEBHOOK | phone_number_id=%s sender=%s msg_id=%s msg_type=%s",
        phone_number_id, sender, msg_id, msg_type,
    )

    # NEVER fall back to WA_PHONE_ID — that would route the message to the wrong tenant.
    # If phone_number_id is missing from the webhook metadata, drop the message.
    if not phone_number_id:
        logger.error(
            "[Webhook] DROPPED — phone_number_id missing from metadata. "
            "msg_type=%s from=%s msg_id=%s",
            msg_type, sender, msg_id,
        )
        return

    # ── Open DB session early (needed for tenant lookup) ─────────────────────
    db = next(get_db(), None)
    if not db:
        logger.error("[Engine] Cannot open DB session for phone=%s", sender)
        return

    # ── Resolve tenant + verified phone_number_id from DB ────────────────────
    # Look up by the phone_number_id that RECEIVED the message.
    # Prefer merchant tenants (tenant_id > 1) over the platform admin tenant,
    # and prefer status='connected' over pending states.
    from sqlalchemy import case as _case  # noqa: PLC0415
    wa_conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.phone_number_id == phone_number_id)
        .order_by(
            # connected status first
            _case((WhatsAppConnection.status == "connected", 0), else_=1),
            # merchant tenants before admin (tenant_id=1)
            _case((WhatsAppConnection.tenant_id == 1, 1), else_=0),
        )
        .first()
    )

    if wa_conn:
        # Use the DB-stored phone_number_id (verified) — not the raw webhook value
        used_pid            = wa_conn.phone_number_id
        resolved_tenant_id  = wa_conn.tenant_id
        logger.info(
            "[TRACE][2/6] TENANT_RESOLVED | webhook_phone_id=%s db_phone_id=%s tenant_id=%s",
            phone_number_id, used_pid, resolved_tenant_id,
        )
        # Guard: mismatch between webhook phone_id and DB phone_id
        if phone_number_id != used_pid:
            logger.critical(
                "[TRACE] MISMATCH DETECTED | webhook_phone_id=%s != db_phone_id=%s — DROPPING",
                phone_number_id, used_pid,
            )
            return
    else:
        # No connected tenant found for this phone_number_id — drop message
        logger.warning(
            "[Webhook] DROPPED — no WhatsAppConnection for phone_number_id=%s from=%s",
            phone_number_id, sender,
        )
        return

    # ── Handle interactive button replies ──────────────────────────────────────
    if msg_type == "interactive":
        if msg.get("interactive", {}).get("type") == "button_reply":
            btn_id = msg["interactive"]["button_reply"].get("id", "")
            await _handle_button_reply(
                btn_id=btn_id, phone_id=used_pid, to=sender,
                tenant_id=resolved_tenant_id, db=db,
            )
        return

    if msg_type != "text":
        return

    text = msg.get("text", {}).get("body", "").strip()
    if not text:
        return

    # ── Merchant bypass: tenant_id > 1 uses store AI, not platform sales bot ──
    PLATFORM_TENANT_ID = 1
    if resolved_tenant_id != PLATFORM_TENANT_ID:
        await _handle_merchant_message(
            phone_id=used_pid, to=sender, text=text,
            tenant_id=resolved_tenant_id, db=db,
        )
        return

    turn_log: Optional[TurnLog] = None
    effective_tenant_id = resolved_tenant_id

    try:
        # ── ① Load state — scoped to the correct merchant tenant ──────────────
        state = StateManager.load(db, phone=sender, tenant_id=effective_tenant_id)
        stage_before = state.stage
        logger.info(
            "[TRACE][3/6] SESSION_LOADED | tenant_id=%s sender=%s stage=%s",
            effective_tenant_id, sender, stage_before,
        )

        # ── ③ Idempotency check ──────────────────────────────────────────────
        if msg_id and IdempotencyGuard.is_duplicate(state, msg_id):
            logger.info("[Idempotency] Skipping duplicate msg_id=%s from=%s", msg_id, sender)
            ObservabilityLogger.log(db, TurnLog(
                phone=sender, turn=state.turn, raw_message=text,
                detected_intent="DUPLICATE", confidence=1.0,
                extracted_slots=[], stage_before=stage_before, stage_after=stage_before,
                stage_transition=None, decision="SKIP", decision_reason="idempotency_duplicate",
                ai_called=False, idempotency_skip=True, latency_ms=0,
            ))
            return

        state.turn += 1
        if msg_id:
            IdempotencyGuard.mark_processed(state, msg_id)

        # ── ③ Intent detection ───────────────────────────────────────────────
        intent, confidence = IntentEngine.classify(text, state)
        logger.info("[Engine] phone=%s turn=%d intent=%s conf=%.1f stage=%s",
                    sender, state.turn, intent, confidence, state.stage)

        # ── ④ Slot update ────────────────────────────────────────────────────
        extracted_slots = SlotUpdater.update(state, intent)

        # ── ⑤ Stage transition ───────────────────────────────────────────────
        stage_transition = StageTransitionEngine.apply(state, intent)

        # ── ⑥ Decision ───────────────────────────────────────────────────────
        action, decision_reason = DecisionEngine.decide(intent, state)
        state.last_action = action
        ai_called = action == GENERATE_AI_REPLY
        logger.info("[Engine] action=%s reason=%s ai_called=%s", action, decision_reason, ai_called)

        # ── ⑦ Execute action ─────────────────────────────────────────────────
        response_text: Optional[str] = None
        fact_guard_issues: List[str] = []

        if action == SHOW_WELCOME_MENU:
            await _send_welcome_menu(phone_id=used_pid, to=sender)

        elif action == SEND_CHECKOUT_LINK:
            state.stage = "checkout"
            await _send_checkout_cta(phone_id=used_pid, to=sender)

        elif action == SEND_TRIAL_LINK:
            await _send_trial_cta(phone_id=used_pid, to=sender)

        elif action == SHOW_PLANS:
            await _send_plans_message(phone_id=used_pid, to=sender, db=db)

        elif action == SEND_FOUNDER_LINK:
            response_text = "زين! تقدر تتواصل مع المؤسس مباشرةً على واتساب 👇\nhttps://wa.me/966555906901"
            await _send_whatsapp_message(phone_id=used_pid, to=sender, text=response_text,
                                         _tenant_id=effective_tenant_id, _db=db)

        elif action == ESCALATE_SUPPORT:
            response_text = "تواصل مع فريق الدعم:\n📧 support@nahlah.ai"
            await _send_whatsapp_message(phone_id=used_pid, to=sender, text=response_text,
                                         _tenant_id=effective_tenant_id, _db=db)

        elif action == FILL_SLOT_PLATFORM:
            # Slot already filled — ask store size if not yet asked
            state.recommended_plan = recommend_plan(state)
            if DeduplicationGuard.should_ask_store_size(state):
                DeduplicationGuard.mark_asked(state, "ask_store_size")
                platform = state.slots.platform or "منصتك"
                await _send_interactive_reply(
                    phone_id=used_pid, to=sender,
                    body_text=f"ممتاز! نحلة تتكامل مع {platform} مباشرةً 🔗\nمتجرك كبير ولا صغير؟",
                    buttons=[
                        {"type": "reply", "reply": {"id": "store_small", "title": "صغير / ناشئ"}},
                        {"type": "reply", "reply": {"id": "store_big",   "title": "متوسط / كبير"}},
                    ],
                )
            else:
                # Store size already known — go to recommendation
                action = GENERATE_AI_REPLY
                ai_called = True

        elif action == FILL_SLOT_SIZE:
            state.stage = "recommendation"
            state.recommended_plan = recommend_plan(state)
            plan_text = {
                "small": "باقة Starter — 899 ريال/شهر ✨",
                "large": "باقة Business أو Pro 💎",
            }.get(state.slots.store_size or "small", "باقة Starter")
            await _send_cta_url(
                phone_id=used_pid, to=sender,
                body_text=f"الأنسب لمتجرك: {plan_text}\nجرّبها 14 يوم مجاناً — بدون بطاقة.",
                btn_label="شوف الباقات وسجّل",
                btn_url="https://app.nahlah.ai/billing",
            )

        # ── ⑦ AI reply — only for GENERATE_AI_REPLY ─────────────────────────
        if ai_called:
            logger.info(
                "[TRACE][4/6] CONTEXT_LOADED | tenant_id=%s sender=%s action=%s",
                effective_tenant_id, sender, action,
            )
            history  = StateManager.load_history(db, phone=sender, tenant_id=effective_tenant_id)
            messages = ContextBuilder.build_messages(history, text)
            state_ctx = ContextBuilder.build_system_injection(state, action, decision_reason)
            response_text = await _call_claude_with_context(
                messages=messages,
                state_injection=state_ctx,
                db=db,
            )
            # ── ⑧ FactGuard — verify reply ────────────────────────────────
            if response_text:
                is_clean, fact_guard_issues = FactGuard.verify_reply(response_text)
                if not is_clean:
                    logger.warning("[FactGuard] Issues in reply: %s", fact_guard_issues)
                await _send_whatsapp_message(phone_id=used_pid, to=sender, text=response_text,
                                             _tenant_id=effective_tenant_id, _db=db)

        # ── ⑨ Persist messages + state ────────────────────────────────────────
        StateManager.save_message(db, sender, text,          "inbound",  tenant_id=effective_tenant_id)
        if response_text:
            StateManager.save_message(db, sender, response_text, "outbound", tenant_id=effective_tenant_id)
        StateManager.save(db, state, tenant_id=effective_tenant_id)

        # ── ⑩ Observability ──────────────────────────────────────────────────
        latency_ms = int((time.monotonic() - t_start) * 1000)
        ObservabilityLogger.log(db, TurnLog(
            phone=sender,
            turn=state.turn,
            raw_message=text,
            detected_intent=intent,
            confidence=confidence,
            extracted_slots=extracted_slots,
            stage_before=stage_before,
            stage_after=state.stage,
            stage_transition=stage_transition,
            decision=action,
            decision_reason=decision_reason,
            ai_called=ai_called,
            fact_guard_issues=fact_guard_issues,
            response_text=response_text,
            latency_ms=latency_ms,
        ), tenant_id=effective_tenant_id)
        logger.info(
            "[Engine] ✅ DONE | tenant=%s from_phone_id=%s to=%s intent=%s action=%s stage=%s→%s latency=%dms",
            effective_tenant_id, used_pid, sender, intent, action, stage_before, state.stage, latency_ms,
        )

    finally:
        try:
            db.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# MERCHANT AI HANDLER — bypasses platform sales logic entirely
# ═══════════════════════════════════════════════════════════════════════════════

async def _handle_merchant_message(
    phone_id: str,
    to: str,
    text: str,
    tenant_id: int,
    db,
) -> None:
    """
    For merchant tenants (tenant_id > 1): reply using the store's own AI context.
    Bypasses the platform sales engine (intent/stage/decision) entirely.
    """
    logger.info("[Merchant] tenant=%s from=%s text_snippet=%s", tenant_id, to, text[:60])

    if not ANTHROPIC_API_KEY:
        logger.error("[Merchant] No ANTHROPIC_API_KEY — cannot generate reply")
        return

    try:
        from core.store_knowledge import build_ai_context  # noqa: PLC0415

        # Load store context for this merchant
        store_context = build_ai_context(db, tenant_id, customer_phone=to, product_query=text)

        # Load recent conversation history for continuity
        history = StateManager.load_history(db, phone=to, tenant_id=tenant_id)
        messages: list = []
        for turn in history[-15:]:
            role = "user" if turn.get("direction") == "inbound" else "assistant"
            body = (turn.get("body") or "").strip()
            if not body:
                continue
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += f"\n{body}"
            else:
                messages.append({"role": role, "content": body})
        # Ensure last turn is current user message
        if not messages or messages[-1]["role"] != "user":
            messages.append({"role": "user", "content": text})
        elif messages[-1]["content"] != text:
            messages.append({"role": "user", "content": text})

        system_prompt = f"""أنت مساعد ذكي لمتجر إلكتروني. مهمتك الرد على استفسارات العملاء بأسلوب ودي واحترافي باللغة العربية.

استخدم المعلومات التالية للإجابة بدقة — لا تخترع معلومات خارجها:

{store_context}

تعليمات:
- أجب باختصار وبوضوح
- لا تذكر منصة نحلة أو أي منصة SaaS أخرى
- إذا لم تجد إجابة في البيانات المتاحة، قل للعميل أنك ستتحقق وتعود إليه
- تحدث كموظف خدمة العملاء للمتجر مباشرةً"""

        history_transcript = "\n".join(
            f"{m['role']}: {m['content']}" for m in messages[:-1]
        ).strip()
        full_prompt = system_prompt
        if history_transcript:
            full_prompt += f"\n\nسجل المحادثة الأخيرة:\n{history_transcript}"

        payload = generate_ai_reply(
            tenant_id=tenant_id,
            customer_phone=to,
            message=text,
            store_name=store_context.get("store_name", ""),
            channel="whatsapp",
            locale="ar",
            context_metadata=store_context,
            prompt_overrides={"__full_system_prompt": full_prompt},
            provider_hint="anthropic",
        )
        reply = payload.reply_text.strip() or "كيف أقدر أساعدك؟"

        # Save messages to history
        StateManager.save_message(db, to, text,  "inbound",  tenant_id=tenant_id)
        StateManager.save_message(db, to, reply, "outbound", tenant_id=tenant_id)

        await _send_whatsapp_message(
            phone_id=phone_id, to=to, text=reply,
            _tenant_id=tenant_id, _db=db,
        )
        logger.info("[Merchant] replied tenant=%s to=%s", tenant_id, to)

    except Exception as exc:
        logger.error("[Merchant] Error generating reply for tenant=%s: %s", tenant_id, exc)


# ═══════════════════════════════════════════════════════════════════════════════
# CLAUDE — Context-Aware Call (only reached via GENERATE_AI_REPLY)
# ═══════════════════════════════════════════════════════════════════════════════

async def _call_claude_with_context(
    messages: list,
    state_injection: str,
    db=None,
) -> str:
    """
    Call Claude with:
    - FactGuard block (ground truth — no hallucinations)
    - State injection (what is known about this user)
    - Recent message history
    """
    if not ANTHROPIC_API_KEY:
        return "عذراً، الخدمة غير متاحة حالياً. يرجى المحاولة لاحقاً."

    try:
        base_system  = build_nahla_system_prompt(db)
        fact_block   = FactGuard.build_fact_block()
        system_prompt = fact_block + state_injection + base_system

        history_transcript = "\n".join(
            f"{m['role']}: {m['content']}" for m in messages[:-1]
        ).strip()
        full_prompt = system_prompt
        if history_transcript:
            full_prompt += f"\n\nRecent conversation history:\n{history_transcript}"

        payload = generate_ai_reply(
            tenant_id=None,
            customer_phone="",
            message=(messages[-1].get("content", "") if messages else ""),
            store_name="Nahla",
            channel="whatsapp",
            locale="ar",
            context_metadata={},
            prompt_overrides={"__full_system_prompt": full_prompt},
            provider_hint="anthropic",
        )
        return payload.reply_text.strip() or "كيف أقدر أساعدك؟"
    except Exception as exc:
        logger.error("[Claude] Call failed: %s", exc)
        return "عذراً، حدث خطأ مؤقت. يرجى المحاولة مرة أخرى."


# ═══════════════════════════════════════════════════════════════════════════════
# WHATSAPP SEND HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def _post_wa(
    phone_id: str,
    payload: dict,
    _tenant_id: Optional[int] = None,
    _store_name: str = "unknown",
    _db=None,
) -> None:
    # Resolve the correct token: prefer the tenant's own token (Embedded Signup)
    # over the global platform token (Direct mode).
    send_token = WA_TOKEN  # default: platform token
    if _tenant_id and _db:
        try:
            from database.models import WhatsAppConnection  # noqa: PLC0415
            wa_conn = _db.query(WhatsAppConnection).filter_by(tenant_id=_tenant_id).first()
            if wa_conn and wa_conn.access_token:
                send_token = wa_conn.access_token
        except Exception:
            pass

    if not send_token:
        logger.warning("[WA] No token available for tenant=%s — skipping send", _tenant_id)
        return

    # Fetch store name from DB if not provided
    if _store_name == "unknown" and _tenant_id and _db:
        try:
            from core.tenant import get_or_create_tenant  # noqa: PLC0415
            t = get_or_create_tenant(_db, _tenant_id)
            _store_name = getattr(t, "store_name", None) or getattr(t, "name", None) or f"tenant_{_tenant_id}"
        except Exception:
            pass

    token_tail = send_token[-6:] if send_token and len(send_token) >= 6 else "EMPTY"

    logger.info(
        "[SEND_DEBUG] tenant_id=%s store=%s phone_number_id=%s token_tail=%s to=%s",
        _tenant_id, _store_name, phone_id, token_tail, payload.get("to", "?"),
    )

    # Lightweight in-process throttling to avoid accidental burst sends to the
    # same recipient. This is not a queue, but it protects against runaway
    # loops/retries within a single process.
    from observability.rate_limiter import check_rate_limit  # noqa: PLC0415
    recipient = str(payload.get("to") or "")
    rate_key = f"wa-send:{_tenant_id or 'platform'}:{recipient}"
    if not check_rate_limit(rate_key, max_count=6, window_seconds=10):
        logger.warning(
            "[WA] throttled burst send | tenant_id=%s to=%s phone_number_id=%s",
            _tenant_id, recipient, phone_id,
        )
        return
    if not check_rate_limit(rate_key, max_count=20, window_seconds=60):
        logger.warning(
            "[WA] throttled minute send | tenant_id=%s to=%s phone_number_id=%s",
            _tenant_id, recipient, phone_id,
        )
        return

    url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {send_token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
            logger.info(
                "[SEND_DEBUG] Meta response | status=%s phone_number_id=%s",
                resp.status_code, phone_id,
            )
            if resp.status_code not in (200, 201):
                logger.warning("[WA] post failed: %s %.200s", resp.status_code, resp.text)
        except Exception as exc:
            logger.error("[WA] post error: %s", exc)


async def _send_whatsapp_message(
    phone_id: str, to: str, text: str,
    _tenant_id: Optional[int] = None, _store_name: str = "unknown", _db=None,
) -> None:
    await _post_wa(phone_id, {
        "messaging_product": "whatsapp", "to": to, "type": "text",
        "text": {"body": text},
    }, _tenant_id=_tenant_id, _store_name=_store_name, _db=_db)


async def _send_interactive_reply(phone_id: str, to: str, body_text: str, buttons: list) -> None:
    await _post_wa(phone_id, {
        "messaging_product": "whatsapp", "to": to, "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {"buttons": buttons[:3]},
        },
    })


async def _send_cta_url(phone_id: str, to: str, body_text: str,
                         btn_label: str, btn_url: str) -> None:
    await _post_wa(phone_id, {
        "messaging_product": "whatsapp", "to": to, "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {"text": body_text},
            "action": {"name": "cta_url", "parameters": {"display_text": btn_label, "url": btn_url}},
        },
    })


async def _send_welcome_menu(phone_id: str, to: str) -> None:
    await _send_interactive_reply(
        phone_id=phone_id, to=to,
        body_text="هلا! أنا نحلة 🍯\nأساعد أصحاب المتاجر يبيعون أكثر عبر واتساب.\n\nوش تبي تعرف؟",
        buttons=[
            {"type": "reply", "reply": {"id": "menu_how",   "title": "كيف تشتغل؟ 🤔"}},
            {"type": "reply", "reply": {"id": "menu_price", "title": "كم الأسعار؟ 💰"}},
            {"type": "reply", "reply": {"id": "menu_trial", "title": "أبي أجرب 🚀"}},
        ],
    )


async def _send_checkout_cta(phone_id: str, to: str) -> None:
    await _send_cta_url(
        phone_id=phone_id, to=to,
        body_text="ممتاز! سجّل الحين وابدأ تجربتك المجانية 14 يوم 🎁\nبدون بطاقة ائتمان.",
        btn_label="سجّل مجاناً الآن",
        btn_url="https://app.nahlah.ai/register",
    )


async def _send_trial_cta(phone_id: str, to: str) -> None:
    await _send_cta_url(
        phone_id=phone_id, to=to,
        body_text="تقدر تبدأ تجربة 14 يوم مجانية — بدون بطاقة ائتمان 🎁",
        btn_label="ابدأ التجربة المجانية",
        btn_url="https://app.nahlah.ai/register",
    )


async def _send_plans_message(phone_id: str, to: str, db=None) -> None:
    plans_text = (
        "🐝 باقات نحلة AI:\n\n"
        "Starter   — 899 ريال/شهر\n"
        "Pro       — 1,499 ريال/شهر\n"
        "Business  — 2,499 ريال/شهر\n\n"
        "كل الباقات: تجربة مجانية 14 يوم — بدون بطاقة.\n\n"
        "متجرك صغير ولا كبير؟ أساعدك تختار الأنسب."
    )
    await _send_whatsapp_message(phone_id=phone_id, to=to, text=plans_text)
    await _send_cta_url(
        phone_id=phone_id, to=to,
        body_text="شوف كل التفاصيل والمقارنة بين الباقات 💎",
        btn_label="عرض الباقات كاملة",
        btn_url="https://app.nahlah.ai/billing",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# BUTTON REPLY HANDLER
# ═══════════════════════════════════════════════════════════════════════════════

async def _handle_button_reply(
    btn_id: str, phone_id: str, to: str,
    tenant_id: Optional[int] = None, db=None,
) -> None:
    """Handle interactive button taps — all deterministic, no Claude."""
    if db is None:
        db = next(get_db(), None)
    state = StateManager.load(db, phone=to, tenant_id=tenant_id) if db else None

    if btn_id == "contact_founder":
        await _send_whatsapp_message(
            phone_id=phone_id, to=to,
            text="زين! تقدر تتواصل مع المؤسس مباشرةً على واتساب 👇\nhttps://wa.me/966555906901",
        )

    elif btn_id == "menu_how":
        await _send_interactive_reply(
            phone_id=phone_id, to=to,
            body_text=(
                "نحلة ترد على عملاء متجرك في واتساب وتساعدهم يكملون طلباتهم لوحدها 🤖\n"
                "24/7 — بدون ما تتدخل أنت.\n\nمتجرك على أي منصة؟"
            ),
            buttons=[
                {"type": "reply", "reply": {"id": "store_salla", "title": "سلة 🛒"}},
                {"type": "reply", "reply": {"id": "store_zid",   "title": "زد 🛒"}},
                {"type": "reply", "reply": {"id": "store_other", "title": "منصة ثانية"}},
            ],
        )
        if state:
            DeduplicationGuard.mark_asked(state, "ask_platform")

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
            DeduplicationGuard.mark_asked(state, "ask_platform")
        await _send_interactive_reply(
            phone_id=phone_id, to=to,
            body_text=f"ممتاز! نحلة تتكامل مع {platform} مباشرةً 🔗\nمتجرك كبير ولا صغير؟",
            buttons=[
                {"type": "reply", "reply": {"id": "store_small", "title": "صغير / ناشئ"}},
                {"type": "reply", "reply": {"id": "store_big",   "title": "متوسط / كبير"}},
            ],
        )
        if state:
            DeduplicationGuard.mark_asked(state, "ask_store_size")

    elif btn_id == "store_other":
        await _send_whatsapp_message(
            phone_id=phone_id, to=to,
            text="حالياً نحلة تدعم سلة وزد بشكل كامل.\nأي منصة تستخدم؟ نشوف إذا في حل 🤝",
        )

    elif btn_id in ("store_small", "store_big"):
        size = "small" if btn_id == "store_small" else "large"
        if state:
            state.slots.store_size = size
            state.stage = "recommendation"
            state.recommended_plan = recommend_plan(state)
            DeduplicationGuard.mark_asked(state, "ask_store_size")
        plan_text = (
            "باقة Starter — 899 ريال/شهر ✨" if size == "small"
            else "باقة Pro أو Business 💎"
        )
        await _send_cta_url(
            phone_id=phone_id, to=to,
            body_text=f"الأنسب لمتجرك: {plan_text}\nجرّبها 14 يوم مجاناً — بدون بطاقة.",
            btn_label="شوف الباقات وسجّل",
            btn_url="https://app.nahlah.ai/billing",
        )

    else:
        logger.debug("[Buttons] Unhandled id=%s", btn_id)

    # Persist state changes from button
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
