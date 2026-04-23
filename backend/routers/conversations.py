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

import logging as _logging  # noqa: E402
_log = _logging.getLogger("nahla-backend")


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
    resolved_name = customer_name
    if not resolved_name:
        existing = db.query(Customer).filter(
            Customer.tenant_id == tenant_id,
            (Customer.phone == customer_phone)
            | (Customer.phone == normalized_phone)
            | (Customer.normalized_phone == normalized_phone),
        ).first()
        resolved_name = (existing.name if existing and existing.name else "") or normalized_phone
    customer = service.upsert_customer_identity(
        phone=normalized_phone,
        name=resolved_name,
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
        try:
            from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
            flag_modified(convo, "extra_metadata")
        except Exception:
            pass
    if not convo.extra_metadata:
        convo.extra_metadata = {"customer_phone": customer_phone, "phone": customer_phone}
    return convo


def _resolve_customer_phone(convo: Conversation) -> str:
    if convo.customer and convo.customer.phone:
        return str(convo.customer.phone)
    meta = convo.extra_metadata or {}
    return str(meta.get("customer_phone") or meta.get("phone") or "")


def record_outbound_message(
    db: Session,
    tenant_id: int,
    phone: str,
    body: str,
    event_type: str = "system",
    customer_name: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Record an outbound message so it appears in the conversations inbox.

    Safe to call from any context (campaigns, automations, COD, AI
    fallback, etc.).  Uses a SAVEPOINT so failures never corrupt the
    caller's transaction.

    When *customer_name* is empty the existing customer name is kept
    intact — we never overwrite a real name with a phone number.
    """
    try:
        db.begin_nested()
        norm_phone = normalize_phone(phone) or phone
        existing_customer = (
            db.query(Customer)
            .filter(Customer.tenant_id == tenant_id)
            .filter(
                (Customer.phone == phone)
                | (Customer.phone == norm_phone)
                | (Customer.normalized_phone == norm_phone)
            )
            .first()
        )
        safe_name = customer_name or (existing_customer.name if existing_customer else "") or ""
        convo = _get_or_create_conversation(db, tenant_id, phone, safe_name)
        meta = {
            "customer_phone": phone,
            "phone": phone,
            "is_ai": False,
        }
        if extra:
            meta.update(extra)
        db.add(MessageEvent(
            conversation_id=convo.id,
            tenant_id=tenant_id,
            direction="outbound",
            body=body,
            event_type=event_type,
            extra_metadata=meta,
        ))
        db.flush()
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        _log.warning("[record_outbound_message] %s tenant=%s: %s", phone, tenant_id, exc)


@router.get("")
async def list_conversations(request: Request, db: Session = Depends(get_db), limit: int = 100):
    """
    Build the conversation list from **all** sources:
    1. ``Conversation`` records (canonical)
    2. Latest ``MessageEvent`` per conversation (actual last message)
    3. ``ConversationTrace`` fallback for phones without MessageEvent
    """
    from sqlalchemy import func  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    def _norm(p: str) -> str:
        return (p or "").strip().replace("+", "").replace("-", "").replace(" ", "")

    active_handoff_norms: set[str] = {
        _norm(row.customer_phone)
        for row in db.query(HandoffSession).filter(
            HandoffSession.tenant_id == tenant_id,
            HandoffSession.status == "active",
        ).all()
    }

    def _status_for(phone: str, convo: Optional[Conversation]) -> str:
        if _norm(phone) in active_handoff_norms or (convo and convo.is_human_handoff):
            return "human"
        if convo and str(convo.status).lower() == "closed":
            return "closed"
        return "active"

    # ── 1. All Conversation records → phone_info map ─────────────────────────
    convo_rows = (
        db.query(Conversation)
        .filter(Conversation.tenant_id == tenant_id)
        .all()
    )
    phone_info: Dict[str, Dict[str, Any]] = {}
    norm_to_key: Dict[str, str] = {}
    conv_id_to_phone: Dict[int, str] = {}
    for convo in convo_rows:
        phone = _resolve_customer_phone(convo)
        if not phone:
            continue
        n = _norm(phone)
        status = _status_for(phone, convo)
        raw_name = convo.customer.name if convo.customer and convo.customer.name else ""
        name_looks_like_phone = raw_name and raw_name.replace("+", "").replace("-", "").replace(" ", "").isdigit()
        name = "" if name_looks_like_phone else raw_name

        existing_key = norm_to_key.get(n)
        if existing_key and existing_key in phone_info:
            prev = phone_info[existing_key]
            prev_has_name = prev["customer"] and prev["customer"] != prev["phone"]
            if name and not prev_has_name:
                phone_info.pop(existing_key)
            elif prev_has_name:
                conv_id_to_phone[convo.id] = existing_key
                continue
            else:
                conv_id_to_phone[convo.id] = existing_key
                continue

        display_name = name or phone
        phone_info[phone] = {
            "id": str(convo.id),
            "customer": display_name,
            "phone": phone,
            "lastMsg": "",
            "time": "",
            "isAI": status != "human",
            "status": status,
            "unread": 0,
            "lastMsgType": "",
            "_conv_id": convo.id,
        }
        norm_to_key[n] = phone
        conv_id_to_phone[convo.id] = phone

    # ── 2. Latest MessageEvent per conversation_id (single query) ────────────
    conv_ids = list(conv_id_to_phone.keys())
    if conv_ids:
        latest_sq = (
            db.query(
                MessageEvent.conversation_id,
                func.max(MessageEvent.id).label("max_id"),
            )
            .filter(
                MessageEvent.tenant_id == tenant_id,
                MessageEvent.conversation_id.in_(conv_ids),
            )
            .group_by(MessageEvent.conversation_id)
            .subquery()
        )
        latest_msgs = (
            db.query(MessageEvent)
            .join(latest_sq, MessageEvent.id == latest_sq.c.max_id)
            .all()
        )
        def _last_msg_hint(msg) -> str:
            et = (msg.event_type or "").lower()
            meta = msg.extra_metadata or {}
            d = (msg.direction or "").lower()
            if d != "outbound":
                return "customer"
            if et == "campaign":
                return "campaign"
            if et in ("ai_reply", "ai_fallback", "whatsapp") or meta.get("is_ai"):
                return "ai"
            if et in ("automation", "cart_recovery"):
                return "automation"
            if et == "cod_confirmation":
                return "cod"
            if et == "manual_reply":
                return "manual"
            return "system"

        for msg in latest_msgs:
            phone = conv_id_to_phone.get(msg.conversation_id)
            if phone and phone in phone_info:
                phone_info[phone]["lastMsg"] = msg.body or ""
                phone_info[phone]["time"] = msg.created_at.isoformat() if msg.created_at else ""
                phone_info[phone]["lastMsgType"] = _last_msg_hint(msg)

    # ── 3. Unread count per conversation (inbound after last outbound) ───────
    if conv_ids:
        last_out_sq = (
            db.query(
                MessageEvent.conversation_id,
                func.max(MessageEvent.created_at).label("last_out"),
            )
            .filter(
                MessageEvent.tenant_id == tenant_id,
                MessageEvent.conversation_id.in_(conv_ids),
                MessageEvent.direction == "outbound",
            )
            .group_by(MessageEvent.conversation_id)
            .subquery()
        )
        unread_rows = (
            db.query(
                MessageEvent.conversation_id,
                func.count(MessageEvent.id).label("cnt"),
            )
            .outerjoin(last_out_sq, MessageEvent.conversation_id == last_out_sq.c.conversation_id)
            .filter(
                MessageEvent.tenant_id == tenant_id,
                MessageEvent.conversation_id.in_(conv_ids),
                MessageEvent.direction != "outbound",
                (MessageEvent.created_at > last_out_sq.c.last_out) | (last_out_sq.c.last_out.is_(None)),
            )
            .group_by(MessageEvent.conversation_id)
            .all()
        )
        for cid, cnt in unread_rows:
            phone = conv_id_to_phone.get(cid)
            if phone and phone in phone_info:
                phone_info[phone]["unread"] = cnt

    # ── 4. ConversationTrace fallback for gaps ───────────────────────────────
    trace_rows = (
        db.query(ConversationTrace)
        .filter(ConversationTrace.tenant_id == tenant_id)
        .order_by(ConversationTrace.created_at.desc())
        .limit(limit * 5)
        .all()
    )
    for row in trace_rows:
        phone = row.customer_phone
        key = norm_to_key.get(_norm(phone)) or (phone if phone in phone_info else None)
        if key and key in phone_info:
            if not phone_info[key]["lastMsg"] and not phone_info[key]["time"]:
                phone_info[key]["lastMsg"] = row.message or ""
                phone_info[key]["time"] = row.created_at.isoformat() if row.created_at else ""
        elif _norm(phone) not in norm_to_key:
            norm_to_key[_norm(phone)] = phone
            trace_customer = db.query(Customer).filter(
                Customer.tenant_id == tenant_id,
                (Customer.phone == phone) | (Customer.normalized_phone == (_norm(phone) if _norm(phone) else phone)),
            ).first()
            trace_name = (trace_customer.name if trace_customer and trace_customer.name else phone)
            phone_info[phone] = {
                "id": row.session_id or f"trace-{phone}",
                "customer": trace_name,
                "phone": phone,
                "lastMsg": row.message or "",
                "time": row.created_at.isoformat() if row.created_at else "",
                "isAI": True,
                "status": _status_for(phone, None),
                "unread": 0,
                "lastMsgType": "ai",
                "_conv_id": None,
            }

    # ── 5. Enrich phone-like names from Customer table ──────────────────────
    for key, info in phone_info.items():
        cname = info.get("customer", "")
        if not cname or cname.replace("+", "").replace("-", "").replace(" ", "").isdigit():
            real = db.query(Customer).filter(
                Customer.tenant_id == tenant_id,
                (Customer.phone == key)
                | (Customer.normalized_phone == _norm(key)),
            ).first()
            if real and real.name and not real.name.replace("+", "").replace("-", "").replace(" ", "").isdigit():
                info["customer"] = real.name

    # ── 6. Build result, strip internal keys ─────────────────────────────────
    result = sorted(phone_info.values(), key=lambda c: c.get("time") or "", reverse=True)
    for c in result:
        c.pop("_conv_id", None)
    return {"conversations": result[:limit]}


@router.get("/messages/{customer_phone}")
async def get_conversation_messages(customer_phone: str, request: Request, db: Session = Depends(get_db), limit: int = 100):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    from sqlalchemy import or_  # noqa: PLC0415
    from core.conversation_engine import PLATFORM_TENANT_ID  # noqa: PLC0415

    normalized = normalize_phone(customer_phone) or customer_phone
    phone_variants = {customer_phone, normalized}
    if normalized.startswith("966"):
        phone_variants.add("+" + normalized)
    stripped = normalized.lstrip("+")
    phone_variants.add(stripped)
    phone_variants.discard("")

    tenant_ids = list({tenant_id, PLATFORM_TENANT_ID})

    phone_filters = []
    for variant in phone_variants:
        phone_filters.append(MessageEvent.extra_metadata["phone"].astext == variant)
        phone_filters.append(MessageEvent.extra_metadata["customer_phone"].astext == variant)

    conv_ids = [
        c.id for c in db.query(Conversation.id).filter(
            Conversation.tenant_id.in_(tenant_ids),
            Conversation.customer_id.in_(
                db.query(Customer.id).filter(
                    Customer.tenant_id.in_(tenant_ids),
                    or_(
                        Customer.phone.in_(list(phone_variants)),
                        Customer.normalized_phone.in_(list(phone_variants)),
                    ),
                )
            ),
        ).all()
    ]

    me_filter = or_(*phone_filters) if phone_filters else False
    if conv_ids:
        me_filter = or_(me_filter, MessageEvent.conversation_id.in_(conv_ids))

    me_rows = (
        db.query(MessageEvent)
        .filter(
            MessageEvent.tenant_id.in_(tenant_ids),
            me_filter,
        )
        .order_by(MessageEvent.created_at.desc())
        .limit(limit)
        .all()
    )

    def _event_type_label(r) -> str:
        et = (r.event_type or "").lower()
        meta = r.extra_metadata or {}
        direction = (r.direction or "").lower()
        if direction != "outbound":
            return "customer"
        if et == "campaign":
            return "campaign"
        if et in ("ai_reply", "ai_fallback") or meta.get("is_ai"):
            return "ai"
        if et in ("automation", "cart_recovery"):
            return "automation"
        if et == "cod_confirmation":
            return "cod"
        if et == "manual_reply":
            return "manual"
        if et == "whatsapp":
            return "ai"
        return "system"

    messages: List[Dict[str, Any]] = [
        {
            "id": str(r.id),
            "direction": "out" if (r.direction or "").lower() == "outbound" else "in",
            "body": r.body or "",
            "time": r.created_at.isoformat() if r.created_at else "",
            "isAI": bool((r.extra_metadata or {}).get("is_ai")),
            "eventType": _event_type_label(r),
            "_ts": r.created_at,
        }
        for r in me_rows
    ]

    me_times = {r.created_at for r in me_rows if r.created_at}

    trace_rows = (
        db.query(ConversationTrace)
        .filter(
            ConversationTrace.tenant_id.in_(tenant_ids),
            ConversationTrace.customer_phone.in_(list(phone_variants)),
        )
        .order_by(ConversationTrace.created_at.desc())
        .limit(limit)
        .all()
    )

    def _near(ts):
        if not ts:
            return False
        for met in me_times:
            if met and abs((ts - met).total_seconds()) < 3:
                return True
        return False

    for idx, row in enumerate(trace_rows):
        if row.message and not _near(row.created_at):
            messages.append({
                "id": f"in-{idx}",
                "direction": "in",
                "body": row.message,
                "time": row.created_at.isoformat() if row.created_at else "",
                "isAI": False,
                "eventType": "customer",
                "_ts": row.created_at,
            })
        if row.response_text and not _near(row.created_at):
            messages.append({
                "id": f"out-{idx}",
                "direction": "out",
                "body": row.response_text,
                "time": row.created_at.isoformat() if row.created_at else "",
                "isAI": bool(row.orchestrator_used),
                "eventType": "ai",
                "_ts": row.created_at,
            })

    messages.sort(key=lambda m: m.get("_ts") or "")
    messages = messages[-limit:]
    for m in messages:
        m.pop("_ts", None)

    return {"messages": messages}


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

    raw = (customer_phone or "").replace("+", "").replace("-", "").replace(" ", "")
    suffix = raw[-9:] if len(raw) >= 9 else raw
    has_active_handoff = False
    for hs in db.query(HandoffSession).filter(
        HandoffSession.tenant_id == tenant_id,
        HandoffSession.status == "active",
    ).all():
        hs_raw = (hs.customer_phone or "").replace("+", "").replace("-", "").replace(" ", "")
        if hs_raw == raw or hs_raw.endswith(suffix):
            has_active_handoff = True
            break

    if not has_active_handoff:
        from handoff.manager import create_handoff_session  # noqa: PLC0415
        create_handoff_session(
            db, tenant_id, customer_phone,
            customer_name=convo.customer.name if convo.customer else customer_phone,
            last_message=body.message,
            reason="staff_takeover",
        )

    convo.status = "human"
    convo.is_human_handoff = True
    convo.paused_by_human = True
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
    close_raw = (body.customer_phone or "").replace("+", "").replace("-", "").replace(" ", "")
    close_suffix = close_raw[-9:] if len(close_raw) >= 9 else close_raw
    for hs in db.query(HandoffSession).filter(
        HandoffSession.tenant_id == tenant_id,
        HandoffSession.status == "active",
    ).all():
        hs_raw = (hs.customer_phone or "").replace("+", "").replace("-", "").replace(" ", "")
        if hs_raw == close_raw or hs_raw.endswith(close_suffix):
            from handoff.manager import resolve_handoff_session  # noqa: PLC0415
            resolve_handoff_session(db, hs.id, tenant_id, resolved_by="dashboard_close")

    convo.status = "active"
    convo.is_human_handoff = False
    convo.paused_by_human = False
    db.add(convo)
    db.commit()
    return {"closed": True}
