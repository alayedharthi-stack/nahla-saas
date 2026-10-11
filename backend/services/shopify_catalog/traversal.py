"""
Complete, conservative traversal of one shop's catalog (Admin API 2026-10).

Shopify pagination is not an atomic snapshot. This module therefore proves
completeness rather than assuming it, and refuses whenever it cannot:

* every connection is followed to ``hasNextPage = false``; a page that
  promises more but repeats or omits its cursor, or returns nothing, is a
  ``pagination_loop``; the number of pages is bounded;
* products are read in ``ID`` order; each product's own ``variantsCount``
  (EXACT) must equal the variants actually read — after one fresh re-read of
  that product's variants, a remaining difference is
  ``source_changed_during_import``;
* the shop's ``productsCount`` (EXACT, at most 25,000) must equal the products
  read, after one bounded re-read;
* every identity is unique where Shopify says it is (product, variant,
  location, inventory level, media) — a repeat is ``duplicate_identity``;
  an inventory item may be shared by several variants (2026-10) but must
  report one tracking value;
* every inventory level must name its parent item in its own GID and a
  location the location pass returned;
* a follow-up for an object that has meanwhile disappeared ends the run
  (``source_changed_during_import``) instead of publishing a partial view.

The traversal never writes the mirror. It hands each unit of work — one page
with all its nested pages — to ``stage`` together with the credential version
the unit was fetched with; ``stage`` raises ``CredentialRotated`` when that
credential is no longer the stored one, and the unit is fetched again.
Absence from a traversal is evidence of nothing until ``confirm_absent``
asks Shopify about each absent object directly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from services.shopify_catalog import errors as E
from services.shopify_catalog import parse, queries
from services.shopify_catalog import config as C
from services.shopify_catalog.models import KIND_ITEM, KIND_LEVEL, KIND_LOCATION, KIND_PRODUCT, KIND_VARIANT

logger = logging.getLogger("nahla.shopify_catalog")

# (document, variables, requested-cost hint) -> (data, credential version used)
QueryFn = Callable[[str, Dict[str, Any], int], Awaitable[Tuple[Dict[str, Any], int]]]
# (records, credential version) -> None; raises CredentialRotated / LeaseLost / ImportFailure
StageFn = Callable[[List[Tuple[str, str, Optional[str], Dict[str, Any]]], int], Awaitable[None]]
SleepFn = Callable[[float], Awaitable[None]]

Record = Tuple[str, str, Optional[str], Dict[str, Any]]


@dataclass
class Snapshot:
    """Identities read by this run (never payloads)."""

    products: Dict[str, None] = field(default_factory=dict)
    variants: Dict[str, str] = field(default_factory=dict)          # variant gid -> product gid
    items: Dict[str, str] = field(default_factory=dict)             # item gid -> tracking
    locations: Set[str] = field(default_factory=set)
    levels: Dict[str, Tuple[str, str]] = field(default_factory=dict)  # level gid -> (item gid, location gid)
    media: Set[str] = field(default_factory=set)
    images_absent: int = 0


class _Cursor:
    """Cursor-loop guard for one connection traversal."""

    def __init__(self, max_pages: int):
        self.seen: Set[str] = set()
        self.after: Optional[str] = None
        self.pages = 0
        self.max_pages = max_pages

    def advance(self, nodes: Sequence[Any], has_next: bool, cursor: Optional[str]) -> bool:
        self.pages += 1
        if self.pages > self.max_pages:
            raise E.ImportFailure(E.R_CATALOG_TOO_LARGE)
        if not has_next:
            return False
        if not nodes or cursor is None or cursor == self.after or cursor in self.seen:
            raise E.ImportFailure(E.R_PAGINATION_LOOP)
        self.seen.add(cursor)
        self.after = cursor
        return True


def _pages(cap: int, page: int) -> int:
    return cap // max(page, 1) + 2


class CatalogTraversal:
    def __init__(self, *, query: QueryFn, stage: StageFn, sleep: SleepFn, currency: str,
                 max_rotations: int = C.MAX_CREDENTIAL_ROTATIONS):
        self._query = query
        self._stage = stage
        self._sleep = sleep
        self.currency = currency
        self.snapshot = Snapshot()
        self.rotations = 0
        self._max_rotations = max_rotations

    # ── unit of work with credential-rotation redo ───────────────────────────

    def _rotated(self) -> None:
        self.rotations += 1
        if self.rotations > self._max_rotations:
            raise E.ImportFailure(E.R_CREDENTIAL_CHURN)

    async def _unit(self, fetch: Callable[[], Awaitable[Tuple[List[Record], Any, Set[int]]]]):
        """Fetch one unit, stage it with the single credential version it used,
        and only then merge it into the snapshot. A rotation detected while
        fetching (the request fence) or while staging discards everything the
        unit read and fetches it again with the current credential."""
        while True:
            try:
                records, merge, versions = await fetch()
                if len(versions) != 1:
                    raise E.CredentialRotated()
                await self._stage(records, next(iter(versions)))
            except E.CredentialRotated:
                self._rotated()
                continue
            merge()
            return

    async def _call(self, document: str, variables: Dict[str, Any], cost: int) -> Dict[str, Any]:
        """A single read outside a staged unit, refetched after a rotation."""
        while True:
            try:
                data, _ = await self._query(document, variables, cost)
                return data
            except E.CredentialRotated:
                self._rotated()

    async def _run(self, versions: Set[int], document: str, variables: Dict[str, Any], cost: int) -> Dict[str, Any]:
        data, version = await self._query(document, variables, cost)
        versions.add(version)
        return data

    # ── nested connections ───────────────────────────────────────────────────

    async def _rest_of(self, versions: Set[int], first_page: Tuple[List[Any], bool, Optional[str]], *,
                       document: str, root: str, owner_gid: str, field_name: str, page: int, cap: int,
                       extra: Optional[Dict[str, Any]] = None, cost: int = 200) -> List[Any]:
        """All nodes of a nested connection: the embedded first page, then
        follow-up pages of ``<root>(id: owner) { <field>(after:) }``."""
        nodes, has_next, cursor = first_page
        collected = list(nodes)
        guard = _Cursor(_pages(cap, page))
        more = guard.advance(nodes, has_next, cursor)
        while more:
            variables = {"id": owner_gid, "first": page, "after": guard.after, **(extra or {})}
            data = await self._run(versions, document, variables, cost)
            owner = data.get(root)
            if owner is None:
                raise E.ImportFailure(E.R_SOURCE_CHANGED)   # vanished between pages
            owner = parse.obj(owner)
            if owner.get("id") != owner_gid:
                raise E.ImportFailure(E.R_INVALID_IDENTITY)
            nodes, has_next, cursor = parse.connection(owner.get(field_name), limit=page)
            collected.extend(nodes)
            if len(collected) > cap:
                raise E.ImportFailure(E.R_CATALOG_TOO_LARGE)
            more = guard.advance(nodes, has_next, cursor)
        if len(collected) > cap:
            raise E.ImportFailure(E.R_CATALOG_TOO_LARGE)
        return collected

    async def _media_list(self, versions: Set[int], conn_value: Any, *, document: str, root: str, owner_gid: str,
                          cap: int, nested_page: int, seen_media: Set[str]) -> List[Dict[str, Any]]:
        first = parse.connection(conn_value, limit=nested_page)
        nodes = await self._rest_of(versions, first, document=document, root=root, owner_gid=owner_gid,
                                    field_name="media", page=C.FOLLOWUP_MEDIA_PAGE, cap=cap)
        out = []
        for node in nodes:
            item = parse.media(node)
            if item["gid"] in seen_media or item["gid"] in self.snapshot.media:
                raise E.ImportFailure(E.R_DUPLICATE_IDENTITY)
            seen_media.add(item["gid"])
            out.append(item)
        return out

    async def _variants_of(self, versions: Set[int], product_gid: str, first_conn: Any, expected: int,
                           seen_media: Set[str]) -> List[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
        """All variants of one product with their media; the count must match."""
        for attempt in (0, 1):
            if attempt == 0:
                first = parse.connection(first_conn, limit=C.NESTED_VARIANTS_PAGE)
            else:
                # One fresh re-read: Shopify's own count and list, read together.
                data = await self._run(versions, queries.PRODUCT_VARIANTS,
                                       {"id": product_gid, "first": C.FOLLOWUP_VARIANTS_PAGE, "after": None,
                                        "variantMedia": C.NESTED_VARIANT_MEDIA_PAGE}, 600)
                owner = data.get("product")
                if owner is None:
                    raise E.ImportFailure(E.R_SOURCE_CHANGED)
                owner = parse.obj(owner)
                if owner.get("id") != product_gid:
                    raise E.ImportFailure(E.R_INVALID_IDENTITY)
                expected, precision = parse.count(owner.get("variantsCount"))
                if precision != "EXACT" or expected > C.MAX_VARIANTS_PER_PRODUCT:
                    raise E.ImportFailure(E.R_CATALOG_TOO_LARGE)
                first = parse.connection(owner.get("variants"), limit=C.FOLLOWUP_VARIANTS_PAGE)
            nodes = await self._rest_of(versions, first, document=queries.PRODUCT_VARIANTS, root="product",
                                        owner_gid=product_gid, field_name="variants",
                                        page=C.FOLLOWUP_VARIANTS_PAGE, cap=C.MAX_VARIANTS_PER_PRODUCT,
                                        extra={"variantMedia": C.NESTED_VARIANT_MEDIA_PAGE}, cost=600)
            if len(nodes) == expected:
                break
            if attempt == 1:
                raise E.ImportFailure(E.R_SOURCE_CHANGED)
            seen_media.clear()
        out = []
        seen: Set[str] = set()
        for node in nodes:
            record = parse.variant(node)
            if record["gid"] in seen:
                raise E.ImportFailure(E.R_DUPLICATE_IDENTITY)
            seen.add(record["gid"])
            variant_media = await self._media_list(versions, parse.obj(node).get("media"),
                                                   document=queries.VARIANT_MEDIA, root="productVariant",
                                                   owner_gid=record["gid"], cap=C.MAX_MEDIA_PER_VARIANT,
                                                   nested_page=C.NESTED_VARIANT_MEDIA_PAGE, seen_media=seen_media)
            out.append((record, variant_media))
        return out

    # ── passes ───────────────────────────────────────────────────────────────

    async def locations(self) -> None:
        guard = _Cursor(_pages(C.MAX_LOCATIONS, C.LOCATIONS_PAGE))
        more = True
        while more:
            state: Dict[str, Any] = {}

            async def fetch():
                versions: Set[int] = set()
                data = await self._run(versions, queries.LOCATIONS,
                                       {"first": C.LOCATIONS_PAGE, "after": guard.after}, 250)
                nodes, has_next, cursor = parse.connection(data.get("locations"), limit=C.LOCATIONS_PAGE)
                records, gids = [], set()
                for node in nodes:
                    loc = parse.location(node)
                    if loc["gid"] in self.snapshot.locations or loc["gid"] in gids:
                        raise E.ImportFailure(E.R_DUPLICATE_IDENTITY)
                    gids.add(loc["gid"])
                    records.append((KIND_LOCATION, loc["gid"], None, loc))
                if len(self.snapshot.locations) + len(gids) > C.MAX_LOCATIONS:
                    raise E.ImportFailure(E.R_CATALOG_TOO_LARGE)
                state.update(nodes=nodes, has_next=has_next, cursor=cursor)
                return records, lambda: self.snapshot.locations.update(gids), versions

            await self._unit(fetch)
            more = guard.advance(state["nodes"], state["has_next"], state["cursor"])

    async def products(self) -> None:
        guard = _Cursor(_pages(C.PAGINATION_CAP, C.PRODUCTS_PAGE))
        more = True
        while more:
            state: Dict[str, Any] = {}

            async def fetch():
                versions: Set[int] = set()
                data = await self._run(versions, queries.PRODUCTS, {
                    "first": C.PRODUCTS_PAGE, "after": guard.after, "variants": C.NESTED_VARIANTS_PAGE,
                    "media": C.NESTED_PRODUCT_MEDIA_PAGE, "variantMedia": C.NESTED_VARIANT_MEDIA_PAGE,
                }, 850)
                nodes, has_next, cursor = parse.connection(data.get("products"), limit=C.PRODUCTS_PAGE)
                records, unit = await self._product_page(versions, nodes)
                state.update(nodes=nodes, has_next=has_next, cursor=cursor)
                return records, lambda: self._merge(unit), versions

            await self._unit(fetch)
            more = guard.advance(state["nodes"], state["has_next"], state["cursor"])

    async def _product_page(self, versions: Set[int], nodes: List[Any]):
        records: List[Record] = []
        unit = Snapshot()
        for node in nodes:
            product = parse.product(node)
            pgid = product["gid"]
            if pgid in self.snapshot.products or pgid in unit.products:
                raise E.ImportFailure(E.R_DUPLICATE_IDENTITY)
            if len(self.snapshot.products) + len(unit.products) + 1 > C.PAGINATION_CAP:
                raise E.ImportFailure(E.R_CATALOG_TOO_LARGE)
            if product["variants_count_precision"] != "EXACT" or product["variants_count"] > C.MAX_VARIANTS_PER_PRODUCT:
                raise E.ImportFailure(E.R_CATALOG_TOO_LARGE)
            seen_media: Set[str] = set(unit.media)
            n = parse.obj(node)
            product_media = await self._media_list(versions, n.get("media"), document=queries.PRODUCT_MEDIA,
                                                   root="product", owner_gid=pgid, cap=C.MAX_MEDIA_PER_PRODUCT,
                                                   nested_page=C.NESTED_PRODUCT_MEDIA_PAGE, seen_media=seen_media)
            featured = product.pop("featured_media")
            if featured is not None and featured["gid"] not in {m["gid"] for m in product_media}:
                raise E.ImportFailure(E.R_IDENTITY_CONFLICT)   # featured media must be one of the product's media
            image = parse.resolve_images(product_media, featured)
            variant_seen_media = set(seen_media)
            variants = await self._variants_of(versions, pgid, n.get("variants"), product["variants_count"],
                                               variant_seen_media)
            product["variants_count"] = len(variants)
            product.pop("variants_count_precision")
            product["image_url"], product["image_alt"] = image
            product["images"] = [{"gid": m["gid"], "url": m["url"], "alt": m["alt"]} for m in product_media if m["url"]]
            if image[0] is None:
                unit.images_absent += 1
            records.append((KIND_PRODUCT, pgid, None, product))
            unit.products[pgid] = None
            unit.media.update(variant_seen_media)
            for record, variant_media in variants:
                vgid = record["gid"]
                if vgid in self.snapshot.variants or vgid in unit.variants:
                    raise E.ImportFailure(E.R_DUPLICATE_IDENTITY)
                if len(self.snapshot.variants) + len(unit.variants) + 1 > C.MAX_VARIANTS_TOTAL:
                    raise E.ImportFailure(E.R_CATALOG_TOO_LARGE)
                item_gid, tracking = record["inventory_item_gid"], record["tracking"]
                known = self.snapshot.items.get(item_gid, unit.items.get(item_gid))
                if known is None:
                    unit.items[item_gid] = tracking
                    records.append((KIND_ITEM, item_gid, None, {"gid": item_gid, "tracking": tracking}))
                elif known != tracking:
                    raise E.ImportFailure(E.R_IDENTITY_CONFLICT)   # one item, two tracking values
                url, alt, source = parse.variant_image(variant_media, image)
                if url is None:
                    unit.images_absent += 1
                payload = dict(record, product_gid=pgid, currency_code=self.currency,
                               image_url=url, image_alt=alt, image_source=source)
                records.append((KIND_VARIANT, vgid, pgid, payload))
                unit.variants[vgid] = pgid
        return records, unit

    def _merge(self, unit: Snapshot) -> None:
        self.snapshot.products.update(unit.products)
        self.snapshot.variants.update(unit.variants)
        self.snapshot.items.update(unit.items)
        self.snapshot.media.update(unit.media)
        self.snapshot.images_absent += unit.images_absent

    async def inventory(self) -> None:
        """Levels of every distinct inventory item (shared items read once)."""
        item_gids = list(self.snapshot.items)
        for start in range(0, len(item_gids), C.INVENTORY_ITEMS_PER_QUERY):
            batch = item_gids[start:start + C.INVENTORY_ITEMS_PER_QUERY]

            async def fetch(batch=batch):
                versions: Set[int] = set()
                data = await self._run(versions, queries.INVENTORY_ITEMS,
                                       {"ids": batch, "first": C.NESTED_LEVELS_PAGE}, 700)
                nodes = parse.lst(data.get("nodes"), limit=len(batch))
                if len(nodes) != len(batch):
                    raise E.ImportFailure(E.R_INVALID_RESPONSE)
                records: List[Record] = []
                levels: Dict[str, Tuple[str, str]] = {}
                pairs: Set[Tuple[str, str]] = {pair for pair in self.snapshot.levels.values()}
                for item_gid, node in zip(batch, nodes):
                    if node is None:
                        raise E.ImportFailure(E.R_SOURCE_CHANGED)
                    n = parse.obj(node)
                    if n.get("__typename") != "InventoryItem" or n.get("id") != item_gid:
                        raise E.ImportFailure(E.R_INVALID_IDENTITY)
                    tracking = "tracked" if parse.boolean(n.get("tracked")) else "untracked"
                    if tracking != self.snapshot.items[item_gid]:
                        raise E.ImportFailure(E.R_SOURCE_CHANGED)
                    first = parse.connection(n.get("inventoryLevels"), limit=C.NESTED_LEVELS_PAGE)
                    level_nodes = await self._rest_of(versions, first, document=queries.INVENTORY_LEVELS,
                                                      root="inventoryItem", owner_gid=item_gid,
                                                      field_name="inventoryLevels", page=C.FOLLOWUP_LEVELS_PAGE,
                                                      cap=C.MAX_LEVELS_PER_ITEM)
                    for level_node in level_nodes:
                        level = parse.inventory_level(level_node, item_gid=item_gid)
                        if level["location_gid"] not in self.snapshot.locations:
                            raise E.ImportFailure(E.R_SOURCE_CHANGED)   # a location the location pass did not return
                        pair = (item_gid, level["location_gid"])
                        if level["gid"] in self.snapshot.levels or level["gid"] in levels or pair in pairs:
                            raise E.ImportFailure(E.R_DUPLICATE_IDENTITY)
                        pairs.add(pair)
                        levels[level["gid"]] = pair
                        records.append((KIND_LEVEL, level["gid"], item_gid, level))
                if len(self.snapshot.levels) + len(levels) > C.MAX_LEVELS_TOTAL:
                    raise E.ImportFailure(E.R_CATALOG_TOO_LARGE)
                return records, lambda: self.snapshot.levels.update(levels), versions

            await self._unit(fetch)

    async def verify_product_count(self) -> int:
        """Shopify's EXACT product count must equal the products read."""
        for attempt in (0, 1):
            data = await self._call(queries.PRODUCTS_COUNT, {}, 10)
            total, precision = parse.count(data.get("productsCount"))
            if precision != "EXACT" or total > C.PAGINATION_CAP:
                raise E.ImportFailure(E.R_CATALOG_TOO_LARGE)
            if total == len(self.snapshot.products):
                return total
            if attempt == 0:
                await self._sleep(C.COUNT_RECHECK_DELAY_SECONDS)
        raise E.ImportFailure(E.R_SOURCE_CHANGED)

    async def confirm_absent(self, gids: Iterable[str]) -> Set[str]:
        """Ask Shopify for each object the traversal did not return; every one
        must be gone (``null``), else the traversal is inconsistent."""
        pending = sorted(set(gids))
        for start in range(0, len(pending), C.CONFIRM_NODES_PER_QUERY):
            batch = pending[start:start + C.CONFIRM_NODES_PER_QUERY]
            data = await self._call(queries.CONFIRM_NODES, {"ids": batch}, len(batch) + 2)
            nodes = parse.lst(data.get("nodes"), limit=len(batch))
            if len(nodes) != len(batch):
                raise E.ImportFailure(E.R_INVALID_RESPONSE)
            if any(node is not None for node in nodes):
                raise E.ImportFailure(E.R_TRAVERSAL_INCONSISTENT)
        return set(pending)
