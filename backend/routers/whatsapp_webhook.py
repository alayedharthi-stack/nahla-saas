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


# Replacement set after the May 2026 #2 merchant feedback ("no loops,
# no clown tone"). The dedup fallback runs ONLY when the brain
# produces a near-duplicate of its previous outbound, and the
# customer would otherwise see the same line twice. We keep the lines
# short, calm, no laughter, no "أرشح لك عسل؟" funnel-opener. Rotated
# by outbound-turn count so the customer doesn't see the same line
# back-to-back either.
_DEDUP_FALLBACK_REPLIES = [
    "تأمر بشي ثاني؟",
    "إذا في شي ثاني تحتاجه أنا معك.",
    "خبرني لو احتجت شي ثاني.",
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
    """Process the four WhatsApp delivery-status webhook events from
    Meta / 360dialog (``sent``, ``delivered``, ``read``, ``failed``)
    and persist them against the matching ``CampaignSendLog`` row.

    Two-layer attribution
    ─────────────────────
    A status event identifies its target by ``wamid`` (a.k.a.
    ``provider_message_id``). We look it up in two places:

      1. ``CampaignSendLog`` keyed by ``provider_message_id`` —
         this is where the campaign dispatcher writes the wamid
         when Meta accepts a template send. It's the authoritative
         row for delivery analytics.
      2. ``MessageEvent`` keyed by ``extra_metadata.wa_message_id``
         (legacy) — kept so that pre-migration rows and one-off
         non-campaign sends (e.g. ``/conversations/reply``) still
         update the aggregate ``Campaign.*_count`` counters.

    Both paths run for every event so we never miss aggregation
    when a row exists in only one of them. Each is independently
    idempotent (timestamp set only if currently NULL, dict-key
    guard on extra_metadata).

    Statuses we persist
    ───────────────────
    ``sent``       — wamid existed already (we set sent_at at dispatch
                     time), so this is informational. We do NOT update
                     anything but we still log it for observability.
    ``delivered``  → CampaignSendLog.delivered_at = now()
                  → Campaign.delivered_count++
    ``read``       → CampaignSendLog.read_at = now()
                  → Campaign.read_count++ (+ delivered_count++ if not
                    already set, since "read" implies "delivered")
    ``failed``     → CampaignSendLog.failed_at = now() — categorised
                     as "failed_after_accept" by the debug endpoint
                     because the send-log row is in ``status='sent'``
                     (a wamid was issued before the failure).
    """
    wamid = status.get("id", "")
    st = (status.get("status") or "").lower()
    if not wamid or st not in ("sent", "delivered", "read", "failed"):
        return

    # `sent` is just an echo of the synchronous send response — we
    # already wrote the wamid at dispatch time. Log and skip the
    # DB round-trip so we don't churn the connection pool with no-ops.
    if st == "sent":
        logger.info("[StatusWebhook] sent echo wamid=%s (no-op)", wamid[:20])
        return

    db = next(get_db(), None)
    if not db:
        return

    now = datetime.utcnow()

    try:
        # ── Layer 1: per-recipient send log (preferred path) ────────
        from models import CampaignSendLog, Campaign  # noqa: PLC0415
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

        log_row = (
            db.query(CampaignSendLog)
            .filter(CampaignSendLog.provider_message_id == wamid)
            .first()
        )
        # ── Delivery Quality Intelligence Layer (May 2026) ──
        # Best-effort append-only event capture. NEVER let this fail
        # the existing dispatcher flow — wrapped in try/except, runs
        # against the same `db` session so commit/rollback below
        # naturally cleans up if needed.
        try:
            from services.delivery_quality import record_status_event  # noqa: PLC0415
            record_status_event(
                db=db,
                tenant_id=(log_row.tenant_id if log_row else None),
                wamid=wamid,
                status=st,
                phone_e164=(log_row.phone_e164 if log_row else None),
                errors_payload=status.get("errors"),
                campaign_send_log_id=(log_row.id if log_row else None),
                source="meta",
            )
        except Exception as exc:
            logger.debug("[StatusWebhook] quality recorder failed: %s", exc)
        log_touched = False
        log_campaign_id: Optional[int] = None
        if log_row:
            log_campaign_id = log_row.campaign_id
            if st == "delivered" and log_row.delivered_at is None:
                log_row.delivered_at = now
                log_touched = True
            elif st == "read":
                if log_row.read_at is None:
                    log_row.read_at = now
                    log_touched = True
                # Reading implies delivered — backfill if Meta didn't
                # send the delivered event (some webhooks coalesce).
                if log_row.delivered_at is None:
                    log_row.delivered_at = now
                    log_touched = True
            elif st == "failed":
                if log_row.failed_at is None:
                    log_row.failed_at = now
                    log_touched = True
                # Stash provider error details, if Meta sent them, on
                # the existing error_code / error_message columns —
                # those started life as "synchronous send error"
                # but post-accept failure is still a wire-level error.
                errs = status.get("errors") or []
                if errs and isinstance(errs, list) and isinstance(errs[0], dict):
                    e0 = errs[0]
                    if not log_row.error_code and e0.get("code"):
                        log_row.error_code = str(e0.get("code"))
                        log_touched = True
                    if not log_row.error_message and e0.get("title"):
                        log_row.error_message = (
                            str(e0.get("title")) +
                            (f" — {e0.get('message')}" if e0.get("message") else "")
                        )
                        log_touched = True
        # ── Layer 2: aggregate counters on Campaign + idempotency on
        #            MessageEvent (legacy path; updates the dashboard
        #            tiles that read .delivered_count / .read_count) ─
        evt_row = (
            db.query(MessageEvent)
            .filter(MessageEvent.extra_metadata["wa_message_id"].astext == wamid)
            .first()
        )
        if evt_row:
            meta = dict(evt_row.extra_metadata or {})
            campaign_id = meta.get("campaign_id") or log_campaign_id
            already_key = f"_status_{st}"
            if campaign_id and not meta.get(already_key):
                campaign = (
                    db.query(Campaign)
                    .filter(Campaign.id == int(campaign_id))
                    .first()
                )
                if campaign:
                    if st == "delivered":
                        campaign.delivered_count = (campaign.delivered_count or 0) + 1
                    elif st == "read":
                        campaign.read_count = (campaign.read_count or 0) + 1
                        if not meta.get("_status_delivered"):
                            campaign.delivered_count = (
                                campaign.delivered_count or 0
                            ) + 1
                            meta["_status_delivered"] = True
                    # `failed` post-accept does NOT decrement sent_count
                    # — the original send was real; we just track the
                    # post-hoc failure on the send-log row for the
                    # delivery_summary breakdown.
                meta[already_key] = True
                evt_row.extra_metadata = meta
                flag_modified(evt_row, "extra_metadata")
        if log_touched or evt_row is not None:
            db.commit()
            logger.info(
                "[StatusWebhook] status=%s wamid=%s log_row=%s log_touched=%s "
                "evt_row=%s",
                st, wamid[:20], bool(log_row), log_touched, bool(evt_row),
            )
        else:
            logger.info(
                "[StatusWebhook] status=%s wamid=%s — no matching row "
                "(probably a non-campaign send)",
                st, wamid[:20],
            )
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
    # F19: defensive import — the observability ring buffer must
    # never break webhook routing if its module fails to import.
    try:
        from core.wa_webhook_observability import (  # noqa: PLC0415
            ROUTE_EXCEPTION,
            ROUTE_MATCHED,
            ROUTE_SCOPE_MISMATCH,
            ROUTE_UNROUTED_AMBIGUOUS,
            ROUTE_UNROUTED_BAD_SECRET,
            ROUTE_UNROUTED_MISSING_PHONE_ID,
            ROUTE_UNROUTED_UNKNOWN_PHONE_ID,
            ROUTE_UNROUTED_WRONG_PROVIDER,
            SECRET_MISMATCH,
            SECRET_NOT_REQUIRED,
            SECRET_OK,
            record_event as _record_webhook_event,
        )
    except Exception:
        _record_webhook_event = None  # type: ignore[assignment]
        # Define fallback constants so the rest of this function still
        # references valid names. The recorder calls become no-ops.
        ROUTE_EXCEPTION = "exception"
        ROUTE_MATCHED = "matched"
        ROUTE_SCOPE_MISMATCH = "scope_mismatch"
        ROUTE_UNROUTED_AMBIGUOUS = "unrouted_ambiguous"
        ROUTE_UNROUTED_BAD_SECRET = "unrouted_bad_secret"
        ROUTE_UNROUTED_MISSING_PHONE_ID = "unrouted_missing_phone_id"
        ROUTE_UNROUTED_UNKNOWN_PHONE_ID = "unrouted_unknown_phone_id"
        ROUTE_UNROUTED_WRONG_PROVIDER = "unrouted_wrong_provider"
        SECRET_MISMATCH = "mismatch"
        SECRET_NOT_REQUIRED = "not_required"
        SECRET_OK = "ok"

    def _safe_record(**kwargs: Any) -> None:
        """Wrap the recorder so a single bad payload can't take the
        routing path down. Never raises."""
        if _record_webhook_event is None:
            return
        try:
            _record_webhook_event(**kwargs)
        except Exception:
            pass

    db = SessionLocal()
    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {}) or {}
                field = str(change.get("field") or "")
                metadata = value.get("metadata", {}) or {}
                phone_number_id = metadata.get("phone_number_id", "")
                display_phone_number = metadata.get("display_phone_number", "")

                # Pre-compute counts so the recorder surfaces "an
                # inbound was in this delivery but it was dropped".
                msgs_count    = len(value.get("messages") or []) if isinstance(value.get("messages"), list) else 0
                statuses_cnt  = len(value.get("statuses") or []) if isinstance(value.get("statuses"), list) else 0
                echoes_cnt    = len(value.get("smb_message_echoes") or []) if isinstance(value.get("smb_message_echoes"), list) else 0

                if not phone_number_id:
                    logger.warning(
                        "[UNROUTED_D360_WEBHOOK] reason=missing_phone_id field=%s scope=%s "
                        "display_phone_number=%s msgs=%d statuses=%d echoes=%d",
                        field, scope, display_phone_number,
                        msgs_count, statuses_cnt, echoes_cnt,
                    )
                    # Preserve the historical log key so existing grep /
                    # alerting on [Webhook360] keeps firing.
                    logger.warning("[Webhook360] Missing phone_number_id field=%s scope=%s", field, scope)
                    logger.info(
                        "[WEBHOOK_IN] phone_number_id=- waba_id=- channel_id=- "
                        "tenant_resolved=- connection_id=- provider=dialog360 scope=%s "
                        "field=%s msgs=%d statuses=%d echoes=%d reason_if_unresolved=missing_phone_id",
                        scope, field, msgs_count, statuses_cnt, echoes_cnt,
                    )
                    _safe_record(
                        scope=scope, field=field,
                        phone_number_id_from_payload=phone_number_id or None,
                        display_phone_number=display_phone_number or None,
                        matched_tenant_id=None,
                        matched_connection_id=None,
                        matched_phone_number_id=None,
                        route_status=ROUTE_UNROUTED_MISSING_PHONE_ID,
                        messages_count=msgs_count,
                        statuses_count=statuses_cnt,
                        echoes_count=echoes_cnt,
                    )
                    continue
                wa_conns = (
                    db.query(WhatsAppConnection)
                    .filter_by(phone_number_id=phone_number_id)
                    .all()
                )
                if not wa_conns:
                    logger.warning(
                        "[UNROUTED_D360_WEBHOOK] reason=unknown_phone_id field=%s scope=%s "
                        "phone_number_id=%s display=%s msgs=%d — no WhatsAppConnection row "
                        "with this phone_number_id. Most common cause: merchant re-paired "
                        "the channel under a NEW phone_number_id but the WhatsAppConnection "
                        "row still stores the OLD one.",
                        field, scope, phone_number_id, display_phone_number, msgs_count,
                    )
                    logger.warning("[Webhook360] Unknown phone_number_id=%s field=%s scope=%s", phone_number_id, field, scope)
                    logger.info(
                        "[WEBHOOK_IN] phone_number_id=%s waba_id=- channel_id=- "
                        "tenant_resolved=- connection_id=- provider=dialog360 scope=%s "
                        "field=%s msgs=%d statuses=%d echoes=%d reason_if_unresolved=unknown_phone_id",
                        phone_number_id, scope, field, msgs_count, statuses_cnt, echoes_cnt,
                    )
                    _safe_record(
                        scope=scope, field=field,
                        phone_number_id_from_payload=phone_number_id,
                        display_phone_number=display_phone_number or None,
                        matched_tenant_id=None,
                        matched_connection_id=None,
                        matched_phone_number_id=None,
                        route_status=ROUTE_UNROUTED_UNKNOWN_PHONE_ID,
                        messages_count=msgs_count,
                        statuses_count=statuses_cnt,
                        echoes_count=echoes_cnt,
                    )
                    continue
                if len(wa_conns) > 1:
                    tenant_ids = [c.tenant_id for c in wa_conns]
                    connection_ids = [c.id for c in wa_conns]
                    logger.error(
                        "[UNROUTED_D360_WEBHOOK] reason=ambiguous phone_number_id=%s "
                        "tenants=%s connections=%s — dropped to prevent cross-tenant leak",
                        phone_number_id, tenant_ids, connection_ids,
                    )
                    logger.error(
                        "[Webhook360] Ambiguous phone_number_id=%s matches tenants=%s — "
                        "message dropped to prevent cross-tenant data leak",
                        phone_number_id, tenant_ids,
                    )
                    logger.info(
                        "[WEBHOOK_IN] phone_number_id=%s waba_id=- channel_id=- "
                        "tenant_resolved=ambiguous connection_id=- provider=dialog360 scope=%s "
                        "field=%s msgs=%d statuses=%d echoes=%d reason_if_unresolved=ambiguous "
                        "candidate_tenant_ids=%s candidate_connection_ids=%s",
                        phone_number_id, scope, field,
                        msgs_count, statuses_cnt, echoes_cnt,
                        tenant_ids, connection_ids,
                    )
                    _safe_record(
                        scope=scope, field=field,
                        phone_number_id_from_payload=phone_number_id,
                        display_phone_number=display_phone_number or None,
                        matched_tenant_id=None,
                        matched_connection_id=None,
                        matched_phone_number_id=None,
                        route_status=ROUTE_UNROUTED_AMBIGUOUS,
                        candidate_tenant_ids=tenant_ids,
                        candidate_connection_ids=connection_ids,
                        messages_count=msgs_count,
                        statuses_count=statuses_cnt,
                        echoes_count=echoes_cnt,
                    )
                    continue
                wa_conn = wa_conns[0]
                if wa_provider(wa_conn) != WHATSAPP_PROVIDER_360DIALOG:
                    logger.warning(
                        "[UNROUTED_D360_WEBHOOK] reason=wrong_provider phone_number_id=%s "
                        "tenant=%s connection=%s provider_on_row=%s",
                        phone_number_id, wa_conn.tenant_id, wa_conn.id,
                        wa_provider(wa_conn),
                    )
                    logger.warning("[Webhook360] phone_number_id=%s is not dialog360 provider", phone_number_id)
                    logger.info(
                        "[WEBHOOK_IN] phone_number_id=%s waba_id=%s channel_id=- "
                        "tenant_resolved=%s connection_id=%s provider=%s scope=%s "
                        "field=%s msgs=%d statuses=%d echoes=%d reason_if_unresolved=wrong_provider",
                        phone_number_id,
                        getattr(wa_conn, "whatsapp_business_account_id", None) or "-",
                        wa_conn.tenant_id, wa_conn.id, wa_provider(wa_conn),
                        scope, field, msgs_count, statuses_cnt, echoes_cnt,
                    )
                    _safe_record(
                        scope=scope, field=field,
                        phone_number_id_from_payload=phone_number_id,
                        display_phone_number=display_phone_number or None,
                        matched_tenant_id=wa_conn.tenant_id,
                        matched_connection_id=wa_conn.id,
                        matched_phone_number_id=getattr(wa_conn, "phone_number_id", None),
                        route_status=ROUTE_UNROUTED_WRONG_PROVIDER,
                        messages_count=msgs_count,
                        statuses_count=statuses_cnt,
                        echoes_count=echoes_cnt,
                    )
                    continue
                expected_secret = str((wa_conn.extra_metadata or {}).get("coexistence_internal_secret") or "")
                provided_secret = headers.get("x_nahla_coexistence_secret", "")
                if expected_secret and provided_secret != expected_secret:
                    logger.warning(
                        "[UNROUTED_D360_WEBHOOK] reason=bad_secret tenant=%s connection=%s "
                        "phone_number_id=%s — X-Nahla-Coexistence-Secret header did not "
                        "match. Most common cause: 360dialog dashboard was not updated "
                        "after the secret was rotated, OR the merchant connected via the "
                        "wrong endpoint URL.",
                        wa_conn.tenant_id, wa_conn.id, phone_number_id,
                    )
                    logger.warning("[Webhook360] Invalid internal secret tenant=%s", wa_conn.tenant_id)
                    logger.info(
                        "[WEBHOOK_IN] phone_number_id=%s waba_id=%s channel_id=- "
                        "tenant_resolved=%s connection_id=%s provider=%s scope=%s "
                        "field=%s msgs=%d statuses=%d echoes=%d reason_if_unresolved=bad_secret",
                        phone_number_id,
                        getattr(wa_conn, "whatsapp_business_account_id", None) or "-",
                        wa_conn.tenant_id, wa_conn.id, wa_provider(wa_conn),
                        scope, field, msgs_count, statuses_cnt, echoes_cnt,
                    )
                    _safe_record(
                        scope=scope, field=field,
                        phone_number_id_from_payload=phone_number_id,
                        display_phone_number=display_phone_number or None,
                        matched_tenant_id=wa_conn.tenant_id,
                        matched_connection_id=wa_conn.id,
                        matched_phone_number_id=getattr(wa_conn, "phone_number_id", None),
                        route_status=ROUTE_UNROUTED_BAD_SECRET,
                        secret_check=SECRET_MISMATCH,
                        messages_count=msgs_count,
                        statuses_count=statuses_cnt,
                        echoes_count=echoes_cnt,
                    )
                    return

                # Reaching here means the connection routing succeeded.
                # Surface both the payload's phone_number_id AND the
                # connection's stored one so a drift between them is
                # obvious in the logs.
                stored_phone_id = getattr(wa_conn, "phone_number_id", None)
                logger.info(
                    "[ROUTED_D360_WEBHOOK] tenant=%s connection=%s field=%s scope=%s "
                    "phone_number_id_from_payload=%s phone_number_id_from_connection=%s "
                    "phone_id_match=%s msgs=%d statuses=%d echoes=%d",
                    wa_conn.tenant_id, wa_conn.id, field, scope,
                    phone_number_id, stored_phone_id,
                    str(phone_number_id) == str(stored_phone_id),
                    msgs_count, statuses_cnt, echoes_cnt,
                )

                # ── Canonical inbound log line ──────────────────────────
                # Single grep-friendly line that captures every dimension
                # support needs when a merchant says "I sent a real WhatsApp
                # message but it didn't arrive in Nahla". Pairs with
                # [D360_WEBHOOK_VERIFY] so a verify-vs-receive flow can be
                # reconstructed from log scrape.
                try:
                    _coex_meta = wa_conn.extra_metadata or {}
                    _pd        = (_coex_meta.get("provider_details") or {}) if isinstance(_coex_meta, dict) else {}
                    _channel_id_local = _pd.get("channel_id") or _pd.get("channel") or "-"
                except Exception:
                    _channel_id_local = "-"
                logger.info(
                    "[WEBHOOK_IN] phone_number_id=%s waba_id=%s channel_id=%s "
                    "tenant_resolved=%s connection_id=%s provider=%s scope=%s "
                    "field=%s msgs=%d statuses=%d echoes=%d reason_if_unresolved=-",
                    phone_number_id,
                    getattr(wa_conn, "whatsapp_business_account_id", None) or "-",
                    _channel_id_local,
                    wa_conn.tenant_id,
                    wa_conn.id,
                    wa_provider(wa_conn),
                    scope,
                    field,
                    msgs_count, statuses_cnt, echoes_cnt,
                )

                family = _classify_360dialog_field(field)

                # Always record per-family receipt — even when the field does
                # not belong to this endpoint's scope. The dashboard surfaces
                # the timestamps so the operator can confirm "the coexistence
                # webhook is alive even if the channel webhook went silent",
                # and vice-versa.
                _stamp_webhook_received(db, wa_conn, family)

                # Opportunistic status auto-heal — throttled to ~5 min/conn.
                # Fires only when the row's status field has drifted out of
                # sync with reality (e.g. left over `action_required` while
                # inbound is clearly flowing). Cheap no-op on healthy rows.
                _maybe_autoheal_status(wa_conn)

                if not _scope_accepts(scope, family):
                    logger.info(
                        "[Webhook360] field=%s family=%s arrived on scope=%s — recorded but not processed",
                        field, family, scope,
                    )
                    _safe_record(
                        scope=scope, field=field,
                        phone_number_id_from_payload=phone_number_id,
                        display_phone_number=display_phone_number or None,
                        matched_tenant_id=wa_conn.tenant_id,
                        matched_connection_id=wa_conn.id,
                        matched_phone_number_id=stored_phone_id,
                        route_status=ROUTE_SCOPE_MISMATCH,
                        secret_check=SECRET_OK if expected_secret else SECRET_NOT_REQUIRED,
                        messages_count=msgs_count,
                        statuses_count=statuses_cnt,
                        echoes_count=echoes_cnt,
                    )
                    continue

                _safe_record(
                    scope=scope, field=field,
                    phone_number_id_from_payload=phone_number_id,
                    display_phone_number=display_phone_number or None,
                    matched_tenant_id=wa_conn.tenant_id,
                    matched_connection_id=wa_conn.id,
                    matched_phone_number_id=stored_phone_id,
                    route_status=ROUTE_MATCHED,
                    secret_check=SECRET_OK if expected_secret else SECRET_NOT_REQUIRED,
                    messages_count=msgs_count,
                    statuses_count=statuses_cnt,
                    echoes_count=echoes_cnt,
                )

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
        _safe_record(
            scope=scope, field="batch",
            phone_number_id_from_payload=None,
            display_phone_number=None,
            matched_tenant_id=None,
            matched_connection_id=None,
            matched_phone_number_id=None,
            route_status=ROUTE_EXCEPTION,
            error_text=f"{type(exc).__name__}: {exc}",
        )
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


# ── Inbound auto-heal: bring stale status back to "connected" ───────────────
# Pattern: every N seconds per connection (default 300 s), if a real inbound
# webhook arrived while ``conn.status`` is still in an in-flight bucket
# (action_required, pending, request_submitted, ...), promote it to
# "connected" so the owner panel and merchant page agree with reality.
# Hard-fail statuses (disconnected/error) are NEVER touched — the merchant
# explicitly broke that connection and must re-onboard.
#
# This is THE fix for "tenant 52 syndrome": webhooks flow, AI replies,
# everything works, but the owner panel still flashes red because some
# old bootstrap step left ``status="action_required"`` in the row.

_RECONCILE_THROTTLE_SEC = 300.0  # at most one reconcile attempt per 5 min per conn
_LAST_RECONCILE_AT: dict[int, float] = {}


def _should_reconcile_now(conn_id: int) -> bool:
    last = _LAST_RECONCILE_AT.get(conn_id)
    if last is None:
        return True
    return (time.monotonic() - last) >= _RECONCILE_THROTTLE_SEC


def _mark_reconcile_attempted(conn_id: int) -> None:
    _LAST_RECONCILE_AT[conn_id] = time.monotonic()


def _bg_reconcile_status(conn_id: int, tenant_id: int | None) -> None:
    """Worker body — runs on the same ``wa-stamp`` thread pool as the
    receipt stamper. Owns its own session so no row lock here can
    contaminate the webhook's main transaction.

    Calls the canonical ``_reconcile_coexistence_status`` helper from
    ``routers/whatsapp_connect`` so the auto-heal rules stay in ONE place.
    """
    bg_db = SessionLocal()
    try:
        from routers.whatsapp_connect import _reconcile_coexistence_status  # noqa: PLC0415
        from database.models import WhatsAppConnection as _WC          # noqa: PLC0415

        bg_db.execute(text("SET LOCAL statement_timeout = :ms"), {"ms": 2000})
        conn = bg_db.query(_WC).filter(_WC.id == conn_id).first()
        if not conn:
            return
        changed = _reconcile_coexistence_status(
            conn, tenant_id=tenant_id or conn.tenant_id, source="inbound_autoheal", db=bg_db,
        )
        if changed:
            logger.info(
                "[Webhook360/reconcile_bg] tenant=%s conn=%s status auto-healed → connected",
                tenant_id or conn.tenant_id, conn_id,
            )
    except Exception as exc:
        try:
            bg_db.rollback()
        except Exception:
            pass
        logger.warning(
            "[Webhook360/reconcile_bg] SKIPPED conn=%s tenant=%s err=%s",
            conn_id, tenant_id, exc,
        )
    finally:
        try:
            bg_db.close()
        except Exception:
            pass


def _maybe_autoheal_status(wa_conn: WhatsAppConnection) -> None:
    """Schedule a fire-and-forget status reconcile when the row's status
    has drifted out of sync with reality. Throttled to once per 5 minutes
    per connection — calling this on every webhook is safe and cheap.
    """
    try:
        status = (getattr(wa_conn, "status", "") or "").lower()
        # Skip the work entirely on healthy/hard-fail rows — neither
        # needs healing.
        if status in {"connected", "disconnected", "error", "not_connected"}:
            return
        if not _should_reconcile_now(int(wa_conn.id)):
            return
        _mark_reconcile_attempted(int(wa_conn.id))
        submit_stamp_background(
            _bg_reconcile_status,
            int(wa_conn.id),
            getattr(wa_conn, "tenant_id", None),
        )
    except Exception as exc:
        logger.debug("[Webhook360/reconcile_bg] schedule failed conn=%s err=%s",
                     getattr(wa_conn, "id", None), exc)


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

            # ── Delivery Quality: auto-reinstate suppression on inbound ──
            # If this phone was previously auto-suppressed (e.g. after
            # repeated not_on_whatsapp failures, or a single
            # blocked_by_user signal), an inbound message is the
            # clearest possible "yes I want to hear from you" signal.
            # Flip the row to ``is_active=False`` so the next dispatch
            # cycle includes the customer again. Never blocks the
            # message flow on a quality-layer hiccup.
            try:
                from services.delivery_quality import reinstate_on_inbound  # noqa: PLC0415
                reinstate_on_inbound(
                    db=db,
                    tenant_id=resolved_tenant_id,
                    normalized_phone=normalized_sender,
                    reason="inbound_message",
                )
            except Exception as _reinstate_exc:
                logger.debug(
                    "[delivery_quality] reinstate on inbound failed: %s",
                    _reinstate_exc,
                )

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

        # ── Order-flow context for the media normalizer ───────────────
        # Load the latest brain_state for this conversation so the
        # PDF/image classifier can boost a generic "document_*.pdf"
        # to ``pdf_kind=payment_receipt`` when the bot just asked
        # for a transfer receipt. Without this hint, Saudi-bank
        # receipts with timestamp-only filenames look like garbage.
        # Read is best-effort: if anything fails (missing
        # conversation row, deserialisation error) we pass an empty
        # context and the classifier falls back to filename/caption
        # signals only.
        _order_context: Dict[str, Any] = {}
        try:
            from core.order_flow import build_order_context  # noqa: PLC0415
            _order_context = build_order_context(
                db=db,
                tenant_id=resolved_tenant_id,
                phone=sender,
            ) or {}
        except Exception as _oc_exc:  # noqa: BLE001
            logger.debug(
                "[ORDER_FLOW_STATE] build_order_context failed "
                "tenant=%s phone=%s err=%s",
                resolved_tenant_id, sender, _oc_exc,
            )

        normalized_inbound = await normalize_whatsapp_inbound(
            db=db,
            wa_conn=wa_conn,
            tenant_id=resolved_tenant_id,
            message=msg,
            order_context=_order_context,
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

        if normalized_inbound.normalized_type not in {"text", "audio", "image", "document"}:
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
        # ── Media-without-text fallback ─────────────────────────────
        # The normalizer detected an audio/image but couldn't extract
        # any usable text (Whisper failed, vision failed, missing
        # caption, etc.) AND there's a canonical Arabic fallback
        # message it wants us to send. We MUST NOT call the brain in
        # that case — we'd spend tokens generating a generic apology
        # while losing the structured metadata that explains why. The
        # fallback reply is short, kind, and asks the customer to
        # retype — exactly the spec's required behaviour.
        if (
            not text
            and normalized_inbound.fallback_reply_ar
            and normalized_inbound.normalized_type in {"audio", "image", "document"}
            and not _is_platform_tenant(db, resolved_tenant_id)
        ):
            logger.info(
                "[MediaFallback] tenant=%s sender=%s normalized_type=%s "
                "no_text → sending fallback reply",
                resolved_tenant_id, sender,
                normalized_inbound.normalized_type,
            )
            await _handle_media_fallback(
                phone_id=used_pid, to=sender,
                tenant_id=resolved_tenant_id, db=db,
                fallback_reply=normalized_inbound.fallback_reply_ar,
                inbound_metadata=normalized_inbound.metadata,
                wa_message_ts=_wa_msg_ts,
                wa_msg_id=msg_id or None,
            )
            return

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
            # ── Payment-receipt short-circuit ─────────────────────────
            # Before calling the brain, check if this inbound is a
            # payment receipt arriving during an active order. If
            # so, bypass the brain entirely with a deterministic
            # acknowledgement that surfaces the product + price +
            # national address from state. This is the fix for the
            # critical bug where AI re-asked product discovery after
            # the customer sent the bank-transfer PDF.
            try:
                from core.order_flow import (  # noqa: PLC0415
                    maybe_handle_receipt_inbound,
                    apply_state_patch,
                )
                _receipt_decision = maybe_handle_receipt_inbound(
                    db=db,
                    tenant_id=resolved_tenant_id,
                    phone=sender,
                    inbound_normalized_type=normalized_inbound.normalized_type,
                    inbound_metadata=normalized_inbound.metadata or {},
                )
            except Exception as _r_exc:  # noqa: BLE001
                logger.warning(
                    "[ORDER_FLOW_STATE] receipt short-circuit check "
                    "failed (non-fatal) tenant=%s phone=%s err=%s",
                    resolved_tenant_id, sender, _r_exc,
                )
                _receipt_decision = None

            # ── Text-only payment-claim short-circuit (May 2026) ──────
            # When the customer SAYS they paid without attaching a
            # receipt ("تم التحويل" / "حولت" / "دفعت"), the brain
            # used to ship a generic "أنا هنا — قول وش تحتاج" line
            # because two-token Arabic inbounds rarely hit any
            # high-confidence intent. We handle the claim
            # deterministically here so the customer always sees a
            # payment-aware acknowledgement that asks for the proof.
            #
            # The brain is bypassed only when:
            #   * inbound is a recognised payment-confirmation phrase,
            #   * NO media is attached (real receipts go through the
            #     ``maybe_handle_receipt_inbound`` branch above),
            #   * the conversation has an active order / awaiting-
            #     receipt / under-review state.
            # On any failure we silently fall through to the brain.
            _payment_claim_decision = None
            if _receipt_decision is None:
                try:
                    from core.payment_intent import (  # noqa: PLC0415
                        maybe_handle_payment_claim,
                    )
                    _payment_claim_decision = maybe_handle_payment_claim(
                        db=db,
                        tenant_id=resolved_tenant_id,
                        phone=sender,
                        inbound_text=text or "",
                        has_attached_media=(
                            normalized_inbound.normalized_type
                            in ("document", "image", "audio")
                        ),
                    )
                except Exception as _pc_exc:  # noqa: BLE001
                    logger.warning(
                        "[PAYMENT_INTENT] short-circuit check failed "
                        "(non-fatal) tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _pc_exc,
                    )
                    _payment_claim_decision = None

            if _payment_claim_decision is not None:
                logger.info(
                    "[ORDER_FLOW_STATE] short_circuit=payment_claim "
                    "tenant=%s phone=*%s next_action=send_ack",
                    resolved_tenant_id,
                    sender[-4:] if sender else "",
                )
                # Persist the state patch first so subsequent inbounds
                # see the awaiting-receipt flag even if the send fails.
                try:
                    apply_state_patch(
                        db,
                        tenant_id=resolved_tenant_id,
                        phone=sender,
                        state_patch=_payment_claim_decision["state_patch"],
                    )
                except Exception as _pp_exc:  # noqa: BLE001
                    logger.warning(
                        "[ORDER_FLOW_STATE] payment_claim state_patch "
                        "apply failed tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _pp_exc,
                    )
                # Persist the inbound + outbound + send via _post_wa
                # so dedup, status stamping, and admin metrics work
                # exactly the same way as the brain path.
                try:
                    StateManager.save_message(
                        db,
                        phone=sender,
                        direction="inbound",
                        body=text or "[payment_claim]",
                        event_type="whatsapp_message",
                        tenant_id=resolved_tenant_id,
                        extra_metadata={
                            "wa_message_id": msg_id or None,
                            "payment_claim_short_circuit": True,
                        },
                    )
                except Exception as _sc_inb_exc:  # noqa: BLE001
                    logger.warning(
                        "[ORDER_FLOW_STATE] payment_claim inbound save "
                        "failed tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _sc_inb_exc,
                    )
                try:
                    StateManager.save_message(
                        db,
                        phone=sender,
                        direction="outbound",
                        body=_payment_claim_decision["reply_text"],
                        event_type="whatsapp_message",
                        tenant_id=resolved_tenant_id,
                        extra_metadata={
                            "is_ai": True,
                            "deterministic_path": "payment_claim_ack",
                        },
                    )
                except Exception as _sc_out_exc:  # noqa: BLE001
                    logger.warning(
                        "[ORDER_FLOW_STATE] payment_claim outbound save "
                        "failed tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _sc_out_exc,
                    )
                await _post_wa(
                    used_pid,
                    {
                        "messaging_product": "whatsapp",
                        "to": sender,
                        "type": "text",
                        "text": {
                            "body": _payment_claim_decision["reply_text"],
                        },
                    },
                    _tenant_id=resolved_tenant_id,
                    _db=db,
                )
                return

            if _receipt_decision is not None:
                logger.info(
                    "[ORDER_FLOW_STATE] short_circuit=receipt_received "
                    "tenant=%s phone=*%s next_action=send_ack",
                    resolved_tenant_id,
                    sender[-4:] if sender else "",
                )
                # 1) Persist the brain_state mutation FIRST so even if
                #    the WhatsApp POST fails the next inbound still
                #    sees ``payment_receipt_received=True`` and our
                #    own context-aware fallback can answer correctly.
                try:
                    apply_state_patch(
                        db,
                        tenant_id=resolved_tenant_id,
                        phone=sender,
                        state_patch=_receipt_decision["state_patch"],
                    )
                except Exception as _patch_exc:  # noqa: BLE001
                    logger.warning(
                        "[ORDER_FLOW_STATE] state_patch apply failed "
                        "tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _patch_exc,
                    )

                # 2) Persist the customer's inbound MessageEvent so the
                #    drawer shows the PDF / image alongside our ACK.
                try:
                    StateManager.save_message(
                        db,
                        phone=sender,
                        direction="inbound",
                        body=text or "[إيصال تحويل]",
                        event_type=(
                            "whatsapp_document"
                            if normalized_inbound.normalized_type == "document"
                            else "whatsapp_image"
                        ),
                        tenant_id=resolved_tenant_id,
                        extra_metadata={
                            "normalized_inbound": normalized_inbound.metadata,
                            "wa_message_id": msg_id or None,
                            "payment_receipt_short_circuit": True,
                        },
                    )
                except Exception as _save_exc:  # noqa: BLE001
                    logger.warning(
                        "[ORDER_FLOW_STATE] inbound save failed "
                        "tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _save_exc,
                    )

                # 3) Send the deterministic ACK. We use the same
                #    ``_post_wa`` path as the brain so dedup, status
                #    stamping, and wamid surfacing all work. Persist
                #    the outbound row with ``is_ai=True`` so it
                #    surfaces in admin AI metrics.
                try:
                    StateManager.save_message(
                        db,
                        phone=sender,
                        direction="outbound",
                        body=_receipt_decision["reply_text"],
                        event_type="whatsapp_message",
                        tenant_id=resolved_tenant_id,
                        extra_metadata={
                            "is_ai": True,
                            "deterministic_path": "payment_receipt_ack",
                            "order_summary": _receipt_decision.get("summary"),
                        },
                    )
                except Exception as _save_out_exc:  # noqa: BLE001
                    logger.warning(
                        "[ORDER_FLOW_STATE] outbound save failed "
                        "tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _save_out_exc,
                    )
                await _post_wa(
                    used_pid,
                    {
                        "messaging_product": "whatsapp",
                        "to": sender,
                        "type": "text",
                        "text": {
                            "body": _receipt_decision["reply_text"],
                        },
                    },
                    _tenant_id=resolved_tenant_id,
                    _db=db,
                )
                return
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


async def _handle_media_fallback(
    *,
    phone_id: str,
    to: str,
    tenant_id: int,
    db,
    fallback_reply: str,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    wa_message_ts: Optional[datetime] = None,
    wa_msg_id: Optional[str] = None,
) -> None:
    """Handle the "media without usable text" branch.

    Called by the dispatcher when the normalizer returns
    ``should_process=False`` for an audio / image message but
    populated ``fallback_reply_ar`` — meaning we successfully
    received the media (and ideally persisted it) but couldn't
    extract any text to feed the brain.

    Contract:

      1. Create / fetch the dashboard conversation so the merchant
         sees the voice note in the inbox even though no AI reply
         was generated.
      2. Persist an INBOUND ``MessageEvent`` with the full
         ``normalized_inbound`` metadata (storage_url,
         transcript_status, ai_used_audio=False, etc.) so
         ``/conversations/{id}/media-debug`` can replay it.
      3. Send the canonical Arabic fallback reply
         (``AUDIO_FALLBACK_REPLY_AR`` / ``IMAGE_FALLBACK_REPLY_AR``)
         and persist that as an OUTBOUND ``MessageEvent`` too.
      4. NEVER call the brain. Spending tokens on "I didn't hear
         you" is wasteful and the canned line is friendlier.

    Errors are logged but never re-raised — the webhook ack loop
    must complete regardless of bookkeeping failures here.
    """
    from routers.conversations import _get_or_create_conversation  # noqa: PLC0415

    inbound_meta_body = "[رسالة وسائط بدون نص قابل للقراءة]"
    convo = None
    try:
        convo = _get_or_create_conversation(db, tenant_id, to)
        # Stamp the inbound row first so /media-debug picks it up
        # even if the outbound send fails (e.g. token expired).
        StateManager.save_message(
            db, to, inbound_meta_body, "inbound",
            conversation_id=convo.id,
            tenant_id=tenant_id,
            extra_metadata={
                "normalized_inbound": dict(inbound_metadata or {}),
                "media_fallback":     True,
                "wa_message_id":      wa_msg_id or "",
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[MediaFallback] failed to persist inbound row tenant=%s "
            "to=%s err=%s",
            tenant_id, to, exc,
        )

    try:
        sent = await _send_whatsapp_message(
            phone_id=phone_id, to=to, text=fallback_reply,
            _tenant_id=tenant_id, _db=db,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[MediaFallback] failed to send fallback reply tenant=%s "
            "to=%s err=%s",
            tenant_id, to, exc,
        )
        sent = False

    if sent and convo is not None:
        try:
            StateManager.save_message(
                db, to, fallback_reply, "outbound",
                conversation_id=convo.id,
                tenant_id=tenant_id,
                extra_metadata={"media_fallback": True},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[MediaFallback] failed to persist outbound row tenant=%s "
                "to=%s err=%s",
                tenant_id, to, exc,
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

    # ── Customer self-introduction capture (May 2026) ────────────────────────
    # If the inbound text is an UNAMBIGUOUS self-intro ("اسمي محمد",
    # "أنا دخيل الله", "معك فهد", "my name is …") and the customer row
    # is either nameless OR was cleared by the merchant via the inline
    # pencil (manual_name_cleared=true), adopt the volunteered name as
    # the canonical ``Customer.name``. Side-effects are bounded:
    #   * NEVER overwrites a non-empty merchant-curated name.
    #   * NEVER fires on incidental name mentions inside a longer
    #     sentence — only on the conservative anchors in
    #     ``core.customer_name_extractor``.
    #   * Failure is logged and ignored — the rest of the inbound
    #     pipeline runs as before.
    try:
        from core.customer_name_extractor import (  # noqa: PLC0415
            extract_high_confidence_name,
        )
        from services.customer_intelligence import (  # noqa: PLC0415
            CustomerIntelligenceService as _NameCIS,
        )

        _name_hit = extract_high_confidence_name(text)
        if _name_hit:
            try:
                _name_svc = _NameCIS(db, tenant_id)
                _name_cust = _name_svc.upsert_customer_identity(
                    phone=to,
                    name=_name_hit.value,
                    source="ai_detected_name",
                )
                # If the row was previously CLEARED by the merchant
                # we now flip ``manual_name_cleared`` back to false
                # because we successfully refilled it. The override
                # flag stays true so future low-trust sources (CSV
                # imports, WhatsApp profile syncs) still cannot touch
                # this name.
                if _name_cust is not None:
                    _meta = dict(_name_cust.extra_metadata or {})
                    if _meta.get("manual_name_cleared"):
                        _meta["manual_name_cleared"] = False
                        from datetime import timezone as _tz_name  # noqa: PLC0415
                        _meta["manual_name_refilled_by_ai_at"] = (
                            datetime.now(_tz_name.utc).isoformat()
                        )
                        _name_cust.extra_metadata = _meta
                        db.add(_name_cust)
                db.flush()
                logger.info(
                    "[NAME_EXTRACTOR] adopted | tenant=%s phone=%s "
                    "pattern=%s name=%r",
                    tenant_id, to, _name_hit.pattern, _name_hit.value,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[NAME_EXTRACTOR] adopt failed | tenant=%s phone=%s err=%s",
                    tenant_id, to, exc,
                )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[NAME_EXTRACTOR] skipped (init err): %s", exc)

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

        # ── PAYMENT-ASSET EARLY BYPASS ───────────────────────────────────
        #
        # This block runs BEFORE the AI pause guard and BEFORE the mode
        # resolver. The merchant reported (with screenshots) that asking
        # "ارسل لي حساب الراجحي" produced a generic handoff message even
        # though the bank-transfer barcode was active in the AI Media
        # Library. Root cause analysis:
        #
        #   1. The conversation had been previously flagged for human
        #      handoff (via support_escalation or ai_pause_guard), which
        #      causes ``should_skip_ai`` to return True and the webhook
        #      to ``return`` without ever reaching the brain (the
        #      "AI stopped" symptom the merchant noticed).
        #   2. Even when the pause guard let the message through, the
        #      mode resolver returned MODE_SUPPORT_ESCALATION and the
        #      hard-coded handoff acknowledgement fired ("وصلت رسالتك.
        #      تم تحويل المحادثة لفريق المتجر…") before the brain ran.
        #
        # The merchant's explicit requirement was: "إذا وجد AI Asset
        # مناسب، يجب أن يتغلب على TEMPLATE fallbacks، contact_owner
        # fallback، generic escalation". So if the inbound is a
        # payment-info request AND we have a high-relevance active
        # media asset, we deterministically:
        #
        #   * send a warm short text + the asset image, then
        #   * stamp a "payment_asset_served" marker on the conversation,
        #   * skip every other branch and return.
        #
        # We deliberately do NOT clear the human-handoff flags here —
        # if the merchant manually took over the conversation, they
        # keep ownership for everything else. The only escape we make
        # is for THIS specific kind of question, which the merchant
        # explicitly authorised by uploading the asset in the first
        # place.
        try:
            from core.ai_libraries import (  # noqa: PLC0415
                find_best_payment_asset as _find_payment_asset,
                is_payment_query as _is_payment_query,
                validate_media_for_send as _validate_media,
            )
            _early_payment_intent = _is_payment_query(text or "")
            _early_payment_asset = (
                _find_payment_asset(db, tenant_id, text or "")
                if _early_payment_intent else None
            )
            logger.info(
                "[PAYMENT_INFO] early-gate tenant=%s convo=%s to=%s "
                "intent_detected=%s asset_found=%s asset_id=%s "
                "asset_score=%s",
                tenant_id, getattr(convo, "id", None), to,
                _early_payment_intent,
                bool(_early_payment_asset),
                (_early_payment_asset or {}).get("id"),
                f"{(_early_payment_asset or {}).get('_relevance_score') or 0:.2f}"
                if _early_payment_asset else None,
            )
            if _early_payment_intent and _early_payment_asset:
                # Validate the asset (HTTPS upgrade, tenant scope, file
                # presence, mime check, size cap) before we touch
                # WhatsApp. On any validation failure we fall through
                # to the normal pipeline so the customer still gets a
                # response.
                _ok, _err, _normalised = _validate_media(
                    _early_payment_asset,
                    expected_tenant_id=tenant_id,
                    db=db,
                )
                if _ok and _normalised:
                    _intro_text = "أكيد 🌷 تفضل، هذه بيانات التحويل البنكي."
                    # 1) warm text reply
                    _text_ok = await _send_whatsapp_message(
                        phone_id=phone_id, to=to, text=_intro_text,
                        _tenant_id=tenant_id, _db=db,
                    )
                    StateManager.save_message(
                        db, to, _intro_text, "outbound",
                        conversation_id=convo.id, tenant_id=tenant_id,
                    )
                    # 2) the media itself
                    _media_ok = await _send_media_message(
                        phone_id=phone_id, to=to,
                        media_type=_normalised.get("media_type") or "image",
                        media_url=_normalised.get("file_url") or "",
                        caption=None,
                        filename=_normalised.get("filename"),
                        _tenant_id=tenant_id, _db=db,
                    )
                    logger.info(
                        "[PAYMENT_INFO] early-bypass APPLIED tenant=%s convo=%s "
                        "asset_id=%s media_type=%s text_send_ok=%s "
                        "media_send_ok=%s url_scheme=%s "
                        "transfer_fallback_skipped=true hard_override=true",
                        tenant_id, getattr(convo, "id", None),
                        _normalised.get("id"),
                        _normalised.get("media_type"),
                        _text_ok, _media_ok,
                        (_normalised.get("file_url") or "").split(":", 1)[0],
                    )
                    # Stamp the conversation so the brain knows we just
                    # served the payment asset (used by future dedup +
                    # by analytics).
                    try:
                        _meta = dict(getattr(convo, "extra_metadata", None) or {})
                        from datetime import datetime as _dtn, timezone as _tzn  # noqa: PLC0415
                        _meta["last_payment_asset_served_at"] = _dtn.now(_tzn.utc).isoformat()
                        _meta["last_payment_asset_id"] = int(_normalised.get("id") or 0)
                        convo.extra_metadata = _meta
                        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
                        flag_modified(convo, "extra_metadata")
                        db.add(convo)
                        db.commit()
                    except Exception as _stamp_exc:
                        logger.debug(
                            "[PAYMENT_INFO] stamp failed: %s — non-fatal",
                            _stamp_exc,
                        )
                        try:
                            db.rollback()
                        except Exception:
                            pass
                    return  # short-circuit — never run the brain for this turn
                else:
                    logger.warning(
                        "[PAYMENT_INFO] early-bypass SKIPPED tenant=%s convo=%s "
                        "asset_id=%s reason=validation_failed err=%s — "
                        "falling through to normal pipeline",
                        tenant_id, getattr(convo, "id", None),
                        _early_payment_asset.get("id"), _err,
                    )
        except Exception as _early_exc:  # noqa: BLE001
            logger.warning(
                "[PAYMENT_INFO] early-bypass FAILED tenant=%s err=%s — "
                "falling through to normal pipeline",
                tenant_id, _early_exc,
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

                # ── No-silent-reply guard ─────────────────────────────────
                # Production regression (May 2026): "السلام عليكم أبي سعر
                # العسل" arrived from a real merchant and got NO outbound
                # at all — brain produced an empty reply for reasons we
                # could not reconstruct from logs (greet locked +
                # downstream search returned an empty composer string).
                # Either the brain returns text OR we send a safe ack.
                # Silence is never acceptable inside the 24-hour window.
                #
                # We also emit a structured trace so the next regression is
                # debuggable. The trace is one log line per silent turn so
                # we can grep [BRAIN_SILENT_REPLY] in production logs.
                if not _billing_denied and not (reply or "").strip():
                    try:
                        _matched_intent = ""
                        _matched_action = ""
                        try:
                            from modules.ai.brain.intent.rules import (  # noqa: PLC0415
                                match as _rules_match,
                            )
                            _mi = _rules_match(text or "")
                            if _mi is not None:
                                _matched_intent = _mi.name
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            _bs_dbg = (
                                (convo.extra_metadata or {}).get("brain_state") or {}
                            )
                            _matched_action = str(_bs_dbg.get("last_action") or "")
                        except Exception:  # noqa: BLE001
                            pass
                        _wamid_dbg = ""
                        try:
                            _wamid_dbg = str(
                                ((value or {}).get("messages") or [{}])[0].get("id")
                                or ""
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        logger.error(
                            "[BRAIN_SILENT_REPLY] tenant=%s phone=*%s "
                            "inbound_text=%r matched_intent=%r "
                            "selected_action=%r final_reply_empty=true "
                            "webhook_event_id=%r",
                            tenant_id,
                            (to or "")[-4:],
                            (text or "")[:120],
                            _matched_intent,
                            _matched_action,
                            _wamid_dbg,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    # Substitute a polite, fact-free ack so the customer
                    # gets a reply within the 24h window even when the
                    # brain produced nothing. Deliberately non-committal
                    # so we don't promise a product/price/policy fact.
                    reply = (
                        "وصلت رسالتك ✅ خبرني وش تحتاج بالتفصيل وأقدر أساعدك."
                    )

                # ── Welcome-gate reply validation (May 2026) ─────────────
                # Production regression: "السلام عليكم أبي سعر العسل" was
                # producing a generic intro card ("أنا نحلة مستشارة... وش
                # تحب نبدأ فيه؟") even though the brain routed the message
                # to ASK_PRICE. The intro came from an upstream
                # MODE_IDENTITY_REPLY short-circuit in
                # ``conversation_mode.detect_identity_topic`` — already
                # fixed at the source by the welcome-gate-yield guard
                # there. This is the DEFENSIVE last-resort check: if for
                # any reason the customer asked an actionable question
                # and the final outbound is still a self-intro / generic
                # "وش تحب نبدأ" card, substitute a price-clarifying reply
                # so we don't ignore the question.
                try:
                    from modules.ai.routing.conversation_mode import (  # noqa: PLC0415
                        _message_has_actionable_after_greeting as _has_action,
                    )
                    _has_action_signal = bool(_has_action(text or ""))
                except Exception:  # noqa: BLE001
                    _has_action_signal = False

                # Detect intro-only / generic-funnel replies. Match the
                # exact phrasings used by ``render_identity_reply`` and
                # the legacy AI fallback. Keep this list narrow so we
                # never replace a legitimate sales-flow reply that just
                # happens to contain the assistant name.
                _INTRO_ONLY_MARKERS = (
                    "وش تحب نبدأ فيه",
                    "وش تحب أعرفك",
                    "وش تحب اعرفك",
                    "كيف أقدر أخدمك اليوم",
                    "كيف اقدر اخدمك اليوم",
                    "أهلاً فيك في",
                    "اهلا فيك في",
                )
                _is_intro_only = bool(
                    reply
                    and any(m in reply for m in _INTRO_ONLY_MARKERS)
                    and len(reply) <= 220
                )

                if _has_action_signal and _is_intro_only:
                    try:
                        logger.error(
                            "[WELCOME_GATE_INVALID_REPLY] tenant=%s phone=*%s "
                            "inbound_text=%r reply=%r reason=intro_only_after_actionable_ask",
                            tenant_id, (to or "")[-4:],
                            (text or "")[:120], (reply or "")[:160],
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    # Substitute a short, actionable clarifying reply
                    # that addresses the ask (price) while still
                    # acknowledging the salaam — exactly what the
                    # welcome-gate would produce. We deliberately don't
                    # invent a price; we ask which type of honey so the
                    # next turn can answer with the catalogue.
                    reply = (
                        "وعليكم السلام ورحمة الله 🌷\n"
                        "أكيد، عندنا عدة أنواع من العسل. "
                        "تحب أعطيك الأسعار حسب النوع (سدر / طلح / "
                        "ضهيان)؟"
                    )

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
                            # Set the FULL canonical human-takeover signal,
                            # not just the legacy `is_human_handoff` flag.
                            # The dashboard "طلب موظف" filter and the
                            # conversations list router both prefer the
                            # newer `needs_human` / `handoff_active`
                            # columns. Skipping them was leaving the
                            # inbox row without a reliable red pill in
                            # some merchant tenants (per production UX
                            # feedback).
                            convo.status = "human"
                            convo.is_human_handoff = True
                            convo.needs_human = True
                            convo.handoff_active = True
                            db.flush()
                            logger.info(
                                "[Merchant/Brain] handoff session created for tenant=%s to=%s "
                                "needs_human=True handoff_active=True",
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
                from core.store_display import clean_store_name  # noqa: PLC0415
                from models import Tenant  # noqa: PLC0415
                _t = db.query(Tenant).filter(Tenant.id == tenant_id).first()
                if _t:
                    for _attr in ("store_name", "name", "display_name"):
                        _val = getattr(_t, _attr, None)
                        if isinstance(_val, str) and _val.strip():
                            _store_name = clean_store_name(_val.strip())
                            break
            except Exception:
                pass

            system_prompt = nahla_persona_system_prompt(
                store_name=_store_name,
                store_context_text=store_context_text,
            )

            # ── Resolver-marker protocol overlay ──────────────────────────
            # Tell Claude about ``[PRODUCT:<name>]`` and
            # ``[MEDIA_KEY:<slug>]`` markers + the concrete list of
            # registry-keyed media this tenant has uploaded. The
            # overlay self-suppresses when neither the catalog nor
            # the keyed media library has content. See
            # ``core.ai_libraries.format_resolver_overlay_for_prompt``
            # for the contract.
            try:
                from services import media_resolver as _media_res  # noqa: PLC0415
                from services import media_key_registry as _media_reg  # noqa: PLC0415
                from core.ai_libraries import (  # noqa: PLC0415
                    format_resolver_overlay_for_prompt,
                )
                _keys_avail = _media_res.available_keys_for_tenant(db, tenant_id)
                _keys_block = _media_reg.format_keys_for_prompt(_keys_avail)
                # "catalog has products?" — cheap existence query.
                from models import Product as _Product  # noqa: PLC0415
                _has_catalog = (
                    db.query(_Product.id)
                      .filter(_Product.tenant_id == tenant_id)
                      .limit(1)
                      .first()
                    is not None
                )
                _resolver_overlay = format_resolver_overlay_for_prompt(
                    available_media_keys_block=_keys_block,
                    catalog_has_products=_has_catalog,
                )
                if _resolver_overlay:
                    system_prompt = f"{system_prompt}\n\n{_resolver_overlay}"
            except Exception as _ovr_exc:  # noqa: BLE001
                # Never let the overlay computation crash the reply
                # path — the AI can still answer in pure text.
                logger.warning(
                    "[Merchant] resolver overlay skipped: %s", _ovr_exc,
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
            _default_short = _short_followup_instead_of_repeat(history)
            # ── Context-aware fallback ────────────────────────────
            # When the conversation has an active order, the canned
            # "أنا هنا — قول وش تحتاج وأكمل معك" line lands MID-
            # FUNNEL and gives the merchant the impression that the
            # bot forgot the order. We swap in a sentence that
            # references the live order state (product / price /
            # awaiting receipt / under review) so the customer
            # never sees a stale generic prompt while a real funnel
            # is in flight.
            try:
                from core.order_flow import (  # noqa: PLC0415
                    context_aware_dedup_fallback,
                )
                reply = context_aware_dedup_fallback(
                    db,
                    tenant_id=tenant_id,
                    phone=to,
                    history=history,
                    default_fallback=_default_short,
                )
            except Exception as _ctx_exc:  # noqa: BLE001
                logger.debug(
                    "[CHAT_DEDUP] context-aware fallback failed: %s",
                    _ctx_exc,
                )
                reply = _default_short
            logger.info(
                "[CHAT_DEDUP] tenant=%s to=%s replaced near-duplicate outbound "
                "(orig_len=%d new_len=%d brain=%s)",
                tenant_id, to, _orig_len, len(reply), _brain_active,
            )

        # ── Payment-context safety net (May 2026) ─────────────────────
        # Last defence against the screenshot bug: even if the brain
        # somehow shipped a generic "أنا هنا — قول وش تحتاج" line
        # after the customer said "تم التحويل" / "حولت" / "دفعت",
        # rewrite that line to a payment-aware acknowledgement. This
        # also catches replies that bypassed the dedup guard (e.g.
        # first-ever message of a session is structurally identical
        # to the fallback). The rewriter is a no-op unless BOTH the
        # inbound is a payment-confirmation claim AND the outbound
        # matches a known generic fallback marker — so legitimate
        # replies never get touched.
        if reply and not _brain_handoff:
            try:
                from core.payment_intent import (  # noqa: PLC0415
                    rewrite_generic_reply_for_payment_context,
                )
                from core.order_flow import (  # noqa: PLC0415
                    _focus_summary as _pi_focus,
                    _load_brain_state as _pi_load,
                )
                _pi_conv, _pi_bs = _pi_load(db, tenant_id=tenant_id, phone=to)
                _pi_summary = _pi_focus(_pi_bs)
                _rewritten = rewrite_generic_reply_for_payment_context(
                    inbound_text=text or "",
                    brain_reply=reply,
                    state_summary=_pi_summary,
                )
                if _rewritten:
                    logger.info(
                        "[PAYMENT_INTENT] rewrote_generic_fallback "
                        "tenant=%s to=%s orig_len=%d new_len=%d",
                        tenant_id, to, len(reply), len(_rewritten),
                    )
                    reply = _rewritten
            except Exception as _pi_exc:  # noqa: BLE001
                logger.debug(
                    "[PAYMENT_INTENT] post-brain rewrite failed: %s",
                    _pi_exc,
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

        # Phase 4 — Response Compression Layer.
        # Run rule-based post-processing on the LLM reply BEFORE any
        # marker extraction. The compression module freezes markers
        # and URLs internally, so it never breaks [PRODUCT:...] /
        # [MEDIA:<id>] / [MEDIA_KEY:<slug>] tokens. Adaptive mode is
        # gated on the inbound customer message — when the customer
        # asks for a detailed explanation we soften the paragraph
        # cap. Compression failures are non-fatal: we log and fall
        # back to the original reply.
        if reply:
            try:
                from modules.ai.postprocess.compression import (  # noqa: PLC0415
                    compress_for_whatsapp as _compress_reply,
                )
                _comp = _compress_reply(reply, customer_message=text or "")
                if not _comp.skipped and _comp.any_change:
                    reply = _comp.text
                try:
                    import json as _json_comp  # noqa: PLC0415
                    _comp_payload: Dict[str, object] = {
                        "event":                    "compression",
                        "tenant_id":                tenant_id,
                        "conversation_id":          getattr(convo, "id", None),
                        "applied":                  bool(
                            _comp.any_change and not _comp.skipped
                        ),
                        "skipped":                  _comp.skipped,
                        "skip_reason":              _comp.skip_reason or None,
                        "adaptive_mode":            _comp.adaptive_mode,
                        "chars_before":             _comp.chars_before,
                        "chars_after":              _comp.chars_after,
                        "lines_before":             _comp.lines_before,
                        "lines_after":              _comp.lines_after,
                        "paragraphs_before":        _comp.paragraphs_before,
                        "paragraphs_after":         _comp.paragraphs_after,
                        "paragraphs_dropped":       _comp.paragraphs_dropped,
                        "fillers_removed":          _comp.fillers_removed,
                        "cold_disclaimers_removed": _comp.cold_disclaimers_removed,
                        "stock_phrase_dedups":      _comp.stock_phrase_dedups,
                        "greetings_deduped":        _comp.greetings_deduped,
                        "emojis_removed":           _comp.emojis_removed,
                        "blank_lines_collapsed":    _comp.blank_lines_collapsed,
                        "markers_preserved":        _comp.markers_preserved,
                        "urls_preserved":           _comp.urls_preserved,
                        "duration_ms":              _comp.duration_ms,
                    }
                    logger.info(
                        "[COMPRESSION] "
                        + _json_comp.dumps(_comp_payload, ensure_ascii=False)
                    )
                except Exception:  # noqa: BLE001 — logging must never break the turn
                    pass
            except Exception as _comp_exc:  # noqa: BLE001
                logger.warning(
                    "[COMPRESSION] skipped tenant=%s err=%s",
                    tenant_id, _comp_exc,
                )

        # Phase 3 — Marker resolution metrics.
        # Capture the LLM's reply BEFORE any marker extraction strips
        # tokens out of it. We pre-scan it for the three marker families
        # and emit a single ``[MARKER_RESOLUTION]`` structured log at
        # the end of the marker pipeline (just before payment override).
        # ``detected`` counts what the LLM emitted, ``resolved`` counts
        # what the resolvers successfully replaced, ``failed`` counts the
        # rest (malformed, unknown id/slug, no matching product, etc.).
        _reply_for_marker_counts = reply or ""
        _marker_detected: Dict[str, int] = {"product": 0, "media_id": 0, "media_key": 0, "call": 0}
        _marker_resolved: Dict[str, int] = {"product": 0, "media_id": 0, "media_key": 0, "call": 0}
        _marker_failed:   Dict[str, int] = {"product": 0, "media_id": 0, "media_key": 0, "call": 0}
        try:
            from core.ai_libraries import _MEDIA_MARKER_RE as _MID_RE  # noqa: PLC0415
            from services.product_resolver import _PRODUCT_MARKER_RE as _PROD_RE  # noqa: PLC0415
            from services.media_resolver import _MEDIA_KEY_MARKER_RE as _MKEY_RE  # noqa: PLC0415
            from services.call_resolver import _CALL_MARKER_RE as _CALL_RE  # noqa: PLC0415
            _marker_detected["media_id"] = len(_MID_RE.findall(_reply_for_marker_counts))
            _marker_detected["media_key"] = len(_MKEY_RE.findall(_reply_for_marker_counts))
            _marker_detected["product"]   = len(_PROD_RE.findall(_reply_for_marker_counts))
            _marker_detected["call"]      = len(_CALL_RE.findall(_reply_for_marker_counts))
        except Exception:  # noqa: BLE001 — counting must never break the turn
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
                _marker_resolved["media_id"] = len(_media_attachments)
                _marker_failed["media_id"] = max(
                    0, _marker_detected["media_id"] - _marker_resolved["media_id"]
                )
            except Exception as _media_exc:
                logger.warning(
                    "[AIMedia.attach] extract failed tenant=%s err=%s",
                    tenant_id, _media_exc,
                )
                _marker_failed["media_id"] = _marker_detected["media_id"]

        # ── [MEDIA_KEY:<slug>] markers ─────────────────────────────
        # New resolver path (Phase 5). Same shape of extraction as
        # ``[MEDIA:<id>]`` above but the LLM uses stable namespaced
        # keys (e.g. ``payment_rajhi_barcode``). The resolver
        # gracefully skips keys for which the merchant hasn't
        # uploaded an asset — those land in ``_missing_media_keys``
        # so the caller can decide whether to append the
        # registry's text fallback to the reply.
        _missing_media_keys: List[str] = []
        if reply and "[MEDIA_KEY:" in reply.upper():
            try:
                from services.media_resolver import (  # noqa: PLC0415
                    extract_media_key_markers as _extract_media_keys,
                )
                _cleaned_reply2, _key_attachments, _missing_media_keys = (
                    _extract_media_keys(
                        db, tenant_id, reply, max_attachments=2,
                    )
                )
                if _key_attachments:
                    _media_attachments.extend(_key_attachments)
                    logger.info(
                        "[AIMedia.attach] tenant=%s key_attachments=%d keys=%s",
                        tenant_id, len(_key_attachments),
                        [a.get("media_key") for a in _key_attachments],
                    )
                _marker_resolved["media_key"] = len(_key_attachments)
                _marker_failed["media_key"]   = len(_missing_media_keys)
                if _cleaned_reply2 != reply:
                    reply = _cleaned_reply2
                if _missing_media_keys:
                    # Append registry fallback text for missing keys
                    # so the conversation doesn't go silent. We
                    # honour at most the FIRST missing key — chaining
                    # multiple fallback blurbs would be noisy.
                    try:
                        from services.media_key_registry import (  # noqa: PLC0415
                            get as _reg_get,
                        )
                        mk = _reg_get(_missing_media_keys[0])
                        if mk and mk.fallback_text:
                            reply = (
                                f"{reply}\n\n{mk.fallback_text}"
                                if reply.strip() else mk.fallback_text
                            )
                            logger.info(
                                "[AIMedia.attach] tenant=%s key=%s "
                                "asset_missing fallback_text_appended",
                                tenant_id, _missing_media_keys[0],
                            )
                    except Exception:
                        pass
            except Exception as _mk_exc:  # noqa: BLE001
                logger.warning(
                    "[AIMedia.attach] MEDIA_KEY extract failed tenant=%s err=%s",
                    tenant_id, _mk_exc,
                )

        # ── [PRODUCT:<query>] markers ──────────────────────────────
        # Resolve LLM-cited products against the synced catalog and
        # collect them as product-card attachments. The actual send
        # (image + caption + cta_url button) lives further down in
        # the outbound dispatch loop.
        _product_attachments: List[Dict[str, Any]] = []
        if reply and "[PRODUCT:" in reply.upper():
            try:
                from services.product_resolver import (  # noqa: PLC0415
                    extract_product_markers as _extract_products,
                    format_product_card_caption as _product_caption,
                )
                # Try to learn the customer id so affinity ranking
                # can fire. The conversation may not yet be persisted
                # in the very first turn — that's fine, we just skip
                # the boost.
                _cust_id_for_aff = None
                try:
                    _cust_id_for_aff = getattr(convo, "customer_id", None) or None
                except Exception:
                    pass

                _cleaned_reply3, _resolutions, _missing_products = (
                    _extract_products(
                        db, tenant_id, reply,
                        customer_id=_cust_id_for_aff,
                        max_attachments=3,
                    )
                )
                if _cleaned_reply3 != reply:
                    reply = _cleaned_reply3
                for _res in _resolutions:
                    # We re-shape into the SAME dict shape the
                    # webhook's media-attachment loop already
                    # understands (so the down-stream sender stays
                    # uniform). The "kind" key tags this as a
                    # product card so the sender knows to attach a
                    # CTA-URL button to the buy link instead of
                    # treating it like a static media file.
                    _product_attachments.append({
                        "kind":         "product_card",
                        "id":           _res.id,
                        "title":        _res.title,
                        "media_type":   "image",
                        "file_url":     _res.image_url,
                        "caption":      _product_caption(_res),
                        "product_url":  _res.product_url,
                        "price":        _res.price,
                        "in_stock":     _res.in_stock,
                        "external_id":  _res.external_id,
                        "confidence":   _res.confidence,
                    })
                if _resolutions:
                    logger.info(
                        "[ProductResolver] tenant=%s resolved=%d "
                        "missing=%d ids=%s",
                        tenant_id, len(_resolutions),
                        len(_missing_products),
                        [r.id for r in _resolutions],
                    )
                if _missing_products and not _resolutions:
                    logger.info(
                        "[ProductResolver] tenant=%s ALL product markers "
                        "unresolved queries=%s — letting text-only reply pass",
                        tenant_id, _missing_products[:3],
                    )
                _marker_resolved["product"] = len(_resolutions)
                _marker_failed["product"]   = len(_missing_products)
            except Exception as _p_exc:  # noqa: BLE001
                logger.warning(
                    "[ProductResolver] extract failed tenant=%s err=%s",
                    tenant_id, _p_exc,
                )
                _marker_failed["product"] = _marker_detected["product"]

        # ── Staff call markers ([CALL:<phone>|<label>]) ────────────────
        # Resolves to a WhatsApp ``contacts`` message dispatched AFTER
        # the main reply. Payment-phone numbers stay as plain text
        # (the LLM is taught to NEVER use [CALL:...] for transfer
        # accounts). Feature-flagged so an unhealthy production
        # signal can be killed in seconds without a rollback.
        _call_targets: List[Any] = []
        if reply and "[CALL:" in reply.upper() and _staff_call_marker_enabled():
            try:
                from services.call_resolver import (  # noqa: PLC0415
                    extract_call_markers as _extract_calls,
                )
                _cleaned_reply_call, _call_targets = _extract_calls(reply)
                if _cleaned_reply_call != reply:
                    reply = _cleaned_reply_call
                _marker_resolved["call"] = len(_call_targets)
                _marker_failed["call"] = max(
                    0, _marker_detected["call"] - _marker_resolved["call"]
                )
                if _call_targets:
                    logger.info(
                        "[CallResolver] tenant=%s conversation_id=%s "
                        "resolved=%d targets=%s",
                        tenant_id, getattr(convo, "id", None),
                        len(_call_targets),
                        [(c.name, c.phone_display) for c in _call_targets],
                    )
            except Exception as _call_exc:  # noqa: BLE001
                logger.warning(
                    "[CallResolver] extract failed tenant=%s err=%s",
                    tenant_id, _call_exc,
                )
                _marker_failed["call"] = _marker_detected["call"]

        # ── Post-LLM Safety Nets (Phase 6) ─────────────────────────────
        # Deterministic backstops for when Claude FORGETS to emit a
        # marker even though the customer clearly asked for the asset
        # the marker would resolve. We attach (never delete) — the
        # marker pipeline above always wins; the nets only fill gaps.
        #
        # Why this layer exists: production observations on 2026-05-13
        # showed three identical failure modes in a single session
        # (product missing → CTA link only; barcode missing → text
        # only; staff contact missing → phone-as-text). Tightening
        # the prompt further had diminishing returns; the durable fix
        # is to NOT rely on a single layer.
        #
        # Each net runs independently (own feature flag, own log),
        # and increments ``_marker_resolved`` so the
        # ``[MARKER_RESOLUTION]`` line below reflects post-net state.
        try:
            from modules.ai.postprocess.safety_nets import (  # noqa: PLC0415
                apply_product_safety_net as _sn_product,
                apply_media_key_safety_net as _sn_media_key,
                apply_staff_contact_safety_net as _sn_staff,
            )
            import json as _json_sn  # noqa: PLC0415

            _cust_id_sn = None
            try:
                _cust_id_sn = getattr(convo, "customer_id", None) or None
            except Exception:
                pass

            # Product safety net
            try:
                _pn = _sn_product(
                    db,
                    tenant_id=tenant_id,
                    customer_msg=text or "",
                    existing_product_attachments=_product_attachments,
                    detected_markers=_marker_detected["product"],
                    customer_id=_cust_id_sn,
                )
                if _pn.fired and _pn.extra_attachment:
                    _product_attachments.append(_pn.extra_attachment)
                    _marker_resolved["product"] += 1
                if _pn.fired or _pn.skipped_reason not in {"claude_marker_present", "already_attached", "no_intent_or_class", "empty_msg"}:
                    # Only log "interesting" outcomes — silence the
                    # boring "marker already there" case to keep log
                    # volume sane (those are the common path).
                    _payload = {
                        "event":             "safety_net",
                        "tenant_id":         tenant_id,
                        "conversation_id":   getattr(convo, "id", None),
                        **_pn.to_log_dict(),
                    }
                    logger.info(
                        "[SAFETY_NET:product] "
                        + _json_sn.dumps(_payload, ensure_ascii=False)
                    )
            except Exception as _spe:  # noqa: BLE001
                logger.warning(
                    "[SAFETY_NET:product] failed tenant=%s err=%s",
                    tenant_id, _spe,
                )

            # Media-key safety net
            try:
                _mn = _sn_media_key(
                    db,
                    tenant_id=tenant_id,
                    customer_msg=text or "",
                    existing_media_attachments=_media_attachments,
                    detected_media_key_markers=_marker_detected["media_key"],
                )
                if _mn.fired and _mn.extra_attachment:
                    _media_attachments.append(_mn.extra_attachment)
                    _marker_resolved["media_key"] += 1
                if _mn.fired or _mn.skipped_reason not in {"claude_marker_present", "already_has_media_key", "empty_msg", "no_trigger_match"}:
                    _payload = {
                        "event":             "safety_net",
                        "tenant_id":         tenant_id,
                        "conversation_id":   getattr(convo, "id", None),
                        **_mn.to_log_dict(),
                    }
                    logger.info(
                        "[SAFETY_NET:media_key] "
                        + _json_sn.dumps(_payload, ensure_ascii=False)
                    )
            except Exception as _sme:  # noqa: BLE001
                logger.warning(
                    "[SAFETY_NET:media_key] failed tenant=%s err=%s",
                    tenant_id, _sme,
                )

            # Staff-contact safety net (only when the CALL marker
            # pipeline itself is enabled; otherwise the merchant has
            # explicitly disabled vCard dispatch).
            if _staff_call_marker_enabled():
                try:
                    _cn = _sn_staff(
                        customer_msg=text or "",
                        reply_text=reply or "",
                        existing_call_targets=_call_targets,
                        detected_call_markers=_marker_detected["call"],
                    )
                    if _cn.fired and _cn.extra_call_target is not None:
                        _call_targets.append(_cn.extra_call_target)
                        _marker_resolved["call"] += 1
                    if _cn.fired or _cn.skipped_reason not in {"claude_marker_present", "already_attached", "empty_msg", "no_staff_intent"}:
                        _payload = {
                            "event":             "safety_net",
                            "tenant_id":         tenant_id,
                            "conversation_id":   getattr(convo, "id", None),
                            **_cn.to_log_dict(),
                        }
                        logger.info(
                            "[SAFETY_NET:staff_contact] "
                            + _json_sn.dumps(_payload, ensure_ascii=False)
                        )
                except Exception as _sse:  # noqa: BLE001
                    logger.warning(
                        "[SAFETY_NET:staff_contact] failed tenant=%s err=%s",
                        tenant_id, _sse,
                    )
        except Exception as _sn_exc:  # noqa: BLE001
            logger.warning(
                "[SAFETY_NET] module import failed tenant=%s err=%s",
                tenant_id, _sn_exc,
            )

        # ── Internal-reasoning scrubber (Phase 6) ──────────────────────
        # Drops lines that contain leaked reasoning prose (e.g. "بناءً
        # على السياق", "في قاعدة المعرفة"). Runs AFTER marker
        # extraction so the customer never sees the meta-text, but
        # the marker pipeline saw it untouched. Feature-flagged via
        # ``REASONING_SCRUB_ENABLED`` (default ON).
        if reply:
            try:
                from modules.ai.postprocess.reasoning_scrub import (  # noqa: PLC0415
                    scrub_reasoning_leaks as _scrub_reasoning,
                )
                _scr = _scrub_reasoning(reply)
                if _scr.any_change:
                    import json as _json_rs  # noqa: PLC0415
                    _scr_payload = {
                        "event":            "reasoning_scrub",
                        "tenant_id":        tenant_id,
                        "conversation_id":  getattr(convo, "id", None),
                        **_scr.to_log_dict(),
                    }
                    logger.info(
                        "[REASONING_SCRUB] "
                        + _json_rs.dumps(_scr_payload, ensure_ascii=False)
                    )
                    reply = _scr.text
            except Exception as _scr_exc:  # noqa: BLE001
                logger.warning(
                    "[REASONING_SCRUB] failed tenant=%s err=%s",
                    tenant_id, _scr_exc,
                )

        # ── Marker resolution structured log (Phase 3) ─────────────────
        # Single JSON line per turn summarising what the LLM emitted vs.
        # what we successfully resolved. This is the metric source for
        # the Product Resolver / Media Library health dashboards (and a
        # cheap signal for future regressions: a sudden spike in
        # `product_markers_failed` means the catalog drifted from what
        # Claude was told it could cite).
        if any(_marker_detected.values()) or any(_marker_resolved.values()):
            try:
                import json as _json_mr  # noqa: PLC0415
                _mr_payload = {
                    "event":                       "marker_resolution",
                    "tenant_id":                   tenant_id,
                    "conversation_id":             getattr(convo, "id", None),
                    "product_markers_detected":    _marker_detected["product"],
                    "product_markers_resolved":    _marker_resolved["product"],
                    "product_markers_failed":      _marker_failed["product"],
                    "media_id_markers_detected":   _marker_detected["media_id"],
                    "media_id_markers_resolved":   _marker_resolved["media_id"],
                    "media_id_markers_failed":     _marker_failed["media_id"],
                    "media_key_markers_detected":  _marker_detected["media_key"],
                    "media_key_markers_resolved":  _marker_resolved["media_key"],
                    "media_key_markers_failed":    _marker_failed["media_key"],
                    "call_markers_detected":       _marker_detected["call"],
                    "call_markers_resolved":       _marker_resolved["call"],
                    "call_markers_failed":         _marker_failed["call"],
                }
                logger.info(
                    "[MARKER_RESOLUTION] " + _json_mr.dumps(_mr_payload, ensure_ascii=False)
                )
            except Exception:  # noqa: BLE001 — never let logging break the turn
                pass

        # ── Payment-asset HARD OVERRIDE ─────────────────────────────────
        # If the customer's inbound looks like a bank/IBAN/QR/transfer
        # request AND we have a relevant active media item BUT GPT didn't
        # cite it, attach the asset anyway. This is the recovery path for
        # the bug where GPT replied "ما عندي بيانات الحساب البنكي" while
        # a "باركود التحويل البنكي الراجحي" item was sitting active in
        # ai_media_library. Detection is rule-based (cheap), and we
        # require the relevance score to clear a threshold so unrelated
        # uploads can't accidentally pre-empt the conversation.
        try:
            from core.ai_libraries import (  # noqa: PLC0415
                find_best_payment_asset as _find_payment_asset,
                is_payment_query as _is_payment_query,
            )
            _payment_intent = _is_payment_query(text or "")
            if _payment_intent:
                _already_attached_ids = {a.get("id") for a in _media_attachments}
                _payment_asset = _find_payment_asset(db, tenant_id, text or "")
                if _payment_asset and _payment_asset.get("id") not in _already_attached_ids:
                    _media_attachments.append(_payment_asset)
                    logger.info(
                        "[PAYMENT_INFO] tenant=%s conversation_id=%s "
                        "intent_detected=true asset_found=true asset_id=%s "
                        "asset_score=%.2f transfer_fallback_skipped=true "
                        "gpt_cited_marker=%s — hard override applied",
                        tenant_id, getattr(convo, "id", None),
                        _payment_asset.get("id"),
                        float(_payment_asset.get("_relevance_score") or 0.0),
                        bool(_already_attached_ids),
                    )
                    # If GPT's reply was a generic "I can't help" / contact
                    # owner template, replace it with a warm short text so
                    # the barcode lands with proper context. We detect that
                    # by checking for canonical contact-owner phrases.
                    _r_low = (reply or "").strip()
                    _looks_like_owner_fallback = bool(_r_low) and any(
                        marker in _r_low for marker in (
                            # Direct refusals — GPT saying it doesn't have the info.
                            "ما عندي بيانات الحساب",
                            "ما عندي معلومات",
                            "ما أقدر أوفرها",
                            "ما اقدر اوفرها",
                            "أعتذر إني ما أقدر",
                            "اعتذر اني ما اقدر",
                            "أعتذر، ما",
                            "اعتذر، ما",
                            "لا أستطيع تقديم",
                            "لا استطيع تقديم",
                            "لا أملك معلومات",
                            "لا املك معلومات",
                            # Generic owner-contact / handoff phrases.
                            "هذه وسائل التواصل",
                            "تواصل مع المتجر",
                            "تواصلي مع المتجر",
                            "سأحوّلك للفريق",
                            "سأحولك للفريق",
                            "أحوّلك للفريق",
                            "احولك للفريق",
                            "تواصل مع المالك",
                            "الفريق راح يتواصل",
                            "راح يتواصل معك",
                            "سيتواصل معك الفريق",
                            "وصل طلبك للفريق",
                            "طلبك وصل للفريق",
                            "وصلت رسالتك",
                        )
                    )
                    if _looks_like_owner_fallback or not _r_low:
                        reply = "أكيد 🌷 تفضل، هذه بيانات التحويل البنكي."
                        logger.info(
                            "[PAYMENT_INFO] tenant=%s replaced owner-fallback "
                            "reply with payment intro text",
                            tenant_id,
                        )
                elif _payment_asset is None:
                    logger.info(
                        "[PAYMENT_INFO] tenant=%s conversation_id=%s "
                        "intent_detected=true asset_found=false — "
                        "no relevant active media; letting GPT reply through",
                        tenant_id, getattr(convo, "id", None),
                    )
                else:
                    logger.info(
                        "[PAYMENT_INFO] tenant=%s conversation_id=%s "
                        "intent_detected=true asset_found=true asset_id=%s "
                        "already_cited_by_gpt=true — no override needed",
                        tenant_id, getattr(convo, "id", None),
                        _payment_asset.get("id"),
                    )
        except Exception as _pi_exc:  # noqa: BLE001 — never crash on the override
            logger.warning(
                "[PAYMENT_INFO] override failed tenant=%s err=%s",
                tenant_id, _pi_exc,
            )

        # ── Marker scrub ────────────────────────────────────────────────
        # Previously scrubbed inline here. The scrub now runs at the
        # wire layer in ``services.whatsapp_platform.service.
        # provider_send_message`` so every outbound caller (manual
        # /conversations/reply, automation engine, orders, cart
        # recovery, admin direct-send, fallback / loop-guard replies)
        # gets the same defense-in-depth, not just the AI reply path.
        # See ``_scrub_outbound_payload`` in that module.
        #
        # We keep an inline scrub on the human-visible ``reply`` text
        # ONLY for logging purposes (so the merchant dashboard, which
        # reads message events directly from the DB, also sees the
        # cleaned copy — not because we trust the inline path).

        # ── Delivery-mode audit (May 2026 — observability) ─────────
        # Per-turn record of what we actually sent to the customer.
        # Each successful send below stamps a flag / increments a
        # counter; at the end of dispatch we compute one closed-enum
        # verdict (catalog / image_cta / media_only / cta_only /
        # text_only / failed) and log a [FINAL_DELIVERY] line. When
        # the customer asked for product / image / catalog content
        # but the verdict is unacceptable, we additionally emit a
        # [DELIVERY_GUARD_FAIL] ERROR — that's the alarm for the
        # exact production regression where the bot replied
        # "أبشر خالد 🍯" to "أبغى أشوف صورة لعسل السمر" with no
        # image, no card, no link, no fallback. Defensive: any
        # exception when computing or logging the mode is swallowed
        # so the customer turn always exits cleanly.
        try:
            from modules.observability import new_delivery_audit as _new_audit  # noqa: PLC0415
            _delivery_audit = _new_audit()
        except Exception:  # noqa: BLE001
            _delivery_audit = {}

        if reply:
            try:
                from core.ai_libraries import scrub_internal_markers as _scrub  # noqa: PLC0415
                _orig = reply
                reply = _scrub(reply)
                if reply != _orig:
                    logger.info(
                        "[MARKER_SCRUB] tenant=%s conversation_id=%s "
                        "stripped_chars=%d (dashboard copy; wire layer "
                        "also scrubs)",
                        tenant_id, getattr(convo, "id", None),
                        len(_orig) - len(reply or ""),
                    )
            except Exception as _scrub_exc:
                logger.warning(
                    "[MARKER_SCRUB] failed tenant=%s err=%s",
                    tenant_id, _scrub_exc,
                )

        if _brain_buttons and reply:
            _send_ok = await _send_interactive_reply(
                phone_id=phone_id, to=to,
                body_text=reply,
                buttons=_brain_buttons,
                _tenant_id=tenant_id, _db=db,
            )
            if _send_ok and isinstance(_delivery_audit, dict):
                _delivery_audit["interactive_buttons_sent"] = True
                _delivery_audit["text_sent"] = True
        else:
            # ── URL → CTA-button normaliser ─────────────────────────
            # The reply may carry 0, 1 or >1 URLs. WhatsApp's
            # ``cta_url`` interactive only supports ONE button per
            # message, so for multi-URL replies we split into a
            # SEQUENCE of messages — each URL gets its own CTA. This
            # closes the production bug where "أبي سمر وطلح" replies
            # contained two product URLs and only the first became a
            # clickable button; the second was rendered as raw text.
            # See ``core.wa_link_buttons.split_text_for_cta_buttons``
            # for the algorithm. Brain replies that already attached
            # quick-reply buttons are skipped (handled above).
            _cta_messages: list = []
            try:
                from core.wa_link_buttons import (  # noqa: PLC0415
                    split_text_for_cta_buttons as _split_cta,
                )
                # We don't pass store_domain here: product detection by
                # path pattern (/products/, /p/, …) is enough for the
                # current AI-reply shapes. A future enhancement can plug
                # the merchant's known domain in for stricter matching.
                _cta_messages = _split_cta(reply or "")
            except Exception as _cta_exc:
                logger.debug("[CTA_BUTTON] split failed tenant=%s: %s", tenant_id, _cta_exc)
                _cta_messages = []

            _send_ok = False
            _multi_cta_count = sum(1 for m in _cta_messages if m.cta is not None)
            if _multi_cta_count >= 2:
                # Multi-URL path: send each message individually. The
                # legacy ``reply`` variable stays for the dashboard
                # transcript (the customer experience is what matters
                # in production; the transcript already showed the
                # full text-with-URLs version anyway).
                logger.info(
                    "[CTA_BUTTON_SPLIT] tenant=%s conversation_id=%s "
                    "messages=%d cta_count=%d",
                    tenant_id, getattr(convo, "id", None),
                    len(_cta_messages), _multi_cta_count,
                )
                _all_ok = True
                _first_send = True
                for _idx, _msg in enumerate(_cta_messages):
                    try:
                        if _msg.cta is not None:
                            _ok_one = await _send_cta_url(
                                phone_id=phone_id, to=to,
                                body_text=_msg.body or _msg.cta.url,
                                btn_label=_msg.cta.button_title,
                                btn_url=_msg.cta.url,
                                _tenant_id=tenant_id, _db=db,
                            )
                            if _ok_one and isinstance(_delivery_audit, dict):
                                _delivery_audit["cta_url_sent_count"] = (
                                    int(_delivery_audit.get("cta_url_sent_count", 0)) + 1
                                )
                        else:
                            _ok_one = await _send_whatsapp_message(
                                phone_id=phone_id, to=to, text=_msg.body,
                                _tenant_id=tenant_id, _db=db,
                            )
                            if _ok_one and isinstance(_delivery_audit, dict):
                                _delivery_audit["text_sent"] = True
                    except Exception as _split_send_exc:
                        logger.warning(
                            "[CTA_BUTTON_SPLIT_FALLBACK] tenant=%s idx=%d reason=%s",
                            tenant_id, _idx, _split_send_exc,
                        )
                        _ok_one = False
                    # Treat overall success as "at least the first
                    # send made it" — partial failures still count as
                    # a delivered AI reply for the outbound counter
                    # so the loop guard doesn't retry.
                    if _first_send:
                        _send_ok = _ok_one
                        _first_send = False
                    if not _ok_one:
                        _all_ok = False
                if not _all_ok:
                    logger.info(
                        "[CTA_BUTTON_SPLIT_PARTIAL] tenant=%s — one or more "
                        "split messages failed; customer received the rest",
                        tenant_id,
                    )
            elif _cta_messages and _cta_messages[0].cta is not None and len(_cta_messages) == 1:
                # Single-CTA path: byte-identical to the legacy
                # behaviour so existing single-product replies don't
                # change shape. Lift the URL into a ``cta_url`` button
                # with the cleaned body.
                _msg = _cta_messages[0]
                _cls = _msg.cta
                logger.info(
                    "[CTA_BUTTON] tenant=%s conversation_id=%s url_type=%s "
                    "button_title=%r url_domain=%s body_len=%d",
                    tenant_id, getattr(convo, "id", None), _cls.kind,
                    _cls.button_title, _cls.domain, len(_msg.body or ""),
                )
                try:
                    _send_ok = await _send_cta_url(
                        phone_id=phone_id, to=to,
                        body_text=_msg.body or reply,
                        btn_label=_cls.button_title,
                        btn_url=_cls.url,
                        _tenant_id=tenant_id, _db=db,
                    )
                    if _send_ok and isinstance(_delivery_audit, dict):
                        _delivery_audit["cta_url_sent_count"] = (
                            int(_delivery_audit.get("cta_url_sent_count", 0)) + 1
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
                    reply = _msg.body or reply
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
                    if _send_ok and isinstance(_delivery_audit, dict):
                        _delivery_audit["text_sent"] = True
            else:
                # No URLs in reply → plain text send (also handles the
                # degenerate case where the splitter returned only
                # plain-text segments).
                _send_ok = await _send_whatsapp_message(
                    phone_id=phone_id, to=to, text=reply,
                    _tenant_id=tenant_id, _db=db,
                )
                if _send_ok and isinstance(_delivery_audit, dict):
                    _delivery_audit["text_sent"] = True
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

            # Concatenate library media + product cards into one
            # ordered list so the customer sees them in the same
            # sequence the LLM intended. Library media go FIRST
            # (typically explanatory — payment barcode, certificate)
            # and product cards SECOND so the customer sees the
            # "context" before the "offer".
            _all_attachments = list(_media_attachments) + list(
                _product_attachments  # may be empty
            )

            # Phase 4: cache the WhatsAppConnection once per turn so
            # the catalog send helper doesn't re-query for every
            # product attachment. We only look this up when there is
            # actually a product card to render — otherwise the
            # legacy media path runs unchanged. The lookup is
            # best-effort: a None result simply means catalog sends
            # are skipped and the legacy image+CTA path runs as
            # before.
            _cached_wa_conn = None
            if _product_attachments:
                try:
                    from database.models import (  # noqa: PLC0415
                        WhatsAppConnection as _WAConn,
                    )
                    _cached_wa_conn = (
                        db.query(_WAConn)
                        .filter(_WAConn.tenant_id == tenant_id)
                        .first()
                    )
                except Exception as _conn_lookup_exc:  # noqa: BLE001
                    logger.debug(
                        "[CATALOG] tenant=%s connection lookup failed "
                        "(catalog send will be skipped, legacy path "
                        "will run): %s",
                        tenant_id, _conn_lookup_exc,
                    )

            for _att in _all_attachments:
                _is_product = (_att.get("kind") == "product_card")

                # Phase 4 — Meta WhatsApp Catalog attempt BEFORE the
                # legacy image+CTA path. _try_send_catalog_product
                # short-circuits on any miss (catalog disabled, no
                # retailer id, provider error) and we fall through
                # to the legacy path so the customer always gets a
                # reply. Success → skip the legacy path entirely.
                if _is_product:
                    try:
                        _catalog_sent = await _try_send_catalog_product(
                            db=db,
                            connection=_cached_wa_conn,
                            tenant_id=tenant_id,
                            phone_id=phone_id,
                            to=to,
                            attachment=_att,
                        )
                    except Exception as _cat_exc:  # noqa: BLE001
                        # _try_send_catalog_product is documented to
                        # never raise, but treat any escape as a
                        # bug-grade fallback so the conversation
                        # still gets a reply.
                        logger.error(
                            "[CATALOG_FALLBACK_TEXT] tenant=%s "
                            "product_id=%s reason=helper_exception "
                            "err=%s",
                            tenant_id, _att.get("id"), _cat_exc,
                        )
                        _catalog_sent = False
                    if _catalog_sent:
                        # Catalog rendered the product card natively
                        # — no need to send a separate image+CTA.
                        # Continue to the next attachment.
                        if isinstance(_delivery_audit, dict):
                            _delivery_audit["catalog_card_sent_count"] = (
                                int(_delivery_audit.get("catalog_card_sent_count", 0)) + 1
                            )
                        continue

                # Library media goes through the full validation
                # gate (tenant scope, MIME, size, HTTPS). Product
                # cards are validated separately: we trust the
                # catalog adapter for the image URL but we DO need
                # to skip cards with no image (so we don't try to
                # send an empty link to Meta).
                if _is_product:
                    if not _att.get("file_url"):
                        logger.info(
                            "[ProductCard.send] tenant=%s product_id=%s "
                            "SKIPPED reason=no_image_url url=%s",
                            tenant_id, _att.get("id"),
                            _att.get("product_url"),
                        )
                        # Still send the URL as a CTA-only message
                        # so the customer at least gets the link.
                        if _att.get("product_url"):
                            try:
                                _cta_only_ok = await _send_cta_url(
                                    phone_id=phone_id, to=to,
                                    body_text=_att.get("title") or "عرض المنتج",
                                    btn_label="عرض المنتج",
                                    btn_url=_att.get("product_url"),
                                    _tenant_id=tenant_id, _db=db,
                                )
                                if _cta_only_ok and isinstance(_delivery_audit, dict):
                                    _delivery_audit["cta_url_sent_count"] = (
                                        int(_delivery_audit.get("cta_url_sent_count", 0)) + 1
                                    )
                            except Exception:
                                pass
                        continue
                elif _validate_media is not None:
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
                # Product cards carry their caption (title + price +
                # short description) on the image itself; library
                # media don't use captions today, so this stays
                # ``None`` for the non-product path and matches the
                # previous behaviour exactly.
                _caption = _att.get("caption") if _is_product else None
                try:
                    _media_ok = await _send_media_message(
                        phone_id=phone_id,
                        to=to,
                        media_type=_media_type_norm,
                        media_url=_att.get("file_url") or "",
                        filename=_filename,
                        caption=_caption,
                        _tenant_id=tenant_id,
                        _db=db,
                    )
                    if _media_ok and isinstance(_delivery_audit, dict):
                        _delivery_audit["legacy_media_sent_count"] = (
                            int(_delivery_audit.get("legacy_media_sent_count", 0)) + 1
                        )
                    if _is_product:
                        logger.info(
                            "[ProductCard.send] tenant=%s to=%s product_id=%s "
                            "ext_id=%s ok=%s confidence=%s",
                            tenant_id, to, _att.get("id"),
                            _att.get("external_id"), _media_ok,
                            _att.get("confidence"),
                        )
                    else:
                        logger.info(
                            "[AIMedia.send] tenant=%s to=%s id=%s type=%s ok=%s",
                            tenant_id, to, _att.get("id"),
                            _media_type_norm, _media_ok,
                        )

                    # After the product image lands, follow up with
                    # a CTA-URL button to the buy page so the
                    # customer can checkout in one tap. We do NOT
                    # rely on the caption containing a link — Meta
                    # won't auto-linkify image captions, and a
                    # separate interactive message has much higher
                    # click-through. Send only when both the send
                    # succeeded and a product URL exists.
                    if _is_product and _media_ok and _att.get("product_url"):
                        try:
                            _product_cta_ok = await _send_cta_url(
                                phone_id=phone_id, to=to,
                                body_text="اضغط زر «عرض المنتج» لإكمال "
                                          "الطلب من المتجر مباشرة.",
                                btn_label="عرض المنتج",
                                btn_url=_att.get("product_url"),
                                _tenant_id=tenant_id, _db=db,
                            )
                            if _product_cta_ok and isinstance(_delivery_audit, dict):
                                _delivery_audit["cta_url_sent_count"] = (
                                    int(_delivery_audit.get("cta_url_sent_count", 0)) + 1
                                )
                        except Exception as _cta_exc:
                            logger.debug(
                                "[ProductCard.cta] tenant=%s product_id=%s "
                                "cta_failed: %s",
                                tenant_id, _att.get("id"), _cta_exc,
                            )
                except Exception as _media_send_exc:
                    logger.warning(
                        "[AIMedia.send] tenant=%s id=%s failed: %s",
                        tenant_id, _att.get("id"), _media_send_exc,
                    )

            # ── Staff call contact cards ────────────────────────────
            # Dispatched LAST so the customer sees: (1) the main
            # reply text → (2) any product / media images → (3) the
            # contact card(s) at the bottom. This order matches the
            # marker placement contract: [PRODUCT/MEDIA_KEY] go at
            # the top of the reply, [CALL] goes at the bottom.
            #
            # We send a SINGLE contacts message containing all
            # resolved targets (up to MAX_CALLS_PER_REPLY). One
            # network call, one notification on the customer's
            # phone — better UX than separate cards.
            if _call_targets:
                try:
                    from services.call_resolver import (  # noqa: PLC0415
                        build_contacts_payload as _build_contacts,
                    )
                    _contacts_payload = _build_contacts(_call_targets, to=to)
                    _contacts_ok = await _send_contacts_message(
                        phone_id=phone_id, to=to,
                        payload=_contacts_payload,
                        _tenant_id=tenant_id, _db=db,
                    )
                    if _contacts_ok and isinstance(_delivery_audit, dict):
                        _delivery_audit["contacts_sent"] = True
                    try:
                        import json as _json_call  # noqa: PLC0415
                        _call_log_payload = {
                            "event":             "call_marker",
                            "tenant_id":         tenant_id,
                            "conversation_id":   getattr(convo, "id", None),
                            "detected":          _marker_detected["call"],
                            "resolved":          len(_call_targets),
                            "dispatched":        bool(_contacts_ok),
                            "targets":           [
                                {"name": c.name, "wa_id": c.wa_id}
                                for c in _call_targets
                            ],
                        }
                        logger.info(
                            "[CALL_MARKER] "
                            + _json_call.dumps(_call_log_payload, ensure_ascii=False)
                        )
                    except Exception:  # noqa: BLE001 — log failures are non-fatal
                        pass
                except Exception as _contacts_exc:  # noqa: BLE001
                    logger.warning(
                        "[CallResolver] contacts send failed tenant=%s err=%s",
                        tenant_id, _contacts_exc,
                    )

            # Track this reply for similarity-based loop scoring on the
            # next turn. Never auto-pauses on counts alone.
            try:
                from core.ai_pause_guard import after_ai_reply as _after_ai_reply  # noqa: PLC0415
                _after_ai_reply(db, convo, tenant_id=tenant_id, reply_text=reply)
            except Exception as _rate_exc:
                logger.debug("[ai_pause] post-reply tracker failed: %s", _rate_exc)

            # ── Awaiting-receipt detection ────────────────────────
            # If the bot asked the customer for a transfer receipt
            # in this very reply, flip ``awaiting_payment_receipt``
            # on the persisted brain_state so the NEXT inbound PDF
            # / image is classified with high confidence even when
            # the bank-generated filename doesn't carry any
            # receipt keywords. Best-effort; failures are logged
            # but never block the conversation.
            try:
                from core.order_flow import (  # noqa: PLC0415
                    detect_awaiting_receipt_in_reply,
                    mark_awaiting_receipt,
                )
                if detect_awaiting_receipt_in_reply(reply or ""):
                    mark_awaiting_receipt(
                        db, tenant_id=tenant_id, phone=to,
                    )
                    logger.info(
                        "[ORDER_FLOW_STATE] transition=awaiting_receipt "
                        "tenant=%s phone=*%s source=brain_reply_keyword",
                        tenant_id, to[-4:] if to else "",
                    )
            except Exception as _ar_exc:  # noqa: BLE001
                logger.debug(
                    "[ORDER_FLOW_STATE] awaiting-receipt detection "
                    "failed: %s", _ar_exc,
                )

            # ── Final-delivery-mode verdict + UX guard (May 2026) ──────
            # See the audit init block earlier for rationale. We log
            # ONE structured line per turn — operators grep
            # ``[FINAL_DELIVERY]`` to answer "did the customer
            # actually see something useful?". When the customer
            # asked for product / image / catalog content but the
            # final mode is unacceptable, we additionally emit a
            # ``[DELIVERY_GUARD_FAIL]`` ERROR. The guard is the
            # alarm for silent UX regressions like the production
            # case where the bot replied "أبشر خالد 🍯" to
            # "أبغى أشوف صورة لعسل السمر" — no image, no card, no
            # link, no fallback, no log line that flagged it.
            try:
                from modules.observability import (  # noqa: PLC0415
                    compute_final_delivery_mode as _compute_mode,
                    customer_wants_product_or_image as _wants_product,
                )
                from modules.observability.delivery_mode import (  # noqa: PLC0415
                    is_acceptable_mode_for_product_intent as _mode_ok,
                )

                _final_mode = _compute_mode(_delivery_audit)
                _wants = _wants_product(
                    inbound_text=text or "",
                    brain_action=_br_action or "",
                )
                logger.info(
                    "[FINAL_DELIVERY] tenant=%s to=*%s mode=%s "
                    "wants_product_or_image=%s brain_action=%s "
                    "audit=%s",
                    tenant_id,
                    (to[-4:] if to else ""),
                    _final_mode,
                    str(bool(_wants)).lower(),
                    _br_action or "?",
                    _delivery_audit,
                )
                if _wants and not _mode_ok(_final_mode):
                    logger.error(
                        "[DELIVERY_GUARD_FAIL] tenant=%s to=*%s "
                        "mode=%s reason=product_intent_without_rich_content "
                        "inbound=%r brain_action=%s reply_len=%d "
                        "audit=%s",
                        tenant_id,
                        (to[-4:] if to else ""),
                        _final_mode,
                        (text or "")[:120],
                        _br_action or "?",
                        len(reply or ""),
                        _delivery_audit,
                    )
            except Exception as _fd_exc:  # noqa: BLE001
                logger.debug(
                    "[FINAL_DELIVERY] tenant=%s instrumentation failed: %s",
                    tenant_id, _fd_exc,
                )
        else:
            # Initial reply send failed. Stamp the audit so the
            # FINAL_DELIVERY classifier (which we still want to emit
            # for observability symmetry) returns ``"failed"``.
            if isinstance(_delivery_audit, dict):
                _delivery_audit["first_send_failed"] = True
            logger.error(
                "[TRACE][5/6] MERCHANT_AI_SEND_FAILED | tenant=%s to=%s reply_len=%s",
                tenant_id, to, len(reply or ""),
            )
            try:
                from modules.observability import (  # noqa: PLC0415
                    compute_final_delivery_mode as _compute_mode,
                    customer_wants_product_or_image as _wants_product,
                )
                from modules.observability.delivery_mode import (  # noqa: PLC0415
                    is_acceptable_mode_for_product_intent as _mode_ok,
                )

                _final_mode = _compute_mode(_delivery_audit)
                _wants = _wants_product(
                    inbound_text=text or "",
                    brain_action=_br_action or "",
                )
                logger.info(
                    "[FINAL_DELIVERY] tenant=%s to=*%s mode=%s "
                    "wants_product_or_image=%s brain_action=%s "
                    "audit=%s",
                    tenant_id,
                    (to[-4:] if to else ""),
                    _final_mode,
                    str(bool(_wants)).lower(),
                    _br_action or "?",
                    _delivery_audit,
                )
                if _wants and not _mode_ok(_final_mode):
                    logger.error(
                        "[DELIVERY_GUARD_FAIL] tenant=%s to=*%s "
                        "mode=%s reason=product_intent_send_failed "
                        "inbound=%r brain_action=%s reply_len=%d",
                        tenant_id,
                        (to[-4:] if to else ""),
                        _final_mode,
                        (text or "")[:120],
                        _br_action or "?",
                        len(reply or ""),
                    )
            except Exception as _fd_exc:  # noqa: BLE001
                logger.debug(
                    "[FINAL_DELIVERY] tenant=%s instrumentation failed: %s",
                    tenant_id, _fd_exc,
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
    # ── External-research leakage guard (May 2026) ────────────────
    # Final scrubber for the May 2026 DuckDuckGo-leak incident: if any
    # subsystem (brain, LLM, legacy code path) produced an outbound
    # reply containing search-dump fingerprints, replace the body
    # with a safe fallback and log [EXTERNAL_RESEARCH_BLOCKED]. The
    # sanitiser is fail-open by design — if it crashes the payload
    # still goes through, but we ALWAYS log the crash for diagnosis.
    # See core/outbound_sanitizer.py for the leakage fingerprints.
    try:
        from core.outbound_sanitizer import sanitize_outbound_payload  # noqa: PLC0415
        _recipient = str((payload or {}).get("to") or "") if isinstance(payload, dict) else ""
        payload, _sanitised = sanitize_outbound_payload(
            payload if isinstance(payload, dict) else {},
            tenant_id=_tenant_id,
            recipient=_recipient,
        )
        if _sanitised:
            # Mark the payload so callers/observers can tell at a
            # glance that this message was rewritten. Stored under
            # an internal underscore-prefixed key that 360dialog /
            # Meta ignore (we strip it before serialisation if any
            # downstream module re-serialises the dict).
            try:
                payload["_nahla_sanitised"] = "external_research_blocked"
            except Exception:
                pass
    except Exception:
        # Sanitiser bug must never block a send. We log inside the
        # sanitiser; here we just continue with the original payload.
        pass

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

        def _stamp_throttled(reason: str) -> None:
            """Mark the persisted outbound row as ``failed`` with a
            clear ``throttled`` reason so the dashboard tells the
            merchant the send was blocked by Nahla's burst protection,
            not silently dropped. Never raises."""
            try:
                from core.outbound_send_status import (  # noqa: PLC0415
                    stamp_outbound_send_status,
                )
                stamp_outbound_send_status(
                    _db,
                    tenant_id=_tenant_id,
                    recipient=recipient,
                    classification="exception",
                    response_body={
                        "error": {
                            "code":    "throttled",
                            "type":    "RateLimit",
                            "message": (
                                "Nahla rate-limited this send to avoid "
                                f"a burst loop ({reason})."
                            ),
                        }
                    },
                    wamid=None,
                    operation="send_message",
                    error_text=f"throttled:{reason}",
                )
            except Exception:
                pass

        if not check_rate_limit(rate_key, max_count=6, window_seconds=10):
            logger.warning(
                "[WA] throttled burst send | tenant_id=%s to=%s phone_number_id=%s",
                _tenant_id, recipient, phone_id,
            )
            _stamp_throttled("burst_10s")
            return False
        if not check_rate_limit(rate_key, max_count=20, window_seconds=60):
            logger.warning(
                "[WA] throttled minute send | tenant_id=%s to=%s phone_number_id=%s",
                _tenant_id, recipient, phone_id,
            )
            _stamp_throttled("burst_60s")
            return False

        # ── Outbound idempotency guard ──────────────────────────────────
        # Stops the same logical AI reply from reaching Meta twice
        # after webhook redelivery, worker restart, auto-register
        # retry, or a double-clicked manual reply. Returns:
        #   * skip=False → first time we've seen this (tenant, to,
        #     body) within 5 min. Proceed with the POST below; the
        #     guard has marked the key as in-flight so concurrent
        #     callers in this process will short-circuit.
        #   * skip=True, wamid=<prior> → we already POSTed this and
        #     got a wamid. Treat the call as a no-op success: re-
        #     stamp the persisted MessageEvent with the existing
        #     wamid and return True so the upstream caller doesn't
        #     mark the message as failed.
        #   * skip=True, wamid=None → another concurrent caller
        #     OWNS this send. Return False without stamping; the
        #     primary call will write the row state.
        try:
            from core.outbound_dedup import check_outbound_send  # noqa: PLC0415
            _dedup_res = check_outbound_send(
                tenant_id=_tenant_id,
                recipient=recipient,
                payload=payload,
            )
        except Exception as _dedup_exc:  # noqa: BLE001
            logger.warning(
                "[WA] outbound dedup check failed (non-fatal): %s",
                _dedup_exc,
            )
            _dedup_res = None

        if _dedup_res is not None and _dedup_res.skip:
            if _dedup_res.reason == "already_sent":
                # Re-stamp the most recent queued row for this
                # recipient with the prior wamid so the dashboard
                # surfaces the "delivered" state even though we
                # didn't actually POST again.
                try:
                    from core.outbound_send_status import (  # noqa: PLC0415
                        stamp_outbound_send_status,
                    )
                    stamp_outbound_send_status(
                        _db,
                        tenant_id=_tenant_id,
                        recipient=recipient,
                        classification="ok",
                        response_body={
                            "messages": [{"id": _dedup_res.wamid or ""}],
                            "_nahla_duplicate_suppressed": True,
                        },
                        wamid=_dedup_res.wamid,
                        operation="send_message_dedup",
                    )
                except Exception:
                    pass
                logger.info(
                    "[WA] duplicate outbound suppressed | tenant=%s to=%s "
                    "wamid=*%s reason=already_sent",
                    _tenant_id, recipient,
                    (_dedup_res.wamid or "")[-6:] or None,
                )
                return True
            # in_flight: another worker is mid-POST. Return False
            # without stamping to avoid racing the primary caller.
            logger.info(
                "[WA] duplicate outbound suppressed (in_flight) | "
                "tenant=%s to=%s",
                _tenant_id, recipient,
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
            # ── Bridge wire-layer outcome → persisted MessageEvent ─────
            # The dashboard reads MessageEvent rows verbatim and used to
            # render every outbound bubble as "delivered" because the
            # row was persisted BEFORE this POST returned. We now stamp
            # the row's ``extra_metadata.provider_send`` with the F18
            # classification (ok / non_2xx / provider_error_field /
            # missing_wamid / exception) + Meta error key + wamid so
            # the UI can show a real status (clock / check / red ×)
            # instead of an optimistic double-check.
            #
            # Errors here are intentionally swallowed: a stamping bug
            # MUST NOT break a successful send. The helper itself never
            # raises, but the import line could in principle fail.
            try:
                from core.outbound_send_status import (  # noqa: PLC0415
                    stamp_outbound_send_status,
                )
                _classification = (resp_data or {}).get("_nahla_classification") or (
                    "ok" if "error" not in (resp_data or {}) else "provider_error_field"
                )
                _wamid = (resp_data or {}).get("_nahla_wamid")
                _duration = (resp_data or {}).get("_nahla_duration_ms")
                stamp_outbound_send_status(
                    _db,
                    tenant_id=_tenant_id,
                    recipient=str(payload.get("to") or ""),
                    classification=_classification,
                    response_body=resp_data,
                    wamid=_wamid,
                    operation="send_message",
                    duration_ms=_duration,
                )
            except Exception as _stamp_exc:  # noqa: BLE001
                logger.warning(
                    "[WA] outbound stamp failed (non-fatal) tenant=%s err=%s",
                    _tenant_id, _stamp_exc,
                )
            # Record the outcome so concurrent retries within the
            # dedup TTL short-circuit. We do this for BOTH ok and
            # error responses — errors get recorded with
            # ``succeeded=False`` so the dedup cache allows a retry
            # to flow through (failures are retryable, successes
            # are not).
            try:
                from core.outbound_dedup import (  # noqa: PLC0415
                    record_outbound_result,
                )
                record_outbound_result(
                    tenant_id=_tenant_id,
                    recipient=recipient,
                    payload=payload,
                    wamid=_wamid,
                    succeeded=("error" not in (resp_data or {}))
                              and bool(_wamid),
                )
            except Exception:
                pass
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
                            # Re-stamp the same row with the retry outcome so
                            # the dashboard reflects the FINAL state, not the
                            # initial failure that triggered auto-register.
                            try:
                                from core.outbound_send_status import (  # noqa: PLC0415
                                    stamp_outbound_send_status,
                                )
                                _retry_cls = (retry_data or {}).get("_nahla_classification") or (
                                    "ok" if "error" not in (retry_data or {}) else "provider_error_field"
                                )
                                stamp_outbound_send_status(
                                    _db,
                                    tenant_id=_tenant_id,
                                    recipient=str(payload.get("to") or ""),
                                    classification=_retry_cls,
                                    response_body=retry_data,
                                    wamid=(retry_data or {}).get("_nahla_wamid"),
                                    operation="send_message_retry",
                                    duration_ms=(retry_data or {}).get("_nahla_duration_ms"),
                                )
                            except Exception as _retry_stamp_exc:  # noqa: BLE001
                                logger.warning(
                                    "[WA] outbound retry-stamp failed (non-fatal) tenant=%s err=%s",
                                    _tenant_id, _retry_stamp_exc,
                                )
                            # Record the retry outcome in the dedup
                            # cache so a future redelivery doesn't
                            # double-send.
                            try:
                                from core.outbound_dedup import (  # noqa: PLC0415
                                    record_outbound_result as _rec_dedup,
                                )
                                _retry_wamid = (retry_data or {}).get("_nahla_wamid")
                                _rec_dedup(
                                    tenant_id=_tenant_id,
                                    recipient=recipient,
                                    payload=payload,
                                    wamid=_retry_wamid,
                                    succeeded=("error" not in (retry_data or {}))
                                              and bool(_retry_wamid),
                                )
                            except Exception:
                                pass
                            return "error" not in (retry_data or {})
                        except Exception as retry_exc:  # noqa: BLE001
                            logger.error(
                                "[WA] retry-after-register failed: %s", retry_exc,
                            )
                            # Stamp as exception so the dashboard shows the
                            # red × even though we caught the raise here.
                            try:
                                from core.outbound_send_status import (  # noqa: PLC0415
                                    stamp_outbound_send_status,
                                )
                                stamp_outbound_send_status(
                                    _db,
                                    tenant_id=_tenant_id,
                                    recipient=str(payload.get("to") or ""),
                                    classification="exception",
                                    response_body=None,
                                    wamid=None,
                                    operation="send_message_retry",
                                    error_text=f"{type(retry_exc).__name__}: {retry_exc}",
                                )
                            except Exception:
                                pass
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
            # Transport-level failure (timeout, DNS, TLS, connection reset).
            # Stamp the row so the merchant sees "تعذّر الاتصال" instead
            # of an optimistic double-check.
            try:
                from core.outbound_send_status import (  # noqa: PLC0415
                    stamp_outbound_send_status,
                )
                stamp_outbound_send_status(
                    _db,
                    tenant_id=_tenant_id,
                    recipient=str(payload.get("to") or ""),
                    classification="exception",
                    response_body=None,
                    wamid=None,
                    operation="send_message",
                    error_text=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
            return False
    finally:
        if owns_db and _db is not None:
            try:
                _db.close()
            except Exception:
                pass


async def _try_send_catalog_product(
    *,
    db,
    connection,
    tenant_id: Optional[int],
    phone_id: str,
    to: str,
    attachment: Dict[str, Any],
) -> bool:
    """Phase 4 — attempt a Meta WhatsApp Catalog send for one product
    attachment. Returns ``True`` iff the catalog message landed at
    the provider AND a wamid came back; the caller treats that as
    "skip the legacy image+CTA path". Returns ``False`` for every
    other outcome — eligibility miss, provider error, transport
    exception — so the caller falls back to the legacy path. We
    NEVER raise: silence is never acceptable, the legacy path must
    always be reachable.

    Three resolution lookups happen here, in order:

    1. The :class:`WhatsAppConnection` is read from the caller's
       cache (passed in as ``connection`` — looking it up once per
       turn is the caller's job, not ours).
    2. The Meta retailer id is resolved by
       :func:`core.catalog.effective_retailer_id`. We attempt a
       small DB lookup on ``Product`` first so a merchant who set
       ``meta_retailer_id`` explicitly gets it honoured; if that
       lookup fails for any reason we fall back to the attachment's
       ``external_id`` (the Salla auto-publish convention covers
       this for 95% of merchants without DB cost).
    3. :func:`core.catalog.is_catalog_eligible` short-circuits the
       send when ``catalog_enabled`` is False, the catalog id is
       empty, or no retailer id resolves.

    Logging convention:
      * ``[CATALOG_MATCH]`` — emitted exactly once when the product
        matches AND the connection is eligible. Followed by either a
        ``[CATALOG_SEND_SUCCESS]`` (from the sender module) or a
        ``[CATALOG_FALLBACK_TEXT]`` line here.
      * ``[CATALOG_FALLBACK_TEXT]`` — emitted whenever this helper
        returns False AFTER attempting catalog (i.e. eligibility
        passed but the provider rejected the payload). Eligibility-
        miss cases log ``[CATALOG_NOT_ELIGIBLE]`` inside the sender
        module and we stay silent here to avoid double-logging.
    """
    if not attachment or attachment.get("kind") != "product_card":
        return False
    try:
        from core.catalog import (  # noqa: PLC0415
            effective_retailer_id, is_catalog_eligible,
        )
        from services.whatsapp_platform.catalog_sender import (  # noqa: PLC0415
            send_single_product_message,
        )
    except Exception as imp_exc:  # noqa: BLE001
        logger.debug(
            "[CATALOG] tenant=%s helpers unavailable, skipping catalog send: %s",
            tenant_id, imp_exc,
        )
        return False

    # Honour an explicit `meta_retailer_id` override when set. One
    # cheap SELECT keyed on (id, tenant_id) — we already have both
    # ids in scope, so this is index-perfect on the products PK.
    retailer_id = ""
    product_row = None
    if db is not None and attachment.get("id") and tenant_id is not None:
        try:
            from database.models import Product  # noqa: PLC0415
            product_row = (
                db.query(Product)
                .filter(Product.id == attachment.get("id"),
                        Product.tenant_id == tenant_id)
                .first()
            )
        except Exception as q_exc:  # noqa: BLE001
            logger.debug(
                "[CATALOG] tenant=%s product lookup failed (will fall "
                "back to attachment external_id): %s",
                tenant_id, q_exc,
            )

    if product_row is not None:
        retailer_id = effective_retailer_id(product_row)
    if not retailer_id:
        # Fallback path: the attachment dict carries external_id from
        # the resolver. effective_retailer_id treats it as the
        # default retailer id when no explicit override exists.
        retailer_id = effective_retailer_id(attachment)

    elig = is_catalog_eligible(
        connection,
        products=[product_row] if product_row is not None else [attachment],
    )
    if not elig.ok:
        # Eligibility miss — silent here; sender module already logs
        # [CATALOG_NOT_ELIGIBLE] when invoked. We bail out so the
        # caller routes to the legacy image+CTA path immediately.
        return False
    if not retailer_id:
        # Shouldn't happen given the eligibility check above also
        # validates retailer_id presence, but defensive.
        return False

    logger.info(
        "[CATALOG_MATCH] tenant=%s product_id=%s ext_id=%s "
        "retailer_id=%s catalog_id=%s confidence=%s",
        tenant_id, attachment.get("id"), attachment.get("external_id"),
        retailer_id, getattr(connection, "meta_catalog_id", None),
        attachment.get("confidence"),
    )

    # Build the body text. Prefer the resolver's caption (already
    # contains title + price + short description in Arabic). Cap at
    # the sender's body limit — the sender will truncate again
    # defensively but we save bytes when the caption is huge.
    body_text = (
        attachment.get("caption")
        or attachment.get("title")
        or "تفضّل المنتج 👇"
    )

    try:
        result = await send_single_product_message(
            db=db,
            connection=connection,
            tenant_id=tenant_id,
            to=to,
            phone_id=phone_id,
            retailer_id=retailer_id,
            body_text=body_text,
            footer_text=None,
        )
    except Exception as send_exc:  # noqa: BLE001
        # The sender module is documented to NEVER raise for routine
        # failures — only programmer errors raise. Treat this as a
        # bug-grade issue and route to fallback so the conversation
        # still gets a reply.
        logger.error(
            "[CATALOG_FALLBACK_TEXT] tenant=%s product_id=%s "
            "reason=sender_exception err=%s",
            tenant_id, attachment.get("id"), send_exc,
        )
        return False

    if result.success:
        return True

    logger.info(
        "[CATALOG_FALLBACK_TEXT] tenant=%s product_id=%s reason=%s "
        "err=%s — routing to legacy image+CTA path",
        tenant_id, attachment.get("id"), result.reason, result.error,
    )
    return False


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


# ── Staff-call contact card sender ───────────────────────────────────────────
# WhatsApp Cloud API "contacts" payload renders as a vCard with native
# Call / Message / Save actions on tap — the closest thing to a "call
# button" in non-template messages (interactive cta_url forbids tel:
# URLs). Sends ONE message that can carry multiple cards if the LLM
# emitted multiple [CALL:...] markers.
#
# Feature-flagged via STAFF_CALL_MARKER_ENABLED (default ON). The flag
# is checked at the resolver call-site, not here — so once we decide
# to dispatch, we always do. Failure is non-fatal (logged + ignored)
# so a vCard outage never breaks the main reply path.
async def _send_contacts_message(
    phone_id: str, to: str, payload: Dict[str, Any],
    _tenant_id: Optional[int] = None, _db=None,
) -> bool:
    return await _post_wa(
        phone_id, payload, _tenant_id=_tenant_id, _db=_db
    )


def _staff_call_marker_enabled() -> bool:
    """Kill-switch for the [CALL:...] → contacts pipeline.

    Default ON for the rollout. Set ``STAFF_CALL_MARKER_ENABLED=false``
    in the host env to disable the resolver entirely — the LLM may
    still emit ``[CALL:...]`` but the marker will be scrubbed as a
    leak (via ``scrub_internal_markers``) and the customer sees only
    the plain reply text. No restart required, env is re-read on
    every reply.
    """
    import os as _os  # noqa: PLC0415
    raw = _os.getenv("STAFF_CALL_MARKER_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


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
