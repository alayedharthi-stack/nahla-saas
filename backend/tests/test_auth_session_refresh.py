"""Rolling session refresh — decode_token_for_refresh security contract."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
for p in (REPO_ROOT, BACKEND_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.auth import create_token, decode_token_for_refresh  # noqa: E402
from core.token_revocation import revoke_jti  # noqa: E402


def test_decode_token_for_refresh_accepts_valid_session_token():
    token = create_token("merchant@example.com", "merchant", tenant_id=7, user_id=3)
    payload = decode_token_for_refresh(token)
    assert payload is not None
    assert payload["sub"] == "merchant@example.com"
    assert payload["tenant_id"] == 7


def test_decode_token_for_refresh_rejects_revoked_jti():
    token = create_token("merchant@example.com", "merchant", tenant_id=7, user_id=3)
    payload = decode_token_for_refresh(token)
    assert payload is not None
    revoke_jti(str(payload["jti"]), int(payload["exp"]))
    assert decode_token_for_refresh(token) is None


def test_decode_token_for_refresh_rejects_beyond_grace_window():
    from jose import jwt as jose_jwt  # noqa: PLC0415
    from core.config import JWT_ALGORITHM, JWT_SECRET  # noqa: E402

    old_exp = datetime.now(timezone.utc) - timedelta(days=31)
    token = jose_jwt.encode(
        {
            "sub": "old@example.com",
            "role": "merchant",
            "tenant_id": 1,
            "user_id": 1,
            "exp": old_exp,
            "iat": old_exp - timedelta(days=7),
            "jti": "old-jti",
        },
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )
    assert decode_token_for_refresh(token) is None


def test_decode_token_for_refresh_rejects_special_purpose_tokens():
    from jose import jwt as jose_jwt  # noqa: PLC0415
    from core.config import JWT_ALGORITHM, JWT_SECRET  # noqa: E402

    exp = datetime.now(timezone.utc) + timedelta(hours=1)
    token = jose_jwt.encode(
        {"type": "password_reset", "sub": "x@example.com", "exp": exp, "jti": "reset-jti"},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )
    assert decode_token_for_refresh(token) is None
