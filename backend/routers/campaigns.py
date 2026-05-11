"""
routers/campaigns.py
─────────────────────
Campaign management endpoints.

Routes:
  GET  /campaigns                   — list all campaigns
  POST /campaigns                   — create a new campaign
  PUT  /campaigns/{id}/status       — update campaign status
  POST /campaigns/test-send         — simulate a test message send
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models import Campaign, CampaignSendLog, Customer, WhatsAppTemplate  # noqa: E402

from core.config import MARKETING_CAMPAIGN_FREQUENCY_CAP_DAYS
from core.database import get_db
from core.tenant import (
    get_or_create_tenant,
    resolve_tenant_id,
)

logger = logging.getLogger("nahla-backend")

router = APIRouter()


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class CreateCampaignIn(BaseModel):
    name: str
    campaign_type: str
    template_id: str
    template_name: str
    template_language: str = "ar"
    template_category: str = "MARKETING"
    template_body: str = ""
    # ``template_variables`` is a free-form bag the frontend uses for
    # both real placeholder values (e.g. {"1": "خصم 20%"}) AND for
    # internal control keys (``_exclude_segments`` is a string[],
    # ``_failed_count`` is an int-as-str, etc.). It MUST accept any
    # JSON value type — restricting to ``Dict[str, str]`` here was
    # the root cause of the 422 production bug ("API error 422" on
    # campaign launch when the wizard sent ``_exclude_segments: []``
    # because Pydantic rejected the list as not-a-string).
    # Normalisation back to strings happens server-side just before
    # we persist into ``Campaign.template_variables``.
    template_variables: Optional[Dict[str, Any]] = None
    audience_type: str = "all"
    audience_count: int = 0
    schedule_type: str = "immediate"
    schedule_time: Optional[str] = None
    delay_minutes: Optional[int] = None
    coupon_code: str = ""
    discount_percent: Optional[int] = None
    auto_coupon: bool = False


class UpdateCampaignStatusIn(BaseModel):
    status: str  # active | paused | completed


class TestSendIn(BaseModel):
    phone: str
    template_id: str
    template_name: str
    template_language: str = "ar"
    variables: Dict[str, str] = {}


# ── Helper functions ───────────────────────────────────────────────────────────

def _campaign_to_dict(c: Campaign) -> Dict[str, Any]:
    tpl_vars = c.template_variables or {}
    auto_coupon = tpl_vars.get("_auto_coupon") == "true"
    discount_pct_raw = tpl_vars.get("_discount_percent")
    discount_pct = int(discount_pct_raw) if discount_pct_raw else None

    failed_count = int(tpl_vars.get("_failed_count", "0") or "0")
    skipped_count = int(tpl_vars.get("_skipped_count", "0") or "0")
    raw_errors = tpl_vars.get("_dispatch_errors", "") or ""
    dispatch_errors = [e for e in raw_errors.split("|") if e] if raw_errors else []
    # ``last_error`` is what the UI puts under the status pill so the
    # merchant can tell ``failed`` from ``failed because the template
    # was unapproved`` at a glance. New writes already include an
    # Arabic label suffixed by ``[canonical_key]`` (see dispatcher),
    # but older rows pre-classifier hold raw English/dispatcher
    # strings — surface a tighter Arabic label by extracting the
    # canonical key from the bracketed suffix when present.
    last_error = dispatch_errors[0] if dispatch_errors else None
    last_error_ar: Optional[str] = None
    last_error_key: Optional[str] = None
    if last_error:
        try:
            from services.meta_errors import (  # noqa: PLC0415
                ERRORS as _META_ERRORS, classify_meta_error,
            )
            import re as _re  # noqa: PLC0415
            m = _re.search(r"\[([a-z_]+)\]\s*$", last_error)
            if m and m.group(1) in _META_ERRORS:
                ce = _META_ERRORS[m.group(1)]
                last_error_key = ce.key
                last_error_ar = ce.label_ar
            else:
                ce = classify_meta_error(message=last_error)
                last_error_key = ce.key
                last_error_ar = ce.label_ar
        except Exception:
            last_error_ar = last_error  # fallback — show raw text

    # Synthetic ``lifecycle`` field: a merchant-friendly verb derived
    # from ``status`` + the per-recipient counters we already persisted
    # to ``template_variables``. Display logic in the UI keys off this
    # instead of the raw status column so "نشطة" never lies — a
    # campaign that died before snapshotting recipients shows up as
    # ``pending_dispatch`` ("ينتظر بدء الإرسال") even though the
    # underlying status is still ``active``.
    sent = c.sent_count or 0
    total_seen = sent + failed_count + skipped_count
    raw_status = (c.status or "").lower()
    if raw_status == "completed":
        if sent > 0 and failed_count == 0:
            lifecycle = "sent"
        elif sent > 0 and failed_count > 0:
            lifecycle = "partial"
        elif failed_count > 0:
            lifecycle = "failed_all"
        else:
            lifecycle = "completed_empty"
    elif raw_status == "failed":
        lifecycle = "failed"
    elif raw_status == "scheduled":
        lifecycle = "waiting_scheduler"
    elif raw_status == "active":
        if total_seen == 0:
            lifecycle = "pending_dispatch"
        elif sent == 0 and failed_count == 0:
            lifecycle = "pending_dispatch"
        else:
            lifecycle = "sending"
    elif raw_status == "draft":
        lifecycle = "draft"
    else:
        lifecycle = raw_status or "unknown"

    return {
        "id": c.id,
        "name": c.name,
        "campaign_type": c.campaign_type,
        "status": c.status,
        # New: merchant-facing verb (see _classify_campaign_lifecycle).
        # The UI should prefer this over ``status`` for the badge label.
        "lifecycle": lifecycle,
        "template_id": c.template_id,
        "template_name": c.template_name,
        "template_language": c.template_language,
        "template_category": c.template_category,
        "template_status": getattr(c, "template_status", None) or "APPROVED",
        "template_body": c.template_body,
        "template_variables": {k: v for k, v in tpl_vars.items() if not k.startswith("_")},
        "audience_type": c.audience_type,
        "audience_count": c.audience_count,
        "schedule_type": c.schedule_type,
        "schedule_time": c.schedule_time.isoformat() if c.schedule_time else None,
        "delay_minutes": c.delay_minutes,
        "coupon_code": c.coupon_code or "",
        "auto_coupon": auto_coupon,
        "discount_percent": discount_pct,
        "sent_count": c.sent_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "dispatch_errors": dispatch_errors,
        "last_error": last_error,
        # Arabic-translated equivalent of last_error — UI uses this
        # under the status pill instead of the raw technical line.
        "last_error_ar": last_error_ar,
        "last_error_key": last_error_key,
        "delivered_count": c.delivered_count,
        "read_count": c.read_count,
        "clicked_count": c.clicked_count,
        "converted_count": c.converted_count,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "launched_at": c.launched_at.isoformat() if c.launched_at else None,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/campaigns/protection-info")
async def get_protection_info(request: Request, db: Session = Depends(get_db)):
    """Return marketing-campaign anti-spam protection settings.

    Powers the "🛡️ حماية ذكية من التكرار" trust card in the campaign
    wizard *before* a campaign exists, so the merchant can see the
    duplicate-protection window (default 14 days) at the moment of
    launch confirmation.
    """
    # Touching the tenant ensures auth still gates the endpoint, and
    # leaves room for per-tenant overrides later (a future per-tenant
    # ``marketing_campaign_frequency_cap_days`` setting can return a
    # different value here without touching the frontend).
    resolve_tenant_id(request, db)
    return {
        "frequency_cap_days": MARKETING_CAMPAIGN_FREQUENCY_CAP_DAYS,
        "idempotent_resend_protected": True,
    }


@router.get("/campaigns")
async def list_campaigns(request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    db.commit()
    campaigns = (
        db.query(Campaign)
        .filter(Campaign.tenant_id == tenant_id)
        .order_by(Campaign.created_at.desc())
        .all()
    )
    return {"campaigns": [_campaign_to_dict(c) for c in campaigns]}


@router.post("/campaigns")
async def create_campaign(
    body: CreateCampaignIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    from core.billing import require_outbound_access  # noqa: PLC0415
    require_outbound_access(db, tenant_id)

    # ── Entitlement checks ─────────────────────────────────────────────────────
    from core.plan_entitlements import (  # noqa: PLC0415
        get_entitlements,
        require_feature,
        require_limit_not_exceeded,
        entitlement_http_error,
        EntitlementError,
    )
    ent = get_entitlements(db, tenant_id)

    # campaign_customer_segments is the base feature — available Starter+
    # This also catches billing_blocked / no_active_subscription states.
    try:
        require_feature(ent, "campaign_customer_segments")
    except EntitlementError as exc:
        entitlement_http_error(exc)

    # Monthly campaign limit (Starter capped, Growth/Scale unlimited)
    from datetime import datetime as _dt2, timezone as _tz2  # noqa: PLC0415
    _month_start = _dt2.now(_tz2.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    _camp_this_month = (
        db.query(Campaign)
        .filter(
            Campaign.tenant_id == tenant_id,
            Campaign.created_at >= _month_start.replace(tzinfo=None),
        )
        .count()
    )
    try:
        require_limit_not_exceeded(ent, "campaigns_per_month", _camp_this_month)
    except EntitlementError as exc:
        entitlement_http_error(exc)

    # Advanced coupons inside campaigns require advanced_coupon_types (Growth+).
    # Abandoned-cart basic coupon is allowed for all plans via abandoned_cart_basic_coupon.
    if body.auto_coupon and body.campaign_type not in ("abandoned_cart", "cart_recovery"):
        try:
            require_feature(ent, "advanced_coupon_types")
        except EntitlementError as exc:
            entitlement_http_error(exc)

    # AI campaign optimization requires campaign_ai_optimization (Growth+)
    if getattr(body, "ai_optimized", False):
        try:
            require_feature(ent, "campaign_ai_optimization")
        except EntitlementError as exc:
            entitlement_http_error(exc)

    try:
        template_db_id = int(body.template_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="template_id غير صالح")

    template = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == template_db_id,
        WhatsAppTemplate.tenant_id == tenant_id,
    ).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    if template.status != "APPROVED":
        raise HTTPException(
            status_code=422,
            detail="لا يمكن إنشاء حملة إلا باستخدام قالب معتمد من Meta",
        )

    schedule_dt = None
    if body.schedule_time:
        try:
            from datetime import datetime as _dt
            schedule_dt = _dt.fromisoformat(body.schedule_time)
        except ValueError:
            pass

    # Coerce anything the frontend stuffed into template_variables to
    # strings (or JSON-strings for lists/dicts). The DB column is a
    # string-keyed JSON map and the dispatcher reads everything back
    # as strings, so any list/int needs serialising once here.
    raw_vars = dict(body.template_variables or {})
    tpl_vars: Dict[str, str] = {}
    for k, v in raw_vars.items():
        if isinstance(v, str):
            tpl_vars[k] = v
        elif isinstance(v, (list, dict)):
            import json as _json  # noqa: PLC0415
            tpl_vars[k] = _json.dumps(v, ensure_ascii=False)
        elif v is None:
            tpl_vars[k] = ""
        else:
            tpl_vars[k] = str(v)

    if body.auto_coupon:
        tpl_vars["_auto_coupon"] = "true"
    if body.discount_percent is not None and body.discount_percent > 0:
        tpl_vars["_discount_percent"] = str(body.discount_percent)

    is_immediate = body.schedule_type == "immediate"
    campaign = Campaign(
        tenant_id=tenant_id,
        name=body.name,
        campaign_type=body.campaign_type,
        status="active" if is_immediate else ("scheduled" if body.schedule_type == "scheduled" and schedule_dt else "draft"),
        template_id=str(template.id),
        template_name=template.name,
        template_language=template.language,
        template_category=template.category,
        template_body=next(
            (
                c.get("text", "")
                for c in (template.components or [])
                if isinstance(c, dict) and c.get("type") == "BODY"
            ),
            body.template_body,
        ),
        template_variables=tpl_vars,
        audience_type=body.audience_type,
        audience_count=body.audience_count,
        schedule_type=body.schedule_type,
        schedule_time=schedule_dt,
        delay_minutes=body.delay_minutes,
        coupon_code=body.coupon_code or None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        launched_at=datetime.now(timezone.utc) if is_immediate else None,
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)

    if is_immediate:
        asyncio.create_task(_dispatch_campaign_async(campaign.id))

    return _campaign_to_dict(campaign)


@router.put("/campaigns/{campaign_id}/status")
async def update_campaign_status(
    campaign_id: int,
    body: UpdateCampaignStatusIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    campaign = db.query(Campaign).filter(
        Campaign.id == campaign_id,
        Campaign.tenant_id == tenant_id,
    ).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    was_not_active = campaign.status != "active"
    campaign.status = body.status
    if body.status == "active" and not campaign.launched_at:
        campaign.launched_at = datetime.now(timezone.utc)
    campaign.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(campaign)

    if body.status == "active" and was_not_active:
        asyncio.create_task(_dispatch_campaign_async(campaign.id))

    return _campaign_to_dict(campaign)


@router.delete("/campaigns/{campaign_id}")
async def delete_campaign(
    campaign_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    campaign = db.query(Campaign).filter(
        Campaign.id == campaign_id,
        Campaign.tenant_id == tenant_id,
    ).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    db.delete(campaign)
    db.commit()
    return {"deleted": True, "id": campaign_id}


class BulkDeleteIn(BaseModel):
    ids: List[int]


@router.post("/campaigns/bulk-delete")
async def bulk_delete_campaigns(
    body: BulkDeleteIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    deleted = (
        db.query(Campaign)
        .filter(Campaign.tenant_id == tenant_id, Campaign.id.in_(body.ids))
        .delete(synchronize_session="fetch")
    )
    db.commit()
    return {"deleted": deleted, "ids": body.ids}


@router.get("/campaigns/debug-template/{template_id}")
async def debug_template(template_id: int, request: Request, db: Session = Depends(get_db)):
    """Diagnostic endpoint: shows raw template components, the generated
    payload, and any validation issues. Returns JSON with no-cache headers."""
    from fastapi.responses import JSONResponse  # noqa: PLC0415
    from services.campaign_dispatcher import (  # noqa: PLC0415
        _build_send_payload,
        _button_needs_param,
        _extract_param_count,
        _example_param_count,
        validate_template_payload,
    )

    tenant_id = resolve_tenant_id(request)
    tpl = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == template_id,
        WhatsAppTemplate.tenant_id == tenant_id,
    ).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    payload = _build_send_payload(
        template=tpl,
        to_phone="966500000000",
        customer_name="أحمد",
        store_name="المتجر",
        coupon_code="SAVE10",
    )

    validation_issues = validate_template_payload(tpl, coupon_code="SAVE10")

    tpl_types = [(c.get("type") or "?") for c in (tpl.components or [])]

    button_analysis: List[Dict[str, Any]] = []
    for comp in (tpl.components or []):
        if (comp.get("type") or "").upper() == "BUTTONS":
            for idx, btn in enumerate(comp.get("buttons") or []):
                button_analysis.append({
                    "index": idx,
                    "type": btn.get("type"),
                    "needs_param": _button_needs_param(btn),
                    "url": btn.get("url"),
                    "example": btn.get("example"),
                })

    notes: List[str] = []
    for comp in (tpl.components or []):
        ct = (comp.get("type") or "").upper()
        if ct == "BODY":
            n = _extract_param_count(comp.get("text") or "") or _example_param_count(comp, "body_text")
            notes.append(f"BODY needs {n} text params")
        if ct == "HEADER" and (comp.get("format") or "").upper() == "TEXT":
            n = _extract_param_count(comp.get("text") or "") or _example_param_count(comp, "header_text")
            if n > 0:
                notes.append(f"HEADER needs {n} text params")

    result = {
        "template_id": tpl.id,
        "template_name": tpl.name,
        "language": tpl.language,
        "status": tpl.status,
        "tpl_types": tpl_types,
        "raw_components": tpl.components,
        "button_analysis": button_analysis,
        "generated_payload": payload,
        "payload_comps": payload.get("template", {}).get("components", []),
        "validation_issues": validation_issues,
        "notes": notes,
    }
    return JSONResponse(
        content=result,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/campaigns/test-send")
async def test_send(body: TestSendIn, request: Request, db: Session = Depends(get_db)):
    """Send a real test WhatsApp message for the chosen template.

    Backwards-compatible wrapper around the wizard's `send_test_message`
    so the existing frontend (which still posts to /campaigns/test-send
    with the old payload shape) keeps working while the new wizard
    moves over to /campaigns/wizard/test-send.

    Behaviour change vs. the previous stub:
      * Previously this endpoint only *simulated* a send and always
        returned success. That made the merchant think the campaign
        worked when nothing had actually been delivered to WhatsApp.
      * Now it delegates to the same provider_send_message pipeline
        used by every other transactional template send (COD, cart
        recovery, etc.). When no live WA connection exists it still
        returns `simulated=True` so dev/sandbox UX is preserved, but
        when a connection is live the message is really sent.
    """
    from services.campaign_wizard.test_send import send_test_message  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    db.commit()

    try:
        template_db_id = int(body.template_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="template_id غير صالح")

    result = await send_test_message(
        db,
        tenant_id=tenant_id,
        template_db_id=template_db_id,
        to_phone=body.phone,
        merchant_vars=body.variables or {},
    )

    # Reshape into the legacy `{success, simulated, message}` envelope
    # the existing frontend expects. The wizard endpoint returns the
    # richer schema directly; only this legacy route does this mapping.
    if result["sent"]:
        if result["simulated"]:
            msg = result["error_message"] or f"تمت المحاكاة — أرسلنا قالب الاختبار إلى {result['to']}"
        else:
            msg = f"تم إرسال رسالة اختبار إلى {result['to']}"
        return {"success": True, "simulated": result["simulated"], "message": msg}
    raise HTTPException(
        status_code=400,
        detail=result["error_message"] or "فشل إرسال رسالة الاختبار",
    )


# ── Campaign report ─────────────────────────────────────────────────────────


@router.get("/campaigns/{campaign_id}/report")
async def get_campaign_report(
    campaign_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Return per-recipient counters for a manual marketing campaign.

    Used by the dashboard to render the post-launch report and the
    pre-launch "🛡️ حماية ذكية من التكرار" trust card. Counters are
    computed from ``campaign_send_logs`` so they survive restarts and
    always agree with the durable per-recipient state.

    Counters:
      total_recipients   — every snapshotted row
      queued             — not yet attempted
      sent               — provider returned a wamid
      failed             — provider error or transient failure
      skipped_duplicate  — frequency-cap hit (default 14d)
      invalid_phone      — phone empty / unparseable
      skipped_unsubscribed — customer opted out before send
      skipped_unreachable  — customer has no phone after audit

    Also returns the configured ``frequency_cap_days`` and the most
    recent error message so the merchant has a single pane to debug.
    """
    tenant_id = resolve_tenant_id(request, db)
    campaign = (
        db.query(Campaign)
        .filter(Campaign.id == campaign_id, Campaign.tenant_id == tenant_id)
        .first()
    )
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    rows = (
        db.query(CampaignSendLog.status, CampaignSendLog.id)
        .filter(CampaignSendLog.campaign_id == campaign_id)
        .all()
    )
    counts: Dict[str, int] = {}
    for status, _ in rows:
        counts[status] = counts.get(status, 0) + 1

    last_error_row = (
        db.query(CampaignSendLog)
        .filter(
            CampaignSendLog.campaign_id == campaign_id,
            CampaignSendLog.status == "failed",
        )
        .order_by(CampaignSendLog.updated_at.desc())
        .first()
    )

    return {
        "campaign_id":           campaign_id,
        "campaign_status":       campaign.status,
        "frequency_cap_days":    MARKETING_CAMPAIGN_FREQUENCY_CAP_DAYS,
        "total_recipients":      sum(counts.values()),
        "queued":                counts.get("queued", 0),
        "sending":               counts.get("sending", 0),
        "sent":                  counts.get("sent", 0),
        "failed":                counts.get("failed", 0),
        "skipped_duplicate":     counts.get("skipped_duplicate", 0),
        "invalid_phone":         counts.get("skipped_invalid", 0),
        "skipped_unsubscribed":  counts.get("skipped_unsubscribed", 0),
        "skipped_unreachable":   counts.get("skipped_unreachable", 0),
        "skipped_manual_exclusion": counts.get("skipped_manual_exclusion", 0),
        "stopped_by_limit":      counts.get("skipped_duplicate", 0),
        "last_error_code":       last_error_row.error_code if last_error_row else None,
        "last_error_message":    last_error_row.error_message if last_error_row else None,
    }


# ── Campaign diagnostics ─────────────────────────────────────────────────────
#
# These two endpoints exist so a merchant (or support) can answer the
# question "I launched the campaign, audience=N, but nobody received
# anything — what happened?" without SSH access to Railway.
#
#   * GET  /campaigns/{id}/debug          → full state snapshot
#   * POST /campaigns/{id}/dispatch-now   → kick the dispatcher in-process
#
# The debug endpoint NEVER raises 500 — every internal failure is caught
# and surfaced as a string in the response so the diagnostic round-trip
# itself never becomes the bug being investigated. Same defensive
# posture we used for /billing/debug/current.


# Set of status values the campaign_dispatcher emits today. Anything in
# CampaignSendLog.status that is NOT in this set is treated as a legacy
# / unknown value and surfaced via the ``unknown_status`` lifecycle so
# the merchant doesn't see a silent "no recipients" report when rows
# actually exist with a non-canonical status (e.g. ``pending``,
# ``processing``, ``created``).
_KNOWN_LOG_STATUSES = {
    "queued", "sending", "sent", "failed",
    "skipped_duplicate", "skipped_invalid", "skipped_unsubscribed",
    "skipped_unreachable", "skipped_manual_exclusion",
}


def _classify_campaign_lifecycle(
    campaign: Campaign,
    counts: Dict[str, int],
    *,
    db: Optional[Session] = None,
    funnel: Optional[Dict[str, Any]] = None,
) -> str:
    """Map the raw ``status`` column + send-log counters onto a
    merchant-friendly verb so the UI never has to show just "نشطة" for
    an inert campaign.

    Order matters: a campaign whose dispatch task crashed BEFORE
    snapshotting any recipient will be ``active`` with zero counts, and
    we want the merchant to see that explicitly as "ينتظر بدء الإرسال"
    instead of the falsely reassuring "نشطة".

    If ``db`` is provided we also peek at the failure-severity mix:
    a campaign that "fails for all 4 customers because none of them
    have WhatsApp" is NOT a campaign failure — it's a recipient-list
    mismatch. We surface that as ``no_whatsapp_recipients`` (sent==0,
    every failure is severity=minor) or ``partial`` (sent>0 with only
    minor failures) so the merchant doesn't see a scary red badge.
    """
    status = (campaign.status or "").lower()
    sent = counts.get("sent", 0)
    failed = counts.get("failed", 0)
    queued = counts.get("queued", 0)
    total = sum(counts.values())
    # Rows that landed under a non-canonical ``status`` value (legacy
    # values, half-finished migrations, hand-edited data). These rows
    # contribute to ``total`` so the merchant doesn't see "0 recipients"
    # when they exist, but the lifecycle surfaces them distinctly.
    known_total = sum(
        v for k, v in counts.items() if k in _KNOWN_LOG_STATUSES
    )
    unknown_total = total - known_total
    materialized_rows = 0
    if funnel is not None:
        try:
            materialized_rows = int(funnel.get("materialized_rows") or 0)
        except (TypeError, ValueError):
            materialized_rows = 0

    # Optional minor-only check — the dispatcher writes canonical
    # error keys (services.meta_errors) into error_code, so we can
    # ask "are all failures of severity=minor?".
    all_failures_minor = False
    if db is not None and failed > 0:
        try:
            from services.meta_errors import severity_of  # noqa: PLC0415
            keys = (
                db.query(CampaignSendLog.error_code)
                .filter(
                    CampaignSendLog.campaign_id == campaign.id,
                    CampaignSendLog.status == "failed",
                )
                .distinct()
                .all()
            )
            severities = {severity_of((k[0] or "").lower()) for k in keys}
            all_failures_minor = severities and severities.issubset({"minor"})
        except Exception:
            all_failures_minor = False

    if status in ("completed",):
        if sent > 0 and failed == 0:
            return "sent"
        if sent > 0 and failed > 0:
            return "partial_minor" if all_failures_minor else "partial"
        if failed > 0 and sent == 0:
            return "no_whatsapp_recipients" if all_failures_minor else "failed_all"
        # sent==0, failed==0 — distinguish several very different cases:
        #   (a) Rows exist but every status is non-canonical
        #       → ``unknown_status`` (data fix needed, not a true zero).
        #   (b) Funnel says rows were materialized but DB has none
        #       → ``orphaned_materialized_rows``.
        #   (c) Audience > 0 but EVERY customer was filtered upstream
        #       → ``excluded_before_send``.
        #   (d) Genuinely empty audience (segment matched 0 customers)
        #       → ``completed_empty``.
        if total > 0 and known_total == 0 and unknown_total > 0:
            return "unknown_status"
        if total == 0 and materialized_rows > 0:
            return "orphaned_materialized_rows"
        if (campaign.audience_count or 0) > 0 and total == 0:
            return "excluded_before_send"
        return "completed_empty"
    if status == "failed":
        return "failed"
    if status == "scheduled":
        return "waiting_scheduler"
    if status == "active":
        if total == 0:
            # Funnel claims rows were materialized but they're not in
            # the DB now — surface this distinctly so the merchant
            # doesn't see the falsely reassuring "ينتظر بدء الإرسال".
            if materialized_rows > 0:
                return "orphaned_materialized_rows"
            return "pending_dispatch"
        # Rows exist but every single one carries a non-canonical
        # status (likely legacy data or a migration that didn't
        # update the values). Don't bucket them as "still sending".
        if known_total == 0 and unknown_total > 0:
            return "unknown_status"
        if queued > 0:
            return "sending"
        if sent == 0 and failed == 0:
            return "pending_dispatch"
        return "sending"
    if status == "draft":
        return "draft"
    return status or "unknown"


@router.get("/campaigns/{campaign_id}/debug")
async def debug_campaign(
    campaign_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Single-call snapshot of a campaign's send-state.

    Returned shape (every field is a primitive — safe to JSON-encode
    no matter what blew up internally)::

        {
          "campaign": { id, name, status, lifecycle, audience_type,
                        audience_count, template_*, schedule_*,
                        launched_at, created_at, dispatch_errors[] },
          "recipients": { total, queued, sending, sent, failed,
                          skipped_duplicate, skipped_unsubscribed,
                          skipped_invalid, skipped_unreachable,
                          skipped_manual_exclusion },
          "sample_failed":   [ {phone(masked), error_code, error_message} ],
          "sample_sent":     [ {phone(masked), provider_message_id, sent_at} ],
          "template":   { id, name, language, category, status, approved },
          "wa_connection": { phone_number_id, status, provider, last_error },
          "scheduler": { campaign_dispatcher_enabled, kill_switch_set,
                         poll_seconds },
          "errors": [ "<diagnostic_section_name>: <error>", … ]
        }

    The ``errors`` array is for *meta* errors (a section that failed to
    compute) — the per-recipient errors live under ``sample_failed``.
    """
    tenant_id = resolve_tenant_id(request)
    errors: List[str] = []

    def _safe(name: str, fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}: {exc!s:.200}")
            try:
                db.rollback()
            except Exception:
                pass
            return None

    campaign = db.query(Campaign).filter(
        Campaign.id == campaign_id, Campaign.tenant_id == tenant_id,
    ).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # ── Recipient counts ────────────────────────────────────────────
    def _counts():
        from services.campaign_dispatcher import _count_log_statuses  # noqa: PLC0415
        return _count_log_statuses(db, campaign_id)
    counts: Dict[str, int] = _safe("recipient_counts", _counts) or {}

    # ── Sample failed / sent rows ───────────────────────────────────
    def _mask(phone: Optional[str]) -> str:
        if not phone:
            return ""
        s = str(phone)
        return ("•" * max(0, len(s) - 4)) + s[-4:] if len(s) > 4 else s

    # ── Verbatim status breakdown ───────────────────────────────────
    # Show the EXACT status values present in campaign_send_logs so
    # legacy / unknown statuses (``pending``, ``processing``, …) are
    # visible to the merchant instead of silently disappearing under
    # a "no recipients" hint. ``known`` keys come first (canonical),
    # then ``unknown`` — UI uses the same keys to render the
    # ``status_breakdown`` block.
    status_breakdown = {
        "queued":                   counts.get("queued", 0),
        "sending":                  counts.get("sending", 0),
        "sent":                     counts.get("sent", 0),
        "failed":                   counts.get("failed", 0),
        "skipped_duplicate":        counts.get("skipped_duplicate", 0),
        "skipped_invalid":          counts.get("skipped_invalid", 0),
        "skipped_unsubscribed":     counts.get("skipped_unsubscribed", 0),
        "skipped_unreachable":      counts.get("skipped_unreachable", 0),
        "skipped_manual_exclusion": counts.get("skipped_manual_exclusion", 0),
        # Bucket every non-canonical status under "unknown_status" so
        # the merchant immediately sees the count without having to
        # parse every key in ``counts``.
        "unknown_status": sum(
            int(v) for k, v in (counts or {}).items()
            if k not in _KNOWN_LOG_STATUSES
        ),
    }
    # Echo the raw mapping too — handy for support to spot exotic
    # legacy statuses like ``pending`` or ``processing``.
    status_breakdown_raw = {str(k): int(v) for k, v in (counts or {}).items()}

    # ── Retry-health diagnostics ────────────────────────────────────
    # Production was burning ~7000 attempts per row before we added
    # the bounded loop + watchdog. This block surfaces the signals
    # the runbook keys off: how high any single row's attempt_count
    # got, how many rows are stuck in ``sending``, and whether ANY
    # row tripped the circuit breaker (``retry_storm`` metric).
    def _retry_health():
        from sqlalchemy import func  # noqa: PLC0415
        from services.campaign_dispatcher import (  # noqa: PLC0415
            ATTEMPT_CIRCUIT_BREAKER, MAX_SEND_ATTEMPTS,
            SENDING_TIMEOUT_SECONDS,
        )
        # Single MAX() roundtrip — the high-water mark is all we need
        # to compute ``retry_storm_detected``.
        max_attempts = int(
            db.query(func.max(CampaignSendLog.attempt_count))
            .filter(CampaignSendLog.campaign_id == campaign_id)
            .scalar() or 0
        )
        at_max = int(
            db.query(func.count(CampaignSendLog.id))
            .filter(
                CampaignSendLog.campaign_id == campaign_id,
                CampaignSendLog.attempt_count >= MAX_SEND_ATTEMPTS,
            )
            .scalar() or 0
        )
        over_breaker = int(
            db.query(func.count(CampaignSendLog.id))
            .filter(
                CampaignSendLog.campaign_id == campaign_id,
                CampaignSendLog.attempt_count > ATTEMPT_CIRCUIT_BREAKER,
            )
            .scalar() or 0
        )
        # Count rows that are STILL in ``sending`` whose updated_at
        # is older than the watchdog threshold — those are zombies
        # waiting for the next dispatch to revive them.
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=SENDING_TIMEOUT_SECONDS)
        zombie_count = int(
            db.query(func.count(CampaignSendLog.id))
            .filter(
                CampaignSendLog.campaign_id == campaign_id,
                CampaignSendLog.status == "sending",
                CampaignSendLog.updated_at < cutoff,
            )
            .scalar() or 0
        )
        return {
            "max_send_attempts":         MAX_SEND_ATTEMPTS,
            "attempt_circuit_breaker":   ATTEMPT_CIRCUIT_BREAKER,
            "sending_timeout_seconds":   SENDING_TIMEOUT_SECONDS,
            "max_attempt_count":         max_attempts,
            "rows_at_attempt_ceiling":   at_max,
            "zombie_sending_count":      zombie_count,
            # Hard truth: if ANY row crossed ATTEMPT_CIRCUIT_BREAKER,
            # we DID see a retry storm. Operators are paged via the
            # ``campaign_send_retry_storm`` log line.
            "retry_storm_detected":      over_breaker > 0,
        }
    retry_health = _safe("retry_health", _retry_health) or {
        "retry_storm_detected": False,
        "max_attempt_count": 0,
        "rows_at_attempt_ceiling": 0,
        "zombie_sending_count": 0,
        "max_send_attempts": 5,
        "attempt_circuit_breaker": 100,
        "sending_timeout_seconds": 300,
    }

    # ── First 10 send-log rows (audit) ──────────────────────────────
    # When materialized_rows>0 but every counter is zero, the merchant
    # needs to SEE the actual rows to diagnose (legacy status, weird
    # skip_reason, etc.). We surface a tiny sample sorted by id so the
    # ordering is stable.
    def _sample_rows():
        rows = (
            db.query(CampaignSendLog)
            .filter(CampaignSendLog.campaign_id == campaign_id)
            .order_by(CampaignSendLog.id.asc())
            .limit(10)
            .all()
        )
        out = []
        for r in rows:
            out.append({
                "id":           int(r.id),
                "phone_masked": _mask(r.customer_phone_e164),
                "status":       r.status,
                "skip_reason":  r.skip_reason,
                "error_code":   r.error_code,
                "error_message": (r.error_message or "")[:240] or None,
                "attempt_count": r.attempt_count,
                "created_at":   r.created_at.isoformat() if r.created_at else None,
                "updated_at":   r.updated_at.isoformat() if r.updated_at else None,
            })
        return out
    sample_rows = _safe("sample_rows", _sample_rows) or []

    def _sample_failed():
        from services.meta_errors import (  # noqa: PLC0415
            ERRORS as META_ERRORS, classify_meta_error, parse_technical,
        )
        rows = (
            db.query(CampaignSendLog)
            .filter(
                CampaignSendLog.campaign_id == campaign_id,
                CampaignSendLog.status == "failed",
            )
            .order_by(CampaignSendLog.updated_at.desc())
            .limit(5)
            .all()
        )
        out = []
        for r in rows:
            # ``error_code`` is the canonical key written by the
            # dispatcher post-classification. If we encounter an
            # older row from before the classifier was deployed,
            # re-classify on the fly so the merchant always sees
            # an Arabic label.
            key = (r.error_code or "").strip().lower()
            classified = (
                META_ERRORS.get(key)
                if key in META_ERRORS
                else classify_meta_error(
                    code=r.error_code, message=r.error_message,
                )
            )
            # Parse the canonical "[code=X subcode=Y type=Z] msg"
            # string back into separate fields so the UI can render
            # raw Meta details prominently when key=="unknown" — no
            # more "خطأ غير معروف" with nothing underneath it.
            parsed = parse_technical(r.error_message)
            out.append({
                "phone":          _mask(r.customer_phone_e164),
                "error_code":     classified.key,
                "error_label_ar": classified.label_ar,
                "severity":       classified.severity,
                "is_recoverable": classified.is_recoverable,
                # New: ``retryable`` is the policy the dispatcher
                # actually keys off (≠ ``is_recoverable``, which is
                # merchant-facing). The UI uses it to hide a "أعد
                # المحاولة" button on rows that can't possibly succeed.
                "retryable":      classified.retryable,
                # Provider-side billing/account restriction marker.
                # Trips the "Contact 360dialog" banner — see the
                # aggregated ``provider_block`` section for the full
                # campaign-level signal.
                "provider_billing_block": classified.provider_billing_block,
                "advice_ar":      classified.advice_ar,
                # Raw technical message kept verbatim — surfaces in
                # the "نسخ الخطأ التقني" button.
                "error_technical":     (r.error_message or "")[:300],
                # Parsed Meta fields — surface separately so the UI
                # can always show meta_error_code / subcode / type /
                # message even for ``unknown`` keys (fingerprint
                # collection for the classifier).
                "meta_error_code":     parsed["meta_error_code"],
                "meta_error_subcode":  parsed["meta_error_subcode"],
                "meta_error_type":     parsed["meta_error_type"],
                "meta_error_message":  parsed["meta_error_message"],
                "attempt_count":   r.attempt_count,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            })
        return out
    sample_failed = _safe("sample_failed", _sample_failed) or []

    # ── Raw Meta error fingerprint bucket ───────────────────────────
    # When ``classified.key == "unknown"`` the dispatcher persists the
    # full request + response payload onto the campaign so support can
    # see EXACTLY what Meta replied. Surfaces under
    # ``raw_meta_error_samples`` (list, oldest → newest).
    def _raw_meta_samples():
        import json as _json  # noqa: PLC0415
        raw = (campaign.template_variables or {}).get(
            "_raw_meta_error_samples"
        )
        if not raw:
            return []
        if isinstance(raw, list):
            return raw[-5:]
        if isinstance(raw, str) and raw.strip():
            try:
                data = _json.loads(raw)
            except Exception:
                return []
            return data[-5:] if isinstance(data, list) else []
        return []
    raw_meta_error_samples = _safe(
        "raw_meta_samples", _raw_meta_samples,
    ) or []

    def _sample_sent():
        rows = (
            db.query(CampaignSendLog)
            .filter(
                CampaignSendLog.campaign_id == campaign_id,
                CampaignSendLog.status == "sent",
            )
            .order_by(CampaignSendLog.sent_at.desc())
            .limit(5)
            .all()
        )
        out = []
        for r in rows:
            # Per-row delivery stage — surfaces the WhatsApp status
            # webhook attribution so the merchant can see which sent
            # recipient actually received vs read vs failed-after-
            # accept. The ladder is one-way:
            #   accepted_by_provider → delivered → read
            # except for `failed_after_accept` which is a terminal
            # off-ladder state for rows that DID get a wamid.
            if r.failed_at is not None:
                delivery_stage = "failed_after_accept"
            elif r.read_at is not None:
                delivery_stage = "read"
            elif r.delivered_at is not None:
                delivery_stage = "delivered"
            else:
                # The send-log row is "sent" (we have a wamid) but
                # no downstream webhook has arrived yet. The UI
                # renders this as "قبلتها Meta — لم تصل بعد".
                delivery_stage = "accepted_by_provider"
            out.append({
                "phone":               _mask(r.customer_phone_e164),
                "provider_message_id": r.provider_message_id,
                # A row in status='sent' WITHOUT a provider_message_id
                # is corrupt — surface it on the UI as a hard warning
                # so we don't pretend Meta accepted it.
                "has_provider_message_id": bool(r.provider_message_id),
                "sent_at":             r.sent_at.isoformat() if r.sent_at else None,
                "delivered_at":        r.delivered_at.isoformat() if r.delivered_at else None,
                "read_at":             r.read_at.isoformat() if r.read_at else None,
                "failed_at":           r.failed_at.isoformat() if r.failed_at else None,
                "delivery_stage":      delivery_stage,
            })
        return out
    sample_sent = _safe("sample_sent", _sample_sent) or []

    # ── Delivery summary (aggregate across the whole campaign) ──
    # Counts every row in status='sent' broken down by the
    # downstream delivery stage. Built from a single query so it
    # stays cheap even for 50k-recipient blasts.
    def _delivery_summary():
        from sqlalchemy import case, func  # noqa: PLC0415
        # Every row that has a provider_message_id is counted in
        # `accepted_by_provider`. Rows go UP the ladder into
        # `delivered` / `read` / `failed_after_accept` based on
        # which webhook event arrived.
        q = db.query(
            func.count(CampaignSendLog.id).label("total_sent"),
            func.sum(
                case(
                    (CampaignSendLog.provider_message_id.isnot(None), 1),
                    else_=0,
                )
            ).label("accepted_by_provider"),
            func.sum(
                case(
                    (CampaignSendLog.delivered_at.isnot(None), 1),
                    else_=0,
                )
            ).label("delivered"),
            func.sum(
                case(
                    (CampaignSendLog.read_at.isnot(None), 1),
                    else_=0,
                )
            ).label("read"),
            func.sum(
                case(
                    (CampaignSendLog.failed_at.isnot(None), 1),
                    else_=0,
                )
            ).label("failed_after_accept"),
            func.sum(
                case(
                    (CampaignSendLog.provider_message_id.is_(None), 1),
                    else_=0,
                )
            ).label("missing_provider_message_id"),
        ).filter(
            CampaignSendLog.campaign_id == campaign_id,
            CampaignSendLog.status == "sent",
        )
        row = q.first()
        if not row:
            return {
                "accepted_by_provider":        0,
                "delivered":                   0,
                "read":                        0,
                "failed_after_accept":         0,
                "unknown_delivery":            0,
                "missing_provider_message_id": 0,
            }
        accepted = int(row.accepted_by_provider or 0)
        delivered = int(row.delivered or 0)
        read_     = int(row.read or 0)
        failed_   = int(row.failed_after_accept or 0)
        missing   = int(row.missing_provider_message_id or 0)
        # ``unknown_delivery`` is the set difference: rows that Meta
        # accepted but for which we never received a downstream
        # delivered/read/failed webhook. This is the bucket support
        # asks about when a merchant says "the campaign says
        # 4 sent but my friend never got it".
        unknown = max(0, accepted - max(delivered, read_, failed_))
        return {
            "accepted_by_provider":        accepted,
            "delivered":                   delivered,
            "read":                        read_,
            "failed_after_accept":         failed_,
            "unknown_delivery":            unknown,
            "missing_provider_message_id": missing,
        }
    delivery_summary = _safe("delivery_summary", _delivery_summary) or {
        "accepted_by_provider":        0,
        "delivered":                   0,
        "read":                        0,
        "failed_after_accept":         0,
        "unknown_delivery":            0,
        "missing_provider_message_id": 0,
    }

    # ── Template (approved/language/etc.) ──────────────────────────
    def _template():
        tpl = db.query(WhatsAppTemplate).filter(
            WhatsAppTemplate.id == int(campaign.template_id or 0),
            WhatsAppTemplate.tenant_id == tenant_id,
        ).first() if campaign.template_id else None
        if not tpl:
            return None
        return {
            "id":         tpl.id,
            "name":       tpl.name,
            "language":   tpl.language,
            "category":   tpl.category,
            "status":     tpl.status,
            "approved":   (tpl.status or "").upper() == "APPROVED",
        }
    template_info = _safe("template", _template)

    # ── WhatsApp connection ────────────────────────────────────────
    def _wa_conn():
        from services.campaign_dispatcher import _get_wa_connection  # noqa: PLC0415
        conn = _get_wa_connection(db, tenant_id)
        if not conn:
            return None
        return {
            "phone_number_id": getattr(conn, "phone_number_id", None),
            "status":          getattr(conn, "status", None),
            "provider":        getattr(conn, "provider", None) or getattr(conn, "provider_name", None),
            "last_error":      getattr(conn, "last_error", None),
        }
    wa_conn_info = _safe("wa_connection", _wa_conn)

    # ── Scheduler health ───────────────────────────────────────────
    def _scheduler():
        kill_switch = (
            os.environ.get("NAHLA_DISABLE_SCHEDULERS", "").strip().lower()
            in ("1", "true", "yes")
        )
        return {
            "campaign_dispatcher_enabled": not kill_switch,
            "kill_switch_set":             kill_switch,
            "poll_seconds":                30,
            "note": (
                "إذا كان kill_switch_set=true فإن الحملات المجدولة لن "
                "تنطلق تلقائياً (NAHLA_DISABLE_SCHEDULERS مفعّلة). "
                "الحملات الفورية تستخدم asyncio.create_task ولا تتأثر "
                "بهذه المفتاح. استخدم dispatch-now للإرسال يدوياً."
            ),
        }
    scheduler_info = _safe("scheduler", _scheduler) or {}

    tpl_vars = campaign.template_variables or {}
    dispatch_errors_raw = tpl_vars.get("_dispatch_errors", "") or ""
    dispatch_errors = [e for e in dispatch_errors_raw.split("|") if e]

    # ``audience_funnel`` is computed below — but the lifecycle
    # classifier needs ``materialized_rows`` to distinguish
    # ``pending_dispatch`` (truly no rows yet) from
    # ``orphaned_materialized_rows`` (snapshot says rows exist but DB
    # disagrees). We forward-define a lightweight read that doesn't
    # depend on the full funnel block.
    _funnel_for_lifecycle: Dict[str, Any] = {}
    try:
        import json as _json_mr  # noqa: PLC0415
        _raw_fl = (campaign.template_variables or {}).get("_audience_funnel")
        if isinstance(_raw_fl, str) and _raw_fl.strip():
            _funnel_for_lifecycle = _json_mr.loads(_raw_fl) or {}
        elif isinstance(_raw_fl, dict):
            _funnel_for_lifecycle = _raw_fl
    except Exception:
        _funnel_for_lifecycle = {}

    lifecycle = _classify_campaign_lifecycle(
        campaign, counts, db=db, funnel=_funnel_for_lifecycle,
    )

    # Group failures by canonical key so the merchant sees, e.g.,
    # "3 عملاء لا يملكون واتساب، 1 خارج نافذة 24 ساعة" rather than
    # 4 separate raw-text rows. Used by the UI's diagnostic block.
    def _failure_summary():
        from services.meta_errors import (  # noqa: PLC0415
            ERRORS as META_ERRORS, classify_meta_error,
        )
        rows = (
            db.query(CampaignSendLog.error_code, func.count(CampaignSendLog.id))
            .filter(
                CampaignSendLog.campaign_id == campaign_id,
                CampaignSendLog.status == "failed",
            )
            .group_by(CampaignSendLog.error_code)
            .all()
        )
        out = []
        for raw_key, n in rows:
            key = (raw_key or "unknown").strip().lower()
            classified = (
                META_ERRORS.get(key)
                if key in META_ERRORS
                else classify_meta_error(code=raw_key)
            )
            out.append({
                "error_code":     classified.key,
                "error_label_ar": classified.label_ar,
                "severity":       classified.severity,
                "is_recoverable": classified.is_recoverable,
                "retryable":      classified.retryable,
                "provider_billing_block": classified.provider_billing_block,
                "advice_ar":      classified.advice_ar,
                "count":          int(n),
            })
        out.sort(key=lambda x: -x["count"])
        return out
    from sqlalchemy import func  # noqa: PLC0415, E402
    failure_summary = _safe("failure_summary", _failure_summary) or []

    # ── Provider-side billing/account block detector ────────────────
    # Any failure tagged ``provider_billing_block=True`` in the
    # catalogue means the campaign cannot proceed without escalating
    # to 360dialog. The UI uses this block to: render the support
    # banner, hide "إرسال الآن", and surface the "نسخ تقرير الدعم"
    # CTA. We compute timestamps + a small sample so the merchant
    # (and our support team) can correlate quickly.
    def _provider_block():
        from services.meta_errors import ERRORS as _ME  # noqa: PLC0415
        blocked_keys = [
            k for k, v in _ME.items() if v.provider_billing_block
        ]
        if not blocked_keys:
            return {
                "detected": False,
                "count": 0,
                "error_keys": [],
                "first_seen_at": None,
                "last_seen_at": None,
                "primary_label_ar": None,
                "support_message_ar": None,
            }
        rows = (
            db.query(CampaignSendLog)
            .filter(
                CampaignSendLog.campaign_id == campaign_id,
                CampaignSendLog.status == "failed",
                CampaignSendLog.error_code.in_(blocked_keys),
            )
            .all()
        )
        if not rows:
            return {
                "detected": False,
                "count": 0,
                "error_keys": [],
                "first_seen_at": None,
                "last_seen_at": None,
                "primary_label_ar": None,
                "support_message_ar": None,
            }
        # Aggregate per error_code so the UI can list each distinct
        # provider error encountered ("client_payment_blocked: 4
        # rows" / "account_locked: 1 row").
        per_key: Dict[str, int] = {}
        first_seen = None
        last_seen = None
        for r in rows:
            k = (r.error_code or "").strip().lower()
            per_key[k] = per_key.get(k, 0) + 1
            ts = r.updated_at or r.created_at
            if ts is None:
                continue
            if first_seen is None or ts < first_seen:
                first_seen = ts
            if last_seen is None or ts > last_seen:
                last_seen = ts
        # Pick the most common key as the "primary" — drives the
        # banner copy. If every recipient hit the same code we want
        # the merchant to see THAT label, not a vague fallback.
        primary_key = max(per_key, key=per_key.get)  # type: ignore[arg-type]
        primary_label = _ME[primary_key].label_ar
        return {
            "detected": True,
            "count": len(rows),
            "error_keys": [
                {
                    "key": k,
                    "count": c,
                    "label_ar": _ME[k].label_ar,
                }
                for k, c in sorted(per_key.items(), key=lambda kv: -kv[1])
            ],
            "first_seen_at": first_seen.isoformat() if first_seen else None,
            "last_seen_at":  last_seen.isoformat()  if last_seen  else None,
            "primary_key":   primary_key,
            "primary_label_ar": primary_label,
            # Fixed banner copy the spec asks for — kept on the
            # backend so all clients (mobile/web/email) show the
            # same message.
            "support_message_ar": (
                "مشكلة من مزود واتساب أو الدفع — تواصل مع 360dialog "
                "وأرفق تقرير الدعم أدناه."
            ),
            "support_provider": "360dialog",
        }
    provider_block = _safe("provider_block", _provider_block) or {
        "detected": False,
        "count": 0,
        "error_keys": [],
        "first_seen_at": None,
        "last_seen_at": None,
        "primary_label_ar": None,
        "support_message_ar": None,
    }

    # ── Audience funnel ─────────────────────────────────────────────
    # Persisted by the dispatcher in template_variables['_audience_funnel']
    # as a JSON-encoded string. We always render a structured object —
    # if the campaign hasn't dispatched yet (or was sent before this
    # feature shipped) we synthesise a best-effort funnel from the
    # current send-log counters so the UI never has missing fields.
    def _audience_funnel():
        import json as _json  # noqa: PLC0415
        raw = (campaign.template_variables or {}).get("_audience_funnel")
        funnel: Dict[str, Any] = {}
        if isinstance(raw, str) and raw.strip():
            try:
                funnel = _json.loads(raw) or {}
            except Exception:
                funnel = {}
        elif isinstance(raw, dict):
            funnel = raw
        # Normalised shape — every key always present so the UI can
        # render the funnel without optional-chaining everywhere.
        total_logs = sum(counts.values())
        return {
            "raw_audience":           int(funnel.get("raw_audience") or 0),
            "after_reachable_filter": int(
                funnel.get("after_reachable_filter")
                # Fallback: assume the materialized rows is the best
                # proxy for "after reachable" on legacy campaigns.
                or total_logs
            ),
            "materialized_rows":      int(
                funnel.get("materialized_rows") or total_logs
            ),
            "queued_for_send":        int(
                funnel.get("queued_for_send") or counts.get("queued", 0)
            ),
            "skipped_at_snapshot":    int(funnel.get("skipped_at_snapshot") or (
                counts.get("skipped_unreachable", 0)
                + counts.get("skipped_unsubscribed", 0)
                + counts.get("skipped_invalid", 0)
                + counts.get("skipped_manual_exclusion", 0)
            )),
            "frequency_cap_skipped":  int(
                funnel.get("frequency_cap_skipped")
                or counts.get("skipped_duplicate", 0)
            ),
            "audience_count_campaign": int(campaign.audience_count or 0),
        }
    audience_funnel = _safe("audience_funnel", _audience_funnel) or {}

    # ── Pre-send exclusion summary ──────────────────────────────────
    # Two layers of exclusions feed this:
    #   (1) skipped_* rows in campaign_send_logs (we know the exact
    #       skip_reason for each).
    #   (2) Customers that never made it to a row at all because the
    #       reachability filter dropped them upstream — we can only
    #       infer this from (raw_audience - after_reachable_filter).
    def _excluded_summary():
        rows = (
            db.query(
                CampaignSendLog.status,
                CampaignSendLog.skip_reason,
                func.count(CampaignSendLog.id),
            )
            .filter(
                CampaignSendLog.campaign_id == campaign_id,
                CampaignSendLog.status.like("skipped_%"),
            )
            .group_by(CampaignSendLog.status, CampaignSendLog.skip_reason)
            .all()
        )
        ar_label = {
            "no_phone":                 "بدون رقم جوال",
            "invalid_phone":            "رقم جوال غير صالح",
            "unsubscribed":             "ألغى الاشتراك",
            "pending_unsubscribe":      "في طور إلغاء الاشتراك",
            "marketing_opt_out_manual": "إلغاء التسويق يدوياً",
            "excluded_by_manual_segment": "مستبعد بواسطة فلتر يدوي",
            "frequency_cap_marketing":  "تجاوز الحد الأقصى للرسائل التسويقية",
        }
        ar_status = {
            "skipped_unreachable":      "غير قابل للوصول",
            "skipped_unsubscribed":     "ألغى الاشتراك",
            "skipped_invalid":          "بيانات غير صالحة",
            "skipped_manual_exclusion": "مستبعد يدوياً",
            "skipped_duplicate":        "تكرار / تجاوز الحد الأقصى",
        }
        out: List[Dict[str, Any]] = []
        for status_v, reason, n in rows:
            key = (reason or status_v or "unknown")
            out.append({
                "status":      status_v,
                "skip_reason": reason,
                "label_ar":    ar_label.get(reason or "")
                                or ar_status.get(status_v or "")
                                or str(key),
                "count":       int(n),
            })

        # Inferred upstream drop: customers who matched the segment
        # but never got a log row because the reachability filter
        # already excluded them (no phone, opted-out, etc.).
        inferred = max(
            int(audience_funnel.get("raw_audience") or 0)
            - int(audience_funnel.get("after_reachable_filter") or 0),
            0,
        )
        if inferred > 0:
            out.append({
                "status":      "filtered_pre_snapshot",
                "skip_reason": "unreachable_or_opted_out",
                "label_ar":    "مستبعد قبل الإرسال (بدون واتساب أو إلغى الاشتراك)",
                "count":       inferred,
            })
        out.sort(key=lambda x: -x["count"])
        return out
    excluded_reasons_summary = _safe("excluded_reasons", _excluded_summary) or []
    excluded_before_send_count = sum(int(r["count"]) for r in excluded_reasons_summary)

    # ── Frequency-cap audit trail ───────────────────────────────────
    # When a row was skipped with skip_reason starting with
    # "frequency_cap_marketing", the merchant has historically had no
    # way to verify *why* — there's no link to the previous campaign
    # that "burned" the cap. We now resolve the most recent successful
    # send for every capped phone so the UI can render
    # "آخر رسالة ناجحة بتاريخ X في حملة #Y" instead of an opaque
    # "skipped_duplicate".
    def _frequency_cap_audit():
        from services.campaign_dispatcher import (  # noqa: PLC0415
            _frequency_cap_evidence_for_phones,
        )
        cap_days_cfg = MARKETING_CAMPAIGN_FREQUENCY_CAP_DAYS
        bypass_flag = bool(
            str((campaign.template_variables or {}).get(
                "_bypass_frequency_cap") or ""
            ).strip().lower() in ("true", "1", "yes")
        )
        capped_rows = (
            db.query(CampaignSendLog)
              .filter(
                  CampaignSendLog.campaign_id == campaign_id,
                  CampaignSendLog.status == "skipped_duplicate",
                  CampaignSendLog.skip_reason.like("frequency_cap_marketing%"),
              )
              .limit(20)
              .all()
        )
        if not capped_rows:
            return {
                "bypassed":                     bypass_flag,
                "cap_days":                     int(cap_days_cfg),
                "capped_count":                 0,
                "frequency_cap_source_rows":    [],
                "source_rows":                  [],
                "last_successful_sent_at":      None,
                "last_successful_campaign_id":    None,
            }
        phones = [r.customer_phone_e164 for r in capped_rows if r.customer_phone_e164]
        evidence = _frequency_cap_evidence_for_phones(db, tenant_id, phones)
        src_rows: List[Dict[str, Any]] = []
        agg_ts: Optional[str] = None
        agg_cid: Optional[int] = None
        for r in capped_rows:
            ev = evidence.get(r.customer_phone_e164 or "", {}) or {}
            ts = ev.get("last_successful_sent_at")
            cid_ev = ev.get("last_successful_campaign_id")
            if isinstance(ts, str) and ts:
                if agg_ts is None or ts > agg_ts:
                    agg_ts = ts
                    agg_cid = int(cid_ev) if cid_ev is not None else None
            src_rows.append({
                "phone_masked":                _mask(r.customer_phone_e164),
                "skip_reason":                 r.skip_reason,
                "last_successful_sent_at":     ts,
                "last_successful_campaign_id": (
                    int(cid_ev) if cid_ev is not None else None
                ),
            })
        return {
            "bypassed":                     False,
            "cap_days":                     int(cap_days_cfg),
            "capped_count":                 len(capped_rows),
            "frequency_cap_source_rows":    src_rows,
            "source_rows":                  src_rows,
            "last_successful_sent_at":      agg_ts,
            "last_successful_campaign_id":  agg_cid,
        }
    frequency_cap_audit = _safe(
        "frequency_cap_audit", _frequency_cap_audit,
    ) or {
        "bypassed":                     False,
        "cap_days":                     MARKETING_CAMPAIGN_FREQUENCY_CAP_DAYS,
        "capped_count":                 0,
        "frequency_cap_source_rows":    [],
        "source_rows":                  [],
        "last_successful_sent_at":      None,
        "last_successful_campaign_id":  None,
    }

    # ── Per-customer exclusion sample ───────────────────────────────
    # The aggregate counts are useful but a merchant looking at an
    # ``excluded_before_send`` campaign needs to know **which** of
    # their 4 customers was dropped and **why**. We surface up to 10
    # excluded customers with the actual field values that drove the
    # decision so support can spot patterns instantly:
    #   "all 4 have raw `phone` but no `normalized_phone`" → import
    #     pipeline didn't normalise.
    #   "all 4 have `is_unsubscribed=true`" → bulk opt-out / data
    #     migration bug.
    #   "all 4 have `extra_metadata.has_whatsapp=false`" → confirmed
    #     by past Meta failures, not a Nahla filter problem.
    #
    # IMPORTANT design choice (per merchant feedback):
    #   `has_whatsapp` is treated as **tri-state** (true / false /
    #   unknown). Only an explicit ``false`` (set after a Meta send
    #   failure) is shown as a blocker. ``null`` / ``unknown`` are
    #   reported as ``unknown`` so the merchant doesn't blame Nahla
    #   for excluding people we'd actually have tried to reach.
    def _sample_excluded():
        from services.nahla_segments import (  # noqa: PLC0415
            build_unified_segment_query,
        )
        raw_q = build_unified_segment_query(
            campaign.audience_type, db, tenant_id, require_reachable=False,
        )
        if raw_q is None:
            return []

        # Cap the raw scan to 200 IDs so the debug call stays cheap
        # even on huge segments. The first 200 are a representative
        # sample for any merchant who's "missing recipients".
        raw_ids = [
            cid for (cid,) in raw_q.with_entities(Customer.id).limit(200).all()
        ]
        if not raw_ids:
            return []

        # IDs that DID make it into campaign_send_logs — we only care
        # about the ones that were dropped before snapshotting.
        materialized_ids = {
            cid for (cid,) in (
                db.query(CampaignSendLog.customer_id)
                  .filter(
                      CampaignSendLog.campaign_id == campaign_id,
                      CampaignSendLog.customer_id.isnot(None),
                  )
                  .distinct()
                  .all()
            )
        }
        excluded_ids = [cid for cid in raw_ids if cid not in materialized_ids]
        if not excluded_ids:
            return []

        # Pull the 10 oldest-id rows (deterministic for diffing) and
        # introspect each customer's actual field values.
        sample_rows = (
            db.query(Customer)
              .filter(
                  Customer.tenant_id == tenant_id,
                  Customer.id.in_(excluded_ids[:10]),
              )
              .all()
        )

        ar_label = {
            "no_phone":              "بدون رقم جوال",
            "phone_not_normalized":  "الرقم غير مُطبَّع (E.164)",
            "unsubscribed":          "ألغى الاشتراك",
            "pending_unsubscribe":   "في طور إلغاء الاشتراك",
            "marketing_opt_out":     "إلغاء التسويق يدوياً",
            "no_whatsapp_confirmed": "تأكّد من Meta أن الرقم بلا واتساب",
            "unknown":               "سبب غير محدّد (راجع البيانات)",
        }

        out: List[Dict[str, Any]] = []
        for cust in sample_rows:
            meta = getattr(cust, "extra_metadata", None) or {}

            # Tri-state has_whatsapp: True / False / None (unknown).
            # ``None`` is the most common value — Meta hasn't told us
            # yet — and MUST be treated as "try to send" not "skip".
            raw_has_wa = meta.get("has_whatsapp")
            if isinstance(raw_has_wa, bool):
                has_whatsapp_state: Any = raw_has_wa
            elif raw_has_wa is None:
                has_whatsapp_state = None  # truly unknown
            else:
                # Strings like "true"/"false" — coerce defensively.
                v = str(raw_has_wa).strip().lower()
                if v in ("true", "1", "yes"):
                    has_whatsapp_state = True
                elif v in ("false", "0", "no"):
                    has_whatsapp_state = False
                else:
                    has_whatsapp_state = None

            has_phone = bool((cust.phone or "").strip())
            phone_normalized_valid = bool((cust.normalized_phone or "").strip())
            is_unsubscribed = bool(meta.get("is_unsubscribed"))
            pending_unsubscribe = bool(meta.get("pending_unsubscribe"))
            marketing_opt_out = bool(meta.get("marketing_opt_out_manual"))

            # Decide the dominant reason. Order matters: a customer
            # without normalized_phone could ALSO be unsubscribed — we
            # report the upstream-most blocker so the merchant fixes
            # the right thing first.
            if not phone_normalized_valid and not has_phone:
                reason_key = "no_phone"
            elif not phone_normalized_valid and has_phone:
                reason_key = "phone_not_normalized"
            elif is_unsubscribed:
                reason_key = "unsubscribed"
            elif pending_unsubscribe:
                reason_key = "pending_unsubscribe"
            elif marketing_opt_out:
                reason_key = "marketing_opt_out"
            elif has_whatsapp_state is False:
                # ONLY explicit false (set by past Meta failure) blocks.
                # Tri-state ``None`` falls through to "unknown" — Meta
                # is the source of truth, not us.
                reason_key = "no_whatsapp_confirmed"
            else:
                reason_key = "unknown"

            out.append({
                "customer_id":     int(cust.id),
                "name":            cust.name or "—",
                "phone_masked":    _mask(cust.normalized_phone or cust.phone),
                "reason_key":      reason_key,
                "reason_label_ar": ar_label.get(reason_key, ar_label["unknown"]),
                "fields": {
                    "has_phone":              has_phone,
                    "phone_normalized_valid": phone_normalized_valid,
                    "whatsapp_opted_out":     is_unsubscribed or pending_unsubscribe,
                    # Tri-state: True / False / None (unknown).
                    # The UI shows null as "غير معروف" so the merchant
                    # understands we don't pre-block on this.
                    "has_whatsapp":           has_whatsapp_state,
                    "is_unsubscribed":        is_unsubscribed,
                    "pending_unsubscribe":    pending_unsubscribe,
                    "marketing_opt_out":      marketing_opt_out,
                },
            })
        return out
    sample_excluded_before_send = _safe("sample_excluded", _sample_excluded) or []

    # Hints are merchant-facing, in Arabic, and follow the strict rule:
    # never claim "no recipients" while ``materialized_rows`` or
    # ``recipients.total`` are > 0 — the merchant has been bitten by
    # that false-positive before.
    hints: List[str] = []
    recipients_total_db = sum(int(v) for v in (counts or {}).values())
    materialized_rows_in_funnel = int(
        (audience_funnel or {}).get("materialized_rows") or 0
    )

    # Genuine "no recipients" — and only when BOTH the in-DB count and
    # the funnel materialization counter agree there are zero rows.
    if (
        lifecycle == "pending_dispatch"
        and (campaign.audience_count or 0) > 0
        and recipients_total_db == 0
        and materialized_rows_in_funnel == 0
    ):
        hints.append(
            "تم إنشاء الحملة بدون أي مستلم في سجل الإرسال. الأسباب "
            "المحتملة: (1) خلل في مَهمّة asyncio الخلفية (راجع سجلات "
            "Railway للبحث عن 'dispatching campaign'). (2) فشل التحقق "
            "من القالب أو اتصال واتساب. استخدم POST "
            f"/campaigns/{campaign_id}/dispatch-now لإعادة المحاولة."
        )

    # Funnel says rows were created but DB disagrees — usually means
    # rows were deleted, or the snapshot finished and crashed before
    # commit. Tell the merchant the truth so they don't trust the
    # falsely reassuring "ينتظر بدء الإرسال".
    if lifecycle == "orphaned_materialized_rows":
        hints.append(
            f"تم رصد materialized_rows={materialized_rows_in_funnel} في "
            "snapshot الحملة، لكن لا توجد صفوف فعلية الآن في "
            "campaign_send_logs. ربما حُذفت الصفوف يدوياً، أو فشل "
            "commit بعد الإنشاء، أو تتابع dispatch-now فوق نفس الحملة "
            "وأعاد التهيئة. استخدم dispatch-now لإعادة الإنشاء."
        )

    # Rows exist but their status values aren't recognised by the
    # current dispatcher. Usually a sign of a legacy migration or
    # hand-edited data. Surface the raw status names so support can
    # decide whether to backfill them to canonical values.
    if lifecycle == "unknown_status":
        raw_keys = ", ".join(
            sorted(
                f"{k}={v}"
                for k, v in status_breakdown_raw.items()
                if k not in _KNOWN_LOG_STATUSES
            )
        )
        hints.append(
            "صفوف موجودة في campaign_send_logs لكن حالتها غير معروفة "
            "(ليست ضمن queued/sending/sent/failed/skipped_*). "
            f"القيم المُلتقطة: {raw_keys or 'غير محدّدة'}. راجع "
            "sample_rows في الأسفل وحدّث الحالة إلى قيمة قانونية أو "
            "أعد التشغيل عبر dispatch-now."
        )
    if lifecycle == "excluded_before_send":
        # Build an explicit, merchant-facing breakdown using the
        # exclusion summary computed above. We show counts in Arabic
        # so the merchant immediately understands "why is no one
        # receiving my campaign?".
        if excluded_reasons_summary:
            parts = [
                f"{r['count']} {r['label_ar']}"
                for r in excluded_reasons_summary
            ]
            hints.append(
                "الجمهور الأولي كان "
                f"{audience_funnel.get('raw_audience', 0)} عميل، "
                "لكن لا أحد منهم يستوفي شروط الإرسال: "
                + "، ".join(parts)
                + ". تأكد من إضافة أرقام واتساب صحيحة أو راجع "
                  "إعدادات إلغاء الاشتراك."
            )
        else:
            hints.append(
                "لم تُكتب أي صفوف في سجل الإرسال. تحقق من اتصال واتساب "
                "ومن أن العملاء يملكون أرقاماً مطبَّعة (normalized_phone)."
            )
    if frequency_cap_audit.get("bypassed"):
        hints.append(
            "حد التكرار (frequency cap) متجاوز لهذه الحملة — كل عميل "
            "في الجمهور سيُرسل له حتى لو وصلته رسالة سابقة خلال نافذة "
            "الحد. ⚠️ استخدم هذا الوضع للاختبار فقط."
        )
    elif (frequency_cap_audit.get("capped_count") or 0) > 0:
        cap_d = int(frequency_cap_audit.get("cap_days") or MARKETING_CAMPAIGN_FREQUENCY_CAP_DAYS)
        ls_at = frequency_cap_audit.get("last_successful_sent_at")
        ls_cid = frequency_cap_audit.get("last_successful_campaign_id")
        tail = ""
        if ls_at:
            tail = f" آخر إرسال ناجح مسجّل كان في {ls_at}"
            if ls_cid is not None:
                tail += f" (حملة #{ls_cid})."
            else:
                tail += "."
        hints.append(
            f"تم تخطّي {frequency_cap_audit['capped_count']} عميل بسبب "
            f"حد التكرار التسويقي (خلال آخر {cap_d} يوماً، وبناءً على "
            "رسائل وصلت إلى Meta بنجاح فقط)."
            + tail
            + " إذا كنت تختبر، فعّل «تجاهل حد التكرار لهذه الحملة» عند الإرسال."
        )
    # Hint: every failure is an UNKNOWN Meta error → support needs
    # to look at the raw payload bucket to fingerprint a new code.
    unknown_failures = sum(
        1 for fs in (failure_summary or [])
        if (fs.get("error_code") or "") == "unknown"
    )
    total_failures = sum(int(fs.get("count") or 0) for fs in (failure_summary or []))
    if unknown_failures > 0 and unknown_failures == len(failure_summary or []) and total_failures > 0:
        hints.append(
            "كل حالات الفشل صنّفها النظام كـ «خطأ غير مصنّف بعد من Meta». "
            "افحص قسم «العيّنات الخام من Meta» في الأسفل — يحوي ردّ Meta "
            "الكامل لكل محاولة (request + response + code + subcode + "
            "type + message). أرسل لقطة منها للدعم لإضافة الكود إلى "
            "المُصنِّف."
        )

    # ── Retry-storm hint ────────────────────────────────────────────
    # If ANY row crossed the circuit breaker, page the merchant
    # explicitly: this almost never happens by accident, and is the
    # signal that production saw the runaway loop bug.
    if retry_health.get("retry_storm_detected"):
        hints.append(
            "🚨 تم رصد retry storm — وصل بعض الصفوف إلى "
            f"{retry_health.get('max_attempt_count', '?')} محاولة "
            f"(الحد الأقصى للتنبيه = "
            f"{retry_health.get('attempt_circuit_breaker', 100)}). "
            "تم إيقاف هذه الصفوف تلقائياً (error_code=retry_storm). "
            "راجع لوغات Railway للبحث عن 'campaign_send_retry_storm'."
        )
    elif (retry_health.get("rows_at_attempt_ceiling") or 0) > 0:
        hints.append(
            f"{retry_health['rows_at_attempt_ceiling']} صف وصل إلى "
            f"الحد الأقصى للمحاولات "
            f"(MAX_SEND_ATTEMPTS={retry_health.get('max_send_attempts', 5)}) — "
            "صُنّفت كـ retry_exhausted ولن يُعاد المحاولة معها."
        )
    if (retry_health.get("zombie_sending_count") or 0) > 0:
        hints.append(
            f"{retry_health['zombie_sending_count']} صف عالق في sending "
            f"أطول من {retry_health.get('sending_timeout_seconds', 300)} ثانية — "
            "سيُعاد إحياؤها تلقائياً عند الإرسال التالي."
        )

    # ── Provider-side billing/account block hint ───────────────────
    # If ANY row failed with a provider_billing_block code, surface
    # the support-escalation copy FIRST — none of the other hints
    # matter until 360dialog is contacted.
    if provider_block.get("detected"):
        keys_summary = "، ".join(
            f"{kk['label_ar']} ({kk['count']})"
            for kk in (provider_block.get("error_keys") or [])
        )
        hints.insert(0, (
            "🛑 مشكلة من مزود واتساب أو الدفع — تواصل مع 360dialog. "
            f"تفاصيل: {keys_summary or provider_block.get('primary_label_ar') or ''}. "
            "أرسل تقرير الدعم الجاهز إلى فريق 360dialog من زر "
            "«نسخ تقرير الدعم»."
        ))

    if not (template_info or {}).get("approved"):
        hints.append(
            "القالب ليس بحالة APPROVED — لن يُرسل واتساب أي رسالة قبل "
            "اعتماد القالب من Meta."
        )
    if not wa_conn_info:
        hints.append("لا يوجد اتصال واتساب نشط لهذا المتجر.")
    if scheduler_info.get("kill_switch_set") and (campaign.schedule_type or "") != "immediate":
        hints.append(
            "NAHLA_DISABLE_SCHEDULERS=1 مفعّلة على Railway — الحملات "
            "المجدولة معطّلة. احذف هذا المتغيّر أو اضبطه على 0 ثم redeploy."
        )

    return {
        "campaign": {
            "id":                campaign.id,
            "name":              campaign.name,
            "status":            campaign.status,
            "lifecycle":         lifecycle,
            "campaign_type":     campaign.campaign_type,
            "audience_type":     campaign.audience_type,
            "audience_count":    campaign.audience_count or 0,
            "schedule_type":     campaign.schedule_type,
            "schedule_time":     campaign.schedule_time.isoformat() if campaign.schedule_time else None,
            "delay_minutes":     campaign.delay_minutes,
            "template_name":     campaign.template_name,
            "template_language": campaign.template_language,
            "launched_at":       campaign.launched_at.isoformat() if campaign.launched_at else None,
            "created_at":        campaign.created_at.isoformat() if campaign.created_at else None,
            "dispatch_errors":   dispatch_errors,
        },
        "recipients": {
            "total":                     sum(counts.values()),
            "queued":                    counts.get("queued", 0),
            "sending":                   counts.get("sending", 0),
            "sent":                      counts.get("sent", 0),
            "failed":                    counts.get("failed", 0),
            "skipped_duplicate":         counts.get("skipped_duplicate", 0),
            "skipped_invalid":           counts.get("skipped_invalid", 0),
            "skipped_unsubscribed":      counts.get("skipped_unsubscribed", 0),
            "skipped_unreachable":       counts.get("skipped_unreachable", 0),
            "skipped_manual_exclusion":  counts.get("skipped_manual_exclusion", 0),
        },
        # NEW: exact per-status breakdown including a bucket for any
        # non-canonical legacy/unknown status names. ``status_breakdown_raw``
        # mirrors counts verbatim so support can spot exotic values.
        "status_breakdown":     status_breakdown,
        "status_breakdown_raw": status_breakdown_raw,
        # NEW: first 10 send-log rows so the merchant can drill down
        # when ``materialized_rows > 0`` but all counters are zero.
        "sample_rows":          sample_rows,
        # NEW: retry health snapshot — surfaces ``retry_storm_detected``
        # and ``max_attempt_count`` so the merchant can see when the
        # circuit-breaker kicked in. Always present, even when there's
        # no sign of trouble (counters are then all zero).
        "retry_health":         retry_health,
        "sample_failed":    sample_failed,
        "sample_sent":      sample_sent,
        # NEW (P3): per-recipient delivery breakdown sourced from the
        # WhatsApp status webhook (delivered/read/failed_after_accept
        # timestamps on CampaignSendLog). Lets the UI distinguish:
        #   * "قبلتها Meta"      — accepted_by_provider
        #   * "وصلت للعميل"     — delivered
        #   * "قرأها العميل"     — read
        #   * "فشلت بعد القبول" — failed_after_accept
        #   * "لم تصل بعد"      — unknown_delivery (Meta accepted but
        #                          no downstream status webhook yet).
        # ``missing_provider_message_id`` flags rows that are in
        # ``status='sent'`` without a wamid — those are CORRUPT and
        # should never have been marked sent. UI surfaces them as
        # a hard warning.
        "delivery_summary": delivery_summary,
        # NEW: aggregated failure breakdown by canonical Meta-error key.
        # Powers the "3 عملاء لا يملكون واتساب" summary in the UI.
        "failure_summary":  failure_summary,
        # NEW: provider-side billing/account block signal. When
        # ``provider_block.detected`` is true the UI must:
        #   1. Show the rose support banner with ``support_message_ar``.
        #   2. Hide the "إرسال الآن" dispatch CTA (no point retrying).
        #   3. Surface the "نسخ تقرير الدعم" CTA which calls
        #      ``GET /campaigns/{id}/support-bundle``.
        "provider_block":   provider_block,
        # NEW: complete audience funnel (raw → reachable → snapshot →
        # queued). Lets the merchant see exactly where customers were
        # dropped before any send happened.
        "audience_funnel":           audience_funnel,
        # NEW: aggregated upstream-exclusion summary in Arabic. Used
        # by the UI to render "🚫 تم استبعاد 4 عملاء: 2 لا يملكون
        # واتساب، 2 أرقام غير صالحة" instead of the generic
        # "حملة بلا مستلمين".
        "excluded_reasons_summary":  excluded_reasons_summary,
        "excluded_before_send_count": excluded_before_send_count,
        # NEW: per-customer drill-down (up to 10) so support can see
        # exactly WHICH 4 customers were dropped and which field flag
        # caused it. Treats ``has_whatsapp=null`` as unknown (would
        # have been sent to Meta), NOT as a blocker.
        "sample_excluded_before_send": sample_excluded_before_send,
        # NEW: frequency-cap audit trail. ``bypassed`` is true when
        # the merchant explicitly set _bypass_frequency_cap on the
        # campaign. ``source_rows`` traces every capped phone to the
        # last successful campaign that "burned" the cap.
        "frequency_cap":             frequency_cap_audit,
        # NEW: raw Meta request/response fingerprints captured on
        # failure (especially when the classifier returned
        # ``unknown``). The UI renders these in an expandable panel
        # so support can fingerprint new error codes Meta started
        # emitting and add them to ``meta_errors._CODE_MAP``.
        "raw_meta_error_samples":    raw_meta_error_samples,
        "template":         template_info,
        "wa_connection":    wa_conn_info,
        "scheduler":        scheduler_info,
        "hints":            hints,
        "errors":           errors,
    }


@router.post("/campaigns/{campaign_id}/dispatch-now")
async def dispatch_campaign_now(
    campaign_id: int,
    request: Request,
    db: Session = Depends(get_db),
    bypass_frequency_cap: bool = Query(
        False,
        description=(
            "إذا كانت true، تُتخطّى حماية حد التكرار لهذا الإرسال فقط "
            "(يُخزَّن في الحملة ثم يُزال تلقائياً بعد قرار الحد)."
        ),
    ),
):
    """Kick the campaign dispatcher for one campaign in the background.

    Use cases:
      * The merchant launched a campaign and the asyncio background
        task died silently → click "إرسال يدوي الآن" to retry.
      * Support wants to confirm that the entire pipeline (template,
        connection, audience, send) works end-to-end without waiting
        for the 30s scheduler tick.

    Why background (not synchronous)?
    ─────────────────────────────────
    The dispatcher inserts a 1.5s pause between every send (Meta TOS
    + soft rate-limit) and a 2s pause between every batch, plus the
    Meta API call latency itself. For a 50-recipient campaign that
    easily exceeds the frontend's 25s ``AbortSignal.timeout``, so
    blocking the HTTP response on the full dispatch produced "signal
    timed out" errors for the merchant even when the send was working.

    Instead we:
      1. Run a *cheap* preflight (campaign exists, tenant scope, not
         already-completed) on the request thread.
      2. Pre-flip ``status='active'`` so the lifecycle pill on the
         page immediately becomes "جاري الإرسال" (instead of waiting
         on the dispatcher to do it ≈3s later).
      3. Hand off to ``_dispatch_campaign_async`` via
         ``asyncio.create_task`` — exactly the same path the
         immediate-launch flow uses.
      4. Return 202-style ``{ok: true, kicked: true}`` so the
         frontend can refresh and watch counters tick up.

    The dispatch_campaign function is itself idempotent (sent rows are
    never re-sent), so a curious merchant double-clicking the button
    cannot duplicate-send.
    """
    tenant_id = resolve_tenant_id(request)
    campaign = db.query(Campaign).filter(
        Campaign.id == campaign_id, Campaign.tenant_id == tenant_id,
    ).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    if (campaign.status or "").lower() == "completed" and (campaign.sent_count or 0) > 0:
        # Avoid wasting a Meta API call on a campaign that already
        # finished cleanly. The frequency cap would block re-sends
        # anyway, but bailing here surfaces it as a clear message
        # rather than "skipped_duplicate=N".
        return {
            "campaign_id": campaign_id,
            "ok":          True,
            "skipped":     True,
            "reason":      "completed",
            "message":     (
                "الحملة مكتملة مسبقاً — لا حاجة لإعادة الإرسال. أنشئ "
                "حملة جديدة إذا كنت تريد إرسالاً جديداً لنفس الجمهور."
            ),
        }

    # Pre-flip status so the merchant sees "جاري الإرسال" on the next
    # /campaigns refresh (≤2s away), instead of "ينتظر بدء الإرسال"
    # while we silently wait for the dispatcher to update it.
    rescheduled_count = 0
    revived_zombies = 0
    try:
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
        from services.campaign_dispatcher import (  # noqa: PLC0415
            reschedule_failed_for_retry, _revive_zombie_sending,
        )
        if bypass_frequency_cap:
            tv = dict(campaign.template_variables or {})
            tv["_bypass_frequency_cap"] = "true"
            campaign.template_variables = tv
            flag_modified(campaign, "template_variables")
        # Resurrect zombie ``sending`` rows from a crashed prior run so
        # the dispatcher doesn't trip over them. The watchdog also
        # runs at the top of the dispatcher itself, but doing it here
        # gives the merchant immediate visibility (the row flips to
        # ``queued`` before the next /debug refresh).
        revived_zombies = _revive_zombie_sending(db, campaign_id)
        # Promote retriable ``failed`` rows back to ``queued`` so this
        # dispatch run picks them up. Recipient-specific failures
        # (e.g. ``not_on_whatsapp``) stay terminal; only transient
        # error codes are retried, and only while ``attempt_count``
        # remains below ``MAX_SEND_ATTEMPTS``.
        rescheduled_count = reschedule_failed_for_retry(db, campaign_id)
        if (campaign.status or "").lower() != "active":
            campaign.status = "active"
        if not campaign.launched_at:
            campaign.launched_at = datetime.now(timezone.utc)
        campaign.updated_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:
        db.rollback()

    logger.info(
        "[campaigns.dispatch-now] tenant=%d campaign=%d status=%s "
        "audience_count=%s bypass_frequency_cap=%s — kicking background dispatch task",
        tenant_id, campaign_id, campaign.status, campaign.audience_count,
        bypass_frequency_cap,
    )

    # Fire-and-forget: same helper the immediate-launch path uses.
    # It opens a fresh DB session, runs the full pipeline, and
    # writes errors back into ``template_variables._dispatch_errors``
    # if anything goes wrong — visible via /campaigns and /debug.
    try:
        asyncio.create_task(_dispatch_campaign_async(campaign_id))
    except Exception as exc:
        logger.exception(
            "[campaigns.dispatch-now] tenant=%d campaign=%d could not "
            "spawn background task: %s",
            tenant_id, campaign_id, exc,
        )
        return {
            "campaign_id": campaign_id,
            "ok":          False,
            "error":       f"{type(exc).__name__}: {exc!s:.200}",
            "message":     "تعذر تشغيل الإرسال — راجع /campaigns/{id}/debug",
        }

    msg = (
        "بدأ الإرسال في الخلفية. حدّث الصفحة بعد لحظات لرؤية "
        "تقدّم العدّادات، أو اضغط 'تشخيص' لمراجعة الحالة فوراً."
    )
    if bypass_frequency_cap:
        msg += (
            " تم تجاهل حد التكرار لهذا الإرسال فقط — سيُستأنف الحماية "
            "تلقائياً في الإرسال التالي."
        )
    return {
        "campaign_id": campaign_id,
        "ok":          True,
        "kicked":      True,
        "status":      campaign.status,
        "bypass_frequency_cap": bypass_frequency_cap,
        # Surface the bookkeeping so the merchant sees "تمت إعادة
        # جدولة N محاولات فاشلة" + "تم تحرير N صف عالق" in the
        # diagnostic panel.
        "rescheduled_failed": rescheduled_count,
        "revived_zombies":    revived_zombies,
        "message":     msg,
    }


# ── Support bundle (provider escalation) ───────────────────────────────────


@router.get("/campaigns/{campaign_id}/support-bundle")
def campaign_support_bundle(
    campaign_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Return a JSON snapshot suitable for pasting into a 360dialog
    support ticket.

    Use case: a recipient (or our own WABA) is blocked by the provider
    over billing/payment/account restrictions
    (``provider_billing_block=True`` in the classifier). The merchant
    cannot fix this from the dashboard. The UI shows a banner and
    surfaces the "نسخ تقرير الدعم" button which calls this endpoint
    and copies the JSON to the merchant's clipboard.

    The bundle is intentionally:

    * **Self-contained** — every field a 360dialog support engineer
      would request (template name, language, WABA phone number id,
      Meta error code + subcode + raw payload). No follow-up
      back-and-forth needed.
    * **PII-aware** — recipient phone numbers are masked to the last
      4 digits. The merchant's WABA phone number id is included
      verbatim because that's exactly what 360dialog asks for.
    * **Stable shape** — versioned so we can extend it without
      breaking automation on the merchant side.

    The endpoint is read-only and safe to call any number of times.
    """
    tenant_id = resolve_tenant_id(request)
    campaign = db.query(Campaign).filter(
        Campaign.id == campaign_id, Campaign.tenant_id == tenant_id,
    ).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    from services.meta_errors import (  # noqa: PLC0415
        ERRORS as META_ERRORS, parse_technical,
    )

    def _mask(phone: Optional[str]) -> str:
        if not phone:
            return ""
        s = str(phone)
        return ("•" * max(0, len(s) - 4)) + s[-4:] if len(s) > 4 else s

    # 1. Aggregate provider-side blocking failures so support sees the
    #    exact distribution at a glance.
    blocked_keys = [k for k, v in META_ERRORS.items() if v.provider_billing_block]
    blocked_rows = []
    if blocked_keys:
        blocked_rows = (
            db.query(CampaignSendLog)
            .filter(
                CampaignSendLog.campaign_id == campaign_id,
                CampaignSendLog.status == "failed",
                CampaignSendLog.error_code.in_(blocked_keys),
            )
            .order_by(CampaignSendLog.updated_at.desc())
            .limit(20)
            .all()
        )

    per_key: Dict[str, int] = {}
    sample_recipients = []
    for r in blocked_rows:
        k = (r.error_code or "").strip().lower()
        per_key[k] = per_key.get(k, 0) + 1
        if len(sample_recipients) < 10:
            parsed = parse_technical(r.error_message)
            sample_recipients.append({
                "phone_masked":       _mask(r.customer_phone_e164),
                "error_code":         r.error_code,
                "error_label_ar":     META_ERRORS[k].label_ar if k in META_ERRORS else None,
                "error_message_raw":  (r.error_message or "")[:400],
                "meta_error_code":    parsed["meta_error_code"],
                "meta_error_subcode": parsed["meta_error_subcode"],
                "meta_error_type":    parsed["meta_error_type"],
                "meta_error_message": parsed["meta_error_message"],
                "attempt_count":      int(r.attempt_count or 0),
                "occurred_at":        r.updated_at.isoformat() if r.updated_at else None,
            })

    # 2. WhatsApp connection (provider, phone_number_id). 360dialog
    #    needs the phone_number_id to identify the WABA — keep it
    #    verbatim, it's not PII.
    wa_conn_info = None
    try:
        from services.campaign_dispatcher import _get_wa_connection  # noqa: PLC0415
        conn = _get_wa_connection(db, tenant_id)
        if conn:
            wa_conn_info = {
                "provider": (
                    getattr(conn, "provider", None)
                    or getattr(conn, "provider_name", None)
                ),
                "phone_number_id": getattr(conn, "phone_number_id", None),
                "business_account_id": getattr(conn, "business_account_id", None),
                "status":            getattr(conn, "status", None),
            }
    except Exception:
        wa_conn_info = None

    # 3. Template metadata (name + language is what support keys off).
    template_info = None
    try:
        tpl = db.query(WhatsAppTemplate).filter(
            WhatsAppTemplate.id == int(campaign.template_id or 0),
            WhatsAppTemplate.tenant_id == tenant_id,
        ).first() if campaign.template_id else None
        if tpl:
            template_info = {
                "id":       tpl.id,
                "name":     tpl.name,
                "language": tpl.language,
                "category": tpl.category,
                "status":   tpl.status,
            }
    except Exception:
        template_info = None

    # 4. Raw Meta error samples (last few). Same data the debug
    #    endpoint exposes — included here so support has a single
    #    JSON payload to work from.
    raw_meta_samples: List[Any] = []
    try:
        import json as _json  # noqa: PLC0415
        raw = (campaign.template_variables or {}).get("_raw_meta_error_samples")
        if isinstance(raw, list):
            raw_meta_samples = raw[-5:]
        elif isinstance(raw, str) and raw.strip():
            try:
                parsed_raw = _json.loads(raw)
                raw_meta_samples = parsed_raw[-5:] if isinstance(parsed_raw, list) else []
            except Exception:
                raw_meta_samples = []
    except Exception:
        raw_meta_samples = []

    detected = bool(blocked_rows)
    primary_key = max(per_key, key=per_key.get) if per_key else None  # type: ignore[arg-type]
    primary_label = (
        META_ERRORS[primary_key].label_ar
        if primary_key and primary_key in META_ERRORS
        else None
    )

    # 5. Human-readable Arabic message the merchant can paste straight
    #    into a 360dialog ticket. Keeps the technical block (JSON)
    #    underneath for the support engineer.
    support_message_ar_lines = [
        "السلام عليكم،",
        "",
        (
            "نواجه مشكلة على حملة واتساب — الردّ من Meta/المزود يشير "
            "إلى قيود على الحساب أو رصيد الدفع."
        ),
        "",
        f"- اسم الحملة: {campaign.name}",
        f"- معرّف الحملة (campaign_id): {campaign.id}",
        f"- عدد الجمهور (audience): {campaign.audience_count or 0}",
        f"- اسم القالب: {(template_info or {}).get('name') or campaign.template_name or '—'}",
        f"- اللغة: {(template_info or {}).get('language') or campaign.template_language or '—'}",
    ]
    if wa_conn_info and wa_conn_info.get("phone_number_id"):
        support_message_ar_lines.append(
            f"- phone_number_id لدى المزود: {wa_conn_info['phone_number_id']}"
        )
    if primary_label:
        support_message_ar_lines.append(
            f"- تصنيف الخطأ الأساسي: {primary_label} "
            f"(عدد {per_key.get(primary_key, 0)})"
        )
    support_message_ar_lines.append("")
    support_message_ar_lines.append(
        "يرجى مراجعة القيود على هذا الحساب/الرقم. "
        "أدناه التقرير التقني الكامل بصيغة JSON."
    )

    bundle = {
        "version": "1",
        "kind":    "nahla.campaign.support_bundle",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id":    tenant_id,
        "support_provider": "360dialog",
        "campaign": {
            "id":             campaign.id,
            "name":           campaign.name,
            "status":         campaign.status,
            "campaign_type":  campaign.campaign_type,
            "audience_count": campaign.audience_count or 0,
            "sent_count":     campaign.sent_count or 0,
            "launched_at":    campaign.launched_at.isoformat() if campaign.launched_at else None,
            "created_at":     campaign.created_at.isoformat() if campaign.created_at else None,
        },
        "template":   template_info,
        "wa_connection": wa_conn_info,
        "provider_block": {
            "detected": detected,
            "count":    len(blocked_rows),
            "error_keys": [
                {
                    "key": k,
                    "count": c,
                    "label_ar": (
                        META_ERRORS[k].label_ar if k in META_ERRORS else None
                    ),
                }
                for k, c in sorted(per_key.items(), key=lambda kv: -kv[1])
            ],
            "primary_key":      primary_key,
            "primary_label_ar": primary_label,
        },
        "sample_recipients":  sample_recipients,
        "raw_meta_samples":   raw_meta_samples,
        "support_message_ar": "\n".join(support_message_ar_lines),
    }
    return bundle


# ── Campaign dispatch (background) ──────────────────────────────────────────

async def _dispatch_campaign_async(campaign_id: int) -> None:
    """Fire-and-forget async task that dispatches a campaign using a fresh
    DB session.  Runs on the main uvicorn event loop via asyncio.create_task,
    so all async HTTP calls (provider_send_message -> httpx) work natively."""
    from core.database import SessionLocal  # noqa: PLC0415
    from services.campaign_dispatcher import dispatch_campaign  # noqa: PLC0415

    logger.info("[campaigns] dispatching campaign=%d in async task", campaign_id)
    db = SessionLocal()
    try:
        result = await dispatch_campaign(db, campaign_id)
        logger.info(
            "[campaigns] dispatch done campaign=%d: sent=%s failed=%s errors=%s",
            campaign_id, result.get("sent"), result.get("failed"),
            result.get("errors", [])[:3],
        )
    except Exception as exc:
        logger.error(
            "[campaigns] dispatch failed campaign=%d: %s",
            campaign_id, exc, exc_info=True,
        )
        try:
            from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
            campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
            if campaign and campaign.status not in ("completed", "failed"):
                campaign.status = "failed"
                tpl_vars = dict(campaign.template_variables or {})
                tpl_vars["_dispatch_errors"] = f"خطأ داخلي: {str(exc)[:200]}"
                campaign.template_variables = tpl_vars
                flag_modified(campaign, "template_variables")
                db.commit()
        except Exception:
            logger.error("[campaigns] could not mark campaign=%d as failed", campaign_id, exc_info=True)
    finally:
        db.close()
