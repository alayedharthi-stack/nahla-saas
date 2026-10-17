"""Readiness gate on REAL PostgreSQL: no ``active`` connection and no
install-first ``ready`` before this shop's ``app/uninstalled`` subscription
is read back for the current connection generation.

Shopify is the in-process fake of the foundation suite plus its shop-scoped
subscription model (``_shopify_webhook_fake``): no network, no merchant, no
credential outside the test. Skipped (never passed) without
``NAHLA_RELIABILITY_PG_ADMIN_DSN``; ``NAHLA_RELIABILITY_REQUIRE_PG=1`` makes a
missing database fail.

Neutral generic merchants and shops only (platform-wide behaviour).
"""
import base64
import json
import secrets
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from backend.tests import test_shopify_connection_pg as foundation
from backend.tests import test_shopify_install_first_pg as first
from backend.tests.test_shopify_install_first import env as install_env
from services.shopify_connection import install, lifecycle, recovery
from services.shopify_connection.install_models import ShopifyInstallFlow as Flow, ShopifyOwnerIdentity as Owner
from services.shopify_connection.oauth import ShopifyApiError
from models import Tenant, User

pytestmark = foundation.pytestmark
template_db = foundation.template_db
pg = foundation.pg
store = first.store

SHOES, SHIRTS, PERFUME = foundation.SHOP_SHOES, foundation.SHOP_SHIRTS, foundation.SHOP_PERFUME
URI = "https://api.shop-review.example.test/webhooks/shopify/app-uninstalled"           # legacy callback host
INSTALL_URI = "https://api.example.test/webhooks/shopify/app-uninstalled"               # install-first host
row, refused, connect, run = foundation.row, foundation.refused, foundation.connect, foundation.run


def transient(code="upstream_unavailable"):
    return ShopifyApiError("transient", code)


def proofs(db_or_pg, shop):
    return [dict(r) for r in foundation._sql(
        db_or_pg, "SELECT generation, detail FROM shopify_connection_audit WHERE shop_domain = :s AND action = :a"
                  " ORDER BY id", s=shop, a=lifecycle.WEBHOOK_VERIFIED)]


def audit_actions(pg, shop):
    return [r["action"] for r in foundation._sql(
        pg, "SELECT action FROM shopify_connection_audit WHERE shop_domain = :s ORDER BY id", s=shop)]


def exact_subscriptions(fake, shop, uri=URI):
    return [s for s in fake.webhooks(shop) if s.uri == uri and s.format == "JSON" and s.topic == "APP_UNINSTALLED"]


def make_due(pg, shop):
    foundation._sql(pg, "UPDATE shopify_connections SET reconcile_next_at = now() - interval '1 second'"
                        " WHERE shop_domain = :s", s=shop)


def tick(pg, fake, env=None, now=None):
    return run(recovery.run_recovery_tick(session_factory=pg.Session, env=env or foundation.ENV, api=fake, now=now))


def ready_for(pg, shop):
    with pg.Session() as db:
        return lifecycle.is_ready(db, lifecycle._connection_by_domain(db, shop))


def access_token_refused(pg, shop, fake):
    r = row(pg, shop)
    with pg.Session() as db:
        with pytest.raises(lifecycle.ConnectionRefused) as info:
            run(lifecycle.active_access_token(db, tenant_id=r["tenant_id"], connection_id=r["id"], api=fake,
                                              cipher=foundation.CIPHER))
    return info.value.code


def assert_held_with_grant(pg, shop, key=None):
    """Not ready, encrypted grant preserved and decryptable, and claimable by
    the recovery runner (a quarantined row is due when ``reconcile_next_at``
    is NULL or past)."""
    r = row(pg, shop)
    assert r["status"] == "quarantined" and r["quarantine_reason"] == lifecycle.WEBHOOK_PENDING
    cipher = foundation.crypto.TokenCipher(key) if key else foundation.CIPHER
    ctx = dict(tenant_id=r["tenant_id"], shop_domain=shop, generation=r["generation"])
    access = cipher.decrypt(r["access_token_enc"], foundation.crypto.token_context(
        foundation.crypto.PURPOSE_ACCESS_TOKEN, **ctx))
    refresh = cipher.decrypt(r["refresh_token_enc"], foundation.crypto.token_context(
        foundation.crypto.PURPOSE_REFRESH_TOKEN, **ctx))
    assert access.startswith("shpat_") and refresh.startswith("shprt_")
    within_cap = lifecycle.utcnow() + timedelta(seconds=recovery.BACKOFF_CAP_SECONDS + 1)
    with pg.Session() as db:   # the durable retry is scheduled no later than the backoff cap
        assert db.scalar(select(lifecycle.ShopifyConnection.id).where(
            recovery._pending_clause(within_cap), lifecycle.ShopifyConnection.id == r["id"])) == r["id"]
    return r


# ── Legacy (JWT) completion ───────────────────────────────────────────────────

def test_connect_registers_once_and_proves_the_current_generation(pg):
    fake, merchant = foundation.FakeShopify(), foundation.Merchant(pg, "shoes")
    summary = connect(pg, merchant, SHOES, fake)
    assert summary["status"] == "active" and ready_for(pg, SHOES)
    [sub] = exact_subscriptions(fake, SHOES)
    assert [k for k, _ in fake.calls] == ["exchange", "identity", "webhook_list", "webhook_create", "webhook_list"]
    [proof] = proofs(pg, SHOES)
    assert proof["generation"] == row(pg, SHOES)["generation"] == 1
    assert proof["detail"] == {"subscription_id": sub.id, "topic": "APP_UNINSTALLED", "uri": URI, "format": "JSON",
                               "created": True, "exact_count": 1, "api_version": "2026-07"}
    actions = audit_actions(pg, SHOES)
    assert actions.index("connected") < actions.index(lifecycle.WEBHOOK_VERIFIED)
    serialized = json.dumps(foundation._sql(pg, "SELECT detail FROM shopify_connection_audit"), default=str)
    assert "shpat_" not in serialized and "shprt_" not in serialized


def test_existing_exact_subscription_is_reused_and_reconnect_never_duplicates(pg):
    fake, merchant = foundation.FakeShopify(), foundation.Merchant(pg, "shirts")
    seeded = fake.add_webhook(SHIRTS, uri=URI)
    fake.add_webhook(SHIRTS, uri="https://elsewhere.example.test/hooks")   # not ours: untouched
    assert connect(pg, merchant, SHIRTS, fake)["status"] == "active"
    assert fake.count("webhook_create") == 0 and proofs(pg, SHIRTS)[0]["detail"]["created"] is False
    with pg.Session() as db:
        lifecycle.disconnect(db, actor=merchant.actor(pg), shop_domain=SHIRTS)
    assert not ready_for(pg, SHIRTS)
    assert connect(pg, merchant, SHIRTS, fake)["status"] == "active"   # reconnect: new generation, same subscription
    assert exact_subscriptions(fake, SHIRTS) == [seeded] and len(fake.webhooks(SHIRTS)) == 2
    assert fake.count("webhook_create") == 0
    assert [p["generation"] for p in proofs(pg, SHIRTS)] == [1, 3] == [1, row(pg, SHIRTS)["generation"]]


def test_failed_registration_keeps_the_grant_not_ready_and_the_runner_completes_without_reexchange(pg):
    fake, merchant = foundation.FakeShopify(), foundation.Merchant(pg, "perfume")
    fake.create_errors = [transient()]
    handle = foundation.ready(pg, merchant, PERFUME, fake)
    assert refused(foundation.complete, pg, merchant, handle, fake) == lifecycle.C_WEBHOOK_PENDING
    assert lifecycle.ConnectionRefused(lifecycle.C_WEBHOOK_PENDING).http_status == 503
    assert_held_with_grant(pg, PERFUME)
    state = foundation.state_row(pg, handle)
    assert state["status"] == "completed" and state["code_enc"] is None   # the code is spent, never re-exchanged
    assert refused(foundation.complete, pg, merchant, handle, fake) == lifecycle.C_REPLAYED
    assert access_token_refused(pg, PERFUME, fake) == lifecycle.C_UNAVAILABLE
    assert proofs(pg, PERFUME) == [] and lifecycle.WEBHOOK_FAILED in audit_actions(pg, PERFUME)
    assert exact_subscriptions(fake, PERFUME) == []
    make_due(pg, PERFUME)
    assert tick(pg, fake).get("retained") == 1
    assert row(pg, PERFUME)["status"] == "active" and ready_for(pg, PERFUME)
    assert fake.count("exchange") == 1 and len(exact_subscriptions(fake, PERFUME)) == 1
    with pg.Session() as db:
        r = row(pg, PERFUME)
        token, generation = run(lifecycle.active_access_token(db, tenant_id=r["tenant_id"], connection_id=r["id"],
                                                              api=fake, cipher=foundation.CIPHER))
    assert generation == r["generation"] and token.startswith("shpat_")


def test_timeout_after_successful_create_is_read_back_without_a_second_create(pg):
    fake, merchant = foundation.FakeShopify(), foundation.Merchant(pg, "lost-response")
    fake.create_lost_response = transient("deadline_exceeded")
    assert connect(pg, merchant, SHOES, fake)["status"] == "active"
    assert fake.count("webhook_create") == 1 and len(fake.webhooks(SHOES)) == 1
    assert proofs(pg, SHOES)[0]["detail"]["created"] is True


def test_lost_create_with_failed_read_back_retries_by_listing_first(pg):
    fake, merchant = foundation.FakeShopify(), foundation.Merchant(pg, "readback")
    fake.create_lost_response = transient("deadline_exceeded")
    fake.list_errors = [None, transient("throttled")]          # the read-back is throttled
    assert refused(connect, pg, merchant, SHOES, fake) == lifecycle.C_WEBHOOK_PENDING
    assert len(fake.webhooks(SHOES)) == 1 and fake.count("webhook_create") == 1   # it exists, unproven
    assert_held_with_grant(pg, SHOES)
    make_due(pg, SHOES)
    assert tick(pg, fake).get("retained") == 1
    assert fake.count("webhook_create") == 1 and len(fake.webhooks(SHOES)) == 1   # query first: no duplicate
    assert proofs(pg, SHOES)[-1]["detail"]["created"] is False and ready_for(pg, SHOES)


@pytest.mark.parametrize("failure", ["list_429", "list_5xx", "create_user_errors", "access_denied", "create_5xx"])
def test_shopify_failures_fail_closed_preserve_the_grant_and_back_off(pg, failure):
    fake, merchant = foundation.FakeShopify(), foundation.Merchant(pg, f"fail-{failure}")
    errors = {
        "list_429": ("list_errors", transient()),
        "list_5xx": ("list_errors", transient()),
        "access_denied": ("list_errors", ShopifyApiError("permanent", "access_denied")),
        "create_user_errors": ("create_errors", ShopifyApiError("permanent", "webhook_user_errors")),
        "create_5xx": ("create_errors", transient()),
    }
    attr, error = errors[failure]
    setattr(fake, attr, [error] * 10)
    assert refused(connect, pg, merchant, SHIRTS, fake) == lifecycle.C_WEBHOOK_PENDING
    first_hold = assert_held_with_grant(pg, SHIRTS)
    assert fake.count("webhook_create") == (0 if attr == "list_errors" else 1)
    attempts = []
    for _ in range(3):
        make_due(pg, SHIRTS)
        assert tick(pg, fake).get("webhook_pending") == 1
        r = assert_held_with_grant(pg, SHIRTS)
        attempts.append((r["reconcile_attempts"], r["reconcile_next_at"]))
    assert [a for a, _ in attempts] == [1, 2, 3]   # bounded: exponential backoff, never a tight loop
    assert attempts[1][1] - attempts[0][1] > timedelta(seconds=20)
    assert row(pg, SHIRTS)["generation"] == first_hold["generation"] and fake.count("exchange") == 1
    assert exact_subscriptions(fake, SHIRTS) == [] and not ready_for(pg, SHIRTS)
    assert foundation._sql(pg, "SELECT count(*) AS n FROM shopify_shop_leases")[0]["n"] == 0


def test_runner_never_registers_while_the_completion_holds_the_shop_lease(pg):
    fake, merchant = foundation.FakeShopify(), foundation.Merchant(pg, "concurrent")
    handle = foundation.ready(pg, merchant, SHOES, fake)
    hook, entered, release = foundation.barrier_hook()
    fake.list_hook = hook
    th, box = foundation.in_thread(foundation.complete, pg, merchant, handle, fake)
    assert entered.wait(20)
    fake.list_hook = None
    make_due(pg, SHOES)
    assert row(pg, SHOES)["status"] == "quarantined"            # stored, not ready, while registering
    assert tick(pg, fake).get("webhook_pending") == 1             # shop lease busy: the runner creates nothing
    assert fake.count("webhook_list") == 1 and fake.count("webhook_create") == 0
    release.set()
    th.join(30)
    assert "error" not in box and box["value"]["status"] == "active"
    assert len(fake.webhooks(SHOES)) == 1 and fake.count("webhook_create") == 1 and ready_for(pg, SHOES)


def test_stale_registration_cannot_prove_or_promote_a_newer_generation(pg):
    fake, merchant = foundation.FakeShopify(), foundation.Merchant(pg, "stale")
    handle = foundation.ready(pg, merchant, SHIRTS, fake)
    hook, entered, release = foundation.barrier_hook()
    fake.create_hook = hook
    th, box = foundation.in_thread(lambda: refused(foundation.complete, pg, merchant, handle, fake))
    assert entered.wait(20)
    with pg.Session() as db:   # the tenant disconnects while the create is in flight
        lifecycle.disconnect(db, actor=merchant.actor(pg), shop_domain=SHIRTS)
    release.set()
    th.join(30)
    assert box["value"] == lifecycle.C_SUPERSEDED
    r = row(pg, SHIRTS)
    assert r["status"] == "disconnected" and r["access_token_enc"] is None and proofs(pg, SHIRTS) == []
    fake.create_hook = None
    assert connect(pg, merchant, SHIRTS, fake)["status"] == "active"
    assert fake.count("webhook_create") == 1 and len(fake.webhooks(SHIRTS)) == 1   # found, not duplicated
    assert [p["generation"] for p in proofs(pg, SHIRTS)] == [row(pg, SHIRTS)["generation"]]


def test_uninstall_delivery_during_registration_blocks_promotion_until_its_own_probe(pg):
    fake, merchant = foundation.FakeShopify(), foundation.Merchant(pg, "mid-delivery")
    handle = foundation.ready(pg, merchant, PERFUME, fake)
    hook, entered, release = foundation.barrier_hook()
    fake.create_hook = hook
    th, box = foundation.in_thread(lambda: refused(foundation.complete, pg, merchant, handle, fake))
    assert entered.wait(20)
    outcome, _ = foundation.uninstall_event(pg, PERFUME, webhook_id=secrets.token_hex(8), digest="e" * 64)
    assert outcome == "quarantined"
    release.set()
    th.join(30)
    assert box["value"] == lifecycle.C_WEBHOOK_PENDING      # proven, but a newer request needs its own probe
    assert [p["generation"] for p in proofs(pg, PERFUME)] == [1]
    assert row(pg, PERFUME)["status"] == "quarantined" and not ready_for(pg, PERFUME)
    assert foundation.reconcile(pg, PERFUME, fake) == "retained" and ready_for(pg, PERFUME)
    assert foundation._sql(pg, "SELECT resolution FROM shopify_webhook_events")[0]["resolution"] == "retained"


def test_reinstall_after_uninstall_needs_its_own_generation_proof(pg):
    fake, merchant = foundation.FakeShopify(), foundation.Merchant(pg, "reinstall")
    connect(pg, merchant, PERFUME, fake)
    fake.uninstall(PERFUME)                                    # Shopify drops the shop's subscriptions too
    foundation.uninstall_event(pg, PERFUME, webhook_id=secrets.token_hex(8), digest="b" * 64)
    assert foundation.reconcile(pg, PERFUME, fake) == "uninstalled"
    fake.create_errors = [transient()]
    assert refused(connect, pg, merchant, PERFUME, fake) == lifecycle.C_WEBHOOK_PENDING
    r = assert_held_with_grant(pg, PERFUME)
    assert [p["generation"] for p in proofs(pg, PERFUME)] == [1] and r["generation"] == 3
    with pg.Session() as db:
        assert not lifecycle.webhook_proven(db, lifecycle._connection_by_domain(db, PERFUME))
    make_due(pg, PERFUME)
    assert tick(pg, fake).get("retained") == 1
    assert [p["generation"] for p in proofs(pg, PERFUME)] == [1, 3] and ready_for(pg, PERFUME)
    assert len(fake.webhooks(PERFUME)) == 1


def test_storage_failure_at_promotion_keeps_the_grant_not_ready_and_recovers_without_duplicate(pg, monkeypatch):
    fake, merchant = foundation.FakeShopify(), foundation.Merchant(pg, "storage")
    handle = foundation.ready(pg, merchant, SHOES, fake)

    def storage_down(*_args, **_kwargs):
        raise OperationalError("UPDATE shopify_connections", {}, Exception("synthetic storage failure"))

    with monkeypatch.context() as m:
        m.setattr(lifecycle, "_promote_if_held", storage_down)
        with pytest.raises(OperationalError):
            foundation.complete(pg, merchant, handle, fake)
    assert_held_with_grant(pg, SHOES)
    assert proofs(pg, SHOES) == [] and len(fake.webhooks(SHOES)) == 1   # created at Shopify, unproven here
    later = lifecycle.utcnow() + timedelta(seconds=lifecycle.EXCHANGE_LEASE_SECONDS + 5)   # stale shop lease
    assert tick(pg, fake, now=later).get("retained") == 1
    assert ready_for(pg, SHOES) and fake.count("webhook_create") == 1 and len(fake.webhooks(SHOES)) == 1
    assert fake.count("exchange") == 1


# ── Reconciliation and pre-gate rows ──────────────────────────────────────────

def test_pre_gate_active_row_without_proof_is_held_then_registered_by_the_runner(pg):
    fake, merchant = foundation.FakeShopify(), foundation.Merchant(pg, "pre-gate")
    connect(pg, merchant, SHIRTS, fake)
    foundation._sql(pg, "DELETE FROM shopify_connection_audit WHERE action = :a", a=lifecycle.WEBHOOK_VERIFIED)
    assert row(pg, SHIRTS)["status"] == "active" and not ready_for(pg, SHIRTS)
    assert access_token_refused(pg, SHIRTS, fake) == lifecycle.C_UNAVAILABLE
    with pg.Session() as db:   # without configuration: held, never promoted
        assert run(lifecycle.reconcile(db, connection_id=row(pg, SHIRTS)["id"], api=fake,
                                       cipher=foundation.CIPHER)) == "webhook_pending"
    assert_held_with_grant(pg, SHIRTS)
    assert "uninstall_webhook_hold" in audit_actions(pg, SHIRTS)
    make_due(pg, SHIRTS)
    assert tick(pg, fake).get("retained") == 1
    assert ready_for(pg, SHIRTS) and fake.count("webhook_create") == 1 and len(fake.webhooks(SHIRTS)) == 1


def test_runner_claims_an_unproven_active_row_without_any_request(pg):
    fake, merchant = foundation.FakeShopify(), foundation.Merchant(pg, "unrequested")
    connect(pg, merchant, PERFUME, fake)
    foundation._sql(pg, "DELETE FROM shopify_connection_audit WHERE action = :a", a=lifecycle.WEBHOOK_VERIFIED)
    r = row(pg, PERFUME)
    assert r["revalidation_requested_at"] is None and r["reconcile_next_at"] is None
    assert tick(pg, fake).get("retained") == 1 and ready_for(pg, PERFUME)
    assert tick(pg, fake)["claimed"] == 0   # proven again: nothing left to claim


def test_quarantine_is_lifted_only_with_proof_including_possible_retirement(pg):
    fake, merchant = foundation.FakeShopify(), foundation.Merchant(pg, "retain")
    connect(pg, merchant, SHOES, fake)
    foundation.uninstall_event(pg, SHOES, webhook_id=secrets.token_hex(8), digest="c" * 64)   # app still installed
    assert foundation.reconcile(pg, SHOES, fake) == "retained" and ready_for(pg, SHOES)   # proof of this generation
    foundation._sql(pg, "DELETE FROM shopify_connection_audit WHERE action = :a", a=lifecycle.WEBHOOK_VERIFIED)
    with pg.Session() as db:
        lifecycle._flag_possible_retirement(db, SHOES, lifecycle.utcnow())
        db.commit()
    assert foundation.reconcile(pg, SHOES, fake) == "webhook_pending"   # forced refresh OK, still no proof
    assert_held_with_grant(pg, SHOES)
    assert fake.count("refresh") == 1
    with pg.Session() as db:
        assert run(lifecycle.reconcile(db, connection_id=row(pg, SHOES)["id"], api=fake, cipher=foundation.CIPHER,
                                       config=foundation.CFG)) == "retained"
    assert ready_for(pg, SHOES) and fake.count("webhook_create") == 1


def test_uninstall_during_hold_tombstones_without_promotion(pg):
    fake, merchant = foundation.FakeShopify(), foundation.Merchant(pg, "gone")
    fake.create_errors = [transient()]
    assert refused(connect, pg, merchant, SHIRTS, fake) == lifecycle.C_WEBHOOK_PENDING
    fake.uninstall(SHIRTS)
    make_due(pg, SHIRTS)
    assert tick(pg, fake).get("uninstalled") == 1
    r = row(pg, SHIRTS)
    assert r["status"] == "uninstalled" and r["access_token_enc"] is None and proofs(pg, SHIRTS) == []


# ── Install-first ─────────────────────────────────────────────────────────────

def test_install_first_is_ready_only_after_the_proof(store):
    fake, browser = first.Fake(), secrets.token_hex(32)
    seen = {}

    def snapshot(_shop):
        with store.Session() as db:
            conn = db.query(lifecycle.ShopifyConnection).one()
            flow = db.query(Flow).filter(Flow.phase != "failed").one()
            seen.update(status=conn.status, reason=conn.quarantine_reason, phase=flow.phase,
                        flow_connection=flow.connection_id == conn.id, handoff=flow.handoff_hash,
                        ready=lifecycle.is_ready(db, conn))

    kind, state = first.online(store, fake, browser)
    fake.list_hook = snapshot
    kind, handle = first.callback(store, fake, browser, state)
    assert kind == "ready"
    assert seen == {"status": "quarantined", "reason": lifecycle.WEBHOOK_PENDING, "phase": "exchanging_offline",
                    "flow_connection": True, "handoff": None, "ready": False}
    [proof] = proofs(store, first.SHOP)
    assert proof["detail"]["uri"] == INSTALL_URI and proof["generation"] == 1
    assert len(exact_subscriptions(fake, first.SHOP, INSTALL_URI)) == 1
    session = first.redeem(store, fake, browser, handle)
    assert session["shop"] == first.SHOP


def test_install_first_failure_preserves_grant_and_login_never_exchanges_again(store):
    fake, browser = first.Fake(), secrets.token_hex(32)
    fake.create_errors = [transient()]
    state = first.online(store, fake, browser)[1]
    with pytest.raises(install.InstallRejected, match="install_pending"):
        first.callback(store, fake, browser, state)
    with store.Session() as db:
        assert db.query(Tenant).count() == db.query(User).count() == db.query(Owner).count() == 1
        flow = db.query(Flow).filter(Flow.phase == "failed").one()
        assert flow.connection_id is not None and flow.handoff_hash is None and flow.online_token_enc is None
    assert_held_with_grant(store, first.SHOP, first.CFG.encryption_key)
    assert fake.count("exchange") == 1
    # A new login proves the owner again but never authorizes (or exchanges) offline again.
    with pytest.raises(install.InstallRejected, match="install_pending"):
        first.callback(store, fake, browser, first.begin(store, browser))
    assert fake.count("exchange") == 1 and fake.count("owner") == 2
    assert row(store, first.SHOP)["reconcile_next_at"] <= lifecycle.utcnow()
    assert tick(store, fake, env=_install_runner_env()).get("retained") == 1
    assert ready_for(store, first.SHOP)
    kind, handle = first.online(store, fake, browser)
    assert kind == "ready" and fake.count("exchange") == 1
    assert first.redeem(store, fake, browser, handle)["shop"] == first.SHOP
    assert len(fake.webhooks(first.SHOP)) == 1


def _install_runner_env():
    """The recovery runner's environment for the install-first suite: the same
    client, key and API host as ``first.CFG``."""
    env = install_env()
    env.update({"SHOPIFY_CLIENT_ID": first.CFG.client_id, "SHOPIFY_CLIENT_SECRET": first.CFG.client_secret,
                "SHOPIFY_TOKEN_ENC_KEY": base64.urlsafe_b64encode(first.CFG.encryption_key).decode()})
    return env


def test_stored_grant_flow_is_not_an_uncertain_exchange_for_sweep(store):
    fake, browser = first.Fake(), secrets.token_hex(32)
    fake.create_errors = [transient()]
    state = first.online(store, fake, browser)[1]
    with pytest.raises(install.InstallRejected):
        first.callback(store, fake, browser, state)
    with store.Session() as db:   # a process that died between storing and proving
        flow = db.query(Flow).filter(Flow.connection_id.isnot(None)).one()
        flow.phase, flow.expires_at = "exchanging_offline", lifecycle.utcnow() - timedelta(seconds=1)
        db.commit()
        assert install.sweep_expired(db) == 1
    assert row(store, first.SHOP)["quarantine_reason"] == lifecycle.WEBHOOK_PENDING   # not "possible retirement"


def test_install_first_sessions_and_handoffs_require_current_generation_proof(store, monkeypatch):
    from core import auth, database
    fake, browser = first.Fake(), secrets.token_hex(32)
    session = first.redeem(store, fake, browser, first.installed(store, fake, browser))
    monkeypatch.setattr(database, "SessionLocal", store.Session)
    monkeypatch.setenv("NAHLA_SHOPIFY_CONNECTION_ENABLED", "1")
    monkeypatch.setenv("NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED", "1")
    assert auth.decode_token(session["access_token"])["tenant_id"] == session["tenant_id"]
    handle = first.installed(store, fake, browser)
    foundation._sql(store, "DELETE FROM shopify_connection_audit WHERE action = :a", a=lifecycle.WEBHOOK_VERIFIED)
    assert auth.decode_token(session["access_token"]) is None            # session validation: proof required
    with pytest.raises(install.InstallRejected):
        first.redeem(store, fake, browser, handle)                        # handoff: proof required
    assert row(store, first.SHOP)["status"] == "active"                   # (a pre-gate row until the runner holds it)


def test_install_first_reinstall_creates_and_proves_a_new_generation(store):
    from services.shopify_connection.webhooks import SignedShopIdentity
    fake, browser = first.Fake(), secrets.token_hex(32)
    first.redeem(store, fake, browser, first.installed(store, fake, browser))
    fake.uninstall(first.SHOP)
    with store.Session() as db:
        _, cid = lifecycle.record_uninstall(db, identity=SignedShopIdentity(
            int(foundation.GIDS[first.SHOP].split("/")[-1]), first.SHOP), webhook_id=secrets.token_hex(8),
            body_sha256="d" * 64, triggered_at_header=None)
        assert run(lifecycle.reconcile(db, connection_id=cid, api=fake, cipher=foundation.crypto.TokenCipher(
            first.CFG.encryption_key))) == "uninstalled"
    assert fake.webhooks(first.SHOP) == []
    first.redeem(store, fake, browser, first.installed(store, fake, browser))
    assert [p["generation"] for p in proofs(store, first.SHOP)] == [1, 3]
    assert fake.count("webhook_create") == 2 and len(fake.webhooks(first.SHOP)) == 1


def test_concurrent_first_install_waits_for_the_registering_flow(store):
    fake = first.Fake()
    browsers = [secrets.token_hex(32), secrets.token_hex(32)]
    states = [first.online(store, fake, b)[1] for b in browsers]
    hook, entered, release = foundation.barrier_hook()
    fake.list_hook = hook
    th, box = foundation.in_thread(first.callback, store, fake, browsers[0], states[0])
    assert entered.wait(20)
    fake.list_hook = None
    with pytest.raises(install.InstallRejected, match="install_busy"):
        first.callback(store, fake, browsers[1], states[1])   # the shop lease is held through registration
    release.set()
    th.join(30)
    assert box["value"][0] == "ready"
    kind, handle = first.callback(store, fake, browsers[1], states[1])
    assert kind == "ready" and fake.count("exchange") == 1 and len(fake.webhooks(first.SHOP)) == 1
