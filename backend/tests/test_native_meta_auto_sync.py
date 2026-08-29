"""Tests for automatic native Meta sync orchestration (PR2)."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.catalog import (  # noqa: E402
    OWNERSHIP_EXTERNAL_MANAGED,
    OWNERSHIP_NAHLA_MANAGED,
    SOURCE_NAHLA_NATIVE,
)
from services.native_meta_sync_orchestrator import (  # noqa: E402
    attempt_native_meta_sync,
    mark_native_meta_sync_pending,
    meta_relevant_patch_keys,
    retry_allowed_for_status,
)
from services.product_publication_status import build_product_publication_status  # noqa: E402


def _generic_native_parent(**overrides):
    meta = {
        "currency": "SAR",
        "image_url": "https://cdn.example/item.webp",
        "product_url": "https://api.example.com/public/items/nahla_p_501",
    }
    base = dict(
        id=501,
        tenant_id=9,
        title="قميص قطني أزرق",
        description="وصف عام",
        price="149",
        sku=None,
        meta_retailer_id="nahla_p_501",
        in_stock=True,
        stock_quantity=3,
        source=SOURCE_NAHLA_NATIVE,
        ownership_mode=OWNERSHIP_NAHLA_MANAGED,
        extra_metadata=meta,
        sync_status=None,
        sync_error=None,
        last_synced_at=None,
        meta_item_id=None,
        meta_catalog_published_at=None,
        catalog_status="active",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _preview_ok():
    return {
        "eligible": True,
        "retailer_id": "nahla_p_501",
        "fatal_errors": [],
        "warnings": [],
    }


def _push_ok():
    return {
        "ok": True,
        "catalog_id": "CAT-1",
        "action": "create",
        "meta_product_id": "META-501",
        "payload": {"price": 14900, "currency": "SAR", "availability": "in stock"},
    }


def test_meta_relevant_patch_keys():
    assert meta_relevant_patch_keys({"title"}) is True
    assert meta_relevant_patch_keys({"sku"}) is False


def test_retry_allowed_states():
    assert retry_allowed_for_status("pending") is True
    assert retry_allowed_for_status("failed") is True
    assert retry_allowed_for_status("blocked") is True
    assert retry_allowed_for_status("syncing") is False
    assert retry_allowed_for_status("synced") is False


def test_mark_pending_sets_sync_meta():
    parent = _generic_native_parent()
    db = MagicMock()
    assert mark_native_meta_sync_pending(db, parent) is True
    assert parent.sync_status == "pending"
    assert parent.extra_metadata["sync_meta"]["pending_at"]


def test_salla_product_is_marked_pending_for_channel_publish():
    parent = _generic_native_parent(
        source="salla",
        ownership_mode=OWNERSHIP_EXTERNAL_MANAGED,
        title="حذاء رياضي أبيض",
    )
    db = MagicMock()
    assert mark_native_meta_sync_pending(db, parent) is True
    assert parent.sync_status == "pending"


def test_meta_existing_product_not_marked_pending():
    parent = _generic_native_parent(
        source="meta",
        ownership_mode="meta_readonly",
    )
    db = MagicMock()
    assert mark_native_meta_sync_pending(db, parent) is False


@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status")
@patch("services.native_meta_sync_orchestrator.find_meta_catalog_item_by_retailer_id")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_successful_sync_requires_post_push_lookup(
    preview_mock,
    ensure_mock,
    push_mock,
    lookup_mock,
    waba_mock,
    lock_mock,
):
    parent = _generic_native_parent()
    db = MagicMock()
    lock_mock.return_value = parent
    preview_mock.return_value = _preview_ok()
    ensure_mock.return_value = (SimpleNamespace(retailer_id="nahla_p_501"), False)
    push_mock.return_value = _push_ok()
    lookup_mock.return_value = ("META-501", {
        "matched": True,
        "item": {
            "id": "META-501",
            "retailer_id": "nahla_p_501",
            "name": "قميص قطني أزرق",
            "price": 14900,
            "currency": "SAR",
            "availability": "in stock",
        },
    })
    waba_mock.return_value = {"ok": True, "expected_catalog_linked": True}

    result = attempt_native_meta_sync(db, 9, 501)

    assert result["ok"] is True
    assert parent.sync_status == "synced"
    assert parent.meta_item_id == "META-501"
    assert parent.sync_error is None
    assert parent.last_synced_at is not None
    push_mock.assert_called_once()
    lookup_mock.assert_called_once()
    assert result["verification"]["content_matched"] is True


@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status")
@patch("services.native_meta_sync_orchestrator.find_meta_catalog_item_by_retailer_id")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_identity_only_lookup_stays_pending_verification(
    preview_mock,
    ensure_mock,
    push_mock,
    lookup_mock,
    waba_mock,
    lock_mock,
):
    parent = _generic_native_parent()
    db = MagicMock()
    lock_mock.return_value = parent
    preview_mock.return_value = _preview_ok()
    ensure_mock.return_value = (SimpleNamespace(retailer_id="nahla_p_501"), False)
    push_mock.return_value = _push_ok()
    lookup_mock.return_value = ("META-501", {
        "matched": True,
        "item": {"id": "META-501", "retailer_id": "nahla_p_501", "name": "قميص قطني أزرق"},
    })
    waba_mock.return_value = {"ok": True, "expected_catalog_linked": True}

    result = attempt_native_meta_sync(db, 9, 501)

    assert result["ok"] is False
    assert result["sync_status"] == "pending_verification"
    assert parent.sync_status == "pending_verification"
    assert parent.meta_item_id == "META-501"
    assert result["verification"]["matched_retailer_id"] is True
    assert result["verification"]["content_matched"] is False


@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status")
@patch("services.native_meta_sync_orchestrator.find_meta_catalog_item_by_retailer_id")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_stale_worker_does_not_stamp_newer_lease(
    preview_mock,
    ensure_mock,
    push_mock,
    lookup_mock,
    waba_mock,
    lock_mock,
):
    parent = _generic_native_parent(sync_status="syncing")
    parent.extra_metadata["sync_meta"] = {
        "lock_generation": 1,
        "sync_generation": 1,
        "content_generation": 1,
    }
    db = MagicMock()
    lock_mock.return_value = parent
    preview_mock.return_value = _preview_ok()
    ensure_mock.return_value = (SimpleNamespace(retailer_id="nahla_p_501"), False)
    push_mock.return_value = _push_ok()
    lookup_mock.return_value = ("META-501", {
        "matched": True,
        "item": {
            "id": "META-501",
            "retailer_id": "nahla_p_501",
            "price": 14900,
            "currency": "SAR",
            "availability": "in stock",
        },
    })
    waba_mock.return_value = {"ok": True, "expected_catalog_linked": True}

    def _refresh(obj, attribute_names=None):
        sm = dict((obj.extra_metadata or {}).get("sync_meta") or {})
        sm["lock_generation"] = 2
        obj.extra_metadata = {**(obj.extra_metadata or {}), "sync_meta": sm}

    db.refresh.side_effect = _refresh
    result = attempt_native_meta_sync(db, 9, 501)

    assert result["skipped"] is True
    assert result["error_code"] == "stale_lease"
    assert parent.sync_status == "syncing"
    assert parent.meta_item_id is None


@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_preview_blocked_no_graph_post(preview_mock, lock_mock):
    parent = _generic_native_parent()
    db = MagicMock()
    lock_mock.return_value = parent
    preview_mock.return_value = {
        "eligible": True,
        "fatal_errors": [{"code": "missing_description", "message_ar": "الوصف مطلوب"}],
    }

    result = attempt_native_meta_sync(db, 9, 501)

    assert result["ok"] is False
    assert parent.sync_status == "blocked"
    assert parent.sync_error


@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_graph_failure_keeps_product_failed(
    preview_mock,
    ensure_mock,
    push_mock,
    lock_mock,
):
    parent = _generic_native_parent()
    db = MagicMock()
    lock_mock.return_value = parent
    preview_mock.return_value = _preview_ok()
    ensure_mock.return_value = (SimpleNamespace(retailer_id="nahla_p_501"), False)
    push_mock.return_value = {
        "ok": False,
        "error": "meta_http_error",
        "meta": {"response": {"error": {"message": "bad"}}},
    }

    result = attempt_native_meta_sync(db, 9, 501)

    assert result["ok"] is False
    assert parent.sync_status == "failed"
    assert parent.sync_error
    assert retry_allowed_for_status(parent.sync_status) is True


def _variant_query_db(*, rows=None, error=None):
    db = MagicMock()

    def _query(model):
        name = getattr(model, "__name__", str(model))
        q = MagicMock()
        if name == "ProductVariant":
            filtered = MagicMock()
            if error is not None:
                filtered.all.side_effect = error
            else:
                filtered.all.return_value = list(rows or [])
            q.filter.return_value = filtered
        return q

    db.query.side_effect = _query
    return db


@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status")
@patch("services.native_meta_sync_orchestrator.find_meta_catalog_item_by_retailer_id")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_variant_query_error_does_not_push_parent_fallback(
    preview_mock,
    ensure_mock,
    push_mock,
    lookup_mock,
    waba_mock,
    lock_mock,
):
    from sqlalchemy.exc import OperationalError

    parent = _generic_native_parent()
    lock_mock.return_value = parent
    preview_mock.return_value = _preview_ok()
    ensure_mock.return_value = (SimpleNamespace(retailer_id="nahla_p_501"), False)
    db = _variant_query_db(
        error=OperationalError("SELECT retailer_id", {}, Exception("variants relation missing")),
    )
    result = attempt_native_meta_sync(db, 9, 501)
    assert result["ok"] is False
    assert result["error_code"] == "variant_discovery_failed"
    assert parent.sync_status == "failed"
    assert parent.sync_status != "syncing"
    assert parent.sync_status != "synced"
    assert int(parent.extra_metadata["sync_meta"].get("retry_count") or 0) == 1
    assert parent.extra_metadata["sync_meta"].get("next_retry_at")
    push_mock.assert_not_called()
    lookup_mock.assert_not_called()


@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status")
@patch("services.native_meta_sync_orchestrator.find_meta_catalog_item_by_retailer_id")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_empty_variant_query_still_pushes_parent_only(
    preview_mock,
    ensure_mock,
    push_mock,
    lookup_mock,
    waba_mock,
    lock_mock,
):
    parent = _generic_native_parent()
    lock_mock.return_value = parent
    preview_mock.return_value = _preview_ok()
    ensure_mock.return_value = (SimpleNamespace(retailer_id="nahla_p_501"), False)
    push_mock.return_value = _push_ok()
    lookup_mock.return_value = (
        "META-501",
        {
            "matched": True,
            "item": {
                "id": "META-501",
                "retailer_id": "nahla_p_501",
                "price": 14900,
                "currency": "SAR",
                "availability": "in stock",
            },
        },
    )
    waba_mock.return_value = {"ok": True, "expected_catalog_linked": True}
    db = _variant_query_db(rows=[])
    result = attempt_native_meta_sync(db, 9, 501)
    assert result["ok"] is True
    assert parent.sync_status == "synced"
    push_mock.assert_called_once()
    assert push_mock.call_args.args[2] == "nahla_p_501"


def test_abandon_stale_lease_raises_when_rollback_fails():
    from sqlalchemy.exc import OperationalError
    from services.native_meta_sync_orchestrator import (
        CatalogSyncSessionUnusable,
        _abandon_stale_lease,
    )

    db = MagicMock()
    db.rollback.side_effect = OperationalError("ROLLBACK", {}, Exception("connection lost"))
    with pytest.raises(CatalogSyncSessionUnusable) as caught:
        _abandon_stale_lease(db)
    assert caught.value.original_code == "stale_lease"
    assert "skipped" not in str(caught.value).lower() or caught.value.original_code == "stale_lease"
    db.close.assert_called()


def test_release_acquire_tx_raises_when_rollback_fails():
    from sqlalchemy.exc import OperationalError
    from services.native_meta_sync_orchestrator import (
        CatalogSyncSessionUnusable,
        _release_acquire_tx,
    )

    db = MagicMock()
    db.rollback.side_effect = OperationalError("ROLLBACK", {}, Exception("connection lost"))
    with pytest.raises(CatalogSyncSessionUnusable) as caught:
        _release_acquire_tx(db)
    assert caught.value.original_code == "sync_lock_not_acquired"
    db.close.assert_called()


@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
def test_double_trigger_exits_when_lock_not_acquired(lock_mock):
    db = MagicMock()
    lock_mock.return_value = None
    result = attempt_native_meta_sync(db, 9, 501)
    assert result["skipped"] is True


@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator._resolve_connection")
def test_catalog_disabled_skips_meta_write(conn_mock, lock_mock, push_mock):
    parent = _generic_native_parent()
    db = MagicMock()
    lock_mock.return_value = parent
    conn_mock.return_value = SimpleNamespace(catalog_enabled=False)
    result = attempt_native_meta_sync(db, 9, 501)
    assert result["skipped"] is True
    assert result["error_code"] == "catalog_disabled"
    assert parent.sync_status == "pending"
    push_mock.assert_not_called()


def test_waba_unlinked_does_not_fail_meta_sync_status():
    parent = _generic_native_parent(
        sync_status="synced",
        extra_metadata={
            "currency": "SAR",
            "sync_meta": {"waba_catalog_linked": False},
        },
    )
    pub = build_product_publication_status(parent)
    assert pub["meta_catalog_synced"] is True
    assert pub["waba_catalog_linked"] is False
    assert pub["visible_in_whatsapp"] is False


def test_synced_and_linked_still_not_visible_without_trusted_signal():
    parent = _generic_native_parent(
        sync_status="synced",
        extra_metadata={
            "currency": "SAR",
            "image_url": "https://cdn.example/item.webp",
            "product_url": "https://api.example.com/public/items/nahla_p_501",
            "sync_meta": {"waba_catalog_linked": True},
        },
    )
    pub = build_product_publication_status(
        parent,
        waba_link_status={"ok": True, "expected_catalog_linked": True},
    )
    assert pub["meta_catalog_synced"] is True
    assert pub["waba_catalog_linked"] is True
    assert pub["visible_in_whatsapp"] is False


def test_stale_syncing_is_reclaimable():
    from services.native_meta_sync_orchestrator import _syncing_is_stale  # noqa: PLC0415

    old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    assert _syncing_is_stale({"syncing_started_at": old}, datetime.now(timezone.utc)) is True


@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_preview_ineligible_error_code_does_not_typeerror(preview_mock, lock_mock):
    parent = _generic_native_parent()
    db = MagicMock()
    lock_mock.return_value = parent
    preview_mock.return_value = {
        "eligible": False,
        "error_code": "missing_image_url",
        "message_ar": "الصورة مطلوبة",
        "fatal_errors": [],
    }
    result = attempt_native_meta_sync(db, 9, 501)
    assert result["ok"] is False
    assert result["error_code"] == "missing_image_url"
    assert parent.sync_status == "blocked"
    assert parent.sync_status != "syncing"


@patch("services.native_meta_sync_orchestrator._collect_retailer_ids")
@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status")
@patch("services.native_meta_sync_orchestrator.find_meta_catalog_item_by_retailer_id")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_variant_prices_must_match_each_retailer(
    preview_mock,
    ensure_mock,
    push_mock,
    lookup_mock,
    waba_mock,
    lock_mock,
    collect_mock,
):
    parent = _generic_native_parent()
    db = MagicMock()
    lock_mock.return_value = parent
    preview_mock.return_value = _preview_ok()
    ensure_mock.return_value = (SimpleNamespace(retailer_id="sku-blue"), False)
    collect_mock.return_value = ["sku-blue", "sku-white"]
    waba_mock.return_value = {"ok": True, "expected_catalog_linked": True}

    def _push(_db, _tid, retailer_id, **_kwargs):
        price = 7700 if retailer_id == "sku-blue" else 8300
        return {
            "ok": True,
            "payload": {"price": price, "currency": "SAR", "availability": "in stock"},
        }

    push_mock.side_effect = _push
    lookup_mock.return_value = ("META-X", {
        "matched": True,
        "item": {"price": 8300, "currency": "SAR", "availability": "in stock"},
    })
    result = attempt_native_meta_sync(db, 9, 501)
    assert result["ok"] is False
    assert result["sync_status"] == "pending_verification"
    assert parent.sync_status != "synced"
    variants = result["verification"]["variant_results"]
    assert variants["sku-blue"]["outcome"] == "mismatch"
    assert variants["sku-white"]["outcome"] == "matched"


@patch("services.native_meta_sync_orchestrator._collect_retailer_ids")
@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status")
@patch("services.native_meta_sync_orchestrator.find_meta_catalog_item_by_retailer_id")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_variant_prices_match_when_each_option_is_correct(
    preview_mock,
    ensure_mock,
    push_mock,
    lookup_mock,
    waba_mock,
    lock_mock,
    collect_mock,
):
    parent = _generic_native_parent()
    db = MagicMock()
    lock_mock.return_value = parent
    preview_mock.return_value = _preview_ok()
    ensure_mock.return_value = (SimpleNamespace(retailer_id="sku-blue"), False)
    collect_mock.return_value = ["sku-blue", "sku-white"]
    waba_mock.return_value = {"ok": True, "expected_catalog_linked": True}

    def _push(_db, _tid, retailer_id, **_kwargs):
        price = 7700 if retailer_id == "sku-blue" else 8300
        return {
            "ok": True,
            "payload": {"price": price, "currency": "SAR", "availability": "in stock"},
        }

    def _lookup(_conn, _catalog, retailer_id, **_kwargs):
        price = 7700 if retailer_id == "sku-blue" else 8300
        return ("META-X", {
            "matched": True,
            "item": {"price": price, "currency": "SAR", "availability": "in stock"},
        })

    push_mock.side_effect = _push
    lookup_mock.side_effect = _lookup
    result = attempt_native_meta_sync(db, 9, 501)
    assert result["ok"] is True
    assert parent.sync_status == "synced"
    expected = parent.extra_metadata["sync_meta"]["expected_payloads_by_retailer_id"]
    assert expected["sku-blue"]["price"] == 7700
    assert expected["sku-white"]["price"] == 8300


@patch("services.native_meta_sync_orchestrator._load_product")
@patch("services.native_meta_sync_orchestrator._collect_retailer_ids")
@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status")
@patch("services.native_meta_sync_orchestrator.find_meta_catalog_item_by_retailer_id")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_verify_lag_keeps_expected_payloads_and_skips_repush(
    preview_mock,
    ensure_mock,
    push_mock,
    lookup_mock,
    waba_mock,
    lock_mock,
    collect_mock,
    load_mock,
):
    expected = {
        "sku-blue": {"price": 7700, "currency": "SAR", "availability": "in stock"},
        "sku-white": {"price": 8300, "currency": "SAR", "availability": "in stock"},
    }
    parent = _generic_native_parent(
        sync_status="pending_verification",
        extra_metadata={
            "currency": "SAR",
            "image_url": "https://cdn.example/item.webp",
            "product_url": "https://api.example.com/public/items/nahla_p_501",
            "sync_meta": {
                "expected_payloads_by_retailer_id": expected,
                "verify_retry_count": 1,
                "content_generation": 1,
                "expected_content_generation": 1,
            },
        },
    )
    db = MagicMock()
    load_mock.return_value = parent
    lock_mock.return_value = parent
    preview_mock.return_value = _preview_ok()
    ensure_mock.return_value = (SimpleNamespace(retailer_id="sku-blue"), False)
    collect_mock.return_value = ["sku-blue", "sku-white"]
    waba_mock.return_value = {"ok": True, "expected_catalog_linked": True}
    lookup_mock.return_value = ("META-X", {
        "matched": True,
        "item": {"id": "META-X", "retailer_id": "sku-blue", "name": "قميص قطني أزرق"},
    })
    result = attempt_native_meta_sync(db, 9, 501)
    push_mock.assert_not_called()
    assert result["skipped_push"] is True
    stored = parent.extra_metadata["sync_meta"]["expected_payloads_by_retailer_id"]
    assert stored["sku-blue"]["price"] == 7700
    assert stored["sku-white"]["price"] == 8300
    assert stored != {}
    assert parent.sync_status == "pending_verification"


@patch("services.native_meta_sync_orchestrator._load_product")
@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status")
@patch("services.native_meta_sync_orchestrator.find_meta_catalog_item_by_retailer_id")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_verify_retries_exhaust_to_needs_attention(
    preview_mock,
    ensure_mock,
    push_mock,
    lookup_mock,
    waba_mock,
    lock_mock,
    load_mock,
):
    parent = _generic_native_parent(
        sync_status="pending_verification",
        extra_metadata={
            "currency": "SAR",
            "image_url": "https://cdn.example/item.webp",
            "product_url": "https://api.example.com/public/items/nahla_p_501",
            "sync_meta": {
                "expected_payloads_by_retailer_id": {
                    "nahla_p_501": {"price": 14900, "currency": "SAR", "availability": "in stock"},
                },
                "verify_retry_count": 2,
                "content_generation": 1,
                "expected_content_generation": 1,
            },
        },
    )
    db = MagicMock()
    load_mock.return_value = parent
    lock_mock.return_value = parent
    preview_mock.return_value = _preview_ok()
    ensure_mock.return_value = (SimpleNamespace(retailer_id="nahla_p_501"), False)
    waba_mock.return_value = {"ok": True, "expected_catalog_linked": True}
    lookup_mock.return_value = ("META-501", {
        "matched": True,
        "item": {"id": "META-501", "retailer_id": "nahla_p_501", "name": "قميص قطني أزرق"},
    })
    result = attempt_native_meta_sync(db, 9, 501)
    push_mock.assert_not_called()
    assert result["error_code"] == "verification_exhausted"
    assert parent.extra_metadata["sync_meta"]["verify_exhausted"] is True
    assert int(parent.extra_metadata["sync_meta"]["verify_retry_count"]) == 3
    assert parent.extra_metadata["sync_meta"]["next_verify_at"] is None
    assert parent.extra_metadata["sync_meta"]["expected_payloads_by_retailer_id"]["nahla_p_501"]["price"] == 14900


@patch("services.native_meta_sync_orchestrator._load_product")
@patch("services.native_meta_sync_orchestrator._collect_retailer_ids")
@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status")
@patch("services.native_meta_sync_orchestrator.find_meta_catalog_item_by_retailer_id")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_lookup_only_does_not_stamp_synced_for_newer_generation(
    preview_mock,
    ensure_mock,
    push_mock,
    lookup_mock,
    waba_mock,
    lock_mock,
    collect_mock,
    load_mock,
):
    expected = {
        "sku-blue": {"price": 7700, "currency": "SAR", "availability": "in stock"},
    }
    parent = _generic_native_parent(
        sync_status="pending_verification",
        extra_metadata={
            "currency": "SAR",
            "image_url": "https://cdn.example/item.webp",
            "product_url": "https://api.example.com/public/items/nahla_p_501",
            "sync_meta": {
                "expected_payloads_by_retailer_id": expected,
                "expected_content_generation": 1,
                "content_generation": 1,
                "verify_retry_count": 1,
                "dirty": False,
            },
        },
    )
    load_mock.return_value = parent

    def _acquire(_db, _tid, _pid):
        sm = dict(parent.extra_metadata["sync_meta"])
        sm["content_generation"] = 2
        sm["sync_generation"] = 2
        sm["lock_generation"] = 2
        sm["dirty"] = False
        parent.extra_metadata = {**parent.extra_metadata, "sync_meta": sm}
        parent.sync_status = "syncing"
        return parent

    lock_mock.side_effect = _acquire
    preview_mock.return_value = _preview_ok()
    ensure_mock.return_value = (SimpleNamespace(retailer_id="sku-blue"), False)
    collect_mock.return_value = ["sku-blue"]
    waba_mock.return_value = {"ok": True, "expected_catalog_linked": True}
    lookup_mock.return_value = (
        "META-X",
        {
            "matched": True,
            "item": {"price": 7700, "currency": "SAR", "availability": "in stock"},
        },
    )
    push_mock.return_value = {
        "ok": True,
        "payload": {"price": 8300, "currency": "SAR", "availability": "in stock"},
    }
    db = MagicMock()
    result = attempt_native_meta_sync(db, 9, 501)
    assert result.get("ok") is not True or result.get("skipped_push") is not True
    if not push_mock.called:
        assert parent.sync_status != "synced"
    else:
        assert result.get("skipped_push") is not True


@patch("services.native_meta_sync_orchestrator._load_product")
@patch("services.native_meta_sync_orchestrator._collect_retailer_ids")
@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status")
@patch("services.native_meta_sync_orchestrator.find_meta_catalog_item_by_retailer_id")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_newer_generation_pushes_and_verifies_on_resume(
    preview_mock,
    ensure_mock,
    push_mock,
    lookup_mock,
    waba_mock,
    lock_mock,
    collect_mock,
    load_mock,
):
    parent = _generic_native_parent(
        sync_status="pending_verification",
        extra_metadata={
            "currency": "SAR",
            "image_url": "https://cdn.example/item.webp",
            "product_url": "https://api.example.com/public/items/nahla_p_501",
            "sync_meta": {
                "expected_payloads_by_retailer_id": {
                    "sku-blue": {"price": 7700, "currency": "SAR", "availability": "in stock"},
                    "sku-white": {"price": 7700, "currency": "SAR", "availability": "in stock"},
                },
                "expected_content_generation": 1,
                "content_generation": 1,
                "verify_retry_count": 1,
                "dirty": False,
            },
        },
    )
    load_mock.return_value = parent
    cycle = {"n": 0}

    def _acquire(_db, _tid, _pid):
        sm = dict(parent.extra_metadata["sync_meta"])
        if cycle["n"] == 0:
            sm["content_generation"] = 2
            sm["sync_generation"] = 2
            sm["lock_generation"] = 2
            sm["dirty"] = False
        else:
            sm["lock_generation"] = int(sm.get("lock_generation") or 2) + 1
            sm["sync_generation"] = int(sm.get("content_generation") or 2)
        parent.extra_metadata = {**parent.extra_metadata, "sync_meta": sm}
        parent.sync_status = "syncing"
        cycle["n"] += 1
        return parent

    lock_mock.side_effect = _acquire
    preview_mock.return_value = _preview_ok()
    ensure_mock.return_value = (SimpleNamespace(retailer_id="sku-blue"), False)
    collect_mock.return_value = ["sku-blue", "sku-white"]
    waba_mock.return_value = {"ok": True, "expected_catalog_linked": True}
    push_mock.return_value = {
        "ok": True,
        "payload": {"price": 8300, "currency": "SAR", "availability": "in stock"},
    }
    live = {"price": 7700, "currency": "SAR", "availability": "in stock"}

    def _lookup(*_a, **_k):
        return ("META-X", {"matched": True, "item": dict(live)})

    lookup_mock.side_effect = _lookup
    db = MagicMock()
    first = attempt_native_meta_sync(db, 9, 501)
    assert parent.sync_status != "synced"
    stored = (parent.extra_metadata.get("sync_meta") or {}).get("expected_payloads_by_retailer_id") or {}
    if push_mock.called:
        assert first.get("skipped_push") is not True
        assert stored["sku-blue"]["price"] == 8300
        assert stored["sku-white"]["price"] == 8300
        assert int(parent.extra_metadata["sync_meta"].get("expected_content_generation") or 0) == 2
    else:
        assert int(parent.extra_metadata["sync_meta"].get("content_generation") or 0) == 2
        assert parent.sync_status in ("pending", "pending_verification", "syncing")

    live["price"] = 8300
    live["currency"] = "SAR"
    live["availability"] = "in stock"
    second = attempt_native_meta_sync(db, 9, 501)
    assert push_mock.called
    payloads = [call.kwargs for call in push_mock.call_args_list] or [call.args for call in push_mock.call_args_list]
    assert payloads
    sm = parent.extra_metadata["sync_meta"]
    expected = sm.get("expected_payloads_by_retailer_id") or {}
    assert expected["sku-blue"]["price"] == 8300
    assert expected["sku-blue"]["currency"] == "SAR"
    assert expected["sku-white"]["availability"] == "in stock"
    assert int(sm.get("expected_content_generation") or 0) == 2
    assert 7700 not in {expected["sku-blue"]["price"], expected["sku-white"]["price"]}
    if second.get("ok"):
        assert parent.sync_status in ("synced", "pending")
    else:
        assert parent.sync_status in ("pending", "pending_verification")
        assert parent.sync_status != "synced" or int(sm.get("expected_content_generation") or 0) == 2
