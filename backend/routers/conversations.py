"""
routers/conversations.py
────────────────────────
Tenant-scoped conversation list/detail endpoints for the merchant dashboard.

Backed by `Conversation`, `MessageEvent`, `ConversationTrace`, and `ConversationLog`
where available. This is intentionally lightweight but real — no fake data.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.wa_usage import has_open_service_window
from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id
from models import Conversation, ConversationLog, ConversationTrace, Customer, HandoffSession, MessageEvent, WhatsAppConnection
from services.customer_intelligence import CustomerIntelligenceService, normalize_phone

router = APIRouter(prefix="/conversations", tags=["Conversations"])


class ReplyIn(BaseModel):
    customer_phone: str
    message: str


class HandoffIn(BaseModel):
    customer_phone: str
    customer_name: str = ""
    last_message: str = ""
    reason: str = "manual_takeover"


class CloseIn(BaseModel):
    customer_phone: str


def _get_or_create_customer(db: Session, tenant_id: int, customer_phone: str, customer_name: str = "") -> Customer:
    """
    Create or retrieve a customer via the single unified identity path.

    Historical note: this function used to fall back to a raw ``Customer(...)``
    insert when ``upsert_customer_identity`` returned ``None``. That was the
    source of duplicate-customer rows (un-normalised phone matching against
    normalised rows). Removed 2026-04-16. If ``upsert_customer_identity``
    cannot produce a customer from the given inputs, we raise instead of
    silently corrupting the data set.
    """
    from core.obs import EVENTS, log_event  # noqa: PLC0415

    service = CustomerIntelligenceService(db, tenant_id)
    normalized_phone = normalize_phone(customer_phone) or customer_phone
    customer = service.upsert_customer_identity(
        phone=normalized_phone,
        name=customer_name or normalized_phone,
        source="whatsapp_inbound",
        extra_metadata={"source": "whatsapp_inbound"},
        seen_at=datetime.now(timezone.utc),
    )
    if customer is None:
        log_event(
            EVENTS.CUSTOMER_UPSERT_FAILED,
            tenant_id=tenant_id,
            source="whatsapp_inbound",
            phone_raw=customer_phone,
            phone_normalized=normalized_phone,
            name=customer_name,
        )
        raise HTTPException(
            status_code=400,
            detail="Cannot resolve customer: phone, name, email or external_id is required.",
        )
    return customer


def _get_or_create_conversation(
    db: Session,
    tenant_id: int,
    customer_phone: str,
    customer_name: str = "",
) -> Conversation:
    customer = _get_or_create_customer(db, tenant_id, customer_phone, customer_name)
    convo = db.query(Conversation).filter(
        Conversation.tenant_id == tenant_id,
        Conversation.customer_id == customer.id,
    ).first()
    if not convo:
        convo = Conversation(
            tenant_id=tenant_id,
            customer_id=customer.id,
            status="active",
            is_human_handoff=False,
            paused_by_human=False,
            extra_metadata={"customer_phone": customer_phone},
        )
        db.add(convo)
        db.flush()
    else:
        meta = dict(convo.extra_metadata or {})
        meta["customer_phone"] = customer_phone
        meta["phone"] = customer_phone
        convo.extra_metadata = meta
    if not convo.extra_metadata:
        convo.extra_metadata = {"customer_phone": customer_phone, "phone": customer_phone}
    return convo


def _resolve_customer_phone(convo: Conversation) -> str:
    if convo.customer and convo.customer.phone:
        return str(convo.customer.phone)
    meta = convo.extra_metadata or {}
    return str(meta.get("customer_phone") or meta.get("phone") or "")


@router.get("")
async def list_conversations(request: Request, db: Session = Depends(get_db), limit: int = 100):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    trace_rows = (
        db.query(ConversationTrace)
        .filter(ConversationTrace.tenant_id == tenant_id)
        .order_by(ConversationTrace.created_at.desc())
        .limit(limit * 5)
        .all()
    )

    grouped: Dict[str, List[ConversationTrace]] = defaultdict(list)
    for row in trace_rows:
        grouped[row.customer_phone].append(row)

    convo_rows = (
        db.query(Conversation)
        .filter(Conversation.tenant_id == tenant_id)
        .all()
    )
    convo_map: Dict[str, Conversation] = {}
    for convo in convo_rows:
        phone = _resolve_customer_phone(convo)
        if phone:
            convo_map[phone] = convo

    active_handoffs = {
        row.customer_phone
        for row in db.query(HandoffSession).filter(
            HandoffSession.tenant_id == tenant_id,
            HandoffSession.status == "active",
        ).all()
    }

    conversations: List[Dict[str, Any]] = []
    for phone, rows in grouped.items():
        latest = rows[0]
        convo = convo_map.get(phone)
        if phone in active_handoffs or (convo and convo.is_human_handoff):
            status = "human"
        elif convo and str(convo.status).lower() == "closed":
            status = "closed"
        else:
            status = "active"
        conversations.append({
            "id": latest.session_id or f"trace-{phone}",
            "customer": (convo.customer.name if convo and convo.customer and convo.customer.name else phone),
            "phone": phone,
            "lastMsg": latest.message or "",
            "time": latest.created_at.isoformat() if latest.created_at else "",
            "isAI": status != "human",
            "status": status,
            "unread": 0,
        })

    for phone, convo in convo_map.items():
        if phone in grouped:
            continue
        status = "human" if (phone in active_handoffs or convo.is_human_handoff) else ("closed" if str(convo.status).lower() == "closed" else "active")
        conversations.append({
            "id": str(convo.id),
            "customer": (convo.customer.name if convo.customer and convo.customer.name else phone),
            "phone": phone,
            "lastMsg": "",
            "time": "",
            "isAI": status != "human",
            "status": status,
            "unread": 0,
        })

    conversations.sort(key=lambda c: c["time"], reverse=True)
    return {"conversations": conversations[:limit]}


@router.get("/messages/{customer_phone}")
async def get_conversation_messages(customer_phone: str, request: Request, db: Session = Depends(get_db), limit: int = 100):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    rows = (
        db.query(MessageEvent)
        .filter(MessageEvent.tenant_id == tenant_id)
        .order_by(MessageEvent.created_at.asc())
        .limit(limit)
        .all()
    )

    filtered = []
    for row in rows:
        meta = row.extra_metadata or {}
        row_phone = meta.get("phone") or meta.get("customer_phone")
        if row_phone == customer_phone:
            filtered.append(row)

    if not filtered:
        trace_rows = (
            db.query(ConversationTrace)
            .filter(
                ConversationTrace.tenant_id == tenant_id,
                ConversationTrace.customer_phone == customer_phone,
            )
            .order_by(ConversationTrace.created_at.asc())
            .limit(limit)
            .all()
        )
        messages: List[Dict[str, Any]] = []
        for idx, row in enumerate(trace_rows):
            messages.append({
                "id": f"in-{idx}",
                "direction": "in",
                "body": row.message or "",
                "time": row.created_at.isoformat() if row.created_at else "",
                "isAI": False,
            })
            if row.response_text:
                messages.append({
                    "id": f"out-{idx}",
                    "direction": "out",
                    "body": row.response_text,
                    "time": row.created_at.isoformat() if row.created_at else "",
                    "isAI": bool(row.orchestrator_used),
                })
        return {"messages": messages}

    return {
        "messages": [
            {
                "id": str(row.id),
                "direction": "out" if (row.direction or "").lower() == "outbound" else "in",
                "body": row.body or "",
                "time": row.created_at.isoformat() if row.created_at else "",
                "isAI": bool((row.extra_metadata or {}).get("is_ai")),
            }
            for row in filtered
        ]
    }


@router.post("/reply")
async def reply_to_conversation(body: ReplyIn, request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    customer_phone = normalize_phone(body.customer_phone) or body.customer_phone

    wa_conn = db.query(WhatsAppConnection).filter(
        WhatsAppConnection.tenant_id == tenant_id,
        WhatsAppConnection.status == "connected",
        WhatsAppConnection.sending_enabled == True,  # noqa: E712
    ).first()
    if not wa_conn or not wa_conn.phone_number_id:
        raise HTTPException(status_code=409, detail="WhatsApp is not connected for this tenant")

    if not has_open_service_window(db, tenant_id, customer_phone):
        raise HTTPException(
            status_code=409,
            detail=(
                "لا يمكن إرسال رسالة نصية حرة خارج نافذة خدمة واتساب (24 ساعة). "
                "استخدم قالبًا معتمدًا من Meta أولاً أو انتظر رد العميل."
            ),
        )

    convo = _get_or_create_conversation(db, tenant_id, customer_phone)

    from routers.whatsapp_webhook import _send_whatsapp_message  # noqa: PLC0415
    await _send_whatsapp_message(
        phone_id=wa_conn.phone_number_id,
        to=customer_phone,
        text=body.message,
        _tenant_id=tenant_id,
        _db=db,
    )

    db.add(MessageEvent(
        conversation_id=convo.id,
        tenant_id=tenant_id,
        direction="outbound",
        body=body.message,
        event_type="manual_reply",
        extra_metadata={"customer_phone": customer_phone, "is_ai": False},
    ))
    convo.status = "active"
    convo.is_human_handoff = False
    convo.paused_by_human = False
    db.add(convo)
    db.commit()
    return {"sent": True}


@router.post("/handoff")
async def handoff_conversation(body: HandoffIn, request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    from handoff.manager import create_handoff_session  # noqa: PLC0415

    convo = _get_or_create_conversation(db, tenant_id, body.customer_phone, body.customer_name)
    session = create_handoff_session(
        db,
        tenant_id=tenant_id,
        customer_phone=body.customer_phone,
        customer_name=body.customer_name or body.customer_phone,
        last_message=body.last_message or "",
        reason=body.reason,
    )
    convo.status = "human"
    convo.is_human_handoff = True
    convo.paused_by_human = True
    db.add(convo)
    db.commit()
    return {"handoff": True, "session_id": session.id}


@router.post("/close")
async def close_conversation(body: CloseIn, request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    convo = _get_or_create_conversation(db, tenant_id, body.customer_phone)
    active_session = db.query(HandoffSession).filter(
        HandoffSession.tenant_id == tenant_id,
        HandoffSession.customer_phone == body.customer_phone,
        HandoffSession.status == "active",
    ).first()
    if active_session:
        from handoff.manager import resolve_handoff_session  # noqa: PLC0415
        resolve_handoff_session(db, active_session.id, tenant_id, resolved_by="dashboard_close")

    convo.status = "closed"
    convo.is_human_handoff = False
    convo.paused_by_human = False
    db.add(convo)
    db.commit()
    return {"closed": True}
