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

import pytest

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
    parent.extra_metadata["sync_meta"]["retire_blocked"] = "no_publication_evidence"
    lock_mock.return_value = parent
    resolve_mock.return_value = _conn()
    preview_mock.return_value = {"eligible": True, "fatal_errors": [], "retailer_id": "nahla_p_501"}
    ensure_mock.return_value = (SimpleNamespace(retailer_id="nahla_p_501"), False)
    push_mock.return_value = {"ok": True, "action": "update", "meta_product_id": "META-501",
                              "payload": {"price": 32000, "currency": "SAR", "availability": "in stock", "visibility": "published"},
                              "meta": {"http_status": 200, "response": {"success": True}}, "lookup": {"matched": True}}
    lookup_mock.return_value = ("META-501", {"matched": True, "item": {"id": "META-501", "retailer_id": "nahla_p_501",
                                                                        "price": 32000, "currency": "SAR", "availability": "in stock"}})
    evidence_writes = []
    with patch("services.native_meta_sync_orchestrator._collect_retailer_ids", return_value=["nahla_p_501"]), \
         patch("services.native_meta_sync_orchestrator.claim_active_meta_item_binding"), \
         patch("services.native_meta_sync_orchestrator.load_variant_for_push",
               return_value=(parent, SimpleNamespace(id=5011))), \
         patch("services.salla_variant_catalog_identity.upsert_native_publication_membership",
               side_effect=lambda db, **kw: evidence_writes.append(kw) or {"ok": True}):
        result = attempt_native_meta_sync(MagicMock(), 9, 501)
    assert result["ok"] is True
    assert push_mock.call_args.kwargs["payload_overrides"] == {"visibility": "published"}
    # the successful update POST re-records publication evidence for the live item
    assert [(w["retailer_id"], w["meta_item_id"], w["variant_id"]) for w in evidence_writes] == [("nahla_p_501", "META-501", 5011)]
    sm = parent.extra_metadata["sync_meta"]
    assert sm["channel_retired_at"] is None and sm["republished_at"]
    assert sm["last_push_at"]
    assert sm["retire_blocked"] is None             # the earlier refusal no longer describes the row


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


# ── Publication evidence is written only for the item this attempt's POST published ──

_ORCH_PATCHES = (
    "services.native_meta_sync_orchestrator._resolve_connection",
    "services.native_meta_sync_orchestrator.preview_native_meta_sync",
    "services.meta_catalog_sync_confirm.ensure_native_default_variant",
    "services.native_meta_sync_orchestrator.push_one_meta_catalog_item",
    "services.native_meta_sync_orchestrator.find_meta_catalog_item_by_retailer_id",
    "services.native_meta_sync_orchestrator.get_waba_catalog_link_status",
    "services.native_meta_sync_orchestrator._try_acquire_sync_lock",
)


def _run_orchestrator(parent, *, push_result, lookup_id, lookup_meta=None, salla=False, upsert_result=None,
                      catalogs=None):
    """One sync attempt with Graph and the database mocked; returns (result, push_mock, evidence writes).

    Every evidence writer (native upsert, Salla upsert, stale-observation
    repair) is recorded in *writes* as (writer, kwargs)."""
    from contextlib import ExitStack

    from services.salla_variant_catalog_identity import SallaVariantIdentity

    writes = []

    def _recorder(name, result=None):
        return lambda db, **kw: writes.append((name, kw)) or dict(result or {"ok": True})

    with ExitStack() as stack:
        mocks = {name.rsplit(".", 1)[-1]: stack.enter_context(patch(name)) for name in _ORCH_PATCHES}
        stack.enter_context(patch("services.native_meta_sync_orchestrator._stamp_with_lease",
                                  side_effect=lambda db, p, lease, fn: (fn(p) or True)))
        stack.enter_context(patch("services.native_meta_sync_orchestrator._collect_retailer_ids",
                                  return_value=["nahla_p_501"]))
        stack.enter_context(patch("services.native_meta_sync_orchestrator.claim_active_meta_item_binding"))
        stack.enter_context(patch("services.native_meta_sync_orchestrator.load_variant_for_push",
                                  return_value=(parent, SimpleNamespace(id=5011))))
        stack.enter_context(patch("services.salla_variant_catalog_identity.upsert_native_publication_membership",
                                  side_effect=_recorder("native", upsert_result)))
        stack.enter_context(patch("services.salla_variant_catalog_identity.upsert_variant_membership",
                                  side_effect=_recorder("salla", upsert_result)))
        stack.enter_context(patch("services.salla_variant_catalog_identity.replace_stale_observation_after_create",
                                  side_effect=_recorder("repair")))
        if salla:
            ident = SallaVariantIdentity(product_id=501, variant_id=5011, salla_variant_id="881",
                                         retailer_id="nahla_p_501", is_default=False)
            stack.enter_context(patch("services.salla_variant_catalog_identity.identity_for_retailer_id",
                                      return_value=ident))
            stack.enter_context(patch("services.salla_variant_catalog_identity.ensure_variant_membership_slot",
                                      return_value={"ok": True, "created": False, "meta_item_id": ""}))
        if catalogs:
            # catalogs = (before the POST, after the POST)
            push_mock = mocks["push_one_meta_catalog_item"]
            mocks["_resolve_connection"].side_effect = lambda *a, **k: _conn(
                meta_catalog_id=catalogs[1] if push_mock.called else catalogs[0])
        else:
            mocks["_resolve_connection"].return_value = _conn()
        mocks["preview_native_meta_sync"].return_value = {"eligible": True, "fatal_errors": [], "retailer_id": "nahla_p_501"}
        mocks["ensure_native_default_variant"].return_value = (SimpleNamespace(retailer_id="nahla_p_501"), False)
        mocks["push_one_meta_catalog_item"].return_value = push_result
        mocks["find_meta_catalog_item_by_retailer_id"].return_value = (lookup_id, lookup_meta if lookup_meta is not None else {
            "matched": True, "item": {"id": lookup_id, "retailer_id": "nahla_p_501", "price": 32000, "currency": "SAR",
                                      "availability": "in stock"}})
        mocks["get_waba_catalog_link_status"].return_value = {"ok": True, "expected_catalog_linked": True}
        mocks["_try_acquire_sync_lock"].return_value = parent
        result = attempt_native_meta_sync(MagicMock(), 9, 501)
        return result, mocks["push_one_meta_catalog_item"], writes


_CREATED = {"ok": True, "action": "create", "meta_product_id": "META-501",
            "payload": {"price": 32000, "currency": "SAR", "availability": "in stock"},
            "meta": {"http_status": 200, "response": {"id": "META-501"}}, "lookup": {}}


def test_verified_create_records_evidence_for_the_created_item():
    result, _push, writes = _run_orchestrator(_generic_native_parent(), push_result=_CREATED, lookup_id="META-501")
    assert result["ok"] is True
    assert [(name, w["retailer_id"], w["meta_item_id"]) for name, w in writes] == [("native", "nahla_p_501", "META-501")]


def test_lookup_finding_another_item_after_the_post_records_no_evidence():
    parent = _generic_native_parent()
    result, _push, writes = _run_orchestrator(parent, push_result=_CREATED, lookup_id="META-SOMEONE-ELSE")
    assert writes == []
    assert result["ok"] is False and result["error_code"] == "verification_failed"
    assert parent.meta_item_id is None


def test_a_post_without_an_item_id_records_no_evidence():
    no_id = {**_CREATED, "meta_product_id": None}
    result, _push, writes = _run_orchestrator(_generic_native_parent(), push_result=no_id, lookup_id="META-501")
    assert writes == [] and result["ok"] is False


def test_a_failed_post_records_no_evidence():
    failed = {"ok": False, "action": "create", "error": "meta_http_error", "meta": {"http_status": 400, "response": {}},
              "payload": {}, "lookup": {}}
    result, _push, writes = _run_orchestrator(_generic_native_parent(), push_result=failed, lookup_id="META-501")
    assert writes == [] and result["ok"] is False


def test_lookup_only_verification_never_writes_evidence():
    payload = {"price": 32000, "currency": "SAR", "availability": "in stock"}
    parent = _generic_native_parent()
    parent.extra_metadata["sync_meta"].update({
        "dirty": False, "content_generation": 3, "expected_content_generation": 3,
        "expected_payloads_by_retailer_id": {"nahla_p_501": payload},
    })
    result, push, writes = _run_orchestrator(parent, push_result=_CREATED, lookup_id="META-501")
    push.assert_not_called()                          # nothing was POSTed in this attempt
    assert writes == []
    assert result["ok"] is True                       # the verification itself still succeeds


_UPDATED = {**_CREATED, "action": "update"}


@pytest.mark.parametrize("salla", [False, True])
@pytest.mark.parametrize("push_result, lookup_id, lookup_meta, label", [
    (_CREATED, "META-SOMEONE-ELSE", None, "create, lookup returns another item"),
    (_CREATED, "META-OLD-STALE", None, "create, stale lookup returns the previous item"),
    (_UPDATED, "META-502", None, "update, item replaced between POST and lookup"),
    (_CREATED, None, {"matched": False}, "create, lookup returns no row (null id)"),
    (_CREATED, None, {"matched": False, "error": "missing_graph_id"}, "create, lookup row without an id"),
    (_CREATED, None, {"matched": False, "error": "ambiguous_graph_rows"}, "create, two Graph rows"),
    ({**_CREATED, "meta_product_id": None}, "META-501", None, "create response without an id"),
])
def test_uncorroborated_publication_never_records_evidence(salla, push_result, lookup_id, lookup_meta, label):
    parent = _generic_native_parent(**({"source": "salla", "external_id": "910600"} if salla else {}))
    result, _push, writes = _run_orchestrator(parent, push_result=push_result, lookup_id=lookup_id,
                                              lookup_meta=lookup_meta, salla=salla)
    assert writes == [], label
    assert result["ok"] is False, label


@pytest.mark.parametrize("salla", [False, True])
def test_corroborated_publication_records_evidence_for_native_and_salla(salla):
    parent = _generic_native_parent(**({"source": "salla", "external_id": "910600"} if salla else {}))
    for push_result in (_CREATED, _UPDATED):
        result, _push, writes = _run_orchestrator(parent, push_result=push_result, lookup_id="META-501", salla=salla)
        assert [(name, w["meta_item_id"]) for name, w in writes] == [("salla" if salla else "native", "META-501")]
        assert result["ok"] is True


@pytest.mark.parametrize("salla", [False, True])
def test_stale_observation_repair_runs_only_after_a_corroborated_create(salla):
    parent = _generic_native_parent(**({"source": "salla", "external_id": "910600"} if salla else {}))
    immutable = {"ok": False, "error": "ambiguous_variant_identity", "reason": "meta_item_id_immutable"}
    result, _push, writes = _run_orchestrator(parent, push_result=_CREATED, lookup_id="META-501", salla=salla,
                                              upsert_result=immutable)
    repair = [kw for name, kw in writes if name == "repair"]
    assert len(repair) == 1 and result["ok"] is True
    assert repair[0]["created_meta_item_id"] == repair[0]["corroborated_meta_item_id"] == "META-501"
    # an update hitting the same refusal never repairs: it fails closed
    result, _push, writes = _run_orchestrator(parent, push_result=_UPDATED, lookup_id="META-501", salla=salla,
                                              upsert_result=immutable)
    assert [name for name, _kw in writes if name == "repair"] == [] and result["ok"] is False


def test_salla_evidence_is_refused_when_the_catalog_changed_during_the_attempt():
    parent = _generic_native_parent(source="salla", external_id="910600")
    result, _push, writes = _run_orchestrator(parent, push_result=_CREATED, lookup_id="META-501", salla=True,
                                              catalogs=["CAT-GENERIC-001", "CAT-MOVED"])
    assert writes == [] and result["ok"] is False and result["error_code"] == "verification_failed"



@pytest.mark.parametrize("action", ["link_canonical_sibling", "skip_existing"])
def test_a_linked_or_existing_item_records_no_attempt_and_no_evidence(action):
    parent = _generic_native_parent()
    linked = {"ok": True, "action": action, "meta_product_id": "META-SIBLING", "catalog_id": "CAT-GENERIC-001",
              "payload": {}, "meta": {"http_status": None},
              "lookup": {"identity_class": "EXISTING_CANONICAL_SIBLING", "sibling_retailer_id": "nahla_p_500",
                         "reason": "canonical_sibling"}}
    _result, _push, writes = _run_orchestrator(parent, push_result=linked, lookup_id="META-SIBLING")
    assert writes == []
    assert "pending_publications" not in parent.extra_metadata["sync_meta"]
