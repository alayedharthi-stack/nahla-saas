"""Outbound dedup — contacts/vCard signature regression (P0 hotfix)."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.outbound_dedup import (  # noqa: E402
    _payload_signature,
    check_outbound_send,
    clear_outbound_dedup,
    record_outbound_result,
)


def _contact_payload(*, wa_id: str, phone: str, name: str) -> dict:
    return {
        "messaging_product": "whatsapp",
        "to": "966549815590",
        "type": "contacts",
        "contacts": [
            {
                "name": {"formatted_name": name, "first_name": name},
                "phones": [
                    {"phone": phone, "wa_id": wa_id, "type": "WORK"},
                ],
            },
        ],
    }


@pytest.fixture(autouse=True)
def _clear_dedup_cache() -> None:
    clear_outbound_dedup()
    yield
    clear_outbound_dedup()


class TestContactsDedupSignatures:
    def test_different_wa_id_are_not_duplicates(self) -> None:
        staff_a = _contact_payload(
            wa_id="966541690226",
            phone="+966541690226",
            name="Staff A",
        )
        staff_b = _contact_payload(
            wa_id="966549815590",
            phone="+966549815590",
            name="Staff B",
        )
        sig_a = _payload_signature(staff_a)
        sig_b = _payload_signature(staff_b)
        assert sig_a != sig_b

        tenant_id = 33
        recipient = "966549815590"
        first = check_outbound_send(
            tenant_id=tenant_id, recipient=recipient, payload=staff_a,
        )
        assert first.skip is False
        record_outbound_result(
            tenant_id=tenant_id,
            recipient=recipient,
            payload=staff_a,
            wamid="wamid.first",
            succeeded=True,
        )

        second = check_outbound_send(
            tenant_id=tenant_id, recipient=recipient, payload=staff_b,
        )
        assert second.skip is False

    def test_same_contact_still_suppressed_within_window(self) -> None:
        payload = _contact_payload(
            wa_id="966541690226",
            phone="+966541690226",
            name="Staff A",
        )
        tenant_id = 33
        recipient = "966549815590"

        assert check_outbound_send(
            tenant_id=tenant_id, recipient=recipient, payload=payload,
        ).skip is False
        record_outbound_result(
            tenant_id=tenant_id,
            recipient=recipient,
            payload=payload,
            wamid="wamid.same",
            succeeded=True,
        )

        dup = check_outbound_send(
            tenant_id=tenant_id, recipient=recipient, payload=payload,
        )
        assert dup.skip is True
        assert dup.reason == "already_sent"
        assert dup.wamid == "wamid.same"

    def test_signature_includes_name_and_phone(self) -> None:
        base = _contact_payload(
            wa_id="966541690226",
            phone="+966541690226",
            name="Staff A",
        )
        renamed = _contact_payload(
            wa_id="966541690226",
            phone="+966541690226",
            name="Staff A Renamed",
        )
        different_phone = _contact_payload(
            wa_id="966541690227",
            phone="+966541690227",
            name="Staff A",
        )
        assert _payload_signature(base) != _payload_signature(renamed)
        assert _payload_signature(base) != _payload_signature(different_phone)


class TestTextTemplateDedupUnchanged:
    def test_text_same_body_still_deduplicates(self) -> None:
        payload = {
            "type": "text",
            "to": "966549815590",
            "text": {"body": "مرحباً"},
        }
        tenant_id = 33
        recipient = "966549815590"

        assert check_outbound_send(
            tenant_id=tenant_id, recipient=recipient, payload=payload,
        ).skip is False
        record_outbound_result(
            tenant_id=tenant_id,
            recipient=recipient,
            payload=payload,
            wamid="wamid.text",
            succeeded=True,
        )
        dup = check_outbound_send(
            tenant_id=tenant_id, recipient=recipient, payload=payload,
        )
        assert dup.skip is True
        assert dup.reason == "already_sent"

    def test_text_signature_unchanged_by_contacts_fix(self) -> None:
        payload = {
            "type": "text",
            "to": "966549815590",
            "text": {"body": "نص ثابت للاختبار"},
        }
        sig = _payload_signature(payload)
        assert sig == _payload_signature(dict(payload))

    def test_template_signature_still_stable(self) -> None:
        payload = {
            "type": "template",
            "to": "966549815590",
            "template": {
                "name": "order_update",
                "language": {"code": "ar"},
                "components": [
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": "123"}],
                    },
                ],
            },
        }
        assert _payload_signature(payload) == _payload_signature(dict(payload))
