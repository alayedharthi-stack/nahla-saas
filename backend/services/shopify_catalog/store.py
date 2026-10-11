"""
Durable state of the catalog import: runs, leases, fences, staging, the
single-transaction publish and the secret-free read model.

Concurrency model
─────────────────
* At most one ``running`` run per connection (partial unique index): a second
  start is refused (``import_in_progress``), never queued.
* A run is owned through ``lease_id`` + ``lease_expires_at``. Every write by
  the runner (heartbeat, staging, preflight, publish) goes through ``fence``,
  which takes **all** its locks first — the connection row (``FOR SHARE``),
  then the run row (``FOR UPDATE``) — and only then reads the write-time
  clock, so a runner that waited on a lock past its lease's expiry is
  refused rather than renewed. It checks:
    - the run is still ``running`` with this lease, not expired;
    - the connection still belongs to the run's tenant, is ``active`` and has
      the run's generation and shop identity (disconnect, uninstall,
      quarantine, re-authorization and reinstall all fail this);
    - optionally, the credential version the data was fetched with is still
      the stored one (a rotation discards the page; it is fetched again).
  Lock order is always connection → run, everywhere (``begin_run``,
  ``fence``, ``publish``); the foundation only ever locks the connection, so
  a queued exclusive disconnect / uninstall / refresh cannot close a cycle.
  Every run predicate carries the run's full identity (id, lease, tenant,
  connection, generation, shop): a handle with a valid lease but another
  tenant's or connection's identity matches nothing.
  ``publish`` re-checks the lease against the clock immediately before it
  commits.
* A run whose lease expired without a result is ``interrupted``: it is
  closed by the next start for that connection (never by a status read), and
  its staged records are deleted.
* Publishing is one transaction under the same fence: the mirror moves from
  the previous committed snapshot to this run's snapshot, or not at all.
  Rows whose stored Shopify ``updatedAt`` is newer than the staged one are
  not overwritten (``stale_skipped``) and keep their previous
  ``last_seen_run_id``: being seen with older data is not a refresh, so the
  availability view keeps calling them stale. Absent products, variants and
  locations become ``deleted`` only if Shopify confirmed each of them gone;
  inventory items and levels no longer referenced become ``detached``.
  Nothing is ever hard-deleted from the mirror.

Nothing here reads or writes ``products`` / ``product_variants`` or any
other shared catalog table, and nothing here stores a token, URL query,
upstream body or message.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from services.shopify_catalog import errors as E
from services.shopify_catalog.config import CATALOG_API_VERSION, LEASE_SECONDS
from services.shopify_catalog.models import (
    CATALOG_TABLE_NAMES,
    DELETED,
    DETACHED,
    INVENTORY_PER_LOCATION,
    INVENTORY_SCOPE_MISSING,
    KIND_ITEM,
    KIND_LEVEL,
    KIND_LOCATION,
    KIND_PRODUCT,
    KIND_VARIANT,
    PRESENT,
    RUN_CANCELLED,
    RUN_COMMITTED,
    RUN_FAILED,
    RUN_RUNNING,
    ShopifyCatalogImportRun as Run,
    ShopifyCatalogInventoryItem as Item,
    ShopifyCatalogInventoryLevel as Level,
    ShopifyCatalogLocation as Location,
    ShopifyCatalogProduct as Product,
    ShopifyCatalogStagedRecord as Staged,
    ShopifyCatalogVariant as Variant,
)
from services.shopify_connection.models import SHOPIFY_TABLE_NAMES, STATUS_ACTIVE, ShopifyConnection

logger = logging.getLogger("nahla.shopify_catalog")

_CHUNK = 500

# Start refusals (the only values a start answers with).
S_NOT_FOUND = "not_found"
S_CONNECTION_UNAVAILABLE = "connection_unavailable"
S_IN_PROGRESS = "import_in_progress"
START_REFUSALS = frozenset({S_NOT_FOUND, S_CONNECTION_UNAVAILABLE, S_IN_PROGRESS})

# Merchant-facing import states.
ST_NOT_STARTED = "not_started"
ST_IMPORTING = "importing"
ST_IMPORTED = "imported"
ST_ERROR = "error"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def wall_clock() -> datetime:
    """The clock lease fences are checked against at write time (test seam)."""
    return datetime.now(timezone.utc)


def _fence_now(now: datetime) -> datetime:
    return max(now, wall_clock())


class StartRefused(Exception):
    def __init__(self, code: str):
        code = code if code in START_REFUSALS else S_CONNECTION_UNAVAILABLE
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RunHandle:
    run_id: int
    lease_id: str = field(repr=False)
    tenant_id: int
    connection_id: int
    shop_domain: str
    shop_gid: str
    generation: int


def tables_present(db: Session) -> bool:
    """True only when inspection proves every foundation (0121) and catalog
    (0122) table exists. Unknown schema is treated as absent (fail closed)."""
    from sqlalchemy import inspect  # noqa: PLC0415

    try:
        names = set(inspect(db.get_bind()).get_table_names())
    except Exception as exc:  # noqa: BLE001 — unknown schema is treated as absent (fail closed)
        logger.warning("[SHOPIFY_CATALOG] schema inspect failed kind=%s", type(exc).__name__)
        return False
    return set(SHOPIFY_TABLE_NAMES) <= names and set(CATALOG_TABLE_NAMES) <= names


# ── Run start and reconciliation ──────────────────────────────────────────────

def _close_interrupted(db: Session, *, connection_id: int, now: datetime) -> List[int]:
    rows = db.execute(text(
        "UPDATE shopify_catalog_import_runs SET status = 'failed', failure_reason = :reason,"
        " lease_id = NULL, lease_expires_at = NULL, finished_at = :now"
        " WHERE connection_id = :c AND status = 'running' AND lease_expires_at <= :fence"
        " RETURNING id"
    ), {"reason": E.R_INTERRUPTED, "now": now, "fence": _fence_now(now), "c": connection_id}).scalars().all()
    if rows:
        db.execute(delete(Staged).where(Staged.run_id.in_(rows)))
    return list(rows)


def reconcile_interrupted(db: Session, *, connection_id: int, now: Optional[datetime] = None) -> List[int]:
    """Close runs whose lease expired without a result (crash, restart)."""
    ids = _close_interrupted(db, connection_id=connection_id, now=now or utcnow())
    db.commit()
    return ids


def begin_run(db: Session, *, tenant_id: int, user_id: Optional[int], shop_domain: str,
              now: Optional[datetime] = None) -> RunHandle:
    """Create the connection's single running import, or refuse.

    The connection is looked up by (tenant, shop) — another tenant's shop is
    ``not_found`` — and must be ``active``. Its row is held ``FOR SHARE`` so a
    concurrent disconnect / uninstall waits for (or precedes) this commit.
    """
    current = now or utcnow()
    conn = db.execute(
        select(ShopifyConnection).where(
            ShopifyConnection.tenant_id == int(tenant_id), ShopifyConnection.shop_domain == shop_domain,
        ).with_for_update(read=True).execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if conn is None:
        db.rollback()
        raise StartRefused(S_NOT_FOUND)
    if conn.status != STATUS_ACTIVE:
        db.rollback()
        raise StartRefused(S_CONNECTION_UNAVAILABLE)
    _close_interrupted(db, connection_id=conn.id, now=current)
    running = db.scalar(select(func.count()).select_from(Run).where(
        Run.connection_id == conn.id, Run.status == RUN_RUNNING))
    if running:
        db.rollback()
        raise StartRefused(S_IN_PROGRESS)
    seq = int(db.scalar(select(func.coalesce(func.max(Run.run_seq), 0)).where(Run.connection_id == conn.id))) + 1
    lease = secrets.token_hex(16)
    fence_now = _fence_now(current)
    run = Run(
        tenant_id=int(tenant_id), connection_id=int(conn.id), run_seq=seq, shop_domain=conn.shop_domain,
        shop_gid=conn.shop_gid, generation=int(conn.generation), api_version=CATALOG_API_VERSION,
        status=RUN_RUNNING, lease_id=lease, lease_expires_at=fence_now + timedelta(seconds=LEASE_SECONDS),
        requested_by_user_id=user_id, started_at=current, heartbeat_at=fence_now,
    )
    db.add(run)
    try:
        db.flush()
        handle = RunHandle(run_id=int(run.id), lease_id=lease, tenant_id=int(tenant_id),
                           connection_id=int(conn.id), shop_domain=conn.shop_domain, shop_gid=conn.shop_gid,
                           generation=int(conn.generation))
        db.commit()
    except IntegrityError:
        # A concurrent start won the one-running-run index or the sequence.
        db.rollback()
        raise StartRefused(S_IN_PROGRESS) from None
    return handle


# ── Fences ────────────────────────────────────────────────────────────────────

def _run_identity(handle: RunHandle) -> list:
    """Every predicate that names a run carries its full identity."""
    return [Run.id == handle.run_id, Run.lease_id == handle.lease_id, Run.tenant_id == handle.tenant_id,
            Run.connection_id == handle.connection_id, Run.generation == handle.generation,
            Run.shop_gid == handle.shop_gid, Run.shop_domain == handle.shop_domain]


def _lease_valid(run: Optional[Run], handle: RunHandle, fnow: datetime) -> bool:
    return (run is not None and run.status == RUN_RUNNING and run.lease_id == handle.lease_id
            and run.lease_expires_at is not None and run.lease_expires_at > fnow)


def fence(db: Session, handle: RunHandle, *, now: Optional[datetime] = None,
          credential_version: Optional[int] = None, extend: bool = True) -> Tuple[Run, ShopifyConnection]:
    """Lock and verify ownership; extend the lease. The caller commits.

    Raises ``LeaseLost`` (write nothing), ``ImportFailure`` with
    ``connection_unavailable`` / ``connection_changed`` (the run must be
    cancelled), or ``CredentialRotated`` (refetch).
    """
    # Locks first, in the global order connection → run.
    conn = db.execute(
        select(ShopifyConnection).where(
            ShopifyConnection.id == handle.connection_id, ShopifyConnection.tenant_id == handle.tenant_id,
        ).with_for_update(read=True).execution_options(populate_existing=True)
    ).scalar_one_or_none()
    run = db.execute(
        select(Run).where(*_run_identity(handle)).with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()
    # Only now, with every lock held, read the write-time clock.
    fnow = _fence_now(now or utcnow())
    if not _lease_valid(run, handle, fnow):
        raise E.LeaseLost()
    if conn is None or conn.status != STATUS_ACTIVE:
        raise E.ImportFailure(E.R_CONNECTION_UNAVAILABLE)
    if (int(conn.generation) != handle.generation or conn.shop_gid != handle.shop_gid
            or conn.shop_domain != handle.shop_domain):
        raise E.ImportFailure(E.R_CONNECTION_CHANGED)
    if credential_version is not None and int(conn.credential_version) != int(credential_version):
        raise E.CredentialRotated()
    if extend:
        run.lease_expires_at = fnow + timedelta(seconds=LEASE_SECONDS)
        run.heartbeat_at = fnow
    return run, conn


def heartbeat(db: Session, handle: RunHandle, *, credential_version: Optional[int] = None) -> None:
    try:
        fence(db, handle, credential_version=credential_version)
        db.commit()
    except BaseException:
        db.rollback()
        raise


def credential_snapshot(db: Session, handle: RunHandle) -> Tuple[int, frozenset]:
    """(credential version, stored granted scopes) of the run's connection,
    which must still be active with the run's generation and shop."""
    row = db.execute(
        select(ShopifyConnection.status, ShopifyConnection.generation, ShopifyConnection.shop_gid,
               ShopifyConnection.shop_domain, ShopifyConnection.credential_version,
               ShopifyConnection.granted_scopes)
        .where(ShopifyConnection.id == handle.connection_id, ShopifyConnection.tenant_id == handle.tenant_id)
    ).first()
    db.rollback()
    if row is None or row.status != STATUS_ACTIVE:
        raise E.ImportFailure(E.R_CONNECTION_UNAVAILABLE)
    if int(row.generation) != handle.generation or row.shop_gid != handle.shop_gid \
            or row.shop_domain != handle.shop_domain:
        raise E.ImportFailure(E.R_CONNECTION_CHANGED)
    scopes = frozenset(s.strip() for s in (row.granted_scopes or "").split(",") if s.strip())
    return int(row.credential_version), scopes


# ── Runner writes ─────────────────────────────────────────────────────────────

def record_preflight(db: Session, handle: RunHandle, *, currency: str, inventory_mode: str,
                     expected_products: int, credential_version: int) -> None:
    try:
        run, _ = fence(db, handle, credential_version=credential_version)
        run.shop_currency = currency
        run.inventory_mode = inventory_mode
        run.expected_products = expected_products
        db.commit()
    except BaseException:
        db.rollback()
        raise


def stage(db: Session, handle: RunHandle, records: List[Tuple[str, str, Optional[str], Dict[str, Any]]], *,
          credential_version: int, pages: int) -> None:
    """Stage one unit's validated records, fenced on the credential version
    they were fetched with."""
    try:
        run, _ = fence(db, handle, credential_version=credential_version)
        if records:
            db.execute(insert(Staged), [
                {"run_id": handle.run_id, "kind": kind, "gid": gid, "parent_gid": parent, "payload": payload}
                for kind, gid, parent, payload in records
            ])
        run.pages_fetched = int(pages)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise E.ImportFailure(E.R_DUPLICATE_IDENTITY) from None
    except BaseException:
        db.rollback()
        raise


def finish(db: Session, handle: RunHandle, *, status: str, reason: str, now: Optional[datetime] = None,
           pages: Optional[int] = None) -> bool:
    """Close the run as failed / cancelled if this worker still owns it
    (even after its lease ran out, as long as nobody closed it). The last
    committed snapshot is untouched; staged records are deleted."""
    assert status in (RUN_FAILED, RUN_CANCELLED)
    current = now or utcnow()
    values = {"status": status, "reason": reason if reason in E.FAILURE_REASONS else E.R_INTERNAL_ERROR,
              "now": current, "id": handle.run_id, "lease": handle.lease_id, "t": handle.tenant_id,
              "c": handle.connection_id, "g": handle.generation, "s": handle.shop_gid, "d": handle.shop_domain}
    try:
        closed = db.execute(text(
            "UPDATE shopify_catalog_import_runs SET status = :status, failure_reason = :reason,"
            " lease_id = NULL, lease_expires_at = NULL, finished_at = :now"
            + (", pages_fetched = :pages" if pages is not None else "")
            + " WHERE id = :id AND status = 'running' AND lease_id = :lease AND tenant_id = :t"
            " AND connection_id = :c AND generation = :g AND shop_gid = :s AND shop_domain = :d RETURNING id"
        ), {**values, **({"pages": int(pages)} if pages is not None else {})}).first()
        if closed is not None:
            db.execute(delete(Staged).where(Staged.run_id == handle.run_id))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return closed is not None


# ── Absence (input to Shopify's own confirmation) ─────────────────────────────

_ABSENT_SQL = {
    KIND_PRODUCT: ("shopify_catalog_products", "product_gid"),
    KIND_VARIANT: ("shopify_catalog_variants", "variant_gid"),
    KIND_LOCATION: ("shopify_catalog_locations", "location_gid"),
}


def _absent_query(kind: str) -> str:
    table, column = _ABSENT_SQL[kind]
    return (
        f"SELECT m.{column} FROM {table} m WHERE m.connection_id = :c AND m.lifecycle = 'present'"
        f" AND NOT EXISTS (SELECT 1 FROM shopify_catalog_staged_records s"
        f"  WHERE s.run_id = :r AND s.kind = '{kind}' AND s.gid = m.{column})"
    )


def absent_identities(db: Session, handle: RunHandle, *, include_locations: bool) -> Dict[str, Set[str]]:
    """Present mirror objects this run's (complete) traversal did not return."""
    kinds = [KIND_PRODUCT, KIND_VARIANT] + ([KIND_LOCATION] if include_locations else [])
    out = {}
    for kind in kinds:
        out[kind] = set(db.execute(text(_absent_query(kind)),
                                   {"c": handle.connection_id, "r": handle.run_id}).scalars())
    db.rollback()
    return out


# ── Publish ───────────────────────────────────────────────────────────────────

def _ts(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _chunks(rows: List[Dict[str, Any]]) -> Iterable[List[Dict[str, Any]]]:
    for start in range(0, len(rows), _CHUNK):
        yield rows[start:start + _CHUNK]


def _upsert(db: Session, model, rows: List[Dict[str, Any]], *, key: List[str], gid_column: str,
            guard: bool) -> Set[str]:
    """Insert or refresh mirror rows; returns the identities actually written
    (a row whose stored ``source_updated_at`` is newer is left untouched)."""
    table = model.__table__
    written: Set[str] = set()
    protected = set(key) | {"id", "first_seen_run_id", "created_at", "tenant_id", "connection_id"}
    for chunk in _chunks(rows):
        stmt = pg_insert(table).values(chunk)
        updates = {c: stmt.excluded[c] for c in chunk[0] if c not in protected}
        where = (table.c.source_updated_at <= stmt.excluded.source_updated_at) if guard else None
        stmt = stmt.on_conflict_do_update(index_elements=key, set_=updates, where=where).returning(table.c[gid_column])
        written.update(db.execute(stmt).scalars())
    return written


def _id_map(db: Session, model, gid_column: str, connection_id: int) -> Dict[str, Tuple[int, ...]]:
    table = model.__table__
    rows = db.execute(select(table).where(table.c.connection_id == connection_id)).mappings().all()
    return {r[gid_column]: r for r in rows}


def _tombstone(db: Session, sql: str, params: Dict[str, Any]) -> Set[str]:
    return set(db.execute(text(sql), params).scalars())


def publish(db: Session, handle: RunHandle, *, confirmed_absent: Dict[str, Set[str]], pages: int,
            now: Optional[datetime] = None) -> Dict[str, Any]:
    """Move the mirror to this run's complete snapshot in one transaction."""
    current = now or utcnow()
    try:
        run, _ = fence(db, handle, now=current)
        mode = run.inventory_mode
        if mode not in (INVENTORY_PER_LOCATION, INVENTORY_SCOPE_MISSING) or not run.shop_currency:
            raise E.ImportFailure(E.R_INTERNAL_ERROR)
        staged: Dict[str, List[Staged]] = {}
        for record in db.execute(select(Staged).where(Staged.run_id == handle.run_id)
                                 .order_by(Staged.id)).scalars():
            staged.setdefault(record.kind, []).append(record)
        if mode == INVENTORY_SCOPE_MISSING and (staged.get(KIND_LOCATION) or staged.get(KIND_LEVEL)):
            raise E.ImportFailure(E.R_INTERNAL_ERROR)
        cid, tid, rid = handle.connection_id, handle.tenant_id, handle.run_id
        base = {"tenant_id": tid, "connection_id": cid, "lifecycle": PRESENT, "removed_at": None,
                "first_seen_run_id": rid, "last_seen_run_id": rid, "created_at": current, "updated_at": current}
        stale = 0

        # Locations (per-location inventory only).
        locations: Dict[str, Any] = {}
        if mode == INVENTORY_PER_LOCATION:
            rows = [dict(base, location_gid=r.gid, name=r.payload["name"], is_active=r.payload["is_active"],
                         fulfills_online_orders=r.payload["fulfills_online_orders"])
                    for r in staged.get(KIND_LOCATION, [])]
            _upsert(db, Location, rows, key=["connection_id", "location_gid"], gid_column="location_gid", guard=False)
            locations = _id_map(db, Location, "location_gid", cid)

        # Products.
        rows = []
        for r in staged.get(KIND_PRODUCT, []):
            p = r.payload
            rows.append(dict(
                base, product_gid=r.gid, title=p["title"], handle=p["handle"], status=p["status"],
                vendor=p["vendor"], product_type=p["product_type"], options=p["options"],
                image_url=p["image_url"], image_alt=p["image_alt"], images=p["images"],
                online_store_published_at=_ts(p["online_store_published_at"]), is_gift_card=p["is_gift_card"],
                requires_selling_plan=p["requires_selling_plan"], combined_listing_role=p["combined_listing_role"],
                has_only_default_variant=p["has_only_default_variant"], variants_count=p["variants_count"],
                source_created_at=_ts(p["source_created_at"]), source_updated_at=_ts(p["source_updated_at"]),
            ))
        written = _upsert(db, Product, rows, key=["connection_id", "product_gid"], gid_column="product_gid", guard=True)
        stale += len({r["product_gid"] for r in rows} - written)
        products = _id_map(db, Product, "product_gid", cid)

        # Inventory items (item-owned inventory; may be shared by variants).
        rows = [dict(base, inventory_item_gid=r.gid, tracking=r.payload["tracking"],
                     source_updated_at=_ts(r.payload["source_updated_at"]))
                for r in staged.get(KIND_ITEM, [])]
        written = _upsert(db, Item, rows, key=["connection_id", "inventory_item_gid"],
                          gid_column="inventory_item_gid", guard=True)
        stale += len({r["inventory_item_gid"] for r in rows} - written)
        items = _id_map(db, Item, "inventory_item_gid", cid)

        # Variants: a variant never moves to another product.
        existing = _id_map(db, Variant, "variant_gid", cid)
        rows = []
        for r in staged.get(KIND_VARIANT, []):
            v = r.payload
            product = products.get(r.parent_gid)
            item = items.get(v["inventory_item_gid"])
            if product is None or item is None or v["product_gid"] != r.parent_gid:
                raise E.ImportFailure(E.R_INVALID_IDENTITY)
            prior = existing.get(r.gid)
            if prior is not None and int(prior["product_id"]) != int(product["id"]):
                raise E.ImportFailure(E.R_IDENTITY_CONFLICT)
            rows.append(dict(
                base, product_id=product["id"], inventory_item_id=item["id"], variant_gid=r.gid,
                title=v["title"], sku=v["sku"], barcode=v["barcode"], position=v["position"],
                price=Decimal(v["price"]),
                compare_at_price=Decimal(v["compare_at_price"]) if v["compare_at_price"] is not None else None,
                currency_code=v["currency_code"], selected_options=v["selected_options"],
                image_url=v["image_url"], image_alt=v["image_alt"], image_source=v["image_source"],
                inventory_policy=v["inventory_policy"], available_for_sale=v["available_for_sale"],
                requires_components=v["requires_components"],
                source_created_at=_ts(v["source_created_at"]), source_updated_at=_ts(v["source_updated_at"]),
            ))
        written = _upsert(db, Variant, rows, key=["connection_id", "variant_gid"], gid_column="variant_gid", guard=True)
        stale += len({r["variant_gid"] for r in rows} - written)

        # Inventory levels: keyed by (item, location), never by variant.
        if mode == INVENTORY_PER_LOCATION:
            prior_levels = _id_map(db, Level, "inventory_level_gid", cid)
            rows = []
            for r in staged.get(KIND_LEVEL, []):
                level = r.payload
                item = items.get(r.parent_gid)
                location = locations.get(level["location_gid"])
                if (item is None or location is None or level["item_gid"] != r.parent_gid
                        or not r.gid.endswith("?inventory_item_id=" + r.parent_gid.rsplit("/", 1)[-1])):
                    raise E.ImportFailure(E.R_INVALID_IDENTITY)
                prior = prior_levels.get(r.gid)
                if prior is not None and (int(prior["inventory_item_id"]), int(prior["location_id"])) != \
                        (int(item["id"]), int(location["id"])):
                    raise E.ImportFailure(E.R_IDENTITY_CONFLICT)
                rows.append(dict(base, inventory_item_id=item["id"], location_id=location["id"],
                                 inventory_level_gid=r.gid, available=level["available"],
                                 source_updated_at=_ts(level["source_updated_at"])))
            written = _upsert(db, Level, rows, key=["inventory_item_id", "location_id"],
                              gid_column="inventory_level_gid", guard=True)
            stale += len({r["inventory_level_gid"] for r in rows} - written)

        # Removals: only what Shopify confirmed gone; detach what is no longer referenced.
        params = {"c": cid, "r": rid, "now": current}
        removed_products = _tombstone(db, (
            "UPDATE shopify_catalog_products m SET lifecycle = 'deleted', removed_at = :now, updated_at = :now"
            " WHERE m.connection_id = :c AND m.lifecycle = 'present' AND NOT EXISTS ("
            "  SELECT 1 FROM shopify_catalog_staged_records s WHERE s.run_id = :r AND s.kind = 'product'"
            "  AND s.gid = m.product_gid) RETURNING m.product_gid"), params)
        removed_variants = _tombstone(db, (
            "UPDATE shopify_catalog_variants m SET lifecycle = 'deleted', removed_at = :now, updated_at = :now"
            " WHERE m.connection_id = :c AND m.lifecycle = 'present' AND NOT EXISTS ("
            "  SELECT 1 FROM shopify_catalog_staged_records s WHERE s.run_id = :r AND s.kind = 'variant'"
            "  AND s.gid = m.variant_gid) RETURNING m.variant_gid"), params)
        if not removed_products <= confirmed_absent.get(KIND_PRODUCT, set()) or \
                not removed_variants <= confirmed_absent.get(KIND_VARIANT, set()):
            raise E.ImportFailure(E.R_TRAVERSAL_INCONSISTENT)
        _tombstone(db, (
            "UPDATE shopify_catalog_inventory_items m SET lifecycle = 'detached', removed_at = :now, updated_at = :now"
            " WHERE m.connection_id = :c AND m.lifecycle = 'present' AND NOT EXISTS ("
            "  SELECT 1 FROM shopify_catalog_staged_records s WHERE s.run_id = :r AND s.kind = 'inventory_item'"
            "  AND s.gid = m.inventory_item_gid) RETURNING m.inventory_item_gid"), params)
        if mode == INVENTORY_PER_LOCATION:
            removed_locations = _tombstone(db, (
                "UPDATE shopify_catalog_locations m SET lifecycle = 'deleted', removed_at = :now, updated_at = :now"
                " WHERE m.connection_id = :c AND m.lifecycle = 'present' AND NOT EXISTS ("
                "  SELECT 1 FROM shopify_catalog_staged_records s WHERE s.run_id = :r AND s.kind = 'location'"
                "  AND s.gid = m.location_gid) RETURNING m.location_gid"), params)
            if not removed_locations <= confirmed_absent.get(KIND_LOCATION, set()):
                raise E.ImportFailure(E.R_TRAVERSAL_INCONSISTENT)
            _tombstone(db, (
                "UPDATE shopify_catalog_inventory_levels m SET lifecycle = 'detached', removed_at = :now,"
                " updated_at = :now WHERE m.connection_id = :c AND m.lifecycle = 'present' AND NOT EXISTS ("
                "  SELECT 1 FROM shopify_catalog_staged_records s WHERE s.run_id = :r AND s.kind = 'inventory_level'"
                "  AND s.gid = m.inventory_level_gid) RETURNING m.inventory_level_gid"), params)
        # Levels of an item no longer in the catalog are detached in every mode.
        _tombstone(db, (
            "UPDATE shopify_catalog_inventory_levels m SET lifecycle = 'detached', removed_at = :now, updated_at = :now"
            " FROM shopify_catalog_inventory_items i WHERE m.inventory_item_id = i.id AND m.connection_id = :c"
            " AND m.lifecycle = 'present' AND i.lifecycle = 'detached' RETURNING m.inventory_level_gid"), params)

        counts = {
            "products_count": len(staged.get(KIND_PRODUCT, [])),
            "variants_count": len(staged.get(KIND_VARIANT, [])),
            "inventory_items_count": len(staged.get(KIND_ITEM, [])),
            "locations_count": len(staged.get(KIND_LOCATION, [])) if mode == INVENTORY_PER_LOCATION else None,
            "inventory_levels_count": len(staged.get(KIND_LEVEL, [])) if mode == INVENTORY_PER_LOCATION else None,
        }
        for name, value in counts.items():
            setattr(run, name, value)
        run.products_removed = len(removed_products)
        run.variants_removed = len(removed_variants)
        run.stale_skipped = stale
        run.pages_fetched = int(pages)
        run.traversal_complete = True
        run.status = RUN_COMMITTED
        run.committed_at = current
        run.finished_at = current
        # Immediately before committing: the lease extended by the fence must
        # still hold against the clock now (a publish that outlived it — or a
        # wait on the locks above — commits nothing).
        if run.lease_id != handle.lease_id or run.lease_expires_at <= _fence_now(utcnow()):
            raise E.LeaseLost()
        run.lease_id = None
        run.lease_expires_at = None
        db.execute(delete(Staged).where(Staged.run_id == rid))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return dict(counts, products_removed=len(removed_products), variants_removed=len(removed_variants),
                stale_skipped=stale)


# ── Read model (secret-free) ──────────────────────────────────────────────────

def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def run_evidence(run: Optional[Run], *, current_generation: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """What a committed run proves. Inventory counts are ``None`` — not 0 —
    when inventory was not read (``scope_missing``). ``current_installation``
    says whether it belongs to the connection's current generation."""
    if run is None or run.status != RUN_COMMITTED:
        return None
    return {
        "current_installation": current_generation is not None and int(run.generation) == int(current_generation),
        "completed_at": _iso(run.committed_at),
        "api_version": run.api_version,
        "run": int(run.run_seq),
        "products": run.products_count,
        "variants": run.variants_count,
        "inventory_items": run.inventory_items_count,
        "removed": {"products": run.products_removed, "variants": run.variants_removed},
        "inventory": {
            "mode": run.inventory_mode,
            "locations": run.locations_count,
            "levels": run.inventory_levels_count,
        },
    }


def import_state(db: Session, conn: ShopifyConnection, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """State of the **current installation** (generation). A success from an
    earlier installation stays visible as ``last_success`` with
    ``current_installation: false`` but never makes the state ``imported``;
    neither does any success while the connection is not active (the mirror
    can no longer be refreshed or trusted as current)."""
    current = now or utcnow()
    generation = int(conn.generation)
    latest = db.execute(select(Run).where(Run.connection_id == conn.id, Run.tenant_id == conn.tenant_id,
                                          Run.generation == generation)
                        .order_by(Run.run_seq.desc()).limit(1)).scalar_one_or_none()
    success = db.execute(select(Run).where(Run.connection_id == conn.id, Run.tenant_id == conn.tenant_id,
                                           Run.status == RUN_COMMITTED)
                         .order_by(Run.run_seq.desc()).limit(1)).scalar_one_or_none()
    reason = None
    started_at = None
    if latest is None:
        state = ST_NOT_STARTED
    elif latest.status == RUN_RUNNING:
        if latest.lease_expires_at is not None and latest.lease_expires_at > current:
            state, started_at = ST_IMPORTING, _iso(latest.started_at)
        else:
            state, reason = ST_ERROR, E.R_INTERRUPTED   # derived; closed by the next start
    elif latest.status == RUN_COMMITTED:
        state = ST_IMPORTED
    else:
        state, reason = ST_ERROR, latest.failure_reason
    if conn.status != STATUS_ACTIVE and state in (ST_IMPORTED, ST_IMPORTING):
        state, reason, started_at = ST_ERROR, E.R_CONNECTION_UNAVAILABLE, None
    return {
        "state": state,
        "reason": reason,
        "started_at": started_at,
        "last_success": run_evidence(success, current_generation=generation),
        "can_start": conn.status == STATUS_ACTIVE and state != ST_IMPORTING,
    }


def import_summaries(db: Session, tenant_id: int, *, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    conns = db.execute(select(ShopifyConnection).where(ShopifyConnection.tenant_id == int(tenant_id))
                       .order_by(ShopifyConnection.id.asc())).scalars().all()
    out = [{"shop_domain": c.shop_domain, "connection_status": c.status, "import": import_state(db, c, now=now)}
           for c in conns]
    db.rollback()
    return out


def variant_availability(db: Session, *, tenant_id: int, connection_id: int, variant_gid: str) -> Dict[str, Any]:
    """Derived, read-only stock view of one mirrored variant (consumed by
    nothing in this slice). Never a sum across locations that cannot fulfil
    online orders, never a sum across variants sharing one inventory item,
    and ``unknown`` whenever the evidence does not support a number."""
    variant = db.execute(select(Variant).where(Variant.tenant_id == int(tenant_id),
                                               Variant.connection_id == int(connection_id),
                                               Variant.variant_gid == variant_gid)).scalar_one_or_none()
    published = db.execute(select(Run).where(Run.connection_id == int(connection_id),
                                             Run.tenant_id == int(tenant_id), Run.status == RUN_COMMITTED)
                           .order_by(Run.run_seq.desc()).limit(1)).scalar_one_or_none()
    conn = db.execute(select(ShopifyConnection).where(ShopifyConnection.id == int(connection_id),
                                                      ShopifyConnection.tenant_id == int(tenant_id))).scalar_one_or_none()
    try:
        if variant is None or published is None or conn is None:
            return {"state": "unknown", "basis": "not_in_mirror"}
        if conn.status != STATUS_ACTIVE:
            return {"state": "unknown", "basis": "connection_inactive"}
        if int(published.generation) != int(conn.generation):
            return {"state": "unknown", "basis": "previous_installation"}
        if variant.lifecycle != PRESENT:
            return {"state": "unknown", "basis": "deleted_at_source"}
        if variant.requires_components:
            return {"state": "unknown", "basis": "bundle_components"}
        item = db.get(Item, variant.inventory_item_id)
        policy = variant.inventory_policy
        if item.tracking == "untracked":
            return {"state": "untracked", "policy": policy}
        if published.inventory_mode != INVENTORY_PER_LOCATION:
            return {"state": "unknown", "basis": "inventory_scope_missing", "policy": policy}
        if item.last_seen_run_id != published.id or variant.last_seen_run_id != published.id:
            return {"state": "unknown", "basis": "stale_observation", "policy": policy}
        rows = db.execute(
            select(Level.available, Level.last_seen_run_id, Location.is_active, Location.fulfills_online_orders,
                   Location.lifecycle)
            .join(Location, Location.id == Level.location_id)
            .where(Level.inventory_item_id == item.id, Level.lifecycle == PRESENT)
        ).all()
        if any(r.last_seen_run_id != published.id for r in rows):
            return {"state": "unknown", "basis": "stale_observation", "policy": policy}
        usable = [r for r in rows if r.lifecycle == PRESENT and r.is_active and r.fulfills_online_orders]
        sharing = db.scalar(select(func.count()).select_from(Variant).where(
            Variant.inventory_item_id == item.id, Variant.lifecycle == PRESENT))
        return {
            "state": "tracked",
            "online_fulfillable_available": sum(int(r.available) for r in usable),
            "fulfillable_locations": len(usable),
            "excluded_locations": len(rows) - len(usable),
            "policy": policy,
            "variants_sharing_item": int(sharing),
            "observed_run": int(published.run_seq),
        }
    finally:
        db.rollback()
