"""Unit contracts for the product-silence delivery recovery helpers.

Pure functions only: provider-outcome classification, verified-candidate
selection, the deterministic grounded catalog list, the ``invalid_payload``
Meta error class and the explicit lifecycle terminals.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.inbound_lifecycle import (  # noqa: E402
    ALL_EVENTS,
    EVENT_DELIVERY_TEXT_RECOVERED,
    EVENT_END_DELIVERY_FAILED,
    EVENT_END_DELIVERY_RECOVERED,
    EVENT_END_OK,
    InboundLifecycleTrace,
)
from core.outbound_send_status import build_provider_send_block  # noqa: E402
from core.product_reply_recovery import (  # noqa: E402
    OUTCOME_AMBIGUOUS_PROVIDER,
    OUTCOME_RICH_REJECTED_TEXT_RECOVERED,
    OUTCOME_TERMINAL_FAILURE,
    PROVIDER_ACCEPTED,
    PROVIDER_AMBIGUOUS,
    PROVIDER_BLOCKED,
    PROVIDER_DEFINITIVE_REJECTION,
    PROVIDER_NOT_ATTEMPTED,
    build_grounded_catalog_text,
    choose_recovery_text,
    classify_provider_outcome,
    eligible_recovery_candidates,
    fallback_provenance_metadata,
    provider_error_snapshot,
    record_product_reply_outcome,
)
from services.meta_errors import classify_meta_error  # noqa: E402

_DUP_ERROR = {
    "error": {
        "message": "(#100) Invalid parameter",
        "type": "OAuthException",
        "code": 100,
        "error_data": {"messaging_product": "whatsapp", "details": "Duplicate button title"},
        "fbtrace_id": "AbCdEf",
    }
}


# ── Provider outcome classification ──────────────────────────────────────────


@pytest.mark.parametrize(
    "sink, send_ok, expected",
    [
        ({"classification": "ok", "wamid": "wamid.1"}, True, PROVIDER_ACCEPTED),
        ({"classification": "ok", "wamid": "wamid.1", "duplicate_suppressed": True}, True, PROVIDER_ACCEPTED),
        ({"classification": "non_2xx", "http_status": 400, "response_body": _DUP_ERROR}, False, PROVIDER_DEFINITIVE_REJECTION),
        ({"classification": "provider_error_field", "http_status": 200, "response_body": _DUP_ERROR}, False, PROVIDER_DEFINITIVE_REJECTION),
        ({"classification": "non_2xx", "http_status": 503}, False, PROVIDER_AMBIGUOUS),
        ({"classification": "non_2xx", "http_status": None}, False, PROVIDER_AMBIGUOUS),
        ({"classification": "exception", "error_text": "ReadTimeout: x"}, False, PROVIDER_AMBIGUOUS),
        ({"classification": "missing_wamid"}, False, PROVIDER_AMBIGUOUS),
        ({"classification": "automation_blocked"}, False, PROVIDER_BLOCKED),
        ({"classification": "recipient_invalid"}, False, PROVIDER_BLOCKED),
        ({}, False, PROVIDER_NOT_ATTEMPTED),
        (None, False, PROVIDER_NOT_ATTEMPTED),
    ],
)
def test_classify_provider_outcome_matrix(sink, send_ok, expected) -> None:
    assert classify_provider_outcome(sink, send_ok=send_ok) == expected


def test_provider_error_snapshot_preserves_original_rejection() -> None:
    snap = provider_error_snapshot({
        "classification": "non_2xx", "http_status": 400, "response_body": _DUP_ERROR,
    })
    assert snap["classification"] == "non_2xx"
    assert snap["http_status"] == 400
    assert snap["code"] == 100
    assert "Duplicate button title" in snap["detail"]
    assert snap["fbtrace_id"] == "AbCdEf"
    assert snap["key"] == "invalid_payload"


def test_provider_error_snapshot_exception_is_ambiguous_not_phone() -> None:
    snap = provider_error_snapshot({"classification": "exception", "error_text": "ReadTimeout: t"})
    assert snap["key"] == "exception"
    assert "ReadTimeout" in snap["detail"]
    assert provider_error_snapshot({"classification": "ok", "wamid": "w"}) == {}


# ── Meta error class: structured payload rejection ≠ invalid phone ───────────


def test_duplicate_button_title_is_invalid_payload_not_invalid_phone() -> None:
    err = _DUP_ERROR["error"]
    classified = classify_meta_error(
        code=err["code"], error_type=err["type"], message=err["message"], raw_response=_DUP_ERROR,
    )
    assert classified.key == "invalid_payload"
    assert classified.suppress_on_repeat is False, "our payload bug must not penalise the phone"
    assert classified.retryable is False
    assert classified.is_recoverable is True


def test_code_100_phone_rejection_still_invalid_phone() -> None:
    body = {"error": {"message": "(#100) Param to is not a valid phone number", "code": 100}}
    classified = classify_meta_error(code=100, message=body["error"]["message"], raw_response=body)
    assert classified.key == "invalid_phone"


def test_outbound_send_status_block_keeps_details_and_invalid_payload_key() -> None:
    block = build_provider_send_block(
        classification="non_2xx", response_body=_DUP_ERROR, wamid=None, operation="send_message",
    )
    assert block["status"] == "failed"
    assert block["error"]["key"] == "invalid_payload"
    assert block["error"]["details"] == "Duplicate button title"
    assert block["error"]["code"] == 100


# ── Verified candidates → grounded text ──────────────────────────────────────


def _state(rows):
    return {"last_search_candidates": rows, "last_action": "search_products"}


def test_eligible_candidates_come_only_from_current_turn_state() -> None:
    rows = [
        {"id": 1, "title": "قميص قطني أزرق", "price": "120", "in_stock": True},
        {"id": 2, "title": "", "price": "1"},
        {"id": 3, "title": "عطر ورد 100ml", "price": "250", "in_stock": True},
    ]
    out = eligible_recovery_candidates(_state(rows))
    assert [r["id"] for r in out] == [1, 3]
    assert eligible_recovery_candidates({}) == []
    assert eligible_recovery_candidates(None) == []


def test_eligible_candidates_narrow_to_compose_ids_when_they_match() -> None:
    rows = [
        {"id": 1, "title": "قميص قطني أزرق"},
        {"id": 2, "title": "حذاء رياضي أبيض"},
    ]
    out = eligible_recovery_candidates(_state(rows), brain_result={"catalog_product_ids": [2]})
    assert [r["id"] for r in out] == [2]
    # Unmatched ids (e.g. external ids) keep the verified stored list.
    out2 = eligible_recovery_candidates(_state(rows), brain_result={"catalog_product_ids": ["x"]})
    assert [r["id"] for r in out2] == [1, 2]


def test_grounded_text_uses_only_authorised_fields() -> None:
    rows = [
        {"id": 1, "title": "فستان", "price": "250", "in_stock": True,
         "description": "وصف مخترع", "product_url": "https://example.test/p/1"},
        {"id": 2, "title": "فستان", "price": "300", "sale_price": "199", "in_stock": True},
        {"id": 3, "title": "جاكيت", "price": "400", "in_stock": False},
    ]
    text = build_grounded_catalog_text(rows)
    lines = text.splitlines()
    assert lines[0].startswith("1. فستان") and "250 ر.س" in lines[0]
    assert lines[1].startswith("2. فستان") and "~~300 ر.س~~ 199 ر.س" in lines[1]
    assert lines[2].startswith("3. جاكيت") and "غير متوفر حالياً" in lines[2]
    assert "غير متوفر" not in lines[0] and "غير متوفر" not in lines[1]
    assert "وصف مخترع" not in text
    assert "http" not in text
    assert build_grounded_catalog_text([]) == ""
    assert build_grounded_catalog_text([{"id": 9, "price": "10"}]) == ""


def test_choose_recovery_text_prefers_surviving_guarded_reply() -> None:
    rows = [{"id": 1, "title": "قميص قطني أزرق", "price": "120"}]
    assert choose_recovery_text(guarded_reply="  نص محروس  ", candidates=rows) == ("نص محروس", "guarded_reply")
    text, source = choose_recovery_text(guarded_reply="", candidates=rows)
    assert source == "fallback_deterministic" and "قميص قطني أزرق" in text
    assert choose_recovery_text(guarded_reply="", candidates=[]) == ("", "")


def test_fallback_provenance_is_constitution_complete() -> None:
    meta = fallback_provenance_metadata(fallback_reason="pre_provider_suppression")
    assert meta["compose_source"] == "fallback_deterministic"
    assert meta["fallback_reason"] == "pre_provider_suppression"
    assert meta["fallback_action_type"] == "catalog_grounded_text_recovery"
    for key in ("response_mode", "chosen_path", "llm_candidate_present",
                "final_text_transformed", "final_transform_reasons"):
        assert key in meta


# ── Lifecycle terminals ───────────────────────────────────────────────────────


def test_lifecycle_vocabulary_has_delivery_terminals() -> None:
    for tok in (EVENT_DELIVERY_TEXT_RECOVERED, EVENT_END_DELIVERY_RECOVERED, EVENT_END_DELIVERY_FAILED):
        assert tok in ALL_EVENTS
    trace = InboundLifecycleTrace(trace_id="t", started_monotonic=0.0)
    trace.record(EVENT_END_DELIVERY_RECOVERED)
    assert trace.final_token == EVENT_END_DELIVERY_RECOVERED
    trace2 = InboundLifecycleTrace(trace_id="t2", started_monotonic=0.0)
    trace2.record(EVENT_END_DELIVERY_FAILED, detail="outcome=ambiguous_provider_outcome")
    assert trace2.final_token == EVENT_END_DELIVERY_FAILED
    assert trace2.final_token != EVENT_END_OK


def test_record_outcome_stamps_audit_without_active_trace() -> None:
    audit = {"text_sent": True}
    out = record_product_reply_outcome(
        audit, outcome=OUTCOME_RICH_REJECTED_TEXT_RECOVERED, final_mode="text_only",
    )
    assert out["product_reply_outcome"] == OUTCOME_RICH_REJECTED_TEXT_RECOVERED
    assert out["final_delivery_mode"] == "text_only"
    assert out["text_recovery_attempts"] == 0
    for outcome in (OUTCOME_AMBIGUOUS_PROVIDER, OUTCOME_TERMINAL_FAILURE):
        assert record_product_reply_outcome({}, outcome=outcome, final_mode="failed")[
            "product_reply_outcome"
        ] == outcome
