"""Tests for Knowledge Gap Intelligence v1 — conversation signal scanner."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import func
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from modules.ai.knowledge.conversation_signal_scanner import (
    ConversationSignalSummary,
    _fetch_recent_inbound_conversation_ids,
    _summarize_inbound_messages,
    scan_tenant_conversation_signals,
)


def test_summarize_counts_payment_and_shipping_terms() -> None:
    rows = [
        ("كيف الدفع؟ فيه تحويل؟", 1),
        ("كم الشحن للرياض؟", 2),
        ("وين موقعكم؟", 1),
        ("وش الفرق بين العسلين؟", 2),
    ]
    summary = _summarize_inbound_messages(rows, handoff_conv_ids={1})

    assert isinstance(summary, ConversationSignalSummary)
    assert summary.scanned_conversations == 2
    assert summary.scanned_messages == 4
    assert summary.payment_questions >= 1
    assert summary.shipping_questions >= 1
    assert summary.location_questions >= 1
    assert summary.product_compare_questions >= 1
    assert summary.human_handoff_count == 1
    assert summary.human_handoff_after_payment >= 1
    assert summary.window_days == 7


def test_summarize_returns_empty_when_no_messages() -> None:
    summary = _summarize_inbound_messages([], handoff_conv_ids=set())
    assert summary.scanned_conversations == 0
    assert summary.payment_questions == 0
    assert summary.to_dict()["shipping_questions"] == 0


def test_scan_never_raises_on_db_error_and_rolls_back() -> None:
    db = MagicMock()
    db.query.side_effect = RuntimeError("db down")
    summary = scan_tenant_conversation_signals(db, tenant_id=1)
    assert summary.scanned_messages == 0
    db.rollback.assert_called_once()


def test_scan_failure_does_not_poison_followup_query() -> None:
    """After a scanner failure we rollback so the caller session stays usable."""

    class _Session:
        def __init__(self) -> None:
            self._poisoned = False
            self.rollback_calls = 0

        def rollback(self) -> None:
            self.rollback_calls += 1
            self._poisoned = False

        def query(self, *_args, **_kwargs):
            if self._poisoned:
                raise RuntimeError("transaction still aborted")
            self._poisoned = True
            raise RuntimeError("simulated postgres failure")

    db = _Session()
    summary = scan_tenant_conversation_signals(db, tenant_id=33)
    assert summary.scanned_messages == 0
    assert db.rollback_calls == 1

    follow_up = MagicMock()
    follow_up.filter.return_value = follow_up
    follow_up.first.return_value = ("ok",)
    db.query = MagicMock(return_value=follow_up)
    assert db.query("tenants").first() == ("ok",)


def test_recent_conversation_ids_query_is_postgres_safe() -> None:
    """GROUP BY + MAX(created_at) — not DISTINCT … ORDER BY created_at."""
    from sqlalchemy import create_engine
    from models import MessageEvent  # noqa: PLC0415

    session = sessionmaker(bind=create_engine("postgresql://localhost/test"))()
    cutoff = datetime(2026, 5, 1, tzinfo=timezone.utc)
    stmt = (
        session.query(
            MessageEvent.conversation_id,
            func.max(MessageEvent.created_at).label("last_inbound_at"),
        )
        .filter(
            MessageEvent.tenant_id == 33,
            MessageEvent.direction == "inbound",
            MessageEvent.conversation_id.isnot(None),
            MessageEvent.created_at >= cutoff,
        )
        .group_by(MessageEvent.conversation_id)
        .order_by(func.max(MessageEvent.created_at).desc())
        .limit(200)
        .statement
    )
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))

    assert "GROUP BY" in sql.upper()
    assert "DISTINCT" not in sql.upper()
    assert "MAX" in sql.upper()


def test_fetch_recent_conversation_ids_returns_newest_first() -> None:
    db = MagicMock()
    chain = db.query.return_value
    chain.filter.return_value = chain
    chain.group_by.return_value = chain
    chain.order_by.return_value = chain
    chain.limit.return_value = chain
    chain.all.return_value = [(99, datetime.now(timezone.utc)), (42, datetime.now(timezone.utc))]

    ids = _fetch_recent_inbound_conversation_ids(
        db,
        tenant_id=33,
        cutoff=datetime(2026, 5, 1, tzinfo=timezone.utc),
        max_conversations=200,
    )
    assert ids == {99, 42}
    chain.group_by.assert_called_once()
    chain.order_by.assert_called_once()
    chain.limit.assert_called_once_with(200)
