"""
services/campaign_dispatcher.py
────────────────────────────────
Bulk campaign dispatch: iterate the audience and send a WhatsApp
template message to each customer **with strict idempotency**.

Why "with strict idempotency" matters
─────────────────────────────────────
The bug we are fixing: a manual marketing campaign that crashes
mid-dispatch (network blip, provider error, server restart) used to
re-iterate the entire audience on the next "Run" click, double-sending
to recipients who already received the message. That is a serious
reputation risk on Meta's tier system and an annoying UX for the
customer.

The new contract:

  1. **Snapshot first.** Before contacting Meta we INSERT a row in
     ``campaign_send_logs`` for every recipient with
     ``status='queued'``. The unique constraint
     ``UNIQUE(tenant_id, campaign_id, customer_phone_e164)`` makes the
     insert idempotent — a re-run silently skips recipients that were
     already snapshotted on a previous attempt.

  2. **Dedupe pass.** Apply the marketing-campaign frequency cap (default
     14 days, env-overridable). Recipients who received a prior
     marketing campaign from the same tenant in the window flip to
     ``status='skipped_duplicate'`` and never reach the provider.

  3. **Batched sends.** Process queued/failed rows in batches of N
     (default 100). Each row transitions
     ``queued/failed → sending → sent/failed`` and a ``sent`` row is
     NEVER touched again unless an admin issues an explicit "force
     resend" (a separate code path that creates a new Campaign).

Scope: this dispatcher handles **manual marketing campaigns** only —
broadcast, promotion, reactivation, etc. Cart recovery
(``core/automation_engine.py``), order messages, generic automations,
and 24h-service replies use their own audit trails and are out of
scope for this log.

Called from:
  - POST /campaigns (when schedule_type == "immediate")
  - PUT  /campaigns/{id}/status (when status → "active")
  - The scheduler loop (for schedule_type == "scheduled" / "delayed")
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import String, and_, cast, func, or_
from sqlalchemy.orm import Session

_THIS = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_THIS, ".."))
_DB = os.path.abspath(os.path.join(_BACKEND, "..", "database"))
for _p in (_BACKEND, _DB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import re as _re

from core.config import (
    MARKETING_CAMPAIGN_BATCH_PAUSE_SECONDS,
    MARKETING_CAMPAIGN_BATCH_SIZE,
    MARKETING_CAMPAIGN_FREQUENCY_CAP_DAYS,
)
from models import (
    Campaign,
    CampaignSendLog,
    Conversation,
    Customer,
    MessageEvent,
    WhatsAppConnection,
    WhatsAppTemplate,
)
from services.meta_errors import is_retryable

logger = logging.getLogger("nahla-backend")

INTER_MESSAGE_DELAY = 1.5

# ── Status constants for campaign_send_logs.status ────────────────────────
# Kept as module-level strings (not Enum) so DB rows stay compatible with
# the open Postgres String column and so we can grep for status names
# across the codebase without IDE help.
LOG_QUEUED              = "queued"
LOG_SENDING             = "sending"
LOG_RETRY_WAITING       = "retry_waiting"
LOG_SENT                = "sent"
LOG_FAILED              = "failed"
LOG_SKIPPED_DUPLICATE   = "skipped_duplicate"
LOG_SKIPPED_INVALID     = "skipped_invalid"
LOG_SKIPPED_UNSUBSCRIBED = "skipped_unsubscribed"
LOG_SKIPPED_UNREACHABLE = "skipped_unreachable"

# ── Retry / circuit-breaker controls ──────────────────────────────────────
# Hard ceiling on how many times a single recipient row may attempt a
# send. After we exhaust attempts we mark the row ``failed`` with
# ``error_code='retry_exhausted'`` so the dispatcher never spins on it
# again. Production was seeing rows accumulate 4000–7000 attempts in
# minutes because failed rows were being re-picked in the same loop;
# this is the hard backstop.
MAX_SEND_ATTEMPTS = 5

# Catastrophic circuit-breaker. If any row crosses this, we don't just
# stop it — we emit a CRITICAL log line tagged
# ``campaign_send_retry_storm`` so support gets paged. Any value above
# this is by definition a runaway worker.
ATTEMPT_CIRCUIT_BREAKER = 100

# A row in ``sending`` is supposed to mean: "an HTTP request to Meta
# is currently in flight for this recipient". If it stays in ``sending``
# longer than this, we treat it as a zombie (worker crashed mid-send,
# event loop cancelled, or DB commit died) and the watchdog reverts
# it to ``queued`` so a future dispatch can pick it up cleanly.
SENDING_TIMEOUT_SECONDS = 300  # 5 minutes

# Exponential backoff between explicit retries (attempt → seconds).
# Used by dispatch-now to schedule the next retry rather than slamming
# Meta immediately. Index = attempt_count when retry is scheduled.
RETRY_BACKOFF_SECONDS: Tuple[int, ...] = (5, 15, 60, 300)
# New: merchant-driven exclusions (drawer toggle "استبعاد من الحملات
# التسويقية" + wizard "استبعد التصنيف X"). Tracked separately from
# customer-driven `skipped_unsubscribed` so the report distinguishes
# "the customer asked us to stop" from "the merchant decided not to
# message them".
LOG_SKIPPED_MANUAL_EXCLUSION = "skipped_manual_exclusion"

# Skip reasons (free-form but kept consistent so the dashboard can
# render an Arabic label for each).
REASON_FREQ_CAP         = "frequency_cap_marketing"
REASON_INVALID_PHONE    = "invalid_phone"
REASON_UNSUBSCRIBED     = "unsubscribed"
REASON_PENDING_OPT_OUT  = "pending_unsubscribe"
REASON_NO_PHONE         = "no_phone"
REASON_MARKETING_OPT_OUT = "marketing_opt_out_manual"
REASON_MANUAL_EXCLUDE   = "excluded_by_manual_segment"
# Delivery Quality Intelligence Layer (May 2026): phones the
# Suppression Engine auto-blocked after repeated quality_risk
# failures (``not_on_whatsapp``, ``invalid_phone``, ``blocked_by_user``,
# …). We log these under a distinct reason so the merchant can tell
# them apart from customer-driven opt-outs in the campaign report.
LOG_SKIPPED_QUALITY_SUPPRESSED = "skipped_quality_suppressed"
REASON_QUALITY_SUPPRESSED = "auto_suppressed_quality"

# Merchant block list (store_settings.blocked_customers). Distinct from
# opt-outs and auto-suppression: the merchant manually marked the phone
# as hard-blocked from every customer touchpoint (AI replies AND
# campaigns). This is the strongest exclusion — it overrides every
# audience-targeting rule. Logged under its own status/reason so the
# campaign debug surface can show the merchant exactly why the row was
# excluded.
LOG_SKIPPED_BLOCKED_CUSTOMER = "skipped_blocked_customer"
REASON_BLOCKED_CUSTOMER = "blocked_customer"


def _normalize_blocked_phone(raw: str) -> str:
    """Return a comparable key for a phone number.

    Prefers full E.164 (via ``utils.phone_utils.normalize_to_e164``) so
    a merchant who typed ``0501234567`` in the block list matches the
    customer's stored ``+966501234567``. Falls back to a digits/plus
    sanitization when libphonenumber isn't available or rejects the
    input, which still lets legacy raw-format entries match.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        from utils.phone_utils import normalize_to_e164  # noqa: PLC0415
        e164 = normalize_to_e164(raw)
        if e164:
            return e164
    except Exception:
        pass
    return "".join(c for c in raw if c.isdigit() or c == "+")


def _load_blocked_phone_set(db: Session, tenant_id: int) -> set:
    """Build the set of E.164/digit-normalized phones the merchant has
    flagged as blocked. Empty list / missing settings → empty set, so
    callers can do a cheap ``if phone in blocked_phones`` membership
    check without conditionals."""
    try:
        from core.tenant import (  # noqa: PLC0415
            DEFAULT_STORE,
            get_or_create_settings,
            merge_defaults,
        )
        settings = get_or_create_settings(db, tenant_id)
        store = merge_defaults(settings.store_settings, DEFAULT_STORE)
        raw = store.get("blocked_customers") or []
    except Exception as exc:
        logger.debug(
            "[campaign_dispatcher] blocked_customers load skipped: %s", exc,
        )
        return set()
    if not isinstance(raw, list):
        return set()
    out: set = set()
    for p in raw:
        norm = _normalize_blocked_phone(str(p))
        if norm:
            out.add(norm)
    return out


def _reconstruct_template_body(
    template: WhatsAppTemplate,
    customer_name: str,
    store_name: str,
    coupon_code: str = "",
) -> str:
    """Render the full template message including buttons, as the customer sees it."""
    slot_values = [customer_name, store_name, coupon_code or store_name,
                   store_name, coupon_code or "", store_name]

    def _sub(m: _re.Match) -> str:
        idx = int(m.group(1)) - 1
        return slot_values[idx] if idx < len(slot_values) else m.group(0)

    parts: list[str] = []

    for comp in (template.components or []):
        ctype = (comp.get("type") or "").upper()

        if ctype == "HEADER":
            fmt = (comp.get("format") or "").upper()
            if fmt == "TEXT" and comp.get("text"):
                header = _re.sub(r"\{\{(\d+)\}\}", _sub, comp["text"])
                parts.append(f"*{header}*")

        elif ctype == "BODY":
            body = _re.sub(r"\{\{(\d+)\}\}", _sub, comp.get("text") or "")
            if body:
                parts.append(body)

        elif ctype == "FOOTER":
            footer = comp.get("text") or ""
            if footer:
                parts.append(footer)

        elif ctype == "BUTTONS":
            btn_lines: list[str] = []
            for btn in (comp.get("buttons") or []):
                btype = (btn.get("type") or "").upper()
                label = btn.get("text") or ""
                if btype == "COPY_CODE":
                    code = coupon_code or "—"
                    btn_lines.append(f"📋 {label or 'نسخ الكود'}: {code}")
                elif btype == "URL":
                    url = btn.get("url") or ""
                    if "{{" in url:
                        btn_lines.append(f"🔗 {label or 'رابط'}")
                    else:
                        btn_lines.append(f"🔗 {label or url}")
                elif btype == "QUICK_REPLY":
                    btn_lines.append(f"↩️ {label}")
                elif btype == "PHONE_NUMBER":
                    btn_lines.append(f"📞 {label}")
                else:
                    if label:
                        btn_lines.append(f"▪️ {label}")
            if btn_lines:
                parts.append("━━━━━\n" + "\n".join(btn_lines))

    return "\n\n".join(parts) if parts else f"[{template.name}]"


def _record_campaign_message(
    db: Session,
    tenant_id: int,
    campaign_id: int,
    customer: Customer,
    phone: str,
    template: WhatsAppTemplate,
    rendered_body: str,
    wa_message_id: str = "",
) -> None:
    """Create a Conversation (if needed) and a MessageEvent via the shared helper."""
    try:
        from routers.conversations import record_outbound_message  # noqa: PLC0415
        record_outbound_message(
            db, tenant_id, phone, rendered_body,
            event_type="campaign",
            customer_name=customer.name or "",
            extra={
                "campaign_id": campaign_id,
                "template_name": template.name,
                "wa_message_id": wa_message_id,
            },
        )
    except Exception as exc:
        logger.warning(
            "[campaign_dispatcher] failed to record message for %s: %s",
            phone, exc,
        )


async def dispatch_campaign(
    db: Session,
    campaign_id: int,
    *,
    only_wave_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Send a campaign's template to every reachable customer with
    strict per-recipient idempotency.

    Pipeline:
      1. Validate campaign / template / connection.
      2. Resolve audience.
      3. Snapshot every recipient into ``campaign_send_logs`` (queued).
      4. Apply the marketing frequency cap (skipped_duplicate).
      5. Mark unreachable / invalid / opted-out rows.
      6. Send ``queued`` + ``failed`` rows in batches.
      7. Recompute summary counters for the campaign report.

    The function is safe to call again on the same ``campaign_id``.
    Already-sent rows are NEVER re-sent — only ``queued`` and
    ``failed`` rows are picked up by step 6.

    Wave-aware dispatch
    ───────────────────
    When ``only_wave_id`` is provided (the Wave Scheduler's normal
    invocation path), the dispatcher SKIPS steps 2–5 entirely
    — the campaign's recipients were already snapshot-ted and
    assigned to waves at launch time by the wave scheduler. Step
    6 then filters its queue by ``wave_id == only_wave_id`` so
    each scheduler tick dispatches only the recipients belonging
    to that wave.

    Legacy ``only_wave_id=None`` retains the historic single-shot
    behaviour for ``send_strategy='immediate'`` campaigns.

    Returns a summary dict including the per-status counters surfaced
    to the merchant in the campaign report.
    """
    from core.acceptance_execution_context import (  # noqa: PLC0415
        current_acceptance_context,
        deny_external_egress,
    )

    if current_acceptance_context() is not None:
        with db.no_autoflush:
            acceptance_tenant_id = (
                db.query(Campaign.tenant_id)
                .filter(Campaign.id == campaign_id)
                .scalar()
            )
        if acceptance_tenant_id is None:
            return _empty_result(error="Campaign not found")
        deny_external_egress(
            egress_kind="campaign",
            operation="dispatch_campaign",
            tenant_id=acceptance_tenant_id,
        )

    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        return _empty_result(error="Campaign not found")

    # Billing first: a tenant without outbound access never takes a lease.
    tenant_id = campaign.tenant_id
    from core.billing import has_billing_access  # noqa: PLC0415
    if not has_billing_access(db, tenant_id):
        err = "billing_access_denied"
        logger.info(
            "[campaign_dispatcher] campaign=%d tenant=%d: outbound blocked (billing_access_denied)",
            campaign_id, tenant_id,
        )
        campaign.status = "failed"
        _persist_dispatch_result(campaign, 0, 0, 0, [err])
        db.commit()
        return _empty_result(error=err)

    # ── One live worker per campaign ─────────────────────────────────
    # The lease is a DB row, so it holds across threads, processes and
    # replicas. A second dispatch-now / scheduler tick while a worker is
    # alive returns here without touching a single recipient.
    from services import campaign_send_ledger as ledger  # noqa: PLC0415

    if not ledger.ledger_available(db):
        # Boot has not created the ledger tables yet (or failed to). Refuse
        # without touching the campaign: nothing is sent and its status is
        # not falsely marked failed; the next tick / click retries.
        result = _empty_result(error="ledger_unavailable")
        result["campaign_id"] = campaign_id
        result["status"] = "ledger_unavailable"
        return result

    ctx = DispatchRunContext(ledger.new_worker_id())
    got = ledger.acquire_lease(
        db, campaign_id=campaign_id, tenant_id=campaign.tenant_id, owner=ctx.owner,
    )
    if not got.acquired:
        logger.warning(
            "[campaign_dispatcher] campaign=%d dispatch skipped: %s (holder=%s until %s)",
            campaign_id, got.reason, got.owner, got.expires_at,
        )
        result = _empty_result(error=got.reason)
        result["campaign_id"] = campaign_id
        result["status"] = "already_running" if got.reason == "held_by_other_worker" else got.reason
        return result
    try:
        return await _dispatch_campaign_with_lease(
            db, campaign_id, ctx, only_wave_id=only_wave_id,
        )
    finally:
        try:
            db.rollback()
            ledger.release_lease(
                db, campaign_id=campaign_id, owner=ctx.owner,
                pause_reason=ctx.pause_reason, pause_detail=ctx.pause_detail,
            )
        except Exception:  # noqa: BLE001
            logger.exception("[campaign_dispatcher] campaign=%d lease release failed", campaign_id)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass


async def _dispatch_campaign_with_lease(
    db: Session,
    campaign_id: int,
    ctx: "DispatchRunContext",
    *,
    only_wave_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Body of :func:`dispatch_campaign`; runs only while ``ctx.owner``
    holds the campaign lease."""
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        return _empty_result(error="Campaign not found")

    tenant_id = campaign.tenant_id
    logger.info(
        "[campaign_dispatcher] starting campaign=%d tenant=%d template_id=%s audience=%s",
        campaign_id, tenant_id, campaign.template_id, campaign.audience_type,
    )

    from core.wa_usage import check_limit  # noqa: PLC0415

    _quota = check_limit(db, tenant_id, category="marketing")
    if not _quota.allowed:
        err = _quota.reason
        logger.info(
            "[campaign_dispatcher] campaign=%d tenant=%d: outbound blocked (%s) "
            "used=%s limit=%s",
            campaign_id,
            tenant_id,
            err,
            _quota.used_total,
            _quota.limit,
        )
        campaign.status = "failed"
        _persist_dispatch_result(
            campaign,
            0,
            0,
            0,
            [f"تم تجاوز حد المحادثات الشهري ({_quota.used_total}/{_quota.limit})"],
        )
        db.commit()
        return _empty_result(error=err)

    template = _load_template(db, campaign)
    if not template:
        err = "لم يتم العثور على القالب أو لم تتم الموافقة عليه"
        logger.warning("[campaign_dispatcher] campaign=%d: template not found or not APPROVED (id=%s)", campaign_id, campaign.template_id)
        campaign.status = "failed"
        _persist_dispatch_result(campaign, 0, 0, 0, [err])
        db.commit()
        return _empty_result(error=err)

    # Campaigns must not require a dashboard visit to discover a tier upgrade.
    from routers.whatsapp_connect import _maybe_refresh_meta_tier  # noqa: PLC0415
    await _maybe_refresh_meta_tier(db, tenant_id, max_age_seconds=900)
    wa_conn = _get_wa_connection(db, tenant_id)
    if not wa_conn:
        err = "لا يوجد اتصال واتساب نشط"
        logger.warning("[campaign_dispatcher] campaign=%d: no active WhatsApp connection", campaign_id)
        campaign.status = "failed"
        _persist_dispatch_result(campaign, 0, 0, 0, [err])
        db.commit()
        return _empty_result(error=err)

    logger.info("[campaign_dispatcher] campaign=%d: WA conn found phone_id=%s", campaign_id, getattr(wa_conn, 'phone_number_id', '?'))

    preflight = validate_template_payload(template, coupon_code="PREFLIGHT")
    if preflight:
        errs_text = " / ".join(preflight)
        logger.warning(
            "[campaign_dispatcher] campaign=%d: pre-flight validation failed: %s",
            campaign_id, errs_text,
        )
        campaign.status = "failed"
        _persist_dispatch_result(campaign, 0, 0, 0, preflight)
        db.commit()
        return _empty_result(error=errs_text)

    # ── Wave-aware fast path ──────────────────────────────────────────
    # When the wave scheduler calls us with ``only_wave_id`` we SKIP
    # audience resolution + snapshot + frequency cap entirely — those
    # ran once at launch time. We re-establish ``customers_by_phone``
    # from the snapshot rows (the wave's queued rows) so the dispatch
    # loop can still personalise messages. Everything downstream from
    # the snapshot (validation already passed above, the dispatch
    # loop, final counters) runs as normal but filtered to the wave.
    if only_wave_id is not None:
        wave_customers = _load_customers_for_wave(
            db, campaign_id=campaign_id, wave_id=only_wave_id,
        )
        customers_by_phone = {
            (c.normalized_phone or ""): c
            for c in wave_customers
            if c.normalized_phone
        }
        manual_coupon = str(getattr(campaign, "coupon_code", "") or "").strip()
        auto_coupon = bool((campaign.template_variables or {}).get("_auto_coupon"))
        discount_pct = (campaign.template_variables or {}).get("_discount_percent")
        store_name = _resolve_store_name(db, tenant_id)
        if auto_coupon or manual_coupon.lower() == "auto":
            manual_coupon = ""

        sent, failed, errors = await _dispatch_queued_rows(
            db,
            campaign=campaign,
            template=template,
            wa_conn=wa_conn,
            store_name=store_name,
            auto_coupon=auto_coupon,
            discount_pct=discount_pct,
            manual_coupon=manual_coupon,
            customers_by_phone=customers_by_phone,
            only_wave_id=only_wave_id,
            ctx=ctx,
        )

        counts = _count_log_statuses(db, campaign_id)
        # Wave runs do NOT flip the campaign's status to terminal —
        # that's the wave scheduler's job once the LAST wave finishes.
        # We only persist incremental counters.
        campaign.sent_count = counts.get("sent", 0)
        campaign.updated_at = datetime.now(timezone.utc)
        if ctx.pause_reason and not ctx.lease_lost:
            campaign.status = "paused"
        _note_capacity_wait(campaign, ctx)
        db.commit()
        return {
            "campaign_id":   campaign_id,
            "wave_id":       only_wave_id,
            "status":        "paused" if ctx.pause_reason else "wave_completed",
            "pause_reason":  ctx.pause_reason,
            "sent":          sent,
            "failed":        failed,
            "errors":        errors[:5],
        }

    # ── Audience funnel — phase 1: raw count (pre-reachability) ────
    # We capture the unfiltered audience count BEFORE reachability
    # filtering so the merchant can see "you targeted 4 customers but
    # 3 had no WhatsApp number". Persisted to template_variables so
    # the debug endpoint can render the funnel without rerunning
    # heavy queries.
    raw_audience_count = 0
    try:
        from services.nahla_segments import (  # noqa: PLC0415
            build_unified_segment_query,
        )
        raw_q = build_unified_segment_query(
            campaign.audience_type, db, tenant_id, require_reachable=False,
        )
        if raw_q is not None:
            raw_audience_count = (
                raw_q.with_entities(func.count(func.distinct(Customer.id)))
                     .scalar()
                or 0
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[campaign_dispatcher] campaign=%d raw audience probe failed: %s",
            campaign_id, exc,
        )

    customers = _resolve_audience(db, tenant_id, campaign.audience_type)
    after_reachable_count = len(customers)
    logger.info(
        "[campaign_dispatcher] campaign=%d funnel: raw=%d "
        "after_reachable=%d (segment=%s)",
        campaign_id, raw_audience_count, after_reachable_count,
        campaign.audience_type,
    )

    # Read manual exclusion list (set by the wizard step 2 "استبعد X").
    # Stored as JSON list under `template_variables._exclude_segments`.
    # The router normalises any non-string value in template_variables
    # to a JSON-encoded string before persisting (because the column
    # is Dict[str, str]), so we accept both shapes here:
    #   * list[str]  — legacy / direct-writer path.
    #   * str        — JSON-encoded list, the new normalised shape.
    tpl_vars_for_excl = campaign.template_variables or {}
    excl_raw = tpl_vars_for_excl.get("_exclude_segments") or []
    if isinstance(excl_raw, str):
        try:
            import json as _json  # noqa: PLC0415
            excl_raw = _json.loads(excl_raw) if excl_raw.strip() else []
        except Exception:
            excl_raw = []
    excluded_segments: List[str] = (
        [str(s).strip().lower() for s in excl_raw if str(s).strip()]
        if isinstance(excl_raw, list) else []
    )

    # Load the merchant's hard-block list once per dispatch. This is
    # the strongest exclusion the platform supports — a phone here
    # is ALWAYS excluded, regardless of audience targeting, segments,
    # or even prior opt-in. Logged with skip_reason=blocked_customer.
    blocked_phones = _load_blocked_phone_set(db, tenant_id)
    if blocked_phones:
        logger.info(
            "[campaign_dispatcher] campaign=%d blocked_customers=%d (merchant block list)",
            campaign_id, len(blocked_phones),
        )

    # ── 1. Snapshot recipients into campaign_send_logs ──────────────────
    snapshot = _snapshot_recipients(
        db, tenant_id, campaign_id, customers, template,
        excluded_segments=excluded_segments,
        blocked_phones=blocked_phones,
    )
    db.commit()

    # ── 2. Frequency-cap dedupe ─────────────────────────────────────────
    # Mandatory for every merchant dispatch. A ``_bypass_frequency_cap``
    # flag left on an older campaign by the removed dispatch-now toggle is
    # ignored and cleared — it neither skips the cap nor re-queues rows the
    # cap skipped before.
    tv_now = campaign.template_variables or {}
    if "_bypass_frequency_cap" in tv_now:
        logger.warning(
            "[campaign_dispatcher] campaign=%d ignoring legacy _bypass_frequency_cap=%r "
            "(the bypass was removed; the frequency cap applies)",
            campaign_id, tv_now.get("_bypass_frequency_cap"),
        )
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
        tv_clean = dict(tv_now)
        tv_clean.pop("_bypass_frequency_cap", None)
        campaign.template_variables = tv_clean
        flag_modified(campaign, "template_variables")
    cap_skipped = _apply_frequency_cap(db, tenant_id, campaign_id)
    db.commit()

    campaign.status = "active"
    if not campaign.launched_at:
        campaign.launched_at = datetime.now(timezone.utc)
    campaign.audience_count = len(customers)

    # ── Audience funnel — phase 2: persist breakdown ────────────────
    # Now that snapshot + frequency cap have run we know exactly how
    # many recipients made it through. Persist the full funnel so
    # /campaigns/{id}/debug can render it without recomputation.
    try:
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
        counts_after_snapshot = _count_log_statuses(db, campaign_id)
        skipped_at_snapshot = (
            counts_after_snapshot.get(LOG_SKIPPED_UNREACHABLE, 0)
            + counts_after_snapshot.get(LOG_SKIPPED_UNSUBSCRIBED, 0)
            + counts_after_snapshot.get(LOG_SKIPPED_INVALID, 0)
            + counts_after_snapshot.get(LOG_SKIPPED_MANUAL_EXCLUSION, 0)
        )
        funnel = {
            # 1. Customers in the unified segment query (auto ∪ overrides)
            "raw_audience":           int(raw_audience_count),
            # 2. After reachability filter (has phone, not opted-out)
            "after_reachable_filter": int(after_reachable_count),
            # 3. Rows actually written to campaign_send_logs
            "materialized_rows":      int(sum(counts_after_snapshot.values())),
            # 4. Of those, how many are queued for actual send
            "queued_for_send":        int(counts_after_snapshot.get(LOG_QUEUED, 0)),
            # 5. How many were already skipped at snapshot time
            "skipped_at_snapshot":    int(skipped_at_snapshot),
            # 6. Frequency-cap dedupes (sent within last N days to same phone)
            "frequency_cap_skipped":  int(cap_skipped),
        }
        tpl_vars_now = dict(campaign.template_variables or {})
        # Persist as JSON-encoded string because the column is
        # ``Dict[str, str]`` (the same shape constraint we hit for
        # ``_exclude_segments``).
        import json as _json  # noqa: PLC0415
        tpl_vars_now["_audience_funnel"] = _json.dumps(funnel, ensure_ascii=False)
        campaign.template_variables = tpl_vars_now
        flag_modified(campaign, "template_variables")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[campaign_dispatcher] campaign=%d funnel persist failed: %s",
            campaign_id, exc,
        )
    db.commit()

    counts_pre = _count_log_statuses(db, campaign_id)
    logger.info(
        "[campaign_dispatcher] campaign=%d snapshot=%s cap_skipped=%d counts=%s",
        campaign_id, snapshot, cap_skipped, counts_pre,
    )

    # ── 3. Send queued / failed rows in batches ─────────────────────────
    tpl_vars = campaign.template_variables or {}
    auto_coupon = tpl_vars.get("_auto_coupon") == "true"
    discount_pct_raw = tpl_vars.get("_discount_percent")
    discount_pct = int(discount_pct_raw) if discount_pct_raw else None
    store_name = _resolve_store_name(db, tenant_id)

    # Manual coupon = whatever the merchant literally typed into the
    # campaign wizard for a manual template. It MUST be sent verbatim
    # to Meta (no coupon-generator override, no AI substitution). The
    # legacy "auto" sentinel that the wizard writes for auto campaigns
    # is filtered here so it never reaches the wire as a literal code.
    manual_coupon = str(getattr(campaign, "coupon_code", "") or "").strip()
    if auto_coupon or manual_coupon.lower() == "auto":
        manual_coupon = ""

    sent, failed, errors = await _dispatch_queued_rows(
        db,
        campaign=campaign,
        template=template,
        wa_conn=wa_conn,
        store_name=store_name,
        auto_coupon=auto_coupon,
        discount_pct=discount_pct,
        manual_coupon=manual_coupon,
        customers_by_phone={
            (c.normalized_phone or ""): c for c in customers if c.normalized_phone
        },
        ctx=ctx,
    )

    # ── 4. Final counters & campaign status ─────────────────────────────
    counts = _count_log_statuses(db, campaign_id)
    if ctx.lease_lost:
        # Another worker owns the campaign now; its run decides status.
        return {
            "campaign_id": campaign_id, "status": "lease_lost",
            "sent": sent, "failed": failed, "errors": errors,
        }
    from services import campaign_send_ledger as ledger  # noqa: PLC0415
    from services.campaign_send_ledger import LOG_UNCERTAIN  # noqa: PLC0415
    if not ctx.pause_reason and counts.get(LOG_QUEUED, 0) > 0:
        # The run ended (a breaker, an abort) with recipients still waiting:
        # that is a stop, never a completion.
        ctx.pause(ledger.PAUSE_RUN_ENDED_WITH_QUEUE,
                  "; ".join(e for e in errors if e.startswith("dispatch_"))[:500])
    if ctx.pause_reason:
        # Stopped on purpose (merchant stop, shared limit, Meta
        # throttling, unknown outcomes): the queue is intact and the
        # reason is on the lease — not a failure, not completion.
        final_status = "paused"
    elif counts.get("sent", 0) > 0:
        final_status = "completed"
    elif counts.get(LOG_UNCERTAIN, 0) > 0:
        final_status = "paused"
        ctx.pause("uncertain_sends", "every started send has an unknown outcome")
    elif counts.get("failed", 0) > 0:
        final_status = "failed"
    else:
        final_status = "completed"
    campaign.sent_count = counts.get("sent", 0)
    campaign.status = final_status
    campaign.updated_at = datetime.now(timezone.utc)
    _note_capacity_wait(campaign, ctx)

    _persist_dispatch_result(
        campaign,
        sent=counts.get("sent", 0),
        failed=counts.get("failed", 0),
        skipped=counts.get(LOG_SKIPPED_DUPLICATE, 0)
                + counts.get(LOG_SKIPPED_INVALID, 0)
                + counts.get(LOG_SKIPPED_UNSUBSCRIBED, 0)
                + counts.get(LOG_SKIPPED_UNREACHABLE, 0)
                + counts.get(LOG_SKIPPED_MANUAL_EXCLUSION, 0),
        errors=errors,
    )
    db.commit()

    logger.info(
        "[campaign_dispatcher] campaign=%d tenant=%d status=%s counts=%s errors=%s",
        campaign_id, tenant_id, final_status, counts, errors[:3],
    )

    return {
        "campaign_id":   campaign_id,
        "status":        final_status,
        "total_recipients":   sum(counts.values()),
        "sent":               counts.get("sent", 0),
        "failed":             counts.get("failed", 0),
        "queued":             counts.get("queued", 0),
        "skipped_duplicate":  counts.get(LOG_SKIPPED_DUPLICATE, 0),
        "invalid_phone":      counts.get(LOG_SKIPPED_INVALID, 0),
        "skipped_unsubscribed": counts.get(LOG_SKIPPED_UNSUBSCRIBED, 0),
        "skipped_unreachable":  counts.get(LOG_SKIPPED_UNREACHABLE, 0),
        "skipped_manual_exclusion": counts.get(LOG_SKIPPED_MANUAL_EXCLUSION, 0),
        "skipped_quality_suppressed":
            counts.get(LOG_SKIPPED_QUALITY_SUPPRESSED, 0),
        "skipped_blocked_customer":
            counts.get(LOG_SKIPPED_BLOCKED_CUSTOMER, 0),
        "frequency_cap_days": MARKETING_CAMPAIGN_FREQUENCY_CAP_DAYS,
        "errors":         errors,
    }


# ── Snapshot + dedupe helpers ────────────────────────────────────────────


def _empty_result(*, error: str = "") -> Dict[str, Any]:
    return {
        "campaign_id":          None,
        "status":               "failed",
        "total_recipients":     0,
        "sent":                 0,
        "failed":               0,
        "queued":               0,
        "skipped_duplicate":    0,
        "invalid_phone":        0,
        "skipped_unsubscribed": 0,
        "skipped_unreachable":  0,
        "skipped_manual_exclusion": 0,
        "skipped_quality_suppressed": 0,
        "skipped_blocked_customer": 0,
        "frequency_cap_days":   MARKETING_CAMPAIGN_FREQUENCY_CAP_DAYS,
        "errors":               [error] if error else [],
    }


def _snapshot_recipients(
    db: Session,
    tenant_id: int,
    campaign_id: int,
    customers: List[Customer],
    template: WhatsAppTemplate,
    *,
    excluded_segments: Optional[List[str]] = None,
    blocked_phones: Optional[set] = None,
) -> Dict[str, int]:
    """Insert one ``campaign_send_logs`` row per recipient with
    ``status='queued'`` (or the appropriate ``skipped_*`` status when
    the recipient is unreachable / opted-out).

    Idempotency: the unique index on
    ``(tenant_id, campaign_id, customer_phone_e164)`` makes the second
    snapshot for the same recipient a no-op. We therefore SELECT the
    set of phones already logged and only INSERT for the rest.
    """
    if not customers:
        return {"new": 0, "existing": 0, "no_phone": 0}

    # Defensive copy so we can ``in`` against multiple key shapes
    # (raw normalized_phone AND a digit-canonicalized variant) without
    # mutating the caller's set.
    blocked_set: set = set(blocked_phones or ())

    # Existing rows for this campaign — these have already gone through
    # snapshot and may even be in `sent` status. We do NOT touch them.
    existing_phones = {
        row[0]
        for row in db.query(CampaignSendLog.customer_phone_e164)
                     .filter(CampaignSendLog.campaign_id == campaign_id)
                     .all()
    }

    # Pull every customer's manual segment set in a single bulk query —
    # avoids N+1 when checking the wizard's "exclude segments" rule.
    excl = {(s or "").strip().lower() for s in (excluded_segments or []) if s}
    manual_segments_by_id: Dict[int, set] = {}
    if excl and customers:
        from services.manual_segments import list_manual_segments_bulk  # noqa: PLC0415
        bulk = list_manual_segments_bulk(db, tenant_id, [c.id for c in customers])
        manual_segments_by_id = {cid: set(keys) for cid, keys in bulk.items()}

    from services.manual_segments import is_marketing_opted_out  # noqa: PLC0415

    # ── Delivery Quality Intelligence Layer (May 2026) ──
    # Bulk-load the set of currently-suppressed phones for this
    # tenant. One query instead of one per recipient. We tolerate
    # the table not existing yet (e.g. during a partial deployment)
    # by catching any exception and treating the set as empty.
    suppressed_phones: set[str] = set()
    try:
        from models import CustomerSuppression  # noqa: PLC0415
        suppressed_phones = {
            row[0]
            for row in db.query(CustomerSuppression.normalized_phone)
                         .filter(
                             CustomerSuppression.tenant_id == tenant_id,
                             CustomerSuppression.is_active.is_(True),
                         )
                         .all()
            if row and row[0]
        }
    except Exception as _supp_exc:
        logger.debug(
            "[snapshot] suppression lookup skipped (table missing?): %s",
            _supp_exc,
        )

    new_rows: List[CampaignSendLog] = []
    no_phone = 0
    for cust in customers:
        phone = (cust.normalized_phone or "").strip()
        if not phone:
            # No normalized phone — record an "unreachable" row keyed
            # off a synthetic phone token so the unique index doesn't
            # explode when multiple customers share an empty phone.
            # Use the customer id as the anchor.
            phone_key = f"__no_phone__:{cust.id}"
            if phone_key in existing_phones:
                continue
            new_rows.append(CampaignSendLog(
                tenant_id=tenant_id,
                campaign_id=campaign_id,
                customer_id=cust.id,
                customer_phone_e164=phone_key,
                template_name=template.name,
                template_language=template.language,
                status=LOG_SKIPPED_UNREACHABLE,
                skip_reason=REASON_NO_PHONE,
            ))
            existing_phones.add(phone_key)
            no_phone += 1
            continue

        if phone in existing_phones:
            continue

        # ── Merchant block list (strongest exclusion) ─────────────
        # Honoured BEFORE any opt-out / segment / suppression rules
        # because the merchant explicitly hard-blocked this customer
        # from every touchpoint. Match against E.164 AND the
        # digit-canonicalized variant so legacy entries stored as
        # ``0501234567`` still match a stored ``+966501234567``.
        if blocked_set:
            phone_keys = {phone, _normalize_blocked_phone(phone)}
            if phone_keys & blocked_set:
                new_rows.append(CampaignSendLog(
                    tenant_id=tenant_id,
                    campaign_id=campaign_id,
                    customer_id=cust.id,
                    customer_phone_e164=phone,
                    template_name=template.name,
                    template_language=template.language,
                    status=LOG_SKIPPED_BLOCKED_CUSTOMER,
                    skip_reason=REASON_BLOCKED_CUSTOMER,
                ))
                existing_phones.add(phone)
                continue

        meta = getattr(cust, "extra_metadata", None) or {}
        if meta.get("is_unsubscribed"):
            new_rows.append(CampaignSendLog(
                tenant_id=tenant_id,
                campaign_id=campaign_id,
                customer_id=cust.id,
                customer_phone_e164=phone,
                template_name=template.name,
                template_language=template.language,
                status=LOG_SKIPPED_UNSUBSCRIBED,
                skip_reason=REASON_UNSUBSCRIBED,
            ))
            existing_phones.add(phone)
            continue

        if meta.get("pending_unsubscribe"):
            new_rows.append(CampaignSendLog(
                tenant_id=tenant_id,
                campaign_id=campaign_id,
                customer_id=cust.id,
                customer_phone_e164=phone,
                template_name=template.name,
                template_language=template.language,
                status=LOG_SKIPPED_UNSUBSCRIBED,
                skip_reason=REASON_PENDING_OPT_OUT,
            ))
            existing_phones.add(phone)
            continue

        # ── Merchant-driven opt-out (drawer toggle) ────────────────
        # Distinct from `is_unsubscribed` (customer-driven). We log it
        # under skipped_manual_exclusion so the campaign report tells
        # the merchant exactly why the row was excluded.
        if is_marketing_opted_out(cust):
            new_rows.append(CampaignSendLog(
                tenant_id=tenant_id,
                campaign_id=campaign_id,
                customer_id=cust.id,
                customer_phone_e164=phone,
                template_name=template.name,
                template_language=template.language,
                status=LOG_SKIPPED_MANUAL_EXCLUSION,
                skip_reason=REASON_MARKETING_OPT_OUT,
            ))
            existing_phones.add(phone)
            continue

        # ── Delivery Quality auto-suppression ─────────────────────
        # Phone is in the active suppression list — i.e. it has hit
        # the auto-suppress threshold (e.g. 2× ``not_on_whatsapp``)
        # or a critical one-shot signal (``blocked_by_user``). We
        # do NOT send and we log the row so the merchant can see
        # how many phones the engine filtered.
        if phone in suppressed_phones:
            new_rows.append(CampaignSendLog(
                tenant_id=tenant_id,
                campaign_id=campaign_id,
                customer_id=cust.id,
                customer_phone_e164=phone,
                template_name=template.name,
                template_language=template.language,
                status=LOG_SKIPPED_QUALITY_SUPPRESSED,
                skip_reason=REASON_QUALITY_SUPPRESSED,
            ))
            existing_phones.add(phone)
            continue

        # ── Wizard exclude-segment rule ────────────────────────────
        # If the merchant said "exclude customers tagged X" in step 2
        # of the wizard, drop those rows here too. We DO NOT silently
        # skip — we record an explicit `skipped_manual_exclusion` row
        # so the report shows the full audit.
        if excl and excl.intersection(manual_segments_by_id.get(cust.id, ())):
            new_rows.append(CampaignSendLog(
                tenant_id=tenant_id,
                campaign_id=campaign_id,
                customer_id=cust.id,
                customer_phone_e164=phone,
                template_name=template.name,
                template_language=template.language,
                status=LOG_SKIPPED_MANUAL_EXCLUSION,
                skip_reason=REASON_MANUAL_EXCLUDE,
            ))
            existing_phones.add(phone)
            continue

        new_rows.append(CampaignSendLog(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            customer_id=cust.id,
            customer_phone_e164=phone,
            template_name=template.name,
            template_language=template.language,
            status=LOG_QUEUED,
        ))
        existing_phones.add(phone)

    if new_rows:
        # ``add_all`` (not ``bulk_save_objects``) so SQLAlchemy applies
        # column defaults — the SQLite shim used in tests requires an
        # autoincrement on the BigInteger PK that bulk_save skips.
        # For realistic audience sizes (≤10k recipients per snapshot)
        # add_all is plenty fast.
        db.add_all(new_rows)
        db.flush()

    return {
        "new": len(new_rows),
        "existing": len(existing_phones) - len(new_rows),
        "no_phone": no_phone,
    }


def _cap_contacted_phones(
    db: Session,
    tenant_id: int,
    campaign_id: Optional[int],
    sent_rows: List[Tuple[int, str]],
    threshold: datetime,
) -> set:
    """Phones that count as "already messaged" for the frequency cap.

    * ``sent`` rows count, except recipients the attempt ledger proves
      never received the message (all attempts refused, or accepted then
      reported failed with no delivered/read evidence). Pre-ledger rows
      keep counting — their single wamid column cannot prove that.
    * ``uncertain`` rows count too: we do not know the customer did not
      get it, and the cap exists to avoid a second copy.
    """
    from services.campaign_send_ledger import (  # noqa: PLC0415
        LOG_UNCERTAIN, log_ids_proven_undelivered,
    )
    failed_ids = log_ids_proven_undelivered(db, [int(i) for i, _ in sent_rows])
    phones = {p for i, p in sent_rows if int(i) not in failed_ids}
    q = db.query(CampaignSendLog.customer_phone_e164).filter(
        CampaignSendLog.tenant_id == tenant_id,
        CampaignSendLog.status == LOG_UNCERTAIN,
        CampaignSendLog.updated_at >= threshold.replace(tzinfo=None),
    )
    if campaign_id is not None:
        q = q.filter(CampaignSendLog.campaign_id != campaign_id)
    phones.update(r[0] for r in q.distinct().all())
    return phones


def _apply_frequency_cap(
    db: Session,
    tenant_id: int,
    campaign_id: int,
) -> int:
    """Mark ``status='queued'`` rows whose phone received another
    *successfully delivered* marketing campaign within the cap window
    as ``status='skipped_duplicate'``.

    Strict definition of "successfully delivered" (per merchant
    feedback — frequency cap was misfiring on rows that never reached
    Meta):

        status == 'sent'
        AND (
            provider_message_id IS NOT NULL
            OR sent_at IS NOT NULL
        )
        AND sent_at >= now() - cap_days

    A row that's ``failed``, ``queued``, ``sending``, ``skipped_*``,
    or ``status=sent`` but missing BOTH ``provider_message_id`` and
    ``sent_at`` is NEVER counted as "the customer received a previous
    campaign". This protects merchants from being silently blocked by
    legacy/synthetic data or by failed-but-status-flipped rows.

    Returns:
        Number of rows updated to ``skipped_duplicate``. A cap value
        of 0 also disables the check entirely (admin escape hatch —
        should never be 0 in prod).
    """
    cap_days = MARKETING_CAMPAIGN_FREQUENCY_CAP_DAYS
    if cap_days <= 0:
        return 0

    threshold = datetime.now(timezone.utc) - timedelta(days=cap_days)

    # ── Stricter "successfully delivered" predicate ──────────────
    delivered_proof = or_(
        CampaignSendLog.provider_message_id.isnot(None),
        CampaignSendLog.sent_at.isnot(None),
    )
    # Time window: prefer ``sent_at``, but legacy rows may carry a
    # wamid without ``sent_at`` — fall back to ``updated_at`` so a
    # provably accepted Meta message still burns the cap slot.
    within_cap_window = or_(
        CampaignSendLog.sent_at >= threshold,
        and_(
            CampaignSendLog.sent_at.is_(None),
            CampaignSendLog.provider_message_id.isnot(None),
            CampaignSendLog.updated_at >= threshold,
        ),
    )

    sent_rows = (
        db.query(CampaignSendLog.id, CampaignSendLog.customer_phone_e164)
        .filter(
            CampaignSendLog.tenant_id == tenant_id,
            CampaignSendLog.status == LOG_SENT,
            delivered_proof,
            within_cap_window,
            # Don't dedupe a campaign against itself (re-runs).
            CampaignSendLog.campaign_id != campaign_id,
        )
        .all()
    )
    sent_phones = _cap_contacted_phones(db, tenant_id, campaign_id, sent_rows, threshold)

    if not sent_phones:
        return 0

    # Bulk update queued rows whose phone is in the dedupe set.
    queued = (
        db.query(CampaignSendLog)
        .filter(
            CampaignSendLog.campaign_id == campaign_id,
            CampaignSendLog.status == LOG_QUEUED,
            CampaignSendLog.customer_phone_e164.in_(sent_phones),
        )
        .all()
    )
    now = datetime.now(timezone.utc)
    for row in queued:
        row.status = LOG_SKIPPED_DUPLICATE
        row.skip_reason = f"{REASON_FREQ_CAP}:{cap_days}d"
        row.updated_at = now

    return len(queued)


def _frequency_cap_evidence_for_phones(
    db: Session,
    tenant_id: int,
    phones: List[str],
    *,
    cap_days: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    """For each phone in ``phones``, return the most recent successful
    send (if any) within the cap window, plus the campaign id that
    sent it. Used by ``/campaigns/{id}/debug`` so the merchant can see
    "we deduped phone X because campaign Y sent to them on date Z" —
    instead of a generic "skipped_duplicate" with no audit trail.

    Returns a dict keyed by phone; phones with no successful send are
    omitted from the result.
    """
    if not phones:
        return {}
    cap = cap_days if cap_days is not None else MARKETING_CAMPAIGN_FREQUENCY_CAP_DAYS
    if cap <= 0:
        # Cap disabled — no evidence to surface.
        return {}
    threshold = datetime.now(timezone.utc) - timedelta(days=cap)

    delivered_proof = or_(
        CampaignSendLog.provider_message_id.isnot(None),
        CampaignSendLog.sent_at.isnot(None),
    )
    within_cap_window = or_(
        CampaignSendLog.sent_at >= threshold,
        and_(
            CampaignSendLog.sent_at.is_(None),
            CampaignSendLog.provider_message_id.isnot(None),
            CampaignSendLog.updated_at >= threshold,
        ),
    )

    rows = (
        db.query(CampaignSendLog)
        .filter(
            CampaignSendLog.tenant_id == tenant_id,
            CampaignSendLog.status == LOG_SENT,
            delivered_proof,
            within_cap_window,
            CampaignSendLog.customer_phone_e164.in_(phones),
        )
        .order_by(CampaignSendLog.sent_at.desc(), CampaignSendLog.updated_at.desc())
        .all()
    )
    # First (most recent) hit per phone wins.
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        phone = r.customer_phone_e164
        if phone in out:
            continue
        out[phone] = {
            "last_successful_sent_at":
                r.sent_at.isoformat() if r.sent_at else None,
            "last_successful_campaign_id": int(r.campaign_id),
            "provider_message_id": r.provider_message_id,
        }
    return out


_MAX_RAW_META_SAMPLES = 5


def _mask_phone_for_log(phone: Optional[str]) -> str:
    """Display the last 4 digits only — matches /debug masking."""
    if not phone:
        return ""
    s = str(phone)
    return ("•" * max(0, len(s) - 4)) + s[-4:] if len(s) > 4 else s


def _mask_payload(payload: Any) -> Any:
    """Deep-copy ``payload`` masking obvious PII (``to`` phone). We
    keep template / components / parameters verbatim because that's
    exactly what support needs to debug ``unknown`` Meta errors —
    e.g. ``template.name`` mismatch, ``language.code`` mismatch, or
    parameter-count mismatch.

    We never mutate the original dict so the live Meta call is
    unaffected by what the debug endpoint stores.
    """
    if not isinstance(payload, dict):
        return payload
    masked: Dict[str, Any] = {}
    for k, v in payload.items():
        if k == "to" and v:
            masked[k] = _mask_phone_for_log(str(v))
        elif isinstance(v, dict):
            masked[k] = _mask_payload(v)
        elif isinstance(v, list):
            masked[k] = [_mask_payload(x) for x in v]
        else:
            masked[k] = v
    return masked


def _summarise_send_payload(
    payload: Dict[str, Any],
    *,
    template: Optional["WhatsAppTemplate"] = None,
) -> Dict[str, Any]:
    """Build the compact ``campaign_send_attempt`` log dict.

    Mirrors the shape the user explicitly asked for so the production
    log line is grep-able as JSON::

        {campaign_id, recipient, template_name, language, category,
         component_count, header_params, body_params, button_params,
         media}

    ``template`` (optional) is consulted for ``category`` since the
    payload doesn't carry it.
    """
    tpl = (payload or {}).get("template") or {}
    components = tpl.get("components") or []
    header_params = 0
    body_params = 0
    button_params = 0
    media = False
    for comp in components:
        ctype = (comp.get("type") or "").lower()
        params = comp.get("parameters") or []
        if ctype == "body":
            body_params += len(params)
        elif ctype == "button":
            button_params += len(params)
        elif ctype == "header":
            for p in params:
                if isinstance(p, dict) and (p.get("type") or "").lower() in (
                    "image", "video", "document",
                ):
                    media = True
                else:
                    header_params += 1
    return {
        "template_name":   tpl.get("name"),
        "language":        ((tpl.get("language") or {}).get("code")
                            if isinstance(tpl.get("language"), dict) else tpl.get("language")),
        "category":        getattr(template, "category", None),
        "recipient":       _mask_phone_for_log(payload.get("to")),
        "component_count": len(components),
        "header_params":   header_params,
        "body_params":     body_params,
        "button_params":   button_params,
        "media":           media,
    }


def _log_send_attempt(
    *,
    campaign_id: int,
    template: WhatsAppTemplate,
    recipient_phone: str,
    payload: Dict[str, Any],
) -> None:
    """Emit the canonical pre-send attempt line. The dict shape is
    stable so support can search ``campaign_send_attempt`` in Railway
    logs and instantly correlate every ``unknown`` Meta failure with
    the template variables that were shipped."""
    try:
        summary = _summarise_send_payload(payload, template=template)
    except Exception:
        summary = {
            "template_name": getattr(template, "name", None),
            "category":      getattr(template, "category", None),
            "recipient":     _mask_phone_for_log(recipient_phone),
        }
    logger.info(
        "[campaign_dispatcher] campaign_send_attempt campaign=%d %s",
        campaign_id, summary,
    )


def _extract_fbtrace_id(resp: Any) -> Optional[str]:
    """Meta sometimes nests fbtrace_id under ``error.fbtrace_id`` and
    sometimes under ``error.error_data.fbtrace_id``. We accept either
    and return ``None`` when neither is present."""
    if not isinstance(resp, dict):
        return None
    err = resp.get("error")
    if isinstance(err, dict):
        for candidate in (
            err.get("fbtrace_id"),
            (err.get("error_data") or {}).get("fbtrace_id")
                if isinstance(err.get("error_data"), dict) else None,
            err.get("trace_id"),
        ):
            if candidate:
                return str(candidate)
    top = resp.get("fbtrace_id")
    return str(top) if top else None


def diff_template_components(
    template: "WhatsAppTemplate",
    payload: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Compare what Meta APPROVED for this template against what we're
    about to send. Returns a list of issues so the merchant can see,
    e.g. "BODY expected 2 params, got 1" or "BUTTON URL param missing"
    right next to the raw Meta error.

    Issue shape::

        {component: "BODY"|"HEADER"|"BUTTONS",
         index:    int|None,
         kind:     "param_count_mismatch"|"missing_button_param"|"missing_media",
         expected: <int|str>,
         sent:     <int|str>,
         message_ar: str}
    """
    issues: List[Dict[str, Any]] = []
    sent_components = (
        ((payload or {}).get("template") or {}).get("components") or []
    )
    # Build a quick lookup of sent components by (type, sub_type, index).
    sent_body_param_count = 0
    sent_header_text_param_count = 0
    sent_header_media = False
    sent_buttons: Dict[Tuple[str, int], int] = {}  # (sub_type, index) → param count
    for comp in sent_components:
        ctype = (comp.get("type") or "").lower()
        params = comp.get("parameters") or []
        if ctype == "body":
            sent_body_param_count = len(params)
        elif ctype == "header":
            for p in params:
                ptype = (p.get("type") or "").lower() if isinstance(p, dict) else ""
                if ptype in ("image", "video", "document"):
                    sent_header_media = True
                else:
                    sent_header_text_param_count += 1
        elif ctype == "button":
            sub = (comp.get("sub_type") or "").lower()
            try:
                idx = int(comp.get("index") or 0)
            except (ValueError, TypeError):
                idx = 0
            sent_buttons[(sub, idx)] = len(params)

    for comp in (template.components or []):
        ctype = (comp.get("type") or "").upper()
        text = comp.get("text") or ""

        if ctype == "BODY":
            expected = _extract_param_count(text) or _example_param_count(comp, "body_text")
            if expected != sent_body_param_count:
                issues.append({
                    "component":  "BODY",
                    "index":      None,
                    "kind":       "param_count_mismatch",
                    "expected":   expected,
                    "sent":       sent_body_param_count,
                    "message_ar": (
                        f"BODY يتوقع {expected} متغيراً، أُرسل {sent_body_param_count}"
                    ),
                })

        elif ctype == "HEADER":
            fmt = (comp.get("format") or "").upper()
            if fmt == "TEXT":
                expected = (
                    _extract_param_count(text)
                    or _example_param_count(comp, "header_text")
                )
                if expected != sent_header_text_param_count:
                    issues.append({
                        "component":  "HEADER",
                        "index":      None,
                        "kind":       "param_count_mismatch",
                        "expected":   expected,
                        "sent":       sent_header_text_param_count,
                        "message_ar": (
                            f"HEADER نصّي يتوقع {expected} متغيّراً، أُرسل "
                            f"{sent_header_text_param_count}"
                        ),
                    })
            elif fmt in ("IMAGE", "VIDEO", "DOCUMENT"):
                if not sent_header_media:
                    issues.append({
                        "component":  "HEADER",
                        "index":      None,
                        "kind":       "missing_media",
                        "expected":   fmt.lower(),
                        "sent":       "—",
                        "message_ar": (
                            f"HEADER يتوقع وسائط {fmt}، لكن لم تُرسل أي وسائط"
                        ),
                    })

        elif ctype == "BUTTONS":
            for idx, btn in enumerate(comp.get("buttons") or []):
                btype = (btn.get("type") or "").upper()
                if btype == "COPY_CODE":
                    sent_count = sent_buttons.get(("copy_code", idx), 0)
                    if sent_count == 0:
                        issues.append({
                            "component":  "BUTTONS",
                            "index":      idx,
                            "kind":       "missing_button_param",
                            "expected":   "coupon_code",
                            "sent":       "—",
                            "message_ar": (
                                f"الزر #{idx} (COPY_CODE) يتطلّب كوبون "
                                "لكنه غير مُمرَّر"
                            ),
                        })
                elif btype == "URL":
                    url = btn.get("url") or ""
                    if "{{" in url:
                        sent_count = sent_buttons.get(("url", idx), 0)
                        if sent_count == 0:
                            issues.append({
                                "component":  "BUTTONS",
                                "index":      idx,
                                "kind":       "missing_button_param",
                                "expected":   "url_suffix",
                                "sent":       "—",
                                "message_ar": (
                                    f"الزر #{idx} (URL ديناميكي) يتطلّب "
                                    "متغيراً لكنه غير مُمرَّر"
                                ),
                            })
                elif btype == "OTP":
                    sent_count = sent_buttons.get(("url", idx), 0)
                    if sent_count == 0:
                        issues.append({
                            "component":  "BUTTONS",
                            "index":      idx,
                            "kind":       "missing_button_param",
                            "expected":   "otp_code",
                            "sent":       "—",
                            "message_ar": (
                                f"الزر #{idx} (OTP) يتطلّب رمزاً لكنه غير "
                                "مُمرَّر"
                            ),
                        })

    return issues


def _record_raw_meta_sample(
    *,
    campaign: "Campaign",
    recipient_phone: str,
    meta_code: Any,
    meta_subcode: Any,
    meta_type: Any,
    meta_message: Any,
    request_payload: Dict[str, Any],
    response_payload: Dict[str, Any],
    classified_key: str,
    template: Optional["WhatsAppTemplate"] = None,
) -> None:
    """Append a bounded sample of the full Meta request/response onto
    ``campaign.template_variables._raw_meta_error_samples`` so the
    debug endpoint can show it verbatim. Capped at
    ``_MAX_RAW_META_SAMPLES`` to keep the JSONB row size sane.

    Order: prefer ``unknown`` keys at the front (they're the ones
    support needs to fingerprint). The classifier improves over time
    by adding the codes that show up here to ``meta_errors._CODE_MAP``.
    """
    try:
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
        import json as _json  # noqa: PLC0415
        tv = dict(campaign.template_variables or {})
        raw_str = tv.get("_raw_meta_error_samples") or ""
        samples: List[Dict[str, Any]] = []
        if isinstance(raw_str, str) and raw_str.strip():
            try:
                samples = _json.loads(raw_str) or []
            except Exception:
                samples = []
        elif isinstance(raw_str, list):
            samples = list(raw_str)

        fbtrace_id = _extract_fbtrace_id(response_payload)
        template_summary = _summarise_send_payload(
            request_payload, template=template,
        )
        component_diff: List[Dict[str, Any]] = []
        if template is not None:
            try:
                component_diff = diff_template_components(
                    template, request_payload,
                )
            except Exception:  # noqa: BLE001
                component_diff = []
        sample = {
            "ts":                  datetime.now(timezone.utc).isoformat(),
            "recipient":           _mask_phone_for_log(recipient_phone),
            "meta_error_code":     str(meta_code) if meta_code is not None else None,
            "meta_error_subcode":  str(meta_subcode) if meta_subcode is not None else None,
            "meta_error_type":     str(meta_type) if meta_type is not None else None,
            "meta_error_message":  str(meta_message or "")[:1000],
            "fbtrace_id":          fbtrace_id,
            "request_payload":     _mask_payload(request_payload),
            "response_payload":    _mask_payload(response_payload),
            "classified_key":      classified_key,
            "template_summary":    template_summary,
            "component_diff":      component_diff,
        }
        # Unknown samples win priority — drop the oldest known sample
        # first when we exceed the cap.
        samples.append(sample)
        if len(samples) > _MAX_RAW_META_SAMPLES:
            unknowns = [s for s in samples if s.get("classified_key") == "unknown"]
            knowns = [s for s in samples if s.get("classified_key") != "unknown"]
            kept = unknowns[-_MAX_RAW_META_SAMPLES:]
            slots_left = max(0, _MAX_RAW_META_SAMPLES - len(kept))
            kept = knowns[-slots_left:] + kept if slots_left > 0 else kept
            samples = kept

        tv["_raw_meta_error_samples"] = _json.dumps(samples, ensure_ascii=False)
        campaign.template_variables = tv
        flag_modified(campaign, "template_variables")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[campaign_dispatcher] failed to persist raw Meta sample: %s",
            exc,
        )


def _revive_zombie_sending(
    db: Session,
    campaign_id: int,
    *,
    timeout_seconds: int = SENDING_TIMEOUT_SECONDS,
    owner: Optional[str] = None,
) -> int:
    """Resolve rows a dead worker left in ``sending``. No commit.

    A row in ``sending`` may already have been accepted by Meta — the
    request can leave the process before the worker records the answer.
    It is therefore never put back on the queue and never marked as a
    retryable failure (the old ``watchdog_timeout`` → retry path is how a
    crashed campaign re-sent to people who had already received it).
    Delegates to :func:`campaign_send_ledger.recover_stale_attempts`:

      * reserved but request never started → ``queued`` (provably unsent);
      * request started, or a pre-ledger row with ≥1 attempt → ``uncertain``.

    Does nothing while another worker holds a live lease on the campaign.
    Returns the number of rows resolved.
    """
    from services import campaign_send_ledger as ledger  # noqa: PLC0415

    lease = ledger.get_lease(db, campaign_id)
    if ledger.lease_is_live(lease) and lease.owner != owner:
        return 0
    out = ledger.recover_stale_attempts(db, campaign_id, stale_seconds=timeout_seconds)
    return sum(out.values())


def _force_terminate_runaway(
    row: CampaignSendLog,
    *,
    campaign_id: int,
) -> bool:
    """Catastrophic circuit-breaker. If a single row crosses
    ``ATTEMPT_CIRCUIT_BREAKER`` we must stop it immediately and page
    operators — this is the ``campaign_send_retry_storm`` metric the
    runbook keys off.

    Returns True when the row was force-terminated and the caller
    must skip the send.
    """
    attempts = int(row.attempt_count or 0)
    if attempts <= ATTEMPT_CIRCUIT_BREAKER:
        return False
    logger.critical(
        "[campaign_dispatcher] campaign_send_retry_storm campaign=%d "
        "row_id=%s attempts=%d phone_last4=%s — force-terminating row",
        campaign_id, row.id, attempts,
        (row.customer_phone_e164 or "")[-4:],
    )
    row.status = LOG_FAILED
    row.error_code = "retry_storm"
    row.error_message = (
        f"[circuit_breaker] attempts={attempts} exceeded "
        f"ATTEMPT_CIRCUIT_BREAKER={ATTEMPT_CIRCUIT_BREAKER}"
    )[:480]
    row.updated_at = datetime.now(timezone.utc)
    return True


def _is_attempts_exhausted(row: CampaignSendLog) -> bool:
    """The merchant-friendly bound: don't ever exceed
    ``MAX_SEND_ATTEMPTS``. Used both before the send (skip exhausted
    rows in the loop) and after a failure (so a row that just crossed
    the threshold is correctly marked ``retry_exhausted`` instead of
    sitting as a generic ``failed`` waiting to be retried.)"""
    return int(row.attempt_count or 0) >= MAX_SEND_ATTEMPTS


# Dispatcher-synthetic ``error_code`` values that don't have a
# ClassifiedError entry but ARE retryable (the row failed for
# infrastructure reasons, not because Meta rejected it). Anything not
# listed here defers to ``meta_errors.is_retryable`` — which keys off
# the canonical ``retryable`` flag in the catalogue.
_DISPATCHER_RETRYABLE_CODES = frozenset({
    "watchdog_revive",
    "internal_error",
})

# Terminal codes that ``reschedule_failed_for_retry`` must never
# touch — these are end-states the dispatcher itself produced and
# putting them back into ``queued`` would re-introduce storms.
#
# The second group are *ambiguous* legacy outcomes written by the
# pre-ledger dispatcher: a watchdog timeout on a ``sending`` row, a
# generic exception around the Meta call, or a success-shaped answer
# without a wamid. Each may hide an accepted message, so an automatic
# retry could send the customer a second copy.
_TERMINAL_DISPATCHER_CODES = frozenset({
    "retry_exhausted",
    "retry_storm",
    "watchdog_timeout",
    "exception",
    "no_message_id",
    "send_outcome_unknown",
})


def _is_error_code_retryable(error_code: Optional[str]) -> bool:
    """Decide whether a row with this stored ``error_code`` is eligible
    for automatic re-queue. Single source of truth used by both
    ``reschedule_failed_for_retry`` and the in-flight dispatcher.

    Policy:
      * Catalogued Meta errors: defer to ``meta_errors.is_retryable``
        (i.e. the ``retryable`` flag on ``ClassifiedError``).
      * Dispatcher-synthetic codes: explicit allow-list above.
      * Empty / unclassified ``error_code``: not retryable. We refuse
        to retry blind; the merchant can press "أرسل الآن" to force
        a single explicit retry instead.
    """
    if not error_code:
        return False
    code = str(error_code).strip().lower()
    if not code:
        return False
    if code in _TERMINAL_DISPATCHER_CODES:
        return False
    if code in _DISPATCHER_RETRYABLE_CODES:
        return True
    # Defer to the central catalogue (retryable=False for
    # client_payment_blocked, not_on_whatsapp, policy_violation, …).
    return is_retryable(code)


def reschedule_failed_for_retry(
    db: Session,
    campaign_id: int,
) -> int:
    """Promote retryable ``failed`` rows back into ``queued``. No commit.

    A recipient is re-queued only when *every* attempt it has is proven
    not to have produced a message (rejected by Meta with a retryable
    code, or never left the process). Recipients with any accepted,
    uncertain or in-flight attempt are never re-queued — that includes
    failures Meta reported *after* accepting (``status='sent'`` with
    ``failed_at``), which are not in ``failed`` at all. Non-retryable
    codes (``not_on_whatsapp``, ``spam_rate_limit``, …) stay terminal.
    Rows past ``MAX_SEND_ATTEMPTS`` become ``retry_exhausted``.
    """
    from services.campaign_send_ledger import log_ids_blocking_retry  # noqa: PLC0415

    rows = (
        db.query(CampaignSendLog)
        .filter(
            CampaignSendLog.campaign_id == campaign_id,
            CampaignSendLog.status == LOG_FAILED,
            CampaignSendLog.error_code != "retry_exhausted",
            CampaignSendLog.error_code != "retry_storm",
        )
        .all()
    )
    blocked = log_ids_blocking_retry(db, [int(r.id) for r in rows])
    moved = 0
    now = datetime.now(timezone.utc)
    for r in rows:
        code = (r.error_code or "").strip().lower()
        if not _is_error_code_retryable(code):
            continue
        if int(r.id) in blocked:
            continue
        if _is_attempts_exhausted(r):
            r.error_code = "retry_exhausted"
            r.updated_at = now
            continue
        r.status = LOG_QUEUED
        r.updated_at = now
        moved += 1
    if moved:
        logger.info(
            "[campaign_dispatcher] reschedule_failed campaign=%d "
            "moved=%d rows back to queued",
            campaign_id, moved,
        )
    return moved


def _count_log_statuses(db: Session, campaign_id: int) -> Dict[str, int]:
    """Return ``{status: count}`` for every status seen on this
    campaign. Backbone of the report endpoint."""
    rows = (
        db.query(CampaignSendLog.status, func.count(CampaignSendLog.id))
        .filter(CampaignSendLog.campaign_id == campaign_id)
        .group_by(CampaignSendLog.status)
        .all()
    )
    return {status: int(count) for status, count in rows}


# ── Batched send ─────────────────────────────────────────────────────────


def _note_capacity_wait(campaign: Campaign, ctx: "DispatchRunContext") -> None:
    """A run stopped by Meta's shared messaging limit is waiting for
    capacity, not failed or merchant-paused: persist when it may continue
    so the scheduler resumes it. A typed marketing breaker gets a separate
    increasing cooldown; spam/unknown/uncertain outcomes clear the wait."""
    from services import campaign_send_ledger as ledger  # noqa: PLC0415
    if ctx.lease_lost:
        return
    if (ctx.pause_reason == ledger.PAUSE_PROVIDER_THROTTLING
            and ctx.post_accept_error_key == "marketing_blocked"):
        wait = ledger.record_marketing_wait(campaign)
        logger.warning(
            "[campaign_dispatcher] campaign=%d waiting after marketing delivery refusals "
            "until %s (attempt %d); untouched recipients only", campaign.id,
            wait["next_eligible_at"], wait["attempt"],
        )
        return
    if ctx.pause_reason == ledger.PAUSE_PROVIDER_RATE_LIMITED:
        wait = ledger.record_rate_limit_wait(campaign, detail=ctx.pause_detail)
        logger.warning(
            "[campaign_dispatcher] campaign=%d backing off after Meta rate limit "
            "until %s (attempt %d): %s", campaign.id, wait["next_eligible_at"],
            wait["attempt"], ctx.pause_detail,
        )
        return
    if ctx.pause_reason == ledger.PAUSE_MESSAGING_LIMIT:
        wait = ledger.record_capacity_wait(campaign, ctx.capacity)
        logger.warning(
            "[campaign_dispatcher] campaign=%d waiting for Meta messaging capacity "
            "until %s (used=%s budget=%s scope=%s)", campaign.id,
            wait["next_eligible_at"], wait["used_24h"], wait["budget"], wait["scope_key"],
        )
    else:
        ledger.clear_capacity_wait(campaign)


class DispatchRunContext:
    """Per-run execution state shared by ``dispatch_campaign`` and the
    send loop: which lease we hold and why (if at all) we stopped."""

    def __init__(self, owner: str) -> None:
        self.owner = owner
        self.pause_reason: Optional[str] = None
        self.pause_detail: str = ""
        self.lease_lost = False
        # Budget snapshot when Meta's shared messaging limit stopped the run.
        self.capacity: Any = None
        self.post_accept_error_key: Optional[str] = None

    def pause(self, reason: str, detail: str = "") -> None:
        if self.pause_reason is None:
            self.pause_reason = reason
            self.pause_detail = detail

    @property
    def stopped(self) -> bool:
        return self.lease_lost or self.pause_reason is not None


# Local guards inside ``provider_send_message`` answer with an error
# envelope before anything reaches Meta.
_LOCAL_REFUSAL_CLASSIFICATIONS = frozenset({
    "automation_blocked", "conversation_quota_blocked", "recipient_invalid",
})
# Non-retryable sync rejections that still mean "stop sending from this
# number now" rather than "this recipient is bad".
_SYNC_THROTTLE_KEYS = {"spam_rate_limit": 5}


async def _dispatch_queued_rows(
    db: Session,
    *,
    campaign: Campaign,
    template: WhatsAppTemplate,
    wa_conn: Any,
    store_name: str,
    auto_coupon: bool,
    discount_pct: Optional[int],
    customers_by_phone: Dict[str, Customer],
    manual_coupon: str = "",
    only_wave_id: Optional[int] = None,
    ctx: Optional["DispatchRunContext"] = None,
) -> Tuple[int, int, List[str]]:
    """Send the campaign's ``queued`` rows, one atomic claim at a time.

    Must run under the campaign lease (``ctx.owner``). For every
    recipient:

      1. check the shared messaging budget and Meta throttling signals —
         stop (pause) instead of sending into a known refusal;
      2. claim the row (``queued → sending`` compare-and-set) and create
         its attempt — committed;
      3. build the payload, mark ``request_started`` — committed;
      4. call Meta and record exactly what we learned: accepted (wamid),
         rejected (explicit error), not sent (pre-connect transport
         failure) or uncertain (anything that may have been accepted).

    Uncertain rows are never re-sent by this loop or by retries.

    When ``only_wave_id`` is set only that wave's rows are considered.
    Returns ``(accepted_count, failed_count, error_messages)``.
    """
    from services.whatsapp_platform.service import provider_send_message  # noqa: PLC0415
    from services import campaign_send_ledger as ledger  # noqa: PLC0415

    if ctx is None:
        # Direct callers (tests, legacy paths) get a lease of their own.
        ctx = DispatchRunContext(ledger.new_worker_id())
        if not ledger.ledger_available(db):
            return 0, 0, ["dispatch_skipped:ledger_unavailable"]
        got = ledger.acquire_lease(
            db, campaign_id=campaign.id, tenant_id=campaign.tenant_id, owner=ctx.owner,
        )
        if not got.acquired:
            return 0, 0, [f"dispatch_skipped:{got.reason}"]
        own_lease = True
    else:
        own_lease = False

    campaign_id = int(campaign.id)
    tenant_id = int(campaign.tenant_id)
    sent = 0
    failed = 0
    errors: List[str] = []
    batch_size = MARKETING_CAMPAIGN_BATCH_SIZE
    pause = MARKETING_CAMPAIGN_BATCH_PAUSE_SECONDS

    # ── Same-error-code circuit breaker ───────────────────────────────
    # A *retryable* code (rate_limit, service_unavailable) repeating
    # SAME_CODE_BREAKER_THRESHOLD times aborts the run so we stop
    # hammering Meta while it asks us to back off.
    SAME_CODE_BREAKER_THRESHOLD = 25
    same_code_counts: Dict[str, int] = {}
    abort_reason: Optional[str] = None
    consecutive_uncertain = 0

    # Resolve whatever a dead worker left in flight before drawing work,
    # and apply status events that arrived before their attempts.
    _revive_zombie_sending(db, campaign_id, owner=ctx.owner)
    db.commit()
    ledger.apply_pending_events_for_campaign(db, campaign_id)

    scope_key = ledger.messaging_scope_key(wa_conn)
    phone_number_id = getattr(wa_conn, "phone_number_id", None)

    # Rows handled in THIS invocation are never drawn again by it.
    processed_ids: set = set()
    safety_iterations = 0
    max_safety_iterations = max(50, (campaign.audience_count or 0) * 2)

    try:
        while not ctx.stopped and abort_reason is None:
            safety_iterations += 1
            if safety_iterations > max_safety_iterations:
                logger.critical(
                    "[campaign_dispatcher] campaign_send_retry_storm campaign=%d "
                    "outer loop hit safety cap (iterations=%d) — aborting",
                    campaign_id, safety_iterations,
                )
                break

            # CRITICAL: only ``queued``. Failed and uncertain rows are
            # terminal within a run; retries go through
            # ``reschedule_failed_for_retry``.
            batch_q = (
                db.query(CampaignSendLog.id, CampaignSendLog.customer_phone_e164)
                .filter(
                    CampaignSendLog.campaign_id == campaign_id,
                    CampaignSendLog.status == LOG_QUEUED,
                    ~CampaignSendLog.id.in_(processed_ids) if processed_ids else True,
                )
                .order_by(CampaignSendLog.id.asc())
                .limit(batch_size)
            )
            if only_wave_id is not None:
                batch_q = batch_q.filter(CampaignSendLog.wave_id == only_wave_id)
            batch = [(int(i), p) for i, p in batch_q.all()]
            if not batch:
                break

            for log_id, phone in batch:
                processed_ids.add(log_id)

                # ── 1. Stop before sending into a known refusal ──────
                budget = ledger.messaging_budget(db, wa_conn)
                if not budget.allows(phone):
                    ctx.capacity = budget
                    ctx.pause(
                        ledger.PAUSE_MESSAGING_LIMIT,
                        f"used={budget.used} budget={budget.budget} limit={budget.limit} "
                        f"source={budget.limit_source} scope={budget.scope_key}",
                    )
                    logger.warning(
                        "[campaign_dispatcher] campaign=%d paused: shared messaging "
                        "budget exhausted %s", campaign_id, budget.to_dict(),
                    )
                    break
                throttle = ledger.post_accept_throttle(db, scope_key)
                if throttle is not None:
                    ctx.post_accept_error_key = throttle[0]
                    _tstat = ledger.post_accept_throttle_status(db, scope_key) or {}
                    ctx.pause(
                        ledger.PAUSE_PROVIDER_THROTTLING,
                        f"post_accept {throttle[0]} x{throttle[1]} in "
                        f"{int(ledger.POST_ACCEPT_BREAKER_WINDOW.total_seconds() // 60)}m"
                        f" clears_at={_tstat.get('clears_at', '')}",
                    )
                    logger.critical(
                        "[campaign_dispatcher] campaign=%d paused: Meta post-accept "
                        "throttling %s x%d scope=%s", campaign_id, throttle[0],
                        throttle[1], scope_key,
                    )
                    break

                # ── Legacy attempt ceilings (no Meta call) ───────────
                row = db.get(CampaignSendLog, log_id)
                if row is None:
                    continue
                db.refresh(row)
                if row.status != LOG_QUEUED:
                    continue
                if _force_terminate_runaway(row, campaign_id=campaign_id):
                    failed += 1
                    db.commit()
                    continue
                if _is_attempts_exhausted(row):
                    row.status = LOG_FAILED
                    row.error_code = "retry_exhausted"
                    row.error_message = f"Exceeded {MAX_SEND_ATTEMPTS} send retries"
                    row.updated_at = datetime.now(timezone.utc)
                    failed += 1
                    logger.warning(
                        "[campaign_dispatcher] campaign=%d row=%s retry_exhausted attempts=%d",
                        campaign_id, log_id, row.attempt_count or 0,
                    )
                    db.commit()
                    continue

                # ── 2. Atomic claim ──────────────────────────────────
                claim = ledger.claim_recipient(
                    db, log_id=log_id, campaign=campaign, owner=ctx.owner,
                    scope_key=scope_key, phone_number_id=phone_number_id,
                    wa_conn=wa_conn,
                )
                if claim.attempt is None:
                    if claim.reason == "budget_exhausted":
                        b = claim.budget
                        ctx.capacity = b
                        ctx.pause(
                            ledger.PAUSE_MESSAGING_LIMIT,
                            f"used={b.used} budget={b.budget} limit={b.limit} "
                            f"source={b.limit_source} scope={b.scope_key}" if b else "",
                        )
                        logger.warning(
                            "[campaign_dispatcher] campaign=%d paused at claim: shared "
                            "messaging budget exhausted %s",
                            campaign_id, b.to_dict() if b else {},
                        )
                        break
                    if claim.reason == "lease_lost":
                        ctx.lease_lost = True
                        logger.error(
                            "[campaign_dispatcher] campaign=%d lease lost by %s — "
                            "stopping without further sends", campaign_id, ctx.owner,
                        )
                        break
                    if claim.reason == "stop_requested":
                        ctx.pause(ledger.PAUSE_MERCHANT_STOP, "stop requested")
                        break
                    if claim.reason == ledger.EVIDENCE_UNREADABLE:
                        # The send guard could not read the recipient's
                        # history: stop the run rather than guess.
                        ctx.pause(ledger.PAUSE_EVIDENCE_UNREADABLE, claim.evidence or "")
                        break
                    if claim.reason == ledger.EVIDENCE_UNRESOLVED:
                        # History that could be anyone's: stop the run with
                        # every row still queued until it is resolved.
                        ctx.pause(ledger.PAUSE_EVIDENCE_UNRESOLVED, claim.evidence or "")
                        break
                    continue
                attempt = claim.attempt

                customer = customers_by_phone.get(phone)
                from core.customer_display import (  # noqa: PLC0415
                    personalization_customer_name_or_fallback,
                )
                customer_name = personalization_customer_name_or_fallback(
                    customer.name if customer else None
                )

                # ── 3. Build the request (nothing sent yet) ──────────
                try:
                    # MANUAL template: merchant's code verbatim. AUTO
                    # template: one generated code per recipient — the
                    # only path that may call ``_get_auto_coupon``.
                    if auto_coupon and discount_pct and customer:
                        coupon_code = await _get_auto_coupon(
                            db, tenant_id, customer, discount_pct,
                        )
                    else:
                        coupon_code = manual_coupon
                    payload = _build_send_payload(
                        template=template,
                        to_phone=phone,
                        customer_name=customer_name,
                        store_name=store_name,
                        coupon_code=coupon_code,
                    )
                    _log_send_attempt(
                        campaign_id=campaign_id,
                        template=template,
                        recipient_phone=phone,
                        payload=payload,
                    )
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    from services.meta_errors import label_for  # noqa: PLC0415
                    ledger.record_not_sent(
                        db, attempt, error_code="internal_error",
                        technical=f"[payload_build {type(exc).__name__}] {str(exc)[:300]}",
                    )
                    db.commit()
                    failed += 1
                    if len(errors) < 10:
                        errors.append(f"{phone}: {label_for('internal_error')} [internal_error]")
                    logger.error(
                        "[campaign_dispatcher] campaign=%d payload build failed for %s: %s",
                        campaign_id, phone, exc, exc_info=True,
                    )
                    continue

                ledger.mark_request_started(db, attempt)

                # ── 4. Send and record exactly what we learned ───────
                try:
                    response, _ctx = await provider_send_message(
                        db,
                        wa_conn,
                        tenant_id=tenant_id,
                        operation="campaign_send",
                        phone_id=wa_conn.phone_number_id,
                        payload=payload,
                    )
                except Exception as exc:  # noqa: BLE001
                    try:
                        db.rollback()
                    except Exception:  # noqa: silent-ok — the send outcome is recorded just below
                        pass
                    exc_class = type(exc).__name__
                    http_status = (
                        getattr(exc, "status_code", None)
                        or getattr(getattr(exc, "response", None), "status_code", None)
                    )
                    technical = f"[exception={exc_class} http={http_status or '—'}] {str(exc)[:400]}"
                    if ledger.exception_proves_not_sent(exc):
                        ledger.record_not_sent(db, attempt, technical=technical)
                        failed += 1
                        consecutive_uncertain = 0
                        err_key = "transport_not_sent"
                    else:
                        # Timeout/protocol error after the request left:
                        # Meta may have accepted it. Never retried blindly.
                        ledger.record_uncertain(
                            db, attempt, reason="transport_error_after_send", technical=technical,
                        )
                        consecutive_uncertain += 1
                        err_key = "send_outcome_unknown"
                    db.commit()
                    if len(errors) < 10:
                        from services.meta_errors import label_for  # noqa: PLC0415
                        errors.append(f"{phone}: {label_for(err_key)} [{err_key}]")
                    logger.error(
                        "[campaign_dispatcher] campaign=%d exception sending to %s "
                        "(attempt=%s outcome=%s): %s",
                        campaign_id, phone, attempt.id, err_key, exc,
                    )
                else:
                    resp = response or {}
                    meta_err = resp.get("error") if isinstance(resp, dict) else None
                    if meta_err:
                        if isinstance(meta_err, dict):
                            meta_msg = (
                                meta_err.get("message")
                                or meta_err.get("error_user_msg")
                                or "Unknown Meta error"
                            )
                            meta_code = meta_err.get("code")
                            meta_subcode = meta_err.get("error_subcode")
                            meta_type = meta_err.get("type")
                        else:
                            meta_msg = str(meta_err) or "Unknown Meta error"
                            meta_code = meta_subcode = meta_type = None
                        from services.meta_errors import (  # noqa: PLC0415
                            classify_meta_error, format_technical,
                        )
                        technical = format_technical(
                            code=meta_code, subcode=meta_subcode,
                            error_type=meta_type, message=meta_msg,
                        )
                        if meta_type == "non_json_response":
                            # Gateway / HTML error page: we cannot tell
                            # whether Meta processed the request.
                            ledger.record_uncertain(
                                db, attempt, reason="provider_non_json_response",
                                technical=technical,
                            )
                            consecutive_uncertain += 1
                            db.commit()
                            if len(errors) < 10:
                                from services.meta_errors import label_for  # noqa: PLC0415
                                errors.append(
                                    f"{phone}: {label_for('send_outcome_unknown')} "
                                    f"[send_outcome_unknown]"
                                )
                        else:
                            consecutive_uncertain = 0
                            classified = classify_meta_error(
                                code=meta_code, subcode=meta_subcode,
                                error_type=meta_type, message=meta_msg,
                                raw_response=resp,
                            )
                            ledger.record_rejected(
                                db, attempt, error_key=classified.key,
                                raw_code=meta_code, technical=technical,
                            )
                            _record_raw_meta_sample(
                                campaign=campaign,
                                recipient_phone=phone,
                                meta_code=meta_code,
                                meta_subcode=meta_subcode,
                                meta_type=meta_type,
                                meta_message=meta_msg,
                                request_payload=payload,
                                response_payload=resp,
                                classified_key=classified.key,
                                template=template,
                            )
                            if classified.key == "unknown":
                                try:
                                    from services import meta_errors as _me  # noqa: PLC0415
                                    _me.note_unknown_code(
                                        code=meta_code, subcode=meta_subcode,
                                        error_type=meta_type, message=meta_msg,
                                    )
                                except Exception:  # noqa: silent-ok — fingerprint registry is best-effort
                                    pass
                            db.commit()
                            failed += 1
                            if len(errors) < 10:
                                errors.append(
                                    f"{phone}: {classified.label_ar} [{classified.key}]"
                                )
                            log_method = (
                                logger.error if classified.key == "unknown"
                                else logger.warning
                            )
                            log_method(
                                "[campaign_dispatcher] campaign=%d Meta error "
                                "key=%s code=%s subcode=%s type=%s phone=%s msg=%s "
                                "retryable=%s severity=%s",
                                campaign_id, classified.key, meta_code,
                                meta_subcode, meta_type, phone, meta_msg,
                                classified.retryable, classified.severity,
                            )
                            bucket = classified.key
                            local_refusal = (
                                isinstance(resp, dict)
                                and resp.get("_nahla_classification") in _LOCAL_REFUSAL_CLASSIFICATIONS
                            )
                            if (classified.retryable or bucket in _SYNC_THROTTLE_KEYS) and not local_refusal:
                                same_code_counts[bucket] = same_code_counts.get(bucket, 0) + 1
                                limit = _SYNC_THROTTLE_KEYS.get(bucket, SAME_CODE_BREAKER_THRESHOLD)
                                if same_code_counts[bucket] >= limit:
                                    abort_reason = (
                                        f"same_code_circuit_breaker:{bucket}"
                                        f"@{same_code_counts[bucket]}"
                                    )
                                    if bucket in _SYNC_THROTTLE_KEYS:
                                        ctx.pause(ledger.PAUSE_PROVIDER_THROTTLING, abort_reason)
                                    elif bucket in ledger.RATE_LIMIT_BUCKETS:
                                        # Meta asked us to slow down (per-minute
                                        # limit, e.g. 130429): back off for a
                                        # recorded time and continue then,
                                        # instead of ending the run.
                                        ctx.pause(ledger.PAUSE_PROVIDER_RATE_LIMITED, abort_reason)
                                    else:
                                        # Any other retryable code repeating is
                                        # not proven temporary: stop for the
                                        # merchant, never resume on a timer.
                                        ctx.pause(ledger.PAUSE_PROVIDER_REPEATED_ERROR, abort_reason)
                                    logger.critical(
                                        "[campaign_dispatcher] campaign=%d "
                                        "same_code_circuit_breaker tripped key=%s "
                                        "count=%d threshold=%d — aborting run",
                                        campaign_id, bucket, same_code_counts[bucket], limit,
                                    )
                    else:
                        messages = resp.get("messages") if isinstance(resp, dict) else None
                        first = messages[0] if isinstance(messages, list) and messages else None
                        wa_msg_id = first.get("id") if isinstance(first, dict) else ""
                        if not wa_msg_id:
                            # A success-shaped answer without a wamid may
                            # still be an accepted message — not provably
                            # failed, so not retried.
                            ledger.record_uncertain(
                                db, attempt, reason="accepted_without_wamid",
                                technical="Meta returned success without messages[0].id",
                            )
                            consecutive_uncertain += 1
                            db.commit()
                            if len(errors) < 10:
                                from services.meta_errors import label_for  # noqa: PLC0415
                                errors.append(
                                    f"{phone}: {label_for('no_message_id')} [no_message_id]"
                                )
                        else:
                            consecutive_uncertain = 0
                            ledger.record_accepted(db, attempt, wamid=wa_msg_id)
                            sent += 1
                            if customer:
                                rendered = _reconstruct_template_body(
                                    template, customer_name, store_name, coupon_code,
                                )
                                _record_campaign_message(
                                    db, tenant_id, campaign_id, customer,
                                    phone, template, rendered,
                                    wa_message_id=wa_msg_id,
                                )
                            # Opens / refreshes the 24h marketing window so
                            # usage counters reflect this send; idempotent
                            # for an already-open window.
                            try:
                                from core.wa_usage import track_conversation  # noqa: PLC0415
                                track_conversation(
                                    db, tenant_id, phone,
                                    source="campaign", category="marketing",
                                )
                            except Exception as _track_exc:  # noqa: BLE001
                                logger.warning(
                                    "[campaign_dispatcher] track_conversation failed "
                                    "campaign=%d phone=***%s err=%s",
                                    campaign_id, phone[-4:] if phone else "?", _track_exc,
                                )
                            db.commit()
                            # Status events that beat this commit.
                            ledger.apply_pending_events_for(db, attempt)
                            logger.info(
                                "[campaign_dispatcher] campaign=%d sent OK to %s wamid=%s "
                                "attempt=%s attempt_no=%s",
                                campaign_id, phone, wa_msg_id, attempt.id, attempt.attempt_no,
                            )

                if consecutive_uncertain >= ledger.UNCERTAIN_BREAKER_THRESHOLD:
                    ctx.pause(
                        ledger.PAUSE_UNCERTAIN,
                        f"{consecutive_uncertain} consecutive sends with unknown outcome",
                    )
                    logger.critical(
                        "[campaign_dispatcher] campaign=%d paused after %d consecutive "
                        "uncertain sends", campaign_id, consecutive_uncertain,
                    )
                    break
                if abort_reason is not None:
                    break
                await asyncio.sleep(INTER_MESSAGE_DELAY)

            campaign.sent_count = _count_log_statuses(db, campaign_id).get(LOG_SENT, 0)
            db.commit()
            if abort_reason is not None:
                errors.append(f"dispatch_aborted:{abort_reason}")
                break
            if ctx.stopped:
                break
            if pause > 0:
                await asyncio.sleep(pause)
    finally:
        if own_lease:
            try:
                ledger.release_lease(
                    db, campaign_id=campaign_id, owner=ctx.owner,
                    pause_reason=ctx.pause_reason, pause_detail=ctx.pause_detail,
                )
            except Exception:  # noqa: BLE001
                db.rollback()

    if ctx.pause_reason:
        errors.append(f"dispatch_paused:{ctx.pause_reason}")
    return sent, failed, errors


def _persist_dispatch_result(
    campaign: Campaign,
    sent: int,
    failed: int,
    skipped: int,
    errors: List[str],
) -> None:
    """Store dispatch metrics in the campaign's JSONB template_variables
    under private underscore keys so they survive without a migration."""
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
    tpl_vars = dict(campaign.template_variables or {})
    tpl_vars["_failed_count"] = str(failed)
    tpl_vars["_skipped_count"] = str(skipped)
    tpl_vars["_dispatch_errors"] = "|".join(errors[:10]) if errors else ""
    campaign.template_variables = tpl_vars
    flag_modified(campaign, "template_variables")


def _load_template(db: Session, campaign: Campaign) -> Optional[WhatsAppTemplate]:
    try:
        tpl_id = int(campaign.template_id)
    except (TypeError, ValueError):
        return None
    tpl = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.id == tpl_id,
            WhatsAppTemplate.tenant_id == campaign.tenant_id,
        )
        .first()
    )
    if tpl and (tpl.status or "").upper() == "APPROVED":
        return tpl
    return None


def _get_wa_connection(db: Session, tenant_id: int) -> Optional[Any]:
    return (
        db.query(WhatsAppConnection)
        .filter(
            WhatsAppConnection.tenant_id == tenant_id,
            WhatsAppConnection.status == "connected",
        )
        .first()
    )


def _resolve_audience(
    db: Session, tenant_id: int, audience_type: str,
) -> List[Customer]:
    # Use the unified-membership query so the campaign audience always
    # matches what the merchant sees on the customers page chip filter.
    # Falling back to ``build_segment_query`` here (auto-only) is what
    # produced the "I tagged هيثم but he's not in the campaign" bug.
    from services.nahla_segments import build_unified_segment_query

    # Special pseudo-segment: the wizard's "test recipients" quick
    # action targets the tenant's internal test list (Customers flagged
    # via `services.manual_segments.set_test_recipient`). It is NOT a
    # registered Nahla segment because it must never appear in chips
    # next to "VIP" / "new" — it's a dry-run helper, not an audience.
    aud_norm = (audience_type or "").strip().lower()
    if aud_norm == "test_recipients":
        from services.manual_segments import list_test_recipient_customer_ids  # noqa: PLC0415
        ids = list_test_recipient_customer_ids(db, tenant_id)
        if not ids:
            return []
        return (
            db.query(Customer)
            .filter(
                Customer.tenant_id == tenant_id,
                Customer.id.in_(ids),
                Customer.normalized_phone.isnot(None),
                Customer.normalized_phone != "",
            )
            .all()
        )

    # Legacy wizard targeting ``manual:<key>`` — kept as an alias for
    # the unified query on the same key. Merchants no longer see
    # auto-vs-manual as a choice (the platform is moving to a single
    # "final membership" concept), but old saved campaigns may still
    # have ``manual:<key>`` in audience_type so we accept it.
    if aud_norm.startswith("manual:"):
        seg_key = aud_norm.split(":", 1)[1]
        q = build_unified_segment_query(
            seg_key, db, tenant_id, require_reachable=True,
        )
        if q is None:
            return []
        return q.all()

    q = build_unified_segment_query(audience_type, db, tenant_id, require_reachable=True)
    if q is None:
        is_unsubscribed = cast(
            Customer.extra_metadata.op("->>")("is_unsubscribed"), String,
        )
        is_pending = cast(
            Customer.extra_metadata.op("->>")("pending_unsubscribe"), String,
        )
        pending_expires_at = cast(
            Customer.extra_metadata.op("->>")("pending_unsubscribe_expires_at"), String,
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        not_unsubscribed = or_(
            is_unsubscribed.is_(None),
            and_(is_unsubscribed != "true", is_unsubscribed != "1"),
        )
        not_pending = or_(
            is_pending.is_(None),
            and_(is_pending != "true", is_pending != "1"),
            pending_expires_at <= now_iso,
        )
        q = (
            db.query(Customer)
            .filter(
                Customer.tenant_id == tenant_id,
                Customer.normalized_phone.isnot(None),
                Customer.normalized_phone != "",
                # Safety net: honour opt-outs (and pending opt-outs) even
                # for the fallback "all" query
                not_unsubscribed,
                not_pending,
            )
        )
    return q.all()


def _load_customers_for_wave(
    db: Session, *, campaign_id: int, wave_id: int,
) -> List[Customer]:
    """Load every ``Customer`` whose ``CampaignSendLog`` row belongs
    to the given wave AND is still pending (``queued``).

    Used by the wave-aware path of ``dispatch_campaign`` so each
    wave dispatcher run gets the per-recipient context
    (``customer.id``, ``customer.name``, ``customer.normalized_phone``)
    it needs to personalise the template — without re-running the
    audience segment query (which would pick up customers added
    AFTER launch, defeating the wave plan).

    Returns at most ``planned_recipients`` customers per wave. The
    legacy non-wave code path is unaffected.
    """
    rows = (
        db.query(Customer)
        .join(
            CampaignSendLog,
            CampaignSendLog.customer_id == Customer.id,
        )
        .filter(
            CampaignSendLog.campaign_id == campaign_id,
            CampaignSendLog.wave_id == wave_id,
            CampaignSendLog.status == LOG_QUEUED,
        )
        .all()
    )
    return list(rows)


def _resolve_store_name(db: Session, tenant_id: int) -> str:
    try:
        from core.store_display import clean_store_name  # noqa: PLC0415
        from core.tenant import get_or_create_settings, merge_defaults, DEFAULT_STORE
        settings = get_or_create_settings(db, tenant_id)
        store = merge_defaults(settings.store_settings, DEFAULT_STORE)
        raw = store.get("store_name", "") or ""
        return clean_store_name(raw) or "المتجر"
    except Exception:
        return "المتجر"


def _extract_param_count(text: str) -> int:
    """Return the highest {{N}} index found in *text*, or 0."""
    import re
    if not text:
        return 0
    matches = re.findall(r"\{\{(\d+)\}\}", text)
    return max((int(m) for m in matches), default=0)


def _example_param_count(comp: Dict[str, Any], key: str) -> int:
    """Fallback: derive parameter count from the example field."""
    ex = comp.get("example") or {}
    vals = ex.get(key) or []
    if isinstance(vals, list) and vals:
        inner = vals[0] if isinstance(vals[0], list) else vals
        return len(inner)
    return 0


def _button_needs_param(btn: Dict[str, Any]) -> bool:
    """Return True if Meta requires a runtime parameter for this button.

    Only dynamic URL buttons (with ``{{1}}`` in the URL) need a suffix
    parameter.  Static URL buttons and QUICK_REPLY buttons need nothing.
    COPY_CODE always needs a coupon_code parameter.
    """
    btype = (btn.get("type") or "").upper()
    if btype == "COPY_CODE":
        return True
    if btype == "URL":
        url = btn.get("url") or ""
        return "{{" in url
    if btype == "OTP":
        return True
    return False


class PayloadValidationError(Exception):
    """Raised when the template payload cannot be built correctly."""


def validate_template_payload(
    template: WhatsAppTemplate,
    coupon_code: str = "",
) -> List[str]:
    """Pre-flight check. Returns a list of human-readable Arabic issues.
    Empty list = everything OK."""
    issues: List[str] = []
    from services.template_image_header import campaign_image_parameter
    try:
        campaign_image_parameter(template.components)
    except ValueError as exc:
        issues.append(str(exc))
    for comp in (template.components or []):
        ctype = (comp.get("type") or "").upper()
        if ctype == "BUTTONS":
            for idx, btn in enumerate(comp.get("buttons") or []):
                btype = (btn.get("type") or "").upper()
                if btype == "COPY_CODE" and not coupon_code:
                    issues.append(
                        f"الزر رقم {idx} (نسخ كود) يحتاج كود خصم لكن لم يتم تمرير كوبون. "
                        f"فعّل الكوبون التلقائي أو أضف كوبوناً يدوياً."
                    )
                if btype == "URL":
                    url = btn.get("url") or ""
                    if "{{" in url:
                        pass
                if btype == "OTP":
                    issues.append(
                        f"الزر رقم {idx} (OTP) غير مدعوم حالياً في الحملات."
                    )
    return issues


def _build_send_payload(
    *,
    template: WhatsAppTemplate,
    to_phone: str,
    customer_name: str,
    store_name: str,
    coupon_code: str = "",
    cart_url: str = "",
) -> Dict[str, Any]:
    """Build the full Meta Cloud API payload for a template message.

    Handles HEADER, BODY, and ALL button types: URL (dynamic suffix),
    COPY_CODE, QUICK_REPLY, and OTP.
    """
    slot_values = [
        customer_name,
        store_name,
        coupon_code or store_name,
        store_name,
        coupon_code or "",
        store_name,
    ]

    def _make_text_params(count: int) -> List[Dict[str, str]]:
        params: List[Dict[str, str]] = []
        for i in range(count):
            val = slot_values[i] if i < len(slot_values) else store_name
            params.append({"type": "text", "text": str(val).strip() or " "})
        return params

    components: List[Dict[str, Any]] = []
    from services.template_image_header import campaign_image_parameter
    try:
        image_parameter = campaign_image_parameter(template.components)
    except ValueError as exc:
        raise PayloadValidationError(str(exc)) from exc
    if image_parameter is not None:
        components.append(image_parameter)

    for comp in (template.components or []):
        ctype = (comp.get("type") or "").upper()
        text = comp.get("text") or ""

        if ctype == "HEADER":
            fmt = (comp.get("format") or "").upper()
            if fmt == "TEXT":
                count = _extract_param_count(text) or _example_param_count(comp, "header_text")
                if count > 0:
                    components.append({"type": "header", "parameters": _make_text_params(count)})

        elif ctype == "BODY":
            count = _extract_param_count(text) or _example_param_count(comp, "body_text")
            if count > 0:
                components.append({"type": "body", "parameters": _make_text_params(count)})

        elif ctype == "BUTTONS":
            for idx, btn in enumerate(comp.get("buttons") or []):
                btype = (btn.get("type") or "").upper()

                if btype == "COPY_CODE":
                    code = coupon_code or "NAHLA"
                    components.append({
                        "type": "button",
                        "sub_type": "copy_code",
                        "index": str(idx),
                        "parameters": [{"type": "coupon_code", "coupon_code": code}],
                    })

                elif btype == "URL":
                    url_tpl = btn.get("url") or ""
                    if "{{" in url_tpl:
                        suffix = cart_url or coupon_code or "shop"
                        components.append({
                            "type": "button",
                            "sub_type": "url",
                            "index": str(idx),
                            "parameters": [{"type": "text", "text": suffix}],
                        })

                elif btype == "OTP":
                    components.append({
                        "type": "button",
                        "sub_type": "url",
                        "index": str(idx),
                        "parameters": [{"type": "text", "text": "000000"}],
                    })

    logger.info(
        "[_build_send_payload] template=%s → %d components built from %d raw",
        template.name, len(components),
        len(template.components or []),
    )

    return {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "template",
        "template": {
            "name": template.name,
            "language": {"code": template.language or "ar"},
            "components": components,
        },
    }


async def _get_auto_coupon(
    db: Session,
    tenant_id: int,
    customer: Customer,
    discount_pct: int,
) -> str:
    try:
        from services.coupon_generator import CouponGeneratorService
        svc = CouponGeneratorService(db, tenant_id)
        segment = getattr(customer, "customer_status", None) or "active"
        coupon = svc.pick_coupon_for_segment(segment)
        if coupon:
            return coupon.code or ""
        coupon = await svc.create_on_demand(segment, discount_pct)
        if coupon:
            return coupon.code or ""
    except Exception as exc:
        logger.warning(
            "[campaign_dispatcher] auto-coupon failed tenant=%d customer=%d: %s",
            tenant_id, customer.id, exc,
        )
    return ""


# ── Waiting for Meta messaging capacity ──────────────────────────────────


async def resume_capacity_waiting(db: Session, *, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Continue campaigns with an authoritative capacity/rate/marketing wait,
    once the limit has room again. Called by the scheduler; durable because
    everything it needs is in the database (campaign status, the lease's
    pause reason and stop flag, the recorded ``_capacity_wait``).

    A campaign continues only when all of these hold:

    * ``status='paused'`` with an explicitly supported automatic pause reason
      and no merchant stop (a stop/pause always wins — it needs the
      merchant's own resume);
    * a wait record written by a dispatch run of this version
      (``authorized_capacity_wait``). The pause columns alone are not
      authority: a campaign paused by an older build, or paused for any
      unsupported reason, continues only on the merchant's explicit resume, and
      this loop never writes a record for it;
    * no worker holds the lease;
    * its ``next_eligible_at`` has passed;
    * it still has ``queued`` recipients;
    * the shared budget now admits a new recipient.

    Otherwise the wait is re-recorded with the next moment a slot frees.
    Continuing goes through the normal leased dispatch path — atomic
    claims, the scope lock and the budget check are unchanged, so a
    resumed run that fills the limit again simply goes back to waiting.
    Wave campaigns are set back to ``active`` and their pending waves are
    dispatched by the wave scheduler.
    """
    from models import CampaignDispatchLease, CampaignWave  # noqa: PLC0415
    from services import campaign_send_ledger as ledger  # noqa: PLC0415

    now = now or ledger.utcnow()
    out: List[Dict[str, Any]] = []
    rows = (
        db.query(Campaign, CampaignDispatchLease)
        .join(CampaignDispatchLease, CampaignDispatchLease.campaign_id == Campaign.id)
        .filter(
            Campaign.status == "paused",
            CampaignDispatchLease.pause_reason.in_(tuple(ledger.AUTO_RESUME_REASONS)),
            CampaignDispatchLease.stop_requested_at.is_(None),
        )
        .all()
    )
    for campaign, lease in rows:
        cid = int(campaign.id)
        entry: Dict[str, Any] = {"campaign_id": cid}
        out.append(entry)
        if ledger.lease_is_live(lease, now=now):
            entry["action"] = "worker_running"
            continue
        wait = ledger.authorized_capacity_wait(campaign.template_variables, lease.pause_reason)
        if wait is None:
            entry["action"] = "needs_merchant_resume"
            continue
        due = wait["next_eligible_at"]
        due_at = ledger._naive(datetime.fromisoformat(due))
        check_at = due_at
        if lease.pause_reason == ledger.PAUSE_MESSAGING_LIMIT:
            # Recheck tier upgrades while waiting for a rolling-window slot.
            # Keep the displayed eligibility estimate separate from polling.
            try:
                since = ledger._naive(datetime.fromisoformat(wait["since"]))
                check_at = min(due_at, since + ledger.CAPACITY_RECHECK_INTERVAL)
            except (KeyError, TypeError, ValueError):
                pass  # old valid records retain their original deadline
        if now < check_at:
            entry.update(action="waiting", next_eligible_at=due)
            continue
        from routers.whatsapp_connect import _maybe_refresh_meta_tier  # noqa: PLC0415
        await _maybe_refresh_meta_tier(db, campaign.tenant_id, max_age_seconds=900)
        # The refresh can yield/commit. Re-read state: merchant stop, another
        # scheduler, cancellation or a live worker always wins.
        db.refresh(campaign)
        db.refresh(lease)
        if (campaign.status != "paused" or lease.stop_requested_at is not None
                or ledger.lease_is_live(lease, now=ledger.utcnow())
                or ledger.authorized_capacity_wait(campaign.template_variables, lease.pause_reason) != wait):
            entry["action"] = "state_changed"
            continue
        requeue = (lease.pause_reason == ledger.PAUSE_PROVIDER_RATE_LIMITED
                   or bool(wait.get("requeue_rate_limited")))
        wa_conn = _get_wa_connection(db, campaign.tenant_id)
        if wa_conn is None:
            entry["action"] = "no_connection"
            continue
        # Re-read the shared breaker before changing state or requeueing.
        # New/late receipts may have appeared during the durable cooldown.
        throttle = ledger.post_accept_throttle(db, ledger.messaging_scope_key(wa_conn), now=now)
        if throttle is not None:
            lease.pause_reason = ledger.PAUSE_PROVIDER_THROTTLING
            lease.pause_detail = f"post_accept {throttle[0]} x{throttle[1]}"
            lease.paused_at = now
            if throttle[0] == "marketing_blocked":
                wait = ledger.record_marketing_wait(campaign, now=now)
                entry.update(action="marketing_wait", next_eligible_at=wait["next_eligible_at"])
            else:
                ledger.clear_capacity_wait(campaign)
                entry.update(action="provider_action_required", error_key=throttle[0])
            db.commit()
            continue
        budget = ledger.messaging_budget(db, wa_conn, now=now)
        if budget.budget is not None and budget.used >= budget.budget:
            # Capacity first: a rate-limit backoff that finds the shared
            # limit full becomes a capacity wait (lease and record agree),
            # still owing its re-queue for when a slot frees.
            lease.pause_reason = ledger.PAUSE_MESSAGING_LIMIT
            wait = ledger.record_capacity_wait(campaign, budget, now=now)
            if requeue:
                wait["requeue_rate_limited"] = True
                ledger.store_capacity_wait(campaign, wait)
            db.commit()
            entry.update(action="still_full", next_eligible_at=wait["next_eligible_at"],
                         used_24h=budget.used, budget=budget.budget)
            continue
        if requeue:
            # Recipients Meta rejected for the per-minute limit were never
            # accepted — put the provably unsent ones back (the claim guard
            # still checks each one).
            entry["requeued_rate_limited"] = reschedule_failed_for_retry(db, cid)
            db.commit()
        queued = (
            db.query(func.count(CampaignSendLog.id))
            .filter(CampaignSendLog.campaign_id == cid, CampaignSendLog.status == LOG_QUEUED)
            .scalar()
        ) or 0
        if not queued:
            entry["action"] = "nothing_queued"
            continue
        has_waves = db.query(CampaignWave.id).filter(CampaignWave.campaign_id == cid).first()
        campaign.status = "active"
        campaign.updated_at = datetime.now(timezone.utc)
        if lease.pause_reason != ledger.PAUSE_PROVIDER_RATE_LIMITED:
            # A rate-limit backoff keeps its record through the run so a
            # second consecutive limit backs off longer; the run replaces
            # it if it pauses again and clears it otherwise.
            ledger.clear_capacity_wait(campaign)
        db.commit()
        logger.info(
            "[campaign_dispatcher] campaign=%d Meta capacity available (used=%s budget=%s) "
            "— continuing %d queued recipients%s", cid, budget.used, budget.budget, queued,
            " via the wave scheduler" if has_waves else "",
        )
        if has_waves:
            entry["action"] = "resumed_waves"
            continue
        result = await dispatch_campaign(db, cid)
        entry.update(action="resumed", status=result.get("status"),
                     sent=result.get("sent"), errors=(result.get("errors") or [])[:3])
    return out
