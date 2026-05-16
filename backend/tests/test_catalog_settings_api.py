"""
tests/test_catalog_settings_api.py
──────────────────────────────────
Unit tests for the production catalog settings router (May 2026
#11). The router lives at ``backend/routers/catalog.py`` and exposes
merchant- and admin-facing endpoints for the WhatsApp Catalog
configuration UI.

These tests cover the parts that don't need a live DB / HTTP layer:

  1. **Permission contract** — merchant routes derive tenant_id ONLY
     from the JWT (via ``resolve_tenant_id``); admin routes require
     ``require_admin``. The body NEVER carries a tenant_id on
     merchant routes — that would let a logged-in merchant write to
     another tenant. We assert this by inspecting the FastAPI route
     dependency tree, not by spinning up HTTPX.

  2. **Validation** — ``_apply_config_changes`` raises HTTPException
     (400, ``catalog_id_required``) whenever the resulting connection
     would have ``catalog_enabled=True`` but a NULL/empty
     ``meta_catalog_id``. This is the rule the dashboard relies on so
     the merchant cannot accidentally toggle the kill switch into an
     unusable state.

  3. **Status advice** — the human-readable advice strings map
     deterministically from eligibility reason + connection state.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from fastapi import HTTPException  # noqa: E402

from routers.catalog import (  # noqa: E402
    AdminCatalogConfigPatch,
    AdminCatalogTestSendBody,
    CatalogConfigPatch,
    CatalogTestSendBody,
    _apply_config_changes,
    _status_advice,
    admin_router,
    merchant_router,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1.  Permission contract
# ─────────────────────────────────────────────────────────────────────────────

def _dep_callable_names(route) -> set[str]:
    """Return the set of dependency function names attached to
    *route* (FastAPI Dependants are nested — we walk one level)."""
    names: set[str] = set()
    deps = list(getattr(route, "dependant", None).dependencies) if getattr(
        route, "dependant", None,
    ) else []
    for dep in deps:
        call = getattr(dep, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", repr(call)))
    return names


def test_merchant_routes_require_get_current_user() -> None:
    """Every merchant route MUST carry ``get_current_user`` so a
    bare token-less request returns 401 at the FastAPI layer
    (defence-in-depth on top of the JWT middleware)."""
    for route in merchant_router.routes:
        names = _dep_callable_names(route)
        assert "get_current_user" in names, (
            f"merchant route {route.path!r} missing get_current_user "
            f"dependency — anyone could call it. Deps: {names}"
        )


def test_admin_routes_require_require_admin() -> None:
    """Every admin route MUST carry ``require_admin`` so merchants
    cannot reach cross-tenant endpoints by guessing the URL."""
    for route in admin_router.routes:
        names = _dep_callable_names(route)
        assert "require_admin" in names, (
            f"admin route {route.path!r} missing require_admin "
            f"dependency. Deps: {names}"
        )


def test_merchant_routes_never_accept_tenant_id_in_body() -> None:
    """A merchant body schema that accepted ``tenant_id`` would
    let a logged-in merchant write to another merchant's connection.
    The fix is structural: merchant body models simply have no
    ``tenant_id`` field. This test pins that contract."""
    for model_cls in (CatalogConfigPatch, CatalogTestSendBody):
        assert "tenant_id" not in model_cls.model_fields, (
            f"{model_cls.__name__} unexpectedly exposes tenant_id — "
            "merchant cannot be allowed to choose the target tenant."
        )


def test_admin_routes_require_tenant_id_in_body() -> None:
    """The admin variants MUST carry tenant_id — that's how an
    admin targets a specific merchant."""
    for model_cls in (AdminCatalogConfigPatch, AdminCatalogTestSendBody):
        assert "tenant_id" in model_cls.model_fields
        # ge=1 — defensive against accidental 0 / negative ids.
        meta = model_cls.model_fields["tenant_id"].metadata
        assert any(getattr(m, "ge", None) == 1 for m in meta), (
            f"{model_cls.__name__}.tenant_id missing ge=1 constraint"
        )


def test_merchant_router_prefix() -> None:
    """Path prefix locked so the dashboard / SDK don't need to
    discover the URL at runtime."""
    assert merchant_router.prefix == "/merchant/catalog"


def test_admin_router_prefix() -> None:
    assert admin_router.prefix == "/admin/catalog"


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Validation — catalog_id required when enabled=True
# ─────────────────────────────────────────────────────────────────────────────

class _StubConn:
    """Minimal stand-in for WhatsAppConnection — only the catalog
    columns matter for ``_apply_config_changes``."""

    def __init__(self, *, meta_catalog_id=None, catalog_enabled=False):
        self.meta_catalog_id = meta_catalog_id
        self.catalog_enabled = catalog_enabled


def test_enabling_with_no_catalog_id_raises_400() -> None:
    """The exact failure mode the dashboard MUST prevent: toggling
    enabled to true while the binding is empty. Raises 400 with the
    structured payload the dashboard renders as a field error."""
    conn = _StubConn(meta_catalog_id=None, catalog_enabled=False)
    with pytest.raises(HTTPException) as exc:
        _apply_config_changes(conn, CatalogConfigPatch(catalog_enabled=True))
    assert exc.value.status_code == 400
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail["error"] == "catalog_id_required"
    assert detail["missing_field"] == "meta_catalog_id"
    # The connection must NOT have been mutated when validation fails.
    assert conn.catalog_enabled is False
    assert conn.meta_catalog_id is None


def test_enabling_with_catalog_id_in_same_patch_succeeds() -> None:
    """Setting both at once is the common new-merchant case — must
    NOT trip the validation."""
    conn = _StubConn()
    changes = _apply_config_changes(
        conn,
        CatalogConfigPatch(
            meta_catalog_id="1234567890",
            catalog_enabled=True,
        ),
    )
    assert "meta_catalog_id" in changes
    assert "catalog_enabled" in changes
    assert conn.meta_catalog_id == "1234567890"
    assert conn.catalog_enabled is True


def test_enabling_when_id_already_set_succeeds() -> None:
    conn = _StubConn(meta_catalog_id="999", catalog_enabled=False)
    changes = _apply_config_changes(
        conn, CatalogConfigPatch(catalog_enabled=True),
    )
    assert changes == {"catalog_enabled": {"before": False, "after": True}}
    assert conn.catalog_enabled is True


def test_disabling_does_not_require_catalog_id() -> None:
    """The kill switch can be turned OFF regardless of id state —
    useful when a merchant wants to pause catalog rendering."""
    conn = _StubConn(meta_catalog_id=None, catalog_enabled=True)
    changes = _apply_config_changes(
        conn, CatalogConfigPatch(catalog_enabled=False),
    )
    assert conn.catalog_enabled is False
    assert "catalog_enabled" in changes


def test_empty_string_catalog_id_clears_binding() -> None:
    """``meta_catalog_id=""`` is the documented "clear" gesture.
    The result is NULL on the DB column, NOT an empty string —
    matches the read path which treats both as missing."""
    conn = _StubConn(meta_catalog_id="OLDID", catalog_enabled=False)
    _apply_config_changes(conn, CatalogConfigPatch(meta_catalog_id=""))
    assert conn.meta_catalog_id is None


def test_idempotent_no_changes_when_already_in_target_state() -> None:
    conn = _StubConn(meta_catalog_id="123", catalog_enabled=True)
    changes = _apply_config_changes(
        conn,
        CatalogConfigPatch(meta_catalog_id="123", catalog_enabled=True),
    )
    assert changes == {}


def test_clear_id_while_enabled_raises() -> None:
    """Clearing the binding while still enabled would leave the
    connection in an unusable state — block it with the same 400."""
    conn = _StubConn(meta_catalog_id="123", catalog_enabled=True)
    with pytest.raises(HTTPException):
        _apply_config_changes(conn, CatalogConfigPatch(meta_catalog_id=""))


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Status advice — deterministic mapping
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reason, expected_token", [
    ("catalog_disabled",     "معطّل"),
    ("catalog_id_missing",   "Catalog ID فارغ"),
    ("ok",                   "جاهز"),
])
def test_status_advice_known_reasons(reason: str, expected_token: str) -> None:
    text = _status_advice(
        elig_reason=reason,
        connection_found=True,
        coverage={"with_retailer_id": 1, "without_retailer_id": 0},
    )
    assert expected_token in text


def test_status_advice_no_connection() -> None:
    text = _status_advice(
        elig_reason="connection_missing",
        connection_found=False,
        coverage={"with_retailer_id": 0, "without_retailer_id": 0},
    )
    assert "ربط واتساب" in text


def test_status_advice_retailer_id_coverage_zero() -> None:
    """A merchant with products that have no retailer_id needs a
    pointed nudge — not a generic eligibility line."""
    text = _status_advice(
        elig_reason="ok",
        connection_found=True,
        coverage={"with_retailer_id": 0, "without_retailer_id": 5},
    )
    assert "retailer_id" in text or "مزامنة" in text
