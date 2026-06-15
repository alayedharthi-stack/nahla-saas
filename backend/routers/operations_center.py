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
from models import BranchArrivalKeyword, BranchContact, BranchEscalationStep, MerchantBranch
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
    location_response_mode: str = "location_only"
    arrival_response_mode: str = "reception_only"
    location_instructions_text: Optional[str] = None


class BranchPatchIn(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    city: Optional[str] = Field(None, max_length=128)
    district: Optional[str] = Field(None, max_length=128)
    address: Optional[str] = None
    maps_url: Optional[str] = Field(None, max_length=2048)
    hours_json: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None
    location_response_mode: Optional[str] = None
    arrival_response_mode: Optional[str] = None
    location_instructions_text: Optional[str] = None


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
    contact_id: Optional[int] = None


class EscalationStepPatchIn(BaseModel):
    escalation_level: Optional[int] = Field(None, ge=1, le=99)
    display_name: Optional[str] = Field(None, min_length=1, max_length=255)
    role: Optional[str] = Field(None, max_length=128)
    phone_e164: Optional[str] = Field(None, min_length=5, max_length=32)
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None
    contact_id: Optional[int] = None


class EscalationReorderIn(BaseModel):
    step_ids: List[int] = Field(min_length=1)


class EscalationLevelUpsertIn(BaseModel):
    contact_ids: List[int] = Field(min_length=1, max_length=20)


class EscalationLevelReorderIn(BaseModel):
    ordered_levels: List[int] = Field(min_length=1)


class ArrivalKeywordCreateIn(BaseModel):
    phrase: str = Field(min_length=1, max_length=512)
    trigger_type: str = Field(min_length=1, max_length=32)
    is_active: bool = True
    sort_order: int = 0


class ArrivalKeywordPatchIn(BaseModel):
    phrase: Optional[str] = Field(None, min_length=1, max_length=512)
    trigger_type: Optional[str] = Field(None, min_length=1, max_length=32)
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


class TriggerPreviewIn(BaseModel):
    message: str = Field(min_length=1, max_length=2048)


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


def _validate_location_mode(mode: str) -> str:
    allowed = {"location_only", "location_plus_reception", "location_plus_instructions"}
    key = (mode or "").strip()
    if key not in allowed:
        raise HTTPException(status_code=422, detail="invalid_location_response_mode")
    return key


def _validate_arrival_mode(mode: str) -> str:
    allowed = {"reception_only", "location_and_reception", "ask_branch_first"}
    key = (mode or "").strip()
    if key not in allowed:
        raise HTTPException(status_code=422, detail="invalid_arrival_response_mode")
    return key


def _validate_trigger_type(trigger_type: str) -> str:
    from modules.operations.branch_arrival_keyword_evidence import VALID_TRIGGER_TYPES  # noqa: PLC0415

    key = (trigger_type or "").strip()
    if key not in VALID_TRIGGER_TYPES:
        raise HTTPException(status_code=422, detail="invalid_trigger_type")
    return key


def _serialize_arrival_keyword(row: BranchArrivalKeyword) -> Dict[str, Any]:
    return {
        "id": row.id,
        "branch_id": row.branch_id,
        "phrase": row.phrase,
        "trigger_type": row.trigger_type,
        "is_active": bool(row.is_active),
        "sort_order": int(row.sort_order or 0),
    }


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
        "location_response_mode": getattr(row, "location_response_mode", "") or "location_only",
        "arrival_response_mode": getattr(row, "arrival_response_mode", "") or "reception_only",
        "location_instructions_text": getattr(row, "location_instructions_text", "") or "",
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
        "contact_id": int(row.contact_id) if row.contact_id else None,
        "display_name": row.display_name,
        "role": row.role or "",
        "phone_e164": row.phone_e164,
        "is_active": bool(row.is_active),
        "sort_order": int(row.sort_order or 0),
    }


def _load_escalation_rows(db: Session, branch_id: int) -> List[BranchEscalationStep]:
    return (
        db.query(BranchEscalationStep)
        .filter(BranchEscalationStep.branch_id == branch_id)
        .order_by(
            BranchEscalationStep.escalation_level.asc(),
            BranchEscalationStep.sort_order.asc(),
            BranchEscalationStep.id.asc(),
        )
        .all()
    )


def _group_escalation_levels(
    rows: List[BranchEscalationStep],
) -> Dict[int, List[BranchEscalationStep]]:
    grouped: Dict[int, List[BranchEscalationStep]] = {}
    for row in rows:
        level = int(row.escalation_level or 1)
        grouped.setdefault(level, []).append(row)
    return grouped


def _resolve_contacts_for_level(
    db: Session,
    branch_id: int,
    contact_ids: List[int],
) -> List[BranchContact]:
    unique_ids = list(dict.fromkeys(int(cid) for cid in contact_ids))
    if not unique_ids:
        raise HTTPException(status_code=422, detail="contact_ids_required")
    rows = (
        db.query(BranchContact)
        .filter(
            BranchContact.branch_id == branch_id,
            BranchContact.id.in_(unique_ids),
            BranchContact.is_active.is_(True),
        )
        .all()
    )
    by_id = {int(r.id): r for r in rows}
    if set(by_id.keys()) != set(unique_ids):
        raise HTTPException(status_code=422, detail="invalid_contact_ids")
    return [by_id[cid] for cid in unique_ids]


def _apply_contact_to_step(
    step: BranchEscalationStep,
    contact: BranchContact,
    *,
    sort_order: int,
) -> None:
    step.contact_id = contact.id
    step.display_name = contact.display_name
    step.role = contact.role
    step.phone_e164 = contact.phone_e164
    step.sort_order = sort_order
    step.is_active = True


def _replace_level_contacts(
    db: Session,
    branch_id: int,
    level: int,
    contacts: List[BranchContact],
) -> None:
    (
        db.query(BranchEscalationStep)
        .filter(
            BranchEscalationStep.branch_id == branch_id,
            BranchEscalationStep.escalation_level == level,
        )
        .delete(synchronize_session=False)
    )
    for idx, contact in enumerate(contacts):
        row = BranchEscalationStep(
            branch_id=branch_id,
            escalation_level=level,
        )
        _apply_contact_to_step(row, contact, sort_order=idx)
        db.add(row)


def _next_escalation_level(db: Session, branch_id: int) -> int:
    current = (
        db.query(func.max(BranchEscalationStep.escalation_level))
        .filter(BranchEscalationStep.branch_id == branch_id)
        .scalar()
    )
    return int(current or 0) + 1


def _renumber_escalation_levels(db: Session, branch_id: int) -> None:
    rows = _load_escalation_rows(db, branch_id)
    grouped = _group_escalation_levels(rows)
    for new_level, old_level in enumerate(sorted(grouped.keys()), start=1):
        for step in grouped[old_level]:
            step.escalation_level = new_level


def _serialize_escalation_levels(
    db: Session,
    branch_id: int,
    rows: List[BranchEscalationStep],
) -> List[Dict[str, Any]]:
    grouped = _group_escalation_levels(rows)
    contact_ids = {
        int(r.contact_id)
        for r in rows
        if getattr(r, "contact_id", None)
    }
    contacts_by_id: Dict[int, BranchContact] = {}
    if contact_ids:
        for contact in (
            db.query(BranchContact)
            .filter(
                BranchContact.branch_id == branch_id,
                BranchContact.id.in_(contact_ids),
            )
            .all()
        ):
            contacts_by_id[int(contact.id)] = contact

    levels: List[Dict[str, Any]] = []
    for level_num in sorted(grouped.keys()):
        steps = grouped[level_num]
        contacts_payload: List[Dict[str, Any]] = []
        ids: List[int] = []
        for step in steps:
            contact = contacts_by_id.get(int(step.contact_id or 0))
            if contact is not None:
                contacts_payload.append(_serialize_contact(contact))
                ids.append(int(contact.id))
            else:
                contacts_payload.append({
                    "id": step.contact_id or 0,
                    "branch_id": branch_id,
                    "display_name": step.display_name,
                    "role": step.role or "",
                    "phone_e164": step.phone_e164,
                    "whatsapp_e164": "",
                    "is_active": bool(step.is_active),
                    "is_default_reception": False,
                    "sort_order": int(step.sort_order or 0),
                })
                if step.contact_id:
                    ids.append(int(step.contact_id))
        levels.append({
            "escalation_level": level_num,
            "contact_ids": ids,
            "contacts": contacts_payload,
        })
    return levels


def _sync_escalation_steps_for_contact(db: Session, contact: BranchContact) -> None:
    if not contact.id:
        return
    rows = (
        db.query(BranchEscalationStep)
        .filter(BranchEscalationStep.contact_id == contact.id)
        .all()
    )
    for row in rows:
        row.display_name = contact.display_name
        row.role = contact.role
        row.phone_e164 = contact.phone_e164


def _ensure_contact_not_in_escalation(
    db: Session,
    branch_id: int,
    contact_id: int,
) -> None:
    used = (
        db.query(func.count(BranchEscalationStep.id))
        .filter(
            BranchEscalationStep.branch_id == branch_id,
            BranchEscalationStep.contact_id == contact_id,
        )
        .scalar()
        or 0
    )
    if used:
        raise HTTPException(status_code=422, detail="contact_used_in_escalation")


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
        location_response_mode=_validate_location_mode(body.location_response_mode),
        arrival_response_mode=_validate_arrival_mode(body.arrival_response_mode),
        location_instructions_text=(body.location_instructions_text or "").strip() or None,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    from modules.operations.branch_arrival_keyword_evidence import (  # noqa: PLC0415
        seed_default_keywords_for_branch,
    )
    seed_default_keywords_for_branch(db, int(row.id))
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
        if key == "location_response_mode" and val is not None:
            val = _validate_location_mode(str(val))
        if key == "arrival_response_mode" and val is not None:
            val = _validate_arrival_mode(str(val))
        if key == "location_instructions_text" and val is not None:
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
):
    tenant_id = resolve_tenant_id(request)
    row = _get_branch(db, tenant_id, branch_id)
    db.delete(row)
    db.commit()
    return None


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
    _sync_escalation_steps_for_contact(db, row)
    db.commit()
    return _serialize_contact(row)


@router.delete("/branches/{branch_id}/contacts/{contact_id}", status_code=204)
async def delete_contact(
    branch_id: int,
    contact_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    row = _get_contact(db, tenant_id, branch_id, contact_id)
    _ensure_contact_not_in_escalation(db, branch_id, contact_id)
    db.delete(row)
    db.commit()
    return None


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
):
    tenant_id = resolve_tenant_id(request)
    row = _get_escalation_step(db, tenant_id, branch_id, step_id)
    db.delete(row)
    db.commit()
    return None


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


# ── Escalation levels (contact-linked) ───────────────────────────────────────


@router.get("/branches/{branch_id}/escalation-levels")
async def list_escalation_levels(
    branch_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    _get_branch(db, tenant_id, branch_id)
    rows = _load_escalation_rows(db, branch_id)
    return {"levels": _serialize_escalation_levels(db, branch_id, rows)}


@router.post("/branches/{branch_id}/escalation-levels", status_code=201)
async def create_escalation_level(
    branch_id: int,
    body: EscalationLevelUpsertIn,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    _get_branch(db, tenant_id, branch_id)
    contacts = _resolve_contacts_for_level(db, branch_id, body.contact_ids)
    level = _next_escalation_level(db, branch_id)
    _replace_level_contacts(db, branch_id, level, contacts)
    db.commit()
    rows = _load_escalation_rows(db, branch_id)
    levels = _serialize_escalation_levels(db, branch_id, rows)
    created = next(l for l in levels if l["escalation_level"] == level)
    return created


@router.put("/branches/{branch_id}/escalation-levels/{level}")
async def update_escalation_level(
    branch_id: int,
    level: int,
    body: EscalationLevelUpsertIn,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    _get_branch(db, tenant_id, branch_id)
    rows = _load_escalation_rows(db, branch_id)
    grouped = _group_escalation_levels(rows)
    if level not in grouped:
        raise HTTPException(status_code=404, detail="escalation_level_not_found")
    contacts = _resolve_contacts_for_level(db, branch_id, body.contact_ids)
    _replace_level_contacts(db, branch_id, level, contacts)
    db.commit()
    rows = _load_escalation_rows(db, branch_id)
    levels = _serialize_escalation_levels(db, branch_id, rows)
    return next(l for l in levels if l["escalation_level"] == level)


@router.delete("/branches/{branch_id}/escalation-levels/{level}", status_code=204)
async def delete_escalation_level(
    branch_id: int,
    level: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    _get_branch(db, tenant_id, branch_id)
    rows = _load_escalation_rows(db, branch_id)
    grouped = _group_escalation_levels(rows)
    if level not in grouped:
        raise HTTPException(status_code=404, detail="escalation_level_not_found")
    (
        db.query(BranchEscalationStep)
        .filter(
            BranchEscalationStep.branch_id == branch_id,
            BranchEscalationStep.escalation_level == level,
        )
        .delete(synchronize_session=False)
    )
    db.flush()
    _renumber_escalation_levels(db, branch_id)
    db.commit()
    return None


@router.post("/branches/{branch_id}/escalation-levels/reorder")
async def reorder_escalation_levels(
    branch_id: int,
    body: EscalationLevelReorderIn,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    _get_branch(db, tenant_id, branch_id)
    rows = _load_escalation_rows(db, branch_id)
    grouped = _group_escalation_levels(rows)
    current_levels = sorted(grouped.keys())
    if set(body.ordered_levels) != set(current_levels):
        raise HTTPException(status_code=422, detail="reorder_levels_mismatch")
    mapping = {
        old_level: idx + 1
        for idx, old_level in enumerate(body.ordered_levels)
    }
    for step in rows:
        step.escalation_level = mapping[int(step.escalation_level)]
    db.commit()
    rows = _load_escalation_rows(db, branch_id)
    return {"levels": _serialize_escalation_levels(db, branch_id, rows)}


@router.get("/branches/{branch_id}/arrival-keywords")
async def list_arrival_keywords(
    branch_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    _get_branch(db, tenant_id, branch_id)
    rows = (
        db.query(BranchArrivalKeyword)
        .filter(BranchArrivalKeyword.branch_id == branch_id)
        .order_by(
            BranchArrivalKeyword.sort_order.asc(),
            BranchArrivalKeyword.id.asc(),
        )
        .all()
    )
    return {"keywords": [_serialize_arrival_keyword(r) for r in rows]}


@router.post("/branches/{branch_id}/arrival-keywords", status_code=201)
async def create_arrival_keyword(
    branch_id: int,
    body: ArrivalKeywordCreateIn,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    _get_branch(db, tenant_id, branch_id)
    now = datetime.now(timezone.utc)
    row = BranchArrivalKeyword(
        branch_id=branch_id,
        phrase=body.phrase.strip(),
        trigger_type=_validate_trigger_type(body.trigger_type),
        is_active=body.is_active,
        sort_order=body.sort_order,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_arrival_keyword(row)


@router.patch("/branches/{branch_id}/arrival-keywords/{keyword_id}")
async def update_arrival_keyword(
    branch_id: int,
    keyword_id: int,
    body: ArrivalKeywordPatchIn,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    _get_branch(db, tenant_id, branch_id)
    row = (
        db.query(BranchArrivalKeyword)
        .filter(
            BranchArrivalKeyword.branch_id == branch_id,
            BranchArrivalKeyword.id == keyword_id,
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="arrival_keyword_not_found")
    data = body.model_dump(exclude_unset=True)
    for key, val in data.items():
        if key == "phrase" and val is not None:
            val = str(val).strip()
        if key == "trigger_type" and val is not None:
            val = _validate_trigger_type(str(val))
        setattr(row, key, val)
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return _serialize_arrival_keyword(row)


@router.delete("/branches/{branch_id}/arrival-keywords/{keyword_id}", status_code=204)
async def delete_arrival_keyword(
    branch_id: int,
    keyword_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    _get_branch(db, tenant_id, branch_id)
    row = (
        db.query(BranchArrivalKeyword)
        .filter(
            BranchArrivalKeyword.branch_id == branch_id,
            BranchArrivalKeyword.id == keyword_id,
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="arrival_keyword_not_found")
    db.delete(row)
    db.commit()
    return None


@router.post("/branches/{branch_id}/arrival-keywords/seed-defaults")
async def seed_arrival_keywords(
    branch_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    _get_branch(db, tenant_id, branch_id)
    from modules.operations.branch_arrival_keyword_evidence import (  # noqa: PLC0415
        seed_default_keywords_for_branch,
    )
    count = seed_default_keywords_for_branch(db, branch_id)
    db.commit()
    rows = (
        db.query(BranchArrivalKeyword)
        .filter(BranchArrivalKeyword.branch_id == branch_id)
        .order_by(
            BranchArrivalKeyword.sort_order.asc(),
            BranchArrivalKeyword.id.asc(),
        )
        .all()
    )
    return {"seeded": count, "keywords": [_serialize_arrival_keyword(r) for r in rows]}


@router.post("/branches/{branch_id}/preview-trigger")
async def preview_branch_trigger(
    branch_id: int,
    body: TriggerPreviewIn,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    tenant_id = resolve_tenant_id(request)
    _get_branch(db, tenant_id, branch_id)
    from modules.operations.branch_arrival_keyword_evidence import (  # noqa: PLC0415
        preview_trigger_actions,
    )
    return preview_trigger_actions(db, tenant_id, branch_id, body.message.strip())
