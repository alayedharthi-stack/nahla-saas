"""
tests/test_owner_merchant_scope_isolation.py
────────────────────────────────────────────
Regression tests for the owner ↔ merchant scope isolation guard.

Background
──────────
Platform admin/owner JWTs carry ``tenant_id = 1`` by convention (see
``backend.core.middleware.jwt_enforcement_middleware``). Without an explicit
guard, any merchant-scoped endpoint that resolves the tenant from the JWT
claim happily returned tenant 1's data when called by an owner — surfacing
that one tenant's conversations, orders and revenue inside the owner
dashboard.

These tests pin the contract of :func:`backend.core.auth.require_merchant_scope`
so the leak cannot silently come back:

* Merchant tokens pass through.
* Platform admin tokens are rejected with HTTP 403.
* Support-impersonation tokens (admin acting *as* a specific merchant) are
  allowed, because they encode an explicit, audited choice of scope.
* The denial is audit-logged.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


def _make_request(path: str = "/store-sync/status") -> SimpleNamespace:
    """Construct the minimal Request shape ``require_merchant_scope`` reads."""
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        method="GET",
        headers={"X-Real-IP": "10.0.0.1"},
        client=SimpleNamespace(host="10.0.0.1"),
        state=SimpleNamespace(),
    )


def _creds(token: str = "fake.jwt.token"):
    from fastapi.security import HTTPAuthorizationCredentials
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


# ── Allowed: merchant tokens ─────────────────────────────────────────────────

class TestMerchantPassesThrough:
    def test_merchant_role_accepted(self):
        from core.auth import require_merchant_scope

        merchant_payload = {
            "sub": "merchant@store.sa",
            "role": "merchant",
            "tenant_id": 42,
            "user_id": 7,
        }
        with patch("core.auth.decode_token", return_value=merchant_payload):
            user = require_merchant_scope(_make_request(), _creds())
        assert user["role"] == "merchant"
        assert user["tenant_id"] == 42

    @pytest.mark.parametrize("role", ["merchant", "merchant_admin", "merchant_user"])
    def test_all_merchant_roles_accepted(self, role: str):
        from core.auth import require_merchant_scope

        with patch(
            "core.auth.decode_token",
            return_value={"sub": "u@x.sa", "role": role, "tenant_id": 99},
        ):
            user = require_merchant_scope(_make_request(), _creds())
        assert user["role"] == role


# ── Denied: platform admin / owner tokens (no impersonation) ─────────────────

class TestOwnerRejectedFromMerchantScope:
    @pytest.mark.parametrize(
        "role",
        ["admin", "owner", "super_admin", "platform_admin", "platform_owner"],
    )
    def test_admin_roles_rejected_with_403(self, role: str):
        from fastapi import HTTPException

        from core.auth import require_merchant_scope

        admin_payload = {
            "sub": "owner@nahla.ai",
            "role": role,
            "tenant_id": 1,  # the leaky convention this guard exists to defend
            "user_id": 1,
        }
        with patch("core.auth.decode_token", return_value=admin_payload):
            with pytest.raises(HTTPException) as exc:
                require_merchant_scope(_make_request(), _creds())
        assert exc.value.status_code == 403

    def test_denied_request_emits_audit_event(self):
        from fastapi import HTTPException

        from core.auth import require_merchant_scope

        admin_payload = {
            "sub": "owner@nahla.ai",
            "role": "owner",
            "tenant_id": 1,
        }
        with patch("core.auth.decode_token", return_value=admin_payload), \
             patch("core.auth.audit") as audit_mock:
            with pytest.raises(HTTPException):
                require_merchant_scope(
                    _make_request("/whatsapp/usage"), _creds()
                )
        audit_mock.assert_called_once()
        event_name, kwargs = audit_mock.call_args.args[0], audit_mock.call_args.kwargs
        assert event_name == "merchant_scope_denied_for_admin"
        assert kwargs["path"] == "/whatsapp/usage"
        assert kwargs["role"] == "owner"
        assert kwargs["tenant_id"] == 1


# ── Allowed: support-impersonation tokens ────────────────────────────────────

class TestSupportImpersonationAllowed:
    def test_support_impersonation_token_passes(self):
        """
        Support sessions intentionally act *as* one specific merchant, with a
        clearly-distinct role and an audited session_version. The middleware
        already gates them on sensitive paths, so they must remain free to
        read merchant-scoped read-only telemetry like /store-sync/status.
        """
        from core.auth import require_merchant_scope

        support_payload = {
            "sub": "merchant@store.sa",
            "role": "support_impersonation",
            "tenant_id": 42,
            "impersonation": True,
            "actor_sub": "support@nahla.ai",
            "session_version": 3,
        }
        with patch("core.auth.decode_token", return_value=support_payload):
            user = require_merchant_scope(_make_request(), _creds())
        assert user["impersonation"] is True
        assert user["tenant_id"] == 42

    def test_admin_role_with_explicit_impersonation_flag_allowed(self):
        """
        Defence in depth: even if an admin token somehow ends up with an
        ``impersonation = True`` claim, we treat it as an explicit choice
        of scope and let it through. Without that flag the same role is
        denied (covered by TestOwnerRejectedFromMerchantScope).
        """
        from core.auth import require_merchant_scope

        with patch(
            "core.auth.decode_token",
            return_value={
                "sub": "owner@nahla.ai",
                "role": "admin",
                "tenant_id": 7,
                "impersonation": True,
            },
        ):
            user = require_merchant_scope(_make_request(), _creds())
        assert user["role"] == "admin"
