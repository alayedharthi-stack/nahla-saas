"""What the WhatsApp transport reported, translated for the ledger (no network).

The ledger decides what a send established; this boundary only has to report
what the provider actually said. These cases prove it never turns a refusal,
a timeout or a missing message id into an acceptance.
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

import pytest

from core.commerce_runtime import ledger_contracts as lc
from core.commerce_runtime import runtime_entry as entry


def transport_for(answer: Tuple[str, Optional[str], Optional[int]], *, seen: List[Any] = None):
    def send(recipient: str, text: str):
        if seen is not None:
            seen.append((recipient, text))
        return answer
    return entry.whatsapp_text_transport(send, recipient="+966500000001")


def test_an_identified_acceptance_is_the_only_shape_that_becomes_accepted():
    transport = transport_for(("ok", "wamid.OK", 200))
    kind, pmid = lc.classify_send_response(transport({"text": "مرحبا"}))
    assert kind == lc.ReceiptKind.ACCEPTED and pmid == "wamid.OK"


def test_a_success_without_a_message_id_is_unknown_not_accepted():
    transport = transport_for(("ok", None, 200))
    kind, pmid = lc.classify_send_response(transport({"text": "مرحبا"}))
    assert kind == lc.ReceiptKind.UNKNOWN and pmid is None


@pytest.mark.parametrize("classification, status, expected", [
    ("blocked", 400, lc.ReceiptKind.REJECTED),
    ("provider_error_field", 403, lc.ReceiptKind.REJECTED),
    ("exception", 500, lc.ReceiptKind.UNKNOWN),
    ("throttled", 429, lc.ReceiptKind.REJECTED),
])
def test_a_refusal_keeps_the_status_the_provider_gave_it(classification, status, expected):
    transport = transport_for((classification, None, status))
    kind, pmid = lc.classify_send_response(transport({"text": "مرحبا"}))
    assert kind == expected and pmid is None


def test_no_status_at_all_is_unknown_and_never_a_proven_rejection():
    transport = transport_for(("timeout", None, None))
    response = transport({"text": "مرحبا"})
    assert response.timed_out is True and response.http_status is None
    kind, _ = lc.classify_send_response(response)
    assert kind == lc.ReceiptKind.UNKNOWN


def test_a_message_id_without_an_ok_classification_is_not_an_acceptance():
    transport = transport_for(("provider_error_field", "wamid.PARTIAL", 400))
    kind, pmid = lc.classify_send_response(transport({"text": "مرحبا"}))
    assert kind == lc.ReceiptKind.REJECTED and pmid is None


def test_the_transport_is_handed_the_reserved_text_and_the_trusted_recipient():
    seen: List[Any] = []
    transport = transport_for(("ok", "wamid.OK", 200), seen=seen)
    transport({"text": "النص المحجوز", "evidence_refs": []})
    assert seen == [("+966500000001", "النص المحجوز")]


def test_a_reserved_payload_without_text_still_reaches_the_transport_as_empty():
    seen: List[Any] = []
    transport = transport_for(("ok", "wamid.OK", 200), seen=seen)
    transport({"evidence_refs": []})
    assert seen == [("+966500000001", "")]


def test_the_pilot_bounds_how_long_it_waits_for_one_send():
    from services import commerce_runtime_pilot as pilot

    assert 0 < pilot.SEND_WAIT_SECONDS <= 120
