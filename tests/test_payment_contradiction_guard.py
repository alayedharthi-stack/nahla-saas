"""
tests/test_payment_contradiction_guard.py
─────────────────────────────────────────
Wave 1 W1.1 — Payment / Receipt Integrity Stabilization, commit 1.

These tests pin the two structural fixes shipped by W1.1:

  A) ``mark_awaiting_receipt`` must REFUSE to flip
     ``awaiting_payment_receipt=True`` when the persisted brain state
     already records a recent ``payment_receipt_received=True``. This
     closes the production complaint where the bot said
     "وصلني الإيصال" and then "أرسل لي الإيصال" inside the same beat.

  B) ``OrderPreparationState`` must round-trip
     ``payment_claim_unverified*`` across ``to_dict`` / ``from_dict``
     so :class:`brain.state.store.DefaultStateStore.save` (which
     replaces ``brain_state`` with ``state.to_dict()`` on every turn)
     cannot silently drop the understanding flag.

Both behaviours sit behind the kill switch
``PAYMENT_CONTRADICTION_GUARD_ENABLED`` for (A); for (B) the
dataclass change is unconditional because it's an additive,
default-False field set with no observable behaviour change unless
the flag has already been stamped by the text-claim helper.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ── Helpers ──────────────────────────────────────────────────────────


class _FakeConv:
    def __init__(self, conv_id: int = 909, brain_state: Optional[Dict[str, Any]] = None):
        self.id = conv_id
        self.extra_metadata = {"brain_state": brain_state or {}}


def _state(
    received: bool = False,
    received_at: Optional[str] = None,
) -> Dict[str, Any]:
    op: Dict[str, Any] = {}
    if received:
        op["payment_receipt_received"] = True
        if received_at is not None:
            op["payment_receipt_at"] = received_at
    return {"order_prep": op}


def _iso_minutes_ago(minutes: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes)
    ).isoformat()


# ── Section A — mark_awaiting_receipt contradiction guard ───────────


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip the kill switch by default so each test sets its own."""
    monkeypatch.delenv("PAYMENT_CONTRADICTION_GUARD_ENABLED", raising=False)


def test_guard_kill_switch_default_off() -> None:
    from core.order_flow import _payment_contradiction_guard_enabled
    assert _payment_contradiction_guard_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_guard_kill_switch_truthy(
    monkeypatch: pytest.MonkeyPatch, val: str,
) -> None:
    from core.order_flow import _payment_contradiction_guard_enabled
    monkeypatch.setenv("PAYMENT_CONTRADICTION_GUARD_ENABLED", val)
    assert _payment_contradiction_guard_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_guard_kill_switch_falsy(
    monkeypatch: pytest.MonkeyPatch, val: str,
) -> None:
    from core.order_flow import _payment_contradiction_guard_enabled
    monkeypatch.setenv("PAYMENT_CONTRADICTION_GUARD_ENABLED", val)
    assert _payment_contradiction_guard_enabled() is False


# Recency window helper


def test_recency_helper_returns_true_for_missing_iso() -> None:
    """Defensive: a confirmed receipt with no ``payment_receipt_at``
    must NEVER be downgraded to "old enough to overwrite". Empty
    strings, ``None``, and unparseable text all map to True."""
    from core.order_flow import _receipt_received_recently
    assert _receipt_received_recently("") is True
    assert _receipt_received_recently("not-an-iso") is True


def test_recency_helper_inside_window_returns_true() -> None:
    from core.order_flow import _receipt_received_recently
    assert _receipt_received_recently(_iso_minutes_ago(5)) is True
    assert _receipt_received_recently(_iso_minutes_ago(29)) is True


def test_recency_helper_outside_window_returns_false() -> None:
    from core.order_flow import _receipt_received_recently
    assert _receipt_received_recently(_iso_minutes_ago(45)) is False
    assert _receipt_received_recently(_iso_minutes_ago(60 * 24)) is False


def test_recency_helper_handles_naive_iso_as_utc() -> None:
    """A timestamp without timezone is treated as UTC so legacy rows
    written before the timezone-aware migration still parse."""
    from core.order_flow import _receipt_received_recently
    naive = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    ).isoformat()
    assert _receipt_received_recently(naive) is True


def test_recency_helper_handles_z_suffix() -> None:
    from core.order_flow import _receipt_received_recently
    iso_with_z = (
        datetime.now(timezone.utc) - timedelta(minutes=5)
    ).isoformat().replace("+00:00", "Z")
    assert _receipt_received_recently(iso_with_z) is True


# Guard wired into mark_awaiting_receipt


def _patch_helpers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    brain_state: Dict[str, Any],
    apply_recorder: List[Dict[str, Any]],
) -> None:
    """Stub ``_load_brain_state`` and ``apply_state_patch`` so the
    test can inspect both the guard's read path and the legacy
    flip path without touching SQLAlchemy."""
    from core import order_flow

    monkeypatch.setattr(
        order_flow,
        "_load_brain_state",
        lambda *_a, **_k: (_FakeConv(), brain_state),
    )

    def _record_apply(*_a: Any, **kw: Any) -> bool:
        apply_recorder.append(dict(kw))
        return True

    monkeypatch.setattr(order_flow, "apply_state_patch", _record_apply)


def test_mark_awaiting_receipt_blocks_after_recent_receipt(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Headline: receipt confirmed 5 minutes ago → bot replies with
    ACK containing "إيصال" → keyword scan would normally call
    ``mark_awaiting_receipt`` → guard refuses the flip and emits
    ``[PAYMENT_CONTRADICTION_GUARD]``."""
    monkeypatch.setenv("PAYMENT_CONTRADICTION_GUARD_ENABLED", "1")
    caplog.set_level(logging.INFO, logger="nahla.order_flow")
    apply_recorder: List[Dict[str, Any]] = []
    _patch_helpers(
        monkeypatch,
        brain_state=_state(received=True, received_at=_iso_minutes_ago(5)),
        apply_recorder=apply_recorder,
    )

    from core.order_flow import mark_awaiting_receipt
    flipped = mark_awaiting_receipt(
        db=object(), tenant_id=33, phone="+966500000777",
    )

    assert flipped is False, (
        "Guard must refuse the flip when the receipt was just confirmed."
    )
    assert apply_recorder == [], (
        "apply_state_patch must NOT run when the guard blocks the flip."
    )
    msgs = [r.getMessage() for r in caplog.records]
    line = next(m for m in msgs if "[PAYMENT_CONTRADICTION_GUARD]" in m)
    assert "decision=block_awaiting_flip" in line
    assert "reason=recent_receipt_received_blocks_awaiting_flip" in line
    assert "tenant_id=33" in line
    assert "*0777" in line  # masked phone
    assert "+966500000777" not in line  # full phone never logged


def test_mark_awaiting_receipt_blocks_when_received_without_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``payment_receipt_received=True`` with no
    ``payment_receipt_at`` is still a confirmed receipt — the guard
    must refuse the flip rather than override a confirmed state
    just because the ISO field is missing."""
    monkeypatch.setenv("PAYMENT_CONTRADICTION_GUARD_ENABLED", "1")
    apply_recorder: List[Dict[str, Any]] = []
    _patch_helpers(
        monkeypatch,
        brain_state=_state(received=True, received_at=None),
        apply_recorder=apply_recorder,
    )

    from core.order_flow import mark_awaiting_receipt
    assert mark_awaiting_receipt(
        db=object(), tenant_id=33, phone="+966500000777",
    ) is False
    assert apply_recorder == []


def test_mark_awaiting_receipt_flips_when_receipt_was_long_ago(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A receipt confirmed 2 hours ago is OUTSIDE the 30-minute
    window. The guard does not interfere — the legacy flip runs."""
    monkeypatch.setenv("PAYMENT_CONTRADICTION_GUARD_ENABLED", "1")
    apply_recorder: List[Dict[str, Any]] = []
    _patch_helpers(
        monkeypatch,
        brain_state=_state(received=True, received_at=_iso_minutes_ago(120)),
        apply_recorder=apply_recorder,
    )

    from core.order_flow import mark_awaiting_receipt
    assert mark_awaiting_receipt(
        db=object(), tenant_id=33, phone="+966500000777",
    ) is True
    assert len(apply_recorder) == 1
    patch = apply_recorder[0]["state_patch"]
    assert patch["awaiting_payment_receipt"] is True
    assert patch["order_status"] == "awaiting_receipt"


def test_mark_awaiting_receipt_flips_when_no_prior_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``payment_receipt_received`` in state → guard inert →
    legacy flip runs. This is the common path: a fresh customer
    asks for the IBAN, the bot ships the receipt-ask copy, and we
    must remember to expect the receipt."""
    monkeypatch.setenv("PAYMENT_CONTRADICTION_GUARD_ENABLED", "1")
    apply_recorder: List[Dict[str, Any]] = []
    _patch_helpers(
        monkeypatch,
        brain_state=_state(received=False),
        apply_recorder=apply_recorder,
    )

    from core.order_flow import mark_awaiting_receipt
    assert mark_awaiting_receipt(
        db=object(), tenant_id=33, phone="+966500000777",
    ) is True
    assert len(apply_recorder) == 1


def test_mark_awaiting_receipt_legacy_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kill switch off → behaviour byte-identical to the pre-guard
    implementation, EVEN IF a recent receipt is on file. We pin
    this so the staged rollout's "flag off" surface is an exact
    legacy preserve, not a half-applied guard."""
    monkeypatch.delenv(
        "PAYMENT_CONTRADICTION_GUARD_ENABLED", raising=False,
    )
    apply_recorder: List[Dict[str, Any]] = []
    _patch_helpers(
        monkeypatch,
        brain_state=_state(received=True, received_at=_iso_minutes_ago(5)),
        apply_recorder=apply_recorder,
    )

    from core.order_flow import mark_awaiting_receipt
    assert mark_awaiting_receipt(
        db=object(), tenant_id=33, phone="+966500000777",
    ) is True
    assert len(apply_recorder) == 1


def test_mark_awaiting_receipt_falls_through_on_inspection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``_load_brain_state`` raises, the guard MUST NOT block the
    legitimate flip. The post-send hook is a defensive checkpoint
    and a guard failure is strictly worse than a missed contradiction
    log."""
    monkeypatch.setenv("PAYMENT_CONTRADICTION_GUARD_ENABLED", "1")
    apply_recorder: List[Dict[str, Any]] = []

    def _raise(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("DB went sideways")

    from core import order_flow
    monkeypatch.setattr(order_flow, "_load_brain_state", _raise)
    monkeypatch.setattr(
        order_flow, "apply_state_patch",
        lambda *_a, **kw: apply_recorder.append(dict(kw)) or True,
    )

    from core.order_flow import mark_awaiting_receipt
    assert mark_awaiting_receipt(
        db=object(), tenant_id=33, phone="+966500000777",
    ) is True
    assert len(apply_recorder) == 1


def test_mark_awaiting_receipt_passes_conversation_id_into_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the webhook passes ``conversation_id``, the canonical log
    line must carry it so operators can grep by conversation."""
    monkeypatch.setenv("PAYMENT_CONTRADICTION_GUARD_ENABLED", "1")
    caplog.set_level(logging.INFO, logger="nahla.order_flow")
    apply_recorder: List[Dict[str, Any]] = []
    _patch_helpers(
        monkeypatch,
        brain_state=_state(received=True, received_at=_iso_minutes_ago(5)),
        apply_recorder=apply_recorder,
    )

    from core.order_flow import mark_awaiting_receipt
    mark_awaiting_receipt(
        db=object(),
        tenant_id=33,
        phone="+966500000777",
        conversation_id=4242,
    )
    line = next(
        m for m in (r.getMessage() for r in caplog.records)
        if "[PAYMENT_CONTRADICTION_GUARD]" in m
    )
    assert "conversation_id=4242" in line


def test_keyword_detection_still_recognises_ack_text() -> None:
    """The contradiction guard sits on top of the existing keyword
    scan; it does NOT replace it. The keyword detector still sees
    ACK text containing the bare "إيصال" word — that's exactly the
    false-match that motivated W1.1. We pin this so future work
    knows the guard is a layer on TOP of the scan, not a removal."""
    from core.order_flow import detect_awaiting_receipt_in_reply
    ack = "وصلنا إيصال التحويل، شكراً لك 🌷"
    assert detect_awaiting_receipt_in_reply(ack) is True


# ── Section B — payment_claim_unverified passthrough ────────────────


def test_order_preparation_state_round_trips_payment_claim_unverified() -> None:
    """``state.to_dict()`` must emit the three claim fields and
    ``from_dict()`` must read them back. This guarantees
    ``DefaultStateStore.save`` (which replaces brain_state with
    ``state.to_dict()`` every turn) does not silently drop the
    understanding flag stamped by ``payment_intent``."""
    from modules.ai.brain.types import OrderPreparationState

    raw = {
        "payment_claim_unverified":    True,
        "payment_claim_unverified_at": "2026-05-25T12:34:56+00:00",
        "payment_claim_text_preview":  "حولت لك المبلغ",
    }
    state = OrderPreparationState.from_dict(raw)
    assert state.payment_claim_unverified is True
    assert state.payment_claim_unverified_at == "2026-05-25T12:34:56+00:00"
    assert state.payment_claim_text_preview == "حولت لك المبلغ"

    out = state.to_dict()
    assert out["payment_claim_unverified"] is True
    assert out["payment_claim_unverified_at"] == "2026-05-25T12:34:56+00:00"
    assert out["payment_claim_text_preview"] == "حولت لك المبلغ"


def test_order_preparation_state_default_payment_claim_fields() -> None:
    """A fresh state has all three fields defaulted (False / "") so
    legacy rows that never carried the claim flag round-trip without
    introducing a stale truthy value."""
    from modules.ai.brain.types import OrderPreparationState

    state = OrderPreparationState()
    assert state.payment_claim_unverified is False
    assert state.payment_claim_unverified_at == ""
    assert state.payment_claim_text_preview == ""

    out = state.to_dict()
    assert out["payment_claim_unverified"] is False
    assert out["payment_claim_unverified_at"] == ""
    assert out["payment_claim_text_preview"] == ""


def test_order_preparation_state_passthrough_does_not_imply_paid() -> None:
    """Architectural invariant: stamping the unverified flag MUST
    NOT touch ``payment_receipt_received`` / ``order_status``. Even
    when both are true on the same row, they remain independent
    truths — the merchant directive's "decision correctness vs
    state correctness" rule."""
    from modules.ai.brain.types import OrderPreparationState

    state = OrderPreparationState.from_dict({
        "payment_claim_unverified": True,
        "payment_claim_unverified_at": "2026-05-25T12:34:56+00:00",
        # The receipt hasn't actually arrived yet — stamping the
        # claim flag must not flip these.
        "payment_receipt_received": False,
        "awaiting_payment_receipt": False,
        "order_status": "",
    })
    assert state.payment_claim_unverified is True
    assert state.payment_receipt_received is False
    assert state.awaiting_payment_receipt is False
    assert state.order_status == ""


def test_order_preparation_state_preserves_claim_after_save_load_cycle() -> None:
    """Simulate :meth:`DefaultStateStore.save` ``→`` ``load`` cycle
    via the dataclass round-trip. The claim fields must survive."""
    from modules.ai.brain.types import OrderPreparationState

    a = OrderPreparationState()
    a.payment_claim_unverified = True
    a.payment_claim_unverified_at = "2026-05-25T12:34:56+00:00"
    a.payment_claim_text_preview = "تم التحويل"

    serialised = a.to_dict()
    b = OrderPreparationState.from_dict(serialised)

    assert b.payment_claim_unverified is True
    assert b.payment_claim_unverified_at == a.payment_claim_unverified_at
    assert b.payment_claim_text_preview == a.payment_claim_text_preview


def test_payment_intent_stamp_helper_writes_into_now_persistent_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the stamp helper writes a state patch which, when
    applied to ``order_prep`` and round-tripped through the
    dataclass, lands on the new fields verbatim. Pinning this stops
    a future refactor from renaming the keys without updating both
    sides."""
    from core import payment_intent
    from modules.ai.brain.types import OrderPreparationState

    captured: Dict[str, Any] = {}

    def _record_patch(*_a: Any, **kw: Any) -> bool:
        captured["state_patch"] = kw.get("state_patch")
        return True

    monkeypatch.setattr(
        "core.order_flow.apply_state_patch", _record_patch,
    )

    payment_intent._stamp_text_claim_unverified_state(
        db=object(),
        tenant_id=33,
        phone="+966500000777",
        inbound_text="حولت لك المبلغ",
    )

    patch = captured["state_patch"]
    assert patch["payment_claim_unverified"] is True
    assert "payment_claim_unverified_at" in patch
    assert patch["payment_claim_text_preview"] == "حولت لك المبلغ"

    # The patch keys must align with the dataclass fields so the
    # next ``DefaultStateStore.save`` does not drop them.
    state = OrderPreparationState.from_dict(patch)
    assert state.payment_claim_unverified is True
    assert state.payment_claim_text_preview == "حولت لك المبلغ"
