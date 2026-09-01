"""Real Handoff Slice 1 — ownership resolver + idle recovery."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.ai_pause_guard import REASON_HUMAN_HANDOFF, REASON_MANUAL_PAUSE
from core.ownership_state import (
    OWNERSHIP_AI_PRIMARY,
    OWNERSHIP_HUMAN_ACTIVE,
    OWNERSHIP_HUMAN_REQUESTED,
    TAKEOVER_EXPLICIT,
    attempt_implicit_takeover_recovery,
    conversation_handoff_active,
    is_explicit_takeover,
    release_implicit_takeover,
    resolve_ownership_state,
)


def _now() -> datetime:
    return datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)


class _Msg:
    def __init__(
        self,
        *,
        direction: str,
        created_at: datetime,
        event_type: str = "",
        extra_metadata: dict | None = None,
    ) -> None:
        self.direction = direction
        self.created_at = created_at
        self.event_type = event_type
        self.extra_metadata = extra_metadata or {}


class _FakeQuery:
    def __init__(self, rows: list) -> None:
        self._rows = list(rows)

    def filter(self, *_args, **_kwargs) -> _FakeQuery:
        return self

    def order_by(self, *_args, **_kwargs) -> _FakeQuery:
        return self

    def limit(self, *_args, **_kwargs) -> _FakeQuery:
        return self

    def first(self):
        inbounds = sorted(
            [r for r in self._rows if r.direction == "inbound"],
            key=lambda r: r.created_at,
            reverse=True,
        )
        return inbounds[0] if inbounds else None

    def all(self):
        return sorted(
            [r for r in self._rows if r.direction == "outbound"],
            key=lambda r: r.created_at,
            reverse=True,
        )


class _FakeDB:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def query(self, _model) -> _FakeQuery:
        return _FakeQuery(self._rows)


def _convo(**kwargs):
    base = dict(
        id=1,
        tenant_id=1,
        ai_paused=False,
        ai_paused_reason=None,
        needs_human=False,
        handoff_active=False,
        is_human_handoff=False,
        status="active",
        paused_by_human=False,
        taken_over_at=None,
        taken_over_by=None,
        extra_metadata={},
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_advisory_flags_only_human_requested() -> None:
    convo = _convo(needs_human=True, handoff_active=True, is_human_handoff=True)
    result = resolve_ownership_state(_FakeDB([]), convo, now=_now())
    assert result.state == OWNERSHIP_HUMAN_REQUESTED


def test_explicit_handoff_ai_paused_is_active() -> None:
    convo = _convo(
        ai_paused=True,
        ai_paused_reason=REASON_HUMAN_HANDOFF,
        paused_by_human=True,
        taken_over_at=_now(),
        taken_over_by="dashboard:handoff",
    )
    assert is_explicit_takeover(convo) is True
    result = resolve_ownership_state(_FakeDB([]), convo, now=_now())
    assert result.state == OWNERSHIP_HUMAN_ACTIVE
    assert result.takeover_class == TAKEOVER_EXPLICIT


def test_manual_pause_is_not_explicit_takeover() -> None:
    convo = _convo(ai_paused=True, ai_paused_reason=REASON_MANUAL_PAUSE)
    assert is_explicit_takeover(convo) is False


def test_implicit_recent_staff_is_not_ai_owner() -> None:
    staff_at = _now() - timedelta(minutes=5)
    convo = _convo(
        paused_by_human=True,
        taken_over_at=staff_at,
        taken_over_by="user:42",
    )
    rows = [
        _Msg(
            direction="outbound",
            created_at=staff_at,
            event_type="manual_reply",
        ),
    ]
    result = resolve_ownership_state(_FakeDB(rows), convo, now=_now())
    assert result.state == OWNERSHIP_AI_PRIMARY
    assert conversation_handoff_active(
        _FakeDB(rows), convo, now=_now(),
    ) is False


def test_implicit_idle_customer_waiting_stays_advisory() -> None:
    staff_at = _now() - timedelta(minutes=20)
    convo = _convo(
        paused_by_human=True,
        taken_over_at=staff_at,
        taken_over_by="user:42",
        needs_human=True,
        handoff_active=True,
    )
    rows = [
        _Msg(direction="outbound", created_at=staff_at, event_type="manual_reply"),
        _Msg(direction="inbound", created_at=_now() - timedelta(minutes=1)),
    ]
    result = resolve_ownership_state(
        _FakeDB(rows), convo, now=_now(), assume_current_inbound=True,
    )
    assert result.state == OWNERSHIP_HUMAN_REQUESTED
    assert result.customer_waiting_after_staff is True


def test_idle_recovery_does_not_auto_release() -> None:
    staff_at = _now() - timedelta(minutes=20)
    convo = _convo(
        paused_by_human=True,
        taken_over_at=staff_at,
        taken_over_by="user:42",
        needs_human=True,
        handoff_active=True,
        status="human",
    )
    rows = [
        _Msg(direction="outbound", created_at=staff_at, event_type="manual_reply"),
        _Msg(direction="inbound", created_at=_now() - timedelta(minutes=1)),
    ]
    db = _FakeDB(rows)
    recovery = attempt_implicit_takeover_recovery(
        db, convo, now=_now(), assume_current_inbound=True,
    )
    assert recovery.released is False
    assert recovery.reason == "ttl_does_not_control_ai"
    assert convo.paused_by_human is True
    assert convo.taken_over_at is not None
    assert convo.needs_human is True
    after = resolve_ownership_state(db, convo, now=_now())
    assert after.state == OWNERSHIP_HUMAN_REQUESTED


def test_explicit_takeover_never_auto_recovers() -> None:
    staff_at = _now() - timedelta(minutes=20)
    convo = _convo(
        ai_paused=True,
        ai_paused_reason=REASON_HUMAN_HANDOFF,
        paused_by_human=True,
        taken_over_at=staff_at,
        taken_over_by="dashboard:handoff",
    )
    recovery = attempt_implicit_takeover_recovery(
        _FakeDB([]), convo, now=_now(), assume_current_inbound=True,
    )
    assert recovery.released is False
    assert convo.taken_over_at is not None


def test_conversation_handoff_active_false_after_idle() -> None:
    staff_at = _now() - timedelta(minutes=20)
    convo = _convo(
        paused_by_human=True,
        taken_over_at=staff_at,
        taken_over_by="user:42",
    )
    db = _FakeDB([
        _Msg(direction="outbound", created_at=staff_at, event_type="manual_reply"),
    ])
    assert conversation_handoff_active(
        db, convo, now=_now(), assume_current_inbound=True,
    ) is False


def test_conversation_handoff_active_false_within_ttl() -> None:
    staff_at = _now() - timedelta(minutes=2)
    convo = _convo(
        paused_by_human=True,
        taken_over_at=staff_at,
        taken_over_by="user:42",
    )
    db = _FakeDB([
        _Msg(direction="outbound", created_at=staff_at, event_type="manual_reply"),
    ])
    assert conversation_handoff_active(
        db, convo, now=_now(), assume_current_inbound=True,
    ) is False


def test_release_implicit_preserves_queue_flags() -> None:
    convo = _convo(
        paused_by_human=True,
        taken_over_at=_now(),
        taken_over_by="user:1",
        needs_human=True,
        handoff_active=True,
        status="human",
    )
    audit = release_implicit_takeover(convo, now=_now())
    assert convo.paused_by_human is False
    assert convo.needs_human is True
    assert audit["ownership_release_reason"] == "staff_idle_ttl"


def test_clean_conversation_ai_primary() -> None:
    result = resolve_ownership_state(_FakeDB([]), _convo(), now=_now())
    assert result.state == OWNERSHIP_AI_PRIMARY


def test_mode_resolver_gate_false_after_release() -> None:
    from modules.ai.routing.conversation_mode import _conversation_handoff_flag

    staff_at = _now() - timedelta(minutes=20)
    convo = _convo(
        paused_by_human=True,
        taken_over_at=staff_at,
        taken_over_by="user:42",
    )
    db = _FakeDB([
        _Msg(direction="outbound", created_at=staff_at, event_type="manual_reply"),
    ])
    release_implicit_takeover(convo, now=_now())
    assert _conversation_handoff_flag(convo, db, now=_now()) is False
