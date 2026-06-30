"""Import and unit checks for verify_wa_token_encryption_rollout.py."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "backend" / "scripts" / "verify_wa_token_encryption_rollout.py"


def _load_module():
    for p in (REPO_ROOT / "backend", REPO_ROOT / "database"):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)
    spec = importlib.util.spec_from_file_location("verify_wa_token_encryption_rollout", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_script_imports():
    mod = _load_module()
    assert callable(mod.main)
    assert callable(mod.verify_connections)


def test_verify_connections_counts_enc1_and_plaintext():
    mod = _load_module()
    enc = SimpleNamespace(access_token="enc1:gAAAAABfakeciphertextvalue")
    plain = SimpleNamespace(access_token="EAABplainTokenValue1234567890")

    with patch(
        "services.whatsapp_platform.wa_connection_secrets.read_access_token",
        return_value="EAABdecryptedToken1234567890",
    ):
        stats = mod.verify_connections([enc, plain])

    assert stats.with_token == 2
    assert stats.enc1 == 1
    assert stats.plaintext_remaining == 1
    assert stats.decrypt_ok == 1
    assert stats.decrypt_fail == 0
    assert stats.passed is False


def test_production_key_error_when_missing(monkeypatch):
    mod = _load_module()
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("WA_TOKEN_ENC_KEY", raising=False)
    assert mod.production_key_error() == "WA_TOKEN_ENC_KEY missing in production"


def test_production_key_ok_in_dev_without_key(monkeypatch):
    mod = _load_module()
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("WA_TOKEN_ENC_KEY", raising=False)
    assert mod.production_key_error() is None
