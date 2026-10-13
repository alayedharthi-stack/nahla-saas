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
from backend.tests.test_shopify_install_first import env, make_app
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
def store(pg):
    from alembic.operations import Operations
    from alembic.migration import MigrationContext
    path = foundation._REPO / "database/migrations/versions/0124_shopify_install_first.py"
    spec = importlib.util.spec_from_file_location("install_migration", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    with pg.engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
        Base.metadata.create_all(conn, tables=[UserTotp.__table__])
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
        url = install.begin(db, shop=shop, browser=browser, config=CFG)
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
    for wrong_browser, wrong_shop in ((secrets.token_hex(32), SHOP), (browser, foundation.SHOP_SHIRTS)):
        with pytest.raises(install.InstallRejected):
            callback(store, fake, wrong_browser, state, wrong_shop)
    kind, value = callback(store, fake, browser, state)
    with pytest.raises(install.InstallRejected):
        callback(store, fake, browser, state)
    with store.Session() as db:
        db.query(Flow).one().expires_at = lifecycle.utcnow() - timedelta(seconds=1); db.commit()
    offline = parse_qs(urlsplit(value).query)["state"][0]
    with pytest.raises(install.InstallRejected):
        callback(store, fake, browser, offline)
    with store.Session() as db:
        assert db.query(Tenant).count() == 0
        assert install.sweep_expired(db) == 1
        assert db.query(Flow).one().online_token_enc is None


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
            refused = await client.post(PREFIX + "/start", data={"shop": SHOP, "browser": browser}, headers={"Origin": "null"})
            assert refused.status_code == 400
            start = await client.post(PREFIX + "/start", data={"shop": SHOP, "browser": browser}, headers={"Origin": "https://app.example.test"})
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
