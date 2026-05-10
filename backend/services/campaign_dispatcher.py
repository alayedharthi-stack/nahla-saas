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

logger = logging.getLogger("nahla-backend")

INTER_MESSAGE_DELAY = 1.5

# ── Status constants for campaign_send_logs.status ────────────────────────
# Kept as module-level strings (not Enum) so DB rows stay compatible with
# the open Postgres String column and so we can grep for status names
# across the codebase without IDE help.
LOG_QUEUED              = "queued"
LOG_SENDING             = "sending"
LOG_SENT                = "sent"
LOG_FAILED              = "failed"
LOG_SKIPPED_DUPLICATE   = "skipped_duplicate"
LOG_SKIPPED_INVALID     = "skipped_invalid"
LOG_SKIPPED_UNSUBSCRIBED = "skipped_unsubscribed"
LOG_SKIPPED_UNREACHABLE = "skipped_unreachable"
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


async def dispatch_campaign(db: Session, campaign_id: int) -> Dict[str, Any]:
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

    Returns a summary dict including the per-status counters surfaced
    to the merchant in the campaign report.
    """
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        return _empty_result(error="Campaign not found")

    tenant_id = campaign.tenant_id
    logger.info(
        "[campaign_dispatcher] starting campaign=%d tenant=%d template_id=%s audience=%s",
        campaign_id, tenant_id, campaign.template_id, campaign.audience_type,
    )

    template = _load_template(db, campaign)
    if not template:
        err = "لم يتم العثور على القالب أو لم تتم الموافقة عليه"
        logger.warning("[campaign_dispatcher] campaign=%d: template not found or not APPROVED (id=%s)", campaign_id, campaign.template_id)
        campaign.status = "failed"
        _persist_dispatch_result(campaign, 0, 0, 0, [err])
        db.commit()
        return _empty_result(error=err)

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

    # ── 1. Snapshot recipients into campaign_send_logs ──────────────────
    snapshot = _snapshot_recipients(
        db, tenant_id, campaign_id, customers, template,
        excluded_segments=excluded_segments,
    )
    db.commit()

    # ── 2. Frequency-cap dedupe ─────────────────────────────────────────
    # Honour the per-campaign bypass toggle (set via the dispatch-now
    # button or directly on template_variables._bypass_frequency_cap).
    # Used in QA / re-test workflows where the merchant explicitly
    # wants to re-send to the same customers without waiting for the
    # global cap window to expire.
    bypass_cap_raw = (campaign.template_variables or {}).get(
        "_bypass_frequency_cap"
    )
    bypass_cap = str(bypass_cap_raw).strip().lower() in (
        "true", "1", "yes",
    ) if bypass_cap_raw is not None else False
    cap_skipped = _apply_frequency_cap(
        db, tenant_id, campaign_id, bypass=bypass_cap,
    )
    # One-shot bypass — remove the flag immediately after the cap
    # decision so the NEXT dispatch (scheduled or manual) runs normal
    # protection again. Merchants opt in per click via dispatch-now.
    if bypass_cap:
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
        tv_after = dict(campaign.template_variables or {})
        tv_after.pop("_bypass_frequency_cap", None)
        campaign.template_variables = tv_after
        flag_modified(campaign, "template_variables")
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

    sent, failed, errors = await _dispatch_queued_rows(
        db,
        campaign=campaign,
        template=template,
        wa_conn=wa_conn,
        store_name=store_name,
        auto_coupon=auto_coupon,
        discount_pct=discount_pct,
        customers_by_phone={
            (c.normalized_phone or ""): c for c in customers if c.normalized_phone
        },
    )

    # ── 4. Final counters & campaign status ─────────────────────────────
    counts = _count_log_statuses(db, campaign_id)
    final_status = (
        "completed" if counts.get("sent", 0) > 0
        else ("failed" if counts.get("failed", 0) > 0 else "completed")
    )
    campaign.sent_count = counts.get("sent", 0)
    campaign.status = final_status
    campaign.updated_at = datetime.now(timezone.utc)

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
        if meta.get("marketing_opt_out_manual"):
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


def _apply_frequency_cap(
    db: Session,
    tenant_id: int,
    campaign_id: int,
    *,
    bypass: bool = False,
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

    Args:
        bypass: If True, skip the cap check entirely and return 0.
            Used by the "تجاهل حد التكرار" toggle on dispatch-now for
            QA / re-test workflows. Always logged.

    Returns:
        Number of rows updated to ``skipped_duplicate``. A cap value
        of 0 also disables the check entirely (admin escape hatch —
        should never be 0 in prod).
    """
    if bypass:
        logger.info(
            "[campaign_dispatcher] campaign=%d frequency_cap BYPASSED "
            "(merchant requested via _bypass_frequency_cap)",
            campaign_id,
        )
        return 0

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

    sent_phones_subq = (
        db.query(CampaignSendLog.customer_phone_e164)
        .filter(
            CampaignSendLog.tenant_id == tenant_id,
            CampaignSendLog.status == LOG_SENT,
            delivered_proof,
            within_cap_window,
            # Don't dedupe a campaign against itself (re-runs).
            CampaignSendLog.campaign_id != campaign_id,
        )
        .distinct()
    )
    sent_phones = {row[0] for row in sent_phones_subq.all()}

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


def _summarise_send_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build the compact ``campaign_send_attempt`` log dict.

    Mirrors the shape the user explicitly asked for so the production
    log line is grep-able as JSON::

        {template_name, language, recipient, component_count,
         body_params, button_params, media}
    """
    tpl = (payload or {}).get("template") or {}
    components = tpl.get("components") or []
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
    return {
        "template_name":   tpl.get("name"),
        "language":        ((tpl.get("language") or {}).get("code")
                            if isinstance(tpl.get("language"), dict) else tpl.get("language")),
        "recipient":       _mask_phone_for_log(payload.get("to")),
        "component_count": len(components),
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
        summary = _summarise_send_payload(payload)
    except Exception:
        summary = {
            "template_name": getattr(template, "name", None),
            "recipient":     _mask_phone_for_log(recipient_phone),
        }
    logger.info(
        "[campaign_dispatcher] campaign_send_attempt campaign=%d %s",
        campaign_id, summary,
    )


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

        sample = {
            "ts":                  datetime.now(timezone.utc).isoformat(),
            "recipient":           _mask_phone_for_log(recipient_phone),
            "meta_error_code":     str(meta_code) if meta_code is not None else None,
            "meta_error_subcode":  str(meta_subcode) if meta_subcode is not None else None,
            "meta_error_type":     str(meta_type) if meta_type is not None else None,
            "meta_error_message":  str(meta_message or "")[:1000],
            "request_payload":     _mask_payload(request_payload),
            "response_payload":    _mask_payload(response_payload),
            "classified_key":      classified_key,
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
) -> Tuple[int, int, List[str]]:
    """Walk the campaign's ``queued`` and ``failed`` rows in batches and
    send each one. Already-sent rows are filtered out by the query
    itself, so a re-run of this function is safe.

    Returns ``(sent_count, failed_count, error_messages)``.
    """
    from services.whatsapp_platform.service import provider_send_message  # noqa: PLC0415

    sent = 0
    failed = 0
    errors: List[str] = []
    batch_size = MARKETING_CAMPAIGN_BATCH_SIZE
    pause = MARKETING_CAMPAIGN_BATCH_PAUSE_SECONDS

    while True:
        # Pull the next batch of work. We re-query each iteration
        # (rather than keep an in-memory list) so:
        #   * a parallel admin "force-resend" never duplicates a row.
        #   * crashes mid-batch resume cleanly on the next call.
        batch = (
            db.query(CampaignSendLog)
            .filter(
                CampaignSendLog.campaign_id == campaign.id,
                CampaignSendLog.status.in_([LOG_QUEUED, LOG_FAILED]),
            )
            .order_by(CampaignSendLog.id.asc())
            .limit(batch_size)
            .all()
        )
        if not batch:
            break

        for row in batch:
            # Idempotency guard: re-check status under the same row
            # (handles the case where another worker already grabbed it).
            if row.status == LOG_SENT:
                continue

            # Mark sending — visible in the dashboard status feed.
            row.status = LOG_SENDING
            row.attempt_count = (row.attempt_count or 0) + 1
            row.updated_at = datetime.now(timezone.utc)
            db.flush()

            phone = row.customer_phone_e164
            customer = customers_by_phone.get(phone)
            customer_name = (customer.name if customer else None) or "العميل"

            try:
                coupon_code = ""
                if auto_coupon and discount_pct and customer:
                    coupon_code = await _get_auto_coupon(
                        db, campaign.tenant_id, customer, discount_pct,
                    )

                payload = _build_send_payload(
                    template=template,
                    to_phone=phone,
                    customer_name=customer_name,
                    store_name=store_name,
                    coupon_code=coupon_code,
                )

                # ── PRE-SEND ATTEMPT LOG ──────────────────────────
                # Emit a single structured line per recipient BEFORE
                # we hit Meta. This is the line support greps for
                # "campaign_send_attempt" when an entire campaign
                # fails with `unknown` — it shows exactly which
                # template / language / parameter shape we shipped.
                _log_send_attempt(
                    campaign_id=campaign.id,
                    template=template,
                    recipient_phone=phone,
                    payload=payload,
                )

                response, _ctx = await provider_send_message(
                    db,
                    wa_conn,
                    tenant_id=campaign.tenant_id,
                    operation="campaign_send",
                    phone_id=wa_conn.phone_number_id,
                    payload=payload,
                )

                resp = response or {}
                meta_err = resp.get("error") if isinstance(resp, dict) else None
                if meta_err:
                    # Extract every signal Meta sends so we can
                    # classify into a canonical, merchant-readable
                    # key (services.meta_errors). The raw English
                    # message + numeric code are kept verbatim in
                    # ``error_message`` so support can still copy
                    # the technical details.
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
                        meta_code = None
                        meta_subcode = None
                        meta_type = None
                    from services.meta_errors import (  # noqa: PLC0415
                        classify_meta_error, format_technical,
                    )
                    classified = classify_meta_error(
                        code=meta_code, subcode=meta_subcode,
                        error_type=meta_type, message=meta_msg,
                        raw_response=resp,
                    )
                    row.status = LOG_FAILED
                    # ``error_code`` becomes the canonical key (e.g.
                    # ``not_on_whatsapp``) — the UI maps it to Arabic
                    # via the same module without needing more state.
                    row.error_code = classified.key[:64]
                    # ``error_message`` keeps the raw Meta string +
                    # numeric code so support can paste it into a
                    # ticket without losing fidelity. The canonical
                    # ``[code=X subcode=Y type=Z] msg`` shape is what
                    # ``parse_technical`` consumes to surface the raw
                    # fields separately in the UI.
                    technical = format_technical(
                        code=meta_code, subcode=meta_subcode,
                        error_type=meta_type, message=meta_msg,
                    )
                    row.error_message = technical[:500]
                    row.updated_at = datetime.now(timezone.utc)
                    # Persist a bounded list of raw fingerprints on the
                    # campaign so the debug endpoint can render the
                    # full Meta payload for every UNKNOWN error — this
                    # is the fingerprint-collection bucket support uses
                    # to grow the canonical classifier.
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
                    )
                    failed += 1
                    if len(errors) < 10:
                        # Friendly Arabic line for the campaign report
                        # (replaces the old "client_side (meta_error)"
                        # gibberish the merchant used to see).
                        errors.append(
                            f"{phone}: {classified.label_ar} "
                            f"[{classified.key}]"
                        )
                    # WARNING for known errors, ERROR for unknown so
                    # operators can grep production for new codes the
                    # classifier doesn't recognise yet.
                    log_method = (
                        logger.error if classified.key == "unknown"
                        else logger.warning
                    )
                    log_method(
                        "[campaign_dispatcher] campaign=%d Meta error "
                        "key=%s code=%s subcode=%s type=%s phone=%s msg=%s",
                        campaign.id, classified.key, meta_code,
                        meta_subcode, meta_type, phone, meta_msg,
                    )
                else:
                    messages = resp.get("messages") if isinstance(resp, dict) else None
                    first = messages[0] if isinstance(messages, list) and messages else None
                    wa_msg_id = first.get("id") if isinstance(first, dict) else ""
                    if not wa_msg_id:
                        # No id means Meta did not actually accept the
                        # message — treat as failure so we don't lie to
                        # the merchant by counting it as sent.
                        from services.meta_errors import label_for  # noqa: PLC0415
                        row.status = LOG_FAILED
                        row.error_code = "no_message_id"
                        row.error_message = (
                            "Meta accepted the request but did not "
                            "return a wamid"
                        )
                        row.updated_at = datetime.now(timezone.utc)
                        failed += 1
                        if len(errors) < 10:
                            errors.append(
                                f"{phone}: {label_for('no_message_id')} "
                                f"[no_message_id]"
                            )
                    else:
                        row.status = LOG_SENT
                        row.provider_message_id = wa_msg_id
                        row.sent_at = datetime.now(timezone.utc)
                        row.error_code = None
                        row.error_message = None
                        row.updated_at = datetime.now(timezone.utc)
                        sent += 1
                        if customer:
                            rendered = _reconstruct_template_body(
                                template, customer_name, store_name, coupon_code,
                            )
                            _record_campaign_message(
                                db, campaign.tenant_id, campaign.id, customer,
                                phone, template, rendered,
                                wa_message_id=wa_msg_id,
                            )
                        logger.info(
                            "[campaign_dispatcher] campaign=%d sent OK to %s wamid=%s",
                            campaign.id, phone, wa_msg_id,
                        )
            except Exception as exc:
                from services.meta_errors import label_for  # noqa: PLC0415
                row.status = LOG_FAILED
                row.error_code = "exception"
                row.error_message = (
                    f"[exception={type(exc).__name__}] {str(exc)[:480]}"
                )
                row.updated_at = datetime.now(timezone.utc)
                failed += 1
                if len(errors) < 10:
                    errors.append(
                        f"{phone}: {label_for('exception')} [exception]"
                    )
                logger.error(
                    "[campaign_dispatcher] campaign=%d exception sending to %s: %s",
                    campaign.id, phone, exc, exc_info=True,
                )

            # Update the campaign-level counter incrementally so the
            # dashboard's progress bar feels live.
            campaign.sent_count = sent
            await asyncio.sleep(INTER_MESSAGE_DELAY)

        # Flush + pause between batches so a parallel worker can pick
        # up the new state and Meta sees a steady cadence.
        db.commit()
        if pause > 0:
            await asyncio.sleep(pause)

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


def _resolve_store_name(db: Session, tenant_id: int) -> str:
    try:
        from core.tenant import get_or_create_settings, merge_defaults, DEFAULT_STORE
        settings = get_or_create_settings(db, tenant_id)
        store = merge_defaults(settings.store_settings, DEFAULT_STORE)
        return store.get("store_name", "") or "المتجر"
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
