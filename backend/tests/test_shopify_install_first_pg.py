"""Actual PostgreSQL provisioning, ownership, replay, uninstall and concurrent installs."""
import asyncio
import importlib.util
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select, text

from backend.tests import test_shopify_connection_pg as foundation
from backend.tests.test_shopify_install_first import env, make_app, PILOT_CODE
from services.shopify_connection import install, lifecycle
from services.shopify_connection.install_config import configuration
from services.shopify_connection.install_models import ShopifyInstallFlow as Flow, ShopifyOwnerIdentity as Owner
from services.shopify_connection.oauth import OwnerGrant, VerifiedCallback
from models import Base, UserTotp, Tenant, User

pytestmark = foundation.pytestmark
template_db = foundation.template_db
pg = foundation.pg
CFG = configuration(env())
SHOP = foundation.SHOP_SHOES


@pytest.fixture
def store(pg, monkeypatch):
    from alembic.operations import Operations
    from alembic.migration import MigrationContext
    path = foundation._REPO / "database/migrations/versions/0124_shopify_install_first.py"
    spec = importlib.util.spec_from_file_location("install_migration", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    with pg.engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
        Base.metadata.create_all(conn, tables=[UserTotp.__table__])
    from backend.tests.test_shopify_install_first import pilot_env
    for key, value in pilot_env([SHOP, foundation.SHOP_SHIRTS, foundation.SHOP_PERFUME]).items():
        monkeypatch.setenv(key, value)
    return pg


class Fake(foundation.FakeShopify):
    owner_id = 42

    async def exchange_owner_code(self, *, shop_domain, code):
        with self.lock:
            if self.codes.pop(code, None) != shop_domain:
                raise RuntimeError("bad_test_code")
            access = "shpat_" + secrets.token_hex(24)
            self.valid_access[access] = shop_domain
            self.calls.append(("owner", shop_domain))
        return OwnerGrant(access, 86400, self.owner_id)


def begin(pg, browser, shop=SHOP):
    with pg.Session() as db:
        url = install.begin(db, shop=shop, browser=browser, config=CFG, pilot_code=PILOT_CODE)
    assert parse_qs(urlsplit(url).query)["grant_options[]"] == ["per-user"]
    return parse_qs(urlsplit(url).query)["state"][0]


def callback(pg, fake, browser, state, shop=SHOP, code=None):
    with pg.Session() as db:
        return asyncio.run(install.callback(db, verified=VerifiedCallback(shop, state, code or fake.issue_code(shop)),
                                           browser=browser, config=CFG, api=fake))


def online(pg, fake, browser, shop=SHOP):
    kind, value = callback(pg, fake, browser, begin(pg, browser, shop), shop)
    if kind == "authorize":
        assert "grant_options" not in value
        return kind, parse_qs(urlsplit(value).query)["state"][0]
    return kind, value


def installed(pg, fake, browser, shop=SHOP):
    kind, state = online(pg, fake, browser, shop)
    if kind == "ready":
        return state
    kind, handle = callback(pg, fake, browser, state, shop)
    assert kind == "ready"
    return handle


def redeem(pg, fake, browser, handle, **kwargs):
    with pg.Session() as db:
        return asyncio.run(install.redeem(db, browser=browser, handle=handle, config=CFG, api=fake, **kwargs))


def test_first_install_atomic_principal_and_single_use_login(store, monkeypatch):
    fake, browser = Fake(), secrets.token_hex(32)
    kind, state = online(store, fake, browser)
    with store.Session() as db:
        assert db.query(Tenant).count() == db.query(User).count() == 0

        flow = db.query(Flow).one()
        assert browser not in flow.browser_hash and flow.online_token_enc.startswith("sgcm1:")
    kind, handle = callback(store, fake, browser, state)
    session = redeem(store, fake, browser, handle)
    with store.Session() as db:
        user = db.query(User).one(); owner = db.query(Owner).one()
        assert user.tenant_id == session["tenant_id"] == owner.tenant_id
        assert user.password_hash is None and user.role == "merchant"
        assert user.email.endswith("@identity.invalid") and owner.shopify_user_id == 42
        assert db.query(Flow).one().online_token_enc is None
    with pytest.raises(install.InstallRejected):
        redeem(store, fake, browser, handle)
    from core import database, auth
    monkeypatch.setattr(database, "SessionLocal", store.Session)
    for key in ("NAHLA_SHOPIFY_CONNECTION_ENABLED", "NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED"):
        monkeypatch.setenv(key, "1")
    assert auth.decode_token(session["access_token"])["tenant_id"] == owner.tenant_id
    assert auth.decode_token_for_refresh(session["access_token"]) is None
    with store.Session() as db:
        db.get(User, owner.user_id).is_active = False; db.commit()
    assert auth.decode_token(session["access_token"]) is None


def test_browser_state_replay_expiry_and_wrong_shop_create_nothing(store):
    fake, browser = Fake(), secrets.token_hex(32)
    state = begin(store, browser)
    for wrong_browser, wrong_shop in ((browser, foundation.SHOP_SHIRTS), (secrets.token_hex(32), SHOP)):
        with pytest.raises(install.InstallRejected):
            callback(store, fake, wrong_browser, state, wrong_shop)
    with pytest.raises(install.InstallRejected):
        callback(store, fake, browser, state)
    assert fake.count("owner") == 0
    state = begin(store, browser)
    kind, value = callback(store, fake, browser, state)
    with pytest.raises(install.InstallRejected):
        callback(store, fake, browser, state)
    with store.Session() as db:
        db.query(Flow).filter(Flow.phase == "offline").one().expires_at = lifecycle.utcnow() - timedelta(seconds=1); db.commit()
    offline = parse_qs(urlsplit(value).query)["state"][0]
    with pytest.raises(install.InstallRejected):
        callback(store, fake, browser, offline)
    with store.Session() as db:
        assert db.query(Tenant).count() == 0
        assert install.sweep_expired(db) == 1
        assert all(flow.online_token_enc is None for flow in db.query(Flow).all())


def test_repeated_login_reuses_tenant_without_offline_rotation(store):
    fake, browser = Fake(), secrets.token_hex(32)
    a = redeem(store, fake, browser, installed(store, fake, browser))
    calls = fake.count("exchange")
    b = redeem(store, fake, browser, installed(store, fake, browser))
    assert a["tenant_id"] == b["tenant_id"] and a["user_id"] == b["user_id"]
    assert fake.count("exchange") == calls == 1
    with store.Session() as db:
        assert db.query(Tenant).count() == db.query(User).count() == 1


def test_two_shops_isolate_sessions_handoffs_and_owner_mapping(store):
    fake = Fake()
    browser_a, browser_b = secrets.token_hex(32), secrets.token_hex(32)
    handle_a = installed(store, fake, browser_a)
    handle_b = installed(store, fake, browser_b, foundation.SHOP_SHIRTS)
    with pytest.raises(install.InstallRejected):
        redeem(store, fake, browser_b, handle_a)
    a = redeem(store, fake, browser_a, handle_a)
    b = redeem(store, fake, browser_b, handle_b)
    assert a["tenant_id"] != b["tenant_id"] and a["user_id"] != b["user_id"]
    fake.owner_id = 99
    with pytest.raises(install.InstallRejected):
        installed(store, fake, secrets.token_hex(32))
    with store.Session() as db:
        assert db.query(Tenant).count() == 2


def test_existing_unmapped_tenant_never_adopted(store):
    fake = Fake()
    merchant = foundation.Merchant(store, "existing")
    foundation.connect(store, merchant, SHOP, fake)
    with pytest.raises(install.InstallRejected):
        installed(store, fake, secrets.token_hex(32))
    with store.Session() as db:
        assert db.query(Owner).count() == 0 and db.query(Tenant).count() == 1


def test_concurrent_first_installs_only_one_tenant_and_offline_grant(store):
    fake = Fake()
    browsers = [secrets.token_hex(32), secrets.token_hex(32)]
    states = [online(store, fake, b)[1] for b in browsers]
    def complete(index):
        try:
            return callback(store, fake, browsers[index], states[index])
        except install.InstallRejected as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(complete, (0, 1)))
    for index, result in enumerate(results):
        if result == "install_busy":
            results[index] = complete(index)
    assert all(isinstance(r, tuple) and r[0] == "ready" for r in results)
    sessions = [redeem(store, fake, browsers[i], results[i][1]) for i in range(2)]
    assert sessions[0]["tenant_id"] == sessions[1]["tenant_id"]
    assert fake.count("exchange") == 1
    with store.Session() as db:
        assert db.query(Tenant).count() == db.query(User).count() == 1


def test_failed_offline_exchange_does_not_provision(store):
    fake, browser = Fake(), secrets.token_hex(32)
    state = online(store, fake, browser)[1]
    with pytest.raises(install.InstallRejected):
        callback(store, fake, browser, state, code="never-issued")
    with store.Session() as db:
        assert db.query(Tenant).count() == db.query(User).count() == 0
        assert db.query(Flow).one().phase == "failed"


def test_uninstall_revokes_session_and_reinstall_reuses_identity(store, monkeypatch):
    fake, browser = Fake(), secrets.token_hex(32)
    session = redeem(store, fake, browser, installed(store, fake, browser))
    from core import auth, database
    from services.shopify_connection.webhooks import SignedShopIdentity
    monkeypatch.setattr(database, "SessionLocal", store.Session)
    monkeypatch.setenv("NAHLA_SHOPIFY_CONNECTION_ENABLED", "1")
    monkeypatch.setenv("NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED", "1")
    fake.uninstall(SHOP)
    with store.Session() as db:
        _, cid = lifecycle.record_uninstall(db, identity=SignedShopIdentity(int(foundation.GIDS[SHOP].split("/")[-1]), SHOP),
                                            webhook_id=secrets.token_hex(16), body_sha256="a" * 64, triggered_at_header=None)
    assert auth.decode_token(session["access_token"]) is None
    with store.Session() as db:
        asyncio.run(lifecycle.reconcile(db, connection_id=cid, api=fake, cipher=foundation.crypto.TokenCipher(CFG.encryption_key)))
    handle = installed(store, fake, browser)
    new = redeem(store, fake, browser, handle)
    assert new["tenant_id"] == session["tenant_id"] and new["user_id"] == session["user_id"]
    assert auth.decode_token(session["access_token"]) is None


def test_totp_is_required_and_code_cannot_replay(store, monkeypatch):
    import pyotp
    from core.totp_crypto import encrypt_secret
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    fake, browser = Fake(), secrets.token_hex(32)
    first = redeem(store, fake, browser, installed(store, fake, browser))
    secret = pyotp.random_base32()
    with store.Session() as db:
        db.add(UserTotp(user_id=first["user_id"], secret_enc=encrypt_secret(secret), confirmed_at=lifecycle.utcnow()))
        db.commit()
    handle = installed(store, fake, browser)
    with pytest.raises(install.InstallRejected, match="totp_required"):
        redeem(store, fake, browser, handle)
    code = pyotp.TOTP(secret).now()
    assert redeem(store, fake, browser, handle, totp=code)["user_id"] == first["user_id"]
    another = installed(store, fake, browser)
    with pytest.raises(install.InstallRejected, match="totp_invalid"):
        redeem(store, fake, browser, another, totp=code)


def test_delayed_offline_flow_cannot_extend_owner_proof(store, monkeypatch):
    fake, browser = Fake(), secrets.token_hex(32)
    started = lifecycle.utcnow()
    state = online(store, fake, browser)[1]
    monkeypatch.setattr(lifecycle, "utcnow", lambda: started + timedelta(seconds=301))
    with pytest.raises(install.InstallRejected):
        callback(store, fake, browser, state)
    assert fake.count("exchange") == 0
    with store.Session() as db:
        assert db.query(Tenant).count() == 0


def test_late_offline_grant_loses_lease_and_cannot_create_tenant(store):
    fake, browser = Fake(), secrets.token_hex(32)
    state = online(store, fake, browser)[1]
    def steal(_shop):
        with store.engine.begin() as conn:
            conn.execute(text("UPDATE shopify_shop_leases SET lease_id = 'replacement-lease' WHERE shop_domain = :shop"), {"shop": SHOP})
    fake.exchange_hook = steal
    with pytest.raises(install.InstallRejected):
        callback(store, fake, browser, state)
    with store.Session() as db:
        assert db.query(Tenant).count() == db.query(User).count() == 0
        assert db.query(Flow).one().online_token_enc is None


def test_actual_asgi_start_cookie_cancelled_and_copied_callback(store, monkeypatch):
    import httpx
    import time
    from core import middleware
    from routers import shopify_install as routes
    from services.shopify_connection.install_config import PREFIX, CALLBACK, COOKIE
    from services.shopify_connection.oauth import compute_query_hmac
    fake, browser = Fake(), secrets.token_hex(32)
    monkeypatch.setattr(middleware, "API_SECRET_KEY", "synthetic-service-key")
    monkeypatch.setattr(routes, "enabled", lambda: True)
    monkeypatch.setattr(routes, "configuration", lambda: CFG)
    monkeypatch.setattr(routes, "_api", lambda config: fake)
    app = make_app(store.Session)
    async def proof():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://api.example.test") as client:
            refused = await client.post(PREFIX + "/start", data={"shop": SHOP, "browser": browser, "pilot_code": PILOT_CODE}, headers={"Origin": "null"})
            assert refused.status_code == 400
            start = await client.post(PREFIX + "/start", data={"shop": SHOP, "browser": browser, "pilot_code": PILOT_CODE}, headers={"Origin": "https://app.example.test"})
            assert start.status_code == 303
            cookie = start.headers["set-cookie"]
            assert all(value in cookie for value in (COOKIE, "Secure", "HttpOnly", "SameSite=lax", "Path=/"))
            assert "Domain=" not in cookie
            state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
            cancelled = await client.get(CALLBACK, params={"shop": SHOP, "state": state, "error": "access_denied"})
            assert cancelled.status_code == 303 and "install_error=refused" in cancelled.headers["location"]
            params = {"shop": SHOP, "state": state, "code": fake.issue_code(SHOP), "timestamp": str(int(time.time()))}
            params["hmac"] = compute_query_hmac(params, CFG.client_secret)
            client.cookies.clear()
            copied = await client.get(CALLBACK, params=params)
            assert "install_error=refused" in copied.headers["location"]
            assert fake.count("owner") == 0
    asyncio.run(proof())
    with store.Session() as db:
        assert db.query(Tenant).count() == db.query(User).count() == 0


def test_sixty_pilot_starts_are_bounded_despite_forwarded_headers(store, monkeypatch):
    import httpx
    from routers import shopify_install as routes
    monkeypatch.setattr(routes, "enabled", lambda: True)
    monkeypatch.setattr(routes, "configuration", lambda: CFG)
    app = make_app(store.Session)
    async def proof():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://api.example.test") as client:
            statuses = []
            for index in range(60):
                response = await client.post(routes.PREFIX + "/start",
                    data={"shop": SHOP, "browser": secrets.token_hex(32), "pilot_code": PILOT_CODE},
                    headers={"Origin": "https://app.example.test", "X-Real-IP": f"192.0.2.{index}",
                             "X-Forwarded-For": f"198.51.100.{index}"})
                statuses.append(response.status_code)
            assert statuses.count(303) == install.MAX_SHOP_STARTS
            assert statuses.count(429) == 60 - install.MAX_SHOP_STARTS
    asyncio.run(proof())
    with store.Session() as db:
        assert db.query(Flow).count() == install.MAX_SHOP_STARTS
        # Expiry must remove retained rows, not turn them into permanent tombstones.
        future = lifecycle.utcnow() + timedelta(seconds=install.FLOW_SECONDS + 1)
        assert install.sweep_expired(db, now=future) == install.MAX_SHOP_STARTS
        assert install.sweep_expired(db, now=future) == 0
        assert db.query(Flow).count() == 0


def test_browser_and_global_quotas_cannot_be_refunded_by_failed_callbacks(store, monkeypatch):
    fake, browser = Fake(), secrets.token_hex(32)
    for index in range(install.MAX_BROWSER_STARTS):
        shop = (SHOP, foundation.SHOP_SHIRTS, foundation.SHOP_PERFUME)[index % 3]
        state = begin(store, browser, shop)
        with pytest.raises(install.InstallRejected):
            callback(store, fake, secrets.token_hex(32), state, shop)
    with pytest.raises(install.InstallRejected, match="install_rate_limited"):
        begin(store, browser, foundation.SHOP_SHIRTS)
    monkeypatch.setattr(install, "MAX_GLOBAL_STARTS_PER_MINUTE", install.MAX_BROWSER_STARTS)
    with pytest.raises(install.InstallRejected, match="install_rate_limited"):
        begin(store, secrets.token_hex(32), foundation.SHOP_SHIRTS)
    with store.Session() as db:
        assert db.query(Flow).count() == install.MAX_BROWSER_STARTS


def test_admission_lock_is_nonblocking_and_concurrent_global_cap_is_atomic(store, monkeypatch):
    from sqlalchemy import func
    with store.Session() as db:
        assert db.scalar(select(func.pg_try_advisory_xact_lock(*install.ADMISSION_LOCK)))
        with pytest.raises(install.InstallRejected, match="install_busy"):
            begin(store, secrets.token_hex(32))
        db.rollback()
    monkeypatch.setattr(install, "MAX_GLOBAL_STARTS_PER_MINUTE", 3)
    def attempt(index):
        try:
            begin(store, secrets.token_hex(32), (SHOP, foundation.SHOP_SHIRTS, foundation.SHOP_PERFUME)[index % 3])
            return "ok"
        except install.InstallRejected as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(24)))
    assert set(results) <= {"ok", "install_busy", "install_rate_limited"}
    for index in range(24, 28):
        attempt(index)
    with store.Session() as db:
        assert db.query(Flow).count() == 3


def test_retention_cap_and_cleanup_never_evict_live_flow_or_identity(store, monkeypatch):
    fake, browser = Fake(), secrets.token_hex(32)
    session = redeem(store, fake, browser, installed(store, fake, browser))
    begin(store, secrets.token_hex(32), foundation.SHOP_SHIRTS)
    monkeypatch.setattr(install, "MAX_RETAINED_FLOWS", 2)
    with pytest.raises(install.InstallRejected, match="install_rate_limited"):
        begin(store, secrets.token_hex(32), foundation.SHOP_PERFUME)
    now = lifecycle.utcnow()
    with store.Session() as db:
        # Cleanup removes only expired records, leaving ownership tombstones intact.
        assert install.sweep_expired(db, now=now) == 0
        assert db.query(Flow).filter(Flow.phase == "redeemed").count() == 1
        assert install.sweep_expired(db, now=now + timedelta(seconds=901), limit=1) == 1
        assert db.query(Flow).count() == 1
        assert install.sweep_expired(db, now=now + timedelta(seconds=901), limit=1) == 1
        assert db.query(Flow).count() == 0
        assert db.query(Owner).one().tenant_id == session["tenant_id"]
        assert db.query(lifecycle.ShopifyConnection).count() == 1


@pytest.mark.parametrize("column", ["name", "domain"])
def test_legacy_tenant_name_or_domain_collision_never_adopts_other_tenant(store, column):
    legacy = foundation.Merchant(store, "legacy-tenant")
    with store.Session() as db:
        tenant = db.get(Tenant, legacy.tenant_id)
        setattr(tenant, column, SHOP)
        db.commit()
    fake, browser = Fake(), secrets.token_hex(32)
    result = redeem(store, fake, browser, installed(store, fake, browser))
    assert result["tenant_id"] != legacy.tenant_id
    with store.Session() as db:
        assert getattr(db.get(Tenant, legacy.tenant_id), column) == SHOP
        assert db.get(Tenant, result["tenant_id"]).domain is None
        assert db.query(Owner).one().tenant_id == result["tenant_id"]
        assert db.query(Tenant).count() == 2


@pytest.mark.parametrize("column", ["username", "email"])
def test_internal_principal_collision_retries_without_adoption(store, monkeypatch, column):
    legacy = foundation.Merchant(store, "legacy-user")
    subject = "shopify-collision@identity.invalid"
    with store.Session() as db:
        user = db.get(User, legacy.user_id)
        setattr(user, column, subject)
        db.commit()
    candidates = iter([subject, "shopify-new-random-subject@identity.invalid"])
    monkeypatch.setattr(install, "_internal_subject", lambda *args: next(candidates))
    fake, browser = Fake(), secrets.token_hex(32)
    result = redeem(store, fake, browser, installed(store, fake, browser))
    assert result["tenant_id"] != legacy.tenant_id and result["user_id"] != legacy.user_id
    with store.Session() as db:
        assert getattr(db.get(User, legacy.user_id), column) == subject
        assert db.query(Tenant).count() == db.query(User).count() == 2


def test_wrong_browser_replay_cannot_burn_ready_or_redeemed_flow(store):
    fake, browser = Fake(), secrets.token_hex(32)
    state = online(store, fake, browser)[1]
    _, handle = callback(store, fake, browser, state)
    for phase in ("ready", "redeemed"):
        with pytest.raises(install.InstallRejected):
            callback(store, fake, secrets.token_hex(32), state)
        with store.Session() as db:
            assert db.query(Flow).one().phase == phase
            assert db.query(lifecycle.ShopifyConnection).one().status == "active"
        if phase == "ready":
            redeem(store, fake, browser, handle)


@pytest.mark.parametrize("response_kind", ["offline", "timeout", "staff", "rejected"])
def test_online_exchange_ambiguity_is_quarantined_but_definitive_rejection_is_not(store, response_kind):
    import httpx
    from backend.tests.test_shopify_install_first import owner_body
    from services.shopify_connection.oauth import ShopifyApi
    fake, browser = Fake(), secrets.token_hex(32)
    redeem(store, fake, browser, installed(store, fake, browser))
    body = owner_body()
    if response_kind == "offline":
        body.pop("associated_user"); body.pop("associated_user_scope")
        body["refresh_token"] = "synthetic-offline-refresh"
    elif response_kind == "staff":
        body["associated_user"]["account_owner"] = False
    def transport(request):
        if response_kind == "timeout":
            raise httpx.ReadTimeout("synthetic response lost")
        return httpx.Response(400 if response_kind == "rejected" else 200, json=body)
    api = ShopifyApi(client_id="synthetic", client_secret="synthetic", api_version="2026-07", transport=httpx.MockTransport(transport))
    fake.exchange_owner_code = api.exchange_owner_code
    with pytest.raises(install.InstallRejected):
        callback(store, fake, browser, begin(store, browser))
    with store.Session() as db:
        conn = db.query(lifecycle.ShopifyConnection).one()
        assert conn.status == ("quarantined" if response_kind in {"offline", "timeout"} else "active")
        assert db.query(Tenant).count() == db.query(Owner).count() == 1


def test_pre_dispatch_lease_refusal_does_not_quarantine_and_crash_cleanup_does(store, monkeypatch):
    fake, browser = Fake(), secrets.token_hex(32)
    redeem(store, fake, browser, installed(store, fake, browser))
    owner_calls = fake.count("owner")
    async def refuse_before_dispatch(*args):
        raise lifecycle._LeaseLost()
    monkeypatch.setattr(lifecycle, "_guarded_call", refuse_before_dispatch)
    with pytest.raises(install.InstallRejected):
        callback(store, fake, browser, begin(store, browser))
    assert fake.count("owner") == owner_calls
    with store.Session() as db:
        assert db.query(lifecycle.ShopifyConnection).one().status == "active"
        flow = db.query(Flow).filter(Flow.phase == "failed").one()
        flow.phase = "exchanging_online"  # Process died with an unknown exchange outcome.
        flow.expires_at = lifecycle.utcnow() - timedelta(seconds=1)
        db.commit()
        install.sweep_expired(db)
        assert db.query(lifecycle.ShopifyConnection).one().status == "quarantined"


def test_offline_pre_dispatch_refusal_does_not_quarantine_recovered_connection(store, monkeypatch):
    fake, browser = Fake(), secrets.token_hex(32)
    redeem(store, fake, browser, installed(store, fake, browser))
    with store.Session() as db:
        db.query(lifecycle.ShopifyConnection).one().status = "quarantined"
        db.commit()
    state = online(store, fake, browser)[1]
    with store.Session() as db:
        db.query(lifecycle.ShopifyConnection).one().status = "active"
        db.commit()
    original = lifecycle._guarded_call
    calls = 0
    async def lose_before_exchange(db, held, call):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise lifecycle._LeaseLost()
        return await original(db, held, call)
    monkeypatch.setattr(lifecycle, "_guarded_call", lose_before_exchange)
    with pytest.raises(install.InstallRejected):
        callback(store, fake, browser, state)
    assert fake.count("exchange") == 1 and calls == 2
    with store.Session() as db:
        assert db.query(lifecycle.ShopifyConnection).one().status == "active"


def _expired_flow(now, *, phase="offline", recent=False):
    return Flow(id=secrets.token_hex(24), state_hash=secrets.token_hex(32), browser_hash=secrets.token_hex(32),
                shop_domain=SHOP, redirect_hash=secrets.token_hex(32), phase=phase, generation=0,
                online_token_enc=None if phase in {"redeemed", "failed"} else "synthetic-ciphertext-to-erase",
                handoff_hash=secrets.token_hex(32), expires_at=now - timedelta(seconds=1),
                created_at=now - timedelta(seconds=60 if recent else 901))


@pytest.mark.parametrize("gate", ["connection_off", "install_off", "missing_keys"])
def test_gate_off_cleanup_erases_only_expired_flow_secrets_and_preserves_uncertainty(store, monkeypatch, gate):
    from sqlalchemy import event
    from services.shopify_connection import install_cleanup, recovery, crypto
    fake, browser = Fake(), secrets.token_hex(32)
    redeem(store, fake, browser, installed(store, fake, browser))
    now = lifecycle.utcnow()
    with store.Session() as db:
        for phase in ("online", "offline", "ready", "redeeming", "redeemed", "failed", "exchanging_online", "exchanging_offline"):
            db.add(_expired_flow(now, phase=phase))
        recent = _expired_flow(now, recent=True)
        db.add(recent); db.commit()
        recent_id = recent.id
        connection = db.query(lifecycle.ShopifyConnection).one()
        before = {col.name: getattr(connection, col.name) for col in connection.__table__.columns}
        live_id = db.query(Flow).filter(Flow.phase == "redeemed", Flow.expires_at > now).one().id
    config = env()
    if gate == "connection_off":
        config["NAHLA_SHOPIFY_CONNECTION_ENABLED"] = "0"
    elif gate == "install_off":
        config["NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED"] = "0"
    else:
        for key in ("SHOPIFY_CLIENT_SECRET", "SHOPIFY_TOKEN_ENC_KEY"):
            config.pop(key); monkeypatch.delenv(key, raising=False)
    for key, value in config.items():
        monkeypatch.setenv(key, value)
    statements = []
    def observe(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP")):
            statements.append(statement)
    event.listen(store.engine, "before_cursor_execute", observe)
    def forbidden(*args, **kwargs):
        raise AssertionError("cleanup touched activation, credentials, or network")
    try:
        with monkeypatch.context() as m:
            m.setattr(crypto, "TokenCipher", forbidden)
            m.setattr(recovery, "_api_for", forbidden)
            m.setattr(lifecycle, "_flag_possible_retirement", forbidden)
            assert asyncio.run(install_cleanup.run_cleanup_tick(session_factory=store.Session, now=now)) == {"erased": 9}
            assert asyncio.run(install_cleanup.run_cleanup_tick(session_factory=store.Session, now=now)) == {"erased": 0}
    finally:
        event.remove(store.engine, "before_cursor_execute", observe)
    assert statements and all(statement.startswith(("UPDATE shopify_install_flows", "DELETE FROM shopify_install_flows")) for statement in statements)
    with store.Session() as db:
        assert db.query(Flow).count() == 4  # live session, recent quota history, two uncertainty markers
        assert db.get(Flow, live_id).phase == "redeemed"
        assert db.get(Flow, recent_id).phase == "failed"
        for row in db.query(Flow).filter(Flow.expires_at <= now):
            assert row.online_token_enc is row.handoff_hash is None
        conn = db.query(lifecycle.ShopifyConnection).one()
        assert {col.name: getattr(conn, col.name) for col in conn.__table__.columns} == before
        assert db.query(Tenant).count() == db.query(User).count() == db.query(Owner).count() == 1
        # Later active recovery can still see the marker and quarantine the incumbent.
        assert install.sweep_expired(db, now=now) == 2
        assert db.query(lifecycle.ShopifyConnection).one().status == "quarantined"


def test_cleanup_missing_revision_never_creates_schema_or_calls_api(pg, monkeypatch):
    from sqlalchemy import inspect
    from services.shopify_connection import install_cleanup
    before = set(inspect(pg.engine).get_table_names())
    assert Flow.__tablename__ not in before
    for key in env():
        monkeypatch.delenv(key, raising=False)
    assert asyncio.run(install_cleanup.run_cleanup_tick(session_factory=pg.Session)) == {"skipped": "storage_unavailable"}
    assert set(inspect(pg.engine).get_table_names()) == before


def test_cleanup_skips_locked_rows_and_overlapping_workers_are_bounded(store):
    from services.shopify_connection import install_cleanup
    now = lifecycle.utcnow()
    with store.Session() as db:
        rows = [_expired_flow(now) for _ in range(5)]
        db.add_all(rows); db.commit()
        first_id = min(row.id for row in rows)
    with store.Session() as locked:
        locked.get(Flow, first_id, with_for_update=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            counts = list(pool.map(lambda _: asyncio.run(install_cleanup.run_cleanup_tick(
                session_factory=store.Session, now=now, limit=2))["erased"], range(2)))
        assert counts == [2, 2]
        with store.Session() as db:
            assert db.query(Flow).count() == 1 and db.get(Flow, first_id).online_token_enc is not None
        locked.rollback()
    assert asyncio.run(install_cleanup.run_cleanup_tick(session_factory=store.Session, now=now)) == {"erased": 1}


def test_scrubbed_uncertainty_markers_do_not_starve_later_cleanup(store):
    from services.shopify_connection import install_cleanup
    now = lifecycle.utcnow()
    with store.Session() as db:
        for _ in range(102):
            row = _expired_flow(now, phase="exchanging_online")
            row.online_token_enc = row.handoff_hash = None
            row.expires_at -= timedelta(seconds=10)
            db.add(row)
        db.add(_expired_flow(now)); db.commit()
    assert asyncio.run(install_cleanup.run_cleanup_tick(session_factory=store.Session, now=now)) == {"erased": 1}
    with store.Session() as db:
        assert db.query(Flow).count() == 102


def test_anonymous_flood_cannot_consume_pilot_quota_and_admitted_shop_cannot_consume_peer_reserve(store, monkeypatch):
    # Wrong secrets and a valid code replayed to an unlisted shop never insert rows.
    with store.Session() as db:
        for index in range(60):
            with pytest.raises(install.InstallRejected, match="install_admission_required"):
                install.begin(db, shop=SHOP if index % 2 else f"unknown-{index}.myshopify.com",
                              browser=secrets.token_hex(32), config=CFG,
                              pilot_code=secrets.token_hex(32) if index % 2 else PILOT_CODE)
        assert db.query(Flow).count() == 0
    monkeypatch.setattr(install, "MAX_SHOP_RETAINED_FLOWS", 2)
    for _ in range(2):
        begin(store, secrets.token_hex(32))
    with pytest.raises(install.InstallRejected, match="install_rate_limited"):
        begin(store, secrets.token_hex(32))
    begin(store, secrets.token_hex(32), foundation.SHOP_SHIRTS)
    with store.Session() as db:
        assert db.query(Flow).filter(Flow.shop_domain == SHOP).count() == 2
        assert db.query(Flow).filter(Flow.shop_domain == foundation.SHOP_SHIRTS).count() == 1
        # No pilot admission credential/hash is persisted in a flow.
        assert PILOT_CODE not in repr([{col.name: getattr(row, col.name) for col in Flow.__table__.columns} for row in db.query(Flow)])
