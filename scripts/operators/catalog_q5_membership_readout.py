#!/usr/bin/env python3
"""
scripts/operators/catalog_q5_membership_readout.py
──────────────────────────────────────────────────
ق-5 — read-only production SELECTs that settle the *publication source* and
the *store ownership* of the items found in a Meta catalog export (ق-4).

What it answers, for one Meta catalog id (default ``871742015873294``):

  1. ``meta_catalog_memberships`` rows of that catalog — tenant, product,
     variant, ``retailer_id``, ``meta_item_id``, ``provenance``; every
     membership (any catalog) whose ``retailer_id`` is one of the exported
     Content IDs; every membership whose ``meta_item_id`` is one of the
     exported Meta item ids (the two columns are matched separately).
  2. Local products ``176``–``182`` (the ``nahla_p_<id>`` identities seen in
     the export): tenant, source, ownership, stamps (``meta_item_id``,
     ``meta_retailer_id``, ``canonical_retailer_id``, published/imported
     timestamps) and their variants; and every product or variant of **any**
     tenant that claims one of those ``nahla_p_*`` identities.
  3. The Salla store behind the exported product links (store marker, e.g.
     ``dev-cgcaqkpx5wgewsyv``): which tenant's integration / settings /
     knowledge snapshot / product URLs carry it, which tenant owns the
     ``external_id`` values embedded in the links (``/p1207801870`` and
     ``/p/1207801870`` forms; Nahla public links are never treated as Salla
     ids), and how that compares with the trial tenant's current store.
  4. WhatsApp connection catalog stamps and retirement ledger rows that
     mention the catalog (indicators only).

Schema preflight (read-only): before any data statement the script reads
``information_schema.columns`` for every table it touches and
``alembic_version``. A statement whose table or required column is absent
on this database (e.g. ``catalog_channel_retirements`` before migration
0118, or a column added after the pinned ``alembic upgrade``) is **skipped
and recorded** under ``skipped`` with the missing names; optional columns
are dropped from the SELECT list and listed under
``schema_preflight.columns_missing``. Nothing is created or migrated.

Guarantees:
  * **SELECT only.** The session is read-only (``set_session(readonly=True)``,
    ``SET default_transaction_read_only = on``) and every statement is
    asserted to start with ``SELECT`` before execution; the transaction is
    rolled back at the end.
  * **No secrets.** No token, secret, whole ``config``/``store_settings``/
    ``extra_metadata`` column is ever selected; the rendered output is
    scanned for token/DSN shapes and the run aborts if any appear.
  * **No Graph, no Salla, no network** besides the database.
  * ``DATABASE_URL`` is read from the container environment and never
    printed.

Runtime: Python 3.11 + psycopg2 (both in the production image); PostgreSQL
≥ 9.6 SQL only (``FILTER``, ``jsonb_typeof``, ``jsonb_object_keys``, ``?``).

Usage (inside the production container, from ``/app``):

    python /tmp/catalog_q5_membership_readout.py \
        --catalog-id 871742015873294 --tenant-id 35 --product-ids 176-182 \
        --content-ids-file /tmp/q4_content_ids.txt \
        --links-file /tmp/q4_links.txt \
        --meta-item-ids-file /tmp/q4_meta_item_ids.txt \
        --store-marker dev-cgcaqkpx5wgewsyv --require-inputs --pretty

``--print-sql`` prints every statement (assuming a full schema) and exits
without connecting. ``--require-inputs`` refuses to run when any of the three
export files is missing or empty.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

READ_ONLY = True
SECRET_SHAPES = ("EAA", "postgres://", "postgresql://", "access_token", "app_secret", "Bearer ")
NAHLA_PUBLIC_PATH = "/public/catalog/items/"
_SALLA_P_SEGMENT = re.compile(r"^p(\d+)$")
_DIGITS = re.compile(r"^\d+$")

Schema = Dict[str, Set[str]]  # table -> columns present

# ── identity helpers ────────────────────────────────────────────────────────


def parse_id_range(spec: str) -> List[int]:
    """``"176-182,190"`` → ``[176, …, 182, 190]``."""
    out: List[int] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    return sorted(set(out))


def read_lines(path: Optional[str]) -> List[str]:
    if not path:
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if ln.strip() and not ln.strip().startswith("#")]


def is_nahla_public_link(link: str) -> bool:
    try:
        return NAHLA_PUBLIC_PATH in (urlparse(link.strip()).path or "")
    except ValueError:
        return False


def nahla_public_ids_from_links(links: Sequence[str]) -> List[str]:
    """``…/public/catalog/items/nahla_p_176`` → ``nahla_p_176`` (Nahla identities, not Salla ids)."""
    out: List[str] = []
    for raw in links:
        if not is_nahla_public_link(raw):
            continue
        path = urlparse(raw.strip()).path
        rid = path.split(NAHLA_PUBLIC_PATH, 1)[1].split("/", 1)[0].strip()
        if rid and rid not in out:
            out.append(rid)
    return out


def external_ids_from_links(links: Sequence[str]) -> List[str]:
    """Salla product ids embedded in exported product links.

    Accepted forms (Salla emits both):
      * ``…/p1207801870``   — ``p`` glued to the numeric id (actual ق-4 links)
      * ``…/p/1207801870``  — ``p`` as its own segment (``store_sync`` emits
        ``{store_url}/p/{external_id}``)
      * otherwise a purely numeric last segment.
    Nahla public links (``/public/catalog/items/…``) are **excluded**: their
    identity is a Nahla ``retailer_id``, never a Salla id. Query strings and
    fragments are ignored.
    """
    out: List[str] = []
    for raw in links:
        raw = raw.strip()
        if not raw or is_nahla_public_link(raw):
            continue
        try:
            path = urlparse(raw).path or ""
        except ValueError:
            continue
        segs = [s for s in path.split("/") if s]
        if not segs:
            continue
        ext: Optional[str] = None
        for i, seg in enumerate(segs):
            if seg == "p" and i + 1 < len(segs) and _DIGITS.match(segs[i + 1]):
                ext = segs[i + 1]
                break
            m = _SALLA_P_SEGMENT.match(seg)
            if m:
                ext = m.group(1)
                break
        if ext is None and _DIGITS.match(segs[-1]):
            ext = segs[-1]
        if ext and ext not in out:
            out.append(ext)
    return out


def store_markers_from_links(links: Sequence[str]) -> List[str]:
    """Store slugs seen in the links: ``dev-xxxx`` path segments and the first
    label of ``<slug>.salla.sa`` hosts. Nahla public links contribute nothing."""
    out: List[str] = []
    for raw in links:
        if is_nahla_public_link(raw):
            continue
        try:
            parsed = urlparse(raw.strip())
        except ValueError:
            continue
        host = parsed.netloc.lower()
        cands: List[str] = []
        if host.endswith(".salla.sa") and host.count(".") >= 2:
            cands.append(host.split(".", 1)[0])
        cands.extend(s for s in parsed.path.split("/")[:2] if s)
        for cand in cands:
            if cand and cand not in out and cand != "p" and not _SALLA_P_SEGMENT.match(cand):
                out.append(cand)
    return out


# ── statements (SELECT only; parameters are bound, never interpolated) ─────


def _avail(schema: Optional[Schema], table: str, cols: Sequence[str]) -> List[str]:
    if schema is None:
        return list(cols)
    present = schema.get(table, set())
    return [c for c in cols if c in present]


def _missing(schema: Optional[Schema], table: str, cols: Sequence[str]) -> List[str]:
    if schema is None:
        return []
    present = schema.get(table, set())
    return [c for c in cols if c not in present]


def _select_list(alias: str, cols: Sequence[str]) -> str:
    return ", ".join(f"{alias}.{c}" for c in cols)


PRODUCT_OPTIONAL_COLS = [
    "external_id", "source", "ownership_mode", "source_external_id", "meta_retailer_id",
    "canonical_retailer_id", "meta_item_id", "meta_catalog_published_at", "imported_at",
    "managed_confirmed_at", "managed_confirmed_by", "sync_status", "catalog_status",
    "merchant_hidden_at", "meta_last_seen_at", "meta_removed_at", "archived_at",
    "has_variants", "in_stock", "created_at",
]
VARIANT_OPTIONAL_COLS = ["salla_variant_id", "retailer_id", "sku", "is_default", "in_stock", "created_at"]
MEMBERSHIP_OPTIONAL_COLS = ["product_id", "variant_id", "salla_variant_id", "meta_item_id", "verified_at"]
WA_OPTIONAL_COLS = [
    "status", "connection_type", "whatsapp_business_account_id", "business_manager_id",
    "meta_business_account_id", "phone_number_id", "meta_catalog_id", "catalog_enabled",
    "meta_import_status", "meta_import_last_at", "meta_import_token_source", "connected_at",
]
TENANT_OPTIONAL_COLS = ["name", "domain", "is_active", "is_platform_tenant", "created_at"]
RETIREMENT_OPTIONAL_COLS = ["catalog_id", "meta_item_id", "product_id", "reason", "status", "attempts", "created_at", "done_at"]

ALL_TABLES = [
    "meta_catalog_memberships", "catalog_channel_retirements", "products", "product_variants",
    "integrations", "tenant_settings", "store_knowledge_snapshots", "tenants",
    "whatsapp_connections", "alembic_version",
]

SCHEMA_PROBE_SQL = """
SELECT table_name, column_name
FROM information_schema.columns
WHERE table_schema = current_schema() AND table_name = ANY(%(tables)s)
ORDER BY table_name, ordinal_position"""
ALEMBIC_SQL = "SELECT version_num FROM alembic_version ORDER BY version_num"


def build_statements(
    *,
    catalog_id: str,
    tenant_id: int,
    product_ids: Sequence[int],
    content_ids: Sequence[str],
    link_external_ids: Sequence[str],
    meta_item_ids: Sequence[str],
    store_marker: str,
    schema: Optional[Schema] = None,
) -> List[Dict[str, Any]]:
    """Return the statements to run. Each carries ``requires`` (hard table/column
    needs) and ``optional_missing`` (columns dropped from its SELECT list because
    this schema lacks them). ``schema=None`` assumes a full schema (``--print-sql``)."""
    nahla_ids = [f"nahla_p_{pid}" for pid in product_ids]
    pids = list(product_ids) or [-1]
    cids = list(content_ids) or ["__none__"]
    exts = list(link_external_ids) or ["__none__"]
    mids = list(meta_item_ids) or ["__none__"]
    nids = nahla_ids or ["__none__"]
    marker_like = f"%{store_marker}%" if store_marker else "%__no_marker__%"

    m_cols = ["id", "tenant_id", "catalog_id", "retailer_id", "provenance"] + _avail(schema, "meta_catalog_memberships", MEMBERSHIP_OPTIONAL_COLS)
    m_missing = _missing(schema, "meta_catalog_memberships", MEMBERSHIP_OPTIONAL_COLS)
    p_cols = ["id", "tenant_id"] + _avail(schema, "products", PRODUCT_OPTIONAL_COLS)
    p_missing = _missing(schema, "products", PRODUCT_OPTIONAL_COLS)
    v_cols = ["id", "tenant_id", "product_id"] + _avail(schema, "product_variants", VARIANT_OPTIONAL_COLS)
    v_missing = _missing(schema, "product_variants", VARIANT_OPTIONAL_COLS)
    w_cols = ["tenant_id"] + _avail(schema, "whatsapp_connections", WA_OPTIONAL_COLS)
    w_missing = _missing(schema, "whatsapp_connections", WA_OPTIONAL_COLS)
    t_cols = ["id"] + _avail(schema, "tenants", TENANT_OPTIONAL_COLS)
    t_missing = _missing(schema, "tenants", TENANT_OPTIONAL_COLS)
    r_cols = ["id", "tenant_id", "retailer_id"] + _avail(schema, "catalog_channel_retirements", RETIREMENT_OPTIONAL_COLS)
    r_missing = _missing(schema, "catalog_channel_retirements", RETIREMENT_OPTIONAL_COLS)

    has = lambda table, col: schema is None or col in schema.get(table, set())  # noqa: E731
    p_has_meta_item = has("products", "meta_item_id")
    p_has_published = has("products", "meta_catalog_published_at")
    p_has_imported = has("products", "imported_at")
    p_has_archived = has("products", "archived_at")
    p_has_src_ext = has("products", "source_external_id")
    p_claim_cols = [c for c in ("meta_retailer_id", "canonical_retailer_id") if has("products", c)]
    r_filter = "r.catalog_id = %(catalog_id)s OR r.retailer_id = ANY(%(content_ids)s)" if has("catalog_channel_retirements", "catalog_id") else "r.retailer_id = ANY(%(content_ids)s)"

    stamp_filters = ["COUNT(*) AS stamped_products"]
    if p_has_published:
        stamp_filters.append("COUNT(*) FILTER (WHERE p.meta_catalog_published_at IS NOT NULL) AS with_published_at")
        stamp_filters.append("MIN(p.meta_catalog_published_at) AS first_published_at")
        stamp_filters.append("MAX(p.meta_catalog_published_at) AS last_published_at")
    if p_has_imported:
        stamp_filters.append("COUNT(*) FILTER (WHERE p.imported_at IS NOT NULL) AS with_imported_at")
    if p_has_archived:
        stamp_filters.append("COUNT(*) FILTER (WHERE p.archived_at IS NOT NULL) AS archived")

    claim_product_sql = ""
    if p_claim_cols:
        claimed = " OR ".join(f"p.{c} = ANY(%(nahla_ids)s)" for c in p_claim_cols)
        coalesce = "COALESCE(" + ", ".join(f"p.{c}" for c in p_claim_cols) + ")" if len(p_claim_cols) > 1 else f"p.{p_claim_cols[0]}"
        claim_product_sql = f"""
SELECT 'product' AS kind, p.id, p.tenant_id, p.id AS product_id, NULL::int AS variant_id,
       {coalesce} AS claimed_retailer_id,
       {'p.meta_item_id' if p_has_meta_item else 'NULL::varchar'} AS meta_item_id,
       {'p.source' if has('products', 'source') else 'NULL::varchar'} AS source,
       {'p.archived_at' if p_has_archived else 'NULL::timestamptz'} AS archived_at
FROM products p
WHERE {claimed}
UNION ALL"""

    ext_filter = "p.external_id = ANY(%(ext_ids)s)" if has("products", "external_id") else "FALSE"
    if p_has_src_ext:
        ext_filter += " OR p.source_external_id = ANY(%(ext_ids)s)"

    return [
        {
            "key": "memberships_for_catalog",
            "question": "من يملك عضويات هذا الكتالوج، وبأي مصدر نشر؟",
            "requires": {"meta_catalog_memberships": ["tenant_id", "catalog_id", "retailer_id", "provenance"]},
            "optional_missing": {"meta_catalog_memberships": m_missing},
            "sql": f"""
SELECT {_select_list('m', m_cols)}
FROM meta_catalog_memberships m
WHERE m.catalog_id = %(catalog_id)s
ORDER BY m.tenant_id, m.retailer_id""",
            "params": {"catalog_id": catalog_id},
        },
        {
            "key": "memberships_summary_all_catalogs",
            "question": "أي كتالوجات أخرى لها عضويات، ولأي مستأجر وبأي مصدر؟ (عدّ فقط)",
            "requires": {"meta_catalog_memberships": ["tenant_id", "catalog_id", "provenance"]},
            "optional_missing": {},
            "sql": f"""
SELECT m.catalog_id, m.tenant_id, m.provenance, COUNT(*) AS rows_count
       {', COUNT(m.meta_item_id) AS with_meta_item_id' if has('meta_catalog_memberships', 'meta_item_id') else ''}
FROM meta_catalog_memberships m
GROUP BY m.catalog_id, m.tenant_id, m.provenance
ORDER BY m.catalog_id, m.tenant_id, m.provenance""",
            "params": {},
        },
        {
            "key": "memberships_matching_q4_content_ids",
            "question": "هل تظهر أي من هويات التصدير (Content ID = retailer_id) في عضويات أي كتالوج؟",
            "requires": {"meta_catalog_memberships": ["tenant_id", "catalog_id", "retailer_id", "provenance"]},
            "optional_missing": {"meta_catalog_memberships": m_missing},
            "sql": f"""
SELECT {_select_list('m', m_cols)}
FROM meta_catalog_memberships m
WHERE m.retailer_id = ANY(%(content_ids)s)
ORDER BY m.catalog_id, m.tenant_id, m.retailer_id""",
            "params": {"content_ids": cids},
        },
        {
            "key": "memberships_matching_q4_meta_item_ids",
            "question": "هل يظهر أي معرّف عنصر Meta من التصدير في عضويات أي كتالوج؟ (مطابقة منفصلة عن Content ID)",
            "requires": {"meta_catalog_memberships": ["tenant_id", "catalog_id", "retailer_id", "provenance", "meta_item_id"]},
            "optional_missing": {"meta_catalog_memberships": m_missing},
            "sql": f"""
SELECT {_select_list('m', m_cols)}
FROM meta_catalog_memberships m
WHERE m.meta_item_id = ANY(%(meta_item_ids)s)
ORDER BY m.catalog_id, m.tenant_id, m.retailer_id""",
            "params": {"meta_item_ids": mids},
        },
        {
            "key": "retirements_for_catalog",
            "question": "هل سُحب أي عنصر من هذا الكتالوج عبر سجل السحب الدائم؟ (الجدول قد لا يوجد قبل 0118)",
            "requires": {"catalog_channel_retirements": ["tenant_id", "retailer_id"]},
            "optional_missing": {"catalog_channel_retirements": r_missing},
            "sql": f"""
SELECT {_select_list('r', r_cols)}
FROM catalog_channel_retirements r
WHERE {r_filter}
ORDER BY r.id""",
            "params": {"catalog_id": catalog_id, "content_ids": cids},
        },
        {
            "key": "products_by_id",
            "question": "لمن المنتجات المحلية ذات المعرّفات المطلوبة، وما مصدرها وأختامها؟",
            "requires": {"products": ["id", "tenant_id", "title", "metadata"]},
            "optional_missing": {"products": p_missing},
            "sql": f"""
SELECT {_select_list('p', p_cols)}, LEFT(p.title, 80) AS title_80,
       p.metadata ->> 'product_url' AS md_product_url,
       p.metadata ->> 'url' AS md_url,
       p.metadata ->> 'store_url' AS md_store_url,
       p.metadata ->> 'source_status' AS md_source_status,
       p.metadata ->> 'source_event_at' AS md_source_event_at,
       (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(
           CASE WHEN jsonb_typeof(p.metadata) = 'object' THEN p.metadata ELSE '{{}}'::jsonb END) k) AS md_keys,
       (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(
           CASE WHEN jsonb_typeof(p.metadata -> 'sync_meta') = 'object' THEN p.metadata -> 'sync_meta' ELSE '{{}}'::jsonb END) k) AS sync_meta_keys
FROM products p
WHERE p.id = ANY(%(product_ids)s)
ORDER BY p.id""",
            "params": {"product_ids": pids},
        },
        {
            "key": "variants_of_products",
            "question": "متغيرات تلك المنتجات وهوياتها",
            "requires": {"product_variants": ["id", "tenant_id", "product_id"]},
            "optional_missing": {"product_variants": v_missing},
            "sql": f"""
SELECT {_select_list('v', v_cols)}
FROM product_variants v
WHERE v.product_id = ANY(%(product_ids)s)
ORDER BY v.product_id, v.id""",
            "params": {"product_ids": pids},
        },
        {
            "key": "claims_on_nahla_identities",
            "question": "هل يدّعي أي منتج أو متغير (لأي مستأجر) هوية nahla_p_* من التصدير؟",
            "requires": {"product_variants": ["id", "tenant_id", "product_id", "retailer_id"], "products": ["id", "tenant_id"]},
            "optional_missing": {"products": [c for c in ("meta_retailer_id", "canonical_retailer_id") if c not in p_claim_cols]},
            "sql": f"""{claim_product_sql}
SELECT 'variant' AS kind, v.id, v.tenant_id, v.product_id, v.id AS variant_id,
       v.retailer_id AS claimed_retailer_id, NULL::varchar AS meta_item_id,
       NULL::varchar AS source, NULL::timestamptz AS archived_at
FROM product_variants v
WHERE v.retailer_id = ANY(%(nahla_ids)s)
ORDER BY kind, tenant_id, id""",
            "params": {"nahla_ids": nids},
        },
        {
            "key": "product_stamp_indicator_by_tenant",
            "question": "أي مستأجرين لديهم ختم products.meta_item_id (مؤشر فقط، ليس دليل نشر)؟",
            "requires": {"products": ["tenant_id", "meta_item_id"]},
            "optional_missing": {"products": [c for c in ("meta_catalog_published_at", "imported_at", "archived_at") if c in p_missing]},
            "sql": f"""
SELECT p.tenant_id, {', '.join(stamp_filters)}
FROM products p
WHERE p.meta_item_id IS NOT NULL AND p.meta_item_id <> ''
GROUP BY p.tenant_id
ORDER BY p.tenant_id""",
            "params": {},
        },
        {
            "key": "products_stamped_with_export_meta_item_ids",
            "question": "هل يحمل أي منتج محلي معرّف عنصر Meta من عمود المعرّف في التصدير؟",
            "requires": {"products": ["id", "tenant_id", "title", "meta_item_id"]},
            "optional_missing": {"products": p_missing},
            "sql": f"""
SELECT {_select_list('p', p_cols)}, LEFT(p.title, 80) AS title_80
FROM products p
WHERE p.meta_item_id = ANY(%(meta_item_ids)s)
ORDER BY p.tenant_id, p.id""",
            "params": {"meta_item_ids": mids},
        },
        {
            "key": "products_matching_link_external_ids",
            "question": "لمن المنتجات التي تحمل معرّفات سلة المضمّنة في روابط التصدير (بما فيها المؤرشفة)؟",
            "requires": {"products": ["id", "tenant_id", "title", "external_id", "metadata"]},
            "optional_missing": {"products": p_missing},
            "sql": f"""
SELECT {_select_list('p', p_cols)}, LEFT(p.title, 80) AS title_80,
       p.metadata ->> 'product_url' AS md_product_url
FROM products p
WHERE {ext_filter}
ORDER BY p.tenant_id, p.id""",
            "params": {"ext_ids": exts},
        },
        {
            "key": "store_identity_integrations",
            "question": "أي مستأجر يملك تكامل سلة، وبأي معرّف متجر واسم ورابط؟ (أعمدة محددة، بلا رموز)",
            "requires": {"integrations": ["id", "tenant_id", "provider", "external_store_id", "config"]},
            "optional_missing": {"integrations": _missing(schema, "integrations", ["enabled"])},
            "sql": f"""
SELECT i.id, i.tenant_id, i.provider, i.external_store_id{', i.enabled' if has('integrations', 'enabled') else ''},
       i.config ->> 'store_id' AS cfg_store_id,
       i.config ->> 'store_name' AS cfg_store_name,
       i.config ->> 'store_url' AS cfg_store_url,
       i.config ->> 'domain' AS cfg_domain,
       i.config ->> 'last_seen' AS cfg_last_seen
FROM integrations i
WHERE i.provider ILIKE '%%salla%%'
ORDER BY i.tenant_id, i.id""",
            "params": {},
        },
        {
            "key": "store_identity_settings",
            "question": "رابط المتجر واسمه في إعدادات كل مستأجر وفي ملف سلة المزامَن (أعمدة محددة)",
            "requires": {"tenant_settings": ["tenant_id", "store_settings"]},
            "optional_missing": {},
            "sql": """
SELECT ts.tenant_id,
       ts.store_settings ->> 'store_url' AS store_url,
       ts.store_settings ->> 'store_name' AS store_name,
       ts.store_settings -> 'salla_store_info' ->> 'store_id' AS salla_store_id,
       ts.store_settings -> 'salla_store_info' ->> 'name' AS salla_store_name,
       ts.store_settings -> 'salla_store_info' ->> 'username' AS salla_username,
       ts.store_settings -> 'salla_store_info' ->> 'url' AS salla_url,
       ts.store_settings -> 'salla_store_info' ->> 'domain' AS salla_domain,
       ts.store_settings -> 'salla_store_info' ->> 'fetched_at' AS salla_fetched_at
FROM tenant_settings ts
WHERE ts.store_settings IS NOT NULL
  AND (ts.store_settings ? 'store_url' OR ts.store_settings ? 'salla_store_info')
ORDER BY ts.tenant_id""",
            "params": {},
        },
        {
            "key": "store_identity_knowledge_snapshot",
            "question": "رابط المتجر كما سُجّل في لقطة المعرفة لكل مستأجر",
            "requires": {"store_knowledge_snapshots": ["tenant_id", "store_profile"]},
            "optional_missing": {},
            "sql": """
SELECT s.tenant_id,
       s.store_profile ->> 'store_url' AS store_url,
       s.store_profile ->> 'store_name' AS store_name
FROM store_knowledge_snapshots s
WHERE s.store_profile IS NOT NULL
ORDER BY s.tenant_id""",
            "params": {},
        },
        {
            "key": "store_marker_hits",
            "question": "أين يظهر معرّف المتجر الوارد في روابط التصدير داخل قاعدة البيانات؟ (عدّ لكل مستأجر)",
            "requires": {
                "integrations": ["tenant_id", "config", "external_store_id"],
                "tenant_settings": ["tenant_id", "store_settings"],
                "store_knowledge_snapshots": ["tenant_id", "store_profile"],
                "products": ["tenant_id", "metadata"],
                "tenants": ["id", "domain"],
            },
            "optional_missing": {},
            "sql": """
SELECT 'integrations' AS place, i.tenant_id, COUNT(*) AS hits
FROM integrations i
WHERE (i.config ->> 'store_url') ILIKE %(marker)s OR (i.config ->> 'domain') ILIKE %(marker)s
   OR i.external_store_id ILIKE %(marker)s OR (i.config ->> 'store_id') ILIKE %(marker)s
GROUP BY i.tenant_id
UNION ALL
SELECT 'tenant_settings' AS place, ts.tenant_id, COUNT(*) AS hits
FROM tenant_settings ts
WHERE (ts.store_settings ->> 'store_url') ILIKE %(marker)s
   OR (ts.store_settings -> 'salla_store_info' ->> 'url') ILIKE %(marker)s
   OR (ts.store_settings -> 'salla_store_info' ->> 'domain') ILIKE %(marker)s
   OR (ts.store_settings -> 'salla_store_info' ->> 'username') ILIKE %(marker)s
GROUP BY ts.tenant_id
UNION ALL
SELECT 'store_knowledge_snapshots' AS place, s.tenant_id, COUNT(*) AS hits
FROM store_knowledge_snapshots s
WHERE (s.store_profile ->> 'store_url') ILIKE %(marker)s
GROUP BY s.tenant_id
UNION ALL
SELECT 'products.metadata urls' AS place, p.tenant_id, COUNT(*) AS hits
FROM products p
WHERE (p.metadata ->> 'product_url') ILIKE %(marker)s OR (p.metadata ->> 'url') ILIKE %(marker)s
   OR (p.metadata ->> 'store_url') ILIKE %(marker)s
GROUP BY p.tenant_id
UNION ALL
SELECT 'tenants.domain' AS place, t.id AS tenant_id, COUNT(*) AS hits
FROM tenants t
WHERE t.domain ILIKE %(marker)s
GROUP BY t.id
ORDER BY place, tenant_id""",
            "params": {"marker": marker_like},
        },
        {
            "key": "tenants_involved",
            "question": "هوية المستأجرين الظاهرين في النتائج (الاسم والنطاق والحالة فقط)",
            "requires": {
                "tenants": ["id"],
                "meta_catalog_memberships": ["tenant_id", "catalog_id"],
                "products": ["tenant_id", "id", "external_id"],
                "tenant_settings": ["tenant_id", "store_settings"],
                "integrations": ["tenant_id", "config"],
            },
            "optional_missing": {"tenants": t_missing},
            "sql": f"""
SELECT {_select_list('t', t_cols)}
FROM tenants t
WHERE t.id = %(tenant_id)s
   OR t.id IN (SELECT m.tenant_id FROM meta_catalog_memberships m WHERE m.catalog_id = %(catalog_id)s)
   OR t.id IN (SELECT p.tenant_id FROM products p WHERE p.id = ANY(%(product_ids)s))
   OR t.id IN (SELECT p.tenant_id FROM products p WHERE {ext_filter})
   OR t.id IN (SELECT ts.tenant_id FROM tenant_settings ts
               WHERE (ts.store_settings ->> 'store_url') ILIKE %(marker)s)
   OR t.id IN (SELECT i.tenant_id FROM integrations i
               WHERE (i.config ->> 'store_url') ILIKE %(marker)s OR (i.config ->> 'domain') ILIKE %(marker)s)
ORDER BY t.id""",
            "params": {
                "tenant_id": tenant_id,
                "catalog_id": catalog_id,
                "product_ids": pids,
                "ext_ids": exts,
                "marker": marker_like,
            },
        },
        {
            "key": "whatsapp_connection_catalog_stamps",
            "question": "أي اتصال واتساب يحمل ختم كتالوج، وما حالته؟ (بلا رموز)",
            "requires": {"whatsapp_connections": ["tenant_id", "meta_catalog_id"]},
            "optional_missing": {"whatsapp_connections": w_missing},
            "sql": f"""
SELECT {_select_list('w', w_cols)}
FROM whatsapp_connections w
WHERE w.meta_catalog_id IS NOT NULL OR w.tenant_id = %(tenant_id)s
ORDER BY w.tenant_id""",
            "params": {"tenant_id": tenant_id},
        },
    ]


def plan_statements(statements: List[Dict[str, Any]], schema: Schema) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Split statements into runnable ones and skipped ones (with the reason)."""
    runnable: List[Dict[str, Any]] = []
    skipped: Dict[str, str] = {}
    for st in statements:
        reasons: List[str] = []
        for table, cols in st["requires"].items():
            if table not in schema:
                reasons.append(f"table_missing:{table}")
                continue
            missing = [c for c in cols if c not in schema[table]]
            if missing:
                reasons.append(f"columns_missing:{table}:{','.join(missing)}")
        if reasons:
            skipped[st["key"]] = "; ".join(reasons)
        else:
            runnable.append(st)
    return runnable, skipped


# ── execution ───────────────────────────────────────────────────────────────


def _assert_select_only(sql: str) -> None:
    head = sql.strip().split(None, 1)[0].upper()
    if head != "SELECT":
        raise RuntimeError(f"refusing non-SELECT statement: {head}")
    lowered = sql.lower()
    for forbidden in ("insert ", "update ", "delete ", "alter ", "drop ", "truncate ", "create ", "grant "):
        for line in lowered.splitlines():
            if line.strip().startswith(forbidden):
                raise RuntimeError(f"refusing statement containing write keyword: {forbidden.strip()}")


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _default_connect(database_url: str):
    import psycopg2  # noqa: PLC0415 — only needed inside the container
    import psycopg2.extras  # noqa: PLC0415

    conn = psycopg2.connect(database_url, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.set_session(readonly=True, autocommit=False)
    return conn


def run(
    build: Callable[[Optional[Schema]], List[Dict[str, Any]]],
    database_url: str,
    *,
    connect: Callable[[str], Any] = _default_connect,
) -> Dict[str, Any]:
    """Probe the schema read-only, build the schema-aware statements, run the
    runnable ones, record the skipped ones. ``build(schema)`` returns the
    statements for the probed schema."""
    out: Dict[str, Any] = {"read_only": READ_ONLY, "secrets_included": False}
    conn = connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SET default_transaction_read_only = on")
            cur.execute("SET statement_timeout = '30s'")
            cur.execute(SCHEMA_PROBE_SQL, {"tables": ALL_TABLES})
            schema: Schema = {}
            for row in cur.fetchall():
                r = dict(row)
                schema.setdefault(str(r["table_name"]), set()).add(str(r["column_name"]))
            alembic: List[str] = []
            if "alembic_version" in schema:
                cur.execute(ALEMBIC_SQL)
                alembic = [str(dict(r)["version_num"]) for r in cur.fetchall()]

            statements = build(schema)
            runnable, skipped = plan_statements(statements, schema)
            columns_missing: Dict[str, List[str]] = {}
            for st in statements:
                for table, cols in (st.get("optional_missing") or {}).items():
                    for c in cols:
                        if table in schema and c not in columns_missing.setdefault(table, []):
                            columns_missing[table].append(c)
            out["schema_preflight"] = {
                "tables_present": sorted(t for t in ALL_TABLES if t in schema),
                "tables_missing": sorted(t for t in ALL_TABLES if t not in schema),
                "columns_missing": {t: sorted(c) for t, c in columns_missing.items() if c},
                "alembic_version": alembic,
                "nothing_created_or_migrated": True,
            }
            out["skipped"] = skipped
            results: Dict[str, Any] = {}
            for st in runnable:
                _assert_select_only(st["sql"])
                cur.execute(st["sql"], st["params"])
                rows = [dict(r) for r in cur.fetchall()]
                results[st["key"]] = {
                    "question": st["question"],
                    "optional_columns_missing": {t: c for t, c in (st.get("optional_missing") or {}).items() if c},
                    "row_count": len(rows),
                    "rows": rows,
                }
            out["results"] = results
        conn.rollback()
    finally:
        conn.close()
    return out


def render(payload: Dict[str, Any], *, pretty: bool) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=_json_default, indent=2 if pretty else None)
    for shape in SECRET_SHAPES:
        if shape in text:
            raise RuntimeError(f"output contains a secret-like shape ({shape!r}); not printing")
    return text


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog-id", default="871742015873294")
    ap.add_argument("--tenant-id", type=int, default=35)
    ap.add_argument("--product-ids", default="176-182", help="local product ids seen as nahla_p_<id> in the export")
    ap.add_argument("--content-ids-file", help="one exported Content ID (= retailer_id) per line")
    ap.add_argument("--links-file", help="one exported product link per line")
    ap.add_argument("--meta-item-ids-file", help="one exported Meta item id per line (separate from Content ID)")
    ap.add_argument("--store-marker", default="", help="store slug seen in the links, e.g. dev-cgcaqkpx5wgewsyv")
    ap.add_argument("--require-inputs", action="store_true", help="refuse to run unless all three export files are present and non-empty")
    ap.add_argument("--print-sql", action="store_true")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args(argv)

    content_ids = read_lines(args.content_ids_file)
    links = read_lines(args.links_file)
    meta_item_ids = read_lines(args.meta_item_ids_file)
    if args.require_inputs and not (content_ids and links and meta_item_ids):
        print(
            "missing inputs: --content-ids-file, --links-file and --meta-item-ids-file must all be present and non-empty",
            file=sys.stderr,
        )
        return 2
    ext_ids = external_ids_from_links(links)
    nahla_public_ids = nahla_public_ids_from_links(links)
    marker = args.store_marker.strip()
    detected_markers = [m for m in store_markers_from_links(links) if m.startswith("dev-")]
    if not marker and detected_markers:
        marker = detected_markers[0]

    def build(schema: Optional[Schema]) -> List[Dict[str, Any]]:
        return build_statements(
            catalog_id=args.catalog_id,
            tenant_id=args.tenant_id,
            product_ids=parse_id_range(args.product_ids),
            content_ids=content_ids,
            link_external_ids=ext_ids,
            meta_item_ids=meta_item_ids,
            store_marker=marker,
            schema=schema,
        )

    inputs = {
        "catalog_id": args.catalog_id,
        "tenant_id": args.tenant_id,
        "product_ids": parse_id_range(args.product_ids),
        "content_ids_count": len(content_ids),
        "links_count": len(links),
        "salla_link_count": len(links) - sum(1 for ln in links if is_nahla_public_link(ln)),
        "nahla_public_link_count": sum(1 for ln in links if is_nahla_public_link(ln)),
        "link_external_ids": ext_ids,
        "nahla_public_ids_from_links": nahla_public_ids,
        "meta_item_ids_count": len(meta_item_ids),
        "meta_item_ids_provided": bool(meta_item_ids),
        "store_marker": marker,
        "store_markers_detected": detected_markers,
    }

    if args.print_sql:
        statements = build(None)
        for st in statements:
            _assert_select_only(st["sql"])
        print(json.dumps({"inputs": inputs, "statements": [
            {"key": s["key"], "question": s["question"], "requires": s["requires"], "sql": s["sql"].strip()} for s in statements
        ]}, ensure_ascii=False, indent=2))
        return 0

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set in this environment", file=sys.stderr)
        return 2

    payload = run(build, database_url)
    payload["inputs"] = inputs
    payload["generated_at"] = datetime.utcnow().isoformat() + "Z"
    print(render(payload, pretty=args.pretty))
    print(
        f"q5-readout catalog={args.catalog_id} tenant={args.tenant_id} "
        f"executed={len(payload.get('results', {}))} skipped={len(payload.get('skipped', {}))} read_only=true",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
