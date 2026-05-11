"""
tests/test_require_admin_support_access.py
──────────────────────────────────────────
Locks the contract for ``core.auth.require_admin``:

  Branch (a) — regular platform-admin JWT  → granted
  Branch (b) — support-impersonation JWT
               actor_user_id still maps to a live admin → granted
               actor demoted / deactivated              → 403
               actor missing / wrong type               → 403
               tampered (impersonation flag but wrong role) → 403
  Neither   → 403

These tests drive the dependency directly (no HTTP client) so they
run fast and exercise the DB revalidation path without any real
request middleware involvement.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ──────────────────────────────────────────────────────────────────────
# Test helpers
# ──────────────────────────────────────────────────────────────────────


class _FakeRequest:
    """Minimal stand-in for fastapi.Request that satisfies the bits
    require_admin reaches for (headers + client.host + url.path +
    method). No middleware, no scope, no body."""

    class _URL:
        def __init__(self, path: str):
            self.path = path

    class _Client:
        host = "127.0.0.1"

    def __init__(self, path: str = "/admin/debug/media-env", method: str = "GET"):
        self.url = self._URL(path)
        self.method = method
        self.client = self._Client()
        self.headers: Dict[str, str] = {}


class _FakeCreds:
    """Stand-in for HTTPAuthorizationCredentials — we patch
    get_current_user, so the actual JWT bytes are irrelevant."""
    def __init__(self):
        self.credentials = "fake.jwt.bytes"


def _call_require_admin(jwt_payload: Dict[str, Any]):
    """Drive ``require_admin`` synchronously with the given JWT
    payload by patching ``get_current_user`` to return it.

    Returns whatever require_admin returns, OR raises whatever it
    raises (so callers can use pytest.raises)."""
    from core import auth as core_auth
    with patch.object(core_auth, "get_current_user", return_value=jwt_payload):
        return core_auth.require_admin(
            request=_FakeRequest(),
            creds=_FakeCreds(),
        )


# ──────────────────────────────────────────────────────────────────────
# Branch (a) — regular platform admin
# ──────────────────────────────────────────────────────────────────────


class TestRegularAdminPath:
    def test_admin_role_granted(self):
        out = _call_require_admin({
            "sub": "admin@nahla", "role": "admin", "tenant_id": 1, "user_id": 1,
        })
        assert out["role"] == "admin"

    def test_owner_role_granted(self):
        out = _call_require_admin({
            "sub": "owner@nahla", "role": "owner", "tenant_id": 1, "user_id": 2,
        })
        assert out["sub"] == "owner@nahla"

    def test_platform_admin_role_granted(self):
        out = _call_require_admin({
            "sub": "p@nahla", "role": "platform_admin", "tenant_id": 1, "user_id": 3,
        })
        assert out is not None

    def test_merchant_role_denied(self):
        with pytest.raises(HTTPException) as exc:
            _call_require_admin({
                "sub": "merchant@x.com", "role": "merchant", "tenant_id": 5, "user_id": 9,
            })
        assert exc.value.status_code == 403
        assert "admin access required" in str(exc.value.detail).lower()

    def test_no_role_denied(self):
        with pytest.raises(HTTPException) as exc:
            _call_require_admin({"sub": "x@y.com", "tenant_id": 5})
        assert exc.value.status_code == 403


# ──────────────────────────────────────────────────────────────────────
# Branch (b) — support-impersonation token
# ──────────────────────────────────────────────────────────────────────


class TestSupportImpersonationPath:
    def _support_payload(self, **overrides):
        base = {
            "sub": "merchant@target.com",
            "role": "support_impersonation",
            "tenant_id": 33,
            "user_id": 99,
            "impersonation": True,
            "actor_sub": "admin@nahla",
            "actor_user_id": 1,
            "session_version": 7,
        }
        base.update(overrides)
        return base

    def test_support_token_with_active_admin_actor_granted(self):
        """The canonical case — admin is in an impersonation
        session and hits an admin-only debug endpoint."""
        from core import auth as core_auth
        with patch.object(core_auth, "_actor_is_still_platform_admin", return_value=True):
            out = _call_require_admin(self._support_payload())
        # The returned payload is the support-impersonation token
        # itself — handlers can inspect actor_sub / tenant_id from it.
        assert out["role"] == "support_impersonation"
        assert out["impersonation"] is True
        assert out["actor_sub"] == "admin@nahla"
        assert out["tenant_id"] == 33

    def test_support_token_with_demoted_actor_denied(self):
        """Admin's role was demoted to ``merchant`` AFTER the
        session started — JWT still works but admin endpoints
        must reject immediately."""
        from core import auth as core_auth
        with patch.object(core_auth, "_actor_is_still_platform_admin", return_value=False):
            with pytest.raises(HTTPException) as exc:
                _call_require_admin(self._support_payload())
        assert exc.value.status_code == 403
        assert "no longer" in str(exc.value.detail).lower()

    def test_support_token_with_missing_actor_user_id_denied(self):
        """A malformed support token without actor_user_id can't
        be revalidated → deny."""
        from core import auth as core_auth
        # Don't even mock — _actor_is_still_platform_admin returns
        # False for None on its own.
        with pytest.raises(HTTPException) as exc:
            _call_require_admin(self._support_payload(actor_user_id=None))
        assert exc.value.status_code == 403

    def test_impersonation_flag_without_support_role_denied(self):
        """``impersonation=True`` alone is NOT enough — we require
        BOTH the flag AND the explicit role. This defends against
        a future bug that sets one but not the other."""
        with pytest.raises(HTTPException) as exc:
            _call_require_admin(self._support_payload(role="merchant"))
        assert exc.value.status_code == 403

    def test_support_role_without_impersonation_flag_denied(self):
        """Symmetric defense — role alone isn't enough either."""
        with pytest.raises(HTTPException) as exc:
            _call_require_admin(self._support_payload(impersonation=False))
        assert exc.value.status_code == 403

    def test_support_token_actor_user_id_as_string_handled(self):
        """JWTs sometimes round-trip integers as strings. The
        revalidator should coerce safely; bad strings → False."""
        from core import auth as core_auth
        # "1" is coercible → admin DB lookup proceeds → patched True.
        with patch.object(core_auth, "_actor_is_still_platform_admin", return_value=True):
            out = _call_require_admin(self._support_payload(actor_user_id="1"))
        assert out["impersonation"] is True


# ──────────────────────────────────────────────────────────────────────
# _actor_is_still_platform_admin — DB revalidation helper
# ──────────────────────────────────────────────────────────────────────


class TestActorRevalidation:
    def test_none_returns_false(self):
        from core.auth import _actor_is_still_platform_admin
        assert _actor_is_still_platform_admin(None) is False

    def test_non_numeric_string_returns_false(self):
        from core.auth import _actor_is_still_platform_admin
        assert _actor_is_still_platform_admin("not-a-number") is False

    def test_active_admin_user_returns_true(self, monkeypatch):
        """Patch the DB call to return an active admin User."""
        from core import auth as core_auth

        class _FakeUser:
            id = 7
            role = "admin"
            is_active = True

        class _FakeQuery:
            def __init__(self, result):
                self._r = result
            def filter(self, *_a, **_k):
                return self
            def first(self):
                return self._r

        class _FakeDB:
            def __init__(self, user): self._u = user
            def query(self, *_a, **_k): return _FakeQuery(self._u)
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(
            core_auth, "SessionLocal", lambda: _FakeDB(_FakeUser()),
            raising=False,
        )
        # Patch the inline imports the helper does — it imports
        # SessionLocal & User from inside the function.
        import core.database
        import database.models
        monkeypatch.setattr(core.database, "SessionLocal", lambda: _FakeDB(_FakeUser()))
        monkeypatch.setattr(database.models, "User", _FakeUser)
        assert core_auth._actor_is_still_platform_admin(7) is True

    def test_deactivated_admin_returns_false(self, monkeypatch):
        from core import auth as core_auth

        class _FakeUser:
            id = 7
            role = "admin"
            is_active = False  # deactivated

        class _FakeQuery:
            def filter(self, *_a, **_k): return self
            def first(self): return _FakeUser()

        class _FakeDB:
            def query(self, *_a, **_k): return _FakeQuery()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        import core.database
        import database.models
        monkeypatch.setattr(core.database, "SessionLocal", lambda: _FakeDB())
        monkeypatch.setattr(database.models, "User", _FakeUser)
        assert core_auth._actor_is_still_platform_admin(7) is False

    def test_demoted_admin_returns_false(self, monkeypatch):
        from core import auth as core_auth

        class _FakeUser:
            id = 7
            role = "merchant"  # demoted
            is_active = True

        class _FakeQuery:
            def filter(self, *_a, **_k): return self
            def first(self): return _FakeUser()

        class _FakeDB:
            def query(self, *_a, **_k): return _FakeQuery()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        import core.database
        import database.models
        monkeypatch.setattr(core.database, "SessionLocal", lambda: _FakeDB())
        monkeypatch.setattr(database.models, "User", _FakeUser)
        assert core_auth._actor_is_still_platform_admin(7) is False

    def test_user_not_found_returns_false(self, monkeypatch):
        from core import auth as core_auth

        class _FakeUser: pass

        class _FakeQuery:
            def filter(self, *_a, **_k): return self
            def first(self): return None  # no user with that id

        class _FakeDB:
            def query(self, *_a, **_k): return _FakeQuery()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        import core.database
        import database.models
        monkeypatch.setattr(core.database, "SessionLocal", lambda: _FakeDB())
        monkeypatch.setattr(database.models, "User", _FakeUser)
        assert core_auth._actor_is_still_platform_admin(42) is False

    def test_db_exception_fails_closed(self, monkeypatch):
        """A DB error must NOT silently allow access — fail closed."""
        from core import auth as core_auth

        def _boom():
            raise RuntimeError("db unreachable")

        import core.database
        monkeypatch.setattr(core.database, "SessionLocal", _boom)
        assert core_auth._actor_is_still_platform_admin(7) is False
