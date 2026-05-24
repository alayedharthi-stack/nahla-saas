"""
tests/test_admin_inbound_debug_timestamps.py
─────────────────────────────────────────────
Pin the May 2026 #42 changes to ``/admin/inbound/recent``:

The merchant reported that the dashboard rendered events from "24
May" while the actual customer complaint was on "25 May" KSA. Root
cause was that the endpoint returned only ``created_at`` (UTC) and
the dashboard was interpreting it as local time. The fix exposes
BOTH timestamp views explicitly:

  * ``event_created_at_utc`` — server-canonical (UTC).
  * ``event_created_at_ksa`` — Asia/Riyadh (UTC+03:00, year-round).
  * ``source_table``         — `message_events` vs `ai_quality_events`
    so downstream tooling can join correctly.

These tests work on the pure summariser helpers + the timezone math
so we don't need a live DB / FastAPI app to verify the contract.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


_BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _msg_event_row(*, created_at, body="hi", direction="inbound", meta=None):
    return SimpleNamespace(
        id=42,
        tenant_id=33,
        conversation_id=123,
        direction=direction,
        body=body,
        created_at=created_at,
        extra_metadata=meta or {},
    )


def _drop_event_row(*, created_at, mismatch_type="normalizer_exception"):
    return SimpleNamespace(
        id=99,
        tenant_id=33,
        conversation_id=None,
        direction="inbound",
        inbound_preview="video msg",
        chosen_path="path=normalize_whatsapp_inbound_raised",
        mismatch_type=mismatch_type,
        mismatch_reason="ValueError: eta-1",
        category="media_failure",
        customer_phone_masked="966***678",
        created_at=created_at,
    )


# ────────────────────────────────────────────────────────────────────
# Timestamp serialisation contract
# ────────────────────────────────────────────────────────────────────


def test_summary_exposes_utc_and_ksa_iso_strings_for_message_event() -> None:
    """A naive UTC timestamp must surface as both UTC ISO and a KSA
    ISO offset so the dashboard can show 'الساعة بتوقيت السعودية'
    next to the raw UTC value."""
    from routers.admin_inbound_debug import _summarise_message_event

    # 2026-05-24 23:30 UTC == 2026-05-25 02:30 KSA
    ts = datetime(2026, 5, 24, 23, 30, 0, tzinfo=timezone.utc)
    summary = _summarise_message_event(_msg_event_row(created_at=ts))

    assert summary["source_table"] == "message_events"
    assert summary["event_created_at_utc"].startswith("2026-05-24T23:30:00")
    assert summary["event_created_at_utc"].endswith("+00:00")
    # KSA is +03:00 → calendar date rolls to 25 May, exact issue the
    # merchant flagged ("24 vs 25 May" confusion).
    assert summary["event_created_at_ksa"].startswith("2026-05-25T02:30:00")
    assert "+03:00" in summary["event_created_at_ksa"]
    # Backwards-compat ``created_at`` must still be present for
    # existing dashboard wiring; same UTC value as event_created_at_utc.
    assert summary["created_at"] == summary["event_created_at_utc"]


def test_summary_exposes_utc_and_ksa_iso_strings_for_drop_event() -> None:
    from routers.admin_inbound_debug import _summarise_drop_event

    ts = datetime(2026, 5, 25, 11, 6, 0, tzinfo=timezone.utc)  # 14:06 KSA
    summary = _summarise_drop_event(_drop_event_row(created_at=ts))

    assert summary["source_table"] == "ai_quality_events"
    assert summary["event_created_at_utc"].startswith("2026-05-25T11:06:00")
    assert summary["event_created_at_ksa"].startswith("2026-05-25T14:06:00")
    assert summary["category"] == "media_failure"
    assert summary["drop_reason"] == "normalizer_exception"


def test_summary_handles_naive_datetime_as_utc() -> None:
    """A SQLAlchemy column may return a naive datetime depending on
    driver / dialect. We treat it as UTC (the platform contract) so
    the KSA offset still works."""
    from routers.admin_inbound_debug import _summarise_message_event

    naive = datetime(2026, 5, 25, 10, 0, 0)  # naive == UTC by convention
    summary = _summarise_message_event(_msg_event_row(created_at=naive))

    assert summary["event_created_at_utc"].startswith("2026-05-25T10:00:00")
    assert summary["event_created_at_ksa"].startswith("2026-05-25T13:00:00")


def test_summary_handles_missing_created_at() -> None:
    """``created_at`` may be ``None`` in adversarial test fixtures.
    The summariser must return empty strings, never raise."""
    from routers.admin_inbound_debug import _summarise_message_event

    summary = _summarise_message_event(_msg_event_row(created_at=None))
    assert summary["event_created_at_utc"] == ""
    assert summary["event_created_at_ksa"] == ""
    assert summary["source_table"] == "message_events"


# ────────────────────────────────────────────────────────────────────
# Constants pinned for downstream consumers
# ────────────────────────────────────────────────────────────────────


def test_ksa_offset_is_plus_3_year_round() -> None:
    """KSA does not observe DST. The offset must stay +03:00 every
    month of the year — we explicitly avoid zoneinfo lookups so the
    test passes on Windows builds without the IANA database."""
    from routers.admin_inbound_debug import KSA_UTC_OFFSET

    for month in (1, 4, 7, 10, 12):
        ts = datetime(2026, month, 15, 12, 0, 0, tzinfo=timezone.utc)
        ksa = ts.astimezone(KSA_UTC_OFFSET)
        assert ksa.utcoffset() == timedelta(hours=3), (
            f"KSA offset must be +03:00 in month {month}"
        )


def test_helper_functions_match_summary_output() -> None:
    """The helper-level renderers must agree with what the summariser
    embeds — guards against accidental divergence between the two."""
    from routers.admin_inbound_debug import (
        _summarise_message_event,
        _to_utc_iso,
        _to_ksa_iso,
    )

    ts = datetime(2026, 5, 25, 11, 6, 0, tzinfo=timezone.utc)
    summary = _summarise_message_event(_msg_event_row(created_at=ts))

    assert summary["event_created_at_utc"] == _to_utc_iso(ts)
    assert summary["event_created_at_ksa"] == _to_ksa_iso(ts)
