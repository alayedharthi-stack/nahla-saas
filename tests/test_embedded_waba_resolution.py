"""Coexistence WABA resolution — graph proof only, no client hints."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from services.embedded_waba_resolution import (  # noqa: E402
    CoexistenceWabaResolutionError,
    REAUTH_REQUIRED,
    WABA_RESOLUTION_CONFLICT,
    WRONG_PHONE,
    resolve_coexistence_assets_from_graph,
)


def _debug(waba_ids: list[str], portfolios: list[str] | None = None) -> dict:
    scopes = [
        {
            "scope": "whatsapp_business_management",
            "target_ids": waba_ids,
        },
    ]
    if portfolios:
        scopes.append({"scope": "business_management", "target_ids": portfolios})
    return {"granular_scopes": scopes}


def test_graph_proof_single_match(monkeypatch):
    async def _run():
        async def fake_get(graph_base, token, node, fields):  # noqa: ANN001
            if node == "WABA-A":
                return {
                    "ok": True,
                    "data": {
                        "id": "WABA-A",
                        "ownership_type": "OWNED",
                        "owner_business_info": {"id": "BIZ-1"},
                    },
                }
            if node.endswith("/phone_numbers"):
                return {
                    "ok": True,
                    "data": {"data": [{"id": "PHONE-1", "display_phone_number": "+966501234567"}]},
                }
            if node == "PHONE-1":
                return {"ok": True, "data": {"id": "PHONE-1", "display_phone_number": "+966501234567"}}
            return {"ok": False, "message": "missing"}

        monkeypatch.setattr("services.embedded_waba_resolution._graph_get", fake_get)
        result = await resolve_coexistence_assets_from_graph(
            "https://graph.facebook.com/v21.0",
            "token",
            _debug(["WABA-A"], ["BIZ-1"]),
            expected_phone_number="+966501234567",
            expected_business_portfolio_id="BIZ-1",
        )
        assert result.waba_id == "WABA-A"
        assert result.phone_number_id == "PHONE-1"


    asyncio.run(_run())
def test_client_waba_id_not_used(monkeypatch):
    async def _run():
        calls = []

        async def fake_get(graph_base, token, node, fields):  # noqa: ANN001
            calls.append(node)
            if node == "WABA-TRUSTED":
                return {"ok": True, "data": {"id": "WABA-TRUSTED", "owner_business_info": {"id": "BIZ-1"}}}
            if node.endswith("/phone_numbers"):
                return {"ok": True, "data": {"data": [{"id": "PHONE-1", "display_phone_number": "966501234567"}]}}
            if node == "PHONE-1":
                return {"ok": True, "data": {"id": "PHONE-1"}}
            return {"ok": False}

        monkeypatch.setattr("services.embedded_waba_resolution._graph_get", fake_get)
        result = await resolve_coexistence_assets_from_graph(
            "https://graph.facebook.com/v21.0",
            "token",
            _debug(["WABA-TRUSTED"], ["BIZ-1"]),
            expected_phone_number="0501234567",
        )
        assert result.waba_id == "WABA-TRUSTED"
        assert "WABA-CLIENT" not in calls


    asyncio.run(_run())
def test_wrong_phone_rejected(monkeypatch):
    async def _run():
        async def fake_get(graph_base, token, node, fields):  # noqa: ANN001
            if node == "WABA-A":
                return {"ok": True, "data": {"id": "WABA-A", "owner_business_info": {"id": "BIZ-1"}}}
            if node.endswith("/phone_numbers"):
                return {"ok": True, "data": {"data": [{"id": "PHONE-1", "display_phone_number": "+966509999999"}]}}
            return {"ok": False}

        monkeypatch.setattr("services.embedded_waba_resolution._graph_get", fake_get)
        with pytest.raises(CoexistenceWabaResolutionError) as exc:
            await resolve_coexistence_assets_from_graph(
                "https://graph.facebook.com/v21.0",
                "token",
                _debug(["WABA-A"], ["BIZ-1"]),
                expected_phone_number="+966501234567",
            )
        assert exc.value.code == WRONG_PHONE


    asyncio.run(_run())
def test_multiple_matches_conflict(monkeypatch):
    async def _run():
        async def fake_get(graph_base, token, node, fields):  # noqa: ANN001
            if node in {"WABA-A", "WABA-B"}:
                return {"ok": True, "data": {"id": node, "owner_business_info": {"id": "BIZ-1"}}}
            if node.endswith("/phone_numbers"):
                return {
                    "ok": True,
                    "data": {"data": [{"id": f"PHONE-{node[-1]}", "display_phone_number": "+966501234567"}]},
                }
            if node.startswith("PHONE-"):
                return {"ok": True, "data": {"id": node}}
            return {"ok": False}

        monkeypatch.setattr("services.embedded_waba_resolution._graph_get", fake_get)
        with pytest.raises(CoexistenceWabaResolutionError) as exc:
            await resolve_coexistence_assets_from_graph(
                "https://graph.facebook.com/v21.0",
                "token",
                _debug(["WABA-A", "WABA-B"], ["BIZ-1"]),
                expected_phone_number="+966501234567",
            )
        assert exc.value.code == WABA_RESOLUTION_CONFLICT


    asyncio.run(_run())
def test_zero_granular_targets_reauth():
    async def _run():
        with pytest.raises(CoexistenceWabaResolutionError) as exc:
            await resolve_coexistence_assets_from_graph(
                "https://graph.facebook.com/v21.0",
                "token",
                {"granular_scopes": []},
                expected_phone_number="+966501234567",
            )
        assert exc.value.code == REAUTH_REQUIRED


    asyncio.run(_run())
def test_no_first_granular_without_phone_match():
    """Resolver evaluates authorized list; never returns first target without phone proof."""

    async def run(monkeypatch):
        async def fake_get(graph_base, token, node, fields):  # noqa: ANN001
            if node == "WABA-FIRST":
                return {"ok": True, "data": {"id": "WABA-FIRST"}}
            if node == "WABA-SECOND":
                return {"ok": True, "data": {"id": "WABA-SECOND"}}
            if node.endswith("/phone_numbers"):
                return {"ok": True, "data": {"data": []}}
            return {"ok": False}

        monkeypatch.setattr("services.embedded_waba_resolution._graph_get", fake_get)
        with pytest.raises(CoexistenceWabaResolutionError) as exc:
            await resolve_coexistence_assets_from_graph(
                "https://graph.facebook.com/v21.0",
                "token",
                _debug(["WABA-FIRST", "WABA-SECOND"]),
                expected_phone_number="+966501234567",
            )
        assert exc.value.code == REAUTH_REQUIRED

    asyncio.run(run(pytest.MonkeyPatch()))