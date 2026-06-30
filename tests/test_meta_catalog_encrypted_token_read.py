"""
tests/test_meta_catalog_encrypted_token_read.py
───────────────────────────────────────────────
PR-A — Meta catalog Graph token selection must decrypt enc1: at-rest tokens.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.wa_token_crypto import encrypt_access_token, is_encrypted_at_rest  # noqa: E402
from services.meta_catalog_import import (  # noqa: E402
    _select_graph_token,
    describe_graph_token_selection,
    sanitize_token_pick,
)


@pytest.fixture
def wa_enc_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("WA_TOKEN_ENC_KEY", key)
    return key


class TestMetaCatalogEncryptedTokenRead:
    def test_encrypted_meta_token_decrypts_for_graph(self, wa_enc_key: str) -> None:
        plain = "EAA" + ("x" * 120)
        stored = encrypt_access_token(plain)
        assert is_encrypted_at_rest(stored)
        assert stored.startswith("enc1:")

        conn = SimpleNamespace(
            provider="meta",
            connection_type="embedded",
            access_token=stored,
        )
        pick = _select_graph_token(conn)

        assert pick["token"] == plain
        assert not str(pick["token"]).startswith("enc1:")
        assert pick["token_source"] == "merchant_meta_oauth"
        assert pick["token"] != stored

    def test_sanitized_diagnostics_never_leak_token(self, wa_enc_key: str) -> None:
        plain = "EAA" + ("y" * 120)
        stored = encrypt_access_token(plain)
        conn = SimpleNamespace(
            provider="meta",
            connection_type="embedded",
            access_token=stored,
        )

        pick = _select_graph_token(conn)
        safe = sanitize_token_pick(pick)
        desc = describe_graph_token_selection(conn)

        assert "token" not in safe
        assert plain not in str(safe)
        assert plain not in str(desc)
        assert stored not in str(desc)
        assert desc["token_present"] is True
        assert desc["token_source"] == "merchant_meta_oauth"

    def test_encrypted_coexistence_eaa_preferred_over_platform(
        self,
        wa_enc_key: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        plain = "EAA" + ("z" * 120)
        stored = encrypt_access_token(plain)
        monkeypatch.setattr("services.meta_catalog_import.WA_TOKEN", "platform_fallback_token")

        conn = SimpleNamespace(
            provider="dialog360",
            connection_type="coexistence",
            access_token=stored,
        )
        pick = _select_graph_token(conn)

        assert pick["token"] == plain
        assert pick["token_source"] == "merchant_meta_oauth"

    def test_no_token_in_logs(self, wa_enc_key: str, caplog: pytest.LogCaptureFixture) -> None:
        plain = "EAA" + ("w" * 120)
        stored = encrypt_access_token(plain)
        conn = SimpleNamespace(
            provider="meta",
            connection_type="embedded",
            access_token=stored,
        )

        with caplog.at_level(logging.DEBUG):
            _select_graph_token(conn)
            describe_graph_token_selection(conn)

        combined = caplog.text
        assert plain not in combined
        assert stored not in combined
