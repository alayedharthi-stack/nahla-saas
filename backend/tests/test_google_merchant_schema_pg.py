"""
tests/test_google_merchant_schema_pg.py
───────────────────────────────────────
PostgreSQL proofs of the dormant Google Merchant schema (revision 0124,
``services/google_merchant_models.py``). Every invariant is exercised with
explicit SQL against a disposable database built by the revision itself (plus
``tenants`` from ``models.Base`` as the foreign-key target) — not through ORM
conventions. All identifiers are synthetic.

Without ``NAHLA_RELIABILITY_PG_ADMIN_DSN`` the module is skipped (reported as
skipped, never as passed); with ``NAHLA_RELIABILITY_REQUIRE_PG=1`` and no DSN
it errors instead.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import secrets
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

_REPO = Path(__file__).resolve().parents[2]
for _p in (_REPO, _REPO / "backend", _REPO / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

ADMIN_URL = (os.environ.get("NAHLA_RELIABILITY_PG_ADMIN_DSN") or "").strip()
if not ADMIN_URL and os.environ.get("NAHLA_RELIABILITY_REQUIRE_PG") == "1":
    raise RuntimeError("NAHLA_RELIABILITY_REQUIRE_PG=1 but NAHLA_RELIABILITY_PG_ADMIN_DSN is not set")

# Disposable local servers only: the DSN must name a loopback host, and neither
# its query nor the libpq environment may redirect the connection elsewhere.
# Checked at import, before any database is created; the DSN is never echoed.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_REDIRECTS = frozenset({"host", "hostaddr", "service"})


def _refuse_non_loopback(dsn: str, environ) -> None:
    url = make_url(dsn)
    if (url.host not in _LOOPBACK_HOSTS or _REDIRECTS & set(url.query)
            or environ.get("PGHOSTADDR") or environ.get("PGSERVICE")):
        raise RuntimeError("NAHLA_RELIABILITY_PG_ADMIN_DSN must target a disposable loopback PostgreSQL")


if ADMIN_URL:
    _refuse_non_loopback(ADMIN_URL, os.environ)
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="NAHLA_RELIABILITY_PG_ADMIN_DSN not set (no real PostgreSQL)")

from services.google_merchant_models import (  # noqa: E402
    GOOGLE_MERCHANT_GUARD_FUNCTIONS,
    GOOGLE_MERCHANT_TABLE_NAMES,
    GoogleMerchantBase,
    create_google_merchant_tables,
)

_MIGRATION_PATH = _REPO / "database/migrations/versions/0124_google_merchant_ledger.py"
CONN = "google_merchant_connections"
LEDGER = "google_merchant_offer_ledger"

# Synthetic identifiers only.
ACCOUNT_A, ACCOUNT_B = "700100", "700200"
SOURCE_A, SOURCE_B = "880011", "880022"

UNIQUE = "23505"
FOREIGN_KEY = "23503"
NOT_NULL = "23502"
CHECK = "23514"
RESTRICT = "23001"
SERIALIZATION = "40001"
STRING_TOO_LONG = "22001"


# ── Database fixtures ─────────────────────────────────────────────────────────

def _admin(*statements: str) -> None:
    eng = create_engine(ADMIN_URL, poolclass=NullPool, isolation_level="AUTOCOMMIT", future=True)
    try:
        with eng.connect() as conn:
            for stmt in statements:
                conn.execute(text(stmt))
    finally:
        eng.dispose()


def _url(db: str) -> str:
    return make_url(ADMIN_URL).set(drivername="postgresql+psycopg2", database=db).render_as_string(hide_password=False)


def _load_migration():
    spec = importlib.util.spec_from_file_location("google_merchant_migration_0124", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(conn, direction: str = "upgrade") -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = _load_migration()
    with Operations.context(MigrationContext.configure(conn)):
        getattr(module, direction)()


def _base_tables(conn) -> None:
    from models import Base, Tenant

    Base.metadata.create_all(conn, tables=[Tenant.__table__])


def _fresh_db(prefix: str, *, template: str | None = None) -> str:
    name = f"{prefix}_{secrets.token_hex(4)}"
    _admin(f"CREATE DATABASE {name}" + (f" TEMPLATE {template}" if template else ""))
    return name


def _drop(name: str) -> None:
    _admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture(scope="module")
def template_db():
    name = _fresh_db("gmc_tmpl")
    try:
        eng = create_engine(_url(name), poolclass=NullPool, future=True)
        try:
            with eng.begin() as conn:
                _base_tables(conn)
                _run_migration(conn)
        finally:
            eng.dispose()
        yield name
    finally:
        _drop(name)


@pytest.fixture()
def pg(template_db):
    name = _fresh_db("gmc", template=template_db)
    eng = create_engine(_url(name), pool_size=8, max_overflow=8, future=True)
    try:
        yield SimpleNamespace(name=name, engine=eng)
    finally:
        eng.dispose()
        _drop(name)


# ── SQL helpers ───────────────────────────────────────────────────────────────

def _sql(pg, statement: str, **params):
    with pg.engine.begin() as conn:
        result = conn.execute(text(statement), params)
        return result.mappings().all() if result.returns_rows else result.rowcount


def _fails(pg, statement: str, codes, **params) -> str:
    """The statement must fail inside PostgreSQL (or the driver) with one of *codes*."""
    with pytest.raises((DBAPIError, ValueError)) as info:
        _sql(pg, statement, **params)
    code = getattr(getattr(info.value, "orig", None), "pgcode", None)
    if codes is not None:
        assert code in codes, (code, str(info.value)[:300])
    return code


def _hex(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _tenant(pg, label: str) -> int:
    return _sql(pg, "INSERT INTO tenants (name, is_active, is_platform_tenant) VALUES (:n, true, false) RETURNING id",
                n=f"gmc-{label}-{secrets.token_hex(3)}")[0]["id"]


def _connection(pg, tenant: int, account: str, *, source: str | None = None, language="ar", label="SA") -> int:
    cid = _sql(pg, f"INSERT INTO {CONN} (tenant_id, account_id, content_language, feed_label, status) "
                   "VALUES (:t, :a, :l, :f, 'authorized') RETURNING id",
               t=tenant, a=account, l=language, f=label)[0]["id"]
    if source is not None:
        _sql(pg, f"UPDATE {CONN} SET data_source_id = :s, status = 'bound', binding_verified_at = now(), "
                 "version = version + 1 WHERE id = :id", s=source, id=cid)
    return cid


def _crow(pg, cid: int):
    return _sql(pg, f"SELECT * FROM {CONN} WHERE id = :id", id=cid)[0]


def _row(pg, rid: int):
    return _sql(pg, f"SELECT * FROM {LEDGER} WHERE id = :id", id=rid)[0]


def _op(seed: str, generation: int, action: str = "insert") -> dict:
    snapshot = f'{{"action":"{action}","seed":"{seed}","generation":{generation}}}'
    return {
        "op_id": uuid.uuid4().hex,
        "op_action": action,
        "op_snapshot": snapshot,
        "op_snapshot_hash": _hex(snapshot),
        "op_idempotency_key": _hex(f"key|{seed}|{generation}|{action}"),
        "op_generation": generation,
    }


def _scope(pg, cid: int) -> dict:
    c = _crow(pg, cid)
    return {"tenant_id": c["tenant_id"], "connection_id": c["id"], "account_id": c["account_id"],
            "data_source_id": c["data_source_id"], "content_language": c["content_language"],
            "feed_label": c["feed_label"]}


_INSERT_LEDGER = (
    f"INSERT INTO {LEDGER} (tenant_id, connection_id, account_id, data_source_id, content_language, feed_label,"
    " offer_id, ownership, op_state, op_id, op_action, op_snapshot, op_snapshot_hash, op_idempotency_key,"
    " op_generation, admitted_generation) VALUES (:tenant_id, :connection_id, :account_id, :data_source_id,"
    " :content_language, :feed_label, :offer_id, 'claiming', 'pending', :op_id, :op_action, :op_snapshot,"
    " :op_snapshot_hash, :op_idempotency_key, :op_generation, :op_generation)"
)


def _claim(pg, cid: int, offer_id: str, generation: int = 1) -> int:
    params = {**_scope(pg, cid), "offer_id": offer_id, **_op(offer_id, generation)}
    return _sql(pg, _INSERT_LEDGER + " RETURNING id", **params)[0]["id"]


def _set(pg, rid: int, *, where: str = "", bump: bool = True, **cols) -> int:
    """UPDATE with ``version = version + 1`` (unless ``bump=False``); returns rowcount."""
    assignments = [f"{k} = :{k}" for k in cols] + (["version = version + 1"] if bump else [])
    return _sql(pg, f"UPDATE {LEDGER} SET {', '.join(assignments)} WHERE id = :rid {where}", rid=rid, **cols)


_DISPATCH = (f"UPDATE {LEDGER} SET op_state = 'dispatching', lease_fence = lease_fence + 1,"
             " attempts = attempts + 1, attempt_id = :a, dispatched_at = clock_timestamp(), lease_owner = 'worker-a',"
             " lease_expires_at = clock_timestamp() + make_interval(secs => :s), {extra} version = version + 1"
             " WHERE id = :rid")


def _dispatch(pg, rid: int, attempt: str, *, lease_seconds: int = 300) -> int:
    """A fresh dispatch: new fence and attempt id (the database clears the previous attempt's outcome)."""
    return _sql(pg, _DISPATCH.format(extra=""), rid=rid, a=attempt, s=lease_seconds)


_LEASE_CLEARED = "lease_owner = NULL, lease_expires_at = NULL"


def _readback(pg, rid: int) -> int:
    return _sql(pg, f"UPDATE {LEDGER} SET op_state = 'readback_required', {_LEASE_CLEARED}, version = version + 1"
                    " WHERE id = :rid", rid=rid)


def _confirm(pg, rid: int, ownership: str) -> int:
    row = _row(pg, rid)
    return _sql(pg, f"UPDATE {LEDGER} SET op_state = 'idle', op_id = NULL, op_action = NULL, op_snapshot = NULL,"
                    " op_snapshot_hash = NULL, op_idempotency_key = NULL, op_generation = NULL,"
                    " applied_attributes = CAST(:attrs AS jsonb), applied_fingerprint = :fp,"
                    " applied_generation = :g, applied_idempotency_key = :k, ownership = :own,"
                    " observed_at = now(), version = version + 1 WHERE id = :rid",
                rid=rid, attrs='{"title":"synthetic"}', fp=_hex(f"fp|{row['op_id']}"),
                g=row["op_generation"], k=row["op_idempotency_key"], own=ownership)


def _new_op(pg, rid: int, generation: int, action: str, ownership: str, **extra) -> int:
    op = _op(f"rid{rid}", generation, action)
    return _set(pg, rid, op_state="pending", admitted_generation=generation, ownership=ownership,
                attempts=0, readback_attempts=0, attempt_id=None, dispatched_at=None, **{**op, **extra})


def _bound_pair(pg):
    ta, tb = _tenant(pg, "a"), _tenant(pg, "b")
    return _connection(pg, ta, ACCOUNT_A, source=SOURCE_A), _connection(pg, tb, ACCOUNT_B, source=SOURCE_B)


def _wait_for_lock_wait(pg, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waiting = _sql(pg, "SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database()"
                           " AND wait_event_type = 'Lock'")[0]["n"]
        if waiting:
            return
        time.sleep(0.05)
    raise AssertionError("second session never waited on the first")


def _race(pg, first_sql: str, second_sql: str, first_params: dict, second_params: dict):
    """Run *second* while *first* holds its uncommitted row; return (first, second) outcomes."""
    box = {}
    first = pg.engine.connect()
    tx = first.begin()
    first_result = first.execute(text(first_sql), first_params).mappings().all()

    def contender():
        try:
            with pg.engine.begin() as conn:
                box["rows"] = conn.execute(text(second_sql), second_params).mappings().all()
        except DBAPIError as exc:
            box["error"] = getattr(exc.orig, "pgcode", None)

    thread = threading.Thread(target=contender)
    thread.start()
    try:
        _wait_for_lock_wait(pg)
    finally:
        tx.commit()
        first.close()
    thread.join(30)
    assert not thread.is_alive()
    return first_result, box


# ── Migration ─────────────────────────────────────────────────────────────────

def _schema(eng) -> dict:
    insp = inspect(eng)
    out = {}
    with eng.connect() as conn:
        for t in GOOGLE_MERCHANT_TABLE_NAMES:
            out[t] = {
                "columns": sorted((c["name"], str(c["type"]), bool(c["nullable"]), str(c.get("default")))
                                  for c in insp.get_columns(t)),
                "pk": insp.get_pk_constraint(t).get("constrained_columns"),
                "constraints": sorted(conn.execute(text(
                    "SELECT conname, contype, pg_get_constraintdef(oid) FROM pg_constraint"
                    " WHERE conrelid = CAST(:t AS regclass)"), {"t": t}).all()),
                "indexes": sorted(conn.execute(text(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t"), {"t": t}).all()),
                "triggers": sorted(conn.execute(text(
                    "SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger"
                    " WHERE tgrelid = CAST(:t AS regclass) AND NOT tgisinternal"), {"t": t}).all()),
            }
        out["functions"] = sorted(conn.execute(text(
            "SELECT proname, pg_get_functiondef(oid) FROM pg_proc WHERE proname = ANY(:n)"),
            {"n": list(GOOGLE_MERCHANT_GUARD_FUNCTIONS)}).all())
    return out


def test_migration_schema_equals_model_metadata():
    by_migration, by_model = _fresh_db("gmc_mig"), None
    try:
        by_model = _fresh_db("gmc_mod")
        e1 = create_engine(_url(by_migration), poolclass=NullPool, future=True)
        e2 = create_engine(_url(by_model), poolclass=NullPool, future=True)
        with e1.begin() as conn:
            _base_tables(conn)
            _run_migration(conn)
        with e2.begin() as conn:
            _base_tables(conn)
            create_google_merchant_tables(conn)
        a, b = _schema(e1), _schema(e2)
        e1.dispose()
        e2.dispose()
        assert a == b
        ledger = {name: definition for name, _kind, definition in a[LEDGER]["constraints"]}
        assert ledger["fk_google_merchant_offer_ledger_scope"] == (
            "FOREIGN KEY (connection_id, tenant_id, account_id, data_source_id, content_language, feed_label)"
            " REFERENCES google_merchant_connections(id, tenant_id, account_id, data_source_id, content_language,"
            " feed_label) MATCH FULL ON UPDATE RESTRICT ON DELETE RESTRICT")
        assert ledger["uq_google_merchant_offer_ledger_identity"] == (
            "UNIQUE (account_id, content_language, feed_label, offer_id)")
        assert ledger["ck_google_merchant_offer_ledger_attempt_outcome"] == (
            "CHECK ((((last_outcome IS NULL) AND (last_error_code IS NULL) AND (next_attempt_at IS NULL))"
            " OR (((op_state)::text <> 'dispatching'::text) AND (attempt_id IS NOT NULL))))")
        assert dict(a[LEDGER]["indexes"])["ux_google_merchant_offer_ledger_identity_lower"] == (
            "CREATE UNIQUE INDEX ux_google_merchant_offer_ledger_identity_lower ON public.google_merchant_offer_ledger"
            " USING btree (account_id, content_language, feed_label, lower((offer_id)::text))")
        for table, trigger, function in ((CONN, "trg_google_merchant_connections_guard",
                                          "google_merchant_connections_guard"),
                                         (LEDGER, "trg_google_merchant_offer_ledger_guard",
                                          "google_merchant_offer_ledger_guard")):
            assert a[table]["triggers"] == [
                (trigger, f"CREATE TRIGGER {trigger} BEFORE INSERT OR DELETE OR UPDATE"
                          f" ON public.{table} FOR EACH ROW EXECUTE FUNCTION {function}()"),
                (f"{trigger}_truncate", f"CREATE TRIGGER {trigger}_truncate BEFORE TRUNCATE"
                                        f" ON public.{table} FOR EACH STATEMENT EXECUTE FUNCTION {function}()"),
            ]
        functions = dict(a["functions"])
        assert sorted(functions) == sorted(GOOGLE_MERCHANT_GUARD_FUNCTIONS)
        for name, definition in functions.items():
            assert definition.startswith(f"CREATE OR REPLACE FUNCTION public.{name}()\n RETURNS trigger\n"
                                         " LANGUAGE plpgsql\nAS $function$"), name
        assert "clock_timestamp()" in functions["google_merchant_offer_ledger_guard"]
    finally:
        _drop(by_migration)
        if by_model:
            _drop(by_model)


def _names(eng):
    insp = inspect(eng)
    names = sorted(insp.get_table_names())
    cols = {t: sorted((c["name"], str(c["type"])) for c in insp.get_columns(t)) for t in names}
    with eng.connect() as conn:
        funcs = sorted(conn.execute(text("SELECT proname FROM pg_proc WHERE proname LIKE 'google_merchant%'")).scalars())
    return names, cols, funcs


@pytest.mark.parametrize("shape", ["full_model_objects", "one_table", "malformed_table", "guard_function_only"])
def test_upgrade_refuses_any_preexisting_object_and_changes_nothing(shape):
    name = _fresh_db("gmc_pre")
    eng = create_engine(_url(name), poolclass=NullPool, future=True)
    try:
        with eng.begin() as conn:
            _base_tables(conn)
            if shape == "full_model_objects":
                create_google_merchant_tables(conn)
            elif shape == "one_table":
                GoogleMerchantBase.metadata.tables[CONN].create(conn)
            elif shape == "malformed_table":
                conn.execute(text(f"CREATE TABLE {LEDGER} (id integer, offer_id text)"))
            else:
                conn.execute(text("CREATE FUNCTION google_merchant_offer_ledger_guard() RETURNS trigger"
                                  " LANGUAGE plpgsql AS $f$ BEGIN RETURN NEW; END; $f$"))
        before = _names(eng)
        with pytest.raises(RuntimeError, match="already exist"):
            with eng.begin() as conn:
                _run_migration(conn)
        assert _names(eng) == before
    finally:
        eng.dispose()
        _drop(name)


def test_downgrade_locks_both_tables_before_checking_and_refuses_any_evidence():
    name = _fresh_db("gmc_down")
    eng = create_engine(_url(name), poolclass=NullPool, future=True)
    statements = []
    try:
        with eng.begin() as conn:
            _base_tables(conn)
            _run_migration(conn)
            tenant = conn.execute(text("INSERT INTO tenants (name, is_active, is_platform_tenant)"
                                       " VALUES ('gmc-down', true, false) RETURNING id")).scalar()
            conn.execute(text(f"INSERT INTO {CONN} (tenant_id, account_id, content_language, feed_label, status)"
                              f" VALUES (:t, '{ACCOUNT_A}', 'ar', 'SA', 'authorized')"), {"t": tenant})
        before = _names(eng)
        event.listen(eng, "before_cursor_execute",
                     lambda _c, _cur, statement, *_a: statements.append(" ".join(statement.split())))
        with pytest.raises(RuntimeError, match="hold evidence"):
            with eng.begin() as conn:
                _run_migration(conn, "downgrade")
        assert _names(eng) == before
        lock = next(i for i, s in enumerate(statements) if s.startswith("LOCK TABLE"))
        assert statements[lock] == f"LOCK TABLE {LEDGER}, {CONN} IN ACCESS EXCLUSIVE MODE"
        checks = [i for i, s in enumerate(statements) if s.startswith("SELECT EXISTS")]
        assert checks and min(checks) > lock
        assert not any(s.startswith("DROP") for s in statements)
        with eng.connect() as conn:
            assert conn.execute(text(f"SELECT count(*) FROM {CONN}")).scalar() == 1
    finally:
        eng.dispose()
        _drop(name)


def test_downgrade_of_empty_tables_drops_only_the_google_objects():
    name = _fresh_db("gmc_drop")
    eng = create_engine(_url(name), poolclass=NullPool, future=True)
    try:
        with eng.begin() as conn:
            _base_tables(conn)
            _run_migration(conn)
        with eng.begin() as conn:
            _run_migration(conn, "downgrade")
        names, _cols, funcs = _names(eng)
        assert not (set(GOOGLE_MERCHANT_TABLE_NAMES) & set(names)) and funcs == []
        assert "tenants" in names
    finally:
        eng.dispose()
        _drop(name)


# The production database records the heads {0111, 0113, 0120}. The same row
# set is reached here only through real upgrades along the repository's actual
# Alembic graph (0093 -> 0111 -> 0113 -> 0120), never by stamping.
PRODUCTION_SHAPED_HEADS = {"0111", "0113", "0120"}
_SHOPIFY_TABLES = {"shopify_connections", "shopify_catalog_products", "shopify_privacy_requests"}


def _alembic(name: str):
    env = {**os.environ, "DATABASE_URL": _url(name)}

    def run(*args):
        return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=str(_REPO / "database"), env=env,
                              capture_output=True, text=True, timeout=600)

    return run


def _versions(eng) -> set:
    with eng.connect() as conn:
        return set(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())


def _google_objects(eng) -> tuple:
    names, _cols, funcs = _names(eng)
    return tuple(sorted(set(GOOGLE_MERCHANT_TABLE_NAMES) & set(names))), tuple(funcs)


def _ok(proc) -> None:
    assert proc.returncode == 0, proc.stderr[-3000:]


def _upgrade_to_production_shape(alembic, eng) -> None:
    for target in ("0093", "0111", "0113", "0120"):
        _ok(alembic("upgrade", target))
    assert _versions(eng) == PRODUCTION_SHAPED_HEADS


_PRESENT = (tuple(sorted(GOOGLE_MERCHANT_TABLE_NAMES)), tuple(sorted(GOOGLE_MERCHANT_GUARD_FUNCTIONS)))


def test_alembic_chain_upgrades_0123_to_0124_and_back():
    name = _fresh_db("gmc_chain")
    alembic = _alembic(name)
    eng = create_engine(_url(name), poolclass=NullPool, future=True)
    try:
        _upgrade_to_production_shape(alembic, eng)
        _ok(alembic("upgrade", "0123"))
        assert _versions(eng) == {"0111", "0113", "0123"}
        assert _google_objects(eng) == ((), ())
        _ok(alembic("upgrade", "0124"))
        assert _versions(eng) == {"0111", "0113", "0124"}
        assert _google_objects(eng) == _PRESENT
        for downgrade_target in ("0124@-1", "0123"):
            _ok(alembic("downgrade", downgrade_target))
            assert _versions(eng) == {"0111", "0113", "0123"}, downgrade_target
            assert _google_objects(eng) == ((), ()), downgrade_target
            assert _SHOPIFY_TABLES <= set(inspect(eng).get_table_names()), downgrade_target
            if downgrade_target == "0124@-1":
                _ok(alembic("upgrade", "0124"))
                assert _versions(eng) == {"0111", "0113", "0124"}
    finally:
        eng.dispose()
        _drop(name)


def test_production_shaped_heads_upgrade_to_0124_and_a_refused_downgrade_keeps_everything():
    name = _fresh_db("gmc_prod")
    alembic = _alembic(name)
    eng = create_engine(_url(name), poolclass=NullPool, future=True)
    try:
        _upgrade_to_production_shape(alembic, eng)
        _ok(alembic("upgrade", "0124"))
        assert _versions(eng) == {"0111", "0113", "0124"}
        assert _google_objects(eng) == _PRESENT
        with eng.begin() as conn:
            tenant = conn.execute(text("INSERT INTO tenants (name, is_active, is_platform_tenant)"
                                       " VALUES (:n, true, false) RETURNING id"),
                                  {"n": f"gmc-prod-{secrets.token_hex(3)}"}).scalar()
            conn.execute(text(f"INSERT INTO {CONN} (tenant_id, account_id, content_language, feed_label, status)"
                              " VALUES (:t, :a, 'ar', 'SA', 'authorized')"), {"t": tenant, "a": ACCOUNT_A})
        proc = alembic("downgrade", "0123")
        assert proc.returncode != 0 and "hold evidence" in proc.stderr
        assert _versions(eng) == {"0111", "0113", "0124"}
        assert _google_objects(eng) == _PRESENT
        assert _SHOPIFY_TABLES <= set(inspect(eng).get_table_names())
        with eng.connect() as conn:
            rows = conn.execute(text(f"SELECT tenant_id, account_id FROM {CONN}")).all()
        assert [tuple(r) for r in rows] == [(tenant, ACCOUNT_A)]
    finally:
        eng.dispose()
        _drop(name)


def test_startup_create_all_of_the_application_base_builds_no_google_object():
    from models import Base

    name = _fresh_db("gmc_boot")
    eng = create_engine(_url(name), poolclass=NullPool, future=True)
    try:
        with eng.begin() as conn:
            Base.metadata.create_all(conn)
        names, _cols, funcs = _names(eng)
        assert "tenants" in names
        assert not (set(GOOGLE_MERCHANT_TABLE_NAMES) & set(names)) and funcs == []
    finally:
        eng.dispose()
        _drop(name)


# ── Connections ───────────────────────────────────────────────────────────────

def test_account_stays_claimed_across_inactive_states_and_rows_are_never_deleted(pg):
    ta, tb = _tenant(pg, "a"), _tenant(pg, "b")
    cid = _connection(pg, ta, ACCOUNT_A, source=SOURCE_A)
    for status in ("disabled", "revoked"):
        _sql(pg, f"UPDATE {CONN} SET status = :s, version = version + 1 WHERE id = :id", s=status, id=cid)
        _fails(pg, f"INSERT INTO {CONN} (tenant_id, account_id, content_language, feed_label, status)"
                   " VALUES (:t, :a, 'ar', 'SA', 'authorized')", {UNIQUE}, t=tb, a=ACCOUNT_A)
    _fails(pg, f"INSERT INTO {CONN} (tenant_id, account_id, content_language, feed_label, status)"
               " VALUES (:t, :a, 'ar', 'SA', 'authorized')", {UNIQUE}, t=ta, a=ACCOUNT_B)
    _fails(pg, f"DELETE FROM {CONN} WHERE id = :id", {RESTRICT}, id=cid)
    _fails(pg, f"DELETE FROM {CONN}", {RESTRICT})
    # TRUNCATE fires no row trigger; the statement trigger refuses it.
    _fails(pg, f"TRUNCATE {CONN} CASCADE", {RESTRICT})
    # The foreign key pulls the ledger into any TRUNCATE of the connections, so
    # prove the connections' own refusal with the ledger's trigger switched off
    # (this throw-away database only).
    _sql(pg, f"ALTER TABLE {LEDGER} DISABLE TRIGGER trg_google_merchant_offer_ledger_guard_truncate")
    _fails(pg, f"TRUNCATE {CONN} CASCADE", {RESTRICT})
    _fails(pg, "DELETE FROM tenants WHERE id = :id", {FOREIGN_KEY, RESTRICT}, id=ta)
    assert _crow(pg, cid)["account_id"] == ACCOUNT_A


def test_concurrent_claims_of_one_account_admit_exactly_one(pg):
    ta, tb = _tenant(pg, "a"), _tenant(pg, "b")
    insert = (f"INSERT INTO {CONN} (tenant_id, account_id, content_language, feed_label, status)"
              " VALUES (:t, :a, 'ar', 'SA', 'authorized') RETURNING id")
    first, second = _race(pg, insert, insert, {"t": ta, "a": ACCOUNT_A}, {"t": tb, "a": ACCOUNT_A})
    assert len(first) == 1 and second == {"error": UNIQUE}
    rows = _sql(pg, f"SELECT tenant_id FROM {CONN} WHERE account_id = :a", a=ACCOUNT_A)
    assert [r["tenant_id"] for r in rows] == [ta]


def test_connection_identity_is_immutable_and_the_source_binds_once(pg):
    ta, tb = _tenant(pg, "a"), _tenant(pg, "b")
    cid = _connection(pg, ta, ACCOUNT_A)
    upd = f"UPDATE {CONN} SET {{}}, version = version + 1 WHERE id = :id"
    for assignment in (f"tenant_id = {tb}", f"account_id = '{ACCOUNT_B}'", "content_language = 'en'",
                       "feed_label = 'AE'", "created_at = now() - interval '1 day'", "id = id + 1000"):
        _fails(pg, upd.format(assignment), {RESTRICT}, id=cid)
    _sql(pg, upd.format(f"data_source_id = '{SOURCE_A}', status = 'bound', binding_verified_at = now()"), id=cid)
    for assignment in (f"data_source_id = '{SOURCE_B}'", "data_source_id = NULL, status = 'authorized'"):
        _fails(pg, upd.format(assignment), {RESTRICT}, id=cid)
    # version must advance by exactly one; plan_sequence never decreases
    _fails(pg, f"UPDATE {CONN} SET status = 'disabled' WHERE id = :id", {SERIALIZATION}, id=cid)
    _fails(pg, f"UPDATE {CONN} SET status = 'disabled', version = version + 2 WHERE id = :id", {SERIALIZATION}, id=cid)
    _sql(pg, upd.format("plan_sequence = 5"), id=cid)
    _fails(pg, upd.format("plan_sequence = 4"), {RESTRICT}, id=cid)
    # rows start authorized, unbound, at version 0
    for status in ("bound", "authorized"):
        _fails(pg, f"INSERT INTO {CONN} (tenant_id, account_id, data_source_id, content_language, feed_label,"
                   " status, binding_verified_at) VALUES (:t, :a, :s, 'ar', 'SA', :st, now())",
               {CHECK}, t=tb, a=ACCOUNT_B, s=SOURCE_B, st=status)
    _fails(pg, f"INSERT INTO {CONN} (tenant_id, account_id, content_language, feed_label, status, version)"
               f" VALUES (:t, '{ACCOUNT_B}', 'ar', 'SA', 'authorized', 3)", {CHECK}, t=tb)


_BAD_IDS = [
    pytest.param("1234\n", id="trailing_lf"), pytest.param("1234\r\n", id="trailing_crlf"),
    pytest.param("1234 ", id="trailing_space"), pytest.param(" 1234", id="leading_space"),
    pytest.param("12\t34", id="inner_tab"), pytest.param("12 34", id="inner_space"),
    pytest.param("1234\x0b", id="vertical_tab"), pytest.param("1234\x7f", id="delete_char"),
    pytest.param("", id="empty"), pytest.param("0123", id="leading_zero"), pytest.param("-1234", id="sign"),
    pytest.param("12a4", id="letter"), pytest.param("١٢٣٤", id="arabic_indic_digits"),
    pytest.param("１２３４", id="fullwidth_digits"), pytest.param("1" * 21, id="too_long"),
]


@pytest.mark.parametrize("value", _BAD_IDS + [pytest.param("1234\x00", id="nul")])
def test_account_id_rejects_control_whitespace_and_non_ascii_digits(pg, value):
    tenant = _tenant(pg, "fmt")
    _fails(pg, f"INSERT INTO {CONN} (tenant_id, account_id, content_language, feed_label, status)"
               " VALUES (:t, :a, 'ar', 'SA', 'authorized')", {CHECK, STRING_TOO_LONG, None}, t=tenant, a=value)


@pytest.mark.parametrize("value", _BAD_IDS)
def test_data_source_id_rejects_the_same_variants(pg, value):
    cid = _connection(pg, _tenant(pg, "src"), ACCOUNT_A)
    _fails(pg, f"UPDATE {CONN} SET data_source_id = :s, version = version + 1 WHERE id = :id",
           {CHECK, STRING_TOO_LONG}, s=value, id=cid)


@pytest.mark.parametrize("column,value", [
    pytest.param("content_language", "AR", id="language_upper"),
    pytest.param("content_language", "a\n", id="language_lf"),
    pytest.param("content_language", "a ", id="language_space"),
    pytest.param("content_language", "عر", id="language_arabic_letters"),
    pytest.param("content_language", "ar\n", id="language_trailing_lf"),
    pytest.param("feed_label", "sa", id="label_lower"),
    pytest.param("feed_label", "SA\n", id="label_trailing_lf"),
    pytest.param("feed_label", "S A", id="label_space"),
    pytest.param("feed_label", "SA\t", id="label_tab"),
    pytest.param("feed_label", "", id="label_empty"),
    pytest.param("feed_label", "ＳＡ", id="label_fullwidth"),
    pytest.param("feed_label", "S.A", id="label_dot"),
    pytest.param("feed_label", "A" * 21, id="label_too_long"),
])
def test_language_and_feed_label_reject_variants(pg, column, value):
    tenant = _tenant(pg, "lbl")
    values = {"content_language": "ar", "feed_label": "SA", column: value}
    _fails(pg, f"INSERT INTO {CONN} (tenant_id, account_id, content_language, feed_label, status)"
               " VALUES (:t, :a, :content_language, :feed_label, 'authorized')",
           {CHECK, STRING_TOO_LONG}, t=tenant, a=ACCOUNT_A, **values)


def test_valid_synthetic_identifiers_are_accepted(pg):
    for i, (account, language, label) in enumerate([("1", "ar", "SA"), ("987654321", "en", "A_B-1"),
                                                     ("9" * 20, "fr", "Z" * 20)]):
        _connection(pg, _tenant(pg, f"ok{i}"), account, language=language, label=label)


def test_bound_and_credential_rules(pg):
    cid = _connection(pg, _tenant(pg, "cred"), ACCOUNT_A)
    upd = f"UPDATE {CONN} SET {{}}, version = version + 1 WHERE id = :id"
    _fails(pg, upd.format("status = 'bound'"), {CHECK}, id=cid)
    _fails(pg, upd.format(f"status = 'bound', data_source_id = '{SOURCE_A}'"), {CHECK}, id=cid)
    for bad in ("enc1:abc", "plaintext", "GMC1:abc", " gmc1:abc"):
        _fails(pg, upd.format("credential_enc = :c"), {CHECK}, id=cid, c=bad)
    _sql(pg, upd.format("credential_enc = :c"), id=cid, c="gmc1:synthetic-envelope")
    _fails(pg, upd.format("status = 'revoked'"), {CHECK}, id=cid)
    _sql(pg, upd.format("status = 'revoked', credential_enc = NULL"), id=cid)


# ── Ledger: admission, scope and identity ────────────────────────────────────

def test_ledger_admits_only_a_never_dispatched_claim_under_a_bound_connection(pg):
    ta = _tenant(pg, "a")
    unbound = _connection(pg, ta, ACCOUNT_A)
    scope = {"tenant_id": ta, "connection_id": unbound, "account_id": ACCOUNT_A, "data_source_id": SOURCE_A,
             "content_language": "ar", "feed_label": "SA"}
    _fails(pg, _INSERT_LEDGER, {RESTRICT, FOREIGN_KEY}, **scope, offer_id="SKU-1", **_op("x", 1))
    _sql(pg, f"UPDATE {CONN} SET data_source_id = :s, status = 'bound', binding_verified_at = now(),"
             " version = version + 1 WHERE id = :id", s=SOURCE_A, id=unbound)
    base = (f"INSERT INTO {LEDGER} (tenant_id, connection_id, account_id, data_source_id, content_language,"
            " feed_label, offer_id, ownership, op_state, op_id, op_action, op_snapshot, op_snapshot_hash,"
            " op_idempotency_key, op_generation, admitted_generation, attempts, lease_fence, version) VALUES"
            " (:tenant_id, :connection_id, :account_id, :data_source_id, :content_language, :feed_label, 'SKU-2',"
            " :own, :state, :op_id, :act, :op_snapshot, :op_snapshot_hash, :op_idempotency_key, :op_generation,"
            " :adm, :att, :fence, :ver)")
    op = {k: v for k, v in _op("y", 1).items() if k != "op_action"}
    good = {"own": "claiming", "state": "pending", "act": "insert", "adm": 1, "att": 0, "fence": 0, "ver": 0}
    for override in ({"own": "owned"}, {"state": "readback_required"}, {"act": "patch"}, {"adm": 2},
                     {"att": 1}, {"fence": 1}, {"ver": 1}):
        _fails(pg, base, {CHECK}, **scope, **op, **{**good, **override})
    _sql(pg, base, **scope, **op, **good)
    _sql(pg, f"UPDATE {CONN} SET status = 'disabled', version = version + 1 WHERE id = :id", id=unbound)
    _fails(pg, _INSERT_LEDGER, {RESTRICT}, **scope, offer_id="SKU-3", **_op("z", 1))


@pytest.mark.parametrize("column,value", [
    ("tenant_id", None), ("account_id", ACCOUNT_B), ("data_source_id", SOURCE_B), ("content_language", "en"),
    ("feed_label", "AE"),
])
def test_scope_must_equal_the_connection_exactly(pg, column, value):
    ca, cb = _bound_pair(pg)
    scope = _scope(pg, ca)
    scope[column] = _scope(pg, cb)["tenant_id"] if column == "tenant_id" else value
    _fails(pg, _INSERT_LEDGER, {FOREIGN_KEY}, **scope, offer_id="SKU-1", **_op("x", 1))


@pytest.mark.parametrize("column", ["tenant_id", "connection_id", "account_id", "data_source_id",
                                    "content_language", "feed_label", "offer_id"])
def test_every_scope_column_rejects_null(pg, column):
    ca, _cb = _bound_pair(pg)
    params = {**_scope(pg, ca), "offer_id": "SKU-1", **_op("x", 1), column: None}
    _fails(pg, _INSERT_LEDGER, {NOT_NULL, RESTRICT}, **params)


def test_scope_is_frozen_even_for_a_coordinated_reparent_to_another_valid_connection(pg):
    ca, cb = _bound_pair(pg)
    rid = _claim(pg, ca, "SKU-1")
    target = _scope(pg, cb)
    _fails(pg, f"UPDATE {LEDGER} SET tenant_id = :tenant_id, connection_id = :connection_id,"
               " account_id = :account_id, data_source_id = :data_source_id, content_language = :content_language,"
               " feed_label = :feed_label, version = version + 1 WHERE id = :rid", {RESTRICT}, rid=rid, **target)
    for column, value in (("offer_id", "SKU-9"), ("connection_id", target["connection_id"]),
                          ("feed_label", "AE")):
        _fails(pg, f"UPDATE {LEDGER} SET {column} = :v, version = version + 1 WHERE id = :rid", {RESTRICT},
               rid=rid, v=value)
    # The referenced connection key cannot move either while the row exists.
    _fails(pg, f"UPDATE {CONN} SET data_source_id = :s, version = version + 1 WHERE id = :id", {RESTRICT},
           s=SOURCE_B, id=ca)
    assert _row(pg, rid)["connection_id"] == ca


@pytest.mark.parametrize("offer_id", [
    pytest.param("SKU-1\n", id="trailing_lf"), pytest.param("SKU-1\r", id="trailing_cr"),
    pytest.param(" SKU-1", id="leading_space"), pytest.param("SKU-1 ", id="trailing_space"),
    pytest.param("SKU\t1", id="inner_tab"), pytest.param("SKU\x0b1", id="vertical_tab"),
    pytest.param("", id="empty"), pytest.param("S" * 51, id="too_long"),
])
def test_offer_id_rejects_control_characters_and_outer_whitespace(pg, offer_id):
    ca, _cb = _bound_pair(pg)
    _fails(pg, _INSERT_LEDGER, {CHECK, STRING_TOO_LONG}, **_scope(pg, ca), offer_id=offer_id, **_op("x", 1))


def test_inner_space_offer_id_is_valid_and_identity_is_exact_and_case_safe(pg):
    ca, _cb = _bound_pair(pg)
    _claim(pg, ca, "SKU 1")
    _claim(pg, ca, "sku/123")
    _fails(pg, _INSERT_LEDGER, {UNIQUE}, **_scope(pg, ca), offer_id="SKU 1", **_op("dup", 1))
    _fails(pg, _INSERT_LEDGER, {UNIQUE}, **_scope(pg, ca), offer_id="sku 1", **_op("case", 1))
    _fails(pg, _INSERT_LEDGER, {UNIQUE}, **_scope(pg, ca), offer_id="SKU/123", **_op("case2", 1))


@pytest.mark.parametrize("second_offer", ["SKU-RACE", "sku-race"])
def test_first_enqueue_race_admits_exactly_one_claim(pg, second_offer):
    ca, _cb = _bound_pair(pg)
    scope = _scope(pg, ca)
    insert = _INSERT_LEDGER + " ON CONFLICT DO NOTHING RETURNING id"
    first, second = _race(pg, insert, insert, {**scope, "offer_id": "SKU-RACE", **_op("r1", 1)},
                          {**scope, "offer_id": second_offer, **_op("r2", 1)})
    assert len(first) == 1 and second == {"rows": []}
    plain_first, plain_second = _race(
        pg, _INSERT_LEDGER + " RETURNING id", _INSERT_LEDGER + " RETURNING id",
        {**scope, "offer_id": "SKU-RACE-2", **_op("r3", 1)}, {**scope, "offer_id": "SKU-RACE-2", **_op("r4", 1)})
    assert len(plain_first) == 1 and plain_second == {"error": UNIQUE}
    assert _sql(pg, f"SELECT count(*) AS n FROM {LEDGER}")[0]["n"] == 2


# ── Ledger: dispatched operation, fence and lease ─────────────────────────────

def test_snapshot_hash_must_match_the_snapshot(pg):
    ca, _cb = _bound_pair(pg)
    op = _op("h", 1)
    op["op_snapshot_hash"] = _hex("something else")
    _fails(pg, _INSERT_LEDGER, {CHECK}, **_scope(pg, ca), offer_id="SKU-1", **op)


def test_dispatched_operation_is_frozen_through_every_unresolved_state(pg):
    ca, _cb = _bound_pair(pg)
    rid = _claim(pg, ca, "SKU-1")
    original = _row(pg, rid)
    _dispatch(pg, rid, "a1")
    other = _op("tamper", 1)
    tampers = [{"op_snapshot": other["op_snapshot"], "op_snapshot_hash": other["op_snapshot_hash"]},
               {"op_id": other["op_id"]}, {"op_idempotency_key": other["op_idempotency_key"]},
               {"op_action": "patch"}, {"op_generation": 1, "admitted_generation": 2}]
    for state in ("dispatching", "readback_required", "unresolved"):
        if state == "readback_required":
            _readback(pg, rid)
        elif state == "unresolved":
            _set(pg, rid, op_state="unresolved")
        for tamper in tampers:
            _fails(pg, f"UPDATE {LEDGER} SET {', '.join(f'{k} = :{k}' for k in tamper)}, version = version + 1"
                       " WHERE id = :rid", {RESTRICT, CHECK}, rid=rid, **tamper)
    row = _row(pg, rid)
    frozen = ("op_id", "op_action", "op_snapshot", "op_snapshot_hash", "op_idempotency_key", "op_generation")
    assert {k: row[k] for k in frozen} == {k: original[k] for k in frozen}
    assert row["op_state"] == "unresolved"


def test_lease_fence_is_monotonic_and_a_stale_completion_cannot_land(pg):
    ca, _cb = _bound_pair(pg)
    rid = _claim(pg, ca, "SKU-1")
    assert _dispatch(pg, rid, "a1") == 1
    first = _row(pg, rid)
    assert (first["lease_fence"], first["attempt_id"], first["attempts"]) == (1, "a1", 1)
    # The live holder records a definitive 429: same operation, back to pending.
    _sql(pg, f"UPDATE {LEDGER} SET op_state = 'pending', {_LEASE_CLEARED}, last_outcome = 'rate_limited',"
             " next_attempt_at = now() + interval '30 seconds', version = version + 1 WHERE id = :rid", rid=rid)
    assert _dispatch(pg, rid, "a2") == 1
    second = _row(pg, rid)
    assert (second["lease_fence"], second["attempt_id"], second["attempts"]) == (2, "a2", 2)
    # A completion from the superseded attempt matches nothing (compare-and-set).
    stale = (f"UPDATE {LEDGER} SET op_state = 'readback_required', {_LEASE_CLEARED}, version = version + 1"
             " WHERE id = :rid AND op_id = :op AND attempt_id = :a AND lease_fence = :f AND version = :v")
    assert _sql(pg, stale, rid=rid, op=first["op_id"], a="a1", f=1, v=first["version"]) == 0
    # Rewriting the attempt identity or the fence is refused outright.
    for assignment in ("attempt_id = 'a1'", "lease_fence = 1", "lease_fence = lease_fence + 2",
                       "lease_fence = lease_fence + 1", "attempts = attempts - 1"):
        _fails(pg, f"UPDATE {LEDGER} SET op_state = 'readback_required', {_LEASE_CLEARED}, {assignment},"
                   " version = version + 1 WHERE id = :rid", {RESTRICT}, rid=rid)
    _fails(pg, f"UPDATE {LEDGER} SET lease_fence = 5, attempt_id = 'a5', version = version + 1 WHERE id = :rid",
           {RESTRICT}, rid=rid)
    # The current attempt completes.
    assert _sql(pg, stale, rid=rid, op=second["op_id"], a="a2", f=2, v=second["version"]) == 1
    assert _row(pg, rid)["lease_fence"] == 2
    # version must advance by exactly one on every write
    _fails(pg, f"UPDATE {LEDGER} SET readback_attempts = readback_attempts + 1 WHERE id = :rid",
           {SERIALIZATION}, rid=rid)


def test_an_expired_lease_can_only_go_to_read_back(pg):
    ca, _cb = _bound_pair(pg)
    rid = _claim(pg, ca, "SKU-1")
    _dispatch(pg, rid, "a1", lease_seconds=60)
    assert _sql(pg, f"UPDATE {LEDGER} SET lease_expires_at = clock_timestamp() - interval '5 seconds',"
                    " version = version + 1 WHERE id = :rid AND lease_expires_at > clock_timestamp()", rid=rid) == 1
    to_pending = (f"UPDATE {LEDGER} SET op_state = 'pending', {_LEASE_CLEARED}, last_outcome = 'rate_limited',"
                  " next_attempt_at = now(), version = version + 1 WHERE id = :rid")
    to_rejected = (f"UPDATE {LEDGER} SET op_state = 'rejected', {_LEASE_CLEARED}, last_error_code = 'invalid',"
                   " version = version + 1 WHERE id = :rid")
    renew = (f"UPDATE {LEDGER} SET lease_expires_at = now() + interval '5 minutes', version = version + 1"
             " WHERE id = :rid")
    for statement in (to_pending, to_rejected, renew):
        _fails(pg, statement, {RESTRICT}, rid=rid)
    assert _readback(pg, rid) == 1
    assert _row(pg, rid)["op_state"] == "readback_required"


def test_only_a_definitive_429_from_the_live_lease_returns_to_pending(pg):
    ca, _cb = _bound_pair(pg)
    rid = _claim(pg, ca, "SKU-1")
    _dispatch(pg, rid, "a1")
    for outcome, when in (("timeout", "now()"), ("server_error", "now()"), ("rate_limited", "NULL"), (None, "now()")):
        _fails(pg, f"UPDATE {LEDGER} SET op_state = 'pending', {_LEASE_CLEARED}, last_outcome = :o,"
                   f" next_attempt_at = {when}, version = version + 1 WHERE id = :rid", {RESTRICT}, rid=rid, o=outcome)
    _fails(pg, f"UPDATE {LEDGER} SET op_state = 'rejected', {_LEASE_CLEARED}, version = version + 1"
               " WHERE id = :rid", {RESTRICT}, rid=rid)
    _readback(pg, rid)
    for state in ("pending", "dispatching"):
        _fails(pg, f"UPDATE {LEDGER} SET op_state = :s, last_outcome = 'rate_limited', next_attempt_at = now(),"
                   " version = version + 1 WHERE id = :rid", {RESTRICT, CHECK}, rid=rid, s=state)
    _set(pg, rid, op_state="unresolved")
    for state in ("pending", "dispatching", "rejected"):
        _fails(pg, f"UPDATE {LEDGER} SET op_state = :s, last_outcome = 'rate_limited', next_attempt_at = now(),"
                   " last_error_code = 'x', version = version + 1 WHERE id = :rid", {RESTRICT, CHECK}, rid=rid,
               s=state)
    assert _row(pg, rid)["op_state"] == "unresolved"


# ── Ledger: confirmation, high-water mark and retention ──────────────────────

def test_confirmation_binds_evidence_to_the_operation_and_drives_ownership(pg):
    ca, _cb = _bound_pair(pg)
    rid = _claim(pg, ca, "SKU-1")
    _dispatch(pg, rid, "a1")
    _readback(pg, rid)
    row = _row(pg, rid)
    confirm = (f"UPDATE {LEDGER} SET op_state = 'idle', op_id = NULL, op_action = NULL, op_snapshot = NULL,"
               " op_snapshot_hash = NULL, op_idempotency_key = NULL, op_generation = NULL,"
               " applied_attributes = '{}'::jsonb, applied_fingerprint = :fp, applied_generation = :g,"
               " applied_idempotency_key = :k, ownership = :own, version = version + 1 WHERE id = :rid")
    fp = _hex("fp")
    for bad in ({"k": _hex("other"), "g": row["op_generation"], "own": "owned"},
                {"k": row["op_idempotency_key"], "g": row["op_generation"], "own": "released"},
                {"k": row["op_idempotency_key"], "g": row["op_generation"], "own": "claiming"}):
        _fails(pg, confirm, {RESTRICT, CHECK}, rid=rid, fp=fp, **bad)
    assert _confirm(pg, rid, "owned") == 1
    owned = _row(pg, rid)
    assert (owned["ownership"], owned["applied_idempotency_key"], owned["applied_generation"]) == (
        "owned", row["op_idempotency_key"], 1)
    # Evidence and ownership do not change while idle.
    for assignment in ("ownership = 'released'", "applied_fingerprint = :fp", "applied_generation = 0"):
        _fails(pg, f"UPDATE {LEDGER} SET {assignment}, version = version + 1 WHERE id = :rid", {RESTRICT},
               rid=rid, fp=_hex("changed"))
    # A patch, then a delete, each confirmed by read-back.
    _new_op(pg, rid, 2, "patch", "owned")
    _dispatch(pg, rid, "e1")
    _readback(pg, rid)
    _confirm(pg, rid, "owned")
    _new_op(pg, rid, 3, "delete", "owned")
    _dispatch(pg, rid, "d1")
    _readback(pg, rid)
    _fails(pg, confirm, {RESTRICT}, rid=rid, fp=fp, k=_row(pg, rid)["op_idempotency_key"], g=3, own="owned")
    _confirm(pg, rid, "released")
    released = _row(pg, rid)
    assert (released["ownership"], released["applied_generation"], released["admitted_generation"]) == (
        "released", 3, 3)


def test_admitted_generation_high_water_survives_supersede_rejection_and_replay(pg):
    ca, _cb = _bound_pair(pg)
    rid = _claim(pg, ca, "SKU-1", generation=1)
    # A never-dispatched operation may be replaced by newer work only.
    assert _new_op(pg, rid, 3, "insert", "claiming") == 1
    for generation in (3, 2):
        op = _op("stale", generation)
        _fails(pg, f"UPDATE {LEDGER} SET op_id = :op_id, op_snapshot = :op_snapshot,"
                   " op_snapshot_hash = :op_snapshot_hash, op_idempotency_key = :op_idempotency_key,"
                   " op_generation = :op_generation, admitted_generation = :op_generation,"
                   " version = version + 1 WHERE id = :rid", {RESTRICT, CHECK}, rid=rid, **op)
    _fails(pg, f"UPDATE {LEDGER} SET admitted_generation = 2, version = version + 1 WHERE id = :rid",
           {RESTRICT, CHECK}, rid=rid)
    # Once dispatched (even if returned to pending by a 429) it cannot be replaced.
    _dispatch(pg, rid, "a1")
    _sql(pg, f"UPDATE {LEDGER} SET op_state = 'pending', {_LEASE_CLEARED}, last_outcome = 'rate_limited',"
             " next_attempt_at = now() + interval '30 seconds', version = version + 1 WHERE id = :rid", rid=rid)
    _fails(pg, f"UPDATE {LEDGER} SET op_id = :op_id, op_snapshot = :op_snapshot, op_snapshot_hash = :op_snapshot_hash,"
               " op_idempotency_key = :op_idempotency_key, op_generation = :op_generation,"
               " admitted_generation = :op_generation, attempts = 0, attempt_id = NULL, dispatched_at = NULL,"
               " version = version + 1 WHERE id = :rid", {RESTRICT}, rid=rid, **_op("newer", 4))
    # A definitive rejection from the live lease; the claim is released.
    _dispatch(pg, rid, "a2")
    _sql(pg, f"UPDATE {LEDGER} SET op_state = 'rejected', {_LEASE_CLEARED}, last_error_code = 'invalid_argument',"
             " version = version + 1 WHERE id = :rid", rid=rid)
    _sql(pg, f"UPDATE {LEDGER} SET op_state = 'idle', op_id = NULL, op_action = NULL, op_snapshot = NULL,"
             " op_snapshot_hash = NULL, op_idempotency_key = NULL, op_generation = NULL, ownership = 'released',"
             " version = version + 1 WHERE id = :rid", rid=rid)
    row = _row(pg, rid)
    assert (row["ownership"], row["admitted_generation"], row["op_state"]) == ("released", 3, "idle")
    # The rejected and superseded generations stay retired; replay of 3 is refused, 4 is admitted.
    _fails(pg, f"UPDATE {LEDGER} SET op_state = 'pending', op_id = :op_id, op_action = 'insert',"
               " op_snapshot = :op_snapshot, op_snapshot_hash = :op_snapshot_hash,"
               " op_idempotency_key = :op_idempotency_key, op_generation = :op_generation,"
               " admitted_generation = :op_generation, ownership = 'claiming', attempts = 0, attempt_id = NULL,"
               " dispatched_at = NULL, version = version + 1 WHERE id = :rid", {RESTRICT, CHECK}, rid=rid,
           **_op("replay", 3))
    assert _new_op(pg, rid, 4, "insert", "claiming") == 1
    assert _row(pg, rid)["admitted_generation"] == 4


def test_applied_key_cannot_be_replayed_as_new_work(pg):
    ca, _cb = _bound_pair(pg)
    rid = _claim(pg, ca, "SKU-1")
    _dispatch(pg, rid, "a1")
    _readback(pg, rid)
    _confirm(pg, rid, "owned")
    applied = _row(pg, rid)["applied_idempotency_key"]
    op = _op("again", 2, "patch")
    op["op_idempotency_key"] = applied
    _fails(pg, f"UPDATE {LEDGER} SET op_state = 'pending', op_id = :op_id, op_action = :op_action,"
               " op_snapshot = :op_snapshot, op_snapshot_hash = :op_snapshot_hash,"
               " op_idempotency_key = :op_idempotency_key, op_generation = :op_generation,"
               " admitted_generation = :op_generation, attempts = 0, attempt_id = NULL, dispatched_at = NULL,"
               " version = version + 1 WHERE id = :rid", {RESTRICT}, rid=rid, **op)


def test_new_work_requires_a_bound_connection_but_resolution_continues(pg):
    ca, _cb = _bound_pair(pg)
    rid = _claim(pg, ca, "SKU-1")
    _dispatch(pg, rid, "a1")
    _readback(pg, rid)
    _sql(pg, f"UPDATE {CONN} SET status = 'disabled', version = version + 1 WHERE id = :id", id=ca)
    assert _confirm(pg, rid, "owned") == 1
    _fails(pg, f"UPDATE {LEDGER} SET op_state = 'pending', op_id = :op_id, op_action = :op_action,"
               " op_snapshot = :op_snapshot, op_snapshot_hash = :op_snapshot_hash,"
               " op_idempotency_key = :op_idempotency_key, op_generation = :op_generation,"
               " admitted_generation = :op_generation, attempts = 0, attempt_id = NULL, dispatched_at = NULL,"
               " version = version + 1 WHERE id = :rid", {RESTRICT}, rid=rid, **_op("later", 2, "patch"))


def test_evidence_is_retained(pg):
    ca, _cb = _bound_pair(pg)
    claiming = _claim(pg, ca, "SKU-1")
    owned = _claim(pg, ca, "SKU-2")
    _dispatch(pg, owned, "a1")
    _readback(pg, owned)
    _confirm(pg, owned, "owned")
    released = _claim(pg, ca, "SKU-3")
    _dispatch(pg, released, "b1")
    _sql(pg, f"UPDATE {LEDGER} SET op_state = 'rejected', {_LEASE_CLEARED}, last_error_code = 'invalid',"
             " version = version + 1 WHERE id = :rid", rid=released)
    for rid in (claiming, owned, released):
        _fails(pg, f"DELETE FROM {LEDGER} WHERE id = :rid", {RESTRICT}, rid=rid)
    _fails(pg, f"DELETE FROM {CONN} WHERE id = :id", {RESTRICT, FOREIGN_KEY}, id=ca)
    _sql(pg, f"UPDATE {LEDGER} SET op_state = 'idle', op_id = NULL, op_action = NULL, op_snapshot = NULL,"
             " op_snapshot_hash = NULL, op_idempotency_key = NULL, op_generation = NULL, ownership = 'released',"
             " version = version + 1 WHERE id = :rid", rid=released)
    # A resolved, released row is a tombstone as well: never deleted.
    _fails(pg, f"DELETE FROM {LEDGER} WHERE id = :rid", {RESTRICT}, rid=released)
    _fails(pg, f"DELETE FROM {LEDGER}", {RESTRICT})
    for statement in (f"TRUNCATE {LEDGER}", f"TRUNCATE {LEDGER}, {CONN}", f"TRUNCATE {CONN} CASCADE"):
        _fails(pg, statement, {RESTRICT})
    assert _sql(pg, f"SELECT count(*) AS n FROM {LEDGER}")[0]["n"] == 3
    assert _sql(pg, f"SELECT count(*) AS n FROM {CONN}")[0]["n"] == 2


def test_tombstones_keep_the_high_water_mark_against_reinsert_and_stale_generations(pg):
    ca, _cb = _bound_pair(pg)
    rid = _claim(pg, ca, "SKU-T", generation=1)
    _dispatch(pg, rid, "a1")
    _readback(pg, rid)
    _confirm(pg, rid, "owned")
    _new_op(pg, rid, 2, "delete", "owned")
    _dispatch(pg, rid, "a2")
    _readback(pg, rid)
    _confirm(pg, rid, "released")
    tomb = _row(pg, rid)
    assert (tomb["ownership"], tomb["op_state"], tomb["admitted_generation"], tomb["applied_generation"]) == (
        "released", "idle", 2, 2)
    _fails(pg, f"DELETE FROM {LEDGER} WHERE id = :rid", {RESTRICT}, rid=rid)
    _fails(pg, f"TRUNCATE {LEDGER}", {RESTRICT})
    # The identity cannot be re-created from generation 1 (or by a case variant).
    for offer_id in ("SKU-T", "sku-t"):
        _fails(pg, _INSERT_LEDGER, {UNIQUE}, **_scope(pg, ca), offer_id=offer_id, **_op(f"re-{offer_id}", 1))
    # Work on the retained row stays above the high-water mark and never replays the applied key.
    renew = (f"UPDATE {LEDGER} SET op_state = 'pending', op_id = :op_id, op_action = 'insert',"
             " op_snapshot = :op_snapshot, op_snapshot_hash = :op_snapshot_hash,"
             " op_idempotency_key = :op_idempotency_key, op_generation = :op_generation,"
             " admitted_generation = :op_generation, ownership = 'claiming', attempts = 0, attempt_id = NULL,"
             " dispatched_at = NULL, version = version + 1 WHERE id = :rid")
    for generation in (1, 2):
        _fails(pg, renew, {RESTRICT, CHECK}, rid=rid, **_op(f"stale{generation}", generation))
    replay = _op("replay", 3)
    replay["op_idempotency_key"] = tomb["applied_idempotency_key"]
    _fails(pg, renew, {RESTRICT}, rid=rid, **replay)
    assert _sql(pg, renew, rid=rid, **_op("fresh", 3)) == 1
    assert _row(pg, rid)["admitted_generation"] == 3


_EXPIRED_USE = {
    "rate_limited": (f"UPDATE {LEDGER} SET op_state = 'pending', {_LEASE_CLEARED}, last_outcome = 'rate_limited',"
                     " next_attempt_at = clock_timestamp() + interval '1 minute', version = version + 1"
                     " WHERE id = :rid"),
    "rejected": (f"UPDATE {LEDGER} SET op_state = 'rejected', {_LEASE_CLEARED}, last_error_code = 'invalid_argument',"
                 " version = version + 1 WHERE id = :rid"),
    "renewal": (f"UPDATE {LEDGER} SET lease_expires_at = clock_timestamp() + interval '5 minutes',"
                " version = version + 1 WHERE id = :rid"),
}


@pytest.mark.parametrize("use", sorted(_EXPIRED_USE))
@pytest.mark.parametrize("mode", ["long_transaction", "lock_wait"])
def test_a_transaction_started_before_expiry_cannot_use_an_expired_lease(pg, mode, use):
    ca, _cb = _bound_pair(pg)
    rid = _claim(pg, ca, "SKU-1")
    _dispatch(pg, rid, "a1", lease_seconds=3)
    late = pg.engine.connect()
    tx = late.begin()
    box = {}
    try:
        # The late transaction starts while the lease is still live: its now() stays before expiry.
        assert late.execute(text(f"SELECT now() < lease_expires_at FROM {LEDGER} WHERE id = :rid"),
                            {"rid": rid}).scalar()
        if mode == "long_transaction":
            late.execute(text("SELECT pg_sleep(GREATEST(0, EXTRACT(EPOCH FROM (lease_expires_at"
                              f" - clock_timestamp()))) + 0.3) FROM {LEDGER} WHERE id = :rid"), {"rid": rid})
            assert late.execute(text(f"SELECT now() < lease_expires_at AND clock_timestamp() > lease_expires_at"
                                     f" FROM {LEDGER} WHERE id = :rid"), {"rid": rid}).scalar()
            try:
                late.execute(text(_EXPIRED_USE[use]), {"rid": rid})
                box["ok"] = True
            except DBAPIError as exc:
                box["code"] = exc.orig.pgcode
        else:
            holder = pg.engine.connect()
            held = holder.begin()
            holder.execute(text(f"SELECT 1 FROM {LEDGER} WHERE id = :rid FOR UPDATE"), {"rid": rid})

            def contender():
                try:
                    late.execute(text(_EXPIRED_USE[use]), {"rid": rid})
                    box["ok"] = True
                except DBAPIError as exc:
                    box["code"] = exc.orig.pgcode

            thread = threading.Thread(target=contender)
            thread.start()
            try:
                _wait_for_lock_wait(pg)
                remaining = holder.execute(text("SELECT EXTRACT(EPOCH FROM (lease_expires_at - clock_timestamp()))"
                                                f" FROM {LEDGER} WHERE id = :rid"), {"rid": rid}).scalar()
                time.sleep(max(0.0, float(remaining)) + 0.3)
            finally:
                held.commit()
                holder.close()
            thread.join(30)
            assert not thread.is_alive()
    finally:
        tx.rollback()
        late.close()
    assert box == {"code": RESTRICT}
    row = _row(pg, rid)
    assert (row["op_state"], row["lease_fence"], row["last_outcome"], row["last_error_code"]) == (
        "dispatching", 1, None, None)
    assert _readback(pg, rid) == 1  # an expired lease is reconcile-only


_DISPATCH_RETURNING = _DISPATCH.format(extra="") + " RETURNING id"
_DISABLE = f"UPDATE {CONN} SET status = :s, credential_enc = NULL, version = version + 1 WHERE id = :cid RETURNING id"


@pytest.mark.parametrize("status", ["disabled", "revoked"])
def test_dispatch_requires_a_currently_bound_connection_but_recovery_continues(pg, status):
    ca, _cb = _bound_pair(pg)
    waiting = _claim(pg, ca, "SKU-1")
    inflight = _claim(pg, ca, "SKU-2")
    _dispatch(pg, inflight, "b1")
    _sql(pg, _DISABLE, s=status, cid=ca)
    _fails(pg, _DISPATCH_RETURNING, {RESTRICT}, rid=waiting, a="a1", s=300)
    row = _row(pg, waiting)
    assert (row["op_state"], row["attempts"], row["lease_fence"]) == ("pending", 0, 0)
    # Already-dispatched (ambiguous) work stays recoverable, read-only.
    assert _readback(pg, inflight) == 1
    assert _confirm(pg, inflight, "owned") == 1


@pytest.mark.parametrize("first", ["disable", "dispatch"])
def test_disable_and_dispatch_serialize_on_the_connection_lock(pg, first):
    ca, _cb = _bound_pair(pg)
    rid = _claim(pg, ca, "SKU-1")
    disable = (_DISABLE, {"s": "disabled", "cid": ca})
    dispatch = (_DISPATCH_RETURNING, {"rid": rid, "a": "a1", "s": 300})
    one, two = (disable, dispatch) if first == "disable" else (dispatch, disable)
    first_rows, second = _race(pg, one[0], two[0], one[1], two[1])
    assert len(first_rows) == 1
    row = _row(pg, rid)
    if first == "disable":
        assert second == {"error": RESTRICT}
        assert (row["op_state"], row["attempts"], row["lease_fence"]) == ("pending", 0, 0)
    else:
        assert len(second.get("rows", [])) == 1
        assert (row["op_state"], row["attempts"], row["lease_fence"]) == ("dispatching", 1, 1)
    assert _crow(pg, ca)["status"] == "disabled"


def test_attempt_outcomes_are_cleared_and_bound_to_the_current_attempt(pg):
    ca, _cb = _bound_pair(pg)
    rid = _claim(pg, ca, "SKU-1")
    _dispatch(pg, rid, "a1")
    # Attempt 1 records a 429 whose retry time is already due.
    assert _sql(pg, f"UPDATE {LEDGER} SET op_state = 'pending', {_LEASE_CLEARED}, last_outcome = 'rate_limited',"
                    " next_attempt_at = clock_timestamp(), version = version + 1 WHERE id = :rid", rid=rid) == 1
    first = _row(pg, rid)
    assert (first["attempt_id"], first["last_outcome"]) == ("a1", "rate_limited")
    # The outcome is frozen with its attempt.
    for assignment in ("last_outcome = 'timeout'", "last_error_code = 'invalid'",
                       "next_attempt_at = next_attempt_at + interval '1 hour'"):
        _fails(pg, f"UPDATE {LEDGER} SET {assignment}, version = version + 1 WHERE id = :rid", {RESTRICT}, rid=rid)
    # A fresh dispatch clears it in the database; the caller writes nothing about it,
    # and the attempt's start is the database clock even when the caller claims an older one.
    stale_start = _DISPATCH.replace("dispatched_at = clock_timestamp()", "dispatched_at = :claimed")
    assert _sql(pg, stale_start.format(extra=""), rid=rid, a="a2", s=300, claimed=first["dispatched_at"]) == 1
    second = _row(pg, rid)
    assert (second["attempt_id"], second["last_outcome"], second["last_error_code"], second["next_attempt_at"]) == (
        "a2", None, None, None)
    assert second["dispatched_at"] > first["next_attempt_at"] > first["dispatched_at"]
    # Nothing is recorded early while the attempt is in flight.
    for assignment in ("last_outcome = 'rate_limited'", "last_error_code = 'invalid'", "next_attempt_at = :old"):
        _fails(pg, f"UPDATE {LEDGER} SET {assignment}, version = version + 1 WHERE id = :rid", {CHECK},
               rid=rid, old=first["next_attempt_at"])
    # Attempt 2 returns to pending only with its own outcome and a retry time after its own dispatch.
    back = f"UPDATE {LEDGER} SET op_state = 'pending', {_LEASE_CLEARED}{{extra}}, version = version + 1 WHERE id = :rid"
    for extra in ("", ", last_outcome = 'rate_limited'", ", next_attempt_at = :later",
                  ", last_outcome = 'rate_limited', next_attempt_at = :old"):
        _fails(pg, back.format(extra=extra), {RESTRICT}, rid=rid, old=first["next_attempt_at"],
               later=second["lease_expires_at"])
    _fails(pg, f"UPDATE {LEDGER} SET op_state = 'rejected', {_LEASE_CLEARED}, version = version + 1 WHERE id = :rid",
           {RESTRICT}, rid=rid)
    assert _sql(pg, _EXPIRED_USE["rate_limited"], rid=rid) == 1
    third = _row(pg, rid)
    assert (third["attempt_id"], third["last_outcome"]) == ("a2", "rate_limited")
    assert third["next_attempt_at"] > second["dispatched_at"]
    # Even a dispatch that tries to carry attempt 2's outcome forward starts clean.
    carry = "last_outcome = 'rate_limited', last_error_code = 'invalid', next_attempt_at = :old,"
    assert _sql(pg, _DISPATCH.format(extra=carry), rid=rid, a="a3", s=300, old=third["next_attempt_at"]) == 1
    carried = _row(pg, rid)
    assert (carried["attempt_id"], carried["last_outcome"], carried["last_error_code"],
            carried["next_attempt_at"]) == ("a3", None, None, None)
    # Attempt 3 is rejected; the terminal code stays with attempt 3, also once the claim is closed.
    assert _sql(pg, _EXPIRED_USE["rejected"], rid=rid) == 1
    _fails(pg, f"UPDATE {LEDGER} SET last_error_code = 'other', version = version + 1 WHERE id = :rid",
           {RESTRICT}, rid=rid)
    close = (f"UPDATE {LEDGER} SET op_state = 'idle', op_id = NULL, op_action = NULL, op_snapshot = NULL,"
             " op_snapshot_hash = NULL, op_idempotency_key = NULL, op_generation = NULL, ownership = 'released',"
             " {extra} version = version + 1 WHERE id = :rid")
    _fails(pg, close.format(extra="last_error_code = NULL,"), {RESTRICT}, rid=rid)
    assert _sql(pg, close.format(extra=""), rid=rid) == 1
    closed = _row(pg, rid)
    assert (closed["attempt_id"], closed["last_error_code"]) == ("a3", "invalid_argument")
    # New work has no attempt, so the database drops the previous operation's outcome.
    assert _new_op(pg, rid, 2, "insert", "claiming", last_error_code="invalid_argument") == 1
    fresh = _row(pg, rid)
    assert (fresh["attempt_id"], fresh["last_outcome"], fresh["last_error_code"], fresh["next_attempt_at"]) == (
        None, None, None, None)
    # Outcome evidence never exists without the attempt it belongs to.
    params = {**_scope(pg, ca), "offer_id": "SKU-2", **_op("unbound", 1)}
    _fails(pg, _INSERT_LEDGER.replace(") VALUES", ", last_outcome) VALUES").replace(
        ":op_generation)", ":op_generation, 'rate_limited')"), {CHECK}, **params)


_TAKEOVERS = {
    "owner_only": "lease_owner = 'worker-b'",
    "owner_and_lease": "lease_owner = 'worker-b', lease_expires_at = clock_timestamp() + interval '5 minutes'",
    "new_fenced_attempt": ("lease_owner = 'worker-b', lease_fence = lease_fence + 1, attempts = attempts + 1,"
                           " attempt_id = 'b1', dispatched_at = clock_timestamp(),"
                           " lease_expires_at = clock_timestamp() + interval '5 minutes'"),
}


def _takeover(kind: str, *, compare_and_set: bool = False) -> str:
    where = " AND version = :v AND lease_owner = 'worker-a'" if compare_and_set else ""
    return (f"UPDATE {LEDGER} SET {_TAKEOVERS[kind]}, version = version + 1 WHERE id = :rid{where}"
            " RETURNING id")


_RENEW = (f"UPDATE {LEDGER} SET lease_expires_at = clock_timestamp() + interval '10 minutes',"
          " version = version + 1 WHERE id = :rid RETURNING id")


def _assert_attempt_of_worker_a(pg, rid: int) -> None:
    row = _row(pg, rid)
    assert (row["op_state"], row["lease_owner"], row["attempt_id"], row["lease_fence"], row["attempts"]) == (
        "dispatching", "worker-a", "a1", 1, 1)


def test_a_live_attempt_keeps_its_owner_and_is_handed_over_only_by_reconciliation(pg):
    ca, _cb = _bound_pair(pg)
    rid = _claim(pg, ca, "SKU-1")
    _dispatch(pg, rid, "a1")
    before = _row(pg, rid)
    assert len(_sql(pg, _RENEW, rid=rid)) == 1  # the holder renews
    renewed = _row(pg, rid)
    assert renewed["lease_expires_at"] > before["lease_expires_at"]
    for kind in sorted(_TAKEOVERS):
        _fails(pg, _takeover(kind), {RESTRICT}, rid=rid)
    _assert_attempt_of_worker_a(pg, rid)
    # Expired, the attempt still cannot change hands; it can only be read back.
    assert _sql(pg, f"UPDATE {LEDGER} SET lease_expires_at = clock_timestamp() - interval '1 second',"
                    " version = version + 1 WHERE id = :rid", rid=rid) == 1
    for kind in sorted(_TAKEOVERS):
        _fails(pg, _takeover(kind), {RESTRICT}, rid=rid)
    _fails(pg, _RENEW, {RESTRICT}, rid=rid)
    assert _readback(pg, rid) == 1
    row = _row(pg, rid)
    assert (row["op_state"], row["lease_owner"], row["attempt_id"], row["lease_fence"]) == (
        "readback_required", None, "a1", 1)


@pytest.mark.parametrize("compare_and_set", [False, True], ids=["guarded", "compare_and_set"])
@pytest.mark.parametrize("kind", sorted(_TAKEOVERS))
def test_a_concurrent_takeover_cannot_replace_the_renewing_owner(pg, kind, compare_and_set):
    ca, _cb = _bound_pair(pg)
    rid = _claim(pg, ca, "SKU-1")
    _dispatch(pg, rid, "a1")
    seen = _row(pg, rid)["version"]
    renewal, takeover = _race(pg, _RENEW, _takeover(kind, compare_and_set=compare_and_set),
                              {"rid": rid}, {"rid": rid, "v": seen})
    assert len(renewal) == 1
    # worker-b waited on worker-a's row lock; afterwards the guard (or a stale
    # compare-and-set) leaves worker-a's attempt in place.
    assert takeover == ({"rows": []} if compare_and_set else {"error": RESTRICT})
    _assert_attempt_of_worker_a(pg, rid)
    assert _row(pg, rid)["version"] == seen + 1


_REMOTE_TARGETS = [
    pytest.param("postgresql://u:p@db.example.invalid:5432/postgres", {}, id="remote_name"),
    pytest.param("postgresql://u:p@192.0.2.10:5432/postgres", {}, id="remote_address"),
    pytest.param("postgresql://u:p@/postgres?host=/var/run/postgresql", {}, id="no_url_host"),
    pytest.param("postgresql://u:p@127.0.0.1/postgres?hostaddr=192.0.2.10", {}, id="query_hostaddr"),
    pytest.param("postgresql://u:p@127.0.0.1/postgres?host=db.example.invalid", {}, id="query_host"),
    pytest.param("postgresql://u:p@127.0.0.1/postgres?service=remote", {}, id="query_service"),
    pytest.param("postgresql://u:p@127.0.0.1/postgres", {"PGHOSTADDR": "192.0.2.10"}, id="env_hostaddr"),
    pytest.param("postgresql://u:p@127.0.0.1/postgres", {"PGSERVICE": "remote"}, id="env_service"),
]


@pytest.mark.parametrize("dsn,environ", _REMOTE_TARGETS)
def test_disposable_target_guard_refuses_anything_but_loopback(dsn, environ):
    with pytest.raises(RuntimeError, match="loopback") as info:
        _refuse_non_loopback(dsn, environ)
    assert "u:p@" not in str(info.value)


def test_disposable_target_guard_accepts_loopback_and_holds_for_this_run():
    for dsn in ("postgresql://u:p@127.0.0.1:5433/postgres", "postgresql://u:p@localhost/postgres",
                "postgresql://u:p@[::1]:5433/postgres"):
        _refuse_non_loopback(dsn, {})
    _refuse_non_loopback(ADMIN_URL, os.environ)
    assert make_url(_url("gmc_any")).host == make_url(ADMIN_URL).host
