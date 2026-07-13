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
    return {"ok": True, "catalog_id": "CAT-1", "action": "create", "meta_product_id": "META-501"}


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


def test_salla_product_not_marked_pending():
    parent = _generic_native_parent(
        source="salla",
        ownership_mode=OWNERSHIP_EXTERNAL_MANAGED,
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
    lookup_mock.return_value = ("META-501", {"matched": True})
    waba_mock.return_value = {"ok": True, "expected_catalog_linked": True}

    result = attempt_native_meta_sync(db, 9, 501)

    assert result["ok"] is True
    assert parent.sync_status == "synced"
    assert parent.meta_item_id == "META-501"
    assert parent.sync_error is None
    assert parent.last_synced_at is not None
    push_mock.assert_called_once()
    lookup_mock.assert_called_once()


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


@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
def test_double_trigger_exits_when_lock_not_acquired(lock_mock):
    db = MagicMock()
    lock_mock.return_value = None
    result = attempt_native_meta_sync(db, 9, 501)
    assert result["skipped"] is True


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
