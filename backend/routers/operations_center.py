"""
routers/operations_center.py
────────────────────────────
Operations Center — structured branches, contacts, escalation (PR-B).

Dashboard CRUD only. Runtime reads these tables when
USE_STRUCTURED_BRANCH_CONTACTS is enabled (PR-A); this router does not
toggle that flag or mutate KB content.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id
from models import BranchContact, BranchEscalationStep, MerchantBranch
from utils.phone_utils import normalize_to_e164

router = APIRouter(prefix="/operations-center", tags=["Operations Center"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class BranchCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    city: Optional[str] = Field(None, max_length=128)
    district: Optional[str] = Field(None, max_length=128)
    address: Optional[str] = None
    maps_url: Optional[str] = Field(None, max_length=2048)
    hours_json: Optional[Dict[str, Any]] = None
    is_active: bool = True
    sort_order: int = 0


class BranchPatchIn(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    city: Optional[str] = Field(None, max_length=128)
    district: Optional[str] = Field(None, max_length=128)
    address: Optional[str] = None
    maps_url: Optional[str] = Field(None, max_length=2048)
    hours_json: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


class ContactCreateIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)
    role: Optional[str] = Field(None, max_length=128)
    phone_e164: str = Field(min_length=5, max_length=32)
    whatsapp_e164: Optional[str] = Field(None, max_length=32)
    is_active: bool = True
    is_default_reception: bool = False
    sort_order: int = 0


class ContactPatchIn(BaseModel):
    display_name: Optional[str] = Field(None, min_length=1, max_length=255)
    role: Optional[str] = Field(None, max_length=128)
    phone_e164: Optional[str] = Field(None, min_length=5, max_length=32)
    whatsapp_e164: Optional[str] = Field(None, max_length=32)
    is_active: Optional[bool] = None
    is_default_reception: Optional[bool] = None
    sort_order: Optional[int] = None


class EscalationStepCreateIn(BaseModel):
    escalation_level: int = Field(ge=1, le=99)
    display_name: str = Field(min_length=1, max_length=255)
    role: Optional[str] = Field(None, max_length=128)
    phone_e164: str = Field(min_length=5, max_length=32)
    is_active: bool = True
    sort_order: int = 0


class EscalationStepPatchIn(BaseModel):
    escalation_level: Optional[int] = Field(None, ge=1, le=99)
    display_name: Optional[str] = Field(None, min_length=1, max_length=255)
    role: Optional[str] = Field(None, max_length=128)
    phone_e164: Optional[str] = Field(None, min_length=5, max_length=32)
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


class EscalationReorderIn(BaseModel):
    step_ids: List[int] = Field(min_length=1)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _normalize_phone_field(raw: Optional[str], *, field: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise HTTPException(status_code=422, detail=f"invalid_{field}")
    e164 = normalize_to_e164(value)
    if not e164:
        raise HTTPException(status_code=422, detail=f"invalid_{field}")
    return e164


def _optional_phone_field(raw: Optional[str], *, field: str) -> Optional[str]:
    value = (raw or "").strip()
    if not value:
        return None
    e164 = normalize_to_e164(value)
    if not e164:
        raise HTTPException(status_code=422, detail=f"invalid_{field}")
    return e164


def _get_branch(db: Session, tenant_id: int, branch_id: int) -> MerchantBranch:
    row = (
        db.query(MerchantBranch)
        .filter(
            MerchantBranch.id == branch_id,
            MerchantBranch.tenant_id == tenant_id,
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="branch_not_found")
    return row


def _get_contact(
    db: Session, tenant_id: int, branch_id: int, contact_id: int,
) -> BranchContact:
    _get_branch(db, tenant_id, branch_id)
    row = (
        db.query(BranchContact)
        .filter(
            BranchContact.id == contact_id,
            BranchContact.branch_id == branch_id,
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="contact_not_found")
    return row


def _get_escalation_step(
    db: Session, tenant_id: int, branch_id: int, step_id: int,
) -> BranchEscalationStep:
    _get_branch(db, tenant_id, branch_id)
    row = (
        db.query(BranchEscalationStep)
        .filter(
            BranchEscalationStep.id == step_id,
            BranchEscalationStep.branch_id == branch_id,
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="escalation_step_not_found")
    return row


def _clear_default_reception(db: Session, branch_id: int, *, except_id: Optional[int] = None) -> None:
    q = db.query(BranchContact).filter(BranchContact.branch_id == branch_id)
    if except_id is not None:
        q = q.filter(BranchContact.id != except_id)
    q.update({BranchContact.is_default_reception: False}, synchronize_session=False)


def _serialize_branch(row: MerchantBranch, *, contact_count: int = 0) -> Dict[str, Any]:
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "name": row.name,
        "city": row.city or "",
        "district": row.district or "",
        "address": row.address or "",
        "maps_url": row.maps_url or "",
        "hours_json": row.hours_json,
        "is_active": bool(row.is_active),
        "sort_order": int(row.sort_order or 0),
        "contact_count": int(contact_count),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _serialize_contact(row: BranchContact) -> Dict[str, Any]:
    return {
        "id": row.id,
        "branch_id": row.branch_id,
        "display_name": row.display_name,
        "role": row.role or "",
        "phone_e164": row.phone_e164,
        "whatsapp_e164": row.whatsapp_e164 or "",
        "is_active": bool(row.is_active),
        "is_default_reception": bool(getattr(row, "is_default_reception", False)),
        "sort_order": int(row.sort_order or 0),
    }


def _serialize_escalation_step(row: BranchEscalationStep) -> Dict[str, Any]:
    return {
        "id": row.id,
        "branch_id": row.branch_id,
        "escalation_level": int(row.escalation_level or 1),
        "display_name": row.display_name,
        "role": row.role or "",
        "phone_e164": row.phone_e164,
        "is_active": bool(row.is_active),
        "sort_order": int(row.sort_order or 0),
    }


# ── Branches ──────────────────────────────────────────────────────────────────


@router.get("/branches")
async def list_branches(request: Request, db: Session = Depends(get_db)) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    counts = dict(
        db.query(BranchContact.branch_id, func.count(BranchContact.id))
        .join(MerchantBranch, MerchantBranch.id == BranchContact.branch_id)
        .filter(MerchantBranch.tenant_id == tenant_id)
        .group_by(BranchContact.branch_id)
        .all()
    )
    rows = (
        db.query(MerchantBranch)
        .filter(MerchantBranch.tenant_id == tenant_id)
        .order_by(MerchantBranch.sort_order.asc(), MerchantBranch.id.asc())
        .all()
    )
    return {
        "branches": [
            _serialize_branch(r, contact_count=int(counts.get(r.id, 0)))
            for r in rows
        ],
    }


@router.post("/branches", status_code=201)
async def create_branch(
    body: BranchCreateIn,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    now = datetime.now(timezone.utc)
    row = MerchantBranch(
        tenant_id=tenant_id,
        name=body.name.strip(),
        city=(body.city or "").strip() or None,
        district=(body.district or "").strip() or None,
        address=(body.address or "").strip() or None,
        maps_url=(body.maps_url or "").strip() or None,
        hours_json=body.hours_json,
        is_active=body.is_active,
        sort_order=body.sort_order,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_branch(row)


@router.get("/branches/{branch_id}")
async def get_branch(
    branch_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    row = _get_branch(db, tenant_id, branch_id)
    contact_count = (
        db.query(func.count(BranchContact.id))
        .filter(BranchContact.branch_id == branch_id)
        .scalar()
        or 0
    )
    return _serialize_branch(row, contact_count=int(contact_count))


@router.put("/branches/{branch_id}")
async def update_branch(
    branch_id: int,
    body: BranchPatchIn,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    row = _get_branch(db, tenant_id, branch_id)
    data = body.model_dump(exclude_unset=True)
    for key, val in data.items():
        if key in {"city", "district", "address", "maps_url"} and val is not None:
            val = str(val).strip() or None
        setattr(row, key, val)
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return _serialize_branch(row)


@router.delete("/branches/{branch_id}", status_code=204)
async def delete_branch(
    branch_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> None:
    tenant_id = resolve_tenant_id(request)
    row = _get_branch(db, tenant_id, branch_id)
    db.delete(row)
    db.commit()


@router.post("/branches/{branch_id}/activate")
async def activate_branch(
    branch_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    row = _get_branch(db, tenant_id, branch_id)
    row.is_active = True
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return _serialize_branch(row)


@router.post("/branches/{branch_id}/deactivate")
async def deactivate_branch(
    branch_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    row = _get_branch(db, tenant_id, branch_id)
    row.is_active = False
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return _serialize_branch(row)


# ── Contacts ──────────────────────────────────────────────────────────────────


@router.get("/branches/{branch_id}/contacts")
async def list_contacts(
    branch_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    _get_branch(db, tenant_id, branch_id)
    rows = (
        db.query(BranchContact)
        .filter(BranchContact.branch_id == branch_id)
        .order_by(BranchContact.sort_order.asc(), BranchContact.id.asc())
        .all()
    )
    return {"contacts": [_serialize_contact(r) for r in rows]}


@router.post("/branches/{branch_id}/contacts", status_code=201)
async def create_contact(
    branch_id: int,
    body: ContactCreateIn,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    _get_branch(db, tenant_id, branch_id)
    phone = _normalize_phone_field(body.phone_e164, field="phone_e164")
    whatsapp = _optional_phone_field(body.whatsapp_e164, field="whatsapp_e164")
    if body.is_default_reception:
        _clear_default_reception(db, branch_id)
    row = BranchContact(
        branch_id=branch_id,
        display_name=body.display_name.strip(),
        role=(body.role or "").strip() or None,
        phone_e164=phone,
        whatsapp_e164=whatsapp,
        is_active=body.is_active,
        is_default_reception=body.is_default_reception,
        sort_order=body.sort_order,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_contact(row)


@router.put("/branches/{branch_id}/contacts/{contact_id}")
async def update_contact(
    branch_id: int,
    contact_id: int,
    body: ContactPatchIn,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    row = _get_contact(db, tenant_id, branch_id, contact_id)
    data = body.model_dump(exclude_unset=True)
    if "phone_e164" in data:
        data["phone_e164"] = _normalize_phone_field(data["phone_e164"], field="phone_e164")
    if "whatsapp_e164" in data:
        data["whatsapp_e164"] = _optional_phone_field(
            data.get("whatsapp_e164"), field="whatsapp_e164",
        )
    if data.get("is_default_reception"):
        _clear_default_reception(db, branch_id, except_id=contact_id)
    for key, val in data.items():
        if key == "role" and val is not None:
            val = str(val).strip() or None
        setattr(row, key, val)
    db.commit()
    db.refresh(row)
    return _serialize_contact(row)


@router.delete("/branches/{branch_id}/contacts/{contact_id}", status_code=204)
async def delete_contact(
    branch_id: int,
    contact_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> None:
    tenant_id = resolve_tenant_id(request)
    row = _get_contact(db, tenant_id, branch_id, contact_id)
    db.delete(row)
    db.commit()


@router.post("/branches/{branch_id}/contacts/{contact_id}/set-default-reception")
async def set_default_reception(
    branch_id: int,
    contact_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    row = _get_contact(db, tenant_id, branch_id, contact_id)
    _clear_default_reception(db, branch_id, except_id=contact_id)
    row.is_default_reception = True
    row.is_active = True
    db.commit()
    db.refresh(row)
    return _serialize_contact(row)


# ── Escalation steps ──────────────────────────────────────────────────────────


@router.get("/branches/{branch_id}/escalation-steps")
async def list_escalation_steps(
    branch_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    _get_branch(db, tenant_id, branch_id)
    rows = (
        db.query(BranchEscalationStep)
        .filter(BranchEscalationStep.branch_id == branch_id)
        .order_by(
            BranchEscalationStep.escalation_level.asc(),
            BranchEscalationStep.sort_order.asc(),
            BranchEscalationStep.id.asc(),
        )
        .all()
    )
    return {"steps": [_serialize_escalation_step(r) for r in rows]}


@router.post("/branches/{branch_id}/escalation-steps", status_code=201)
async def create_escalation_step(
    branch_id: int,
    body: EscalationStepCreateIn,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    _get_branch(db, tenant_id, branch_id)
    phone = _normalize_phone_field(body.phone_e164, field="phone_e164")
    row = BranchEscalationStep(
        branch_id=branch_id,
        escalation_level=body.escalation_level,
        display_name=body.display_name.strip(),
        role=(body.role or "").strip() or None,
        phone_e164=phone,
        is_active=body.is_active,
        sort_order=body.sort_order,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_escalation_step(row)


@router.put("/branches/{branch_id}/escalation-steps/{step_id}")
async def update_escalation_step(
    branch_id: int,
    step_id: int,
    body: EscalationStepPatchIn,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    row = _get_escalation_step(db, tenant_id, branch_id, step_id)
    data = body.model_dump(exclude_unset=True)
    if "phone_e164" in data:
        data["phone_e164"] = _normalize_phone_field(data["phone_e164"], field="phone_e164")
    for key, val in data.items():
        if key == "role" and val is not None:
            val = str(val).strip() or None
        setattr(row, key, val)
    db.commit()
    db.refresh(row)
    return _serialize_escalation_step(row)


@router.delete("/branches/{branch_id}/escalation-steps/{step_id}", status_code=204)
async def delete_escalation_step(
    branch_id: int,
    step_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> None:
    tenant_id = resolve_tenant_id(request)
    row = _get_escalation_step(db, tenant_id, branch_id, step_id)
    db.delete(row)
    db.commit()


@router.post("/branches/{branch_id}/escalation-steps/reorder")
async def reorder_escalation_steps(
    branch_id: int,
    body: EscalationReorderIn,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    _get_branch(db, tenant_id, branch_id)
    rows = (
        db.query(BranchEscalationStep)
        .filter(BranchEscalationStep.branch_id == branch_id)
        .all()
    )
    by_id = {r.id: r for r in rows}
    if set(body.step_ids) != set(by_id.keys()):
        raise HTTPException(status_code=422, detail="reorder_ids_mismatch")
    for idx, step_id in enumerate(body.step_ids, start=1):
        row = by_id[step_id]
        row.escalation_level = idx
        row.sort_order = idx - 1
    db.commit()
    ordered = (
        db.query(BranchEscalationStep)
        .filter(BranchEscalationStep.branch_id == branch_id)
        .order_by(
            BranchEscalationStep.escalation_level.asc(),
            BranchEscalationStep.sort_order.asc(),
        )
        .all()
    )
    return {"steps": [_serialize_escalation_step(r) for r in ordered]}
