"""Tenant sync status is evidence-based; token/permission failures block, not retry.

Generic merchant data only. Asserts structured fields (phase, link state,
counts, latency, action codes), never Arabic sentences.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_BACKEND_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.catalog import OWNERSHIP_EXTERNAL_MANAGED, OWNERSHIP_NAHLA_MANAGED, SOURCE_NAHLA_NATIVE  # noqa: E402
from services.native_meta_sync_orchestrator import (  # noqa: E402
    MAX_AUTO_RETRIES,
    attempt_native_meta_sync,
    classify_block_code,
    classify_graph_push_failure,
)
from services.whatsapp_catalog_sync import (  # noqa: E402
    _failed_requeue_after_connection_change,
    build_whatsapp_catalog_sync_status,
    catalog_link_evidence,
    failure_action_code,
)


def _entitled(*_a, **_k):
    return SimpleNamespace(has_feature=lambda key: key == "meta_catalog_sync")


def _conn(**overrides):
    base = dict(
        tenant_id=9,
        catalog_enabled=True,
        meta_catalog_id="CAT-GENERIC-001",
        whatsapp_business_account_id="WABA-9",
        access_token="EAAB-test",
        extra_metadata={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _db(conn):
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


def _synced_row(i, *, pending_at, push_at, verified_at, linked=True):
    return SimpleNamespace(
        id=300 + i,
        tenant_id=9,
        title="حذاء رياضي أبيض",
        source="salla",
        ownership_mode=OWNERSHIP_EXTERNAL_MANAGED,
        catalog_status="active",
        merchant_hidden_at=None,
        in_stock=True,
        stock_quantity=1,
        sync_status="synced",
        sync_error=None,
        last_synced_at=verified_at,
        meta_item_id=None,
        extra_metadata={
            "currency": "SAR",
            "sync_meta": {
                "pending_at": pending_at.isoformat(),
                "last_push_at": push_at.isoformat(),
                "verified_at": verified_at.isoformat(),
                "content_verified": True,
                "waba_catalog_linked": linked,
            },
        },
    )


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
def test_published_without_link_proof_is_not_reported_as_published():
    now = datetime.now(timezone.utc)
    rows = [_synced_row(i, pending_at=now - timedelta(seconds=90), push_at=now - timedelta(seconds=30),
                        verified_at=now - timedelta(seconds=25), linked=None) for i in range(3)]
    for row in rows:
        row.extra_metadata["sync_meta"].pop("waba_catalog_linked")
    db = _db(_conn())
    with patch("services.whatsapp_catalog_sync.iter_tenant_products", return_value=rows):
        status = build_whatsapp_catalog_sync_status(db, 9)
    assert status["counts"]["synced"] == 3
    assert status["catalog_linked"] is False
    assert status["catalog_configured"] is True
    assert status["catalog_link"]["state"] == "unknown"
    assert status["phase"] == "needs_attention"
    assert status["blocker_code"] == "waba_catalog_link_unproven"
    assert status["stages"]["catalog_link"]["action_code"] == "verify_catalog_link"
    assert status["stages"]["publish"]["verified_in_meta"] == 3


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
def test_link_proof_from_reconnect_bind_makes_phase_published_and_measures_latency():
    now = datetime.now(timezone.utc)
    rows = [_synced_row(i, pending_at=now - timedelta(seconds=100 + i), push_at=now - timedelta(seconds=40),
                        verified_at=now - timedelta(seconds=35)) for i in range(2)]
    conn = _conn(extra_metadata={
        "meta_catalog_bind": {"ok": True, "link_status": "linked", "catalog_id": "CAT-GENERIC-001",
                              "at": (now - timedelta(minutes=5)).isoformat()},
    })
    with patch("services.whatsapp_catalog_sync.iter_tenant_products", return_value=rows):
        status = build_whatsapp_catalog_sync_status(_db(conn), 9)
    assert status["phase"] == "published"
    assert status["catalog_linked"] is True
    # the product verification stamps (35s ago) are newer than the bind (5 min ago)
    assert status["catalog_link"]["evidence_source"] == "product_verification"
    assert status["catalog_link"]["stale"] is False
    lat = status["latency"]
    assert lat["platform"]["n"] == 2 and 59 <= lat["platform"]["p50_seconds"] <= 62
    assert lat["channel"]["n"] == 2 and 4 <= lat["channel"]["p50_seconds"] <= 6
    assert lat["waiting"]["n"] == 0


def test_link_evidence_for_another_catalog_is_ignored_and_product_stamp_wins_when_newer():
    now = datetime.now(timezone.utc)
    conn = _conn(extra_metadata={
        "meta_catalog_bind": {"ok": True, "link_status": "linked", "catalog_id": "CAT-OLD-999",
                              "at": (now - timedelta(minutes=1)).isoformat()},
        "wa_catalog_reconcile": {"catalog_id": "CAT-GENERIC-001", "waba_link_state": "not_linked",
                                 "at": (now - timedelta(hours=3)).isoformat()},
    })
    out = catalog_link_evidence(MagicMock(), 9, conn=conn)
    assert out["state"] == "not_linked" and out["evidence_source"] == "reconcile"
    newer = {"at": (now - timedelta(minutes=30)).isoformat(), "linked": True}
    out = catalog_link_evidence(MagicMock(), 9, conn=conn, product_evidence=newer)
    assert out["state"] == "linked" and out["evidence_source"] == "product_verification"
    assert catalog_link_evidence(MagicMock(), 9, conn=_conn())["state"] == "unknown"


@patch("services.whatsapp_catalog_sync.get_entitlements", _entitled)
def test_failures_carry_action_codes_and_retirement_counts_are_reported():
    now = datetime.now(timezone.utc)
    failed = _synced_row(1, pending_at=now, push_at=now, verified_at=now)
    failed.sync_status = "blocked"
    failed.sync_error = "catalog_permission_denied"
    failed.extra_metadata["sync_meta"].update({"last_error_code": "catalog_permission_denied", "last_error_summary": "x"})
    hidden = _synced_row(2, pending_at=now, push_at=now, verified_at=now)
    hidden.catalog_status = "merchant_hidden"
    hidden.merchant_hidden_at = now
    hidden.extra_metadata["sync_meta"].update({"retire_pending": True, "retire_exhausted": True, "retire_last_error": "meta_http_error"})
    retired = _synced_row(3, pending_at=now, push_at=now, verified_at=now)
    retired.catalog_status = "merchant_hidden"
    retired.merchant_hidden_at = now
    retired.sync_status = "retired"
    with patch("services.whatsapp_catalog_sync.iter_tenant_products", return_value=[failed, hidden, retired]):
        status = build_whatsapp_catalog_sync_status(_db(_conn()), 9)
    codes = {f["product_id"]: f["action_code"] for f in status["failures"]}
    assert codes[301] == "grant_catalog_permission"
    assert codes[302] == "check_item_in_meta"
    assert status["counts"]["retire_pending"] == 1 and status["counts"]["retired"] == 1
    assert status["retirement"]["exhausted"] == 1
    assert status["phase"] == "needs_attention"
    assert status["stages"]["retirement"]["state"] == "attention"


def test_failure_action_code_defaults():
    assert failure_action_code("access_token_invalid") == "reconnect_whatsapp"
    assert failure_action_code("missing_image_url") == "add_product_image"
    assert failure_action_code("something_new") == "check_product"


# ── Token expiry / permission loss ────────────────────────────────────────

def test_graph_token_and_permission_errors_are_readiness_blocks():
    expired = {"meta": {"http_status": 400, "response": {"error": {"code": 190, "error_subcode": 463, "message": "Session expired"}}}}
    assert classify_graph_push_failure(expired, "meta_http_error") == "access_token_invalid"
    assert classify_block_code("access_token_invalid") == "readiness"
    perm = {"meta": {"http_status": 403, "response": {"error": {"code": 10, "message": "permission"}}}}
    assert classify_graph_push_failure(perm, "meta_http_error") == "catalog_permission_denied"
    link_perm = {"meta": {"http_status": 400, "response": {"error": {"code": 100, "error_subcode": 2388100}}}}
    assert classify_graph_push_failure(link_perm, "meta_http_error") == "catalog_permission_denied"
    rate = {"meta": {"http_status": 429, "response": {"error": {"code": 4}}}}
    assert classify_graph_push_failure(rate, "meta_http_error") == "meta_rate_limited"
    lookup = {"lookup": {"http_status": 400, "error": '{"error":{"code":190,"message":"expired"}}'}}
    assert classify_graph_push_failure(lookup, "lookup_failed") == "access_token_invalid"
    other = {"meta": {"http_status": 500, "response": {"error": {"code": 1}}}}
    assert classify_graph_push_failure(other, "meta_http_error") == "meta_http_error"


def _generic_native_parent(**overrides):
    base = dict(
        id=501, tenant_id=9, title="عطر ورد 100ml", description="وصف", price="320", sku=None,
        meta_retailer_id="nahla_p_501", in_stock=True, stock_quantity=3, source=SOURCE_NAHLA_NATIVE,
        ownership_mode=OWNERSHIP_NAHLA_MANAGED, catalog_status="active", merchant_hidden_at=None,
        extra_metadata={"currency": "SAR", "image_url": "https://cdn.example/rose.webp",
                        "product_url": "https://store.example/p/rose", "sync_meta": {"lock_generation": 1}},
        sync_status="syncing", sync_error=None, last_synced_at=None, meta_item_id=None, meta_catalog_published_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@patch("services.native_meta_sync_orchestrator._stamp_with_lease", side_effect=lambda db, p, lease, fn: (fn(p) or True))
@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
@patch("services.native_meta_sync_orchestrator._resolve_connection")
def test_expired_token_blocks_without_consuming_retry_budget(resolve_mock, preview_mock, ensure_mock, push_mock, lock_mock, _stamp):
    parent = _generic_native_parent()
    lock_mock.return_value = parent
    resolve_mock.return_value = _conn()
    preview_mock.return_value = {"eligible": True, "fatal_errors": [], "retailer_id": "nahla_p_501"}
    ensure_mock.return_value = (SimpleNamespace(retailer_id="nahla_p_501"), False)
    push_mock.return_value = {
        "ok": False, "action": "create", "error": "meta_http_error",
        "meta": {"http_status": 400, "response": {"error": {"code": 190, "message": "Error validating access token"}}},
        "payload": {}, "lookup": {},
    }
    with patch("services.native_meta_sync_orchestrator._collect_retailer_ids", return_value=["nahla_p_501"]):
        result = attempt_native_meta_sync(MagicMock(), 9, 501)
    assert result["ok"] is False
    assert result["error_code"] == "access_token_invalid"
    assert parent.sync_status == "blocked"
    sm = parent.extra_metadata["sync_meta"]
    assert sm["block_class"] == "readiness"
    assert int(sm.get("retry_count") or 0) == 0


@patch("services.native_meta_sync_orchestrator._stamp_with_lease", side_effect=lambda db, p, lease, fn: (fn(p) or True))
@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status", return_value={"ok": True, "expected_catalog_linked": True})
@patch("services.native_meta_sync_orchestrator.find_meta_catalog_item_by_retailer_id")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
@patch("services.native_meta_sync_orchestrator._resolve_connection")
def test_republish_after_retirement_sends_visibility_published(resolve_mock, preview_mock, ensure_mock, push_mock, lookup_mock, _waba, lock_mock, _stamp):
    parent = _generic_native_parent()
    parent.extra_metadata["sync_meta"]["channel_retired_at"] = datetime.now(timezone.utc).isoformat()
    lock_mock.return_value = parent
    resolve_mock.return_value = _conn()
    preview_mock.return_value = {"eligible": True, "fatal_errors": [], "retailer_id": "nahla_p_501"}
    ensure_mock.return_value = (SimpleNamespace(retailer_id="nahla_p_501"), False)
    push_mock.return_value = {"ok": True, "action": "update", "meta_product_id": "META-501",
                              "payload": {"price": 32000, "currency": "SAR", "availability": "in stock", "visibility": "published"},
                              "meta": {"http_status": 200, "response": {"success": True}}, "lookup": {"matched": True}}
    lookup_mock.return_value = ("META-501", {"matched": True, "item": {"id": "META-501", "retailer_id": "nahla_p_501",
                                                                        "price": 32000, "currency": "SAR", "availability": "in stock"}})
    with patch("services.native_meta_sync_orchestrator._collect_retailer_ids", return_value=["nahla_p_501"]), \
         patch("services.native_meta_sync_orchestrator.claim_active_meta_item_binding"):
        result = attempt_native_meta_sync(MagicMock(), 9, 501)
    assert result["ok"] is True
    assert push_mock.call_args.kwargs["payload_overrides"] == {"visibility": "published"}
    sm = parent.extra_metadata["sync_meta"]
    assert sm["channel_retired_at"] is None and sm["republished_at"]
    assert sm["last_push_at"]


def test_exhausted_failed_row_is_requeued_only_after_connection_changes():
    row = SimpleNamespace(
        id=7, tenant_id=9, source="salla", ownership_mode=OWNERSHIP_EXTERNAL_MANAGED, catalog_status="active",
        merchant_hidden_at=None, sync_status="failed", sync_error="meta_http_error", in_stock=True,
        extra_metadata={"sync_meta": {"retry_count": MAX_AUTO_RETRIES, "failed_connection_fp": "1|CAT|aaaa|bbbb"}},
    )
    db = MagicMock()
    assert _failed_requeue_after_connection_change(db, row, "1|CAT|aaaa|bbbb") is False
    assert row.sync_status == "failed"
    assert _failed_requeue_after_connection_change(db, row, "1|CAT|cccc|bbbb") is True
    assert row.sync_status == "pending"
    assert row.extra_metadata["sync_meta"]["retry_count"] == 0
