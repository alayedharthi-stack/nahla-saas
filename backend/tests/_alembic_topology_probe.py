"""Test-only probes for the multi-head Alembic chain proofs (disposable PostgreSQL only).

The three Shopify chain proofs build the deployed topology ``{0111, 0113, 0120}``
by genuine ``alembic upgrade`` calls and then step through 0121, 0122 and
0123 with branch-qualified ``@-1`` rollbacks. These helpers let them assert,
after every step:

* the **exact** ``alembic_version`` rows (count and ids, duplicates included);
* that the non-Shopify schema is unchanged, compared by **canonical PostgreSQL
  definition**: columns (name, type, nullability, default), every constraint
  through ``pg_get_constraintdef`` (primary key, unique, check, foreign key
  including referenced table, ON DELETE / ON UPDATE, MATCH, deferrability)
  and every index through ``pg_get_indexdef`` (method, columns, expressions,
  ordering and operator classes, INCLUDE, partial predicate);
* that synthetic sentinel rows planted in generic, catalog and retained
  0111 / 0113 branch tables survive untouched;
* and, as a negative control, that a same-name constraint or index whose
  definition changes **is** detected.

Nothing here stamps, merges, fabricates or deletes version rows, and nothing
prints a token column, a DSN or a row: failures name tables only.
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Any, Dict, List, Tuple

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

S0 = frozenset({"0111", "0113", "0120"})
S1 = frozenset({"0111", "0113", "0121"})
S2 = frozenset({"0111", "0113", "0122"})
S3 = frozenset({"0111", "0113", "0123"})
RETAINED = frozenset({"0111", "0113"})

_SYNTHETIC_TOKEN = "synthetic-ciphertext-placeholder-not-a-token"

_TABLES_SQL = text(
    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p') ORDER BY c.relname")
_COLUMNS_SQL = text(
    "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,"
    " pg_get_expr(d.adbin, d.adrelid), a.attidentity, a.attgenerated"
    " FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
    " WHERE a.attrelid = CAST(:tbl AS regclass) AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum")
_CONSTRAINTS_SQL = text(
    "SELECT conname, contype, pg_get_constraintdef(oid), condeferrable, condeferred, convalidated"
    " FROM pg_constraint WHERE conrelid = CAST(:tbl AS regclass) ORDER BY conname")
_INDEXES_SQL = text(
    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() AND tablename = :tbl ORDER BY indexname")


def _engine(url: str):
    return create_engine(url, poolclass=NullPool, future=True)


def version_rows(url: str) -> List[str]:
    """Every alembic_version row, sorted, duplicates kept (a set would hide them)."""
    eng = _engine(url)
    try:
        with eng.connect() as conn:
            return sorted(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())
    finally:
        eng.dispose()


def assert_rows(url: str, expected: frozenset, step: str) -> None:
    rows = version_rows(url)
    assert rows == sorted(expected), f"{step}: alembic_version rows {rows} != {sorted(expected)}"
    assert RETAINED <= set(rows), f"{step}: retained branch rows 0111/0113 missing"


def schema_snapshot(url: str, *, exclude_prefixes: Tuple[str, ...] = ("shopify_",), exclude: Tuple[str, ...] = ("alembic_version",)) -> Dict[str, Any]:
    """Canonical-definition snapshot of every table outside the excluded set (current schema)."""
    eng = _engine(url)
    try:
        with eng.connect() as conn:
            snap: Dict[str, Any] = {}
            for (table,) in conn.execute(_TABLES_SQL):
                if table in exclude or table.startswith(exclude_prefixes):
                    continue
                qualified = f'"{table}"'
                snap[table] = {
                    "columns": [tuple(r) for r in conn.execute(_COLUMNS_SQL, {"tbl": qualified})],
                    "constraints": [tuple(r) for r in conn.execute(_CONSTRAINTS_SQL, {"tbl": qualified})],
                    "indexes": [tuple(r) for r in conn.execute(_INDEXES_SQL, {"tbl": table})],
                }
            return snap
    finally:
        eng.dispose()


def schema_diff(a: Dict[str, Any], b: Dict[str, Any]) -> List[str]:
    """Names of tables whose definition differs (never the definitions themselves)."""
    return sorted(t for t in set(a) | set(b) if a.get(t) != b.get(t))


def assert_schema_unchanged(url: str, baseline: Dict[str, Any], step: str) -> None:
    changed = schema_diff(schema_snapshot(url), baseline)
    assert not changed, f"{step}: non-Shopify schema definition changed for tables {changed}"


def plant_sentinels(url: str) -> Dict[str, Any]:
    """Insert synthetic rows into generic, catalog and retained-branch tables; return what to verify later."""
    tag = secrets.token_hex(4)
    eng = _engine(url)
    try:
        with eng.begin() as conn:
            tenant = conn.execute(text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"), {"n": f"mh-sentinel-tenant-{tag}"}).scalar_one()
            product = conn.execute(text("INSERT INTO products (title, tenant_id) VALUES (:t, :tid) RETURNING id"),
                                   {"t": f"sentinel white sneakers {tag}", "tid": tenant}).scalar_one()
            customer = conn.execute(text("INSERT INTO customers (tenant_id, name) VALUES (:tid, :n) RETURNING id"),
                                    {"tid": tenant, "n": f"sentinel customer {tag}"}).scalar_one()
            address = conn.execute(text("INSERT INTO customer_addresses (tenant_id, customer_id, raw_address) VALUES (:tid, :cid, :a) RETURNING id"),
                                   {"tid": tenant, "cid": customer, "a": f"sentinel address {tag}"}).scalar_one()
            # retained 0113 branch (0110 customer address provenance + 0113 navigation snapshot)
            provenance = conn.execute(text(
                "INSERT INTO customer_address_provenance (tenant_id, customer_id, customer_address_id, source, content_fingerprint, source_observed_at)"
                " VALUES (:tid, :cid, :aid, 'synthetic', :fp, now()) RETURNING id"),
                {"tid": tenant, "cid": customer, "aid": address, "fp": f"fp-{tag}"}).scalar_one()
            conversation = conn.execute(text(
                "INSERT INTO commerce_runtime_conversations (tenant_id, namespace, conversation_ref) VALUES (:tid, 'shadow', :ref) RETURNING id"),
                {"tid": tenant, "ref": f"conv-{tag}"}).scalar_one()
            snapshot = conn.execute(text(
                "INSERT INTO commerce_runtime_navigation_snapshots (token, series, tenant_id, namespace, conversation_id, minted_by_turn_id,"
                " origin_turn_id, origin_call_id, search_method, query_digest, product_ids, complete, page_offset, page_size, more_label,"
                " button_label, expires_at) VALUES (:tok, :ser, :tid, 'shadow', :conv, 1, 1, 'call-1', 'synthetic', :q,"
                " CAST(:pids AS jsonb), false, 1, 1, 'more', 'open', now() + interval '1 hour') RETURNING id"),
                {"tok": f"tok-{tag}", "ser": f"series-{tag}", "tid": tenant, "conv": conversation, "q": f"q-{tag}", "pids": f"[{product}, {product + 1}]"}).scalar_one()
            # retained 0111 branch
            worker = conn.execute(text(
                "INSERT INTO commerce_runtime_handover_workers (tenant_id, namespace, worker_id, observed_generation, observed_state)"
                " VALUES (:tid, 'shadow', :w, 0, 'synthetic') RETURNING id"), {"tid": tenant, "w": f"worker-{tag}"}).scalar_one()
            # catalog relations (generic 0099 membership, 0118 retirement, 0120 authorization)
            membership = conn.execute(text(
                "INSERT INTO meta_catalog_memberships (tenant_id, catalog_id, retailer_id, product_id, verified_at, provenance)"
                " VALUES (:tid, :cat, :ret, :pid, now(), 'synthetic') RETURNING id"),
                {"tid": tenant, "cat": f"catalog-{tag}", "ret": f"retailer-{tag}", "pid": product}).scalar_one()
            retirement = conn.execute(text(
                "INSERT INTO catalog_channel_retirements (tenant_id, retailer_id, reason) VALUES (:tid, :ret, 'synthetic') RETURNING id"),
                {"tid": tenant, "ret": f"retailer-{tag}"}).scalar_one()
            authorization = conn.execute(text(
                "INSERT INTO meta_catalog_authorizations (tenant_id, catalog_id, business_id, meta_app_id, meta_user_id, access_token_enc,"
                " granted_scopes, status, verified_at) VALUES (:tid, :cat, :biz, :app, :usr, :tok, CAST(:scopes AS json), 'active', now()) RETURNING id"),
                {"tid": tenant, "cat": f"catalog-{tag}", "biz": f"biz-{tag}", "app": f"app-{tag}", "usr": f"user-{tag}",
                 "tok": _SYNTHETIC_TOKEN, "scopes": "[]"}).scalar_one()
            ids = {"tenants": tenant, "products": product, "customers": customer, "customer_addresses": address,
                   "customer_address_provenance": provenance, "commerce_runtime_conversations": conversation,
                   "commerce_runtime_navigation_snapshots": snapshot, "commerce_runtime_handover_workers": worker,
                   "meta_catalog_memberships": membership, "catalog_channel_retirements": retirement,
                   "meta_catalog_authorizations": authorization}
            # canonical whole-row digests (every column, token columns included), taken in the inserting transaction
            digests = {t: _row_digest(conn, t, i) for t, i in ids.items()}
        return {
            "row_digests": digests,
            "tag": tag,
            "tenants": (tenant, {"name": f"mh-sentinel-tenant-{tag}"}),
            "products": (product, {"title": f"sentinel white sneakers {tag}", "tenant_id": tenant}),
            "customers": (customer, {"tenant_id": tenant, "name": f"sentinel customer {tag}"}),
            "customer_addresses": (address, {"tenant_id": tenant, "customer_id": customer, "raw_address": f"sentinel address {tag}"}),
            "customer_address_provenance": (provenance, {"tenant_id": tenant, "customer_id": customer, "customer_address_id": address,
                                                         "source": "synthetic", "content_fingerprint": f"fp-{tag}"}),
            "commerce_runtime_conversations": (conversation, {"tenant_id": tenant, "namespace": "shadow", "conversation_ref": f"conv-{tag}"}),
            "commerce_runtime_navigation_snapshots": (snapshot, {"token": f"tok-{tag}", "series": f"series-{tag}", "tenant_id": tenant,
                                                                 "conversation_id": conversation, "page_offset": 1, "page_size": 1}),
            "commerce_runtime_handover_workers": (worker, {"tenant_id": tenant, "namespace": "shadow", "worker_id": f"worker-{tag}",
                                                           "observed_generation": 0, "observed_state": "synthetic"}),
            "meta_catalog_memberships": (membership, {"tenant_id": tenant, "catalog_id": f"catalog-{tag}", "retailer_id": f"retailer-{tag}",
                                                      "product_id": product, "provenance": "synthetic"}),
            "catalog_channel_retirements": (retirement, {"tenant_id": tenant, "retailer_id": f"retailer-{tag}", "reason": "synthetic"}),
            "meta_catalog_authorizations": (authorization, {"tenant_id": tenant, "catalog_id": f"catalog-{tag}", "business_id": f"biz-{tag}",
                                                            "meta_app_id": f"app-{tag}", "meta_user_id": f"user-{tag}", "status": "active"}),
            "token_sha256": hashlib.sha256(_SYNTHETIC_TOKEN.encode()).hexdigest(),
        }
    finally:
        eng.dispose()


def _row_digest(conn, table: str, row_id: int) -> str:
    """sha256 of the canonical text form of the whole row (every column, values never returned to Python)."""
    return conn.execute(text(f"SELECT encode(sha256(convert_to(CAST(t AS text), 'UTF8')), 'hex') FROM {table} t WHERE id = :id"),
                        {"id": row_id}).scalar_one()


def assert_sentinels(url: str, planted: Dict[str, Any], step: str) -> None:
    """Every planted row still exists with the planted values and its whole-row digest equals the one taken at
    insertion (every column, token columns included); failures name the table only."""
    eng = _engine(url)
    try:
        with eng.connect() as conn:
            for table, spec in planted.items():
                if table in ("tag", "token_sha256", "row_digests"):
                    continue
                assert _row_digest(conn, table, spec[0]) == planted["row_digests"][table], f"{step}: sentinel row digest changed in {table}"
                row_id, values = spec
                cols = ", ".join(values)
                row = conn.execute(text(f"SELECT {cols} FROM {table} WHERE id = :id"), {"id": row_id}).mappings().first()
                assert row is not None, f"{step}: sentinel row missing from {table}"
                for col, expected in values.items():
                    assert row[col] == expected, f"{step}: sentinel column {table}.{col} changed"
            digest = conn.execute(text("SELECT encode(sha256(convert_to(access_token_enc, 'UTF8')), 'hex') FROM meta_catalog_authorizations WHERE id = :id"),
                                  {"id": planted["meta_catalog_authorizations"][0]}).scalar_one()
            assert digest == planted["token_sha256"], f"{step}: sentinel token column of meta_catalog_authorizations changed"
    finally:
        eng.dispose()


def row_counts(url: str, tables: Tuple[str, ...]) -> Dict[str, int]:
    eng = _engine(url)
    try:
        with eng.connect() as conn:
            return {t: int(conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()) for t in tables}
    finally:
        eng.dispose()


SENTINEL_TABLES = ("tenants", "products", "customers", "customer_addresses", "customer_address_provenance",
                   "commerce_runtime_conversations", "commerce_runtime_navigation_snapshots", "commerce_runtime_handover_workers",
                   "meta_catalog_memberships", "catalog_channel_retirements", "meta_catalog_authorizations")


def assert_step(url: str, expected_rows: frozenset, baseline_schema: Dict[str, Any], planted: Dict[str, Any], counts: Dict[str, int], step: str) -> None:
    """The full per-step contract: exact rows, retained branches, schema by definition, sentinels, row counts."""
    assert_rows(url, expected_rows, step)
    assert_schema_unchanged(url, baseline_schema, step)
    assert_sentinels(url, planted, step)
    assert row_counts(url, SENTINEL_TABLES) == counts, f"{step}: row counts of non-Shopify tables changed"


def assert_sentinel_mutations_are_detected(url: str, planted: Dict[str, Any]) -> None:
    """Negative control: mutating a seeded column that the projection does not list (navigation
    query_digest, authorization granted_scopes, provenance source_observed_at) must fail the sentinel check,
    and restoring the exact original value must make it pass again. Values are never printed."""
    nav_id = planted["commerce_runtime_navigation_snapshots"][0]
    auth_id = planted["meta_catalog_authorizations"][0]
    prov_id = planted["customer_address_provenance"][0]
    eng = _engine(url)
    try:
        with eng.connect() as conn:
            original_observed_at = conn.execute(text("SELECT source_observed_at FROM customer_address_provenance WHERE id = :id"),
                                                {"id": prov_id}).scalar_one()
        cases = [
            ("commerce_runtime_navigation_snapshots", "UPDATE commerce_runtime_navigation_snapshots SET query_digest = query_digest || '-mutated' WHERE id = :id",
             "UPDATE commerce_runtime_navigation_snapshots SET query_digest = :q WHERE id = :id", {"id": nav_id, "q": f"q-{planted['tag']}"}),
            ("meta_catalog_authorizations", "UPDATE meta_catalog_authorizations SET granted_scopes = CAST('[\"mutated\"]' AS json) WHERE id = :id",
             "UPDATE meta_catalog_authorizations SET granted_scopes = CAST('[]' AS json) WHERE id = :id", {"id": auth_id}),
            ("customer_address_provenance", "UPDATE customer_address_provenance SET source_observed_at = source_observed_at + interval '1 second' WHERE id = :id",
             "UPDATE customer_address_provenance SET source_observed_at = :ts WHERE id = :id", {"id": prov_id, "ts": original_observed_at}),
        ]
        for table, mutate, restore, params in cases:
            with eng.begin() as conn:
                conn.execute(text(mutate), {"id": params["id"]})
            try:
                assert_sentinels(url, planted, f"negative control on {table}")
            except AssertionError as exc:
                assert table in str(exc), f"sentinel failure did not name {table}"
            else:
                raise AssertionError(f"mutation of a seeded column in {table} was not detected")
            with eng.begin() as conn:
                conn.execute(text(restore), params)
            assert_sentinels(url, planted, f"after restoring {table}")
    finally:
        eng.dispose()


def assert_definition_changes_are_detected(url: str, baseline: Dict[str, Any]) -> None:
    """Negative control, run last on the disposable database: three same-name definition changes on
    retained-branch tables (a CHECK predicate, a foreign key's ON DELETE / deferrability, an index gaining a
    partial predicate) must each be detected by the snapshot and must disappear again once restored."""
    eng = _engine(url)
    try:
        with eng.connect() as conn:
            check_def = conn.execute(text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'commerce_runtime_handover_workers'::regclass"
                " AND conname = 'ck_commerce_runtime_handover_workers_generation'")).scalar_one()
            fk_def = conn.execute(text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'customer_address_provenance'::regclass"
                " AND conname = 'customer_address_provenance_tenant_id_fkey'")).scalar_one()
            index_row = None
            for candidate in ("customer_address_provenance", "commerce_runtime_handover_workers", "commerce_runtime_navigation_snapshots",
                              "commerce_runtime_conversations", "catalog_channel_retirements", "meta_catalog_authorizations"):
                found = conn.execute(text(
                    "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() AND tablename = :t"
                    " AND indexdef NOT LIKE 'CREATE UNIQUE%' AND indexdef NOT LIKE '% WHERE %' ORDER BY indexname LIMIT 1"), {"t": candidate}).first()
                if found is not None:
                    index_row = found
                    break
        assert "observed_generation >= 0" in check_def and check_def.startswith("CHECK"), "unexpected baseline CHECK definition"
        assert fk_def.startswith("FOREIGN KEY (tenant_id) REFERENCES tenants(id)"), "unexpected baseline FK definition"

        mutations = [
            ("commerce_runtime_handover_workers",
             "ALTER TABLE commerce_runtime_handover_workers DROP CONSTRAINT ck_commerce_runtime_handover_workers_generation;"
             " ALTER TABLE commerce_runtime_handover_workers ADD CONSTRAINT ck_commerce_runtime_handover_workers_generation CHECK (observed_generation >= 0 AND observed_generation <= 2147483647)",
             "ALTER TABLE commerce_runtime_handover_workers DROP CONSTRAINT ck_commerce_runtime_handover_workers_generation;"
             f" ALTER TABLE commerce_runtime_handover_workers ADD CONSTRAINT ck_commerce_runtime_handover_workers_generation {check_def}"),
            ("customer_address_provenance",
             "ALTER TABLE customer_address_provenance DROP CONSTRAINT customer_address_provenance_tenant_id_fkey;"
             " ALTER TABLE customer_address_provenance ADD CONSTRAINT customer_address_provenance_tenant_id_fkey"
             " FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED",
             "ALTER TABLE customer_address_provenance DROP CONSTRAINT customer_address_provenance_tenant_id_fkey;"
             f" ALTER TABLE customer_address_provenance ADD CONSTRAINT customer_address_provenance_tenant_id_fkey {fk_def}"),
        ]
        if index_row is not None:
            table, name, indexdef = index_row
            mutations.append((table, f"DROP INDEX {name}; {indexdef} WHERE (id > 0)", f"DROP INDEX {name}; {indexdef}"))
        assert len(mutations) == 3, "negative control needs a plain btree index on a retained-branch or catalog table"
        for table, mutate, restore in mutations:
            with eng.begin() as conn:
                for stmt in mutate.split(";"):
                    conn.execute(text(stmt))
            assert schema_diff(schema_snapshot(url), baseline) == [table], f"definition change on {table} was not detected"
            with eng.begin() as conn:
                for stmt in restore.split(";"):
                    conn.execute(text(stmt))
            assert schema_diff(schema_snapshot(url), baseline) == [], f"restored definition on {table} still differs"
    finally:
        eng.dispose()
