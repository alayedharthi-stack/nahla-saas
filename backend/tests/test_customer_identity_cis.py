"""
tests/test_customer_identity_cis.py
───────────────────────────────────
CRM persistence for B-WIRE-01 contact phone (metadata only).
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services.customer_intelligence import CustomerIntelligenceService  # noqa: E402


def _customer(*, norm: str, meta: dict | None = None):
    row = MagicMock()
    row.id = 42
    row.normalized_phone = norm
    row.extra_metadata = dict(meta or {})
    return row


class TestPersistOrderFlowContactPhone:
    def test_writes_metadata_without_overwriting_normalized_phone(self):
        db = MagicMock()
        svc = CustomerIntelligenceService(db, tenant_id=1)
        channel = "+966500000001"
        alternate = "+966551234567"
        row = _customer(norm=channel)
        svc.find_customer_by_phone = MagicMock(return_value=row)  # type: ignore[method-assign]

        assert svc.persist_order_flow_contact_phone(
            channel_phone=channel,
            contact_phone_raw="0551234567",
        )

        assert row.normalized_phone == channel
        assert row.extra_metadata["contact_phone"] == alternate
        assert row.extra_metadata["shipping_phone"] == alternate
        assert row.extra_metadata["contact_phone_source"] == "ai_order_flow"

    def test_skips_when_contact_matches_channel_identity(self):
        db = MagicMock()
        svc = CustomerIntelligenceService(db, tenant_id=1)
        channel = "+966500000001"
        row = _customer(norm=channel)
        svc.find_customer_by_phone = MagicMock(return_value=row)  # type: ignore[method-assign]

        assert not svc.persist_order_flow_contact_phone(
            channel_phone=channel,
            contact_phone_raw=channel,
        )
        assert "contact_phone" not in (row.extra_metadata or {})

    def test_skips_when_customer_row_missing(self):
        db = MagicMock()
        svc = CustomerIntelligenceService(db, tenant_id=1)
        svc.find_customer_by_phone = MagicMock(return_value=None)  # type: ignore[method-assign]

        assert not svc.persist_order_flow_contact_phone(
            channel_phone="+966500000001",
            contact_phone_raw="0551234567",
        )
