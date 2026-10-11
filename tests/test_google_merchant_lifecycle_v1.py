"""tests/test_google_merchant_lifecycle_v1.py
───────────────────────────────────────────
Fail-closed Merchant API v1 lifecycle planning and fake-only simulation.

Offers come from the real offline mapper for a neutral generic merchant
(``متجر تجريبي عام``: perfume single SKU, apparel variants) and the honey
store shape. Account / data source ids are fake test values. Sockets and
sleeping are disabled for every test: planning and simulation must do no
I/O and must not wait. Assertions target actions, reasons, request shapes,
call counts and state — never prose.
"""
from __future__ import annotations

import dataclasses
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from services.google_merchant_lifecycle_v1 import (  # noqa: E402
    ACTION_BLOCKED,
    ACTION_DELETE,
    ACTION_INSERT,
    ACTION_NOOP,
    ACTION_PATCH,
    OBS_ABSENT,
    OBS_PRESENT_FOREIGN_SOURCE,
    OBS_PRESENT_IN_SOURCE,
    ORIGIN_PRODUCTS_LIST,
    OUTCOME_CONFIRMED,
    OUTCOME_GAVE_UP,
    OUTCOME_PERMANENT_FAILURE,
    OUTCOME_REFUSED,
    OUTCOME_UNRESOLVED,
    SIMULATION_ACK,
    CatalogSnapshotProof,
    FakeMerchantTransport,
    FreshnessPolicy,
    OfferScope,
    OwnedOfferEvidence,
    PendingWrite,
    PlannedAction,
    RemoteObservation,
    SimulationNotPermitted,
    SimulationOptIn,
    action_snapshot,
    evidence_after_apply,
    observation_absent_from_get,
    plan_lifecycle,
    scope_for,
    simulate_plan,
)
from services.google_merchant_products_v1 import (  # noqa: E402
    GoogleMerchantTarget,
    OfferPreview,
    map_product,
    product_input_encoded_id,
)
from services.google_merchant_protocol_v1 import (  # noqa: E402
    PUBLICATION_APPROVED,
    PUBLICATION_UNKNOWN,
    MerchantApiBinding,
    RetryPolicy,
    TransportResponse,
)

NOW = 1_800_000_000
TENANT = 7
ACCOUNT = "1111111111"
SOURCE = "2222222222"
OTHER_ACCOUNT = "4444444444"
OTHER_SOURCE = "3333333333"
TARGET = GoogleMerchantTarget(tenant_id=TENANT, content_language="ar", feed_label="SA")
FRESH = FreshnessPolicy(binding_max_age_s=3600, ownership_max_age_s=86400, observation_max_age_s=600)
POLICY = RetryPolicy(max_attempts=3, base_delay_s=2, max_delay_s=60)
OPT_IN = SimulationOptIn(SIMULATION_ACK)


@pytest.fixture(autouse=True)
def _no_network_no_sleep(monkeypatch):
    def _refuse(*_a, **_k):
        raise AssertionError("network or sleep attempted")
    monkeypatch.setattr(socket, "socket", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(time, "sleep", _refuse)


# ─────────────────────────────────────────────────────────────────────
# Rows → real mapper offers
# ─────────────────────────────────────────────────────────────────────


def _product(**kw):
    meta = {
        "status": "active", "currency": "SAR",
        "image_url": "https://generic-shop.test/img/p.jpg",
        "product_url": "https://generic-shop.test/p",
        "brand": "علامة تجريبية",
    }
    meta.update(kw.pop("meta", {}))
    base = dict(
        id=1, tenant_id=TENANT, source="salla", external_id="PRF-100",
        title="عطر ورد 100ml", description="وصف المنتج من المتجر",
        price="249.50", in_stock=True, stock_quantity=None,
        catalog_status="active", merchant_hidden_at=None, archived_at=None,
        ownership_mode=None, has_variants=False, extra_metadata=meta,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _variant(**kw):
    base = dict(
        id=10, tenant_id=TENANT, product_id=2, salla_variant_id=None, sku=None,
        retailer_id=None, price="120", currency="SAR", stock_quantity=None,
        in_stock=True, options=None, option_summary=None, image_url=None,
        is_default=False, extra_metadata=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _perfume(**kw):
    return map_product(TARGET, _product(**kw), []).offers[0]


def _shirt_offers():
    p = _product(id=2, external_id="5550", title="قميص قطني أزرق", price="120")
    rows = [
        _variant(id=21, salla_variant_id="901", options={"size": "M", "color": "Blue"}),
        _variant(id=22, salla_variant_id="902", options={"size": "L", "color": "Blue"}),
    ]
    return map_product(TARGET, p, rows).offers


def _honey():
    return map_product(TARGET, _product(id=3, external_id="7001", title="عسل سدر 500 جرام", price="85"), []).offers[0]


def _binding(**kw):
    base = dict(tenant_id=TENANT, account_id=ACCOUNT, data_source_id=SOURCE, data_source_input="API",
                content_language="ar", feed_label="SA", observed_at=NOW - 60)
    base.update(kw)
    return MerchantApiBinding(**base)


def _scope(offer_id, **kw):
    base = dict(tenant_id=TENANT, account_id=ACCOUNT, data_source_id=SOURCE,
                content_language="ar", feed_label="SA", offer_id=offer_id)
    base.update(kw)
    return OfferScope(**base)


def _absent(offer_id, at=NOW - 30):
    return observation_absent_from_get(_binding(), offer_id, at)


def _present(offer_id, state=OBS_PRESENT_IN_SOURCE, at=NOW - 30, **scope_kw):
    return RemoteObservation(_scope(offer_id, **scope_kw), state, "products.get", at)


def _owned(offer, attrs=None, at=NOW - 300, **scope_kw):
    attrs = offer.product_input["productAttributes"] if attrs is None else attrs
    return OwnedOfferEvidence(_scope(offer.offer_id, **scope_kw), dict(attrs), at)


def _plan(offers, **kw):
    binding = kw.pop("binding", _binding())
    return plan_lifecycle(TARGET, binding, offers, now=kw.pop("now", NOW), freshness=FRESH, **kw)


def _only(plan):
    assert len(plan.actions) == 1
    return plan.actions[0]


def _rid(offer_id):
    return product_input_encoded_id("ar", "SA", offer_id)


def _paths(offer_id):
    return (
        f"/products/v1/accounts/{ACCOUNT}/productInputs:insert",
        f"/products/v1/accounts/{ACCOUNT}/productInputs/{_rid(offer_id)}",
        f"/products/v1/accounts/{ACCOUNT}/products/{_rid(offer_id)}",
    )


def _readback(offer, attrs=None, data_source=None, status="approved"):
    statuses = {
        "approved": {"destinationStatuses": [{"reportingContext": "SHOPPING_ADS", "approvedCountries": ["SA"]}]},
        "pending": {"destinationStatuses": [{"reportingContext": "SHOPPING_ADS", "pendingCountries": ["SA"]}]},
    }
    body = {
        "name": f"accounts/{ACCOUNT}/products/ar~SA~{offer.offer_id}",
        "base64EncodedName": f"accounts/{ACCOUNT}/products/{_rid(offer.offer_id)}",
        "offerId": offer.offer_id, "contentLanguage": "ar", "feedLabel": "SA",
        "dataSource": data_source or f"accounts/{ACCOUNT}/dataSources/{SOURCE}",
        "productAttributes": dict(offer.product_input["productAttributes"] if attrs is None else attrs),
    }
    if status in statuses:
        body["productStatus"] = statuses[status]
    return TransportResponse(200, body=body)


# ─────────────────────────────────────────────────────────────────────
# Planning: binding and scope
# ─────────────────────────────────────────────────────────────────────


class TestBindingAndScope:

    def test_dry_run_plan_makes_zero_transport_calls(self):
        offers = [_perfume(), *_shirt_offers(), _honey()]
        fake = FakeMerchantTransport()
        plan = _plan(offers, observations=[_absent(o.offer_id) for o in offers])
        assert [a.action for a in plan.actions] == [ACTION_INSERT] * 4
        assert fake.calls == []

    @pytest.mark.parametrize("binding,reason", [
        (None, "binding_missing"),
        ("URL", "data_source_not_api"),
        ("FILE", "data_source_not_api"),
        ("stale", "binding_evidence_stale"),
        ("future", "binding_evidence_future"),
        ("tenant", "binding_tenant_mismatch"),
    ])
    def test_unusable_binding_blocks_every_offer(self, binding, reason):
        b = {
            None: None,
            "URL": _binding(data_source_input="URL"),
            "FILE": _binding(data_source_input="FILE"),
            "stale": _binding(observed_at=NOW - 7200),
            "future": _binding(observed_at=NOW + 5),
            "tenant": _binding(tenant_id=TENANT + 1),
        }[binding]
        offers = [_perfume(), _honey()]
        plan = _plan(offers, binding=b, observations=[_absent(o.offer_id) for o in offers])
        assert plan.executable == ()
        assert {a.action for a in plan.actions} == {ACTION_BLOCKED}
        assert all(reason in a.reasons for a in plan.actions)
        assert all(a.request is None for a in plan.actions)

    @pytest.mark.parametrize("scope_kw", [
        {"tenant_id": TENANT + 1}, {"account_id": OTHER_ACCOUNT}, {"data_source_id": OTHER_SOURCE},
    ])
    def test_foreign_scope_evidence_blocks(self, scope_kw):
        offer = _perfume()
        action = _only(_plan([offer], owned=[_owned(offer, **scope_kw)], observations=[_present(offer.offer_id)]))
        assert (action.action, action.reasons) == (ACTION_BLOCKED, ("evidence_scope_mismatch",))

    def test_foreign_scope_observation_blocks_insert(self):
        offer = _perfume()
        obs = RemoteObservation(_scope(offer.offer_id, account_id=OTHER_ACCOUNT), OBS_ABSENT, "products.get", NOW - 5)
        assert _only(_plan([offer], observations=[obs])).reasons == ("evidence_scope_mismatch",)

    def test_tenants_are_isolated(self):
        a, b = _perfume(), _honey()
        other = OwnedOfferEvidence(_scope(b.offer_id, tenant_id=TENANT + 1), b.product_input["productAttributes"], NOW - 5)
        plan = _plan([a, b], owned=[other], observations=[_absent(a.offer_id), _absent(b.offer_id)])
        by_id = {x.offer_id: x for x in plan.actions}
        assert by_id[a.offer_id].action == ACTION_INSERT
        assert by_id[b.offer_id].reasons == ("evidence_scope_mismatch",)


# ─────────────────────────────────────────────────────────────────────
# Planning: insert
# ─────────────────────────────────────────────────────────────────────


class TestInsert:

    def test_insert_only_on_fresh_get_absence(self):
        offer = _perfume()
        action = _only(_plan([offer], observations=[_absent(offer.offer_id)]))
        assert action.action == ACTION_INSERT
        assert (action.request.method, action.request.path) == ("POST", _paths(offer.offer_id)[0])
        assert action.request.body == offer.product_input
        assert action.request.query == (("dataSource", f"accounts/{ACCOUNT}/dataSources/{SOURCE}"),)

    @pytest.mark.parametrize("obs_factory,reason", [
        (lambda o: [], "remote_observation_missing"),
        (lambda o: [_absent(o, at=NOW - 3600)], "remote_observation_stale"),
        (lambda o: [_absent(o, at=NOW + 10)], "remote_observation_future"),
        (lambda o: [RemoteObservation(_scope(o), OBS_ABSENT, ORIGIN_PRODUCTS_LIST, NOW - 5)],
         "absence_not_proven_by_list_page"),
        (lambda o: [_present(o, state="unknown")], "remote_state_unknown"),
        (lambda o: [_present(o, OBS_PRESENT_FOREIGN_SOURCE)], "foreign_source_owns_offer"),
        (lambda o: [_present(o)], "ownership_evidence_missing"),
        (lambda o: [_absent(o), _absent(o, at=NOW - 10)], "ambiguous_remote_observation"),
    ])
    def test_insert_fails_closed_without_proven_absence(self, obs_factory, reason):
        offer = _perfume()
        action = _only(_plan([offer], observations=obs_factory(offer.offer_id)))
        assert (action.action, action.reasons, action.request) == (ACTION_BLOCKED, (reason,), None)

    def test_unresolved_prior_write_blocks_insert(self):
        offer = _honey()
        action = _only(_plan([offer], observations=[_absent(offer.offer_id)],
                             pending=[PendingWrite(_scope(offer.offer_id), NOW - 20)]))
        assert action.reasons == ("prior_write_unresolved",)

    def test_mapper_blocked_offer_never_executes(self):
        offer = _perfume(description="")
        assert offer.status == "blocked"
        action = _only(_plan([offer], observations=[_absent(offer.offer_id)]))
        assert action.reasons == ("offer_blocked_offline",)

    def test_duplicate_identity_by_case_is_blocked(self):
        a = _perfume(external_id="SKU-1")
        b = OfferPreview("sku-1", 99, None, {**a.product_input, "offerId": "sku-1"}, "active", tenant_id=TENANT)
        action = _only(_plan([a, b], observations=[_absent("SKU-1")]))
        assert action.action == ACTION_BLOCKED and "duplicate_offer_identity" in action.reasons

    def test_offer_for_other_language_or_label_is_blocked(self):
        offer = _perfume()
        moved = OfferPreview(offer.offer_id, 1, None, {**offer.product_input, "feedLabel": "AE"}, "active", tenant_id=TENANT)
        assert "offer_target_mismatch" in _only(_plan([moved])).reasons

    def test_attribute_outside_supported_schema_is_blocked(self):
        offer = _perfume()
        attrs = {**offer.product_input["productAttributes"], "customLabel0": "x"}
        odd = OfferPreview(offer.offer_id, 1, None, {**offer.product_input, "productAttributes": attrs}, "active",
                           tenant_id=TENANT)
        assert _only(_plan([odd], observations=[_absent(offer.offer_id)])).reasons == (
            "attribute_not_in_supported_schema",)


# ─────────────────────────────────────────────────────────────────────
# Planning: noop / patch / delete bounded by ownership
# ─────────────────────────────────────────────────────────────────────


class TestOwnedLifecycle:

    def test_unchanged_owned_offer_is_noop(self):
        offer = _perfume()
        action = _only(_plan([offer], owned=[_owned(offer)], observations=[_present(offer.offer_id)]))
        assert (action.action, action.request) == (ACTION_NOOP, None)

    def test_changed_owned_offer_is_patched_with_explicit_mask(self):
        offer = _shirt_offers()[0]
        old = {**offer.product_input["productAttributes"], "title": "قميص قديم", "mpn": "OLD-1"}
        action = _only(_plan([offer], owned=[_owned(offer, attrs=old)], observations=[_present(offer.offer_id)]))
        assert action.action == ACTION_PATCH
        assert (action.request.method, action.request.path) == ("PATCH", _paths(offer.offer_id)[1])
        assert dict(action.request.query)["updateMask"] == "productAttributes.mpn,productAttributes.title"
        assert action.request.body == {"productAttributes": {"title": "قميص قطني أزرق"}}
        assert action.removed_attributes == ("mpn",)

    @pytest.mark.parametrize("obs_factory,reason", [
        (lambda o: [], "remote_observation_missing"),
        (lambda o: [_present(o, at=NOW - 3600)], "remote_observation_stale"),
        (lambda o: [_present(o, OBS_PRESENT_FOREIGN_SOURCE)], "foreign_source_owns_offer"),
        (lambda o: [_absent(o)], "owned_offer_absent_remotely"),
    ])
    def test_owned_record_alone_never_authorizes_patch(self, obs_factory, reason):
        offer = _perfume()
        old = {**offer.product_input["productAttributes"], "title": "old"}
        action = _only(_plan([offer], owned=[_owned(offer, attrs=old)], observations=obs_factory(offer.offer_id)))
        assert (action.action, action.reasons) == (ACTION_BLOCKED, (reason,))

    @pytest.mark.parametrize("at,reason", [(NOW - 90000, "ownership_evidence_stale"),
                                           (NOW + 1, "ownership_evidence_future")])
    def test_stale_or_future_ownership_blocks(self, at, reason):
        offer = _perfume()
        action = _only(_plan([offer], owned=[_owned(offer, at=at)], observations=[_present(offer.offer_id)]))
        assert action.reasons == (reason,)

    def test_ambiguous_or_case_conflicting_ownership_blocks(self):
        offer = _perfume(external_id="SKU-9")
        twice = [_owned(offer), _owned(offer, at=NOW - 100)]
        assert "ambiguous_ownership_evidence" in _only(
            _plan([offer], owned=twice, observations=[_present("SKU-9")])).reasons
        lower = OwnedOfferEvidence(_scope("sku-9"), offer.product_input["productAttributes"], NOW - 5)
        assert "offer_id_case_conflict" in _only(
            _plan([offer], owned=[lower], observations=[_present("SKU-9")])).reasons

    def test_owned_but_mapper_blocked_offer_is_not_deleted(self):
        good = _perfume()
        blocked = _perfume(description="")
        proof = CatalogSnapshotProof(TENANT, "ar", "SA", True, NOW - 5)
        action = _only(_plan([blocked], owned=[_owned(good)], observations=[_present(good.offer_id)],
                             snapshot_proof=proof))
        assert (action.action, action.reasons) == (ACTION_BLOCKED, ("offer_blocked_offline",))

    def test_retired_owned_offer_deleted_only_with_complete_snapshot_proof(self):
        gone = _shirt_offers()[1]
        keep = _perfume()
        common = dict(owned=[_owned(gone), _owned(keep)],
                      observations=[_present(gone.offer_id), _present(keep.offer_id)])
        proof = CatalogSnapshotProof(TENANT, "ar", "SA", True, NOW - 5)
        plan = _plan([keep], snapshot_proof=proof, **common)
        by_id = {a.offer_id: a for a in plan.actions}
        assert by_id[keep.offer_id].action == ACTION_NOOP
        delete = by_id[gone.offer_id]
        assert delete.action == ACTION_DELETE
        assert (delete.request.method, delete.request.path, delete.request.body) == (
            "DELETE", _paths(gone.offer_id)[1], None)
        assert delete.request.query == (("dataSource", f"accounts/{ACCOUNT}/dataSources/{SOURCE}"),)

        for bad, reason in [
            (None, "retirement_requires_complete_snapshot_proof"),
            (CatalogSnapshotProof(TENANT, "ar", "SA", False, NOW - 5), "retirement_requires_complete_snapshot_proof"),
            (CatalogSnapshotProof(TENANT, "ar", "SA", 1, NOW - 5), "retirement_requires_complete_snapshot_proof"),
            (CatalogSnapshotProof(TENANT + 1, "ar", "SA", True, NOW - 5), "snapshot_proof_scope_mismatch"),
            (CatalogSnapshotProof(TENANT, "ar", "SA", True, NOW - 3600), "snapshot_proof_stale"),
        ]:
            blocked = {a.offer_id: a for a in _plan([keep], snapshot_proof=bad, **common).actions}[gone.offer_id]
            assert (blocked.action, blocked.reasons) == (ACTION_BLOCKED, (reason,))

    def test_retired_offer_already_absent_is_noop_and_foreign_is_blocked(self):
        gone = _honey()
        proof = CatalogSnapshotProof(TENANT, "ar", "SA", True, NOW - 5)
        absent = _only(_plan([], owned=[_owned(gone)], observations=[_absent(gone.offer_id)], snapshot_proof=proof))
        assert (absent.action, absent.reasons) == (ACTION_NOOP, ("already_absent",))
        foreign = _only(_plan([], owned=[_owned(gone)], observations=[_present(gone.offer_id, OBS_PRESENT_FOREIGN_SOURCE)],
                              snapshot_proof=proof))
        assert foreign.reasons == ("foreign_source_owns_offer",)

    def test_unowned_remote_offers_are_never_deleted(self):
        proof = CatalogSnapshotProof(TENANT, "ar", "SA", True, NOW - 5)
        plan = _plan([], observations=[_present("URL-FEED-1"), _present("URL-FEED-2", OBS_PRESENT_FOREIGN_SOURCE)],
                     snapshot_proof=proof)
        assert plan.actions == ()


# ─────────────────────────────────────────────────────────────────────
# Fake-only simulation
# ─────────────────────────────────────────────────────────────────────


def _insert_plan(offer):
    return _plan([offer], observations=[_absent(offer.offer_id)])


class _NotFake(FakeMerchantTransport):
    pass


class TestSimulationGate:

    @pytest.mark.parametrize("opt_in", [None, SimulationOptIn("yes"), "fake_transport_only"])
    def test_requires_explicit_opt_in(self, opt_in):
        fake = FakeMerchantTransport()
        with pytest.raises(SimulationNotPermitted):
            simulate_plan(_insert_plan(_perfume()), fake, opt_in=opt_in, now=NOW, retry_policy=POLICY)
        assert fake.calls == []

    @pytest.mark.parametrize("transport", [MagicMock(), _NotFake(), object()])
    def test_requires_exact_fake_transport(self, transport):
        with pytest.raises(SimulationNotPermitted):
            simulate_plan(_insert_plan(_perfume()), transport, opt_in=OPT_IN, now=NOW, retry_policy=POLICY)
        if isinstance(transport, MagicMock):
            transport.send.assert_not_called()

    def test_blocked_plan_simulates_nothing(self):
        fake = FakeMerchantTransport()
        plan = _plan([_perfume()], binding=_binding(data_source_input="URL"))
        assert simulate_plan(plan, fake, opt_in=OPT_IN, now=NOW, retry_policy=POLICY) == ()
        assert fake.calls == []


def _sim(plan, script):
    fake = FakeMerchantTransport(script)
    return simulate_plan(plan, fake, opt_in=OPT_IN, now=NOW, retry_policy=POLICY), fake


class TestSimulation:

    def test_insert_confirmed_only_by_matching_readback(self):
        offer = _perfume()
        ins, _patch, get = _paths(offer.offer_id)
        (res,), fake = _sim(_insert_plan(offer), {("POST", ins): [TransportResponse(200, body={})],
                                                  ("GET", get): [_readback(offer)]})
        assert res.outcome == OUTCOME_CONFIRMED
        assert res.publication_state == PUBLICATION_APPROVED
        assert [c.op for c in fake.calls] == ["insert", "get"]

    @pytest.mark.parametrize("readback,reason", [
        ("stale", "readback_content_not_matching"),
        ("foreign", "readback_foreign_source"),
        ("missing", "readback_not_found"),
    ])
    def test_accepted_write_without_matching_readback_stays_unresolved(self, readback, reason):
        offer = _honey()
        ins, _patch, get = _paths(offer.offer_id)
        rb = {
            "stale": _readback(offer, attrs={**offer.product_input["productAttributes"], "title": "old"}),
            "foreign": _readback(offer, data_source=f"accounts/{ACCOUNT}/dataSources/{OTHER_SOURCE}"),
            "missing": TransportResponse(404),
        }[readback]
        (res,), _fake = _sim(_insert_plan(offer), {("POST", ins): [TransportResponse(200, body={})], ("GET", get): [rb]})
        assert res.outcome == OUTCOME_UNRESOLVED
        assert reason in res.reasons
        assert evidence_after_apply((), [res]) == ()

    def test_unknown_status_is_content_confirmed_but_publication_unknown(self):
        offer = _perfume()
        ins, _patch, get = _paths(offer.offer_id)
        (res,), _ = _sim(_insert_plan(offer), {("POST", ins): [TransportResponse(200, body={})],
                                               ("GET", get): [_readback(offer, status="none")]})
        assert (res.outcome, res.publication_state) == (OUTCOME_CONFIRMED, PUBLICATION_UNKNOWN)

    @pytest.mark.parametrize("failure", [TransportResponse(None, error="timeout"), TransportResponse(503),
                                         TransportResponse(500)])
    def test_uncertain_write_reads_back_and_never_resends(self, failure):
        offer = _perfume()
        ins, _patch, get = _paths(offer.offer_id)
        (res,), fake = _sim(_insert_plan(offer), {("POST", ins): [failure, TransportResponse(200, body={})],
                                                  ("GET", get): [TransportResponse(404)]})
        assert res.outcome == OUTCOME_UNRESOLVED
        assert "write_uncertain_readback_only" in res.reasons
        assert [c.op for c in fake.calls] == ["insert", "get"]

    def test_uncertain_write_confirmed_by_matching_readback(self):
        offer = _perfume()
        ins, _patch, get = _paths(offer.offer_id)
        (res,), fake = _sim(_insert_plan(offer), {("POST", ins): [TransportResponse(None, error="timeout")],
                                                  ("GET", get): [_readback(offer)]})
        assert res.outcome == OUTCOME_CONFIRMED
        assert sum(c.op == "insert" for c in fake.calls) == 1

    def test_rate_limited_write_is_rescheduled_on_virtual_clock(self):
        offer = _perfume()
        ins, _patch, get = _paths(offer.offer_id)
        (res,), fake = _sim(_insert_plan(offer), {
            ("POST", ins): [TransportResponse(429, {"Retry-After": "10"}), TransportResponse(200, body={})],
            ("GET", get): [_readback(offer)],
        })
        assert res.outcome == OUTCOME_CONFIRMED
        assert res.retry_schedule == (NOW + 10,)
        assert [(a.op, a.at, a.status) for a in res.attempts] == [
            ("insert", NOW, 429), ("insert", NOW + 10, 200), ("get", NOW + 10, 200)]

    def test_rate_limit_gives_up_at_max_attempts(self):
        offer = _perfume()
        ins, _patch, _get = _paths(offer.offer_id)
        (res,), fake = _sim(_insert_plan(offer), {("POST", ins): [TransportResponse(429)] * 5})
        assert res.outcome == OUTCOME_GAVE_UP
        assert len(fake.calls) == POLICY.max_attempts
        assert res.retry_schedule == (NOW + 2, NOW + 6)

    def test_transient_readback_is_retried(self):
        offer = _honey()
        ins, _patch, get = _paths(offer.offer_id)
        (res,), fake = _sim(_insert_plan(offer), {("POST", ins): [TransportResponse(200, body={})],
                                                  ("GET", get): [TransportResponse(503), _readback(offer)]})
        assert res.outcome == OUTCOME_CONFIRMED
        assert [c.op for c in fake.calls] == ["insert", "get", "get"]

    @pytest.mark.parametrize("status,kind", [(401, "permanent_auth"), (403, "permanent_permission"),
                                             (400, "permanent_validation")])
    def test_permanent_write_errors_stop_without_retry_or_readback(self, status, kind):
        offer = _perfume()
        ins, _patch, _get = _paths(offer.offer_id)
        (res,), fake = _sim(_insert_plan(offer), {("POST", ins): [TransportResponse(status)]})
        assert (res.outcome, res.reasons) == (OUTCOME_PERMANENT_FAILURE, (kind,))
        assert len(fake.calls) == 1

    def test_patch_removed_attribute_must_be_gone_in_readback(self):
        offer = _shirt_offers()[0]
        old = {**offer.product_input["productAttributes"], "mpn": "OLD-1"}
        plan = _plan([offer], owned=[_owned(offer, attrs=old)], observations=[_present(offer.offer_id)])
        _ins, patch, get = _paths(offer.offer_id)
        (stale,), _ = _sim(plan, {("PATCH", patch): [TransportResponse(200, body={})],
                                  ("GET", get): [_readback(offer, attrs=old)]})
        assert stale.outcome == OUTCOME_UNRESOLVED
        (done,), _ = _sim(plan, {("PATCH", patch): [TransportResponse(200, body={})], ("GET", get): [_readback(offer)]})
        assert done.outcome == OUTCOME_CONFIRMED
        (evidence,) = evidence_after_apply([_owned(offer, attrs=old)], [done])
        assert dict(evidence.product_attributes) == offer.product_input["productAttributes"]

    def test_owned_delete_confirmed_by_readback_absence(self):
        gone = _honey()
        proof = CatalogSnapshotProof(TENANT, "ar", "SA", True, NOW - 5)
        owned = [_owned(gone)]
        plan = _plan([], owned=owned, observations=[_present(gone.offer_id)], snapshot_proof=proof)
        _ins, path, get = _paths(gone.offer_id)
        (still,), _ = _sim(plan, {("DELETE", path): [TransportResponse(200, body={})], ("GET", get): [_readback(gone)]})
        assert still.outcome == OUTCOME_UNRESOLVED
        assert evidence_after_apply(owned, [still]) == tuple(owned)
        (done,), fake = _sim(plan, {("DELETE", path): [TransportResponse(200, body={})],
                                    ("GET", get): [TransportResponse(404)]})
        assert done.outcome == OUTCOME_CONFIRMED
        assert fake.calls[0].body is None
        assert evidence_after_apply(owned, [done]) == ()


# ─────────────────────────────────────────────────────────────────────
# Replay / idempotence
# ─────────────────────────────────────────────────────────────────────


class TestReplay:

    def test_same_inputs_give_identical_plan(self):
        offers = [_perfume(), *_shirt_offers(), _honey()]
        obs = [_absent(o.offer_id) for o in offers]
        first, second = _plan(offers, observations=obs), _plan(list(reversed(offers)), observations=list(reversed(obs)))
        assert first.fingerprint == second.fingerprint
        assert first.actions == second.actions

    def test_confirmed_insert_replays_as_noop_and_unresolved_stays_blocked(self):
        ok, unsure = _perfume(), _honey()
        script = {}
        for offer, write, rb in [(ok, TransportResponse(200, body={}), _readback(ok)),
                                 (unsure, TransportResponse(None, error="timeout"), TransportResponse(404))]:
            ins, _patch, get = _paths(offer.offer_id)
            script.setdefault(("POST", ins), []).append(write)
            script[("GET", get)] = [rb]
        plan = _plan([ok, unsure], observations=[_absent(ok.offer_id), _absent(unsure.offer_id)])
        results, _ = _sim(plan, script)
        by_id = {r.scope.offer_id: r for r in results}
        assert by_id[ok.offer_id].outcome == OUTCOME_CONFIRMED
        assert by_id[unsure.offer_id].outcome == OUTCOME_UNRESOLVED

        owned = evidence_after_apply((), results)
        assert [e.scope.offer_id for e in owned] == [ok.offer_id]
        replay_now = by_id[ok.offer_id].confirmed_at + 1
        replay = plan_lifecycle(
            TARGET, _binding(observed_at=replay_now - 10), [ok, unsure], now=replay_now, freshness=FRESH,
            owned=owned,
            observations=[by_id[ok.offer_id].observation, _absent(unsure.offer_id, at=replay_now - 1)],
            pending=[PendingWrite(scope_for(_binding(), unsure.offer_id), NOW)],
        )
        actions = {a.offer_id: a for a in replay.actions}
        assert actions[ok.offer_id].action == ACTION_NOOP
        assert actions[unsure.offer_id].reasons == ("prior_write_unresolved",)
        assert replay.executable == ()


# ─────────────────────────────────────────────────────────────────────
# Review regressions: tenant provenance, envelope identity, exact case,
# official read-back names, malformed status
# ─────────────────────────────────────────────────────────────────────


class TestTenantProvenance:

    def test_real_mapper_offer_from_other_tenant_never_inserts(self):
        other = GoogleMerchantTarget(tenant_id=TENANT + 1, content_language="ar", feed_label="SA")
        row = _product(id=80, tenant_id=TENANT + 1, external_id="PRF-100")
        foreign = map_product(other, row, []).offers[0]
        own = _perfume()
        assert (foreign.tenant_id, own.tenant_id) == (TENANT + 1, TENANT)
        assert foreign.status == own.status == "passes_offline_checks"
        assert foreign.offer_id == own.offer_id

        plan = _plan([foreign], observations=[_absent(foreign.offer_id)])
        action = _only(plan)
        assert (action.action, action.reasons, action.request) == (ACTION_BLOCKED, ("offer_tenant_mismatch",), None)
        fake = FakeMerchantTransport()
        assert simulate_plan(plan, fake, opt_in=OPT_IN, now=NOW, retry_policy=POLICY) == ()
        assert fake.calls == []

    @pytest.mark.parametrize("tenant,reason", [
        (None, "offer_tenant_provenance_missing"), (TENANT + 1, "offer_tenant_mismatch"),
        (str(TENANT), "offer_tenant_mismatch"), (True, "offer_tenant_mismatch"),
    ])
    def test_offer_without_matching_tenant_provenance_is_blocked(self, tenant, reason):
        src = _perfume()
        offer = OfferPreview(src.offer_id, 1, None, src.product_input, "active", tenant_id=tenant)
        assert _only(_plan([offer], observations=[_absent(src.offer_id)])).reasons == (reason,)

    def test_non_mapper_offer_objects_are_refused(self):
        with pytest.raises(TypeError):
            _plan([SimpleNamespace(offer_id="PRF-100", product_input={}, tenant_id=TENANT)])


class TestEnvelopeIdentity:

    def test_payload_offer_id_must_equal_envelope(self):
        src = _perfume()
        body = {**src.product_input, "offerId": "FOREIGN-B"}
        smuggled = OfferPreview("PRF-100", 1, None, body, "active", tenant_id=TENANT)
        for obs in ([_absent("PRF-100")], [_absent("PRF-100"), _absent("FOREIGN-B")], [_absent("FOREIGN-B")]):
            action = _only(_plan([smuggled], observations=obs))
            assert action.action == ACTION_BLOCKED and action.request is None
            assert "offer_identity_envelope_mismatch" in action.reasons

    @pytest.mark.parametrize("field,value", [
        ("legacyLocal", True), ("customAttributes", [{"name": "x", "value": "y"}]), ("versionNumber", "7"),
    ])
    def test_extra_top_level_field_in_offer_payload_is_blocked_before_planning(self, field, value):
        src = _perfume()
        offer = OfferPreview(src.offer_id, 1, None, {**src.product_input, field: value}, "active", tenant_id=TENANT)
        action = _only(_plan([offer], observations=[_absent(src.offer_id)]))
        assert (action.action, action.reasons, action.request) == (
            ACTION_BLOCKED, ("product_input_field_not_allowed",), None)


    @pytest.mark.parametrize("field,value", [
        ("legacyLocal", True), ("customAttributes", [{"name": "x", "value": "y"}]), ("versionNumber", "7"),
    ])
    def test_extra_top_level_field_blocks_owned_offer_patch_and_noop(self, field, value):
        src = _perfume()
        offer = OfferPreview(src.offer_id, 1, None, {**src.product_input, field: value}, "active", tenant_id=TENANT)
        for attrs in (src.product_input["productAttributes"], {**src.product_input["productAttributes"], "title": "old"}):
            action = _only(_plan([offer], owned=[_owned(src, attrs=attrs)], observations=[_present(src.offer_id)]))
            assert (action.action, action.reasons) == (ACTION_BLOCKED, ("product_input_field_not_allowed",))


_INSERT_FIELD_TAMPERS = [
    ("legacyLocal", True), ("customAttributes", [{"name": "x", "value": "y"}]), ("versionNumber", "7"),
]
_INSERT_TAMPERS = [
    *[(lambda b, f=f, v=v: b.__setitem__(f, v), "request_field_not_allowed") for f, v in _INSERT_FIELD_TAMPERS],
    (lambda b: b.__setitem__("offerId", "FOREIGN-B"), "request_identity_mismatch"),
    (lambda b: b.__setitem__("feedLabel", "AE"), "request_identity_mismatch"),
    (lambda b: b["productAttributes"].__setitem__("title", "x"), "request_payload_mismatch"),
]
_PATCH_TAMPERS = [
    lambda b: b["productAttributes"].__setitem__("brand", "dropped"),
    lambda b: b["productAttributes"].__setitem__("title", "x"),
    lambda b: b.__setitem__("productAttributes", 1),
    lambda b: b.__setitem__("offerId", "FOREIGN-B"),
    *[lambda b, f=f, v=v: b.__setitem__(f, v) for f, v in _INSERT_FIELD_TAMPERS],
]
_IDS = ["legacyLocal", "customAttributes", "versionNumber", "offerId", "feedLabel", "title"]


def _forge(action):
    """Re-snapshot a tampered action, as if the snapshot guard were bypassed."""
    return dataclasses.replace(action, snapshot=action_snapshot(
        action.offer_id, action.action, action.request, action.desired_attributes, action.removed_attributes,
    ))


def _patch_plan():
    offer = _shirt_offers()[0]
    old = {**offer.product_input["productAttributes"], "title": "old", "mpn": "OLD-1"}
    return _plan([offer], owned=[_owned(offer, attrs=old)], observations=[_present(offer.offer_id)])


def _refused(plan, reason):
    fake = FakeMerchantTransport()
    (res,) = simulate_plan(plan, fake, opt_in=OPT_IN, now=NOW, retry_policy=POLICY)
    assert (res.outcome, res.reasons) == (OUTCOME_REFUSED, (reason,))
    assert fake.calls == []


class TestPostPlanIntegrity:

    @pytest.mark.parametrize("tamper,_reason", _INSERT_TAMPERS, ids=_IDS)
    def test_post_plan_insert_mutation_is_refused_unsent(self, tamper, _reason):
        plan = _insert_plan(_perfume())
        tamper(plan.executable[0].request.body)
        _refused(plan, "request_mutated_after_planning")

    @pytest.mark.parametrize("tamper,reason", _INSERT_TAMPERS, ids=_IDS)
    def test_forged_snapshot_insert_still_fails_structural_checks(self, tamper, reason):
        plan = _insert_plan(_perfume())
        tamper(plan.executable[0].request.body)
        _refused(dataclasses.replace(plan, actions=(_forge(plan.executable[0]),)), reason)

    @pytest.mark.parametrize("tamper", _PATCH_TAMPERS)
    def test_post_plan_patch_mutation_is_refused_unsent(self, tamper):
        plan = _patch_plan()
        tamper(plan.executable[0].request.body)
        _refused(plan, "request_mutated_after_planning")

    @pytest.mark.parametrize("tamper", _PATCH_TAMPERS)
    def test_forged_snapshot_patch_still_fails_structural_checks(self, tamper):
        plan = _patch_plan()
        tamper(plan.executable[0].request.body)
        _refused(dataclasses.replace(plan, actions=(_forge(plan.executable[0]),)), "request_payload_mismatch")

    def test_consistent_mutation_of_desired_and_body_is_refused(self):
        plan = _insert_plan(_perfume())
        action = plan.executable[0]
        action.request.body["productAttributes"]["title"] = "x"
        action.desired_attributes["title"] = "x"
        _refused(plan, "request_mutated_after_planning")

    def test_action_without_planning_snapshot_is_refused(self):
        plan = _insert_plan(_perfume())
        bare = dataclasses.replace(plan.executable[0], snapshot="")
        _refused(dataclasses.replace(plan, actions=(bare,)), "request_mutated_after_planning")

    def test_unserializable_mutation_is_refused(self):
        plan = _insert_plan(_perfume())
        plan.executable[0].request.body["productAttributes"]["title"] = object()
        _refused(plan, "request_not_serializable")

    def test_sent_request_is_rebuilt_from_snapshot(self):
        offer = _perfume()
        plan = _insert_plan(offer)
        ins, _patch, get = _paths(offer.offer_id)
        fake = FakeMerchantTransport({("POST", ins): [TransportResponse(200, body={})], ("GET", get): [_readback(offer)]})
        (res,) = simulate_plan(plan, fake, opt_in=OPT_IN, now=NOW, retry_policy=POLICY)
        assert res.outcome == OUTCOME_CONFIRMED
        sent = fake.calls[0]
        assert sent == plan.executable[0].request and sent is not plan.executable[0].request
        assert set(sent.body) == {"offerId", "contentLanguage", "feedLabel", "productAttributes"}
        sent.body["legacyLocal"] = True  # mutating what the transport saw cannot reach the plan
        assert "legacyLocal" not in plan.executable[0].request.body
        assert res.desired_attributes == plan.executable[0].desired_attributes
        assert res.desired_attributes is not plan.executable[0].desired_attributes

    def test_transport_mutating_request_cannot_change_confirmation(self):
        offer = _perfume()
        plan = _insert_plan(offer)
        ins, _patch, get = _paths(offer.offer_id)

        fake = FakeMerchantTransport({("POST", ins): [TransportResponse(200, body={})],
                                      ("GET", get): [_readback(offer, attrs={**offer.product_input["productAttributes"],
                                                                             "title": "x"})]})
        original_send = fake.send

        def send(request):  # instance attribute; still the exact FakeMerchantTransport type
            if request.body:
                request.body["productAttributes"]["title"] = "x"
            return original_send(request)

        fake.send = send
        (res,) = simulate_plan(plan, fake, opt_in=OPT_IN, now=NOW, retry_policy=POLICY)
        assert (res.outcome, res.reasons) == (OUTCOME_UNRESOLVED, ("readback_content_not_matching",))
        assert plan.executable[0].request.body["productAttributes"]["title"] == offer.product_input[
            "productAttributes"]["title"]


class TestExactCaseIdentity:

    def test_lowercase_get_404_never_authorizes_uppercase_insert(self):
        offer = _perfume()
        assert offer.offer_id == "PRF-100"
        action = _only(_plan([offer], observations=[_absent("prf-100")]))
        assert (action.action, action.reasons, action.request) == (ACTION_BLOCKED, ("offer_id_case_conflict",), None)

    def test_exact_probe_still_inserts(self):
        assert _only(_plan([_perfume()], observations=[_absent("PRF-100")])).action == ACTION_INSERT

    @pytest.mark.parametrize("kw", [
        {"pending": [PendingWrite(_scope("prf-100"), NOW - 5)]},
        {"owned": [OwnedOfferEvidence(_scope("prf-100"), {}, NOW - 5)]},
        {"observations": [_absent("PRF-100"), _absent("prf-100")]},
    ])
    def test_any_case_variant_record_blocks(self, kw):
        kw.setdefault("observations", [_absent("PRF-100")])
        action = _only(_plan([_perfume()], **kw))
        assert action.action == ACTION_BLOCKED and "offer_id_case_conflict" in action.reasons

    def test_case_variant_observation_never_authorizes_delete(self):
        sku = _perfume(external_id="SKU-9")
        proof = CatalogSnapshotProof(TENANT, "ar", "SA", True, NOW - 5)
        exact = _only(_plan([], owned=[_owned(sku)], observations=[_present("SKU-9")], snapshot_proof=proof))
        assert exact.action == ACTION_DELETE
        action = _only(_plan([], owned=[_owned(sku)], observations=[_present("sku-9")], snapshot_proof=proof))
        assert (action.action, action.reasons) == (ACTION_BLOCKED, ("offer_id_case_conflict",))


class TestOfficialReadbackForms:

    def test_raw_name_with_slash_confirms_insert(self):
        offer = _perfume(external_id="sku/123")
        assert offer.offer_id == "sku/123"
        ins, _patch, get = _paths(offer.offer_id)
        (res,), _ = _sim(_insert_plan(offer), {("POST", ins): [TransportResponse(200, body={})],
                                               ("GET", get): [_readback(offer)]})
        assert res.readback.name == f"accounts/{ACCOUNT}/products/ar~SA~sku/123"
        assert res.outcome == OUTCOME_CONFIRMED

    def test_inconsistent_encoded_name_stays_unresolved(self):
        offer = _perfume()
        ins, _patch, get = _paths(offer.offer_id)
        rb = _readback(offer)
        rb.body["base64EncodedName"] = f"accounts/{ACCOUNT}/products/{_rid('OTHER')}"
        (res,), _ = _sim(_insert_plan(offer), {("POST", ins): [TransportResponse(200, body={})], ("GET", get): [rb]})
        assert (res.outcome, res.reasons) == (OUTCOME_UNRESOLVED, ("readback_identity_mismatch",))


class TestMalformedReadbackSimulation:

    @pytest.mark.parametrize("status", [
        1, {"destinationStatuses": 1}, {"destinationStatuses": [{"reportingContext": "SHOPPING_ADS",
                                                                "approvedCountries": ["SA"]}], "itemLevelIssues": 1},
        {"destinationStatuses": [{"reportingContext": "SHOPPING_ADS", "approvedCountries": ["SA"],
                                  "disapprovedCountries": "SA"}]},
        {"destinationStatuses": [{"reportingContext": "SHOPPING_ADS", "approvedCountries": [""]}]},
    ])
    def test_malformed_status_never_confirms(self, status):
        offer = _perfume()
        ins, _patch, get = _paths(offer.offer_id)
        rb = _readback(offer)
        rb.body["productStatus"] = status
        (res,), _ = _sim(_insert_plan(offer), {("POST", ins): [TransportResponse(200, body={})], ("GET", get): [rb]})
        assert (res.outcome, res.reasons) == (OUTCOME_UNRESOLVED, ("readback_malformed",))
        assert res.readback.publication_state == "unknown"
        assert evidence_after_apply((), [res]) == ()

    @pytest.mark.parametrize("body", [None, [], 1])
    def test_non_object_readback_body_never_confirms(self, body):
        offer = _perfume()
        ins, _patch, get = _paths(offer.offer_id)
        (res,), _ = _sim(_insert_plan(offer), {("POST", ins): [TransportResponse(200, body={})],
                                               ("GET", get): [TransportResponse(200, body=body)]})
        assert res.outcome == OUTCOME_UNRESOLVED
