"""
tests/test_payment_text_claim_brain_driven.py
─────────────────────────────────────────────
Tenant 33 #48 (May 2026) — payment understanding correction.

These tests pin the new behaviour of
``core.payment_intent.maybe_handle_payment_claim`` and
``core.payment_intent.rewrite_generic_reply_for_payment_context``:

    Mere mention of a transfer ("حولت" / "تم التحويل" / "حولت لك")
    in plain text does NOT short-circuit the brain with a hardcoded
    ACK and does NOT flip ``awaiting_payment_receipt=True`` /
    ``order_status='awaiting_receipt'``. Instead the helper stamps
    the lightweight understanding flag ``payment_claim_unverified``
    on brain state and returns ``None`` so the brain composes its
    own organic reply.

The new behaviour is governed by the env flag
``PAYMENT_TEXT_CLAIM_BRAIN_DRIVEN_ENABLED`` (default "1"). The
legacy hardcoded-ACK path is still reachable when the operator
sets the flag to "0" — covered by existing regression tests in
``test_post_shipment_delivery_gate.py`` and
``test_minor_ai_fixes.py``.

Coverage map
────────────
1. Default flag is on (no env var) → text-only claim returns None.
2. Default flag is on → no hardcoded ACK reply leaks out.
3. Default flag is on → brain state gets the
   ``payment_claim_unverified=True`` stamp + timestamp.
4. Default flag is on → ``awaiting_payment_receipt`` /
   ``order_status`` are NOT mutated by the text-claim path.
5. Real attached media → still goes through the receipt branch
   (handled by ``maybe_handle_receipt_inbound``).
6. ``rewrite_generic_reply_for_payment_context`` returns None when
   the flag is on — the brain owns the wording even when it
   shipped a generic fallback.
7. Disabling the flag rolls back to the legacy hardcoded-ACK path.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest


_BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ── Tiny in-memory fixtures ────────────────────────────────────────


class _FakeQuery:
    def __init__(self, events: List[Any]) -> None:
        self._events = events

    def filter(self, *_a: Any, **_k: Any) -> "_FakeQuery":
        return self

    def order_by(self, *_a: Any, **_k: Any) -> "_FakeQuery":
        return self

    def limit(self, _n: int) -> "_FakeQuery":
        return self

    def all(self) -> List[Any]:
        return list(self._events)


class _FakeDB:
    """Fake DB that accepts every model and returns the canned
    event list. The text-claim policy's stamp helper attempts to
    apply a state patch via ``apply_state_patch``; we monkeypatch
    that to a recording stub in the relevant tests."""

    def __init__(self, events: Optional[List[Any]] = None) -> None:
        self._events = events or []

    def query(self, *_a: Any, **_k: Any) -> _FakeQuery:
        return _FakeQuery(self._events)


@pytest.fixture(autouse=True)
def _ensure_default_flag(monkeypatch):
    """Strip any pre-set env value so the module's default ON
    behaviour is exercised."""
    monkeypatch.delenv("PAYMENT_TEXT_CLAIM_BRAIN_DRIVEN_ENABLED", raising=False)


# ── 1. Returns None for text-only claim ────────────────────────────


def test_text_only_claim_returns_none_under_default_flag(monkeypatch):
    """Headline regression: 'حولت' / 'تم التحويل' must NOT short-
    circuit the brain. The new default behaviour is to return None
    so the brain replies naturally."""
    from core import payment_intent as pi

    # Stub the post-shipment gate so the helper actually reaches
    # the policy branch.
    monkeypatch.setattr(
        pi, "is_post_shipment_delivery_confirmation",
        lambda *_a, **_k: False,
    )
    # Stub the brain-state load so the active-context gate passes.
    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda *_a, **_k: (
            None,
            {
                "current_product_focus": {"title": "عسل سدر"},
                "order_prep": {"awaiting_payment_receipt": True},
            },
        ),
    )
    # Suppress the receipt-override branch — no prior PDF.
    monkeypatch.setattr(
        pi, "_maybe_promote_prior_evidence",
        lambda **_k: None,
    )
    # Capture any state patch the helper attempts to apply.
    captured: Dict[str, Any] = {}
    monkeypatch.setattr(
        "core.order_flow.apply_state_patch",
        lambda *_a, **kw: captured.setdefault("patch", kw.get("state_patch")),
    )

    result = pi.maybe_handle_payment_claim(
        _FakeDB(),
        tenant_id=33,
        phone="+966500000099",
        inbound_text="تم التحويل",
        has_attached_media=False,
    )
    assert result is None, (
        "text-only payment claim must NOT short-circuit the brain "
        "under the new default policy"
    )


# ── 2. No hardcoded ACK leaks out ──────────────────────────────────


@pytest.mark.parametrize("inbound", [
    "تم التحويل",
    "حولت",
    "حولت لك المبلغ",
    "دفعت",
    "تم الدفع",
])
def test_no_hardcoded_ack_for_text_only_claims(monkeypatch, inbound):
    from core import payment_intent as pi

    monkeypatch.setattr(
        pi, "is_post_shipment_delivery_confirmation",
        lambda *_a, **_k: False,
    )
    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda *_a, **_k: (
            None,
            {
                "current_product_focus": {"title": "عسل"},
                "order_prep": {"awaiting_payment_receipt": True},
            },
        ),
    )
    monkeypatch.setattr(
        pi, "_maybe_promote_prior_evidence",
        lambda **_k: None,
    )
    monkeypatch.setattr(
        "core.order_flow.apply_state_patch",
        lambda *_a, **_k: None,
    )

    result = pi.maybe_handle_payment_claim(
        _FakeDB(),
        tenant_id=33, phone="+966500000099",
        inbound_text=inbound,
        has_attached_media=False,
    )
    assert result is None


# ── 3. Stamp records the unverified flag ───────────────────────────


def test_text_only_claim_stamps_unverified_flag(monkeypatch):
    """The helper must stamp ``payment_claim_unverified=True`` (and
    a timestamp) on brain state so the next-turn brain prompt can
    see the situation. State that would imply 'we got something'
    (``awaiting_payment_receipt``, ``order_status``) MUST NOT
    appear in the patch."""
    from core import payment_intent as pi

    monkeypatch.setattr(
        pi, "is_post_shipment_delivery_confirmation",
        lambda *_a, **_k: False,
    )
    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda *_a, **_k: (
            None,
            {
                "current_product_focus": {"title": "عسل"},
                "order_prep": {},
            },
        ),
    )
    monkeypatch.setattr(
        pi, "_maybe_promote_prior_evidence",
        lambda **_k: None,
    )

    captured: Dict[str, Any] = {}

    def _record_patch(*_a, **kw):
        captured["patch"] = kw.get("state_patch")

    monkeypatch.setattr(
        "core.order_flow.apply_state_patch",
        _record_patch,
    )

    result = pi.maybe_handle_payment_claim(
        _FakeDB(),
        tenant_id=33, phone="+966500000099",
        inbound_text="حولت لك المبلغ",
        has_attached_media=False,
    )
    assert result is None
    patch = captured.get("patch") or {}
    assert patch.get("payment_claim_unverified") is True
    assert "payment_claim_unverified_at" in patch
    # The merchant directive: state must not lie.
    assert "awaiting_payment_receipt" not in patch
    assert "order_status" not in patch
    assert "payment_receipt_received" not in patch


# ── 4. State is not mutated to imply receipt arrived ───────────────


def test_text_claim_does_not_flip_awaiting_or_order_status(monkeypatch):
    """Even when the active-order context has product + price, a
    text-only claim must not mutate ``awaiting_payment_receipt`` or
    ``order_status`` in the brain state."""
    from core import payment_intent as pi

    monkeypatch.setattr(
        pi, "is_post_shipment_delivery_confirmation",
        lambda *_a, **_k: False,
    )
    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda *_a, **_k: (
            None,
            {
                "current_product_focus": {
                    "title": "عسل سدر", "price": 360, "currency": "SAR",
                },
                "order_prep": {},
            },
        ),
    )
    monkeypatch.setattr(
        pi, "_maybe_promote_prior_evidence",
        lambda **_k: None,
    )

    seen_patches: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        "core.order_flow.apply_state_patch",
        lambda *_a, **kw: seen_patches.append(kw.get("state_patch") or {}),
    )

    pi.maybe_handle_payment_claim(
        _FakeDB(),
        tenant_id=33, phone="+966500000077",
        inbound_text="تم التحويل",
        has_attached_media=False,
    )
    for patch in seen_patches:
        for forbidden in (
            "awaiting_payment_receipt",
            "order_status",
            "payment_receipt_received",
            "payment_receipt_at",
        ):
            assert forbidden not in patch, (
                f"text-claim path must not mutate {forbidden!r}"
            )


# ── 5. Real media still flows through the receipt branch ───────────


def test_real_media_short_circuits_correctly(monkeypatch):
    """Sanity check: ``has_attached_media=True`` returns None
    (the receipt branch in maybe_handle_receipt_inbound owns this
    path). The brain-driven text-claim policy must not interfere."""
    from core import payment_intent as pi

    result = pi.maybe_handle_payment_claim(
        _FakeDB(),
        tenant_id=33, phone="+966500000077",
        inbound_text="تم التحويل وأرسلت لك الإيصال",
        has_attached_media=True,
    )
    assert result is None


# ── 6. rewrite_generic_reply_for_payment_context honours the flag ──


def test_generic_reply_rewriter_returns_none_under_flag():
    """When the brain shipped a generic fallback for a payment-
    context inbound, the rewriter used to substitute the brain's
    wording with a hardcoded ACK. Under the new policy the brain
    owns the wording and the rewriter returns None."""
    from core import payment_intent as pi

    out = pi.rewrite_generic_reply_for_payment_context(
        inbound_text="تم التحويل",
        brain_reply="أنا هنا — قول وش تحتاج وأكمل معك",
        state_summary={
            "selected_product": "عسل سدر",
            "awaiting_payment_receipt": True,
        },
    )
    assert out is None


# ── 7. Flag-off rollback path still works ──────────────────────────


def test_flag_off_restores_legacy_hardcoded_ack(monkeypatch):
    """Setting the env var to '0' falls back to the original
    hardcoded ACK + state mutation. Operators can opt-out if the
    new policy regresses anything in production."""
    monkeypatch.setenv("PAYMENT_TEXT_CLAIM_BRAIN_DRIVEN_ENABLED", "0")

    from core import payment_intent as pi

    monkeypatch.setattr(
        pi, "is_post_shipment_delivery_confirmation",
        lambda *_a, **_k: False,
    )
    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda *_a, **_k: (
            None,
            {
                "current_product_focus": {"title": "عسل سدر"},
                "order_prep": {"awaiting_payment_receipt": True},
            },
        ),
    )
    monkeypatch.setattr(
        pi, "_maybe_promote_prior_evidence",
        lambda **_k: None,
    )

    result = pi.maybe_handle_payment_claim(
        _FakeDB(),
        tenant_id=33, phone="+966500000077",
        inbound_text="تم التحويل",
        has_attached_media=False,
    )
    assert result is not None
    assert "reply_text" in result
    assert "state_patch" in result
