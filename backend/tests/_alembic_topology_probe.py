"""Test-only probes for the multi-head Alembic chain proofs (disposable PostgreSQL only).

The three Shopify chain proofs build the deployed topology ``{0111, 0113, 0120}``
by genuine ``alembic upgrade`` calls and then step through 0121, 0122 and
0123 with branch-qualified ``@-1`` rollbacks. These helpers let them assert,
after every step:

* the **exact** ``alembic_version`` rows (count and ids, duplicates included);
* that the non-Shopify schema is unchanged — tables, columns, primary keys,
  unique, check and foreign-key constraints and indexes, compared by
  definition;
* that synthetic sentinel rows planted in generic, catalog and retained
  0111 / 0113 branch tables survive untouched.

Nothing here stamps, merges, fabricates or deletes version rows, and nothing
prints a token column, a DSN or a row: failures name tables only.
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Any, Dict, List, Tuple

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

S0 = frozenset({"0111", "0113", "0120"})
S1 = frozenset({"0111", "0113", "0121"})
S2 = frozenset({"0111", "0113", "0122"})
S3 = frozenset({"0111", "0113", "0123"})
RETAINED = frozenset({"0111", "0113"})

_SYNTHETIC_TOKEN = "synthetic-ciphertext-placeholder-not-a-token"


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
    """Definition-level snapshot of every table outside the excluded set."""
    eng = _engine(url)
    try:
        insp = inspect(eng)
        snap: Dict[str, Any] = {}
        for table in sorted(insp.get_table_names()):
            if table in exclude or table.startswith(exclude_prefixes):
                continue
            pk = insp.get_pk_constraint(table)
            snap[table] = {
                "columns": [(c["name"], str(c["type"]), bool(c["nullable"]), str(c.get("default"))) for c in insp.get_columns(table)],
                "primary_key": (pk.get("name"), list(pk.get("constrained_columns") or [])),
                "unique": sorted((u.get("name"), list(u["column_names"])) for u in insp.get_unique_constraints(table)),
                "check": sorted((c.get("name"), c.get("sqltext")) for c in insp.get_check_constraints(table)),
                "foreign_keys": sorted((f.get("name"), list(f["constrained_columns"]), f["referred_table"], list(f["referred_columns"]),
                                        (f.get("options") or {}).get("ondelete")) for f in insp.get_foreign_keys(table)),
                "indexes": sorted((i.get("name"), list(i["column_names"]), bool(i.get("unique"))) for i in insp.get_indexes(table)),
            }
        return snap
    finally:
        eng.dispose()


def assert_schema_unchanged(url: str, baseline: Dict[str, Any], step: str) -> None:
    now = schema_snapshot(url)
    if now != baseline:
        changed = sorted(t for t in set(now) | set(baseline) if now.get(t) != baseline.get(t))
        raise AssertionError(f"{step}: non-Shopify schema changed for tables {changed}")


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
        return {
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


def assert_sentinels(url: str, planted: Dict[str, Any], step: str) -> None:
    """Every planted row still exists with the planted values; the token column is compared by digest only."""
    eng = _engine(url)
    try:
        with eng.connect() as conn:
            for table, spec in planted.items():
                if table in ("tag", "token_sha256"):
                    continue
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
