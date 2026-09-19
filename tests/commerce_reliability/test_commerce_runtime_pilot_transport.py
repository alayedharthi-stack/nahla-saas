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


# ── The schema probe's cache ─────────────────────────────────────────────────


class _FakeUrl:
    def __init__(self, text: str) -> None:
        self._text = text

    def __str__(self) -> str:
        return self._text


class _FakeEngine:
    def __init__(self, url: str) -> None:
        self.url = _FakeUrl(url)
        self.connects = 0

    def connect(self):
        self.connects += 1
        raise RuntimeError("no database here")


def test_the_schema_probe_is_remembered_per_database_not_per_object(monkeypatch):
    entry.reset_schema_probe()
    first = _FakeEngine("postgresql://u:***@db-a/nahla")
    assert entry.runtime_schema_available(first) is False
    assert entry.runtime_schema_available(first) is False
    assert first.connects == 1                      # probed once, then remembered

    same_database = _FakeEngine("postgresql://u:***@db-a/nahla")
    assert entry.runtime_schema_available(same_database) is False
    assert same_database.connects == 0              # a new object, the same database

    other = _FakeEngine("postgresql://u:***@db-b/nahla")
    assert entry.runtime_schema_available(other) is False
    assert other.connects == 1                      # a different database is probed
    entry.reset_schema_probe()


def test_an_engine_that_cannot_name_itself_is_probed_every_time():
    entry.reset_schema_probe()

    class Nameless(_FakeEngine):
        @property
        def url(self):
            raise RuntimeError("no url")

        @url.setter
        def url(self, value):
            pass

    engine = Nameless("x")
    assert entry.runtime_schema_available(engine) is False
    assert entry.runtime_schema_available(engine) is False
    assert engine.connects == 1                     # keyed by identity as a last resort
    entry.reset_schema_probe()
