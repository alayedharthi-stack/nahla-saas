"""
tests/test_receipt_extraction.py
────────────────────────────────
Wave 1 W1.3 — Receipt field extraction & structured visibility.

Headline guarantees pinned here:
  * ``FieldConfidence`` is a closed four-state enum.
  * ``ReceiptFields`` carries per-field confidence (the W1.4 layer
    will require finer-grained decisions than overall alone).
  * ``compose_full_evidence_text`` prefers full-text over the
    legacy 280-char preview and reports truncation via
    ``source_text_was_truncated``.
  * The default ``RegexHeuristicExtractor`` is registered, pure,
    never raises, and reuses the validated IBAN / beneficiary /
    bank-brand helpers from ``core.tenant_payment_accounts`` so
    extraction normalisation matches what the verifier expects.
  * The orchestrator stamps provenance fields the engine left
    blank without overwriting fields the engine populated.
  * The ``[PAYMENT_RECEIPT_EXTRACTED]`` log line carries the full
    canonical field set.
  * The kill switch is independent and default OFF.
  * The wiring at the order-flow short-circuit sites is
    observation-only: state_patch / reply_text are byte-identical
    with the flag on or off.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ── 1. Closed enum invariant ────────────────────────────────────────


def test_field_confidence_is_closed() -> None:
    from core.receipt_extraction import FieldConfidence

    expected = {"high", "medium", "low", "absent"}
    actual = {c.value for c in FieldConfidence}
    assert actual == expected, (
        f"FieldConfidence drifted: expected={sorted(expected)}, "
        f"actual={sorted(actual)}"
    )


def test_field_confidence_pin_set_matches_enum() -> None:
    from core.receipt_extraction import (
        FIELD_CONFIDENCE_ALL, FIELD_CONFIDENCE_VALUES, FieldConfidence,
    )
    assert FIELD_CONFIDENCE_ALL == frozenset(FieldConfidence)
    assert FIELD_CONFIDENCE_VALUES == {c.value for c in FieldConfidence}


# ── 2. Receipt fields dataclass invariants ──────────────────────────


def test_receipt_fields_has_per_field_confidence() -> None:
    """The merchant directive: per-field confidence, not just
    overall. W1.4 will need it. Pin the field shape so a future
    refactor can't silently delete the per-field surface."""
    from core.receipt_extraction import FieldConfidence, ReceiptFields

    rf = ReceiptFields()
    for field_name in (
        "iban_confidence",
        "beneficiary_confidence",
        "bank_brand_confidence",
        "amount_confidence",
        "reference_confidence",
        "date_confidence",
        "overall_confidence",
    ):
        assert hasattr(rf, field_name), field_name
        assert isinstance(getattr(rf, field_name), FieldConfidence)


def test_receipt_fields_default_is_absent_and_empty() -> None:
    from core.receipt_extraction import FieldConfidence, ReceiptFields

    rf = ReceiptFields()
    assert rf.is_empty is True
    assert rf.iban_confidence == FieldConfidence.ABSENT
    assert rf.overall_confidence == FieldConfidence.ABSENT


def test_receipt_fields_is_empty_property() -> None:
    from core.receipt_extraction import ReceiptFields, FieldConfidence

    rf = ReceiptFields(
        ibans=("SA0380000000608010167519",),
        iban_confidence=FieldConfidence.HIGH,
        overall_confidence=FieldConfidence.HIGH,
    )
    assert rf.is_empty is False


def test_receipt_fields_is_frozen() -> None:
    from core.receipt_extraction import ReceiptFields

    rf = ReceiptFields()
    with pytest.raises((AttributeError, Exception)):
        rf.ibans = ("X",)  # type: ignore[misc]


def test_receipt_fields_to_log_dict_has_canonical_keys() -> None:
    from core.receipt_extraction import ReceiptFields

    rf = ReceiptFields()
    payload = rf.to_log_dict()
    expected = {
        "extractor",
        "source_text_field",
        "source_text_length",
        "source_text_was_truncated",
        "source_text_preview_len",
        "source_text_full_len",
        "iban_count",
        "iban_confidence",
        "beneficiary_count",
        "beneficiary_confidence",
        "bank_brand_count",
        "bank_brand_confidence",
        "amount_count",
        "amount_confidence",
        "reference_count",
        "reference_confidence",
        "date_count",
        "date_confidence",
        "overall_confidence",
    }
    assert set(payload.keys()) == expected


# ── 3. Extractor abstraction ────────────────────────────────────────


def test_default_extractor_registered() -> None:
    from core.receipt_extraction import list_registered_extractors

    names = list_registered_extractors()
    assert "regex_heuristic" in names


def test_register_extractor_rejects_non_extractor() -> None:
    from core.receipt_extraction import register_extractor

    with pytest.raises(TypeError):
        register_extractor("not_an_extractor")  # type: ignore[arg-type]


def test_register_extractor_rejects_blank_or_abstract_name() -> None:
    from core.receipt_extraction import (
        ReceiptFields, ReceiptFieldsExtractor, register_extractor,
    )

    class _BlankExtractor(ReceiptFieldsExtractor):
        name = ""

        def extract(self, *, text, metadata):
            return ReceiptFields()

    class _AbstractExtractor(ReceiptFieldsExtractor):
        name = "abstract"

        def extract(self, *, text, metadata):
            return ReceiptFields()

    with pytest.raises(ValueError):
        register_extractor(_BlankExtractor())
    with pytest.raises(ValueError):
        register_extractor(_AbstractExtractor())


def test_register_extractor_replaces_by_name() -> None:
    """Registering a new extractor with an existing name replaces
    it in place — keeps the registry idempotent for tests / hot
    reload."""
    from core.receipt_extraction import (
        ReceiptFields, ReceiptFieldsExtractor,
        list_registered_extractors, register_extractor,
    )

    class _StubExtractor(ReceiptFieldsExtractor):
        name = "regex_heuristic"

        def extract(self, *, text, metadata):
            return ReceiptFields(source_engine=self.name)

    register_extractor(_StubExtractor())
    names = list_registered_extractors()
    assert names.count("regex_heuristic") == 1

    # Re-register the real one to leave the global state clean for
    # other tests in this session.
    from core.receipt_extraction import RegexHeuristicExtractor
    register_extractor(RegexHeuristicExtractor())


# ── 4. Regex extractor coverage ─────────────────────────────────────


def test_regex_extractor_is_pure_and_never_raises() -> None:
    from core.receipt_extraction import RegexHeuristicExtractor

    e = RegexHeuristicExtractor()
    rf = e.extract(text=None, metadata=None)  # type: ignore[arg-type]
    assert rf.is_empty is True
    rf = e.extract(text="\x00\x01garbage🔥", metadata={"caption": object()})
    assert rf.source_engine == "regex_heuristic"


def test_regex_extractor_pulls_iban() -> None:
    from core.receipt_extraction import (
        FieldConfidence, RegexHeuristicExtractor,
    )

    text = (
        "تم التحويل بنجاح\n"
        "إلى الحساب SA03 8000 0000 6080 1016 7519\n"
        "اسم المستفيد: شركة نحلة\n"
    )
    rf = RegexHeuristicExtractor().extract(text=text, metadata={})

    assert rf.ibans == ("SA0380000000608010167519",)
    assert rf.iban_confidence == FieldConfidence.HIGH
    assert "نحله" in "".join(rf.beneficiaries) or rf.beneficiaries
    assert rf.beneficiary_confidence == FieldConfidence.HIGH
    assert rf.overall_confidence == FieldConfidence.HIGH


def test_regex_extractor_pulls_bank_brand() -> None:
    from core.receipt_extraction import (
        FieldConfidence, RegexHeuristicExtractor,
    )

    rf = RegexHeuristicExtractor().extract(
        text="مصرف الراجحي - تأكيد العملية", metadata={},
    )
    assert rf.bank_brands
    assert rf.bank_brand_confidence == FieldConfidence.HIGH


def test_regex_extractor_pulls_currency_labelled_amount() -> None:
    from core.receipt_extraction import (
        FieldConfidence, RegexHeuristicExtractor,
    )

    rf = RegexHeuristicExtractor().extract(
        text="المبلغ 250.00 ر.س", metadata={},
    )
    assert rf.amounts, rf.amounts
    assert rf.amounts[0].currency == "SAR"
    assert rf.amount_confidence == FieldConfidence.HIGH


def test_regex_extractor_amount_without_currency_requires_money_context() -> None:
    """A bare number without currency or money-context shouldn't
    trip the amount extractor — those are reference-shaped digits."""
    from core.receipt_extraction import RegexHeuristicExtractor

    rf = RegexHeuristicExtractor().extract(
        text="رقم العملية 1234567890", metadata={},
    )
    assert rf.amounts == ()


def test_regex_extractor_pulls_reference_label() -> None:
    from core.receipt_extraction import (
        FieldConfidence, RegexHeuristicExtractor,
    )

    rf = RegexHeuristicExtractor().extract(
        text="رقم العملية: 1234567890", metadata={},
    )
    assert "1234567890" in rf.references
    assert rf.reference_confidence == FieldConfidence.HIGH


def test_regex_extractor_pulls_date() -> None:
    from core.receipt_extraction import (
        FieldConfidence, RegexHeuristicExtractor,
    )

    rf = RegexHeuristicExtractor().extract(
        text="تاريخ العملية 2026-05-25", metadata={},
    )
    assert rf.dates
    assert rf.date_confidence == FieldConfidence.HIGH


def test_regex_extractor_does_not_promote_iban_digits_as_reference() -> None:
    """The bare-reference fallback must skip digit runs that are
    substrings of an extracted IBAN."""
    from core.receipt_extraction import RegexHeuristicExtractor

    rf = RegexHeuristicExtractor().extract(
        text="إلى الحساب SA0380000000608010167519",
        metadata={},
    )
    assert rf.ibans == ("SA0380000000608010167519",)
    # No labelled reference; bare-reference fallback should NOT
    # surface a chunk of the IBAN as a transaction id.
    assert all(
        "0380000000608010167519" not in r and r != "0380000000608010167519"
        for r in rf.references
    ), rf.references


def test_overall_is_max_of_per_field() -> None:
    from core.receipt_extraction import (
        FieldConfidence, RegexHeuristicExtractor,
    )

    rf = RegexHeuristicExtractor().extract(
        text="تم التحويل بنجاح من حساب الراجحي\n"
             "إلى SA0380000000608010167519\n"
             "المبلغ 250 ر.س\n"
             "رقم العملية: 1122334455\n"
             "التاريخ 2026-05-25",
        metadata={},
    )
    assert rf.overall_confidence == FieldConfidence.HIGH


def test_completely_unrelated_text_returns_low_overall() -> None:
    from core.receipt_extraction import (
        FieldConfidence, RegexHeuristicExtractor,
    )

    rf = RegexHeuristicExtractor().extract(
        text="مرحبًا، أبغى أعرف موعد التوصيل",
        metadata={},
    )
    assert rf.overall_confidence == FieldConfidence.ABSENT
    assert rf.is_empty is True


# ── 5. compose_full_evidence_text — truncation telemetry ────────────


def test_compose_prefers_pdf_text_full_over_preview() -> None:
    """The 280-char preview problem from the merchant directive:
    when both ``pdf_text_full`` and ``pdf_text_preview`` are
    populated, extraction must read the full text. The preview
    stays for Brain/UI elsewhere."""
    from core.receipt_extraction import compose_full_evidence_text

    long_body = "X" * 1500 + " SA0380000000608010167519 Y"
    md = {
        "pdf_text_full": long_body,
        "pdf_text_preview": long_body[:280].replace("\n", " "),
        "caption": "إيصال",
    }
    text, source_field, was_truncated, preview_len, full_len = (
        compose_full_evidence_text(md)
    )
    assert source_field == "pdf_text_full"
    assert was_truncated is False
    assert "SA0380000000608010167519" in text
    assert preview_len == 280
    assert full_len == len(long_body)


def test_compose_falls_back_to_preview_and_marks_truncation() -> None:
    """When only the legacy preview is populated, the orchestrator
    must surface ``source_text_was_truncated=True`` so on-call can
    quantify how often this is the bottleneck."""
    from core.receipt_extraction import compose_full_evidence_text

    md = {
        "pdf_text_preview": "تم التحويل... [truncated mid-IBAN]",
        "caption": "",
    }
    text, source_field, was_truncated, preview_len, full_len = (
        compose_full_evidence_text(md)
    )
    assert source_field == "pdf_text_preview"
    assert was_truncated is True
    assert "تم التحويل" in text
    assert preview_len == len(md["pdf_text_preview"])


def test_compose_uses_vision_text_for_images() -> None:
    """Image inbounds carry the full vision describer text in
    ``vision_text``; the legacy preview is irrelevant for them."""
    from core.receipt_extraction import compose_full_evidence_text

    md = {"vision_text": "Vision describer says: payment receipt"}
    text, source_field, was_truncated, _, _ = compose_full_evidence_text(md)
    assert source_field == "vision_text"
    assert was_truncated is False
    assert "Vision describer" in text


def test_compose_handles_empty_metadata() -> None:
    from core.receipt_extraction import compose_full_evidence_text

    text, source_field, was_truncated, preview_len, full_len = (
        compose_full_evidence_text({})
    )
    assert text == ""
    assert source_field == ""
    assert was_truncated is False
    assert preview_len == 0
    assert full_len == 0


# ── 6. Orchestrator ─────────────────────────────────────────────────


def test_orchestrator_uses_default_extractor_when_none_provided() -> None:
    from core.receipt_extraction import compute_receipt_fields

    rf = compute_receipt_fields(metadata={
        "pdf_text_full": "إلى SA0380000000608010167519",
    })
    assert rf.source_engine == "regex_heuristic"
    assert rf.ibans == ("SA0380000000608010167519",)


def test_orchestrator_stamps_provenance_fields() -> None:
    from core.receipt_extraction import compute_receipt_fields

    md = {"pdf_text_full": "FULL BODY " * 50}
    rf = compute_receipt_fields(metadata=md)
    assert rf.source_text_field == "pdf_text_full"
    assert rf.source_text_length > 0
    assert rf.source_text_was_truncated is False
    assert rf.source_text_full_len == len(md["pdf_text_full"])


def test_orchestrator_swallows_extractor_exceptions() -> None:
    from core.receipt_extraction import (
        ReceiptFields, ReceiptFieldsExtractor, compute_receipt_fields,
    )

    class _BoomExtractor(ReceiptFieldsExtractor):
        name = "boom"

        def extract(self, *, text, metadata):
            raise RuntimeError("explode")

    rf = compute_receipt_fields(
        metadata={"pdf_text_full": "anything"},
        extractor=_BoomExtractor(),
    )
    assert isinstance(rf, ReceiptFields)
    assert rf.is_empty is True
    assert rf.source_engine == "boom"
    assert rf.source_text_field == "pdf_text_full"


def test_orchestrator_does_not_mutate_metadata() -> None:
    from core.receipt_extraction import compute_receipt_fields

    md = {
        "pdf_text_full": "إلى SA0380000000608010167519",
        "caption": "إيصال",
    }
    snapshot = dict(md)
    compute_receipt_fields(metadata=md)
    assert md == snapshot


# ── 7. Kill switch ──────────────────────────────────────────────────


@pytest.fixture
def _isolate_extraction_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECEIPT_FIELD_EXTRACTION_TELEMETRY_ENABLED", raising=False)


def test_kill_switch_default_off(_isolate_extraction_flag) -> None:
    from core.receipt_extraction import is_receipt_extraction_telemetry_enabled
    assert is_receipt_extraction_telemetry_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_kill_switch_truthy(
    monkeypatch: pytest.MonkeyPatch, val: str,
) -> None:
    from core.receipt_extraction import is_receipt_extraction_telemetry_enabled
    monkeypatch.setenv("RECEIPT_FIELD_EXTRACTION_TELEMETRY_ENABLED", val)
    assert is_receipt_extraction_telemetry_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_kill_switch_falsy(
    monkeypatch: pytest.MonkeyPatch, val: str,
) -> None:
    from core.receipt_extraction import is_receipt_extraction_telemetry_enabled
    monkeypatch.setenv("RECEIPT_FIELD_EXTRACTION_TELEMETRY_ENABLED", val)
    assert is_receipt_extraction_telemetry_enabled() is False


# ── 8. Log emission ─────────────────────────────────────────────────


def test_log_line_carries_all_canonical_fields(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RECEIPT_FIELD_EXTRACTION_TELEMETRY_ENABLED", "1")
    caplog.set_level(logging.INFO, logger="nahla.receipt_extraction")

    from core.receipt_extraction import (
        compute_receipt_fields, log_receipt_fields,
    )
    rf = compute_receipt_fields(metadata={
        "pdf_text_full": (
            "تم التحويل بنجاح من حساب الراجحي\n"
            "إلى SA0380000000608010167519\n"
            "اسم المستفيد: شركة نحلة\n"
            "المبلغ 250.00 ر.س\n"
            "رقم العملية: 9988776655\n"
            "التاريخ 2026-05-25\n"
        ),
    })
    log_receipt_fields(
        tenant_id=33, phone="+966500000999",
        conversation_id=909, message_id="wamid.XYZ",
        source="receipt_inbound", fields=rf,
    )

    line = next(
        m for m in (r.getMessage() for r in caplog.records)
        if "[PAYMENT_RECEIPT_EXTRACTED]" in m
    )
    assert "tenant_id=33" in line
    assert "conversation_id=909" in line
    assert "message_id=wamid.XYZ" in line
    assert "*0999" in line
    assert "+966500000999" not in line
    assert "extractor=regex_heuristic" in line
    assert "source_text_field=pdf_text_full" in line
    assert "source_text_was_truncated=False" in line
    assert "iban_count=1" in line
    assert "iban_confidence=high" in line
    assert "beneficiary_confidence=high" in line
    assert "amount_confidence=high" in line
    assert "reference_confidence=high" in line
    assert "date_confidence=high" in line
    assert "overall_confidence=high" in line


def test_log_emission_inert_with_flag_off(
    caplog: pytest.LogCaptureFixture, _isolate_extraction_flag,
) -> None:
    caplog.set_level(logging.INFO, logger="nahla.receipt_extraction")

    from core.receipt_extraction import (
        ReceiptFields, log_receipt_fields,
    )
    rf = ReceiptFields()
    log_receipt_fields(
        tenant_id=33, phone="+966500000999",
        source="receipt_inbound", fields=rf,
    )
    assert not any(
        "[PAYMENT_RECEIPT_EXTRACTED]" in r.getMessage()
        for r in caplog.records
    )


def test_log_emission_never_raises_on_garbage() -> None:
    from core.receipt_extraction import ReceiptFields, log_receipt_fields

    log_receipt_fields(  # type: ignore[arg-type]
        tenant_id={"weird": True}, phone=None,
        conversation_id=[1, 2], message_id=None,
        source="x", fields=ReceiptFields(),
    )


# ── 9. Wiring at order-flow short-circuit sites ────────────────────


def test_w13_helper_is_observation_only_with_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RECEIPT_FIELD_EXTRACTION_TELEMETRY_ENABLED", "1")
    from core import order_flow

    recorded: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        "core.receipt_extraction.log_receipt_fields",
        lambda **kw: recorded.append(kw),
    )

    order_flow._w13_emit_receipt_extraction(
        tenant_id=33, phone="+966500000999",
        conversation_id=909, message_id="wamid.123",
        source="receipt_inbound",
        metadata={
            "pdf_text_full": "SA0380000000608010167519 ر.س 250",
        },
    )
    assert len(recorded) == 1
    assert recorded[0]["source"] == "receipt_inbound"
    fields = recorded[0]["fields"]
    assert fields.ibans == ("SA0380000000608010167519",)


def test_w13_helper_inert_with_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "RECEIPT_FIELD_EXTRACTION_TELEMETRY_ENABLED", raising=False,
    )
    from core import order_flow

    recorded: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        "core.receipt_extraction.log_receipt_fields",
        lambda **kw: recorded.append(kw),
    )
    order_flow._w13_emit_receipt_extraction(
        tenant_id=33, phone="+966500000999",
        source="receipt_inbound",
        metadata={"pdf_text_full": "anything"},
    )
    # Helper short-circuits before computing OR logging when the
    # flag is off — keeps the hot path zero-cost.
    assert recorded == []


def test_w13_helper_swallows_compute_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RECEIPT_FIELD_EXTRACTION_TELEMETRY_ENABLED", "1")

    def _raise(**_kw: Any) -> Any:
        raise RuntimeError("compute exploded")

    monkeypatch.setattr(
        "core.receipt_extraction.compute_receipt_fields", _raise,
    )

    from core import order_flow
    # Must not raise — telemetry must never break the pipeline.
    order_flow._w13_emit_receipt_extraction(
        tenant_id=33, phone="+966500000999",
        source="receipt_inbound",
        metadata={"pdf_text_full": "anything"},
    )


def test_receipt_inbound_byte_identical_with_extraction_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wiring is observation-only. With
    ``RECEIPT_FIELD_EXTRACTION_TELEMETRY_ENABLED=1`` the receipt
    short-circuit MUST return the same shape (modulo non-deterministic
    timestamps) as with the flag OFF."""
    from core import order_flow

    md = {
        "pdf_kind": "payment_receipt",
        "payment_evidence_status": "confirmed",
        "pdf_text_preview": (
            "تم التحويل\n"
            "إلى SA0380000000608010167519"
        ),
        "pdf_text_full": (
            "تم التحويل بنجاح\n"
            "إلى SA0380000000608010167519\n"
            "اسم المستفيد: نحلة\n"
            "المبلغ 250 ر.س"
        ),
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

    monkeypatch.delenv(
        "RECEIPT_FIELD_EXTRACTION_TELEMETRY_ENABLED", raising=False,
    )
    monkeypatch.delenv("RECEIPT_VERDICT_TELEMETRY_ENABLED", raising=False)
    out_off = order_flow.maybe_handle_receipt_inbound(
        db=object(), tenant_id=33, phone="+966500000999",
        inbound_normalized_type="document",
        inbound_metadata=md,
    )

    monkeypatch.setenv("RECEIPT_FIELD_EXTRACTION_TELEMETRY_ENABLED", "1")
    out_on = order_flow.maybe_handle_receipt_inbound(
        db=object(), tenant_id=33, phone="+966500000999",
        inbound_normalized_type="document",
        inbound_metadata=md,
    )

    assert out_off is not None and out_on is not None
    assert out_off["reply_text"] == out_on["reply_text"]

    def _strip_timestamps(sp: Dict[str, Any]) -> Dict[str, Any]:
        sp = dict(sp)
        sp.pop("payment_receipt_at", None)
        sp.pop("payment_submission_at", None)
        meta = dict(sp.get("payment_receipt_metadata") or {})
        meta.pop("received_at", None)
        if meta:
            sp["payment_receipt_metadata"] = meta
        return sp

    sp_off = _strip_timestamps(out_off["state_patch"])
    sp_on = _strip_timestamps(out_on["state_patch"])
    assert sp_off == sp_on
