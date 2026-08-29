"""WhatsApp catalog auto-sync + manual enqueue (platform-wide)."""

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
    OWNERSHIP_META_READONLY,
    OWNERSHIP_NAHLA_MANAGED,
    SOURCE_NAHLA_NATIVE,
    is_merchant_editable_product,
    is_meta_export_eligible,
    is_whatsapp_channel_publish_eligible,
)
from services.native_meta_sync_orchestrator import (  # noqa: E402
    mark_native_meta_sync_pending,
    retry_is_due,
    _mark_synced,
    _refresh_sync_meta_from_db,
    _requeue_if_dirty,
    compare_pushed_content_to_lookup,
    classify_block_code,
)
from services.whatsapp_catalog_sync import (  # noqa: E402
    build_whatsapp_catalog_sync_status,
    channel_content_fingerprint,
    drain_whatsapp_catalog_sync,
    enqueue_whatsapp_catalog_sync,
    evaluate_whatsapp_catalog_sync_readiness,
    mark_product_pending_after_catalog_write,
    schedule_whatsapp_catalog_drain,
    should_reconsider_blocked,
    whatsapp_catalog_auto_sync_enabled,
)


def _entitled(*_args, **_kwargs):
    return SimpleNamespace(has_feature=lambda key: key == "meta_catalog_sync")


def _conn(**overrides):
    base = dict(
        tenant_id=9,
        catalog_enabled=True,
        meta_catalog_id="CAT-GENERIC-001",
        access_token="EAAB-test",
        extra_metadata={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _product(**overrides):
    base = dict(
        id=201,
        tenant_id=9,
        title="قميص قطني أزرق",
        source="salla",
        ownership_mode=OWNERSHIP_EXTERNAL_MANAGED,
        catalog_status="active",
        merchant_hidden_at=None,
        in_stock=True,
        stock_quantity=2,
        sync_status=None,
        sync_error=None,
        last_synced_at=None,
        meta_item_id=None,
        extra_metadata={"currency": "SAR"},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _db_with_conn(conn):
    db = MagicMock()

    def _query(model):
        q = MagicMock()
        name = getattr(model, "__name__", str(model))
        if name == "WhatsAppConnection":
            q.filter.return_value.first.return_value = conn
        else:
            q.filter.return_value.first.return_value = None
            q.filter.return_value.all.return_value = []
        return q

    db.query.side_effect = _query
    return db


def test_channel_publish_allows_salla_but_not_merchant_edit():
    salla = _product()
    assert is_whatsapp_channel_publish_eligible(salla) is True
    assert is_merchant_editable_product(salla) is False
    assert is_meta_export_eligible(salla) is False


def test_channel_publish_allows_generic_native_and_rejects_meta_import():
    native = _product(
        id=501,
        source=SOURCE_NAHLA_NATIVE,
        ownership_mode=OWNERSHIP_NAHLA_MANAGED,
        title="عطر ورد 100ml",
    )
    meta_row = _product(
        id=77,
        source="meta",
        ownership_mode=OWNERSHIP_META_READONLY,
        title="فستان مستورد",
    )
    assert is_whatsapp_channel_publish_eligible(native) is True
    assert is_whatsapp_channel_publish_eligible(meta_row) is False


def test_out_of_stock_remains_channel_publish_eligible():
    row = _product(in_stock=False, stock_quantity=0, title="حذاء رياضي أبيض")
    assert is_whatsapp_channel_publish_eligible(row) is True


def test_hidden_product_is_not_channel_publish_eligible():
    row = _product(merchant_hidden_at=datetime.now(timezone.utc), catalog_status="merchant_hidden")
    assert is_whatsapp_channel_publish_eligible(row) is False


def _not_entitled(*_args, **_kwargs):
    return SimpleNamespace(
        has_feature=lambda key: False,
        is_blocked=False,
        is_active=True,
        plan_slug="starter",
    )


def _upgrade_copy_present(payload: dict) -> bool:
    blob = " ".join(
        str(payload.get(key) or "")
        for key in ("message_ar", "action_ar", "blocker_code")
    )
    return "feature_locked" in blob or "رقِّ الخطة" in blob or "غير مضمّنة في خطتك" in blob


@patch("services.whatsapp_catalog_sync.attempt_native_meta_sync")
def test_transient_entitlement_outage_does_not_lock_plan_and_recovers(attempt_mock):
    from sqlalchemy.exc import OperationalError

    pending = _product(id=201, sync_status="pending")
    db = _db_with_conn(_conn())
    attempt_mock.return_value = {"ok": True, "sync_status": "synced"}
    state = {"fail": True}

    def _ent(*_args, **_kwargs):
        if state["fail"]:
            raise OperationalError("SELECT 1", {}, Exception("entitlement store timeout"))
        return _entitled()

    with patch("services.whatsapp_catalog_sync.get_entitlements", side_effect=_ent), patch(
        "services.whatsapp_catalog_sync.iter_tenant_products",
        return_value=[pending],
    ):
        queued = enqueue_whatsapp_catalog_sync(db, 9, force=True, trigger="manual")
        status = build_whatsapp_catalog_sync_status(db, 9)
        drained = drain_whatsapp_catalog_sync(db, 9)

    assert queued["ok"] is False
    assert queued["queued"] is False
    assert queued["blocker_code"] == "entitlement_unavailable"
    assert _upgrade_copy_present(queued) is False
    assert "تعذر" in (queued.get("message_ar") or "") or "تعذّر" in (queued.get("message_ar") or "")
    assert status["blocker_code"] == "entitlement_unavailable"
    assert status["phase"] == "retrying"
    assert status["ready"] is False
    assert _upgrade_copy_present(status) is False
    assert drained.get("skipped") is True
    attempt_mock.assert_not_called()
    assert pending.sync_status == "pending"

    state["fail"] = False
    with patch("services.whatsapp_catalog_sync.get_entitlements", side_effect=_ent), patch(
        "services.whatsapp_catalog_sync.iter_tenant_products",
        return_value=[pending],
    ):
        queued_ok = enqueue_whatsapp_catalog_sync(db, 9, force=True, trigger="manual")
        status_ok = build_whatsapp_catalog_sync_status(db, 9)
        drained_ok = drain_whatsapp_catalog_sync(db, 9)

    assert queued_ok["queued"] is True
    assert queued_ok["blocker_code"] is None
    assert status_ok["ready"] is True
    assert status_ok["blocker_code"] is None
    assert status_ok["phase"] != "blocked"
    attempt_mock.assert_called()
    assert drained_ok.get("skipped") is not True


@patch("services.whatsapp_catalog_sync.get_entitlements", _not_entitled)
def test_confirmed_missing_entitlement_still_feature_locked():
    db = _db_with_conn(_conn())
    ready = evaluate_whatsapp_catalog_sync_readiness(db, 9)
    queued = enqueue_whatsapp_catalog_sync(db, 9, force=True, trigger="manual")
    status = build_whatsapp_catalog_sync_status(db, 9)
    assert ready["blocker_code"] == "feature_locked"
    assert queued["blocker_code"] == "feature_locked"
    assert status["blocker_code"] == "feature_locked"
    assert status["phase"] == "blocked"
    assert _upgrade_copy_present(queued) is True
    assert _upgrade_copy_present(status) is True


def _db_salla_entitlement_outage(conn=None, *, products=None):
    from sqlalchemy.exc import OperationalError

    db = MagicMock()
    rows = list(products or [])

    def _query(model):
        q = MagicMock()
        name = getattr(model, "__name__", str(model))
        if name == "Integration":
            q.filter.return_value.first.side_effect = OperationalError(
                "SELECT integrations", {}, Exception("catalog entitlement store timeout")
            )
            return q
        if name == "WhatsAppConnection":
            q.filter.return_value.first.return_value = conn or _conn()
            return q
        if name == "Product":
            filtered = MagicMock()
            filtered.order_by.return_value.offset.return_value.limit.return_value.all.return_value = rows
            filtered.first.return_value = rows[0] if rows else None
            q.filter.return_value = filtered
            return q
        q.filter.return_value.first.return_value = None
        q.filter.return_value.all.return_value = []
        return q

    db.query.side_effect = _query
    return db


def test_swallowed_entitlement_lookup_does_not_lock_plan_or_upgrade():
    pending = _product(id=201, sync_status="pending")
    db = _db_salla_entitlement_outage(products=[pending])
    with patch("core.billing.get_tenant_subscription", return_value=None), patch(
        "core.manual_billing_grant.is_manual_gift_grant_active",
        return_value=False,
    ), patch("services.whatsapp_catalog_sync.attempt_native_meta_sync") as attempt_mock:
        queued = enqueue_whatsapp_catalog_sync(db, 9, force=True, trigger="manual")
        status = build_whatsapp_catalog_sync_status(db, 9)
        drained = drain_whatsapp_catalog_sync(db, 9)
    assert queued["blocker_code"] == "entitlement_unavailable"
    assert queued["phase"] == "retrying"
    assert _upgrade_copy_present(queued) is False
    assert status["blocker_code"] == "entitlement_unavailable"
    assert status["phase"] == "retrying"
    assert _upgrade_copy_present(status) is False
    assert "تعذر" in (status.get("message_ar") or "") or "تعذّر" in (status.get("message_ar") or "")
    assert drained.get("skipped") is True
    attempt_mock.assert_not_called()
    assert pending.sync_status == "pending"

    with patch("services.whatsapp_catalog_sync.get_entitlements", _entitled), patch(
        "services.whatsapp_catalog_sync.iter_tenant_products",
        return_value=[pending],
    ), patch("services.whatsapp_catalog_sync.attempt_native_meta_sync") as recovered:
        recovered.return_value = {"ok": True, "sync_status": "synced"}
        queued_ok = enqueue_whatsapp_catalog_sync(db, 9, force=True, trigger="manual")
        status_ok = build_whatsapp_catalog_sync_status(db, 9)
        drained_ok = drain_whatsapp_catalog_sync(db, 9)
    assert queued_ok["queued"] is True
    assert queued_ok["blocker_code"] is None
    assert status_ok["ready"] is True
    assert status_ok["phase"] != "blocked"
    assert drained_ok.get("skipped") is not True
    recovered.assert_called()


def test_status_returns_retrying_when_product_scan_would_also_fail():
    from sqlalchemy.exc import OperationalError

    db = MagicMock()

    def _query(model):
        raise OperationalError("SELECT", {}, Exception("session poisoned after entitlement lookup"))

    db.query.side_effect = _query
    with patch("core.billing.get_tenant_subscription", return_value=None), patch(
        "core.manual_billing_grant.is_manual_gift_grant_active",
        return_value=False,
    ):
        status = build_whatsapp_catalog_sync_status(db, 9)
    assert status["blocker_code"] == "entitlement_unavailable"
    assert status["phase"] == "retrying"
    assert status["ok"] is True
    assert _upgrade_copy_present(status) is False


def test_whatsapp_sync_post_transient_is_503_not_upgrade():
    from fastapi import HTTPException
    from routers.catalog import _WhatsappCatalogSyncBody, merchant_whatsapp_catalog_sync

    db = _db_salla_entitlement_outage()
    request = MagicMock()
    with patch("routers.catalog.resolve_tenant_id", return_value=9), patch(
        "core.billing.get_tenant_subscription",
        return_value=None,
    ), patch(
        "core.manual_billing_grant.is_manual_gift_grant_active",
        return_value=False,
    ), patch(
        "services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain",
    ) as drain_sched:
        with pytest.raises(HTTPException) as caught:
            import asyncio

            asyncio.run(
                merchant_whatsapp_catalog_sync(
                    _WhatsappCatalogSyncBody(),
                    request,
                    db,
                    {"sub": "t"},
                )
            )
    assert caught.value.status_code == 503
    detail = caught.value.detail
    blob = detail if isinstance(detail, dict) else {}
    assert blob.get("blocker_code") == "entitlement_unavailable" or blob.get("error") == "entitlement_unavailable"
    assert "feature_locked" not in str(detail)
    assert "رقِّ" not in str(detail)
    drain_sched.assert_not_called()


def test_whatsapp_sync_post_confirmed_lock_remains_403():
    from fastapi import HTTPException
    from routers.catalog import _WhatsappCatalogSyncBody, merchant_whatsapp_catalog_sync

    db = _db_with_conn(_conn())
    request = MagicMock()
    with patch("routers.catalog.resolve_tenant_id", return_value=9), patch(
        "routers.catalog.get_entitlements",
        _not_entitled,
    ):
        with pytest.raises(HTTPException) as caught:
            import asyncio

            asyncio.run(
                merchant_whatsapp_catalog_sync(
                    _WhatsappCatalogSyncBody(),
                    request,
                    db,
                    {"sub": "t"},
                )
            )
    assert caught.value.status_code == 403
    assert "upgrade_required" in str(caught.value.detail) or "feature" in str(caught.value.detail).lower()


def test_ui_keeps_polling_retrying_and_has_no_upgrade_copy_in_card():
    from pathlib import Path

    card = (
        Path(__file__).resolve().parents[2]
        / "dashboard"
        / "src"
        / "components"
        / "catalog"
        / "CatalogWhatsAppSyncCard.tsx"
    ).read_text(encoding="utf-8")
    assert "'retrying'" in card
    assert "FOLLOW_PHASES" in card
    assert "رقِّ الخطة" not in card


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
def test_readiness_blocks_when_catalog_unlinked():
    db = _db_with_conn(_conn(catalog_enabled=False, meta_catalog_id=None, access_token=""))
    ready = evaluate_whatsapp_catalog_sync_readiness(db, 9)
    assert ready["ready"] is False
    assert ready["blocker_code"] == "catalog_disabled"


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
def test_enqueue_refuses_invalid_link_without_claiming_success():
    db = _db_with_conn(_conn(catalog_enabled=False))
    result = enqueue_whatsapp_catalog_sync(db, 9, force=True, trigger="manual")
    assert result["queued"] is False
    assert result["phase"] == "blocked"
    assert result["ok"] is False


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
def test_enqueue_marks_salla_and_native_not_meta_import():
    salla = _product(id=201)
    native = _product(
        id=501,
        source=SOURCE_NAHLA_NATIVE,
        ownership_mode=OWNERSHIP_NAHLA_MANAGED,
        title="عطر ورد 100ml",
    )
    meta_row = _product(
        id=77,
        source="meta",
        ownership_mode=OWNERSHIP_META_READONLY,
    )
    other_tenant = _product(id=9, tenant_id=10, title="يجب ألا يُمس")
    db = _db_with_conn(_conn())
    with patch(
        "services.whatsapp_catalog_sync.iter_tenant_products",
        return_value=[salla, native, meta_row, other_tenant],
    ):
        result = enqueue_whatsapp_catalog_sync(db, 9, force=True, trigger="manual")
    assert result["queued"] is True
    assert result["phase"] == "queued"
    assert result["enqueued"] == 2
    assert salla.sync_status == "pending"
    assert native.sync_status == "pending"
    assert meta_row.sync_status is None
    assert other_tenant.sync_status is None


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
def test_empty_catalog_enqueue_is_queued_zero():
    db = _db_with_conn(_conn())
    with patch("services.whatsapp_catalog_sync.iter_tenant_products", return_value=[]):
        result = enqueue_whatsapp_catalog_sync(db, 9, force=True, trigger="auto")
    assert result["queued"] is True
    assert result["enqueued"] == 0
    assert result["eligible"] == 0


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
@patch("services.whatsapp_catalog_sync.attempt_native_meta_sync")
def test_drain_isolates_tenants_and_skips_synced(attempt_mock):
    pending = _product(id=201, sync_status="pending")
    synced = _product(id=202, sync_status="synced", last_synced_at=datetime.now(timezone.utc))
    db = _db_with_conn(_conn())
    attempt_mock.return_value = {"ok": True, "sync_status": "synced"}
    with patch(
        "services.whatsapp_catalog_sync.iter_tenant_products",
        return_value=[pending, synced],
    ):
        out = drain_whatsapp_catalog_sync(db, 9, limit=25)
    assert out["processed"] == 1
    attempt_mock.assert_called_once_with(db, 9, 201, client=None)


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
@patch("services.whatsapp_catalog_sync.attempt_native_meta_sync")
def test_readiness_stamp_rollback_failure_stops_batch(attempt_mock):
    from sqlalchemy.exc import OperationalError
    from services.native_meta_sync_orchestrator import CatalogSyncSessionUnusable

    first = _product(id=201, sync_status="pending")
    second = _product(id=202, sync_status="pending")
    db = _db_with_conn(_conn())
    inner_query = db.query.side_effect

    def _query(model):
        name = getattr(model, "__name__", str(model))
        if name == "Product":
            q = MagicMock()
            q.filter.return_value.first.return_value = first
            return q
        return inner_query(model)

    db.query.side_effect = _query
    db.commit.side_effect = RuntimeError("stamp commit failed")
    db.rollback.side_effect = OperationalError("ROLLBACK", {}, Exception("dead session"))
    attempt_mock.side_effect = [
        {"ok": False, "sync_status": "blocked"},
        {"ok": True, "sync_status": "synced"},
    ]
    with patch(
        "services.whatsapp_catalog_sync.iter_tenant_products",
        return_value=[first, second],
    ):
        with pytest.raises(CatalogSyncSessionUnusable) as caught:
            drain_whatsapp_catalog_sync(db, 9)
    assert caught.value.original_code == "readiness_stamp_failed"
    assert caught.value.__cause__ is not None
    assert attempt_mock.call_count == 1
    db.close.assert_called()


def _drain_acquire_db(row):
    db = _db_with_conn(_conn())
    inner = db.query.side_effect

    def _query(model):
        name = getattr(model, "__name__", str(model))
        if name == "Product":
            q = MagicMock()
            filtered = MagicMock()
            filtered.with_for_update.return_value.populate_existing.return_value.first.return_value = row
            filtered.first.return_value = row
            q.filter.return_value = filtered
            return q
        return inner(model)

    db.query.side_effect = _query
    return db


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
def test_drain_stops_when_live_syncing_rollback_fails(push_mock):
    from sqlalchemy.exc import OperationalError
    from services.native_meta_sync_orchestrator import CatalogSyncSessionUnusable

    listed_first = _product(id=201, sync_status="pending")
    second = _product(id=202, sync_status="pending")
    locked = _product(
        id=201,
        sync_status="syncing",
        extra_metadata={
            "sync_meta": {"syncing_started_at": datetime.now(timezone.utc).isoformat()},
        },
    )
    db = _drain_acquire_db(locked)
    db.rollback.side_effect = OperationalError("ROLLBACK", {}, Exception("dead session"))
    with patch(
        "services.whatsapp_catalog_sync.iter_tenant_products",
        return_value=[listed_first, second],
    ):
        with pytest.raises(CatalogSyncSessionUnusable) as caught:
            drain_whatsapp_catalog_sync(db, 9)
    assert caught.value.original_code == "sync_lock_not_acquired"
    push_mock.assert_not_called()
    db.close.assert_called()


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
def test_drain_stops_when_synced_rollback_fails(push_mock):
    from sqlalchemy.exc import OperationalError
    from services.native_meta_sync_orchestrator import CatalogSyncSessionUnusable

    listed_first = _product(id=201, sync_status="pending")
    second = _product(id=202, sync_status="pending")
    locked = _product(id=201, sync_status="synced")
    db = _drain_acquire_db(locked)
    db.rollback.side_effect = OperationalError("ROLLBACK", {}, Exception("dead session"))
    with patch(
        "services.whatsapp_catalog_sync.iter_tenant_products",
        return_value=[listed_first, second],
    ):
        with pytest.raises(CatalogSyncSessionUnusable):
            drain_whatsapp_catalog_sync(db, 9)
    push_mock.assert_not_called()


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
def test_drain_stops_when_non_acquirable_rollback_fails(push_mock):
    from sqlalchemy.exc import OperationalError
    from services.native_meta_sync_orchestrator import CatalogSyncSessionUnusable

    listed_first = _product(id=201, sync_status="pending")
    second = _product(id=202, sync_status="pending")
    locked = _product(id=201, sync_status="paused")
    db = _drain_acquire_db(locked)
    db.rollback.side_effect = OperationalError("ROLLBACK", {}, Exception("dead session"))
    with patch(
        "services.whatsapp_catalog_sync.iter_tenant_products",
        return_value=[listed_first, second],
    ):
        with pytest.raises(CatalogSyncSessionUnusable):
            drain_whatsapp_catalog_sync(db, 9)
    push_mock.assert_not_called()


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
@patch("services.whatsapp_catalog_sync.attempt_native_meta_sync")
def test_drain_retries_failed_when_due(attempt_mock):
    failed = _product(
        id=203,
        sync_status="failed",
        extra_metadata={
            "sync_meta": {
                "retry_count": 1,
                "next_retry_at": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
            }
        },
    )
    db = _db_with_conn(_conn())
    attempt_mock.return_value = {"ok": False, "sync_status": "failed"}
    with patch("services.whatsapp_catalog_sync.iter_tenant_products", return_value=[failed]):
        out = drain_whatsapp_catalog_sync(db, 9)
    assert out["processed"] == 1
    attempt_mock.assert_called_once()


def test_retry_is_due_respects_backoff_and_cap():
    now = datetime.now(timezone.utc)
    due = _product(
        sync_status="failed",
        extra_metadata={"sync_meta": {"retry_count": 1, "next_retry_at": (now - timedelta(seconds=1)).isoformat()}},
    )
    later = _product(
        sync_status="failed",
        extra_metadata={"sync_meta": {"retry_count": 1, "next_retry_at": (now + timedelta(hours=2)).isoformat()}},
    )
    exhausted = _product(
        sync_status="failed",
        extra_metadata={"sync_meta": {"retry_count": 5, "next_retry_at": (now - timedelta(seconds=1)).isoformat()}},
    )
    assert retry_is_due(due, now) is True
    assert retry_is_due(later, now) is False
    assert retry_is_due(exhausted, now) is False


def test_dirty_flag_when_update_arrives_during_sync():
    row = _product(sync_status="syncing")
    row.extra_metadata = {
        "sync_meta": {
            "syncing_started_at": datetime.now(timezone.utc).isoformat(),
        }
    }
    db = MagicMock()
    assert mark_native_meta_sync_pending(db, row) is True
    assert row.sync_status == "syncing"
    assert row.extra_metadata["sync_meta"]["dirty"] is True


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
def test_status_does_not_treat_enqueue_as_published():
    pending = _product(id=201, sync_status="pending")
    db = _db_with_conn(_conn())
    with patch("services.whatsapp_catalog_sync.iter_tenant_products", return_value=[pending]):
        status = build_whatsapp_catalog_sync_status(db, 9)
    assert status["phase"] == "queued"
    assert status["counts"]["pending"] == 1
    assert status["counts"]["synced"] == 0


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
@patch("services.whatsapp_catalog_sync.attempt_native_meta_sync")
def test_manual_and_auto_share_drain(attempt_mock):
    row = _product(id=201, sync_status="pending")
    db = _db_with_conn(_conn())
    attempt_mock.return_value = {"ok": True, "sync_status": "synced"}
    with patch("services.whatsapp_catalog_sync.iter_tenant_products", return_value=[row]):
        auto = drain_whatsapp_catalog_sync(db, 9)
        manual = drain_whatsapp_catalog_sync(db, 9)
    assert auto["processed"] == 1
    assert manual["processed"] == 1
    assert attempt_mock.call_count == 2


def test_unchanged_fingerprint_does_not_requeue():
    row = _product(sync_status="synced")
    fp = channel_content_fingerprint(row)
    db = MagicMock()
    assert mark_product_pending_after_catalog_write(db, row, previous_fingerprint=fp) is False
    assert row.sync_status == "synced"


def test_price_change_fingerprint_requeues():
    row = _product(sync_status="synced", price="77")
    before = channel_content_fingerprint(row)
    row.price = "83"
    db = MagicMock()
    assert mark_product_pending_after_catalog_write(db, row, previous_fingerprint=before) is True
    assert row.sync_status == "pending"


def test_variant_option_change_fingerprint_requeues():
    row = _product(
        sync_status="synced",
        extra_metadata={
            "currency": "SAR",
            "variants": [
                {
                    "retailer_id": "sku-blue-m",
                    "price": "83",
                    "stock_qty": 2,
                    "in_stock": True,
                    "options": {"color": "أزرق", "size": "M"},
                }
            ],
        },
    )
    before = channel_content_fingerprint(row)
    row.extra_metadata["variants"][0]["options"]["size"] = "L"
    db = MagicMock()
    assert mark_product_pending_after_catalog_write(db, row, previous_fingerprint=before) is True
    assert row.sync_status == "pending"


def test_description_change_fingerprint_requeues():
    row = _product(sync_status="synced", description="قديم")
    before = channel_content_fingerprint(row)
    row.description = "وصف محدّث للقميص"
    db = MagicMock()
    assert mark_product_pending_after_catalog_write(db, row, previous_fingerprint=before) is True
    assert row.sync_status == "pending"


def test_finalize_does_not_clear_newer_dirty_flag():
    row = _product(sync_status="syncing", extra_metadata={
        "sync_meta": {"sync_generation": 1, "content_generation": 1, "dirty": False},
    })
    db = MagicMock()

    def _refresh(obj, attribute_names=None):
        obj.extra_metadata = {
            "sync_meta": {"dirty": True, "content_generation": 2},
        }

    db.refresh.side_effect = _refresh
    _refresh_sync_meta_from_db(db, row)
    _mark_synced(row, meta_item_id="META-201", waba_linked=True)
    assert _requeue_if_dirty(row) is True
    assert row.sync_status == "pending"
    assert row.extra_metadata["sync_meta"]["dirty"] is False


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
@patch("services.whatsapp_catalog_sync.attempt_native_meta_sync")
def test_drain_skips_blocked_with_unchanged_reason(attempt_mock):
    blocked = _product(id=1, sync_status="blocked")
    fp = channel_content_fingerprint(blocked)
    blocked.extra_metadata = {
        "currency": "SAR",
        "sync_meta": {
            "block_class": "product",
            "last_error_code": "missing_image_url",
            "blocked_fingerprint": fp,
        },
    }
    synced = _product(id=2, sync_status="synced")
    db = _db_with_conn(_conn())
    with patch(
        "services.whatsapp_catalog_sync.iter_tenant_products",
        return_value=[blocked, synced],
    ):
        out = drain_whatsapp_catalog_sync(db, 9)
    assert out["processed"] == 0
    attempt_mock.assert_not_called()


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
def test_status_exposes_verification_limits():
    db = _db_with_conn(_conn())
    with patch("services.whatsapp_catalog_sync.iter_tenant_products", return_value=[]):
        status = build_whatsapp_catalog_sync_status(db, 9)
    assert "price" in status["verification"]["content_fields"]
    assert "price" in status["verification"]["lookup_fields"]
    assert "whatsapp_storefront_visibility" in status["verification"]["not_verified_fields"]


def test_force_on_in_flight_product_does_not_reset_backoff():
    now = datetime.now(timezone.utc)
    row = _product(
        sync_status="syncing",
        extra_metadata={
            "sync_meta": {
                "syncing_started_at": now.isoformat(),
                "retry_count": 3,
                "next_retry_at": (now + timedelta(hours=1)).isoformat(),
            }
        },
    )
    db = MagicMock()
    assert mark_native_meta_sync_pending(db, row) is True
    assert row.sync_status == "syncing"
    assert row.extra_metadata["sync_meta"]["retry_count"] == 3
    assert row.extra_metadata["sync_meta"]["dirty"] is True


def test_stale_lock_refresh_aborts_without_keeping_old_lease():
    row = _product(
        sync_status="syncing",
        extra_metadata={"sync_meta": {"lock_generation": 1, "sync_generation": 1, "content_generation": 1}},
    )
    db = MagicMock()

    def _refresh(obj, attribute_names=None):
        obj.sync_status = "syncing"
        obj.extra_metadata = {"sync_meta": {"lock_generation": 2, "content_generation": 1}}

    db.refresh.side_effect = _refresh
    result = _refresh_sync_meta_from_db(db, row)
    assert result["lost_lease"] is True
    assert row.extra_metadata["sync_meta"]["lock_generation"] == 2


def test_content_compare_requires_price_currency_availability():
    payload = {"price": 14900, "currency": "SAR", "availability": "in stock"}
    matched = compare_pushed_content_to_lookup(
        payload,
        {"price": 14900, "currency": "SAR", "availability": "in stock"},
    )
    assert matched["outcome"] == "matched"
    identity_only = compare_pushed_content_to_lookup(
        payload,
        {"id": "META-1", "retailer_id": "sku-1", "name": "قميص قطني أزرق"},
    )
    assert identity_only["outcome"] == "incomplete"
    mismatch = compare_pushed_content_to_lookup(
        payload,
        {"price": 8300, "currency": "SAR", "availability": "in stock"},
    )
    assert mismatch["outcome"] == "mismatch"
    assert "price" in mismatch["mismatched_fields"]


def test_classify_block_codes():
    assert classify_block_code("access_token_missing") == "readiness"
    assert classify_block_code("missing_image_url") == "product"
    assert classify_block_code("product_already_meta_managed") == "permanent"


def test_blocked_product_is_reconsidered_after_content_fix():
    row = _product(sync_status="blocked", extra_metadata={"image_url": ""})
    row.extra_metadata["sync_meta"] = {
        "block_class": "product",
        "last_error_code": "missing_image_url",
        "blocked_fingerprint": channel_content_fingerprint(row),
    }
    readiness = {"ready": True, "blocker_code": None}
    assert should_reconsider_blocked(row, readiness) is False
    row.extra_metadata["image_url"] = "https://cdn.example/shirt.webp"
    assert should_reconsider_blocked(row, readiness) is True


def test_blocked_readiness_is_reconsidered_only_when_ready_changes():
    row = _product(
        sync_status="blocked",
        extra_metadata={
            "sync_meta": {
                "block_class": "readiness",
                "last_error_code": "access_token_missing",
                "blocked_readiness_fp": "0|access_token_missing",
            }
        },
    )
    assert should_reconsider_blocked(row, {"ready": False, "blocker_code": "access_token_missing"}) is False
    assert should_reconsider_blocked(row, {"ready": True, "blocker_code": None}) is True
    row.extra_metadata["sync_meta"]["blocked_readiness_fp"] = "1|"
    assert should_reconsider_blocked(row, {"ready": True, "blocker_code": None}) is False


def test_auto_sync_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("NAHLA_WHATSAPP_CATALOG_AUTO_SYNC", raising=False)
    assert whatsapp_catalog_auto_sync_enabled() is False
    monkeypatch.setenv("NAHLA_WHATSAPP_CATALOG_AUTO_SYNC", "1")
    assert whatsapp_catalog_auto_sync_enabled() is True


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
@patch("services.whatsapp_catalog_sync.attempt_native_meta_sync")
def test_drain_retries_blocked_after_product_fix(attempt_mock):
    row = _product(id=201, sync_status="blocked", extra_metadata={"image_url": ""})
    row.extra_metadata["sync_meta"] = {
        "block_class": "product",
        "last_error_code": "missing_image_url",
        "blocked_fingerprint": "old-fp",
    }
    db = _db_with_conn(_conn())
    attempt_mock.return_value = {"ok": True, "sync_status": "synced"}
    with patch("services.whatsapp_catalog_sync.iter_tenant_products", return_value=[row]):
        out = drain_whatsapp_catalog_sync(db, 9)
    assert out["processed"] == 1
    attempt_mock.assert_called_once()


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
@patch("services.whatsapp_catalog_sync.attempt_native_meta_sync")
def test_concurrent_force_and_drain_skip_in_flight(attempt_mock):
    inflight = _product(
        id=201,
        sync_status="syncing",
        extra_metadata={
            "sync_meta": {
                "syncing_started_at": datetime.now(timezone.utc).isoformat(),
                "retry_count": 2,
            }
        },
    )
    db = _db_with_conn(_conn())
    with patch(
        "services.whatsapp_catalog_sync.iter_tenant_products",
        return_value=[inflight],
    ):
        queued = enqueue_whatsapp_catalog_sync(db, 9, force=True, trigger="manual")
        out = drain_whatsapp_catalog_sync(db, 9)
    assert queued["queued"] is True
    assert inflight.sync_status == "syncing"
    assert inflight.extra_metadata["sync_meta"]["retry_count"] == 2
    assert inflight.extra_metadata["sync_meta"]["dirty"] is True
    assert out["processed"] == 0
    attempt_mock.assert_not_called()


def test_drain_ready_tenants_noops_when_auto_flag_off(monkeypatch):
    monkeypatch.delenv("NAHLA_WHATSAPP_CATALOG_AUTO_SYNC", raising=False)
    from services.whatsapp_catalog_sync import drain_ready_tenants

    db = MagicMock()
    out = drain_ready_tenants(db)
    assert out["skipped"] is True
    db.query.assert_not_called()


def test_verify_retry_caps_without_unbounded_repush():
    from services.native_meta_sync_orchestrator import verify_retry_is_due

    now = datetime.now(timezone.utc)
    due = _product(
        sync_status="pending_verification",
        extra_metadata={"sync_meta": {"verify_retry_count": 1, "next_verify_at": (now - timedelta(seconds=1)).isoformat()}},
    )
    capped = _product(
        sync_status="pending_verification",
        extra_metadata={"sync_meta": {"verify_retry_count": 3, "next_verify_at": (now - timedelta(seconds=1)).isoformat()}},
    )
    assert verify_retry_is_due(due, now) is True
    assert verify_retry_is_due(capped, now) is False
    assert retry_is_due(capped, now) is False


def test_schedule_drain_does_not_bypass_auto_flag(monkeypatch):
    monkeypatch.delenv("NAHLA_WHATSAPP_CATALOG_AUTO_SYNC", raising=False)
    monkeypatch.setenv("NAHLA_FORCE_WHATSAPP_CATALOG_DRAIN", "1")
    with patch("services.whatsapp_catalog_sync.get_whatsapp_catalog_drain_executor") as ex_mock:
        schedule_whatsapp_catalog_drain(9)
        schedule_whatsapp_catalog_drain(9, allow_without_auto_flag=False)
        ex_mock.assert_not_called()
    monkeypatch.setenv("NAHLA_WHATSAPP_CATALOG_AUTO_SYNC", "1")
    executor = MagicMock()
    with patch("services.whatsapp_catalog_sync.get_whatsapp_catalog_drain_executor", return_value=executor):
        schedule_whatsapp_catalog_drain(9)
        executor.submit.assert_called_once()


def test_database_url_alone_never_used_by_pg_lock_suite(monkeypatch):
    from test_whatsapp_catalog_sync_postgres_locks import (
        _candidate_database_urls,
        _connect_engine,
    )

    monkeypatch.delenv("WA_CATALOG_SYNC_PG_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("WA_CATALOG_SYNC_PG_REQUIRED", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://must-not-connect.example:5432/prod")
    assert _candidate_database_urls() == []
    import test_whatsapp_catalog_sync_postgres_locks as pgmod

    with patch.object(pgmod, "create_engine") as ce:
        with pytest.raises(pytest.skip.Exception):
            _connect_engine()
        ce.assert_not_called()


def test_salla_bulk_save_preserves_sync_meta_during_syncing():
    from services.store_sync import StoreSyncService

    now = datetime.now(timezone.utc)
    existing = _product(
        id=201,
        external_id="salla-201",
        price="77",
        sku="SKU-SHIRT",
        description="وصف",
        sync_status="syncing",
        extra_metadata={
            "currency": "SAR",
            "image_url": "https://cdn.example/shirt.webp",
            "sync_meta": {
                "syncing_started_at": now.isoformat(),
                "retry_count": 3,
                "lock_generation": 4,
                "next_retry_at": (now + timedelta(hours=1)).isoformat(),
            },
        },
    )
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = existing
    svc = StoreSyncService(db, 9, adapter=SimpleNamespace())
    normalised = {
        "external_id": "salla-201",
        "title": "قميص قطني أزرق",
        "description": "وصف",
        "price": "83",
        "sku": "SKU-SHIRT",
        "in_stock": True,
        "stock_qty": 5,
        "currency": "SAR",
        "image_url": "https://cdn.example/shirt.webp",
        "source": "salla",
    }
    with patch("services.store_sync._upsert_variants_for"), patch(
        "core.catalog.assign_canonical_retailer_id"
    ):
        result = svc._apply_normalised_product(normalised, "salla")
    assert result["action"] == "updated"
    assert existing.source == "salla"
    assert existing.ownership_mode == OWNERSHIP_EXTERNAL_MANAGED
    assert is_merchant_editable_product(existing) is False
    assert existing.price == "83"
    assert existing.sync_status == "syncing"
    meta = existing.extra_metadata["sync_meta"]
    assert meta["retry_count"] == 3
    assert meta["lock_generation"] == 4
    assert meta["dirty"] is True
    assert int(meta.get("content_generation") or 0) >= 1


def test_salla_webhook_save_preserves_sync_meta_during_syncing():
    import asyncio

    from services.store_sync import StoreSyncService

    now = datetime.now(timezone.utc)
    existing = _product(
        id=201,
        external_id="salla-201",
        price="77",
        sku="SKU-SHIRT",
        description="وصف",
        sync_status="syncing",
        extra_metadata={
            "currency": "SAR",
            "image_url": "https://cdn.example/shirt.webp",
            "sync_meta": {
                "syncing_started_at": now.isoformat(),
                "retry_count": 2,
                "lock_generation": 6,
            },
        },
    )
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = existing
    svc = StoreSyncService(db, 9, adapter=SimpleNamespace())
    payload = {
        "id": "salla-201",
        "title": "قميص قطني أزرق",
        "description": "وصف",
        "price": "83",
        "sku": "SKU-SHIRT",
        "in_stock": True,
        "quantity": 4,
        "currency": "SAR",
        "image_url": "https://cdn.example/shirt.webp",
    }
    with patch("services.store_sync._upsert_variants_for"):
        asyncio.run(svc.handle_product_webhook(payload, webhook_event_type="product.updated"))
    assert existing.source == "salla"
    assert existing.ownership_mode == OWNERSHIP_EXTERNAL_MANAGED
    assert existing.price == "83"
    assert existing.sync_status == "syncing"
    meta = existing.extra_metadata["sync_meta"]
    assert meta["retry_count"] == 2
    assert meta["lock_generation"] == 6
    assert meta["dirty"] is True


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
@patch("services.whatsapp_catalog_sync.attempt_native_meta_sync")
def test_drain_reclaims_stale_syncing_but_skips_live(attempt_mock):
    now = datetime.now(timezone.utc)
    stale = _product(
        id=201,
        sync_status="syncing",
        extra_metadata={
            "sync_meta": {
                "syncing_started_at": (now - timedelta(minutes=20)).isoformat(),
                "lock_generation": 1,
            }
        },
    )
    live = _product(
        id=202,
        sync_status="syncing",
        extra_metadata={
            "sync_meta": {
                "syncing_started_at": now.isoformat(),
                "lock_generation": 2,
            }
        },
    )
    db = _db_with_conn(_conn())
    attempt_mock.return_value = {"ok": True, "sync_status": "synced"}
    with patch(
        "services.whatsapp_catalog_sync.iter_tenant_products",
        return_value=[stale, live],
    ):
        out = drain_whatsapp_catalog_sync(db, 9)
    assert out["processed"] == 1
    attempt_mock.assert_called_once_with(db, 9, 201, client=None)


def test_blocked_permission_resumes_when_connection_fingerprint_changes():
    old_fp = "1|CAT-GENERIC-001|aaaa|bbbb"
    row = _product(
        sync_status="blocked",
        extra_metadata={
            "sync_meta": {
                "block_class": "readiness",
                "last_error_code": "catalog_permission_denied",
                "blocked_readiness_fp": "1|",
                "blocked_connection_fp": old_fp,
                "permission_probe_count": 1,
                "permission_probe_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    same = {"ready": True, "blocker_code": None, "connection_fp": old_fp}
    assert should_reconsider_blocked(row, same) is False
    restored = {"ready": True, "blocker_code": None, "connection_fp": "1|CAT-GENERIC-001|cccc|dddd"}
    assert should_reconsider_blocked(row, restored) is True


def test_blocked_permission_does_not_loop_when_unchanged():
    row = _product(
        sync_status="blocked",
        extra_metadata={
            "sync_meta": {
                "block_class": "readiness",
                "last_error_code": "catalog_permission_denied",
                "blocked_readiness_fp": "1|",
                "blocked_connection_fp": "1|CAT|tok|ex",
                "permission_probe_count": 3,
                "permission_probe_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    readiness = {"ready": True, "blocker_code": None, "connection_fp": "1|CAT|tok|ex"}
    assert should_reconsider_blocked(row, readiness) is False


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
def test_status_counts_exhausted_verify_as_needs_attention():
    row = _product(
        id=201,
        sync_status="pending_verification",
        extra_metadata={"sync_meta": {"verify_retry_count": 3, "verify_exhausted": True}},
    )
    db = _db_with_conn(_conn())
    with patch("services.whatsapp_catalog_sync.iter_tenant_products", return_value=[row]):
        status = build_whatsapp_catalog_sync_status(db, 9)
    assert status["phase"] == "needs_attention"
    assert status["counts"]["failed"] == 1
    assert status["counts"]["pending_verification"] == 0
    assert "auto_sync_enabled" in status


def test_drain_tick_opens_and_closes_its_own_session(monkeypatch):
    from services.whatsapp_catalog_sync import run_whatsapp_catalog_drain_tick

    closed = []

    class _Sess:
        def close(self):
            closed.append("close")

        def rollback(self):
            return None

    monkeypatch.setenv("NAHLA_WHATSAPP_CATALOG_AUTO_SYNC", "1")
    monkeypatch.setattr("core.database.SessionLocal", lambda: _Sess())
    with patch("services.whatsapp_catalog_sync.drain_ready_tenants", return_value={"processed": 0}) as drain:
        out = run_whatsapp_catalog_drain_tick()
    drain.assert_called_once()
    assert closed == ["close"]
    assert out["processed"] == 0


def test_slow_drain_tick_does_not_block_event_loop(monkeypatch):
    import asyncio
    import time

    from services.whatsapp_catalog_sync import get_whatsapp_catalog_drain_executor

    def _slow():
        time.sleep(0.35)
        return {"processed": 0}

    async def _run():
        loop = asyncio.get_running_loop()
        drain_fut = loop.run_in_executor(get_whatsapp_catalog_drain_executor(), _slow)

        async def _ping():
            await asyncio.sleep(0.05)
            return "ok"

        pinged = await asyncio.wait_for(_ping(), timeout=0.2)
        await drain_fut
        return pinged

    assert asyncio.run(_run()) == "ok"


def test_connection_fingerprint_ignores_validation_clock():
    from services.whatsapp_catalog_sync import connection_sync_fingerprint

    token = "EAAB-stable"
    t1 = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 28, 18, 0, 0, tzinfo=timezone.utc)
    same = dict(
        access_token=token,
        extra_metadata={
            "token_scopes": ["catalog_management"],
            "granted_scopes": ["catalog_management"],
            "production_ready": True,
            "token_status": "valid",
        },
        meta_catalog_id="CAT-GENERIC-001",
        catalog_enabled=True,
        status="connected",
    )
    fp1 = connection_sync_fingerprint(_conn(**same, last_verified_at=t1))
    fp2 = connection_sync_fingerprint(_conn(**same, last_verified_at=t2))
    assert fp1 == fp2
    changed = dict(same)
    changed["extra_metadata"] = {
        **same["extra_metadata"],
        "granted_scopes": ["whatsapp_business_management"],
    }
    fp3 = connection_sync_fingerprint(_conn(**changed, last_verified_at=t2))
    assert fp3 != fp1
    row = _product(
        sync_status="blocked",
        extra_metadata={
            "sync_meta": {
                "block_class": "readiness",
                "last_error_code": "catalog_permission_denied",
                "blocked_readiness_fp": "1|",
                "blocked_connection_fp": fp1,
                "permission_probe_count": 3,
                "permission_probe_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    assert should_reconsider_blocked(row, {"ready": True, "blocker_code": None, "connection_fp": fp2}) is False
    assert should_reconsider_blocked(row, {"ready": True, "blocker_code": None, "connection_fp": fp3}) is True


def test_unexpected_sync_error_consumes_retry_budget():
    from services.native_meta_sync_orchestrator import MAX_AUTO_RETRIES, _mark_failed

    row = _product(sync_status="failed", extra_metadata={"sync_meta": {"retry_count": 0}})
    for i in range(MAX_AUTO_RETRIES):
        _mark_failed(row, error_code="unexpected_sync_error", summary="TypeError: boom")
        assert int(row.extra_metadata["sync_meta"].get("retry_count") or 0) == i + 1
    assert retry_is_due(row) is False
    _mark_failed(row, error_code="unexpected_sync_error", summary="TypeError: boom")
    assert int(row.extra_metadata["sync_meta"]["retry_count"]) >= MAX_AUTO_RETRIES
    assert retry_is_due(row) is False
    assert row.extra_metadata["sync_meta"].get("next_retry_at") is None


def test_new_content_generation_resets_verify_budget():
    from services.native_meta_sync_orchestrator import _mark_pending_verification

    row = _product(
        sync_status="pending_verification",
        extra_metadata={
            "currency": "SAR",
            "image_url": "https://cdn.example/item.webp",
            "sync_meta": {
                "content_generation": 1,
                "expected_content_generation": 1,
                "verify_retry_count": 3,
                "verify_exhausted": True,
            },
        },
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = None
    assert mark_native_meta_sync_pending(db, row) is True
    _mark_pending_verification(
        row,
        meta_item_id="META-201",
        comparison={"outcome": "mismatch"},
        waba_linked=True,
    )
    sm = row.extra_metadata["sync_meta"]
    assert int(sm.get("content_generation") or 0) >= 2
    assert int(sm.get("verify_retry_count") or 0) == 1
    assert sm.get("verify_exhausted") is False
    assert sm.get("next_verify_at") is not None


def test_salla_webhook_create_stamps_external_source():
    import asyncio

    from services.store_sync import StoreSyncService

    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    added = []
    db.add.side_effect = added.append
    svc = StoreSyncService(db, 9, adapter=SimpleNamespace(platform="salla"))
    payload = {
        "id": "salla-new-9",
        "title": "حذاء رياضي أبيض",
        "description": "وصف",
        "price": "120",
        "sku": "SHOE-1",
        "in_stock": True,
        "quantity": 3,
        "currency": "SAR",
        "image_url": "https://cdn.example/shoe.webp",
    }
    with patch("services.store_sync._upsert_variants_for"), patch(
        "services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"
    ):
        asyncio.run(svc.handle_product_webhook(payload, webhook_event_type="product.created"))
    assert added
    created = added[0]
    assert created.source == "salla"
    assert created.ownership_mode == OWNERSHIP_EXTERNAL_MANAGED
    assert is_merchant_editable_product(created) is False
    assert is_whatsapp_channel_publish_eligible(created) is True


def test_schedule_drain_coalesces_duplicate_tenant_and_bounds_queue(monkeypatch):
    import services.whatsapp_catalog_sync as wcs

    monkeypatch.setenv("NAHLA_WHATSAPP_CATALOG_AUTO_SYNC", "1")
    monkeypatch.setenv("NAHLA_FORCE_WHATSAPP_CATALOG_DRAIN", "1")
    if hasattr(wcs, "reset_whatsapp_catalog_drain_scheduler_for_tests"):
        wcs.reset_whatsapp_catalog_drain_scheduler_for_tests()
    submitted = []

    class _Exec:
        def submit(self, fn, *args, **kwargs):
            submitted.append(args)
            fut = MagicMock()
            fut.done.return_value = False
            return fut

    with patch("services.whatsapp_catalog_sync.get_whatsapp_catalog_drain_executor", return_value=_Exec()):
        for _ in range(1000):
            wcs.schedule_whatsapp_catalog_drain(9)
        queue_max = int(getattr(wcs, "DRAIN_QUEUE_MAX", 2) or 2)
        for tid in range(100, 100 + queue_max + 20):
            wcs.schedule_whatsapp_catalog_drain(tid)
    assert submitted.count((9,)) == 1
    assert len(submitted) <= queue_max + 2


def test_reconnect_schedule_coalesces_duplicate_tenant(monkeypatch):
    import services.meta_catalog_reconnect as rec

    monkeypatch.setenv("NAHLA_FORCE_META_CATALOG_RECONNECT", "1")
    if hasattr(rec, "reset_meta_catalog_reconnect_scheduler_for_tests"):
        rec.reset_meta_catalog_reconnect_scheduler_for_tests()
    started = []

    class _Exec:
        def submit(self, fn, *args, **kwargs):
            started.append(args)
            return MagicMock()

    rec.reset_meta_catalog_reconnect_scheduler_for_tests()
    with patch("services.meta_catalog_reconnect.get_meta_catalog_reconnect_executor", return_value=_Exec()):
        for _ in range(50):
            rec.schedule_meta_catalog_reconnect_best_effort(9)
    assert len(started) == 1


def test_drain_overflow_is_bounded_and_deferred_tenants_resume(monkeypatch):
    import services.whatsapp_catalog_sync as wcs

    monkeypatch.setenv("NAHLA_WHATSAPP_CATALOG_AUTO_SYNC", "1")
    monkeypatch.setenv("NAHLA_FORCE_WHATSAPP_CATALOG_DRAIN", "1")
    wcs.reset_whatsapp_catalog_drain_scheduler_for_tests()
    submitted = []
    run_log = []

    class _Exec:
        def __init__(self):
            self.jobs = []

        def submit(self, fn, *args, **kwargs):
            submitted.append(args)
            self.jobs.append((fn, args))
            fut = MagicMock()
            fut.done.return_value = False
            return fut

        def drain_one(self):
            fn, args = self.jobs.pop(0)
            fn(*args)

    executor = _Exec()

    def _runner(tid):
        run_log.append(int(tid))

    coalescer = wcs.TenantWorkCoalescer(
        max_queued=4,
        max_overflow=4,
        get_executor=lambda: executor,
        runner=_runner,
    )
    for _ in range(1000):
        coalescer.submit(9)
    assert submitted.count((9,)) == 1
    assert run_log == []

    extra = list(range(100, 140))
    for tid in extra:
        coalescer.submit(tid)
    snapshot = coalescer.queue_snapshot()
    bounded = snapshot["pending"] + snapshot["inflight"] + snapshot["dirty"] + snapshot["overflow"]
    assert snapshot["max_queued"] == 4
    assert snapshot["max_overflow"] == 4
    assert wcs.DRAIN_QUEUE_MAX == 32
    assert wcs.DRAIN_OVERFLOW_MAX == 32
    assert bounded <= snapshot["max_queued"] + snapshot["max_overflow"]
    assert snapshot["overflow"] <= snapshot["max_overflow"]
    assert snapshot["pending"] + snapshot["inflight"] <= snapshot["max_queued"]

    while executor.jobs:
        executor.drain_one()
    dropped = [tid for tid in extra if tid not in run_log]
    assert dropped
    for tid in dropped[:3]:
        coalescer.submit(tid)
    while executor.jobs:
        executor.drain_one()
    assert set(dropped[:3]).issubset(set(run_log))


def test_verify_budget_resets_on_new_generation_without_pending_preamble():
    from services.native_meta_sync_orchestrator import _mark_pending_verification

    row = _product(
        sync_status="pending_verification",
        extra_metadata={
            "currency": "SAR",
            "sync_meta": {
                "content_generation": 2,
                "expected_content_generation": 1,
                "verify_generation": 1,
                "verify_retry_count": 3,
                "verify_exhausted": True,
                "expected_payloads_by_retailer_id": {
                    "sku-blue": {"price": 7700, "currency": "SAR", "availability": "in stock"},
                },
            },
        },
    )
    _mark_pending_verification(
        row,
        meta_item_id="META-201",
        comparison={"outcome": "mismatch"},
        waba_linked=True,
    )
    sm = row.extra_metadata["sync_meta"]
    assert int(sm.get("verify_retry_count") or 0) == 1
    assert sm.get("verify_exhausted") is False
    assert sm.get("next_verify_at") is not None
    assert int(sm.get("verify_generation") or 0) == 2


def test_legacy_zero_verify_generation_exhausted_resets_on_new_content_gen():
    from services.native_meta_sync_orchestrator import _mark_pending_verification

    row = _product(
        sync_status="pending_verification",
        extra_metadata={
            "sync_meta": {
                "content_generation": 2,
                "expected_content_generation": 1,
                "verify_generation": 0,
                "verify_retry_count": 3,
                "verify_exhausted": True,
            },
        },
    )
    _mark_pending_verification(
        row,
        meta_item_id="META-9",
        comparison={"outcome": "mismatch"},
        waba_linked=False,
    )
    sm = row.extra_metadata["sync_meta"]
    assert int(sm.get("verify_retry_count") or 0) == 1
    assert sm.get("verify_exhausted") is False
    assert int(sm.get("verify_generation") or 0) == 2

    legacy_unspecified = _product(
        sync_status="pending_verification",
        extra_metadata={
            "sync_meta": {
                "content_generation": 2,
                "expected_content_generation": 0,
                "verify_generation": 0,
                "verify_retry_count": 3,
                "verify_exhausted": True,
            },
        },
    )
    _mark_pending_verification(
        legacy_unspecified,
        meta_item_id="META-10",
        comparison={"outcome": "mismatch"},
        waba_linked=False,
    )
    sm0 = legacy_unspecified.extra_metadata["sync_meta"]
    assert int(sm0.get("verify_retry_count") or 0) == 1
    assert sm0.get("verify_exhausted") is False


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
def test_force_same_generation_does_not_reset_verify_budget():
    row = _product(
        sync_status="pending_verification",
        extra_metadata={
            "currency": "SAR",
            "image_url": "https://cdn.example/item.webp",
            "sync_meta": {
                "content_generation": 2,
                "expected_content_generation": 2,
                "verify_generation": 2,
                "verify_retry_count": 2,
                "verify_exhausted": False,
            },
        },
    )
    db = _db_with_conn(_conn())
    with patch("services.whatsapp_catalog_sync.iter_tenant_products", return_value=[row]):
        enqueue_whatsapp_catalog_sync(db, 9, force=True, trigger="manual")
    sm = row.extra_metadata["sync_meta"]
    assert int(sm.get("content_generation") or 0) == 2
    assert int(sm.get("verify_retry_count") or 0) == 2


@patch("services.native_meta_sync_orchestrator._load_product")
@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status")
@patch("services.native_meta_sync_orchestrator.find_meta_catalog_item_by_retailer_id")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_unexpected_graph_exception_consumes_budget_through_except(
    preview_mock,
    ensure_mock,
    push_mock,
    lookup_mock,
    waba_mock,
    lock_mock,
    load_mock,
):
    from services.native_meta_sync_orchestrator import (
        MAX_AUTO_RETRIES,
        attempt_native_meta_sync,
        retry_is_due,
    )

    row = _product(
        id=201,
        sync_status="pending",
        extra_metadata={"currency": "SAR", "image_url": "https://cdn.example/item.webp", "sync_meta": {}},
    )
    load_mock.return_value = row

    def _acquire(_db, _tid, _pid):
        row.sync_status = "syncing"
        sm = dict(row.extra_metadata.get("sync_meta") or {})
        sm["lock_generation"] = int(sm.get("lock_generation") or 0) + 1
        row.extra_metadata = {**row.extra_metadata, "sync_meta": sm}
        return row

    lock_mock.side_effect = _acquire
    preview_mock.side_effect = RuntimeError("graph client exploded")
    db = MagicMock()
    for i in range(MAX_AUTO_RETRIES):
        result = attempt_native_meta_sync(db, 9, 201)
        assert result.get("error_code") == "unexpected_sync_error"
        assert row.sync_status != "syncing"
        if i == 0:
            assert row.extra_metadata["sync_meta"].get("next_retry_at") is not None
            assert int(row.extra_metadata["sync_meta"].get("retry_count") or 0) == 1
    assert int(row.extra_metadata["sync_meta"].get("retry_count") or 0) >= MAX_AUTO_RETRIES
    assert retry_is_due(row) is False
    push_mock.assert_not_called()


def test_dirty_update_during_push_is_drained_without_unbounded_resubmit(monkeypatch):
    import threading

    import services.whatsapp_catalog_sync as wcs
    from services.native_meta_sync_orchestrator import attempt_native_meta_sync

    monkeypatch.setenv("NAHLA_WHATSAPP_CATALOG_AUTO_SYNC", "1")
    monkeypatch.setenv("NAHLA_FORCE_WHATSAPP_CATALOG_DRAIN", "1")
    wcs.reset_whatsapp_catalog_drain_scheduler_for_tests()

    row = _product(
        id=201,
        sync_status="pending",
        extra_metadata={
            "currency": "SAR",
            "image_url": "https://cdn.example/item.webp",
            "product_url": "https://example.test/p",
            "sync_meta": {"content_generation": 1, "lock_generation": 0},
        },
    )
    started = threading.Event()
    release = threading.Event()
    pushed = []

    def _acquire(_db, _tid, _pid):
        row.sync_status = "syncing"
        sm = dict(row.extra_metadata["sync_meta"])
        sm["lock_generation"] = int(sm.get("lock_generation") or 0) + 1
        sm["sync_generation"] = int(sm.get("content_generation") or 1)
        row.extra_metadata = {**row.extra_metadata, "sync_meta": sm}
        return row

    def _push(*_a, **_k):
        started.set()
        assert release.wait(timeout=2)
        pushed.append(dict(row.extra_metadata["sync_meta"]))
        return {
            "ok": True,
            "payload": {"price": 8300, "currency": "SAR", "availability": "in stock"},
        }

    submitted = []

    class _Exec:
        def submit(self, fn, *args, **kwargs):
            submitted.append(args)
            return MagicMock()

    db = MagicMock()
    with patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock", side_effect=_acquire), patch(
        "services.native_meta_sync_orchestrator._load_product", return_value=row
    ), patch(
        "services.native_meta_sync_orchestrator.preview_native_meta_sync",
        return_value={"eligible": True, "retailer_id": "sku-blue", "fatal_errors": [], "warnings": []},
    ), patch(
        "services.meta_catalog_sync_confirm.ensure_native_default_variant",
        return_value=(SimpleNamespace(retailer_id="sku-blue"), False),
    ), patch(
        "services.native_meta_sync_orchestrator._collect_retailer_ids", return_value=["sku-blue"]
    ), patch(
        "services.native_meta_sync_orchestrator.push_one_meta_catalog_item", side_effect=_push
    ), patch(
        "services.native_meta_sync_orchestrator.find_meta_catalog_item_by_retailer_id",
        return_value=("META-X", {"matched": True, "item": {"price": 8300, "currency": "SAR", "availability": "in stock"}}),
    ), patch(
        "services.native_meta_sync_orchestrator.get_waba_catalog_link_status",
        return_value={"ok": True, "expected_catalog_linked": True},
    ), patch(
        "services.whatsapp_catalog_sync.get_whatsapp_catalog_drain_executor", return_value=_Exec()
    ):
        worker = threading.Thread(target=lambda: attempt_native_meta_sync(db, 9, 201), daemon=True)
        worker.start()
        assert started.wait(timeout=2)
        assert mark_native_meta_sync_pending(db, row) is True
        for _ in range(25):
            wcs.schedule_whatsapp_catalog_drain(9)
        assert submitted.count((9,)) <= 1
        release.set()
        worker.join(timeout=2)
        assert worker.is_alive() is False
        assert row.extra_metadata["sync_meta"].get("dirty") is True or row.sync_status == "pending"
        assert int(row.extra_metadata["sync_meta"].get("content_generation") or 0) >= 2
        second = attempt_native_meta_sync(db, 9, 201)
        assert second.get("error_code") != "sync_lock_not_acquired"
        assert row.sync_status != "syncing"
        assert len(pushed) >= 2
        assert int(row.extra_metadata["sync_meta"].get("content_generation") or 0) >= 2
        assert int((pushed[-1] or {}).get("content_generation") or 0) >= 2


def test_ci_whatsapp_catalog_pg_job_uses_isolated_dsn_only():
    from pathlib import Path

    ci_text = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "whatsapp-catalog-sync-postgres:" in ci_text
    job_start = ci_text.index("whatsapp-catalog-sync-postgres:")
    window = ci_text[job_start:job_start + 4500]
    assert "WA_CATALOG_SYNC_PG_REQUIRED: \"1\"" in window or "WA_CATALOG_SYNC_PG_REQUIRED: '1'" in window
    assert "WA_CATALOG_SYNC_PG_TEST_DATABASE_URL:" in window
    assert "test_whatsapp_catalog_sync_postgres_locks.py" in window
    assert "unset DATABASE_URL" in window
    assert "scripts/check_junit_clean.py" in window
    assigned_database_url = [
        line.strip()
        for line in window.splitlines()
        if line.strip().startswith("DATABASE_URL:")
    ]
    assert assigned_database_url == []
