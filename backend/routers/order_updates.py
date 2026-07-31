"""HTTP API for Order Updates (تحديثات الطلبات) merchant settings."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth import get_current_user
from core.commerce_lifecycle.order_updates import (
    ORDER_UPDATE_SERVICE_KEYS,
    create_revision_from_active,
    get_order_update_flags,
    is_order_update_service_key,
    promote_approved_revision,
    resolve_active_and_pending,
    set_order_update_flags,
)
from core.database import get_db

router = APIRouter(prefix="/order-updates", tags=["order-updates"])


class OrderUpdateFlagsPayload(BaseModel):
    order_confirmation: Optional[Any] = None
    shipping_tracking: Optional[Any] = None
    services: Optional[Dict[str, Any]] = None
    flags: Optional[Dict[str, Any]] = None


class RevisionCreatePayload(BaseModel):
    body_text: str = Field(..., min_length=1)
    display_name_ar: Optional[str] = None
    submit_to_meta: bool = False


def _tenant_id(user: Any) -> int:
    tid = getattr(user, "tenant_id", None)
    if tid is None:
        raise HTTPException(status_code=403, detail="tenant_required")
    return int(tid)


async def _submit_draft_to_meta(db: Session, tenant_id: int, tpl: Any) -> Dict[str, Any]:
    from models import WhatsAppConnection  # noqa: PLC0415
    from core.billing import has_billing_access  # noqa: PLC0415
    from routers.settings import DEFAULT_WHATSAPP, get_or_create_settings, merge_defaults  # noqa: PLC0415
    from routers.templates import _submit_template_to_meta, _tpl_to_dict  # noqa: PLC0415
    from services.whatsapp_platform.wa_connection_secrets import read_access_token  # noqa: PLC0415

    if not has_billing_access(db, tenant_id):
        raise HTTPException(
            status_code=402,
            detail={"code": "subscription_inactive", "message": "الاشتراك غير فعّال."},
        )
    if str(tpl.status) not in ("DRAFT", "REJECTED"):
        raise HTTPException(
            status_code=400,
            detail={"code": "template_status_invalid", "message": f"حالة غير قابلة للإرسال: {tpl.status}"},
        )

    wa_conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == tenant_id)
        .order_by(WhatsAppConnection.created_at.desc())
        .first()
    )
    settings = get_or_create_settings(db, tenant_id)
    wa = merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    waba_id = (
        (wa_conn.whatsapp_business_account_id if wa_conn else None)
        or wa.get("whatsapp_business_account_id", "")
    )
    if not waba_id:
        raise HTTPException(
            status_code=422,
            detail={"code": "missing_waba_id", "message": "WABA مفقود — أعد ربط واتساب."},
        )

    try:
        meta_id = await _submit_template_to_meta(
            db=db,
            conn=wa_conn,
            tenant_id=tenant_id,
            waba_id=str(waba_id),
            name=tpl.name,
            language=tpl.language or "ar",
            category=tpl.category or "UTILITY",
            components=list(tpl.components or []),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "meta_validation_error", "message": str(exc)},
        ) from exc

    if not meta_id:
        raise HTTPException(
            status_code=502,
            detail={"code": "meta_no_id_returned", "message": "Meta لم تُرجع معرّف القالب."},
        )

    tpl.meta_template_id = meta_id
    tpl.status = "PENDING"
    tpl.synced_at = datetime.now(timezone.utc)
    tpl.updated_at = datetime.now(timezone.utc)
    db.flush()
    return {"submitted": True, "template": _tpl_to_dict(tpl)}


@router.get("/settings")
def get_settings(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
) -> Dict[str, Any]:
    tid = _tenant_id(user)
    flags = get_order_update_flags(db, tid)
    services = {
        key: resolve_active_and_pending(db, tid, key)
        for key in ORDER_UPDATE_SERVICE_KEYS
    }
    # Flat enable toggles for the Settings tab API client.
    return {
        "flags": flags,
        "services": {
            key: {"enabled": flags[key], **services[key]}
            for key in ORDER_UPDATE_SERVICE_KEYS
        },
        "order_confirmation": {"enabled": flags["order_confirmation"]},
        "shipping_tracking": {"enabled": flags["shipping_tracking"]},
    }


@router.put("/settings")
def put_settings(
    payload: OrderUpdateFlagsPayload,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
) -> Dict[str, Any]:
    tid = _tenant_id(user)
    raw = payload.model_dump(exclude_none=True)
    updates: Dict[str, bool] = {}

    def _consume(key: str, val: Any) -> None:
        if isinstance(val, bool):
            updates[key] = val
        elif isinstance(val, dict) and "enabled" in val:
            updates[key] = bool(val["enabled"])

    for key in ORDER_UPDATE_SERVICE_KEYS:
        if key in raw:
            _consume(key, raw[key])
    for bucket_name in ("services", "flags"):
        bucket = raw.get(bucket_name) or {}
        if isinstance(bucket, dict):
            for key in ORDER_UPDATE_SERVICE_KEYS:
                if key in bucket:
                    _consume(key, bucket[key])

    set_order_update_flags(db, tid, updates, commit=True)
    return get_settings(db=db, user=user)


@router.get("/{service_key}")
def get_service(
    service_key: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
) -> Dict[str, Any]:
    if not is_order_update_service_key(service_key):
        raise HTTPException(status_code=404, detail="unknown_service_key")
    return resolve_active_and_pending(db, _tenant_id(user), service_key)


@router.post("/{service_key}/revisions")
async def create_revision(
    service_key: str,
    payload: RevisionCreatePayload,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
) -> Dict[str, Any]:
    if not is_order_update_service_key(service_key):
        raise HTTPException(status_code=404, detail="unknown_service_key")
    tid = _tenant_id(user)
    try:
        draft = create_revision_from_active(
            db,
            tid,
            service_key,
            payload.body_text,
            display_name_ar=payload.display_name_ar,
            commit=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    submit_result: Optional[Dict[str, Any]] = None
    if payload.submit_to_meta:
        submit_result = await _submit_draft_to_meta(db, tid, draft)
        db.commit()
        db.refresh(draft)

    return {
        "revision": resolve_active_and_pending(db, tid, service_key),
        "created_template_id": int(draft.id),
        "submit_result": submit_result,
    }


@router.post("/{service_key}/revisions/{template_id}/submit")
async def submit_revision(
    service_key: str,
    template_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
) -> Dict[str, Any]:
    if not is_order_update_service_key(service_key):
        raise HTTPException(status_code=404, detail="unknown_service_key")
    tid = _tenant_id(user)
    from models import WhatsAppTemplate  # noqa: PLC0415

    tpl = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.id == int(template_id),
            WhatsAppTemplate.tenant_id == tid,
            WhatsAppTemplate.service_key == service_key,
        )
        .first()
    )
    if tpl is None:
        raise HTTPException(status_code=404, detail="template_not_found")
    result = await _submit_draft_to_meta(db, tid, tpl)
    db.commit()
    return {
        "submit_result": result,
        "service": resolve_active_and_pending(db, tid, service_key),
    }


@router.post("/{service_key}/revisions/{template_id}/promote")
def promote_revision(
    service_key: str,
    template_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
) -> Dict[str, Any]:
    if not is_order_update_service_key(service_key):
        raise HTTPException(status_code=404, detail="unknown_service_key")
    tid = _tenant_id(user)
    ok = promote_approved_revision(db, tenant_id=tid, template_id=template_id, commit=True)
    if not ok:
        raise HTTPException(status_code=400, detail="promote_not_applicable")
    return resolve_active_and_pending(db, tid, service_key)
