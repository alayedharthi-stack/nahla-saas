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
from datetime import datetime
from typing import Any, Dict, List, Optional

import anthropic
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from models import MessageEvent, WhatsAppConnection

from core.config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    MERCHANT_BRAIN_ALLOW_LEGACY_FALLBACK,
    MERCHANT_BRAIN_ENABLED,
    MERCHANT_BRAIN_TENANT_IDS,
    ORCHESTRATOR_URL,
    WA_VERIFY_TOKEN,
)
from core.conversation_lock import conversation_lock
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
    HISTORY_WINDOW,
)
from services.whatsapp_platform.service import provider_send_message
from services.whatsapp_platform.provider_utils import WHATSAPP_PROVIDER_360DIALOG, wa_provider
from core.database import get_db
from core.wa_conn_write_metrics import (
    WA_STAMP_THROTTLE_SEC,
    approx_json_bytes,
    record_row_flush,
    reset_stamp_marker,
    should_stamp_now,
    submit_stamp_background,
)
from session import SessionLocal
from sqlalchemy import text
from core.nahla_knowledge import build_nahla_system_prompt
from core.wa_usage import track_conversation
from modules.ai.media.normalizer import normalize_whatsapp_inbound
from modules.ai.orchestrator.adapter import generate_ai_reply
from services.customer_intelligence import CustomerIntelligenceService, normalize_phone

logger = logging.getLogger("nahla-backend")
router = APIRouter(tags=["WhatsApp Webhook"])

# Bound coexistence audit JSON — full webhook payloads were blowing JSONB + triggering PG timeouts.
_COEX_PAYLOAD_PREVIEW_MAX = 512
_COEX_MAX_EVENT_CATEGORIES = 10


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


# ── Outbound semantic dedup ────────────────────────────────────────────────
#
# Why this lives at the webhook layer and not inside the Brain Composer:
# the Composer already has a per-template "exact-prefix" duplicate check
# (`DefaultComposer._is_duplicate`), but it only catches templates we
# repeat verbatim. The real-world failure was different: an automation
# message ("سلتك في انتظارك! 🛒…") arrives, then the customer says
# "السلام عليكم" and the legacy LLM path produces a fresh-looking reply
# that is *semantically* the same cart-recovery copy. To the customer,
# that reads as the bot ignoring them and re-sending the campaign.
#
# So this guard runs AFTER reply generation (Brain or legacy), BEFORE
# we hit the WhatsApp send. It's intentionally a tiny lexical heuristic
# rather than embeddings:
#   * extracts the last two outbound messages from the recorded history
#   * tokenises both into Arabic word stems (whitespace + punctuation)
#   * if the new reply shares > 60% of its content words with EITHER of
#     those previous outbound messages, we mark it as a repeat
# The caller can then short-circuit to a short follow-up instead.
#
# 60% chosen empirically on the failing transcripts: high enough that
# a normal "هل تحتاج مساعدة في طلبك؟" follow-up after a draft order
# does not trip (those typically share < 40% of stems with the draft
# template), low enough that "نفس رسالة استرجاع السلة بصياغة مختلفة"
# trips reliably.

_DEDUP_OVERLAP_THRESHOLD = 0.60
_DEDUP_MIN_TOKENS = 6  # ignore very short replies — they always overlap
_DEDUP_LOOKBACK_OUTBOUND = 2  # how many recent outbound turns to check


def _dedup_tokenise(text: str) -> set:
    """Lowercase + strip Arabic diacritics, drop punctuation, return the
    set of distinct content tokens. Pure helper, no I/O."""
    if not text:
        return set()
    import re as _re  # noqa: PLC0415
    # Strip Arabic tatweel + harakat so "سَلَّتُك" and "سلتك" match.
    stripped = _re.sub(r"[\u064B-\u0652\u0670\u0640]", "", str(text))
    # Replace anything that isn't an Arabic / Latin word char with a space.
    cleaned = _re.sub(r"[^\w\u0600-\u06FF]+", " ", stripped, flags=_re.UNICODE)
    tokens = {t for t in cleaned.lower().split() if len(t) >= 2}
    return tokens


def _is_repeat_reply(new_reply: str, history: list) -> bool:
    """True when ``new_reply`` overlaps too much with one of the most
    recent outbound messages.

    ``history`` is the same list the brain pipeline receives — each
    turn is ``{"direction": "in"/"inbound" | "out"/"outbound", "body": str}``.
    Robust to either spelling. Falls back to a safe ``False`` on any
    unexpected shape so the dedup never blocks a real reply.
    """
    new_tokens = _dedup_tokenise(new_reply)
    if len(new_tokens) < _DEDUP_MIN_TOKENS:
        return False

    outbound_seen = 0
    try:
        for turn in reversed(history or []):
            direction = str((turn or {}).get("direction") or "").lower()
            if direction not in ("out", "outbound"):
                continue
            outbound_seen += 1
            prev_tokens = _dedup_tokenise(turn.get("body") or "")
            if len(prev_tokens) < _DEDUP_MIN_TOKENS:
                if outbound_seen >= _DEDUP_LOOKBACK_OUTBOUND:
                    break
                continue
            overlap = len(new_tokens & prev_tokens) / max(1, len(new_tokens))
            if overlap >= _DEDUP_OVERLAP_THRESHOLD:
                return True
            if outbound_seen >= _DEDUP_LOOKBACK_OUTBOUND:
                break
    except Exception:  # noqa: BLE001 silent-ok
        # Dedup is a best-effort safety net — never block a real reply
        # because of an unexpected history shape. Any malformed turn
        # just falls through to the normal send path.
        return False
    return False


_DEDUP_FALLBACK_REPLIES = [
    "وش أقدر أخدمك فيه الحين؟ 🌸",
    "أنا هنا — قول وش تحتاج وأكمّل معك.",
    "تأمر بشيء أكمّل لك؟",
]


def _short_followup_instead_of_repeat(history: list) -> str:
    """Pick a varied short follow-up to substitute when the generated
    reply was flagged as a repeat. Uses the count of outbound turns in
    the history as a deterministic rotation key so a customer who keeps
    poking the bot does not see the SAME fallback every time either."""
    try:
        out_count = sum(
            1 for t in (history or [])
            if str((t or {}).get("direction") or "").lower() in ("out", "outbound")
        )
    except Exception:  # noqa: BLE001 silent-ok
        out_count = 0
    return _DEDUP_FALLBACK_REPLIES[out_count % len(_DEDUP_FALLBACK_REPLIES)]


# ── Smart notification helpers ────────────────────────────────────────────────

def _should_notify_merchant_email(
    *,
    db,
    tenant_id: int,
    customer,
    silence_hours: int = 24,
) -> dict:
    """
    Decide whether to send a merchant email notification for an inbound message.

    Rules (in order):
    1. No customer record → skip (can't determine context)
    2. Customer's first_seen_at ≈ now (< 5 min) → SEND (first message ever)
    3. An email was sent for this customer in the last `silence_hours` → SKIP
    4. Customer's last_interaction_at was > silence_hours ago → SEND (returning)
    5. Otherwise → SKIP (active conversation, no need to spam)

    Returns dict: {"send": bool, "reason": str, "reason_ar": str}
    """
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415
    from database.models import NotificationLog        # noqa: PLC0415

    now = datetime.now(timezone.utc)

    # ── 1. No lead ────────────────────────────────────────────────────────────
    if customer is None:
        return {"send": False, "reason": "no_customer", "reason_ar": "لا يوجد سجل عميل"}

    # Helper to get tz-aware datetime
    def _tz(dt):
        if dt is None:
            return None
        if hasattr(dt, "tzinfo") and dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    first_seen   = _tz(getattr(customer, "first_seen_at", None))
    last_contact = _tz(getattr(customer, "last_interaction_at", None))
    customer_id  = getattr(customer, "id", None)

    # ── 2. First message ever (first_seen_at < 5 minutes ago) ────────────────
    if first_seen and (now - first_seen) < timedelta(minutes=5):
        return {"send": True, "reason": "first_message",
                "reason_ar": "أول رسالة من هذا العميل"}

    # ── 3. Check if we already sent an email recently ─────────────────────────
    cutoff = now - timedelta(hours=silence_hours)
    try:
        recent_notif = (
            db.query(NotificationLog)
            .filter(
                NotificationLog.tenant_id  == tenant_id,
                NotificationLog.customer_id == customer_id,
                NotificationLog.event.in_(["new_whatsapp_message", "returning_customer"]),
                NotificationLog.status     == "sent",
                NotificationLog.created_at >= cutoff,
            )
            .first()
        )
        if recent_notif:
            return {
                "send":      False,
                "reason":    "throttled",
                "reason_ar": f"تم إرسال إشعار منذ أقل من {silence_hours} ساعة",
            }
    except Exception:
        # DB error → fail-open (allow send) rather than silently skipping
        pass

    # ── 4. Customer returning after silence ───────────────────────────────────
    if last_contact and (now - last_contact) > timedelta(hours=silence_hours):
        return {"send": True, "reason": "returning_customer",
                "reason_ar": f"العميل عاد بعد أكثر من {silence_hours} ساعة صمت"}

    # ── 5. Active conversation — skip ─────────────────────────────────────────
    return {
        "send":      False,
        "reason":    "active_conversation",
        "reason_ar": "المحادثة نشطة — تم التواصل مؤخراً",
    }


def _log_notification(
    *,
    db,
    tenant_id: int,
    customer_id: Optional[int],
    event: str,
    status: str,
    reason: str = "",
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """Write a NotificationLog row — non-fatal on any error."""
    try:
        from database.models import NotificationLog  # noqa: PLC0415
        row = NotificationLog(
            tenant_id=tenant_id,
            customer_id=customer_id,
            type="email",
            event=event,
            status=status,
            reason=reason or None,
            details=details or {},
        )
        db.add(row)
        db.commit()
    except Exception as exc:
        logger.warning("[Webhook] notification_log write failed (non-fatal): %s", exc)
        try:
            db.rollback()
        except Exception:
            pass


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
    """
    Ack-first endpoint for the legacy Meta-direct webhook.

    Returns 200 OK as soon as the JSON body is parsed; the AI / state
    processing pipeline runs in a tracked background task. This guarantees
    the worker is never blocked for >12 s on a single inbound message and
    means /auth/login and /healthz stay responsive even under burst load.
    Idempotency is already handled by core.inbound_dedup so the upstream
    provider can retry safely while we hold the previous turn.

    Hardened with the same safety contract as the 360dialog routes —
    ALWAYS returns 200 even on parse / spawn / cancellation, so the
    middleware chain never ends with "No response returned".
    """
    import asyncio as _asyncio  # noqa: PLC0415
    body: Dict[str, Any] = {}
    try:
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[webhook/meta] body parse failed (returning 200): %s", exc)
            body = {}
        try:
            from core.runtime_perf import spawn_background  # noqa: PLC0415
            spawn_background(_handle_whatsapp_body(body), name="webhook_meta")
        except Exception as exc:  # noqa: BLE001
            logger.exception("[webhook/meta] spawn_background failed: %s", exc)
    except _asyncio.CancelledError:
        logger.warning(
            "[webhook/meta] client cancelled — returning 200 to protect "
            "the middleware chain",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[webhook/meta] unexpected handler error (returning 200): %s", exc)
    return JSONResponse({"status": "ok"}, status_code=200)


# ── 360dialog Channel Webhook ────────────────────────────────────────────────
# Customer-originated messages and message status (delivered / read) callbacks.
# Coexistence-specific events (smb_message_echoes, app-state sync, pairing
# changes, …) are handled by the dedicated coexistence endpoint below — this
# split lets merchants who run "WhatsApp Business App + API" side-by-side keep
# the two streams cleanly separated in 360dialog's dashboard, exactly the way
# 360dialog itself models them.
#
# The legacy single-URL behaviour is preserved when callers configure only
# this endpoint: scope="any" still accepts every field for backward compat.

@router.post("/webhook/whatsapp/360dialog")
async def whatsapp_incoming_360dialog(request: Request):
    """Ack-first 360dialog channel webhook — see ack-first design note above.

    Hardened to ALWAYS return HTTP 200 with a JSON body, even on
    parse failures, BG-spawn failures, or client disconnects (which
    arrive as ``asyncio.CancelledError`` and would otherwise unwind
    the BaseHTTPMiddleware chain without a response, surfacing as
    ``RuntimeError: No response returned`` in Railway logs and
    triggering 360dialog retries that pile up on the worker).
    """
    return await _safe_360dialog_ack(request, scope="any", name="webhook_360dialog_any")


# ── 360dialog Coexistence Webhook (dedicated) ───────────────────────────────
# Receives ONLY the coexistence-specific event families:
#   • smb_message_echoes            (outbound from merchant's mobile WA app)
#   • smb_app_state_sync / device_sync / pairing_changes / phone_app_handover /
#     mobile_app_connection_state   (Coexistence lifecycle events)
#
# Configuring this URL in 360dialog is *recommended* (cleaner separation) but
# not required — the channel endpoint above will continue to accept these
# events when scope="any" is used.

@router.post("/webhook/whatsapp/360dialog/coexistence")
async def whatsapp_incoming_360dialog_coexistence(request: Request):
    """Ack-first 360dialog Coexistence webhook — see ack-first design note above."""
    return await _safe_360dialog_ack(
        request, scope="coexistence", name="webhook_360dialog_coexistence",
    )


# ── 360dialog Status / Health Webhook (dedicated) ───────────────────────────
# Channel-level health and operational callbacks: account alerts, account
# review updates, phone number quality / name updates, and other lifecycle
# events that do NOT belong on the message stream.

@router.post("/webhook/whatsapp/360dialog/status")
async def whatsapp_incoming_360dialog_status(request: Request):
    """Ack-first 360dialog Status/Health webhook — see ack-first design note above."""
    return await _safe_360dialog_ack(
        request, scope="status", name="webhook_360dialog_status",
    )


# ── Internal: shared safe-ack wrapper ───────────────────────────────────────
async def _safe_360dialog_ack(request: Request, *, scope: str, name: str):
    """
    Always-200 wrapper around the 360dialog ack-first webhook flow.

    Catches ``Exception`` AND ``asyncio.CancelledError`` so the route
    NEVER raises into the ASGI middleware chain. CancelledError in
    particular fires when 360dialog (or any client) disconnects mid-
    request; if that propagates, Starlette's ``BaseHTTPMiddleware``
    finishes without sending a response and we see
    ``RuntimeError: No response returned`` in Railway logs.

    The contract with 360dialog is: a 200 within 5 s on every webhook
    delivery. Any failure to read the body, capture headers, or
    schedule the background processing is swallowed and surfaced via
    structured logs instead of an HTTP error — 360dialog will retry
    on non-200, which compounds the very congestion we're trying to
    avoid.
    """
    import asyncio as _asyncio  # noqa: PLC0415
    body: Dict[str, Any] = {}
    try:
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[webhook/360dialog/%s] body parse failed (returning 200): %s",
                scope, exc,
            )
            body = {}

        try:
            headers = _capture_webhook_headers(request)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[webhook/360dialog/%s] header capture failed: %s", scope, exc,
            )
            headers = {}

        try:
            from core.runtime_perf import spawn_background  # noqa: PLC0415
            spawn_background(
                _handle_360dialog_body(body, headers, scope=scope),
                name=name,
            )
        except Exception as exc:  # noqa: BLE001
            # If we can't even schedule the BG task, keep the 200 so the
            # provider doesn't retry — but make this loud in the log.
            logger.exception(
                "[webhook/360dialog/%s] spawn_background failed: %s", scope, exc,
            )
    except _asyncio.CancelledError:
        # Client disconnected. Returning a 200 here protects the
        # middleware chain from ending without a response. Re-raising
        # would be the textbook approach but Starlette's
        # BaseHTTPMiddleware mishandles cancellation and surfaces it
        # as "No response returned." The provider has already given
        # up on this delivery anyway — the BG task we spawned (if it
        # ran before cancellation) will still finish out-of-band.
        logger.warning(
            "[webhook/360dialog/%s] client cancelled — returning 200 to "
            "protect the middleware chain", scope,
        )
    except Exception as exc:  # noqa: BLE001
        # Defensive — any other unexpected exception path also returns
        # 200 so the provider doesn't enter retry-storm mode.
        logger.exception(
            "[webhook/360dialog/%s] unexpected handler error (returning 200): %s",
            scope, exc,
        )
    return JSONResponse({"status": "ok"}, status_code=200)


# ── Internal helper: snapshot the headers we care about ────────────────────
# We pull them from the live `Request` BEFORE spawning the background task
# because Starlette will tear the request down once the response is sent.
# All downstream code only reads two headers (the coexistence shared secret
# + the incoming user-agent for diagnostics), so a tiny dict is enough.
def _capture_webhook_headers(request: Request) -> Dict[str, str]:
    return {
        "x_nahla_coexistence_secret": request.headers.get("X-Nahla-Coexistence-Secret", ""),
        "user_agent":                 request.headers.get("user-agent", ""),
    }


# ── Field classification ────────────────────────────────────────────────────
# Three disjoint families. Anything we don't recognise lands in *coexistence*
# by default — that endpoint is the catch-all for non-message provider events
# so we never silently drop new event types 360dialog ships in the future.

_CHANNEL_FIELDS: set = {"messages"}
_COEXISTENCE_FIELDS: set = {
    "smb_message_echoes",
    "smb_app_state_sync",
    "device_sync",
    "coexistence_state",
    "pairing_changes",
    "phone_app_handover",
    "mobile_app_connection_state",
}
_STATUS_FIELDS: set = {
    "account_alerts",
    "account_review_update",
    "account_update",
    "phone_number_quality_update",
    "phone_number_name_update",
    "channel_status",
    "messaging_health",
    "template_status_update",
}


def _classify_360dialog_field(field: str) -> str:
    """Return the family (channel | coexistence | status) for a 360dialog
    webhook field. Unknown fields are treated as coexistence so they are
    surfaced to the operator instead of being silently discarded."""
    if field in _CHANNEL_FIELDS:
        return "channel"
    if field in _STATUS_FIELDS:
        return "status"
    return "coexistence"


def _scope_accepts(scope: str, family: str) -> bool:
    """Whether the given endpoint scope should process events of this family."""
    if scope == "any":
        return True
    if scope == "channel":
        return family == "channel"
    if scope == "coexistence":
        return family == "coexistence"
    if scope == "status":
        return family == "status"
    return False


async def _handle_whatsapp_body(body: Dict[str, Any]) -> None:
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
            for msg in value.get("messages", []):
                await _dispatch_message(phone_number_id, msg, value)
            for status in value.get("statuses", []):
                await _handle_message_status(status)


async def _handle_message_status(status: Dict[str, Any]) -> None:
    """Process delivery/read receipts from Meta Cloud API.

    Updates ``Campaign.delivered_count`` / ``read_count`` when the
    status webhook carries a wamid that matches a campaign send.
    """
    wamid = status.get("id", "")
    st = (status.get("status") or "").lower()
    if not wamid or st not in ("delivered", "read"):
        return

    db = next(get_db(), None)
    if not db:
        return
    try:
        row = (
            db.query(MessageEvent)
            .filter(
                MessageEvent.extra_metadata["wa_message_id"].astext == wamid,
            )
            .first()
        )
        if not row:
            return
        meta = row.extra_metadata or {}
        campaign_id = meta.get("campaign_id")
        if not campaign_id:
            return

        already_key = f"_status_{st}"
        if meta.get(already_key):
            return

        from models import Campaign  # noqa: PLC0415
        campaign = db.query(Campaign).filter(Campaign.id == int(campaign_id)).first()
        if not campaign:
            return

        if st == "delivered":
            campaign.delivered_count = (campaign.delivered_count or 0) + 1
        elif st == "read":
            campaign.read_count = (campaign.read_count or 0) + 1
            if not meta.get("_status_delivered"):
                campaign.delivered_count = (campaign.delivered_count or 0) + 1
                meta["_status_delivered"] = True

        meta[already_key] = True
        row.extra_metadata = meta
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
        flag_modified(row, "extra_metadata")
        db.commit()
        logger.info("[StatusWebhook] campaign=%s status=%s wamid=%s", campaign_id, st, wamid[:20])
    except Exception as exc:
        logger.warning("[StatusWebhook] error processing status: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        try:
            db.close()
        except Exception:
            pass


async def _handle_360dialog_body(
    body: Dict[str, Any],
    headers: Dict[str, str],
    scope: str = "any",
) -> None:
    """Dispatch a 360dialog webhook delivery.

    ``scope`` selects which event families the caller (= the URL the merchant
    or platform configured in 360dialog) is willing to process:

      * ``"any"``         – legacy / single-URL setup. Accepts everything.
                            This is the default so existing tests and merchants
                            who only registered one URL keep working unchanged.
      * ``"channel"``     – customer messages + message statuses only.
      * ``"coexistence"`` – smb_message_echoes + Coexistence lifecycle events
                            (device sync, pairing, phone-app handover, mobile
                            app connection state).
      * ``"status"``      – channel/account health + quality update events.

    Events that do not match the scope are *recorded* (so we still see them
    in the per-tenant audit trail) but not acted upon — they are expected to
    arrive on a different URL.
    """
    db = SessionLocal()
    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {}) or {}
                field = str(change.get("field") or "")
                phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
                if not phone_number_id:
                    logger.warning("[Webhook360] Missing phone_number_id field=%s scope=%s", field, scope)
                    continue
                wa_conns = (
                    db.query(WhatsAppConnection)
                    .filter_by(phone_number_id=phone_number_id)
                    .all()
                )
                if not wa_conns:
                    logger.warning("[Webhook360] Unknown phone_number_id=%s field=%s scope=%s", phone_number_id, field, scope)
                    continue
                if len(wa_conns) > 1:
                    tenant_ids = [c.tenant_id for c in wa_conns]
                    logger.error(
                        "[Webhook360] Ambiguous phone_number_id=%s matches tenants=%s — "
                        "message dropped to prevent cross-tenant data leak",
                        phone_number_id, tenant_ids,
                    )
                    continue
                wa_conn = wa_conns[0]
                if wa_provider(wa_conn) != WHATSAPP_PROVIDER_360DIALOG:
                    logger.warning("[Webhook360] phone_number_id=%s is not dialog360 provider", phone_number_id)
                    continue
                expected_secret = str((wa_conn.extra_metadata or {}).get("coexistence_internal_secret") or "")
                provided_secret = headers.get("x_nahla_coexistence_secret", "")
                if expected_secret and provided_secret != expected_secret:
                    logger.warning("[Webhook360] Invalid internal secret tenant=%s", wa_conn.tenant_id)
                    return

                family = _classify_360dialog_field(field)

                # Always record per-family receipt — even when the field does
                # not belong to this endpoint's scope. The dashboard surfaces
                # the timestamps so the operator can confirm "the coexistence
                # webhook is alive even if the channel webhook went silent",
                # and vice-versa.
                _stamp_webhook_received(db, wa_conn, family)

                if not _scope_accepts(scope, family):
                    logger.info(
                        "[Webhook360] field=%s family=%s arrived on scope=%s — recorded but not processed",
                        field, family, scope,
                    )
                    continue

                # ── Channel events ────────────────────────────────────────
                if field == "messages":
                    for msg in value.get("messages", []):
                        await _dispatch_message(phone_number_id, msg, value)
                    for st_obj in value.get("statuses", []):
                        await _handle_message_status(st_obj)
                    continue

                # ── Coexistence events ────────────────────────────────────
                # Each event is committed individually so the row-level lock
                # on `whatsapp_connections` is released quickly. Holding the
                # lock across multiple events (e.g. coex + status arriving in
                # the same delivery) was producing `statement_timeout` on
                # the next webhook delivery for the same tenant.
                if field == "smb_message_echoes":
                    await _ingest_smb_message_echoes(db, wa_conn, value)
                    _record_coexistence_event(
                        db, wa_conn,
                        event_type=field,
                        category="merchant_mobile_echo",
                        value=value,
                    )
                    db.commit()
                    continue

                if family == "coexistence":
                    _record_coexistence_event(
                        db, wa_conn,
                        event_type=field,
                        category=_coexistence_category_for(field),
                        value=value,
                    )
                    db.commit()
                    continue

                # ── Status / health events ────────────────────────────────
                if family == "status":
                    _record_status_event(db, wa_conn, event_type=field, value=value)
                    db.commit()
                    continue

                logger.info("[Webhook360] Ignored field=%s tenant=%s phone_number_id=%s", field, wa_conn.tenant_id, phone_number_id)
        # Final no-op commit guarantees we close the implicit tx the SELECTs opened,
        # even if no event branches above wrote anything.
        db.commit()
    except Exception as exc:
        logger.exception("[Webhook360] batch failed — rolled back: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        try:
            db.close()
        except Exception:
            pass


# ── Webhook receipt bookkeeping ─────────────────────────────────────────────

_STAMP_COLUMN_BY_FAMILY = {
    "channel":     "last_webhook_received_at",
    "coexistence": "webhook_coexistence_received_at",
    "status":      "webhook_status_received_at",
}

# Per-statement timeout used by the background stamp. Kept short on purpose —
# if `whatsapp_connections.id=N` is contended, we'd rather drop the stamp than
# pile up waiters that then cascade into request-thread timeouts.
_BG_STAMP_TIMEOUT_MS = 1500


def _bg_stamp_run(conn_id: int, tenant_id: int | None, family: str, column: str) -> None:
    """Worker body — runs on the shared ``wa-stamp`` thread pool.

    Owns its own SQLAlchemy session/connection so no row lock or
    ``statement_timeout`` here can ever bleed into the webhook's main
    transaction. Worst case: this UPDATE fails, the next webhook (after the
    throttle window) retries. Stamping is purely informational.
    """
    bg_db = SessionLocal()
    t0 = time.perf_counter()
    try:
        # Bound this single UPDATE so we cannot block the worker thread for
        # longer than the timeout, even if PG is under heavy lock pressure.
        bg_db.execute(
            text("SET LOCAL statement_timeout = :ms"),
            {"ms": _BG_STAMP_TIMEOUT_MS},
        )
        result = bg_db.execute(
            text(
                f"UPDATE whatsapp_connections "
                f"SET {column} = now() "
                f"WHERE id = :id "
                f"AND (COALESCE({column}, 'epoch'::timestamptz) "
                f"     < now() - make_interval(secs => :thr))"
            ),
            {"id": conn_id, "thr": float(WA_STAMP_THROTTLE_SEC)},
        )
        bg_db.commit()
        elapsed = int((time.perf_counter() - t0) * 1000)
        record_row_flush(
            source=f"webhook_stamp_bg:{family}",
            tenant_id=tenant_id,
            conn_id=conn_id,
            flush_ms=elapsed,
        )
        if (result.rowcount or 0) == 0:
            logger.debug(
                "[Webhook360/stamp_bg] noop family=%s conn_id=%s rowcount=0 (sql_guard hit)",
                family, conn_id,
            )
    except Exception as exc:
        try:
            bg_db.rollback()
        except Exception:
            pass
        # Allow the next inbound to retry instead of waiting out the throttle.
        reset_stamp_marker(conn_id, family)
        logger.warning(
            "[Webhook360/stamp_bg] SKIPPED family=%s conn_id=%s tenant=%s elapsed_ms=%s err=%s",
            family,
            conn_id,
            tenant_id,
            int((time.perf_counter() - t0) * 1000),
            exc,
        )
    finally:
        try:
            bg_db.close()
        except Exception:
            pass


def _stamp_webhook_received(db, wa_conn: WhatsAppConnection, family: str) -> None:
    """Schedule a fire-and-forget stamp on the shared background pool.

    Returns immediately — never blocks the message pipeline. The previous
    in-line implementation, even with a SAVEPOINT, still held a row lock on
    ``whatsapp_connections`` for the duration of the surrounding webhook
    batch. Under burst traffic that surfaced as
    ``QueryCanceled: canceling statement due to statement timeout`` on
    parallel deliveries for the same connection. Moving the UPDATE to a
    dedicated session removes that interaction entirely.

    The ``db`` argument is intentionally unused — kept for API stability.
    """
    column = _STAMP_COLUMN_BY_FAMILY.get(family)
    if not column:
        return
    if not should_stamp_now(wa_conn.id, family):
        return

    submit_stamp_background(
        _bg_stamp_run,
        int(wa_conn.id),
        getattr(wa_conn, "tenant_id", None),
        family,
        column,
    )


def _coexistence_category_for(field: str) -> str:
    """Map a raw coexistence field to a stable category identifier the
    dashboard (and operators) can reason about."""
    table = {
        "smb_message_echoes":           "merchant_mobile_echo",
        "smb_app_state_sync":           "device_sync",
        "device_sync":                  "device_sync",
        "coexistence_state":            "coexistence_state",
        "pairing_changes":              "pairing_state",
        "phone_app_handover":           "phone_app_handover",
        "mobile_app_connection_state":  "mobile_app_connection_state",
    }
    return table.get(field, "coexistence_event")


# Stable enum used to advertise the merchant-facing sync state.
_COEX_SYNC_STATE_BY_CATEGORY = {
    "device_sync":                "synced",
    "pairing_state":              "paired",
    "phone_app_handover":         "handover",
    "mobile_app_connection_state": "mobile_app_connected",
    "coexistence_state":          "synced",
}


def _record_coexistence_event(
    db,
    wa_conn: WhatsAppConnection,
    *,
    event_type: str,
    category: str,
    value: Dict[str, Any],
) -> None:
    """Persist a Coexistence lifecycle event onto the connection.

    Stored under ``extra_metadata.coexistence`` but bounded — previews are
    truncated and ``last_event_by_category`` keeps only the newest N keys so
    webhook bursts cannot grow JSONB without bound.

    Does **not** commit — the enclosing 360dialog webhook batch commits once.
    """
    from datetime import timezone as _tz, datetime as _dt  # noqa: PLC0415
    import json as _json  # noqa: PLC0415

    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    now_iso = _dt.now(_tz.utc).isoformat()

    try:
        payload_preview = _json.dumps(value, ensure_ascii=False)[:_COEX_PAYLOAD_PREVIEW_MAX]
    except Exception:
        payload_preview = ""

    meta = dict(wa_conn.extra_metadata or {})
    coex = dict(meta.get("coexistence") or {})
    events = dict(coex.get("last_event_by_category") or {})

    last = {
        "event_type":  event_type,
        "category":    category,
        "received_at": now_iso,
        "payload_preview": payload_preview,
    }
    coex["last_event"] = last
    events[category] = last
    if len(events) > _COEX_MAX_EVENT_CATEGORIES:
        ranked = sorted(
            events.items(),
            key=lambda kv: kv[1].get("received_at") or "",
            reverse=True,
        )
        events = dict(ranked[:_COEX_MAX_EVENT_CATEGORIES])
    coex["last_event_by_category"] = events

    derived = _COEX_SYNC_STATE_BY_CATEGORY.get(category)
    if derived:
        coex["sync_state"] = derived

    if category == "pairing_state":
        coex["pairing_state"] = _extract_pairing_state(value) or "paired"
    if category == "mobile_app_connection_state":
        coex["mobile_app_connection_state"] = (
            _extract_mobile_app_state(value) or "connected"
        )
    if category == "phone_app_handover":
        coex["phone_app_handover_at"] = now_iso

    meta["coexistence"] = coex
    wa_conn.extra_metadata = meta

    flag_modified(wa_conn, "extra_metadata")
    db.add(wa_conn)

    approx = approx_json_bytes(meta)
    t_flush = time.perf_counter()
    db.flush()
    flush_ms = int((time.perf_counter() - t_flush) * 1000)
    record_row_flush(
        source="webhook360_coex_event",
        tenant_id=wa_conn.tenant_id,
        conn_id=wa_conn.id,
        flush_ms=flush_ms,
        approx_meta_json_bytes=approx,
    )
    logger.info(
        "[Webhook360/coex] tenant=%s event=%s category=%s sync_state=%s",
        wa_conn.tenant_id, event_type, category, coex.get("sync_state"),
    )


def _record_status_event(
    db,
    wa_conn: WhatsAppConnection,
    *,
    event_type: str,
    value: Dict[str, Any],
) -> None:
    """Persist a status / health event under ``extra_metadata.coexistence.status``.

    Flushes only — caller commits the 360dialog webhook batch.
    """
    from datetime import timezone as _tz, datetime as _dt  # noqa: PLC0415
    import json as _json  # noqa: PLC0415

    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    try:
        payload_preview = _json.dumps(value, ensure_ascii=False)[:_COEX_PAYLOAD_PREVIEW_MAX]
    except Exception:
        payload_preview = ""

    meta = dict(wa_conn.extra_metadata or {})
    coex = dict(meta.get("coexistence") or {})
    status_block = dict(coex.get("status") or {})
    status_block["last_event"] = {
        "event_type":  event_type,
        "received_at": _dt.now(_tz.utc).isoformat(),
        "payload_preview": payload_preview,
    }
    coex["status"] = status_block
    meta["coexistence"] = coex
    wa_conn.extra_metadata = meta

    flag_modified(wa_conn, "extra_metadata")
    db.add(wa_conn)

    approx = approx_json_bytes(meta)
    t_flush = time.perf_counter()
    db.flush()
    flush_ms = int((time.perf_counter() - t_flush) * 1000)
    record_row_flush(
        source="webhook360_status_event",
        tenant_id=wa_conn.tenant_id,
        conn_id=wa_conn.id,
        flush_ms=flush_ms,
        approx_meta_json_bytes=approx,
    )
    logger.info(
        "[Webhook360/status] tenant=%s event=%s",
        wa_conn.tenant_id, event_type,
    )


def _extract_pairing_state(value: Dict[str, Any]) -> Optional[str]:
    for key in ("pairing_state", "state", "status"):
        v = value.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def _extract_mobile_app_state(value: Dict[str, Any]) -> Optional[str]:
    for key in ("connection_state", "mobile_app_state", "state", "status"):
        v = value.get(key)
        if isinstance(v, str) and v:
            return v
    return None


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
    db.flush()


# ═══════════════════════════════════════════════════════════════════════════════
# CORE DISPATCH — Full Engine Pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_wa_message_text(message: Dict[str, Any]):
    """Universal WhatsApp message text extractor.

    Returns (text, source) for ALL message types:
      - "text"              → body
      - "button"            → button.text  (template quick-reply tap)
      - "interactive/button_reply" → button_reply.title  (interactive reply)
      - "interactive/list_reply"   → list_reply.title
    """
    # 1. Plain text
    _txt = message.get("text")
    if _txt and _txt.get("body"):
        return str(_txt["body"]).strip(), "text"

    # 2. Template button reply (type="button")
    _btn = message.get("button")
    if _btn:
        _payload = str(_btn.get("payload") or "").strip()
        _text    = str(_btn.get("text") or "").strip()
        # `payload` and `text` are both populated; `text` is human-readable
        if _text:
            return _text, "button"
        if _payload:
            return _payload, "button_payload"

    # 3. Interactive reply (type="interactive")
    _ia = message.get("interactive") or {}
    if _ia.get("type") == "button_reply":
        _br = _ia.get("button_reply") or {}
        _title = str(_br.get("title") or "").strip()
        if _title:
            return _title, "button_reply"

    if _ia.get("type") == "list_reply":
        _lr = _ia.get("list_reply") or {}
        _title = str(_lr.get("title") or "").strip()
        if _title:
            return _title, "list_reply"

    return None, "unknown"


async def _dispatch_message(
    phone_number_id: str,
    msg: Dict[str, Any],
    value: Dict[str, Any],
) -> None:
    t_start  = time.monotonic()
    msg_type = msg.get("type")
    sender   = msg.get("from", "")
    msg_id   = msg.get("id", "")

    # Universal text extraction — covers text, button, interactive.
    # This is the SINGLE source of truth for "what did the customer say?"
    # used for logging and for routing button taps to the merchant brain.
    _wa_text, _wa_source = _extract_wa_message_text(msg)
    _text_preview = (_wa_text or "")[:80].replace("\n", " ")

    logger.info(
        "[WA PARSER] extracted_text=%r source=%s msg_type=%s sender=%s",
        _wa_text, _wa_source, msg_type, sender,
    )

    # ── TRACE: log every incoming webhook ─────────────────────────────────────
    logger.info(
        "[TRACE][1/6] INCOMING_WEBHOOK | phone_number_id=%s sender=%s msg_id=%s msg_type=%s",
        phone_number_id, sender, msg_id, msg_type,
    )
    logger.info(
        "[WEBHOOK_IN] phone_number_id=%s from=%s msg_id=%s type=%s text=%r",
        phone_number_id, sender, msg_id, msg_type, _text_preview,
    )

    if not phone_number_id:
        logger.error(
            "[Webhook] DROPPED — phone_number_id missing from metadata. "
            "msg_type=%s from=%s msg_id=%s",
            msg_type, sender, msg_id,
        )
        return

    # ── Strict in-memory dedup — runs BEFORE the conversation lock ─────────
    # Both Meta and 360dialog retry inbound webhooks (same `msg_id`) within
    # seconds of each other. Without this gate the duplicates entered the
    # per-conversation lock queue (logged as `waiters_ahead=1`,
    # `waiters_ahead=2`, …), occupied a DB session, and were only dropped
    # later by `IdempotencyGuard` after a full state load. The DB guard
    # remains the source of truth, but this O(1) fast path eliminates the
    # queueing artefact and the wasted DB round-trips on retried deliveries.
    #
    # Key: (phone_number_id, msg_id) with a 10-minute TTL — comfortably
    # longer than any provider retry window, far shorter than the 24 h
    # conversation window so the cache cannot drift.
    if msg_id:
        try:
            from core.inbound_dedup import is_duplicate_inbound  # noqa: PLC0415
            if is_duplicate_inbound(phone_number_id=phone_number_id, msg_id=msg_id):
                logger.info(
                    "[Idempotency] DROP duplicate inbound (early/in-memory) "
                    "msg_id=%s phone_number_id=%s from=%s — provider retry, "
                    "skipping conversation lock + DB",
                    msg_id, phone_number_id, sender,
                )
                return
        except Exception as _early_dedup_exc:
            # Never block real traffic on a dedup hiccup — fall through to
            # the slower DB-backed guard, which has the same behaviour.
            logger.warning(
                "[Idempotency] early dedup failed phone_number_id=%s msg_id=%s err=%s",
                phone_number_id, msg_id, _early_dedup_exc,
            )

    # ── Open DB session early (needed for tenant lookup) ─────────────────────
    db = next(get_db(), None)
    if not db:
        logger.error("[Engine] Cannot open DB session for phone=%s", sender)
        return

    # Lock state-vars: declared before the try so the finally can always
    # safely test / release them, regardless of how far we got.
    _conv_lock_cm = None
    _conv_lock_active = False

    try:
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

        # WhatsApp business timestamp + AI live cutoff (ignore backlog before activation).
        from core.whatsapp_ai_live import (  # noqa: PLC0415
            is_inbound_before_ai_live_since as _hist_before_ai_live,
            parse_whatsapp_message_timestamp_utc as _parse_wa_msg_ts,
        )
        _wa_msg_ts = _parse_wa_msg_ts(msg.get("timestamp"))
        _hist_skip_live = _hist_before_ai_live(wa_conn, _wa_msg_ts)

        # ── Structured runtime resolver log ──────────────────────────────────
        # Carries the canonical record's identity so log aggregators can
        # compare runtime selection against the admin/status page selection.
        # Adds `waba_id` to the tenant_integrity row (was always None before).
        _runtime_provider = (wa_conn.provider or "").lower() or None
        _runtime_waba_id  = wa_conn.whatsapp_business_account_id or None
        _runtime_pid      = wa_conn.phone_number_id
        try:
            from core.tenant_integrity import log_integrity_event as _lie  # noqa: PLC0415
            _lie(
                db, "tenant_resolved",
                tenant_id=wa_conn.tenant_id,
                phone_number_id=phone_number_id,
                waba_id=_runtime_waba_id,
                provider=_runtime_provider,
                action="webhook_dispatch",
                result="ok",
                detail=(
                    f"runtime_integration_id={wa_conn.id} "
                    f"runtime_waba_id={_runtime_waba_id or 'missing'} "
                    f"runtime_phone_number_id={_runtime_pid or 'missing'} "
                    f"runtime_source=phone_number_id_lookup"
                ),
            )
        except Exception:
            pass

        used_pid           = _runtime_pid
        resolved_tenant_id = wa_conn.tenant_id
        logger.info(
            "[TRACE][2/6] TENANT_RESOLVED | phone_number_id=%s tenant_id=%s status=%s "
            "runtime_integration_id=%s runtime_waba_id=%s runtime_provider=%s "
            "runtime_source=phone_number_id_lookup",
            used_pid, resolved_tenant_id, wa_conn.status,
            wa_conn.id, _runtime_waba_id or "missing", _runtime_provider or "unknown",
        )

        # Loud warning when runtime resolved a record with NO waba_id — this
        # is the exact mismatch tenant 33 hit. Webhook routing still works
        # (phone_number_id was present), but business-template sending will
        # fail until WABA ID is filled in. Owner can fix via admin sync/edit.
        if not _runtime_waba_id:
            logger.warning(
                "[WA RUNTIME] tenant=%s integration=%s phone_number_id=%s "
                "WABA ID missing on canonical record — webhooks still route, "
                "but advanced sending features (templates) are limited until "
                "owner runs admin/coexistence/sync-record or edit-record.",
                resolved_tenant_id, wa_conn.id, used_pid,
            )

        # ── Per-conversation processing lock ──────────────────────────────────
        # Serialise inbound turns from the same (tenant, phone) so two fast
        # messages ("8" → "TAPA7401") cannot race on the same brain_state /
        # order_prep row. See `core/conversation_lock.py` for design notes.
        _conv_lock_cm = conversation_lock(
            resolved_tenant_id, sender,
            msg_id=msg_id, text_snippet=_text_preview,
        )
        await _conv_lock_cm.__aenter__()
        _conv_lock_active = True

        # ── Stamp last_webhook_received_at for guardian activity tracking ─────────
        try:
            from datetime import timezone as _tz  # noqa: PLC0415
            from datetime import datetime as _dt  # noqa: PLC0415
            wa_conn.last_webhook_received_at = _dt.now(_tz.utc)
            db.add(wa_conn)
            db.flush()
        except Exception as _stamp_exc:
            logger.debug("[Webhook] Failed to stamp last_webhook_received_at: %s", _stamp_exc)

        # ── Universal idempotency guard ─────────────────────────────────
        # Meta retries webhooks aggressively. Without this check on the
        # MERCHANT + INTERACTIVE paths (the legacy guard only ran inside
        # the platform-tenant branch below) a single retry produced a
        # second brain turn — which is exactly how the dashboard ended
        # up with duplicated "addon recommendations" and "recorded
        # interest" bubbles. Same logic for interactive button replies:
        # a duplicate `cart:resume_cart:…` tap previously fired the
        # CTA-URL twice, sending two checkout messages back-to-back.
        #
        # We load conversation state once here and reuse it for the
        # downstream branches via ``inbound_dedup_state`` so we never
        # double-load the row. State persists in
        # ``Conversation.extra_metadata`` (rolling window of last 50
        # message ids), which means the guard survives container
        # restarts and is shared across worker processes.
        inbound_dedup_state = None
        if sender and msg_id:
            try:
                inbound_dedup_state = StateManager.load(
                    db, phone=sender, tenant_id=resolved_tenant_id,
                )
                if IdempotencyGuard.is_duplicate(inbound_dedup_state, msg_id):
                    logger.info(
                        "[Idempotency] DROP duplicate inbound msg_id=%s "
                        "tenant=%s from=%s — Meta webhook retry",
                        msg_id, resolved_tenant_id, sender,
                    )
                    return
                IdempotencyGuard.mark_processed(inbound_dedup_state, msg_id)
                StateManager.save(
                    db, inbound_dedup_state, tenant_id=resolved_tenant_id,
                )
            except Exception as _dedup_exc:
                # Never block real traffic on a dedup-table hiccup. Log
                # at WARNING (not DEBUG) so a repeated failure surfaces.
                logger.warning(
                    "[Idempotency] guard load/save failed tenant=%s msg_id=%s "
                    "err=%s — proceeding without dedup for this message",
                    resolved_tenant_id, msg_id, _dedup_exc,
                )

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

            # ── Unsubscribe / Pending / Re-subscribe gate ────────────────────
            # 3-state flow:
            #   PENDING_UNSUBSCRIBE → confirmation buttons sent, AI/automation paused
            #   UNSUBSCRIBED        → final, all sends blocked
            #   ORDINARY            → normal operation
            #
            # This block runs BEFORE the automation_event emit and the AI
            # routing so the system never wastes work on a customer who
            # is asking to be left alone.
            _unsub_short_circuit = False
            try:
                from services.unsubscribe import (  # noqa: PLC0415
                    UNSUB_CANCEL_BUTTON_ID,
                    UNSUB_CONFIRM_BUTTON_ID,
                    CANCELLED_UNSUB_MSG_AR,
                    FINAL_UNSUBSCRIBED_MSG_AR,
                    build_confirmation_fallback_payload,
                    build_confirmation_payload,
                    build_text_payload,
                    classify_confirmation_text,
                    clear_pending_unsubscribe,
                    expire_pending_if_needed,
                    is_customer_pending_unsubscribe,
                    is_customer_unsubscribed,
                    is_unsubscribe_request,
                    mark_pending_unsubscribe,
                    mark_pending_prompt_sent,
                    mark_resubscribed,
                    mark_unsubscribed,
                    should_send_pending_prompt,
                )

                _inbound_text = ""
                if msg_type == "text":
                    _inbound_text = (msg.get("text") or {}).get("body", "")

                _btn_id = ""
                _btn_title = ""
                if msg_type == "interactive":
                    _interactive = msg.get("interactive") or {}
                    if _interactive.get("type") == "button_reply":
                        _btn_id    = ((_interactive.get("button_reply") or {}).get("id")    or "")
                        _btn_title = ((_interactive.get("button_reply") or {}).get("title") or "")

                # ── Get/create the visible dashboard conversation so every
                # unsubscribe message — inbound and outbound — appears in the
                # merchant inbox just like normal AI conversations.
                from routers.conversations import _get_or_create_conversation  # noqa: PLC0415
                # StateManager is imported at module level — no local re-import
                # (a local `from … import StateManager` inside this try-block
                # causes Python to treat StateManager as a local variable for the
                # ENTIRE _dispatch_message scope, triggering UnboundLocalError at
                # the idempotency guard on line ~705 which runs BEFORE this block.)
                _unsub_convo = None
                try:
                    _unsub_convo = _get_or_create_conversation(db, resolved_tenant_id, normalized_sender)
                    if _unsub_convo and _unsub_convo.status != "human" and not _unsub_convo.is_human_handoff:
                        _unsub_convo.status = "active"
                        db.add(_unsub_convo)
                        db.flush()
                except Exception as _convo_exc:
                    logger.warning("[Webhook] failed to ensure unsub convo: %s", _convo_exc)

                _convo_id = getattr(_unsub_convo, "id", None)

                def _save_unsub_msg(body: str, direction: str) -> None:
                    """Persist an unsubscribe-related message to MessageEvent so
                    it shows up in the merchant inbox."""
                    if not body:
                        return
                    try:
                        StateManager.save_message(
                            db, normalized_sender, body, direction,
                            conversation_id=_convo_id, tenant_id=resolved_tenant_id,
                        )
                    except Exception as _save_exc:
                        logger.debug("[Webhook] save unsub msg failed: %s", _save_exc)

                async def _send_unsub_confirmation_prompt() -> bool:
                    """Send quick-reply buttons inside the open session window.

                    This is intentionally NOT a Meta template. If interactive
                    buttons fail for any provider/device reason, fall back to a
                    short plain-text instruction that accepts `1` / `2`.
                    """
                    from services.unsubscribe import CONFIRMATION_BODY_AR, CONFIRMATION_FALLBACK_MSG_AR  # noqa: PLC0415
                    ok = await _post_wa(
                        phone_id=phone_number_id,
                        payload=build_confirmation_payload(normalized_sender),
                        _tenant_id=resolved_tenant_id, _db=db,
                    )
                    if ok:
                        _save_unsub_msg(CONFIRMATION_BODY_AR, "outbound")
                    else:
                        logger.warning(
                            "[Webhook] interactive unsubscribe prompt failed; sending text fallback"
                        )
                        ok = await _post_wa(
                            phone_id=phone_number_id,
                            payload=build_confirmation_fallback_payload(normalized_sender),
                            _tenant_id=resolved_tenant_id, _db=db,
                        )
                        if ok:
                            _save_unsub_msg(CONFIRMATION_FALLBACK_MSG_AR, "outbound")
                    if ok and _lead:
                        mark_pending_prompt_sent(db, _lead, commit=True)
                    return bool(ok)

                # Clear stale pending state on the next inbound message so a
                # customer never remains suspended forever if they ignored the
                # buttons. We still honour explicit buttons before this block's
                # normal routing returns.
                if _lead and not _btn_id:
                    expire_pending_if_needed(db, _lead, commit=True)

                _fallback_decision = (
                    classify_confirmation_text(_inbound_text)
                    if _lead and is_customer_pending_unsubscribe(_lead) and _inbound_text
                    else None
                )

                # ── 1. Final-state customer sent any new inbound → restore ───
                # Requirement: after final unsubscribe, ANY customer-originated
                # inbound message brings them back to normal lists. Do this
                # before keyword detection so a stale "إلغاء" message doesn't
                # trap an already-unsubscribed customer in a loop.
                if _lead and is_customer_unsubscribed(_lead):
                    mark_resubscribed(db, _lead, commit=True)
                    logger.info(
                        "[Webhook] RE-SUBSCRIBED %s tenant=%s",
                        normalized_sender, resolved_tenant_id,
                    )
                    # Continue processing normally — fall through.

                # ── 2. Confirmation button: "نعم متأكد" ──────────────────────
                elif _lead and (
                    _btn_id == UNSUB_CONFIRM_BUTTON_ID
                    or _fallback_decision == "confirm"
                ):
                    # Persist the customer's confirmation reply so it shows in inbox
                    _save_unsub_msg(_btn_title or _inbound_text or "نعم متأكد", "inbound")
                    mark_unsubscribed(db, _lead, commit=True)
                    try:
                        _ok = await _post_wa(
                            phone_id=phone_number_id,
                            payload=build_text_payload(normalized_sender, FINAL_UNSUBSCRIBED_MSG_AR),
                            _tenant_id=resolved_tenant_id, _db=db,
                        )
                        if _ok:
                            _save_unsub_msg(FINAL_UNSUBSCRIBED_MSG_AR, "outbound")
                    except Exception as _send_exc:
                        logger.warning("[Webhook] Failed to send goodbye msg: %s", _send_exc)
                    logger.info(
                        "[Webhook] UNSUBSCRIBE CONFIRMED via button | tenant=%s phone=%s",
                        resolved_tenant_id, normalized_sender,
                    )
                    _unsub_short_circuit = True

                # ── 3. Cancel button: "تراجع" ────────────────────────────────
                elif _lead and (
                    _btn_id == UNSUB_CANCEL_BUTTON_ID
                    or _fallback_decision == "cancel"
                ):
                    _save_unsub_msg(_btn_title or _inbound_text or "تراجع", "inbound")
                    clear_pending_unsubscribe(db, _lead, commit=True)
                    try:
                        _ok = await _post_wa(
                            phone_id=phone_number_id,
                            payload=build_text_payload(normalized_sender, CANCELLED_UNSUB_MSG_AR),
                            _tenant_id=resolved_tenant_id, _db=db,
                        )
                        if _ok:
                            _save_unsub_msg(CANCELLED_UNSUB_MSG_AR, "outbound")
                    except Exception as _send_exc:
                        logger.warning("[Webhook] Failed to send cancel msg: %s", _send_exc)
                    logger.info(
                        "[Webhook] UNSUBSCRIBE CANCELLED via button | tenant=%s phone=%s",
                        resolved_tenant_id, normalized_sender,
                    )
                    _unsub_short_circuit = True

                # ── 4. Inbound text matches an unsubscribe keyword ──────────
                elif _lead and is_unsubscribe_request(_inbound_text):
                    _save_unsub_msg(_inbound_text, "inbound")
                    if not is_customer_pending_unsubscribe(_lead):
                        mark_pending_unsubscribe(db, _lead, commit=True)
                    # Always (re-)send confirmation prompt so the customer
                    # never gets stuck without a way to opt out.
                    try:
                        await _send_unsub_confirmation_prompt()
                    except Exception as _send_exc:
                        logger.warning("[Webhook] Failed to send confirmation prompt: %s", _send_exc)
                    logger.info(
                        "[Webhook] UNSUBSCRIBE PENDING (sent confirmation) | tenant=%s phone=%s",
                        resolved_tenant_id, normalized_sender,
                    )
                    _unsub_short_circuit = True

                # ── 5. Customer is already PENDING and sent a non-button msg ─
                elif _lead and is_customer_pending_unsubscribe(_lead) and not _btn_id:
                    _save_unsub_msg(_inbound_text, "inbound")
                    # Don't run AI/automation while pending — just nudge them
                    # again (throttled) so they see the buttons.
                    try:
                        if should_send_pending_prompt(_lead):
                            await _send_unsub_confirmation_prompt()
                    except Exception as _send_exc:
                        logger.warning("[Webhook] Failed to re-send confirmation prompt: %s", _send_exc)
                    logger.info(
                        "[Webhook] UNSUBSCRIBE STILL PENDING (resent prompt) | tenant=%s phone=%s",
                        resolved_tenant_id, normalized_sender,
                    )
                    _unsub_short_circuit = True

            except Exception as _unsub_exc:
                logger.warning("[Webhook] Unsubscribe gate error: %s", _unsub_exc)

            if _unsub_short_circuit:
                # Skip automation / AI for unsubscribe-related events.
                return

            # ── Email: smart notification (first message OR 24h silence) ────
            # Fixed: old logic used total_messages which doesn't exist on Customer
            # and fired on every single message. New logic:
            #   1. Truly first message from this customer ever → send
            #   2. Customer returns after 24h silence         → send
            #   3. Anything else                              → skip + log
            if not _hist_skip_live:
                try:
                    _notify_result = _should_notify_merchant_email(
                        db=db,
                        tenant_id=resolved_tenant_id,
                        customer=_lead,
                        silence_hours=24,
                    )
                    if _notify_result["send"]:
                        from services.email_service import enqueue_email as _enq  # noqa: PLC0415
                        from database.models import User as _U                     # noqa: PLC0415
                        _mu = db.query(_U).filter(
                            _U.tenant_id == resolved_tenant_id, _U.role == "merchant",
                        ).first()
                        if _mu and _mu.email:
                            _msg_text = ""
                            if msg_type == "text":
                                _msg_text = (msg.get("text") or {}).get("body", "")
                            _is_new   = _notify_result["reason"] == "first_message"
                            _enq(
                                to=_mu.email,
                                subject=(
                                    "🎉 أول رسالة واتساب وصلت لمتجرك!"
                                    if _is_new else
                                    "💬 عميل عاد ليتواصل معك على واتساب"
                                ),
                                template="first_whatsapp_message",
                                sender_type="growth",
                                variables={
                                    "merchant_name":   _mu.username or "",
                                    "customer_name":   contact_name or "",
                                    "customer_phone":  normalized_sender,
                                    "message_preview": _msg_text,
                                    "conversation_url": f"{__import__('core.config', fromlist=['DASHBOARD_URL']).DASHBOARD_URL}/conversations",
                                },
                            )
                            _log_notification(
                                db=db, tenant_id=resolved_tenant_id,
                                customer_id=getattr(_lead, "id", None),
                                event="returning_customer" if not _is_new else "new_whatsapp_message",
                                status="sent",
                                details={"phone": normalized_sender, "preview": _msg_text[:80]},
                            )
                    else:
                        _log_notification(
                            db=db, tenant_id=resolved_tenant_id,
                            customer_id=getattr(_lead, "id", None),
                            event="new_whatsapp_message",
                            status="skipped",
                            reason=_notify_result.get("reason_ar", ""),
                            details={"phone": normalized_sender},
                        )
                except Exception as _em:
                    logger.debug("[Webhook] smart-notify email error: %s", _em)

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
        if not _hist_skip_live:
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

        normalized_inbound = await normalize_whatsapp_inbound(
            db=db,
            wa_conn=wa_conn,
            tenant_id=resolved_tenant_id,
            message=msg,
        )
        logger.info(
            "[TRACE][3/6] INBOUND_NORMALIZED | tenant_id=%s sender=%s normalized_type=%s should_process=%s",
            resolved_tenant_id, sender,
            normalized_inbound.normalized_type,
            normalized_inbound.should_process,
        )

        # ── Handle interactive button replies ──────────────────────────────────────
        if normalized_inbound.normalized_type == "interactive":
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

                # Product-pick buttons from merchant brain — route to merchant AI
                if btn_id.startswith("pick_") and not _is_platform_tenant(db, resolved_tenant_id):
                    pick_num = btn_id.split("_", 1)[-1]  # "1", "2", "3"
                    await _handle_merchant_message(
                        phone_id=used_pid, to=sender, text=pick_num,
                        tenant_id=resolved_tenant_id, db=db,
                        wa_message_ts=_wa_msg_ts,
                        wa_msg_id=msg_id or None,
                    )
                    return

                # Product-option buttons (size/color quick-replies). The title
                # carries the human-readable value name ("M", "أسود"), which
                # `_merge_message_options` already matches via its value-name
                # path. Forward the title so the brain treats it like a normal
                # text reply.
                if btn_id.startswith("opt_") and not _is_platform_tenant(db, resolved_tenant_id):
                    forwarded = (btn_txt or "").strip() or btn_id.split("_", 1)[-1]
                    logger.info(
                        "[WA PARSER] extracted_text=%r source=button_reply btn_id=%s tenant=%s",
                        forwarded, btn_id, resolved_tenant_id,
                    )
                    await _handle_merchant_message(
                        phone_id=used_pid, to=sender, text=forwarded,
                        tenant_id=resolved_tenant_id, db=db,
                        wa_message_ts=_wa_msg_ts,
                        wa_msg_id=msg_id or None,
                    )
                    return

                # Generic interactive button from merchant tenant — forward the
                # human-readable title to the brain so it behaves like typed text.
                if not _is_platform_tenant(db, resolved_tenant_id) and btn_txt:
                    logger.info(
                        "[WA PARSER] extracted_text=%r source=button_reply (generic) btn_id=%s tenant=%s",
                        btn_txt, btn_id, resolved_tenant_id,
                    )
                    await _handle_merchant_message(
                        phone_id=used_pid, to=sender, text=btn_txt,
                        tenant_id=resolved_tenant_id, db=db,
                        wa_message_ts=_wa_msg_ts,
                        wa_msg_id=msg_id or None,
                    )
                    return

                await _handle_button_reply(
                    btn_id=btn_id, phone_id=used_pid, to=sender,
                    tenant_id=resolved_tenant_id, db=db,
                )

            elif interactive.get("type") == "list_reply":
                lr       = interactive.get("list_reply", {}) or {}
                lr_id    = lr.get("id", "")
                lr_title = (lr.get("title", "") or lr_id).strip()
                if lr_title and not _is_platform_tenant(db, resolved_tenant_id):
                    logger.info(
                        "[WA PARSER] extracted_text=%r source=list_reply lr_id=%s tenant=%s",
                        lr_title, lr_id, resolved_tenant_id,
                    )
                    await _handle_merchant_message(
                        phone_id=used_pid, to=sender, text=lr_title,
                        tenant_id=resolved_tenant_id, db=db,
                        wa_message_ts=_wa_msg_ts,
                        wa_msg_id=msg_id or None,
                    )
            return

        if normalized_inbound.normalized_type not in {"text", "audio"}:
            # ── Button-tap rescue: "button" type = customer tapped a template
            # quick-reply.  The normalizer marks it unsupported, but we have
            # already extracted human-readable text via _extract_wa_message_text.
            # Treat it exactly like a text message so the Brain receives it.
            if _wa_text and msg_type == "button" and not _is_platform_tenant(db, resolved_tenant_id):
                logger.info(
                    "[WA PARSER] button tap rescued | tenant=%s text=%r source=%s",
                    resolved_tenant_id, _wa_text, _wa_source,
                )
                await _handle_merchant_message(
                    phone_id=used_pid, to=sender, text=_wa_text,
                    tenant_id=resolved_tenant_id, db=db,
                    wa_message_ts=_wa_msg_ts,
                    wa_msg_id=msg_id or None,
                )
                return
            logger.info(
                "[TRACE][4/6] INBOUND_IGNORED_UNSUPPORTED | tenant_id=%s sender=%s normalized_type=%s",
                resolved_tenant_id, sender, normalized_inbound.normalized_type,
            )
            return

        text = normalized_inbound.text.strip()
        if not text:
            logger.info(
                "[TRACE][4/6] INBOUND_IGNORED_EMPTY_TEXT | tenant_id=%s sender=%s normalized_type=%s",
                resolved_tenant_id, sender, normalized_inbound.normalized_type,
            )
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
            logger.info(
                "[TRACE][4/6] ROUTE_MERCHANT_AI | tenant_id=%s sender=%s text_len=%s",
                resolved_tenant_id, sender, len(text),
            )
            logger.info(
                "[WEBHOOK_ROUTE] route=merchant_ai tenant=%s from=%s msg_id=%s",
                resolved_tenant_id, sender, msg_id,
            )
            await _handle_merchant_message(
                phone_id=used_pid, to=sender, text=text,
                tenant_id=resolved_tenant_id, db=db,
                inbound_metadata=normalized_inbound.metadata,
                wa_message_ts=_wa_msg_ts,
                wa_msg_id=msg_id or None,
            )
            return

        turn_log: Optional[TurnLog] = None
        effective_tenant_id = resolved_tenant_id

        # ── ① Load state — scoped to the correct merchant tenant ──────────────
        # Reuse the row already loaded by the universal dedup guard above
        # when the tenant matches; this avoids a second SELECT/UPDATE
        # round-trip per inbound. Falls back to a fresh load only when
        # the dedup guard couldn't run (no msg_id, or DB hiccup logged).
        if (
            inbound_dedup_state is not None
            and effective_tenant_id == resolved_tenant_id
        ):
            state = inbound_dedup_state
        else:
            state = StateManager.load(
                db, phone=sender, tenant_id=effective_tenant_id,
            )

        # ── Greeting catch-net ────────────────────────────────────────────────
        # If the persisted state predates the `greeted` flag (or was reset by
        # a corrupt save), but message history shows we already replied to
        # this customer at least once, we MUST treat the conversation as
        # already greeted. Prevents the platform bot from re-introducing
        # itself on every "هلا" after a redeploy.
        if not state.greeted:
            try:
                _hist = StateManager.load_history(
                    db, phone=sender, limit=HISTORY_WINDOW,
                    tenant_id=effective_tenant_id,
                )
                if any((m.get("direction") == "outbound") for m in _hist):
                    state.greeted = True
                    logger.info(
                        "[Engine] inferred greeted=True from history phone=%s tenant=%s",
                        sender, effective_tenant_id,
                    )
            except Exception as _exc:  # observability only
                logger.debug("[Engine] greeted-from-history check failed: %s", _exc)

        stage_before = state.stage
        logger.info(
            "[TRACE][3/6] SESSION_LOADED | tenant_id=%s sender=%s stage=%s",
            effective_tenant_id, sender, stage_before,
        )

        # NOTE: idempotency check moved to ``_dispatch_message`` (universal
        # guard right after tenant resolution) so merchant + interactive
        # paths get the same protection. The legacy per-branch check that
        # used to live here would now always fire on a re-loaded state
        # row — the universal guard already marked the id as processed.
        state.turn += 1

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
            # Lock the greeting so subsequent "هلا" / "مرحبا" don't replay
            # the welcome menu — they fall through to GENERATE_AI_REPLY.
            state.greeted = True

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
            state_ctx = ContextBuilder.build_system_injection(
                state, action, decision_reason, intent=intent,
            )
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
        # Release the per-conversation lock BEFORE closing the DB session so
        # the next queued inbound for this customer can start as soon as
        # possible. We pass (None, None, None) to __aexit__ since any
        # exception propagating out of the try body will still be raised
        # by Python after this finally completes.
        if _conv_lock_active and _conv_lock_cm is not None:
            try:
                await _conv_lock_cm.__aexit__(None, None, None)
            except Exception as _lock_exc:  # never let lock release block cleanup
                logger.warning(
                    "[ORDER FLOW] conversation lock release error | tenant=%s phone=%s err=%s",
                    locals().get("resolved_tenant_id"), sender, _lock_exc,
                )
            _conv_lock_active = False
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
    inbound_metadata: Optional[Dict[str, Any]] = None,
    wa_message_ts: Optional[datetime] = None,
    wa_msg_id: Optional[str] = None,
) -> None:
    """
    For merchant tenants (tenant_id > 1): reply using the store's own AI context.
    Bypasses the platform sales engine (intent/stage/decision) entirely.

    INVARIANT: this handler is only ever invoked from the WhatsApp webhook
    in response to an actual inbound customer message. It must NEVER be
    used to start a conversation. Background automations (campaigns,
    cart recovery, COD confirmations, payment reminders) have their own
    dedicated paths that emit pre-approved templates / canned copy and
    never enter this conversational pipeline.
    """
    if not (text or "").strip():
        # Hard guard: refuse to spend tokens / send replies on empty
        # inbound. Empty body usually means the upstream parser failed
        # to extract text from a non-text message type.
        logger.info(
            "[Merchant] DROPPED empty inbound — no reply generated | tenant=%s to=%s",
            tenant_id, to,
        )
        return
    logger.info(
        "[Merchant/INBOUND_TRIGGER] tenant=%s from=%s direction=inbound text_len=%d snippet=%r",
        tenant_id, to, len(text or ""), (text or "")[:60],
    )

    # ── Strict AI activation cutoff (WhatsApp business timestamp) ───────────
    # Anything strictly before ``whatsapp_ai_live_since`` is persisted for
    # inbox/history only — never COD / Brain / outbound automation side-effects.
    from datetime import timezone as _tz_hist  # noqa: PLC0415

    from core.whatsapp_ai_live import is_inbound_before_ai_live_since  # noqa: PLC0415

    wa_conn_hist = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == tenant_id)
        .first()
    )
    if is_inbound_before_ai_live_since(wa_conn_hist, wa_message_ts):
        from routers.conversations import _get_or_create_conversation  # noqa: PLC0415

        convo_hist = _get_or_create_conversation(db, tenant_id, to)
        if convo_hist.status != "human" and not convo_hist.is_human_handoff:
            convo_hist.status = "active"
        db.add(convo_hist)
        db.flush()

        _cutoff = getattr(wa_conn_hist, "whatsapp_ai_live_since", None)
        _msg_iso = wa_message_ts.isoformat() if wa_message_ts else ""
        _cut_iso = _cutoff.isoformat() if _cutoff else ""

        _created_naive = wa_message_ts
        if _created_naive is not None and _created_naive.tzinfo is not None:
            _created_naive = _created_naive.astimezone(_tz_hist.utc).replace(tzinfo=None)

        _hist_meta = {
            "historical_import": True,
            "message_origin": "historical_sync",
            "wa_message_id": wa_msg_id or "",
            "whatsapp_timestamp": _msg_iso,
        }
        StateManager.save_message(
            db, to, text, "inbound",
            conversation_id=convo_hist.id,
            tenant_id=tenant_id,
            created_at=_created_naive,
            extra_metadata=_hist_meta,
        )
        logger.info(
            "[HISTORICAL_MESSAGE_SKIP_AI] tenant_id=%s conversation_id=%s message_id=%s "
            "message_ts=%s ai_live_since=%s",
            tenant_id, convo_hist.id, wa_msg_id or "", _msg_iso, _cut_iso,
        )
        return

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
        if convo.status != "human" and not convo.is_human_handoff:
            convo.status = "active"
        db.add(convo)
        db.flush()

        # Persist inbound immediately for inbox visibility and history continuity.
        _live_in_meta: Dict[str, Any] = {
            "message_origin": "live_webhook",
            "historical_import": False,
        }
        if wa_msg_id:
            _live_in_meta["wa_message_id"] = wa_msg_id
        if wa_message_ts:
            _live_in_meta["whatsapp_timestamp"] = wa_message_ts.isoformat()
        StateManager.save_message(
            db, to, text, "inbound",
            conversation_id=convo.id,
            tenant_id=tenant_id,
            extra_metadata=_live_in_meta,
        )

        # ── AI loop / cost guard ─────────────────────────────────────────
        # Runs BEFORE any LLM/Brain call. If the conversation is paused
        # (manual takeover, internal/blocked number, prior bot-loop
        # detection, rate-limit cap, or human handoff already issued), we
        # store the inbound message above and return without spending any
        # tokens. See `core/ai_pause_guard` for the full policy.
        try:
            from core.ai_pause_guard import should_skip_ai as _ai_should_skip  # noqa: PLC0415
            _skip, _skip_reason = _ai_should_skip(
                db, convo,
                tenant_id=tenant_id,
                customer_phone=to,
                inbound_text=text,
            )
        except Exception as _guard_exc:
            logger.warning("[ai_pause] guard failed (open): %s", _guard_exc)
            _skip, _skip_reason = False, None

        if _skip:
            logger.info(
                "[ai_pause] SKIP_LLM tenant=%s convo=%s to=%s reason=%s — inbound stored, no reply",
                tenant_id, convo.id, to, _skip_reason,
            )
            try:
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
            return

        if inbound_metadata:
            try:
                latest_event = (
                    db.query(MessageEvent)
                    .filter(
                        MessageEvent.tenant_id == tenant_id,
                        MessageEvent.conversation_id == convo.id,
                    )
                    .order_by(MessageEvent.id.desc())
                    .first()
                )
                if latest_event:
                    meta = dict(latest_event.extra_metadata or {})
                    meta["normalized_inbound"] = dict(inbound_metadata)
                    latest_event.extra_metadata = meta
                    db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass

        # Keep a lightweight state row in sync with the same phone key used by history.
        state = StateManager.load(db, phone=to, tenant_id=tenant_id)
        state.turn += 1
        state.stage = "active"
        StateManager.save(db, state, tenant_id=tenant_id)

        # Load recent conversation history for both paths
        history = StateManager.load_history(db, phone=to, tenant_id=tenant_id)
        _brain_buttons: list = []  # populated by brain when product buttons should be sent
        _brain_handoff: bool = False  # set True only by the brain handoff branch

        # ── Top-level Conversation Mode Controller ───────────────────────────
        # Decides who owns this turn (live chat, automation recovery,
        # identity reply, support escalation, checkout assist, post
        # purchase) BEFORE routing to Brain or legacy. Persists a sticky
        # lease on the conversation so a free-form reply that overrides
        # automation cannot bounce back to recovery in the same window.
        from modules.ai.routing.conversation_mode import (  # noqa: PLC0415
            MODE_IDENTITY_REPLY,
            mode_prompt_overlay,
            render_identity_reply,
            resolve_conversation_mode,
            save_lease,
        )
        mode_decision = resolve_conversation_mode(
            db,
            tenant_id=tenant_id,
            convo=convo,
            customer_phone=to,
            text=text,
            history=history,
        )
        save_lease(db, convo, mode_decision.lease)
        logger.info("[Mode] tenant=%s to=%s decision=%s",
                    tenant_id, to, mode_decision.to_log_dict())

        # ── Trial / subscription guard for AI replies ────────────────────────
        # If trial expired and no subscription: send a static fallback reply
        # so the customer isn't left hanging, but skip the expensive AI call.
        from core.billing import has_billing_access as _has_billing  # noqa: PLC0415
        if not _has_billing(db, tenant_id):
            reply = "شكراً لتواصلك! هذا الحساب في وضع التجربة المنتهية. يُرجى التواصل مع صاحب المتجر."
            StateManager.save_message(db, to, reply, "outbound", conversation_id=convo.id, tenant_id=tenant_id)
            await _send_whatsapp_message(phone_id=phone_id, to=to, text=reply, _tenant_id=tenant_id, _db=db)
            return

        # ── Identity / greeting fast path ────────────────────────────────────
        # When the customer asked "who are you?" / "السلام عليكم", answer
        # deterministically with the merchant's configured assistant name
        # so we never fall through to AI fallbacks that might leak
        # automation boilerplate.
        if mode_decision.mode == MODE_IDENTITY_REPLY:
            reply = render_identity_reply(
                db, tenant_id=tenant_id, topic=mode_decision.identity_topic,
            )
            StateManager.save_message(
                db, to, reply, "outbound",
                conversation_id=convo.id, tenant_id=tenant_id,
            )
            await _send_whatsapp_message(
                phone_id=phone_id, to=to, text=reply,
                _tenant_id=tenant_id, _db=db,
            )
            logger.info(
                "[Mode] identity_reply tenant=%s topic=%s",
                tenant_id, mode_decision.identity_topic,
            )
            return

        # ── Human handoff / support escalation ───────────────────────────────
        # If the dashboard conversation is flagged for human handoff, do NOT
        # call the LLM/Brain. Production showed a request hanging after the
        # mode resolver returned support_escalation, leaving the whole service
        # unresponsive. This path is deterministic, fast, and respects the
        # merchant's intent: a human owns the conversation.
        try:
            from modules.ai.routing.conversation_mode import MODE_SUPPORT_ESCALATION  # noqa: PLC0415
        except Exception:
            MODE_SUPPORT_ESCALATION = "support_escalation"  # type: ignore[assignment]
        if mode_decision.mode == MODE_SUPPORT_ESCALATION:
            # ── Order-flow recovery override ──────────────────────────────────────
            # A customer who keeps sending order data (national short address code,
            # explicit order intent, numeric pick) is NOT asking for human support.
            # The handoff flag and/or a non-recovery lease are often left over
            # from a misclassified intent or a Salla failure — we must not let
            # them trap the customer. The signal detector is centralised in
            # `conversation_mode.message_has_order_recovery_signal` so the
            # human_handoff_flag override, the live_chat_lease_held override,
            # and the engine's ACTION_HANDOFF guard all stay in sync.
            try:
                from modules.ai.routing.conversation_mode import (  # noqa: PLC0415
                    message_has_order_recovery_signal,
                    save_lease as _save_lease,
                    _build_lease as _mk_lease,
                    MODE_LIVE_CHAT as _MODE_LIVE_CHAT,
                    SOURCE_OVERRIDE_FREEFORM as _SRC_OVERRIDE,
                    DEFAULT_LEASE_MINUTES_LIVE_CHAT as _LEASE_MIN,
                )
            except Exception:
                message_has_order_recovery_signal = lambda _t: False  # type: ignore
                _save_lease = None  # type: ignore
                _mk_lease = None    # type: ignore

            _has_recovery_signal = bool(message_has_order_recovery_signal(text or ""))
            _was_lease_held = (
                getattr(mode_decision, "source", "") == "live_chat_lease_held"
            )

            if _has_recovery_signal:
                if _was_lease_held:
                    logger.info(
                        "[ORDER FLOW] restoring flow after live chat lease | "
                        "tenant=%s to=%s lease_until=%s",
                        tenant_id, to, mode_decision.lease.locked_until,
                    )
                    logger.info(
                        "[ORDER FLOW] clearing live chat lease for order recovery | "
                        "tenant=%s to=%s",
                        tenant_id, to,
                    )
                else:
                    logger.info(
                        "[ORDER FLOW] restoring flow after escalation flag | "
                        "tenant=%s to=%s",
                        tenant_id, to,
                    )
                logger.info(
                    "[ORDER FLOW] skipping HUMAN_HANDOFF_ACK due to order recovery signal | "
                    "tenant=%s to=%s text_preview=%r",
                    tenant_id, to, (text or "")[:40],
                )
                logger.info(
                    "[ORDER FLOW] ignoring human handoff flag — clearing on conversation"
                )
                # Reset conversation handoff/pause flags. We deliberately
                # DO NOT touch convo.extra_metadata['brain_state'] here —
                # the Order Flow's order_prep must survive the lease
                # reset so the customer doesn't have to re-enter data.
                try:
                    convo.is_human_handoff = False
                    convo.paused_by_human = False
                    if convo.status == "human":
                        convo.status = "active"
                    db.flush()
                except Exception as _flag_exc:
                    logger.warning("[ORDER FLOW] flag clear failed: %s", _flag_exc)

                # Replace the held escalation/checkout lease with a fresh
                # live_chat lease so the resolver doesn't re-pin the next
                # turn back to support_escalation.
                if _save_lease and _mk_lease:
                    try:
                        _new_lease = _mk_lease(
                            mode=_MODE_LIVE_CHAT,
                            previous_mode=mode_decision.mode,
                            reason="order recovery signal — webhook override",
                            source=_SRC_OVERRIDE,
                            minutes=_LEASE_MIN,
                        )
                        _save_lease(db, convo, _new_lease)
                    except Exception as _lease_exc:
                        logger.warning("[ORDER FLOW] lease reset failed: %s", _lease_exc)
                # Fall through to Brain pipeline below — DO NOT return.
            else:
                # Handoff notice cooldown — never re-send the same
                # acknowledgement within HANDOFF_NOTICE_COOLDOWN_SEC even
                # if the upstream pause flag was cleared by a different
                # code path. This is belt-and-suspenders on top of the
                # ai_pause_guard so a brief race can't replay the line.
                _HANDOFF_COOLDOWN_SEC = 1800  # 30 min
                _last_at = None
                try:
                    _meta = (convo.extra_metadata or {}) if convo is not None else {}
                    _raw = _meta.get("last_handoff_notice_at") if isinstance(_meta, dict) else None
                    if _raw:
                        from datetime import datetime as _dt, timezone as _tz  # noqa: PLC0415
                        _dt_parsed = _dt.fromisoformat(str(_raw))
                        if _dt_parsed.tzinfo is None:
                            _dt_parsed = _dt_parsed.replace(tzinfo=_tz.utc)
                        _last_at = _dt_parsed
                except Exception:
                    _last_at = None
                from datetime import datetime as _dt2, timezone as _tz2  # noqa: PLC0415
                _now_utc = _dt2.now(_tz2.utc)
                _within_cooldown = (
                    _last_at is not None
                    and (_now_utc - _last_at).total_seconds() < _HANDOFF_COOLDOWN_SEC
                )
                if _within_cooldown:
                    logger.info(
                        "[HANDOFF_DEDUP] skip handoff notice — within cooldown | "
                        "tenant=%s to=%s last_at=%s seconds_since=%d",
                        tenant_id, to, _last_at,
                        int((_now_utc - _last_at).total_seconds()),
                    )
                    # Still ensure AI stays paused so subsequent inbound
                    # turns don't loop back into this branch.
                    try:
                        from core.ai_pause_guard import pause_ai as _pause_ai, REASON_HUMAN_HANDOFF as _R_HOFF  # noqa: PLC0415
                        _pause_ai(db, convo, reason=_R_HOFF, by="system:human_handoff")
                    except Exception as _hoff_exc:
                        logger.debug("[ai_pause] handoff pause failed: %s", _hoff_exc)
                    logger.info(
                        "[OUTBOUND] tenant=%s to=%s source=handoff_dedup trigger=inbound "
                        "intent=human_handoff handoff_triggered=true dedup_blocked=true reply_len=0",
                        tenant_id, to,
                    )
                    return

                reply = (
                    "وصلت رسالتك. تم تحويل المحادثة لفريق المتجر، "
                    "وسيرد عليك أحد الموظفين في أقرب وقت."
                )
                StateManager.save_message(
                    db, to, reply, "outbound",
                    conversation_id=convo.id, tenant_id=tenant_id,
                )
                _send_ok = await _send_whatsapp_message(
                    phone_id=phone_id, to=to, text=reply,
                    _tenant_id=tenant_id, _db=db,
                )
                if _send_ok:
                    logger.info("[TRACE][5/6] HUMAN_HANDOFF_ACK_SENT | tenant=%s to=%s", tenant_id, to)
                    # Stamp the cooldown marker so duplicate webhook
                    # deliveries / racing turns don't replay the line.
                    try:
                        _new_meta = dict(convo.extra_metadata or {})
                        _new_meta["last_handoff_notice_at"] = _now_utc.isoformat()
                        convo.extra_metadata = _new_meta
                        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
                        flag_modified(convo, "extra_metadata")
                        db.add(convo)
                        db.flush()
                    except Exception as _stamp_exc:
                        logger.debug("[handoff] cooldown stamp failed: %s", _stamp_exc)
                else:
                    logger.error("[TRACE][5/6] HUMAN_HANDOFF_ACK_SEND_FAILED | tenant=%s to=%s", tenant_id, to)
                # Pause AI for this conversation so subsequent inbound
                # messages don't keep replaying the same handoff acknowledgement.
                try:
                    from core.ai_pause_guard import pause_ai as _pause_ai, REASON_HUMAN_HANDOFF as _R_HOFF  # noqa: PLC0415
                    _pause_ai(db, convo, reason=_R_HOFF, by="system:human_handoff")
                except Exception as _hoff_exc:
                    logger.debug("[ai_pause] handoff pause failed: %s", _hoff_exc)
                logger.info(
                    "[OUTBOUND] tenant=%s to=%s source=support_escalation trigger=inbound "
                    "intent=human_handoff handoff_triggered=true dedup_blocked=false "
                    "reply_len=%d",
                    tenant_id, to, len(reply),
                )
                return

        # ── Merchant Brain (Phase 1) ──────────────────────────────────────────
        # Active when: global flag is on OR this tenant is in the per-tenant list
        _brain_active = MERCHANT_BRAIN_ENABLED or (tenant_id in MERCHANT_BRAIN_TENANT_IDS)
        if _brain_active:
            try:
                from modules.ai.brain.pipeline import get_brain  # noqa: PLC0415
                from services.customer_intelligence import CustomerIntelligenceService  # noqa: PLC0415

                profile = {}
                try:
                    svc = CustomerIntelligenceService(db, tenant_id)
                    customer = svc.upsert_lead_customer(
                        phone=to,
                        source="whatsapp_inbound",
                        extra_metadata={
                            "channel": "whatsapp",
                            "normalized_inbound": inbound_metadata or {},
                        },
                        commit=False,
                    )
                    profile = {
                        "name":  getattr(customer, "name", None) or "",
                        "email": getattr(customer, "email", None) or "",
                        "id":    getattr(customer, "id", None),
                    }
                    if customer is not None:
                        full_profile = svc.ensure_profile(customer)
                        profile.update({
                            "segment": getattr(full_profile, "segment", "") or "",
                            "customer_status": getattr(full_profile, "customer_status", "") or "",
                            "rfm_segment": getattr(full_profile, "rfm_segment", "") or "",
                            "is_returning": bool(getattr(full_profile, "is_returning", False)),
                            "total_orders": int(getattr(full_profile, "total_orders", 0) or 0),
                            "total_spend_sar": float(getattr(full_profile, "total_spend_sar", 0.0) or 0.0),
                            "last_order_at": (
                                full_profile.last_order_at.isoformat()
                                if getattr(full_profile, "last_order_at", None)
                                else None
                            ),
                        })
                except Exception:
                    pass

                brain = get_brain()
                logger.info(
                    "[BRAIN_IN] tenant=%s to=%s text=%r history_len=%d",
                    tenant_id, to, (text or "")[:80].replace("\n", " "),
                    len(history or []),
                )
                brain_result = await brain.process(
                    db=db,
                    tenant_id=tenant_id,
                    customer_phone=to,
                    message=text,
                    history=history,
                    profile=profile,
                    customer_id=profile.get("id"),
                    conversation_id=convo.id,
                )
                # process() returns dict {"reply": str, "buttons": list, "handoff": bool}
                if isinstance(brain_result, dict):
                    # billing_access_denied → skipped=True, reply=None — no outbound send
                    _billing_denied = (
                        brain_result.get("skipped")
                        and brain_result.get("reason") == "billing_access_denied"
                    )
                    if _billing_denied:
                        logger.info(
                            "[BRAIN] billing_access_denied — inbound recorded, outbound suppressed | tenant=%s",
                            tenant_id,
                        )
                        MERCHANT_BRAIN_ENABLED_FALLBACK = False
                        reply          = ""
                        _brain_buttons = []
                        _brain_handoff = False
                    else:
                        reply   = brain_result.get("reply", "") or ""
                        _brain_buttons = brain_result.get("buttons") or []
                        _brain_handoff = bool(brain_result.get("handoff"))
                else:
                    reply          = str(brain_result or "")
                    _brain_buttons = []
                    _brain_handoff = False

                # ── BRAIN_RESULT trace ───────────────────────────────────────
                # Pull the just-saved brain_state out of the conversation row
                # so the log line tells us what the brain decided AND what
                # state survived the turn (selected product, pending options,
                # missing fields). Defensive — never let log-formatting break
                # the actual response path.
                _br_action = ""
                _br_stage  = ""
                _br_focus  = ""
                _br_missing: list = []
                _br_options_pending: list = []
                _br_unsync = False
                try:
                    _bs = ((convo.extra_metadata or {}).get("brain_state") or {})
                    _br_action = str(_bs.get("last_action") or "")
                    _br_stage  = str(_bs.get("stage") or "")
                    _focus = _bs.get("current_product_focus") or {}
                    _br_focus = (
                        f"name={_focus.get('title')!r} "
                        f"salla_id={_focus.get('external_id')} "
                        f"nahla_id={_focus.get('id')}"
                    )
                    _prep = _bs.get("order_prep") or {}
                    _br_missing = list(_prep.get("missing_fields") or [])
                    _br_unsync = bool(_prep.get("product_unsyncable"))
                    # Detect option groups still missing a value pick.
                    _meta = _prep.get("product_options_meta") or []
                    _picked = _prep.get("product_options") or {}
                    for _g in _meta:
                        if not _g.get("values"):
                            continue
                        _name = (_g.get("name") or "").strip()
                        if _name and _name.lower() not in {k.lower() for k in _picked.keys()}:
                            _br_options_pending.append(_name)
                except Exception:
                    pass

                logger.info(
                    "[BRAIN_RESULT] tenant=%s to=%s action=%s stage=%s "
                    "focus=(%s) missing_fields=%s options_pending=%s "
                    "product_unsyncable=%s reply_len=%d buttons=%d handoff=%s",
                    tenant_id, to,
                    _br_action or "?", _br_stage or "?", _br_focus,
                    _br_missing, _br_options_pending, _br_unsync,
                    len(reply or ""), len(_brain_buttons), _brain_handoff,
                )

                if _brain_handoff:
                    # Guard: if there is active order preparation (a focused
                    # product or saved order_state), DO NOT auto-pin the
                    # conversation to human. Brain would only emit handoff
                    # when the customer explicitly asked for a human, and
                    # in that case we still want them to be able to resume
                    # the order on the next turn just by sending order data.
                    _has_active_order = False
                    try:
                        meta = (convo.extra_metadata or {}) if convo is not None else {}
                        bstate = (meta or {}).get("brain_state") or {}
                        prep = (bstate or {}).get("order_prep") or {}
                        if prep.get("product_id") or prep.get("product_name"):
                            _has_active_order = True
                        if bstate.get("current_product_focus"):
                            _has_active_order = True
                    except Exception:
                        _has_active_order = False

                    try:
                        from handoff.manager import create_handoff_session  # noqa: PLC0415
                        _cust_name = profile.get("name") or to
                        create_handoff_session(
                            db, tenant_id, to, _cust_name, text,
                            reason="customer_request",
                        )
                        if _has_active_order:
                            logger.info(
                                "[ORDER FLOW] continuing order despite handoff request | "
                                "skipping convo handoff flag | tenant=%s to=%s",
                                tenant_id, to,
                            )
                        else:
                            convo.status = "human"
                            convo.is_human_handoff = True
                            db.flush()
                            logger.info(
                                "[Merchant/Brain] handoff session created for tenant=%s to=%s",
                                tenant_id, to,
                            )
                            # Pause AI so subsequent inbounds don't keep
                            # producing brain handoff replies. Mirrors the
                            # support-escalation branch's behaviour.
                            try:
                                from core.ai_pause_guard import (  # noqa: PLC0415
                                    pause_ai as _pause_ai,
                                    REASON_HUMAN_HANDOFF as _R_HOFF,
                                )
                                _pause_ai(db, convo, reason=_R_HOFF, by="brain:handoff")
                            except Exception as _ph_exc:
                                logger.debug("[ai_pause] brain handoff pause failed: %s", _ph_exc)
                    except Exception as ho_exc:
                        logger.error("[Merchant/Brain] failed to create handoff session: %s", ho_exc)

                logger.info("[Merchant/Brain] replied tenant=%s to=%s buttons=%d handoff=%s",
                            tenant_id, to, len(_brain_buttons), _brain_handoff)
            except Exception as brain_exc:
                if MERCHANT_BRAIN_ALLOW_LEGACY_FALLBACK:
                    logger.error(
                        "[Merchant/Brain] Brain pipeline failed: %s — falling back to legacy "
                        "(MERCHANT_BRAIN_ALLOW_LEGACY_FALLBACK=true)",
                        brain_exc,
                    )
                    MERCHANT_BRAIN_ENABLED_FALLBACK = True
                else:
                    # Brain is the only sanctioned conversational path. We
                    # send a single safe canned reply and stop instead of
                    # routing the customer through the unprotected legacy
                    # LLM (no intent/dedup/handoff guards).
                    logger.error(
                        "[Merchant/Brain] Brain pipeline failed: %s — sending canned safe reply, "
                        "legacy fallback DISABLED (set MERCHANT_BRAIN_ALLOW_LEGACY_FALLBACK=true to re-enable)",
                        brain_exc,
                    )
                    _safe_reply = (
                        "وصلت رسالتك ✅ سيتم الرد عليك في أقرب وقت من فريق المتجر."
                    )
                    StateManager.save_message(
                        db, to, _safe_reply, "outbound",
                        conversation_id=convo.id, tenant_id=tenant_id,
                    )
                    try:
                        await _send_whatsapp_message(
                            phone_id=phone_id, to=to, text=_safe_reply,
                            _tenant_id=tenant_id, _db=db,
                        )
                    except Exception as _safe_exc:
                        logger.error(
                            "[Merchant/Brain] safe-reply send failed tenant=%s to=%s: %s",
                            tenant_id, to, _safe_exc,
                        )
                    logger.info(
                        "[OUTBOUND] tenant=%s to=%s source=brain_canned_safe trigger=inbound "
                        "intent=brain_failed handoff_triggered=false dedup_blocked=false reply_len=%d",
                        tenant_id, to, len(_safe_reply),
                    )
                    return
            else:
                MERCHANT_BRAIN_ENABLED_FALLBACK = False
        else:
            MERCHANT_BRAIN_ENABLED_FALLBACK = True

        # ── Legacy path (original generate_ai_reply) ──────────────────────────
        # Only entered when explicitly opted-in via
        # MERCHANT_BRAIN_ALLOW_LEGACY_FALLBACK=true (debug only) OR when
        # the Brain is disabled for this tenant. New tenants never see
        # legacy behaviour because Brain is global-default-true.
        if (not _brain_active or MERCHANT_BRAIN_ENABLED_FALLBACK) and not MERCHANT_BRAIN_ALLOW_LEGACY_FALLBACK and _brain_active:
            # Defensive: brain_active path SHOULD have returned above on
            # failure. If we land here with brain_active=true and
            # legacy_fallback=true the explicit opt-in is missing — log
            # loudly and bail with a safe canned reply.
            logger.critical(
                "[Merchant/LegacyGuard] would have entered legacy path but "
                "MERCHANT_BRAIN_ALLOW_LEGACY_FALLBACK=false — sending canned reply | "
                "tenant=%s to=%s",
                tenant_id, to,
            )
            return

        if not _brain_active or MERCHANT_BRAIN_ENABLED_FALLBACK:
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
            if not messages or messages[-1]["role"] != "user":
                messages.append({"role": "user", "content": text})
            elif messages[-1]["content"] != text:
                messages.append({"role": "user", "content": text})

            store_context_text = build_ai_context(db, tenant_id, customer_phone=to, product_query=text)

            from modules.ai.prompts.tenant_overlay import load_tenant_ai_overlay  # noqa: PLC0415
            from modules.ai.prompts.nahla_persona import nahla_persona_system_prompt  # noqa: PLC0415
            tenant_overlay = load_tenant_ai_overlay(db, tenant_id)
            mode_overlay = mode_prompt_overlay(mode_decision)

            # Resolve a friendly store display name (best-effort) so the
            # persona can introduce itself as «نحلة من <store>» instead
            # of the generic «نحلة من المتجر».
            _store_name = ""
            try:
                from models import Tenant  # noqa: PLC0415
                _t = db.query(Tenant).filter(Tenant.id == tenant_id).first()
                if _t:
                    for _attr in ("store_name", "name", "display_name"):
                        _val = getattr(_t, _attr, None)
                        if isinstance(_val, str) and _val.strip():
                            _store_name = _val.strip()
                            break
            except Exception:
                pass

            system_prompt = nahla_persona_system_prompt(
                store_name=_store_name,
                store_context_text=store_context_text,
            )

            # ── Anti-repeat + recent-message-priority overlay ─────────────
            # The legacy path has none of the structured intent / state /
            # dedup protections that the Brain provides, so without these
            # explicit instructions the LLM happily re-sends the
            # automation copy whenever the customer's reply is short or
            # unrelated. Keep the rules in plain Arabic — Claude follows
            # an Arabic system prompt better than a switched-language one.
            _CHAT_BEHAVIOR_OVERLAY = (
                "تعليمات سلوك المحادثة (مهمة جداً):\n"
                "- أهم رسالة هي آخر رسالة من العميل — رد عليها مباشرة.\n"
                "- لا تكرر رسائل التذكير أو الحملات الترويجية أو رسائل "
                "استرجاع السلة إذا كانت ظاهرة في سجل المحادثة.\n"
                "- إذا حيّاك العميل بـ«السلام عليكم» أو «هلا» أو ما يشبهها، "
                "رد بتحية قصيرة دافئة دون إعادة تقديم نفسك بالكامل.\n"
                "- إذا سألك «من أنت؟» عرّف نفسك باسم المساعد المحدد للمتجر "
                "بإيجاز، ثم اسأله عن حاجته.\n"
                "- لا تعد إرسال نفس الرسالة التي أرسلتها للعميل قبل قليل، "
                "حتى لو بصياغة مختلفة — تابع من حيث توقّفت المحادثة."
            )
            system_prompt = f"{system_prompt}\n\n{_CHAT_BEHAVIOR_OVERLAY}"

            if mode_overlay:
                system_prompt = f"{system_prompt}\n\n{mode_overlay}"
            if tenant_overlay:
                # Tenant overlay sits on top so merchant-specific tone /
                # rules can override the default Nahla persona.
                system_prompt = f"{system_prompt}\n\n{tenant_overlay}"

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

        # ── Outbound dedup guard ─────────────────────────────────────────
        # Last-mile safety net for BOTH paths (Brain + legacy). If the
        # generated reply overlaps too much with a recent outbound
        # message we substitute a short follow-up so the customer does
        # not see the same automation / cart-recovery copy twice in a
        # row. See `_is_repeat_reply` for the heuristic + threshold
        # rationale. Skipped for empty replies (already handled
        # upstream — billing_denied, etc.) and for the handoff path
        # (_brain_handoff replies are intentionally distinct).
        if reply and not _brain_handoff and _is_repeat_reply(reply, history):
            _orig_len = len(reply)
            reply = _short_followup_instead_of_repeat(history)
            logger.info(
                "[CHAT_DEDUP] tenant=%s to=%s replaced near-duplicate outbound "
                "(orig_len=%d new_len=%d brain=%s)",
                tenant_id, to, _orig_len, len(reply), _brain_active,
            )

        # ── Loop guard (similarity / repetition based) ────────────────────
        # Decides whether to:
        #   continue → send `reply` as-is
        #   recovery → swap `reply` for a one-shot recovery line
        #   pause    → escalate: pause AI + send the canned handoff notice
        # The guard NEVER pauses based on conversation length; it only
        # reacts when the assistant is repeating itself or the customer
        # side appears automated.
        _loop_replaced_with_recovery = False
        if reply and not _brain_handoff:
            try:
                from core.ai_pause_guard import (  # noqa: PLC0415
                    evaluate_loop_pre_send as _eval_loop,
                    note_recovery_sent as _note_recovery,
                    pause_ai as _loop_pause_ai,
                    REASON_BOT_LOOP as _R_LOOP,
                )
                _decision = _eval_loop(
                    db, convo,
                    tenant_id=tenant_id,
                    candidate_reply=reply,
                    inbound_text=text,
                )
                if _decision.action == "pause":
                    _loop_pause_ai(db, convo, reason=_R_LOOP, by="system:loop_pause")
                    # Send the same canned handoff notice the support
                    # escalation branch uses; the 30-min cooldown there
                    # prevents double-sends if multiple turns arrive.
                    _handoff_text = (
                        "أشوف إنه فيه شيء أحتاج فهمه أكثر — سأحوّل المحادثة "
                        "لفريق المتجر الآن وسيرد عليك أحد الموظفين قريباً 🌷"
                    )
                    StateManager.save_message(
                        db, to, _handoff_text, "outbound",
                        conversation_id=convo.id, tenant_id=tenant_id,
                    )
                    try:
                        await _send_whatsapp_message(
                            phone_id=phone_id, to=to, text=_handoff_text,
                            _tenant_id=tenant_id, _db=db,
                        )
                    except Exception as _send_exc:
                        logger.error(
                            "[Merchant] loop-pause handoff send failed tenant=%s to=%s: %s",
                            tenant_id, to, _send_exc,
                        )
                    logger.info(
                        "[OUTBOUND] tenant=%s to=%s source=loop_guard_pause trigger=inbound "
                        "intent=bot_loop_detected handoff_triggered=true dedup_blocked=true "
                        "loop_score=%d similarity=%.2f reply_len=%d",
                        tenant_id, to, _decision.score, _decision.similarity, len(_handoff_text),
                    )
                    return
                if _decision.action == "recovery" and _decision.recovery_text:
                    reply = _decision.recovery_text
                    _loop_replaced_with_recovery = True
                    _note_recovery(int(tenant_id), int(convo.id), recovery_text=reply)
                    logger.info(
                        "[OUTBOUND_PRE_SEND] tenant=%s to=%s replaced reply with recovery line "
                        "loop_score=%d similarity=%.2f",
                        tenant_id, to, _decision.score, _decision.similarity,
                    )
            except Exception as _loop_exc:
                logger.debug("[loop_guard] evaluate failed (open): %s", _loop_exc)

        # Save outbound reply after generation.
        StateManager.save_message(db, to, reply, "outbound", conversation_id=convo.id, tenant_id=tenant_id)

        latency_ms = 0
        try:
            ObservabilityLogger.log(db, TurnLog(
                phone=to,
                turn=max(int(getattr(state, "turn", 1) or 1), 1),
                raw_message=text,
                detected_intent="merchant_brain" if _brain_active else "merchant_store_ai",
                confidence=1.0,
                extracted_slots=[],
                stage_before="merchant",
                stage_after="merchant",
                stage_transition=None,
                decision="MERCHANT_BRAIN" if _brain_active else "GENERATE_AI_REPLY",
                decision_reason="merchant_whatsapp_inbound",
                ai_called=True,
                response_text=reply,
                latency_ms=latency_ms,
            ), tenant_id=tenant_id)
        except Exception:
            pass

        # ── AI media library attachments ───────────────────────────────
        # Strip [MEDIA:<id>] markers from the reply BEFORE we ship the
        # text. Each id resolves to a row in ai_media_library; matching
        # rows are dispatched as image/video/document/audio messages
        # AFTER the primary text/interactive reply has been sent. The
        # cleaned reply is what we send + persist (so the customer never
        # sees the marker, and the dashboard transcript matches what
        # they actually received on WhatsApp).
        _media_attachments: List[Dict[str, Any]] = []
        if reply:
            try:
                from core.ai_libraries import extract_media_markers as _extract_media  # noqa: PLC0415
                _cleaned_reply, _media_attachments = _extract_media(
                    db, tenant_id, reply, max_attachments=2,
                )
                if _media_attachments:
                    logger.info(
                        "[AIMedia.attach] tenant=%s conversation_id=%s "
                        "attachments=%d ids=%s",
                        tenant_id, getattr(convo, "id", None),
                        len(_media_attachments),
                        [a.get("id") for a in _media_attachments],
                    )
                    reply = _cleaned_reply
                elif "[MEDIA:" in reply.upper():
                    # Marker present but didn't resolve — strip it so the
                    # customer never sees the placeholder.
                    reply = _cleaned_reply
            except Exception as _media_exc:
                logger.warning(
                    "[AIMedia.attach] extract failed tenant=%s err=%s",
                    tenant_id, _media_exc,
                )

        if _brain_buttons and reply:
            _send_ok = await _send_interactive_reply(
                phone_id=phone_id, to=to,
                body_text=reply,
                buttons=_brain_buttons,
                _tenant_id=tenant_id, _db=db,
            )
        else:
            # ── URL → CTA-button normaliser ─────────────────────────
            # If the AI reply embeds a long product / payment / tracking
            # / location URL, lift it into a single ``cta_url`` button.
            # Brain replies that already attached quick-reply buttons
            # are skipped (handled above). General URLs (e.g. a wa.me
            # contact link inside marketing copy) stay inline.
            _cta_extraction = None
            try:
                from core.wa_link_buttons import extract_first_cta_url as _extract_cta  # noqa: PLC0415
                # We don't pass store_domain here: product detection by
                # path pattern (/products/, /p/, …) is enough for the
                # current AI-reply shapes. A future enhancement can plug
                # the merchant's known domain in for stricter matching.
                _cta_extraction = _extract_cta(reply or "")
            except Exception as _cta_exc:
                logger.debug("[CTA_BUTTON] extract failed tenant=%s: %s", tenant_id, _cta_exc)

            _send_ok = False
            if _cta_extraction and _cta_extraction.classification.kind != "general":
                _cls = _cta_extraction.classification
                logger.info(
                    "[CTA_BUTTON] tenant=%s conversation_id=%s url_type=%s "
                    "button_title=%r url_domain=%s body_len=%d",
                    tenant_id, getattr(convo, "id", None), _cls.kind,
                    _cls.button_title, _cls.domain, len(_cta_extraction.cleaned_text or ""),
                )
                try:
                    _send_ok = await _send_cta_url(
                        phone_id=phone_id, to=to,
                        body_text=_cta_extraction.cleaned_text or reply,
                        btn_label=_cls.button_title,
                        btn_url=_cls.url,
                        _tenant_id=tenant_id, _db=db,
                    )
                except Exception as _cta_send_exc:
                    logger.warning(
                        "[CTA_BUTTON_FALLBACK] tenant=%s reason=%s url_type=%s",
                        tenant_id, _cta_send_exc, _cls.kind,
                    )
                    _send_ok = False
                if _send_ok:
                    # Replace the persisted reply body with the cleaned
                    # version so the dashboard transcript matches what
                    # the customer saw on WhatsApp.
                    reply = _cta_extraction.cleaned_text or reply
                else:
                    # WhatsApp rejected the interactive (e.g. outside the
                    # 24h window): fall back to the original plain text
                    # send so the customer still receives the link.
                    logger.info(
                        "[CTA_BUTTON_FALLBACK] tenant=%s reason=interactive_send_failed "
                        "url_type=%s — sending plain text",
                        tenant_id, _cls.kind,
                    )
                    _send_ok = await _send_whatsapp_message(
                        phone_id=phone_id, to=to, text=reply,
                        _tenant_id=tenant_id, _db=db,
                    )
            else:
                _send_ok = await _send_whatsapp_message(
                    phone_id=phone_id, to=to, text=reply,
                    _tenant_id=tenant_id, _db=db,
                )
        if _send_ok:
            logger.info("[TRACE][5/6] MERCHANT_AI_SENT | tenant=%s to=%s", tenant_id, to)
            logger.info("[Merchant] replied tenant=%s to=%s", tenant_id, to)
            _outbound_source = (
                "loop_guard_recovery" if _loop_replaced_with_recovery
                else ("brain" if (_brain_active and not MERCHANT_BRAIN_ENABLED_FALLBACK) else "legacy")
            )
            logger.info(
                "[OUTBOUND] tenant=%s to=%s source=%s trigger=inbound "
                "intent=merchant_reply handoff_triggered=%s dedup_blocked=%s "
                "buttons=%d reply_len=%d",
                tenant_id, to, _outbound_source,
                str(bool(_brain_handoff)).lower(),
                str(_loop_replaced_with_recovery).lower(),
                len(_brain_buttons or []), len(reply or ""),
            )

            # Dispatch any media library attachments now that the text /
            # interactive reply has been delivered. We send them in order
            # so the customer sees the explanation first, then the file.
            #
            # Each attachment passes through the final-stage safety gate
            # (`validate_media_for_send`) before we call the WhatsApp
            # Cloud API: tenant scope, live `is_active` flag, supported
            # media type, HTTPS / on-disk presence, size cap, safe
            # filename. A failure here MUST log a warning and continue —
            # we never crash the conversation over a single bad attachment.
            try:
                from core.ai_libraries import (  # noqa: PLC0415
                    validate_media_for_send as _validate_media,
                )
            except Exception:  # noqa: BLE001
                _validate_media = None  # type: ignore[assignment]

            for _att in _media_attachments:
                if _validate_media is not None:
                    _ok, _why, _normed = _validate_media(
                        _att, expected_tenant_id=tenant_id, db=db,
                    )
                    if not _ok:
                        logger.warning(
                            "[AIMedia.validate] tenant=%s id=%s SKIPPED reason=%s",
                            tenant_id, _att.get("id"), _why,
                        )
                        continue
                    _att = _normed or _att

                _media_type_norm = (_att.get("media_type") or "image").lower()
                _filename = _att.get("filename")
                if _filename is None and _media_type_norm in ("document", "pdf"):
                    _filename = _att.get("title") or "document"
                try:
                    _media_ok = await _send_media_message(
                        phone_id=phone_id,
                        to=to,
                        media_type=_media_type_norm,
                        media_url=_att.get("file_url") or "",
                        filename=_filename,
                        _tenant_id=tenant_id,
                        _db=db,
                    )
                    logger.info(
                        "[AIMedia.send] tenant=%s to=%s id=%s type=%s ok=%s",
                        tenant_id, to, _att.get("id"),
                        _media_type_norm, _media_ok,
                    )
                except Exception as _media_send_exc:
                    logger.warning(
                        "[AIMedia.send] tenant=%s id=%s failed: %s",
                        tenant_id, _att.get("id"), _media_send_exc,
                    )
            # Track this reply for similarity-based loop scoring on the
            # next turn. Never auto-pauses on counts alone.
            try:
                from core.ai_pause_guard import after_ai_reply as _after_ai_reply  # noqa: PLC0415
                _after_ai_reply(db, convo, tenant_id=tenant_id, reply_text=reply)
            except Exception as _rate_exc:
                logger.debug("[ai_pause] post-reply tracker failed: %s", _rate_exc)
        else:
            logger.error(
                "[TRACE][5/6] MERCHANT_AI_SEND_FAILED | tenant=%s to=%s reply_len=%s",
                tenant_id, to, len(reply or ""),
            )

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
        _fallback_text = "وصلت رسالتك ✅ سيتم الرد عليك في أقرب وقت."
        try:
            await _send_whatsapp_message(
                phone_id=phone_id, to=to,
                text=_fallback_text,
                _tenant_id=tenant_id, _db=db,
            )
            try:
                from routers.conversations import record_outbound_message  # noqa: PLC0415
                record_outbound_message(
                    db, tenant_id, to, _fallback_text,
                    event_type="ai_fallback",
                    extra={"is_ai": True},
                )
            except Exception:
                pass
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
) -> bool:
    owns_db = False
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
            owns_db = _db is not None
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

    try:
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
            return False
        if not check_rate_limit(rate_key, max_count=20, window_seconds=60):
            logger.warning(
                "[WA] throttled minute send | tenant_id=%s to=%s phone_number_id=%s",
                _tenant_id, recipient, phone_id,
            )
            return False

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
                            return "error" not in (retry_data or {})
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
                return False
            return True
        except Exception as exc:
            logger.error("[WA] post error: %s", exc)
            return False
    finally:
        if owns_db and _db is not None:
            try:
                _db.close()
            except Exception:
                pass


async def _send_whatsapp_message(
    phone_id: str, to: str, text: str,
    _tenant_id: Optional[int] = None, _store_name: str = "unknown", _db=None,
) -> bool:
    return await _post_wa(phone_id, {
        "messaging_product": "whatsapp", "to": to, "type": "text",
        "text": {"body": text},
    }, _tenant_id=_tenant_id, _store_name=_store_name, _db=_db)


async def _send_interactive_reply(
    phone_id: str, to: str, body_text: str, buttons: list,
    _tenant_id: Optional[int] = None, _db=None,
) -> bool:
    return await _post_wa(phone_id, {
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
) -> bool:
    return await _post_wa(phone_id, {
        "messaging_product": "whatsapp", "to": to, "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {"text": body_text},
            "action": {"name": "cta_url", "parameters": {"display_text": btn_label, "url": btn_url}},
        },
    }, _tenant_id=_tenant_id, _db=_db)


# Map merchant-library media_type → WhatsApp Cloud API outer "type" key.
# "pdf" is a UX-only label; on the wire it's a document.
_WA_MEDIA_OUTER_TYPE = {
    "image": "image",
    "video": "video",
    "audio": "audio",
    "document": "document",
    "pdf": "document",
}


async def _send_media_message(
    phone_id: str,
    to: str,
    media_type: str,
    media_url: str,
    *,
    caption: Optional[str] = None,
    filename: Optional[str] = None,
    _tenant_id: Optional[int] = None,
    _db=None,
) -> bool:
    """Send an image / video / audio / document message via WhatsApp Cloud.

    ``media_type`` is the merchant-library label (image, video, pdf,
    document, audio); ``media_url`` MUST be publicly reachable by Meta.
    Caption is silently ignored for audio (unsupported by the API).
    """
    outer = _WA_MEDIA_OUTER_TYPE.get((media_type or "").strip().lower())
    if not outer:
        logger.warning("[WA] _send_media_message unsupported type=%s", media_type)
        return False
    media_block: Dict[str, Any] = {"link": media_url}
    if caption and outer in ("image", "video", "document"):
        media_block["caption"] = caption[:1024]
    if outer == "document" and filename:
        media_block["filename"] = filename[:255]
    return await _post_wa(
        phone_id,
        {
            "messaging_product": "whatsapp",
            "to": to,
            "type": outer,
            outer: media_block,
        },
        _tenant_id=_tenant_id,
        _db=_db,
    )


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
    owns_db = False
    if db is None:
        db = next(get_db(), None)
        owns_db = db is not None
    try:
        state = StateManager.load(db, phone=to, tenant_id=tenant_id) if db else None

        # ── Dynamic abandoned-cart recovery buttons ──────────────────────
        # These ids carry the cart/coupon/stage context inline, so they
        # bypass the fixed-id ladder below and route through their own
        # dispatcher. We do this first because the prefix check is a
        # cheap startswith and we want recovery taps to be acknowledged
        # before any state machine mutation.
        try:
            from services.cart_recovery_actions import (  # noqa: PLC0415
                handle_cart_recovery_button, is_cart_recovery_button,
            )
            if is_cart_recovery_button(btn_id) and db is not None:
                handled = await handle_cart_recovery_button(
                    db=db,
                    button_id=btn_id,
                    phone_id=phone_id,
                    to_phone=to,
                    tenant_id=tenant_id,
                    send_cta_url=_send_cta_url,
                    send_text=_send_whatsapp_message,
                    send_buttons=_send_interactive_reply,
                )
                if handled:
                    if db and state:
                        try:
                            StateManager.save_message(
                                db, to, f"[button:{btn_id}]", "inbound",
                            )
                            StateManager.save(db, state)
                        except Exception:
                            pass
                    return
        except Exception:
            logger.exception(
                "[Buttons] Cart-recovery dispatcher failed id=%s tenant=%s",
                btn_id, tenant_id,
            )

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
    finally:
        if owns_db and db:
            try:
                db.close()
            except Exception:
                pass
