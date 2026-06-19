"""SQL conversation counts per inbox filter tab.

Chip badges MUST NOT be derived from the paginated client list — they
use ``compute_conversation_filter_counts`` which runs the same WHERE
clauses as ``build_conversation_filter_clauses`` (shared with
``GET /conversations?filter=…``).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from models import Conversation, Customer, MessageEvent
from services.manual_segments import marketing_opt_out_manual_sql_truthy

CONVERSATION_FILTER_SLUGS: tuple[str, ...] = (
    "active",
    "human",
    "agent_req",
    "paused",
    "blocked",
    "paid",
    "unsubscribed",
    "campaign_excluded",
    "closed",
)


@dataclass(frozen=True)
class ConversationFilterContext:
    active_handoffs: Dict[str, str]
    blocked_digits: Set[str]
    open_window_phones: Set[str]
    window_cutoff: datetime


def _phone_variants(digits: Set[str]) -> List[str]:
    out: Set[str] = set()
    for d in digits:
        if not d:
            continue
        out.add(d)
        out.add(f"+{d}")
    return list(out)


def _customer_unsubscribed_clause(now_iso: str):
    from sqlalchemy import String, cast  # noqa: PLC0415

    is_unsub = cast(Customer.extra_metadata.op("->>")("is_unsubscribed"), String)
    is_pending = cast(Customer.extra_metadata.op("->>")("pending_unsubscribe"), String)
    pending_exp = cast(
        Customer.extra_metadata.op("->>")("pending_unsubscribe_expires_at"), String,
    )
    unsub = or_(is_unsub == "true", is_unsub == "1")
    pending = and_(
        or_(is_pending == "true", is_pending == "1"),
        pending_exp > now_iso,
    )
    return Conversation.customer.has(or_(unsub, pending))


def _customer_not_unsubscribed_clause(now_iso: str):
    from sqlalchemy import String, cast  # noqa: PLC0415

    is_unsub = cast(Customer.extra_metadata.op("->>")("is_unsubscribed"), String)
    is_pending = cast(Customer.extra_metadata.op("->>")("pending_unsubscribe"), String)
    pending_exp = cast(
        Customer.extra_metadata.op("->>")("pending_unsubscribe_expires_at"), String,
    )
    not_unsub = or_(
        is_unsub.is_(None),
        and_(is_unsub != "true", is_unsub != "1"),
    )
    not_pending = or_(
        is_pending.is_(None),
        and_(is_pending != "true", is_pending != "1"),
        pending_exp.is_(None),
        pending_exp <= now_iso,
    )
    return Conversation.customer.has(and_(not_unsub, not_pending))


def _human_takeover_or_clauses(ctx: ConversationFilterContext) -> list:
    handoff_or = [
        Conversation.is_human_handoff.is_(True),
        Conversation.needs_human.is_(True),
        Conversation.handoff_active.is_(True),
        Conversation.taken_over_at.isnot(None),
        func.lower(Conversation.status) == "human",
    ]
    if ctx.active_handoffs:
        variants = _phone_variants(set(ctx.active_handoffs.keys()))
        if variants:
            handoff_or.append(
                Conversation.customer.has(
                    or_(
                        Customer.normalized_phone.in_(variants),
                        Customer.phone.in_(variants),
                    )
                )
            )
    return handoff_or


def _blocked_or_clauses(ctx: ConversationFilterContext) -> list:
    blocked_clauses: list = []
    if ctx.blocked_digits:
        variants = _phone_variants(set(ctx.blocked_digits))
        if variants:
            blocked_clauses.append(
                Conversation.customer.has(
                    or_(
                        Customer.normalized_phone.in_(variants),
                        Customer.phone.in_(variants),
                    )
                )
            )
    blocked_clauses.append(
        and_(
            Conversation.ai_paused.is_(True),
            func.lower(Conversation.ai_paused_reason) == "internal_number",
        )
    )
    return blocked_clauses


def _agent_req_not_manual_clause(db: Session, tenant_id: int):
    """Exclude rows whose latest message is an outbound manual reply."""
    last_ts_sq = (
        db.query(
            MessageEvent.conversation_id.label("cid"),
            func.max(MessageEvent.created_at).label("mx"),
        )
        .filter(MessageEvent.tenant_id == tenant_id)
        .group_by(MessageEvent.conversation_id)
        .subquery()
    )
    manual_last = (
        db.query(MessageEvent.conversation_id)
        .join(
            last_ts_sq,
            and_(
                MessageEvent.conversation_id == last_ts_sq.c.cid,
                MessageEvent.created_at == last_ts_sq.c.mx,
            ),
        )
        .filter(
            MessageEvent.tenant_id == tenant_id,
            func.lower(MessageEvent.direction) == "outbound",
            func.lower(MessageEvent.event_type) == "manual_reply",
        )
    )
    return ~Conversation.id.in_(manual_last)


def build_conversation_filter_clauses(
    filter_slug: str,
    ctx: ConversationFilterContext,
    *,
    tenant_id: int,
    db: Optional[Session] = None,
) -> List:
    """Return extra WHERE clauses for ``Conversation`` list + COUNT."""
    slug = (filter_slug or "all").strip().lower()
    extra: List = []
    now_iso = datetime.now(timezone.utc).isoformat()

    if slug in ("human", "agent_req"):
        extra.append(or_(*_human_takeover_or_clauses(ctx)))
        if slug == "agent_req" and db is not None:
            extra.append(_agent_req_not_manual_clause(db, tenant_id))
    elif slug == "closed":
        extra.append(func.lower(Conversation.status) == "closed")
    elif slug == "paused":
        extra.extend([
            Conversation.ai_paused.is_(True),
            Conversation.is_human_handoff.is_(False),
            Conversation.needs_human.is_(False),
            Conversation.handoff_active.is_(False),
            or_(
                Conversation.ai_paused_reason.is_(None),
                func.lower(Conversation.ai_paused_reason) != "internal_number",
            ),
        ])
    elif slug == "blocked":
        extra.append(or_(*_blocked_or_clauses(ctx)))
    elif slug == "paid":
        extra.append(Conversation.last_payment_confirmed_at.isnot(None))
    elif slug == "campaign_excluded":
        extra.append(
            Conversation.customer.has(marketing_opt_out_manual_sql_truthy())
        )
    elif slug == "unsubscribed":
        extra.append(_customer_unsubscribed_clause(now_iso))
    elif slug == "active":
        not_unsub = _customer_not_unsubscribed_clause(now_iso)
        window_parts: list = []
        window_vars = _phone_variants(ctx.open_window_phones)
        if window_vars:
            window_parts.append(
                Conversation.customer.has(
                    or_(
                        Customer.normalized_phone.in_(window_vars),
                        Customer.phone.in_(window_vars),
                    )
                )
            )
        recent = exists(
            select(1).where(
                MessageEvent.conversation_id == Conversation.id,
                MessageEvent.tenant_id == tenant_id,
                MessageEvent.created_at >= ctx.window_cutoff,
            )
        )
        window_parts.append(recent)
        extra.append(and_(not_unsub, or_(*window_parts)))

    return extra


def count_conversations_for_filter(
    db: Session,
    tenant_id: int,
    filter_slug: str,
    ctx: ConversationFilterContext,
) -> int:
    clauses = build_conversation_filter_clauses(
        filter_slug, ctx, tenant_id=tenant_id, db=db,
    )
    return (
        db.query(func.count(Conversation.id))
        .filter(Conversation.tenant_id == tenant_id, *clauses)
        .scalar()
    ) or 0


def compute_conversation_filter_counts(
    db: Session,
    tenant_id: int,
    ctx: ConversationFilterContext,
) -> Dict[str, int]:
    return {
        slug: count_conversations_for_filter(db, tenant_id, slug, ctx)
        for slug in CONVERSATION_FILTER_SLUGS
    }


__all__ = [
    "CONVERSATION_FILTER_SLUGS",
    "ConversationFilterContext",
    "build_conversation_filter_clauses",
    "compute_conversation_filter_counts",
    "count_conversations_for_filter",
]
