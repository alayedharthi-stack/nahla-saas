"""New-merchant WABA catalog onboarding — discover / reuse / create, never name-match."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from services.meta_catalog_onboarding import (  # noqa: E402
    ERROR_AMBIGUOUS_OWNED_CATALOGS,
    ERROR_AMBIGUOUS_WABA_CATALOGS,
    ERROR_CATALOG_BUSINESS_MISMATCH,
    ERROR_CATALOG_CLAIMED_OTHER_TENANT,
    ERROR_CATALOG_MANAGE_PERMISSION,
    ERROR_CATALOG_BUSINESS_UNPROVEN,
    ERROR_ONBOARDING_DISABLED,
    ERROR_ONBOARDING_LOCK_FAILED,
    ERROR_OWNED_CATALOGS_UNREADABLE,
    OnboardingLockError,
    auto_catalog_onboarding_enabled,
    ensure_waba_catalog_for_tenant,
)


@pytest.fixture(autouse=True)
def _enable_auto_onboarding(monkeypatch):
    monkeypatch.setenv("NAHLA_AUTO_CATALOG_ONBOARDING", "1")


@pytest.fixture(autouse=True)
def _catalog_readable():
    with patch(
        "services.meta_catalog_onboarding.probe_catalog_readable",
        return_value={"ok": True, "business_id": "BM-MERCHANT"},
    ):
        yield


@pytest.fixture(autouse=True)
def _catalog_management_granted():
    with patch(
        "services.meta_catalog_onboarding._catalog_management_granted",
        return_value=True,
    ):
        yield


def _conn(**overrides):
    base = dict(
        tenant_id=9,
        meta_catalog_id="",
        whatsapp_business_account_id="WABA-GENERIC-001",
        catalog_enabled=False,
        extra_metadata={},
        access_token="EAAB-merchant",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _db(conn, other_claims=None):
    db = MagicMock()
    claims = list(other_claims) if other_claims is not None else [conn]

    def _query(model):
        q = MagicMock()
        name = getattr(model, "__name__", str(model))
        if name == "WhatsAppConnection":
            q.filter.return_value.first.return_value = conn
            q.filter.return_value.all.return_value = claims
        else:
            q.filter.return_value.first.return_value = None
            q.filter.return_value.all.return_value = []
        return q

    db.query.side_effect = _query
    return db


def _owner_ok():
    return patch(
        "services.meta_catalog_onboarding.fetch_waba_owner_business_id",
        return_value={"ok": True, "business_id": "BM-MERCHANT"},
    )


def _token():
    return patch(
        "services.meta_catalog_onboarding._select_graph_token",
        return_value={"token": "EAAB-merchant"},
    )


def test_reuses_single_waba_linked_catalog_without_create():
    conn = _conn()
    db = _db(conn)
    with _token(), _owner_ok():
        with patch(
            "services.meta_catalog_onboarding._fetch_waba_product_catalogs",
            return_value=([{"id": "CAT-LIVE-1", "name": "فساتين"}], 200, None),
        ):
            with patch("services.meta_catalog_onboarding._graph_json") as graph:
                with patch("services.meta_catalog_onboarding.link_waba_to_catalog") as link:
                    out = ensure_waba_catalog_for_tenant(db, 9, confirm=True)
    assert out["ok"] is True
    assert out["action"] == "reuse_linked"
    assert out["catalog_id"] == "CAT-LIVE-1"
    assert out["created"] is False
    assert conn.meta_catalog_id == "CAT-LIVE-1"
    graph.assert_not_called()
    link.assert_not_called()


def test_second_run_does_not_create_another_catalog():
    conn = _conn(meta_catalog_id="CAT-LIVE-1")
    db = _db(conn)
    with _token(), _owner_ok():
        with patch(
            "services.meta_catalog_onboarding._fetch_waba_product_catalogs",
            return_value=([{"id": "CAT-LIVE-1", "name": "فساتين"}], 200, None),
        ):
            with patch("services.meta_catalog_onboarding._create_owned_catalog") as create:
                first = ensure_waba_catalog_for_tenant(db, 9, confirm=True)
                second = ensure_waba_catalog_for_tenant(db, 9, confirm=True)
    assert first["ok"] is True and second["ok"] is True
    assert first["created"] is False and second["created"] is False
    create.assert_not_called()


def test_ambiguous_linked_catalogs_are_not_picked_by_name():
    conn = _conn()
    db = _db(conn)
    with _token(), _owner_ok():
        with patch(
            "services.meta_catalog_onboarding._fetch_waba_product_catalogs",
            return_value=(
                [
                    {"id": "CAT-A", "name": "متجر فساتين"},
                    {"id": "CAT-B", "name": "متجر فساتين"},
                ],
                200,
                None,
            ),
        ):
            with patch("services.meta_catalog_onboarding._create_owned_catalog") as create:
                out = ensure_waba_catalog_for_tenant(db, 9, confirm=True)
    assert out["ok"] is False
    assert out["error"] == ERROR_AMBIGUOUS_WABA_CATALOGS
    assert conn.meta_catalog_id in (None, "")
    create.assert_not_called()


def test_stamped_other_business_is_legacy_mismatch_no_create():
    conn = _conn(meta_catalog_id="CAT-PLATFORM")
    db = _db(conn)
    with _token(), _owner_ok():
        with patch(
            "services.meta_catalog_onboarding._fetch_waba_product_catalogs",
            return_value=([], 200, None),
        ):
            with patch(
                "services.meta_catalog_onboarding.probe_catalog_readable",
                return_value={"ok": True, "business_id": "BM-PLATFORM"},
            ):
                with patch("services.meta_catalog_onboarding._create_owned_catalog") as create:
                    with patch("services.meta_catalog_onboarding.link_waba_to_catalog") as link:
                        out = ensure_waba_catalog_for_tenant(db, 9, confirm=True)
    assert out["ok"] is False
    assert out["error"] == ERROR_CATALOG_BUSINESS_MISMATCH
    assert out["legacy_repair"] is True
    assert conn.meta_catalog_id == "CAT-PLATFORM"
    create.assert_not_called()
    link.assert_not_called()


def test_empty_wallet_creates_one_catalog_then_links():
    conn = _conn()
    db = _db(conn)
    with _token(), _owner_ok():
        with patch(
            "services.meta_catalog_onboarding._fetch_waba_product_catalogs",
            return_value=([], 200, None),
        ):
            with patch(
                "services.meta_catalog_onboarding._list_owned_catalog_ids",
                return_value=([], None, False),
            ):
                with patch(
                    "services.meta_catalog_onboarding._create_owned_catalog",
                    return_value=("CAT-NEW-1", None),
                ) as create:
                    with patch(
                        "services.meta_catalog_onboarding.link_waba_to_catalog",
                        return_value={
                            "ok": True,
                            "already_linked": True,
                            "action": "link",
                            "error": None,
                        },
                    ) as link:
                        with patch(
                            "services.meta_catalog_onboarding.get_entitlements",
                        ) as ent:
                            ent.return_value.has_feature.return_value = True
                            out = ensure_waba_catalog_for_tenant(db, 9, confirm=True)
    assert out["ok"] is True
    assert out["created"] is True
    assert out["catalog_id"] == "CAT-NEW-1"
    assert conn.meta_catalog_id == "CAT-NEW-1"
    create.assert_called_once()
    link.assert_called_once()


def test_multiple_owned_catalogs_are_ambiguous_not_named():
    conn = _conn()
    db = _db(conn)
    with _token(), _owner_ok():
        with patch(
            "services.meta_catalog_onboarding._fetch_waba_product_catalogs",
            return_value=([], 200, None),
        ):
            with patch(
                "services.meta_catalog_onboarding._list_owned_catalog_ids",
                return_value=(["CAT-1", "CAT-2"], None, False),
            ):
                with patch("services.meta_catalog_onboarding._create_owned_catalog") as create:
                    out = ensure_waba_catalog_for_tenant(db, 9, confirm=True)
    assert out["ok"] is False
    assert out["error"] == ERROR_AMBIGUOUS_OWNED_CATALOGS
    create.assert_not_called()


def test_dry_run_create_does_not_post():
    conn = _conn()
    db = _db(conn)
    with _token(), _owner_ok():
        with patch(
            "services.meta_catalog_onboarding._fetch_waba_product_catalogs",
            return_value=([], 200, None),
        ):
            with patch(
                "services.meta_catalog_onboarding._list_owned_catalog_ids",
                return_value=([], None, False),
            ):
                with patch("services.meta_catalog_onboarding._create_owned_catalog") as create:
                    out = ensure_waba_catalog_for_tenant(db, 9, confirm=False)
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["action"] == "create_and_link"
    assert out["created"] is False
    create.assert_not_called()
    assert conn.meta_catalog_id in (None, "")


def test_flag_defaults_off_and_skips_graph(monkeypatch):
    monkeypatch.delenv("NAHLA_AUTO_CATALOG_ONBOARDING", raising=False)
    assert auto_catalog_onboarding_enabled() is False
    conn = _conn()
    db = _db(conn)
    with patch("services.meta_catalog_onboarding.fetch_waba_owner_business_id") as owner:
        with patch("services.meta_catalog_onboarding._fetch_waba_product_catalogs") as fetch:
            with patch("services.meta_catalog_onboarding._create_owned_catalog") as create:
                out = ensure_waba_catalog_for_tenant(db, 9, confirm=True)
    assert out["ok"] is False
    assert out["skipped"] is True
    assert out["error"] == ERROR_ONBOARDING_DISABLED
    assert conn.meta_catalog_id in (None, "")
    owner.assert_not_called()
    fetch.assert_not_called()
    create.assert_not_called()


def test_missing_catalog_management_is_explicit_blocker():
    conn = _conn()
    db = _db(conn)
    with _token(), _owner_ok():
        with patch(
            "services.meta_catalog_onboarding._fetch_waba_product_catalogs",
            return_value=([], 200, None),
        ):
            with patch(
                "services.meta_catalog_onboarding._list_owned_catalog_ids",
                return_value=([], ERROR_CATALOG_MANAGE_PERMISSION, False),
            ):
                with patch("services.meta_catalog_onboarding._create_owned_catalog") as create:
                    out = ensure_waba_catalog_for_tenant(db, 9, confirm=True)
    assert out["ok"] is False
    assert out["error"] == ERROR_CATALOG_MANAGE_PERMISSION
    assert conn.meta_catalog_id in (None, "")
    create.assert_not_called()


def test_waba_catalogs_unreadable_does_not_create():
    conn = _conn()
    db = _db(conn)
    with _token(), _owner_ok():
        with patch(
            "services.meta_catalog_onboarding._fetch_waba_product_catalogs",
            return_value=([], 403, {"code": 200, "message": "permission"}),
        ):
            with patch("services.meta_catalog_onboarding._create_owned_catalog") as create:
                out = ensure_waba_catalog_for_tenant(db, 9, confirm=True)
    assert out["ok"] is False
    assert out["error"] == "waba_catalogs_unreadable"
    create.assert_not_called()
    assert conn.meta_catalog_id in (None, "")


def test_link_failure_after_create_does_not_stamp():
    conn = _conn()
    db = _db(conn)
    with _token(), _owner_ok():
        with patch(
            "services.meta_catalog_onboarding._fetch_waba_product_catalogs",
            return_value=([], 200, None),
        ):
            with patch(
                "services.meta_catalog_onboarding._list_owned_catalog_ids",
                return_value=([], None, False),
            ):
                with patch(
                    "services.meta_catalog_onboarding._create_owned_catalog",
                    return_value=("CAT-NEW-1", None),
                ):
                    with patch(
                        "services.meta_catalog_onboarding.link_waba_to_catalog",
                        return_value={"ok": False, "error": "waba_catalog_link_failed"},
                    ):
                        out = ensure_waba_catalog_for_tenant(db, 9, confirm=True)
    assert out["ok"] is False
    assert out["created"] is True
    assert out["error"] == "waba_catalog_link_failed"
    assert conn.meta_catalog_id in (None, "")


def test_unreadable_catalog_after_link_does_not_stamp():
    conn = _conn()
    db = _db(conn)
    with _token(), _owner_ok():
        with patch(
            "services.meta_catalog_onboarding._fetch_waba_product_catalogs",
            return_value=([], 200, None),
        ):
            with patch(
                "services.meta_catalog_onboarding._list_owned_catalog_ids",
                return_value=([], None, False),
            ):
                with patch(
                    "services.meta_catalog_onboarding._create_owned_catalog",
                    return_value=("CAT-NEW-1", None),
                ):
                    with patch(
                        "services.meta_catalog_onboarding.link_waba_to_catalog",
                        return_value={"ok": True, "already_linked": False, "action": "link"},
                    ):
                        with patch(
                            "services.meta_catalog_onboarding.probe_catalog_readable",
                            return_value={"ok": False},
                        ):
                            out = ensure_waba_catalog_for_tenant(db, 9, confirm=True)
    assert out["ok"] is False
    assert out["error"] == "catalog_not_readable"
    assert conn.meta_catalog_id in (None, "")


def test_does_not_load_another_tenant_connection():
    db = MagicMock()

    def _query(model):
        q = MagicMock()
        q.filter.return_value.first.return_value = None
        return q

    db.query.side_effect = _query
    out = ensure_waba_catalog_for_tenant(db, 9, confirm=True)
    assert out["error"] == "connection_not_found"
    assert out["tenant_id"] == 9
    assert out.get("catalog_id") in (None, "")


def test_product_auto_sync_flag_does_not_enable_onboarding(monkeypatch):
    monkeypatch.setenv("NAHLA_WHATSAPP_CATALOG_AUTO_SYNC", "1")
    monkeypatch.delenv("NAHLA_AUTO_CATALOG_ONBOARDING", raising=False)
    assert auto_catalog_onboarding_enabled() is False


def test_stamped_among_multiple_linked_is_still_ambiguous():
    conn = _conn(meta_catalog_id="CAT-A")
    db = _db(conn)
    with _token(), _owner_ok():
        with patch(
            "services.meta_catalog_onboarding._fetch_waba_product_catalogs",
            return_value=(
                [{"id": "CAT-A", "name": "A"}, {"id": "CAT-B", "name": "B"}],
                200,
                None,
            ),
        ):
            with patch("services.meta_catalog_onboarding._create_owned_catalog") as create:
                out = ensure_waba_catalog_for_tenant(db, 9, confirm=True)
    assert out["ok"] is False
    assert out["error"] == ERROR_AMBIGUOUS_WABA_CATALOGS
    assert conn.meta_catalog_id == "CAT-A"
    create.assert_not_called()


def test_linked_catalog_other_business_is_mismatch_not_ok():
    conn = _conn()
    db = _db(conn)
    with _token(), _owner_ok():
        with patch(
            "services.meta_catalog_onboarding._fetch_waba_product_catalogs",
            return_value=([{"id": "CAT-LIVE-1"}], 200, None),
        ):
            with patch(
                "services.meta_catalog_onboarding.probe_catalog_readable",
                return_value={"ok": True, "business_id": "BM-PLATFORM"},
            ):
                with patch("services.meta_catalog_onboarding._create_owned_catalog") as create:
                    out = ensure_waba_catalog_for_tenant(db, 9, confirm=True)
    assert out["ok"] is False
    assert out["error"] == ERROR_CATALOG_BUSINESS_MISMATCH
    assert conn.meta_catalog_id in (None, "")
    create.assert_not_called()


def test_empty_owned_list_without_catalog_management_does_not_create():
    conn = _conn()
    db = _db(conn)
    with _token(), _owner_ok():
        with patch(
            "services.meta_catalog_onboarding._fetch_waba_product_catalogs",
            return_value=([], 200, None),
        ):
            with patch(
                "services.meta_catalog_onboarding._list_owned_catalog_ids",
                return_value=([], None, False),
            ):
                with patch(
                    "services.meta_catalog_onboarding._catalog_management_granted",
                    return_value=False,
                ):
                    with patch("services.meta_catalog_onboarding._create_owned_catalog") as create:
                        out = ensure_waba_catalog_for_tenant(db, 9, confirm=True)
    assert out["ok"] is False
    assert out["error"] == ERROR_CATALOG_MANAGE_PERMISSION
    create.assert_not_called()
    assert conn.meta_catalog_id in (None, "")


def test_empty_owned_list_unproven_permission_does_not_create():
    conn = _conn()
    db = _db(conn)
    with _token(), _owner_ok():
        with patch(
            "services.meta_catalog_onboarding._fetch_waba_product_catalogs",
            return_value=([], 200, None),
        ):
            with patch(
                "services.meta_catalog_onboarding._list_owned_catalog_ids",
                return_value=([], None, False),
            ):
                with patch(
                    "services.meta_catalog_onboarding._catalog_management_granted",
                    return_value=None,
                ):
                    with patch("services.meta_catalog_onboarding._create_owned_catalog") as create:
                        out = ensure_waba_catalog_for_tenant(db, 9, confirm=True)
    assert out["ok"] is False
    assert out["error"] == ERROR_OWNED_CATALOGS_UNREADABLE
    create.assert_not_called()


def test_does_not_stamp_catalog_claimed_by_other_tenant():
    conn = _conn()
    other = SimpleNamespace(tenant_id=99, meta_catalog_id="CAT-LIVE-1")
    db = _db(conn, other_claims=[other, conn])
    with _token(), _owner_ok():
        with patch(
            "services.meta_catalog_onboarding._fetch_waba_product_catalogs",
            return_value=([{"id": "CAT-LIVE-1"}], 200, None),
        ):
            out = ensure_waba_catalog_for_tenant(db, 9, confirm=True)
    assert out["ok"] is False
    assert out["error"] == ERROR_CATALOG_CLAIMED_OTHER_TENANT
    assert conn.meta_catalog_id in (None, "")


def test_lock_failure_is_fail_closed_no_create():
    conn = _conn()
    db = _db(conn)
    with patch(
        "services.meta_catalog_onboarding._acquire_tenant_onboard_lock",
        side_effect=OnboardingLockError(ERROR_ONBOARDING_LOCK_FAILED),
    ):
        with patch("services.meta_catalog_onboarding._create_owned_catalog") as create:
            out = ensure_waba_catalog_for_tenant(db, 9, confirm=True)
    assert out["ok"] is False
    assert out["error"] == ERROR_ONBOARDING_LOCK_FAILED
    create.assert_not_called()
    assert conn.meta_catalog_id in (None, "")


def test_readable_linked_catalog_without_business_id_does_not_stamp():
    conn = _conn()
    db = _db(conn)
    with _token(), _owner_ok():
        with patch(
            "services.meta_catalog_onboarding._fetch_waba_product_catalogs",
            return_value=([{"id": "CAT-LIVE-1"}], 200, None),
        ):
            with patch(
                "services.meta_catalog_onboarding.probe_catalog_readable",
                return_value={"ok": True, "business_id": ""},
            ):
                with patch("services.meta_catalog_onboarding._create_owned_catalog") as create:
                    out = ensure_waba_catalog_for_tenant(db, 9, confirm=True)
    assert out["ok"] is False
    assert out["error"] == ERROR_CATALOG_BUSINESS_UNPROVEN
    assert conn.meta_catalog_id in (None, "")
    create.assert_not_called()
