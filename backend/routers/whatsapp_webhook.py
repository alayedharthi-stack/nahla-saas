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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import anthropic
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from models import MessageEvent, WhatsAppConnection

from core.config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    COMMERCE_AGENT_V2_ENABLED,
    MERCHANT_BRAIN_ALLOW_LEGACY_FALLBACK,
    MERCHANT_BRAIN_ENABLED,
    MERCHANT_BRAIN_TENANT_IDS,
    META_APP_SECRET,
    META_WEBHOOK_ALLOW_MISSING_SIGNATURE,
    META_WEBHOOK_ENFORCE_SIGNATURE,
    ORCHESTRATOR_URL,
    WA_VERIFY_TOKEN,
)
from core.webhook_audit import record_result as _record_signature_audit
from core.webhook_security import (
    SignatureStatus,
    evaluate_replay_claim,
    mark_replay_completed,
    release_replay_nonce,
    verify_meta_signature,
)
from core import webhook_security as _security
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
    SHOW_PLAN_DETAILS,
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
from services.whatsapp_platform.provider_utils import WHATSAPP_PROVIDER_META, wa_provider
from core.acceptance_compose_observer import compose_stage as _acceptance_compose_stage
from core.acceptance_failure_injection import (
    maybe_inject_guard_failure as _acceptance_inject_guard_failure,
)
from core.acceptance_execution_context import capture_outbound_payload
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
from modules.ai.media.normalizer import inbound_persist_body, normalize_whatsapp_inbound
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
#   * computes token-set overlap of the NEW reply against EACH previous
#   * picks the highest overlap, classifies it into a tier
#
# Two-tier philosophy (May 2026 #34 — "pass-through, don't muzzle")
# ─────────────────────────────────────────────────────────────────
# v1 (#19) was a one-tier guard: any overlap ≥ 60% replaced the reply
# with a canned fallback line. In real merchant traffic that fired on
# perfectly legitimate re-asks ("كم سعره؟" → "كم سعره؟" right after,
# common with voice notes or customers returning to the conversation
# after a few minutes), making the bot feel cold and robotic.
#
# The merchant's spec for v2:
#   "إذا كان الرد مكررًا أو قريبًا من رد سابق، دع الـ LLM يرد طبيعيًا
#    … بدل إدخال fallback ثابت جاهز. dedup لا يمنع الإجابة، ولا
#    يستبدل الرد بجملة canned. بل يتحول إلى ‘خفف التكرار الحرفي فقط’
#    بدون قتل conversational flow."
#
# Implementation:
#   * SOFT tier (60% ≤ overlap < 85%): the LLM is repeating a topic
#     but with its own wording. Pass the reply through, log
#     ``[CHAT_DEDUP_SOFT]`` for telemetry. No replacement.
#   * HARD tier (overlap ≥ 85%): near-verbatim. This is the actual
#     loop case the original guard was built for. Replace with a
#     fallback so the customer doesn't see the same paragraph twice.
#   * ASSET-BEARING bypass: even on HARD overlap, if the reply
#     carries a URL / phone / `[MEDIA…]` / `[PRODUCT:]` / `[CALL:]`
#     marker, pass through. The asset itself is the new content —
#     the customer asking "ابي الباركود" twice deserves the barcode
#     both times, not a canned "we covered that already".

_DEDUP_OVERLAP_THRESHOLD       = 0.60   # SOFT — log only, don't replace
_DEDUP_HARD_OVERLAP_THRESHOLD  = 0.85   # HARD — actually replace
_DEDUP_MIN_TOKENS = 6                   # ignore very short replies — they always overlap
_DEDUP_LOOKBACK_OUTBOUND = 2            # how many recent outbound turns to check


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


def _max_outbound_overlap(new_reply: str, history: list) -> float:
    """Return the highest Jaccard-style overlap (0..1) between the
    NEW reply's token set and any of the last
    ``_DEDUP_LOOKBACK_OUTBOUND`` outbound messages in ``history``.

    Returns ``0.0`` when:
      * the new reply is too short (< _DEDUP_MIN_TOKENS),
      * no comparable previous outbound exists,
      * or any exception interrupts the walk (defensive default).

    The numerator is ``len(new ∩ prev)``, the denominator is
    ``len(new)`` — same shape the v1 guard used so the SOFT threshold
    keeps its empirical calibration.
    """
    new_tokens = _dedup_tokenise(new_reply)
    if len(new_tokens) < _DEDUP_MIN_TOKENS:
        return 0.0
    outbound_seen = 0
    best = 0.0
    try:
        for turn in reversed(history or []):
            direction = str((turn or {}).get("direction") or "").lower()
            if direction not in ("out", "outbound"):
                continue
            outbound_seen += 1
            prev_tokens = _dedup_tokenise(turn.get("body") or "")
            if len(prev_tokens) >= _DEDUP_MIN_TOKENS:
                overlap = len(new_tokens & prev_tokens) / max(1, len(new_tokens))
                if overlap > best:
                    best = overlap
            if outbound_seen >= _DEDUP_LOOKBACK_OUTBOUND:
                break
    except Exception:  # noqa: BLE001 silent-ok
        return 0.0
    return best


def _otp_apply_reply(
    tracker: Any,
    current: str,
    new: str,
    *,
    layer: str,
    op: str = "replace",
    text_written: Optional[bool] = None,
) -> str:
    """Apply a postprocess reply change and record it on the tracker."""
    new = str(new or "")
    if tracker is None:
        return new
    try:
        tracker.record_mutation(
            layer=layer,
            op=op,
            before=current or "",
            after=new,
            text_written=text_written,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — policy must not block send
        pass
    return new


def _otp_record_metadata_mutation(
    tracker: Any,
    current: str,
    *,
    layer: str,
    op: str = "noop",
) -> None:
    """Record a postprocess layer that emitted facts/metadata only."""
    if tracker is None:
        return
    try:
        tracker.record_mutation(
            layer=layer,
            op=op,
            before=current or "",
            after=current or "",
            text_written=False,
        )
        tracker.note(f"{layer}:metadata_only")
    except Exception:  # noqa: BLE001  # noqa: silent-ok — policy must not block send
        pass


def _of2_save_metadata(
    base: Dict[str, Any], *, turn_ref: str = "",
) -> Dict[str, Any]:
    """Attach this turn's timing snapshot to an OrderFlowV2 outbound save.

    The Brain path gets ``turn_timing`` through ``_otp_merge_save_metadata``;
    these two OrderFlowV2 saves never went through it, so an address turn
    persisted its provenance with no measurable latency beside it. Frequency
    and latency for this path therefore had no queryable source at all.
    Measurement only, and fail-open.
    """
    out = dict(base or {})
    # The inbound turn this reply answers. Persisting it is what lets the
    # captured payload, this stored metadata and the presentation receipt
    # be shown to describe ONE turn rather than three readings that merely
    # look compatible.
    if str(turn_ref or "").strip():
        out["address_turn_ref"] = str(turn_ref).strip()
    try:
        from core.turn_latency import (  # noqa: PLC0415
            get_turn_latency,
            merge_turn_latency_into_metadata,
        )

        merge_turn_latency_into_metadata(out, get_turn_latency())
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass
    return out


def _otp_merge_save_metadata(
    tracker: Any,
    persona_meta: Optional[Dict[str, Any]] = None,
    *,
    persona_compose_event: Optional[Dict[str, Any]] = None,
    brain_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    from core.outbound_text_policy import merge_policy_into_extra_metadata  # noqa: PLC0415
    from modules.ai.brain.persona.integration import (  # noqa: PLC0415
        merge_persona_compose_into_extra_metadata,
    )

    base = merge_persona_compose_into_extra_metadata(
        dict(persona_meta or {}),
        persona_compose_event,
    )
    try:
        from modules.ai.compose.reply_metadata_export import (  # noqa: PLC0415
            extract_reply_metadata_export,
        )

        brain_metadata = extract_reply_metadata_export(
            brain_result,
            chosen_path=str((brain_result or {}).get("chosen_path") or ""),
        )
        # Later owners may already have stamped post-compose transformations.
        # Brain metadata fills missing fields without overwriting that evidence.
        for key, value in brain_metadata.items():
            base.setdefault(key, value)
        ownership = base.get("persona_ownership")
        if isinstance(ownership, dict) and ownership.get("expression_owner"):
            base["final_expression_owner"] = ownership.get("expression_owner")
    except Exception:  # noqa: BLE001  # noqa: silent-ok — provenance must not block reply
        pass
    try:
        from modules.ai.brain.observability.url_context_trace import (  # noqa: PLC0415
            merge_url_context_trace_into_extra_metadata,
        )

        merge_url_context_trace_into_extra_metadata(base, brain_result)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — url context trace must not block reply
        pass
    # Measurement-only: attach turn_timing snapshot (never the live object).
    try:
        from core.turn_latency import (  # noqa: PLC0415
            get_turn_latency,
            merge_turn_latency_into_metadata,
        )

        merge_turn_latency_into_metadata(base, get_turn_latency())
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass
    if tracker is None:
        return base
    try:
        return merge_policy_into_extra_metadata(base, tracker.to_metadata())
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        return base


def _is_repeat_reply(
    new_reply: str,
    history: list,
    *,
    threshold: float = _DEDUP_HARD_OVERLAP_THRESHOLD,
) -> bool:
    """True when ``new_reply`` overlaps with a recent outbound at or
    above ``threshold``.

    Defaults to the HARD threshold so any caller that doesn't pass an
    explicit tier gets the conservative "only actual loops" behaviour.
    The webhook explicitly passes ``_DEDUP_OVERLAP_THRESHOLD`` (soft)
    when it wants to log a near-duplicate without replacing it.

    ``history`` is the same list the brain pipeline receives — each
    turn is ``{"direction": "in"/"inbound" | "out"/"outbound", "body": str}``.
    Robust to either spelling. Falls back to a safe ``False`` on any
    unexpected shape so the dedup never blocks a real reply.
    """
    if not new_reply:
        return False
    return _max_outbound_overlap(new_reply, history) >= float(threshold)


# Patterns used by ``_reply_carries_new_signal`` (May 2026 #34) to
# bypass the dedup REPLACE step when the reply contains material
# the customer hasn't seen yet (a URL, a phone number, or an
# asset marker that resolves to a CTA / media / contact card
# downstream). Module-level compile so we don't recompile per turn.
import re as _re_signal  # noqa: E402, PLC0415
_REPLY_SIGNAL_URL_RE   = _re_signal.compile(r"https?://\S+", _re_signal.IGNORECASE)
_REPLY_SIGNAL_PHONE_RE = _re_signal.compile(
    # Saudi mobile (+9665…, 9665…, 05…), and any international
    # number with at least 7 digits — same shape the asset-promise
    # sanitizer uses, so the two layers agree on what counts as
    # "a phone is in the reply".
    r"(?:\+?966|00966|0)?5\d{8}|\+\d{7,15}",
)
# Markers the LLM emits that resolve to attachments downstream.
# Listed verbatim so a grep over the code base finds them. Order
# is irrelevant — any one of them flips the signal to True.
_REPLY_SIGNAL_MARKERS  = ("[MEDIA:", "[MEDIA_KEY:", "[PRODUCT:", "[CALL:")


def _reply_carries_new_signal(reply: str) -> bool:
    """True when ``reply`` carries information the customer hasn't
    seen yet — even when the surrounding text is a near-duplicate of
    a previous outbound.

    Examples this guards against accidentally muting:
      * "تفضل باركود الراجحي 🌷\\nhttps://…" sent again because the
        customer re-asked "ابي الباركود".
      * "[PRODUCT:عسل السمر]" marker repeated after the customer
        asks "ورّني السمر" twice in a row.
      * "[CALL:+966500…|أبو هشام]" repeated when the customer asks
        for the staff number again.

    Pure — no DB / network. Tolerates ``None`` / empty input.
    """
    if not reply:
        return False
    if _REPLY_SIGNAL_URL_RE.search(reply):
        return True
    if _REPLY_SIGNAL_PHONE_RE.search(reply):
        return True
    for marker in _REPLY_SIGNAL_MARKERS:
        if marker in reply:
            return True
    return False


# P1-D-1: dedup must not substitute rotating canned personality text.
# Hard-tier near-duplicate handling uses ``context_aware_dedup_fallback``
# for operational state only; otherwise the outbound is suppressed.


def _dedup_operational_substitute(
    db,
    *,
    tenant_id: int,
    phone: str,
    history: list,
    inbound_text: str,
    inbound_metadata: dict | None,
    normalized_type: str | None,
    decision: object | None = None,
    decision_action: str = "",
    decision_args: dict | None = None,
) -> str:
    """Operational dedup fallback only — empty when no state-backed message."""
    try:
        from core.order_flow import context_aware_dedup_fallback  # noqa: PLC0415

        return context_aware_dedup_fallback(
            db,
            tenant_id=tenant_id,
            phone=phone,
            history=history,
            default_fallback="",
            inbound_text=inbound_text,
            inbound_metadata=inbound_metadata,
            normalized_type=normalized_type,
            decision=decision,
            decision_action=decision_action,
            decision_args=decision_args,
        )
    except Exception as _ctx_exc:  # noqa: BLE001
        logger.debug(
            "[CHAT_DEDUP] context-aware fallback failed: %s",
            _ctx_exc,
        )
        return ""


def _empty_reply_fallback() -> str:
    from core.fallback_policy import empty_reply_fallback  # noqa: PLC0415

    return empty_reply_fallback()


def _should_suppress_empty_outbound_reply(
    reply: str | None,
    *,
    brain_buttons: list | None = None,
    pending_attachments: list | None = None,
    require_substantive_commerce_text: bool = False,
) -> bool:
    """True when the final wire payload is empty or an unusable commerce remnant."""
    from core.outbound_final_boundary import should_suppress_final_outbound  # noqa: PLC0415

    return should_suppress_final_outbound(
        reply,
        brain_buttons=brain_buttons,
        pending_attachments=pending_attachments,
        require_substantive_commerce_text=require_substantive_commerce_text,
    )


def _log_empty_outbound_suppressed(
    *,
    tenant_id: int,
    to: str,
    conversation_id: int | None,
    reason: str,
) -> None:
    logger.info(
        "[OUTBOUND_SUPPRESSED_EMPTY_REPLY] tenant=%s to=%s conversation_id=%s reason=%s",
        tenant_id,
        to,
        conversation_id,
        reason,
    )


def _maybe_log_outbound_candidate_abort(
    *,
    tenant_id: int,
    conversation_id: int | None,
    customer_id: int | None,
    brain_candidate: str | None,
    final_reply: str | None,
    abort_reason: str,
    final_stage: str,
    suppressor: str | None = None,
    expression_owner: str | None = None,
    final_response_non_substantive: bool = False,
) -> None:
    """Structured audit when the turn ends without a useful outbound."""
    candidate = (brain_candidate or "").strip()
    if (final_reply or "").strip() and not final_response_non_substantive:
        return
    try:
        from core.outbound_abort_audit import log_outbound_candidate_abort  # noqa: PLC0415

        log_outbound_candidate_abort(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            generated_candidate_non_empty=bool(candidate),
            final_response_empty=not bool((final_reply or "").strip()),
            final_response_non_substantive=final_response_non_substantive,
            abort_reason=abort_reason,
            final_stage=final_stage,
            suppressor=suppressor,
            expression_owner=expression_owner,
            candidate_preview=candidate or None,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "[OUTBOUND_CANDIDATE_ABORT_AUDIT_FAILED] tenant=%s",
            tenant_id,
        )


# ── Smart notification helpers ────────────────────────────────────────────────

def _should_notify_merchant_email(
    *,
    db,
    tenant_id: int,
    customer,
    silence_hours: int = 24,
) -> dict:
    """First tenant-scoped customer contact only. ``silence_hours`` is ignored."""
    from services.merchant_first_contact import try_claim_first_contact  # noqa: PLC0415

    return try_claim_first_contact(db=db, tenant_id=tenant_id, customer=customer)


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

    Hardened with an always-200 safety contract —
    ALWAYS returns 200 even on parse / spawn / cancellation, so the
    middleware chain never ends with "No response returned".

    Phase 1B — Meta HMAC audit-mode
    ───────────────────────────────
    We now read the raw body BEFORE parsing JSON so we can verify
    ``X-Hub-Signature-256`` against ``META_APP_SECRET``. The verification
    result is recorded to ``core.webhook_audit`` for the operator
    dashboard. Whether to actually reject on a bad signature is gated by
    ``META_WEBHOOK_ENFORCE_SIGNATURE`` + ``META_WEBHOOK_ALLOW_MISSING_SIGNATURE``
    so we can ship audit-only first and only flip to enforce after a
    7-day clean window.
    """
    import asyncio as _asyncio  # noqa: PLC0415
    body: Dict[str, Any] = {}
    try:
        # Read raw body first — required for HMAC verification and still
        # parseable as JSON below.
        try:
            raw_body = await request.body()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[webhook/meta] raw-body read failed (returning 200): %s", exc)
            raw_body = b""

        sig_header = request.headers.get("X-Hub-Signature-256") or request.headers.get(
            "x-hub-signature-256"
        )
        result = verify_meta_signature(
            raw_body=raw_body,
            header_value=sig_header,
            secret=META_APP_SECRET or None,
        )
        try:
            _record_signature_audit(
                result,
                tenant_id=None,  # Meta inbound is platform-shared; tenant resolved later
                request_meta={
                    "ip": (request.headers.get("X-Real-IP")
                           or (request.client.host if request.client else None)),
                    "user_agent": request.headers.get("user-agent", "")[:120],
                    "signature_header_sample": sig_header,
                },
            )
        except Exception as exc:  # noqa: BLE001 — audit is best-effort
            logger.warning("[webhook/meta] audit record failed: %s", exc)

        if _meta_should_reject(result):
            try:
                from core.inbound_lifecycle import (  # noqa: PLC0415
                    EVENT_HTTP_SIGNATURE_REJECT, emit_standalone_event,
                )
                emit_standalone_event(
                    EVENT_HTTP_SIGNATURE_REJECT,
                    provider="meta",
                    detail=getattr(result, "status", ""),
                )
            except Exception:
                pass
            logger.warning(
                "[webhook/meta] rejecting request: status=%s detail=%s",
                result.status.value, result.detail,
            )
            # Even a "rejected" Meta webhook returns 200 to stop Meta's
            # retry storm — but we DO NOT process the body. Meta's retry
            # behaviour escalates aggressively on 4xx/5xx and an attacker
            # could DoS our logs by spamming 401s.
            return JSONResponse(
                {"status": "ignored", "reason": "signature_rejected"},
                status_code=200,
            )

        # Replay protection (Phase 1B-5) — flag-gated. ``evaluate_replay_claim``
        # is a no-op until ``WEBHOOK_REPLAY_PROTECTION_ENABLED=true``, and only
        # rejects when ``WEBHOOK_REPLAY_REJECT_ENABLED`` is ALSO true (the
        # audit-then-reject staging). It additionally reports whether *this*
        # request claimed the nonce, so a request that turns out not to be an
        # acceptance can give it back — see the 503 path below.
        try:
            import json as _json  # noqa: PLC0415
            body = _json.loads(raw_body) if raw_body else {}
            if not isinstance(body, dict):
                body = {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[webhook/meta] body parse failed (returning 200): %s", exc)
            body = {}

        _replay = evaluate_replay_claim(
            "meta",
            raw_body,
            request_meta={
                "ip": (request.headers.get("X-Real-IP")
                       or (request.client.host if request.client else None)),
                "user_agent": request.headers.get("user-agent", "")[:120],
            },
        )
        if _replay.reject and _replay.in_flight:
            # Another request with this exact body claimed the nonce and is
            # still deciding. Its outcome is not known, so nothing is
            # acknowledged on its behalf: the copy is answered retryable and
            # the provider's later redelivery finds either a completed nonce
            # (a replay) or an orphaned claim it may take over.
            logger.warning("[webhook/meta] duplicate body while the first copy is still in "
                           "flight — answering retryable, not acknowledging")
            return JSONResponse({"status": "retry", "reason": "replay_in_flight"},
                                status_code=503)
        if _replay.reject:
            # A completed nonce says a request with this body finished
            # deciding. Before it answers 200 on its own, every pilot-scoped
            # message in the body still has to be on record — the pilot's
            # obligations are durable rows, and a row is what proves one.
            _durable = await _durable_pilot_status(body, provider="meta")
            if _durable.retryable:
                logger.error(
                    "[COMMERCE_RUNTIME_ACCEPT] meta replay carries pilot-scoped work with "
                    "no durable acceptance scoped=%s undurable=%s undecidable=%s — a nonce "
                    "is not an acceptance; treating the request as a first attempt",
                    _durable.scoped, len(_durable.undurable), len(_durable.undecidable))
                _replay = _security.ReplayVerdict(reject=False, claimed=False, key=_replay.key)
            else:
                try:
                    from core.inbound_lifecycle import (  # noqa: PLC0415
                        EVENT_HTTP_REPLAY_REJECT, emit_standalone_event,
                    )
                    emit_standalone_event(
                        EVENT_HTTP_REPLAY_REJECT,
                        provider="meta",
                    )
                except Exception:
                    pass
                return JSONResponse(
                    {"status": "ignored", "reason": "replay"},
                    status_code=200,
                )

        # Durable before acknowledged. A 200 ends the provider's retries, so a
        # pilot-scoped message is written down first; if it cannot be, this
        # request is answered as *not* accepted and nothing is spawned, so the
        # redelivery finds none of the batch already processed. The signature
        # verdict travels in: a pilot obligation is taken only from a request
        # whose signature was **valid**, whatever the legacy path's audit mode
        # lets through for everything else.
        _accepted = await _accept_pilot_inbound(
            body, provider="meta", authenticated=bool(result.is_valid))
        if not _accepted.ok:
            # The work was not made durable, so this request is not an
            # acceptance and must not look like one. The nonce this request
            # claimed goes back with it: without that, the provider's identical
            # retry would be dropped as a replay — a 200 for a message nothing
            # ever processed, which is the exact silent loss the 503 exists to
            # prevent. Nothing is spawned either, so the redelivery finds no
            # part of the batch already processed.
            release_replay_nonce(_replay)
            logger.error("[COMMERCE_RUNTIME_ACCEPT] meta not acknowledged reason=%s "
                         "recorded=%s failed=%s replay_nonce_released=%s signature=%s",
                         _accepted.reason, len(_accepted.recorded),
                         len(_accepted.failed), _replay.claimed, result.status.value)
            return _pilot_refusal_response(_accepted, signature=result)

        try:
            from core.runtime_perf import spawn_background  # noqa: PLC0415
            spawn_background(_handle_whatsapp_body(body), name="webhook_meta")
        except Exception as exc:  # noqa: BLE001
            logger.exception("[webhook/meta] spawn_background failed: %s", exc)
        # This request has finished deciding: the pilot's obligations are
        # durable and processing is scheduled. From here on a copy of this
        # body is a replay. Until this line it was only a claim, and a claim
        # a dying process leaves behind is taken over by the retry.
        mark_replay_completed(_replay)
    except _asyncio.CancelledError:
        logger.warning(
            "[webhook/meta] client cancelled — returning 200 to protect "
            "the middleware chain",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[webhook/meta] unexpected handler error (returning 200): %s", exc)
    return JSONResponse({"status": "ok"}, status_code=200)


def _meta_should_reject(result) -> bool:
    """Decide whether to drop a Meta webhook based on its verification
    result and the audit-mode flags.

    Audit-mode (the default) NEVER rejects — we only log. Once
    ``META_WEBHOOK_ENFORCE_SIGNATURE`` is true:
      * VALID                 → process
      * INVALID               → reject
      * MISSING               → reject UNLESS ``META_WEBHOOK_ALLOW_MISSING_SIGNATURE`` is true
      * SECRET_NOT_CONFIGURED → process (single-secret deploy with empty env;
                                ops will see this in audit telemetry and
                                set the secret before flipping enforce)
    """
    if not META_WEBHOOK_ENFORCE_SIGNATURE:
        return False
    if result.status == SignatureStatus.VALID:
        return False
    if result.status == SignatureStatus.SECRET_NOT_CONFIGURED:
        return False
    if result.status == SignatureStatus.MISSING and META_WEBHOOK_ALLOW_MISSING_SIGNATURE:
        return False
    return True


# ── Retired provider: 360dialog ──────────────────────────────────────────────
# 360dialog is no longer a supported WhatsApp provider. Meta WhatsApp Cloud API
# is the only one.
#
# The URLs stay mounted and answer 410 Gone, on purpose. A merchant channel
# still configured in 360dialog would otherwise post into a 404 that says
# nothing, and the one outcome that must never follow is the body being read,
# accepted, acknowledged, persisted, processed in the background, or handed to
# the Meta path as though it had arrived there. This route does none of those:
# it reads no body, resolves no tenant, records no acceptance, spawns nothing
# and answers a status that names the retirement.

RETIRED_PROVIDER_PATHS = (
    "/webhook/whatsapp/360dialog",
    "/webhook/whatsapp/360dialog/coexistence",
    "/webhook/whatsapp/360dialog/status",
)

RETIRED_PROVIDER_REASON = "provider_retired"


@router.post("/webhook/whatsapp/360dialog")
@router.post("/webhook/whatsapp/360dialog/coexistence")
@router.post("/webhook/whatsapp/360dialog/status")
async def whatsapp_incoming_retired_provider(request: Request):
    """Refuse a delivery for the retired 360dialog integration.

    410 rather than 200: a 200 is an acceptance, and nothing here accepts
    anything. Nothing is parsed, nothing is stored and nothing is scheduled,
    so there is no path from this URL into the Meta pipeline.
    """
    logger.warning("[webhook/retired] refused a delivery for a retired provider path=%s",
                   request.url.path)
    return JSONResponse(
        {"status": "gone", "reason": RETIRED_PROVIDER_REASON,
         "detail": "360dialog is no longer supported; Meta WhatsApp Cloud API is the "
                   "only supported provider"},
        status_code=410,
    )


async def _accept_pilot_inbound(body: Dict[str, Any], *, provider: str,
                                authenticated: bool = False) -> Any:
    """Record every pilot-scoped message in this body before it is acknowledged.

    Off by default and free when the pilot is disabled. Never raises: an
    unexpected failure is reported as *not accepted*, which is the safe
    direction — the provider redelivers and the existing deduplication keeps
    the unaffected messages from being processed twice.

    ``authenticated`` is whether the provider's signature on this request was
    **valid**. Nothing pilot-scoped is recorded or processed without it.
    """
    import asyncio as _asyncio  # noqa: PLC0415

    from services.commerce_runtime_acceptance import (  # noqa: PLC0415
        Acceptance, record_before_acknowledging,
    )

    try:
        accepted = await _asyncio.to_thread(
            record_before_acknowledging, body, authenticated=bool(authenticated))
    except Exception as exc:  # noqa: BLE001
        logger.error("[COMMERCE_RUNTIME_ACCEPT] %s acceptance check failed: %s "
                     "— answering retryable", provider, type(exc).__name__)
        return Acceptance(accepted=False, reason=f"error:{type(exc).__name__}")
    if not accepted.ok:
        logger.error("[COMMERCE_RUNTIME_ACCEPT] %s not acknowledging: reason=%s failed=%s",
                     provider, accepted.reason, len(accepted.failed))
    return accepted


async def _durable_pilot_status(body: Dict[str, Any], *, provider: str) -> Any:
    """Whether every pilot-scoped message in this body is already on record.

    Asked when replay protection says the body was seen before. Never raises:
    what cannot be established is reported as *not durable*, so the request is
    treated as a first attempt rather than dropped on the strength of a nonce.
    """
    import asyncio as _asyncio  # noqa: PLC0415

    from services.commerce_runtime_acceptance import (  # noqa: PLC0415
        DurableStatus, durable_status,
    )

    try:
        return await _asyncio.to_thread(durable_status, body)
    except Exception as exc:  # noqa: BLE001
        logger.error("[COMMERCE_RUNTIME_ACCEPT] %s durable status failed: %s — a nonce "
                     "alone will not answer this request", provider, type(exc).__name__)
        return DurableStatus(undecidable=(f"error:{type(exc).__name__}",))


def _pilot_refusal_response(accepted: Any, *, signature: Any = None) -> JSONResponse:
    """The retryable answer for a request the pilot could not take, by reason.

    Every one of these is 503: the work was not made durable, so the provider
    must send it again. The reason names what an operator has to fix, and an
    unauthenticated pilot-scoped request is also written to the lifecycle
    audit as a signature rejection, because that is what it is.
    """
    from services.commerce_runtime_acceptance import (  # noqa: PLC0415
        REFUSED_RELEASED, REFUSED_UNAUTHENTICATED,
    )

    reason = "inbound_not_persisted"
    if accepted.reason == REFUSED_UNAUTHENTICATED:
        reason = "pilot_scope_unauthenticated"
        try:
            from core.inbound_lifecycle import (  # noqa: PLC0415
                EVENT_HTTP_SIGNATURE_REJECT, emit_standalone_event,
            )
            emit_standalone_event(
                EVENT_HTTP_SIGNATURE_REJECT, provider="meta",
                detail=f"pilot_scope:{getattr(getattr(signature, 'status', None), 'value', '')}",
            )
        except Exception:
            pass
    elif accepted.reason == REFUSED_RELEASED:
        reason = "pilot_released"
    return JSONResponse({"status": "retry", "reason": reason}, status_code=503)


async def _handle_whatsapp_body(body: Dict[str, Any]) -> None:
    # ── W2.0.1 (May 2026): Inbound-lifecycle telemetry wrap ─────────
    # Each Meta inbound message is wrapped in a per-message trace.
    # The context manager records EVENT_RECEIVED on entry and emits
    # the canonical [INBOUND_LIFECYCLE] summary on exit (success or
    # exception). Status events are NOT customer messages and stay
    # untraced — they go through the campaign delivery audit path.
    from core.inbound_lifecycle import inbound_lifecycle_trace  # noqa: PLC0415
    for entry in body.get("entry", []):
        waba_entry_id = str(entry.get("id") or "")
        for change in entry.get("changes", []):
            field = str(change.get("field") or "")
            value = change.get("value", {}) or {}
            phone_number_id = (value.get("metadata") or {}).get("phone_number_id", "")

            if field in {"smb_message_echoes", "history", "smb_app_state_sync", "account_update"}:
                try:
                    await _handle_meta_coexistence_change(
                        field=field,
                        value=value,
                        phone_number_id=str(phone_number_id or ""),
                        waba_id=waba_entry_id,
                    )
                except Exception:
                    logger.exception(
                        "[webhook/meta] coexistence field=%s failed phone_id=%s",
                        field, phone_number_id,
                    )
                continue

            for msg in value.get("messages", []):
                with inbound_lifecycle_trace(
                    provider="meta",
                    phone_number_id=phone_number_id,
                    msg=msg,
                ):
                    await _dispatch_message(phone_number_id, msg, value)
            for status in value.get("statuses", []):
                await _handle_message_status(status)


async def _handle_meta_coexistence_change(
    *,
    field: str,
    value: Dict[str, Any],
    phone_number_id: str,
    waba_id: str,
) -> None:
    db = next(get_db(), None)
    if not db:
        return
    try:
        wa_conn = None
        if phone_number_id:
            wa_conn = (
                db.query(WhatsAppConnection)
                .filter(WhatsAppConnection.phone_number_id == str(phone_number_id))
                .first()
            )
        if wa_conn is None and waba_id:
            wa_conn = (
                db.query(WhatsAppConnection)
                .filter(WhatsAppConnection.whatsapp_business_account_id == str(waba_id))
                .first()
            )
        if wa_conn is None:
            logger.info(
                "[webhook/meta] coexistence field=%s dropped — unknown phone_id=%s waba=%s",
                field, phone_number_id, waba_id,
            )
            return

        provider = str(getattr(wa_conn, "provider", "") or "").strip().lower()
        ctype = str(getattr(wa_conn, "connection_type", "") or "").strip().lower()
        if provider != "meta" or ctype != "embedded":
            logger.info(
                "[webhook/meta] coexistence field=%s dropped — not Meta embedded tenant=%s provider=%s type=%s",
                field, wa_conn.tenant_id, provider, ctype,
            )
            return

        from services.meta_coexistence import is_coexistence_mode  # noqa: PLC0415
        if field in {"history", "smb_app_state_sync", "smb_message_echoes", "account_update"} and not is_coexistence_mode(wa_conn):
            logger.info(
                "[webhook/meta] coexistence field=%s dropped — not Meta coexistence tenant=%s",
                field, wa_conn.tenant_id,
            )
            return

        if field == "smb_message_echoes":
            # Reuse the echo ingest; stamp provider from the connection.
            await _ingest_smb_message_echoes(db, wa_conn, value)
            db.commit()
            return

        if field == "history":
            _ingest_coexistence_history(db, wa_conn, value)
            db.commit()
            return

        if field == "smb_app_state_sync":
            contacts = value.get("state_sync") or []
            meta = dict(getattr(wa_conn, "extra_metadata", None) or {})
            meta["last_state_sync_at"] = datetime.now(timezone.utc).isoformat()
            meta["last_state_sync_count"] = len(contacts) if isinstance(contacts, list) else 0
            wa_conn.extra_metadata = meta
            db.commit()
            return

        if field == "account_update":
            event = str((value or {}).get("event") or "").strip().upper()
            if event == "PARTNER_REMOVED":
                wa_conn.status = "disconnected"
                wa_conn.sending_enabled = False
                wa_conn.last_error = "تم فصل حساب واتساب الأعمال من التطبيق."
                meta = dict(getattr(wa_conn, "extra_metadata", None) or {})
                meta["failure_code"] = "partner_removed"
                meta["partner_removed_at"] = datetime.now(timezone.utc).isoformat()
                wa_conn.extra_metadata = meta
                db.commit()
                logger.info(
                    "[webhook/meta] PARTNER_REMOVED tenant=%s phone_id=%s",
                    wa_conn.tenant_id, wa_conn.phone_number_id,
                )
            return
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            db.close()
        except Exception:
            pass


def _ingest_coexistence_history(db, wa_conn: WhatsAppConnection, value: Dict[str, Any]) -> None:
    """Persist history webhooks without Brain / automation / order mutation."""
    from routers.conversations import _get_or_create_conversation  # noqa: PLC0415
    from services.meta_coexistence import merge_coexistence_metadata  # noqa: PLC0415

    history = value.get("history") or []
    if not isinstance(history, list):
        return
    for chunk in history:
        errors = (chunk or {}).get("errors") or []
        if errors:
            codes = [err.get("code") for err in errors if isinstance(err, dict)]
            merge_coexistence_metadata(
                wa_conn,
                history_share_declined=2593109 in codes,
                history_share_errors=codes,
            )
            continue
        threads = (chunk or {}).get("threads") or []
        for thread in threads:
            customer_phone = str((thread or {}).get("id") or "")
            if not customer_phone:
                continue
            convo = _get_or_create_conversation(
                db, wa_conn.tenant_id, customer_phone,
                source="whatsapp_history_sync",
            )
            for msg in (thread or {}).get("messages") or []:
                wamid = str(msg.get("id") or "")
                if wamid:
                    try:
                        exists = (
                            db.query(MessageEvent)
                            .filter(
                                MessageEvent.tenant_id == wa_conn.tenant_id,
                                MessageEvent.event_type == "coexistence_history",
                            )
                            .order_by(MessageEvent.id.desc())
                            .limit(50)
                            .all()
                        )
                        if any(
                            str((row.extra_metadata or {}).get("message_id") or "") == wamid
                            for row in exists
                        ):
                            continue
                    except Exception:
                        pass
                msg_type = str(msg.get("type") or "text")
                body_text = ""
                if msg_type == "text":
                    body_text = str(((msg.get("text") or {}).get("body")) or "")
                elif msg_type != "media_placeholder":
                    body_text = str(((msg.get(msg_type) or {}).get("caption")) or "") or f"[{msg_type}]"
                from_phone = str(msg.get("from") or "")
                direction = "outbound" if from_phone and from_phone != customer_phone else "inbound"
                db.add(MessageEvent(
                    conversation_id=convo.id,
                    tenant_id=wa_conn.tenant_id,
                    direction=direction,
                    body=body_text,
                    event_type="coexistence_history",
                    extra_metadata={
                        "message_id": wamid,
                        "source": "coexistence_history",
                        "historical_only": True,
                        "historical_import": True,
                        "message_origin": "coexistence_history",
                        "phone_number_id": wa_conn.phone_number_id,
                    },
                ))
    merge_coexistence_metadata(
        wa_conn,
        last_history_sync_at=datetime.now(timezone.utc).isoformat(),
    )


def _sanitize_status_webhook_errors(errors: Any) -> List[Dict[str, Any]]:
    """Extract safe diagnostic fields from Meta status errors[]."""
    if not isinstance(errors, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in errors:
        if not isinstance(item, dict):
            out.append({"raw": str(item)[:500]})
            continue
        row: Dict[str, Any] = {}
        for key in ("code", "title", "message", "error_subcode", "href"):
            val = item.get(key)
            if val is not None:
                row[key] = val
        details = item.get("error_data") or item.get("details")
        if details is not None:
            row["details"] = details
        out.append(row)
    return out


async def _handle_message_status(status: Dict[str, Any]) -> None:
    """Process the four WhatsApp delivery-status webhook events from
    Meta (``sent``, ``delivered``, ``read``, ``failed``)
    and persist them against the matching ``CampaignSendLog`` row.

    Two-layer attribution
    ─────────────────────
    A status event identifies its target by ``wamid`` (a.k.a.
    ``provider_message_id``). We look it up in two places:

      1. ``CampaignSendLog`` keyed by ``provider_message_id`` —
         this is where the campaign dispatcher writes the wamid
         when Meta accepts a template send. It's the authoritative
         row for delivery analytics.
      2. ``MessageEvent`` keyed by legacy ``extra_metadata.wa_message_id``
         or canonical ``extra_metadata.provider_send.wamid`` — kept so
         that pre-migration rows and one-off
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

        # ── Resolve tenant_id BEFORE calling the quality recorder ──
        # message_delivery_events.tenant_id is NOT NULL. Three resolution
        # layers, fail-open at the end:
        #   1. CampaignSendLog.tenant_id     (campaign sends)
        #   2. MessageEvent.tenant_id        (AI replies + manual agent
        #                                     outbound; wamid lives in
        #                                     extra_metadata.wa_message_id)
        #   3. None                          → skip the analytics insert
        #                                     entirely (defensive guard
        #                                     inside record_status_event
        #                                     also enforces this).
        #
        # We materialise evt_row here so Layer 2 can also reuse it below
        # without a second query. Layer 2 was previously fetched AFTER
        # the quality recorder ran, which caused every non-campaign
        # delivery webhook to trigger a NotNullViolation that poisoned
        # the entire transaction (logged as `[StatusWebhook] error
        # processing status: ... null value in column "tenant_id" of
        # relation "message_delivery_events"`).
        from sqlalchemy import or_  # noqa: PLC0415

        evt_row = (
            db.query(MessageEvent)
            .filter(or_(
                MessageEvent.extra_metadata["wa_message_id"].astext == wamid,
                MessageEvent.extra_metadata["provider_send"]["wamid"].astext == wamid,
            ))
            .first()
        )
        resolved_tenant_id: Optional[int] = None
        if log_row and log_row.tenant_id:
            resolved_tenant_id = log_row.tenant_id
        elif evt_row and evt_row.tenant_id:
            resolved_tenant_id = evt_row.tenant_id

        if st == "failed":
            logger.info(
                "[PAYMENT_MEDIA_DIAG] status_failed wamid=%s status=%s "
                "recipient_id=%s timestamp=%s tenant_id=%s "
                "message_event=%s campaign_send_log=%s errors=%s",
                wamid,
                st,
                status.get("recipient_id"),
                status.get("timestamp"),
                resolved_tenant_id,
                "matched" if evt_row else "orphan",
                "matched" if log_row else "orphan",
                _sanitize_status_webhook_errors(status.get("errors")),
            )

        # ── Delivery Quality Intelligence Layer (May 2026) ──
        # Best-effort append-only event capture. NEVER let this fail
        # the existing dispatcher flow — wrapped in try/except, runs
        # against the same `db` session so commit/rollback below
        # naturally cleans up if needed. Skips silently when no tenant
        # can be resolved (the service refuses None defensively).
        try:
            from services.delivery_quality import record_status_event  # noqa: PLC0415
            record_status_event(
                db=db,
                tenant_id=resolved_tenant_id,
                wamid=wamid,
                status=st,
                phone_e164=(log_row.phone_e164 if log_row else None),
                errors_payload=status.get("errors"),
                campaign_send_log_id=(log_row.id if log_row else None),
                source="meta",
            )
        except Exception as exc:
            logger.debug("[StatusWebhook] quality recorder failed: %s", exc)
            # Defensive: a flush inside record_status_event that managed
            # to poison the session would block every downstream query
            # in this handler. Roll back to a clean state and reopen.
            try:
                db.rollback()
            except Exception:
                pass
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
        #            tiles that read .delivered_count / .read_count).
        # evt_row was already materialised at the top of the handler
        # so the quality recorder could resolve a fallback tenant_id
        # for non-campaign sends.
        if evt_row:
            meta = dict(evt_row.extra_metadata or {})
            campaign_id = meta.get("campaign_id") or log_campaign_id
            already_key = f"_status_{st}"
            if campaign_id and not meta.get(already_key):
                campaign = (
                    db.query(Campaign)
                    .filter(
                        Campaign.id == int(campaign_id),
                        Campaign.tenant_id == evt_row.tenant_id,
                    )
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
            # Every matched outbound row carries its latest receipt for the
            # conversation renderer, including non-campaign/manual/AI sends.
            # Idempotent receipt flags preserve the existing campaign counter
            # guards; read implies delivered even when Meta coalesces events.
            meta[already_key] = True
            if st == "read":
                meta["_status_delivered"] = True
            current_delivery = str(meta.get("delivery_status") or "").lower()
            if current_delivery != "read" or st == "read":
                meta["delivery_status"] = st
            if st == "failed":
                meta["delivery_error"] = _sanitize_status_webhook_errors(
                    status.get("errors"),
                )
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


# Media types we can download + materialise as proper media bubbles in the
# dashboard. Anything outside this set falls back to a friendly placeholder
# string (May 2026 P1 fix — Tenant 33 saw "[merchant_image]" literal text
# instead of the actual image when the owner replied from the mobile app).
_SMB_ECHO_MEDIA_TYPES: tuple[str, ...] = ("image", "video", "audio", "document")

# Display copy for echo types we can't decode (sticker, location, contacts,
# interactive, "unsupported"). Keeps the merchant-facing string readable
# rather than the cryptic ``[merchant_unsupported]`` bracket form.
_SMB_ECHO_UNSUPPORTED_DISPLAY = "📎 رسالة من تطبيق الجوال — صيغة غير مدعومة"


async def _ingest_smb_message_echoes(db, wa_conn: WhatsAppConnection, value: Dict[str, Any]) -> None:
    """Persist merchant-mobile (Coexistence) echoes into the conversation.

    May 2026 P1 (Tenant 33): pre-fix this function only handled
    ``type == "text"`` and stamped ``f"[merchant_{msg_type}]"`` into the
    body for everything else, so images / videos / documents the
    merchant sent from his mobile app surfaced in Nahla as the literal
    placeholder string ``[merchant_image]`` instead of the actual media.
    The fix mirrors what ``_process_image`` (in
    ``modules/ai/media/normalizer.py``) already does for inbound
    customer images: download the binary via the same
    ``_download_meta_media`` helper, persist via ``save_inbound_media``,
    and stamp ``extra_metadata.normalized_inbound`` so the dashboard's
    ``_build_media_block`` can serialise it as a real media row.

    May 2026 #37: this branch used to crash with
    ``OperationalError: statement timeout`` on the implicit
    ``UPDATE customers SET last_interaction_at=…`` that
    :func:`_get_or_create_conversation` triggered (it forwards
    ``source="whatsapp_inbound"`` by default). Echoes are
    OUTBOUND from the merchant's app, not customer-driven inbound,
    so they MUST NOT bump the timestamp — and we no longer want
    a single timed-out UPDATE to take down the whole batch. Two
    fixes:
      1. Forward ``source="whatsapp_outbound_echo"`` so the
         CustomerIntelligenceService skips the timestamp UPDATE.
      2. Wrap the per-echo flush in a savepoint so a timeout (or
         any OperationalError) on one echo doesn't poison the
         outer batch transaction; we log telemetry and continue.
    """
    from routers.conversations import _get_or_create_conversation  # noqa: PLC0415
    from sqlalchemy.exc import OperationalError  # noqa: PLC0415

    phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
    for echo in value.get("message_echoes", []) or []:
        to_phone = str(echo.get("to") or "")
        msg_type = str(echo.get("type") or "")
        if not to_phone:
            continue

        convo = _get_or_create_conversation(
            db, wa_conn.tenant_id, to_phone,
            source="whatsapp_outbound_echo",
        )

        # Extras stamped on every echo — kept identical across branches
        # so downstream consumers can rely on the shape.
        extra: Dict[str, Any] = {
            "customer_phone": to_phone,
            "phone": to_phone,
            "provider": getattr(wa_conn, "provider", None) or WHATSAPP_PROVIDER_META,
            "phone_number_id": phone_number_id,
            "message_id": echo.get("id"),
            "source": "merchant_mobile_app",
            "echo_type": msg_type,
        }
        body_text = ""

        if msg_type == "text":
            body_text = str(((echo.get("text") or {}).get("body")) or "")

        elif msg_type in _SMB_ECHO_MEDIA_TYPES:
            # Per the WhatsApp Cloud API spec, every media echo carries
            # the same sub-object shape as an inbound media message —
            # i.e. ``echo["image"] = {"id": "...", "mime_type": "...",
            # "caption": "...", "sha256": "..."}``. ``echo["video"]``,
            # ``echo["audio"]``, ``echo["document"]`` follow the same
            # pattern.
            media_block = echo.get(msg_type) or {}
            media_id   = str(media_block.get("id") or "")
            mime_type  = str(media_block.get("mime_type") or "")
            caption    = str(media_block.get("caption") or "")
            sha256_hex = str(media_block.get("sha256") or "")
            body_text  = caption  # may be empty — that's fine

            # Try to fetch + persist the binary so it renders as a real
            # bubble. If anything fails we fall back to a friendly
            # placeholder so the merchant at least sees that something
            # arrived (the old behaviour just dropped the brackets in).
            stored_url: Optional[str] = None
            storage_status = "missing_media_id"
            byte_size: Optional[int] = None

            if media_id:
                storage_status = "download_failed"
                try:
                    from modules.ai.media.normalizer import _download_meta_media  # noqa: PLC0415
                    from services.inbound_media_storage import save_inbound_media  # noqa: PLC0415

                    download = await _download_meta_media(
                        db=db, wa_conn=wa_conn,
                        tenant_id=wa_conn.tenant_id,
                        media_id=media_id, mime_type=mime_type,
                    )
                    if download and download.get("bytes"):
                        binary = download["bytes"]
                        eff_mime = download.get("mime_type") or mime_type or "application/octet-stream"
                        byte_size = len(binary)
                        stored = save_inbound_media(
                            kind=msg_type,
                            tenant_id=int(wa_conn.tenant_id),
                            binary=binary,
                            mime_type=eff_mime,
                            sha256_hex=sha256_hex or None,
                        )
                        if stored is not None:
                            stored_url = getattr(stored, "storage_url", None)
                            storage_status = "ok"
                            extra["normalized_inbound"] = {
                                "source_type": msg_type,
                                "storage_url": stored_url,
                                "mime_type": eff_mime,
                                "byte_size": byte_size,
                                "storage_sha256": getattr(stored, "storage_sha256", sha256_hex) or sha256_hex,
                                "caption": caption,
                                "direction": "outbound",
                                "echo_source": "merchant_mobile_app",
                                "image_download_status": "ok",
                            }
                except Exception as exc:  # noqa: BLE001
                    storage_status = "exception"
                    logger.warning(
                        "[SMB_ECHO_MEDIA] tenant=%s media_id=%s mime=%s err=%s",
                        wa_conn.tenant_id, media_id, mime_type, exc,
                    )

            extra["media_id"] = media_id or None
            extra["media_mime_type"] = mime_type or None
            extra["media_storage_status"] = storage_status

            logger.info(
                "[SMB_ECHO] tenant=%s to=%s type=%s media_id=%s status=%s "
                "bytes=%s caption_len=%d",
                wa_conn.tenant_id, to_phone, msg_type, media_id or "-",
                storage_status, byte_size if byte_size is not None else "-",
                len(caption),
            )

            # When download succeeded, leave ``body_text`` as the caption
            # (often empty). When it failed, surface a readable
            # placeholder so the merchant sees something landed.
            if storage_status != "ok" and not body_text:
                body_text = f"📎 رسالة {msg_type} من تطبيق الجوال"

        else:
            # sticker / location / contacts / interactive / "unsupported"
            # — nothing we can render, but use a friendlier placeholder
            # than ``[merchant_unsupported]``.
            body_text = _SMB_ECHO_UNSUPPORTED_DISPLAY
            extra["media_storage_status"] = "unsupported_type"
            logger.info(
                "[SMB_ECHO] tenant=%s to=%s type=%s status=unsupported_type",
                wa_conn.tenant_id, to_phone, msg_type,
            )

        db.add(MessageEvent(
            conversation_id=convo.id,
            tenant_id=wa_conn.tenant_id,
            direction="outbound",
            body=body_text,
            event_type="smb_message_echo",
            extra_metadata=extra,
        ))
        convo.status = "active"
        db.add(convo)

        # Per-echo telemetry — answers "did this echo touch the
        # customer row, and how long did it take?". The
        # ``last_interaction_at_skipped`` flag is True by default
        # for echoes; if the future flips an echo back to inbound-
        # like semantics, this stays observable.
        logger.info(
            "[LAST_INTERACTION] tenant=%s customer_id=%s family=coexistence "
            "type=%s direction=outbound source=whatsapp_outbound_echo "
            "last_interaction_at_skipped=True",
            wa_conn.tenant_id,
            getattr(convo, "customer_id", None),
            msg_type or "unknown",
        )

    # Best-effort flush — under contention the bare UPDATE
    # statement on ``customers`` (e.g. via name updates from
    # ``upsert_customer_identity``) can hit the 5s
    # ``statement_timeout`` and surface as a QueryCanceled
    # OperationalError. Pre-fix the whole coexistence batch
    # rolled back because of one slow row; we now isolate the
    # failure to the per-echo branch via the outer try in
    # :func:`whatsapp_webhook` and surface a structured warning
    # so production triage can chart how often it fires without
    # it costing us echoes.
    flush_started = time.monotonic()
    try:
        db.flush()
        logger.info(
            "[LAST_INTERACTION] flush=ok tenant=%s duration_ms=%d",
            wa_conn.tenant_id,
            int((time.monotonic() - flush_started) * 1000),
        )
    except OperationalError as exc:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(
            "[LAST_INTERACTION] flush=timeout_or_op_error tenant=%s "
            "duration_ms=%d err=%s — dropping echo batch, pipeline "
            "stays alive",
            wa_conn.tenant_id,
            int((time.monotonic() - flush_started) * 1000),
            exc,
        )
        return


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

    # ── [INBOUND_MEDIA_RAW] — May 2026 #41 ────────────────────────────
    # Single grep-able line for EVERY inbound payload before any
    # routing / dedup / tenant-resolution gate fires. Surfaces the
    # exact shape that Meta delivered so on-call can
    # answer "did the message even reach the webhook?" with a single
    # log query, no DB hit required. Never raises — this is pure
    # observability and must not affect message delivery.
    try:
        _media_block = msg.get(msg_type) if isinstance(msg.get(msg_type), dict) else None
        _raw_caption = (_media_block or {}).get("caption") or ""
        logger.info(
            "[INBOUND_MEDIA_RAW] phone_number_id=%s from=%s msg_id=%s "
            "msg_type=%s mime=%s media_id=%s has_caption=%s "
            "has_text_block=%s payload_keys=%s",
            phone_number_id, sender, msg_id or "-",
            msg_type or "-",
            (_media_block or {}).get("mime_type") or "-",
            (_media_block or {}).get("id") or "-",
            bool(_raw_caption.strip()),
            bool(msg.get("text")),
            ",".join(sorted(msg.keys())),
        )
    except Exception:
        pass

    if not phone_number_id:
        logger.error(
            "[Webhook] DROPPED — phone_number_id missing from metadata. "
            "msg_type=%s from=%s msg_id=%s",
            msg_type, sender, msg_id,
        )
        try:
            from core.inbound_lifecycle import (  # noqa: PLC0415
                EVENT_MISSING_PHONE_ID, EVENT_END_DROPPED, record_lifecycle,
            )
            record_lifecycle(EVENT_MISSING_PHONE_ID)
            record_lifecycle(EVENT_END_DROPPED)
        except Exception:
            pass
        return

    # ── Strict in-memory dedup — runs BEFORE the conversation lock ─────────
    # Meta retries inbound webhooks (same `msg_id`) within
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
                if _duplicate_is_unfinished_runtime_work(
                    phone_number_id=phone_number_id, sender=sender, msg_id=msg_id,
                ):
                    # Not a second answer: the commerce runtime admitted this
                    # exact inbound message and never finished it, and this
                    # retry is the only event that can reach that turn. Nothing
                    # is resent — the delivery ledger still refuses to dispatch
                    # an attempt whose outcome is pending, accepted or unknown.
                    logger.warning(
                        "[Idempotency] ALLOW duplicate inbound for commerce-runtime recovery "
                        "msg_id=%s phone_number_id=%s from=%s",
                        msg_id, phone_number_id, sender,
                    )
                else:
                    logger.info(
                        "[Idempotency] DROP duplicate inbound (early/in-memory) "
                        "msg_id=%s phone_number_id=%s from=%s — provider retry, "
                        "skipping conversation lock + DB",
                        msg_id, phone_number_id, sender,
                    )
                    try:
                        from core.inbound_lifecycle import (  # noqa: PLC0415
                            EVENT_DEDUP_DROP_MEMORY, EVENT_END_DROPPED,
                            record_lifecycle,
                        )
                        record_lifecycle(EVENT_DEDUP_DROP_MEMORY)
                        record_lifecycle(EVENT_END_DROPPED)
                    except Exception:
                        pass
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
        try:
            from core.inbound_lifecycle import (  # noqa: PLC0415
                EVENT_DB_SESSION_FAIL, EVENT_END_DROPPED, record_lifecycle,
            )
            record_lifecycle(EVENT_DB_SESSION_FAIL)
            record_lifecycle(EVENT_END_DROPPED)
        except Exception:
            pass
        return

    # Lock state-vars: declared before the try so the finally can always
    # safely test / release them, regardless of how far we got.
    _conv_lock_cm = None
    _conv_lock_active = False
    # Latency observability (measurement-only). Bound before lock so wait/hold
    # can correlate; reset in finally. Never affects reply behavior.
    _turn_latency = None
    _turn_latency_token = None
    _tenant_resolution_t0 = None

    try:
        # ── Resolve tenant from phone_number_id (must be exactly 1 match) ────────
        try:
            import time as _time_lat  # noqa: PLC0415

            _tenant_resolution_t0 = _time_lat.monotonic()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            _tenant_resolution_t0 = None
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
            try:
                from core.inbound_lifecycle import (  # noqa: PLC0415
                    EVENT_UNKNOWN_PHONE_ID, EVENT_END_DROPPED,
                    record_lifecycle,
                )
                record_lifecycle(EVENT_UNKNOWN_PHONE_ID)
                record_lifecycle(EVENT_END_DROPPED)
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
            try:
                from core.inbound_lifecycle import (  # noqa: PLC0415
                    EVENT_AMBIGUOUS_PHONE_ID, EVENT_END_DROPPED,
                    record_lifecycle,
                )
                record_lifecycle(
                    EVENT_AMBIGUOUS_PHONE_ID,
                    detail=f"matches={len(wa_matches)}",
                )
                record_lifecycle(EVENT_END_DROPPED)
            except Exception:
                pass
            return

        wa_conn = wa_matches[0]
        # ── W2.0.1 (May 2026): tenant_resolved attaches tenant_id to
        # the trace so the summary line surfaces it without a DB
        # round-trip when on-call is grepping for a specific tenant.
        try:
            from core.inbound_lifecycle import attach_tenant  # noqa: PLC0415
            attach_tenant(getattr(wa_conn, "tenant_id", None))
        except Exception:
            pass

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
        # Measurement-only: bind turn latency before conversation_lock so
        # wait_ms / held_ms land on the same correlated object.
        try:
            from core.turn_latency import (  # noqa: PLC0415
                bind_turn_latency,
                new_turn_latency,
                safe_record_ms,
            )
            import time as _time_lat2  # noqa: PLC0415

            _turn_latency = new_turn_latency(
                tenant_id=int(resolved_tenant_id),
                message_id=str(msg_id or ""),
            )
            _turn_latency_token = bind_turn_latency(_turn_latency)
            if _tenant_resolution_t0 is not None:
                safe_record_ms(
                    "tenant_resolution",
                    (_time_lat2.monotonic() - _tenant_resolution_t0) * 1000.0,
                )
            from core.turn_latency import safe_mark_webhook_pre_persist_start  # noqa: PLC0415

            safe_mark_webhook_pre_persist_start()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            _turn_latency = None
            _turn_latency_token = None

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
                    if _duplicate_is_unfinished_runtime_work(
                        phone_number_id=phone_number_id, sender=sender, msg_id=msg_id,
                    ):
                        # See the in-memory guard above: this retry is the only
                        # event that can finish a commerce-runtime turn nobody
                        # closed, and letting it through resends nothing.
                        logger.warning(
                            "[Idempotency] ALLOW duplicate inbound for commerce-runtime recovery "
                            "msg_id=%s tenant=%s from=%s",
                            msg_id, resolved_tenant_id, sender,
                        )
                    else:
                        logger.info(
                            "[Idempotency] DROP duplicate inbound msg_id=%s "
                            "tenant=%s from=%s — Meta webhook retry",
                            msg_id, resolved_tenant_id, sender,
                        )
                        try:
                            from core.inbound_lifecycle import (  # noqa: PLC0415
                                EVENT_DEDUP_DROP_DB, EVENT_END_DROPPED,
                                record_lifecycle,
                            )
                            record_lifecycle(EVENT_DEDUP_DROP_DB)
                            record_lifecycle(EVENT_END_DROPPED)
                        except Exception:
                            pass
                        return
                IdempotencyGuard.mark_processed(inbound_dedup_state, msg_id)
                StateManager.save(
                    db, inbound_dedup_state, tenant_id=resolved_tenant_id,
                )
                # ── W2.0.1 (May 2026): the dedup mark was just
                # committed BEFORE any Conversation / MessageEvent for
                # this inbound. If anything below crashes silently,
                # the next provider retry will hit the duplicate-drop
                # branch above with zero rows written. The trace
                # records both halves so the summary tells operators
                # exactly when the mark fired vs. the message saved.
                try:
                    from core.inbound_lifecycle import (  # noqa: PLC0415
                        EVENT_DEDUP_MARKED, record_lifecycle,
                    )
                    record_lifecycle(EVENT_DEDUP_MARKED)
                except Exception:
                    pass
            except Exception as _dedup_exc:
                # Never block real traffic on a dedup-table hiccup. Log
                # at WARNING (not DEBUG) so a repeated failure surfaces.
                logger.warning(
                    "[Idempotency] guard load/save failed tenant=%s msg_id=%s "
                    "err=%s — proceeding without dedup for this message",
                    resolved_tenant_id, msg_id, _dedup_exc,
                )

        normalized_sender = normalize_phone(sender) or sender
        try:
            from core.wa_usage import record_customer_inbound_window  # noqa: PLC0415
            record_customer_inbound_window(
                db,
                resolved_tenant_id,
                normalized_sender,
                inbound_at=_wa_msg_ts,
                commit=True,
            )
        except Exception as _win_exc:
            logger.warning(
                "[Webhook] record_customer_inbound_window failed tenant=%s err=%s",
                resolved_tenant_id,
                _win_exc,
            )
        contact_name = _extract_contact_name(value, sender)
        _inbound_customer_id: int | None = None
        try:
            _lead_meta: dict = {
                "channel": "whatsapp",
                "phone_number_id": phone_number_id,
                "provider": wa_provider(wa_conn),
            }
            if contact_name:
                _lead_meta["wa_profile_name"] = contact_name
            _lead = CustomerIntelligenceService(db, resolved_tenant_id).upsert_lead_customer(
                phone=normalized_sender,
                name=contact_name or None,
                source="whatsapp_inbound",
                extra_metadata=_lead_meta,
                commit=True,
            )
            if _lead:
                _inbound_customer_id = _lead.id
            # Identity belongs to inbound ingestion, before any AI-reply gate.
            # Tenant resolution, the conversation lock, and both replay guards
            # have already run. Use the resolved customer, never a name lookup.
            # Only original live customer text may establish self-report evidence;
            # media descriptions, history, buttons and outbound echoes may not.
            if _lead and not _hist_skip_live and msg_type == "text":
                try:
                    from core.customer_name_extractor import extract_high_confidence_name  # noqa: PLC0415
                    from core.customer_identity_resolver import apply_customer_name  # noqa: PLC0415

                    _name_text = (msg.get("text") or {}).get("body", "")
                    _name_hit = extract_high_confidence_name(_name_text)
                    if _name_hit:
                        apply_customer_name(
                            _lead, _name_hit.value, source="ai_detected_name",
                            explicit_customer_entry=True,
                            message_context={
                                "message": _name_text,
                                "wa_message_id": msg_id,
                                "name_capture_pattern": _name_hit.pattern,
                            },
                        )
                        # Also commit blocked-attempt provenance; a True return
                        # alone does not mean the canonical name changed.
                        db.commit()
                except Exception:  # noqa: BLE001 — identity failure must not lose the inbound message
                    db.rollback()
                    logger.warning(
                        "[NAME_EXTRACTOR] inbound capture failed tenant=%s customer=%s",
                        resolved_tenant_id, _inbound_customer_id,
                    )
            if _lead and _hist_skip_live:
                try:
                    from services.merchant_first_contact import (  # noqa: PLC0415
                        suppress_first_contact,
                    )
                    suppress_first_contact(
                        db=db,
                        tenant_id=resolved_tenant_id,
                        customer=_lead,
                    )
                except Exception as _hist_stamp_exc:
                    logger.warning(
                        "[Webhook] history first-contact suppress failed: %s",
                        _hist_stamp_exc,
                    )

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

            # First-contact email before unsubscribe return so a new customer's
            # first inbound (even an opt-out keyword) still notifies the merchant.
            if not _hist_skip_live:
                try:
                    from services.merchant_first_contact import (  # noqa: PLC0415
                        maybe_notify_first_customer,
                    )
                    _msg_text = ""
                    if msg_type == "text":
                        _msg_text = (msg.get("text") or {}).get("body", "")
                    maybe_notify_first_customer(
                        db=db,
                        tenant_id=resolved_tenant_id,
                        customer=_lead,
                        customer_phone=normalized_sender,
                        message_preview=_msg_text,
                        log_notification=_log_notification,
                    )
                except Exception as _em:
                    logger.warning("[Webhook] first-contact email error: %s", _em)

            if _unsub_short_circuit:
                # Skip automation / AI for unsubscribe-related events.
                try:
                    from core.inbound_lifecycle import (  # noqa: PLC0415
                        EVENT_UNSUB_SHORT_CIRCUIT, EVENT_END_DROPPED,
                        record_lifecycle,
                    )
                    record_lifecycle(EVENT_UNSUB_SHORT_CIRCUIT)
                    record_lifecycle(EVENT_END_DROPPED)
                except Exception:
                    pass
                return

            if not _hist_skip_live:
                track_conversation(
                    db,
                    resolved_tenant_id,
                    normalized_sender,
                    source="inbound",
                    category="service",
                    inbound_at=_wa_msg_ts,
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

        # ── Normalize with safe-stub fallback (May 2026 #41) ────────
        # Any unhandled exception inside the normalizer must NOT
        # silently drop the inbound — pre-fix, a normalizer crash
        # bubbled up the dispatch try/except and the message
        # vanished from the merchant inbox with only a warning log.
        # We catch + persist a minimal placeholder so the
        # conversation row always exists; the merchant can then
        # ask the customer to retype while ops triages the
        # exception offline.
        try:
            normalized_inbound = await normalize_whatsapp_inbound(
                db=db,
                wa_conn=wa_conn,
                tenant_id=resolved_tenant_id,
                message=msg,
                order_context=_order_context,
            )
            # ── W2.0.1 (May 2026): normalizer happy path — pin the
            # outcome on the trace so the summary line surfaces the
            # normalized type / text length / fallback flag without
            # parsing free-form logs.
            try:
                from core.inbound_lifecycle import (  # noqa: PLC0415
                    attach_normalizer_outcome,
                )
                attach_normalizer_outcome(
                    normalized_type=getattr(normalized_inbound, "normalized_type", None),
                    text_len=len(getattr(normalized_inbound, "text", "") or ""),
                    fallback_set=bool(
                        getattr(normalized_inbound, "fallback_reply_ar", "") or ""
                    ),
                )
            except Exception:
                pass
        except Exception as _norm_exc:  # noqa: BLE001
            logger.error(
                "[INBOUND_MEDIA_ERROR] normalize_whatsapp_inbound raised | "
                "tenant_id=%s sender=%s msg_type=%s wa_msg_id=%s err=%s",
                resolved_tenant_id, sender, msg_type, msg_id or "-", _norm_exc,
                exc_info=True,
            )
            try:
                from core.inbound_lifecycle import (  # noqa: PLC0415
                    EVENT_NORMALIZER_FAIL, record_lifecycle,
                )
                record_lifecycle(
                    EVENT_NORMALIZER_FAIL,
                    detail=f"exc={type(_norm_exc).__name__}",
                )
            except Exception:
                pass
            _persist_inbound_only(
                db=db,
                tenant_id=resolved_tenant_id,
                sender=sender or "",
                msg_type=msg_type or "",
                normalized_type="error",
                inbound_metadata={
                    "normalizer_error": f"{type(_norm_exc).__name__}: {str(_norm_exc)[:200]}",
                    "media_id":         (msg.get(msg_type) or {}).get("id") if isinstance(msg.get(msg_type), dict) else None,
                    "mime_type":        (msg.get(msg_type) or {}).get("mime_type") if isinstance(msg.get(msg_type), dict) else None,
                    "caption":          (msg.get(msg_type) or {}).get("caption") if isinstance(msg.get(msg_type), dict) else None,
                },
                wa_msg_id=msg_id or None,
                drop_reason="normalizer_exception",
            )
            # ── [AI_TEMP_ERROR_FALLBACK] (May 2026 #42) ────────────────
            # Normalizer raised → conversation persisted with placeholder,
            # but the customer effectively saw "no AI reply" for that
            # turn. Pinning the exception class + message here closes
            # the loop with the merchant: every silent media drop now
            # has a single greppable marker.
            try:
                from services.fallback_policy import (  # noqa: PLC0415
                    STAGE_NORMALIZER_EXCEPTION as _STG_NORM_EXC,
                    emit_temp_error_fallback_log as _emit_temp_err_norm,
                )
                _emit_temp_err_norm(
                    tenant_id=resolved_tenant_id,
                    conversation_id=None,
                    sender=sender or "",
                    inbound_msg_id=str(msg_id or ""),
                    msg_type=str(msg_type or ""),
                    intent="",
                    stage=_STG_NORM_EXC,
                    exception=_norm_exc,
                    fallback_kind="persist_only_placeholder",
                    response_goal="silent",
                )
            except Exception:  # noqa: BLE001
                pass
            return
        logger.info(
            "[TRACE][3/6] INBOUND_NORMALIZED | tenant_id=%s sender=%s normalized_type=%s should_process=%s",
            resolved_tenant_id, sender,
            normalized_inbound.normalized_type,
            normalized_inbound.should_process,
        )
        # Structured equivalent of the trace line above with the
        # full media metadata surface (mime / caption / media_id)
        # in a single grep-able shape. The TRACE line stays for
        # backward-compatible dashboards; this one is the May 2026
        # #41 audit contract.
        try:
            _norm_meta_for_log = normalized_inbound.metadata or {}
            logger.info(
                "[INBOUND_MEDIA_NORMALIZED] tenant_id=%s sender=%s "
                "msg_type=%s normalized_type=%s should_process=%s "
                "has_text=%s text_chars=%d has_caption=%s "
                "fallback_reply=%s mime=%s media_id=%s wa_msg_id=%s",
                resolved_tenant_id, sender, msg_type or "-",
                normalized_inbound.normalized_type or "-",
                normalized_inbound.should_process,
                bool((normalized_inbound.text or "").strip()),
                len(normalized_inbound.text or ""),
                bool((_norm_meta_for_log.get("caption") or "").strip()),
                bool(normalized_inbound.fallback_reply_ar),
                _norm_meta_for_log.get("mime_type") or "-",
                _norm_meta_for_log.get("media_id") or "-",
                msg_id or "-",
            )
        except Exception:
            pass

        # ── Commerce runtime ownership claim ──────────────────────────────────────
        # THE ownership decision, taken here because this is the first point at
        # which every competing owner is still below it. The branches that
        # follow are not formatters: the COD routes transition an order and send
        # the customer a follow-up, and the payment receipt / evidence / map /
        # claim short circuits mutate order state and send, all before the
        # merchant handler is entered. A decision taken after any of them would
        # never see those turns at all.
        #
        # The claim answers nothing by itself. It is scoped to this exact inbound
        # and is carried into the handler, where every gate that can silence the
        # turn still decides. Without a claim nothing below changes.
        _runtime_claim = None
        if not _is_platform_tenant(db, resolved_tenant_id):
            _claim_text = (getattr(normalized_inbound, "text", "") or "").strip() \
                or (_wa_text or "").strip()
            _runtime_claim = _commerce_runtime_claims_inbound(
                db, tenant_id=resolved_tenant_id, phone_id=used_pid, to=sender,
                text=_claim_text, wa_msg_id=msg_id,
            )
            if _runtime_claim is not None:
                logger.info(
                    "[WEBHOOK_ROUTE] route=commerce_runtime_claim tenant=%s from=%s "
                    "msg_id=%s basis=%s",
                    resolved_tenant_id, sender, msg_id, _runtime_claim.basis,
                )

        # ── Handle interactive button replies ──────────────────────────────────────
        if normalized_inbound.normalized_type == "interactive":
            interactive = msg.get("interactive", {})
            if interactive.get("type") == "button_reply":
                br      = interactive.get("button_reply", {}) or {}
                btn_id  = br.get("id", "")
                btn_txt = br.get("title", "") or btn_id
                context_wamid = str(
                    (msg.get("context") or {}).get("id") or ""
                ).strip()

                # COD confirmation flow runs for every tenant (it's a merchant-
                # facing template, not the Nahla SaaS sales bot). Try it FIRST so
                # the platform-sales button handler doesn't accidentally swallow
                # a "تأكيد الطلب" tap on a merchant tenant.
                try:
                    from services.cod_confirmation import (  # noqa: PLC0415
                        consume_owned_cod_button_inbound,
                        is_owned_cod_button_payload,
                    )
                except Exception as exc:
                    logger.error("[Webhook] COD button import failed: %s", exc)
                else:
                    if _runtime_claim is None and is_owned_cod_button_payload(btn_id):
                        async def _cod_followup(decision, order):
                            await _send_cod_followup_message(
                                phone_id=used_pid, to=sender,
                                decision=decision, order=order,
                                _tenant_id=resolved_tenant_id, _db=db,
                            )
                        try:
                            await consume_owned_cod_button_inbound(
                                db,
                                tenant_id=resolved_tenant_id,
                                customer_phone=sender,
                                text=btn_txt,
                                button_payload=btn_id,
                                context_wamid=context_wamid,
                                followup_send=_cod_followup,
                            )
                        except Exception as exc:
                            logger.error("[Webhook] COD button handler failed: %s", exc)
                        return

                # Product-pick buttons from merchant brain — route to merchant AI
                if btn_id.startswith("pick_") and not _is_platform_tenant(db, resolved_tenant_id):
                    pick_num = btn_id.split("_", 1)[-1]  # "1", "2", "3"
                    await _handle_merchant_message(
                        phone_id=used_pid, to=sender, text=pick_num,
                        tenant_id=resolved_tenant_id, db=db,
                        wa_message_ts=_wa_msg_ts,
                        wa_msg_id=msg_id or None,
                        inbound_metadata={"button_id": btn_id, "button_provenance": btn_id},
                        commerce_runtime_claim=_runtime_claim,
                    )
                    return

                # CatalogNavigator collection buttons — page-local index pick
                if btn_id.startswith("coll_") and not _is_platform_tenant(db, resolved_tenant_id):
                    pick_num = btn_id.split("_", 1)[-1]
                    await _handle_merchant_message(
                        phone_id=used_pid, to=sender, text=pick_num,
                        tenant_id=resolved_tenant_id, db=db,
                        wa_message_ts=_wa_msg_ts,
                        wa_msg_id=msg_id or None,
                        inbound_metadata={"button_id": btn_id, "button_provenance": btn_id},
                        commerce_runtime_claim=_runtime_claim,
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
                        commerce_runtime_claim=_runtime_claim,
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
                        inbound_metadata={
                            "button_id": btn_id,
                            "button_title": btn_txt,
                            "button_provenance": btn_id,
                        },
                        commerce_runtime_claim=_runtime_claim,
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
                        # The reply id is the structured half of a list tap.
                        # Forwarding only the title would leave the turn with
                        # words alone, which cannot carry an unambiguous
                        # choice of a specific row.
                        inbound_metadata={
                            "list_reply_id": lr_id,
                            "list_reply_title": lr_title,
                        },
                        commerce_runtime_claim=_runtime_claim,
                    )
            return

        # ── WhatsApp location pin → order address ingestion (PR-6) ──
        if (
            normalized_inbound.normalized_type == "location"
            and not _is_platform_tenant(db, resolved_tenant_id)
        ):
            try:
                from core.order_flow import (  # noqa: PLC0415
                    maybe_handle_wa_address_inbound,
                    persist_checkout_location_outcome,
                )
                from modules.ai.media.customer_turn_completion import (  # noqa: PLC0415
                    resolve_checkout_location_persist_turn,
                )
                _loc_decision = maybe_handle_wa_address_inbound(
                    db=db,
                    tenant_id=resolved_tenant_id,
                    phone=sender or "",
                    inbound_normalized_type="location",
                    inbound_metadata=normalized_inbound.metadata or {},
                )
            except Exception as _loc_exc:  # noqa: BLE001
                logger.warning(
                    "[ORDER_FLOW_STATE] location short-circuit failed "
                    "tenant=%s phone=%s err=%s",
                    resolved_tenant_id, sender, _loc_exc,
                )
                _loc_decision = None
            if _loc_decision is not None:
                _loc_persisted = False
                _loc_persist_reason = "apply_state_patch_false"
                try:
                    _loc_persisted, _loc_persist_reason = persist_checkout_location_outcome(
                        db,
                        tenant_id=resolved_tenant_id,
                        phone=sender or "",
                        state_patch=_loc_decision.get("state_patch") or {},
                    )
                except Exception as _loc_patch_exc:  # noqa: BLE001
                    logger.warning(
                        "[ORDER_FLOW_STATE] location state_patch failed "
                        "tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _loc_patch_exc,
                    )
                    _loc_persist_reason = "apply_state_patch_exception"
                _loc_turn = resolve_checkout_location_persist_turn(
                    persist_ok=_loc_persisted,
                    persist_reason=_loc_persist_reason,
                    inbound_type="location",
                    inbound_metadata=normalized_inbound.metadata or {},
                    inbound_text="",
                    state_patch=_loc_decision.get("state_patch") or {},
                )
                if _loc_turn.get("emit_success_ack"):
                    from core.address_ingest_post_persist import (  # noqa: PLC0415
                        persist_address_ingest_turn_messages,
                        reproject_address_ingest_decision_after_persist,
                    )
                    from core.constrained_operational_compose import (  # noqa: PLC0415
                        resolve_prebrain_reply_text,
                    )

                    _loc_decision = reproject_address_ingest_decision_after_persist(
                        db,
                        tenant_id=resolved_tenant_id,
                        phone=sender or "",
                        inbound_text="",
                        address_type=str(
                            (_loc_decision.get("state_patch") or {}).get(
                                "delivery_address_type"
                            )
                            or "location"
                        ),
                    )
                    _loc_reply_text, _loc_compose_meta = await resolve_prebrain_reply_text(
                        db=db,
                        tenant_id=resolved_tenant_id,
                        phone=sender or "",
                        decision=_loc_decision,
                        inbound_text="",
                    )
                    persist_address_ingest_turn_messages(
                        db,
                        tenant_id=resolved_tenant_id,
                        phone=sender or "",
                        inbound_body=str(
                            (_loc_turn.get("inbound_metadata") or {}).get("google_maps_url")
                            or ((_loc_decision.get("known_facts") or {}).get("checkout_maps_url"))
                            or "[location]"
                        ),
                        outbound_body=_loc_reply_text,
                        inbound_event_type="whatsapp_location",
                        extra_metadata={
                            "wa_message_id": msg_id or None,
                            **(_loc_compose_meta or {}),
                        },
                    )
                    await _post_wa(
                        used_pid,
                        {
                            "messaging_product": "whatsapp",
                            "to": sender,
                            "type": "text",
                            "text": {"body": _loc_reply_text},
                        },
                        _tenant_id=resolved_tenant_id,
                        _db=db,
                    )
                    return
                logger.warning(
                    "[ORDER_FLOW_STATE] location ack skipped persist_failed "
                    "tenant=%s phone=*%s reason=%s completion=%s",
                    resolved_tenant_id,
                    (sender or "")[-4:],
                    _loc_persist_reason,
                    _loc_turn.get("completion_class"),
                )
                await _handle_merchant_message(
                    phone_id=used_pid,
                    to=sender,
                    text=str(_loc_turn.get("brain_text") or ""),
                    tenant_id=resolved_tenant_id,
                    db=db,
                    inbound_metadata=_loc_turn.get("inbound_metadata"),
                    inbound_persist_body=str(_loc_turn.get("brain_text") or ""),
                    wa_message_ts=_wa_msg_ts,
                    wa_msg_id=msg_id or None,
                    commerce_runtime_claim=_runtime_claim,
                )
                return

        # ── INBOUND_MEDIA_TRACE (mandatory pre-ignore audit line) ──
        # Per the May 2026 spec: emit ONE structured trace line for
        # every inbound media payload BEFORE the type allow-list
        # decides whether to drop it. Lets on-call grep production
        # for "where did this video / audio / sticker go?" without
        # re-running anything. Never raises.
        try:
            _imt_meta = normalized_inbound.metadata or {}
            logger.info(
                "[INBOUND_MEDIA_TRACE] provider=%s msg_type=%s "
                "normalized_type=%s wamid=%s media_id=%s tenant=%s "
                "phone_number_id=%s sender=%s "
                "normalized_should_process=%s "
                "caption=%r mime=%s filename=%r "
                "message_saved=pending ui_visible=unknown",
                # Provider hint. We can't
                # know definitively here; surface what the route
                # registered as the source.
                "wa_cloud",
                msg_type,
                normalized_inbound.normalized_type,
                msg_id or None,
                _imt_meta.get("media_id"),
                resolved_tenant_id,
                phone_number_id or None,
                sender,
                normalized_inbound.should_process,
                (_imt_meta.get("caption") or "")[:80],
                _imt_meta.get("mime_type"),
                _imt_meta.get("filename"),
            )
        except Exception:
            pass

        # Allow-list of normalized types the brain pipeline accepts.
        # Updated May 2026 to include ``video`` so inbound video
        # messages flow to the new lightweight passthrough in the
        # normaliser instead of being silently dropped at
        # ``INBOUND_IGNORED_UNSUPPORTED``. Video reaches the brain
        # exactly like a captioned image: the normaliser builds an
        # Arabic-framed prompt and the brain writes its own reply.
        if normalized_inbound.normalized_type not in {"text", "audio", "image", "document", "video", "sticker"}:
            # ── Button-tap rescue: "button" type = customer tapped a template
            # quick-reply.  The normalizer marks it unsupported, but we have
            # already extracted human-readable text via _extract_wa_message_text.
            # Treat it exactly like a text message so the Brain receives it.
            if _wa_text and msg_type == "button" and not _is_platform_tenant(db, resolved_tenant_id):
                _btn_payload = str((msg.get("button") or {}).get("payload") or "")
                _context_wamid = str(
                    (msg.get("context") or {}).get("id") or ""
                ).strip()
                # The claim is honoured BEFORE the classification, not only
                # before the action. ``resolve_owned_cod_button_payload_from_context``
                # is a correlation against this tenant's recent COD sends: it is
                # what decides that a noncanonical payload carrying a
                # ``context.id`` *is* a COD reply, and the consuming action acts
                # on that decision. Running it for a turn this runtime owns
                # already puts a second owner on the turn.
                if _runtime_claim is not None:
                    logger.info(
                        "[COD_BUTTON_ROUTE] skipped tenant=%s sender=%s basis=%s — the "
                        "commerce runtime owns this inbound; no COD classification, "
                        "correlation, order mutation or follow-up",
                        resolved_tenant_id, sender, _runtime_claim.basis,
                    )
                else:
                    _owned_btn_payload, _cod_correlation = _classify_owned_cod_button(
                        db, tenant_id=resolved_tenant_id, sender=sender,
                        button_payload=_btn_payload, button_text=_wa_text,
                        context_wamid=_context_wamid,
                    )
                    if _owned_btn_payload:
                        async def _cod_tpl_followup(decision, order):
                            await _send_cod_followup_message(
                                phone_id=used_pid, to=sender,
                                decision=decision, order=order,
                                _tenant_id=resolved_tenant_id, _db=db,
                            )
                        logger.info(
                            "[COD_BUTTON_ROUTE] consumed tenant=%s sender=%s "
                            "correlation=%s context_wamid=%s",
                            resolved_tenant_id,
                            sender,
                            _cod_correlation,
                            str((msg.get("context") or {}).get("id") or "-")[-24:],
                        )
                        try:
                            from services.cod_confirmation import (  # noqa: PLC0415
                                consume_owned_cod_button_inbound,
                            )
                            await consume_owned_cod_button_inbound(
                                db,
                                tenant_id=resolved_tenant_id,
                                customer_phone=sender,
                                text=_wa_text,
                                button_payload=_owned_btn_payload,
                                context_wamid=_context_wamid,
                                followup_send=_cod_tpl_followup,
                            )
                        except Exception as exc:
                            logger.error("[Webhook] COD template-button handler failed: %s", exc)
                        return
                logger.info(
                    "[WA PARSER] button tap rescued | tenant=%s text=%r source=%s",
                    resolved_tenant_id, _wa_text, _wa_source,
                )
                await _handle_merchant_message(
                    phone_id=used_pid, to=sender, text=_wa_text,
                    tenant_id=resolved_tenant_id, db=db,
                    wa_message_ts=_wa_msg_ts,
                    wa_msg_id=msg_id or None,
                    commerce_runtime_claim=_runtime_claim,
                )
                return
            logger.info(
                "[TRACE][4/6] INBOUND_IGNORED_UNSUPPORTED | tenant_id=%s sender=%s normalized_type=%s",
                resolved_tenant_id, sender, normalized_inbound.normalized_type,
            )
            # Pre-brain silent-drop visibility (May 2026 #22): mirror
            # the trace log into ``ai_quality_events`` so the owner
            # dashboard's "إسقاطات الإدخال" tab shows the merchant
            # exactly which sticker/reaction/location/contacts message
            # never reached the AI. Without this row the dashboard
            # stays at zero while production drops messages silently.
            try:
                from core.inbound_observability import (  # noqa: PLC0415
                    record_inbound_drop,
                    DROP_UNSUPPORTED_TYPE,
                )
                record_inbound_drop(
                    tenant_id=resolved_tenant_id,
                    drop_kind=DROP_UNSUPPORTED_TYPE,
                    customer_phone=sender or "",
                    chosen_path=f"normalized_type={normalized_inbound.normalized_type}",
                    detail=(
                        f"msg_type={msg_type!r} normalized_type="
                        f"{normalized_inbound.normalized_type!r}"
                    ),
                    # May 2026 #37 — pass the normalized type so the
                    # observability writer can apply the noise filter
                    # (reaction / revoke / ephemeral / system don't
                    # open AI Quality rows; merchants don't want to
                    # triage emoji reactions).
                    normalized_type=normalized_inbound.normalized_type,
                )
            except Exception as _obs_exc:  # noqa: BLE001
                logger.warning("[INBOUND_OBS] hook failed: %s", _obs_exc)
            # May 2026 #41 — persist a placeholder inbound row so
            # the merchant inbox lists the message even when the AI
            # could not process it. Sticker / reaction / unknown
            # types previously vanished from the conversation list;
            # the merchant had no way to see "the customer DID
            # send something at 1:05 PM" without raw provider logs.
            try:
                from core.inbound_lifecycle import (  # noqa: PLC0415
                    EVENT_UNSUPPORTED_TYPE, record_lifecycle,
                )
                record_lifecycle(
                    EVENT_UNSUPPORTED_TYPE,
                    detail=f"normalized_type={normalized_inbound.normalized_type!r}",
                )
            except Exception:
                pass
            _persist_inbound_only(
                db=db,
                tenant_id=resolved_tenant_id,
                sender=sender or "",
                msg_type=msg_type or "",
                normalized_type=normalized_inbound.normalized_type or "",
                inbound_metadata=normalized_inbound.metadata,
                wa_msg_id=msg_id or None,
                drop_reason=f"unsupported_type:{normalized_inbound.normalized_type}",
                placeholder_body=f"[رسالة وسائط: {normalized_inbound.normalized_type}]",
            )
            return

        text = normalized_inbound.text.strip()
        route_unclear_audio_order_support = False
        try:
            from modules.ai.media.routing_guard import (  # noqa: PLC0415
                resolve_inbound_semantic_routing,
            )

            _semantic_routing = resolve_inbound_semantic_routing(
                brain_text=text,
                inbound_metadata=normalized_inbound.metadata,
                inbound_normalized_type=normalized_inbound.normalized_type,
                history=StateManager.load_history(
                    db,
                    phone=sender,
                    tenant_id=resolved_tenant_id,
                ),
            )
            text = _semantic_routing.semantic_text
            route_unclear_audio_order_support = (
                _semantic_routing.route_unclear_audio_order_support
            )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — semantic resolve must not block inbound routing
            text = normalized_inbound.text.strip()
            route_unclear_audio_order_support = False
        try:
            from modules.ai.media.customer_turn_completion import (  # noqa: PLC0415
                maybe_restore_catalog_order_semantic_text,
            )

            text, _catalog_completion_meta = maybe_restore_catalog_order_semantic_text(
                semantic_text=text,
                original_brain_text=normalized_inbound.text,
                inbound_metadata=normalized_inbound.metadata,
            )
            if _catalog_completion_meta:
                _ni_meta = dict(normalized_inbound.metadata or {})
                _ni_meta.update(_catalog_completion_meta)
                normalized_inbound.metadata = _ni_meta
        except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog completion restore must not block inbound
            pass
        if route_unclear_audio_order_support:
            _ni_meta = dict(normalized_inbound.metadata or {})
            _ni_meta["route_unclear_audio_order_support"] = True
            normalized_inbound.metadata = _ni_meta
        elif isinstance(normalized_inbound.metadata, dict):
            _ni_meta = dict(normalized_inbound.metadata)
            _ni_meta.pop("route_unclear_audio_order_support", None)
            normalized_inbound.metadata = _ni_meta
        persist_body = inbound_persist_body(normalized_inbound)
        # ── Media-without-text fallback ─────────────────────────────
        # The normalizer detected an audio/image/video/document but
        # couldn't extract any usable text (Whisper failed, vision
        # failed, missing caption, etc.) AND there's a canonical
        # Arabic fallback message it wants us to send. We MUST NOT
        # call the brain in that case — we'd spend tokens generating
        # a generic apology while losing the structured metadata that
        # explains why. The fallback reply is short, kind, and asks
        # the customer to retype — exactly the spec's required
        # behaviour.
        #
        # May 2026 #41: ``video`` added to the fallback set defensively.
        # The normalizer normally builds non-empty Arabic framing text
        # for inbound video, but if a future regression ships an empty
        # transcript path the fallback handler (which persists the
        # inbound row + sends a courtesy reply) is a safer landing
        # than the empty-text drop further down.
        if (
            not text
            and not route_unclear_audio_order_support
            and normalized_inbound.fallback_reply_ar
            and normalized_inbound.normalized_type in {"audio", "image", "document", "video", "sticker"}
            and not _is_platform_tenant(db, resolved_tenant_id)
        ):
            logger.info(
                "[MediaFallback] tenant=%s sender=%s normalized_type=%s "
                "no_text → sending fallback reply",
                resolved_tenant_id, sender,
                normalized_inbound.normalized_type,
            )
            try:
                from core.inbound_lifecycle import (  # noqa: PLC0415
                    EVENT_EMPTY_TEXT_FALLBACK, record_lifecycle,
                )
                record_lifecycle(
                    EVENT_EMPTY_TEXT_FALLBACK,
                    detail=f"normalized_type={normalized_inbound.normalized_type!r}",
                )
            except Exception:
                pass
            await _handle_media_fallback(
                phone_id=used_pid, to=sender,
                tenant_id=resolved_tenant_id, db=db,
                fallback_reply=normalized_inbound.fallback_reply_ar,
                inbound_metadata=normalized_inbound.metadata,
                wa_message_ts=_wa_msg_ts,
                wa_msg_id=msg_id or None,
            )
            return

        route_catalog_order_structured = False
        if not text and not route_unclear_audio_order_support:
            try:
                from modules.ai.media.customer_turn_completion import (  # noqa: PLC0415
                    should_continue_structured_catalog_order,
                )

                if should_continue_structured_catalog_order(
                    normalized_inbound.metadata,
                    normalized_inbound.text or "",
                ):
                    route_catalog_order_structured = True
                    _ni_meta = dict(normalized_inbound.metadata or {})
                    _ni_meta["catalog_order_structured_event"] = True
                    _ni_meta["catalog_order_empty_text_continued"] = True
                    _ni_meta["synthetic_customer_phrase"] = False
                    _ctc = dict(_ni_meta.get("customer_turn_completion") or {})
                    _ctc.update({
                        "input_type": "catalog_order",
                        "semantic_owner": "brain",
                        "structured_action_owner": "wa_native_catalog_order",
                        "completion_class": "structured_action_and_natural_continuation",
                        "suppression_reason": None,
                    })
                    _ni_meta["customer_turn_completion"] = _ctc
                    normalized_inbound.metadata = _ni_meta
                    logger.info(
                        "[CUSTOMER_TURN_COMPLETION] catalog_order continued past "
                        "empty_text_no_fallback tenant=%s sender=%s "
                        "synthetic_customer_phrase=false",
                        resolved_tenant_id,
                        sender,
                    )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog completion must not block inbound
                pass

        if (
            not text
            and not route_unclear_audio_order_support
            and not route_catalog_order_structured
        ):
            logger.info(
                "[TRACE][4/6] INBOUND_IGNORED_EMPTY_TEXT | tenant_id=%s sender=%s normalized_type=%s",
                resolved_tenant_id, sender, normalized_inbound.normalized_type,
            )
            # Pre-brain silent-drop visibility (May 2026 #22): the normalizer
            # produced no text AND no fallback reply, so we just ``return``.
            # Without an audit row the dashboard would never know this
            # particular audio/image/document arrived but vanished.
            try:
                from core.inbound_observability import (  # noqa: PLC0415
                    record_inbound_drop,
                    DROP_EMPTY_TEXT,
                )
                record_inbound_drop(
                    tenant_id=resolved_tenant_id,
                    drop_kind=DROP_EMPTY_TEXT,
                    customer_phone=sender or "",
                    chosen_path=f"normalized_type={normalized_inbound.normalized_type}",
                    detail=(
                        f"normalized_type={normalized_inbound.normalized_type!r} "
                        f"no_fallback=True"
                    ),
                )
            except Exception as _obs_exc:  # noqa: BLE001
                logger.warning("[INBOUND_OBS] hook failed: %s", _obs_exc)
            # May 2026 #41 — never let a media-only inbound vanish
            # from the merchant inbox. This branch fires when the
            # normalizer produced no text AND no fallback (or for
            # platform tenants which the fallback skips). Persist a
            # structured placeholder so the conversation row exists,
            # then return without spending brain tokens.
            try:
                from core.inbound_lifecycle import (  # noqa: PLC0415
                    EVENT_EMPTY_TEXT_NO_FALLBACK, record_lifecycle,
                )
                record_lifecycle(
                    EVENT_EMPTY_TEXT_NO_FALLBACK,
                    detail=f"normalized_type={normalized_inbound.normalized_type!r}",
                )
            except Exception:
                pass
            _persist_inbound_only(
                db=db,
                tenant_id=resolved_tenant_id,
                sender=sender or "",
                msg_type=msg_type or "",
                normalized_type=normalized_inbound.normalized_type or "",
                inbound_metadata=normalized_inbound.metadata,
                wa_msg_id=msg_id or None,
                drop_reason="empty_text_no_fallback",
            )
            try:
                from core.wa_catalog_order_immediate_draft import (  # noqa: PLC0415
                    is_catalog_order_inbound,
                    persist_catalog_order_immediate_draft,
                )
                from core.wa_native_catalog_order import (  # noqa: PLC0415
                    persist_structured_catalog_order_referent,
                )
                from routers.conversations import _get_or_create_conversation  # noqa: PLC0415

                _cat_meta = dict(normalized_inbound.metadata or {})
                if is_catalog_order_inbound(_cat_meta) and sender:
                    _cat_convo = _get_or_create_conversation(
                        db, resolved_tenant_id, sender,
                    )
                    persist_structured_catalog_order_referent(
                        db,
                        tenant_id=int(resolved_tenant_id or 0),
                        phone=sender or "",
                        inbound_metadata=_cat_meta,
                        conversation=_cat_convo,
                    )
                    persist_catalog_order_immediate_draft(
                        db,
                        tenant_id=int(resolved_tenant_id or 0),
                        conversation=_cat_convo,
                        inbound_metadata=_cat_meta,
                        phone=sender or "",
                        source_message_key=(msg_id or None),
                    )
                    db.commit()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[WA_NATIVE_ORDER] persist_only_catalog_order_stamp_failed "
                    "tenant=%s sender=%s",
                    resolved_tenant_id,
                    sender,
                )
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001  # noqa: silent-ok — rollback after failed persist-only catalog commit
                    pass
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
            # The claim established above, acted on here: the turn goes straight
            # to the merchant handler with the fully prepared text, and every
            # short circuit below — payment receipt, payment evidence, map
            # image, payment claim, address, payment method — is skipped.
            if _runtime_claim is not None:
                await _handle_merchant_message(
                    phone_id=used_pid, to=sender, text=text,
                    tenant_id=resolved_tenant_id, db=db,
                    inbound_metadata=normalized_inbound.metadata,
                    inbound_persist_body=persist_body,
                    wa_message_ts=_wa_msg_ts,
                    wa_msg_id=msg_id or None,
                    commerce_runtime_claim=_runtime_claim,
                )
                return

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
                    maybe_handle_payment_evidence_inbound,
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

            # ── Pre-transfer-review / pending-evidence short-circuit ──
            # Universal payment-evidence gate (May 2026): when the
            # normalizer flagged the inbound as a pre-transfer
            # review screen or as payment-context data without a
            # completion marker, reply with a short polite sentence
            # asking for the final receipt — WITHOUT mutating
            # order_status / payment_receipt_received, and WITHOUT
            # leaking any internal phone number.
            #
            # This branch only fires when the dedicated receipt
            # short-circuit above did NOT fire (i.e. we have
            # payment-related media but evidence is not confirmed).
            _evidence_decision = None
            if _receipt_decision is None:
                try:
                    _evidence_decision = maybe_handle_payment_evidence_inbound(
                        db=db,
                        tenant_id=resolved_tenant_id,
                        phone=sender,
                        inbound_normalized_type=normalized_inbound.normalized_type,
                        inbound_metadata=normalized_inbound.metadata or {},
                    )
                except Exception as _ev_exc:  # noqa: BLE001
                    logger.warning(
                        "[PAYMENT_EVIDENCE] short-circuit check failed "
                        "(non-fatal) tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _ev_exc,
                    )
                    _evidence_decision = None

            # ── Map-image short-circuit (May 2026 hotfix) ──────────────
            # Apple Maps / Google Maps screenshots arrive as plain
            # images (not WhatsApp location messages) so the brain
            # used to silently ignore them. Run the dedicated
            # detector here BEFORE the payment-claim branch so a
            # map image during an active order produces an explicit
            # "send the link or the national short-address" reply
            # instead of leaking into the LLM fallback.
            _map_image_decision = None
            if _receipt_decision is None and _evidence_decision is None:
                try:
                    from core.order_flow import (  # noqa: PLC0415
                        maybe_handle_map_image_inbound,
                    )
                    _map_image_decision = maybe_handle_map_image_inbound(
                        db=db,
                        tenant_id=resolved_tenant_id,
                        phone=sender,
                        inbound_normalized_type=normalized_inbound.normalized_type,
                        inbound_metadata=normalized_inbound.metadata or {},
                    )
                except Exception as _map_exc:  # noqa: BLE001
                    logger.warning(
                        "[ORDER_FLOW_STATE] map-image short-circuit check "
                        "failed (non-fatal) tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _map_exc,
                    )
                    _map_image_decision = None

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
            _address_decision = None
            _payment_method_decision = None
            if (
                _receipt_decision is None
                and _evidence_decision is None
                and _map_image_decision is None
            ):
                try:
                    from core.order_flow import maybe_handle_wa_address_inbound  # noqa: PLC0415
                    _address_decision = maybe_handle_wa_address_inbound(
                        db=db,
                        tenant_id=resolved_tenant_id,
                        phone=sender,
                        inbound_normalized_type=normalized_inbound.normalized_type,
                        inbound_metadata=normalized_inbound.metadata or {},
                        inbound_text=text or "",
                    )
                except Exception as _addr_exc:  # noqa: BLE001
                    logger.warning(
                        "[ORDER_FLOW_STATE] address text short-circuit failed "
                        "tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _addr_exc,
                    )
                    _address_decision = None
            if (
                _receipt_decision is None
                and _evidence_decision is None
                and _map_image_decision is None
                and _address_decision is None
                and normalized_inbound.normalized_type == "text"
            ):
                try:
                    from core.order_flow import (  # noqa: PLC0415
                        maybe_handle_payment_method_selection_inbound,
                    )
                    _payment_method_decision = maybe_handle_payment_method_selection_inbound(
                        db=db,
                        tenant_id=resolved_tenant_id,
                        phone=sender or "",
                        inbound_text=text or "",
                    )
                except Exception as _pm_exc:  # noqa: BLE001
                    logger.warning(
                        "[ORDER_FLOW_STATE] payment_method short-circuit failed "
                        "tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _pm_exc,
                    )
                    _payment_method_decision = None
            if (
                _receipt_decision is None
                and _evidence_decision is None
                and _map_image_decision is None
                and _address_decision is None
                and _payment_method_decision is None
            ):
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
                # ── W2.0.1 (May 2026): pin the short-circuit on the
                # trace. (Pre-W2.0.3 this branch produced orphan
                # MessageEvents because save_message ran without a
                # conversation_id; W2.0.3 below resolves a row first
                # and threads its id into every save.)
                try:
                    from core.inbound_lifecycle import (  # noqa: PLC0415
                        EVENT_PAYMENT_SHORT_CIRCUIT, record_lifecycle,
                    )
                    record_lifecycle(EVENT_PAYMENT_SHORT_CIRCUIT)
                except Exception:
                    pass
                # ── W2.0.3 (May 2026): Conversation-linking integrity.
                # Resolve the Conversation row BEFORE any save_message
                # so the short-circuit path no longer produces orphans.
                # Fail-open contract: if the resolver raises (DB hiccup,
                # session error, etc.) we record an auto_link_failed
                # event and fall back to the legacy orphan behaviour —
                # the user's media still gets persisted, just without
                # a conversation_id, exactly like before this patch.
                _w203_conv_id_pc: Optional[int] = None
                try:
                    from routers.conversations import (  # noqa: PLC0415
                        _get_or_create_conversation as _w203_resolve_pc,
                    )
                    _w203_convo_pc = _w203_resolve_pc(
                        db, resolved_tenant_id, sender,
                    )
                    _w203_conv_id_pc = (
                        int(getattr(_w203_convo_pc, "id", 0) or 0)
                        or None
                    )
                    try:
                        from core.inbound_lifecycle import (  # noqa: PLC0415
                            EVENT_AUTO_LINK_OK, record_lifecycle as _rl_pc,
                        )
                        _rl_pc(
                            EVENT_AUTO_LINK_OK,
                            detail="branch=payment_claim",
                            conversation_id=_w203_conv_id_pc,
                        )
                    except Exception:
                        pass
                except Exception as _w203_exc_pc:  # noqa: BLE001
                    logger.warning(
                        "[ORDER_FLOW_STATE] payment_claim auto-link "
                        "failed (fail-open to legacy orphan write) "
                        "tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _w203_exc_pc,
                    )
                    try:
                        from core.inbound_lifecycle import (  # noqa: PLC0415
                            EVENT_AUTO_LINK_FAILED, record_lifecycle as _rl_pc,
                        )
                        _rl_pc(
                            EVENT_AUTO_LINK_FAILED,
                            detail=(
                                f"branch=payment_claim "
                                f"exc={type(_w203_exc_pc).__name__}"
                            ),
                        )
                    except Exception:
                        pass
                    _w203_conv_id_pc = None
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
                        body=persist_body or "[payment_claim]",
                        event_type="whatsapp_message",
                        conversation_id=_w203_conv_id_pc,
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
                from core.constrained_operational_compose import (  # noqa: PLC0415
                    resolve_prebrain_reply_text,
                )

                _pc_reply_text, _pc_compose_meta = await resolve_prebrain_reply_text(
                    db=db,
                    tenant_id=resolved_tenant_id,
                    phone=sender,
                    decision=_payment_claim_decision,
                    inbound_text=text or "",
                )
                try:
                    StateManager.save_message(
                        db,
                        phone=sender,
                        direction="outbound",
                        body=_pc_reply_text,
                        event_type="whatsapp_message",
                        conversation_id=_w203_conv_id_pc,
                        tenant_id=resolved_tenant_id,
                        extra_metadata={
                            "is_ai": True,
                            "deterministic_path": "payment_claim_ack",
                            **_pc_compose_meta,
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
                            "body": _pc_reply_text,
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
                # ── W2.0.1 (May 2026): receipt short-circuit marker.
                # (Pre-W2.0.3 this branch produced orphan
                # MessageEvents because save_message ran without a
                # conversation_id; W2.0.3 below resolves a row first
                # and threads its id into every save.)
                try:
                    from core.inbound_lifecycle import (  # noqa: PLC0415
                        EVENT_RECEIPT_SHORT_CIRCUIT, record_lifecycle,
                    )
                    record_lifecycle(EVENT_RECEIPT_SHORT_CIRCUIT)
                except Exception:
                    pass
                # ── W2.0.3: Conversation-linking integrity (fail-open).
                _w203_conv_id_rc: Optional[int] = None
                try:
                    from routers.conversations import (  # noqa: PLC0415
                        _get_or_create_conversation as _w203_resolve_rc,
                    )
                    _w203_convo_rc = _w203_resolve_rc(
                        db, resolved_tenant_id, sender,
                    )
                    _w203_conv_id_rc = (
                        int(getattr(_w203_convo_rc, "id", 0) or 0)
                        or None
                    )
                    try:
                        from core.inbound_lifecycle import (  # noqa: PLC0415
                            EVENT_AUTO_LINK_OK, record_lifecycle as _rl_rc,
                        )
                        _rl_rc(
                            EVENT_AUTO_LINK_OK,
                            detail="branch=payment_receipt",
                            conversation_id=_w203_conv_id_rc,
                        )
                    except Exception:
                        pass
                except Exception as _w203_exc_rc:  # noqa: BLE001
                    logger.warning(
                        "[ORDER_FLOW_STATE] payment_receipt auto-link "
                        "failed (fail-open to legacy orphan write) "
                        "tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _w203_exc_rc,
                    )
                    try:
                        from core.inbound_lifecycle import (  # noqa: PLC0415
                            EVENT_AUTO_LINK_FAILED, record_lifecycle as _rl_rc,
                        )
                        _rl_rc(
                            EVENT_AUTO_LINK_FAILED,
                            detail=(
                                f"branch=payment_receipt "
                                f"exc={type(_w203_exc_rc).__name__}"
                            ),
                        )
                    except Exception:
                        pass
                    _w203_conv_id_rc = None
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
                        body=persist_body or "[إيصال تحويل]",
                        event_type=(
                            "whatsapp_document"
                            if normalized_inbound.normalized_type == "document"
                            else "whatsapp_image"
                        ),
                        conversation_id=_w203_conv_id_rc,
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
                from core.constrained_operational_compose import (  # noqa: PLC0415
                    resolve_prebrain_reply_text,
                )

                _rc_reply_text, _rc_compose_meta = await resolve_prebrain_reply_text(
                    db=db,
                    tenant_id=resolved_tenant_id,
                    phone=sender,
                    decision=_receipt_decision,
                    inbound_text=persist_body or text or "",
                )
                try:
                    StateManager.save_message(
                        db,
                        phone=sender,
                        direction="outbound",
                        body=_rc_reply_text,
                        event_type="whatsapp_message",
                        conversation_id=_w203_conv_id_rc,
                        tenant_id=resolved_tenant_id,
                        extra_metadata={
                            "is_ai": True,
                            "deterministic_path": "payment_receipt_ack",
                            "order_summary": _receipt_decision.get("summary"),
                            **_rc_compose_meta,
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
                            "body": _rc_reply_text,
                        },
                    },
                    _tenant_id=resolved_tenant_id,
                    _db=db,
                )
                return

            if _map_image_decision is not None:
                # Apple/Google Maps screenshot during an active
                # order. Persist inbound + outbound deterministically,
                # apply the optional state_patch (e.g.
                # awaiting_location_text=True), then reply asking
                # for a parseable location form.
                try:
                    from core.inbound_lifecycle import (  # noqa: PLC0415
                        EVENT_MAP_SHORT_CIRCUIT, record_lifecycle,
                    )
                    record_lifecycle(EVENT_MAP_SHORT_CIRCUIT)
                except Exception:
                    pass
                # ── W2.0.3: Conversation-linking integrity (fail-open).
                _w203_conv_id_mp: Optional[int] = None
                try:
                    from routers.conversations import (  # noqa: PLC0415
                        _get_or_create_conversation as _w203_resolve_mp,
                    )
                    _w203_convo_mp = _w203_resolve_mp(
                        db, resolved_tenant_id, sender,
                    )
                    _w203_conv_id_mp = (
                        int(getattr(_w203_convo_mp, "id", 0) or 0)
                        or None
                    )
                    try:
                        from core.inbound_lifecycle import (  # noqa: PLC0415
                            EVENT_AUTO_LINK_OK, record_lifecycle as _rl_mp,
                        )
                        _rl_mp(
                            EVENT_AUTO_LINK_OK,
                            detail="branch=map_image",
                            conversation_id=_w203_conv_id_mp,
                        )
                    except Exception:
                        pass
                except Exception as _w203_exc_mp:  # noqa: BLE001
                    logger.warning(
                        "[ORDER_FLOW_STATE] map_image auto-link "
                        "failed (fail-open to legacy orphan write) "
                        "tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _w203_exc_mp,
                    )
                    try:
                        from core.inbound_lifecycle import (  # noqa: PLC0415
                            EVENT_AUTO_LINK_FAILED, record_lifecycle as _rl_mp,
                        )
                        _rl_mp(
                            EVENT_AUTO_LINK_FAILED,
                            detail=(
                                f"branch=map_image "
                                f"exc={type(_w203_exc_mp).__name__}"
                            ),
                        )
                    except Exception:
                        pass
                    _w203_conv_id_mp = None
                try:
                    from core.order_flow import (  # noqa: PLC0415
                        apply_state_patch as _apply_state_patch_map,
                    )
                    _apply_state_patch_map(
                        db,
                        tenant_id=resolved_tenant_id,
                        phone=sender,
                        state_patch=_map_image_decision.get("state_patch") or {},
                    )
                except Exception as _mp_pp_exc:  # noqa: BLE001
                    logger.warning(
                        "[ORDER_FLOW_STATE] map_image state_patch apply "
                        "failed tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _mp_pp_exc,
                    )
                try:
                    StateManager.save_message(
                        db,
                        phone=sender,
                        direction="inbound",
                        body=persist_body or "[لقطة خرائط]",
                        event_type="whatsapp_image",
                        conversation_id=_w203_conv_id_mp,
                        tenant_id=resolved_tenant_id,
                        extra_metadata={
                            "normalized_inbound": normalized_inbound.metadata,
                            "wa_message_id": msg_id or None,
                            "map_image_short_circuit": True,
                        },
                    )
                except Exception as _mp_in_exc:  # noqa: BLE001
                    logger.warning(
                        "[ORDER_FLOW_STATE] map_image inbound save failed "
                        "tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _mp_in_exc,
                    )
                from core.constrained_operational_compose import (  # noqa: PLC0415
                    resolve_prebrain_reply_text,
                )

                _mp_reply_text, _mp_compose_meta = await resolve_prebrain_reply_text(
                    db=db,
                    tenant_id=resolved_tenant_id,
                    phone=sender,
                    decision=_map_image_decision,
                    inbound_text=persist_body or text or "",
                )
                try:
                    StateManager.save_message(
                        db,
                        phone=sender,
                        direction="outbound",
                        body=_mp_reply_text,
                        event_type="whatsapp_message",
                        conversation_id=_w203_conv_id_mp,
                        tenant_id=resolved_tenant_id,
                        extra_metadata={
                            "is_ai": True,
                            "deterministic_path": "map_image_ack",
                            **_mp_compose_meta,
                        },
                    )
                except Exception as _mp_out_exc:  # noqa: BLE001
                    logger.warning(
                        "[ORDER_FLOW_STATE] map_image outbound save failed "
                        "tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _mp_out_exc,
                    )
                await _post_wa(
                    used_pid,
                    {
                        "messaging_product": "whatsapp",
                        "to": sender,
                        "type": "text",
                        "text": {
                            "body": _mp_reply_text,
                        },
                    },
                    _tenant_id=resolved_tenant_id,
                    _db=db,
                )
                return

            if _address_decision is not None:
                from core.order_flow import persist_checkout_location_outcome  # noqa: PLC0415
                from modules.ai.media.customer_turn_completion import (  # noqa: PLC0415
                    resolve_checkout_location_persist_turn,
                )

                _addr_persisted = False
                _addr_persist_reason = "apply_state_patch_false"
                try:
                    _addr_persisted, _addr_persist_reason = persist_checkout_location_outcome(
                        db,
                        tenant_id=resolved_tenant_id,
                        phone=sender,
                        state_patch=_address_decision.get("state_patch") or {},
                    )
                except Exception as _addr_patch_exc:  # noqa: BLE001
                    logger.warning(
                        "[ORDER_FLOW_STATE] address text state_patch failed "
                        "tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _addr_patch_exc,
                    )
                    _addr_persist_reason = "apply_state_patch_exception"
                _addr_turn = resolve_checkout_location_persist_turn(
                    persist_ok=_addr_persisted,
                    persist_reason=_addr_persist_reason,
                    inbound_type="address_text",
                    inbound_metadata=normalized_inbound.metadata or {},
                    inbound_text=persist_body or text or "",
                    state_patch=_address_decision.get("state_patch") or {},
                )
                if not _addr_turn.get("emit_success_ack"):
                    logger.warning(
                        "[ORDER_FLOW_STATE] address ack skipped persist_failed "
                        "tenant=%s phone=*%s reason=%s completion=%s",
                        resolved_tenant_id,
                        (sender or "")[-4:],
                        _addr_persist_reason,
                        _addr_turn.get("completion_class"),
                    )
                    normalized_inbound.metadata = _addr_turn.get("inbound_metadata")
                    text = str(_addr_turn.get("brain_text") or "")
                    # Continue to the existing Brain owner below — do not
                    # emit the saved ack and do not silent-return.
                else:
                    from core.address_ingest_post_persist import (  # noqa: PLC0415
                        persist_address_ingest_turn_messages,
                        reproject_address_ingest_decision_after_persist,
                    )
                    from core.constrained_operational_compose import (  # noqa: PLC0415
                        resolve_prebrain_reply_text,
                    )

                    _address_decision = reproject_address_ingest_decision_after_persist(
                        db,
                        tenant_id=resolved_tenant_id,
                        phone=sender,
                        inbound_text=persist_body or text or "",
                        address_type=str(
                            (_address_decision.get("state_patch") or {}).get(
                                "delivery_address_type"
                            )
                            or "maps_url"
                        ),
                    )
                    _addr_reply_text, _addr_compose_meta = await resolve_prebrain_reply_text(
                        db=db,
                        tenant_id=resolved_tenant_id,
                        phone=sender,
                        decision=_address_decision,
                        inbound_text=persist_body or text or "",
                    )
                    persist_address_ingest_turn_messages(
                        db,
                        tenant_id=resolved_tenant_id,
                        phone=sender,
                        inbound_body=persist_body or text or "",
                        outbound_body=_addr_reply_text,
                        inbound_event_type="whatsapp_message",
                        extra_metadata={
                            "wa_message_id": msg_id or None,
                            **(_addr_compose_meta or {}),
                        },
                    )
                    await _post_wa(
                        used_pid,
                        {
                            "messaging_product": "whatsapp",
                            "to": sender,
                            "type": "text",
                            "text": {"body": _addr_reply_text},
                        },
                        _tenant_id=resolved_tenant_id,
                        _db=db,
                    )
                    return

            if _payment_method_decision is not None:
                if _payment_method_decision.get("state_patch"):
                    try:
                        apply_state_patch(
                            db,
                            tenant_id=resolved_tenant_id,
                            phone=sender,
                            state_patch=_payment_method_decision["state_patch"],
                        )
                    except Exception as _pm_patch_exc:  # noqa: BLE001
                        logger.warning(
                            "[ORDER_FLOW_STATE] payment_method state_patch failed "
                            "tenant=%s phone=%s err=%s",
                            resolved_tenant_id, sender, _pm_patch_exc,
                        )
                from core.constrained_operational_compose import (  # noqa: PLC0415
                    resolve_prebrain_reply_text,
                )

                _pm_reply_text, _pm_compose_meta = await resolve_prebrain_reply_text(
                    db=db,
                    tenant_id=resolved_tenant_id,
                    phone=sender,
                    decision=_payment_method_decision,
                    inbound_text=text or "",
                )
                await _post_wa(
                    used_pid,
                    {
                        "messaging_product": "whatsapp",
                        "to": sender,
                        "type": "text",
                        "text": {"body": _pm_reply_text},
                    },
                    _tenant_id=resolved_tenant_id,
                    _db=db,
                )
                return

            if _evidence_decision is not None:
                # Pre-transfer-review or pending-evidence inbound.
                # We send a short polite sentence and DO NOT mutate
                # order state. The customer's funnel remains open;
                # the next inbound (hopefully the real receipt) is
                # handled by the regular path.
                _pe_status = (normalized_inbound.metadata or {}).get(
                    "payment_evidence_status"
                )
                _pe_reason = (normalized_inbound.metadata or {}).get(
                    "payment_evidence_reason"
                )
                logger.info(
                    "[PAYMENT_EVIDENCE] short_circuit=evidence_soft "
                    "tenant=%s phone=*%s payment_evidence_status=%s "
                    "payment_evidence_reason=%s next_action=send_soft_ack",
                    resolved_tenant_id,
                    sender[-4:] if sender else "",
                    _pe_status, _pe_reason,
                )
                try:
                    from core.inbound_lifecycle import (  # noqa: PLC0415
                        EVENT_PAYMENT_SHORT_CIRCUIT, record_lifecycle,
                    )
                    record_lifecycle(
                        EVENT_PAYMENT_SHORT_CIRCUIT,
                        detail=(
                            f"kind=evidence_soft "
                            f"status={_pe_status} "
                            f"reason={_pe_reason}"
                        ),
                    )
                except Exception:
                    pass
                # ── W2.0.3: Conversation-linking integrity (fail-open).
                _w203_conv_id_ev: Optional[int] = None
                try:
                    from routers.conversations import (  # noqa: PLC0415
                        _get_or_create_conversation as _w203_resolve_ev,
                    )
                    _w203_convo_ev = _w203_resolve_ev(
                        db, resolved_tenant_id, sender,
                    )
                    _w203_conv_id_ev = (
                        int(getattr(_w203_convo_ev, "id", 0) or 0)
                        or None
                    )
                    try:
                        from core.inbound_lifecycle import (  # noqa: PLC0415
                            EVENT_AUTO_LINK_OK, record_lifecycle as _rl_ev,
                        )
                        _rl_ev(
                            EVENT_AUTO_LINK_OK,
                            detail="branch=payment_evidence",
                            conversation_id=_w203_conv_id_ev,
                        )
                    except Exception:
                        pass
                except Exception as _w203_exc_ev:  # noqa: BLE001
                    logger.warning(
                        "[PAYMENT_EVIDENCE] auto-link failed "
                        "(fail-open to legacy orphan write) "
                        "tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _w203_exc_ev,
                    )
                    try:
                        from core.inbound_lifecycle import (  # noqa: PLC0415
                            EVENT_AUTO_LINK_FAILED, record_lifecycle as _rl_ev,
                        )
                        _rl_ev(
                            EVENT_AUTO_LINK_FAILED,
                            detail=(
                                f"branch=payment_evidence "
                                f"exc={type(_w203_exc_ev).__name__}"
                            ),
                        )
                    except Exception:
                        pass
                    _w203_conv_id_ev = None
                # Persist the customer's inbound so the merchant
                # drawer keeps the original PDF / image alongside
                # our soft reply.
                try:
                    StateManager.save_message(
                        db,
                        phone=sender,
                        direction="inbound",
                        body=persist_body or "[إثبات دفع غير مؤكد]",
                        event_type=(
                            "whatsapp_document"
                            if normalized_inbound.normalized_type == "document"
                            else "whatsapp_image"
                        ),
                        conversation_id=_w203_conv_id_ev,
                        tenant_id=resolved_tenant_id,
                        extra_metadata={
                            "normalized_inbound": normalized_inbound.metadata,
                            "wa_message_id": msg_id or None,
                            "payment_evidence_short_circuit": True,
                        },
                    )
                except Exception as _ev_in_exc:  # noqa: BLE001
                    logger.warning(
                        "[PAYMENT_EVIDENCE] inbound save failed "
                        "tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _ev_in_exc,
                    )
                from core.constrained_operational_compose import (  # noqa: PLC0415
                    resolve_prebrain_reply_text,
                )

                _ev_reply_text, _ev_compose_meta = await resolve_prebrain_reply_text(
                    db=db,
                    tenant_id=resolved_tenant_id,
                    phone=sender,
                    decision=_evidence_decision,
                    inbound_text=persist_body or text or "",
                )
                try:
                    StateManager.save_message(
                        db,
                        phone=sender,
                        direction="outbound",
                        body=_ev_reply_text,
                        event_type="whatsapp_message",
                        conversation_id=_w203_conv_id_ev,
                        tenant_id=resolved_tenant_id,
                        extra_metadata={
                            "is_ai": True,
                            "deterministic_path": "payment_evidence_soft_ack",
                            "payment_evidence_status": _pe_status,
                            "payment_evidence_reason": _pe_reason,
                            **_ev_compose_meta,
                        },
                    )
                except Exception as _ev_out_exc:  # noqa: BLE001
                    logger.warning(
                        "[PAYMENT_EVIDENCE] outbound save failed "
                        "tenant=%s phone=%s err=%s",
                        resolved_tenant_id, sender, _ev_out_exc,
                    )
                await _post_wa(
                    used_pid,
                    {
                        "messaging_product": "whatsapp",
                        "to": sender,
                        "type": "text",
                        "text": {
                            "body": _ev_reply_text,
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
            try:
                from core.inbound_lifecycle import (  # noqa: PLC0415
                    EVENT_BRAIN_INVOKED, record_lifecycle,
                )
                record_lifecycle(EVENT_BRAIN_INVOKED)
            except Exception:
                pass
            await _handle_merchant_message(
                phone_id=used_pid, to=sender, text=text,
                tenant_id=resolved_tenant_id, db=db,
                inbound_metadata=normalized_inbound.metadata,
                inbound_persist_body=persist_body,
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

        elif action == SHOW_PLAN_DETAILS:
            await _send_plan_details_message(
                phone_id=used_pid, to=sender, db=db,
                _tenant_id=effective_tenant_id,
            )

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
        StateManager.save_message(db, sender, persist_body,          "inbound",  tenant_id=effective_tenant_id)
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
        # Measurement-only: finalize + unbind turn latency ContextVar.
        try:
            from core.turn_latency import reset_turn_latency  # noqa: PLC0415

            if _turn_latency is not None:
                try:
                    _turn_latency.snapshot(finalize_total=True)
                    _turn_latency.emit_log()
                except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                    pass
            if _turn_latency_token is not None:
                reset_turn_latency(_turn_latency_token)
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            pass
        try:
            db.close()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# MERCHANT AI HANDLER — bypasses platform sales logic entirely
# ═══════════════════════════════════════════════════════════════════════════════

async def _send_cod_followup_message(
    *, phone_id: str, to: str, decision: str, order: Any,
    _tenant_id: Optional[int] = None, _db=None,
) -> None:
    """
    Complete the customer-visible COD outcome after the button is processed.

    Confirmation uses the canonical approved order-confirmation template;
    it is emitted only after Salla creation/status mutation succeeded.  Cancel
    keeps the existing in-window acknowledgement.  Failed provider mutations
    make no success claim.
    """
    if decision == "confirm":
        from services.cod_confirmation import (  # noqa: PLC0415
            send_order_confirmation_after_cod,
        )
        result = await send_order_confirmation_after_cod(
            _db,
            tenant_id=int(_tenant_id),
            order=order,
        )
        logger.info(
            "[COD] post-confirmation order template tenant=%s order=%s sent=%s duplicate=%s error=%s",
            _tenant_id,
            getattr(order, "id", None),
            result.get("sent"),
            result.get("duplicate"),
            result.get("error"),
        )
        return
    if decision == "cancel":
        body = (
            f"تم إلغاء طلبك #{order.id} بنجاح.\n"
            f"إذا كان هناك أي خطأ يمكنك إعادة الطلب في أي وقت."
        )
        await _send_whatsapp_message(
            phone_id=phone_id, to=to, text=body,
            _tenant_id=_tenant_id, _db=_db,
        )
        return
    logger.error(
        "[COD] no success followup after provider mutation failure tenant=%s order=%s decision=%s",
        _tenant_id,
        getattr(order, "id", None),
        decision,
    )


def _persist_inbound_only(
    *,
    db,
    tenant_id: int,
    sender: str,
    msg_type: str,
    normalized_type: str,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    wa_msg_id: Optional[str] = None,
    drop_reason: str = "",
    placeholder_body: str = "[رسالة وسائط بدون نص قابل للقراءة]",
) -> bool:
    """Best-effort persist of an inbound message that did NOT reach the
    brain — to guarantee EVERY inbound row that Meta hands
    us appears in the merchant's conversation list, even when the
    normalizer returned no usable text or the type fell outside the
    brain's allow-list.

    This is the single backstop the May 2026 #41 audit exposed:
    pre-fix, an image / video / sticker without caption could be
    dropped silently at the type-allow-list guard or the empty-text
    guard with NO conversation row, leaving the merchant convinced
    the bot "ignored" the customer when the row was simply never
    written.

    Contract:

      1. Look up / create the dashboard conversation so the merchant
         sees the message in the inbox.
      2. Persist a single INBOUND ``MessageEvent`` carrying the full
         normalized metadata (storage_url, transcript_status, mime,
         caption, etc.) plus a ``drop_reason`` field so the dashboard
         media-debug pane can explain "why no AI reply" without
         re-running anything.
      3. Emit a structured ``[INBOUND_MEDIA_STORE]`` log line on
         success and a ``[INBOUND_MEDIA_ERROR]`` on any failure.
      4. NEVER raise — the webhook ack loop must complete regardless
         of bookkeeping failures here.

    Returns ``True`` when the inbound row was persisted, ``False``
    otherwise. The caller can use the return value to decide whether
    to send a courtesy reply (we deliberately do not couple sending
    to persistence — a failed persist must not silence the bot).
    """
    meta = dict(inbound_metadata or {})
    has_caption = bool((meta.get("caption") or "").strip())
    mime_type = (meta.get("mime_type") or "")
    media_id = meta.get("media_id") or ""
    try:
        from routers.conversations import _get_or_create_conversation  # noqa: PLC0415
        convo = _get_or_create_conversation(db, tenant_id, sender)
        StateManager.save_message(
            db, sender, placeholder_body, "inbound",
            conversation_id=convo.id,
            tenant_id=tenant_id,
            extra_metadata={
                "normalized_inbound": meta,
                "media_persist_only": True,
                "drop_reason":        drop_reason or "",
                "wa_message_id":      wa_msg_id or "",
            },
        )
        logger.info(
            "[INBOUND_MEDIA_STORE] tenant_id=%s sender=%s msg_type=%s "
            "normalized_type=%s mime=%s media_id=%s has_caption=%s "
            "wa_msg_id=%s convo_id=%s drop_reason=%s persisted=True",
            tenant_id, sender, msg_type, normalized_type,
            mime_type or "-", media_id or "-", has_caption,
            wa_msg_id or "-", getattr(convo, "id", None),
            drop_reason or "-",
        )
        try:
            from core.inbound_lifecycle import (  # noqa: PLC0415
                EVENT_PERSIST_INBOUND_ONLY_OK, record_lifecycle,
            )
            record_lifecycle(
                EVENT_PERSIST_INBOUND_ONLY_OK,
                detail=f"drop_reason={drop_reason or '-'}",
            )
        except Exception:
            pass
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[INBOUND_MEDIA_ERROR] tenant_id=%s sender=%s msg_type=%s "
            "normalized_type=%s wa_msg_id=%s drop_reason=%s persisted=False "
            "err=%s",
            tenant_id, sender, msg_type, normalized_type,
            wa_msg_id or "-", drop_reason or "-", exc,
        )
        try:
            from core.inbound_lifecycle import (  # noqa: PLC0415
                EVENT_PERSIST_INBOUND_ONLY_FAIL, record_lifecycle,
            )
            record_lifecycle(
                EVENT_PERSIST_INBOUND_ONLY_FAIL,
                detail=f"exc={type(exc).__name__} drop_reason={drop_reason or '-'}",
            )
        except Exception:
            pass
        return False


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
        try:
            from core.inbound_lifecycle import (  # noqa: PLC0415
                EVENT_MEDIA_FALLBACK_OK, record_lifecycle,
            )
            record_lifecycle(EVENT_MEDIA_FALLBACK_OK)
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[MediaFallback] failed to persist inbound row tenant=%s "
            "to=%s err=%s",
            tenant_id, to, exc,
        )
        try:
            from core.inbound_lifecycle import (  # noqa: PLC0415
                EVENT_MEDIA_FALLBACK_FAIL, record_lifecycle,
            )
            record_lifecycle(
                EVENT_MEDIA_FALLBACK_FAIL,
                detail=f"exc={type(exc).__name__}",
            )
        except Exception:
            pass

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


def _build_customer_ai_profile(
    customer: Any,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the in-process customer profile passed to Brain.

    Administrative display labels may legitimately occupy ``Customer.name``
    with proposed authority.  Only the resolver snapshot can distinguish them
    from an approved personal identity, so raw model/profile aliases are never
    used to make that decision here.
    """
    from core.customer_display import (  # noqa: PLC0415
        RESOLVED_CUSTOMER_IDENTITY_CONTEXT_KEY,
        approved_personalization_customer_name_or_fallback,
    )
    from core.customer_identity_resolver import read_customer_identity  # noqa: PLC0415

    identity = read_customer_identity(customer)
    approved_name = approved_personalization_customer_name_or_fallback(
        identity,
        fallback="",
    )
    return {
        RESOLVED_CUSTOMER_IDENTITY_CONTEXT_KEY: identity,
        "name": approved_name,
        "customer_name": approved_name,
        "email": getattr(customer, "email", None) or "",
        "id": getattr(customer, "id", None),
        "inbound_metadata": dict(inbound_metadata or {}),
    }


async def _handle_merchant_message(
    phone_id: str,
    to: str,
    text: str,
    tenant_id: int,
    db,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    inbound_persist_body: Optional[str] = None,
    wa_message_ts: Optional[datetime] = None,
    wa_msg_id: Optional[str] = None,
    commerce_runtime_claim: Optional[Any] = None,
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
    _catalog_order_structured_continue = False
    _location_persist_failure_continue = False
    if not (text or "").strip():
        try:
            from modules.ai.media.customer_turn_completion import (  # noqa: PLC0415
                customer_authored_catalog_order_text,
                customer_authored_location_continue_text,
                should_continue_checkout_location_persist_failure,
                should_continue_structured_catalog_order,
            )

            _cat_meta = (
                inbound_metadata if isinstance(inbound_metadata, dict) else {}
            )
            if should_continue_checkout_location_persist_failure(_cat_meta):
                _location_persist_failure_continue = True
                text = customer_authored_location_continue_text(
                    _cat_meta,
                    inbound_persist_body or "",
                )
            elif should_continue_structured_catalog_order(
                _cat_meta,
                inbound_persist_body or "",
            ):
                _catalog_order_structured_continue = True
                text = customer_authored_catalog_order_text(
                    _cat_meta,
                    inbound_persist_body or "",
                )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog restore must not block merchant entry
            pass
    if (
        not (text or "").strip()
        and not _catalog_order_structured_continue
        and not _location_persist_failure_continue
    ):
        _unclear_audio_order_support = False
        try:
            from modules.ai.media.routing_guard import (  # noqa: PLC0415
                should_route_unclear_audio_to_existing_order_support,
            )

            _inbound_meta = (
                dict(inbound_metadata or {})
                if isinstance(inbound_metadata, dict)
                else {}
            )
            _unclear_audio_order_support = should_route_unclear_audio_to_existing_order_support(
                inbound_metadata=_inbound_meta,
                semantic_message="",
                inbound_normalized_type=str(
                    _inbound_meta.get("inbound_normalized_type")
                    or _inbound_meta.get("normalized_type")
                    or _inbound_meta.get("type")
                    or "audio"
                ),
                history=StateManager.load_history(
                    db,
                    phone=to,
                    tenant_id=tenant_id,
                ),
            )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — unclear-audio gate must not block merchant entry
            _unclear_audio_order_support = False
        if not _unclear_audio_order_support:
            # Hard guard: refuse to spend tokens / send replies on empty
            # inbound. Empty body usually means the upstream parser failed
            # to extract text from a non-text message type.
            logger.info(
                "[Merchant] DROPPED empty inbound — no reply generated | tenant=%s to=%s",
                tenant_id, to,
            )
            try:
                from core.inbound_lifecycle import (  # noqa: PLC0415
                    EVENT_END_DROPPED, record_lifecycle,
                )
                record_lifecycle(
                    EVENT_END_DROPPED,
                    detail="merchant_empty_text_guard",
                )
            except Exception:
                pass
            return
    logger.info(
        "[Merchant/INBOUND_TRIGGER] tenant=%s from=%s direction=inbound text_len=%d snippet=%r",
        tenant_id, to, len(text or ""), (text or "")[:60],
    )

    # ── Turn lifecycle observability (May 2026 #16) ─────────────────────────
    # One ``TurnTrace`` per inbound message. Every layer of the pipeline
    # (pause guard / mode resolver / brain / composer / fallback) mutates
    # fields on this object. A single ``[TURN] ...`` log line is emitted
    # in the ``finally`` block so partial-progress turns still produce
    # one greppable record.
    #
    # The flag ``_trace.outbound_sent`` is also used defensively by the
    # safe-reply path to skip a second send when a primary reply has
    # already gone out for this same message_id — see
    # ``mark_outbound_sent`` / ``outbound_lock_acquired`` semantics.
    from services import turn_trace as _TS  # noqa: PLC0415
    _trace = _TS.new_trace(
        tenant_id    = tenant_id,
        phone        = to,
        message_id   = wa_msg_id or "",
        inbound_text = text or "",
    )
    # Attach correlated turn_id on the pre-lock TurnLatency ContextVar.
    # Do NOT store the live TurnLatency object on trace.extra (not JSON-safe).
    try:
        from core.turn_latency import (  # noqa: PLC0415
            get_turn_latency,
            new_turn_latency,
            bind_turn_latency,
        )

        _timing = get_turn_latency()
        if _timing is None:
            _timing = new_turn_latency(
                tenant_id=int(tenant_id),
                message_id=str(wa_msg_id or ""),
            )
            bind_turn_latency(_timing)
        else:
            _timing.set_identity(
                tenant_id=int(tenant_id),
                message_id=str(wa_msg_id or ""),
            )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass
    from modules.ai.brain.persona_ownership import (  # noqa: PLC0415
        PersonaBypassReason as _POReason,
        PersonaOwnershipRecord as _PORecord,
        sync_persona_to_turn_trace as _sync_po_trace,
    )
    _persona_ownership = _PORecord()
    _brain_reply_candidate = ""
    _outbound_abort_suppressor = ""
    _outbound_abort_audited = False
    _outbound_event_id = None
    _wire_audit_token = None
    _outbound_customer_id: int | None = None
    _t_merchant_entry_gates = None
    _generic_handoff_signal = False
    _d2_execution_result = None
    try:
        from modules.ai.brain.execution.staff_escalation_execution import (  # noqa: PLC0415
            clear_current_turn_d2_result as _d2_clear_turn,
        )

        _d2_clear_turn(db)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — leftover D2 stamp clear must not block turn
        pass

    def _sync_persona_observability() -> None:
        _sync_po_trace(_trace, _persona_ownership)

    # ── P0 AI disabled kill switch (before ANY outbound / brain path) ───────
    try:
        import time as _time_meg  # noqa: PLC0415

        _t_merchant_entry_gates = _time_meg.monotonic()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        _t_merchant_entry_gates = None
    try:
        from core.ai_disabled_gate import (  # noqa: PLC0415
            is_ai_disabled_for_conversation,
            log_ai_disabled_gate,
            persist_inbound_for_suppressed_turn,
        )

        _ai_off = is_ai_disabled_for_conversation(
            db,
            tenant_id=tenant_id,
            customer_phone=to,
            source="merchant_webhook_entry",
        )
        if _ai_off.disabled:
            _suppressed_convo = persist_inbound_for_suppressed_turn(
                db,
                tenant_id=tenant_id,
                customer_phone=to,
                inbound_body=(inbound_persist_body or text or "").strip(),
                wa_msg_id=wa_msg_id,
                wa_message_ts=wa_message_ts,
                inbound_metadata=inbound_metadata,
                suppression_reason=_ai_off.reason,
            )
            log_ai_disabled_gate(
                tenant_id=tenant_id,
                customer_phone=to,
                decision=_ai_off,
                source="merchant_webhook_entry",
            )
            try:
                _trace.paused = True
                _trace.fallback_source = "ai_disabled_gate"
                _trace.response_goal = "suppressed"
                _trace.reply_source = _TS.SOURCE_PAUSED
            except Exception:  # noqa: silent-ok — trace stamp must not block suppression
                pass
            try:
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
            _sync_persona_observability()
            return
    except Exception as _ai_gate_exc:  # noqa: BLE001  # noqa: silent-ok — gate must not open on error
        from core.handoff_truth import (  # noqa: PLC0415
            REASON_GATE_VERIFY_FAILED,
            evaluate_gate_error_fail_closed,
        )

        if evaluate_gate_error_fail_closed(
            db,
            tenant_id=tenant_id,
            customer_phone=to,
            gate="merchant_webhook_entry",
            error=_ai_gate_exc,
        ):
            try:
                from core.ai_disabled_gate import (  # noqa: PLC0415
                    AIDisabledDecision,
                    log_ai_disabled_gate,
                    persist_inbound_for_suppressed_turn,
                )

                _suppressed_convo = persist_inbound_for_suppressed_turn(
                    db,
                    tenant_id=tenant_id,
                    customer_phone=to,
                    inbound_body=(inbound_persist_body or text or "").strip(),
                    wa_msg_id=wa_msg_id,
                    wa_message_ts=wa_message_ts,
                    inbound_metadata=inbound_metadata,
                    suppression_reason=REASON_GATE_VERIFY_FAILED,
                )
                log_ai_disabled_gate(
                    tenant_id=tenant_id,
                    customer_phone=to,
                    decision=AIDisabledDecision(
                        disabled=True,
                        reason=REASON_GATE_VERIFY_FAILED,
                        conversation=_suppressed_convo,
                        source="merchant_webhook_entry",
                    ),
                    source="merchant_webhook_entry",
                )
                try:
                    _trace.paused = True
                    _trace.fallback_source = "ai_gate_fail_closed"
                    _trace.response_goal = "suppressed"
                    _trace.reply_source = _TS.SOURCE_PAUSED
                except Exception:  # noqa: silent-ok
                    pass
                try:
                    db.commit()
                except Exception:
                    try:
                        db.rollback()
                    except Exception:
                        pass
                _sync_persona_observability()
                return
            except Exception as _fail_closed_exc:  # noqa: BLE001
                logger.warning(
                    "[AI_GATE_FAIL_CLOSED] persist failed tenant=%s to=%s err=%s",
                    tenant_id, to, _fail_closed_exc,
                )
        logger.warning(
            "[AI_DISABLED_GATE] entry check failed tenant=%s to=%s err=%s",
            tenant_id, to, _ai_gate_exc,
        )

    # ── Structured admin / L3 direct contact (Operations Center) ────────
    # Must run BEFORE generic handoff so «أبي الإدارة» delivers configured
    # admin contacts instead of owner_vague clarifier or Brain/KB refusal.
    try:
        from modules.operations.structured_admin_contact_policy import (  # noqa: PLC0415
            evaluate_structured_admin_contact_policy as _eval_structured_admin,
        )
        _sac_decision = _eval_structured_admin(
            db,
            tenant_id=tenant_id,
            message=text or "",
        )
    except Exception as _sac_exc:  # noqa: BLE001
        logger.warning(
            "[STRUCTURED_ADMIN_REQUEST] pre-brain check failed tenant=%s err=%s",
            tenant_id, _sac_exc,
        )
        _sac_decision = None

    if _sac_decision is not None:
        _persona_ownership.mark_bypass(
            _POReason.STAFF_CONTACT_RECOVERY,
            owner="structured_admin_contact",
        )
        _sac_reply = _sac_decision.reply_text
        _sac_text_ok = False
        _sac_convo = None
        try:
            from routers.conversations import _get_or_create_conversation  # noqa: PLC0415
            _sac_convo = _get_or_create_conversation(db, tenant_id, to)
        except Exception as _sac_conv_exc:  # noqa: BLE001
            logger.warning(
                "[STRUCTURED_ADMIN_REQUEST] conversation lookup failed tenant=%s err=%s",
                tenant_id, _sac_conv_exc,
            )
        try:
            _sac_text_ok = await _send_whatsapp_message(
                phone_id=phone_id, to=to, text=_sac_reply,
                _tenant_id=tenant_id, _db=db,
            )
            if _sac_convo is not None:
                StateManager.save_message(
                    db, to, _sac_reply, "outbound",
                    conversation_id=_sac_convo.id, tenant_id=tenant_id,
                    extra_metadata={
                        **_persona_ownership.to_metadata(),
                        "deterministic_path": "structured_admin_contact",
                        "structured_admin_reason": _sac_decision.reason,
                        "structured_admin_branch_id": _sac_decision.branch_id,
                        "structured_admin_level": _sac_decision.escalation_level,
                        "structured_admin_deliver": _sac_decision.deliver_contact,
                    },
                )
        except Exception as _sac_send_exc:  # noqa: BLE001
            logger.warning(
                "[STRUCTURED_ADMIN_REQUEST] text send failed tenant=%s err=%s",
                tenant_id, _sac_send_exc,
            )

        _sac_contacts_ok = False
        if _sac_decision.deliver_contact and _sac_decision.call_targets:
            try:
                from services.call_resolver import build_contacts_payload  # noqa: PLC0415
                _sac_payload = build_contacts_payload(
                    list(_sac_decision.call_targets), to=to,
                )
                _sac_contacts_ok = await _send_contacts_message(
                    phone_id=phone_id, to=to,
                    payload=_sac_payload,
                    _tenant_id=tenant_id, _db=db,
                    customer_message=text or "",
                    delivery_path="structured_admin_contact",
                    policy_deliver_contact=bool(_sac_decision.deliver_contact),
                )
                if _sac_contacts_ok:
                    try:
                        from modules.ai.brain.commerce.contact_escalation import (  # noqa: PLC0415
                            persist_staff_contact_sent,
                        )
                        from core.order_flow import _load_brain_state  # noqa: PLC0415

                        _sac_conv2, _sac_bs = _load_brain_state(
                            db, tenant_id=tenant_id, phone=to,
                        )
                        _sac_turn = int((_sac_bs or {}).get("turn") or 0)
                        for _sac_target in _sac_decision.call_targets:
                            persist_staff_contact_sent(
                                db,
                                tenant_id=tenant_id,
                                phone=to,
                                name=getattr(_sac_target, "name", "") or "",
                                contact_phone=(
                                    getattr(_sac_target, "raw_phone", "")
                                    or getattr(_sac_target, "wa_id", "")
                                ),
                                turn=_sac_turn,
                            )
                    except Exception as _sac_persist_exc:  # noqa: BLE001
                        logger.exception(
                            "[STRUCTURED_ADMIN_REQUEST] persist failed tenant=%s",
                            tenant_id,
                        )
            except Exception as _sac_card_exc:  # noqa: BLE001
                logger.warning(
                    "[STRUCTURED_ADMIN_REQUEST] vCard send failed tenant=%s err=%s",
                    tenant_id, _sac_card_exc,
                )

        logger.info(
            "[STRUCTURED_ADMIN_REQUEST] short_circuit tenant=%s to=%s "
            "deliver=%s text_ok=%s contacts_ok=%s skip_brain=true reason=%s",
            tenant_id, to,
            _sac_decision.deliver_contact,
            _sac_text_ok, _sac_contacts_ok,
            _sac_decision.reason,
        )
        try:
            _trace.fallback_source = "structured_admin_contact"
            _trace.response_goal = "structured_admin_contact"
            _trace.intent = "structured_admin_contact"
            if _sac_text_ok:
                _trace.mark_outbound_sent(
                    source=_TS.SOURCE_BRAIN,
                    length=len(_sac_reply or ""),
                )
        except Exception:  # noqa: silent-ok — trace stamp must not block admin contact delivery
            pass
        _sync_persona_observability()
        return

    # ── PRE-BRAIN HANDOFF GUARD (May 2026 critical hotfix) ─────────
    # Production regression: a customer typing "أبي أتكلم مع أحد"
    # would receive "حصل خطأ مؤقت 🙏 ممكن تعيد رسالتك؟" whenever the
    # brain raised — and the conversation NEVER landed in the
    # merchant's "طلب موظف" filter. Every prior fix sat INSIDE the
    # brain try-block, so any earlier failure swallowed the request.
    #
    # The guard below runs BEFORE any brain / DB-heavy path:
    #
    #   1. Pure-string detector ``is_handoff_request`` (Arabic
    #      normalised, no I/O) → True for "ابي اتكلم مع احد",
    #      "كلموني", "حولني لموظف", "احد يرد علي", etc.
    #   2. Find/create the conversation row and flip the FULL
    #      canonical handoff signal: needs_human + handoff_active +
    #      is_human_handoff + status="human".
    #   3. Create a handoff_session via ``handoff.manager`` so the
    #      merchant inbox shows a proper "طلب موظف" entry.
    #   4. ``pause_ai(REASON_HUMAN_HANDOFF)`` so subsequent inbounds
    #      stop looping the AI pipeline.
    #   5. Send the canonical Arabic acknowledgement, record the
    #      outbound, return cleanly. The brain is NEVER reached for
    #      this turn — guaranteeing no exception can swallow the
    #      handoff intent.
    #
    # Every nested step has its own try/except. A failure at any
    # step still produces the acknowledgement send + early return so
    # the customer never sees the generic retry copy for an explicit
    # human-handoff request.
    try:
        from core.handoff_detector import (  # noqa: PLC0415
            is_handoff_request as _is_handoff_req,
            is_owner_contact_request as _is_owner_contact_req,
            is_post_payment_modification_request as _is_post_pay_mod_req,
            HANDOFF_ACK_TEXT_AR as _HANDOFF_ACK_TEXT,
            HANDOFF_OWNER_ACK_TEXT_AR as _HANDOFF_OWNER_ACK_TEXT,
            HANDOFF_POST_PAYMENT_ACK_TEXT_AR as _HANDOFF_POST_PAY_ACK_TEXT,
        )
        _is_handoff = _is_handoff_req(text or "")
        _is_post_pay_mod = _is_post_pay_mod_req(text or "")
        _is_owner_contact = _is_owner_contact_req(text or "")
        # Owner-contact phrasings ("أبي أتواصل مع المالك" / "اكلم
        # المالك") are a SUBSET of handoff intent — the customer
        # explicitly chose the owner/management framing. We:
        #   1. Promote them to the handoff path even if the generic
        #      ``is_handoff_request`` substring scan happened to
        #      miss the wording (defence in depth).
        #   2. Override the ack text below so the customer sees the
        #      clarifier-style copy ("ممكن توضح سبب التواصل مع
        #      المالك؟") instead of the generic team line.
        if _is_owner_contact and not _is_handoff:
            _is_handoff = True
            logger.info(
                "[Merchant/HANDOFF_GUARD] owner-contact phrase promoted "
                "to handoff | tenant=%s to=%s snippet=%r",
                tenant_id, to, (text or "")[:80],
            )
    except Exception as _hd_exc:  # noqa: BLE001
        logger.debug("[Merchant/HANDOFF_GUARD] detector failed: %s", _hd_exc)
        _is_handoff = False
        _is_post_pay_mod = False
        _is_owner_contact = False

    # Post-payment modification check piggybacks on the same handoff
    # plumbing. We only promote it to a handoff when the customer
    # has actually paid (or is in a post-payment state) — otherwise
    # "ابي اضيف عسل" pre-payment must keep flowing into the brain
    # so the catalog flow can add the new product to the cart.
    if _is_post_pay_mod and not _is_handoff:
        try:
            from core.order_flow import (  # noqa: PLC0415
                _load_brain_state as _ho_load_state,
                _focus_summary as _ho_focus_summary,
            )
            _ho_state_conv, _ho_bs = _ho_load_state(
                db, tenant_id=tenant_id, phone=to,
            )
            _ho_summary = _ho_focus_summary(_ho_bs)
            _post_paid = (
                bool(_ho_summary.get("payment_receipt_received"))
                or str(_ho_summary.get("order_status") or "").lower()
                in ("under_review", "processing", "payment_pending")
            )
        except Exception as _ho_post_exc:  # noqa: BLE001
            logger.debug(
                "[Merchant/HANDOFF_GUARD] post-pay state load failed: %s",
                _ho_post_exc,
            )
            _post_paid = False
        if _post_paid:
            # Reuse the canonical handoff branch below but with the
            # post-payment acknowledgement copy.
            _is_handoff = True
            _HANDOFF_ACK_TEXT = _HANDOFF_POST_PAY_ACK_TEXT
            logger.info(
                "[Merchant/HANDOFF_GUARD] PRE-BRAIN post-payment "
                "modification → handoff | tenant=%s to=%s snippet=%r",
                tenant_id, to, (text or "")[:80],
            )

    if _is_handoff:
        # ── May 2026 #44 + #46 — Owner-contact escalation TIERS ──────
        # Tier resolution is unchanged from #44; the pause/flip
        # decision is now centralised in
        # ``core.handoff_detector.resolve_handoff_pause_policy``
        # which encodes the Tenant-33 #46 policy:
        #
        #   "الإيقاف الكامل للذكاء يجب أن يكون يدويًا فقط من الموظف
        #    داخل لوحة نحلة."
        #
        # Concretely:
        #   * VAGUE     — clarifier ack + soft needs_human flag.
        #                 No full flip, no session, AI alive.
        #   * CLEAR     — full flip + session so the dashboard's
        #                 "طلب موظف" filter sees the entry. AI alive.
        #   * COMPLAINT — full flip + session, apologetic ack. AI
        #                 alive (the customer often asks unrelated
        #                 product questions while waiting; we must
        #                 not silence the brain).
        #   * generic   — full flip + session. AI alive.
        #
        # ALL tiers return ``do_pause_ai=False``. Manual pause from
        # the dashboard is the only path that still silences the AI.
        from core.handoff_detector import (  # noqa: PLC0415
            classify_owner_escalation_tier as _classify_owner_tier,
            resolve_handoff_pause_policy as _resolve_handoff_policy,
            GENERIC_HANDOFF_TIER as _GENERIC_HANDOFF_TIER,
            OWNER_TIER_CLEAR as _OWNER_TIER_CLEAR,
            OWNER_TIER_COMPLAINT as _OWNER_TIER_COMPLAINT,
            OWNER_TIER_VAGUE as _OWNER_TIER_VAGUE,
            HANDOFF_OWNER_HANDOFF_TEXT_AR as _HANDOFF_OWNER_HANDOFF_TEXT,
            HANDOFF_OWNER_COMPLAINT_TEXT_AR as _HANDOFF_OWNER_COMPLAINT_TEXT,
        )

        _ho_tier = _GENERIC_HANDOFF_TIER

        if _is_owner_contact:
            _ho_tier = _classify_owner_tier(text or "")
            if _ho_tier == _OWNER_TIER_VAGUE:
                _HANDOFF_ACK_TEXT = _HANDOFF_OWNER_ACK_TEXT
            elif _ho_tier == _OWNER_TIER_CLEAR:
                _HANDOFF_ACK_TEXT = _HANDOFF_OWNER_HANDOFF_TEXT
            elif _ho_tier == _OWNER_TIER_COMPLAINT:
                _HANDOFF_ACK_TEXT = _HANDOFF_OWNER_COMPLAINT_TEXT

        _ho_policy             = _resolve_handoff_policy(_ho_tier)
        _do_full_handoff_flip  = _ho_policy["do_full_handoff_flip"]
        _do_create_session     = _ho_policy["do_create_session"]
        _do_pause_ai           = _ho_policy["do_pause_ai"]

        from modules.ai.brain.execution.staff_escalation_execution import (  # noqa: PLC0415
            should_defer_generic_prebrain_execution as _defer_generic_prebrain,
        )
        if _defer_generic_prebrain(
            is_handoff=True,
            is_owner_contact=bool(_is_owner_contact),
            is_post_pay_mod=bool(_is_post_pay_mod),
            tier=_ho_tier,
        ):
            _generic_handoff_signal = True
            logger.info(
                "[Merchant/HANDOFF_GUARD] generic_handoff deferred to Brain/D2 "
                "| tenant=%s to=%s snippet=%r",
                tenant_id, to, (text or "")[:80],
            )
        else:
            _handoff_vcard_target = None
            if (
                not _is_owner_contact
                and _ho_tier == _GENERIC_HANDOFF_TIER
                and not _is_post_pay_mod
            ):
                try:
                    from modules.ai.brain.commerce.staff_contact_policy import (  # noqa: PLC0415
                        evaluate_generic_handoff_contact_policy as _eval_gh_policy,
                    )
                    _gh_policy = _eval_gh_policy(
                        db,
                        tenant_id=tenant_id,
                        message=text or "",
                        customer_phone=to or "",
                    )
                    if _gh_policy is not None:
                        _HANDOFF_ACK_TEXT = _gh_policy.reply_text
                        if _gh_policy.deliver_contact:
                            _handoff_vcard_target = _gh_policy.call_target
                except Exception as _gh_pol_exc:  # noqa: BLE001
                    logger.debug(
                        "[Merchant/HANDOFF_GUARD] generic contact policy failed: %s",
                        _gh_pol_exc,
                    )

            logger.info(
                "[Merchant/HANDOFF_GUARD] PRE-BRAIN handoff fired | tenant=%s "
                "to=%s text_snippet=%r owner_contact=%s tier=%s "
                "full_flip=%s create_session=%s pause_ai=%s",
                tenant_id, to, (text or "")[:80], _is_owner_contact, _ho_tier,
                _do_full_handoff_flip, _do_create_session, _do_pause_ai,
            )
            _ho_convo = None
            try:
                from routers.conversations import _get_or_create_conversation  # noqa: PLC0415
                _ho_convo = _get_or_create_conversation(db, tenant_id, to)
            except Exception as _ho_conv_exc:  # noqa: BLE001
                logger.warning(
                    "[Merchant/HANDOFF_GUARD] conversation lookup failed | "
                    "tenant=%s err=%s",
                    tenant_id, _ho_conv_exc,
                )
            if _ho_convo is not None:
                try:
                    if _do_full_handoff_flip:
                        # Queue flags for the staff inbox. Do not mark the
                        # conversation human-owned — notify-only must keep AI.
                        _ho_convo.is_human_handoff  = True
                        _ho_convo.needs_human       = True
                        _ho_convo.handoff_active    = True
                    else:
                        # VAGUE tier — soft flag only. We deliberately
                        # leave status / is_human_handoff / handoff_active
                        # untouched so the conversation remains
                        # AI-served and the next inbound goes through
                        # the brain.
                        _ho_convo.needs_human       = True
                    db.flush()
                except Exception as _ho_flag_exc:  # noqa: BLE001
                    logger.warning(
                        "[Merchant/HANDOFF_GUARD] flag flip failed | "
                        "tenant=%s err=%s",
                        tenant_id, _ho_flag_exc,
                    )
            if _do_create_session:
                try:
                    from handoff.manager import create_handoff_session  # noqa: PLC0415
                    create_handoff_session(
                        db, tenant_id, to, to, text or "",
                        reason="customer_request",
                        context_snapshot={"pre_brain_tier": _ho_tier},
                    )
                except Exception as _ho_sess_exc:  # noqa: BLE001
                    logger.warning(
                        "[Merchant/HANDOFF_GUARD] session creation failed | "
                        "tenant=%s err=%s",
                        tenant_id, _ho_sess_exc,
                    )
            if _do_pause_ai and _ho_convo is not None:
                try:
                    from core.ai_pause_guard import (  # noqa: PLC0415
                        pause_ai as _ho_pause_ai,
                        REASON_HUMAN_HANDOFF as _HO_R_HOFF,
                    )
                    _ho_pause_ai(db, _ho_convo, reason=_HO_R_HOFF,
                                 by=f"webhook:pre_brain_handoff:{_ho_tier}")
                except Exception as _ho_pause_exc:  # noqa: BLE001
                    logger.debug(
                        "[Merchant/HANDOFF_GUARD] pause_ai failed: %s",
                        _ho_pause_exc,
                    )
            try:
                await _send_whatsapp_message(
                    phone_id=phone_id, to=to,
                    text=_HANDOFF_ACK_TEXT,
                    _tenant_id=tenant_id, _db=db,
                )
                if _handoff_vcard_target is not None and _staff_call_marker_enabled():
                    try:
                        from services.call_resolver import (  # noqa: PLC0415
                            build_contacts_payload as _ho_build_contacts,
                        )
                        _ho_payload = _ho_build_contacts(
                            [_handoff_vcard_target], to=to,
                        )
                        await _send_contacts_message(
                            phone_id=phone_id, to=to,
                            payload=_ho_payload,
                            _tenant_id=tenant_id, _db=db,
                            customer_message=text or "",
                            delivery_path="handoff",
                            escalation_reason="handoff",
                        )
                    except Exception as _ho_card_exc:  # noqa: BLE001
                        logger.warning(
                            "[Merchant/HANDOFF_GUARD] vCard send failed | tenant=%s err=%s",
                            tenant_id, _ho_card_exc,
                        )
                _trace.mark_outbound_sent(
                    source="pre_brain_handoff",
                    length=len(_HANDOFF_ACK_TEXT),
                )
            except Exception as _ho_send_exc:  # noqa: BLE001
                logger.exception(
                    "[Merchant/HANDOFF_GUARD] ack send failed | tenant=%s to=%s",
                    tenant_id, to,
                )
                # Pre-brain silent-drop visibility (May 2026 #22): the customer
                # explicitly asked for a human, the handoff guard fired, but the
                # acknowledgement send raised. The customer sees nothing AND
                # the brain pipeline is bypassed — exactly the class of drop the
                # owner dashboard's "إسقاطات الإدخال" tab is meant to surface.
                try:
                    from core.inbound_observability import (  # noqa: PLC0415
                        record_inbound_drop,
                        DROP_PRE_BRAIN_HANDOFF,
                    )
                    record_inbound_drop(
                        tenant_id=tenant_id,
                        drop_kind=DROP_PRE_BRAIN_HANDOFF,
                        customer_phone=to or "",
                        conversation_id=getattr(_ho_convo, "id", None),
                        inbound_preview=text or "",
                        chosen_path="handoff_guard_ack_send",
                        detail=(
                            f"ack_send_exception={_ho_send_exc.__class__.__name__}: "
                            f"{str(_ho_send_exc)[:120]}"
                        ),
                    )
                except Exception as _obs_exc:  # noqa: BLE001
                    logger.warning("[INBOUND_OBS] hook failed: %s", _obs_exc)
            try:
                from routers.conversations import record_outbound_message  # noqa: PLC0415
                record_outbound_message(
                    db, tenant_id, to, _HANDOFF_ACK_TEXT,
                    event_type="ai_handoff_ack",
                    extra={
                        "is_ai":              True,
                        "deterministic_path": f"pre_brain_handoff:{_ho_tier}",
                        "handoff_active":     bool(_do_full_handoff_flip),
                        "needs_human":        True,
                        "ai_paused":          bool(_do_pause_ai),
                        "owner_contact":      bool(_is_owner_contact),
                        "owner_tier":         _ho_tier,
                        **(_persona_ownership.to_metadata()),
                    },
                )
            except Exception as _ho_rec_exc:  # noqa: BLE001
                logger.debug(
                    "[Merchant/HANDOFF_GUARD] outbound record failed: %s",
                    _ho_rec_exc,
                )
            try:
                _persona_ownership.mark_bypass(
                    _POReason.PRE_BRAIN_HANDOFF,
                    owner=f"pre_brain_handoff:{_ho_tier}",
                )
                _trace.fallback_source = f"pre_brain_handoff:{_ho_tier}"
                _trace.response_goal   = "handoff"
                _trace.intent          = "talk_to_human"
                _sync_persona_observability()
                _trace.emit()
            except Exception:  # noqa: BLE001
                pass
            return

    try:
        if _t_merchant_entry_gates is not None:
            import time as _time_meg2  # noqa: PLC0415
            from core.turn_latency import safe_record_ms  # noqa: PLC0415

            safe_record_ms(
                "merchant_entry_gates",
                (_time_meg2.monotonic() - _t_merchant_entry_gates) * 1000.0,
            )
            _t_merchant_entry_gates = None
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        pass

    # ── Top-k intent ranking (May 2026 #17) ─────────────────────────────────
    # Run the rule classifier ONCE at the top of the turn, capture the
    # full ranking, and stash it on the trace. The Brain pipeline will
    # run its own (richer) classifier later — this is observability
    # data for the [TURN] line AND a cheap "current-turn intent" signal
    # the fallback paths can consult later without re-running the
    # regex chain.
    #
    # Pure compute — no DB, no network. Cost is bounded by the regex
    # chain length × inbound length, negligible vs the Brain pipeline.
    _top_intents_raw: list = []
    try:
        from modules.ai.brain.intent import rules as _intent_rules  # noqa: PLC0415
        _top_intents_raw = _intent_rules.match_top_k(text or "", k=3)
        _trace.top_intents = [(float(c), it.name) for c, it in _top_intents_raw]
        if _top_intents_raw:
            _best_conf, _best_intent = _top_intents_raw[0]
            _trace.intent             = _best_intent.name
            _trace.intent_confidence  = float(_best_conf)
    except Exception as _topk_exc:  # noqa: BLE001
        logger.debug("[TURN] top_intents capture failed: %s", _topk_exc)

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


    # The dispatcher's ownership claim, re-checked once for this turn and used
    # by every owner below. It is established before any of them and scoped to
    # this exact inbound; see the seam block for what it does and does not mean.
    _claim_holds = _commerce_runtime_claim_holds(
        commerce_runtime_claim, tenant_id=tenant_id, to=to, wa_msg_id=wa_msg_id)

    # ── COD reply interception ────────────────────────────────────────────────
    # Some WhatsApp clients render QUICK_REPLY taps as plain text rather
    # than interactive button payloads. We pattern-match the message
    # against the COD whitelist BEFORE the AI takes over, otherwise the
    # store's AI assistant would happily reply "ok!" without ever
    # transitioning the order. classify_cod_reply returns None on any
    # unrelated text, so this guard is safe to run on every message.
    #
    # It is an *owner*, not a formatter: it transitions the order and sends the
    # customer a follow-up. So it runs only when the commerce runtime has not
    # claimed this inbound. A claimed turn belongs to one runtime, and a second
    # owner acting on it is the double answer the claim exists to prevent.
    # Without a claim nothing here changes at all.
    if _claim_holds:
        logger.info("[COMMERCE_RUNTIME_PILOT] cod branches skipped tenant=%s — "
                    "the runtime claimed this inbound", tenant_id)
    else:
        try:
            from services.cod_confirmation import (  # noqa: PLC0415
                classify_cod_reply, handle_cod_reply, is_owned_cod_button_payload,
            )
            if is_owned_cod_button_payload(text):
                decision, order = await handle_cod_reply(
                    db,
                    tenant_id=tenant_id,
                    customer_phone=to,
                    text=text,
                    button_payload=text,
                )
                if order is not None:
                    await _send_cod_followup_message(
                        phone_id=phone_id, to=to, decision=decision, order=order,
                        _tenant_id=tenant_id, _db=db,
                    )
                return
            if classify_cod_reply(text) is not None:
                decision, order = await handle_cod_reply(
                    db,
                    tenant_id=tenant_id,
                    customer_phone=to,
                    text=text,
                    button_payload=text,
                )
                if order is not None:
                    await _send_cod_followup_message(
                        phone_id=phone_id, to=to, decision=decision, order=order,
                        _tenant_id=tenant_id, _db=db,
                    )
                    return
                # Title-only fallback with no pending order continues to AI.
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
        _inbound_body = (inbound_persist_body or text or "").strip()
        try:
            import time as _time_inb  # noqa: PLC0415
            from core.turn_latency import (  # noqa: PLC0415
                get_turn_latency,
                safe_flush_webhook_pre_persist,
                safe_record_ms,
            )

            safe_flush_webhook_pre_persist()
            _t_inb = _time_inb.monotonic()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            _t_inb = None
        StateManager.save_message(
            db, to, _inbound_body, "inbound",
            conversation_id=convo.id,
            tenant_id=tenant_id,
            extra_metadata=_live_in_meta,
        )
        try:
            import time as _time_inb2  # noqa: PLC0415
            from core.turn_latency import get_turn_latency, safe_record_ms  # noqa: PLC0415

            if _t_inb is not None:
                safe_record_ms(
                    "inbound_persist",
                    (_time_inb2.monotonic() - _t_inb) * 1000.0,
                )
            _tl_inb = get_turn_latency()
            if _tl_inb is not None:
                _tl_inb.set_identity(conversation_id=int(convo.id))
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            pass

        # ── Repeated short fragment guard (Jun 2026) ─────────────────────
        # Same short text repeated within a brief window must not each spawn
        # a full brain turn (and catalog fallback). Inbound is already visible
        # in the inbox; suppress duplicate processing or clarify once.
        try:
            from modules.ai.brain.commerce.inbound_fragment_guard import (  # noqa: PLC0415
                duplicate_fragment_clarification_reply,
                evaluate_duplicate_fragment_turn,
            )

            _frag_decision = evaluate_duplicate_fragment_turn(
                tenant_id=tenant_id,
                customer_phone=to,
                text=text or "",
            )
            if not _frag_decision.process_turn:
                _trace.fallback_source = "inbound_fragment_guard"
                _trace.response_goal = _frag_decision.reason or "duplicate_fragment"
                if _frag_decision.send_clarification_once:
                    _frag_reply = duplicate_fragment_clarification_reply(
                        inbound_text=text or "",
                    )
                    _frag_ok = await _send_whatsapp_message(
                        phone_id=phone_id,
                        to=to,
                        text=_frag_reply,
                        _tenant_id=tenant_id,
                        _db=db,
                    )
                    if _frag_ok:
                        StateManager.save_message(
                            db,
                            to,
                            _frag_reply,
                            "outbound",
                            conversation_id=convo.id,
                            tenant_id=tenant_id,
                            extra_metadata={
                                **_persona_ownership.to_metadata(),
                                "deterministic_path": "inbound_fragment_guard",
                                "fragment_guard_reason": _frag_decision.reason,
                            },
                        )
                        try:
                            db.commit()
                        except Exception:
                            try:
                                db.rollback()
                            except Exception:  # noqa: silent-ok
                                pass
                        _trace.outbound_sent = True
                        _trace.reply_source = _TS.SOURCE_FALLBACK
                else:
                    try:
                        db.commit()
                    except Exception:
                        try:
                            db.rollback()
                        except Exception:  # noqa: silent-ok
                            pass
                logger.info(
                    "[INBOUND_FRAGMENT_GUARD] suppressed tenant=%s to=%s "
                    "reason=%s clarify=%s inbound=%r",
                    tenant_id,
                    to,
                    _frag_decision.reason or "-",
                    _frag_decision.send_clarification_once,
                    (text or "")[:80],
                )
                _sync_persona_observability()
                return
        except Exception as _frag_exc:  # noqa: BLE001
            logger.warning(
                "[INBOUND_FRAGMENT_GUARD] check failed tenant=%s to=%s err=%s",
                tenant_id,
                to,
                _frag_exc,
            )

        # ── PAYMENT-ASSET EARLY BYPASS ───────────────────────────────────
        #
        # Unstructured natural language must NOT execute payment media
        # before Brain/LLM. Weak lexical hits (``is_payment_query`` /
        # bank-name substrings) and merchant-asset existence are not
        # customer semantic intent.
        #
        # Pre-Brain payment execution is allowed only for structured
        # payment actions (button / list / machine action IDs). Genuine
        # free-text payment asks continue into Brain; the platform may
        # attach the authoritative asset after compose.
        try:
            from core.ai_libraries import (  # noqa: PLC0415
                find_best_payment_asset as _find_payment_asset,
                validate_media_for_send as _validate_media,
            )
            from modules.ai.brain.decision.payment_barcode_routing import (  # noqa: PLC0415
                is_payment_barcode_image_request as _is_barcode_image_request,
            )
            from modules.ai.brain.commerce.conversational_priority import (  # noqa: PLC0415
                has_payment_outbound_consent as _has_payment_consent,
            )
            from modules.ai.brain.commerce.customer_origin_intent import (  # noqa: PLC0415
                split_inbound_text,
            )
            from modules.ai.brain.commerce.payment_execution_ownership import (  # noqa: PLC0415
                payment_early_bypass_allowed as _payment_early_bypass_allowed,
            )
            from services.media_resolver import resolve_for_query as _resolve_for_query  # noqa: PLC0415

            _in_meta_early = inbound_metadata if isinstance(inbound_metadata, dict) else {}
            _norm_type_early = str(
                _in_meta_early.get("normalized_type")
                or _in_meta_early.get("source_type")
                or ""
            )
            _split_early = split_inbound_text(
                text or "",
                inbound_metadata=_in_meta_early,
                normalized_type=_norm_type_early or None,
            )
            _origin_early = _split_early.customer_origin
            _early_bypass_ok = _payment_early_bypass_allowed(
                inbound_metadata=_in_meta_early,
                normalized_type=_norm_type_early or None,
            )
            _payment_consent = False
            _early_barcode_image = False
            _early_payment_intent = False
            if not _early_bypass_ok:
                logger.info(
                    "[PAYMENT_INFO] early-bypass SKIPPED tenant=%s convo=%s "
                    "reason=unstructured_requires_brain_semantic_ownership",
                    tenant_id, getattr(convo, "id", None),
                )
            else:
                _payment_consent = _has_payment_consent(
                    text or "",
                    inbound_metadata=_in_meta_early,
                    normalized_type=_norm_type_early or None,
                    tenant_id=tenant_id,
                    route="early_payment_bypass",
                    conversation_id=getattr(convo, "id", None),
                )
                _early_barcode_image = (
                    _payment_consent and _is_barcode_image_request(_origin_early)
                )
                # Structured payment action IDs are already explicit intent.
                # Do not re-derive intent from ``is_payment_query``.
                _early_payment_intent = True
            _early_payment_asset = None
            _early_payment_key = ""
            if _early_barcode_image:
                try:
                    _early_resolution, _early_payment_key = _resolve_for_query(
                        db, tenant_id, _origin_early or "",
                    )
                    if _early_resolution:
                        _early_payment_asset = _early_resolution.to_attachment()
                        _early_payment_asset["_relevance_score"] = 99.0
                except Exception as _early_resolve_exc:
                    logger.warning(
                        "[PAYMENT_BARCODE] early-bypass resolve failed tenant=%s err=%s",
                        tenant_id, _early_resolve_exc,
                    )
            elif _early_payment_intent:
                _early_payment_asset = _find_payment_asset(
                    db, tenant_id, _origin_early or "",
                )
            logger.info(
                "[PAYMENT_INFO] early-gate tenant=%s convo=%s to=%s "
                "intent_detected=%s barcode_image=%s asset_found=%s asset_id=%s "
                "asset_key=%s asset_score=%s",
                tenant_id, getattr(convo, "id", None), to,
                _early_payment_intent,
                _early_barcode_image,
                bool(_early_payment_asset),
                (_early_payment_asset or {}).get("id"),
                (_early_payment_asset or {}).get("media_key") or _early_payment_key or None,
                f"{(_early_payment_asset or {}).get('_relevance_score') or 0:.2f}"
                if _early_payment_asset else None,
            )
            if _early_payment_intent and _early_payment_asset:
                from modules.ai.checkout_authority import (  # noqa: PLC0415
                    brain_payment_paths_should_defer_to_checkout_owner as _defer_payment_to_checkout,
                )
                if _defer_payment_to_checkout(
                    db,
                    tenant_id=tenant_id,
                    conversation=convo,
                    message=text or "",
                    inbound_metadata=inbound_metadata,
                ):
                    logger.info(
                        "[PAYMENT_INFO] early-bypass SKIPPED tenant=%s convo=%s "
                        "reason=active_checkout_owner",
                        tenant_id, getattr(convo, "id", None),
                    )
                else:
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
                        _media_ok = await _send_media_message(
                            phone_id=phone_id, to=to,
                            media_type=_normalised.get("media_type") or "image",
                            media_url=_normalised.get("file_url") or "",
                            caption=None,
                            filename=_normalised.get("filename"),
                            _tenant_id=tenant_id, _db=db,
                        )
                        if _media_ok:
                            _early_mk = (
                                _early_payment_key
                                or _normalised.get("media_key")
                                or ""
                            )
                            _early_persona_event = None
                            if _early_barcode_image:
                                from core.tenant import (  # noqa: PLC0415
                                    get_or_create_settings,
                                    merge_ai_defaults,
                                )
                                from modules.ai.brain.persona.payment_media_intro import (  # noqa: PLC0415
                                    try_compose_payment_media_intro,
                                )

                                _early_ai = merge_ai_defaults(
                                    dict(
                                        get_or_create_settings(db, tenant_id).ai_settings
                                        or {}
                                    )
                                )
                                _intro_text, _, _early_persona_event = (
                                    await try_compose_payment_media_intro(
                                        tenant_id=tenant_id,
                                        customer_phone=to,
                                        inbound_text=text or "",
                                        media_key=_early_mk,
                                        media_url_present=True,
                                        ai_settings=_early_ai,
                                    )
                                )
                            else:
                                # Structured payment action: send the
                                # authoritative asset. Do not invent
                                # customer-facing prose here.
                                _intro_text = ""
                            _text_ok = True
                            if _intro_text:
                                _text_ok = await _send_whatsapp_message(
                                    phone_id=phone_id, to=to, text=_intro_text,
                                    _tenant_id=tenant_id, _db=db,
                                )
                                StateManager.save_message(
                                    db, to, _intro_text, "outbound",
                                    conversation_id=convo.id, tenant_id=tenant_id,
                                    extra_metadata=_otp_merge_save_metadata(
                                        None,
                                        {},
                                        persona_compose_event=_early_persona_event,
                                    )
                                    if _early_persona_event
                                    else None,
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
                                except Exception as _rollback_exc:
                                    logger.exception(
                                        "[PAYMENT_INFO] rollback after stamp failure "
                                        "tenant=%s err=%s",
                                        tenant_id,
                                        _rollback_exc,
                                    )
                            return  # short-circuit — never run the brain for this turn
                        logger.warning(
                            "[PAYMENT_INFO] early-bypass SKIPPED tenant=%s convo=%s "
                            "asset_id=%s reason=media_send_failed",
                            tenant_id, getattr(convo, "id", None),
                            _early_payment_asset.get("id"),
                        )
                    else:
                        logger.warning(
                            "[PAYMENT_INFO] early-bypass SKIPPED tenant=%s convo=%s "
                            "asset_id=%s reason=validation_failed err=%s — "
                            "falling through to normal pipeline",
                            tenant_id, getattr(convo, "id", None),
                            _early_payment_asset.get("id"), _err,
                        )
            elif _early_payment_intent and not _early_payment_asset:
                from modules.ai.checkout_authority import (  # noqa: PLC0415
                    brain_payment_paths_should_defer_to_checkout_owner as _defer_payment_to_checkout,
                )
                if _defer_payment_to_checkout(
                    db,
                    tenant_id=tenant_id,
                    conversation=convo,
                    message=text or "",
                    inbound_metadata=inbound_metadata,
                ):
                    logger.info(
                        "[PAYMENT_INFO] early-bypass SKIPPED tenant=%s convo=%s "
                        "reason=active_checkout_owner_no_asset",
                        tenant_id, getattr(convo, "id", None),
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
            from core.handoff_truth import (  # noqa: PLC0415
                REASON_GATE_VERIFY_FAILED,
                evaluate_gate_error_fail_closed,
            )

            if evaluate_gate_error_fail_closed(
                db,
                tenant_id=tenant_id,
                customer_phone=to,
                conversation=convo,
                gate="ai_pause_guard",
                error=_guard_exc,
            ):
                _skip, _skip_reason = True, REASON_GATE_VERIFY_FAILED
            else:
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

        # Surface a single structured log line right at the entry to the
        # brain pipeline when Human-Priority Mode is taking over this
        # turn. Lets the rollout dashboard (manual ``grep`` for now) count
        # how often the new mode fires without scraping the whole pipeline
        # log output.
        if _skip_reason == "human_priority":
            logger.info(
                "[HUMAN_PRIORITY] webhook=enter tenant=%s convo=%s to=%s — "
                "brain pipeline will run in clamped mode "
                "(no payment_link / draft_order / coupon / addon)",
                tenant_id, convo.id, to,
            )

        _catalog_message_event_id: Optional[int] = None
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
                    _catalog_message_event_id = int(getattr(latest_event, "id", 0) or 0) or None
                    db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass

        if inbound_metadata:
            try:
                from core.wa_catalog_order_immediate_draft import (  # noqa: PLC0415
                    is_catalog_order_inbound,
                    persist_catalog_order_immediate_draft,
                )

                if is_catalog_order_inbound(inbound_metadata):
                    persist_catalog_order_immediate_draft(
                        db,
                        tenant_id=int(tenant_id),
                        conversation=convo,
                        inbound_metadata=dict(inbound_metadata),
                        phone=to,
                        message_event_id=_catalog_message_event_id,
                        source_message_key=(wa_msg_id or None),
                    )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[CATALOG_ORDER_DRAFT] immediate persist hook failed tenant=%s conv=%s",
                    tenant_id,
                    getattr(convo, "id", None),
                )

        # Keep a lightweight state row in sync with the same phone key used by history.
        try:
            import time as _time_csl  # noqa: PLC0415
            from core.turn_latency import safe_record_ms as _tl_csl  # noqa: PLC0415

            _t_csl = _time_csl.monotonic()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            _t_csl = None
            _tl_csl = None  # type: ignore[assignment]
        state = StateManager.load(db, phone=to, tenant_id=tenant_id)
        try:
            if _t_csl is not None and _tl_csl is not None:
                import time as _time_csl2  # noqa: PLC0415

                _tl_csl(
                    "conversation_state_load",
                    (_time_csl2.monotonic() - _t_csl) * 1000.0,
                )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            pass
        state.turn += 1
        state.stage = "active"
        try:
            import time as _time_css  # noqa: PLC0415
            from core.turn_latency import safe_record_ms as _tl_css  # noqa: PLC0415

            _t_css = _time_css.monotonic()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            _t_css = None
            _tl_css = None  # type: ignore[assignment]
        StateManager.save(db, state, tenant_id=tenant_id)
        try:
            if _t_css is not None and _tl_css is not None:
                import time as _time_css2  # noqa: PLC0415

                _tl_css(
                    "conversation_state_save",
                    (_time_css2.monotonic() - _t_css) * 1000.0,
                )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            pass

        # Load recent conversation history for both paths
        try:
            import time as _time_hist  # noqa: PLC0415
            from core.turn_latency import safe_record_ms as _tl_hist  # noqa: PLC0415

            _t_hist = _time_hist.monotonic()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            _t_hist = None
            _tl_hist = None  # type: ignore[assignment]
        history = StateManager.load_history(db, phone=to, tenant_id=tenant_id)
        try:
            if _t_hist is not None and _tl_hist is not None:
                import time as _time_hist2  # noqa: PLC0415

                _tl_hist(
                    "history_load",
                    (_time_hist2.monotonic() - _t_hist) * 1000.0,
                )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            pass
        try:
            import time as _time_pbr  # noqa: PLC0415

            _t_pre_brain_remaining = _time_pbr.monotonic()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            _t_pre_brain_remaining = None

        # ── Trusted Context shadow (telemetry only — no prompt/Brain wiring) ──
        try:
            from modules.ai.brain.truth_surface.flags import (  # noqa: PLC0415
                is_trusted_context_shadow_enabled,
            )
            from modules.ai.brain.truth_surface.trusted_context import (  # noqa: PLC0415
                current_trusted_context,
                pop_shadow_build_error_class,
                run_trusted_context_shadow,
                safe_shadow_trace_metadata,
            )

            if is_trusted_context_shadow_enabled():
                _tc_snapshot = current_trusted_context()
                if _tc_snapshot is None:
                    _tc_snapshot = run_trusted_context_shadow(
                        db=db,
                        tenant_id=tenant_id,
                        customer_phone=to,
                        message=text or "",
                        conversation=convo,
                        conversation_id=getattr(convo, "id", None),
                        brain_state=state,
                        inbound_metadata=inbound_metadata,
                    )
                if _tc_snapshot is not None:
                    _trace.extra.update(safe_shadow_trace_metadata(_tc_snapshot))
                else:
                    _build_err = pop_shadow_build_error_class()
                    if _build_err:
                        _trace.extra["trusted_context_shadow_status"] = "build_error"
                        _trace.extra["trusted_context_shadow_error_class"] = _build_err
                        _trace.extra["trusted_context_shadow_stage"] = "build"
        except Exception as _tc_wire_exc:  # noqa: BLE001
            logger.warning(
                "[TRUSTED_CONTEXT_SHADOW] wire_failed tenant=%s stage=wireup error_class=%s",
                tenant_id,
                _tc_wire_exc.__class__.__name__,
            )
            _trace.extra["trusted_context_shadow_status"] = "wireup_error"
            _trace.extra["trusted_context_shadow_error_class"] = (
                _tc_wire_exc.__class__.__name__
            )
            _trace.extra["trusted_context_shadow_stage"] = "wireup"

        _brain_buttons: list = []  # populated by brain when product buttons should be sent
        _brain_product_cards: list = []  # single-resolved rich product cards from compose
        _native_catalog_entry: dict = {}
        _outbound_text_tracker = None
        brain_result: Optional[Dict[str, Any]] = None
        _brain_persona_compose_event: Optional[Dict[str, Any]] = None
        _payment_persona_compose_event: Optional[Dict[str, Any]] = None
        _brain_handoff: bool = False  # set True only by the brain handoff branch
        _brain_nc_block: bool = False
        _brain_nc_category: str = ""
        _nc_turn = None
        _br_action: str = ""  # brain last_action — used by final dispatch guard
        _br_dec_action: str = ""
        _br_dec_args: dict = {}
        # Tenant 33 #49 (Commit 3): empty string when the relational
        # layer is disabled or no moment was identified — guarantees
        # the safety-net suppression gate stays inert.
        _relational_moment: str = ""
        _turn_eval_applied: bool = False

        # ── Top-level Conversation Mode Controller ───────────────────────────
        # Decides who owns this turn (live chat, automation recovery,
        # identity reply, support escalation, checkout assist, post
        # purchase) BEFORE routing to Brain or legacy. Persists a sticky
        # lease on the conversation so a free-form reply that overrides
        # automation cannot bounce back to recovery in the same window.
        from modules.ai.routing.conversation_mode import (  # noqa: PLC0415
            MODE_IDENTITY_REPLY,
            mode_prompt_overlay,
            resolve_conversation_mode,
            save_lease,
        )
        from core.ownership_state import (  # noqa: PLC0415
            attempt_implicit_takeover_recovery,
            resolve_ownership_state,
        )
        _ownership_before = resolve_ownership_state(
            db, convo, assume_current_inbound=True,
        )
        _ownership_recovery = attempt_implicit_takeover_recovery(
            db, convo, assume_current_inbound=True,
        )
        if _ownership_recovery.released:
            try:
                db.add(convo)
                db.flush()
            except Exception as _own_exc:
                logger.warning("[OWNERSHIP_IDLE_RELEASE] flush failed: %s", _own_exc)
        _ownership_after = resolve_ownership_state(
            db, convo, assume_current_inbound=True,
        )
        if _trace is not None:
            _trace.ownership_state = _ownership_after.state
            _trace.ownership_takeover_class = _ownership_after.takeover_class
            if _ownership_recovery.released:
                _trace.ownership_release_reason = _ownership_recovery.reason or ""
        logger.info(
            "[OWNERSHIP] tenant=%s to=%s before=%s after=%s released=%s class=%s",
            tenant_id,
            to,
            _ownership_before.state,
            _ownership_after.state,
            _ownership_recovery.released,
            _ownership_after.takeover_class,
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

        # ── Billing guard: silent outbound block (merchant-only expiry state) ─
        # Inbound is already persisted above. Never expose trial/subscription
        # expiry to customers — no static fallback, no AI reply, no WhatsApp send.
        from core.billing import has_billing_access as _has_billing  # noqa: PLC0415
        if not _has_billing(db, tenant_id):
            _persona_ownership.mark_bypass(_POReason.BILLING_DENIED, owner="billing_guard")
            _trace.fallback_source = _TS.SOURCE_BILLING_DENIED
            _trace.response_goal   = "silent"
            _trace.reply_source    = _TS.SOURCE_BILLING_DENIED
            logger.info(
                "[BILLING_GUARD] inbound recorded, outbound suppressed (silent) | tenant=%s to=%s",
                tenant_id, to,
            )
            _sync_persona_observability()
            return

        # ── Conversation quota guard: silent outbound block at plan limit ───────
        from core.wa_usage import check_limit as _check_conv_limit  # noqa: PLC0415

        _conv_quota = _check_conv_limit(db, tenant_id, category="service")
        if not _conv_quota.allowed:
            _persona_ownership.mark_bypass(_POReason.BILLING_DENIED, owner="conversation_quota_guard")
            _trace.fallback_source = _TS.SOURCE_BILLING_DENIED
            _trace.response_goal   = "silent"
            _trace.reply_source    = _TS.SOURCE_BILLING_DENIED
            logger.info(
                "[CONVERSATION_LIMIT] inbound recorded, outbound suppressed (silent) | "
                "tenant=%s to=%s used=%s limit=%s reason=%s",
                tenant_id,
                to,
                _conv_quota.used_total,
                _conv_quota.limit,
                _conv_quota.reason,
            )
            _sync_persona_observability()
            return

        # ── Commerce runtime (owner pilot) ────────────────────────────────
        # THE routing decision, asked once, here: after every gate that may
        # silence this turn (AI pause / handoff / blocklist above, billing and
        # conversation quota just above) and before every path that may answer
        # it — the Commerce Agent V2 owner below, OrderFlowV2, checkout
        # routing, the pre-brain routers and MerchantBrain. Asking later would
        # not be a routing decision at all: it would only see the turns no
        # other owner wanted.
        #
        # The answer is exclusive. ``handled`` means this handler returns and
        # nothing below runs for that inbound message, so the two runtimes can
        # never both answer it. Fail-closed: a refusal, or an error asking,
        # leaves the legacy path exactly as it is.
        _commerce_runtime_owns_turn = False
        _pilot_outcome = "not_asked"
        # A claim established at the dispatcher is **sticky**. The dispatcher
        # withheld this turn from its own owners on the strength of it, so from
        # here a refusal, a lookup failure or an exception may stop the runtime
        # executing — but none of them may hand the turn to the legacy path,
        # which would answer a message this runtime owns and may already have
        # answered. The claim is scoped, and is honoured only for the turn it
        # was established for.
        try:
            from services.commerce_runtime_pilot import (  # noqa: PLC0415
                maybe_handle_with_commerce_runtime,
            )

            _pilot = await maybe_handle_with_commerce_runtime(
                db=db,
                tenant_id=tenant_id,
                phone_id=phone_id,
                to=to,
                text=text or "",
                convo=convo,
                wa_msg_id=wa_msg_id,
                inbound_metadata=inbound_metadata if isinstance(inbound_metadata, dict) else None,
                trace=_trace,
                legacy_already_answered=not _trace.outbound_lock_acquired(),
                ai_gate_skipped=bool(_skip),
            )
            _commerce_runtime_owns_turn = bool(_pilot.handled)
            _pilot_outcome = str(_pilot.reason)
        except Exception as _pilot_exc:  # noqa: BLE001
            # Without a claim the route was never taken and nothing was sent by
            # it, so a failure to even ask must not cost the customer a reply.
            # With one, the turn stays ours and is silenced below.
            _pilot_outcome = f"seam_error:{type(_pilot_exc).__name__}"
            logger.warning(
                "[COMMERCE_RUNTIME_PILOT] route check failed tenant=%s err=%s claim=%s",
                tenant_id,
                type(_pilot_exc).__name__,
                bool(_claim_holds),
            )

        if _claim_holds and not _commerce_runtime_owns_turn:
            logger.error(
                "[COMMERCE_RUNTIME_PILOT] claimed turn was not executed tenant=%s "
                "outcome=%s — withheld from the legacy path, nothing was answered",
                tenant_id, _pilot_outcome,
            )

        if _commerce_runtime_owns_turn or _claim_holds:
            # From here the turn belongs to the commerce runtime — because it
            # answered, or because it claimed the turn and did not. Either way
            # it may already have answered the customer, and nothing after this
            # point may hand it back: observability is not allowed to reopen the
            # legacy path by raising, so it is caught and the return is
            # unconditional.
            try:
                _sync_persona_observability()
            except Exception as _pilot_observe_exc:  # noqa: BLE001
                logger.warning(
                    "[COMMERCE_RUNTIME_PILOT] observability failed tenant=%s err=%s "
                    "v1_bypassed=true silent_v1_fallback=false",
                    tenant_id,
                    type(_pilot_observe_exc).__name__,
                )
            return

        # ── Commerce Agent V2 tenant-scoped outbound owner ───────────────
        # This seam intentionally precedes OrderFlowV2, checkout routing,
        # MerchantBrain, legacy compose, and all legacy response delivery.
        # Once the explicit tenant gate passes, this turn returns here even
        # when V2 falls back or delivery fails; ownership never silently
        # transfers to V1.
        from modules.ai.commerce_agent_v2.ownership import (  # noqa: PLC0415
            outbound_enabled_for_tenant as _v2_outbound_enabled,
        )

        if not _skip and _v2_outbound_enabled(int(tenant_id)):
            try:
                _v2_owner = await _run_and_deliver_commerce_v2_owner(
                    db=db,
                    tenant_id=int(tenant_id),
                    conversation=convo,
                    customer_phone=to,
                    connection=wa_conn_hist,
                    phone_id=phone_id,
                    inbound_trace_id=str(wa_msg_id or f"conversation-{convo.id}"),
                    user_input=text or "",
                )
            except Exception as _v2_owner_exc:  # noqa: BLE001 — selected tenant stays V2-owned
                logger.exception(
                    "[COMMERCE_V2_OWNER_FATAL] tenant=%s conversation=%s error=%s "
                    "v1_bypassed=true silent_v1_fallback=false",
                    tenant_id,
                    getattr(convo, "id", None),
                    type(_v2_owner_exc).__name__,
                )
                _v2_owner = {
                    "status": "failed",
                    "model": "",
                    "text_sent": False,
                    "presentations_sent": 0,
                    "unsupported_presentations": 0,
                }
            try:
                logger.info(
                    "[COMMERCE_V2_OWNER] tenant=%s conversation=%s status=%s "
                    "model=%s text_sent=%s presentations_sent=%s "
                    "unsupported_presentations=%s v1_bypassed=true silent_v1_fallback=false",
                    tenant_id,
                    getattr(convo, "id", None),
                    _v2_owner.get("status"),
                    _v2_owner.get("model"),
                    _v2_owner.get("text_sent"),
                    _v2_owner.get("presentations_sent"),
                    _v2_owner.get("unsupported_presentations"),
                )
                _persona_ownership.mark_bypass(
                    _POReason.PRE_BRAIN_FAST_PATH,
                    owner="commerce_agent_v2_outbound",
                )
                _sync_persona_observability()
            except Exception as _v2_observe_exc:  # noqa: BLE001 — observability cannot release ownership
                logger.warning(
                    "[COMMERCE_V2_OWNER_OBSERVABILITY_FAILED] tenant=%s error=%s "
                    "v1_bypassed=true silent_v1_fallback=false",
                    tenant_id,
                    type(_v2_observe_exc).__name__,
                )
            return

        # ── OrderFlowV2 deterministic checkout owner ─────────────────────
        _of2_result = None
        _of2_catalog_error = False
        _inbound_normalized_type = "text"
        if isinstance(inbound_metadata, dict):
            _inbound_normalized_type = str(
                inbound_metadata.get("inbound_normalized_type")
                or inbound_metadata.get("type")
                or "text"
            )
        try:
            from modules.ai.order_flow_v2.owner import (  # noqa: PLC0415
                persist_order_flow_v2_result,
                try_handle_order_flow_v2,
            )

            _of2_result = try_handle_order_flow_v2(
                db,
                tenant_id=tenant_id,
                customer_phone=to,
                message=text or "",
                inbound_metadata=inbound_metadata if isinstance(inbound_metadata, dict) else {},
                inbound_normalized_type=_inbound_normalized_type,
            )
            _of2_catalog_error = str(getattr(_of2_result, "reason", "") or "") == "catalog_order_v2_error"
            if _of2_result.handled and _of2_result.reply and _trace.outbound_lock_acquired():
                persist_order_flow_v2_result(
                    db,
                    tenant_id=tenant_id,
                    customer_phone=to,
                    result=_of2_result,
                )
                _of2_reply = _of2_result.reply
                # Provenance for THIS reply, recorded on the stored
                # message. The address guard fills it in when it removes a
                # claim or hands the turn to the emergency fallback.
                _of2_provenance: Dict[str, Any] = {}
                # The ordinary address turn is composed, not written here.
                # Asking the customer where to deliver is a clarification,
                # and AGENTS.md assigns that wording to the composer; a
                # fixed sentence for it is exactly the deterministic
                # customer-facing prose the doctrine prohibits. Everything
                # structural about this turn — the action ids, the choice
                # labels, the paging, the receipt — stays platform-owned
                # and is untouched.
                # WHICH field this turn is collecting is decided by the
                # owner's own state patch — never by the customer's
                # wording — and it is resolved ONCE, here, so that the
                # ordinary composition below and the recovery that
                # follows a refused candidate pursue the SAME goal with
                # the SAME facts. Reading it inline keeps the decision
                # alive even when the recovery module is the thing that
                # failed to import, which is exactly when the branch
                # below must still refuse to revive the old prose.
                _of2_address_field = str(
                    (getattr(_of2_result, "state_patch", None) or {}).get(
                        "order_flow_v2_last_field"
                    )
                    or ""
                )
                if _of2_address_field not in ("delivery_address", "city"):
                    _of2_address_field = ""
                _of2_is_address_turn = bool(_of2_address_field)
                if _of2_is_address_turn:
                    _of2_composed = None
                    try:
                        from modules.ai.order_flow_v2.address_reply_recovery import (  # noqa: PLC0415
                            address_collection_field,
                            address_turn_facts,
                            compose_address_turn_reply,
                            response_goal_for_field,
                        )

                        # The owner's own reader is authoritative when it
                        # is available; the inline read above is only the
                        # resilient copy of the same state.
                        _of2_address_field = (
                            address_collection_field(_of2_result) or _of2_address_field
                        )
                        # Declares WHICH stage is running, from the branch
                        # that runs it. Inert outside internal E2E.
                        with _acceptance_compose_stage(
                            "ordinary",
                            collection_field=_of2_address_field,
                            turn_ref=str(wa_msg_id or ""),
                        ):
                            _of2_composed = await compose_address_turn_reply(
                                db,
                                tenant_id=int(tenant_id),
                                conversation=convo,
                                customer_phone=to,
                                message=str(text or ""),
                                known_facts=address_turn_facts(
                                    order_prep=(
                                        ((getattr(convo, "extra_metadata", None) or {}).get("brain_state") or {}).get(
                                            "order_prep"
                                        )
                                        or {}
                                    ),
                                    presentation=getattr(
                                        _of2_result, "address_presentation", None
                                    ),
                                    field_name=_of2_address_field,
                                ),
                                turn_ref=str(wa_msg_id or ""),
                                response_goal=response_goal_for_field(
                                    _of2_address_field
                                ),
                            )
                    except Exception:  # noqa: BLE001  # noqa: silent-ok — handled immediately below, where the turn falls to the approved line rather than to the prose this path no longer owns
                        logger.exception(
                            "[ORDER_FLOW_V2] address reply compose unavailable "
                            "tenant=%s to=%s",
                            tenant_id,
                            to,
                        )
                    if _of2_composed is not None and _of2_composed.spoke:
                        _of2_reply = _of2_composed.text
                        _of2_provenance.update(_of2_composed.as_metadata())
                        _of2_provenance.pop("address_reply_recovered", None)
                        _of2_provenance["address_reply_composed"] = True
                    else:
                        # Compose could not even be entered — its import
                        # failed, its inputs could not be built, or it
                        # raised on the way in. Falling back to the
                        # deterministic reply would quietly restore the
                        # prose ownership this path just gave up, so the
                        # approved minimal line speaks instead. It is
                        # recorded as what it is: NO generation was
                        # attempted, which is a different fact from a
                        # provider that tried and failed.
                        try:
                            from core.fallback_policy import (  # noqa: PLC0415
                                empty_reply_fallback,
                                operational_compose_error_fallback,
                            )

                            _of2_reply = str(
                                operational_compose_error_fallback() or ""
                            ).strip() or str(empty_reply_fallback() or "").strip()
                        except Exception:  # noqa: BLE001  # noqa: silent-ok — with no approved line available the suppression flag below refuses the turn instead of sending unowned prose
                            _of2_reply = ""
                        _of2_provenance.update({
                            "compose_source": "fallback_deterministic",
                            "response_mode": "fallback_deterministic",
                            "chosen_path": "order_flow_v2_address_reply",
                            "llm_candidate_present": False,
                            "address_claim_compose_attempted": False,
                            "address_reply_composed": False,
                            "fallback_reason": "address_reply_compose_unavailable",
                            "fallback_action_type": "order_flow_v2_address_reply",
                            "final_customer_text_source": "fallback_deterministic",
                            "final_text_transformed": True,
                            "final_transform_reasons": ["address_reply_compose_unavailable"],
                        })
                        if not _of2_reply:
                            _of2_provenance["address_claim_send_suppressed"] = True
                            _of2_provenance["address_save_claim_suppress_reason"] = (
                                "address_reply_compose_unavailable"
                            )
                try:
                    from modules.ai.order_flow_v2.outbound_guards import (  # noqa: PLC0415
                        apply_order_flow_v2_outbound_guards,
                    )

                    # Inert in production; fires only where a scenario
                    # armed it. This is the boundary the recovery path
                    # exists for, so injecting anywhere else would test a
                    # different failure than the one claimed.
                    _acceptance_inject_guard_failure()
                    _of2_reply = apply_order_flow_v2_outbound_guards(
                        _of2_reply,
                        db=db,
                        tenant_id=int(tenant_id),
                        conversation_id=getattr(convo, "id", None),
                        conversation=convo,
                        turn_ref=str(wa_msg_id or ""),
                        provenance_sink=_of2_provenance,
                        order_prep=dict(
                            ((getattr(convo, "extra_metadata", None) or {}).get("brain_state") or {}).get(
                                "order_prep"
                            )
                            or {}
                        ),
                    )
                except Exception:  # noqa: BLE001
                    # The guard could not run AT ALL — its import failed,
                    # its arguments could not be built, or it raised on
                    # the way in. The refusal inside that function cannot
                    # protect a failure that stops the function returning,
                    # and the reply still sitting in ``_of2_reply`` is the
                    # unchecked candidate. Fail closed HERE, at the send
                    # decision, or the whole guard is optional.
                    logger.exception(
                        "[ORDER_FLOW_V2] outbound guard failed tenant=%s to=%s",
                        tenant_id,
                        to,
                    )
                    _of2_provenance["address_claim_send_suppressed"] = True
                    _of2_provenance["address_save_claim_suppress_reason"] = (
                        "guard_boundary_failed"
                    )
                    _of2_reply = ""
                if _of2_provenance.get("address_claim_send_suppressed") or not str(
                    _of2_reply or ""
                ).strip():
                    # Nothing here can be stood behind. Refusing to send
                    # the claim is right; leaving the customer's turn
                    # unanswered is not — they asked a question. So the
                    # turn is recovered through the composition route the
                    # Brain already uses: trusted facts in, wording out,
                    # revalidated by the same guard, and only a GENUINE
                    # compose failure reaches the approved minimal line.
                    logger.error(
                        "[ORDER_FLOW_V2] address claim unverifiable, recovering "
                        "tenant=%s to=%s reason=%s",
                        tenant_id,
                        to,
                        _of2_provenance.get("address_save_claim_suppress_reason"),
                    )
                    _of2_recovery = None
                    try:
                        from modules.ai.order_flow_v2.address_reply_recovery import (  # noqa: PLC0415
                            address_turn_facts,
                            compose_address_recovery_reply,
                            response_goal_for_field,
                        )

                        # The refused candidate does not change WHAT the
                        # turn is collecting. Recovery therefore reuses
                        # the same supported fact projection and the same
                        # collection goal as the ordinary composition
                        # above — accepted address facts included —
                        # instead of falling back to the helper's
                        # delivery-address default, which would answer a
                        # city turn with the wrong question.
                        with _acceptance_compose_stage(
                            "recovery",
                            collection_field=_of2_address_field,
                            turn_ref=str(wa_msg_id or ""),
                        ):
                            _of2_recovery = await compose_address_recovery_reply(
                                db,
                                tenant_id=int(tenant_id),
                                conversation=convo,
                                customer_phone=to,
                                message=str(text or ""),
                                known_facts=address_turn_facts(
                                    order_prep=(
                                        ((getattr(convo, "extra_metadata", None) or {}).get("brain_state") or {}).get(
                                            "order_prep"
                                        )
                                        or {}
                                    ),
                                    presentation=getattr(
                                        _of2_result, "address_presentation", None
                                    ),
                                    field_name=_of2_address_field,
                                ),
                                turn_ref=str(wa_msg_id or ""),
                                response_goal=response_goal_for_field(
                                    _of2_address_field
                                ),
                            )
                    except Exception:  # noqa: BLE001  # noqa: silent-ok — handled immediately below, where an unrecovered turn is logged rather than answered with unverified text
                        logger.exception(
                            "[ORDER_FLOW_V2] address recovery failed tenant=%s to=%s",
                            tenant_id,
                            to,
                        )
                    if _of2_recovery is not None and _of2_recovery.spoke:
                        _recovery_sink: Dict[str, Any] = {}
                        _recovery_ok = await _send_whatsapp_message(
                            phone_id=phone_id,
                            to=to,
                            text=_of2_recovery.text,
                            _tenant_id=tenant_id,
                            _db=db,
                            _inbound_message_id=wa_msg_id,
                            _result_sink=_recovery_sink,
                        )
                        if _recovery_ok:
                            _persona_ownership.mark_bypass(
                                _POReason.PRE_BRAIN_FAST_PATH,
                                owner="order_flow_v2:address_reply_recovery",
                            )
                            StateManager.save_message(
                                db,
                                to,
                                _of2_recovery.text,
                                "outbound",
                                conversation_id=convo.id,
                                tenant_id=tenant_id,
                                extra_metadata=_of2_save_metadata({
                                    **_persona_ownership.to_metadata(),
                                    "reply_owner": "order_flow_v2",
                                    "order_flow_v2_reason": "address_reply_recovery",
                                    **_of2_provenance,
                                    **_of2_recovery.as_metadata(),
                                }, turn_ref=str(wa_msg_id or "")),
                            )
                    else:
                        logger.error(
                            "[ORDER_FLOW_V2] address turn unanswered tenant=%s to=%s",
                            tenant_id,
                            to,
                        )
                    try:
                        db.commit()
                    except Exception:  # noqa: BLE001  # noqa: silent-ok — the owner's own state was already persisted above; a commit failure here must not raise into the webhook
                        try:
                            db.rollback()
                        except Exception:  # noqa: silent-ok — rollback best-effort after commit failure
                            pass
                    _sync_persona_observability()
                    return
                _persona_ownership.mark_bypass(
                    _POReason.PRE_BRAIN_FAST_PATH,
                    owner=f"order_flow_v2:{_of2_result.reason}",
                )
                # Structured address choices ride the supported interactive
                # surface. A text payload cannot carry an action the
                # customer can tap, so without this the selection path had
                # no producer and the ids existed only in tests.
                _of2_actions = list(getattr(_of2_result, "address_choice_actions", None) or [])
                _of2_rows = list(getattr(_of2_result, "address_choice_rows", None) or [])
                _of2_surface = str(getattr(_of2_result, "address_choice_surface", "") or "")
                _of2_sink: Dict[str, Any] = {}
                if _of2_surface == "list" and _of2_rows:
                    # More choices than three buttons can carry, or a page
                    # beyond the first. Truncating to buttons here is what
                    # made the fourth saved address unreachable.
                    _of2_ok = await _send_list_reply(
                        phone_id=phone_id,
                        to=to,
                        body_text=_of2_reply,
                        rows=_of2_rows,
                        button_label="العناوين المحفوظة",
                        _tenant_id=tenant_id,
                        _db=db,
                        _result_sink=_of2_sink,
                    )
                elif _of2_actions:
                    _of2_ok = await _send_interactive_reply(
                        phone_id=phone_id,
                        to=to,
                        body_text=_of2_reply,
                        buttons=_of2_actions,
                        _tenant_id=tenant_id,
                        _db=db,
                        _result_sink=_of2_sink,
                    )
                else:
                    _of2_ok = await _send_whatsapp_message(
                        phone_id=phone_id,
                        to=to,
                        text=_of2_reply,
                        _tenant_id=tenant_id,
                        _db=db,
                        _inbound_message_id=wa_msg_id,
                        _result_sink=_of2_sink,
                    )
                if _of2_ok:
                    # The message reached the provider, so what it carried
                    # was genuinely presented. Record it with the OUTBOUND
                    # identity; a send the dedup answered with an earlier
                    # message's id may only reaffirm an identical offer.
                    try:
                        from modules.ai.order_flow_v2.checkout_context import (  # noqa: PLC0415
                            delivered_address_action_ids,
                            record_presented_address_offer,
                        )

                        _of2_presentation = getattr(_of2_result, "address_presentation", None)
                        if _of2_presentation is not None:
                            # Read the payload BACK. Between the reply and
                            # the provider the wire layer drops rows whose
                            # titles or ids collide, so only the payload
                            # that actually left says what was presented.
                            record_presented_address_offer(
                                db,
                                tenant_id=int(tenant_id),
                                conversation=convo,
                                presentation=_of2_presentation,
                                delivery_ref=str(_of2_sink.get("wamid") or ""),
                                duplicate_suppressed=bool(
                                    _of2_sink.get("duplicate_suppressed")
                                ),
                                delivered_action_ids=delivered_address_action_ids(
                                    _of2_sink.get("sent_payload")
                                ),
                            )
                    except Exception:  # noqa: BLE001  # noqa: silent-ok — an unrecorded presentation makes a later tap refuse, which is the safe direction, and must not fail a delivered message
                        logger.debug(
                            "[ORDER_FLOW_V2] address presentation not recorded tenant=%s",
                            tenant_id,
                            exc_info=True,
                        )
                    StateManager.save_message(
                        db,
                        to,
                        _of2_reply,
                        "outbound",
                        conversation_id=convo.id,
                        tenant_id=tenant_id,
                        extra_metadata=_of2_save_metadata({
                            **_persona_ownership.to_metadata(),
                            "reply_owner": "order_flow_v2",
                            "order_flow_v2_reason": _of2_result.reason,
                            # What the customer actually received, and why.
                            # A guard that removed a claim, or an emergency
                            # fallback that replaced the reply outright, is
                            # recorded here rather than left implicit.
                            **_of2_provenance,
                        }, turn_ref=str(wa_msg_id or "")),
                    )
                    try:
                        db.commit()
                    except Exception:
                        try:
                            db.rollback()
                        except Exception:  # noqa: silent-ok — rollback best-effort after commit failure
                            pass
                    _sync_persona_observability()
                    return
        except Exception:
            _of2_catalog_error = True
            logger.exception(
                "[ORDER_FLOW_V2] pre-brain owner failed tenant=%s to=%s",
                tenant_id,
                to,
            )

        if _of2_catalog_error and _trace.outbound_lock_acquired():
            try:
                from modules.ai.brain.commerce.catalog_order_resilience import (  # noqa: PLC0415
                    try_catalog_order_pre_brain_safe_reply,
                )
                from modules.ai.order_flow_v2.triggers import is_catalog_order_inbound  # noqa: PLC0415

                _in_meta_of2 = inbound_metadata if isinstance(inbound_metadata, dict) else {}
                if is_catalog_order_inbound(_in_meta_of2, text or ""):
                    _safe_catalog_reply = try_catalog_order_pre_brain_safe_reply(
                        db,
                        tenant_id=int(tenant_id),
                        customer_phone=to,
                        message=text or "",
                        inbound_metadata=_in_meta_of2,
                    )
                    if _safe_catalog_reply:
                        _persona_ownership.mark_bypass(
                            _POReason.PRE_BRAIN_FAST_PATH,
                            owner="catalog_order_resilience:pre_brain_safe",
                        )
                        _of2_ok = await _send_whatsapp_message(
                            phone_id=phone_id,
                            to=to,
                            text=_safe_catalog_reply,
                            _tenant_id=tenant_id,
                            _db=db,
                            _inbound_message_id=wa_msg_id,
                        )
                        if _of2_ok:
                            StateManager.save_message(
                                db,
                                to,
                                _safe_catalog_reply,
                                "outbound",
                                conversation_id=convo.id,
                                tenant_id=tenant_id,
                                extra_metadata={
                                    **_persona_ownership.to_metadata(),
                                    "reply_owner": "catalog_order_resilience",
                                    "order_flow_v2_reason": "catalog_order_pre_brain_safe",
                                },
                            )
                            try:
                                db.commit()
                            except Exception:
                                try:
                                    db.rollback()
                                except Exception:  # noqa: silent-ok — rollback best-effort after commit failure
                                    pass
                            _sync_persona_observability()
                            return
            except Exception:
                logger.exception(
                    "[CATALOG_ORDER_RESILIENCE] pre-brain safe send failed tenant=%s to=%s",
                    tenant_id,
                    to,
                )

        # ── Checkout route owner: explicit structured channel chrome only ──
        # Button IDs/titles may execute deterministically. Unstructured NL,
        # including pending-choice free text, returns to Brain.
        _checkout_route_decision = None
        try:
            from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: PLC0415
                evaluate_checkout_route_owner,
            )

            _checkout_route_decision = evaluate_checkout_route_owner(
                db,
                tenant_id=tenant_id,
                customer_phone=to,
                message=text or "",
                inbound_metadata=inbound_metadata if isinstance(inbound_metadata, dict) else None,
            )
        except Exception:
            logger.exception(
                "[CHECKOUT_ROUTE] pre-brain owner failed tenant=%s to=%s",
                tenant_id,
                to,
            )
            _checkout_route_decision = None

        if _checkout_route_decision is not None:
            _checkout_reply = str(_checkout_route_decision.reply_text or "").strip()
            if _checkout_reply and _trace.outbound_lock_acquired():
                _persona_ownership.mark_bypass(
                    _POReason.PRE_BRAIN_FAST_PATH,
                    owner=f"checkout_route_owner:{_checkout_route_decision.reason}",
                )
                _checkout_ok = False
                _checkout_delivery = "text"
                _buttons = list(getattr(_checkout_route_decision, "buttons", None) or [])
                _cta_url = str(getattr(_checkout_route_decision, "cta_url", "") or "").strip()
                _cta_label = (
                    str(getattr(_checkout_route_decision, "cta_label", "") or "").strip()
                    or "فتح المتجر الإلكتروني"
                )
                if _cta_url:
                    _checkout_delivery = "cta_url"
                    _checkout_ok = await _send_cta_url(
                        phone_id=phone_id,
                        to=to,
                        body_text=_checkout_reply,
                        btn_label=_cta_label,
                        btn_url=_cta_url,
                        _tenant_id=tenant_id,
                        _db=db,
                    )
                    if not _checkout_ok:
                        _checkout_reply = f"{_checkout_reply}\n{_cta_url}"
                        _checkout_ok = await _send_whatsapp_message(
                            phone_id=phone_id,
                            to=to,
                            text=_checkout_reply,
                            _tenant_id=tenant_id,
                            _db=db,
                        )
                        _checkout_delivery = "text"
                elif _buttons:
                    _checkout_delivery = "interactive"
                    _checkout_ok = await _send_interactive_reply(
                        phone_id=phone_id,
                        to=to,
                        body_text=_checkout_reply,
                        buttons=_buttons,
                        _tenant_id=tenant_id,
                        _db=db,
                    )
                    if not _checkout_ok:
                        _checkout_ok = await _send_whatsapp_message(
                            phone_id=phone_id,
                            to=to,
                            text=_checkout_reply,
                            _tenant_id=tenant_id,
                            _db=db,
                        )
                        _checkout_delivery = "text"
                else:
                    _checkout_ok = await _send_whatsapp_message(
                        phone_id=phone_id,
                        to=to,
                        text=_checkout_reply,
                        _tenant_id=tenant_id,
                        _db=db,
                    )

                if _checkout_ok:
                    StateManager.save_message(
                        db,
                        to,
                        _checkout_reply,
                        "outbound",
                        conversation_id=convo.id,
                        tenant_id=tenant_id,
                        extra_metadata={
                            **_persona_ownership.to_metadata(),
                            "reply_owner": "checkout_route_owner",
                            "checkout_route_reason": _checkout_route_decision.reason,
                            "checkout_route_delivery": _checkout_delivery,
                        },
                    )
                    _trace.response_goal = "checkout_route"
                    _trace.delivery = (
                        _TS.DELIVERY_INTERACTIVE
                        if _checkout_delivery in {"interactive", "cta_url"}
                        else _TS.DELIVERY_TEXT
                    )
                    _trace.mark_outbound_sent(
                        source=_TS.SOURCE_LAYER0,
                        length=len(_checkout_reply),
                    )
                    logger.info(
                        "[CHECKOUT_ROUTE] sent tenant=%s to=%s reason=%s delivery=%s",
                        tenant_id,
                        to,
                        _checkout_route_decision.reason,
                        _checkout_delivery,
                    )
            _sync_persona_observability()
            return

        # Pure greetings (cold or established) route through Brain persona_social
        # compose — no PRE_BRAIN_FAST_PATH / render_identity_reply shortcut.
        if (
            mode_decision.mode == MODE_IDENTITY_REPLY
            and mode_decision.identity_topic == "greeting"
        ):
            logger.info(
                "[PERSONA_SOCIAL] route=brain persona_social greeting tenant=%s",
                tenant_id,
            )

        if (
            mode_decision.mode == MODE_IDENTITY_REPLY
            and mode_decision.identity_topic == "identity"
        ):
            logger.info(
                "[PERSONA_IDENTITY] route=persona_identity intent=who_are_you "
                "webhook=bypass_disabled tenant=%s",
                tenant_id,
            )

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
                    # May 2026 #46 — no automatic pause_ai on
                    # customer-side escalation. The cooldown stamp
                    # already prevents the canonical handoff line
                    # from being replayed within 30 minutes; the
                    # mode resolver no longer routes back into this
                    # branch on subsequent inbounds unless staff has
                    # actively taken over (paused_by_human /
                    # taken_over_at), so the brain handles natural
                    # follow-up questions ("ايش طرق التوصيل؟")
                    # without being silenced.
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
                _persona_ownership.mark_bypass(
                    _POReason.WEBHOOK_ESCALATION,
                    owner="support_escalation",
                )
                StateManager.save_message(
                    db, to, reply, "outbound",
                    conversation_id=convo.id, tenant_id=tenant_id,
                    extra_metadata=_persona_ownership.to_metadata(),
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
                    except Exception as _stamp_exc:  # noqa: BLE001  # noqa: silent-ok — cooldown stamp is best-effort
                        logger.debug("[handoff] cooldown stamp failed: %s", _stamp_exc)
                else:
                    logger.error("[TRACE][5/6] HUMAN_HANDOFF_ACK_SEND_FAILED | tenant=%s to=%s", tenant_id, to)
                # May 2026 #46 — no automatic pause_ai on
                # customer-side escalation. The cooldown stamp
                # prevents replay; subsequent inbounds flow through
                # the brain (mode resolver only pivots to support
                # escalation when staff has actually taken over).
                logger.info(
                    "[OUTBOUND] tenant=%s to=%s source=support_escalation trigger=inbound "
                    "intent=human_handoff handoff_triggered=true dedup_blocked=false "
                    "reply_len=%d",
                    tenant_id, to, len(reply),
                )
                if _send_ok:
                    _trace.mark_outbound_sent(
                        source=_TS.SOURCE_SUPPORT_ESCALATION,
                        length=len(reply or ""),
                    )
                _sync_persona_observability()
                return

        # ── Pre-brain customer message (caption-only for media) ───────────
        _pre_brain_customer_msg = text or ""
        try:
            from modules.ai.media.routing_guard import (  # noqa: PLC0415
                resolve_pre_brain_customer_message as _resolve_pre_brain_msg,
            )
            _pre_brain_customer_msg = _resolve_pre_brain_msg(
                brain_text=text or "",
                inbound_metadata=inbound_metadata,
            )
        except Exception as _pbr_exc:  # noqa: BLE001
            logger.warning(
                "[MEDIA_ROUTING_GUARD] resolve failed tenant=%s err=%s",
                tenant_id, _pbr_exc,
            )

        # ── Branch trigger router (pre-brain, PR-C structured keywords) ───
        _btr_decision = None
        try:
            from modules.ai.brain.commerce.branch_trigger_router import (  # noqa: PLC0415
                evaluate_branch_trigger_routing as _evaluate_branch_trigger_routing,
            )
            from modules.ai.brain.turn_owner_contract import (  # noqa: PLC0415
                build_prebrain_route_contract,
            )

            _prebrain_route_contract = build_prebrain_route_contract(
                message=text or "",
                inbound_metadata=inbound_metadata,
            )
            _btr_inbound_metadata = dict(inbound_metadata or {})
            if _prebrain_route_contract.suppress_reason:
                _btr_inbound_metadata["prebrain_route_contract"] = (
                    _prebrain_route_contract.to_metadata()
                )
            _btr_decision = _evaluate_branch_trigger_routing(
                db,
                tenant_id=tenant_id,
                message=text or "",
                customer_phone=to,
                inbound_metadata=_btr_inbound_metadata,
            )
        except Exception as _btr_exc:  # noqa: BLE001
            logger.warning(
                "[BRANCH_TRIGGER_ROUTER] pre-brain check failed tenant=%s err=%s",
                tenant_id, _btr_exc,
            )
            _btr_decision = None

        if _btr_decision is not None:
            _persona_ownership.mark_bypass(
                _POReason.STAFF_CONTACT_RECOVERY,
                owner="branch_trigger_router",
            )
            _btr_reply = _btr_decision.reply_text
            _btr_compose_meta: dict = {}
            _btr_compose_facts = getattr(_btr_decision, "compose_facts", None)
            if _btr_compose_facts is not None:
                try:
                    from modules.ai.brain.persona.branch_action_compose import (  # noqa: PLC0415
                        compose_branch_trigger_body,
                        plain_text_location_fallback_body,
                    )

                    _btr_compose_out = await compose_branch_trigger_body(
                        db,
                        tenant_id=tenant_id,
                        customer_phone=to,
                        compose_facts=_btr_compose_facts,
                    )
                    _btr_reply = plain_text_location_fallback_body(
                        _btr_compose_out.text,
                        _btr_decision.maps_url,
                        use_cta=bool(
                            _btr_decision.use_cta and _btr_decision.maps_url
                        ),
                    )
                    _btr_compose_meta = _btr_compose_out.to_metadata()
                except Exception as _btr_comp_exc:  # noqa: BLE001
                    logger.warning(
                        "[BRANCH_TRIGGER_ROUTER] compose failed tenant=%s err=%s",
                        tenant_id,
                        _btr_comp_exc,
                    )
            _btr_ok = False
            _btr_vcard_ok = False
            _btr_cta_fallback = False
            try:
                if _btr_decision.use_cta and _btr_decision.maps_url:
                    _btr_ok = await _send_cta_url(
                        phone_id=phone_id,
                        to=to,
                        body_text=_btr_reply or "…",
                        btn_label=_btr_decision.cta_button_label or "موقع المتجر",
                        btn_url=_btr_decision.maps_url,
                        _tenant_id=tenant_id,
                        _db=db,
                    )
                    if not _btr_ok:
                        from modules.ai.brain.persona.branch_action_compose import (  # noqa: PLC0415
                            plain_text_location_fallback_body,
                        )

                        _btr_cta_fallback = True
                        _btr_reply = plain_text_location_fallback_body(
                            _btr_reply,
                            _btr_decision.maps_url,
                            use_cta=False,
                        )
                        _btr_ok = await _send_whatsapp_message(
                            phone_id=phone_id,
                            to=to,
                            text=_btr_reply,
                            _tenant_id=tenant_id,
                            _db=db,
                            _inbound_message_id=wa_msg_id,
                        )
                elif _btr_reply:
                    _btr_ok = await _send_whatsapp_message(
                        phone_id=phone_id, to=to, text=_btr_reply,
                        _tenant_id=tenant_id, _db=db,
                        _inbound_message_id=wa_msg_id,
                    )

                _btr_contact_target = None
                if _btr_decision.deliver_contact and _btr_decision.call_target is not None:
                    _btr_contact_target = _btr_decision.call_target
                elif (
                    _btr_decision.deliver_reception_after_maps
                    and _btr_decision.reception_call_target is not None
                ):
                    _btr_contact_target = _btr_decision.reception_call_target

                if _btr_contact_target is not None:
                    from services.call_resolver import (  # noqa: PLC0415
                        build_contacts_payload as _btr_build_contacts,
                    )
                    _btr_payload = _btr_build_contacts([_btr_contact_target], to=to)
                    _btr_vcard_ok = await _send_contacts_message(
                        phone_id=phone_id, to=to,
                        payload=_btr_payload,
                        _tenant_id=tenant_id, _db=db,
                        customer_message=text or "",
                        delivery_path="branch_trigger_router",
                        escalation_reason=_btr_decision.reason or "",
                        policy_deliver_contact=True,
                    )
                    if _btr_vcard_ok and _btr_decision.persist_contact:
                        try:
                            from modules.ai.brain.commerce.contact_escalation import (  # noqa: PLC0415
                                persist_staff_contact_sent,
                            )
                            from core.order_flow import _load_brain_state  # noqa: PLC0415

                            _btr_conv, _btr_bs = _load_brain_state(
                                db, tenant_id=tenant_id, phone=to,
                            )
                            persist_staff_contact_sent(
                                db,
                                tenant_id=tenant_id,
                                phone=to,
                                name=getattr(_btr_contact_target, "name", "") or "",
                                contact_phone=(
                                    getattr(_btr_contact_target, "raw_phone", "")
                                    or getattr(_btr_contact_target, "wa_id", "")
                                    or ""
                                ),
                                turn=int((_btr_bs or {}).get("turn") or 0),
                            )
                        except Exception as _btr_persist_exc:  # noqa: BLE001
                            logger.exception(
                                "[BRANCH_TRIGGER_ROUTER] persist failed tenant=%s",
                                tenant_id,
                            )
                    elif not _btr_vcard_ok:
                        _trusted_phone = (
                            getattr(_btr_contact_target, "phone_display", "")
                            or getattr(_btr_contact_target, "raw_phone", "")
                            or getattr(_btr_contact_target, "wa_id", "")
                            or ""
                        )
                        if _trusted_phone:
                            if _btr_ok and (_btr_reply or "").strip():
                                _phone_fallback = _trusted_phone
                            else:
                                _phone_fallback = (
                                    f"{(_btr_reply or '').strip()}\n{_trusted_phone}".strip()
                                )
                            if _phone_fallback:
                                _btr_ok = await _send_whatsapp_message(
                                    phone_id=phone_id,
                                    to=to,
                                    text=_phone_fallback,
                                    _tenant_id=tenant_id,
                                    _db=db,
                                )
                                if not (_btr_reply or "").strip():
                                    _btr_reply = _phone_fallback
                                _btr_compose_meta["contact_card_fallback"] = "trusted_phone"
                    if (
                        _btr_decision.deliver_reception_after_maps
                        and _btr_decision.reception_reply_text
                        and _btr_vcard_ok
                    ):
                        await _send_whatsapp_message(
                            phone_id=phone_id, to=to,
                            text=_btr_decision.reception_reply_text,
                            _tenant_id=tenant_id, _db=db,
                        )
                        _btr_reply = _btr_decision.reception_reply_text

                StateManager.save_message(
                    db, to, _btr_reply or "", "outbound",
                    conversation_id=convo.id, tenant_id=tenant_id,
                    extra_metadata={
                        **_persona_ownership.to_metadata(),
                        "deterministic_path": _btr_decision.metadata_path,
                        "branch_trigger_type": _btr_decision.trigger_type,
                        "branch_trigger_reason": _btr_decision.reason,
                        "branch_trigger_phrase": _btr_decision.matched_phrase,
                        "branch_id": _btr_decision.branch_id,
                        "branch_vcard_sent": _btr_vcard_ok,
                        **_btr_compose_meta,
                        **({"cta_send_fallback": "plain_text_url"} if _btr_cta_fallback else {}),
                    },
                )
                _pending_choice = str(
                    getattr(_btr_decision, "persist_pending_choice", "") or "",
                ).strip()
                if _btr_ok and _pending_choice:
                    try:
                        from modules.ai.brain.commerce.pending_operational_choice import (  # noqa: PLC0415
                            persist_pending_operational_choice as _persist_pending_choice,
                        )

                        _persist_pending_choice(
                            db,
                            tenant_id=tenant_id,
                            phone=to,
                            choice=_pending_choice,
                            branch_id=int(
                                getattr(_btr_decision, "pending_branch_id", 0) or 0,
                            ),
                        )
                    except Exception as _pending_exc:  # noqa: BLE001
                        logger.warning(
                            "[BRANCH_TRIGGER_ROUTER] pending choice persist "
                            "failed tenant=%s err=%s",
                            tenant_id,
                            _pending_exc,
                        )
            except Exception as _btr_send_exc:  # noqa: BLE001
                logger.warning(
                    "[BRANCH_TRIGGER_ROUTER] send failed tenant=%s err=%s",
                    tenant_id, _btr_send_exc,
                )
            logger.info(
                "[BRANCH_TRIGGER_ROUTER] short_circuit tenant=%s trigger=%s "
                "reason=%s ok=%s vcard_ok=%s skip_brain=true",
                tenant_id,
                _btr_decision.trigger_type,
                _btr_decision.reason,
                _btr_ok,
                _btr_vcard_ok,
            )
            _sync_persona_observability()
            return

        # ── Location link policy (pre-brain) ──────────────────────────────
        # Physical location asks must never enter staff escalation policy.
        # Media inbounds (PDF/image/audio/video) must not use brain/OCR
        # text — only the customer's caption counts as current intent.
        _llp_decision = None
        _pre_brain_customer_msg = text or ""
        try:
            from modules.ai.media.routing_guard import (  # noqa: PLC0415
                resolve_pre_brain_customer_message as _resolve_pre_brain_msg,
            )
            _pre_brain_customer_msg = _resolve_pre_brain_msg(
                brain_text=text or "",
                inbound_metadata=inbound_metadata,
            )
        except Exception as _pbr_exc:  # noqa: BLE001
            logger.warning(
                "[MEDIA_ROUTING_GUARD] resolve failed tenant=%s err=%s",
                tenant_id, _pbr_exc,
            )
        try:
            from modules.ai.brain.commerce.location_link_policy import (  # noqa: PLC0415
                evaluate_location_link_policy as _evaluate_location_link_policy,
            )
            _llp_decision = _evaluate_location_link_policy(
                db,
                tenant_id=tenant_id,
                message=_pre_brain_customer_msg,
            )
        except Exception as _llp_exc:  # noqa: BLE001
            logger.warning(
                "[LOCATION_LINK_POLICY] pre-brain check failed tenant=%s err=%s",
                tenant_id, _llp_exc,
            )
            _llp_decision = None

        if _llp_decision is not None:
            _llp_reply = _llp_decision.reply_text
            try:
                if (
                    _llp_decision.maps_url
                    and getattr(_llp_decision, "use_cta", False)
                ):
                    _llp_ok = await _send_cta_url(
                        phone_id=phone_id,
                        to=to,
                        body_text=_llp_reply or "موقعنا 📍",
                        btn_label=(
                            getattr(_llp_decision, "cta_button_label", "")
                            or "موقع المتجر"
                        ),
                        btn_url=_llp_decision.maps_url,
                        _tenant_id=tenant_id,
                        _db=db,
                    )
                else:
                    _llp_ok = await _send_whatsapp_message(
                        phone_id=phone_id, to=to, text=_llp_reply,
                        _tenant_id=tenant_id, _db=db,
                    )
                StateManager.save_message(
                    db, to, _llp_reply, "outbound",
                    conversation_id=convo.id, tenant_id=tenant_id,
                    extra_metadata={
                        **_persona_ownership.to_metadata(),
                        "deterministic_path": "location_link_policy",
                        "location_link_policy_reason": _llp_decision.reason,
                        "location_maps_source": _llp_decision.source,
                        "location_delivery_mode": (
                            "cta_url" if getattr(_llp_decision, "use_cta", False)
                            else "text"
                        ),
                    },
                )
            except Exception as _llp_send_exc:  # noqa: BLE001
                logger.warning(
                    "[LOCATION_LINK_POLICY] send failed tenant=%s err=%s",
                    tenant_id, _llp_send_exc,
                )
                _llp_ok = False
            logger.info(
                "[LOCATION_LINK_POLICY] short_circuit tenant=%s deliver=%s "
                "mode=%s text_ok=%s skip_brain=true",
                tenant_id,
                bool(_llp_decision.maps_url),
                "cta_url" if getattr(_llp_decision, "use_cta", False) else "text",
                _llp_ok,
            )
            try:
                _trace.fallback_source = "location_link_policy"
                _trace.response_goal = "store_location"
                _trace.intent = "ask_location"
                if _llp_ok:
                    _trace.mark_outbound_sent(
                        source=_TS.SOURCE_BRAIN,
                        length=len(_llp_reply or ""),
                    )
            except Exception:  # noqa: BLE001
                pass
            _sync_persona_observability()
            return

        # ── Arrival contact delivery (pre-brain) ─────────────────────────
        # Showroom-first from compiled arrival_contact evidence — before LLM.
        _acd_decision = None
        try:
            from modules.ai.brain.commerce.arrival_contact_delivery_policy import (  # noqa: PLC0415
                evaluate_arrival_contact_delivery as _evaluate_arrival_contact_delivery,
            )
            _acd_decision = _evaluate_arrival_contact_delivery(
                db,
                tenant_id=tenant_id,
                message=_pre_brain_customer_msg,
                customer_phone=to or "",
            )
        except Exception as _acd_exc:  # noqa: BLE001
            logger.warning(
                "[ARRIVAL_CONTACT_DELIVERY] pre-brain check failed tenant=%s err=%s",
                tenant_id, _acd_exc,
            )
            _acd_decision = None

        if _acd_decision is not None:
            _persona_ownership.mark_bypass(
                _POReason.STAFF_CONTACT_RECOVERY,
                owner="arrival_contact_delivery",
            )
            from modules.ai.brain.commerce.arrival_contact_delivery_policy import (  # noqa: PLC0415
                MSG_ARRIVAL_CONTACT_NOT_CONFIGURED,
            )
            from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
                MSG_CONTACT_CARD_FAILED,
            )
            _acd_reply = _acd_decision.reply_text
            _acd_text_ok = False
            _acd_contacts_ok = False
            try:
                if (
                    _acd_decision.deliver_contact
                    and _acd_decision.call_target is not None
                ):
                    from services.call_resolver import (  # noqa: PLC0415
                        build_contacts_payload as _acd_build_contacts,
                    )
                    _acd_payload = _acd_build_contacts(
                        [_acd_decision.call_target], to=to,
                    )
                    _acd_contacts_ok = await _send_contacts_message(
                        phone_id=phone_id, to=to,
                        payload=_acd_payload,
                        _tenant_id=tenant_id, _db=db,
                        customer_message=text or "",
                        delivery_path="arrival_contact_delivery",
                        policy_deliver_contact=bool(_acd_decision.deliver_contact),
                    )
                    if _acd_contacts_ok:
                        try:
                            from modules.ai.brain.commerce.contact_escalation import (  # noqa: PLC0415
                                persist_staff_contact_sent,
                            )
                            from core.order_flow import _load_brain_state  # noqa: PLC0415

                            _acd_conv, _acd_bs = _load_brain_state(
                                db, tenant_id=tenant_id, phone=to,
                            )
                            persist_staff_contact_sent(
                                db,
                                tenant_id=tenant_id,
                                phone=to,
                                name=(
                                    getattr(_acd_decision.call_target, "name", "")
                                    or _acd_decision.contact_lookup_name
                                ),
                                contact_phone=(
                                    getattr(_acd_decision.call_target, "raw_phone", "")
                                    or getattr(_acd_decision.call_target, "wa_id", "")
                                    or _acd_decision.contact_phone
                                ),
                                turn=int((_acd_bs or {}).get("turn") or 0),
                            )
                        except Exception as _acd_persist_exc:  # noqa: BLE001
                            logger.exception(
                                "[ARRIVAL_CONTACT_DELIVERY] persist failed tenant=%s",
                                tenant_id,
                            )
                        _acd_text_ok = await _send_whatsapp_message(
                            phone_id=phone_id, to=to, text=_acd_reply,
                            _tenant_id=tenant_id, _db=db,
                        )
                    else:
                        logger.warning(
                            "[ARRIVAL_CONTACT_DELIVERY] vCard send failed tenant=%s",
                            tenant_id,
                        )
                        _acd_reply = MSG_CONTACT_CARD_FAILED
                        _acd_text_ok = await _send_whatsapp_message(
                            phone_id=phone_id, to=to, text=_acd_reply,
                            _tenant_id=tenant_id, _db=db,
                        )
                else:
                    _acd_text_ok = await _send_whatsapp_message(
                        phone_id=phone_id, to=to, text=_acd_reply,
                        _tenant_id=tenant_id, _db=db,
                    )
                StateManager.save_message(
                    db, to, _acd_reply, "outbound",
                    conversation_id=convo.id, tenant_id=tenant_id,
                    extra_metadata={
                        **_persona_ownership.to_metadata(),
                        "deterministic_path": "arrival_contact_delivery",
                        "arrival_contact_delivery_reason": _acd_decision.reason,
                        "arrival_contact_deliver": _acd_decision.deliver_contact,
                        "arrival_vcard_sent": _acd_contacts_ok,
                    },
                )
            except Exception as _acd_send_exc:  # noqa: BLE001
                logger.warning(
                    "[ARRIVAL_CONTACT_DELIVERY] send failed tenant=%s err=%s",
                    tenant_id, _acd_send_exc,
                )

            logger.info(
                "[ARRIVAL_CONTACT_DELIVERY] short_circuit tenant=%s deliver=%s "
                "vCard_ok=%s skip_brain=true reason=%s",
                tenant_id,
                _acd_decision.deliver_contact and _acd_contacts_ok,
                _acd_contacts_ok,
                _acd_decision.reason,
            )
            try:
                _trace.fallback_source = "arrival_contact_delivery"
                _trace.response_goal = "arrival_contact"
                _trace.intent = "store_arrival"
                if _acd_text_ok:
                    _trace.mark_outbound_sent(
                        source=_TS.SOURCE_BRAIN,
                        length=len(_acd_reply or ""),
                    )
            except Exception:  # noqa: BLE001
                pass
            _sync_persona_observability()
            return

        # ── Staff contact policy (Phase A) ────────────────────────────────
        # Explicit CS / named contact asks — deterministic evidence only.
        _scp_decision = None
        try:
            from modules.ai.brain.commerce.staff_contact_policy import (  # noqa: PLC0415
                evaluate_staff_contact_policy as _evaluate_staff_contact_policy,
            )
            _scp_decision = _evaluate_staff_contact_policy(
                db,
                tenant_id=tenant_id,
                message=text or "",
                customer_phone=to or "",
            )
        except Exception as _scp_exc:  # noqa: BLE001
            logger.warning(
                "[STAFF_CONTACT_POLICY] pre-brain check failed tenant=%s err=%s",
                tenant_id, _scp_exc,
            )
            _scp_decision = None

        if _scp_decision is not None and _generic_handoff_signal:
            logger.info(
                "[STAFF_CONTACT_POLICY] yield to Brain/D2 generic_handoff_signal "
                "tenant=%s kind=%s",
                tenant_id,
                getattr(_scp_decision, "request_kind", "") or "",
            )
            _scp_decision = None

        if _scp_decision is not None:
            _persona_ownership.mark_bypass(
                _POReason.STAFF_CONTACT_RECOVERY,
                owner="staff_contact_policy",
            )
            _scp_reply = _scp_decision.reply_text
            _scp_text_ok = False
            try:
                _scp_text_ok = await _send_whatsapp_message(
                    phone_id=phone_id, to=to, text=_scp_reply,
                    _tenant_id=tenant_id, _db=db,
                )
                StateManager.save_message(
                    db, to, _scp_reply, "outbound",
                    conversation_id=convo.id, tenant_id=tenant_id,
                    extra_metadata={
                        **_persona_ownership.to_metadata(),
                        "deterministic_path": "staff_contact_policy",
                        "staff_contact_policy_kind": _scp_decision.request_kind,
                        "staff_contact_policy_reason": _scp_decision.reason,
                        "staff_contact_evidence_source": _scp_decision.evidence_source,
                    },
                )
                try:
                    from modules.ai.brain.commerce.staff_contact_target_continuity import (  # noqa: PLC0415
                        try_persist_pending_contact_target_from_outbound,
                    )

                    try_persist_pending_contact_target_from_outbound(
                        db,
                        tenant_id=tenant_id,
                        phone=to,
                        outbound_text=_scp_reply or "",
                        source="staff_contact_policy_outbound",
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[STAFF_CONTACT_POLICY] pending_contact_target_outbound_capture_failed",
                    )
            except Exception as _scp_send_exc:  # noqa: BLE001
                logger.warning(
                    "[STAFF_CONTACT_POLICY] text send failed tenant=%s err=%s",
                    tenant_id, _scp_send_exc,
                )

            _scp_contacts_ok = False
            if (
                _scp_decision.deliver_contact
                and _scp_decision.call_target is not None
            ):
                try:
                    from services.call_resolver import (  # noqa: PLC0415
                        build_contacts_payload as _scp_build_contacts,
                    )
                    _scp_payload = _scp_build_contacts(
                        [_scp_decision.call_target], to=to,
                    )
                    _scp_contacts_ok = await _send_contacts_message(
                        phone_id=phone_id, to=to,
                        payload=_scp_payload,
                        _tenant_id=tenant_id, _db=db,
                        customer_message=text or "",
                        delivery_path="staff_contact_policy",
                        intent_name=_scp_decision.request_kind or "",
                        policy_deliver_contact=bool(_scp_decision.deliver_contact),
                    )
                    if _scp_contacts_ok:
                        try:
                            from modules.ai.brain.commerce.contact_escalation import (  # noqa: PLC0415
                                persist_staff_contact_sent,
                            )
                            from core.order_flow import _load_brain_state  # noqa: PLC0415

                            _scp_conv, _scp_bs = _load_brain_state(
                                db, tenant_id=tenant_id, phone=to,
                            )
                            persist_staff_contact_sent(
                                db,
                                tenant_id=tenant_id,
                                phone=to,
                                name=getattr(_scp_decision.call_target, "name", "") or "",
                                contact_phone=(
                                    getattr(_scp_decision.call_target, "raw_phone", "")
                                    or getattr(_scp_decision.call_target, "wa_id", "")
                                ),
                                turn=int((_scp_bs or {}).get("turn") or 0),
                            )
                        except Exception as _scp_persist_exc:  # noqa: BLE001
                            logger.exception(
                                "[STAFF_CONTACT_POLICY] persist failed tenant=%s",
                                tenant_id,
                            )
                except Exception as _scp_card_exc:  # noqa: BLE001
                    logger.warning(
                        "[STAFF_CONTACT_POLICY] vCard send failed tenant=%s err=%s",
                        tenant_id, _scp_card_exc,
                    )

            logger.info(
                "[STAFF_CONTACT_POLICY] short_circuit tenant=%s to=%s "
                "kind=%s deliver=%s text_ok=%s contacts_ok=%s skip_brain=true",
                tenant_id, to,
                _scp_decision.request_kind,
                _scp_decision.deliver_contact,
                _scp_text_ok, _scp_contacts_ok,
            )
            try:
                _trace.fallback_source = "staff_contact_policy"
                _trace.response_goal = "staff_contact_evidence"
                _trace.intent = _scp_decision.request_kind or "staff_contact"
                if _scp_text_ok:
                    _trace.mark_outbound_sent(
                        source=_TS.SOURCE_BRAIN,
                        length=len(_scp_reply or ""),
                    )
            except Exception:  # noqa: BLE001
                pass
            _sync_persona_observability()
            return

        # ── Staff contact recovery (P1 Slice 2) ─────────────────────────────
        # «ما يرد» after a staff vCard was sent must advance the KB chain
        # deterministically — never fall through to LLM generic greeting reset.
        _scr_decision = None
        try:
            from modules.ai.brain.commerce.staff_contact_recovery import (  # noqa: PLC0415
                maybe_staff_contact_recovery as _maybe_staff_contact_recovery,
            )
            _scr_decision = _maybe_staff_contact_recovery(
                db,
                tenant_id=tenant_id,
                phone=to,
                message=text or "",
                conversation_id=getattr(convo, "id", None),
            )
        except Exception as _scr_exc:  # noqa: BLE001
            logger.warning(
                "[STAFF_CONTACT_RECOVERY] pre-brain check failed tenant=%s err=%s",
                tenant_id, _scr_exc,
            )
            _scr_decision = None

        if _scr_decision is not None:
            _persona_ownership.mark_bypass(
                _POReason.STAFF_CONTACT_RECOVERY,
                owner="staff_contact_recovery",
            )
            from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
                MSG_CONTACT_CARD_FAILED,
                MSG_NO_NEXT_ESCALATION,
            )
            _scr_reply = _scr_decision.reply_text
            _scr_text_ok = False
            _scr_contacts_ok = False
            try:
                if (
                    _scr_decision.deliver_contact
                    and _scr_decision.call_target is not None
                ):
                    from services.call_resolver import (  # noqa: PLC0415
                        build_contacts_payload as _scr_build_contacts,
                    )
                    _scr_payload = _scr_build_contacts(
                        [_scr_decision.call_target], to=to,
                    )
                    _scr_contacts_ok = await _send_contacts_message(
                        phone_id=phone_id, to=to,
                        payload=_scr_payload,
                        _tenant_id=tenant_id, _db=db,
                        customer_message=text or "",
                        delivery_path="staff_contact_recovery",
                        policy_deliver_contact=bool(_scr_decision.deliver_contact),
                    )
                    if _scr_contacts_ok:
                        try:
                            from modules.ai.brain.commerce.contact_escalation import (  # noqa: PLC0415
                                persist_staff_contacts_sent_batch,
                            )
                            persist_staff_contacts_sent_batch(
                                db,
                                tenant_id=tenant_id,
                                phone=to,
                                entries=[{
                                    "name": _scr_decision.next_contact_name,
                                    "phone": (
                                        getattr(
                                            _scr_decision.call_target,
                                            "wa_id",
                                            "",
                                        )
                                        or getattr(
                                            _scr_decision.call_target,
                                            "raw_phone",
                                            "",
                                        )
                                        or _scr_decision.next_contact_phone
                                    ),
                                    "turn": int(_scr_decision.conversation_turn or 0),
                                }],
                            )
                        except Exception as _scr_persist_exc:  # noqa: BLE001
                            logger.exception(
                                "[STAFF_CONTACT_RECOVERY] persist failed tenant=%s",
                                tenant_id,
                            )
                        _scr_text_ok = await _send_whatsapp_message(
                            phone_id=phone_id, to=to, text=_scr_reply,
                            _tenant_id=tenant_id, _db=db,
                        )
                    else:
                        logger.warning(
                            "[STAFF_CONTACT_RECOVERY] vCard send failed tenant=%s",
                            tenant_id,
                        )
                        _scr_reply = MSG_CONTACT_CARD_FAILED
                        _scr_text_ok = await _send_whatsapp_message(
                            phone_id=phone_id, to=to, text=_scr_reply,
                            _tenant_id=tenant_id, _db=db,
                        )
                else:
                    _scr_text_ok = await _send_whatsapp_message(
                        phone_id=phone_id, to=to, text=_scr_reply,
                        _tenant_id=tenant_id, _db=db,
                    )
                StateManager.save_message(
                    db, to, _scr_reply, "outbound",
                    conversation_id=convo.id, tenant_id=tenant_id,
                    extra_metadata={
                        **_persona_ownership.to_metadata(),
                        "deterministic_path": "staff_contact_recovery",
                        "staff_contact_recovery_trigger": _scr_decision.trigger,
                        "staff_contact_recovery_reason": _scr_decision.reason,
                        "staff_recovery_vcard_sent": _scr_contacts_ok,
                    },
                )
                try:
                    from modules.ai.brain.commerce.staff_contact_target_continuity import (  # noqa: PLC0415
                        try_persist_pending_contact_target_from_outbound,
                    )

                    try_persist_pending_contact_target_from_outbound(
                        db,
                        tenant_id=tenant_id,
                        phone=to,
                        outbound_text=_scr_reply or "",
                        source="staff_contact_recovery_outbound",
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[STAFF_CONTACT_RECOVERY] pending_contact_target_outbound_capture_failed",
                    )
            except Exception as _scr_send_exc:  # noqa: BLE001
                logger.warning(
                    "[STAFF_CONTACT_RECOVERY] send failed tenant=%s err=%s",
                    tenant_id, _scr_send_exc,
                )
                _scr_text_ok = False

            logger.info(
                "[STAFF_CONTACT_RECOVERY] short_circuit tenant=%s to=%s "
                "text_ok=%s contacts_ok=%s selected=%r skip_brain=true",
                tenant_id, to, _scr_text_ok, _scr_contacts_ok,
                (_scr_decision.next_contact_name or "")[:48],
            )
            try:
                _trace.fallback_source = "staff_contact_recovery"
                _trace.response_goal = "staff_contact_fallback"
                _trace.intent = "employee_not_responding"
                if _scr_text_ok:
                    _trace.mark_outbound_sent(
                        source=_TS.SOURCE_BRAIN,
                        length=len(_scr_reply or ""),
                    )
                _sync_persona_observability()
                _trace.emit()
            except Exception:  # noqa: BLE001
                pass
            return

        # ── Layer 0 Router (Phase A — zero LLM, pre-Brain) ────────────────
        try:
            from modules.ai.routing.layer0_router import (  # noqa: PLC0415
                evaluate_layer0_route as _evaluate_layer0_route,
                layer0_router_enabled as _layer0_enabled,
            )
            if _layer0_enabled():
                _l0_decision = _evaluate_layer0_route(
                    db,
                    tenant_id=tenant_id,
                    customer_phone=to,
                    message=text or "",
                    history=history,
                    conversation_id=convo.id,
                )
                if _l0_decision is not None:
                    _l0_reply = _l0_decision.reply_text
                    _l0_ok = await _send_whatsapp_message(
                        phone_id=phone_id,
                        to=to,
                        text=_l0_reply,
                        _tenant_id=tenant_id,
                        _db=db,
                    )
                    if _l0_ok:
                        try:
                            from modules.ai.brain.postprocess.social_single_reply_guard import (  # noqa: PLC0415
                                SocialReplySelection,
                                claim_social_reply_selection,
                            )

                            claim_social_reply_selection(
                                _trace,
                                selection=SocialReplySelection(
                                    action=_l0_decision.intent_name or _l0_decision.matched,
                                    source="layer0_router",
                                    category=_l0_decision.social_category,
                                ),
                            )
                        except Exception:  # noqa: BLE001  # noqa: silent-ok — trace claim must not block send
                            pass
                        _trace.mark_outbound_sent(
                            source=_TS.SOURCE_LAYER0,
                            length=len(_l0_reply or ""),
                        )
                    try:
                        StateManager.save_message(
                            db,
                            to,
                            _l0_reply,
                            "outbound",
                            conversation_id=convo.id,
                            tenant_id=tenant_id,
                            extra_metadata={
                                **_persona_ownership.to_metadata(),
                                "deterministic_path": f"layer0:{_l0_decision.matched}",
                                "layer0_matched": _l0_decision.matched,
                                "layer0_intent": _l0_decision.intent_name,
                            },
                        )
                    except Exception as _l0_save_exc:  # noqa: BLE001
                        logger.exception(
                            "[LAYER0_ROUTER] persist failed tenant=%s err=%s "
                            "— outbound already sent, not falling through to Brain",
                            tenant_id,
                            _l0_save_exc,
                        )
                    try:
                        _trace.intent = _l0_decision.intent_name or _l0_decision.matched
                        _trace.response_goal = f"layer0_{_l0_decision.matched}"
                        _trace.fallback_source = "layer0_router"
                    except Exception:  # noqa: BLE001
                        pass
                    _sync_persona_observability()
                    return
        except Exception as _l0_exc:  # noqa: BLE001
            logger.warning(
                "[LAYER0_ROUTER] pre-brain check failed tenant=%s err=%s — "
                "falling through to Brain",
                tenant_id,
                _l0_exc,
            )

        # ── Merchant Brain (Phase 1) ──────────────────────────────────────────
        # Active when: global flag is on OR this tenant is in the per-tenant list
        _brain_active = MERCHANT_BRAIN_ENABLED or (tenant_id in MERCHANT_BRAIN_TENANT_IDS)
        if _brain_active and not _trace.outbound_lock_acquired():
            logger.warning(
                "[BRAIN] skipped — outbound already sent for this inbound | "
                "tenant=%s to=%s reply_source=%s",
                tenant_id,
                to,
                getattr(_trace, "reply_source", ""),
            )
            _sync_persona_observability()
            return
        if _brain_active:
            from services.merchant_brain_turn import (  # noqa: PLC0415
                LiveMerchantBrainPreconditions,
                LiveMerchantBrainTurnInput,
                evaluate_live_merchant_brain_turn,
            )

            profile = {}
            try:
                try:
                    if _t_pre_brain_remaining is not None:
                        import time as _time_pbr_ci  # noqa: PLC0415
                        from core.turn_latency import safe_record_ms as _tl_pbr  # noqa: PLC0415

                        _tl_pbr(
                            "pre_brain_remaining_prep",
                            (_time_pbr_ci.monotonic() - _t_pre_brain_remaining) * 1000.0,
                        )
                        _t_pre_brain_remaining = None
                except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                    pass
                from services.customer_intelligence import CustomerIntelligenceService  # noqa: PLC0415

                svc = CustomerIntelligenceService(db, tenant_id)
                try:
                    import time as _time_ci  # noqa: PLC0415
                    from core.turn_latency import safe_record_ms as _tl_ci  # noqa: PLC0415

                    _t_ci = _time_ci.monotonic()
                except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                    _t_ci = None
                    _tl_ci = None  # type: ignore[assignment]
                customer = svc.upsert_lead_customer(
                    phone=to,
                    source="whatsapp_inbound",
                    extra_metadata={
                        "channel": "whatsapp",
                        "normalized_inbound": inbound_metadata or {},
                    },
                    commit=False,
                )
                try:
                    if _t_ci is not None and _tl_ci is not None:
                        import time as _time_ci2  # noqa: PLC0415

                        _tl_ci(
                            "customer_intelligence_upsert",
                            (_time_ci2.monotonic() - _t_ci) * 1000.0,
                        )
                except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                    pass
                profile = _build_customer_ai_profile(customer, inbound_metadata)
                if customer is not None:
                    try:
                        import time as _time_prof  # noqa: PLC0415
                        from core.turn_latency import safe_record_ms as _tl_prof  # noqa: PLC0415

                        _t_prof = _time_prof.monotonic()
                    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                        _t_prof = None
                        _tl_prof = None  # type: ignore[assignment]
                    full_profile = svc.ensure_profile(customer)
                    try:
                        if _t_prof is not None and _tl_prof is not None:
                            import time as _time_prof2  # noqa: PLC0415

                            _tl_prof(
                                "customer_profile_ensure",
                                (_time_prof2.monotonic() - _t_prof) * 1000.0,
                            )
                    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                        pass
                    try:
                        import time as _time_pbr2  # noqa: PLC0415

                        _t_pre_brain_remaining = _time_pbr2.monotonic()
                    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                        _t_pre_brain_remaining = None
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

            from modules.ai.brain.pipeline import get_brain  # noqa: PLC0415

            _turn_preconditions = LiveMerchantBrainPreconditions(
                brain_active=True,
                skip_ai=bool(_skip),
                skip_reason=_skip_reason,
                human_priority=(_skip_reason == "human_priority"),
                billing_allowed=True,
                conversation_quota_allowed=True,
                outbound_lock_available=_trace.outbound_lock_acquired(),
            )
            _turn_input = LiveMerchantBrainTurnInput(
                customer_phone=to,
                text=text or "",
                inbound_metadata=inbound_metadata if isinstance(inbound_metadata, dict) else None,
                wa_msg_id=wa_msg_id,
                conversation_id=getattr(convo, "id", None),
                history=history,
                preconditions=_turn_preconditions,
                profile=profile,
            )
            # V2 Phase 1 is a shadow-only copy. It owns no outbound function,
            # runs in a separate DB session/task, and is fully gated OFF by
            # default. V1 remains the sole reply owner below.
            if not _skip and COMMERCE_AGENT_V2_ENABLED:
                try:
                    from modules.ai.commerce_agent_v2.shadow import (  # noqa: PLC0415
                        schedule_commerce_agent_v2_shadow,
                    )

                    schedule_commerce_agent_v2_shadow(
                        tenant_id=int(tenant_id),
                        conversation_id=int(convo.id),
                        customer_id=(
                            int(convo.customer_id)
                            if getattr(convo, "customer_id", None) is not None
                            else None
                        ),
                        normalized_customer_phone=normalize_phone(to),
                        connection_id=str(getattr(wa_conn_hist, "id", "") or ""),
                        inbound_trace_id=str(wa_msg_id or f"conversation-{convo.id}"),
                        user_input=text or "",
                    )
                except Exception as _v2_shadow_exc:  # noqa: BLE001
                    logger.warning(
                        "[COMMERCE_V2_SHADOW_SCHEDULE_FAILED] tenant=%s conversation=%s error=%s",
                        tenant_id,
                        getattr(convo, "id", None),
                        type(_v2_shadow_exc).__name__,
                    )
            # Trusted-context source-order contract marker: brain.process(
            try:
                if _t_pre_brain_remaining is not None:
                    import time as _time_pbr3  # noqa: PLC0415
                    from core.turn_latency import safe_record_ms as _tl_pbr2  # noqa: PLC0415

                    _tl_pbr2(
                        "pre_brain_remaining_prep",
                        (_time_pbr3.monotonic() - _t_pre_brain_remaining) * 1000.0,
                    )
                    _t_pre_brain_remaining = None
            except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                pass
            try:
                import time as _time_bbe  # noqa: PLC0415
                from core.turn_latency import safe_record_ms  # noqa: PLC0415

                _t_brain_boundary = _time_bbe.monotonic()
                safe_record_ms("brain_boundary_enter", 0)
            except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                _t_brain_boundary = None
            _turn_eval = await evaluate_live_merchant_brain_turn(
                db=db,
                tenant_id=tenant_id,
                phone_id=phone_id,
                turn_input=_turn_input,
                convo=convo,
                trace=_trace,
                persona_ownership=_persona_ownership,
                brain_factory=get_brain,
                brain_active=True,
                skip_reason=_skip_reason,
            )
            try:
                if _t_brain_boundary is not None:
                    import time as _time_bbe2  # noqa: PLC0415
                    from core.turn_latency import (  # noqa: PLC0415
                        safe_mark_post_brain_dispatch_start,
                        safe_record_ms,
                    )

                    safe_record_ms(
                        "brain_boundary_exit",
                        (_time_bbe2.monotonic() - _t_brain_boundary) * 1000.0,
                    )
                    safe_mark_post_brain_dispatch_start()
            except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                pass
            if _turn_eval.status == "brain_exception":
                brain_exc = _turn_eval.brain_exception
                try:
                    db.rollback()
                except Exception as _rb_exc:  # noqa: BLE001
                    logger.warning(
                        "[Merchant/Brain] rollback after brain_exc failed "
                        "| tenant=%s to=%s rb_err=%s",
                        tenant_id, to, _rb_exc.__class__.__name__,
                    )
                try:
                    from core.conversation_engine import _diag_sql_error  # noqa: PLC0415

                    logger.error(
                        "[Merchant/Brain] brain_exc diag | tenant=%s to=%s | %s",
                        tenant_id, to, _diag_sql_error(brain_exc, db=db),
                    )
                except Exception:  # noqa: BLE001
                    pass
                _trace.mark_brain_exception(brain_exc)
                try:
                    from core.inbound_observability import (  # noqa: PLC0415
                        record_inbound_drop,
                        DROP_DISPATCHER_EXCEPTION,
                    )
                    record_inbound_drop(
                        tenant_id=tenant_id,
                        drop_kind=DROP_DISPATCHER_EXCEPTION,
                        customer_phone=to or "",
                        conversation_id=getattr(convo, "id", None),
                        inbound_preview=text or "",
                        chosen_path="brain_pipeline_raised",
                        detail=(
                            f"exception={brain_exc.__class__.__name__}: "
                            f"{str(brain_exc)[:200]}"
                        ),
                    )
                except Exception as _obs_exc:  # noqa: BLE001
                    logger.warning("[INBOUND_OBS] hook failed: %s", _obs_exc)
                if MERCHANT_BRAIN_ALLOW_LEGACY_FALLBACK:
                    logger.exception(
                        "[Merchant/Brain] Brain pipeline failed — falling back to legacy "
                        "(MERCHANT_BRAIN_ALLOW_LEGACY_FALLBACK=true) | tenant=%s to=%s",
                        tenant_id, to,
                    )
                    MERCHANT_BRAIN_ENABLED_FALLBACK = True
                else:
                    logger.exception(
                        "[Merchant/Brain] Brain pipeline failed — sending policy-driven safe reply, "
                        "legacy fallback DISABLED (set MERCHANT_BRAIN_ALLOW_LEGACY_FALLBACK=true to re-enable) "
                        "| tenant=%s to=%s",
                        tenant_id, to,
                    )
                    from services.fallback_policy import (  # noqa: PLC0415
                        FALLBACK_KIND_INTENT_DETERMINISTIC,
                        FALLBACK_KIND_SOFT_RETRY,
                        FALLBACK_REASON_BRAIN_EXCEPTION,
                        choose_intent_aware_fallback,
                    )
                    _ship_info_for_fallback: Dict[str, Any] = {}
                    try:
                        from core.store_knowledge import (  # noqa: PLC0415
                            build_merchant_context as _bmc,
                        )
                        _mctx = _bmc(db, tenant_id, customer_phone=to)
                        _policies = (_mctx or {}).get("policies") or {}
                        if isinstance(_policies, dict):
                            _ship_info_for_fallback = {
                                "shipping_methods": _policies.get("shipping_methods") or [],
                                "shipping_notes": _policies.get("shipping_notes") or "",
                                "shipping_policy": _policies.get("shipping_policy") or "",
                                "delivery_areas": _policies.get("delivery_areas") or [],
                                "support_hours": _policies.get("working_hours") or "",
                            }
                    except Exception:  # noqa: BLE001
                        _ship_info_for_fallback = {}
                    _safe_reply = ""
                    _d2_compose_ok = False
                    if _generic_handoff_signal:
                        from core.fallback_policy import (  # noqa: PLC0415
                            empty_reply_fallback as _d2_empty_fb,
                        )
                        from modules.ai.brain.execution.staff_escalation_execution import (  # noqa: PLC0415
                            compose_staff_escalation_with_verifier as _d2_compose,
                            d2_operational_escalation_succeeded as _d2_ok,
                            execute_staff_escalation_for_safety_signal as _d2_safety,
                        )
                        try:
                            _d2_safety_result = await _d2_safety(
                                db=db,
                                tenant_id=int(tenant_id),
                                customer_phone=to,
                                message=text or "",
                                convo=convo,
                            )
                            _d2_execution_result = _d2_safety_result
                            logger.info(
                                "[Merchant/HANDOFF_GUARD] Brain exception D2 safety "
                                "tenant=%s success=%s",
                                tenant_id,
                                bool(_d2_safety_result.success),
                            )
                        except Exception as _d2_exc:  # noqa: BLE001
                            logger.warning(
                                "[Merchant/HANDOFF_GUARD] Brain exception D2 safety "
                                "failed tenant=%s err=%s",
                                tenant_id,
                                type(_d2_exc).__name__,
                            )
                            _d2_execution_result = None
                        try:
                            if _d2_ok(_d2_execution_result):
                                _safe_reply = await _d2_compose(
                                    db=db,
                                    tenant_id=int(tenant_id),
                                    customer_phone=to,
                                    message=text or "",
                                    result=_d2_execution_result,
                                    conversation_id=getattr(convo, "id", None),
                                )
                            else:
                                _safe_reply = _d2_empty_fb()
                        except Exception as _d2_cmp_exc:  # noqa: BLE001
                            logger.warning(
                                "[Merchant/HANDOFF_GUARD] Brain exception D2 compose "
                                "failed tenant=%s err=%s",
                                tenant_id,
                                type(_d2_cmp_exc).__name__,
                            )
                            _safe_reply = _d2_empty_fb()
                        _safe_reply = str(_safe_reply or "").strip() or _d2_empty_fb()
                        _d2_compose_ok = True
                    if not _d2_compose_ok:
                        _decision = choose_intent_aware_fallback(
                            text or "",
                            reason=FALLBACK_REASON_BRAIN_EXCEPTION,
                            store_has_live_agent=False,
                            shipping_info=_ship_info_for_fallback,
                            escalation_evidence_ok=False,
                        )
                        _trace.fallback_source = _decision.kind
                        _trace.response_goal = _decision.response_goal
                        try:
                            from services.fallback_policy import (  # noqa: PLC0415
                                STAGE_BRAIN_EXCEPTION as _STG_BRAIN_EXC,
                                emit_temp_error_fallback_log as _emit_temp_err,
                            )

                            _emit_temp_err(
                                tenant_id=tenant_id,
                                conversation_id=getattr(convo, "id", None),
                                sender=to or "",
                                inbound_msg_id=str(wa_msg_id or ""),
                                msg_type=str(getattr(_trace, "msg_type", "") or "text"),
                                intent=str(getattr(_trace, "intent", "") or ""),
                                stage=_STG_BRAIN_EXC,
                                exception=brain_exc,
                                fallback_kind=str(_decision.kind),
                                response_goal=str(_decision.response_goal),
                            )
                        except Exception:  # noqa: silent-ok — fallback telemetry must not block reply
                            pass
                        if _decision.kind == FALLBACK_KIND_SOFT_RETRY:
                            _trace.clarification_triggered = True
                            _trace.clarification_reason = (
                                "no_confident_intent" if not _trace.top_intents
                                else f"top_conf_{_trace.intent_confidence:.2f}_below_threshold"
                            )
                        elif _decision.kind == FALLBACK_KIND_INTENT_DETERMINISTIC:
                            _trace.clarification_triggered = False
                            _trace.clarification_reason = "suppressed_by_confident_intent"
                        logger.info(
                            "[FALLBACK_POLICY] tenant=%s to=%s kind=%s goal=%s rationale=%s",
                            tenant_id, to, _decision.kind, _decision.response_goal,
                            _decision.rationale,
                        )
                        _safe_reply = _decision.text
                        _persona_ownership.mark_bypass(
                            _POReason.FALLBACK_REPLY,
                            owner=f"brain_exception:{_decision.kind}",
                        )
                    else:
                        _trace.fallback_source = "staff_escalation_compose"
                        _trace.response_goal = "handoff"
                        _persona_ownership.mark_bypass(
                            _POReason.FALLBACK_REPLY,
                            owner="brain_exception:staff_escalation_compose",
                        )
                    if not _trace.outbound_lock_acquired():
                        return
                    _fallback_meta = _persona_ownership.to_metadata()
                    try:
                        from core.outbound_text_policy import OutboundTextTracker  # noqa: PLC0415

                        _fb_tracker = OutboundTextTracker()
                        _fb_kind = (
                            "staff_escalation_compose"
                            if _d2_compose_ok
                            else str(getattr(_decision, "kind", "") or "")
                        )
                        _fb_tracker.mark_fallback(
                            reason=FALLBACK_REASON_BRAIN_EXCEPTION,
                            kind=_fb_kind,
                            intent=str(getattr(_trace, "intent", "") or ""),
                            decision_action=str(
                                getattr(_trace, "decision_action", "") or ""
                            ),
                        )
                        _fallback_meta = {
                            **_fallback_meta,
                            "outbound_text_policy": _fb_tracker.to_metadata(),
                        }
                    except Exception:  # noqa: silent-ok — fallback metadata must not block send
                        pass
                    StateManager.save_message(
                        db, to, _safe_reply, "outbound",
                        conversation_id=convo.id, tenant_id=tenant_id,
                        extra_metadata=_fallback_meta,
                    )
                    try:
                        await _send_whatsapp_message(
                            phone_id=phone_id, to=to, text=_safe_reply,
                            _tenant_id=tenant_id, _db=db,
                        )
                    except Exception as _safe_exc:
                        _trace.outbound_error = _safe_exc.__class__.__name__
                        logger.exception(
                            "[Merchant/Brain] safe-reply send failed | tenant=%s to=%s",
                            tenant_id, to,
                        )
                    _trace.mark_outbound_sent(
                        source=_TS.SOURCE_BRAIN_EXCEPTION,
                        length=len(_safe_reply),
                    )
                    if _generic_handoff_signal:
                        try:
                            from modules.ai.brain.execution.staff_escalation_execution import (  # noqa: PLC0415
                                d2_operational_escalation_succeeded as _d2_exc_ok,
                                read_current_turn_d2_result as _d2_read,
                            )

                            _vcard_d2 = _d2_execution_result or _d2_read(db)
                            if _d2_exc_ok(_vcard_d2):
                                await _maybe_deliver_generic_handoff_vcard(
                                    phone_id=phone_id,
                                    to=to,
                                    tenant_id=tenant_id,
                                    db=db,
                                    message=text or "",
                                    d2_result=_vcard_d2,
                                )
                        except Exception as _gh_vcard_exc:  # noqa: BLE001
                            logger.warning(
                                "[Merchant/HANDOFF_GUARD] post-D2 vCard failed tenant=%s err=%s",
                                tenant_id,
                                type(_gh_vcard_exc).__name__,
                            )
                    return
            elif _turn_eval.status == "evaluated":
                _turn_eval_applied = True
                reply = _turn_eval.reply_text
                _brain_reply_candidate = _turn_eval.brain_reply_candidate
                _outbound_customer_id = _turn_eval.outbound_customer_id
                _brain_buttons = list(_turn_eval.brain_buttons or [])
                _brain_product_cards = list(
                    getattr(_turn_eval, "brain_product_cards", None) or []
                )
                _native_catalog_entry = dict(_turn_eval.native_catalog_entry or {})
                _brain_handoff = bool(_turn_eval.brain_handoff)
                _relational_moment = _turn_eval.relational_moment
                _brain_nc_block = _turn_eval.brain_nc_block
                _brain_nc_category = _turn_eval.brain_nc_category
                _br_action = _turn_eval.br_action
                _br_dec_action = _turn_eval.br_dec_action
                _br_dec_args = dict(_turn_eval.br_dec_args or {})
                if _generic_handoff_signal:
                    from modules.ai.brain.execution.staff_escalation_execution import (  # noqa: PLC0415
                        action_handoff_already_executed as _d2_already,
                        compose_staff_escalation_with_verifier as _d2_compose,
                        execute_staff_escalation_for_safety_signal as _d2_safety,
                        read_current_turn_d2_result as _d2_read,
                    )
                    if _d2_execution_result is None:
                        _d2_execution_result = _d2_read(db)
                    if not _d2_already(
                        brain_handoff=_brain_handoff,
                        decision_action=str(_br_dec_action or ""),
                    ):
                        try:
                            _d2_miss = await _d2_safety(
                                db=db,
                                tenant_id=int(tenant_id),
                                customer_phone=to,
                                message=text or "",
                                convo=convo,
                            )
                            _d2_execution_result = _d2_miss
                            if _d2_miss.success:
                                reply = await _d2_compose(
                                    db=db,
                                    tenant_id=int(tenant_id),
                                    customer_phone=to,
                                    message=text or "",
                                    result=_d2_miss,
                                    conversation_id=getattr(convo, "id", None),
                                )
                                _brain_handoff = True
                                logger.info(
                                    "[Merchant/HANDOFF_GUARD] semantic-miss D2 fallback "
                                    "tenant=%s session=%s",
                                    tenant_id,
                                    (_d2_miss.data or {}).get("handoff_session_id"),
                                )
                        except Exception as _d2_miss_exc:  # noqa: BLE001
                            logger.warning(
                                "[Merchant/HANDOFF_GUARD] semantic-miss D2 fallback "
                                "failed tenant=%s err=%s",
                                tenant_id,
                                type(_d2_miss_exc).__name__,
                            )
                _outbound_abort_suppressor = _turn_eval.outbound_abort_suppressor
                _brain_persona_compose_event = _turn_eval.brain_persona_compose_event
                _outbound_text_tracker = _turn_eval.outbound_text_tracker
                brain_result = _turn_eval.brain_result
                MERCHANT_BRAIN_ENABLED_FALLBACK = _turn_eval.merchant_brain_enabled_fallback
                # Billing lifecycle source contract marker: billing_access_denied
                if _turn_eval.billing_denied:
                    _trace.fallback_source = _TS.SOURCE_BILLING_DENIED
                    _trace.response_goal = "silent"
                    _trace.reply_source = _TS.SOURCE_BILLING_DENIED
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
            _persona_ownership.mark_bypass(
                _POReason.LEGACY_ROUTE,
                owner="legacy_generate_ai_reply",
            )
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
                reply = payload.reply_text.strip() or _empty_reply_fallback()

        # ── Outbound dedup guard (May 2026 #34 — tiered) ─────────────────
        # Last-mile safety net for BOTH paths (Brain + legacy). The
        # previous one-tier guard replaced ANY reply with ≥60% overlap,
        # which fired on perfectly legitimate re-asks (voice notes,
        # delayed re-engagement, customers asking the same question
        # twice to confirm). The merchant's v2 spec: "dedup لا يمنع
        # الإجابة، ولا يستبدل الرد بجملة canned — بل يخفّف التكرار
        # الحرفي فقط." See ``_is_repeat_reply`` + ``_max_outbound_overlap``
        # for the heuristic.
        #
        # Tier behaviour:
        #   * overlap < 60%    → no signal, pass through silently
        #   * 60% ≤ ovl < 85%  → SOFT: log only, pass through (LLM
        #                        is repeating a TOPIC, not the words)
        #   * overlap ≥ 85%    → HARD candidate. Replace IFF the reply
        #                        does NOT carry a new signal (URL /
        #                        phone / [MEDIA…] / [PRODUCT:] / [CALL:]).
        #                        Asset-bearing replies always pass —
        #                        the asset itself is the new content.
        #
        # Skipped entirely for empty replies (handled upstream) and
        # for the handoff path (_brain_handoff replies are
        # intentionally distinct).
        if reply and not _brain_handoff and not _turn_eval_applied:
            _po_reply_before_dedup = reply
            _overlap = _max_outbound_overlap(reply, history)
            _is_hard = _overlap >= _DEDUP_HARD_OVERLAP_THRESHOLD
            _is_soft = (
                _overlap >= _DEDUP_OVERLAP_THRESHOLD and not _is_hard
            )
            _carries_signal = _reply_carries_new_signal(reply)
            try:
                from modules.ai.brain.commerce.product_visual import (  # noqa: PLC0415
                    is_product_visual_request as _is_visual_inbound,
                )
                _visual_inbound = _is_visual_inbound(text or "")
            except Exception:  # noqa: BLE001
                _visual_inbound = False

            if _is_hard and not _carries_signal and not _visual_inbound:
                _skip_dedup_substitution = False
                try:
                    from modules.ai.brain.commerce.fallback_guard import (  # noqa: PLC0415
                        detect_hard_topic_shift,
                    )
                    if detect_hard_topic_shift(text or "", history=history):
                        _skip_dedup_substitution = True
                        logger.info(
                            "[CHAT_DEDUP] tenant=%s to=%s tier=hard "
                            "overlap=%.2f hard_topic_shift=true — "
                            "pass-through (no canned repeat substitution)",
                            tenant_id, to, _overlap,
                        )
                except Exception:  # noqa: BLE001
                    _skip_dedup_substitution = False

                if not _skip_dedup_substitution:
                    try:
                        from modules.ai.brain.commerce.dedup_operational_delta import (  # noqa: PLC0415
                            has_operational_delta_since_last_reply,
                            last_outbound_body as _dedup_last_outbound_body,
                        )
                        if has_operational_delta_since_last_reply(
                            text or "",
                            _po_reply_before_dedup,
                            _dedup_last_outbound_body(history),
                            history=history,
                        ):
                            _skip_dedup_substitution = True
                            logger.info(
                                "[CHAT_DEDUP] tenant=%s to=%s tier=hard "
                                "overlap=%.2f operational_delta=true — "
                                "pass-through (customer added new order detail)",
                                tenant_id, to, _overlap,
                            )
                    except Exception:  # noqa: BLE001
                        pass

                    if not _skip_dedup_substitution:
                        try:
                            from modules.ai.brain.commerce.dedup_operational_delta import (  # noqa: PLC0415
                                should_bypass_hard_dedup_repeat_availability,
                                last_outbound_body as _dedup_last_outbound_body,
                            )
                            if should_bypass_hard_dedup_repeat_availability(
                                text or "",
                                _dedup_last_outbound_body(history),
                            ):
                                _skip_dedup_substitution = True
                                logger.info(
                                    "[CHAT_DEDUP] tenant=%s to=%s tier=hard "
                                    "overlap=%.2f repeat_availability_after_guard=true — "
                                    "pass-through (prior guard rewrite was unhelpful)",
                                    tenant_id, to, _overlap,
                                )
                        except Exception:  # noqa: BLE001
                            pass

                if _skip_dedup_substitution:
                    pass
                else:
                    # ── Wave 3 (May 2026): Relational/seasonal-aware
                    # dedup suppression gate. Eid-season audit on
                    # Tenant 33 found this branch was substituting
                    # natural Brain replies to religious / seasonal
                    # greetings ("بارك الله فيك", "كل عام مبارك") with
                    # the canned "هذي نفس الإجابة قبل قليل — إيش
                    # الناقص؟" line. The gate is a pure function gated
                    # by ``RELATIONAL_DEDUP_SUPPRESSION_ENABLED``
                    # (default OFF). When ON and the inbound is in a
                    # relational moment / matches a religious or
                    # seasonal marker phrase, suppression fires and we
                    # leave the Brain's reply untouched. When OFF —
                    # or when the gate fails for any reason — the
                    # legacy substitution path below runs unchanged.
                    _w3_suppressed = False
                    try:
                        from modules.ai.brain.relational import (  # noqa: PLC0415
                            log_dedup_suppression as _cx_log_dedup_suppression,
                            should_suppress_dedup_substitution as _cx_should_suppress_dedup,
                        )
                        _w3_decision = _cx_should_suppress_dedup(
                            inbound_text=text,
                            relational_moment=_relational_moment or None,
                            overlap=_overlap,
                        )
                        _cx_log_dedup_suppression(
                            decision=_w3_decision,
                            tenant_id=tenant_id,
                            conversation_id=getattr(convo, "id", None),
                            overlap=_overlap,
                            would_have_replaced=True,
                        )
                        if _w3_decision.suppress:
                            _w3_suppressed = True
                    except Exception as _w3_exc:  # noqa: BLE001
                        logger.debug(
                            "[CX] dedup_suppression gate failed; falling "
                            "back to legacy substitution: %s",
                            _w3_exc,
                        )
                        _w3_suppressed = False

                    if _w3_suppressed:
                        # Brain reply passes through unchanged. Emit a
                        # ``[CHAT_DEDUP]`` line with the relational tag
                        # so existing operator dashboards still surface
                        # the high-overlap event for cross-checking.
                        logger.info(
                            "[CHAT_DEDUP] tenant=%s to=%s tier=hard "
                            "overlap=%.2f relational_suppressed=true "
                            "moment=%s reason=%s reply_len=%d brain=%s",
                            tenant_id, to, _overlap,
                            getattr(_w3_decision, "moment_token", "") or "",
                            getattr(_w3_decision, "reason", "") or "",
                            len(reply), _brain_active,
                        )
                    else:
                        _orig_len = len(reply)
                        _meta_for_dedup = (
                            dict(inbound_metadata or {})
                            if isinstance(inbound_metadata, dict)
                            else None
                        )
                        _norm_type = str(
                            (inbound_metadata or {}).get("source_type")
                            or (inbound_metadata or {}).get("normalized_type")
                            or ""
                        ) or None
                        reply = _dedup_operational_substitute(
                            db,
                            tenant_id=tenant_id,
                            phone=to,
                            history=history,
                            inbound_text=text,
                            inbound_metadata=_meta_for_dedup,
                            normalized_type=_norm_type,
                            decision_action=str(_br_dec_action or ""),
                            decision_args=dict(_br_dec_args or {}),
                        )
                        if not (reply or "").strip():
                            logger.info(
                                "[CHAT_DEDUP] tenant=%s to=%s tier=hard overlap=%.2f "
                                "suppressed=true personality_substitute_blocked "
                                "(orig_len=%d brain=%s)",
                                tenant_id, to, _overlap,
                                _orig_len, _brain_active,
                            )
                            _outbound_abort_suppressor = "chat_dedup_hard"
                            try:
                                from core.order_status_dedup_reply import (  # noqa: PLC0415
                                    build_dedup_local_order_short_reply,
                                )
                                from modules.ai.brain.commerce.dedup_operational_delta import (  # noqa: PLC0415
                                    last_outbound_body as _dedup_last_outbound_body,
                                    should_restore_brain_reply_after_dedup_silence,
                                )

                                _dedup_prev_outbound = _dedup_last_outbound_body(
                                    history,
                                )
                                _order_status_alt = (
                                    build_dedup_local_order_short_reply(
                                        db,
                                        tenant_id=tenant_id,
                                        phone=to,
                                        conversation_id=(
                                            getattr(convo, "id", None)
                                            if convo is not None
                                            else None
                                        ),
                                        inbound_text=text or "",
                                        previous_outbound=_dedup_prev_outbound,
                                    )
                                )
                                if _order_status_alt:
                                    reply = _order_status_alt
                                    _outbound_abort_suppressor = None
                                    logger.info(
                                        "[CHAT_DEDUP] tenant=%s to=%s tier=hard "
                                        "order_status_dedup_alt=true reply_len=%d",
                                        tenant_id,
                                        to,
                                        len(reply or ""),
                                    )
                                elif should_restore_brain_reply_after_dedup_silence(
                                    current_inbound=text or "",
                                    candidate_reply=_po_reply_before_dedup,
                                    previous_outbound=_dedup_prev_outbound,
                                    intent_name=str(
                                        (_meta_for_dedup or {}).get("intent")
                                        or (_meta_for_dedup or {}).get("intent_name")
                                        or ""
                                    ),
                                ):
                                    reply = _po_reply_before_dedup
                                    _outbound_abort_suppressor = None
                                    logger.info(
                                        "[CHAT_DEDUP] tenant=%s to=%s tier=hard "
                                        "commerce_inquiry_silence_blocked=true "
                                        "restored_brain_candidate=true reply_len=%d",
                                        tenant_id,
                                        to,
                                        len(reply or ""),
                                    )
                                else:
                                    reply = ""
                            except Exception as _dedup_restore_exc:  # noqa: BLE001  # noqa: silent-ok — optional commerce restore probe
                                logger.debug(
                                    "[CHAT_DEDUP] commerce inquiry restore probe failed: %s",
                                    _dedup_restore_exc,
                                )
                                reply = ""
                        else:
                            _persona_ownership.on_text_replaced(
                                layer="dedup_substitution",
                                reason=_POReason.DEDUP_REPLY,
                                before=_po_reply_before_dedup,
                                after=reply,
                            )
                            if isinstance(_brain_persona_compose_event, dict):
                                try:
                                    from modules.ai.brain.persona.trusted_coupon_offer_provenance import (  # noqa: PLC0415
                                        note_trusted_coupon_offer_dedup_substitution,
                                    )
                                    from modules.ai.brain.persona.customer_conditional_coupon_provenance import (  # noqa: PLC0415
                                        note_customer_conditional_coupon_dedup_substitution,
                                    )
                                    from modules.ai.brain.persona.product_sale_offer_provenance import (  # noqa: PLC0415
                                        note_product_sale_offer_dedup_substitution,
                                    )
                                    from modules.ai.brain.persona.track_order_need_identifiers_provenance import (  # noqa: PLC0415
                                        note_track_order_need_identifiers_dedup_substitution,
                                    )

                                    note_trusted_coupon_offer_dedup_substitution(
                                        _brain_persona_compose_event,
                                        before=_po_reply_before_dedup,
                                        after=reply,
                                    )
                                    note_customer_conditional_coupon_dedup_substitution(
                                        _brain_persona_compose_event,
                                        before=_po_reply_before_dedup,
                                        after=reply,
                                    )
                                    note_product_sale_offer_dedup_substitution(
                                        _brain_persona_compose_event,
                                        before=_po_reply_before_dedup,
                                        after=reply,
                                    )
                                    note_track_order_need_identifiers_dedup_substitution(
                                        _brain_persona_compose_event,
                                        before=_po_reply_before_dedup,
                                        after=reply,
                                    )
                                except Exception:  # noqa: BLE001  # noqa: silent-ok — provenance must not block send
                                    pass
                            logger.info(
                                "[CHAT_DEDUP] tenant=%s to=%s tier=hard overlap=%.2f "
                                "replaced near-duplicate outbound "
                                "(orig_len=%d new_len=%d brain=%s)",
                                tenant_id, to, _overlap,
                                _orig_len, len(reply), _brain_active,
                            )
            elif _is_hard and _carries_signal:
                # Near-verbatim wording, BUT the reply ships a URL /
                # phone / asset marker. The customer asked again
                # (probably for that exact asset) — give it to them.
                logger.info(
                    "[CHAT_DEDUP_BYPASS_ASSET] tenant=%s to=%s "
                    "overlap=%.2f reply_len=%d brain=%s — pass-through "
                    "(reply carries url/phone/marker)",
                    tenant_id, to, _overlap, len(reply), _brain_active,
                )
            elif _is_soft:
                # 60–85% overlap. LLM is repeating the TOPIC but with
                # its own wording. Trust the LLM — replacing here is
                # what made the bot feel cold/canned. Telemetry only.
                logger.info(
                    "[CHAT_DEDUP_SOFT] tenant=%s to=%s overlap=%.2f "
                    "carries_signal=%s reply_len=%d brain=%s — "
                    "pass-through",
                    tenant_id, to, _overlap,
                    str(_carries_signal).lower(),
                    len(reply), _brain_active,
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
        # ── Post-compose truth guards (P1-B consolidated pipeline) ────────
        if reply:
            from modules.ai.brain.postprocess.post_compose_guard_pipeline import (  # noqa: PLC0415
                run_post_compose_truth_guards,
            )

            def _webhook_staff_ack_emit(**_ack_kwargs) -> None:
                try:
                    from modules.ai.brain.observability.order_flow_evidence import (  # noqa: PLC0415
                        detect_input_types,
                        emit_ack_decision,
                        is_generic_ack_stub,
                        reply_acknowledges_important_input,
                    )
                    from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
                        has_active_commerce_from_state,
                    )

                    _setg_meta = _ack_kwargs.get("inbound_metadata") or {}
                    _setg_bs = _ack_kwargs.get("brain_state") or {}
                    _setg_path = str(_ack_kwargs.get("chosen_path") or "")
                    _ack_reply = str(_ack_kwargs.get("reply") or "")
                    _wh_types = detect_input_types(
                        message=text or "",
                        inbound_metadata=_setg_meta,
                    )
                    _wh_important = bool(_wh_types)
                    _wh_ack = reply_acknowledges_important_input(
                        reply=_ack_reply,
                        input_types=_wh_types,
                        state=_setg_bs,
                    )
                    _wh_stub = is_generic_ack_stub(_ack_reply)
                    emit_ack_decision(
                        tenant_id=tenant_id,
                        phone_tail=(to or "")[-4:],
                        important_customer_input=_wh_important,
                        input_types=_wh_types,
                        acknowledged=_wh_ack,
                        reason=(
                            "webhook_belt_generic_ack_violation"
                            if _wh_important and _wh_stub and not _wh_ack
                            else "webhook_belt"
                        ),
                        outbound_preview=_ack_reply,
                        decision_action=str(_br_action or ""),
                        chosen_path=_setg_path,
                        generic_ack_stub=_wh_stub,
                        generic_ack_violation=bool(
                            _wh_important and _wh_stub and not _wh_ack
                        ),
                        staff_route_detected=False,
                        fulfillment_locked=has_active_commerce_from_state(_setg_bs),
                    )
                except Exception as _wh_ack_exc:  # noqa: BLE001  # noqa: silent-ok — evidence emit must not block send
                    logger.debug(
                        "[ACK_DECISION] webhook emit skipped tenant=%s err=%s",
                        tenant_id,
                        _wh_ack_exc,
                    )

            _pc_primary_already = False
            if _turn_eval_applied:
                _pc_primary_already = bool(
                    getattr(_turn_eval, "post_compose_primary_applied", False)
                )
            _pc_mode = "last_line" if _turn_eval_applied else "primary"
            _pc_result = run_post_compose_truth_guards(
                db=db,
                tenant_id=tenant_id,
                to=to,
                text=text,
                reply=reply,
                convo=convo,
                inbound_metadata=inbound_metadata,
                brain_handoff=bool(_brain_handoff),
                brain_nc_block=_brain_nc_block,
                brain_nc_category=_brain_nc_category,
                br_action=_br_action,
                brain_persona_compose_event=_brain_persona_compose_event,
                mode=_pc_mode,
                primary_already_applied=_pc_primary_already,
                persona_ownership=_persona_ownership,
                conversation_id=getattr(convo, "id", None),
                on_staff_guard_complete=_webhook_staff_ack_emit,
            )
            reply = _pc_result.reply

        if reply and not _brain_handoff:
            _po_reply_before_spg = reply
            try:
                from modules.ai.brain.postprocess.social_phrase_quality_guard import (  # noqa: PLC0415
                    apply_social_phrase_quality_guard,
                )
                _spg = apply_social_phrase_quality_guard(
                    reply,
                    inbound_text=text or "",
                    tenant_id=tenant_id,
                )
                if _spg.stripped:
                    reply = _spg.reply
                    _persona_ownership.on_text_replaced(
                        layer="social_phrase_quality_guard",
                        reason=_POReason.FALLBACK_REPLY,
                        before=_po_reply_before_spg,
                        after=reply,
                    )
            except Exception as _spg_exc:  # noqa: BLE001  # noqa: silent-ok — belt guard best-effort
                logger.debug(
                    "[SOCIAL_PHRASE_QUALITY_GUARD] webhook hook failed tenant=%s err=%s",
                    tenant_id, _spg_exc,
                )

        if reply and not _brain_handoff and not _brain_active:
            try:
                from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: PLC0415
                    apply_product_availability_truth_guard,
                    product_availability_guard_mode,
                )
                if product_availability_guard_mode() != "off":
                    from core.order_flow import (  # noqa: PLC0415
                        _load_brain_state as _pavg_load,
                    )
                    from modules.ai.brain.postprocess.availability_context_builder import (  # noqa: PLC0415
                        build_availability_context,
                    )
                    _, _pavg_bs = _pavg_load(db, tenant_id=tenant_id, phone=to)
                    _pavg_rec_ids: list = []
                    for _rec in (_pavg_bs.get("last_recommended_products") or [])[:5]:
                        _rid = (_rec or {}).get("id") if isinstance(_rec, dict) else None
                        if isinstance(_rid, int):
                            _pavg_rec_ids.append(_rid)
                    _pavg_focus = _pavg_bs.get("current_product_focus")
                    try:
                        from modules.ai.brain.commerce.product_breadth_policy import (  # noqa: PLC0415
                            global_availability_browse_requested as _pavg_global_browse,
                        )
                        from modules.ai.brain.postprocess.availability_guard_policy import (  # noqa: PLC0415
                            browse_alternatives_requested as _pavg_browse_alt,
                        )

                        if _pavg_global_browse(text or "") or _pavg_browse_alt(text or ""):
                            _pavg_focus = None
                    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional browse defocus imports
                        pass
                    _pavg_ctx = build_availability_context(
                        db,
                        tenant_id,
                        focus_product=_pavg_focus,
                        recommended_product_ids=_pavg_rec_ids,
                    )
                    _pavg_path = str(
                        (inbound_metadata or {}).get("deterministic_path") or _br_action or ""
                    )
                    _pavg_result = apply_product_availability_truth_guard(
                        reply=reply,
                        availability_context=_pavg_ctx,
                        inbound_text=text or "",
                        chosen_path=_pavg_path,
                        tenant_id=tenant_id,
                        conversation_id=getattr(convo, "id", None),
                        invocation_site="webhook",
                    )
                    _pavg_replaced = bool(_pavg_result.replaced)
                    if _pavg_replaced:
                        reply = _pavg_result.reply
                    try:
                        from modules.ai.brain.commerce_reply_humanizer import (  # noqa: PLC0415
                            apply_commerce_reply_humanizer,
                            is_operational_availability_fact,
                        )
                        from modules.ai.brain.intent_priority.types import (  # noqa: PLC0415
                            GOAL_PRODUCT_AVAILABILITY,
                        )

                        if _pavg_replaced or is_operational_availability_fact(reply or ""):
                            _wh_product_title = ""
                            try:
                                _wh_focus = (_pavg_bs or {}).get("current_product_focus") or {}
                                if isinstance(_wh_focus, dict):
                                    _wh_product_title = str(
                                        _wh_focus.get("title")
                                        or _wh_focus.get("name")
                                        or ""
                                    ).strip()
                            except Exception:  # noqa: BLE001
                                _wh_product_title = ""
                            _wh_intent = str(
                                (inbound_metadata or {}).get("intent_name") or ""
                            ).strip()
                            _wh_goal = str(
                                (inbound_metadata or {}).get("primary_customer_goal")
                                or GOAL_PRODUCT_AVAILABILITY
                            ).strip()
                            _wh_crh = apply_commerce_reply_humanizer(
                                reply or "",
                                inbound_text=text or "",
                                intent_name=_wh_intent,
                                primary_customer_goal=_wh_goal,
                                locale="ar",
                                chosen_path=_pavg_path,
                                product_title=_wh_product_title,
                                tenant_id=tenant_id,
                                conversation_id=getattr(convo, "id", None),
                                post_guard_rewrite=_pavg_replaced,
                            )
                            if _wh_crh.replaced:
                                reply = _wh_crh.reply
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "[WHATSAPP_WEBHOOK] commerce_reply_humanizer_hook_failed "
                            "tenant=%s",
                            tenant_id,
                        )
            except Exception as _pavg_exc:  # noqa: BLE001
                logger.debug(
                    "[PRODUCT_AVAILABILITY_TRUTH_GUARD] webhook hook failed tenant=%s err=%s",
                    tenant_id, _pavg_exc,
                )

        if reply and _brain_active and not _brain_handoff:
            try:
                from modules.ai.brain.commerce_reply_humanizer import (  # noqa: PLC0415
                    apply_commerce_reply_humanizer,
                    is_operational_availability_fact,
                )
                from modules.ai.brain.intent_priority.types import (  # noqa: PLC0415
                    GOAL_PRODUCT_AVAILABILITY,
                )

                if is_operational_availability_fact(reply or ""):
                    _wh_product_title = ""
                    try:
                        from core.order_flow import _load_brain_state as _wh_bs_load  # noqa: PLC0415

                        _, _wh_bs = _wh_bs_load(db, tenant_id=tenant_id, phone=to)
                        _wh_focus = (_wh_bs or {}).get("current_product_focus") or {}
                        if isinstance(_wh_focus, dict):
                            _wh_product_title = str(
                                _wh_focus.get("title") or _wh_focus.get("name") or ""
                            ).strip()
                    except Exception:  # noqa: BLE001
                        _wh_product_title = ""
                    _wh_crh = apply_commerce_reply_humanizer(
                        reply or "",
                        inbound_text=text or "",
                        intent_name=str(
                            (inbound_metadata or {}).get("intent_name") or "ask_product"
                        ),
                        primary_customer_goal=str(
                            (inbound_metadata or {}).get("primary_customer_goal")
                            or GOAL_PRODUCT_AVAILABILITY
                        ),
                        locale="ar",
                        chosen_path=str(
                            (inbound_metadata or {}).get("deterministic_path")
                            or _br_action
                            or "llm"
                        ),
                        product_title=_wh_product_title,
                        tenant_id=tenant_id,
                        conversation_id=getattr(convo, "id", None),
                        post_guard_rewrite=True,
                    )
                    if _wh_crh.replaced:
                        reply = _wh_crh.reply
            except Exception as _wh_br_crh_exc:  # noqa: BLE001
                logger.debug(
                    "[COMMERCE_REPLY_HUMANIZER] brain-path safety net failed tenant=%s err=%s",
                    tenant_id, _wh_br_crh_exc,
                )

        if (
            reply
            and "_po_reply_before_guards" in locals()
            and (reply or "").strip() != (_po_reply_before_guards or "").strip()
        ):
            _persona_ownership.on_text_replaced(
                layer="webhook_truth_guards",
                reason=_POReason.TRUTH_GUARD_REWRITE,
                before=_po_reply_before_guards,
                after=reply or "",
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
        _loop_guard_provenance: Dict[str, Any] = {}
        if reply and not _brain_handoff:
            try:
                from core.ai_pause_guard import (  # noqa: PLC0415
                    evaluate_loop_pre_send as _eval_loop,
                    note_recovery_sent as _note_recovery,
                    pause_ai as _loop_pause_ai,
                    REASON_BOT_LOOP as _R_LOOP,
                )
                _loop_checkout_active = False
                _loop_checkout_recovery = ""
                try:
                    from core.order_flow import _load_brain_state as _loop_load_bs  # noqa: PLC0415
                    from modules.ai.brain.commerce.checkout_slot_fallback import (  # noqa: PLC0415
                        build_checkout_slot_fallback_reply,
                    )
                    from modules.ai.brain.commerce.checkout_slot_turn_gate import (  # noqa: PLC0415
                        current_turn_allows_checkout_slot_fallback,
                    )
                    from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: PLC0415
                        order_support_reply_protected,
                    )
                    from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
                        has_active_commerce_from_state,
                    )

                    _, _loop_bs = _loop_load_bs(db, tenant_id=tenant_id, phone=to)
                    _loop_checkout_active = has_active_commerce_from_state(_loop_bs)
                    _skip_legacy_loop = False
                    try:
                        from modules.ai.order_flow_v2.flags import (  # noqa: PLC0415
                            should_skip_legacy_order_flow_reply,
                        )

                        _skip_legacy_loop = should_skip_legacy_order_flow_reply()
                    except Exception:  # noqa: BLE001  # noqa: silent-ok — V2 gate must not break loop guard
                        pass
                    _loop_order_support_owned = order_support_reply_protected(
                        decision_action=str(_br_dec_action or ""),
                        decision_args=dict(_br_dec_args or {}),
                    )
                    if _loop_order_support_owned:
                        from core.outbound_text_policy import (  # noqa: PLC0415
                            OutboundTextSource as _LoopGuardOTS,
                        )

                        _pre_loop_src = (
                            _outbound_text_tracker.text_source.value
                            if _outbound_text_tracker is not None
                            else _LoopGuardOTS.UNKNOWN.value
                        )
                        _loop_guard_provenance = {
                            "pre_loop_guard_text_source": _pre_loop_src,
                            "loop_guard_override_applied": False,
                            "loop_guard_override_skipped_reason": "order_support_owned",
                        }
                    elif (
                        _loop_checkout_active
                        and not _skip_legacy_loop
                        and current_turn_allows_checkout_slot_fallback(
                            str(_br_dec_action or "")
                        )
                    ):
                        _loop_checkout_recovery = (
                            build_checkout_slot_fallback_reply(
                                state=_loop_bs,
                                inbound_text=text or "",
                            )
                            or ""
                        )
                except Exception:  # noqa: BLE001  # noqa: silent-ok — loop checkout brain-state load is best-effort
                    logger.exception(
                        "[Merchant/LOOP] checkout brain-state load skipped tenant=%s",
                        tenant_id,
                    )
                _decision = _eval_loop(
                    db, convo,
                    tenant_id=tenant_id,
                    candidate_reply=reply,
                    inbound_text=text,
                    checkout_active=_loop_checkout_active,
                    checkout_recovery_reply=_loop_checkout_recovery or None,
                )
                if _decision.action == "pause":
                    # ── Promote loop-pause to REAL handoff (May 2026 P1) ────
                    # Pre-fix, this branch only flipped ``ai_paused`` under
                    # REASON_BOT_LOOP and then sent a text claiming the
                    # conversation would be transferred to a human. None
                    # of the canonical handoff flags
                    # (``status="human"``, ``is_human_handoff``,
                    # ``needs_human``, ``handoff_active``) were raised, so
                    # the conversation never showed up in the dashboard's
                    # "طلب موظف" inbox and the AI silently resumed on the
                    # next inbound. The fix below mirrors the post-brain
                    # ACTION_HANDOFF branch (~line 5078): flip every
                    # canonical flag, open a HandoffSession row, and pause
                    # the AI under REASON_HUMAN_HANDOFF — so the outbound
                    # text promise is now backed by real state.
                    from datetime import datetime as _dt, timezone as _tz  # noqa: PLC0415
                    try:
                        from core.ai_pause_guard import (  # noqa: PLC0415
                            REASON_HUMAN_HANDOFF as _R_HOFF,
                        )
                    except Exception:  # noqa: BLE001
                        _R_HOFF = "human_handoff"

                    try:
                        convo.status = "human"
                        convo.is_human_handoff = True
                        convo.needs_human = True
                        convo.handoff_active = True
                        if not getattr(convo, "taken_over_at", None):
                            convo.taken_over_at = _dt.now(_tz.utc)
                        if not getattr(convo, "taken_over_by", None):
                            convo.taken_over_by = "system:loop_pause"
                        db.flush()
                    except Exception as _flag_exc:  # noqa: BLE001
                        logger.warning(
                            "[Merchant/LOOP_HANDOFF] flag-flip failed tenant=%s "
                            "convo=%s err=%s",
                            tenant_id, getattr(convo, "id", None), _flag_exc,
                        )

                    try:
                        from handoff.manager import create_handoff_session  # noqa: PLC0415
                        _cust_name = ""
                        try:
                            _cust_name = (getattr(convo, "customer", None)
                                          and (getattr(convo.customer, "name", "") or "")) or ""
                        except Exception:
                            _cust_name = ""
                        create_handoff_session(
                            db, tenant_id, to, _cust_name, text or "",
                            reason="bot_loop_handoff",
                        )
                    except Exception as _hs_exc:  # noqa: BLE001
                        logger.warning(
                            "[Merchant/LOOP_HANDOFF] HandoffSession create failed "
                            "tenant=%s to=%s err=%s",
                            tenant_id, to, _hs_exc,
                        )

                    # Pause AI under HUMAN_HANDOFF (not BOT_LOOP) so the
                    # ai_pause_guard / dashboard correctly classify why
                    # the AI stopped.
                    _loop_pause_ai(db, convo, reason=_R_HOFF, by="system:loop_pause")

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
                        "[OUTBOUND] tenant=%s to=%s source=loop_guard_handoff trigger=inbound "
                        "intent=bot_loop_detected handoff_triggered=true handoff_state=flags_set "
                        "loop_score=%d similarity=%.2f reply_len=%d",
                        tenant_id, to, _decision.score, _decision.similarity, len(_handoff_text),
                    )
                    return
                if _decision.action == "recovery" and _decision.recovery_text:
                    _po_before_loop = reply
                    reply = _decision.recovery_text
                    _persona_ownership.on_text_replaced(
                        layer="loop_guard_recovery",
                        reason=_POReason.FALLBACK_REPLY,
                        before=_po_before_loop or "",
                        after=reply,
                    )
                    _loop_replaced_with_recovery = True
                    _note_recovery(int(tenant_id), int(convo.id), recovery_text=reply)
                    logger.info(
                        "[OUTBOUND_PRE_SEND] tenant=%s to=%s replaced reply with recovery line "
                        "loop_score=%d similarity=%.2f",
                        tenant_id, to, _decision.score, _decision.similarity,
                    )
            except Exception as _loop_exc:
                logger.debug("[loop_guard] evaluate failed (open): %s", _loop_exc)

        # Save outbound reply after generation (skip empty — P0 wire suppress).
        try:
            from core.native_catalog_fallback import defer_native_catalog_customer_reply  # noqa: PLC0415

            reply = defer_native_catalog_customer_reply(
                reply,
                native_catalog_entry=_native_catalog_entry,
            )
            if not (_brain_reply_candidate or "").strip():
                _brain_reply_candidate = (reply or "").strip()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — defer must not block persist gate
            pass
        if (
            _should_suppress_empty_outbound_reply(reply, brain_buttons=_brain_buttons)
            and not _native_catalog_entry.get("thumbnail_product_retailer_id")
        ):
            if not _outbound_abort_audited:
                _maybe_log_outbound_candidate_abort(
                    tenant_id=tenant_id,
                    conversation_id=getattr(convo, "id", None),
                    customer_id=_outbound_customer_id,
                    brain_candidate=_brain_reply_candidate,
                    final_reply=reply,
                    abort_reason="skip_persist",
                    final_stage="pre_persist",
                    suppressor=_outbound_abort_suppressor or None,
                    expression_owner=_persona_ownership.expression_owner,
                )
                _outbound_abort_audited = True
            _log_empty_outbound_suppressed(
                tenant_id=tenant_id,
                to=to,
                conversation_id=getattr(convo, "id", None),
                reason="skip_persist",
            )
        else:
            try:
                import time as _time_outp  # noqa: PLC0415
                from core.turn_latency import safe_flush_post_brain_dispatch  # noqa: PLC0415

                safe_flush_post_brain_dispatch()
                _t_outp = _time_outp.monotonic()
            except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                _t_outp = None
            _outbound_event_id = StateManager.save_message(
                db, to, reply, "outbound",
                conversation_id=convo.id, tenant_id=tenant_id,
                extra_metadata=_otp_merge_save_metadata(
                    _outbound_text_tracker,
                    {
                        **_persona_ownership.to_metadata(),
                        **_loop_guard_provenance,
                    },
                    persona_compose_event=(
                        _payment_persona_compose_event or _brain_persona_compose_event
                    ),
                    brain_result=brain_result if isinstance(brain_result, dict) else None,
                ),
            )
            try:
                if _t_outp is not None:
                    import time as _time_outp2  # noqa: PLC0415
                    from core.turn_latency import safe_record_ms  # noqa: PLC0415

                    safe_record_ms(
                        "outbound_persist",
                        (_time_outp2.monotonic() - _t_outp) * 1000.0,
                    )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                pass

        from core.outbound_wire_audit import bind_wire_audit  # noqa: PLC0415

        _wire_audit_token = bind_wire_audit(
            db, _outbound_event_id, tenant_id, to, reply or "",
            _otp_merge_save_metadata(
                _outbound_text_tracker, _persona_ownership.to_metadata(),
                persona_compose_event=_payment_persona_compose_event or _brain_persona_compose_event,
                brain_result=brain_result if isinstance(brain_result, dict) else None,
            ),
        )

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
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
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
                import time as _time_rn  # noqa: PLC0415

                _t_reply_norm = _time_rn.monotonic()
            except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                _t_reply_norm = None
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
            try:
                if _t_reply_norm is not None:
                    import time as _time_rn2  # noqa: PLC0415
                    from core.turn_latency import safe_record_ms  # noqa: PLC0415

                    safe_record_ms(
                        "reply_normalization",
                        (_time_rn2.monotonic() - _t_reply_norm) * 1000.0,
                    )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                pass

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
                    try:
                        from modules.ai.brain.commerce.customer_origin_intent import (  # noqa: PLC0415
                            customer_origin_has_payment_request,
                            filter_payment_media_attachments,
                            split_inbound_text,
                        )
                        _in_meta_legacy = {}
                        if isinstance(inbound_metadata, dict):
                            _in_meta_legacy = dict(inbound_metadata)
                        try:
                            _in_meta_legacy.update(dict(_live_in_meta or {}))
                        except Exception:  # noqa: BLE001
                            pass
                        _split_legacy = split_inbound_text(
                            text or "",
                            inbound_metadata=_in_meta_legacy,
                            normalized_type=str(
                                _in_meta_legacy.get("normalized_type")
                                or _in_meta_legacy.get("source_type")
                                or ""
                            ) or None,
                        )
                        _allow_payment_legacy = customer_origin_has_payment_request(
                            _split_legacy.customer_origin,
                            inbound_metadata=_in_meta_legacy,
                            normalized_type=_split_legacy.normalized_type or None,
                        )
                        _before_legacy = len(_media_attachments)
                        _media_attachments = filter_payment_media_attachments(
                            _media_attachments,
                            allow_payment=_allow_payment_legacy,
                        )
                        if _before_legacy != len(_media_attachments):
                            logger.info(
                                "[CUSTOMER_ORIGIN_INTENT] tenant=%s "
                                "conversation_id=%s route=legacy_media_id "
                                "filtered_payment_attachments=%d allow=%s",
                                tenant_id,
                                getattr(convo, "id", None),
                                _before_legacy - len(_media_attachments),
                                "true" if _allow_payment_legacy else "false",
                            )
                    except Exception:  # noqa: BLE001
                        pass
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
                try:
                    from modules.ai.brain.commerce.customer_origin_intent import (  # noqa: PLC0415
                        customer_origin_has_payment_request,
                        emit_payment_intent_telemetry,
                        filter_payment_media_attachments,
                        is_payment_media_key,
                        split_inbound_text,
                    )
                    _in_meta_mk = {}
                    if isinstance(inbound_metadata, dict):
                        _in_meta_mk = dict(inbound_metadata)
                    try:
                        _in_meta_mk.update(dict(_live_in_meta or {}))
                    except Exception:  # noqa: BLE001
                        pass
                    _split_mk = split_inbound_text(
                        text or "",
                        inbound_metadata=_in_meta_mk,
                        normalized_type=str(
                            _in_meta_mk.get("normalized_type")
                            or _in_meta_mk.get("source_type")
                            or ""
                        ) or None,
                    )
                    _allow_payment_keys = customer_origin_has_payment_request(
                        _split_mk.customer_origin,
                        inbound_metadata=_in_meta_mk,
                        normalized_type=_split_mk.normalized_type or None,
                    )
                    _keys_before = len(_key_attachments)
                    _key_attachments = filter_payment_media_attachments(
                        _key_attachments,
                        allow_payment=_allow_payment_keys,
                    )
                    if not _allow_payment_keys:
                        _missing_media_keys = [
                            k for k in (_missing_media_keys or [])
                            if not is_payment_media_key(k)
                        ]
                    if _keys_before != len(_key_attachments):
                        emit_payment_intent_telemetry(
                            tenant_id=tenant_id,
                            route="media_key_marker_extract",
                            split=_split_mk,
                            allow_outbound=_allow_payment_keys,
                            reason=(
                                "ok" if _allow_payment_keys
                                else "no_customer_origin_payment_intent"
                            ),
                            conversation_id=getattr(convo, "id", None),
                        )
                except Exception as _mk_gate_exc:  # noqa: BLE001
                    logger.debug(
                        "[CUSTOMER_ORIGIN_INTENT] media_key gate failed tenant=%s err=%s",
                        tenant_id, _mk_gate_exc,
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

        # ── Non-commerce safety gate (May 2026) ─────────────────────
        # Suppress product markers, safety nets, visual enforcement,
        # and catalog sends when inbound media/text is social/religious.
        _commerce_blocked = False
        _fulfillment_discovery_blocked = False
        _positive_commerce = False
        _catalog_card_limit = 2
        _bs_for_nc: dict = {}
        _intent_for_nc = ""
        _allow_product_cards = False
        _dispatch_guard_reason = "pending"
        try:
            from modules.ai.brain.intent.non_commerce_classifier import (  # noqa: PLC0415
                has_positive_commerce_intent,
                resolve_commerce_block,
            )
            from modules.ai.brain.commerce.product_breadth_policy import (  # noqa: PLC0415
                resolve_breadth_for_inbound,
            )
            from modules.ai.brain.order_context_gate import (  # noqa: PLC0415
                should_suppress_product_escalation,
            )
            from modules.ai.brain.product_discovery_gate import (  # noqa: PLC0415
                should_suppress_recommendation_escalation,
            )
            _bs_for_nc = ((convo.extra_metadata or {}).get("brain_state") or {})
            _intent_for_nc = str(_bs_for_nc.get("last_intent") or "")
            _nc_turn = resolve_commerce_block(
                text or "",
                inbound_metadata=inbound_metadata,
                intent_name=_intent_for_nc or None,
            )
            _commerce_blocked = _nc_turn is not None
            _fulfillment_discovery_blocked = should_suppress_recommendation_escalation(
                message=text or "",
                brain_state=_bs_for_nc,
                intent_name=_intent_for_nc or None,
            )
            try:
                from modules.ai.brain.order_context_gate import (  # noqa: PLC0415
                    is_fulfillment_discovery_unlock as _fulfillment_unlock,
                )
                if _fulfillment_discovery_blocked and _fulfillment_unlock(
                    text or "",
                    intent_name=_intent_for_nc or None,
                ):
                    _fulfillment_discovery_blocked = False
            except Exception:  # noqa: BLE001
                pass
            _positive_commerce = has_positive_commerce_intent(_intent_for_nc)
            _catalog_card_limit = resolve_breadth_for_inbound(
                message=text or "",
                inbound_metadata=inbound_metadata,
                brain_state=_bs_for_nc,
            ).catalog_card_limit
            if _commerce_blocked:
                logger.info(
                    "[NON_COMMERCE_BLOCK] tenant=%s suppressing commerce "
                    "escalation category=%s source=%s",
                    tenant_id,
                    _nc_turn.category if _nc_turn else "?",
                    _nc_turn.source if _nc_turn else "?",
                )
            if _fulfillment_discovery_blocked:
                logger.info(
                    "[FULFILLMENT_LOCK] tenant=%s suppressing product escalation "
                    "webhook=1 preview=%r",
                    tenant_id,
                    (text or "")[:80],
                )
        except Exception as _nc_exc:  # noqa: BLE001
            logger.debug(
                "[NON_COMMERCE_BLOCK] tenant=%s gate skipped: %s",
                tenant_id, _nc_exc,
            )

        # ── Final dispatch guard (May 2026) ─────────────────────────
        # Authoritative last-chance gate: even when earlier layers
        # blocked search, stale [PRODUCT:] markers, visual enforcement,
        # safety nets, and the catalog loop may still attach cards.
        # Require POSITIVE current-turn commerce permission.
        _dispatch_decision = None
        try:
            from services.final_dispatch_guard import (  # noqa: PLC0415
                log_final_dispatch_guard as _log_dispatch_guard,
                should_allow_product_attachment_dispatch as _eval_dispatch_guard,
            )
            _intent_conf_nc = (_bs_for_nc or {}).get("last_intent_confidence")
            _active_order_nc = None
            _prep_nc = (_bs_for_nc or {}).get("order_prep") or {}
            if _prep_nc.get("product_id") or _prep_nc.get("product_name"):
                _active_order_nc = dict(_prep_nc)
            _dispatch_decision = _eval_dispatch_guard(
                brain_action=_br_action or "",
                intent_name=_intent_for_nc or "",
                intent_confidence=(
                    float(_intent_conf_nc)
                    if _intent_conf_nc is not None
                    else None
                ),
                inbound_message=text or "",
                reply_text=reply or "",
                brain_handoff=bool(_brain_handoff),
                commerce_blocked=_commerce_blocked,
                fulfillment_discovery_blocked=_fulfillment_discovery_blocked,
                brain_state=_bs_for_nc,
                active_order_state=_active_order_nc,
            )
            _allow_product_cards = bool(_dispatch_decision.allow)
            _dispatch_guard_reason = str(_dispatch_decision.reason or "ok")
            _log_dispatch_guard(
                decision=_dispatch_decision,
                tenant_id=tenant_id,
                brain_action=_br_action or "",
                intent_name=_intent_for_nc or "",
            )
        except Exception as _fdg_exc:  # noqa: BLE001
            _allow_product_cards = False
            _dispatch_guard_reason = "guard_eval_failed"
            logger.warning(
                "[FINAL_DISPATCH_GUARD] tenant=%s evaluation failed (fail-closed): %s",
                tenant_id, _fdg_exc,
            )

        _product_escalation_blocked = (
            _commerce_blocked
            or _fulfillment_discovery_blocked
            or not _allow_product_cards
        )
        _structured_catalog_miss = False
        try:
            from services.final_dispatch_guard import (  # noqa: PLC0415
                is_structured_catalog_miss as _is_structured_catalog_miss,
            )

            _structured_catalog_miss = _is_structured_catalog_miss(brain_result)
        except Exception as _catalog_miss_exc:  # noqa: BLE001
            logger.debug(
                "[FINAL_DISPATCH_GUARD] structured catalog miss check failed "
                "tenant=%s: %s",
                tenant_id,
                _catalog_miss_exc,
            )
        _visual_product_escalation_blocked = (
            _product_escalation_blocked or _structured_catalog_miss
        )

        # ── [PRODUCT:<query>] markers ──────────────────────────────
        # Resolve LLM-cited products against the synced catalog and
        # collect them as product-card attachments. The actual send
        # (image + caption + cta_url button) lives further down in
        # the outbound dispatch loop.
        _product_attachments: List[Dict[str, Any]] = []

        # Compose may stamp current-turn rich cards for a single resolved
        # catalog product (no pick_N). Reuse the existing product_card path.
        if _brain_product_cards:
            for _card in _brain_product_cards:
                if not isinstance(_card, dict):
                    continue
                if str(_card.get("kind") or "") != "product_card":
                    continue
                _product_attachments.append(dict(_card))
            if _product_attachments:
                # Do not also send pick_N for the same singleton.
                _brain_buttons = []
                logger.info(
                    "[PRODUCT_ATTACHMENT] tenant=%s stage=resolved "
                    "source=single_resolved_presentation count=%d ids=%s "
                    "with_image=%d with_url=%d",
                    tenant_id,
                    len(_product_attachments),
                    [a.get("id") for a in _product_attachments],
                    sum(1 for a in _product_attachments if a.get("file_url")),
                    sum(1 for a in _product_attachments if a.get("product_url")),
                )
                # Current-turn presentation from search — not recommendation
                # escalation. Allow dispatch even when fulfillment lock would
                # suppress browse-card safety nets.
                if (
                    str(_br_action or "").strip() == "search_products"
                    or str(_br_dec_action or "").strip() == "search_products"
                ):
                    _allow_product_cards = True
                    _dispatch_guard_reason = "single_resolved_presentation"
                    _fulfillment_discovery_blocked = False
                    _product_escalation_blocked = bool(_commerce_blocked)

        if (
            not _product_escalation_blocked
            and reply
            and "[PRODUCT:" in reply.upper()
        ):
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
                        max_attachments=_catalog_card_limit,
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
                        # Variant intelligence (migration 0064 — Phase 3).
                        # Carries the resolver's variant insight onto
                        # the attachment so the catalog send path can:
                        #   1. Short-circuit the card when 2+ in-stock
                        #      variants exist and the customer hasn't
                        #      picked one yet (ship the question
                        #      instead, via the responder).
                        #   2. Pick the correct per-SKU retailer_id
                        #      when a variant HAS been picked.
                        "needs_variant_choice": getattr(_res, "needs_variant_choice", False),
                        "variants":             list(getattr(_res, "variants", []) or []),
                        "has_variants":         getattr(_res, "has_variants", False),
                        "default_variant_retailer_id": getattr(_res, "default_variant_retailer_id", None),
                        "dispatch_source": "llm_marker",
                    })
                if _resolutions:
                    logger.info(
                        "[ProductResolver] tenant=%s resolved=%d "
                        "missing=%d ids=%s",
                        tenant_id, len(_resolutions),
                        len(_missing_products),
                        [r.id for r in _resolutions],
                    )
                    # May 2026 #10 — structured lifecycle log so an
                    # operator grepping ``[PRODUCT_ATTACHMENT]``
                    # sees the entire path of a product card from
                    # marker → resolver → dispatch loop. Source
                    # ``llm_marker`` distinguishes this from the
                    # visual-enforcer (``visual_enforcement``) and
                    # safety-net (``safety_net``) paths added below.
                    logger.info(
                        "[PRODUCT_ATTACHMENT] tenant=%s stage=resolved "
                        "source=llm_marker count=%d ids=%s "
                        "with_image=%d with_url=%d",
                        tenant_id, len(_resolutions),
                        [r.id for r in _resolutions],
                        sum(1 for r in _resolutions if r.image_url),
                        sum(1 for r in _resolutions if r.product_url),
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
        _po_reply_before_safety_nets = reply or ""
        if _outbound_text_tracker is not None:
            _outbound_text_tracker.pre_postprocess_body = _po_reply_before_safety_nets
            _outbound_text_tracker.postprocess_body = _po_reply_before_safety_nets
        try:
            from modules.ai.postprocess.safety_nets import (  # noqa: PLC0415
                apply_product_safety_net as _sn_product,
                apply_media_key_safety_net as _sn_media_key,
                apply_staff_contact_safety_net as _sn_staff,
                apply_store_link_safety_net as _sn_store_link,
                apply_location_safety_net as _sn_location,
                apply_delivery_info_context_net as _sn_delivery_ctx,
                apply_product_reask_guard as _sn_product_reask,
                apply_clear_intent_fallback_net as _sn_clear_intent,
                apply_outbound_artifact_guard as _sn_artifact_guard,
            )
            import json as _json_sn  # noqa: PLC0415

            _cust_id_sn = None
            try:
                _cust_id_sn = getattr(convo, "customer_id", None) or None
            except Exception:
                pass

            # Product safety net — skipped on non-commerce turns and when
            # recommendation-breadth cap is already reached.
            if not _product_escalation_blocked:
                try:
                    _pn = _sn_product(
                        db,
                        tenant_id=tenant_id,
                        customer_msg=text or "",
                        existing_product_attachments=_product_attachments,
                        detected_markers=_marker_detected["product"],
                        customer_id=_cust_id_sn,
                        brain_state=_bs_for_nc,
                    )
                    if (
                        _pn.fired
                        and _pn.extra_attachment
                        and len(_product_attachments) < _catalog_card_limit
                    ):
                        _product_attachments.append(_pn.extra_attachment)
                        _marker_resolved["product"] += 1
                    elif _pn.fired and _pn.extra_attachment:
                        logger.info(
                            "[RECOMMENDATION_BREADTH] tenant=%s "
                            "skipping product safety net append "
                            "reason=catalog_card_limit limit=%d count=%d",
                            tenant_id,
                            _catalog_card_limit,
                            len(_product_attachments),
                        )
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
            else:
                _sn_skip_reason = (
                    "fulfillment_lock"
                    if _fulfillment_discovery_blocked
                    else "non_commerce_block"
                )
                logger.info(
                    "[SAFETY_NET:product] tenant=%s skipped reason=%s",
                    tenant_id,
                    _sn_skip_reason,
                )

            # Media-key safety net
            try:
                _mn = _sn_media_key(
                    db,
                    tenant_id=tenant_id,
                    customer_msg=text or "",
                    existing_media_attachments=_media_attachments,
                    detected_media_key_markers=_marker_detected["media_key"],
                    inbound_metadata=(
                        dict(inbound_metadata or {})
                        if isinstance(inbound_metadata, dict)
                        else {}
                    ),
                    normalized_type=str(
                        (inbound_metadata or {}).get("normalized_type")
                        or (inbound_metadata or {}).get("source_type")
                        or ""
                    ) or None,
                    conversation_id=getattr(convo, "id", None),
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
                        db=db,
                        tenant_id=tenant_id,
                        customer_phone=to or "",
                        history=history if isinstance(history, list) else None,
                        staff_contacts_sent=list(
                            _bs_for_nc.get("staff_contacts_sent") or []
                        ),
                        conversation_turn=int(_bs_for_nc.get("turn") or 0),
                        conversation_id=getattr(convo, "id", None),
                        commerce_session=dict(
                            _bs_for_nc.get("commerce_session") or {}
                        ),
                    )
                    if _cn.fired and _cn.extra_call_target is not None:
                        _call_targets.append(_cn.extra_call_target)
                        _marker_resolved["call"] += 1
                        if getattr(_cn, "strip_phones_from_reply", False):
                            from modules.ai.postprocess.safety_nets import (  # noqa: PLC0415
                                strip_embedded_phones_from_reply as _strip_reply_phones,
                            )
                            reply = _strip_reply_phones(reply or "")
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

            # Store-link safety net (May 2026): the customer asked
            # for the store URL but the LLM shipped a generic line
            # like "هذا متجرنا 🌷" with no link. We inject the
            # configured ``store_url`` from TenantSettings so the
            # customer sees a tappable preview — and never invent
            # a URL when none is on file (polite fallback instead).
            #
            # Tenant 33 #49 (Commit 3): the relational suppression
            # gate runs BEFORE invoking the net. When the brain has
            # produced an emotionally-correct reply for a praise /
            # complaint moment, a cold "تفضل رابط متجرنا" injection
            # would overwrite the warmth — the gate skips the call
            # entirely and emits a structured ``[CX]
            # safety_net_suppressed`` line. The gate stays inert
            # whenever the kill switch is off or no moment is set.
            _sl_suppressed = False
            try:
                from modules.ai.brain.relational import (  # noqa: PLC0415
                    log_safety_net_suppressed as _cx_log_suppressed,
                    should_suppress_safety_net as _cx_should_suppress,
                )
                _sl_suppressed, _sl_reason = _cx_should_suppress(
                    net_name="store_link",
                    moment=_relational_moment or None,
                )
                if _sl_suppressed:
                    _cx_log_suppressed(
                        net_name="store_link",
                        moment=_relational_moment,
                        reason=_sl_reason,
                        tenant_id=tenant_id,
                        conversation_id=getattr(convo, "id", None),
                        customer_phone=to,
                    )
            except Exception:
                _sl_suppressed = False

            if not _sl_suppressed:
                try:
                    _sl = _sn_store_link(
                        db,
                        tenant_id=tenant_id,
                        customer_msg=text or "",
                        reply_text=reply or "",
                        history=history if isinstance(history, list) else None,
                    )
                    if _sl.fired and _sl.rewrote_reply and _sl.new_reply:
                        reply = _otp_apply_reply(
                            _outbound_text_tracker,
                            reply,
                            _sl.new_reply,
                            layer="store_link_safety_net",
                            op="reconcile" if getattr(_sl, "reconciled", False) else "replace",
                        )
                    if _sl.fired or _sl.skipped_reason not in {
                        "no_store_link_intent", "url_already_in_reply",
                    }:
                        _payload = {
                            "event":             "safety_net",
                            "tenant_id":         tenant_id,
                            "conversation_id":   getattr(convo, "id", None),
                            **_sl.to_log_dict(),
                        }
                        logger.info(
                            "[SAFETY_NET:store_link] "
                            + _json_sn.dumps(_payload, ensure_ascii=False)
                        )
                except Exception as _sle:  # noqa: BLE001
                    logger.warning(
                        "[SAFETY_NET:store_link] failed tenant=%s err=%s",
                        tenant_id, _sle,
                    )

            # ── Location / Google Maps safety net (May 2026 #36) ────
            # Sibling of the store-link net. Customer asked "وين
            # موقعكم؟" / "ابي رابط الموقع" / "اللوكيشن" and the LLM
            # either omitted any URL or shipped the e-commerce
            # ``store_url`` instead of Google Maps. We resolve the
            # canonical maps URL via the platform-wide chain
            # (snapshot → store_settings.google_maps_location → KB
            # branches/store_story/custom sections) and inject it
            # so the customer gets a tappable maps preview. NEVER
            # invents a URL — falls back to a polite clarifying
            # line when nothing is configured.
            # Tenant 33 #49 (Commit 3): same surgical suppression
            # gate as the store-link block above. A maps-URL injection
            # on top of an empathy-shaped complaint reply or a thank-
            # you reply derails the warmth — we skip the call and
            # log the canonical ``[CX] safety_net_suppressed`` line.
            _ll_suppressed = False
            try:
                from modules.ai.brain.relational import (  # noqa: PLC0415
                    log_safety_net_suppressed as _cx_log_suppressed_ll,
                    should_suppress_safety_net as _cx_should_suppress_ll,
                )
                _ll_suppressed, _ll_reason = _cx_should_suppress_ll(
                    net_name="location",
                    moment=_relational_moment or None,
                )
                if _ll_suppressed:
                    _cx_log_suppressed_ll(
                        net_name="location",
                        moment=_relational_moment,
                        reason=_ll_reason,
                        tenant_id=tenant_id,
                        conversation_id=getattr(convo, "id", None),
                        customer_phone=to,
                    )
            except Exception:
                _ll_suppressed = False

            if not _ll_suppressed:
                try:
                    _ll = _sn_location(
                        db,
                        tenant_id=tenant_id,
                        customer_msg=text or "",
                        reply_text=reply or "",
                    )
                    if _ll.fired and _ll.rewrote_reply and _ll.new_reply:
                        reply = _otp_apply_reply(
                            _outbound_text_tracker,
                            reply,
                            _ll.new_reply,
                            layer="location_safety_net",
                            op="replace",
                        )
                    if _ll.fired or _ll.skipped_reason not in {
                        "no_location_intent", "maps_url_already_in_reply",
                    }:
                        _payload = {
                            "event":             "safety_net",
                            "tenant_id":         tenant_id,
                            "conversation_id":   getattr(convo, "id", None),
                            **_ll.to_log_dict(),
                        }
                        logger.info(
                            "[SAFETY_NET:location_link] "
                            + _json_sn.dumps(_payload, ensure_ascii=False)
                        )
                except Exception as _lle:  # noqa: BLE001
                    logger.warning(
                        "[SAFETY_NET:location_link] failed tenant=%s err=%s",
                        tenant_id, _lle,
                    )

            # ── Delivery-info context safety net (May 2026) ──────────
            # When the bot's last outbound asked for delivery info
            # (address / city / name+phone) and the customer's reply
            # contains delivery signals — but the LLM dismissed it
            # as out_of_scope / "didn't understand" — rewrite to a
            # short acknowledgement so we don't tell the customer
            # that "this is outside my scope" RIGHT AFTER we asked
            # them for their address. Pure text rewrite — order
            # state is updated by the regular slot extractor on
            # the next turn.
            try:
                _dlv = _sn_delivery_ctx(
                    customer_msg=text or "",
                    reply_text=reply or "",
                    history=history,
                )
                if _dlv.fired and _dlv.new_reply:
                    reply = _otp_apply_reply(
                        _outbound_text_tracker,
                        reply,
                        _dlv.new_reply,
                        layer="delivery_info_context_net",
                        op="replace",
                    )
                if _dlv.fired or _dlv.skipped_reason not in {
                    "flag_disabled",
                    "bot_not_awaiting_delivery",
                    "reply_not_dismissive",
                    "active_order_context_but_weak_signal",
                }:
                    _payload = {
                        "event":             "safety_net",
                        "tenant_id":         tenant_id,
                        "conversation_id":   getattr(convo, "id", None),
                        **_dlv.to_log_dict(),
                    }
                    logger.info(
                        "[SAFETY_NET:delivery_info_context] "
                        + _json_sn.dumps(_payload, ensure_ascii=False)
                    )
            except Exception as _dle:  # noqa: BLE001
                logger.warning(
                    "[SAFETY_NET:delivery_info_context] failed tenant=%s err=%s",
                    tenant_id, _dle,
                )

            # ── Product re-ask guard (May 2026 #47) ─────────────────
            # Recurring Tenant 33 regression: customer confirmed
            # product+price+quantity, bot asked for location, customer
            # sent a Google Maps URL → brain replied "اختر المنتج
            # اللي تبغاه من القائمة". This guard rewrites that single
            # contradictory turn into a short order-continuation ACK.
            # Pure text rewrite — no order-state mutation. Three
            # independent signals must align (product re-ask phrase
            # in the reply + location signal in the inbound + active
            # order in recent history) so the guard is impossible to
            # over-fire.
            try:
                _prg = _sn_product_reask(
                    customer_msg=text or "",
                    reply_text=reply or "",
                    history=history,
                )
                if _prg.fired and _prg.new_reply:
                    reply = _otp_apply_reply(
                        _outbound_text_tracker,
                        reply,
                        _prg.new_reply,
                        layer="product_reask_guard",
                        op="replace",
                    )
                if _prg.fired or _prg.skipped_reason not in {
                    "flag_disabled",
                    "empty_reply",
                    "reply_not_product_reask",
                    "inbound_not_location",
                    "no_active_order_context",
                }:
                    _payload = {
                        "event":             "safety_net",
                        "tenant_id":         tenant_id,
                        "conversation_id":   getattr(convo, "id", None),
                        **_prg.to_log_dict(),
                    }
                    logger.info(
                        "[SAFETY_NET:product_reask_guard] "
                        + _json_sn.dumps(_payload, ensure_ascii=False)
                    )
            except Exception as _prge:  # noqa: BLE001
                logger.warning(
                    "[SAFETY_NET:product_reask_guard] failed tenant=%s err=%s",
                    tenant_id, _prge,
                )

            # Payment barcode image route — queue outbound media before
            # the text-only artifact guard can rewrite to phone fallback.
            try:
                from modules.ai.brain.decision.payment_barcode_routing import (  # noqa: PLC0415
                    apply_payment_barcode_image_route as _apply_barcode_route,
                    payment_barcode_intro_text as _barcode_intro_text,
                )
                _pbr_meta = {}
                if isinstance(inbound_metadata, dict):
                    _pbr_meta = dict(inbound_metadata)
                try:
                    _pbr_meta.update(dict(_live_in_meta or {}))
                except Exception:  # noqa: BLE001
                    pass
                _pbr = _apply_barcode_route(
                    db,
                    tenant_id=tenant_id,
                    customer_msg=text or "",
                    media_attachments=_media_attachments,
                    reply_text=reply or "",
                    conversation_id=getattr(convo, "id", None),
                    inbound_metadata=_pbr_meta,
                    normalized_type=str(
                        _pbr_meta.get("normalized_type")
                        or _pbr_meta.get("source_type")
                        or ""
                    ) or None,
                )
                if _pbr.rewrote_reply:
                    from core.tenant import get_or_create_settings, merge_ai_defaults  # noqa: PLC0415
                    from modules.ai.brain.persona.payment_media_intro import (  # noqa: PLC0415
                        try_compose_payment_media_intro,
                    )

                    _pbr_ai = merge_ai_defaults(
                        dict(get_or_create_settings(db, tenant_id).ai_settings or {})
                    )
                    _pbr_media_url_present = bool(
                        _pbr.asset_found
                        and isinstance(_pbr.attachment, dict)
                        and str(_pbr.attachment.get("file_url") or "").strip()
                    )
                    reply, _, _payment_persona_compose_event = (
                        await try_compose_payment_media_intro(
                            tenant_id=tenant_id,
                            customer_phone=to,
                            inbound_text=text or "",
                            media_key=_pbr.media_key or "",
                            media_url_present=_pbr_media_url_present,
                            ai_settings=_pbr_ai,
                        )
                    )
            except Exception as _pbr_exc:  # noqa: BLE001
                logger.warning(
                    "[PAYMENT_BARCODE] route failed tenant=%s err=%s",
                    tenant_id, _pbr_exc,
                )

            # ── Outbound artifact guard (May 2026 #37 / D2) ──────────
            # Final hollow-affirmation guard. Catches the residual
            # case where every upstream net passed (or didn't fire)
            # and the LLM still shipped a short "أبشر" / "تفضل"
            # reply for a customer who explicitly asked for a
            # phone / barcode / maps URL / store URL. Either
            # injects the artifact (when resolvable from KB /
            # tenant settings) or rewrites to an honest "غير مضاف
            # حاليًا" line so we never promise a delivery the
            # customer doesn't receive. Runs before the generic
            # clear-intent fallback so artifact-specific copy
            # wins over the generic "I didn't understand" copy.
            try:
                _in_meta_ag = {}
                if isinstance(inbound_metadata, dict):
                    _in_meta_ag = dict(inbound_metadata)
                try:
                    _in_meta_ag.update(dict(_live_in_meta or {}))
                except Exception:  # noqa: BLE001
                    pass
                _ag = _sn_artifact_guard(
                    db,
                    tenant_id=tenant_id,
                    customer_msg=text or "",
                    reply_text=reply or "",
                    media_attachments=_media_attachments,
                    call_targets=_call_targets,
                    # May 2026 #38 — pass conversation history so the
                    # guard can carry an artifact intent forward
                    # when the current customer message is a
                    # complaint ("ما جاني شي") rather than a fresh
                    # ask. Falls back to ``None`` gracefully when
                    # history isn't in scope at this call site.
                    history=history if isinstance(history, list) else None,
                    inbound_metadata=_in_meta_ag,
                    normalized_type=str(
                        _in_meta_ag.get("normalized_type")
                        or _in_meta_ag.get("source_type")
                        or ""
                    ) or None,
                    conversation_id=getattr(convo, "id", None),
                )
                if _ag.fired and _ag.rewrote_reply and _ag.new_reply:
                    reply = _otp_apply_reply(
                        _outbound_text_tracker,
                        reply,
                        _ag.new_reply,
                        layer="outbound_artifact_guard",
                        op="replace",
                    )
                # Always log the outcome — including the
                # ``action="pass"`` path — so production triage
                # can chart how often each artifact class actually
                # gets satisfied vs rewritten.
                logger.info(
                    "[OUTBOUND_ARTIFACT_GUARD] tenant=%s "
                    "artifact=%s expected=%s satisfied=%s "
                    "action=%s skipped_reason=%s",
                    tenant_id,
                    _ag.expected_artifact,
                    _ag.expected_artifact != "none",
                    _ag.artifact_satisfied,
                    _ag.action,
                    _ag.skipped_reason or "-",
                )
            except Exception as _age:  # noqa: BLE001
                logger.warning(
                    "[OUTBOUND_ARTIFACT_GUARD] failed tenant=%s err=%s",
                    tenant_id, _age,
                )

            # ── Clear-intent fallback safety net (May 2026) ──────────
            # Phase 2 P0: detect clear intent on generic LLM fallback but
            # do NOT replace outbound text — record facts/metadata only.
            try:
                _ci = _sn_clear_intent(
                    customer_msg=text or "",
                    reply_text=reply or "",
                )
                if _ci.fired:
                    _otp_record_metadata_mutation(
                        _outbound_text_tracker,
                        reply or "",
                        layer="clear_intent_fallback_net",
                        op="noop",
                    )
                    if _outbound_text_tracker is not None and _ci.metadata:
                        _outbound_text_tracker.note(
                            "clear_intent_fallback:"
                            + str(_ci.customer_intent or "")
                        )
                    if _ci.facts.get("required_delivery") == "llm_rephrase":
                        from core.clear_intent_recompose import (  # noqa: PLC0415
                            maybe_recompose_clear_intent_reply,
                        )

                        _ci_reply, _ci_recompose_meta = await maybe_recompose_clear_intent_reply(
                            db=db,
                            tenant_id=tenant_id,
                            phone=to or "",
                            clear_intent_result=_ci,
                            inbound_text=text or "",
                            weak_reply=reply or "",
                        )
                        if _ci_recompose_meta.get("recomposed") and _ci_reply.strip():
                            reply = _otp_apply_reply(
                                _outbound_text_tracker,
                                reply or "",
                                _ci_reply,
                                layer="clear_intent_recompose",
                                op="replace",
                            )
                            if _outbound_text_tracker is not None:
                                from core.outbound_text_policy import OutboundTextSource  # noqa: PLC0415

                                _outbound_text_tracker.set_compose_provenance(
                                    source=OutboundTextSource.LLM,
                                    policy_path="clear_intent_recompose.constrained_compose",
                                    debt=False,
                                )
                                _outbound_text_tracker.note(
                                    "clear_intent_recompose:facts="
                                    + str(_ci.facts.get("detected_intent") or "")
                                )
                if _ci.fired or _ci.skipped_reason not in {
                    "flag_disabled",
                    "reply_not_generic_fallback",
                    "empty_reply",
                    "no_clear_intent",
                }:
                    _payload = {
                        "event":             "safety_net",
                        "tenant_id":         tenant_id,
                        "conversation_id":   getattr(convo, "id", None),
                        "customer_intent":   _ci.customer_intent,
                        "reason":            _ci.reason or _ci.skipped_reason,
                        "text_written":      _ci.text_written,
                        "facts":             _ci.facts,
                    }
                    logger.info(
                        "[SAFETY_NET:clear_intent_fallback] "
                        + _json_sn.dumps(_payload, ensure_ascii=False)
                    )
                elif _ci.fired:
                    logger.info(
                        "[SAFETY_NET:clear_intent_fallback] "
                        "customer_intent=%s reason=%s",
                        _ci.customer_intent,
                        _ci.reason,
                    )
            except Exception as _cie:  # noqa: BLE001
                logger.warning(
                    "[SAFETY_NET:clear_intent_fallback] failed tenant=%s err=%s",
                    tenant_id, _cie,
                )
        except Exception as _sn_exc:  # noqa: BLE001
            logger.warning(
                "[SAFETY_NET] module import failed tenant=%s err=%s",
                tenant_id, _sn_exc,
            )

        if (reply or "").strip() != (_po_reply_before_safety_nets or "").strip():
            _persona_ownership.on_text_replaced(
                layer="safety_nets",
                reason=_POReason.SAFETY_NET_REWRITE,
                before=_po_reply_before_safety_nets,
                after=reply or "",
            )

        # ── Service-closer guard (pass 2 — post safety nets) ─────────────
        if reply and not _brain_handoff:
            _po_reply_before_scg2 = reply
            try:
                from modules.ai.brain.postprocess.service_closer_guard import (  # noqa: PLC0415
                    apply_service_closer_guard as _apply_scg_pass2,
                )
                _nc_meta_scg2 = (
                    dict(inbound_metadata or {})
                    if isinstance(inbound_metadata, dict)
                    else {}
                )
                if _brain_nc_category:
                    _nc_meta_scg2.setdefault(
                        "non_commerce_category", _brain_nc_category,
                    )
                if _commerce_blocked and _nc_turn is not None:
                    _nc_meta_scg2.setdefault("non_commerce_category", _nc_turn.category)
                    _nc_meta_scg2.setdefault("block_commerce_escalation", True)
                _scg_pass2 = _apply_scg_pass2(
                    reply,
                    inbound_text=text or "",
                    inbound_metadata=_nc_meta_scg2,
                    non_commerce_block_mode=(_brain_nc_block or _commerce_blocked),
                    block_commerce_escalation=(_brain_nc_block or _commerce_blocked),
                    tenant_id=tenant_id,
                )
                if _scg_pass2.stripped:
                    reply = _scg_pass2.reply
                    _persona_ownership.on_text_replaced(
                        layer="service_closer_guard_pass2",
                        reason=_POReason.FALLBACK_REPLY,
                        before=_po_reply_before_scg2,
                        after=reply,
                    )
            except Exception as _scg2_exc:  # noqa: BLE001
                logger.debug(
                    "[SERVICE_CLOSER_GUARD] pass2 failed tenant=%s err=%s",
                    tenant_id, _scg2_exc,
                )

        # ── Social phrase quality guard (P1-F — post safety nets belt) ───
        if reply and not _brain_handoff:
            _po_reply_before_spg2 = reply
            try:
                from modules.ai.brain.postprocess.social_phrase_quality_guard import (  # noqa: PLC0415
                    apply_social_phrase_quality_guard as _apply_spg_pass2,
                )
                _spg_pass2 = _apply_spg_pass2(
                    reply,
                    inbound_text=text or "",
                    tenant_id=tenant_id,
                )
                if _spg_pass2.stripped:
                    reply = _spg_pass2.reply
                    _persona_ownership.on_text_replaced(
                        layer="social_phrase_quality_guard_pass2",
                        reason=_POReason.FALLBACK_REPLY,
                        before=_po_reply_before_spg2,
                        after=reply,
                    )
            except Exception as _spg2_exc:  # noqa: BLE001  # noqa: silent-ok — belt guard best-effort
                logger.debug(
                    "[SOCIAL_PHRASE_QUALITY_GUARD] pass2 failed tenant=%s err=%s",
                    tenant_id, _spg2_exc,
                )

        # ── Occasion reply guard (P1-D-3 — post safety nets belt) ────────
        if reply and not _brain_handoff:
            _po_reply_before_org = reply
            try:
                from modules.ai.brain.postprocess.occasion_reply_guard import (  # noqa: PLC0415
                    apply_occasion_reply_guard,
                )
                _org = apply_occasion_reply_guard(
                    reply,
                    inbound_text=text or "",
                    inbound_metadata=(
                        dict(inbound_metadata or {})
                        if isinstance(inbound_metadata, dict)
                        else {}
                    ),
                    tenant_id=tenant_id,
                )
                if _org.stripped:
                    reply = _org.reply
                    _persona_ownership.on_text_replaced(
                        layer="occasion_reply_guard",
                        reason=_POReason.FALLBACK_REPLY,
                        before=_po_reply_before_org,
                        after=reply,
                    )
            except Exception as _org_exc:  # noqa: BLE001  # noqa: silent-ok — occasion guard best-effort
                logger.debug(
                    "[OCCASION_REPLY_GUARD] failed tenant=%s err=%s",
                    tenant_id, _org_exc,
                )

        # ── Product media reply guard (P1-E — belt) ───────────────────────
        if reply and not _brain_handoff:
            _po_reply_before_pmg = reply
            try:
                from modules.ai.brain.postprocess.product_media_reply_guard import (  # noqa: PLC0415
                    apply_product_media_reply_guard,
                )
                _pmg = apply_product_media_reply_guard(
                    reply,
                    inbound_text=text or "",
                    inbound_metadata=(
                        dict(inbound_metadata or {})
                        if isinstance(inbound_metadata, dict)
                        else {}
                    ),
                    tenant_id=tenant_id,
                )
                if _pmg.stripped:
                    reply = _pmg.reply
                    _persona_ownership.on_text_replaced(
                        layer="product_media_reply_guard",
                        reason=_POReason.FALLBACK_REPLY,
                        before=_po_reply_before_pmg,
                        after=reply,
                    )
            except Exception as _pmg_exc:  # noqa: BLE001  # noqa: silent-ok — product media guard best-effort
                logger.debug(
                    "[PRODUCT_MEDIA_REPLY_GUARD] failed tenant=%s err=%s",
                    tenant_id, _pmg_exc,
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
        # After Brain/LLM has owned the turn: if requestive payment
        # consent is true (not a weak bank-name collision) and GPT did
        # not cite the merchant asset, attach the authoritative media.
        # Do not replace a valid LLM reply with canned prose.
        try:
            from core.ai_libraries import (  # noqa: PLC0415
                find_best_payment_asset as _find_payment_asset,
            )
            from modules.ai.brain.commerce.conversational_priority import (  # noqa: PLC0415
                has_payment_outbound_consent as _has_payment_consent_hard,
            )
            from modules.ai.brain.commerce.payment_execution_ownership import (  # noqa: PLC0415
                may_attach_payment_asset_after_brain as _may_attach_payment_asset,
            )
            _in_meta_hard = {}
            try:
                _in_meta_hard = dict(_live_in_meta or {})
            except Exception:  # noqa: BLE001
                _in_meta_hard = {}
            if isinstance(inbound_metadata, dict):
                _in_meta_hard.update(inbound_metadata)
            _payment_consent_hard = _has_payment_consent_hard(
                text or "",
                inbound_metadata=_in_meta_hard,
                tenant_id=tenant_id,
                route="payment_hard_override",
                conversation_id=getattr(convo, "id", None),
            )
            _payment_intent = _may_attach_payment_asset(
                requestive_consent=_payment_consent_hard,
                inbound_metadata=_in_meta_hard,
                normalized_type=str(
                    _in_meta_hard.get("normalized_type")
                    or _in_meta_hard.get("source_type")
                    or ""
                ) or None,
                brain_decision_args=_br_dec_args,
                brain_intent_name=str(_br_dec_action or ""),
            )
            if _payment_intent:
                from modules.ai.checkout_authority import (  # noqa: PLC0415
                    brain_payment_paths_should_defer_to_checkout_owner as _defer_payment_to_checkout,
                )
                if _defer_payment_to_checkout(
                    db,
                    tenant_id=tenant_id,
                    conversation=convo,
                    message=text or "",
                    inbound_metadata=_in_meta_hard,
                ):
                    logger.info(
                        "[PAYMENT_INFO] tenant=%s conversation_id=%s "
                        "intent_detected=true hard_override_skipped=true "
                        "reason=active_checkout_owner",
                        tenant_id, getattr(convo, "id", None),
                    )
                else:
                    from modules.ai.brain.commerce.customer_origin_intent import (  # noqa: PLC0415
                        split_inbound_text,
                    )
                    from core.ai_libraries import validate_media_for_send as _validate_media_hard  # noqa: PLC0415

                    _split_hard = split_inbound_text(
                        text or "",
                        inbound_metadata=_in_meta_hard,
                        normalized_type=str(
                            _in_meta_hard.get("normalized_type")
                            or _in_meta_hard.get("source_type")
                            or ""
                        ) or None,
                    )
                    _origin_hard = _split_hard.customer_origin
                    _already_attached_ids = {a.get("id") for a in _media_attachments}
                    _payment_asset = _find_payment_asset(
                        db, tenant_id, _origin_hard or "",
                    )
                    if _payment_asset and _payment_asset.get("id") not in _already_attached_ids:
                        _ok_hard, _err_hard, _normalised_hard = _validate_media_hard(
                            _payment_asset,
                            expected_tenant_id=tenant_id,
                            db=db,
                        )
                        if _ok_hard and _normalised_hard:
                            _media_attachments.append(_normalised_hard)
                            logger.info(
                                "[PAYMENT_INFO] tenant=%s conversation_id=%s "
                                "intent_detected=true asset_found=true asset_id=%s "
                                "asset_score=%.2f transfer_fallback_skipped=true "
                                "gpt_cited_marker=%s — hard override applied",
                                tenant_id, getattr(convo, "id", None),
                                _normalised_hard.get("id"),
                                float(_normalised_hard.get("_relevance_score") or 0.0),
                                bool(_already_attached_ids),
                            )
                        else:
                            logger.info(
                                "[PAYMENT_INFO] tenant=%s conversation_id=%s "
                                "intent_detected=true asset_found=true asset_id=%s "
                                "hard_override_skipped=true reason=asset_not_sendable err=%s",
                                tenant_id, getattr(convo, "id", None),
                                _payment_asset.get("id"),
                                _err_hard,
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

        if reply:
            try:
                from modules.ai.media.display_guard import (  # noqa: PLC0415
                    apply_media_display_outbound_guard as _media_display_guard,
                )
                _orig_media_guard = reply
                reply, _media_guard_scrubbed = _media_display_guard(reply or "")
                if _media_guard_scrubbed:
                    logger.info(
                        "[MEDIA_DISPLAY_GUARD] tenant=%s conversation_id=%s "
                        "scrubbed=true orig_len=%d new_len=%d",
                        tenant_id, getattr(convo, "id", None),
                        len(_orig_media_guard or ""), len(reply or ""),
                    )
            except Exception as _mdg_exc:  # noqa: BLE001
                logger.exception(
                    "[MEDIA_DISPLAY_GUARD] failed tenant=%s",
                    tenant_id,
                )

        if reply:
            try:
                from core.outbound_sanitizer import sanitize_outbound_text as _scrub_policy  # noqa: PLC0415
                _orig_policy = reply
                reply, _policy_scrubbed = _scrub_policy(
                    reply or "",
                    tenant_id=tenant_id,
                    recipient=to,
                )
                if _policy_scrubbed:
                    logger.info(
                        "[INTERNAL_POLICY_BLOCKED] tenant=%s conversation_id=%s "
                        "scrubbed=true orig_len=%d new_len=%d",
                        tenant_id, getattr(convo, "id", None),
                        len(_orig_policy or ""), len(reply or ""),
                    )
            except Exception as _policy_exc:  # noqa: BLE001
                logger.debug(
                    "[INTERNAL_POLICY_BLOCKED] inline scrub failed tenant=%s: %s",
                    tenant_id, _policy_exc,
                )

        # ── Asset-promise guard (May 2026 #30) ────────────────────────
        # Detect "I will send you the link/number/barcode/location" in
        # the outbound text and verify the matching asset is actually
        # queued. When the promise is broken (text says yes, dispatch
        # says no) we rewrite the offending span to a neutral copy so
        # the customer doesn't get a false promise. Three signals:
        #
        #   * ``_has_url``: any explicit URL in the reply OR a product
        #     card queued (product cards carry their own CTA URL).
        #   * ``_has_media``: at least one media attachment in
        #     ``_media_attachments`` (image / video / document /
        #     barcode picked up by the resolver or safety net).
        #   * ``_has_phone``: phone digits in the reply OR a
        #     ``[CALL:...]`` contact card queued in ``_call_targets``.
        #
        # The guard runs AFTER the marker scrub so we operate on the
        # exact text the customer will see, and BEFORE every send
        # branch (brain buttons / cta split / single text) so all
        # outbound paths are covered.
        if reply:
            try:
                from core.outbound_sanitizer import (  # noqa: PLC0415
                    maybe_scrub_unkept_asset_promise as _scrub_promise,
                )
                import re as _re_ap  # noqa: PLC0415
                _has_url_in_text = bool(
                    _re_ap.search(r"https?://\S+", reply or "")
                )
                _has_media_queued = bool(_media_attachments)
                _has_product_card_queued = bool(_product_attachments)
                _has_url_total = (
                    _has_url_in_text or _has_product_card_queued
                )
                _has_phone_in_text = bool(
                    _re_ap.search(
                        r"(?:\+?966|00966|0)?5\d{8}|\+\d{7,15}", reply or "",
                    )
                )
                _has_call_target = bool(_call_targets)
                _has_phone_total = _has_phone_in_text or _has_call_target

                _new_reply_promise, _scrubbed_promise, _asset_cls = _scrub_promise(
                    reply or "",
                    has_url=_has_url_total,
                    has_media=_has_media_queued,
                    has_phone=_has_phone_total,
                    has_product_card=_has_product_card_queued,
                    tenant_id=tenant_id,
                    recipient=to,
                    skip_asset_promise_scrub=(
                        str(_br_dec_action or "") == "customer_ledger_reply"
                    ),
                )
                if _scrubbed_promise:
                    logger.info(
                        "[ASSET_PROMISE_SCRUBBED] tenant=%s "
                        "conversation_id=%s asset_class=%s "
                        "has_url=%s has_media=%s has_phone=%s "
                        "has_product_card=%s",
                        tenant_id, getattr(convo, "id", None),
                        _asset_cls,
                        _has_url_total, _has_media_queued,
                        _has_phone_total, _has_product_card_queued,
                    )
                    reply = _new_reply_promise
            except Exception as _ap_exc:  # noqa: BLE001 — never break send
                logger.debug(
                    "[ASSET_PROMISE_SCRUB] evaluate failed (open): %s",
                    _ap_exc,
                )

        # ── Marketing emoji policy (text-only polish) ─────────────────
        # Runs after truth guards and scrubs; does not touch buttons,
        # payloads, or attachments — only the outbound body string.
        if reply:
            try:
                from core.active_order_context import (  # noqa: PLC0415
                    load_commerce_bundle as _mep_load_bundle,
                )
                from core.order_flow import (  # noqa: PLC0415
                    _focus_summary as _mep_focus,
                    _load_brain_state as _mep_load,
                )
                from core.tenant import get_or_create_settings as _mep_settings  # noqa: PLC0415
                from modules.ai.brain.postprocess.shipment_evidence import (  # noqa: PLC0415
                    evaluate_shipment_evidence as _mep_ship_ev,
                )
                from modules.ai.postprocess.marketing_emoji_policy import (  # noqa: PLC0415
                    apply_marketing_emoji_policy as _apply_mep,
                    build_marketing_emoji_context as _build_mep_ctx,
                )

                _mep_meta = (
                    dict(inbound_metadata or {})
                    if isinstance(inbound_metadata, dict)
                    else {}
                )
                _, _mep_bs = _mep_load(db, tenant_id=tenant_id, phone=to)
                _mep_bs_dict = (
                    _mep_bs if isinstance(_mep_bs, dict)
                    else (_bs_for_nc if isinstance(_bs_for_nc, dict) else {})
                )
                _mep_summary = _mep_focus(_mep_bs_dict)
                _mep_bundle = _mep_load_bundle(
                    dict(getattr(convo, "extra_metadata", None) or {})
                )
                _mep_pe = str(
                    _mep_meta.get("payment_evidence_status")
                    or _mep_summary.get("payment_evidence_status")
                    or ""
                )
                _mep_prep = _mep_bs_dict.get("order_prep") or {}
                if not isinstance(_mep_prep, dict):
                    _mep_prep = {}
                _mep_ship = _mep_ship_ev(
                    commerce_bundle=_mep_bundle,
                    inbound_metadata=_mep_meta,
                    payment_receipt_received=bool(
                        _mep_summary.get("payment_receipt_received")
                    ),
                )
                _mep_settings_obj = _mep_settings(db, tenant_id)
                _mep_ai = dict(
                    getattr(_mep_settings_obj, "ai_settings", None) or {}
                )
                _mep_chosen = str(
                    _mep_meta.get("deterministic_path")
                    or _mep_prep.get("chosen_path")
                    or (_br_dec_args or {}).get("chosen_path")
                    or ""
                )
                _mep_ctx = _build_mep_ctx(
                    tenant_id=tenant_id,
                    conversation_id=getattr(convo, "id", None),
                    inbound_text=text or "",
                    intent_name=str(
                        _mep_meta.get("intent_name")
                        or (_bs_for_nc or {}).get("last_intent")
                        or ""
                    ),
                    decision_action=_br_dec_action or _br_action or "",
                    decision_args=_br_dec_args or {},
                    chosen_path=_mep_chosen,
                    reply_instruction_path=str(
                        _mep_meta.get("deterministic_path") or ""
                    ),
                    stage=str(_mep_prep.get("stage") or ""),
                    owner=str(_mep_bs_dict.get("turn_owner") or ""),
                    navigator_step=str(
                        (_br_dec_args or {}).get("navigator_step") or ""
                    ),
                    catalog_navigation_source=str(
                        _mep_meta.get("catalog_navigation_source") or ""
                    ),
                    order_status=str(
                        _mep_summary.get("order_status")
                        or (_mep_bundle.get("active_order_context") or {}).get(
                            "order_status"
                        )
                        or ""
                    ),
                    awaiting_payment_receipt=bool(
                        _mep_summary.get("awaiting_payment_receipt")
                    ),
                    payment_receipt_received=bool(
                        _mep_summary.get("payment_receipt_received")
                    ),
                    payment_evidence_status=_mep_pe,
                    shipment_evidence_ok=bool(_mep_ship.evidence_ok),
                    social_category=str(
                        (_br_dec_args or {}).get("social_category") or ""
                    ),
                    human_priority=bool(_brain_handoff),
                    locale="ar",
                    ai_settings=_mep_ai,
                    reply_text=reply or "",
                )
                _mep_result = _apply_mep(reply or "", _mep_ctx)
                if _mep_result.changed:
                    reply = _mep_result.reply
            except Exception as _mep_exc:  # noqa: BLE001 — never break send
                logger.debug(
                    "[MARKETING_EMOJI_POLICY] webhook hook failed tenant=%s err=%s",
                    tenant_id, _mep_exc,
                )

        # Purchase-channel selector: if an Online Store button already
        # owns the canonical storefront URL, do not also print that
        # same URL in the visible body. Presentation/wire only.
        if _brain_buttons and reply:
            try:
                from core.wa_link_buttons import (  # noqa: PLC0415
                    prepare_purchase_channel_selector_presentation as _prep_pcs,
                )
                from modules.ai.brain.commerce.store_url_resolver import (  # noqa: PLC0415
                    resolve_store_url as _resolve_pcs_store_url,
                )

                _pcs_store_url = str(
                    _resolve_pcs_store_url(db, int(tenant_id or 0)).url or ""
                ).strip()
                reply, _brain_buttons = _prep_pcs(
                    body=reply,
                    buttons=list(_brain_buttons or []),
                    topic=str((_br_dec_args or {}).get("topic") or ""),
                    owner=str((_br_dec_args or {}).get("owner") or ""),
                    canonical_store_url=_pcs_store_url,
                )
            except Exception as _pcs_exc:  # noqa: BLE001  # noqa: silent-ok — body URL elision must not block send
                logger.debug(
                    "[PURCHASE_CHANNEL_URL] presentation skipped tenant=%s err=%s",
                    tenant_id,
                    _pcs_exc,
                )

        # ── Sync persisted body to post-safety-net reply ────────────
        # ``StateManager.save_message(direction="outbound")`` ran way
        # upstream (≈ L5883), BEFORE the safety nets / scrub / asset-
        # promise guard / CTA-button extractor touched the reply. The
        # dashboard inbox reads MessageEvent rows verbatim, so without
        # this sync the merchant sees the brain's RAW pre-safety-net
        # text — e.g. "هذا متجرنا 🌷" — while the customer's WhatsApp
        # receives the FINAL text with the injected ``store_url``.
        #
        # The sync uses the same (tenant, recipient, queued) lookup as
        # ``stamp_outbound_send_status`` so we always touch the row
        # the wire layer will stamp next. It never raises — any DB
        # error is logged and swallowed so the send path is
        # uncompromised. See ``core.outbound_send_status`` for the
        # rationale.
        if reply:
            try:
                from core.outbound_send_status import (  # noqa: PLC0415
                    sync_outbound_body_to_final as _sync_body,
                )
                if _outbound_text_tracker is not None:
                    _outbound_text_tracker.postprocess_body = reply or ""
                _sync_body(
                    db,
                    tenant_id=tenant_id,
                    recipient=to,
                    final_body=reply,
                    reason="post_safety_nets_pre_send",
                    outbound_text_policy=(
                        _outbound_text_tracker.to_metadata()
                        if _outbound_text_tracker is not None
                        else None
                    ),
                    persona_compose_event=(
                        _payment_persona_compose_event or _brain_persona_compose_event
                    ),
                )
                if _outbound_text_tracker is not None:
                    from core.outbound_text_policy import log_outbound_text_policy  # noqa: PLC0415

                    log_outbound_text_policy(
                        _outbound_text_tracker,
                        tenant_id=tenant_id,
                        to=to,
                    )
            except Exception as _sync_exc:  # noqa: BLE001 — never break send
                logger.debug(
                    "[OUTBOUND_BODY_SYNC] evaluate failed (open): %s",
                    _sync_exc,
                )

        if reply and isinstance(brain_result, dict):
            from core.ai_quality_events import observe_turn_quality  # noqa: PLC0415

            _recent_outbound = [
                str(h.get("body") or "")
                for h in (history or [])
                if str(h.get("direction") or "").lower() in {"out", "outbound"}
            ][-3:]
            observe_turn_quality(
                db,
                tenant_id=int(tenant_id),
                conversation_id=getattr(convo, "id", None),
                customer_phone=to,
                inbound_text=text or "",
                reply_text=reply or "",
                brain_result=brain_result,
                outbound_text_policy=(
                    _outbound_text_tracker.to_metadata()
                    if _outbound_text_tracker is not None
                    else None
                ),
                recent_outbound_bodies=_recent_outbound,
                turn=int(getattr(state, "turn", 0) or 0),
            )

        _visual_enforced_pre_send = False
        _reply_before_final_visual_boundary = str(reply or "")
        _had_product_delivery_candidate = bool(
            _native_catalog_entry or _product_attachments
        )
        try:
            from services.visual_product_dispatch import (  # noqa: PLC0415
                maybe_enforce_visual_product_card as _maybe_visual_card,
            )
            _cust_id_vp = None
            try:
                _cust_id_vp = getattr(convo, "customer_id", None) or None
            except Exception:  # noqa: BLE001
                _cust_id_vp = None
            _product_attachments, _visual_enforced_pre_send = _maybe_visual_card(
                db=db,
                tenant_id=tenant_id,
                inbound_message=text or "",
                reply_text=reply or "",
                brain_action=_br_action or "",
                brain_state=_bs_for_nc if isinstance(_bs_for_nc, dict) else {},
                product_attachments=_product_attachments,
                media_attachments=_media_attachments,
                product_escalation_blocked=_visual_product_escalation_blocked,
                fulfillment_discovery_blocked=_fulfillment_discovery_blocked,
                allow_product_cards=_allow_product_cards,
                dispatch_guard_reason=_dispatch_guard_reason,
                catalog_card_limit=_catalog_card_limit,
                customer_id=_cust_id_vp,
            )
        except Exception as _vp_pre_exc:  # noqa: BLE001
            logger.exception(
                "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s pre_send failed",
                tenant_id,
            )

        if (
            not _allow_product_cards
            and _product_attachments
            and _dispatch_decision is not None
        ):
            try:
                from services.final_dispatch_guard import (  # noqa: PLC0415
                    suppress_product_attachments as _purge_product_atts_pre,
                )
                _product_attachments, reply = _purge_product_atts_pre(
                    product_attachments=_product_attachments,
                    reply_text=reply or "",
                    decision=_dispatch_decision,
                    tenant_id=tenant_id,
                    had_stale_candidates=bool(_product_attachments),
                )
            except Exception as _purge_pre_exc:  # noqa: BLE001
                logger.debug(
                    "[FINAL_DISPATCH_GUARD] pre_send purge failed tenant=%s: %s",
                    tenant_id,
                    _purge_pre_exc,
                )

        # Last persisted provenance snapshot before any provider call.  This
        # records the body that survived every marker/visual/dispatch layer,
        # and deliberately does not compose or replace customer prose.
        from core.outbound_final_boundary import (  # noqa: PLC0415
            commerce_delivery_expected as _commerce_delivery_expected,
            outbound_body_kind as _outbound_body_kind,
        )

        _pending_wire_attachments = (
            (
                [{"kind": "native_catalog"}]
                if _native_catalog_entry.get("thumbnail_product_retailer_id")
                else []
            )
            + (_product_attachments or [])
            + (_media_attachments or [])
        )
        _commerce_text_required = _commerce_delivery_expected(
            decision_action=_br_dec_action,
            brain_action=_br_action,
            brain_result=(brain_result if isinstance(brain_result, dict) else None),
            had_product_delivery_candidate=_had_product_delivery_candidate,
        )
        _final_wire_body_kind = _outbound_body_kind(reply)
        _brain_final_text_transformed = bool(
            (brain_result or {}).get("final_text_transformed")
        ) if isinstance(brain_result, dict) else False
        _brain_transform_reasons = (
            (brain_result or {}).get("final_transform_reasons")
            if isinstance(brain_result, dict)
            else []
        )
        _wire_transform_reasons = [
            str(_reason)
            for _reason in (
                _brain_transform_reasons
                if isinstance(_brain_transform_reasons, (list, tuple))
                else []
            )
            if str(_reason or "").strip()
        ]
        if str(reply or "") != _reply_before_final_visual_boundary:
            if "final_visual_dispatch" not in _wire_transform_reasons:
                _wire_transform_reasons.append("final_visual_dispatch")
        if _outbound_text_tracker is not None:
            for _mutation in (_outbound_text_tracker.postprocess_mutations or []):
                _layer = str(getattr(_mutation, "layer", "") or "").strip()
                if getattr(_mutation, "text_changed", False) and _layer and _layer not in _wire_transform_reasons:
                    _wire_transform_reasons.append(_layer)

        _final_expression_owner = str(
            getattr(_persona_ownership, "expression_owner", "") or ""
        )
        if str(reply or "") != _reply_before_final_visual_boundary:
            _final_expression_owner = "final_visual_dispatch"

        try:
            from core.outbound_send_status import (  # noqa: PLC0415
                sync_outbound_body_to_final as _sync_final_wire_body,
            )

            _sync_final_wire_body(
                db,
                tenant_id=tenant_id,
                recipient=to,
                final_body=reply or "",
                reason="final_wire_boundary",
                outbound_text_policy=(
                    _outbound_text_tracker.to_metadata()
                    if _outbound_text_tracker is not None
                    else None
                ),
                persona_compose_event=(
                    _payment_persona_compose_event or _brain_persona_compose_event
                ),
                provenance_metadata={
                    "final_expression_owner": _final_expression_owner,
                    "final_wire_body_kind": _final_wire_body_kind,
                    "final_wire_structured_delivery": bool(
                        _brain_buttons or _pending_wire_attachments
                    ),
                    "final_text_transformed": bool(
                        _brain_final_text_transformed or _wire_transform_reasons
                    ),
                    "final_transform_reasons": _wire_transform_reasons,
                },
            )
        except Exception as _final_sync_exc:  # noqa: BLE001  # noqa: silent-ok — audit must not block send
            logger.debug(
                "[OUTBOUND_FINAL_PROVENANCE] sync failed tenant=%s err=%s",
                tenant_id,
                _final_sync_exc,
            )

        from core.outbound_wire_audit import refresh_wire_audit  # noqa: PLC0415

        refresh_wire_audit(
            tenant_id, to, reply or "",
            {
                **_otp_merge_save_metadata(
                    _outbound_text_tracker,
                    _persona_ownership.to_metadata(),
                    persona_compose_event=_payment_persona_compose_event or _brain_persona_compose_event,
                    brain_result=brain_result if isinstance(brain_result, dict) else None,
                ),
                "final_expression_owner": _final_expression_owner,
                "final_text_transformed": bool(_brain_final_text_transformed or _wire_transform_reasons),
                "final_transform_reasons": _wire_transform_reasons,
            },
        )
        _send_ok = False
        _social_send_suppressed = False
        _outbound_wire_boundary_done = False
        _interactive_recovered_as_text = False
        try:
            from modules.ai.brain.postprocess.social_single_reply_guard import (  # noqa: PLC0415
                should_suppress_competing_social_outbound,
            )

            _social_send_suppressed = should_suppress_competing_social_outbound(
                _trace,
                source="brain_wire_send",
                action=str(getattr(_trace, "brain_action", "") or ""),
                inbound_text=text or "",
            )
        except Exception:  # noqa: BLE001
            _social_send_suppressed = False

        if _social_send_suppressed:
            _outbound_wire_boundary_done = True
            if not _outbound_abort_audited:
                _maybe_log_outbound_candidate_abort(
                    tenant_id=tenant_id,
                    conversation_id=getattr(convo, "id", None),
                    customer_id=_outbound_customer_id,
                    brain_candidate=_brain_reply_candidate,
                    final_reply=reply,
                    abort_reason="social_single_reply_guard",
                    final_stage="pre_provider_send",
                    suppressor=_outbound_abort_suppressor or None,
                    expression_owner=_persona_ownership.expression_owner,
                )
                _outbound_abort_audited = True
            _log_empty_outbound_suppressed(
                tenant_id=tenant_id,
                to=to,
                conversation_id=getattr(convo, "id", None),
                reason="social_single_reply_guard",
            )
        elif _should_suppress_empty_outbound_reply(
            reply,
            brain_buttons=_brain_buttons,
            pending_attachments=_pending_wire_attachments,
            require_substantive_commerce_text=_commerce_text_required,
        ):
            _outbound_wire_boundary_done = True
            _non_substantive_commerce = bool(
                _commerce_text_required
                and _final_wire_body_kind == "symbols_only"
            )
            _wire_suppression_reason = (
                "non_substantive_commerce_reply"
                if _non_substantive_commerce
                else "skip_wire_send"
            )
            if not _outbound_abort_audited:
                _maybe_log_outbound_candidate_abort(
                    tenant_id=tenant_id,
                    conversation_id=getattr(convo, "id", None),
                    customer_id=_outbound_customer_id,
                    brain_candidate=_brain_reply_candidate,
                    final_reply=reply,
                    abort_reason=_wire_suppression_reason,
                    final_stage="pre_provider_send",
                    suppressor=_outbound_abort_suppressor or None,
                    expression_owner=_final_expression_owner,
                    final_response_non_substantive=_non_substantive_commerce,
                )
                _outbound_abort_audited = True
            try:
                from core.outbound_send_status import (  # noqa: PLC0415
                    stamp_outbound_suppressed as _stamp_suppressed,
                )

                _stamp_suppressed(
                    db,
                    tenant_id=tenant_id,
                    recipient=to,
                    reason=_wire_suppression_reason,
                    final_stage="pre_provider_send",
                    body_kind=_final_wire_body_kind,
                )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — suppression audit must not block webhook
                pass
            _log_empty_outbound_suppressed(
                tenant_id=tenant_id,
                to=to,
                conversation_id=getattr(convo, "id", None),
                reason=_wire_suppression_reason,
            )
        elif _native_catalog_entry.get("thumbnail_product_retailer_id"):
            _outbound_wire_boundary_done = True
            _brain_reply_before_native = str(reply or "").strip()
            _native_send_result = await _try_send_native_catalog_entry(
                db=db,
                tenant_id=tenant_id,
                phone_id=phone_id,
                to=to,
                entry=_native_catalog_entry,
                fallback_body=reply or "",
            )
            if _native_send_result.success:
                _send_ok = True
                try:
                    from modules.ai.brain.commerce.catalog_body_policy import (  # noqa: PLC0415
                        resolve_native_catalog_body_text,
                    )

                    reply = resolve_native_catalog_body_text(
                        context_reply=reply or "",
                        inbound_customer_message=str(text or ""),
                    )
                except Exception:  # noqa: BLE001  # noqa: silent-ok — success body for trace only
                    pass
                if isinstance(_delivery_audit, dict):
                    _delivery_audit["native_catalog_sent"] = True
                    _delivery_audit["text_sent"] = True
                    try:
                        from modules.ai.media.customer_turn_completion import (  # noqa: PLC0415
                            native_catalog_send_completion,
                        )

                        _delivery_audit.update(
                            native_catalog_send_completion(
                                sent=True,
                                has_brain_text=bool(_brain_reply_before_native),
                            )
                        )
                    except Exception:  # noqa: BLE001  # noqa: silent-ok — completion stamp must not block send
                        pass
                if _outbound_text_tracker is not None:
                    _outbound_text_tracker.set_native_catalog(body=reply or "")
                try:
                    from modules.ai.brain.commerce.selection_context import (  # noqa: PLC0415
                        apply_selection_context_patch,
                    )
                    from modules.ai.brain.state.store import DefaultStateStore  # noqa: PLC0415

                    _nc_store = DefaultStateStore()
                    _nc_state = _nc_store.load(db, tenant_id, to)
                    apply_selection_context_patch(
                        _nc_state,
                        {"native_catalog_send_failed": False},
                    )
                    _nc_store.save(db, tenant_id, to, _nc_state)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    from core.outbound_send_status import (  # noqa: PLC0415
                        sync_outbound_body_to_final as _sync_nc_body,
                    )

                    _sync_nc_body(
                        db,
                        tenant_id=tenant_id,
                        recipient=to,
                        final_body=reply,
                        reason="native_catalog_sent",
                        outbound_text_policy=(
                            _outbound_text_tracker.to_metadata()
                            if _outbound_text_tracker is not None
                            else None
                        ),
                    )
                except Exception:  # noqa: BLE001  # noqa: silent-ok — dashboard sync must not block send
                    pass
            else:
                _nc_fallback = None
                try:
                    from core.native_catalog_capability import (  # noqa: PLC0415
                        invalidate_meta_catalog_publish_for_retailer_id,
                    )
                    from core.native_catalog_fallback import (  # noqa: PLC0415
                        compose_native_catalog_failure_decision,
                    )
                    from modules.ai.brain.commerce.selection_context import (  # noqa: PLC0415
                        apply_selection_context_patch,
                    )
                    from modules.ai.brain.state.store import DefaultStateStore  # noqa: PLC0415

                    _nc_store = DefaultStateStore()
                    _nc_state = _nc_store.load(db, tenant_id, to)
                    apply_selection_context_patch(
                        _nc_state,
                        {
                            "native_catalog_send_failed": True,
                            "catalog_navigation_source": "top_fallback",
                        },
                    )
                    _nc_store.save(db, tenant_id, to, _nc_state)
                    if _native_send_result.reason == "meta_products_not_found":
                        invalidate_meta_catalog_publish_for_retailer_id(
                            db,
                            int(tenant_id),
                            str(_native_catalog_entry.get("thumbnail_product_retailer_id") or ""),
                            catalog_id=str(_native_catalog_entry.get("catalog_id") or ""),
                        )
                    _nc_fallback = compose_native_catalog_failure_decision(
                        db,
                        tenant_id,
                        failure_reason=_native_send_result.reason,
                        customer_message=text or reply or "",
                    )
                    reply = str(_nc_fallback.text or "").strip()
                    from core.outbound_wire_audit import set_wire_expression  # noqa: PLC0415

                    set_wire_expression(
                        tenant_id, to, reply, "native_catalog_failure_fallback",
                        source="deterministic",
                    )
                except Exception as _nc_fb_exc:  # noqa: BLE001  # noqa: silent-ok — honest fallback must not block webhook reply
                    logger.debug(
                        "[NATIVE_CATALOG] honest_fallback_failed tenant=%s err=%s",
                        tenant_id,
                        _nc_fb_exc,
                    )
                    _nc_fallback = None
                    reply = ""
                if reply:
                    _cta_url = (
                        str(getattr(_nc_fallback, "cta_url", "") or "").strip()
                        if _nc_fallback is not None
                        else ""
                    )
                    _cta_label = (
                        str(getattr(_nc_fallback, "cta_label", "") or "").strip()
                        or "فتح المتجر الإلكتروني"
                    )
                    if _cta_url:
                        _send_ok = await _send_cta_url(
                            phone_id=phone_id,
                            to=to,
                            body_text=reply,
                            btn_label=_cta_label,
                            btn_url=_cta_url,
                            _tenant_id=tenant_id,
                            _db=db,
                        )
                        if not _send_ok:
                            reply = f"{reply}\n{_cta_url}"
                            _send_ok = await _send_whatsapp_message(
                                phone_id=phone_id,
                                to=to,
                                text=reply,
                                _tenant_id=tenant_id,
                                _db=db,
                                _inbound_message_id=wa_msg_id,
                            )
                    else:
                        _send_ok = await _send_whatsapp_message(
                            phone_id=phone_id,
                            to=to,
                            text=reply,
                            _tenant_id=tenant_id,
                            _db=db,
                            _inbound_message_id=wa_msg_id,
                        )
                    if _send_ok and isinstance(_delivery_audit, dict):
                        _delivery_audit["text_sent"] = True
                        _delivery_audit["native_catalog_fallback_text"] = True
                        if _cta_url:
                            _delivery_audit["cta_url_sent_count"] = (
                                int(_delivery_audit.get("cta_url_sent_count", 0)) + 1
                            )
                    if _send_ok and reply:
                        try:
                            from core.outbound_send_status import (  # noqa: PLC0415
                                sync_outbound_body_to_final as _sync_nc_body,
                            )

                            _sync_nc_body(
                                db,
                                tenant_id=tenant_id,
                                recipient=to,
                                final_body=reply,
                                reason="native_catalog_failure_fallback",
                            )
                        except Exception:  # noqa: BLE001  # noqa: silent-ok — dashboard sync must not block send
                            pass
        elif _brain_buttons and reply:
            _outbound_wire_boundary_done = True
            _interactive_sink: Dict[str, Any] = {}
            _send_ok = await _send_interactive_reply(
                phone_id=phone_id, to=to,
                body_text=reply,
                buttons=_brain_buttons,
                _tenant_id=tenant_id, _db=db,
                _result_sink=_interactive_sink,
            )
            if _send_ok and isinstance(_delivery_audit, dict):
                _delivery_audit["interactive_buttons_sent"] = True
                _delivery_audit["text_sent"] = True
            elif not _send_ok and isinstance(_delivery_audit, dict):
                # ── Sept 2026 product-silence incident ──────────────
                # Meta DEFINITIVELY rejected the interactive payload
                # (e.g. HTTP 400 "Duplicate button title"). The guarded
                # text body already survived every truth guard, so send
                # it once as plain text. Ambiguous outcomes (timeout,
                # connection loss, 5xx, malformed 2xx) are NEVER resent:
                # the customer may already have the message.
                try:
                    from core.product_reply_recovery import (  # noqa: PLC0415
                        PROVIDER_DEFINITIVE_REJECTION as _PRR_DEFINITIVE,
                        classify_provider_outcome as _prr_classify,
                        new_recovery_audit_fields as _prr_fields,
                        provider_error_snapshot as _prr_error,
                    )

                    for _prr_k, _prr_v in _prr_fields().items():
                        _delivery_audit.setdefault(_prr_k, _prr_v)
                    _rich_outcome = _prr_classify(_interactive_sink, send_ok=False)
                    _delivery_audit["provider_outcome"] = _rich_outcome
                    _delivery_audit["original_provider_error"] = _prr_error(_interactive_sink)
                    _delivery_audit["interactive_rejected"] = (
                        _rich_outcome == _PRR_DEFINITIVE
                    )
                    logger.warning(
                        "[PRODUCT_TEXT_RECOVERY] tenant=%s to=*%s interactive_outcome=%s "
                        "error_key=%s http_status=%s detail=%r",
                        tenant_id, (to[-4:] if to else ""), _rich_outcome,
                        (_delivery_audit["original_provider_error"] or {}).get("key"),
                        (_delivery_audit["original_provider_error"] or {}).get("http_status"),
                        str((_delivery_audit["original_provider_error"] or {}).get("detail") or "")[:120],
                    )
                    if _rich_outcome == _PRR_DEFINITIVE:
                        _recovery_ok = await _recover_product_reply_with_grounded_text(
                            db=db,
                            tenant_id=tenant_id,
                            phone_id=phone_id,
                            to=to,
                            wa_msg_id=wa_msg_id,
                            convo=convo,
                            guarded_reply=reply,
                            brain_state=_bs_for_nc if isinstance(_bs_for_nc, dict) else {},
                            brain_result=brain_result if isinstance(brain_result, dict) else None,
                            delivery_audit=_delivery_audit,
                            outbound_event_id=_outbound_event_id,
                            base_metadata=_persona_ownership.to_metadata(),
                            trace=_trace,
                            trigger="interactive_rejected",
                        )
                        if _recovery_ok:
                            _send_ok = True
                            _interactive_recovered_as_text = True
                except Exception as _prr_exc:  # noqa: BLE001
                    logger.warning(
                        "[PRODUCT_TEXT_RECOVERY] tenant=%s interactive recovery failed: %s",
                        tenant_id, _prr_exc,
                    )
        elif (reply or "").strip():
            _outbound_wire_boundary_done = True
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
            _reply_before_cta = reply or ""
            _keep_textual_url = False
            try:
                from core.wa_link_buttons import (  # noqa: PLC0415
                    customer_requested_textual_url as _want_text_url,
                    split_text_for_cta_buttons as _split_cta,
                )
                _keep_textual_url = bool(_want_text_url(text or ""))
                # We don't pass store_domain here: product detection by
                # path pattern (/products/, /p/, …) is enough for the
                # current AI-reply shapes. A future enhancement can plug
                # the merchant's known domain in for stricter matching.
                if not _keep_textual_url:
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
                                _inbound_message_id=wa_msg_id,
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
                    if _outbound_text_tracker is not None:
                        _outbound_text_tracker.set_cta_delivery(
                            pre_cta_body=_reply_before_cta or "",
                            body_after_cta=_msg.body or "",
                            cta_url=_cls.url,
                            cta_label=_cls.button_title,
                        )
                    try:
                        from core.outbound_send_status import (  # noqa: PLC0415
                            sync_outbound_body_to_final as _sync_cta_body,
                        )

                        _sync_cta_body(
                            db,
                            tenant_id=tenant_id,
                            recipient=to,
                            final_body=reply,
                            reason="post_cta_normalization",
                            cta_metadata={
                                "body_after_cta": _msg.body or "",
                                "cta_url": _cls.url,
                                "cta_label": _cls.button_title,
                                "pre_cta_body": _reply_before_cta,
                                "url_type": _cls.kind,
                            },
                            outbound_text_policy=(
                                _outbound_text_tracker.to_metadata()
                                if _outbound_text_tracker is not None
                                else None
                            ),
                        )
                    except Exception:  # noqa: BLE001  # noqa: silent-ok — dashboard sync must not block send
                        pass
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
                        _inbound_message_id=wa_msg_id,
                    )
                    if _send_ok and isinstance(_delivery_audit, dict):
                        _delivery_audit["text_sent"] = True
            else:
                # No URLs in reply → plain text send (also handles the
                # degenerate case where the splitter returned only
                # plain-text segments).
                _plain_text_sink: Dict[str, Any] = {}
                _send_ok = await _send_whatsapp_message(
                    phone_id=phone_id, to=to, text=reply,
                    _tenant_id=tenant_id, _db=db,
                    _inbound_message_id=wa_msg_id,
                    _result_sink=_plain_text_sink,
                )
                if _send_ok and isinstance(_delivery_audit, dict):
                    _delivery_audit["text_sent"] = True
                elif isinstance(_delivery_audit, dict):
                    # Telemetry only: keep the ORIGINAL provider outcome so
                    # an ambiguous transport failure is never reported as a
                    # terminal rejection. Plain text is never auto-resent.
                    try:
                        from core.product_reply_recovery import (  # noqa: PLC0415
                            classify_provider_outcome as _prr_classify_text,
                            provider_error_snapshot as _prr_error_text,
                        )

                        _delivery_audit["provider_outcome"] = _prr_classify_text(
                            _plain_text_sink, send_ok=False,
                        )
                        _delivery_audit["original_provider_error"] = _prr_error_text(
                            _plain_text_sink,
                        )
                    except Exception:  # noqa: BLE001  # noqa: silent-ok — telemetry must not block the turn
                        pass
        else:
            # Cards/media dispatch separately below. Do not send an empty
            # standalone text merely because a structured attachment exists.
            _outbound_wire_boundary_done = True
        if _send_ok:
            logger.info("[TRACE][5/6] MERCHANT_AI_SENT | tenant=%s to=%s", tenant_id, to)
            logger.info("[Merchant] replied tenant=%s to=%s", tenant_id, to)
            if _generic_handoff_signal:
                try:
                    from modules.ai.brain.execution.staff_escalation_execution import (  # noqa: PLC0415
                        d2_operational_escalation_succeeded as _d2_vcard_ok,
                        read_current_turn_d2_result as _d2_vcard_read,
                    )

                    _vcard_d2 = _d2_execution_result or _d2_vcard_read(db)
                    if _d2_vcard_ok(_vcard_d2):
                        await _maybe_deliver_generic_handoff_vcard(
                            phone_id=phone_id,
                            to=to,
                            tenant_id=tenant_id,
                            db=db,
                            message=text or "",
                            d2_result=_vcard_d2,
                        )
                except Exception as _gh_vcard_exc:  # noqa: BLE001
                    logger.warning(
                        "[Merchant/HANDOFF_GUARD] post-D2 vCard failed tenant=%s err=%s",
                        tenant_id,
                        type(_gh_vcard_exc).__name__,
                    )
            _outbound_source = (
                "loop_guard_recovery" if _loop_replaced_with_recovery
                else ("brain" if (_brain_active and not MERCHANT_BRAIN_ENABLED_FALLBACK) else "legacy")
            )
            # Mark the turn trace's outbound lock — primary reply went
            # out successfully. Any fallback path that runs AFTER this
            # point (e.g. the outer try/except) will see
            # ``outbound_lock_acquired()=False`` and refuse to send a
            # second message for the same inbound — the merchant's
            # rule:
            #   "إذا تم إرسال outbound reply بنجاح، فيجب إلغاء أي
            #    pending auto_ack لنفس turn/message_id."
            #
            # The trace source mirrors the existing _outbound_source so
            # downstream dashboards stay consistent.
            _trace_src = {
                "brain":  _TS.SOURCE_BRAIN,
                "legacy": _TS.SOURCE_LEGACY,
                "loop_guard_recovery": _TS.SOURCE_BRAIN,
            }.get(_outbound_source, _TS.SOURCE_UNKNOWN)
            _trace.mark_outbound_sent(
                source=_trace_src,
                length=len(reply or ""),
                mode=(
                    _TS.DELIVERY_INTERACTIVE
                    if (_brain_buttons and not _interactive_recovered_as_text)
                    else _TS.DELIVERY_TEXT
                ),
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

        # Deferred memory summarise — after outbound wire-send boundary
        # (attempted send, success or failure, or intentional silent/empty
        # suppress). Customer path must not await summary LLM.
        if _outbound_wire_boundary_done and isinstance(brain_result, dict):
            _mem_sum_payload = brain_result.get("memory_summarise_deferred")
            if isinstance(_mem_sum_payload, dict) and _mem_sum_payload:
                try:
                    from modules.ai.brain.memory.updater import (  # noqa: PLC0415
                        schedule_deferred_memory_summarise,
                    )

                    schedule_deferred_memory_summarise(
                        dict(_mem_sum_payload),
                        request_id=str(getattr(_trace, "message_id", "") or "") or None,
                    )
                except Exception:  # noqa: BLE001  # noqa: silent-ok — deferred summarise fail-open
                    pass

        if _send_ok or _product_attachments or _media_attachments:
            # Dispatch any media library attachments now that the text /
            # interactive reply has been delivered (when present). Card-only
            # visual turns may have no text — attachments still dispatch.
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

            # ── Final dispatch guard — last-chance attachment purge ──
            # Even if an earlier layer queued cards, hard-suppress
            # before visual enforcement and the catalog send loop.
            if not _allow_product_cards:
                try:
                    from services.final_dispatch_guard import (  # noqa: PLC0415
                        suppress_product_attachments as _purge_product_atts,
                    )
                    if _dispatch_decision is not None:
                        _product_attachments, reply = _purge_product_atts(
                            product_attachments=_product_attachments,
                            reply_text=reply or "",
                            decision=_dispatch_decision,
                            tenant_id=tenant_id,
                            had_stale_candidates=bool(_product_attachments),
                        )
                    elif _product_attachments:
                        logger.info(
                            "[PRODUCT_ATTACHMENT_SUPPRESSED] tenant=%s "
                            "reason=%s count=%d ids=%s",
                            tenant_id,
                            _dispatch_guard_reason,
                            len(_product_attachments),
                            [a.get("id") for a in _product_attachments],
                        )
                        _product_attachments = []
                except Exception as _purge_exc:  # noqa: BLE001
                    logger.debug(
                        "[FINAL_DISPATCH_GUARD] purge failed tenant=%s: %s",
                        tenant_id, _purge_exc,
                    )
                    _product_attachments = []

            # ── [VISUAL_PRODUCT_ENFORCEMENT] — May 2026 #6 ────────────
            # Production regression: customer says "أبغى أشوف صورة
            # لعسل السمر" / "ورني السمر" / "أرسل رابط السمر" / "أبي
            # الكتالوج" / "عندك صورة للضهيان؟" and the LLM replies
            # with a TEXT description ("هذا سمر الحجاز إنتاج 1446…").
            # No [PRODUCT:] marker → no product card attached → no
            # image → final_delivery_mode = text_only. The
            # [DELIVERY_GUARD_FAIL] alarm fires but is observation-
            # only; the customer still sees text.
            #
            # The enforcer below intercepts EXACTLY that case:
            #   * the inbound matches a visual-product intent
            #     (closed keyword set in delivery_mode.py +
            #     brain-action signals)
            #   * AND no [PRODUCT:]/[MEDIA_KEY:] marker landed
            #   * AND no library/product attachment is queued
            # → we resolve a best-candidate title from
            # brain_state (current_product_focus →
            # last_search_candidates → last_recommended_products →
            # inbound text fallback) and APPEND a synthetic
            # product_card attachment so the existing
            # dispatch loop (catalog → legacy image+CTA) delivers
            # rich content as a FOLLOW-UP message. The original
            # text reply is preserved verbatim.
            #
            # Scope is intentionally narrow: only fires on visual
            # intents, only when no rich content exists, only
            # attaches ONE card. Zero impact on any other path.
            _enforcement_applied = bool(_visual_enforced_pre_send)
            try:
                from modules.observability import (  # noqa: PLC0415
                    customer_wants_product_or_image as _wants_visual,
                    has_visual_marker as _has_marker,
                    pick_best_candidate_title as _pick_candidate,
                )
                _vp_wants = _wants_visual(
                    inbound_text=text or "",
                    brain_action=_br_action or "",
                )
                _vp_has_marker = _has_marker(reply or "")
                _vp_has_rich = bool(_product_attachments) or any(
                    str(_a.get("media_type") or "").lower().startswith("image")
                    for _a in (_media_attachments or [])
                )
                if _visual_product_escalation_blocked:
                    if _structured_catalog_miss:
                        _vp_skip_reason = "structured_catalog_miss"
                    elif _fulfillment_discovery_blocked:
                        _vp_skip_reason = "fulfillment_lock"
                    elif not _allow_product_cards:
                        _vp_skip_reason = _dispatch_guard_reason
                    else:
                        _vp_skip_reason = "non_commerce_block"
                    logger.info(
                        "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s SKIP "
                        "reason=%s inbound=%r",
                        tenant_id,
                        _vp_skip_reason,
                        (text or "")[:80],
                    )
                elif not _vp_wants:
                    logger.debug(
                        "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s SKIP "
                        "reason=not_visual_intent inbound=%r brain_action=%s",
                        tenant_id, (text or "")[:80], _br_action or "?",
                    )
                elif _vp_has_marker or _vp_has_rich:
                    logger.info(
                        "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s SKIP "
                        "reason=already_rich inbound=%r brain_action=%s "
                        "has_marker=%s product_attachments=%d media_attachments=%d",
                        tenant_id, (text or "")[:80], _br_action or "?",
                        str(_vp_has_marker).lower(),
                        len(_product_attachments or []),
                        len(_media_attachments or []),
                    )
                elif len(_product_attachments or []) >= _catalog_card_limit:
                    logger.info(
                        "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s SKIP "
                        "reason=catalog_card_limit limit=%d count=%d "
                        "inbound=%r brain_action=%s",
                        tenant_id,
                        _catalog_card_limit,
                        len(_product_attachments or []),
                        (text or "")[:80],
                        _br_action or "?",
                    )
                else:
                    _candidate_title, _candidate_source = _pick_candidate(
                        _bs if isinstance(_bs, dict) else {},
                        text or "",
                    )
                    logger.info(
                        "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s TRIGGER "
                        "inbound=%r brain_action=%s candidate=%r source=%s",
                        tenant_id, (text or "")[:80], _br_action or "?",
                        _candidate_title[:80], _candidate_source,
                    )
                    if _candidate_title:
                        try:
                            from services.product_resolver import (  # noqa: PLC0415
                                resolve_by_query as _resolve_query,
                                format_product_card_caption as _vp_caption,
                            )
                            _cust_id_for_aff = None
                            try:
                                _cust_id_for_aff = getattr(convo, "customer_id", None) or None
                            except Exception:
                                _cust_id_for_aff = None
                            _vp_res = _resolve_query(
                                db, tenant_id, _candidate_title,
                                customer_id=_cust_id_for_aff,
                            )
                            if _vp_res and _vp_res.image_url:
                                _product_attachments.append({
                                    "kind":         "product_card",
                                    "id":           _vp_res.id,
                                    "title":        _vp_res.title,
                                    "media_type":   "image",
                                    "file_url":     _vp_res.image_url,
                                    "caption":      _vp_caption(
                                        _vp_res, include_description=False,
                                    ),
                                    "product_url":  _vp_res.product_url,
                                    "price":        _vp_res.price,
                                    "in_stock":     _vp_res.in_stock,
                                    "external_id":  _vp_res.external_id,
                                    "confidence":   _vp_res.confidence,
                                    "_enforced":    True,
                                    "dispatch_source": "visual",
                                    "candidate_origin": _candidate_source,
                                })
                                _enforcement_applied = True
                                logger.info(
                                    "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s "
                                    "ENFORCED product_id=%s title=%r "
                                    "image=%s url=%s source=%s",
                                    tenant_id, _vp_res.id, _vp_res.title,
                                    bool(_vp_res.image_url),
                                    bool(_vp_res.product_url),
                                    _candidate_source,
                                )
                                logger.info(
                                    "[PRODUCT_ATTACHMENT] tenant=%s "
                                    "stage=enforced source=visual_enforcement "
                                    "count=1 ids=[%s] with_image=1 with_url=%d",
                                    tenant_id, _vp_res.id,
                                    1 if _vp_res.product_url else 0,
                                )
                            elif _vp_res and _vp_res.product_url:
                                _product_attachments.append({
                                    "kind":         "product_card",
                                    "id":           _vp_res.id,
                                    "title":        _vp_res.title,
                                    "media_type":   "image",
                                    "file_url":     "",
                                    "caption":      _vp_caption(
                                        _vp_res, include_description=False,
                                    ),
                                    "product_url":  _vp_res.product_url,
                                    "price":        _vp_res.price,
                                    "in_stock":     _vp_res.in_stock,
                                    "external_id":  _vp_res.external_id,
                                    "confidence":   _vp_res.confidence,
                                    "_enforced":    True,
                                    "dispatch_source": "visual",
                                    "candidate_origin": _candidate_source,
                                })
                                _enforcement_applied = True
                                logger.info(
                                    "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s "
                                    "ENFORCED_CTA_ONLY product_id=%s title=%r "
                                    "reason=no_image_url source=%s",
                                    tenant_id, _vp_res.id, _vp_res.title,
                                    _candidate_source,
                                )
                                logger.info(
                                    "[PRODUCT_ATTACHMENT] tenant=%s "
                                    "stage=enforced source=visual_enforcement "
                                    "count=1 ids=[%s] with_image=0 with_url=1",
                                    tenant_id, _vp_res.id,
                                )
                            else:
                                logger.warning(
                                    "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s "
                                    "FALLBACK_TEXT_ONLY reason=resolver_no_match "
                                    "candidate=%r source=%s",
                                    tenant_id, _candidate_title[:80],
                                    _candidate_source,
                                )
                        except Exception as _vp_res_exc:  # noqa: BLE001
                            logger.warning(
                                "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s "
                                "RESOLVER_FAILED candidate=%r err=%s",
                                tenant_id, _candidate_title[:80],
                                _vp_res_exc,
                            )
                    else:
                        logger.warning(
                            "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s "
                            "FALLBACK_TEXT_ONLY reason=no_candidate "
                            "inbound=%r brain_action=%s",
                            tenant_id, (text or "")[:80],
                            _br_action or "?",
                        )
            except Exception as _vp_exc:  # noqa: BLE001
                logger.debug(
                    "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s instrumentation "
                    "failed: %s", tenant_id, _vp_exc,
                )

            # Catalog intelligence Phase 4 — scope product cards to merchant groups.
            if _product_attachments:
                try:
                    from modules.ai.brain.catalog.catalog_product_card_filter import (  # noqa: PLC0415
                        filter_product_card_attachments as _filter_product_cards,
                    )

                    _card_filter = _filter_product_cards(
                        _product_attachments,
                        db=db,
                        tenant_id=tenant_id,
                        message=text or "",
                        query=str((_bs_for_nc or {}).get("last_browse_query") or ""),
                        source=str(_br_action or ""),
                        brain_state=_bs_for_nc if isinstance(_bs_for_nc, dict) else None,
                    )
                    if _card_filter.dropped:
                        logger.info(
                            "[PRODUCT_CARD_FILTER] tenant=%s dropped=%d kept=%d evidence=%s",
                            tenant_id,
                            _card_filter.dropped,
                            len(_card_filter.attachments),
                            _card_filter.evidence,
                        )
                    _product_attachments = _card_filter.attachments
                except Exception as _pcf_exc:  # noqa: BLE001
                    logger.warning(
                        "[PRODUCT_CARD_FILTER] tenant=%s skipped err=%s",
                        tenant_id,
                        _pcf_exc,
                    )

            # LIMIT_RECOMMENDATION_BREADTH — cap stacked catalog cards per turn.
            if (
                _product_attachments
                and len(_product_attachments) > _catalog_card_limit
            ):
                logger.info(
                    "[RECOMMENDATION_BREADTH] tenant=%s trimming product cards "
                    "count=%d limit=%d",
                    tenant_id,
                    len(_product_attachments),
                    _catalog_card_limit,
                )
                _product_attachments = _product_attachments[:_catalog_card_limit]

            # Concatenate library media + product cards into one
            # ordered list so the customer sees them in the same
            # sequence the LLM intended. Library media go FIRST
            # (typically explanatory — payment barcode, certificate)
            # and product cards SECOND so the customer sees the
            # "context" before the "offer".
            _all_attachments = list(_media_attachments) + list(
                _product_attachments  # may be empty
            )

            # ── [PAYMENT_BARCODE_ATTACH] lifecycle probe ─────────────
            try:
                _pbc_attachments = [
                    a for a in (_media_attachments or [])
                    if isinstance(a, dict)
                    and (
                        (a.get("media_key") or "").strip().lower()
                        in ("payment_rajhi_barcode",)
                        or "barcode" in (a.get("media_key") or "").lower()
                        or a.get("payment_barcode_route")
                    )
                ]
                if _pbc_attachments:
                    logger.info(
                        "[PAYMENT_BARCODE_ATTACH] tenant=%s conversation_id=%s "
                        "attachments_count=%d media_keys=%s media_ids=%s "
                        "all_attachments_count=%d",
                        tenant_id,
                        getattr(convo, "id", None),
                        len(_pbc_attachments),
                        [a.get("media_key") for a in _pbc_attachments],
                        [a.get("id") for a in _pbc_attachments],
                        len(_all_attachments),
                    )
            except Exception:  # noqa: BLE001
                pass

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
                    import time as _time_pres  # noqa: PLC0415

                    _t_presentation = _time_pres.monotonic()
                except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                    _t_presentation = None
                try:
                    from database.models import (  # noqa: PLC0415
                        WhatsAppConnection as _WAConn,
                    )
                    _cached_wa_conn = (
                        db.query(_WAConn)
                        .filter(_WAConn.tenant_id == tenant_id)
                        .first()
                    )
                except Exception as _conn_lookup_exc:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                    logger.debug(
                        "[CATALOG] tenant=%s connection lookup failed "
                        "(catalog send will be skipped, legacy path "
                        "will run): %s",
                        tenant_id, _conn_lookup_exc,
                    )

                # ── [CATALOG_ELIGIBILITY] — May 2026 #10 ────────────
                # One structured line per turn that has product cards
                # queued. Mirrors the exact decision the dispatch loop
                # will make for the FIRST product attachment. Operator
                # grepping ``[CATALOG_ELIGIBILITY]`` answers
                # "did the catalog path even get a chance to run, and
                # if not why?". Reasons are the closed set from
                # ``core.catalog.CatalogEligibility.reason``.
                try:
                    from core.catalog import (  # noqa: PLC0415
                        is_catalog_eligible as _is_elig,
                        effective_retailer_id as _eff_rid,
                        catalog_summary as _cat_summary,
                    )
                    _first_att = _product_attachments[0]
                    _summary = _cat_summary(_cached_wa_conn)
                    _elig = _is_elig(_cached_wa_conn, products=[_first_att])
                    logger.info(
                        "[CATALOG_ELIGIBILITY] tenant=%s eligible=%s "
                        "reason=%s catalog_bound=%s catalog_enabled=%s "
                        "meta_catalog_id_set=%s product_id=%s "
                        "retailer_id=%r attachments=%d",
                        tenant_id, str(_elig.ok).lower(), _elig.reason,
                        str(_summary["catalog_bound"]).lower(),
                        str(_summary["catalog_enabled"]).lower(),
                        bool(_summary["meta_catalog_id"]),
                        _first_att.get("id"),
                        _eff_rid(_first_att) or "",
                        len(_product_attachments),
                    )
                except Exception as _elig_log_exc:  # noqa: BLE001
                    logger.debug(
                        "[CATALOG_ELIGIBILITY] tenant=%s instrumentation "
                        "failed: %s", tenant_id, _elig_log_exc,
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
                    _att_source = str(
                        _att.get("dispatch_source")
                        or ("safety_net" if _att.get("safety_net") else "queue")
                    )
                    try:
                        from services.final_dispatch_guard import (  # noqa: PLC0415
                            log_final_product_send_attempt as _log_product_send,
                            record_fail_closed_product_suppression,
                            validate_product_attachment_for_send as _validate_product_send,
                        )
                        _send_ok, _send_reason = _validate_product_send(
                            inbound_message=text or "",
                            attachment=_att,
                            brain_state=_bs_for_nc,
                            intent_name=_intent_for_nc or "",
                            brain_action=_br_action or "",
                            dispatch_allowed=_allow_product_cards,
                        )
                        _focus_bs = (_bs_for_nc or {}).get("current_product_focus") or {}
                        _log_product_send(
                            tenant_id=tenant_id,
                            product=str(_att.get("title") or ""),
                            allow=_send_ok,
                            reason=_send_reason,
                            source=_att_source,
                            candidate_origin=str(
                                _att.get("candidate_origin") or _att_source
                            ),
                            focus_title=str(_focus_bs.get("title") or ""),
                            focus_id=str(_focus_bs.get("id") or ""),
                            inbound_preview=(text or "")[:80],
                        )
                    except Exception as _vps_exc:  # noqa: BLE001
                        _send_ok = _allow_product_cards
                        _send_reason = f"validate_error:{_vps_exc}"
                        logger.debug(
                            "[FINAL_PRODUCT_SEND_ATTEMPT] tenant=%s validate skipped: %s",
                            tenant_id, _vps_exc,
                        )
                    if not _allow_product_cards or not _send_ok:
                        if _allow_product_cards and not _send_ok:
                            record_fail_closed_product_suppression(
                                attachment=_att,
                                delivery_audit=(
                                    _delivery_audit
                                    if isinstance(_delivery_audit, dict)
                                    else None
                                ),
                                reason=_send_reason,
                            )
                        else:
                            logger.info(
                                "[PRODUCT_ATTACHMENT_SUPPRESSED] tenant=%s "
                                "reason=%s product_id=%s stage=dispatch_loop "
                                "source=%s",
                                tenant_id,
                                _send_reason if not _send_ok else _dispatch_guard_reason,
                                _att.get("id"),
                                _att_source,
                            )
                        continue
                    try:
                        _catalog_sent = await _try_send_catalog_product(
                            db=db,
                            connection=_cached_wa_conn,
                            tenant_id=tenant_id,
                            phone_id=phone_id,
                            to=to,
                            attachment=_att,
                            block_commerce_escalation=_commerce_blocked,
                            positive_commerce_intent=_positive_commerce,
                            delivery_audit=(
                                _delivery_audit
                                if isinstance(_delivery_audit, dict)
                                else None
                            ),
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
                        # True Meta catalog product message only — never count
                        # deferred variant prompts as catalog_card_sent.
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
                    if (
                        isinstance(_delivery_audit, dict)
                        and _delivery_audit.get("membership_fail_closed")
                    ):
                        try:
                            from core.fail_closed_visual_presentation import (  # noqa: PLC0415
                                commit_fail_closed_rewrite as _commit_fc_visual,
                                rewrite_queued_card_after_membership_fail_closed as _rewrite_fc_visual,
                            )

                            _rewritten = _rewrite_fc_visual(
                                db,
                                tenant_id,
                                _att,
                                brain_state=(
                                    _bs_for_nc if isinstance(_bs_for_nc, dict) else None
                                ),
                                audit=_delivery_audit,
                            )
                            _rewrite_ok = _commit_fc_visual(_att, _rewritten)
                        except Exception as _rw_exc:  # noqa: BLE001
                            logger.warning(
                                "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s "
                                "fail_closed_rewrite_failed product_id=%s err=%s",
                                tenant_id,
                                _att.get("id"),
                                _rw_exc,
                            )
                            _att["file_url"] = ""
                            _att["product_url"] = ""
                            _att["caption"] = ""
                            _att["title"] = ""
                            _att["fail_closed_visual_suppressed"] = True
                            _rewrite_ok = False
                        if not _rewrite_ok:
                            logger.warning(
                                "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s "
                                "FALLBACK_TEXT_ONLY reason=no_safe_bound_visual "
                                "product_id=%s",
                                tenant_id,
                                _att.get("id"),
                            )
                            if isinstance(_delivery_audit, dict):
                                _delivery_audit["fail_closed_visual_suppressed"] = True
                            continue
                    _image_url = str(_att.get("file_url") or "").strip()
                    _product_url = _safe_cta_http_url(_att.get("product_url"))
                    _factual_body = _product_card_factual_body(_att)

                    # Option A: image + URL → ONE interactive.cta_url
                    # (header image + factual body + button). No canned
                    # «اضغط زر…» bubble and no standalone image payload.
                    if _image_url and _product_url:
                        if isinstance(_delivery_audit, dict):
                            _delivery_audit["unified_product_card_attempted_count"] = (
                                int(
                                    _delivery_audit.get(
                                        "unified_product_card_attempted_count", 0
                                    )
                                    or 0
                                )
                                + 1
                            )
                        _unified_ok = False
                        try:
                            _unified_ok = await _send_cta_url(
                                phone_id=phone_id,
                                to=to,
                                body_text=_factual_body,
                                btn_label="عرض المنتج",
                                btn_url=_product_url,
                                header_image_url=_image_url,
                                _tenant_id=tenant_id,
                                _db=db,
                            )
                        except Exception as _unified_exc:  # noqa: BLE001
                            logger.warning(
                                "[ProductCard.unified] tenant=%s product_id=%s "
                                "failed: %s",
                                tenant_id,
                                _att.get("id"),
                                _unified_exc,
                            )
                        if _unified_ok and isinstance(_delivery_audit, dict):
                            _delivery_audit["unified_product_card_sent_count"] = (
                                int(
                                    _delivery_audit.get(
                                        "unified_product_card_sent_count", 0
                                    )
                                    or 0
                                )
                                + 1
                            )
                        logger.info(
                            "[ProductCard.send] tenant=%s to=%s product_id=%s "
                            "ext_id=%s ok=%s confidence=%s mode=unified_cta",
                            tenant_id,
                            to,
                            _att.get("id"),
                            _att.get("external_id"),
                            _unified_ok,
                            _att.get("confidence"),
                        )
                        await _maybe_send_variant_prompt_after_product_card(
                            db=db,
                            tenant_id=tenant_id,
                            phone_id=phone_id,
                            to=to,
                            attachment=_att,
                            delivery_audit=(
                                _delivery_audit
                                if isinstance(_delivery_audit, dict)
                                else None
                            ),
                        )
                        continue

                    if not _image_url:
                        logger.info(
                            "[ProductCard.send] tenant=%s product_id=%s "
                            "SKIPPED reason=no_image_url url=%s",
                            tenant_id, _att.get("id"),
                            bool(_product_url),
                        )
                        # URL-only: CTA with factual body (no canned instruction).
                        if _product_url:
                            try:
                                _cta_only_ok = await _send_cta_url(
                                    phone_id=phone_id, to=to,
                                    body_text=_factual_body,
                                    btn_label="عرض المنتج",
                                    btn_url=_product_url,
                                    _tenant_id=tenant_id, _db=db,
                                )
                                if _cta_only_ok and isinstance(_delivery_audit, dict):
                                    _delivery_audit["cta_url_sent_count"] = (
                                        int(_delivery_audit.get("cta_url_sent_count", 0)) + 1
                                    )
                            except Exception:  # noqa: silent-ok — CTA-only fallback must not block card loop
                                pass
                        await _maybe_send_variant_prompt_after_product_card(
                            db=db,
                            tenant_id=tenant_id,
                            phone_id=phone_id,
                            to=to,
                            attachment=_att,
                            delivery_audit=_delivery_audit if isinstance(_delivery_audit, dict) else None,
                        )
                        continue
                    # Image-only (no usable URL): fall through to media send.
                elif _validate_media is not None:
                    _ok, _why, _normed = _validate_media(
                        _att, expected_tenant_id=tenant_id, db=db,
                    )
                    if not _ok:
                        logger.warning(
                            "[AIMedia.validate] tenant=%s id=%s SKIPPED reason=%s",
                            tenant_id, _att.get("id"), _why,
                        )
                        if (_att.get("media_key") or "").strip().lower().endswith(
                            "_barcode"
                        ) or "barcode" in (str(_att.get("media_key") or "").lower()):
                            logger.info(
                                "[PAYMENT_BARCODE_SEND] tenant=%s conversation_id=%s "
                                "calling_send_media=false media_key=%s media_id=%s "
                                "reason=%s storage_kind=%s storage_path=%s "
                                "file_url=%s",
                                tenant_id,
                                getattr(convo, "id", None),
                                _att.get("media_key") or "-",
                                _att.get("id") or "-",
                                _why or "-",
                                _att.get("storage_kind") or "-",
                                (_att.get("storage_path") or "-")[:120],
                                (_att.get("file_url") or "-")[:120],
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
                if (
                    not _is_product
                    and (
                        (_att.get("media_key") or "").strip().lower().endswith("_barcode")
                        or "barcode" in (str(_att.get("media_key") or "").lower())
                    )
                ):
                    logger.info(
                        "[PAYMENT_BARCODE_SEND] tenant=%s conversation_id=%s "
                        "calling_send_media=true media_key=%s media_id=%s "
                        "media_type=%s file_url=%s",
                        tenant_id,
                        getattr(convo, "id", None),
                        _att.get("media_key") or "-",
                        _att.get("id") or "-",
                        _media_type_norm,
                        (_att.get("file_url") or "-")[:120],
                    )
                    logger.info(
                        "[PAYMENT_BARCODE_PROVIDER] tenant=%s conversation_id=%s "
                        "payload_type=%s media_url=%s",
                        tenant_id,
                        getattr(convo, "id", None),
                        _media_type_norm,
                        (_att.get("file_url") or "-")[:120],
                    )
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
                        if (
                            not _is_product
                            and (
                                (_att.get("media_key") or "").strip().lower().endswith(
                                    "_barcode"
                                )
                                or "barcode" in (
                                    str(_att.get("media_key") or "").lower()
                                )
                            )
                        ):
                            logger.info(
                                "[PAYMENT_BARCODE_PROVIDER_RESPONSE] tenant=%s "
                                "conversation_id=%s payload_type=%s status=%s",
                                tenant_id,
                                getattr(convo, "id", None),
                                _media_type_norm,
                                "ok" if _media_ok else "failed",
                            )

                    # ── OUTBOUND_MEDIA_ATTACH (May 2026 #29) ─────────────────
                    # Structured per-attachment audit line. Distinct from
                    # the legacy ``[AIMedia.send]`` line because it
                    # carries ``media_key`` + ``conversation_id`` — the
                    # two fields needed to diagnose "the customer asked
                    # for the Rajhi barcode but never got an image". This
                    # is wrapped in try/except so a logging failure never
                    # disrupts the send loop.
                    try:
                        logger.info(
                            "[OUTBOUND_MEDIA_ATTACH] tenant_id=%s "
                            "conversation_id=%s media_key=%s media_id=%s "
                            "media_type=%s safety_net=%s sent=%s reason=%s",
                            tenant_id,
                            getattr(convo, "id", None),
                            _att.get("media_key") or "-",
                            _att.get("id") or "-",
                            _media_type_norm,
                            bool(_att.get("safety_net")),
                            "true" if _media_ok else "false",
                            "delivered" if _media_ok
                            else "send_returned_false",
                        )
                    except Exception:  # noqa: BLE001
                        pass

                    # Product image+URL uses unified cta_url earlier in this
                    # loop. Image-only products intentionally skip CTA here.
                except Exception as _media_send_exc:
                    logger.warning(
                        "[AIMedia.send] tenant=%s id=%s failed: %s",
                        tenant_id, _att.get("id"), _media_send_exc,
                    )
                    try:
                        logger.info(
                            "[OUTBOUND_MEDIA_ATTACH] tenant_id=%s "
                            "conversation_id=%s media_key=%s media_id=%s "
                            "media_type=%s safety_net=%s sent=false "
                            "reason=exception:%s",
                            tenant_id,
                            getattr(convo, "id", None),
                            _att.get("media_key") or "-",
                            _att.get("id") or "-",
                            _media_type_norm,
                            bool(_att.get("safety_net")),
                            type(_media_send_exc).__name__,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                if _is_product:
                    await _maybe_send_variant_prompt_after_product_card(
                        db=db,
                        tenant_id=tenant_id,
                        phone_id=phone_id,
                        to=to,
                        attachment=_att,
                        delivery_audit=_delivery_audit if isinstance(_delivery_audit, dict) else None,
                    )

                try:
                    if _t_presentation is not None:
                        import time as _time_pres2  # noqa: PLC0415
                        from core.turn_latency import safe_record_ms  # noqa: PLC0415

                        safe_record_ms(
                            "presentation",
                            (_time_pres2.monotonic() - _t_presentation) * 1000.0,
                        )
                except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                    pass

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
                        customer_message=text or "",
                        delivery_path="call_marker",
                    )
                    if _contacts_ok and isinstance(_delivery_audit, dict):
                        _delivery_audit["contacts_sent"] = True
                    if _contacts_ok:
                        try:
                            from modules.ai.brain.commerce.contact_escalation import (  # noqa: PLC0415
                                persist_staff_contacts_sent_batch,
                            )
                            _persist_turn = int(
                                ((_bs_for_nc or {}).get("turn") or 0)
                            )
                            _contact_entries = [
                                {
                                    "name": getattr(_ct, "name", "") or "",
                                    "phone": (
                                        getattr(_ct, "phone_display", "")
                                        or getattr(_ct, "wa_id", "")
                                        or ""
                                    ),
                                    "turn": _persist_turn,
                                }
                                for _ct in _call_targets
                            ]
                            persist_staff_contacts_sent_batch(
                                db,
                                tenant_id=tenant_id,
                                phone=to,
                                entries=_contact_entries,
                            )
                        except Exception as _ces_exc:  # noqa: BLE001  # noqa: silent-ok — contact persist after send is best-effort
                            logger.debug(
                                "[CONTACT_ESCALATION] persist after send "
                                "failed tenant=%s err=%s",
                                tenant_id, _ces_exc,
                            )
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
                    # Wave 1 W1.1 (contradiction guard): pass the
                    # conversation id so the structured
                    # ``[PAYMENT_CONTRADICTION_GUARD]`` log line
                    # carries it whenever the guard refuses the flip.
                    _flipped = mark_awaiting_receipt(
                        db,
                        tenant_id=tenant_id,
                        phone=to,
                        conversation_id=getattr(convo, "id", None),
                    )
                    if _flipped:
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
                    DELIVERY_MODE_FAILED as _MODE_FAILED,
                    DELIVERY_MODE_TEXT_ONLY as _MODE_TEXT_ONLY,
                    is_acceptable_mode_for_product_intent as _mode_ok,
                )

                _final_mode = _compute_mode(_delivery_audit)
                _wants = _wants_product(
                    inbound_text=text or "",
                    brain_action=_br_action or "",
                )

                # ── Grounded plain-text recovery (Sept 2026 incident) ──
                # Nothing was provider-accepted (``failed``) although the
                # customer asked for products and this turn's catalog
                # search produced verified candidates: the composed text
                # was emptied by a truth guard and the only queued card
                # was (correctly) rejected as stale/unrelated. Send ONE
                # deterministic list built from the verified candidates.
                # Never after an ambiguous provider outcome, never twice.
                _prr_product_turn = _is_product_reply_turn(
                    wants_product=bool(_wants),
                    decision_action=_br_dec_action,
                    brain_action=_br_action,
                )
                if (
                    _prr_product_turn
                    and _final_mode == _MODE_FAILED
                    and not _brain_handoff
                    and not _social_send_suppressed
                    and not _commerce_blocked
                    and not _structured_catalog_miss
                    and not (reply or "").strip()
                    and not _delivery_audit.get("text_sent")
                    and not _native_catalog_entry.get("thumbnail_product_retailer_id")
                    and _product_discovery_action(_br_dec_action, _br_action)
                    and str(_delivery_audit.get("provider_outcome") or "") not in (
                        "ambiguous", "blocked",
                    )
                ):
                    try:
                        await _recover_product_reply_with_grounded_text(
                            db=db,
                            tenant_id=tenant_id,
                            phone_id=phone_id,
                            to=to,
                            wa_msg_id=wa_msg_id,
                            convo=convo,
                            guarded_reply="",
                            brain_state=_bs_for_nc if isinstance(_bs_for_nc, dict) else {},
                            brain_result=brain_result if isinstance(brain_result, dict) else None,
                            delivery_audit=_delivery_audit,
                            outbound_event_id=_outbound_event_id,
                            base_metadata=_persona_ownership.to_metadata(),
                            trace=_trace,
                            trigger="pre_provider_suppression",
                        )
                    except Exception as _prr_exc:  # noqa: BLE001
                        logger.warning(
                            "[PRODUCT_TEXT_RECOVERY] tenant=%s final-stage recovery failed: %s",
                            tenant_id, _prr_exc,
                        )
                    _final_mode = _compute_mode(_delivery_audit)

                # ── Hard fallback recovery — May 2026 #10 ──────────
                # The [DELIVERY_GUARD_FAIL] log used to be observation
                # only; this block actively RECOVERS the turn. When
                # the customer asked for a product/image/catalog but
                # the final mode is ``text_only`` (catalog + media
                # both fell through OR no product was queued at all),
                # we attempt one last CTA-URL send so the customer
                # leaves the turn with at LEAST a clickable link.
                #
                # Source ladder (first non-empty wins):
                #   1. ``product_url`` on any product attachment that
                #      was queued earlier in the turn (catalog send
                #      failed but the resolver knew where to point).
                #   2. A one-shot resolver pass on the inbound text
                #      itself — covers the case where the visual
                #      enforcer's candidate didn't match but a fresh
                #      query (the customer's exact words) does.
                # Failure here is silent; the [DELIVERY_GUARD_FAIL]
                # log still fires so operators can see we tried.
                _recovered = False
                if (
                    _wants
                    and _final_mode == _MODE_TEXT_ONLY
                    and not _delivery_audit.get("first_send_failed")
                    and _allow_product_cards
                    and not _structured_catalog_miss
                    # A grounded text recovery already answered the turn;
                    # a title-resolved CTA would pick a product the
                    # customer never chose.
                    and not _delivery_audit.get("product_text_recovery_sent")
                ):
                    _rescue_url = ""
                    _rescue_title = ""
                    for _att in (_product_attachments or []):
                        if _att.get("fail_closed_visual_suppressed"):
                            continue
                        _u = (_att.get("product_url") or "").strip()
                        if _u:
                            _rescue_url = _u
                            _rescue_title = str(_att.get("title") or "")
                            break
                    _block_title_rescue = False
                    try:
                        from core.fail_closed_visual_presentation import (  # noqa: PLC0415
                            extract_structured_product_id as _structured_id,
                            extract_structured_variant_id as _structured_vid,
                            should_block_title_query_substitution as _block_title_sub,
                        )
                        from services.final_dispatch_guard import (  # noqa: PLC0415
                            has_fail_closed_product_suppression as _has_product_suppression,
                        )

                        _focus = (
                            (_bs_for_nc or {}).get("current_product_focus")
                            if isinstance(_bs_for_nc, dict)
                            else None
                        )
                        _pid, _ext = _structured_id(
                            {"id": _delivery_audit.get("canonical_product_id")}
                            if _delivery_audit.get("canonical_product_id") not in (None, "")
                            else None,
                            _focus,
                            _product_attachments,
                        )
                        _vid = _structured_vid(
                            _delivery_audit,
                            _focus,
                            _product_attachments,
                        )
                        _block_title_rescue = _has_product_suppression(
                            _delivery_audit,
                        ) or _block_title_sub(
                            membership_fail_closed=bool(
                                _delivery_audit.get("membership_fail_closed")
                            ),
                            canonical_product_id=_pid,
                            canonical_external_id=_ext,
                            canonical_variant_id=_vid,
                        )
                    except Exception:  # noqa: BLE001  # noqa: silent-ok — fail-closed bind must not crash rescue
                        _block_title_rescue = bool(
                            _delivery_audit.get("membership_fail_closed")
                        )
                    if not _rescue_url and _block_title_rescue:
                        logger.warning(
                            "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s FALLBACK_TEXT_ONLY "
                            "reason=no_safe_bound_visual inbound=%r",
                            tenant_id,
                            (text or "")[:80],
                        )
                    elif not _rescue_url and (text or "").strip():
                        try:
                            # Unstructured visual asks only. After membership
                            # fail-closed, or when a structured product id is
                            # already known, title/inbound FTS must not invent
                            # a different SKU (AD-F3-R2B-1).
                            from services.product_resolver import (  # noqa: PLC0415
                                resolve_best_effort as _rescue_resolve,
                            )
                            _r = _rescue_resolve(
                                db, tenant_id, (text or "").strip(),
                                customer_id=getattr(convo, "customer_id", None),
                            )
                            if _r and _r.product_url:
                                _rescue_url = _r.product_url
                                _rescue_title = _r.title or ""
                                logger.info(
                                    "[CATALOG_PRODUCT_RESOLVE] tenant=%s "
                                    "rescue_via=best_effort product_id=%s "
                                    "confidence=%s",
                                    tenant_id, _r.id, _r.confidence,
                                )
                        except Exception as _rescue_exc:  # noqa: BLE001  # noqa: silent-ok — visual rescue best-effort
                            logger.debug(
                                "[VISUAL_FALLBACK_RESCUE] tenant=%s "
                                "resolver_failed: %s", tenant_id, _rescue_exc,
                            )
                    if _rescue_url:
                        try:
                            _rescue_ok = await _send_cta_url(
                                phone_id=phone_id, to=to,
                                body_text=(_rescue_title or "عرض المنتج"),
                                btn_label="عرض المنتج",
                                btn_url=_rescue_url,
                                _tenant_id=tenant_id, _db=db,
                            )
                            if _rescue_ok and isinstance(_delivery_audit, dict):
                                _delivery_audit["cta_url_sent_count"] = (
                                    int(_delivery_audit.get("cta_url_sent_count", 0)) + 1
                                )
                                _recovered = True
                                _final_mode = _compute_mode(_delivery_audit)
                                logger.info(
                                    "[VISUAL_FALLBACK_RECOVERED] tenant=%s "
                                    "to=*%s title=%r url=%s new_mode=%s",
                                    tenant_id,
                                    (to[-4:] if to else ""),
                                    _rescue_title[:80], bool(_rescue_url),
                                    _final_mode,
                                )
                        except Exception as _cta_rescue_exc:  # noqa: BLE001
                            logger.warning(
                                "[VISUAL_FALLBACK_RESCUE] tenant=%s "
                                "cta_send_failed: %s",
                                tenant_id, _cta_rescue_exc,
                            )
                    else:
                        logger.warning(
                            "[VISUAL_FALLBACK_NO_PRODUCT] tenant=%s "
                            "to=*%s inbound=%r — no rescue URL found, "
                            "guard will fire",
                            tenant_id, (to[-4:] if to else ""),
                            (text or "")[:80],
                        )

                logger.info(
                    "[FINAL_DELIVERY] tenant=%s to=*%s mode=%s "
                    "wants_product_or_image=%s brain_action=%s "
                    "recovered=%s audit=%s",
                    tenant_id,
                    (to[-4:] if to else ""),
                    _final_mode,
                    str(bool(_wants)).lower(),
                    _br_action or "?",
                    str(bool(_recovered)).lower(),
                    _delivery_audit,
                )
                try:
                    from modules.ai.brain.commerce.presentation_mode import (  # noqa: PLC0415
                        log_presentation_mode_dispatch_shadow as _log_pm_shadow,
                    )

                    _pm_mode = ""
                    if isinstance(_bs_for_nc, dict):
                        _pm_mode = str(
                            _bs_for_nc.get("last_presentation_mode") or ""
                        ).strip()
                    _log_pm_shadow(
                        tenant_id=tenant_id,
                        presentation_mode=_pm_mode,
                        delivery_audit=_delivery_audit,
                        brain_action=_br_action or "",
                        inbound_preview=text or "",
                    )
                except Exception as _pm_shadow_exc:  # noqa: BLE001  # noqa: silent-ok — dispatch shadow is observability-only
                    logger.debug(
                        "[PRESENTATION_MODE_SHADOW] tenant=%s skipped: %s",
                        tenant_id, _pm_shadow_exc,
                    )
                _catalog_fact_meta = (
                    dict(_brain_persona_compose_event)
                    if isinstance(_brain_persona_compose_event, dict)
                    else None
                )
                if _wants and not _mode_ok(
                    _final_mode,
                    audit=_delivery_audit,
                    brain_action=_br_action or "",
                    catalog_fact_meta=_catalog_fact_meta,
                    reply_body=reply or "",
                ):
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
                _record_product_reply_delivery_outcome(
                    delivery_audit=_delivery_audit,
                    final_mode=_final_mode,
                    product_reply_turn=_prr_product_turn,
                    tenant_id=tenant_id,
                    to=to,
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
                _catalog_fact_meta = (
                    dict(_brain_persona_compose_event)
                    if isinstance(_brain_persona_compose_event, dict)
                    else None
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
                # ── Grounded plain-text recovery (Sept 2026 incident) ──
                # Same contract as the main dispatch block: a product turn
                # whose composed text was emptied and that queued no card
                # at all lands here with NOTHING provider-accepted.
                _prr_product_turn = _is_product_reply_turn(
                    wants_product=bool(_wants),
                    decision_action=_br_dec_action,
                    brain_action=_br_action,
                )
                if (
                    _prr_product_turn
                    and not _brain_handoff
                    and not _social_send_suppressed
                    and not _commerce_blocked
                    and not _structured_catalog_miss
                    and not (reply or "").strip()
                    and not _delivery_audit.get("text_sent")
                    and not _native_catalog_entry.get("thumbnail_product_retailer_id")
                    and _product_discovery_action(_br_dec_action, _br_action)
                    and str(_delivery_audit.get("provider_outcome") or "") not in (
                        "ambiguous", "blocked",
                    )
                ):
                    try:
                        _prr_ok = await _recover_product_reply_with_grounded_text(
                            db=db,
                            tenant_id=tenant_id,
                            phone_id=phone_id,
                            to=to,
                            wa_msg_id=wa_msg_id,
                            convo=convo,
                            guarded_reply="",
                            brain_state=_bs_for_nc if isinstance(_bs_for_nc, dict) else {},
                            brain_result=brain_result if isinstance(brain_result, dict) else None,
                            delivery_audit=_delivery_audit,
                            outbound_event_id=_outbound_event_id,
                            base_metadata=_persona_ownership.to_metadata(),
                            trace=_trace,
                            trigger="pre_provider_suppression",
                        )
                        if _prr_ok:
                            _delivery_audit["first_send_failed"] = False
                    except Exception as _prr_exc:  # noqa: BLE001
                        logger.warning(
                            "[PRODUCT_TEXT_RECOVERY] tenant=%s failed-branch recovery failed: %s",
                            tenant_id, _prr_exc,
                        )
                    _final_mode = _compute_mode(_delivery_audit)
                if _wants and not _mode_ok(
                    _final_mode,
                    audit=_delivery_audit,
                    brain_action=_br_action or "",
                    catalog_fact_meta=_catalog_fact_meta,
                    reply_body=reply or "",
                ):
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
                _record_product_reply_delivery_outcome(
                    delivery_audit=_delivery_audit,
                    final_mode=_final_mode,
                    product_reply_turn=_prr_product_turn,
                    tenant_id=tenant_id,
                    to=to,
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
        #
        # The fallback text used to be the hardcoded
        # "وصلت رسالتك ✅ سيتم الرد عليك في أقرب وقت." — which lies to
        # the customer that a human will respond. We now route through
        # ``fallback_policy`` so informational questions get an honest
        # "ask to re-phrase" reply instead.
        #
        # ── Session-poisoning recovery (May 2026 #19) ───────────────
        # Same rationale as the inner brain_exc arm: roll back the
        # session FIRST so the diagnostic + fallback persistence
        # don't inherit a poisoned transaction and crash with a
        # cascade of ``InFailedSqlTransaction`` errors that masks the
        # real root cause.
        try:
            db.rollback()
        except Exception as _rb_outer_exc:  # noqa: BLE001
            logger.warning(
                "[Merchant] rollback after outer exc failed | tenant=%s rb_err=%s",
                tenant_id, _rb_outer_exc.__class__.__name__,
            )
        try:
            from core.conversation_engine import _diag_sql_error  # noqa: PLC0415
            logger.error(
                "[Merchant] outer_exc diag | tenant=%s | %s",
                tenant_id, _diag_sql_error(exc, db=db),
            )
        except Exception:  # noqa: BLE001
            pass
        logger.exception(
            "[Merchant] Error generating reply for tenant=%s | inbound=%r",
            tenant_id, (text or "")[:200],
        )
        # Defence in depth: if the primary path already sent something
        # for this turn (e.g. brain reply succeeded then a downstream
        # delivery-guard raised) we skip the outer fallback to avoid
        # a double-message.
        if not _trace.outbound_lock_acquired():
            logger.warning(
                "[Merchant] outer-fallback suppressed (outbound already sent) | tenant=%s to=%s",
                tenant_id, to,
            )
        else:
            # ── Rule-based handoff promotion on the OUTER except path ─
            # Even when the brain raised, an explicit "أبي أتكلم مع
            # أحد" must still land in the merchant's "طلب موظف"
            # filter — otherwise the customer types the same phrase
            # twice and gets only generic "حصل خطأ مؤقت" copies. We
            # run the deterministic rule classifier here, and when it
            # fires we:
            #   1. Create the handoff session + flip needs_human /
            #      handoff_active.
            #   2. Override the fallback reply with the canonical
            #      handoff message instead of asking the customer to
            #      resend a message they already sent.
            #   3. Pause the AI so subsequent inbounds don't keep
            #      triggering the same broken brain branch.
            _outer_handoff_text: Optional[str] = None
            try:
                from modules.ai.brain.intent.rules import (  # noqa: PLC0415
                    match as _outer_rules_match,
                )
                from modules.ai.brain.types import (  # noqa: PLC0415
                    INTENT_TALK_HUMAN as _OUTER_INTENT_TALK_HUMAN,
                )
                _outer_rule_intent = _outer_rules_match(text or "")
                if (
                    _outer_rule_intent is not None
                    and getattr(_outer_rule_intent, "name", "") == _OUTER_INTENT_TALK_HUMAN
                    and float(getattr(_outer_rule_intent, "confidence", 0.0) or 0.0) >= 0.85
                ):
                    try:
                        from handoff.manager import create_handoff_session  # noqa: PLC0415
                        from models import Conversation, Customer  # noqa: PLC0415
                        from core.order_flow import (  # noqa: PLC0415
                            _find_conversation_by_phone as _outer_find_conv,
                            _normalize_e164 as _outer_norm_e164,
                        )
                        _outer_e164 = _outer_norm_e164(to) or to
                        _outer_conv = _outer_find_conv(
                            db, tenant_id=int(tenant_id),
                            phones=(_outer_e164, to),
                            Conversation=Conversation, Customer=Customer,
                        )
                        create_handoff_session(
                            db, tenant_id, to, to, text or "",
                            reason="customer_request",
                            context_snapshot={"source": "outer_exception"},
                        )
                        if _outer_conv is not None:
                            _outer_conv.is_human_handoff  = True
                            _outer_conv.needs_human       = True
                            _outer_conv.handoff_active    = True
                            db.flush()
                        # May 2026 #46 — no automatic pause_ai on the
                        # outer-exception handoff path either. The
                        # advisory flags above surface the request to
                        # staff (dashboard "طلب موظف" filter); the
                        # brain keeps responding to follow-up questions
                        # on the next inbound. Manual pause from the
                        # dashboard remains the only kill-switch.
                        # Canonical handoff copy so the customer
                        # doesn't think the request was lost. We use
                        # the same wording as the brain's `T.handoff`
                        # variant 0 — kept inline to avoid importing
                        # the responder during an error path.
                        _outer_handoff_text = (
                            "تمام، راح يتواصل معك أحد فريقنا في أقرب وقت 🌷"
                        )
                        logger.info(
                            "[Merchant] outer-exc handoff promoted | "
                            "tenant=%s to=%s rule_conf=%.2f",
                            tenant_id, to,
                            float(getattr(_outer_rule_intent, "confidence", 0.0) or 0.0),
                        )
                    except Exception as _outer_ho_exc:  # noqa: BLE001
                        logger.warning(
                            "[Merchant] outer-exc handoff create failed | "
                            "tenant=%s err=%s",
                            tenant_id, _outer_ho_exc,
                        )
            except Exception as _outer_rule_exc:  # noqa: BLE001
                logger.debug(
                    "[Merchant] outer-exc rule match failed | tenant=%s err=%s",
                    tenant_id, _outer_rule_exc,
                )

            try:
                from services.fallback_policy import (  # noqa: PLC0415
                    FALLBACK_REASON_OUTER_EXCEPTION,
                    choose_intent_aware_fallback as _choose_intent_aware,
                )
                # Outer-except runs AFTER an indeterminate amount of
                # pipeline work; we may or may not have store
                # knowledge available depending on which layer
                # raised. Try cheaply for shipping info; if anything
                # blows up we still get a valid fallback (the
                # policy delegates to soft_retry when no
                # deterministic answer applies).
                _outer_ship_info: Dict[str, Any] = {}
                try:
                    from core.store_knowledge import (  # noqa: PLC0415
                        build_merchant_context as _bmc_outer,
                    )
                    _mctx_outer = _bmc_outer(db, tenant_id, customer_phone=to)
                    _pol_outer = (_mctx_outer or {}).get("policies") or {}
                    if isinstance(_pol_outer, dict):
                        _outer_ship_info = {
                            "shipping_methods": _pol_outer.get("shipping_methods") or [],
                            "shipping_notes":   _pol_outer.get("shipping_notes")   or "",
                            "shipping_policy":  _pol_outer.get("shipping_policy")  or "",
                            "delivery_areas":   _pol_outer.get("delivery_areas")   or [],
                        }
                except Exception:  # noqa: BLE001
                    _outer_ship_info = {}
                _outer_decision = _choose_intent_aware(
                    text or "",
                    reason=FALLBACK_REASON_OUTER_EXCEPTION,
                    store_has_live_agent=False,
                    shipping_info=_outer_ship_info,
                )
            except Exception:  # noqa: BLE001 — policy import shouldn't fail, but never crash here
                from services.fallback_policy import FallbackDecision as _FD  # type: ignore[unused-ignore]  # noqa: PLC0415
                _outer_decision = _FD(
                    text="حصل خطأ مؤقت 🙏 ممكن تعيد رسالتك؟",
                    kind="neutral_retry", response_goal="retry",
                )
            _trace.fallback_source = _outer_decision.kind
            _trace.response_goal   = _outer_decision.response_goal
            _fallback_text         = _outer_decision.text
            # When the rule classifier promoted this turn to handoff,
            # send the handoff acknowledgement instead of the generic
            # neutral-retry copy. The flags / pause have already been
            # applied above so the conversation stops looping the
            # broken brain branch.
            if _outer_handoff_text:
                _fallback_text = _outer_handoff_text
                _trace.fallback_source = "outer_exception_handoff_promoted"
            logger.info(
                "[FALLBACK_POLICY] tenant=%s to=%s kind=%s goal=%s "
                "rationale=outer_exception_path",
                tenant_id, to, _outer_decision.kind, _outer_decision.response_goal,
            )
            # ── [AI_TEMP_ERROR_FALLBACK] (May 2026 #42) ────────────────
            # Outer-exception path: SOMETHING in the request lifecycle
            # raised before the brain reply could land. ``exc`` is the
            # outer exception — pin it on the log so on-call can grep
            # for the exception class and find the real culprit
            # (DB connection drop / KB load failure / OpenAI timeout /
            # JSON decode error / etc.) without re-paging through
            # generic ``[Merchant] Error generating reply`` lines.
            try:
                from services.fallback_policy import (  # noqa: PLC0415
                    STAGE_OUTER_EXCEPTION as _STG_OUTER_EXC,
                    emit_temp_error_fallback_log as _emit_temp_err_outer,
                )
                _emit_temp_err_outer(
                    tenant_id=tenant_id,
                    conversation_id=getattr(convo, "id", None) if 'convo' in locals() else None,
                    sender=to or "",
                    inbound_msg_id=str(wa_msg_id or ""),
                    msg_type=str(getattr(_trace, "msg_type", "") or "text"),
                    intent=str(getattr(_trace, "intent", "") or ""),
                    stage=_STG_OUTER_EXC,
                    exception=exc,
                    fallback_kind=str(_outer_decision.kind),
                    response_goal=str(_outer_decision.response_goal),
                    extra={
                        "handoff_promoted": bool(_outer_handoff_text),
                    },
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                from core.outbound_wire_audit import set_wire_expression  # noqa: PLC0415

                set_wire_expression(
                    tenant_id, to, _fallback_text, "outer_exception_fallback",
                    source="deterministic",
                )
                await _send_whatsapp_message(
                    phone_id=phone_id, to=to,
                    text=_fallback_text,
                    _tenant_id=tenant_id, _db=db,
                )
                _trace.mark_outbound_sent(
                    source=_TS.SOURCE_OUTER_EXCEPTION,
                    length=len(_fallback_text),
                )
                try:
                    from routers.conversations import record_outbound_message  # noqa: PLC0415
                    record_outbound_message(
                        db, tenant_id, to, _fallback_text,
                        event_type="ai_fallback",
                        extra={"is_ai": True, "fallback_kind": _outer_decision.kind},
                    )
                except Exception:  # noqa: BLE001
                    pass
            except Exception as send_exc:  # noqa: BLE001
                _trace.outbound_error = send_exc.__class__.__name__
                logger.exception(
                    "[Merchant] Fallback send also failed | tenant=%s to=%s",
                    tenant_id, to,
                )
    finally:
        if _wire_audit_token is not None:
            from core.outbound_wire_audit import reset_wire_audit  # noqa: PLC0415

            reset_wire_audit(_wire_audit_token)
        # Emit ONE structured turn-trace line, no matter how the
        # function exited. ``emit()`` is wrapped in its own try/except
        # internally — observability MUST NOT take down the response
        # path under any circumstance.
        try:
            from modules.ai.brain.truth_surface.trusted_context import (  # noqa: PLC0415
                clear_trusted_context,
            )

            clear_trusted_context()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — context cleanup must not block emit
            pass
        # This runs however the function exited, and the comment above is the
        # contract: observability must not take down the response path. It was
        # the one call here that could, and an exception escaping this handler
        # does not stay local — both provider entry points acknowledge before
        # processing, so it abandons the rest of an already-acknowledged batch:
        # the next message in a Meta batch, and the status receipts after it.
        try:
            _sync_persona_observability()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — observability never fails a turn
            logger.warning(
                "[PERSONA_OBSERVABILITY] final sync failed tenant=%s to=%s — "
                "the turn and the rest of its batch continue",
                tenant_id, to,
            )
        try:
            # Finalize turn_timing snapshot onto the trace for metadata merge.
            from core.turn_latency import get_turn_latency  # noqa: PLC0415

            _tl = get_turn_latency()
            if _tl is not None:
                snap = _tl.snapshot(finalize_total=True)
                _trace.extra["turn_timing_snapshot"] = snap
                _tl.emit_log()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            pass
        try:
            _trace.emit()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            pass


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
        return payload.reply_text.strip() or _empty_reply_fallback()
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


def _classify_owned_cod_button(db, *, tenant_id, sender: str, button_payload: str,
                               button_text: str, context_wamid: str):
    """Whether this template-button tap is one of *this tenant's* COD sends.

    Two ways in, and both of them are classification: a canonical owned payload,
    or a correlation of a noncanonical payload against the tenant's recent COD
    sends using the ``context.id`` the customer's client echoed back. The second
    reads this tenant's order and message state to reach its verdict, which is
    why it is a competing owner's decision and not a formatting detail — the
    caller decides ownership before either of them runs.

    Returns ``(owned_payload, correlation)``; an empty payload means this tap is
    not a COD reply. Never raises.
    """
    try:
        from services.cod_confirmation import (  # noqa: PLC0415
            is_owned_cod_button_payload,
            resolve_owned_cod_button_payload_from_context,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("[Webhook] COD template-button import failed: %s", exc)
        return "", "unavailable"
    try:
        if is_owned_cod_button_payload(button_payload):
            return button_payload, "payload"
        resolved = resolve_owned_cod_button_payload_from_context(
            db, tenant_id=tenant_id, customer_phone=sender,
            button_text=button_text, context_wamid=context_wamid,
        ) or ""
        if resolved and is_owned_cod_button_payload(resolved):
            return resolved, "context_wamid"
    except Exception as exc:  # noqa: BLE001 - an unclassifiable tap is not a COD reply
        logger.error("[Webhook] COD template-button classification failed: %s", exc)
    return "", "none"


def _commerce_runtime_claims_inbound(db, *, tenant_id, phone_id: str, to: str,
                                     text: str, wa_msg_id) -> bool:
    """Whether the commerce runtime owns this inbound, asked before any owner acts.

    Thin, fail-closed wrapper: anything it cannot establish is ``False``, which
    leaves the dispatcher's own short circuits exactly as they are today.
    """
    try:
        from services.commerce_runtime_pilot import (  # noqa: PLC0415
            commerce_runtime_claims_inbound,
        )

        return commerce_runtime_claims_inbound(
            db, tenant_id=tenant_id, phone_id=phone_id, to=to, text=text,
            wa_msg_id=wa_msg_id,
        )
    except Exception as exc:  # noqa: BLE001 - an undecidable claim is not a claim
        logger.warning(
            "[COMMERCE_RUNTIME_PILOT] ownership claim unavailable tenant=%s err=%s",
            tenant_id, type(exc).__name__,
        )
        return False


def _commerce_runtime_claim_holds(claim: Any, *, tenant_id: Any, to: str,
                                  wa_msg_id: Any) -> bool:
    """Whether a claim carried from the dispatcher is about *this* turn.

    A claim is an assertion that one specific inbound message belongs to the
    commerce runtime, so it is honoured only for that message. Anything that is
    not a claim, or is a claim about a different tenant, recipient or provider
    message id, holds nothing.
    """
    if claim is None:
        return False
    applies = getattr(claim, "applies_to", None)
    if not callable(applies):
        logger.error("[COMMERCE_RUNTIME_PILOT] ignoring an unscoped ownership claim tenant=%s",
                     tenant_id)
        return False
    held = bool(applies(tenant_id=tenant_id, recipient=to, provider_message_id=wa_msg_id))
    if not held:
        logger.error(
            "[COMMERCE_RUNTIME_PILOT] ignoring a claim established for another turn tenant=%s",
            tenant_id,
        )
    return held


def _duplicate_is_unfinished_runtime_work(
    *, phone_number_id: Optional[str], sender: Optional[str], msg_id: Optional[str],
) -> bool:
    """Whether a duplicate the platform is about to drop must be let through.

    The commerce runtime keys a turn by the inbound provider message id, so a
    turn it admitted and never finished can only be reached by the event that
    carries that id — a provider retry, or a recovery replay of an inbound the
    platform accepted and nobody admitted. Both are asked about here, for an
    allowlisted tenant and recipient only: an unfinished turn, or a durable
    acceptance record still pending. It sends nothing, writes nothing, and
    answers ``False`` for anything it cannot establish, which leaves
    deduplication exactly as it is today.
    """
    try:
        from core.commerce_runtime.recovery import (  # noqa: PLC0415
            duplicate_carries_unfinished_work,
        )

        return duplicate_carries_unfinished_work(
            phone_number_id=phone_number_id, customer_phone=sender,
            provider_message_id=msg_id,
        ) is not None
    except Exception as exc:  # noqa: BLE001 - a duplicate stays a duplicate
        logger.warning(
            "[COMMERCE_RUNTIME_RECOVERY] duplicate check failed msg_id=%s err=%s",
            msg_id, type(exc).__name__,
        )
        return False


async def _post_wa(
    phone_id: str,
    payload: dict,
    _tenant_id: Optional[int] = None,
    _store_name: str = "unknown",
    _db=None,
    _allow_manual: bool = False,
    _blocked_path: str = "post_wa",
    _treat_dedup_as_success: bool = True,
    _result_sink: Optional[Dict[str, Any]] = None,
) -> bool:
    from core.outbound_wire_audit import observe_wire_payload  # noqa: PLC0415

    _payload_kind = (payload.get("interactive") or {}).get("type") or payload.get("type") or "unknown"
    observe_wire_payload(_tenant_id, payload, "whatsapp_payload_assembly:" + str(_payload_kind))
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
            db=_db,
            skip_handoff_scrub=bool(_allow_manual),
        )
        if isinstance(payload, dict) and payload.pop("_nahla_suppress_send", None):
            logger.warning(
                "[HANDOFF_PROMISE_SCRUBBED] suppressing send tenant=%s to=%s "
                "path=%s",
                _tenant_id,
                _recipient,
                _blocked_path or "post_wa",
            )
            return False
        if _sanitised:
            # Mark the payload so callers/observers can tell at a
            # glance that this message was rewritten. Stored under
            # an internal underscore-prefixed key that
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

    observe_wire_payload(_tenant_id, payload, "outbound_payload_sanitizer")
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

        if _db is not None and _tenant_id and recipient and not _allow_manual:
            try:
                from core.ai_disabled_gate import (  # noqa: PLC0415
                    evaluate_ai_disabled_send_block,
                )

                _send_blocked, _ = evaluate_ai_disabled_send_block(
                    _db,
                    tenant_id=int(_tenant_id),
                    customer_phone=recipient,
                    blocked_path=_blocked_path or "post_wa",
                    allow_manual=_allow_manual,
                )
                if _send_blocked:
                    return False
            except Exception as _send_gate_exc:  # noqa: BLE001  # noqa: silent-ok
                from core.handoff_truth import evaluate_gate_error_fail_closed  # noqa: PLC0415

                if evaluate_gate_error_fail_closed(
                    _db,
                    tenant_id=int(_tenant_id),
                    customer_phone=recipient,
                    gate=_blocked_path or "post_wa",
                    error=_send_gate_exc,
                ):
                    return False
                logger.warning(
                    "[AI_DISABLED_SEND_BLOCK] pre_send check failed tenant=%s err=%s",
                    _tenant_id,
                    _send_gate_exc,
                )

        if _db is not None and _tenant_id and not _allow_manual:
            try:
                from core.wa_usage import check_limit as _check_conv_quota  # noqa: PLC0415

                _quota = _check_conv_quota(_db, int(_tenant_id), category="service")
                if not _quota.allowed:
                    logger.info(
                        "[CONVERSATION_LIMIT] post_wa blocked tenant=%s to=%s "
                        "used=%s limit=%s reason=%s path=%s",
                        _tenant_id,
                        recipient,
                        _quota.used_total,
                        _quota.limit,
                        _quota.reason,
                        _blocked_path or "post_wa",
                    )
                    return False
            except Exception as _quota_exc:  # noqa: BLE001  # noqa: silent-ok
                logger.warning(
                    "[CONVERSATION_LIMIT] pre_send check failed tenant=%s err=%s",
                    _tenant_id,
                    _quota_exc,
                )

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
                if _result_sink is not None:
                    _result_sink.update(
                        {
                            "classification": "ok",
                            "wamid": _dedup_res.wamid,
                            "duration_ms": 0,
                            "duplicate_suppressed": True,
                        }
                    )
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
                if not _treat_dedup_as_success:
                    return False
                return True
            # in_flight: another worker is mid-POST. Return False
            # without stamping to avoid racing the primary caller.
            logger.info(
                "[WA] duplicate outbound suppressed (in_flight) | "
                "tenant=%s to=%s",
                _tenant_id, recipient,
            )
            return False

        # ── internal-E2E transport capture ───────────────────────────
        # Everything above this line is the real send boundary and stays
        # real: the sanitizer, the dedup decision, the recipient checks.
        # Only the dispatch itself is replaced, and only while a
        # validated acceptance context is installed. The call below is a
        # no-op in production (empty string) and FAIL-CLOSED otherwise —
        # an absent, mismatched or failing sink raises here rather than
        # letting control reach ``provider_send_message``.
        try:
            _captured_delivery_id = capture_outbound_payload(
                egress_kind="whatsapp_provider",
                operation="send_message",
                tenant_id=_tenant_id,
                phone_id=phone_id,
                payload=payload,
            )
        except Exception:
            # ``check_outbound_send`` above already RESERVED this key. A
            # refusal that leaves the reservation standing would make the
            # next identical send — two consecutive fallback lines, say —
            # observe an unfinished attempt instead of a free slot. The
            # reservation is released as a failure (failures are
            # retryable) before the refusal propagates and stops the turn.
            try:
                from core.outbound_dedup import (  # noqa: PLC0415
                    record_outbound_result as _release_captured,
                )

                _release_captured(
                    tenant_id=_tenant_id,
                    recipient=recipient,
                    payload=payload,
                    wamid=None,
                    succeeded=False,
                )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — the refusal below is the outcome; release is best-effort
                pass
            raise
        if _captured_delivery_id:
            # A captured send completes the SAME local lifecycle a
            # dispatched one does, so the dedup cache records a finished
            # success rather than an attempt still in flight.
            try:
                from core.outbound_dedup import (  # noqa: PLC0415
                    record_outbound_result as _record_captured,
                )

                _record_captured(
                    tenant_id=_tenant_id,
                    recipient=recipient,
                    payload=payload,
                    wamid=_captured_delivery_id,
                    succeeded=True,
                )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — dedup bookkeeping must not change the captured outcome
                pass
            if _result_sink is not None:
                _result_sink.update(
                    {
                        "classification": "ok",
                        "wamid": _captured_delivery_id,
                        "duration_ms": 0,
                        "duplicate_suppressed": False,
                        "sent_payload": payload,
                        "transport": "captured",
                    }
                )
            logger.info(
                "[WA] outbound captured (internal E2E, not dispatched) | "
                "tenant=%s to=%s",
                _tenant_id, recipient,
            )
            return True

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
                allow_manual=_allow_manual,
                blocked_path=_blocked_path or "post_wa",
            )
            token_source = ctx.source if ctx else None
            logger.info(
                "[SEND_DEBUG] tenant_id=%s store=%s phone_number_id=%s token_source=%s to=%s",
                _tenant_id, _store_name, phone_id, token_source, payload.get("to", "?"),
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
                if _result_sink is not None:
                    _result_sink.update(
                        {
                            "classification": _classification,
                            "wamid": _wamid,
                            "duration_ms": _duration,
                            "duplicate_suppressed": False,
                            "http_status": (resp_data or {}).get("_nahla_http_status"),
                            "response_body": resp_data,
                            # What left, after every sanitizer. A caller
                            # that must record "this is what the customer
                            # saw" has to read the sent payload back; the
                            # one it handed in is only what it asked for.
                            "sent_payload": payload,
                        }
                    )
                _stamped_id = stamp_outbound_send_status(
                    _db,
                    tenant_id=_tenant_id,
                    recipient=str(payload.get("to") or ""),
                    classification=_classification,
                    response_body=resp_data,
                    wamid=_wamid,
                    operation="send_message",
                    duration_ms=_duration,
                )
                try:
                    from core.turn_latency import (  # noqa: PLC0415
                        get_turn_latency,
                        refresh_turn_latency_on_outbound_message,
                        safe_record_ms,
                    )

                    if _duration is not None:
                        safe_record_ms("provider_send", _duration)
                    refresh_turn_latency_on_outbound_message(
                        _db,
                        get_turn_latency(),
                        message_event_id=_stamped_id,
                    )
                except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                    pass
            except Exception as _stamp_exc:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
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
            except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
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
                    from services.meta_coexistence import is_coexistence_mode  # noqa: PLC0415
                    if wa_conn is not None and is_coexistence_mode(wa_conn):
                        logger.warning(
                            "[WA] auto-register SKIPPED — Meta coexistence "
                            "tenant=%s phone_id=%s",
                            _tenant_id, phone_id,
                        )
                        reg_ok, reg_err = False, "coexistence_skip_register"
                    else:
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
                                allow_manual=_allow_manual,
                                blocked_path=_blocked_path or "post_wa",
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
                            except Exception as _retry_stamp_exc:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
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
                            except Exception as _dedup_rec_exc:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
                                logger.exception(
                                    "[OUTBOUND_DEDUP] record_outbound_result failed "
                                    "tenant=%s recipient=*%s",
                                    _tenant_id,
                                    (recipient or "")[-4:],
                                )
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
                            except Exception:  # noqa: BLE001
                                logger.exception(
                                    "[WA] stamp_outbound_send_status failed tenant=%s",
                                    _tenant_id,
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
            # Transport-level failure (timeout, DNS, TLS, connection reset).
            # The outcome is AMBIGUOUS — the provider may have accepted the
            # message before the connection died — so callers must never
            # auto-resend. Surface that through the result sink.
            if _result_sink is not None:
                _result_sink.update(
                    {
                        "classification": "exception",
                        "wamid": None,
                        "duration_ms": None,
                        "duplicate_suppressed": False,
                        "http_status": None,
                        "response_body": None,
                        "error_text": f"{type(exc).__name__}: {exc}",
                    }
                )
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


async def _try_send_native_catalog_entry(
    *,
    db,
    tenant_id: Optional[int],
    phone_id: str,
    to: str,
    entry: Dict[str, Any],
    fallback_body: str = "",
):
    """Send WhatsApp ``interactive.type=catalog_message`` for browse entry."""
    from services.whatsapp_platform.catalog_sender import (  # noqa: PLC0415
        CatalogSendResult,
        send_catalog_message,
    )

    thumbnail = str(entry.get("thumbnail_product_retailer_id") or "").strip()
    if not thumbnail or tenant_id is None:
        return CatalogSendResult(
            success=False,
            fallback_recommended=True,
            reason="no_retailer_id",
        )
    try:
        from core.native_catalog_capability import load_whatsapp_connection  # noqa: PLC0415
    except Exception as imp_exc:  # noqa: BLE001
        logger.debug(
            "[NATIVE_CATALOG] send helpers unavailable tenant=%s err=%s",
            tenant_id,
            imp_exc,
        )
        return CatalogSendResult(
            success=False,
            fallback_recommended=True,
            reason="connection_missing",
            error=str(imp_exc),
        )

    connection = load_whatsapp_connection(db, int(tenant_id))
    if connection is None:
        logger.info(
            "[NATIVE_CATALOG] native_catalog_entry_fallback tenant=%s reason=connection_missing",
            tenant_id,
        )
        return CatalogSendResult(
            success=False,
            fallback_recommended=True,
            reason="connection_missing",
        )

    catalog_id = str(getattr(connection, "meta_catalog_id", "") or "").strip()
    bound_catalog = str(entry.get("catalog_id") or "").strip()
    if not bound_catalog:
        logger.info(
            "[NATIVE_CATALOG] native_catalog_entry_fallback tenant=%s reason=catalog_id_missing",
            tenant_id,
        )
        return CatalogSendResult(
            success=False,
            fallback_recommended=True,
            reason="catalog_id_missing",
        )
    if not catalog_id:
        logger.info(
            "[NATIVE_CATALOG] native_catalog_entry_fallback tenant=%s reason=catalog_id_missing",
            tenant_id,
        )
        return CatalogSendResult(
            success=False,
            fallback_recommended=True,
            reason="catalog_id_missing",
        )
    if bound_catalog != catalog_id:
        logger.info(
            "[NATIVE_CATALOG] native_catalog_entry_fallback tenant=%s reason=catalog_id_mismatch",
            tenant_id,
        )
        return CatalogSendResult(
            success=False,
            fallback_recommended=True,
            reason="catalog_id_mismatch",
        )
    from core.meta_catalog_membership import load_meta_catalog_membership  # noqa: PLC0415

    membership = load_meta_catalog_membership(
        db,
        tenant_id=int(tenant_id),
        catalog_id=catalog_id,
        retailer_id=thumbnail,
    )
    if membership is None:
        logger.info(
            "[NATIVE_CATALOG] native_catalog_entry_fallback tenant=%s reason=meta_catalog_unverified",
            tenant_id,
        )
        return CatalogSendResult(
            success=False,
            fallback_recommended=True,
            reason="meta_catalog_unverified",
        )

    from modules.ai.brain.commerce.catalog_body_policy import (  # noqa: PLC0415
        resolve_native_catalog_body_text,
    )

    body_text = resolve_native_catalog_body_text(
        context_reply=str(fallback_body or entry.get("body_text") or "").strip(),
        inbound_customer_message="",
    )

    result = await send_catalog_message(
        db,
        connection,
        tenant_id=tenant_id,
        to=to,
        phone_id=phone_id,
        thumbnail_product_retailer_id=thumbnail,
        body_text=body_text,
    )
    if not result.success:
        logger.info(
            "[NATIVE_CATALOG] native_catalog_entry_fallback tenant=%s reason=%s",
            tenant_id,
            result.reason,
        )
        if result.reason == "meta_products_not_found":
            try:
                from core.native_catalog_capability import (  # noqa: PLC0415
                    invalidate_meta_catalog_publish_for_retailer_id,
                )

                invalidate_meta_catalog_publish_for_retailer_id(
                    db,
                    int(tenant_id),
                    str(thumbnail or ""),
                    catalog_id=catalog_id,
                )
            except Exception as inv_exc:  # noqa: BLE001
                logger.debug(
                    "[NATIVE_CATALOG] tenant=%s membership invalidate failed: %s",
                    tenant_id,
                    inv_exc,
                )
    return result


async def _maybe_send_variant_prompt_after_product_card(
    *,
    db,
    tenant_id: Optional[int],
    phone_id: str,
    to: str,
    attachment: Dict[str, Any],
    delivery_audit: Optional[Dict[str, Any]] = None,
) -> bool:
    """Send structured variant selection after product-level rich presentation.

    Complements the product card — does not invent variants, URLs, or checkout.
    Sets ``awaiting_variant_choice`` so draft/order paths stay fail-closed until
    the customer pins a sellable SKU.
    """
    if tenant_id is None or not attachment or attachment.get("kind") != "product_card":
        return False
    if (attachment.get("picked_variant_retailer_id") or "").strip():
        return False
    if not bool(attachment.get("needs_variant_choice")):
        return False
    try:
        from services.catalog_product_orchestrator import (  # noqa: PLC0415
            variant_send_enabled,
        )
        if not variant_send_enabled():
            return False
    except Exception:  # noqa: BLE001
        return False

    try:
        from modules.ai.brain.compose.templates import (  # noqa: PLC0415
            ask_product_variants as _ask_variants,
        )
    except Exception as imp_exc:  # noqa: BLE001
        logger.debug(
            "[CATALOG_VARIANT_PROMPT] tenant=%s helpers unavailable: %s",
            tenant_id, imp_exc,
        )
        return False

    try:
        prompt = _ask_variants(
            {"title": attachment.get("title")},
            list(attachment.get("variants") or []),
        )
        await _send_whatsapp_message(
            phone_id=phone_id, to=to, text=prompt,
            _tenant_id=tenant_id, _db=db,
        )
        logger.info(
            "[CATALOG_VARIANT_PROMPT] tenant=%s product_id=%s "
            "variants=%d — sent_after_product_presentation "
            "card_suppressed=false",
            tenant_id,
            attachment.get("id"),
            len(attachment.get("variants") or []),
        )
        if isinstance(delivery_audit, dict):
            delivery_audit["variant_prompt_sent_count"] = (
                int(delivery_audit.get("variant_prompt_sent_count", 0) or 0) + 1
            )
        try:
            from core.order_flow import apply_state_patch  # noqa: PLC0415
            apply_state_patch(
                db,
                tenant_id=tenant_id,
                phone=to,
                state_patch={
                    "awaiting_variant_choice": True,
                    "pending_variant_product_id": str(attachment.get("id") or ""),
                },
            )
        except Exception as _patch_exc:  # noqa: BLE001
            logger.debug(
                "[CATALOG_VARIANT_PROMPT] tenant=%s state patch "
                "failed (non-fatal): %s",
                tenant_id, _patch_exc,
            )
        return True
    except Exception as _prompt_exc:  # noqa: BLE001
        logger.warning(
            "[CATALOG_VARIANT_PROMPT] tenant=%s after_card prompt failed: %s",
            tenant_id, _prompt_exc,
        )
        return False


async def _try_send_catalog_product(
    *,
    db,
    connection,
    tenant_id: Optional[int],
    phone_id: str,
    to: str,
    attachment: Dict[str, Any],
    block_commerce_escalation: bool = False,
    positive_commerce_intent: bool = False,
    delivery_audit: Optional[Dict[str, Any]] = None,
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
    if tenant_id is None:
        return False
    try:
        from services.whatsapp_platform.catalog_sender import (  # noqa: PLC0415
            send_single_product_message,
        )
        from services.catalog_product_orchestrator import (  # noqa: PLC0415
            ProductCardSendAction,
            catalog_send_retailer_id,
            evaluate_product_card_send,
            log_product_card_decision,
            query_retailer_id_collision_peer_ids,
            resolve_attachment_retailer_id,
            should_attempt_catalog_send,
        )
    except Exception as imp_exc:  # noqa: BLE001
        logger.debug(
            "[CATALOG] tenant=%s helpers unavailable, skipping catalog send: %s",
            tenant_id, imp_exc,
        )
        return False

    product_row = None
    if db is not None and attachment.get("id"):
        try:
            from database.models import Product  # noqa: PLC0415
            product_row = (
                db.query(Product)
                .filter(
                    Product.id == attachment.get("id"),
                    Product.tenant_id == tenant_id,
                )
                .first()
            )
        except Exception as q_exc:  # noqa: BLE001
            logger.debug(
                "[CATALOG] tenant=%s product lookup failed (will fall "
                "back to attachment external_id): %s",
                tenant_id, q_exc,
            )

    try:
        candidate_rid = resolve_attachment_retailer_id(attachment, product_row)
        peer_ids: List[int] = []
        if db is not None and candidate_rid and attachment.get("id"):
            peer_ids = query_retailer_id_collision_peer_ids(
                db,
                tenant_id=int(tenant_id),
                retailer_id=candidate_rid,
                exclude_product_id=int(attachment.get("id")),
                limit=2,
            )
        membership = None
        catalog_id = str(getattr(connection, "meta_catalog_id", "") or "").strip()
        if db is not None and catalog_id and candidate_rid:
            try:
                from core.meta_catalog_membership import (  # noqa: PLC0415
                    load_meta_catalog_membership,
                )

                membership = load_meta_catalog_membership(
                    db,
                    tenant_id=int(tenant_id),
                    catalog_id=catalog_id,
                    retailer_id=candidate_rid,
                )
            except Exception as mem_exc:  # noqa: BLE001
                logger.debug(
                    "[CATALOG] tenant=%s membership load failed: %s",
                    tenant_id,
                    mem_exc,
                )
                membership = None
        decision = evaluate_product_card_send(
            tenant_id=int(tenant_id),
            connection=connection,
            attachment=attachment,
            product_row=product_row,
            collision_peer_ids=peer_ids or None,
            block_commerce_escalation=block_commerce_escalation,
            positive_commerce_intent=positive_commerce_intent,
            membership=membership,
        )
        log_product_card_decision(
            decision, tenant_id=tenant_id, attachment=attachment,
        )
    except Exception as orch_exc:  # noqa: BLE001
        logger.debug(
            "[CATALOG] tenant=%s orchestrator decision failed: %s",
            tenant_id, orch_exc,
        )
        return False

    if decision.action == ProductCardSendAction.VARIANT_PROMPT:
        # Meta catalog retailer binding stays blocked until a variant is
        # picked (wrong-SKU safety). Return False so the dispatch loop can
        # still send the product-level rich card (image + trusted URL), then
        # `_maybe_send_variant_prompt_after_product_card` asks for the size.
        logger.info(
            "[CATALOG_VARIANT_PROMPT] tenant=%s product_id=%s "
            "variants=%d — defer_after_product_presentation "
            "meta_catalog_suppressed=true rich_card_allowed=true",
            tenant_id,
            attachment.get("id"),
            len(attachment.get("variants") or []),
        )
        return False

    if not should_attempt_catalog_send(decision):
        try:
            from core.fail_closed_visual_presentation import (  # noqa: PLC0415
                stamp_membership_fail_closed,
            )

            stamp_membership_fail_closed(delivery_audit, decision)
        except Exception:  # noqa: BLE001  # noqa: silent-ok — audit stamp must not block legacy fallback
            pass
        return False

    retailer_id = catalog_send_retailer_id(decision)
    if not retailer_id:
        return False

    logger.info(
        "[CATALOG_MATCH] tenant=%s product_id=%s ext_id=%s "
        "retailer_id=%s catalog_id=%s confidence=%s",
        tenant_id, attachment.get("id"), attachment.get("external_id"),
        retailer_id, getattr(connection, "meta_catalog_id", None),
        attachment.get("confidence"),
    )

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
        logger.error(
            "[CATALOG_FALLBACK_TEXT] tenant=%s product_id=%s "
            "reason=sender_exception err=%s",
            tenant_id, attachment.get("id"), send_exc,
        )
        return False

    if result.success:
        return True

    if getattr(result, "reason", "") == "meta_products_not_found":
        try:
            from core.native_catalog_capability import (  # noqa: PLC0415
                invalidate_meta_catalog_publish_for_retailer_id,
            )

            invalidate_meta_catalog_publish_for_retailer_id(
                db,
                int(tenant_id),
                str(retailer_id or ""),
                catalog_id=str(getattr(connection, "meta_catalog_id", "") or ""),
            )
        except Exception as inv_exc:  # noqa: BLE001
            logger.debug(
                "[CATALOG] tenant=%s membership invalidate failed: %s",
                tenant_id,
                inv_exc,
            )

    logger.info(
        "[CATALOG_FALLBACK_TEXT] tenant=%s product_id=%s reason=%s "
        "err=%s — routing to legacy image+CTA path",
        tenant_id, attachment.get("id"), result.reason, result.error,
    )
    return False


async def _run_and_deliver_commerce_v2_owner(
    **kwargs: Any,
) -> Dict[str, Any]:
    """Correlate the real WhatsApp boundary with SDK-owned runtime spans."""
    from modules.ai.commerce_agent_v2.tracing import (  # noqa: PLC0415
        whatsapp_turn_trace,
    )

    with whatsapp_turn_trace(
        tenant_id=int(kwargs["tenant_id"]),
        conversation_id=int(kwargs["conversation"].id),
        inbound_wamid=str(kwargs["inbound_trace_id"]),
    ):
        return await _run_and_deliver_commerce_v2_owner_impl(**kwargs)


async def _run_and_deliver_commerce_v2_owner_impl(
    *,
    db,
    tenant_id: int,
    conversation,
    customer_phone: str,
    connection,
    phone_id: str,
    inbound_trace_id: str,
    user_input: str,
) -> Dict[str, Any]:
    """Run and deliver one V2-owned turn without any V1 fallback path."""
    from modules.ai.commerce_agent_v2.context import CommerceAgentContext  # noqa: PLC0415
    from modules.ai.commerce_agent_v2.delivery import (  # noqa: PLC0415
        build_commerce_delivery_plan,
    )
    from modules.ai.commerce_agent_v2.runner import (  # noqa: PLC0415
        run_commerce_agent,
        safe_fallback_reply,
    )
    from modules.ai.commerce_agent_v2.shadow import (  # noqa: PLC0415
        persist_commerce_agent_result,
    )
    from modules.ai.commerce_agent_v2.tracing import (  # noqa: PLC0415
        commerce_delivery_span,
    )

    result = None
    try:
        context = CommerceAgentContext.from_trusted_scope(
            db=db,
            tenant_id=int(tenant_id),
            conversation_id=int(conversation.id),
            customer_id=(
                int(conversation.customer_id)
                if getattr(conversation, "customer_id", None) is not None
                else None
            ),
            normalized_customer_phone=normalize_phone(customer_phone),
            connection_id=str(getattr(connection, "id", "") or ""),
            inbound_trace_id=str(inbound_trace_id),
        )
        result = await run_commerce_agent(
            context=context,
            user_input=user_input,
            execution_mode="outbound",
        )
        persist_commerce_agent_result(
            db,
            context,
            result,
            usage_reason="commerce_agent_v2_outbound",
        )
        reply = result.reply
        status = result.status
        model = result.model
        plan = build_commerce_delivery_plan(reply, context.evidence)
    except Exception as exc:  # noqa: BLE001 — fail closed to V2; never transfer to V1
        logger.exception(
            "[COMMERCE_V2_OWNER_FAILED] tenant=%s conversation=%s error=%s "
            "v1_bypassed=true silent_v1_fallback=false",
            tenant_id,
            getattr(conversation, "id", None),
            type(exc).__name__,
        )
        reply = safe_fallback_reply(f"owner_runtime_error:{type(exc).__name__}")
        status = "failed"
        model = ""
        plan = build_commerce_delivery_plan(reply, {})

    text_sent = False
    presentations_sent = 0
    unsupported_presentations = 0
    presented_urls: set[str] = set()
    delivery_receipt: Dict[str, Any] = {}
    for action in plan:
        payload = action.payload
        if action.kind == "text":
            with commerce_delivery_span(
                tenant_id=tenant_id,
                conversation_id=int(conversation.id),
                action_kind="text",
            ):
                text_sent = await _send_whatsapp_message(
                    phone_id=phone_id,
                    to=customer_phone,
                    text=str(payload.get("text") or ""),
                    _tenant_id=tenant_id,
                    _db=db,
                    _inbound_message_id=inbound_trace_id,
                    _blocked_path="commerce_agent_v2_outbound_text",
                    _result_sink=delivery_receipt,
                )
            continue
        if action.kind == "unsupported":
            unsupported_presentations += 1
            logger.warning(
                "[COMMERCE_V2_PRESENTATION_UNSUPPORTED] tenant=%s conversation=%s "
                "capability=%s reason=%s",
                tenant_id,
                getattr(conversation, "id", None),
                payload.get("capability"),
                payload.get("reason"),
            )
            continue

        delivered = False
        if action.kind == "product":
            delivered = await _try_send_catalog_product(
                db=db,
                connection=connection,
                tenant_id=tenant_id,
                phone_id=phone_id,
                to=customer_phone,
                attachment=payload,
                block_commerce_escalation=False,
                positive_commerce_intent=True,
                delivery_audit=None,
            )
            image_url = str(payload.get("file_url") or "").strip()
            product_url = str(payload.get("product_url") or "").strip()
            if not delivered and image_url and product_url:
                delivered = await _send_cta_url(
                    phone_id=phone_id,
                    to=customer_phone,
                    body_text=str(payload.get("caption") or payload.get("title") or ""),
                    btn_label="عرض المنتج",
                    btn_url=product_url,
                    header_image_url=image_url,
                    _tenant_id=tenant_id,
                    _db=db,
                )
            elif not delivered and image_url:
                delivered = await _send_media_message(
                    phone_id,
                    customer_phone,
                    "image",
                    image_url,
                    _tenant_id=tenant_id,
                    _db=db,
                )
            elif not delivered and product_url:
                delivered = await _send_cta_url(
                    phone_id=phone_id,
                    to=customer_phone,
                    body_text=str(payload.get("caption") or payload.get("title") or ""),
                    btn_label="عرض المنتج",
                    btn_url=product_url,
                    _tenant_id=tenant_id,
                    _db=db,
                )
            presented_urls.update(value for value in (image_url, product_url) if value)
        elif action.kind == "image":
            media_url = str(payload.get("url") or "").strip()
            if media_url not in presented_urls:
                delivered = await _send_media_message(
                    phone_id,
                    customer_phone,
                    "image",
                    media_url,
                    _tenant_id=tenant_id,
                    _db=db,
                )
                presented_urls.add(media_url)
        elif action.kind == "ui_action":
            action_url = str(payload.get("url") or "").strip()
            if action_url not in presented_urls:
                delivered = await _send_cta_url(
                    phone_id=phone_id,
                    to=customer_phone,
                    body_text=str(payload.get("label") or ""),
                    btn_label=str(payload.get("label") or "")[:20],
                    btn_url=action_url,
                    _tenant_id=tenant_id,
                    _db=db,
                )
                presented_urls.add(action_url)

        if delivered:
            presentations_sent += 1
        else:
            unsupported_presentations += 1
            logger.warning(
                "[COMMERCE_V2_PRESENTATION_UNSUPPORTED] tenant=%s conversation=%s "
                "capability=%s reason=existing_delivery_path_unavailable",
                tenant_id,
                getattr(conversation, "id", None),
                action.kind,
            )

    if text_sent:
        StateManager.save_message(
            db,
            customer_phone,
            reply.text,
            "outbound",
            conversation_id=conversation.id,
            tenant_id=tenant_id,
            extra_metadata={
                "reply_owner": "commerce_agent_v2",
                "sdk_trace_id": getattr(result, "sdk_trace_id", ""),
                "v2_status": status,
                "v1_bypassed": True,
                "silent_v1_fallback": False,
                "presentation_count": presentations_sent,
                "unsupported_presentation_count": unsupported_presentations,
                "outbound_provider_wamid": delivery_receipt.get("wamid"),
                "outbound_provider_duration_ms": delivery_receipt.get("duration_ms"),
                "outbound_provider_classification": delivery_receipt.get("classification"),
                "delivery_failure_reason": None if text_sent else "delivery_failed",
            },
        )
        db.commit()

    return {
        "status": status,
        "model": model,
        "text_sent": text_sent,
        "presentations_sent": presentations_sent,
        "unsupported_presentations": unsupported_presentations,
        "failure_reason": "" if text_sent else "delivery_failed",
        "outbound_provider_wamid": delivery_receipt.get("wamid"),
    }


_PRODUCT_DISCOVERY_ACTIONS = frozenset({"search_products", "narrow", "recommend_addon"})


def _product_discovery_action(decision_action: Any, brain_action: Any) -> bool:
    """True when THIS turn's brain decision was a product-discovery action.

    ``decision_action`` comes from the current ``brain_result``; the state
    ``last_action`` is only consulted when the result carries no action.
    Stale candidates from an earlier turn are never listed on a turn the
    brain did not decide as product discovery.
    """
    action = str(decision_action or "").strip() or str(brain_action or "").strip()
    return action in _PRODUCT_DISCOVERY_ACTIONS


def _is_product_reply_turn(
    *, wants_product: bool, decision_action: Any, brain_action: Any,
) -> bool:
    """A turn whose customer REQUIRES a product reply (outcome is recorded)."""
    return bool(wants_product) or _product_discovery_action(decision_action, brain_action)


async def _recover_product_reply_with_grounded_text(
    *,
    db,
    tenant_id: int,
    phone_id: str,
    to: str,
    wa_msg_id: Optional[str],
    convo,
    guarded_reply: str,
    brain_state: Optional[Dict[str, Any]],
    brain_result: Optional[Dict[str, Any]],
    delivery_audit: Optional[Dict[str, Any]],
    outbound_event_id: Optional[int],
    base_metadata: Optional[Dict[str, Any]] = None,
    trace=None,
    trigger: str = "pre_provider_suppression",
) -> bool:
    """One-shot grounded plain-text recovery for a product turn (Sept 2026).

    Runs strictly AFTER every truth / commercial-claim / stale-product guard
    and only consumes their verdicts:

    * reuses the guarded LLM text verbatim when it survived; otherwise builds
      the deterministic factual list from THIS turn's verified candidates
      (``core.product_reply_recovery``) — nothing is resolved by title, no
      product is pre-selected for the customer;
    * persists exactly one outbound row when the original reply was
      suppressed before persist (and binds the wire audit to it), otherwise
      re-stamps the existing row with the final provider outcome;
    * attempts the provider once. Ambiguous outcomes are recorded, never
      retried. Returns True only when the provider accepted the text.
    """
    from core.outbound_wire_audit import (  # noqa: PLC0415
        attach_wire_audit_row,
        note_wire_delivery_recovery,
        set_wire_expression,
    )
    from core.product_reply_recovery import (  # noqa: PLC0415
        CHOSEN_PATH_TEXT_RECOVERY,
        choose_recovery_text,
        classify_provider_outcome,
        eligible_recovery_candidates,
        fallback_provenance_metadata,
        new_recovery_audit_fields,
        provider_error_snapshot,
    )

    audit = delivery_audit if isinstance(delivery_audit, dict) else {}
    for key, value in new_recovery_audit_fields().items():
        audit.setdefault(key, value)
    if int(audit.get("text_recovery_attempts") or 0) > 0:
        audit["text_recovery_skip_reason"] = "already_attempted"
        return False

    candidates = eligible_recovery_candidates(brain_state, brain_result=brain_result)
    text, source = choose_recovery_text(guarded_reply=guarded_reply, candidates=candidates)
    if not text:
        audit["text_recovery_skip_reason"] = "no_eligible_candidates"
        logger.warning(
            "[PRODUCT_TEXT_RECOVERY] tenant=%s to=*%s SKIP reason=no_eligible_candidates "
            "trigger=%s",
            tenant_id, (to[-4:] if to else ""), trigger,
        )
        return False

    audit["text_recovery_attempts"] = int(audit.get("text_recovery_attempts") or 0) + 1
    audit["text_recovery_source"] = source
    recovery_info: Dict[str, Any] = {
        "trigger": trigger,
        "text_source": source,
        "candidate_count": len(candidates),
        "original_provider_error": audit.get("original_provider_error"),
    }
    provenance = (
        fallback_provenance_metadata(fallback_reason=trigger)
        if source == "fallback_deterministic"
        else {}
    )

    row_id = outbound_event_id if (type(outbound_event_id) is int and outbound_event_id > 0) else None
    if row_id is None:
        # The original (empty) reply was never persisted — this text is the
        # turn's single outbound row.
        meta: Dict[str, Any] = dict(base_metadata or {})
        meta.update(provenance)
        meta["delivery_recovery"] = dict(recovery_info)
        try:
            row_id = StateManager.save_message(
                db, to, text, "outbound",
                conversation_id=getattr(convo, "id", None),
                tenant_id=tenant_id,
                extra_metadata=meta,
            )
        except Exception as persist_exc:  # noqa: BLE001
            logger.warning(
                "[PRODUCT_TEXT_RECOVERY] tenant=%s outbound persist failed: %s",
                tenant_id, persist_exc,
            )
            row_id = None
        try:
            attach_wire_audit_row(tenant_id, to, row_id, text, meta)
        except Exception:  # noqa: BLE001  # noqa: silent-ok — audit binding must not block recovery
            pass
    try:
        if source == "fallback_deterministic":
            set_wire_expression(
                tenant_id, to, text, CHOSEN_PATH_TEXT_RECOVERY,
                source="deterministic", model_metadata=provenance,
            )
        note_wire_delivery_recovery(tenant_id, to, recovery_info)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — provenance stamp must not block recovery
        pass

    sink: Dict[str, Any] = {}
    ok = await _send_whatsapp_message(
        phone_id=phone_id, to=to, text=text,
        _tenant_id=tenant_id, _db=db,
        _inbound_message_id=wa_msg_id,
        _result_sink=sink,
    )
    audit["text_recovery_provider_outcome"] = classify_provider_outcome(sink, send_ok=ok)
    if ok:
        audit["text_sent"] = True
        audit["product_text_recovery_sent"] = True
        audit["text_recovery_wamid"] = sink.get("wamid")
        audit["text_recovery_duplicate_suppressed"] = bool(sink.get("duplicate_suppressed"))
        if trace is not None:
            try:
                from services import turn_trace as _turn_trace  # noqa: PLC0415

                trace.mark_outbound_sent(
                    source=_turn_trace.SOURCE_BRAIN,
                    length=len(text),
                    mode=_turn_trace.DELIVERY_TEXT,
                )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — turn trace is observability only
                pass
        logger.info(
            "[PRODUCT_TEXT_RECOVERY] tenant=%s to=*%s SENT trigger=%s source=%s "
            "candidates=%d wamid_present=%s duplicate_suppressed=%s row=%s",
            tenant_id, (to[-4:] if to else ""), trigger, source, len(candidates),
            str(bool(sink.get("wamid"))).lower(),
            str(bool(sink.get("duplicate_suppressed"))).lower(),
            row_id,
        )
    else:
        audit["text_recovery_error"] = provider_error_snapshot(sink)
        if audit.get("original_provider_error") is None:
            audit["original_provider_error"] = audit["text_recovery_error"]
        logger.error(
            "[PRODUCT_TEXT_RECOVERY] tenant=%s to=*%s FAILED trigger=%s source=%s "
            "provider_outcome=%s error_key=%s",
            tenant_id, (to[-4:] if to else ""), trigger, source,
            audit["text_recovery_provider_outcome"],
            (audit["text_recovery_error"] or {}).get("key"),
        )
    return bool(ok)


def _record_product_reply_delivery_outcome(
    *,
    delivery_audit: Optional[Dict[str, Any]],
    final_mode: str,
    product_reply_turn: bool,
    tenant_id: int,
    to: str,
) -> None:
    """Closed-enum delivery verdict + explicit lifecycle terminal (Sept 2026).

    ``end_ok`` must not mean "the handler returned": a product turn ends as
    ``end_delivery_recovered`` when text recovery was provider-accepted and
    as ``end_delivery_failed`` when nothing was (ambiguous vs terminal is
    kept apart in the outcome). Never raises.
    """
    if not product_reply_turn or not isinstance(delivery_audit, dict):
        return
    try:
        from core.product_reply_recovery import (  # noqa: PLC0415
            OUTCOME_AMBIGUOUS_PROVIDER,
            OUTCOME_RICH_ACCEPTED,
            OUTCOME_RICH_REJECTED_TEXT_RECOVERED,
            OUTCOME_SUPPRESSED_TEXT_RECOVERED,
            OUTCOME_TERMINAL_FAILURE,
            OUTCOME_TEXT_ACCEPTED,
            PROVIDER_AMBIGUOUS,
            record_product_reply_outcome,
        )
        from modules.observability.delivery_mode import (  # noqa: PLC0415
            DELIVERY_MODE_FAILED,
            DELIVERY_MODE_TEXT_ONLY,
        )

        audit = delivery_audit
        if audit.get("product_text_recovery_sent"):
            outcome = (
                OUTCOME_RICH_REJECTED_TEXT_RECOVERED
                if audit.get("interactive_rejected")
                else OUTCOME_SUPPRESSED_TEXT_RECOVERED
            )
        elif final_mode == DELIVERY_MODE_FAILED:
            outcomes = {
                str(audit.get("provider_outcome") or ""),
                str(audit.get("text_recovery_provider_outcome") or ""),
            }
            outcome = (
                OUTCOME_AMBIGUOUS_PROVIDER
                if PROVIDER_AMBIGUOUS in outcomes
                else OUTCOME_TERMINAL_FAILURE
            )
        elif final_mode == DELIVERY_MODE_TEXT_ONLY and not audit.get(
            "interactive_buttons_sent"
        ):
            outcome = OUTCOME_TEXT_ACCEPTED
        else:
            # catalog / image_cta / media_only / cta_only, or an accepted
            # interactive (buttons) payload — the rich presentation landed.
            outcome = OUTCOME_RICH_ACCEPTED
        record_product_reply_outcome(
            audit,
            outcome=outcome,
            final_mode=final_mode,
            detail=(
                f"trigger={audit.get('text_recovery_source') or '-'} "
                f"skip={audit.get('text_recovery_skip_reason') or '-'}"
            ),
        )
        err = audit.get("original_provider_error") or {}
        logger.info(
            "[PRODUCT_REPLY_OUTCOME] tenant=%s to=*%s outcome=%s mode=%s "
            "recovery_attempts=%s recovery_wamid_present=%s skip=%s "
            "provider_error_key=%s provider_http_status=%s",
            tenant_id, (to[-4:] if to else ""), outcome, final_mode,
            audit.get("text_recovery_attempts"),
            str(bool(audit.get("text_recovery_wamid"))).lower(),
            audit.get("text_recovery_skip_reason") or "-",
            (err or {}).get("key") or "-",
            (err or {}).get("http_status") or "-",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[PRODUCT_REPLY_OUTCOME] tenant=%s recording failed: %s", tenant_id, exc,
        )


async def _send_whatsapp_message(
    phone_id: str, to: str, text: str,
    _tenant_id: Optional[int] = None, _store_name: str = "unknown", _db=None,
    _allow_manual: bool = False,
    _blocked_path: str = "send_whatsapp_message",
    _inbound_message_id: Optional[str] = None,
    _result_sink: Optional[Dict[str, Any]] = None,
) -> bool:
    payload = {
        "messaging_product": "whatsapp", "to": to, "type": "text",
        "text": {"body": text},
    }
    inbound_id = str(_inbound_message_id or "").strip()
    if inbound_id:
        payload["_nahla_inbound_id"] = inbound_id
    return await _post_wa(phone_id, payload, _tenant_id=_tenant_id, _store_name=_store_name, _db=_db,
       _allow_manual=_allow_manual, _blocked_path=_blocked_path, _result_sink=_result_sink)


async def _send_interactive_reply(
    phone_id: str, to: str, body_text: str, buttons: list,
    _tenant_id: Optional[int] = None, _db=None,
    _result_sink: Optional[Dict[str, Any]] = None,
) -> bool:
    wire_buttons = list(buttons or [])[:3]
    try:
        from core.wa_link_buttons import (  # noqa: PLC0415
            whatsapp_reply_buttons_payload as _wa_reply_buttons,
        )

        wire_buttons = _wa_reply_buttons(wire_buttons)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — payload sanitize must not block send
        wire_buttons = list(buttons or [])[:3]
    return await _post_wa(phone_id, {
        "messaging_product": "whatsapp", "to": to, "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {"buttons": wire_buttons[:3]},
        },
    }, _tenant_id=_tenant_id, _db=_db, _result_sink=_result_sink)


async def _send_list_reply(
    phone_id: str, to: str, body_text: str, rows: list, button_label: str,
    _tenant_id: Optional[int] = None, _db=None,
    _result_sink: Optional[Dict[str, Any]] = None,
) -> bool:
    """Interactive list: the surface for more choices than buttons hold.

    Reply buttons stop at three. A list carries up to ten rows, each with
    a description, which is what lets several addresses in the same city
    be told apart. Rows are de-duplicated by id and title for the same
    reason buttons are — the provider rejects the whole payload otherwise.
    """
    wire_rows: list = []
    seen_ids: set = set()
    seen_titles: set = set()
    try:
        from core.product_button_label import normalize_button_title_key  # noqa: PLC0415
    except Exception:  # noqa: BLE001  # noqa: silent-ok — fall back to exact titles
        def normalize_button_title_key(value):  # type: ignore[misc]
            return str(value or "").strip().lower()
    for row in list(rows or []):
        if not isinstance(row, dict):
            continue
        row_id = str(row.get("id") or "").strip()
        title = str(row.get("title") or "").strip()
        title_key = normalize_button_title_key(title)
        if not row_id or not title:
            continue
        if row_id in seen_ids or (title_key and title_key in seen_titles):
            continue
        seen_ids.add(row_id)
        if title_key:
            seen_titles.add(title_key)
        wire_row: Dict[str, Any] = {"id": row_id[:200], "title": title[:24]}
        description = str(row.get("description") or "").strip()
        if description:
            wire_row["description"] = description[:72]
        wire_rows.append(wire_row)
        if len(wire_rows) >= 10:
            break
    if not wire_rows:
        return False
    return await _post_wa(phone_id, {
        "messaging_product": "whatsapp", "to": to, "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body_text},
            "action": {
                "button": (str(button_label or "").strip() or "اختر")[:20],
                "sections": [{"rows": wire_rows}],
            },
        },
    }, _tenant_id=_tenant_id, _db=_db, _result_sink=_result_sink)


def _safe_cta_http_url(url: Optional[str]) -> str:
    """Return http(s) CTA URLs only — Meta forbids tel: on cta_url."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    lower = raw.lower()
    if lower.startswith("tel:"):
        return ""
    if not (lower.startswith("http://") or lower.startswith("https://")):
        return ""
    return raw


def build_cta_url_payload(
    *,
    to: str,
    body_text: str,
    btn_label: str,
    btn_url: str,
    header_image_url: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Build interactive.cta_url payload; optional image header (Option A).

    Returns ``None`` when ``btn_url`` is not a usable http(s) URL.
    """
    safe_url = _safe_cta_http_url(btn_url)
    if not safe_url:
        return None
    body = str(body_text or "").strip() or "."
    interactive: Dict[str, Any] = {
        "type": "cta_url",
        "body": {"text": body[:1024]},
        "action": {
            "name": "cta_url",
            "parameters": {
                "display_text": str(btn_label or "عرض المنتج")[:20],
                "url": safe_url,
            },
        },
    }
    header_img = _safe_cta_http_url(header_image_url)
    if header_img:
        interactive["header"] = {
            "type": "image",
            "image": {"link": header_img},
        }
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": interactive,
    }


def _product_card_factual_body(attachment: Dict[str, Any]) -> str:
    """Trusted product facts for CTA body — never canned press-the-button prose."""
    caption = str(attachment.get("caption") or "").strip()
    if caption:
        return caption[:1024]
    title = str(attachment.get("title") or "").strip()
    if title:
        price = attachment.get("price")
        if price is not None and str(price).strip():
            price_text = str(price).strip()
            if _re_signal.match(r"^\d+(\.\d+)?$", price_text):
                price_text = f"{price_text} ر.س"
            return f"{title}\nالسعر: {price_text}"[:1024]
        return title[:1024]
    return "عرض المنتج"


async def _send_cta_url(
    phone_id: str, to: str, body_text: str,
    btn_label: str, btn_url: str,
    _tenant_id: Optional[int] = None, _db=None,
    *,
    header_image_url: Optional[str] = None,
    keep_textual_url: bool = False,
) -> bool:
    try:
        from core.wa_link_buttons import prepare_cta_body_text  # noqa: PLC0415

        body_text = prepare_cta_body_text(
            body_text or "",
            btn_url or "",
            keep_textual_url=keep_textual_url,
        ) or (body_text or "")
    except Exception:  # noqa: BLE001  # noqa: silent-ok — CTA body prep must not block send
        pass
    payload = build_cta_url_payload(
        to=to,
        body_text=body_text,
        btn_label=btn_label,
        btn_url=btn_url,
        header_image_url=header_image_url,
    )
    if not payload:
        return False
    return await _post_wa(phone_id, payload, _tenant_id=_tenant_id, _db=_db)


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
    *,
    customer_message: str = "",
    delivery_path: str = "",
    intent_name: str = "",
    reply_mentions_staff: bool = False,
    escalation_reason: str = "",
    policy_deliver_contact: bool = False,
) -> bool:
    from modules.ai.brain.commerce.contact_delivery_gate import (  # noqa: PLC0415
        evaluate_contact_delivery_gate,
    )

    gate = evaluate_contact_delivery_gate(
        customer_message=customer_message,
        delivery_path=delivery_path,
        intent_name=intent_name,
        reply_mentions_staff=reply_mentions_staff,
        escalation_reason=escalation_reason,
        policy_deliver_contact=policy_deliver_contact,
    )
    if not gate.allow:
        logger.info(
            "[CONTACT_DELIVERY_GATE] blocked path=%s reason=%s preview=%r",
            delivery_path or "-",
            gate.reason,
            (customer_message or "")[:80],
        )
        return False

    # vCard paths must not treat dedup ``already_sent`` as a fresh
    # provider POST — upstream uses this bool for ``vcard_ok`` evidence.
    return await _post_wa(
        phone_id,
        payload,
        _tenant_id=_tenant_id,
        _db=_db,
        _treat_dedup_as_success=False,
    )


async def _maybe_deliver_generic_handoff_vcard(
    *,
    phone_id: str,
    to: str,
    tenant_id: int,
    db,
    message: str,
    d2_result=None,
) -> None:
    """Preserve configured generic staff vCard after proven D2 execution.

    Detector / decision / chosen_path / brain_handoff are not enough.
    D3 owner/admin contact resolution is not used here.
    """
    from modules.ai.brain.execution.staff_escalation_execution import (  # noqa: PLC0415
        d2_operational_escalation_succeeded,
    )

    if not d2_operational_escalation_succeeded(d2_result):
        return
    if not _staff_call_marker_enabled():
        return
    from modules.ai.brain.commerce.staff_contact_policy import (  # noqa: PLC0415
        evaluate_generic_handoff_contact_policy,
    )

    policy = evaluate_generic_handoff_contact_policy(
        db,
        tenant_id=tenant_id,
        message=message or "",
        customer_phone=to or "",
    )
    if policy is None or not policy.deliver_contact or policy.call_target is None:
        return
    from services.call_resolver import build_contacts_payload  # noqa: PLC0415

    payload = build_contacts_payload([policy.call_target], to=to)
    await _send_contacts_message(
        phone_id=phone_id,
        to=to,
        payload=payload,
        _tenant_id=tenant_id,
        _db=db,
        customer_message=message or "",
        delivery_path="handoff",
        escalation_reason="handoff",
        policy_deliver_contact=True,
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
    """Send the public price card.

    May 2026 #21 — prices shown are the *launch promo* (50% off for two
    months). Original full prices are not shown to the customer to avoid
    early sticker shock; the launch banner is explicit so it doesn't read
    as the steady-state price. Plan names mirror ``recommend_plan``.
    """
    plans_text = (
        "أكيد 🐝\n"
        "أسعار عرض الإطلاق حاليًا:\n\n"
        "Starter — 449 ريال شهريًا\n"
        "Growth  — 849 ريال شهريًا\n"
        "Scale   — 1,499 ريال شهريًا\n\n"
        "العرض بخصم 50٪ لمدة شهرين، وقد يتم تمديده لاحقًا. "
        "وكل الباقات تبدأ بتجربة مجانية 14 يوم بدون بطاقة 🌷"
    )
    await _send_whatsapp_message(
        phone_id=phone_id, to=to, text=plans_text,
        _tenant_id=_tenant_id, _db=db,
    )
    await _send_cta_url(
        phone_id=phone_id, to=to,
        body_text="شوف المقارنة الكاملة بين الباقات 💎",
        btn_label="عرض الباقات كاملة",
        btn_url="https://app.nahlah.ai/billing",
        _tenant_id=_tenant_id, _db=db,
    )


async def _send_plan_details_message(
    phone_id: str, to: str, db=None,
    _tenant_id: Optional[int] = None,
) -> None:
    """Long-form plan descriptions sent on follow-up ("تفاصيل أكثر").

    Reached from DecisionEngine when ``state.last_action == SHOW_PLANS``
    and the customer asks to elaborate. We intentionally do NOT repeat
    the price table here — the customer just saw it — and we end with an
    open question instead of a closing line so the conversation stays
    alive (May 2026 #21).
    """
    details_text = (
        "أكيد 🌷\n"
        "Starter مناسبة للبداية والردود الأساسية على عملاء واتساب وتشمل "
        "الذكاء الاصطناعي وردود الكتالوج والتذكيرات الأساسية.\n\n"
        "Growth للمتاجر النشطة، وتضيف الحملات والأتمتة واسترجاع السلات "
        "المتروكة بشكل أقوى ودعم أوسع للقوالب.\n\n"
        "Scale للعلامات الأكبر، وتشمل مزايا التوسع ودعم أولوية وتكاملات "
        "متقدمة مع سلة وزد وأكثر.\n\n"
        "تبي أرشّح لك الأنسب لمتجرك؟"
    )
    await _send_whatsapp_message(
        phone_id=phone_id, to=to, text=details_text,
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
