"""The catalog import's shop bound on REAL PostgreSQL.

While the install-first pilot is on, an import run may start only for a shop
the bound (``NAHLA_SHOPIFY_CATALOG_IMPORT_SHOPS``) admits; the S4_PILOT
release contract pins that bound to the one pilot shop. A refusal happens
before any database work. Reuses the catalog suite's disposable databases,
synthetic merchants and in-process Shopify fake (no network, no credential).
Skipped (never passed) without ``NAHLA_RELIABILITY_PG_ADMIN_DSN``.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from backend.tests import test_shopify_catalog_import_pg as c
from services.shopify_catalog import config as C
from services.shopify_catalog import store

pytestmark = c.pytestmark
template_db = c.template_db
pg = c.pg

PILOT = ("nahlah-dev.myshopify.com", "gid://shopify/Shop/7100000099")


def _bound(*shops, hours=1):
    deadline = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps({shop: {"expires_at": deadline} for shop in shops})


def _pilot_env(bound=None):
    env = {**c.ENV, C.INSTALL_FIRST_ENV: "true"}
    if bound is not None:
        env[C.IMPORT_SHOPS_ENV] = bound
    return env


def _writes_during(pg, fn):
    statements = []

    def observe(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "SELECT")):
            statements.append(statement)

    event.listen(pg.engine, "before_cursor_execute", observe)
    try:
        return fn(), statements
    except store.StartRefused as exc:
        return exc.code, statements
    finally:
        event.remove(pg.engine, "before_cursor_execute", observe)


def _begin(pg, merchant, fake, env, actor=None):
    actor = actor or merchant.actor(pg)
    with pg.Session() as db:
        return store.begin_run(db, actor=actor, shop_domain=fake.shop_domain, env=env)


@pytest.mark.parametrize("bound", [None, "", "{", _bound("other-store.myshopify.com")])
def test_pilot_refuses_a_shop_outside_the_bound_before_any_database_work(pg, bound):
    merchant = c.Merchant(pg, "outside")
    fake = c.shoe_store()
    c.connect(pg, merchant, fake)
    actor = merchant.actor(pg)   # the caller's revalidation; only begin_run is observed
    code, statements = _writes_during(pg, lambda: _begin(pg, merchant, fake, _pilot_env(bound), actor))
    assert code == store.S_SHOP_NOT_ADMITTED
    assert statements == []                                      # not even a read
    assert c._sql(pg, "SELECT count(*) AS n FROM shopify_catalog_import_runs")[0]["n"] == 0


def test_pilot_bound_admits_only_the_pilot_shop_and_its_import_commits(pg):
    pilot_merchant, other_merchant = c.Merchant(pg, "pilot"), c.Merchant(pg, "other")
    pilot_shop, other_shop = c.shoe_store(PILOT), c.shoe_store()
    c.connect(pg, pilot_merchant, pilot_shop)
    c.connect(pg, other_merchant, other_shop)
    env = _pilot_env(_bound(PILOT[0]))
    assert _writes_during(pg, lambda: _begin(pg, other_merchant, other_shop, env))[0] == store.S_SHOP_NOT_ADMITTED
    handle = _begin(pg, pilot_merchant, pilot_shop, env)
    assert c.execute(pg, handle, pilot_shop) == "committed"
    runs = c._sql(pg, "SELECT shop_domain, status FROM shopify_catalog_import_runs")
    assert [(r["shop_domain"], r["status"]) for r in runs] == [(PILOT[0], "committed")]
    # The mirror holds the pilot shop's rows only.
    owners = c._sql(pg, "SELECT DISTINCT tenant_id FROM shopify_catalog_products")
    assert [r["tenant_id"] for r in owners] == [pilot_merchant.tenant_id]


def test_an_expired_bound_admits_nothing(pg):
    merchant = c.Merchant(pg, "expired")
    pilot_shop = c.shoe_store(PILOT)
    c.connect(pg, merchant, pilot_shop)
    code, _ = _writes_during(pg, lambda: _begin(pg, merchant, pilot_shop, _pilot_env(_bound(PILOT[0], hours=-1))))
    assert code == store.S_SHOP_NOT_ADMITTED


def test_http_start_answers_403_outside_the_bound_and_202_for_the_pilot_shop(pg, monkeypatch):
    pilot_merchant, other_merchant = c.Merchant(pg, "http-pilot"), c.Merchant(pg, "http-other")
    pilot_shop, other_shop = c.shoe_store(PILOT), c.shoe_store()
    c.connect(pg, pilot_merchant, pilot_shop)
    c.connect(pg, other_merchant, other_shop)
    jobs = []

    async def job(handle):
        jobs.append(handle.run_id)

    monkeypatch.setattr(c.importer_module, "run_import_job", job)
    env = _pilot_env(_bound(PILOT[0]))
    base = "/merchant/integrations/shopify/catalog-import"
    other = c._http(pg, monkeypatch, other_merchant.payload(), env=env)
    resp = other.post(f"{base}/start", json={"shop": other_shop.shop_domain})
    assert (resp.status_code, resp.json()) == (403, {"detail": {"error": "shop_not_admitted"}})
    pilot = c._http(pg, monkeypatch, pilot_merchant.payload(), env=env)
    assert pilot.post(f"{base}/start", json={"shop": PILOT[0]}).status_code == 202
    assert len(jobs) == 1
