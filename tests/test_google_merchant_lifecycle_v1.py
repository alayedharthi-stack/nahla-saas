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
    OUTCOME_UNRESOLVED,
    SIMULATION_ACK,
    CatalogSnapshotProof,
    FakeMerchantTransport,
    FreshnessPolicy,
    OfferScope,
    OwnedOfferEvidence,
    PendingWrite,
    RemoteObservation,
    SimulationNotPermitted,
    SimulationOptIn,
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
        "name": f"accounts/{ACCOUNT}/products/{_rid(offer.offer_id)}",
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
        b = OfferPreview("sku-1", 99, None, {**a.product_input, "offerId": "sku-1"}, "active")
        action = _only(_plan([a, b], observations=[_absent("SKU-1")]))
        assert action.action == ACTION_BLOCKED and "duplicate_offer_identity" in action.reasons

    def test_offer_for_other_language_or_label_is_blocked(self):
        offer = _perfume()
        moved = OfferPreview(offer.offer_id, 1, None, {**offer.product_input, "feedLabel": "AE"}, "active")
        assert "offer_target_mismatch" in _only(_plan([moved])).reasons

    def test_attribute_outside_supported_schema_is_blocked(self):
        offer = _perfume()
        attrs = {**offer.product_input["productAttributes"], "customLabel0": "x"}
        odd = OfferPreview(offer.offer_id, 1, None, {**offer.product_input, "productAttributes": attrs}, "active")
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
