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

from models import MessageEvent, WhatsAppConnection

from core.config import ANTHROPIC_API_KEY, CLAUDE_MODEL, ORCHESTRATOR_URL, WA_VERIFY_TOKEN
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
from services.whatsapp_platform.service import provider_send_message
from services.whatsapp_platform.provider_utils import WHATSAPP_PROVIDER_360DIALOG, wa_provider
from core.database import get_db
from core.nahla_knowledge import build_nahla_system_prompt
from core.wa_usage import track_conversation
from modules.ai.orchestrator.adapter import generate_ai_reply
from services.customer_intelligence import CustomerIntelligenceService, normalize_phone

logger = logging.getLogger("nahla-backend")
router = APIRouter(tags=["WhatsApp Webhook"])


# ─── Platform-vs-merchant routing ────────────────────────────────────────────
# A tenant is the Nahla *platform* sales workspace only when explicitly
# flagged via `tenants.is_platform_tenant=True`. Until that flag is set on
# exactly one tenant, every inbound message is treated as merchant traffic
# and answered by the store's own AI (`_handle_merchant_message`). We
# cache the lookup per process: this column changes ~never, and the
# lookup runs on every inbound message.

_PLATFORM_TENANT_CACHE: dict[str, Any] = {"value": None, "loaded": False}


def _is_platform_tenant(db, tenant_id: Optional[int]) -> bool:
    """True iff this tenant has the explicit `is_platform_tenant=True`
    flag set. Defaults to False — the safe choice for a SaaS where the
    overwhelmingly common case is "this inbound message belongs to a
    merchant store, not the platform sales bot"."""
    if tenant_id is None:
        return False
    if not _PLATFORM_TENANT_CACHE["loaded"]:
        try:
            from models import Tenant  # noqa: PLC0415
            row = (
                db.query(Tenant.id)
                .filter(Tenant.is_platform_tenant.is_(True))
                .first()
            )
            _PLATFORM_TENANT_CACHE["value"] = row[0] if row else None
        except Exception as exc:  # noqa: BLE001
            # Fail-safe: if the column does not exist yet (pre-migration)
            # or the lookup throws for any reason, treat NO tenant as the
            # platform — every store-AI path then works correctly.
            logger.debug("[platform-tenant] resolver lookup failed: %s", exc)
            _PLATFORM_TENANT_CACHE["value"] = None
        _PLATFORM_TENANT_CACHE["loaded"] = True
    return _PLATFORM_TENANT_CACHE["value"] == tenant_id


def _reset_platform_tenant_cache() -> None:
    """Test/admin helper to invalidate the cached platform-tenant id."""
    _PLATFORM_TENANT_CACHE["value"] = None
    _PLATFORM_TENANT_CACHE["loaded"] = False


def _extract_contact_name(value: Dict[str, Any], sender: str) -> str:
    sender_digits = "".join(ch for ch in str(sender or "") if ch.isdigit())
    for contact in value.get("contacts", []) or []:
        wa_id = str(contact.get("wa_id") or "")
        if wa_id == sender or "".join(ch for ch in wa_id if ch.isdigit()) == sender_digits:
            profile = contact.get("profile") or {}
            return str(profile.get("name") or "").strip()
    return ""


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


@router.post("/webhook/whatsapp/360dialog")
async def whatsapp_incoming_360dialog(request: Request):
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        return {"status": "ok"}
    try:
        await _handle_360dialog_body(body, request)
    except Exception as exc:
        logger.error("[Webhook360] Unhandled error: %s", exc, exc_info=True)
    return {"status": "ok"}


async def _handle_whatsapp_body(body: Dict[str, Any]) -> None:
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
            for msg in value.get("messages", []):
                await _dispatch_message(phone_number_id, msg, value)


async def _handle_360dialog_body(body: Dict[str, Any], request: Request) -> None:
    db = next(get_db(), None)
    if not db:
        logger.error("[Webhook360] Cannot open DB session")
        return
    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {}) or {}
                field = str(change.get("field") or "")
                phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
                if not phone_number_id:
                    logger.warning("[Webhook360] Missing phone_number_id field=%s", field)
                    continue
                wa_conn = db.query(WhatsAppConnection).filter_by(phone_number_id=phone_number_id).first()
                if not wa_conn:
                    logger.warning("[Webhook360] Unknown phone_number_id=%s field=%s", phone_number_id, field)
                    continue
                if wa_provider(wa_conn) != WHATSAPP_PROVIDER_360DIALOG:
                    logger.warning("[Webhook360] phone_number_id=%s is not dialog360 provider", phone_number_id)
                    continue
                expected_secret = str((wa_conn.extra_metadata or {}).get("coexistence_internal_secret") or "")
                provided_secret = request.headers.get("X-Nahla-Coexistence-Secret", "")
                if expected_secret and provided_secret != expected_secret:
                    logger.warning("[Webhook360] Invalid internal secret tenant=%s", wa_conn.tenant_id)
                    return

                if field in ("messages", "smb_message_echoes"):
                    # Stamp activity for guardian — one lightweight update per webhook delivery
                    try:
                        from datetime import timezone as _tz, datetime as _dt  # noqa: PLC0415
                        wa_conn.last_webhook_received_at = _dt.now(_tz.utc)
                        db.add(wa_conn)
                        db.flush()
                    except Exception:
                        pass

                if field == "messages":
                    for msg in value.get("messages", []):
                        await _dispatch_message(phone_number_id, msg, value)
                    continue

                if field == "smb_message_echoes":
                    await _ingest_smb_message_echoes(db, wa_conn, value)
                    continue

                logger.info("[Webhook360] Ignored field=%s tenant=%s phone_number_id=%s", field, wa_conn.tenant_id, phone_number_id)
    finally:
        try:
            db.close()
        except Exception:
            pass


async def _ingest_smb_message_echoes(db, wa_conn: WhatsAppConnection, value: Dict[str, Any]) -> None:
    from routers.conversations import _get_or_create_conversation  # noqa: PLC0415

    phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
    for echo in value.get("message_echoes", []) or []:
        to_phone = str(echo.get("to") or "")
        msg_type = str(echo.get("type") or "")
        body_text = ""
        if msg_type == "text":
            body_text = str(((echo.get("text") or {}).get("body")) or "")
        else:
            body_text = f"[merchant_{msg_type}]"

        if not to_phone:
            continue

        convo = _get_or_create_conversation(db, wa_conn.tenant_id, to_phone)
        db.add(MessageEvent(
            conversation_id=convo.id,
            tenant_id=wa_conn.tenant_id,
            direction="outbound",
            body=body_text,
            event_type="smb_message_echo",
            extra_metadata={
                "customer_phone": to_phone,
                "phone": to_phone,
                "provider": WHATSAPP_PROVIDER_360DIALOG,
                "phone_number_id": phone_number_id,
                "message_id": echo.get("id"),
                "source": "merchant_mobile_app",
                "echo_type": msg_type,
            },
        ))
        convo.status = "active"
        db.add(convo)
    db.commit()


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

    # ── Resolve tenant from phone_number_id (must be exactly 1 match) ────────
    wa_matches = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.phone_number_id == phone_number_id)
        .all()
    )

    if len(wa_matches) == 0:
        logger.warning(
            "[Webhook] DROPPED — no WhatsAppConnection for phone_number_id=%s from=%s",
            phone_number_id, sender,
        )
        # Log integrity event for observability
        try:
            from core.tenant_integrity import log_integrity_event as _lie  # noqa: PLC0415
            _lie(
                db, "tenant_resolved",
                phone_number_id=phone_number_id,
                action="webhook_dispatch",
                result="dropped_no_match",
                detail=f"No WhatsAppConnection for phone_number_id={phone_number_id}",
            )
            db.commit()
        except Exception:
            pass
        return

    if len(wa_matches) > 1:
        # CRITICAL: ambiguous routing — phone_number_id is supposed to be globally unique.
        # The unique partial index should prevent this, but we guard defensively.
        tenant_ids = [c.tenant_id for c in wa_matches]
        logger.critical(
            "[Webhook] CRITICAL — AMBIGUOUS phone_number_id=%s matched %d tenants=%s — DROPPED",
            phone_number_id, len(wa_matches), tenant_ids,
        )
        try:
            from core.tenant_integrity import log_integrity_event as _lie  # noqa: PLC0415
            _lie(
                db, "duplicate_identity",
                phone_number_id=phone_number_id,
                action="webhook_dispatch",
                result="dropped_ambiguous",
                detail=f"phone_number_id={phone_number_id} matched {len(wa_matches)} tenants: {tenant_ids}",
            )
            db.commit()
        except Exception:
            pass
        return

    wa_conn = wa_matches[0]

    # Log successful tenant resolution
    try:
        from core.tenant_integrity import log_integrity_event as _lie  # noqa: PLC0415
        _lie(
            db, "tenant_resolved",
            tenant_id=wa_conn.tenant_id,
            phone_number_id=phone_number_id,
            action="webhook_dispatch",
            result="ok",
        )
    except Exception:
        pass

    used_pid           = wa_conn.phone_number_id
    resolved_tenant_id = wa_conn.tenant_id
    logger.info(
        "[TRACE][2/6] TENANT_RESOLVED | phone_number_id=%s tenant_id=%s status=%s",
        used_pid, resolved_tenant_id, wa_conn.status,
    )

    # ── Stamp last_webhook_received_at for guardian activity tracking ─────────
    try:
        from datetime import timezone as _tz  # noqa: PLC0415
        from datetime import datetime as _dt  # noqa: PLC0415
        wa_conn.last_webhook_received_at = _dt.now(_tz.utc)
        db.add(wa_conn)
        db.flush()
    except Exception as _stamp_exc:
        logger.debug("[Webhook] Failed to stamp last_webhook_received_at: %s", _stamp_exc)

    normalized_sender = normalize_phone(sender) or sender
    contact_name = _extract_contact_name(value, sender)
    _inbound_customer_id: int | None = None
    try:
        _lead = CustomerIntelligenceService(db, resolved_tenant_id).upsert_lead_customer(
            phone=normalized_sender,
            name=contact_name or normalized_sender,
            source="whatsapp_inbound",
            extra_metadata={
                "channel": "whatsapp",
                "phone_number_id": phone_number_id,
                "provider": wa_provider(wa_conn),
            },
            commit=True,
        )
        if _lead:
            _inbound_customer_id = _lead.id
        track_conversation(
            db,
            resolved_tenant_id,
            normalized_sender,
            source="inbound",
            category="service",
        )
    except Exception as exc:
        logger.warning(
            "[Webhook] Failed to sync inbound customer lead | tenant=%s sender=%s err=%s",
            resolved_tenant_id, normalized_sender, exc,
        )

    # Emit automation event for inbound WhatsApp message (non-blocking)
    try:
        from core.automation_engine import emit_automation_event  # noqa: PLC0415
        emit_automation_event(
            db,
            resolved_tenant_id,
            "whatsapp_message_received",
            customer_id=_inbound_customer_id,
            payload={
                "phone": normalized_sender,
                "msg_type": msg_type,
                "phone_number_id": phone_number_id,
            },
            commit=True,
        )
    except Exception as exc:
        logger.debug("[Webhook] emit whatsapp_message_received failed: %s", exc)

    # ── Handle interactive button replies ──────────────────────────────────────
    if msg_type == "interactive":
        interactive = msg.get("interactive", {})
        if interactive.get("type") == "button_reply":
            br      = interactive.get("button_reply", {}) or {}
            btn_id  = br.get("id", "")
            btn_txt = br.get("title", "") or btn_id

            # COD confirmation flow runs for every tenant (it's a merchant-
            # facing template, not the Nahla SaaS sales bot). Try it FIRST so
            # the platform-sales button handler doesn't accidentally swallow
            # a "تأكيد الطلب" tap on a merchant tenant.
            try:
                from services.cod_confirmation import (  # noqa: PLC0415
                    classify_cod_reply, handle_cod_reply,
                )
                if classify_cod_reply(btn_txt) is not None:
                    decision, order = await handle_cod_reply(
                        db,
                        tenant_id=resolved_tenant_id,
                        customer_phone=sender,
                        text=btn_txt,
                    )
                    if order is not None:
                        await _send_cod_followup_message(
                            phone_id=used_pid, to=sender,
                            decision=decision, order=order,
                            _tenant_id=resolved_tenant_id, _db=db,
                        )
                        return
            except Exception as exc:
                logger.error("[Webhook] COD button handler failed: %s", exc)

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

    # ── Merchant vs Platform routing ─────────────────────────────────────────
    # We used to hard-code `PLATFORM_TENANT_ID = 1` here, which silently
    # routed real merchants whose tenant happened to live at id=1 into
    # the platform sales-bot flow (CTA "سجّل في نحلة" instead of the
    # store's AI). The decision now lives in the data: a tenant is the
    # platform workspace ONLY when `tenants.is_platform_tenant=True`.
    # When no tenant has the flag, every inbound message defaults to
    # merchant flow — which is the safe, expected behaviour for any
    # production environment that hasn't explicitly enabled the
    # platform-brain workspace.
    if not _is_platform_tenant(db, resolved_tenant_id):
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
            await _send_welcome_menu(phone_id=used_pid, to=sender,
                                     _tenant_id=effective_tenant_id, _db=db)

        elif action == SEND_CHECKOUT_LINK:
            state.stage = "checkout"
            await _send_checkout_cta(phone_id=used_pid, to=sender,
                                     _tenant_id=effective_tenant_id, _db=db)

        elif action == SEND_TRIAL_LINK:
            await _send_trial_cta(phone_id=used_pid, to=sender,
                                  _tenant_id=effective_tenant_id, _db=db)

        elif action == SHOW_PLANS:
            await _send_plans_message(phone_id=used_pid, to=sender, db=db,
                                      _tenant_id=effective_tenant_id)

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
                    _tenant_id=effective_tenant_id, _db=db,
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
                _tenant_id=effective_tenant_id, _db=db,
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

async def _send_cod_followup_message(
    *, phone_id: str, to: str, decision: str, order: Any,
    _tenant_id: Optional[int] = None, _db=None,
) -> None:
    """
    Reply to the customer after their COD button tap is processed. Kept
    plain text (no template) because we're inside the 24-hour customer
    care window — the customer just messaged us, so a session message is
    Meta-policy compliant and does not require a pre-approved template.
    """
    if decision == "confirm":
        body = (
            f"شكراً لك ✅\n"
            f"تم تأكيد طلبك #{order.id}.\n"
            f"سيتم تجهيزه والتواصل معك قريباً لتأكيد التوصيل."
        )
    else:
        body = (
            f"تم إلغاء طلبك #{order.id} بنجاح.\n"
            f"إذا كان هناك أي خطأ يمكنك إعادة الطلب في أي وقت."
        )
    await _send_whatsapp_message(
        phone_id=phone_id, to=to, text=body,
        _tenant_id=_tenant_id, _db=_db,
    )


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

    # ── COD reply interception ────────────────────────────────────────────────
    # Some WhatsApp clients render QUICK_REPLY taps as plain text rather
    # than interactive button payloads. We pattern-match the message
    # against the COD whitelist BEFORE the AI takes over, otherwise the
    # store's AI assistant would happily reply "ok!" without ever
    # transitioning the order. classify_cod_reply returns None on any
    # unrelated text, so this guard is safe to run on every message.
    try:
        from services.cod_confirmation import (  # noqa: PLC0415
            classify_cod_reply, handle_cod_reply,
        )
        if classify_cod_reply(text) is not None:
            decision, order = await handle_cod_reply(
                db,
                tenant_id=tenant_id,
                customer_phone=to,
                text=text,
            )
            if order is not None:
                await _send_cod_followup_message(
                    phone_id=phone_id, to=to, decision=decision, order=order,
                    _tenant_id=tenant_id, _db=db,
                )
                return
            # No pending COD order → fall through to normal AI reply, but
            # don't block the rest of the conversation.
    except Exception as exc:
        logger.error("[Merchant] COD text-reply handler failed: %s", exc)

    try:
        from core.store_knowledge import build_ai_context  # noqa: PLC0415
        from routers.conversations import _get_or_create_conversation  # noqa: PLC0415

        # Create/update the visible dashboard conversation first so inbound
        # messages appear even if AI generation or sending fails later.
        convo = _get_or_create_conversation(db, tenant_id, to)
        convo.status = "active"
        convo.is_human_handoff = False
        convo.paused_by_human = False
        db.add(convo)
        db.flush()

        # Persist inbound immediately for inbox visibility and history continuity.
        StateManager.save_message(db, to, text, "inbound", conversation_id=convo.id, tenant_id=tenant_id)

        # Keep a lightweight state row in sync with the same phone key used by history.
        state = StateManager.load(db, phone=to, tenant_id=tenant_id)
        state.turn += 1
        state.stage = "active"
        StateManager.save(db, state, tenant_id=tenant_id)

        # Load store context for this merchant.
        store_context_text = build_ai_context(db, tenant_id, customer_phone=to, product_query=text)

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

{store_context_text}

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

        if not ANTHROPIC_API_KEY:
            logger.error("[Merchant] No ANTHROPIC_API_KEY — using fallback reply")
            reply = "وصلت رسالتك بنجاح. فريق المتجر أو المساعد الذكي سيراجع طلبك ويعود إليك قريبًا."
        else:
            payload = generate_ai_reply(
                tenant_id=tenant_id,
                customer_phone=to,
                message=text,
                store_name="",
                channel="whatsapp",
                locale="ar",
                context_metadata={"store_context": store_context_text},
                prompt_overrides={"__full_system_prompt": full_prompt},
                provider_hint="anthropic",
            )
            reply = payload.reply_text.strip() or "كيف أقدر أساعدك؟"

        # Save outbound reply after generation.
        StateManager.save_message(db, to, reply, "outbound", conversation_id=convo.id, tenant_id=tenant_id)

        latency_ms = 0
        try:
            ObservabilityLogger.log(db, TurnLog(
                phone=to,
                turn=max(int(getattr(state, "turn", 1) or 1), 1),
                raw_message=text,
                detected_intent="merchant_store_ai",
                confidence=1.0,
                extracted_slots=[],
                stage_before="merchant",
                stage_after="merchant",
                stage_transition=None,
                decision="GENERATE_AI_REPLY",
                decision_reason="merchant_whatsapp_inbound",
                ai_called=True,
                response_text=reply,
                latency_ms=latency_ms,
            ), tenant_id=tenant_id)
        except Exception:
            pass

        await _send_whatsapp_message(
            phone_id=phone_id, to=to, text=reply,
            _tenant_id=tenant_id, _db=db,
        )
        logger.info("[Merchant] replied tenant=%s to=%s", tenant_id, to)

    except Exception as exc:
        # Any failure inside the merchant reply pipeline (store_knowledge,
        # AI orchestrator, WA send) used to leave the customer in dead
        # silence and the merchant unable to see what went wrong. Log the
        # full traceback so we can diagnose the next regression, AND
        # send a single polite fallback so the customer still gets a
        # reply within the 24-hour service window. The fallback uses the
        # same tenant-scoped send path as the primary reply.
        import traceback  # noqa: PLC0415
        logger.error(
            "[Merchant] Error generating reply for tenant=%s: %s\n%s",
            tenant_id, exc, traceback.format_exc(),
        )
        try:
            await _send_whatsapp_message(
                phone_id=phone_id, to=to,
                text="وصلت رسالتك ✅ سيتم الرد عليك في أقرب وقت.",
                _tenant_id=tenant_id, _db=db,
            )
        except Exception as send_exc:  # noqa: BLE001
            logger.error(
                "[Merchant] Fallback send also failed for tenant=%s: %s",
                tenant_id, send_exc,
            )


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

_AUTO_REREGISTERED_PHONE_IDS: set[str] = set()


def _resolve_wa_conn_by_phone_id(_db, phone_id: str):
    """Look up a WhatsAppConnection row by phone_number_id when the caller
    didn't pass one. Many of our internal send helpers (welcome menu,
    checkout CTA, plan menu, button replies) are invoked with just the
    phone_number_id and lose the tenant context that the engine had.
    Without this lookup, _post_wa would call provider_send_message with
    conn=None / tenant_id=None and fall back to the platform token even
    when a perfectly valid merchant token exists for that phone_number_id.
    """
    if not _db or not phone_id:
        return None, None
    try:
        from database.models import WhatsAppConnection  # noqa: PLC0415
        conn = (
            _db.query(WhatsAppConnection)
            .filter_by(phone_number_id=str(phone_id))
            .first()
        )
        if conn:
            return conn, getattr(conn, "tenant_id", None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[WA] phone_id lookup failed: %s", exc)
    return None, None


async def _post_wa(
    phone_id: str,
    payload: dict,
    _tenant_id: Optional[int] = None,
    _store_name: str = "unknown",
    _db=None,
) -> None:
    wa_conn = None
    if _tenant_id and _db:
        try:
            from database.models import WhatsAppConnection  # noqa: PLC0415
            wa_conn = _db.query(WhatsAppConnection).filter_by(tenant_id=_tenant_id).first()
        except Exception:
            pass

    # Self-heal: if the caller forgot to pass tenant context, look the
    # connection up by phone_number_id so we don't fall back to platform.
    if wa_conn is None and _db is None:
        try:
            _db = next(get_db(), None)
        except Exception:
            _db = None
    if wa_conn is None and _db is not None:
        wa_conn, resolved_tid = _resolve_wa_conn_by_phone_id(_db, phone_id)
        if wa_conn and not _tenant_id:
            _tenant_id = resolved_tid
            logger.info(
                "[WA] tenant_id self-resolved from phone_number_id=%s → tenant=%s",
                phone_id, _tenant_id,
            )

    # Fetch store name from DB if not provided
    if _store_name == "unknown" and _tenant_id and _db:
        try:
            from core.tenant import get_or_create_tenant  # noqa: PLC0415
            t = get_or_create_tenant(_db, _tenant_id)
            _store_name = getattr(t, "store_name", None) or getattr(t, "name", None) or f"tenant_{_tenant_id}"
        except Exception:
            pass

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
    try:
        resp_data, ctx = await provider_send_message(
            _db,
            wa_conn,
            tenant_id=_tenant_id,
            operation="send_message",
            phone_id=phone_id,
            payload=payload,
            prefer_platform=bool(wa_conn and getattr(wa_conn, "connection_type", None) == "direct"),
            timeout=15,
        )
        token_tail = ctx.token[-6:] if ctx.token and len(ctx.token) >= 6 else "EMPTY"
        logger.info(
            "[SEND_DEBUG] tenant_id=%s store=%s phone_number_id=%s token_source=%s token_tail=%s to=%s",
            _tenant_id, _store_name, phone_id, ctx.source, token_tail, payload.get("to", "?"),
        )
        logger.info(
            "[SEND_DEBUG] provider response | tenant=%s phone_number_id=%s provider_payload=%s",
            _tenant_id, phone_id, resp_data,
        )
        if "error" in (resp_data or {}):
            err = (resp_data.get("error") or {}) if isinstance(resp_data, dict) else {}
            err_code = err.get("code")
            err_subcode = err.get("error_subcode")
            logger.warning("[WA] provider send failed: %.200s", str(resp_data))

            # Self-heal: GraphMethodException (code=100, subcode=33) on
            # /{phone_id}/messages typically means the phone has not been
            # registered with the Cloud API under our app, OR the active
            # token lacks `whatsapp_business_messaging` on this WABA.
            # Attempt /register once per process for this phone_id and
            # retry the send. If register also fails, we surface a clear
            # diagnostic so the merchant can see "needs reauth".
            if (
                err_code == 100
                and err_subcode == 33
                and phone_id
                and phone_id not in _AUTO_REREGISTERED_PHONE_IDS
                and ctx
                and ctx.token
            ):
                _AUTO_REREGISTERED_PHONE_IDS.add(phone_id)
                logger.warning(
                    "[WA] auto-register attempt — tenant=%s phone_id=%s "
                    "token_source=%s (response was code=100/subcode=33; "
                    "phone likely not registered or token lacks WABA scope)",
                    _tenant_id, phone_id, ctx.source,
                )
                try:
                    from services.whatsapp_connection_service import (  # noqa: PLC0415
                        register_phone_number,
                    )
                    reg_ok, reg_err = register_phone_number(
                        phone_id, ctx.token, _tenant_id or 0,
                    )
                except Exception as reg_exc:  # noqa: BLE001
                    reg_ok, reg_err = False, str(reg_exc)
                if reg_ok:
                    logger.info(
                        "[WA] auto-register OK — retrying send tenant=%s phone_id=%s",
                        _tenant_id, phone_id,
                    )
                    try:
                        retry_data, _retry_ctx = await provider_send_message(
                            _db, wa_conn,
                            tenant_id=_tenant_id,
                            operation="send_message_retry",
                            phone_id=phone_id,
                            payload=payload,
                            prefer_platform=bool(
                                wa_conn
                                and getattr(wa_conn, "connection_type", None) == "direct"
                            ),
                            timeout=15,
                        )
                        logger.info(
                            "[SEND_DEBUG] retry-after-register | tenant=%s phone_id=%s "
                            "result=%s",
                            _tenant_id, phone_id,
                            "ok" if "error" not in (retry_data or {}) else "still_failed",
                        )
                    except Exception as retry_exc:  # noqa: BLE001
                        logger.error(
                            "[WA] retry-after-register failed: %s", retry_exc,
                        )
                else:
                    logger.error(
                        "[WA] auto-register FAILED — tenant=%s phone_id=%s err=%r — "
                        "merchant likely needs to re-authenticate WhatsApp from the "
                        "dashboard (active token does not have permission for this WABA)",
                        _tenant_id, phone_id, reg_err,
                    )
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


async def _send_interactive_reply(
    phone_id: str, to: str, body_text: str, buttons: list,
    _tenant_id: Optional[int] = None, _db=None,
) -> None:
    await _post_wa(phone_id, {
        "messaging_product": "whatsapp", "to": to, "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {"buttons": buttons[:3]},
        },
    }, _tenant_id=_tenant_id, _db=_db)


async def _send_cta_url(
    phone_id: str, to: str, body_text: str,
    btn_label: str, btn_url: str,
    _tenant_id: Optional[int] = None, _db=None,
) -> None:
    await _post_wa(phone_id, {
        "messaging_product": "whatsapp", "to": to, "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {"text": body_text},
            "action": {"name": "cta_url", "parameters": {"display_text": btn_label, "url": btn_url}},
        },
    }, _tenant_id=_tenant_id, _db=_db)


async def _send_welcome_menu(
    phone_id: str, to: str,
    _tenant_id: Optional[int] = None, _db=None,
) -> None:
    await _send_interactive_reply(
        phone_id=phone_id, to=to,
        body_text="هلا! أنا نحلة 🍯\nأساعد أصحاب المتاجر يبيعون أكثر عبر واتساب.\n\nوش تبي تعرف؟",
        buttons=[
            {"type": "reply", "reply": {"id": "menu_how",   "title": "كيف تشتغل؟ 🤔"}},
            {"type": "reply", "reply": {"id": "menu_price", "title": "كم الأسعار؟ 💰"}},
            {"type": "reply", "reply": {"id": "menu_trial", "title": "أبي أجرب 🚀"}},
        ],
        _tenant_id=_tenant_id, _db=_db,
    )


async def _send_checkout_cta(
    phone_id: str, to: str,
    _tenant_id: Optional[int] = None, _db=None,
) -> None:
    await _send_cta_url(
        phone_id=phone_id, to=to,
        body_text="ممتاز! سجّل الحين وابدأ تجربتك المجانية 14 يوم 🎁\nبدون بطاقة ائتمان.",
        btn_label="سجّل مجاناً الآن",
        btn_url="https://app.nahlah.ai/register",
        _tenant_id=_tenant_id, _db=_db,
    )


async def _send_trial_cta(
    phone_id: str, to: str,
    _tenant_id: Optional[int] = None, _db=None,
) -> None:
    await _send_cta_url(
        phone_id=phone_id, to=to,
        body_text="تقدر تبدأ تجربة 14 يوم مجانية — بدون بطاقة ائتمان 🎁",
        btn_label="ابدأ التجربة المجانية",
        btn_url="https://app.nahlah.ai/register",
        _tenant_id=_tenant_id, _db=_db,
    )


async def _send_plans_message(
    phone_id: str, to: str, db=None,
    _tenant_id: Optional[int] = None,
) -> None:
    plans_text = (
        "🐝 باقات نحلة AI:\n\n"
        "Starter   — 899 ريال/شهر\n"
        "Pro       — 1,499 ريال/شهر\n"
        "Business  — 2,499 ريال/شهر\n\n"
        "كل الباقات: تجربة مجانية 14 يوم — بدون بطاقة.\n\n"
        "متجرك صغير ولا كبير؟ أساعدك تختار الأنسب."
    )
    await _send_whatsapp_message(
        phone_id=phone_id, to=to, text=plans_text,
        _tenant_id=_tenant_id, _db=db,
    )
    await _send_cta_url(
        phone_id=phone_id, to=to,
        body_text="شوف كل التفاصيل والمقارنة بين الباقات 💎",
        btn_label="عرض الباقات كاملة",
        btn_url="https://app.nahlah.ai/billing",
        _tenant_id=_tenant_id, _db=db,
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
            _tenant_id=tenant_id, _db=db,
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
            _tenant_id=tenant_id, _db=db,
        )
        if state:
            DeduplicationGuard.mark_asked(state, "ask_platform")

    elif btn_id == "menu_price":
        await _send_plans_message(phone_id=phone_id, to=to, db=db,
                                  _tenant_id=tenant_id)

    elif btn_id == "menu_trial":
        await _send_trial_cta(phone_id=phone_id, to=to,
                              _tenant_id=tenant_id, _db=db)
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
            _tenant_id=tenant_id, _db=db,
        )
        if state:
            DeduplicationGuard.mark_asked(state, "ask_store_size")

    elif btn_id == "store_other":
        await _send_whatsapp_message(
            phone_id=phone_id, to=to,
            text="حالياً نحلة تدعم سلة وزد بشكل كامل.\nأي منصة تستخدم؟ نشوف إذا في حل 🤝",
            _tenant_id=tenant_id, _db=db,
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
            _tenant_id=tenant_id, _db=db,
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
