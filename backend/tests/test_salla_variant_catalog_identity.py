"""Salla catalog identity: one Meta item per sellable variant SKU."""
from __future__ import annotations

from types import SimpleNamespace

from unittest.mock import MagicMock, patch

from services.salla_variant_catalog_identity import (
    CLASS_AMBIGUOUS,
    CLASS_UNBOUND_PARENT,
    CLASS_UNBOUND_VARIANT,
    ERROR_AMBIGUOUS_VARIANT_IDENTITY,
    AmbiguousVariantIdentity,
    classify_graph_retailer_id,
    collect_push_retailer_ids,
    deterministic_variant_retailer_id,
    ensure_variant_membership_slot,
    identity_for_retailer_id,
    literal_bind_plan,
    sellable_salla_identities,
    upsert_variant_membership,
)
from services.native_meta_sync_orchestrator import attempt_native_meta_sync


def _parent(**kw):
    data = dict(id=21, tenant_id=1, source="salla", external_id="863278879")
    data.update(kw)
    return SimpleNamespace(**data)


def _variant(**kw):
    data = dict(
        id=1,
        product_id=21,
        tenant_id=1,
        salla_variant_id=None,
        retailer_id="",
        is_default=False,
    )
    data.update(kw)
    return SimpleNamespace(**data)


class _Query:
    def __init__(self, rows, pred=None):
        self._rows = rows
        self._pred = pred or (lambda _row: True)

    def filter(self, *clauses):
        wanted = {}
        for clause in clauses:
            key = getattr(getattr(clause, "left", None), "key", None)
            right = getattr(clause, "right", None)
            value = getattr(right, "value", None)
            if key is not None and value is not None:
                wanted[key] = value

        def pred(row):
            if not self._pred(row):
                return False
            for key, value in wanted.items():
                if str(getattr(row, key, None) or "") != str(value):
                    return False
            return True

        return _Query(self._rows, pred)

    def all(self):
        return [row for row in self._rows if self._pred(row)]

    def first(self):
        rows = self.all()
        return rows[0] if rows else None


class _Db:
    def __init__(self, rows, memberships=None):
        self.rows = rows
        self.memberships = list(memberships or [])
        self.added = []

    def query(self, model):
        name = getattr(model, "__name__", str(model))
        if "Membership" in name:
            return _Query(self.memberships)
        return _Query(self.rows)

    def add(self, row):
        self.added.append(row)
        self.memberships.append(row)


def test_five_sizes_are_five_identities_not_parent():
    parent = _parent()
    default = _variant(id=34, retailer_id="863278879", is_default=True)
    sizes = [
        _variant(id=200 + i, salla_variant_id=str(100 + i), retailer_id=f"863278879-{100 + i}")
        for i in range(5)
    ]
    ids = sellable_salla_identities(parent, [default, *sizes])
    assert len(ids) == 5
    assert {item.retailer_id for item in ids} == {f"863278879-{100 + i}" for i in range(5)}
    assert "863278879" not in {item.retailer_id for item in ids}


def test_default_stub_without_salla_variant_id_is_not_sellable():
    parent = _parent()
    default = _variant(id=34, retailer_id="863278879", is_default=True)
    assert sellable_salla_identities(parent, [default]) == []
    db = _Db([default])
    try:
        collect_push_retailer_ids(db, parent, "863278879")
        assert False, "expected AmbiguousVariantIdentity"
    except AmbiguousVariantIdentity as exc:
        assert exc.reason == ERROR_AMBIGUOUS_VARIANT_IDENTITY


def test_missing_salla_variant_id_never_uses_bare_external_id():
    parent = _parent()
    row = _variant(id=9, salla_variant_id="", retailer_id="863278879", is_default=True)
    db = _Db([row])
    try:
        collect_push_retailer_ids(db, parent, "863278879")
    except AmbiguousVariantIdentity:
        return
    assert False, "bare parent fallback must not become a CREATE identity"


def test_native_product_still_uses_variant_retailer_ids():
    parent = _parent(source="manual", external_id=None)
    row = _variant(id=1, retailer_id="nahla_p_176", is_default=True)
    db = _Db([row])
    assert collect_push_retailer_ids(db, parent, "nahla_p_176") == ["nahla_p_176"]


def test_identity_lookup_is_deterministic():
    parent = _parent()
    v = _variant(id=217, salla_variant_id="845296417", retailer_id="863278879-845296417")
    found = identity_for_retailer_id(parent, [v], "863278879-845296417")
    assert found is not None
    assert found.variant_id == 217
    assert found.retailer_id == deterministic_variant_retailer_id("863278879", "845296417")
    assert identity_for_retailer_id(parent, [v], "863278879") is None


def test_classify_parent_and_variant_and_ambiguous():
    parent = _parent()
    v = _variant(id=217, salla_variant_id="845296417")
    assert classify_graph_retailer_id(
        retailer_id="863278879",
        external_id=parent.external_id,
        variants=[v],
    ) == CLASS_UNBOUND_PARENT
    assert classify_graph_retailer_id(
        retailer_id="863278879-845296417",
        external_id=parent.external_id,
        variants=[v],
    ) == CLASS_UNBOUND_VARIANT
    assert classify_graph_retailer_id(
        retailer_id="863278879-999",
        external_id=parent.external_id,
        variants=[v],
    ) == CLASS_AMBIGUOUS


def test_upsert_does_not_change_existing_meta_item_id():
    existing = SimpleNamespace(
        tenant_id=1,
        catalog_id="cat",
        retailer_id="863278879-845296417",
        product_id=21,
        variant_id=217,
        salla_variant_id="845296417",
        meta_item_id="meta-a",
        provenance="x",
        verified_at=None,
    )
    parent = _parent()
    v = _variant(id=217, salla_variant_id="845296417")
    ident = identity_for_retailer_id(parent, [v], "863278879-845296417")
    db = _Db([v], memberships=[existing])
    result = upsert_variant_membership(
        db,
        tenant_id=1,
        catalog_id="cat",
        identity=ident,
        meta_item_id="meta-b",
    )
    assert result["ok"] is False
    assert result["reason"] == "meta_item_id_immutable"
    assert existing.meta_item_id == "meta-a"


def test_price_currency_availability_image_are_variant_scoped():
    """Regression lock: five SKUs must not collapse to the parent payload."""
    parent = _parent()
    sizes = [
        _variant(
            id=10 + i,
            salla_variant_id=str(i + 1),
            retailer_id=f"863278879-{i + 1}",
            price=str(100 + i),
            currency="SAR",
            in_stock=True,
            image_url=f"https://cdn.example/{i}.jpg",
        )
        for i in range(5)
    ]
    rids = [item.retailer_id for item in sellable_salla_identities(parent, sizes)]
    assert len(set(rids)) == 5
    assert all(rid != parent.external_id for rid in rids)
    plans = [
        literal_bind_plan(
            graph_item={
                "id": f"meta-{i}",
                "retailer_id": rid,
                "price": (100 + i) * 100,
                "currency": "SAR",
                "availability": "in stock",
                "image_url": f"https://cdn.example/{i}.jpg",
            },
            product=parent,
            variants=sizes,
        )
        for i, rid in enumerate(rids)
    ]
    assert all(p["would_bind"] for p in plans)
    assert len({p["salla_variant_id"] for p in plans}) == 5


def test_literal_bind_never_matches_by_name():
    parent = _parent()
    v = _variant(
        id=217,
        salla_variant_id="845296417",
        retailer_id="863278879-845296417",
        price="199",
        currency="SAR",
        in_stock=True,
        image_url="https://cdn.example/a.jpg",
    )
    plan = literal_bind_plan(
        graph_item={
            "id": "g1",
            "retailer_id": "other-sku",
            "name": "فستان أحمر",
            "price": "199",
            "currency": "SAR",
            "availability": "in stock",
            "image_url": "https://cdn.example/a.jpg",
        },
        product=parent,
        variants=[v],
    )
    assert plan["class"] == CLASS_AMBIGUOUS
    assert plan["would_bind"] is False
    assert plan["quarantine"] is True
    assert plan["name_match_used"] is False


def test_parent_graph_item_is_quarantined_not_bound():
    parent = _parent()
    default = _variant(id=34, retailer_id="863278879", is_default=True)
    v = _variant(id=217, salla_variant_id="845296417", retailer_id="863278879-845296417")
    plan = literal_bind_plan(
        graph_item={"id": "g-parent", "retailer_id": "863278879", "name": parent.external_id},
        product=parent,
        variants=[default, v],
    )
    assert plan["class"] == CLASS_UNBOUND_PARENT
    assert plan["would_bind"] is False
    assert plan["quarantine"] is True


def _sync_db(variants):
    db = _Db(variants)

    def _query(model):
        name = getattr(model, "__name__", str(model))
        if "Membership" in name:
            return _Query(db.memberships)
        if "Variant" in name:
            return _Query(db.rows)
        q = MagicMock()
        filtered = MagicMock()
        filtered.first.return_value = None
        filtered.all.return_value = []
        filtered.with_for_update.return_value.populate_existing.return_value.first.return_value = None
        q.filter.return_value = filtered
        return q

    db.query = _query
    db.flush = lambda: None
    db.commit = lambda: None
    db.refresh = lambda *a, **k: None
    db.rollback = lambda: None
    db.execute = MagicMock()
    return db


def _salla_sync_parent():
    return SimpleNamespace(
        id=21,
        tenant_id=1,
        source="salla",
        external_id="863278879",
        title="فستان",
        extra_metadata={},
        sync_status="syncing",
        sync_error=None,
        last_synced_at=None,
        meta_item_id=None,
        catalog_status="active",
        ownership_mode="external_managed",
    )


def _matched_lookup(rid, meta_id):
    return (
        meta_id,
        {
            "matched": True,
            "item": {
                "id": meta_id,
                "retailer_id": rid,
                "price": 19900,
                "currency": "SAR",
                "availability": "in stock",
            },
        },
    )


@patch("services.native_meta_sync_orchestrator._resolve_connection")
@patch("services.native_meta_sync_orchestrator._collect_retailer_ids")
@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status")
@patch("services.native_meta_sync_orchestrator.find_meta_catalog_item_by_retailer_id")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_five_salla_sizes_create_five_identities_and_leave_parent_unbound(
    preview_mock,
    ensure_mock,
    push_mock,
    lookup_mock,
    waba_mock,
    lock_mock,
    collect_mock,
    conn_mock,
):
    parent = _salla_sync_parent()
    sizes = [
        _variant(id=200 + i, salla_variant_id=str(100 + i), retailer_id=f"863278879-{100 + i}")
        for i in range(5)
    ]
    rids = [v.retailer_id for v in sizes]
    db = _sync_db(sizes)
    lock_mock.return_value = parent
    preview_mock.return_value = {"eligible": True, "retailer_id": rids[0], "fatal_errors": []}
    ensure_mock.return_value = (sizes[0], False)
    collect_mock.return_value = rids
    conn_mock.return_value = SimpleNamespace(meta_catalog_id="cat-new", catalog_enabled=True)
    waba_mock.return_value = {"ok": True, "expected_catalog_linked": True}

    def _push(_db, _tid, retailer_id, **_kwargs):
        return {
            "ok": True,
            "action": "create",
            "meta_product_id": f"meta-{retailer_id}",
            "payload": {"price": 19900, "currency": "SAR", "availability": "in stock"},
        }

    push_mock.side_effect = _push
    lookup_mock.side_effect = lambda _conn, _cat, rid, **kw: _matched_lookup(rid, f"meta-{rid}")
    result = attempt_native_meta_sync(db, 1, 21)
    assert result.get("ok") is True, result
    assert parent.meta_item_id in (None, "")
    assert len(db.added) == 5
    assert {getattr(row, "retailer_id", None) for row in db.added} == set(rids)
    assert push_mock.call_count == 5
    assert all(call.args[2] != "863278879" for call in push_mock.call_args_list)


@patch("services.native_meta_sync_orchestrator._resolve_connection")
@patch("services.native_meta_sync_orchestrator._collect_retailer_ids")
@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status")
@patch("services.native_meta_sync_orchestrator.find_meta_catalog_item_by_retailer_id")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_retry_is_idempotent_and_does_not_change_meta_item_id(
    preview_mock,
    ensure_mock,
    push_mock,
    lookup_mock,
    waba_mock,
    lock_mock,
    collect_mock,
    conn_mock,
):
    parent = _salla_sync_parent()
    v = _variant(id=217, salla_variant_id="845296417", retailer_id="863278879-845296417")
    db = _sync_db([v])
    lock_mock.return_value = parent
    preview_mock.return_value = {"eligible": True, "retailer_id": v.retailer_id, "fatal_errors": []}
    ensure_mock.return_value = (v, False)
    collect_mock.return_value = [v.retailer_id]
    conn_mock.return_value = SimpleNamespace(meta_catalog_id="cat-new", catalog_enabled=True)
    waba_mock.return_value = {"ok": True, "expected_catalog_linked": True}
    push_mock.return_value = {
        "ok": True,
        "action": "create",
        "meta_product_id": "meta-a",
        "payload": {"price": 19900, "currency": "SAR", "availability": "in stock"},
    }
    lookup_mock.return_value = _matched_lookup(v.retailer_id, "meta-a")
    first = attempt_native_meta_sync(db, 1, 21)
    assert first["ok"] is True
    push_mock.return_value = {
        "ok": True,
        "action": "update",
        "meta_product_id": "meta-a",
        "payload": {"price": 19900, "currency": "SAR", "availability": "in stock"},
    }
    second = attempt_native_meta_sync(db, 1, 21)
    assert second["ok"] is True
    assert len(db.added) == 1
    bound = db.memberships[0]
    assert bound.meta_item_id == "meta-a"


@patch("services.native_meta_sync_orchestrator._resolve_connection")
@patch("services.native_meta_sync_orchestrator._collect_retailer_ids")
@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status")
@patch("services.native_meta_sync_orchestrator.find_meta_catalog_item_by_retailer_id")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_partial_variant_failure_keeps_successful_memberships(
    preview_mock,
    ensure_mock,
    push_mock,
    lookup_mock,
    waba_mock,
    lock_mock,
    collect_mock,
    conn_mock,
):
    parent = _salla_sync_parent()
    sizes = [
        _variant(id=200 + i, salla_variant_id=str(100 + i), retailer_id=f"863278879-{100 + i}")
        for i in range(3)
    ]
    rids = [v.retailer_id for v in sizes]
    db = _sync_db(sizes)
    lock_mock.return_value = parent
    preview_mock.return_value = {"eligible": True, "retailer_id": rids[0], "fatal_errors": []}
    ensure_mock.return_value = (sizes[0], False)
    collect_mock.return_value = rids
    conn_mock.return_value = SimpleNamespace(meta_catalog_id="cat-new", catalog_enabled=True)
    waba_mock.return_value = {"ok": True, "expected_catalog_linked": True}

    def _push(_db, _tid, retailer_id, **_kwargs):
        if retailer_id.endswith("-102"):
            return {"ok": False, "error": "meta_http_error"}
        return {
            "ok": True,
            "action": "create",
            "meta_product_id": f"meta-{retailer_id}",
            "payload": {"price": 19900, "currency": "SAR", "availability": "in stock"},
        }

    push_mock.side_effect = _push
    lookup_mock.side_effect = lambda _conn, _cat, rid, **kw: _matched_lookup(rid, f"meta-{rid}")
    result = attempt_native_meta_sync(db, 1, 21)
    assert result["ok"] is False
    assert len(db.added) == 3
    stamped = [row for row in db.added if getattr(row, "meta_item_id", None)]
    assert len(stamped) == 2
    assert {row.retailer_id for row in stamped} == {rids[0], rids[1]}


@patch("services.native_meta_sync_orchestrator._resolve_connection")
@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_salla_missing_variant_id_blocks_without_create(
    preview_mock,
    ensure_mock,
    push_mock,
    lock_mock,
    conn_mock,
):
    parent = _salla_sync_parent()
    default = _variant(id=34, retailer_id="863278879", is_default=True)
    db = _sync_db([default])
    lock_mock.return_value = parent
    conn_mock.return_value = SimpleNamespace(meta_catalog_id="cat-new", catalog_enabled=True)
    preview_mock.return_value = {"eligible": True, "retailer_id": "863278879", "fatal_errors": []}
    ensure_mock.return_value = (default, False)
    result = attempt_native_meta_sync(db, 1, 21)
    assert result["ok"] is False
    assert result["error_code"] == ERROR_AMBIGUOUS_VARIANT_IDENTITY
    push_mock.assert_not_called()
    assert db.added == []


def test_duplicate_normalized_svid_is_blocked():
    parent = _parent()
    a = _variant(id=1, salla_variant_id="845296417", retailer_id="863278879-845296417")
    b = _variant(id=2, salla_variant_id="845296417", retailer_id="863278879-845296417")
    try:
        sellable_salla_identities(parent, [a, b])
        assert False, "duplicate svid must not be discarded"
    except AmbiguousVariantIdentity as exc:
        assert exc.reason == ERROR_AMBIGUOUS_VARIANT_IDENTITY


def test_membership_local_identity_is_immutable():
    existing = SimpleNamespace(
        tenant_id=1,
        catalog_id="cat",
        retailer_id="863278879-845296417",
        product_id=21,
        variant_id=217,
        salla_variant_id="845296417",
        meta_item_id="meta-a",
        provenance="x",
        verified_at=None,
    )
    parent = _parent()
    other = _variant(id=999, salla_variant_id="845296417")
    ident = identity_for_retailer_id(parent, [other], "863278879-845296417")
    db = _Db([other], memberships=[existing])
    result = upsert_variant_membership(
        db,
        tenant_id=1,
        catalog_id="cat",
        identity=ident,
        meta_item_id="meta-a",
    )
    assert result["ok"] is False
    assert result["reason"] == "variant_id_immutable"
    assert existing.variant_id == 217


def test_graph_minor_units_match_local_major_price():
    parent = _parent()
    v = _variant(
        id=217,
        salla_variant_id="845296417",
        retailer_id="863278879-845296417",
        price="199",
        currency="SAR",
        in_stock=True,
        image_url="https://cdn.example/a.jpg",
    )
    plan = literal_bind_plan(
        graph_item={
            "id": "g1",
            "retailer_id": "863278879-845296417",
            "price": 19900,
            "currency": "SAR",
            "availability": "in stock",
            "image_url": "https://cdn.example/a.jpg",
        },
        product=parent,
        variants=[v],
    )
    assert plan["content_exact"] is True
    assert plan["would_bind"] is True
    assert plan["quarantine"] is False


@patch("services.native_meta_sync_orchestrator._resolve_connection")
@patch("services.native_meta_sync_orchestrator._collect_retailer_ids")
@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status")
@patch("services.native_meta_sync_orchestrator.find_meta_catalog_item_by_retailer_id")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_single_salla_variant_leaves_parent_unbound_across_catalog_switch(
    preview_mock,
    ensure_mock,
    push_mock,
    lookup_mock,
    waba_mock,
    lock_mock,
    collect_mock,
    conn_mock,
):
    parent = _salla_sync_parent()
    parent.meta_item_id = "OLD-CATALOG-ITEM"
    v = _variant(id=217, salla_variant_id="845296417", retailer_id="863278879-845296417")
    db = _sync_db([v])
    lock_mock.return_value = parent
    preview_mock.return_value = {"eligible": True, "retailer_id": v.retailer_id, "fatal_errors": []}
    ensure_mock.return_value = (v, False)
    collect_mock.return_value = [v.retailer_id]
    conn_mock.return_value = SimpleNamespace(meta_catalog_id="cat-new", catalog_enabled=True)
    waba_mock.return_value = {"ok": True, "expected_catalog_linked": True}
    push_mock.return_value = {
        "ok": True,
        "action": "create",
        "meta_product_id": "meta-new",
        "payload": {"price": 19900, "currency": "SAR", "availability": "in stock"},
    }
    lookup_mock.return_value = _matched_lookup(v.retailer_id, "meta-new")
    result = attempt_native_meta_sync(db, 1, 21)
    assert result.get("ok") is True, result
    assert parent.meta_item_id in (None, "")
    assert db.memberships[0].meta_item_id == "meta-new"
    assert db.memberships[0].catalog_id == "cat-new"


@patch("services.native_meta_sync_orchestrator._resolve_connection")
@patch("services.native_meta_sync_orchestrator._collect_retailer_ids")
@patch("services.native_meta_sync_orchestrator._try_acquire_sync_lock")
@patch("services.native_meta_sync_orchestrator.get_waba_catalog_link_status")
@patch("services.native_meta_sync_orchestrator.find_meta_catalog_item_by_retailer_id")
@patch("services.native_meta_sync_orchestrator.push_one_meta_catalog_item")
@patch("services.meta_catalog_sync_confirm.ensure_native_default_variant")
@patch("services.native_meta_sync_orchestrator.preview_native_meta_sync")
def test_membership_commit_failure_after_graph_create_retries_by_retailer_id(
    preview_mock,
    ensure_mock,
    push_mock,
    lookup_mock,
    waba_mock,
    lock_mock,
    collect_mock,
    conn_mock,
):
    from sqlalchemy.exc import SQLAlchemyError

    parent = _salla_sync_parent()
    v = _variant(id=217, salla_variant_id="845296417", retailer_id="863278879-845296417")
    db = _sync_db([v])
    lock_mock.return_value = parent
    preview_mock.return_value = {"eligible": True, "retailer_id": v.retailer_id, "fatal_errors": []}
    ensure_mock.return_value = (v, False)
    collect_mock.return_value = [v.retailer_id]
    conn_mock.return_value = SimpleNamespace(meta_catalog_id="cat-new", catalog_enabled=True)
    waba_mock.return_value = {"ok": True, "expected_catalog_linked": True}
    push_mock.return_value = {
        "ok": True,
        "action": "create",
        "meta_product_id": "meta-a",
        "payload": {"price": 19900, "currency": "SAR", "availability": "in stock"},
    }
    lookup_mock.return_value = _matched_lookup(v.retailer_id, "meta-a")
    commits = {"n": 0}

    def _commit():
        commits["n"] += 1
        if commits["n"] == 2:
            raise SQLAlchemyError("injected membership stamp failure")

    db.commit = _commit
    first = attempt_native_meta_sync(db, 1, 21)
    assert first["ok"] is False
    assert first["error_code"] == ERROR_AMBIGUOUS_VARIANT_IDENTITY
    assert "membership_commit_failed" in str(parent.sync_error or "")
    assert push_mock.call_count == 1
    db.commit = lambda: None
    parent.sync_status = "pending"
    second = attempt_native_meta_sync(db, 1, 21)
    assert second["ok"] is True
    assert db.memberships[0].meta_item_id == "meta-a"


def test_two_workers_cannot_rebind_same_slot_to_other_variant():
    parent = _parent()
    first = _variant(id=217, salla_variant_id="845296417")
    ident = identity_for_retailer_id(parent, [first], "863278879-845296417")
    db = _Db([first])
    slot = ensure_variant_membership_slot(
        db, tenant_id=1, catalog_id="cat", identity=ident,
    )
    assert slot["ok"] is True
    other_parent = _parent(id=99)
    other = _variant(id=1, product_id=99, salla_variant_id="845296417")
    other_ident = identity_for_retailer_id(other_parent, [other], "863278879-845296417")
    raced = ensure_variant_membership_slot(
        db, tenant_id=1, catalog_id="cat", identity=other_ident,
    )
    assert raced["ok"] is False
    assert raced["reason"] == "product_id_immutable"
