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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models import Campaign, CampaignSendLog, WhatsAppTemplate  # noqa: E402

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
    # was unapproved`` at a glance.
    last_error = dispatch_errors[0] if dispatch_errors else None

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


def _classify_campaign_lifecycle(campaign: Campaign, counts: Dict[str, int]) -> str:
    """Map the raw ``status`` column + send-log counters onto a
    merchant-friendly verb so the UI never has to show just "نشطة" for
    an inert campaign.

    Order matters: a campaign whose dispatch task crashed BEFORE
    snapshotting any recipient will be ``active`` with zero counts, and
    we want the merchant to see that explicitly as "ينتظر بدء الإرسال"
    instead of the falsely reassuring "نشطة".
    """
    status = (campaign.status or "").lower()
    sent = counts.get("sent", 0)
    failed = counts.get("failed", 0)
    queued = counts.get("queued", 0)
    total = sum(counts.values())

    if status in ("completed",):
        if sent > 0 and failed == 0:
            return "sent"               # تم الإرسال
        if sent > 0 and failed > 0:
            return "partial"            # أُرسل جزئياً
        if failed > 0 and sent == 0:
            return "failed_all"         # فشل الإرسال للجميع
        return "completed_empty"        # اكتملت بدون مستلمين فعليين
    if status == "failed":
        return "failed"                 # فشل الإرسال
    if status == "scheduled":
        return "waiting_scheduler"      # بانتظار المُجدول
    if status == "active":
        if total == 0:
            return "pending_dispatch"   # ينتظر بدء الإرسال
        if queued > 0:
            return "sending"            # جاري الإرسال
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

    def _sample_failed():
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
        return [
            {
                "phone":              _mask(r.customer_phone_e164),
                "error_code":         r.error_code,
                "error_message":      (r.error_message or "")[:300],
                "attempt_count":      r.attempt_count,
                "updated_at":         r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]
    sample_failed = _safe("sample_failed", _sample_failed) or []

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
        return [
            {
                "phone":              _mask(r.customer_phone_e164),
                "provider_message_id": r.provider_message_id,
                "sent_at":            r.sent_at.isoformat() if r.sent_at else None,
            }
            for r in rows
        ]
    sample_sent = _safe("sample_sent", _sample_sent) or []

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

    lifecycle = _classify_campaign_lifecycle(campaign, counts)

    # If lifecycle is pending_dispatch but status=active and audience is
    # > 0, that almost always means the asyncio.create_task background
    # job died before _snapshot_recipients ran. Surface a direct hint.
    hints: List[str] = []
    if lifecycle == "pending_dispatch" and (campaign.audience_count or 0) > 0:
        hints.append(
            "تم إنشاء الحملة بدون أي مستلم في سجل الإرسال. الأسباب "
            "المحتملة: (1) خلل في مَهمّة asyncio الخلفية (راجع سجلات "
            "Railway للبحث عن 'dispatching campaign'). (2) فشل التحقق "
            "من القالب أو اتصال واتساب. استخدم POST "
            f"/campaigns/{campaign_id}/dispatch-now لإعادة المحاولة."
        )
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
        "sample_failed":    sample_failed,
        "sample_sent":      sample_sent,
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
):
    """Synchronously run the campaign dispatcher for one campaign.

    Use cases:
      * The merchant launched a campaign and the asyncio background
        task died silently → click "إرسال يدوي الآن" to retry.
      * Support wants to confirm that the entire pipeline (template,
        connection, audience, send) works end-to-end without waiting
        for the 30s scheduler tick.

    Strict tenant scoping: the campaign must belong to the requesting
    tenant. Returns the full :func:`dispatch_campaign` result dict so
    the merchant can see ``sent`` / ``failed`` / ``errors`` directly
    in the response (no second round-trip required).

    Idempotency note — ``dispatch_campaign`` is itself idempotent:
    rows already in ``status='sent'`` are NEVER re-sent on retry, so
    calling this endpoint twice in a row will not double-send.
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
            "skipped":     True,
            "reason":      "completed",
            "message":     (
                "الحملة مكتملة مسبقاً — لا حاجة لإعادة الإرسال. أنشئ "
                "حملة جديدة إذا كنت تريد إرسالاً جديداً لنفس الجمهور."
            ),
        }

    logger.info(
        "[campaigns.dispatch-now] tenant=%d campaign=%d status=%s audience_count=%s",
        tenant_id, campaign_id, campaign.status, campaign.audience_count,
    )
    from services.campaign_dispatcher import dispatch_campaign  # noqa: PLC0415
    try:
        result = await dispatch_campaign(db, campaign_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[campaigns.dispatch-now] tenant=%d campaign=%d crashed",
            tenant_id, campaign_id,
        )
        try:
            from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
            campaign.status = "failed"
            tpl_vars = dict(campaign.template_variables or {})
            tpl_vars["_dispatch_errors"] = f"manual dispatch crashed: {str(exc)[:200]}"
            campaign.template_variables = tpl_vars
            flag_modified(campaign, "template_variables")
            db.commit()
        except Exception:
            db.rollback()
        return {
            "campaign_id": campaign_id,
            "ok":          False,
            "error":       f"{type(exc).__name__}: {exc!s:.200}",
            "message":     "فشل تشغيل الإرسال يدوياً — راجع /campaigns/{id}/debug",
        }

    return {
        "campaign_id": campaign_id,
        "ok":          True,
        **result,
    }


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
