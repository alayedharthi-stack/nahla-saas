#!/usr/bin/env python3
"""
scripts/operators/catalog_q5_membership_readout.py
──────────────────────────────────────────────────
ق-5 — read-only production SELECTs that settle the *publication source* and
the *store ownership* of the items found in a Meta catalog export (ق-4).

What it answers, for one Meta catalog id (default ``871742015873294``):

  1. ``meta_catalog_memberships`` rows of that catalog — tenant, product,
     variant, ``retailer_id``, ``meta_item_id``, ``provenance``; plus every
     membership (any catalog) whose ``retailer_id`` is one of the exported
     Content IDs.
  2. Local products ``176``–``182`` (the ``nahla_p_<id>`` identities seen in
     the export): tenant, source, ownership, stamps (``meta_item_id``,
     ``meta_retailer_id``, ``canonical_retailer_id``, published/imported
     timestamps) and their variants; and every product or variant of **any**
     tenant that claims one of those ``nahla_p_*`` identities.
  3. The Salla store behind the exported product links (store marker, e.g.
     ``dev-cgcaqkpx5wgewsyv``): which tenant's integration / settings /
     knowledge snapshot / product URLs carry it, which tenant owns the
     ``external_id`` values embedded in the links, and how that compares
     with the trial tenant's current store identity.
  4. WhatsApp connection catalog stamps and retirement ledger rows that
     mention the catalog (indicators only).

Guarantees:
  * **SELECT only.** The connection runs ``SET default_transaction_read_only
    = on`` and a ``READ ONLY`` transaction; every statement is asserted to
    start with ``SELECT`` before execution.
  * **No secrets.** No token, secret, whole ``config``/``store_settings``/
    ``extra_metadata`` column is ever selected; the rendered output is
    scanned for token/DSN shapes and the run aborts if any appear.
  * **No Graph, no Salla, no network** besides the database.
  * Exposes nothing by default: ``DATABASE_URL`` is read from the container
    environment and never printed.

Usage (inside the production container, from ``/app``):

    python /tmp/catalog_q5_membership_readout.py \
        --catalog-id 871742015873294 --tenant-id 35 \
        --product-ids 176-182 \
        --content-ids-file /tmp/q4_content_ids.txt \
        --links-file /tmp/q4_links.txt \
        --store-marker dev-cgcaqkpx5wgewsyv \
        [--meta-item-ids-file /tmp/q4_meta_item_ids.txt] --pretty

``--print-sql`` prints every statement and exits without connecting.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

READ_ONLY = True
SECRET_SHAPES = ("EAA", "postgres://", "postgresql://", "access_token", "app_secret", "Bearer ")

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


def external_ids_from_links(links: Sequence[str]) -> List[str]:
    """Salla product pages are emitted as ``{store_url}/p/{external_id}``.

    The ``/p/<id>`` segment is taken when present; otherwise the last path
    segment is used. Query strings and fragments are ignored.
    """
    out: List[str] = []
    for raw in links:
        try:
            path = urlparse(raw.strip()).path or ""
        except ValueError:
            continue
        segs = [s for s in path.split("/") if s]
        if not segs:
            continue
        ext = None
        for i, seg in enumerate(segs):
            if seg == "p" and i + 1 < len(segs):
                ext = segs[i + 1]
                break
        if ext is None:
            ext = segs[-1]
        ext = ext.strip()
        if ext and ext not in out:
            out.append(ext)
    return out


def store_markers_from_links(links: Sequence[str]) -> List[str]:
    """Hosts and first path segments of the links (e.g. ``dev-xxxx`` store slugs)."""
    out: List[str] = []
    for raw in links:
        try:
            parsed = urlparse(raw.strip())
        except ValueError:
            continue
        for cand in (parsed.netloc.lower(), *(s for s in parsed.path.split("/")[:2] if s)):
            if cand and cand not in out and cand != "p":
                out.append(cand)
    return out


# ── statements (SELECT only; parameters are bound, never interpolated) ─────


def build_statements(
    *,
    catalog_id: str,
    tenant_id: int,
    product_ids: Sequence[int],
    content_ids: Sequence[str],
    link_external_ids: Sequence[str],
    meta_item_ids: Sequence[str],
    store_marker: str,
) -> List[Dict[str, Any]]:
    nahla_ids = [f"nahla_p_{pid}" for pid in product_ids]
    pids = list(product_ids) or [-1]
    cids = list(content_ids) or ["__none__"]
    exts = list(link_external_ids) or ["__none__"]
    mids = list(meta_item_ids) or ["__none__"]
    nids = nahla_ids or ["__none__"]
    marker_like = f"%{store_marker}%" if store_marker else "%__no_marker__%"

    return [
        {
            "key": "memberships_for_catalog",
            "question": "من يملك عضويات هذا الكتالوج، وبأي مصدر نشر؟",
            "sql": """
SELECT m.id, m.tenant_id, m.product_id, m.variant_id, m.salla_variant_id,
       m.retailer_id, m.meta_item_id, m.provenance, m.verified_at
FROM meta_catalog_memberships m
WHERE m.catalog_id = %(catalog_id)s
ORDER BY m.tenant_id, m.retailer_id""",
            "params": {"catalog_id": catalog_id},
        },
        {
            "key": "memberships_summary_all_catalogs",
            "question": "أي كتالوجات أخرى لها عضويات، ولأي مستأجر وبأي مصدر؟ (عدّ فقط)",
            "sql": """
SELECT m.catalog_id, m.tenant_id, m.provenance, COUNT(*) AS rows_count,
       COUNT(m.meta_item_id) AS with_meta_item_id
FROM meta_catalog_memberships m
GROUP BY m.catalog_id, m.tenant_id, m.provenance
ORDER BY m.catalog_id, m.tenant_id, m.provenance""",
            "params": {},
        },
        {
            "key": "memberships_matching_q4_content_ids",
            "question": "هل تظهر أي من هويات التصدير (Content ID) في عضويات أي كتالوج آخر؟",
            "sql": """
SELECT m.id, m.tenant_id, m.catalog_id, m.product_id, m.variant_id,
       m.retailer_id, m.meta_item_id, m.provenance, m.verified_at
FROM meta_catalog_memberships m
WHERE m.retailer_id = ANY(%(content_ids)s)
ORDER BY m.catalog_id, m.tenant_id, m.retailer_id""",
            "params": {"content_ids": cids},
        },
        {
            "key": "retirements_for_catalog",
            "question": "هل سُحب أي عنصر من هذا الكتالوج عبر سجل السحب الدائم؟",
            "sql": """
SELECT r.id, r.tenant_id, r.catalog_id, r.retailer_id, r.meta_item_id, r.product_id,
       r.reason, r.status, r.attempts, r.created_at, r.done_at
FROM catalog_channel_retirements r
WHERE r.catalog_id = %(catalog_id)s OR r.retailer_id = ANY(%(content_ids)s)
ORDER BY r.id""",
            "params": {"catalog_id": catalog_id, "content_ids": cids},
        },
        {
            "key": "products_by_id",
            "question": "لمن المنتجات المحلية ذات المعرّفات المطلوبة، وما مصدرها وأختامها؟",
            "sql": """
SELECT p.id, p.tenant_id, LEFT(p.title, 80) AS title_80, p.external_id, p.source,
       p.ownership_mode, p.source_external_id, p.meta_retailer_id, p.canonical_retailer_id,
       p.meta_item_id, p.meta_catalog_published_at, p.imported_at, p.managed_confirmed_at,
       p.managed_confirmed_by, p.sync_status, p.catalog_status, p.merchant_hidden_at,
       p.meta_last_seen_at, p.meta_removed_at, p.archived_at, p.has_variants, p.in_stock,
       p.created_at,
       p.metadata ->> 'product_url' AS md_product_url,
       p.metadata ->> 'url' AS md_url,
       p.metadata ->> 'store_url' AS md_store_url,
       p.metadata ->> 'source_status' AS md_source_status,
       p.metadata ->> 'source_event_at' AS md_source_event_at,
       (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(
           CASE WHEN jsonb_typeof(p.metadata) = 'object' THEN p.metadata ELSE '{}'::jsonb END) k) AS md_keys,
       (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(
           CASE WHEN jsonb_typeof(p.metadata -> 'sync_meta') = 'object' THEN p.metadata -> 'sync_meta' ELSE '{}'::jsonb END) k) AS sync_meta_keys
FROM products p
WHERE p.id = ANY(%(product_ids)s)
ORDER BY p.id""",
            "params": {"product_ids": pids},
        },
        {
            "key": "variants_of_products",
            "question": "متغيرات تلك المنتجات وهوياتها",
            "sql": """
SELECT v.id, v.tenant_id, v.product_id, v.salla_variant_id, v.retailer_id, v.sku,
       v.is_default, v.in_stock, v.created_at
FROM product_variants v
WHERE v.product_id = ANY(%(product_ids)s)
ORDER BY v.product_id, v.id""",
            "params": {"product_ids": pids},
        },
        {
            "key": "claims_on_nahla_identities",
            "question": "هل يدّعي أي منتج أو متغير (لأي مستأجر) هوية nahla_p_* من التصدير؟",
            "sql": """
SELECT 'product' AS kind, p.id, p.tenant_id, p.id AS product_id, NULL::int AS variant_id,
       COALESCE(p.meta_retailer_id, p.canonical_retailer_id) AS claimed_retailer_id,
       p.meta_item_id, p.source, p.archived_at
FROM products p
WHERE p.meta_retailer_id = ANY(%(nahla_ids)s) OR p.canonical_retailer_id = ANY(%(nahla_ids)s)
UNION ALL
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
            "sql": """
SELECT p.tenant_id, COUNT(*) AS stamped_products,
       COUNT(*) FILTER (WHERE p.meta_catalog_published_at IS NOT NULL) AS with_published_at,
       COUNT(*) FILTER (WHERE p.imported_at IS NOT NULL) AS with_imported_at,
       COUNT(*) FILTER (WHERE p.archived_at IS NOT NULL) AS archived,
       MIN(p.meta_catalog_published_at) AS first_published_at,
       MAX(p.meta_catalog_published_at) AS last_published_at
FROM products p
WHERE p.meta_item_id IS NOT NULL AND p.meta_item_id <> ''
GROUP BY p.tenant_id
ORDER BY p.tenant_id""",
            "params": {},
        },
        {
            "key": "products_stamped_with_export_meta_item_ids",
            "question": "هل يحمل أي منتج محلي معرّف عنصر Meta من عمود المعرّف في التصدير (إن وُجد)؟",
            "sql": """
SELECT p.id, p.tenant_id, LEFT(p.title, 80) AS title_80, p.external_id, p.source,
       p.meta_retailer_id, p.canonical_retailer_id, p.meta_item_id,
       p.meta_catalog_published_at, p.imported_at, p.archived_at
FROM products p
WHERE p.meta_item_id = ANY(%(meta_item_ids)s)
ORDER BY p.tenant_id, p.id""",
            "params": {"meta_item_ids": mids},
        },
        {
            "key": "products_matching_link_external_ids",
            "question": "لمن المنتجات التي تحمل معرّفات سلة المضمّنة في روابط التصدير (بما فيها المؤرشفة)؟",
            "sql": """
SELECT p.id, p.tenant_id, LEFT(p.title, 80) AS title_80, p.external_id, p.source_external_id,
       p.source, p.meta_retailer_id, p.canonical_retailer_id, p.meta_item_id,
       p.meta_catalog_published_at, p.archived_at, p.created_at,
       p.metadata ->> 'product_url' AS md_product_url
FROM products p
WHERE p.external_id = ANY(%(ext_ids)s) OR p.source_external_id = ANY(%(ext_ids)s)
ORDER BY p.tenant_id, p.id""",
            "params": {"ext_ids": exts},
        },
        {
            "key": "store_identity_integrations",
            "question": "أي مستأجر يملك تكامل سلة، وبأي معرّف متجر واسم ورابط؟ (أعمدة محددة، بلا رموز)",
            "sql": """
SELECT i.id, i.tenant_id, i.provider, i.external_store_id, i.enabled,
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
            "sql": """
SELECT t.id, t.name, t.domain, t.is_active, t.is_platform_tenant, t.created_at
FROM tenants t
WHERE t.id = %(tenant_id)s
   OR t.id IN (SELECT m.tenant_id FROM meta_catalog_memberships m WHERE m.catalog_id = %(catalog_id)s)
   OR t.id IN (SELECT p.tenant_id FROM products p WHERE p.id = ANY(%(product_ids)s))
   OR t.id IN (SELECT p.tenant_id FROM products p
               WHERE p.external_id = ANY(%(ext_ids)s) OR p.source_external_id = ANY(%(ext_ids)s))
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
            "sql": """
SELECT w.tenant_id, w.status, w.connection_type, w.whatsapp_business_account_id,
       w.business_manager_id, w.meta_business_account_id, w.phone_number_id,
       w.meta_catalog_id, w.catalog_enabled, w.meta_import_status, w.meta_import_last_at,
       w.meta_import_token_source, w.connected_at
FROM whatsapp_connections w
WHERE w.meta_catalog_id IS NOT NULL OR w.tenant_id = %(tenant_id)s
ORDER BY w.tenant_id""",
            "params": {"tenant_id": tenant_id},
        },
    ]


# ── execution ───────────────────────────────────────────────────────────────


def _assert_select_only(sql: str) -> None:
    head = sql.strip().split(None, 1)[0].upper()
    if head != "SELECT":
        raise RuntimeError(f"refusing non-SELECT statement: {head}")
    lowered = sql.lower()
    for forbidden in ("insert ", "update ", "delete ", "alter ", "drop ", "truncate ", "create ", "grant "):
        # ``update`` may legitimately appear inside identifiers; check as a leading keyword only.
        for line in lowered.splitlines():
            if line.strip().startswith(forbidden):
                raise RuntimeError(f"refusing statement containing write keyword: {forbidden.strip()}")


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def run(statements: List[Dict[str, Any]], database_url: str) -> Dict[str, Any]:
    import psycopg2  # noqa: PLC0415 — only needed inside the container
    import psycopg2.extras  # noqa: PLC0415

    out: Dict[str, Any] = {"read_only": READ_ONLY, "secrets_included": False, "results": {}}
    conn = psycopg2.connect(database_url)
    try:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET default_transaction_read_only = on")
            cur.execute("SET statement_timeout = '30s'")
            for st in statements:
                _assert_select_only(st["sql"])
                cur.execute(st["sql"], st["params"])
                rows = [dict(r) for r in cur.fetchall()]
                out["results"][st["key"]] = {
                    "question": st["question"],
                    "row_count": len(rows),
                    "rows": rows,
                }
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
    ap.add_argument("--meta-item-ids-file", help="one exported Meta item id per line, if the export had that column")
    ap.add_argument("--store-marker", default="", help="store slug/host seen in the links, e.g. dev-cgcaqkpx5wgewsyv")
    ap.add_argument("--print-sql", action="store_true")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args(argv)

    content_ids = read_lines(args.content_ids_file)
    links = read_lines(args.links_file)
    meta_item_ids = read_lines(args.meta_item_ids_file)
    ext_ids = external_ids_from_links(links)
    marker = args.store_marker.strip()
    if not marker and links:
        markers = [m for m in store_markers_from_links(links) if m.startswith("dev-")]
        marker = markers[0] if markers else ""

    statements = build_statements(
        catalog_id=args.catalog_id,
        tenant_id=args.tenant_id,
        product_ids=parse_id_range(args.product_ids),
        content_ids=content_ids,
        link_external_ids=ext_ids,
        meta_item_ids=meta_item_ids,
        store_marker=marker,
    )
    for st in statements:
        _assert_select_only(st["sql"])

    inputs = {
        "catalog_id": args.catalog_id,
        "tenant_id": args.tenant_id,
        "product_ids": parse_id_range(args.product_ids),
        "content_ids_count": len(content_ids),
        "links_count": len(links),
        "link_external_ids": ext_ids,
        "meta_item_ids_count": len(meta_item_ids),
        "store_marker": marker,
    }

    if args.print_sql:
        print(json.dumps({"inputs": inputs, "statements": [
            {"key": s["key"], "question": s["question"], "sql": s["sql"].strip()} for s in statements
        ]}, ensure_ascii=False, indent=2))
        return 0

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set in this environment", file=sys.stderr)
        return 2

    payload = run(statements, database_url)
    payload["inputs"] = inputs
    payload["generated_at"] = datetime.utcnow().isoformat() + "Z"
    print(render(payload, pretty=args.pretty))
    print(f"q5-readout catalog={args.catalog_id} tenant={args.tenant_id} statements={len(statements)} read_only=true", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
