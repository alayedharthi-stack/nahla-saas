"""API error contract for WhatsApp connection conflicts."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from services.whatsapp_connection_service import (  # noqa: E402
    CONFLICT_SAFE_MESSAGES,
    WhatsAppConnectionConflict,
    connection_conflict_http_detail,
)


def test_connection_conflict_http_detail_uses_safe_message_not_raw_exception() -> None:
    exc = WhatsAppConnectionConflict("raw internal tenant=99 phone=123", code="CONFLICT_PHONE_CLAIMED")
    detail = connection_conflict_http_detail(exc)
    assert detail["code"] == "CONFLICT_PHONE_CLAIMED"
    assert detail["message"] == CONFLICT_SAFE_MESSAGES["CONFLICT_PHONE_CLAIMED"]
    assert "tenant=99" not in detail["message"]
    assert "phone=123" not in detail["message"]


def test_connection_conflict_http_detail_fallback_code() -> None:
    exc = WhatsAppConnectionConflict("internal", code="UNKNOWN_CODE")
    detail = connection_conflict_http_detail(exc)
    assert detail["code"] == "UNKNOWN_CODE"
    assert detail["message"] == CONFLICT_SAFE_MESSAGES["CONFLICT_ASSET_CLAIMED"]
