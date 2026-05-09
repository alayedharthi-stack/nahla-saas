"""Unit tests for WhatsApp AI live cutoff (historical inbound guard)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "database"))

from core.whatsapp_ai_live import (
    is_inbound_before_ai_live_since,
    parse_whatsapp_message_timestamp_utc,
    stamp_whatsapp_ai_live_since_if_empty,
)


def test_parse_whatsapp_timestamp():
    now = datetime.now(timezone.utc)
    raw = str(int(now.timestamp()))
    got = parse_whatsapp_message_timestamp_utc(raw)
    assert got is not None
    assert abs((got - now).total_seconds()) < 2


def test_message_before_cutoff_is_historical():
    cutoff = datetime.now(timezone.utc)
    conn = type("WC", (), {"whatsapp_ai_live_since": cutoff})()
    past = cutoff - timedelta(hours=1)
    assert is_inbound_before_ai_live_since(conn, past) is True


def test_message_after_cutoff_is_live():
    cutoff = datetime.now(timezone.utc)
    conn = type("WC", (), {"whatsapp_ai_live_since": cutoff})()
    fut = cutoff + timedelta(seconds=1)
    assert is_inbound_before_ai_live_since(conn, fut) is False


def test_missing_ts_never_marked_historical():
    cutoff = datetime.now(timezone.utc)
    conn = type("WC", (), {"whatsapp_ai_live_since": cutoff})()
    assert is_inbound_before_ai_live_since(conn, None) is False


def test_stamp_only_once():
    conn = type("WC", (), {"whatsapp_ai_live_since": None})()
    stamp_whatsapp_ai_live_since_if_empty(conn)
    assert conn.whatsapp_ai_live_since is not None
    first = conn.whatsapp_ai_live_since
    stamp_whatsapp_ai_live_since_if_empty(conn)
    assert conn.whatsapp_ai_live_since == first
