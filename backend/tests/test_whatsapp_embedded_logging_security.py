"""Caplog security tests for embedded signup logging."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend")):
    if entry not in sys.path:
        sys.path.insert(0, entry)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.log_redaction import redact_graph_id  # noqa: E402
from core.tenant_integrity import TenantIntegrityError  # noqa: E402
from routers import whatsapp_embedded as we  # noqa: E402

PHONE_ID = "EMBEDDED-PHONE-CANARY-877"
WABA = "EMBEDDED-WABA-CANARY-877"
E164 = "+966501112233"
TENANT = 990877


def _assert_absent(text: str) -> None:
    for value in (PHONE_ID, WABA, E164):
        assert value not in text


def test_select_phone_conflict_log_redacts_phone_id(caplog):
    caplog.set_level(logging.ERROR, logger="nahla-backend")
    conn = SimpleNamespace(
        tenant_id=TENANT,
        phone_number_id=None,
        whatsapp_business_account_id=WABA,
        connection_type="embedded",
        provider="meta",
        extra_metadata={},
    )
    request = SimpleNamespace(state=SimpleNamespace(tenant_id=TENANT), headers={})
    body = SimpleNamespace(phone_number_id=PHONE_ID)
    db = SimpleNamespace(
        query=lambda *a, **k: SimpleNamespace(filter_by=lambda **kw: SimpleNamespace(first=lambda: conn)),
        commit=lambda: None,
    )
    with patch.object(we, "resolve_tenant_id", return_value=TENANT), \
         patch.object(we, "_is_coexistence_conn", return_value=False), \
         patch.object(we, "_get_phone_details_with_fallback", new=AsyncMock(return_value=({"id": PHONE_ID}, "merchant_oauth"))), \
         patch("core.tenant_integrity.assert_phone_id_not_claimed", side_effect=TenantIntegrityError("claimed")):
        with pytest.raises(Exception):
            asyncio.run(we.select_phone(body, request, db))
    _assert_absent(caplog.text)
    assert redact_graph_id(PHONE_ID) in caplog.text


def test_add_phone_start_log_redacts_phone(caplog):
    caplog.set_level(logging.INFO, logger=we.logger.name)
    request = SimpleNamespace(state=SimpleNamespace(tenant_id=TENANT), headers={"origin": "https://app.test"})
    body = SimpleNamespace(country_code="966", phone_number=E164, verified_name="Test", code_method="SMS")
    conn = SimpleNamespace(tenant_id=TENANT, whatsapp_business_account_id=WABA)
    db = SimpleNamespace(query=lambda *a, **k: SimpleNamespace(filter_by=lambda **kw: SimpleNamespace(first=lambda: conn)))
    with patch.object(we, "resolve_tenant_id", return_value=TENANT), \
         patch.object(we, "_candidate_graph_tokens", return_value=[]):
        with pytest.raises(Exception):
            asyncio.run(we.add_phone(body, request, db))
    _assert_absent(caplog.text)
    assert "phone_present=True" in caplog.text
