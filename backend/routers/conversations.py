"""
routers/conversations.py
────────────────────────
Tenant-scoped conversation list/detail endpoints for the merchant dashboard.

Backed by `Conversation`, `MessageEvent`, `ConversationTrace`, and `ConversationLog`
where available. This is intentionally lightweight but real — no fake data.
"""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

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


class AIPauseIn(BaseModel):
    customer_phone: str
    reason: str = "manual_pause"


class AIResumeIn(BaseModel):
    customer_phone: str


class MarkReadIn(BaseModel):
    customer_phone: str


class BlocklistIn(BaseModel):
    phone: str
    customer_phone: str | None = None  # optional: also pause that conversation


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


def _digits_only(value: str | None) -> str:
    """Phone helper — strip everything but digits."""
    if not value:
        return ""
    return "".join(c for c in str(value) if c.isdigit())


def _find_conversations_for_phone(
    db: Session,
    tenant_id: int,
    customer_phone: str,
) -> list[Conversation]:
    """Return EVERY Conversation row in this tenant matching *customer_phone*.

    The list view dedupes multiple legacy rows for the same customer
    into one display entry, but pause/resume must update ALL of them
    otherwise the dashboard shows stale state from a sibling row.

    Match strategy (most specific first):
    1. Customer.id ↔ Conversation.customer_id, when we can resolve the
       phone to a Customer via normalized phone.
    2. Conversation.extra_metadata->>'customer_phone' / 'phone' fallback
       (digits-only suffix match) for orphaned rows that never got a
       Customer link.
    """
    norm = normalize_phone(customer_phone) or customer_phone
    digits = _digits_only(norm)
    suffix = digits[-9:] if len(digits) >= 9 else digits

    candidates: list[str] = []
    for v in (customer_phone, norm, digits, f"+{digits}" if digits else ""):
        v = (v or "").strip()
        if v and v not in candidates:
            candidates.append(v)

    customer_ids: list[int] = []
    if candidates:
        from sqlalchemy import or_  # noqa: PLC0415
        rows = (
            db.query(Customer.id)
            .filter(
                Customer.tenant_id == tenant_id,
                or_(
                    Customer.phone.in_(candidates),
                    Customer.normalized_phone.in_(candidates),
                ),
            )
            .all()
        )
        customer_ids = [r[0] for r in rows]

    matches: dict[int, Conversation] = {}
    if customer_ids:
        for c in (
            db.query(Conversation)
            .filter(
                Conversation.tenant_id == tenant_id,
                Conversation.customer_id.in_(customer_ids),
            )
            .all()
        ):
            matches[c.id] = c

    # Fallback: scan extra_metadata for orphaned conversation rows.
    # Suffix match keeps the cost-vs-correctness trade-off reasonable for
    # a per-tenant scan.
    if suffix:
        for c in (
            db.query(Conversation)
            .filter(Conversation.tenant_id == tenant_id)
            .all()
        ):
            if c.id in matches:
                continue
            meta = c.extra_metadata or {}
            phone_meta = str(meta.get("customer_phone") or meta.get("phone") or "")
            d = _digits_only(phone_meta)
            if d and (d == digits or d.endswith(suffix)):
                matches[c.id] = c

    return list(matches.values())


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
    # ── Marker scrub on outbound persistence ──────────────────────
    # Same rationale as in StateManager.save_message: this function
    # is called by campaigns / automations / COD / AI-fallback /
    # orders dashboard — each one could write text into a
    # MessageEvent that the dashboard later renders. Strip any
    # `[TEMPLATE:foo]`-style internal marker that leaked from a
    # template-substitution step BEFORE we persist. Wire-layer
    # scrub in provider_send_message handles the WhatsApp send;
    # this one keeps the dashboard preview clean.
    safe_body = body
    if isinstance(body, str) and body:
        try:
            from core.ai_libraries import scrub_internal_markers  # noqa: PLC0415
            safe_body = scrub_internal_markers(body)
            if safe_body != body:
                _log.info(
                    "[PERSIST_SCRUB] record_outbound_message "
                    "tenant=%s phone=%s len_before=%d len_after=%d",
                    tenant_id, phone, len(body), len(safe_body or ""),
                )
        except Exception as _scrub_exc:
            _log.warning(
                "[PERSIST_SCRUB] failed tenant=%s err=%s — "
                "writing original body", tenant_id, _scrub_exc,
            )
            safe_body = body

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
            body=safe_body,
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
async def list_conversations(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = 80,
    offset: int = 0,
    filter: str = "all",
):
    """
    Build the conversation list from **all** sources:
    1. ``Conversation`` records (canonical)
    2. Latest ``MessageEvent`` per conversation (actual last message)
    3. ``ConversationTrace`` fallback for phones without MessageEvent

    Optional ``filter`` param narrows the SQL fetch BEFORE pagination,
    so deep merchants (>1500 conversations) can still page reliably
    within a single filter:

      - ``human`` / ``agent_req``   → conversations flagged by any of
                                      ``is_human_handoff`` / ``needs_human`` /
                                      ``handoff_active`` / ``taken_over_at`` /
                                      ``status='human'``, or with an active
                                      ``HandoffSession`` row.
      - ``closed``                  → ``status='closed'`` (server-stamped) —
                                      the client also surfaces 24h-window
                                      expiry as closed when ``status`` is not
                                      explicitly set.
      - ``paused``                  → ``ai_paused=True`` AND not human-takeover.
      - ``blocked``                 → phone in the tenant blocklist.
      - ``active`` / ``unsubscribed`` / ``all`` → no SQL narrowing; the
                                      client filter is enough (cheap to do
                                      because the SQL cap already trims the
                                      tail by recency).

    ``total_count`` and ``has_more`` are recomputed against the SAME
    filter so the merchant can keep pressing "load more" until the
    filtered tail is exhausted — closing the regression that bit the
    inbox after the SQL-limit pagination rollout.
    """
    from sqlalchemy import and_, func, or_  # noqa: PLC0415
    import json as _json  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    _t0 = _time.perf_counter()

    def _inbox_live_only_clause():
        """Rows stamped as historical/backfill must not drive inbox surface."""
        hi = MessageEvent.extra_metadata["historical_import"].astext
        mo = MessageEvent.extra_metadata["message_origin"].astext
        return and_(
            or_(hi.is_(None), hi != "true"),
            or_(mo.is_(None), mo != "historical_sync"),
        )

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    paging_cap_limit = max(1, min(int(limit or 80), 200))

    # Canonical filter slug. Unknown / empty values fall back to ``all``
    # so an old dashboard that doesn't send the param keeps working.
    _allowed_filters = {
        "all", "active", "human", "agent_req",
        "paused", "blocked", "unsubscribed", "closed",
    }
    filter_slug = (filter or "all").strip().lower()
    if filter_slug not in _allowed_filters:
        filter_slug = "all"

    def _norm(p: str) -> str:
        return (p or "").strip().replace("+", "").replace("-", "").replace(" ", "")

    active_handoffs: dict[str, str] = {}
    for row in db.query(HandoffSession).filter(
        HandoffSession.tenant_id == tenant_id,
        HandoffSession.status == "active",
    ).all():
        active_handoffs[_norm(row.customer_phone)] = row.handoff_reason or "unknown"

    # Tenant-level blocklist — pre-load once so the per-row classifier
    # never re-queries the tenant on every conversation.
    from core.ai_pause_guard import (  # noqa: PLC0415
        _digits as _ai_digits,
        list_blocked_numbers as _list_blocked,
    )
    _blocked_digits: set[str] = set()
    try:
        for raw in _list_blocked(db, tenant_id):
            d = _ai_digits(raw)
            if d:
                _blocked_digits.add(d)
    except Exception as exc:
        _log.debug("[list_conversations] blocklist load failed tenant=%s: %s", tenant_id, exc)

    def _is_phone_blocked(phone: str) -> bool:
        d = _ai_digits(phone)
        if not d:
            return False
        if d in _blocked_digits:
            return True
        suffix = d[-9:] if len(d) >= 9 else d
        for entry in _blocked_digits:
            if entry == d or entry.endswith(suffix):
                return True
        return False

    from datetime import timedelta  # noqa: PLC0415
    from models import WaConversationWindow  # noqa: PLC0415
    _window_cutoff = datetime.utcnow() - timedelta(hours=24)
    _open_windows: set[str] = set()
    for w in db.query(WaConversationWindow).filter(
        WaConversationWindow.tenant_id == tenant_id,
        WaConversationWindow.category.in_(["service", "marketing"]),
        WaConversationWindow.window_start >= _window_cutoff,
    ).all():
        _open_windows.add(_norm(w.customer_phone))

    def _is_human_takeover(convo: Optional[Conversation]) -> bool:
        """Single source of truth for the "بشري" filter.

        A conversation is considered a human takeover ONLY when one of
        the explicit human-state columns is set. ``ai_paused`` is *not*
        consulted here on purpose — manual pause (REASON_MANUAL_PAUSE)
        is a different UX path than human takeover and must never make
        the conversation appear in the human-reply filter.
        """
        if convo is None:
            return False
        if bool(getattr(convo, "is_human_handoff", False)):
            return True
        if bool(getattr(convo, "needs_human", False)):
            return True
        if bool(getattr(convo, "handoff_active", False)):
            return True
        if getattr(convo, "taken_over_at", None) is not None:
            return True
        return False

    def _status_for(phone: str, convo: Optional[Conversation]) -> str:
        n = _norm(phone)
        if n in active_handoffs or _is_human_takeover(convo):
            return "human"
        if convo and str(convo.status).lower() == "closed":
            return "closed"
        return "active"

    def _handoff_reason_for(phone: str) -> Optional[str]:
        return active_handoffs.get(_norm(phone))

    def _has_window(phone: str) -> bool:
        return _norm(phone) in _open_windows

    # ── 1. SQL-paginated Conversation records → phone_info map ───────────────
    # Earlier revisions loaded EVERY Conversation row for the tenant and
    # then sliced in Python. For a tenant with 2 900+ conversations that
    # was still ~470ms (after the N+1 fix) and grew linearly with inbox
    # size. We now order by the latest MessageEvent timestamp per row at
    # the SQL level and only fetch ``fetch_cap`` rows.
    #
    # ``fetch_cap`` is sized so that after sibling-row collapsing (multiple
    # Conversation rows can map to the same phone) we still have enough
    # unique phones to fill the requested ``[offset, offset+limit]`` slice.
    # 200 rows is the floor for offset=0 and is multiplied as the merchant
    # pages deeper via "load more". The hard ceiling (1500) keeps memory
    # bounded against pathological deep paging.
    fetch_cap = max(200, (paging_cap_limit + max(0, int(offset or 0))) * 3)
    fetch_cap = min(fetch_cap, 1500)

    # MAX(created_at) per conversation — used both for ordering here AND
    # reused below as the "latest message" source where possible.
    last_msg_ts_sq = (
        db.query(
            MessageEvent.conversation_id.label("conv_id"),
            func.max(MessageEvent.created_at).label("last_msg_at"),
        )
        .filter(
            MessageEvent.tenant_id == tenant_id,
            _inbox_live_only_clause(),
        )
        .group_by(MessageEvent.conversation_id)
        .subquery()
    )
    # ── SQL-level filter narrowing ───────────────────────────────────────────
    # We build a list of additional WHERE clauses that BOTH the row
    # query and the COUNT(*) query share so ``has_more`` math stays
    # consistent with the slice we return. For filters that need data
    # outside the Conversation row (HandoffSession, blocklist, …) we
    # still pre-resolve here so the SQL stays a single roundtrip.
    extra_clauses: list = []
    if filter_slug in ("human", "agent_req"):
        # Canonical "this conversation needs human attention" check.
        # Mirrors `_is_human_takeover()` + active handoff-session lookup.
        handoff_session_phones = list(active_handoffs.keys())  # already normalised
        handoff_or = [
            Conversation.is_human_handoff.is_(True),
            Conversation.needs_human.is_(True),
            Conversation.handoff_active.is_(True),
            Conversation.taken_over_at.isnot(None),
            func.lower(Conversation.status) == "human",
        ]
        if handoff_session_phones:
            # Pre-loaded set of normalised customer phones with an
            # active HandoffSession. The Customer rows may store the
            # phone with or without a leading '+'; cover both.
            phone_variants: set[str] = set()
            for p in handoff_session_phones:
                if not p:
                    continue
                phone_variants.add(p)
                phone_variants.add(f"+{p}")
            handoff_or.append(
                Conversation.customer.has(
                    or_(
                        Customer.normalized_phone.in_(list(phone_variants)),
                        Customer.phone.in_(list(phone_variants)),
                    )
                )
            )
        extra_clauses.append(or_(*handoff_or))
    elif filter_slug == "closed":
        # Only conversations the merchant (or an automation) explicitly
        # marked as closed. 24h-window expiry is a client-only signal
        # because the WhatsApp window can re-open the moment the
        # customer sends a new message — we don't want the row to
        # vanish from the filter just because the API was hit a few
        # seconds later than the timer.
        extra_clauses.append(func.lower(Conversation.status) == "closed")
    elif filter_slug == "paused":
        # AI paused AND NOT a human takeover. The "blocked" and
        # "human" filters are kept disjoint at the SQL level too so
        # the badge counts add up to <= the tenant total.
        extra_clauses.extend([
            Conversation.ai_paused.is_(True),
            Conversation.is_human_handoff.is_(False),
            Conversation.needs_human.is_(False),
            Conversation.handoff_active.is_(False),
            or_(
                Conversation.ai_paused_reason.is_(None),
                func.lower(Conversation.ai_paused_reason) != "internal_number",
            ),
        ])
    elif filter_slug == "blocked":
        # The tenant blocklist is the source of truth (`_blocked_digits`
        # — already loaded). We also accept the legacy
        # ``ai_paused_reason='internal_number'`` row as blocked. The
        # blocklist join uses the normalised phone via Customer.
        blocked_clauses = []
        if _blocked_digits:
            blocked_variants: set[str] = set()
            for d in _blocked_digits:
                if not d:
                    continue
                blocked_variants.add(d)
                blocked_variants.add(f"+{d}")
            blocked_clauses.append(
                Conversation.customer.has(
                    or_(
                        Customer.normalized_phone.in_(list(blocked_variants)),
                        Customer.phone.in_(list(blocked_variants)),
                    )
                )
            )
        blocked_clauses.append(
            and_(
                Conversation.ai_paused.is_(True),
                func.lower(Conversation.ai_paused_reason) == "internal_number",
            )
        )
        extra_clauses.append(or_(*blocked_clauses))
    # ``all`` / ``active`` / ``unsubscribed`` → no SQL narrowing.

    convo_rows_q = (
        db.query(Conversation)
        .options(joinedload(Conversation.customer))
        .outerjoin(last_msg_ts_sq, last_msg_ts_sq.c.conv_id == Conversation.id)
        .filter(Conversation.tenant_id == tenant_id, *extra_clauses)
        .order_by(
            last_msg_ts_sq.c.last_msg_at.desc().nullslast(),
            Conversation.id.desc(),
        )
        .limit(fetch_cap)
    )
    convo_rows = convo_rows_q.all()

    # Tenant-wide totals (cheap COUNT — used for has_more / paging math).
    # Recomputed against the SAME filter so the slice math is honest.
    tenant_convo_count = (
        db.query(func.count(Conversation.id))
        .filter(Conversation.tenant_id == tenant_id, *extra_clauses)
        .scalar()
    ) or 0
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
            # If THIS row is paused or in human takeover, the merged
            # display row should reflect that even when another sibling
            # row isn't.
            if bool(getattr(convo, "ai_paused", False)):
                prev_at = prev.get("aiPausedAt") or ""
                this_at = (
                    convo.ai_paused_at.isoformat()
                    if getattr(convo, "ai_paused_at", None) else ""
                )
                if not prev.get("aiPaused") or this_at > prev_at:
                    prev["aiPaused"] = True
                    prev["aiPausedReason"] = getattr(convo, "ai_paused_reason", None)
                    prev["aiPausedAt"] = this_at or prev.get("aiPausedAt")
                    prev["isAI"] = False
            if _is_human_takeover(convo) or n in active_handoffs:
                prev["needsHuman"] = True
                prev["status"] = "human"
                prev["isAI"] = False
                if bool(getattr(convo, "handoff_active", False)):
                    prev["handoffActive"] = True
                if getattr(convo, "taken_over_at", None) and not prev.get("takenOverAt"):
                    prev["takenOverAt"] = convo.taken_over_at.isoformat()
                if getattr(convo, "taken_over_by", None) and not prev.get("takenOverBy"):
                    prev["takenOverBy"] = convo.taken_over_by
            # If ANY sibling row matches the blocklist, the merged
            # display row is blocked.
            if _is_phone_blocked(phone) or (
                bool(getattr(convo, "ai_paused", False))
                and (getattr(convo, "ai_paused_reason", None) or "") == "internal_number"
            ):
                prev["isBlocked"] = True
                prev["isAI"] = False
            prev_has_name = prev["customer"] and prev["customer"] != prev["phone"]
            if name and not prev_has_name:
                phone_info.pop(existing_key)
            elif prev_has_name:
                conv_id_to_phone[convo.id] = existing_key
                continue
            else:
                conv_id_to_phone[convo.id] = existing_key
                continue

        ai_paused_now = bool(getattr(convo, "ai_paused", False))
        needs_human_now = _is_human_takeover(convo) or n in active_handoffs
        # ``isBlocked`` is true when the phone matches the tenant's
        # blocklist OR the conversation is paused with the
        # ``internal_number`` reason (legacy rows that pre-date the
        # blocklist persistence). It's mutually-exclusive with the
        # human and "paused only" filters at the frontend level.
        is_blocked_now = _is_phone_blocked(phone) or (
            ai_paused_now
            and (getattr(convo, "ai_paused_reason", None) or "") == "internal_number"
        )
        display_name = name or phone
        phone_info[phone] = {
            "id": str(convo.id),
            "customer": display_name,
            "phone": phone,
            "lastMsg": "",
            "time": "",
            "isAI": status != "human" and not ai_paused_now and not needs_human_now and not is_blocked_now,
            "status": status,
            "unread": 0,
            "lastMsgType": "",
            "windowOpen": _has_window(phone),
            "handoffReason": _handoff_reason_for(phone),
            "isUnsubscribed": False,
            "pendingUnsubscribe": False,
            "aiPaused": ai_paused_now,
            "aiPausedReason": getattr(convo, "ai_paused_reason", None),
            "aiPausedAt": (
                convo.ai_paused_at.isoformat()
                if getattr(convo, "ai_paused_at", None) else None
            ),
            "needsHuman": needs_human_now,
            "handoffActive": bool(getattr(convo, "handoff_active", False)),
            "takenOverAt": (
                convo.taken_over_at.isoformat()
                if getattr(convo, "taken_over_at", None) else None
            ),
            "takenOverBy": getattr(convo, "taken_over_by", None),
            "isBlocked": is_blocked_now,
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
                _inbox_live_only_clause(),
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
        # Per-conversation last_read_at (set by /mark-read when the
        # merchant opens the conversation). When NULL we fall back to
        # the legacy "newer than last outbound" rule.
        last_read_map: dict[int, datetime] = {}
        for cid, lra in (
            db.query(Conversation.id, Conversation.last_read_at)
            .filter(Conversation.tenant_id == tenant_id, Conversation.id.in_(conv_ids))
            .all()
        ):
            if lra is not None:
                last_read_map[cid] = lra
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
                _inbox_live_only_clause(),
                (MessageEvent.created_at > last_out_sq.c.last_out) | (last_out_sq.c.last_out.is_(None)),
            )
            .group_by(MessageEvent.conversation_id)
            .all()
        )
        # Adjust unread counts for conversations that have a last_read_at
        # timestamp. Previously this issued one COUNT query per conversation
        # (catastrophic N+1: 200 convos = 200 sequential DB roundtrips). We
        # now batch the work into a single GROUP BY query for the subset
        # of conversation IDs that actually have a last_read_at set, then
        # merge the per-cid counts in Python.
        if last_read_map:
            from sqlalchemy import case  # noqa: PLC0415

            # Build a single CASE expression: "the cid's lra" → join via
            # values(). On Postgres we use a temporary VALUES list; on
            # SQLite we fall back to chunked WHERE id IN (...).
            cids_with_lra = list(last_read_map.keys())
            # Run one query that counts unread per cid, applying the per-cid
            # ``MessageEvent.created_at > lra`` filter inline through CASE.
            lra_expr = case(
                {cid: lra for cid, lra in last_read_map.items()},
                value=MessageEvent.conversation_id,
            )
            live_rows = (
                db.query(
                    MessageEvent.conversation_id,
                    func.count(MessageEvent.id).label("cnt"),
                )
                .outerjoin(
                    last_out_sq,
                    MessageEvent.conversation_id == last_out_sq.c.conversation_id,
                )
                .filter(
                    MessageEvent.tenant_id == tenant_id,
                    MessageEvent.conversation_id.in_(cids_with_lra),
                    MessageEvent.direction != "outbound",
                    _inbox_live_only_clause(),
                    (MessageEvent.created_at > last_out_sq.c.last_out)
                    | (last_out_sq.c.last_out.is_(None)),
                    MessageEvent.created_at > lra_expr,
                )
                .group_by(MessageEvent.conversation_id)
                .all()
            )
            live_map: dict[int, int] = {cid: int(cnt) for cid, cnt in live_rows}
            adjusted: list[tuple[int, int]] = []
            for cid, _cnt in unread_rows:
                if cid in last_read_map:
                    adjusted.append((cid, live_map.get(cid, 0)))
                else:
                    adjusted.append((cid, _cnt))
            unread_rows = adjusted
        for cid, cnt in unread_rows:
            phone = conv_id_to_phone.get(cid)
            if phone and phone in phone_info:
                phone_info[phone]["unread"] = cnt

    # ── 4. ConversationTrace fallback for gaps ───────────────────────────────
    trace_rows = (
        db.query(ConversationTrace)
        .filter(ConversationTrace.tenant_id == tenant_id)
        .order_by(ConversationTrace.created_at.desc())
        .limit(min(500, max(250, paging_cap_limit * 6)))
        .all()
    )

    # Pre-fetch every Customer row that any trace row might need so we
    # don't fire one query per phone inside the loop (the previous
    # implementation issued N queries on cold cache).
    trace_unknown_phones: set[str] = set()
    for row in trace_rows:
        if not row.customer_phone:
            continue
        if _norm(row.customer_phone) not in norm_to_key:
            trace_unknown_phones.add(row.customer_phone)
            n = _norm(row.customer_phone)
            if n:
                trace_unknown_phones.add(n)
                trace_unknown_phones.add(f"+{n}")
    trace_name_by_norm: dict[str, str] = {}
    if trace_unknown_phones:
        for phone_val, norm_val, name_val in (
            db.query(Customer.phone, Customer.normalized_phone, Customer.name)
            .filter(
                Customer.tenant_id == tenant_id,
                or_(
                    Customer.phone.in_(list(trace_unknown_phones)),
                    Customer.normalized_phone.in_(list(trace_unknown_phones)),
                ),
            )
            .all()
        ):
            if not name_val:
                continue
            for candidate in (norm_val, phone_val):
                cnorm = _norm(candidate or "")
                if cnorm and cnorm not in trace_name_by_norm:
                    trace_name_by_norm[cnorm] = name_val

    for row in trace_rows:
        phone = row.customer_phone
        key = norm_to_key.get(_norm(phone)) or (phone if phone in phone_info else None)
        if key and key in phone_info:
            if not phone_info[key]["lastMsg"] and not phone_info[key]["time"]:
                phone_info[key]["lastMsg"] = row.message or ""
                phone_info[key]["time"] = row.created_at.isoformat() if row.created_at else ""
        elif filter_slug != "all":
            # When a filter is active, the SQL query above already
            # narrowed to the canonical matching set. ConversationTrace
            # rows have no human-takeover/closed columns, so adding
            # them here would pollute the filtered slice (e.g. an
            # ``agent_req`` page silently showing trace-only phones).
            # The trace fallback is purely for the unfiltered inbox.
            continue
        elif _norm(phone) not in norm_to_key:
            norm_to_key[_norm(phone)] = phone
            trace_name = trace_name_by_norm.get(_norm(phone)) or phone
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
                "windowOpen": _has_window(phone),
                "handoffReason": _handoff_reason_for(phone),
                "isUnsubscribed": False,
                "pendingUnsubscribe": False,
                "aiPaused": False,
                "aiPausedReason": None,
                "aiPausedAt": None,
                "isBlocked": _is_phone_blocked(phone),
                "_conv_id": None,
            }

    # ── 5. Enrich phone-like names from Customer table (bulk) ────────────────
    # Previously this issued one query per phone-like row (another N+1).
    # We now collect all candidate phones up front and look them up in a
    # single SQL call, then update the in-memory map.
    phones_needing_name: list[str] = []
    keys_needing_name: list[str] = []
    for key, info in phone_info.items():
        cname = info.get("customer", "")
        if not cname or cname.replace("+", "").replace("-", "").replace(" ", "").isdigit():
            keys_needing_name.append(key)
            phones_needing_name.append(key)
            n = _norm(key)
            if n:
                phones_needing_name.append(n)
                phones_needing_name.append(f"+{n}")

    if phones_needing_name:
        from sqlalchemy import or_ as _or_  # noqa: PLC0415
        enrich_rows = (
            db.query(Customer.phone, Customer.normalized_phone, Customer.name)
            .filter(
                Customer.tenant_id == tenant_id,
                _or_(
                    Customer.phone.in_(phones_needing_name),
                    Customer.normalized_phone.in_(phones_needing_name),
                ),
            )
            .all()
        )
        name_by_norm: dict[str, str] = {}
        for phone_val, norm_val, name_val in enrich_rows:
            if not name_val:
                continue
            if name_val.replace("+", "").replace("-", "").replace(" ", "").isdigit():
                continue
            for candidate in (norm_val, phone_val):
                cnorm = _norm(candidate or "")
                if cnorm and cnorm not in name_by_norm:
                    name_by_norm[cnorm] = name_val
        for key in keys_needing_name:
            real_name = name_by_norm.get(_norm(key))
            if real_name:
                phone_info[key]["customer"] = real_name

    # ── 5b. Enrich unsubscribe status from Customer table (bulk) ────────────
    def _is_pending_active(meta: dict) -> bool:
        """Return True only when pending_unsubscribe flag is set AND not expired."""
        if not meta.get("pending_unsubscribe"):
            return False
        exp_str = meta.get("pending_unsubscribe_expires_at")
        if not exp_str:
            return True
        try:
            exp_dt = datetime.fromisoformat(exp_str)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) < exp_dt
        except Exception:
            return True

    # Build candidate phone formats per conversation (with/without +) so we
    # match Customer rows regardless of how the phone column is stored.
    from sqlalchemy import or_  # noqa: PLC0415
    _all_phone_candidates: set[str] = set()
    for p in phone_info:
        n = _norm(p)
        if n:
            _all_phone_candidates.add(n)
            _all_phone_candidates.add(f"+{n}")
        if p:
            _all_phone_candidates.add(p)

    if _all_phone_candidates:
        unsub_customers = (
            db.query(Customer)
            .filter(
                Customer.tenant_id == tenant_id,
                or_(
                    Customer.normalized_phone.in_(list(_all_phone_candidates)),
                    Customer.phone.in_(list(_all_phone_candidates)),
                ),
            )
            .all()
        )
        for cust in unsub_customers:
            meta = cust.extra_metadata or {}
            if not meta:
                continue
            is_unsub = bool(meta.get("is_unsubscribed"))
            is_pending = _is_pending_active(meta)
            if not is_unsub and not is_pending:
                continue
            c_norm = _norm(cust.normalized_phone or cust.phone or "")
            matching_key = norm_to_key.get(c_norm)
            if matching_key and matching_key in phone_info:
                phone_info[matching_key]["isUnsubscribed"] = is_unsub
                phone_info[matching_key]["pendingUnsubscribe"] = is_pending

    # ── 6. Fallback: if last message is within 24h, mark window open ────────
    _24h_ago = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    for info in phone_info.values():
        if not info.get("windowOpen") and info.get("time") and info["time"] >= _24h_ago:
            info["windowOpen"] = True

    # ── 7. Build result, strip internal keys ─────────────────────────────────
    result = sorted(phone_info.values(), key=lambda c: c.get("time") or "", reverse=True)
    for c in result:
        c.pop("_conv_id", None)
    merged_count = len(result)
    safe_offset = max(0, int(offset or 0))
    safe_limit = paging_cap_limit
    page = result[safe_offset : safe_offset + safe_limit]

    # ``total_count`` is now the cheap COUNT(*) on the Conversation table
    # rather than ``len(result)``. The two differ when sibling rows
    # collapse into one phone — the merged count is a (small) lower
    # bound. For the merchant UX (showing "has more") the conservative
    # signal is whether we filled the page AND whether we hit fetch_cap.
    total = max(tenant_convo_count, merged_count)
    fetch_cap_hit = len(convo_rows) >= fetch_cap
    has_more = bool(
        fetch_cap_hit
        or (safe_offset + len(page)) < merged_count
    )

    # ── 8. Structured perf log ──────────────────────────────────────────────
    # INFO for fast responses, WARNING when duration_ms >= 2000ms so we
    # get an automatic alert if we regress past the post-fix expectation.
    duration_ms = int((_time.perf_counter() - _t0) * 1000)
    perf_payload = {
        "event":                     "conversations_list_perf",
        "tenant_id":                 tenant_id,
        "filter":                    filter_slug,
        "limit":                     safe_limit,
        "offset":                    safe_offset,
        "conversations_count":       tenant_convo_count,
        "merged_phone_count":        merged_count,
        "fetch_cap":                 fetch_cap,
        "fetch_cap_hit":             fetch_cap_hit,
        "page_size":                 len(page),
        "duration_ms":               duration_ms,
        "unread_count_strategy":     "grouped",
        "used_joinedload_customer":  True,
        "pagination":                "sql_limit",
        "msg_events_index_assumed":  "ix_msg_events_tenant_conv_created",
    }
    log_line = "[CONV_LIST_PERF] " + _json.dumps(perf_payload, ensure_ascii=False)
    if duration_ms >= 2000:
        _log.warning(log_line)
    else:
        _log.info(log_line)

    return {
        "conversations": page,
        "total_count": total,
        "has_more": has_more,
    }


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

    def _media_block(message_event_id: int, meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Surface inbound media so the conversation drawer can render
        an audio player / image preview with the transcript / vision
        text rendered underneath. Returns ``None`` for text rows so
        the frontend can fall back to the plain bubble layout.

        We include ``download_status`` separately from the AI status
        so the UI can distinguish three cases that previously all
        looked like "تعذر عرض ...":

          * download_status='failed' AND storage_url=null
            → the bytes never landed; reprocess will try to
              re-download from Meta if the media_id is still valid.
          * download_status='ok'      AND storage_url=null
            → impossible by construction (kept for safety).
          * download_status='ok'      AND ai status='skipped'
            → bytes are stored, transcript/vision was skipped
              because OPENAI_API_KEY is missing in this environment.
        """
        ni = (meta or {}).get("normalized_inbound") or {}
        src = str(ni.get("source_type") or "").lower()
        if src not in {"audio", "image"}:
            return None

        # ── Stale `skipped` override ──────────────────────────────
        #
        # Historical rows that came in BEFORE the OPENAI_API_KEY env
        # var was added to the worker still carry
        # ``transcript_status='skipped'`` / ``vision_status='skipped'``
        # in their `extra_metadata.normalized_inbound`. The frontend's
        # conditional renders THAT status as
        #
        #   "ميزة وصف الصور غير مفعّلة على الخادم (OPENAI_API_KEY مفقود)"
        #
        # which is now MISLEADING — the key IS present, only the
        # historical snapshot was taken before it was. We translate
        # the stale `skipped` to `stale_skipped` at read-time so the
        # UI can show "snapshot from when the key was missing — try
        # reprocess" instead. We do this only when:
        #
        #   1. The status is exactly 'skipped', AND
        #   2. The recorded error matches the "not configured" reason
        #      (so we don't accidentally override a legitimate future
        #      'skipped' that means something different), AND
        #   3. The CURRENT runtime env has the key (i.e. the
        #      misleading-snapshot condition is actually fixed now).
        #
        # We don't mutate the DB row — pure read-time translation.
        # The reprocess endpoint can still be invoked from the
        # button; the new ETag is just whether the merchant sees a
        # legacy "key missing" claim or not.
        _openai_present_now = bool(os.environ.get("OPENAI_API_KEY", "").strip())
        if src == "audio":
            t_status = ni.get("transcript_status")
            t_error  = ni.get("transcript_error")
            if (
                _openai_present_now
                and t_status == "skipped"
                and t_error in ("stt_not_configured", "vision_not_configured")
            ):
                t_status = "stale_skipped"
            return {
                "kind":              "audio",
                "message_event_id":  message_event_id,
                "storage_url":       ni.get("storage_url"),
                "mime_type":         ni.get("mime_type"),
                "duration":          ni.get("duration_seconds"),
                "voice":             bool(ni.get("voice")),
                "transcript":        ni.get("transcript_text"),
                "transcript_status": t_status,
                "download_status":   ni.get("audio_download_status"),
                "ai_used":           bool(ni.get("ai_used_audio") or False),
                "caption":           ni.get("caption"),
                "error":             ni.get("transcript_error"),
            }
        # image
        v_status = ni.get("vision_status")
        v_error  = ni.get("vision_error")
        if (
            _openai_present_now
            and v_status == "skipped"
            and v_error in ("vision_not_configured", "stt_not_configured")
        ):
            v_status = "stale_skipped"
        return {
            "kind":              "image",
            "message_event_id":  message_event_id,
            "storage_url":       ni.get("storage_url"),
            "mime_type":         ni.get("mime_type"),
            "description":       ni.get("vision_text"),
            "vision_status":     v_status,
            "download_status":   ni.get("image_download_status"),
            "ai_used":           bool(ni.get("ai_used_image") or False),
            "caption":           ni.get("caption"),
            "error":             ni.get("vision_error"),
        }

    messages: List[Dict[str, Any]] = [
        {
            "id": str(r.id),
            "direction": "out" if (r.direction or "").lower() == "outbound" else "in",
            "body": r.body or "",
            "time": r.created_at.isoformat() if r.created_at else "",
            "isAI": bool((r.extra_metadata or {}).get("is_ai")),
            "eventType": _event_type_label(r),
            "media": _media_block(r.id, r.extra_metadata or {}),
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


@router.get("/{conversation_id:int}/media-debug")
async def conversation_media_debug(
    conversation_id: int,
    request: Request,
    db: Session = Depends(get_db),
    limit: int = 50,
):
    """Return every inbound media message on this conversation with
    the per-stage status fields the merchant brain pipeline records.

    Used by the dashboard's "تشخيص الوسائط" panel and by support to
    answer questions like:

      * "did نحلة actually receive the voice note?"
      * "did Whisper transcribe it?"
      * "what did the AI see in the photo customer attached?"

    The endpoint surfaces, for each inbound MessageEvent that has a
    ``normalized_inbound`` payload on its ``extra_metadata``:

      - ``message_id``                — row id (stable for support)
      - ``created_at``                — when it landed
      - ``direction``                 — always 'inbound' here
      - ``source_type``               — audio / image / text / …
      - ``mime_type``, ``byte_size``  — what we stored
      - ``storage_url``               — playable / viewable in the UI
      - ``audio_download_status``     — pending / ok / failed
      - ``transcript_status``         — pending / ok / empty / failed / skipped
      - ``transcript_text_preview``   — first 240 chars (full text in body)
      - ``transcript_error``          — short error description
      - ``ai_used_audio`` / ``ai_used_image``
      - ``image_download_status`` / ``vision_status`` / ``vision_text``
      - ``error_message`` (alias of ``transcript_error`` / ``vision_error``)

    Tenant-scoped via the standard ``resolve_tenant_id`` flow.
    """
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    convo = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.tenant_id == tenant_id,
    ).first()
    if not convo:
        raise HTTPException(status_code=404, detail="conversation_not_found")

    rows = (
        db.query(MessageEvent)
        .filter(
            MessageEvent.tenant_id == tenant_id,
            MessageEvent.conversation_id == conversation_id,
            MessageEvent.direction == "inbound",
        )
        .order_by(MessageEvent.created_at.desc())
        .limit(max(1, min(int(limit or 50), 200)))
        .all()
    )

    # Lazy import — keeps the module-level cycle clean and avoids
    # paying for an inbound-media-storage import when no row on this
    # conversation actually has media metadata.
    from services.inbound_media_storage import resolve_storage_path  # noqa: PLC0415

    out: List[Dict[str, Any]] = []
    for r in rows:
        meta = dict(r.extra_metadata or {})
        ni = meta.get("normalized_inbound") or {}
        if not ni:
            # No media metadata on this row — skip. We surface
            # text-only inbounds in the main messages endpoint
            # already; here we only want media diagnostics.
            continue
        source_type = str(ni.get("source_type") or "").lower()
        if source_type not in {"audio", "image"}:
            continue

        transcript_text = ni.get("transcript_text") or ""
        vision_text     = ni.get("vision_text") or ""
        # ``error_message`` is the single string the UI surfaces in
        # the diagnostic panel. Pick the one populated by whichever
        # stage failed so we never show two competing reasons.
        error_message = (
            ni.get("transcript_error")
            or ni.get("vision_error")
            or None
        )

        # Walk the storage layer to verify the bytes are still on
        # disk where the storage_url claims they are. A persisted
        # row pointing at a missing file is the #1 cause of "تعذر
        # عرض الصورة" — we surface it explicitly so support has a
        # single field to grep on.
        storage_sha256 = ni.get("storage_sha256")
        local_path_exists: Optional[bool] = None
        if storage_sha256:
            try:
                local = resolve_storage_path(
                    tenant_id=tenant_id, sha256=storage_sha256,
                )
                local_path_exists = bool(local and local.exists())
            except Exception:
                local_path_exists = False

        out.append({
            "message_id":            r.id,
            "created_at":            r.created_at.isoformat() if r.created_at else None,
            "direction":             r.direction,
            "source_type":           source_type,
            "media_type":            source_type,  # alias matching spec
            "media_id":              ni.get("media_id"),
            "original_media_id":     ni.get("media_id"),  # alias matching spec
            "mime_type":             ni.get("mime_type"),
            "voice":                 ni.get("voice"),
            "duration_seconds":      ni.get("duration_seconds"),
            "caption":               ni.get("caption"),
            "byte_size":             ni.get("byte_size"),
            "storage_url":           ni.get("storage_url"),
            "public_media_url":      ni.get("storage_url"),  # alias
            "storage_sha256":        storage_sha256,
            "local_path_exists":     local_path_exists,
            # Unified ``download_status`` regardless of media type, so
            # the UI can render one column without branching.
            "download_status": (
                ni.get("audio_download_status")
                if source_type == "audio"
                else ni.get("image_download_status")
            ),
            "audio_download_status": ni.get("audio_download_status"),
            "image_download_status": ni.get("image_download_status"),
            "transcript_status":     ni.get("transcript_status"),
            "vision_status":         ni.get("vision_status"),
            "transcript_text":       transcript_text or None,
            # Short preview so a list of 50 rows stays scrollable.
            "transcript_text_preview": (
                (transcript_text or vision_text or "")[:240] or None
            ),
            "vision_text":           vision_text or None,
            "transcript_error":      ni.get("transcript_error"),
            "vision_error":          ni.get("vision_error"),
            "error_message":         error_message,
            "last_error":            error_message,  # alias matching spec
            "ai_used_audio":         bool(ni.get("ai_used_audio") or False),
            "ai_used_image":         bool(ni.get("ai_used_image") or False),
            "media_fallback":        bool(meta.get("media_fallback") or False),
            "wa_message_id":         ni.get("wa_message_id") or meta.get("wa_message_id"),
            "wa_timestamp":          ni.get("wa_timestamp"),
        })

    return {
        "conversation_id": conversation_id,
        "tenant_id":       tenant_id,
        "count":           len(out),
        "rows":            out,
    }


@router.post("/media/{message_event_id:int}/reprocess")
async def reprocess_inbound_media(
    message_event_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Re-run the inbound-media pipeline (download + persist + AI) for
    a single existing ``MessageEvent`` row.

    Why this exists: in production we've seen cases where:

      * The bytes were downloaded but persisted to a volume that
        wasn't mounted on the new deploy → storage_url points to a
        404. Reprocess re-downloads from Meta (if the media_id is
        still valid) and writes to the current storage root.
      * OPENAI_API_KEY was missing at intake time → `transcript_status
        = skipped`. Once the key is set, reprocess re-runs Whisper /
        Vision without forcing the customer to re-send the message.
      * A new vision/STT model was rolled out and we want to backfill
        descriptions for old rows.

    This DOES NOT call the brain — it only refreshes the metadata
    block on the row. The AI didn't reply the first time around;
    re-running transcription doesn't change that.

    Tenant-scoped: 404 if the row belongs to a different tenant.
    """
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    row = db.query(MessageEvent).filter(
        MessageEvent.id == message_event_id,
        MessageEvent.tenant_id == tenant_id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="message_event_not_found")

    meta = dict(row.extra_metadata or {})
    ni = dict(meta.get("normalized_inbound") or {})
    source_type = str(ni.get("source_type") or "").lower()
    media_id = str(ni.get("media_id") or "").strip()

    if source_type not in {"audio", "image"} or not media_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "row is not a reprocessable inbound media event "
                f"(source_type={source_type!r}, media_id={media_id!r})"
            ),
        )

    # Resolve the merchant's WhatsApp connection — needed for the
    # Meta token download. We deliberately use the most-recent
    # connection so a re-onboarded tenant still works.
    wa_conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == tenant_id)
        .order_by(WhatsAppConnection.id.desc())
        .first()
    )
    if not wa_conn:
        raise HTTPException(
            status_code=404,
            detail="no WhatsAppConnection for this tenant — cannot redownload",
        )

    # Reconstruct the original webhook ``message`` shape so we can
    # reuse the production normalizer end-to-end. This guarantees
    # reprocessing exercises the exact same code path as the live
    # webhook does — no parallel logic to drift.
    msg: Dict[str, Any] = {
        "type":       source_type,
        "timestamp":  ni.get("wa_timestamp"),
        "id":         ni.get("wa_message_id") or "",
    }
    payload_inner: Dict[str, Any] = {
        "id":        media_id,
        "mime_type": ni.get("mime_type") or "",
    }
    if source_type == "audio":
        if ni.get("voice"):
            payload_inner["voice"] = True
        if ni.get("caption"):
            payload_inner["caption"] = ni["caption"]
        msg["audio"] = payload_inner
    else:
        if ni.get("caption"):
            payload_inner["caption"] = ni["caption"]
        msg["image"] = payload_inner

    from modules.ai.media.normalizer import normalize_whatsapp_inbound  # noqa: PLC0415

    try:
        result = await normalize_whatsapp_inbound(
            db=db, wa_conn=wa_conn, tenant_id=tenant_id, message=msg,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "[ReprocessMedia] tenant=%s msg_event=%s err=%s",
            tenant_id, message_event_id, exc,
        )
        raise HTTPException(status_code=502, detail=f"normalizer_failed: {exc}")

    new_ni = dict(result.metadata or {})
    # Preserve operator-only fields the new run won't re-emit.
    new_ni["reprocessed_at"] = datetime.now(timezone.utc).isoformat()
    new_ni["reprocessed_by"] = "manual_reprocess_endpoint"

    meta["normalized_inbound"] = new_ni
    row.extra_metadata = meta
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
    flag_modified(row, "extra_metadata")
    db.commit()

    _log.info(
        "[ReprocessMedia] tenant=%s msg_event=%s source=%s "
        "transcript_status=%s vision_status=%s download=%s",
        tenant_id, message_event_id, source_type,
        new_ni.get("transcript_status"),
        new_ni.get("vision_status"),
        new_ni.get("audio_download_status")
        or new_ni.get("image_download_status"),
    )

    return {
        "ok":              True,
        "message_event_id": message_event_id,
        "source_type":     source_type,
        "normalized_inbound": new_ni,
        "should_process":  bool(result.should_process),
        "fallback_reply_ar": result.fallback_reply_ar,
    }


@router.post("/reply")
async def reply_to_conversation(body: ReplyIn, request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    # Manual reply is an outbound action — blocked when no active billing
    from core.billing import require_outbound_access  # noqa: PLC0415
    require_outbound_access(db, tenant_id)

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
    from core.ai_pause_guard import (  # noqa: PLC0415
        pause_ai as _pause_ai,
        REASON_HUMAN_HANDOFF as _R_HOFF,
        REASON_MANUAL_TAKEOVER as _R_MANUAL,
        REASON_SUPPORT_ESCALATION as _R_ESCAL,
    )
    from core.auth import get_jwt_user_id  # noqa: PLC0415

    actor_user_id = None
    try:
        actor_user_id = get_jwt_user_id(request)
    except Exception:
        actor_user_id = None

    convo = _get_or_create_conversation(db, tenant_id, body.customer_phone, body.customer_name)
    session = create_handoff_session(
        db,
        tenant_id=tenant_id,
        customer_phone=body.customer_phone,
        customer_name=body.customer_name or body.customer_phone,
        last_message=body.last_message or "",
        reason=body.reason,
    )
    now = datetime.now(timezone.utc)

    if body.reason == "support_escalation":
        pause_reason = _R_ESCAL
    elif body.reason in {"manual_takeover", "staff_takeover"}:
        pause_reason = _R_MANUAL
    else:
        pause_reason = _R_HOFF

    # Update EVERY conversation row for this customer so the inbox view
    # (which dedupes by phone) is consistent regardless of which row was
    # picked first.
    convos = _find_conversations_for_phone(db, tenant_id, body.customer_phone) or [convo]
    for c in convos:
        c.status = "human"
        c.is_human_handoff = True
        c.paused_by_human = True
        c.needs_human = True
        c.handoff_active = True
        c.taken_over_at = now
        c.taken_over_by = (
            f"user:{actor_user_id}" if actor_user_id is not None else "dashboard:handoff"
        )
        db.add(c)
    db.commit()

    # Flip the AI loop guard so subsequent inbound messages don't trigger
    # token-spending replies after the dashboard takeover. We do this in
    # a second pass so the human-state columns above are persisted first.
    for c in convos:
        try:
            _pause_ai(db, c, reason=pause_reason, by="dashboard:handoff", commit=False)
        except Exception as exc:
            _log.debug("[ai_pause] handoff_conversation pause failed: %s", exc)
    db.commit()

    _log.info(
        "[HANDOFF_API] tenant=%s phone=%r reason=%s pause_reason=%s by=%s rows=%d",
        tenant_id, body.customer_phone, body.reason, pause_reason,
        actor_user_id or "dashboard", len(convos),
    )
    return {
        "handoff": True,
        "session_id": session.id,
        "needsHuman": True,
        "handoffActive": True,
        "takenOverAt": now.isoformat(),
        "takenOverBy": str(actor_user_id) if actor_user_id is not None else "dashboard",
    }


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


# ──────────────────────────────────────────────────────────────────────────
# AI loop / cost guard endpoints (used by the Conversations panel buttons)
# ──────────────────────────────────────────────────────────────────────────
def _serialize_ai_state(convo: Conversation) -> Dict[str, Any]:
    return {
        "ai_paused": bool(getattr(convo, "ai_paused", False)),
        "ai_paused_reason": getattr(convo, "ai_paused_reason", None),
        "ai_paused_at": (
            convo.ai_paused_at.isoformat()
            if getattr(convo, "ai_paused_at", None) else None
        ),
        "ai_paused_by": getattr(convo, "ai_paused_by", None),
    }


def _aggregate_ai_state(convos: list[Conversation]) -> Dict[str, Any]:
    """Return ai_paused state aggregated across all matching rows.

    A phone is considered paused if ANY of its conversation rows is
    paused. The reason / timestamp / actor are taken from the most
    recently paused row. This mirrors what the conversations list will
    display once we deduplicate rows by phone.
    """
    if not convos:
        return {
            "ai_paused": False,
            "ai_paused_reason": None,
            "ai_paused_at": None,
            "ai_paused_by": None,
        }
    paused_rows = [c for c in convos if bool(getattr(c, "ai_paused", False))]
    if not paused_rows:
        return _serialize_ai_state(convos[0])
    paused_rows.sort(
        key=lambda c: (c.ai_paused_at or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    return _serialize_ai_state(paused_rows[0])


@router.post("/ai-pause")
async def pause_conversation_ai(body: AIPauseIn, request: Request, db: Session = Depends(get_db)):
    """Pause AI replies for one customer's conversation. Inbound messages
    will still be stored, but the LLM is never called and no outbound
    reply is sent until the merchant resumes the AI from the dashboard."""
    from core.ai_pause_guard import pause_ai as _pause_ai, VALID_REASONS as _VALID  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    raw_phone = body.customer_phone or ""
    normalized_phone = normalize_phone(raw_phone) or raw_phone
    # Default to ``manual_pause`` (NOT a human takeover). Legacy callers
    # passing ``manual`` are accepted but normalised forward.
    reason = body.reason if body.reason in _VALID else "manual_pause"
    if reason == "manual":
        reason = "manual_pause"

    convos = _find_conversations_for_phone(db, tenant_id, normalized_phone)
    before = [
        {"id": c.id, "ai_paused": bool(c.ai_paused), "reason": c.ai_paused_reason}
        for c in convos
    ]
    if not convos:
        # No row yet — create one so the merchant's button click is
        # remembered the next time a webhook arrives.
        convos = [_get_or_create_conversation(db, tenant_id, normalized_phone)]

    for c in convos:
        _pause_ai(db, c, reason=reason, by=f"dashboard:{reason}", commit=False)
    db.commit()

    after = [
        {"id": c.id, "ai_paused": bool(c.ai_paused), "reason": c.ai_paused_reason}
        for c in convos
    ]
    _log.info(
        "[AI_PAUSE_API] tenant=%s phone_raw=%r normalized=%r reason=%s "
        "found_conversations=%d before=%s after=%s",
        tenant_id, raw_phone, normalized_phone, reason, len(convos), before, after,
    )
    state = _aggregate_ai_state(convos)
    return {
        "ok": True,
        "customerPhone": normalized_phone,
        "aiPaused": state["ai_paused"],
        "aiPausedReason": state["ai_paused_reason"],
        "aiPausedAt": state["ai_paused_at"],
        "aiPausedBy": state["ai_paused_by"],
        # Backwards-compatible snake_case for existing callers.
        **state,
    }


@router.post("/ai-resume")
async def resume_conversation_ai(body: AIResumeIn, request: Request, db: Session = Depends(get_db)):
    """Resume AI replies after a *manual pause*.

    This endpoint is **only** for the manual-pause UX path. It clears
    ``ai_paused`` and nothing else — the human-takeover columns stay
    intact. Calling resume on a conversation that's currently in a
    human takeover is a no-op for the takeover state (the merchant
    must use ``/handoff/return-to-ai`` to undo a takeover).
    """
    from core.ai_pause_guard import resume_ai as _resume_ai  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    raw_phone = body.customer_phone or ""
    normalized_phone = normalize_phone(raw_phone) or raw_phone

    convos = _find_conversations_for_phone(db, tenant_id, normalized_phone)
    before = [
        {"id": c.id, "ai_paused": bool(c.ai_paused), "reason": c.ai_paused_reason,
         "needs_human": bool(getattr(c, "needs_human", False)),
         "handoff_active": bool(getattr(c, "handoff_active", False))}
        for c in convos
    ]
    if not convos:
        convos = [_get_or_create_conversation(db, tenant_id, normalized_phone)]

    now = datetime.now(timezone.utc)
    for c in convos:
        # Stamp a resume marker so the loop guard ignores history older
        # than this point — otherwise the next inbound could re-trip the
        # heuristic that paused us.
        try:
            meta = dict(c.extra_metadata or {})
            meta["ai_resumed_at"] = now.isoformat()
            c.extra_metadata = meta
            from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
            flag_modified(c, "extra_metadata")
        except Exception:
            pass
        # Clear ONLY the AI pause state. Human takeover columns
        # (needs_human, handoff_active, taken_over_at, taken_over_by,
        # status='human', is_human_handoff, paused_by_human) stay
        # exactly as they were — that's the new contract.
        _resume_ai(db, c, by="dashboard:resume", commit=False)
    db.commit()

    after = [
        {"id": c.id, "ai_paused": bool(c.ai_paused), "reason": c.ai_paused_reason}
        for c in convos
    ]
    _log.info(
        "[AI_RESUME_API] tenant=%s phone_raw=%r normalized=%r "
        "found_conversations=%d before=%s after=%s",
        tenant_id, raw_phone, normalized_phone, len(convos), before, after,
    )
    state = _aggregate_ai_state(convos)
    return {
        "ok": True,
        "customerPhone": normalized_phone,
        "aiPaused": state["ai_paused"],
        "aiPausedReason": state["ai_paused_reason"],
        "aiPausedAt": state["ai_paused_at"],
        "aiPausedBy": state["ai_paused_by"],
        **state,
    }


@router.post("/handoff/return-to-ai")
async def return_handoff_to_ai(body: AIResumeIn, request: Request, db: Session = Depends(get_db)):
    """End a human takeover and hand the conversation back to the AI.

    Distinct from ``/ai-resume`` because it has to clear the entire
    human-takeover state set, not just the AI pause flag:
    ``status``, ``is_human_handoff``, ``paused_by_human``,
    ``needs_human``, ``handoff_active``, ``taken_over_at``,
    ``taken_over_by`` — plus any active ``HandoffSession`` rows — and
    finally ``ai_paused``. The dashboard surfaces this as the "إعادة
    الذكاء" button shown only while the conversation is in a takeover
    state.
    """
    from core.ai_pause_guard import resume_ai as _resume_ai  # noqa: PLC0415
    from handoff.manager import resolve_handoff_session  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    raw_phone = body.customer_phone or ""
    normalized_phone = normalize_phone(raw_phone) or raw_phone

    convos = _find_conversations_for_phone(db, tenant_id, normalized_phone)
    if not convos:
        convos = [_get_or_create_conversation(db, tenant_id, normalized_phone)]

    before = [
        {"id": c.id,
         "needs_human": bool(getattr(c, "needs_human", False)),
         "handoff_active": bool(getattr(c, "handoff_active", False)),
         "is_human_handoff": bool(c.is_human_handoff),
         "ai_paused": bool(c.ai_paused),
         "status": c.status}
        for c in convos
    ]

    now = datetime.now(timezone.utc)
    for c in convos:
        c.is_human_handoff = False
        c.paused_by_human = False
        c.needs_human = False
        c.handoff_active = False
        c.taken_over_at = None
        c.taken_over_by = None
        c.status = "active"
        try:
            meta = dict(c.extra_metadata or {})
            meta["ai_resumed_at"] = now.isoformat()
            meta["handoff_returned_at"] = now.isoformat()
            c.extra_metadata = meta
            from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
            flag_modified(c, "extra_metadata")
        except Exception:
            pass
        db.add(c)
        _resume_ai(db, c, by="dashboard:return_to_ai", commit=False)

    # Resolve any active HandoffSession rows for this customer so the
    # legacy handoff worker doesn't keep treating them as live.
    try:
        digits = _digits_only(normalized_phone)
        suffix = digits[-9:] if len(digits) >= 9 else digits
        for hs in db.query(HandoffSession).filter(
            HandoffSession.tenant_id == tenant_id,
            HandoffSession.status == "active",
        ).all():
            hs_digits = _digits_only(hs.customer_phone or "")
            if hs_digits == digits or (suffix and hs_digits.endswith(suffix)):
                resolve_handoff_session(db, hs.id, tenant_id, resolved_by="dashboard:return_to_ai")
    except Exception as exc:
        _log.debug("[handoff/return-to-ai] HandoffSession resolve failed: %s", exc)

    db.commit()

    after = [
        {"id": c.id,
         "needs_human": bool(getattr(c, "needs_human", False)),
         "handoff_active": bool(getattr(c, "handoff_active", False)),
         "ai_paused": bool(c.ai_paused), "status": c.status}
        for c in convos
    ]
    _log.info(
        "[HANDOFF_RETURN_API] tenant=%s phone_raw=%r normalized=%r rows=%d "
        "before=%s after=%s",
        tenant_id, raw_phone, normalized_phone, len(convos), before, after,
    )
    state = _aggregate_ai_state(convos)
    return {
        "ok": True,
        "customerPhone": normalized_phone,
        "aiPaused": state["ai_paused"],
        "aiPausedReason": state["ai_paused_reason"],
        "aiPausedAt": state["ai_paused_at"],
        "aiPausedBy": state["ai_paused_by"],
        "needsHuman": False,
        "handoffActive": False,
        "takenOverAt": None,
        "takenOverBy": None,
        **state,
    }


@router.post("/mark-read")
async def mark_conversation_read(body: MarkReadIn, request: Request, db: Session = Depends(get_db)):
    """Mark the conversation as read up to "now".

    Stamps ``last_read_at`` on every Conversation row that matches the
    customer phone, so the inbox unread badge zeroes immediately even
    when the merchant only opened the conversation without sending a
    reply. Subsequent inbound messages will bump unread again as they
    arrive (created_at > last_read_at).
    """
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    raw_phone = body.customer_phone or ""
    normalized_phone = normalize_phone(raw_phone) or raw_phone

    convos = _find_conversations_for_phone(db, tenant_id, normalized_phone)
    if not convos:
        return {"ok": True, "customerPhone": normalized_phone, "updated": 0}

    now = datetime.now(timezone.utc)
    for c in convos:
        c.last_read_at = now
        db.add(c)
    db.commit()
    _log.info(
        "[MARK_READ_API] tenant=%s phone=%r normalized=%r rows=%d at=%s",
        tenant_id, raw_phone, normalized_phone, len(convos), now.isoformat(),
    )
    return {
        "ok": True,
        "customerPhone": normalized_phone,
        "updated": len(convos),
        "lastReadAt": now.isoformat(),
    }


@router.get("/blocklist")
async def get_blocklist(request: Request, db: Session = Depends(get_db)):
    from core.ai_pause_guard import list_blocked_numbers as _list_blocked  # noqa: PLC0415
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    return {"numbers": _list_blocked(db, tenant_id)}


@router.post("/blocklist/add")
async def add_to_blocklist(body: BlocklistIn, request: Request, db: Session = Depends(get_db)):
    """Add a phone number to the merchant's AI blocklist. Optionally also
    pauses the matching conversation right away (recommended)."""
    from core.ai_pause_guard import (  # noqa: PLC0415
        add_blocked_number as _add_blocked,
        pause_ai as _pause_ai,
        REASON_INTERNAL_NUMBER as _R_INT,
    )

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    if not (body.phone or "").strip():
        raise HTTPException(status_code=400, detail="phone is required")
    numbers = _add_blocked(db, tenant_id, body.phone)
    target_phone = body.customer_phone or body.phone
    target_norm = normalize_phone(target_phone) or target_phone
    if target_norm:
        try:
            convos = _find_conversations_for_phone(db, tenant_id, target_norm)
            if not convos:
                convos = [_get_or_create_conversation(db, tenant_id, target_norm)]
            for c in convos:
                _pause_ai(db, c, reason=_R_INT, by="dashboard:blocklist", commit=False)
            db.commit()
            _log.info(
                "[AI_PAUSE_API] blocklist_add tenant=%s phone=%r conversations=%d",
                tenant_id, target_norm, len(convos),
            )
        except Exception as exc:
            _log.debug("[ai_pause] blocklist add — convo pause failed: %s", exc)
    return {"ok": True, "numbers": numbers}


@router.post("/blocklist/remove")
async def remove_from_blocklist(body: BlocklistIn, request: Request, db: Session = Depends(get_db)):
    from core.ai_pause_guard import remove_blocked_number as _rem_blocked  # noqa: PLC0415
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    numbers = _rem_blocked(db, tenant_id, body.phone)
    return {"ok": True, "numbers": numbers}
