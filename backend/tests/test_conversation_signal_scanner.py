"""Tests for Knowledge Gap Intelligence v1 — conversation signal scanner."""
from __future__ import annotations

from unittest.mock import MagicMock

from modules.ai.knowledge.conversation_signal_scanner import (
    ConversationSignalSummary,
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


def test_scan_never_raises_on_db_error() -> None:
    db = MagicMock()
    db.query.side_effect = RuntimeError("db down")
    summary = scan_tenant_conversation_signals(db, tenant_id=1)
    assert summary.scanned_messages == 0
