"""Public-route and replay guards for embedded OAuth callback."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from core.middleware import is_jwt_public_path  # noqa: E402
from routers import whatsapp_embedded as emb  # noqa: E402


def test_oauth_callback_is_jwt_public_exact_path() -> None:
    assert is_jwt_public_path("/whatsapp/embedded/oauth/callback") is True


def test_oauth_start_is_not_public() -> None:
    assert is_jwt_public_path("/whatsapp/embedded/oauth/start") is False


def test_verify_oauth_state_rejects_tampered_state() -> None:
    with pytest.raises(HTTPException) as excinfo:
        emb._verify_oauth_state("not-a-valid-state")
    assert excinfo.value.status_code == 400


def test_oauth_callback_replay_after_durable_consume() -> None:
    db = MagicMock()
    db.get_bind.return_value = MagicMock()
    request = MagicMock()
    oauth_state = emb._OAuthState(
        tenant_id=42,
        redirect_uri="https://api.example.com/whatsapp/embedded/oauth/callback",
        connection_mode="cloud_api",
        nonce="nonce-42",
    )
    with patch.object(emb, "_verify_oauth_state", return_value=oauth_state), \
         patch.object(emb, "consume_oauth_nonce_durable", return_value="already_consumed"), \
         patch.object(emb, "_oauth_callback_finish", return_value="done") as finish:
        out = asyncio.run(
            emb.oauth_callback(
                request=request,
                db=db,
                code="code-42",
                state="signed-state",
            )
        )
    assert out == "done"
    finish.assert_called_once()
    assert finish.call_args.kwargs.get("ok") is False
