"""Regression: coexistence WABA resolution prefers hints over first scope."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from services.embedded_waba_resolution import (  # noqa: E402
    _digits,
    _waba_ids_from_debug,
    resolve_embedded_waba_id,
)


def test_digits_normalizes_phone():
    assert _digits("+966 55 590 6901") == "966555906901"


def test_waba_ids_from_debug_preserves_order():
    debug = {
        "granular_scopes": [
            {"scope": "whatsapp_business_management", "target_ids": ["111", "222"]},
        ],
    }
    assert _waba_ids_from_debug(debug) == ["111", "222"]


def test_resolve_prefers_session_waba_hint(monkeypatch):
    async def _ok(graph, token, waba_id):  # noqa: ANN001
        return waba_id == "TARGET"

    monkeypatch.setattr(
        "services.embedded_waba_resolution._can_read_waba",
        _ok,
    )
    waba = asyncio.run(
        resolve_embedded_waba_id(
            "https://graph.facebook.com/v21.0",
            "token",
            {"granular_scopes": [{"scope": "whatsapp_business_management", "target_ids": ["OLD"]}]},
            hinted_waba_id="TARGET",
        )
    )
    assert waba == "TARGET"
