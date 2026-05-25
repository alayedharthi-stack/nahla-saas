"""
tests/test_receipt_verdict.py
─────────────────────────────
Wave 1 W1.2 — Receipt Verdict Telemetry. Pure unit + integration
coverage for ``core.receipt_verdict``.

Headline guarantees pinned here:
  * The ``ReceiptVerdict`` enum is closed to the seven values
    listed in the merchant directive. Adding / renaming any of
    them fails the build.
  * ``compute_receipt_verdict`` is pure: never raises on garbage,
    never mutates inputs, deterministic for the same inputs.
  * Every status in ``PaymentUnderstanding`` maps to some verdict.
  * The ``[PAYMENT_VERIFICATION_DECISION]`` log line carries the
    full canonical field set.
  * The kill switch is independent and default OFF.
  * The wiring at the three payment short-circuit sites is
    observation-only: behaviour is byte-identical with the flag
    on or off.
"""
from __future__ import annotations

import inspect
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ── 1. Architectural invariants — closed enum & pin sets ────────────


def test_receipt_verdict_enum_is_closed() -> None:
    """The seven verdicts pinned by the merchant directive. Drift
    in either direction (renaming, deleting, adding) fails the
    build until the change is deliberate."""
    from core.receipt_verdict import ReceiptVerdict

    expected = {
        "verified_match",
        "probable_match",
        "unclear_receipt",
        "account_mismatch",
        "fake_or_corrupted",
        "text_claim_unverified",
        "not_payment",
    }
    actual = {v.value for v in ReceiptVerdict}
    assert actual == expected, (
        f"ReceiptVerdict drifted: expected={sorted(expected)}, "
        f"actual={sorted(actual)}"
    )


def test_paid_flow_allowed_verdicts_is_only_verified_match() -> None:
    """W1.2 architectural pin: the only verdict allowed to gate the
    paid flow (in W1.4) is ``verified_match``. Any future commit
    that widens this set MUST update both the enum, the pin, and
    this test deliberately."""
    from core.receipt_verdict import (
        PAID_FLOW_ALLOWED_VERDICTS,
        ReceiptVerdict,
    )
    assert PAID_FLOW_ALLOWED_VERDICTS == frozenset({ReceiptVerdict.VERIFIED_MATCH})


def test_paid_flow_blocked_verdicts_includes_dangerous_states() -> None:
    """``unclear_receipt``, ``account_mismatch``, ``fake_or_corrupted``,
    ``text_claim_unverified`` MUST always block the paid flow.
    Pin this so future drift cannot accidentally promote one of
    them out of the blocked set."""
    from core.receipt_verdict import (
        PAID_FLOW_BLOCKED_VERDICTS,
        ReceiptVerdict,
    )
    expected_blocked = frozenset({
        ReceiptVerdict.UNCLEAR_RECEIPT,
        ReceiptVerdict.ACCOUNT_MISMATCH,
        ReceiptVerdict.FAKE_OR_CORRUPTED,
        ReceiptVerdict.TEXT_CLAIM_UNVERIFIED,
    })
    assert PAID_FLOW_BLOCKED_VERDICTS == expected_blocked


def test_allowed_and_blocked_verdicts_are_disjoint() -> None:
    """A verdict cannot simultaneously allow and block the paid
    flow. The architectural test catches a future drift that
    violates this invariant."""
    from core.receipt_verdict import (
        PAID_FLOW_ALLOWED_VERDICTS,
        PAID_FLOW_BLOCKED_VERDICTS,
    )
    assert (PAID_FLOW_ALLOWED_VERDICTS & PAID_FLOW_BLOCKED_VERDICTS) == set()


# ── 2. Kill switch ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECEIPT_VERDICT_TELEMETRY_ENABLED", raising=False)


def test_kill_switch_default_off() -> None:
    from core.receipt_verdict import is_receipt_verdict_telemetry_enabled
    assert is_receipt_verdict_telemetry_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_kill_switch_truthy(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    from core.receipt_verdict import is_receipt_verdict_telemetry_enabled
    monkeypatch.setenv("RECEIPT_VERDICT_TELEMETRY_ENABLED", val)
    assert is_receipt_verdict_telemetry_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_kill_switch_falsy(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    from core.receipt_verdict import is_receipt_verdict_telemetry_enabled
    monkeypatch.setenv("RECEIPT_VERDICT_TELEMETRY_ENABLED", val)
    assert is_receipt_verdict_telemetry_enabled() is False


# ── 3. Pure function — purity invariants ────────────────────────────


def test_compute_never_raises_on_empty_inputs() -> None:
    from core.receipt_verdict import compute_receipt_verdict, ReceiptVerdict
    rv = compute_receipt_verdict()
    assert rv.verdict == ReceiptVerdict.NOT_PAYMENT
    assert rv.reason == "no_payment_signal"


def test_compute_never_raises_on_garbage_inputs() -> None:
    from core.receipt_verdict import compute_receipt_verdict, ReceiptVerdict
    for kwargs in [
        {"payment_understanding": object()},
        {"payment_evidence_status": object()},
        {"image_kind": 123},
        {"pdf_kind": 4.5},
        {"has_attached_media": "yes"},
        {"has_text_only_claim": []},
        {
            "payment_understanding": object(),
            "payment_evidence_status": ["weird"],
            "image_kind": None,
            "pdf_kind": {"x": 1},
        },
    ]:
        rv = compute_receipt_verdict(**kwargs)  # type: ignore[arg-type]
        # Any verdict is acceptable as long as we got a populated result.
        assert isinstance(rv.verdict, ReceiptVerdict), kwargs


def test_compute_is_deterministic() -> None:
    from core.receipt_verdict import compute_receipt_verdict
    kwargs: Dict[str, Any] = {
        "payment_understanding": "evidence_verified",
        "payment_evidence_status": "confirmed",
        "image_kind": "payment_receipt",
        "has_attached_media": True,
    }
    a = compute_receipt_verdict(**kwargs)
    b = compute_receipt_verdict(**kwargs)
    assert a == b


def test_compute_does_not_mutate_inputs() -> None:
    from core.receipt_verdict import compute_receipt_verdict

    inputs: Dict[str, Any] = {
        "payment_evidence_status": "confirmed",
        "image_kind": "payment_receipt",
        "has_attached_media": True,
        "has_text_only_claim": False,
    }
    snapshot = dict(inputs)
    compute_receipt_verdict(**inputs)
    assert inputs == snapshot


def test_compute_module_uses_no_side_effect_symbols() -> None:
    """Pure function source MUST NOT reference DB/network/IO
    symbols. Inspired by Commit 1 of the relational layer rollout."""
    from core.receipt_verdict import _compute_unsafe

    src = inspect.getsource(_compute_unsafe)
    forbidden = (
        "session", "db.commit", "requests.", "httpx.", "urllib",
        "open(", "Path(", "subprocess",
    )
    leaked = [t for t in forbidden if t in src]
    assert leaked == [], (
        f"compute_receipt_verdict source references forbidden symbols: "
        f"{leaked}. The verdict layer MUST stay pure."
    )


# ── 4. PaymentUnderstanding mapping coverage ────────────────────────


@pytest.mark.parametrize(
    "pu_status, expected_verdict, expected_reason",
    [
        ("evidence_verified",                  "verified_match",        "evidence_iban_or_beneficiary_match"),
        ("evidence_account_mismatch",          "account_mismatch",      "evidence_iban_or_beneficiary_mismatch"),
        ("evidence_unverified",                "unclear_receipt",       "evidence_without_matchable_tokens"),
        ("text_claim_unverified",              "text_claim_unverified", "text_claim_without_evidence"),
    ],
)
def test_payment_understanding_status_maps_to_verdict(
    pu_status: str, expected_verdict: str, expected_reason: str,
) -> None:
    """Every primary ``PaymentUnderstanding`` status maps to a
    deterministic verdict + reason."""
    from core.receipt_verdict import compute_receipt_verdict, ReceiptVerdict
    rv = compute_receipt_verdict(
        payment_understanding=pu_status,
        has_attached_media=True,
    )
    assert rv.verdict == ReceiptVerdict(expected_verdict)
    assert rv.reason == expected_reason
    assert rv.derived_from == "payment_understanding"


def test_evidence_no_tenant_accounts_with_confirmed_legacy_is_probable() -> None:
    """When the merchant has no registered accounts but the legacy
    classifier said ``confirmed``, telemetry treats it as probable."""
    from core.receipt_verdict import compute_receipt_verdict, ReceiptVerdict
    rv = compute_receipt_verdict(
        payment_understanding="evidence_received_no_tenant_accounts",
        payment_evidence_status="confirmed",
        has_attached_media=True,
    )
    assert rv.verdict == ReceiptVerdict.PROBABLE_MATCH
    assert rv.reason == "legacy_confirmed_no_tenant_accounts"


@pytest.mark.parametrize("legacy", ["pre_transfer_review", "needs_confirmation"])
def test_evidence_no_tenant_accounts_with_partial_legacy_is_unclear(
    legacy: str,
) -> None:
    from core.receipt_verdict import compute_receipt_verdict, ReceiptVerdict
    rv = compute_receipt_verdict(
        payment_understanding="evidence_received_no_tenant_accounts",
        payment_evidence_status=legacy,
        has_attached_media=True,
    )
    assert rv.verdict == ReceiptVerdict.UNCLEAR_RECEIPT
    assert rv.reason == f"legacy_{legacy}_no_tenant_accounts"


def test_no_signal_understanding_falls_through_to_legacy_layer() -> None:
    """``no_signal`` PU status means PU could not classify — fall
    back to the legacy classifier."""
    from core.receipt_verdict import compute_receipt_verdict, ReceiptVerdict
    rv = compute_receipt_verdict(
        payment_understanding="no_signal",
        payment_evidence_status="confirmed",
        has_attached_media=True,
    )
    assert rv.verdict == ReceiptVerdict.PROBABLE_MATCH
    assert rv.derived_from == "payment_evidence_status"


def test_payment_understanding_object_is_duck_typed() -> None:
    """``compute_receipt_verdict`` must accept the real
    ``PaymentUnderstanding`` dataclass as well as the bare status
    string. We pin both surfaces."""
    from core.payment_understanding import (
        PAYMENT_UNDERSTANDING_EVIDENCE_VERIFIED,
        PaymentUnderstanding,
    )
    from core.receipt_verdict import compute_receipt_verdict, ReceiptVerdict

    pu = PaymentUnderstanding(
        status=PAYMENT_UNDERSTANDING_EVIDENCE_VERIFIED,
        can_flip_receipt_received=True,
        blocks_order_paid_flow=False,
        matched_iban="SA0380000000608010167519",
        matched_beneficiary="نحلة",
    )
    rv = compute_receipt_verdict(
        payment_understanding=pu,
        has_attached_media=True,
    )
    assert rv.verdict == ReceiptVerdict.VERIFIED_MATCH
    assert rv.matched_iban == "SA0380000000608010167519"
    assert rv.matched_beneficiary == "نحلة"


def test_every_payment_understanding_status_maps_to_a_verdict() -> None:
    """Architectural coverage check. Every status the W1.0
    layer can emit must produce SOME verdict — never raise, never
    return None."""
    from core.payment_understanding import _ALL_STATUSES
    from core.receipt_verdict import compute_receipt_verdict, ReceiptVerdict

    for status in _ALL_STATUSES:
        rv = compute_receipt_verdict(
            payment_understanding=status,
            payment_evidence_status="confirmed",
            has_attached_media=True,
        )
        assert isinstance(rv.verdict, ReceiptVerdict), status


# ── 5. Legacy-only (no PU) mapping coverage ─────────────────────────


def test_legacy_confirmed_without_pu_is_probable() -> None:
    from core.receipt_verdict import compute_receipt_verdict, ReceiptVerdict
    rv = compute_receipt_verdict(
        payment_evidence_status="confirmed",
        has_attached_media=True,
    )
    assert rv.verdict == ReceiptVerdict.PROBABLE_MATCH
    assert rv.derived_from == "payment_evidence_status"


@pytest.mark.parametrize("pe", ["pre_transfer_review", "needs_confirmation"])
def test_legacy_partial_without_pu_is_unclear(pe: str) -> None:
    from core.receipt_verdict import compute_receipt_verdict, ReceiptVerdict
    rv = compute_receipt_verdict(
        payment_evidence_status=pe,
        has_attached_media=True,
    )
    assert rv.verdict == ReceiptVerdict.UNCLEAR_RECEIPT


# ── 6. Fake / corrupted heuristic ───────────────────────────────────


def test_payment_kind_with_empty_text_is_fake_or_corrupted() -> None:
    """A normalizer that classifies the inbound as payment-shaped but
    OCR returns empty → defensive ``fake_or_corrupted`` verdict."""
    from core.receipt_verdict import compute_receipt_verdict, ReceiptVerdict
    rv = compute_receipt_verdict(
        payment_evidence_status="empty_text",
        image_kind="payment_receipt",
        has_attached_media=True,
    )
    assert rv.verdict == ReceiptVerdict.FAKE_OR_CORRUPTED


def test_payment_kind_attached_with_no_evidence_status_is_fake_or_corrupted() -> None:
    from core.receipt_verdict import compute_receipt_verdict, ReceiptVerdict
    rv = compute_receipt_verdict(
        pdf_kind="payment_pre_review",
        has_attached_media=True,
    )
    assert rv.verdict == ReceiptVerdict.FAKE_OR_CORRUPTED


def test_non_payment_kind_with_empty_text_is_not_payment() -> None:
    """Greeting-card / unrelated screenshots WITHOUT a payment kind
    must NOT be flagged as fake/corrupted — they're simply
    not_payment."""
    from core.receipt_verdict import compute_receipt_verdict, ReceiptVerdict
    rv = compute_receipt_verdict(
        payment_evidence_status="empty_text",
        image_kind="greeting_card",
        has_attached_media=True,
    )
    assert rv.verdict == ReceiptVerdict.NOT_PAYMENT


# ── 7. Text-claim path ──────────────────────────────────────────────


def test_text_only_claim_no_media_is_text_claim_unverified() -> None:
    from core.receipt_verdict import compute_receipt_verdict, ReceiptVerdict
    rv = compute_receipt_verdict(
        has_text_only_claim=True,
        has_attached_media=False,
    )
    assert rv.verdict == ReceiptVerdict.TEXT_CLAIM_UNVERIFIED
    assert rv.derived_from == "fallback"


def test_text_claim_with_media_does_not_collapse_to_text_claim() -> None:
    """If the customer attached media AND said "حولت", the media
    decides — text-claim only fires when media is absent."""
    from core.receipt_verdict import compute_receipt_verdict, ReceiptVerdict
    rv = compute_receipt_verdict(
        has_text_only_claim=True,
        has_attached_media=True,
        payment_evidence_status="confirmed",
        image_kind="payment_receipt",
    )
    assert rv.verdict == ReceiptVerdict.PROBABLE_MATCH


# ── 8. Inert path ───────────────────────────────────────────────────


def test_no_signals_at_all_is_not_payment() -> None:
    from core.receipt_verdict import compute_receipt_verdict, ReceiptVerdict
    rv = compute_receipt_verdict()
    assert rv.verdict == ReceiptVerdict.NOT_PAYMENT


# ── 9. Result dataclass + log shape ─────────────────────────────────


def test_result_to_log_dict_has_canonical_keys() -> None:
    from core.receipt_verdict import compute_receipt_verdict
    rv = compute_receipt_verdict(
        payment_understanding="evidence_verified",
        has_attached_media=True,
    )
    payload = rv.to_log_dict()
    expected_keys = {
        "receipt_verdict",
        "receipt_verdict_reason",
        "receipt_verdict_derived_from",
        "payment_understanding_status",
        "payment_evidence_status",
        "image_or_pdf_kind",
        "has_attached_media",
        "has_text_only_claim",
        "matched_iban",
        "matched_beneficiary",
        "receipt_iban_count",
        "receipt_beneficiary_count",
    }
    assert set(payload.keys()) == expected_keys


def test_result_paid_flow_properties_match_pin_sets() -> None:
    """``is_paid_flow_allowed`` / ``is_paid_flow_blocked`` derive
    from the architectural pin sets, not from per-call logic. Pin
    so future drift fails the test."""
    from core.receipt_verdict import (
        PAID_FLOW_ALLOWED_VERDICTS,
        PAID_FLOW_BLOCKED_VERDICTS,
        ReceiptVerdict,
        ReceiptVerdictResult,
    )
    for v in ReceiptVerdict:
        rv = ReceiptVerdictResult(verdict=v)
        assert rv.is_paid_flow_allowed == (v in PAID_FLOW_ALLOWED_VERDICTS)
        assert rv.is_paid_flow_blocked == (v in PAID_FLOW_BLOCKED_VERDICTS)


def test_log_line_carries_all_canonical_fields(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RECEIPT_VERDICT_TELEMETRY_ENABLED", "1")
    caplog.set_level(logging.INFO, logger="nahla.receipt_verdict")

    from core.receipt_verdict import (
        compute_receipt_verdict, log_receipt_verdict,
    )
    rv = compute_receipt_verdict(
        payment_understanding="evidence_verified",
        payment_evidence_status="confirmed",
        image_kind="payment_receipt",
        has_attached_media=True,
    )
    log_receipt_verdict(
        tenant_id=33, phone="+966500000999",
        conversation_id=909, message_id="wamid.123",
        source="receipt_inbound", verdict=rv,
    )

    line = next(
        m for m in (r.getMessage() for r in caplog.records)
        if "[PAYMENT_VERIFICATION_DECISION]" in m
    )
    assert "tenant_id=33" in line
    assert "conversation_id=909" in line
    assert "message_id=wamid.123" in line
    assert "source=receipt_inbound" in line
    assert "*0999" in line  # masked phone
    assert "+966500000999" not in line
    assert "receipt_verdict=verified_match" in line
    assert "receipt_verdict_reason=evidence_iban_or_beneficiary_match" in line
    assert "payment_understanding_status=evidence_verified" in line
    assert "payment_evidence_status=confirmed" in line


def test_log_emission_inert_with_flag_off(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the kill switch is off, ``log_receipt_verdict`` must
    emit nothing — the single source of truth for telemetry
    on/off."""
    monkeypatch.delenv("RECEIPT_VERDICT_TELEMETRY_ENABLED", raising=False)
    caplog.set_level(logging.INFO, logger="nahla.receipt_verdict")

    from core.receipt_verdict import (
        compute_receipt_verdict, log_receipt_verdict, ReceiptVerdictResult,
        ReceiptVerdict,
    )
    rv = ReceiptVerdictResult(verdict=ReceiptVerdict.VERIFIED_MATCH)
    log_receipt_verdict(
        tenant_id=33, phone="+966500000999",
        source="receipt_inbound", verdict=rv,
    )
    assert not any(
        "[PAYMENT_VERIFICATION_DECISION]" in r.getMessage()
        for r in caplog.records
    )


def test_log_emission_never_raises_on_garbage() -> None:
    from core.receipt_verdict import log_receipt_verdict, ReceiptVerdictResult, ReceiptVerdict
    # Never raises even with weird inputs
    rv = ReceiptVerdictResult(verdict=ReceiptVerdict.NOT_PAYMENT)
    log_receipt_verdict(  # type: ignore[arg-type]
        tenant_id={"weird": True}, phone=None,
        conversation_id=[1, 2], message_id=None,
        source="x", verdict=rv,
    )


# ── 10. Wiring is observation-only ──────────────────────────────────


def test_wiring_helper_in_order_flow_is_observation_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_w12_emit_receipt_verdict`` must NEVER raise nor mutate
    state, even with the flag on. Verified by stubbing
    ``log_receipt_verdict`` to record the call and confirming no
    side effects beyond the log."""
    monkeypatch.setenv("RECEIPT_VERDICT_TELEMETRY_ENABLED", "1")
    from core import order_flow

    recorded: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        "core.receipt_verdict.log_receipt_verdict",
        lambda **kw: recorded.append(kw),
    )

    order_flow._w12_emit_receipt_verdict(
        tenant_id=33, phone="+966500000999",
        conversation_id=909, message_id="wamid.123",
        source="receipt_inbound",
        payment_understanding="evidence_verified",
        payment_evidence_status="confirmed",
        image_kind="payment_receipt",
        has_attached_media=True,
        has_text_only_claim=False,
    )
    assert len(recorded) == 1
    assert recorded[0]["source"] == "receipt_inbound"
    assert recorded[0]["verdict"].verdict.value == "verified_match"


def test_wiring_helper_inert_with_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RECEIPT_VERDICT_TELEMETRY_ENABLED", raising=False)
    from core import order_flow

    recorded: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        "core.receipt_verdict.log_receipt_verdict",
        lambda **kw: recorded.append(kw),
    )

    order_flow._w12_emit_receipt_verdict(
        tenant_id=33, phone="+966500000999",
        source="receipt_inbound",
        payment_understanding="evidence_verified",
        has_attached_media=True,
    )
    # ``log_receipt_verdict`` itself early-returns on flag off, so
    # the helper-side gate also early-returns. No call recorded.
    assert recorded == []


def test_wiring_helper_swallows_compute_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``compute_receipt_verdict`` were ever to raise, the
    wiring helper must not propagate. Inject a raising stub to
    confirm the swallow behaviour."""
    monkeypatch.setenv("RECEIPT_VERDICT_TELEMETRY_ENABLED", "1")

    def _raise(**_kw: Any) -> Any:
        raise RuntimeError("verdict compute exploded")

    monkeypatch.setattr(
        "core.receipt_verdict.compute_receipt_verdict", _raise,
    )

    from core import order_flow
    # Must not raise — the pipeline path it lives on is the
    # webhook's payment short-circuit and any exception there would
    # break a customer reply.
    order_flow._w12_emit_receipt_verdict(
        tenant_id=33, phone="+966500000999",
        source="receipt_inbound",
        payment_understanding="evidence_verified",
        has_attached_media=True,
    )


# ── 11. End-to-end at the receipt-inbound short-circuit site ─────────


def test_receipt_inbound_byte_identical_with_telemetry_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The W1.2 wiring is observation-only. With the telemetry flag
    ON, the receipt short-circuit MUST return the same shape as
    with the flag OFF for an identical input."""
    from core import order_flow

    md = {
        "pdf_kind": "payment_receipt",
        "payment_evidence_status": "confirmed",
        "pdf_text_preview": "تم التحويل\nإلى SA0380000000608010167519",
        "pdf_kind_confidence": "high",
        "message_id": "wamid.test",
    }

    class _FakeAccount:
        has_accounts = True

    class _FakeConv:
        id = 909

    monkeypatch.setattr(
        order_flow, "_load_brain_state",
        lambda *_a, **_k: (_FakeConv(), {
            "current_product_focus": {"title": "عسل"},
            "order_prep": {"awaiting_payment_receipt": True},
        }),
    )
    monkeypatch.setattr(
        "core.tenant_payment_accounts.load_tenant_payment_accounts",
        lambda *_a, **_k: _FakeAccount(),
    )

    # Stub PaymentUnderstanding to a verified match so the
    # short-circuit confirms.
    from core.payment_understanding import PaymentUnderstanding
    pu = PaymentUnderstanding(
        status="evidence_verified",
        can_flip_receipt_received=True,
        blocks_order_paid_flow=False,
        matched_iban="SA0380000000608010167519",
        matched_beneficiary="نحلة",
    )
    monkeypatch.setattr(
        "core.payment_understanding.compute_payment_understanding",
        lambda **_k: pu,
    )

    # First run: flag OFF.
    monkeypatch.delenv("RECEIPT_VERDICT_TELEMETRY_ENABLED", raising=False)
    out_off = order_flow.maybe_handle_receipt_inbound(
        db=object(), tenant_id=33, phone="+966500000999",
        inbound_normalized_type="document",
        inbound_metadata=md,
    )

    # Second run: flag ON.
    monkeypatch.setenv("RECEIPT_VERDICT_TELEMETRY_ENABLED", "1")
    out_on = order_flow.maybe_handle_receipt_inbound(
        db=object(), tenant_id=33, phone="+966500000999",
        inbound_normalized_type="document",
        inbound_metadata=md,
    )

    # Both runs must produce the same return shape — telemetry
    # never touches the decision. ``payment_receipt_at`` is set
    # to ``datetime.now(...)`` inside the handler, so we exclude
    # only that timestamp from the equality check; every other
    # state-affecting field MUST match.
    assert out_off is not None and out_on is not None
    assert out_off["reply_text"] == out_on["reply_text"]

    def _strip_timestamps(sp: Dict[str, Any]) -> Dict[str, Any]:
        sp = dict(sp)
        sp.pop("payment_receipt_at", None)
        meta = dict(sp.get("payment_receipt_metadata") or {})
        meta.pop("received_at", None)
        if meta:
            sp["payment_receipt_metadata"] = meta
        return sp

    sp_off = _strip_timestamps(out_off["state_patch"])
    sp_on = _strip_timestamps(out_on["state_patch"])
    assert sp_off == sp_on, (
        "Telemetry path is not observation-only: state_patch "
        f"differs between flag OFF and flag ON.\n  off={sp_off}\n  on={sp_on}"
    )


def test_text_claim_path_emits_verdict_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The text-claim brain-driven path emits the canonical
    ``[PAYMENT_VERIFICATION_DECISION]`` line when the flag is on,
    without altering its return value."""
    monkeypatch.setenv("RECEIPT_VERDICT_TELEMETRY_ENABLED", "1")
    caplog.set_level(logging.INFO, logger="nahla.receipt_verdict")

    from core import payment_intent

    monkeypatch.setattr(
        payment_intent, "is_post_shipment_delivery_confirmation",
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
        payment_intent, "_maybe_promote_prior_evidence",
        lambda **_k: None,
    )
    monkeypatch.setattr(
        "core.order_flow.apply_state_patch",
        lambda *_a, **_k: True,
    )

    result = payment_intent.maybe_handle_payment_claim(
        db=object(),
        tenant_id=33,
        phone="+966500000999",
        inbound_text="حولت لك المبلغ",
        has_attached_media=False,
    )
    assert result is None  # text-claim path returns None — unchanged

    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "[PAYMENT_VERIFICATION_DECISION]" in m
        and "receipt_verdict=text_claim_unverified" in m
        for m in msgs
    )
