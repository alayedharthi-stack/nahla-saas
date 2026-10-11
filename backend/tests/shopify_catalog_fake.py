"""In-process fake of the Shopify Admin GraphQL API 2026-10 catalog surface.

Used by the catalog import unit tests and PostgreSQL proofs (no network, no
real merchant, every token generated at runtime). It answers the import's
own operations by name with documented 2026-10 shapes:

* real-format GIDs, including the parameterised InventoryLevel GID
  ``gid://shopify/InventoryLevel/<n>?inventory_item_id=<m>``;
* opaque cursors over ``ID``-sorted connections; ``hasNextPage`` /
  ``endCursor``;
* ``X-Shopify-API-Version`` response header and ``extensions.cost``
  (``throttleStatus``) like Shopify;
* inventory items owned by variants, optionally shared by several variants
  (2026-10), levels keyed by (item, location);
* variant media are associations to the product's own media (Shopify links
  existing product media to variants): ``image=True`` adds an image to the
  product and links it; ``link_media`` links existing product media;
* ``ACCESS_DENIED`` for inventory levels / locations without
  ``read_inventory``.

Faults are injected with ``hooks``: ``hooks[op]`` is called with
``(fake, variables, call_index)`` before the answer is built and may return
an ``httpx.Response`` to send instead, or mutate the fake (to simulate
concurrent changes during pagination).
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

API_VERSION = "2026-10"
_OP_RE = re.compile(r"query\s+(\w+)")
CDN = "https://cdn.shopify.com/s/files/1/0000/0001/files"


def gid(kind: str, number: int) -> str:
    return f"gid://shopify/{kind}/{number}"


def level_gid(level_number: int, item_number: int) -> str:
    return f"gid://shopify/InventoryLevel/{level_number}?inventory_item_id={item_number}"


def _num(value: str) -> int:
    return int(value.rsplit("/", 1)[-1].split("?", 1)[0])


def _cursor(kind: str, number: int) -> str:
    return base64.b64encode(f"{kind}:{number}:{secrets.token_hex(2)}".encode()).decode()


def _after(cursor: Optional[str]) -> int:
    if not cursor:
        return 0
    return int(base64.b64decode(cursor).decode().split(":")[1])


@dataclass
class Media:
    number: int
    kind: str = "IMAGE"          # IMAGE | VIDEO | EXTERNAL_VIDEO | MODEL_3D
    url: Optional[str] = None    # None: image still processing
    alt: Optional[str] = None

    def node(self) -> Dict[str, Any]:
        type_name = {"IMAGE": "MediaImage", "VIDEO": "Video", "EXTERNAL_VIDEO": "ExternalVideo",
                     "MODEL_3D": "Model3d"}[self.kind]
        out: Dict[str, Any] = {"id": gid(type_name, self.number), "mediaContentType": self.kind}
        if self.kind == "IMAGE":
            out["image"] = None if self.url is None else {"url": self.url, "altText": self.alt}
        return out


@dataclass
class Variant:
    number: int
    product: int
    item: int
    title: str
    price: str
    sku: Optional[str] = None
    barcode: Optional[str] = None
    compare_at: Optional[str] = None
    position: int = 1
    available_for_sale: bool = True
    policy: str = "DENY"
    requires_components: bool = False
    options: List[Tuple[str, str]] = field(default_factory=list)
    media: List[int] = field(default_factory=list)
    updated_at: str = "2026-10-01T08:00:00Z"
    created_at: str = "2026-09-01T08:00:00Z"


@dataclass
class Product:
    number: int
    title: str
    handle: str
    status: str = "ACTIVE"
    vendor: str = "متجر تجريبي عام"
    product_type: str = ""
    published_at: Optional[str] = "2026-09-02T08:00:00Z"
    gift_card: bool = False
    selling_plan: bool = False
    combined_role: Optional[str] = None
    options: List[Tuple[str, List[str]]] = field(default_factory=list)
    media: List[int] = field(default_factory=list)
    featured: Optional[int] = None
    updated_at: str = "2026-10-01T08:00:00Z"
    created_at: str = "2026-09-01T08:00:00Z"


class FakeCatalogShop:
    def __init__(self, shop_domain: str, shop_gid: str, *, currency: str = "SAR",
                 scopes: Tuple[str, ...] = ("read_products",)):
        self.shop_domain = shop_domain
        self.shop_gid = shop_gid
        self.currency = currency
        self.scopes = list(scopes)
        self.lock = threading.Lock()
        self.tokens: set = set()
        self.products: Dict[int, Product] = {}
        self.variants: Dict[int, Variant] = {}
        self.items: Dict[int, Dict[str, Any]] = {}            # item -> {tracked, updatedAt}
        self.locations: Dict[int, Dict[str, Any]] = {}
        self.levels: Dict[Tuple[int, int], Dict[str, Any]] = {}  # (item, location) -> {number, available, updatedAt}
        self.media: Dict[int, Media] = {}
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.hooks: Dict[str, Callable] = {}
        self.version_header: Optional[str] = API_VERSION
        self.count_override: Optional[Tuple[int, str]] = None
        self._next = 1000

    # ── builders ──────────────────────────────────────────────────────────

    def _id(self) -> int:
        self._next += 1
        return self._next

    def add_image(self, *, url: Optional[str] = "default", alt: Optional[str] = None, kind: str = "IMAGE") -> int:
        number = self._id()
        if url == "default":
            url = f"{CDN}/image-{number}.jpg?v=1727000000"
        self.media[number] = Media(number, kind=kind, url=url, alt=alt)
        return number

    def add_location(self, name: str, *, active: bool = True, online: bool = True) -> int:
        number = self._id()
        self.locations[number] = {"id": gid("Location", number), "name": name, "isActive": active,
                                  "fulfillsOnlineOrders": online}
        return number

    def add_product(self, title: str, *, variants: List[Dict[str, Any]], status: str = "ACTIVE",
                    images: int = 1, published: bool = True, options: Optional[List[Tuple[str, List[str]]]] = None,
                    **extra) -> int:
        number = self._id()
        media = [self.add_image(alt=title) for _ in range(images)]
        product = Product(number=number, title=title, handle=f"product-{number}", status=status,
                          published_at="2026-09-02T08:00:00Z" if published else None,
                          options=options or [("Title", ["Default Title"])], media=media,
                          featured=media[0] if media else None, **extra)
        self.products[number] = product
        for position, spec in enumerate(variants, start=1):
            self.add_variant(number, position=position, **spec)
        return number

    def add_variant(self, product: int, *, price: str, title: str = "Default Title", position: int = 1,
                    tracked: bool = True, item: Optional[int] = None, levels: Optional[Dict[int, int]] = None,
                    options: Optional[List[Tuple[str, str]]] = None, image: bool = False,
                    link_media: Optional[List[int]] = None, item_updated_at: str = "2026-10-01T07:00:00Z",
                    **extra) -> int:
        number = self._id()
        if item is None:
            item = self._id()
            self.items[item] = {"tracked": tracked, "updatedAt": item_updated_at}
        media = list(link_media or [])
        if image:
            image_number = self.add_image(alt=title)
            self.products[product].media.append(image_number)
            media.append(image_number)
        self.variants[number] = Variant(number=number, product=product, item=item, title=title, price=price,
                                        position=position, options=options or [("Title", "Default Title")],
                                        media=media, **extra)
        for location, available in (levels or {}).items():
            self.set_level(item, location, available)
        return number

    def set_level(self, item: int, location: int, available: int, *, updated_at: str = "2026-10-01T09:00:00Z"):
        prior = self.levels.get((item, location))
        number = prior["number"] if prior else self._id()
        self.levels[(item, location)] = {"number": number, "available": available, "updatedAt": updated_at}

    def delete_product(self, product: int) -> None:
        for number in [n for n, v in self.variants.items() if v.product == product]:
            self.delete_variant(number)
        self.products.pop(product, None)

    def delete_variant(self, number: int) -> None:
        variant = self.variants.pop(number)
        if not any(v.item == variant.item for v in self.variants.values()):
            self.items.pop(variant.item, None)
            for key in [k for k in self.levels if k[0] == variant.item]:
                self.levels.pop(key)

    def issue_token(self) -> str:
        token = f"shpat_{secrets.token_hex(16)}"
        self.tokens.add(token)
        return token

    # ── rendering ─────────────────────────────────────────────────────────

    def _media_conn(self, numbers: List[int], first: int, after: Optional[str]) -> Dict[str, Any]:
        start = _after(after)
        ordered = sorted(n for n in numbers if n > start)
        page = ordered[:first]
        return {"pageInfo": {"hasNextPage": len(ordered) > first,
                             "endCursor": _cursor("media", page[-1]) if page else None},
                "nodes": [self.media[n].node() for n in page]}

    def _variant_node(self, v: Variant, variant_media: int) -> Dict[str, Any]:
        return {
            "id": gid("ProductVariant", v.number), "legacyResourceId": str(v.number), "title": v.title,
            "sku": v.sku if v.sku is not None else "", "barcode": v.barcode, "position": v.position,
            "price": v.price, "compareAtPrice": v.compare_at, "availableForSale": v.available_for_sale,
            "inventoryPolicy": v.policy, "requiresComponents": v.requires_components,
            "createdAt": v.created_at, "updatedAt": v.updated_at,
            "selectedOptions": [{"name": n, "value": val} for n, val in v.options],
            "inventoryItem": {"id": gid("InventoryItem", v.item), "tracked": self.items[v.item]["tracked"],
                              "updatedAt": self.items[v.item]["updatedAt"]},
            "media": self._media_conn(v.media, variant_media, None),
        }

    def _variants_conn(self, product: int, first: int, after: Optional[str], variant_media: int) -> Dict[str, Any]:
        start = _after(after)
        ordered = sorted(n for n, v in self.variants.items() if v.product == product and n > start)
        page = ordered[:first]
        return {"pageInfo": {"hasNextPage": len(ordered) > first,
                             "endCursor": _cursor("variant", page[-1]) if page else None},
                "nodes": [self._variant_node(self.variants[n], variant_media) for n in page]}

    def _variants_count(self, product: int) -> Dict[str, Any]:
        return {"count": sum(1 for v in self.variants.values() if v.product == product), "precision": "EXACT"}

    def _product_node(self, p: Product, variables: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": gid("Product", p.number), "legacyResourceId": str(p.number), "title": p.title,
            "handle": p.handle, "status": p.status, "vendor": p.vendor, "productType": p.product_type,
            "createdAt": p.created_at, "updatedAt": p.updated_at, "publishedAt": p.published_at,
            "isGiftCard": p.gift_card, "requiresSellingPlan": p.selling_plan,
            "combinedListingRole": p.combined_role,
            "hasOnlyDefaultVariant": self._variants_count(p.number)["count"] == 1,
            "options": [{"name": name, "position": i, "optionValues": [{"name": v} for v in values]}
                        for i, (name, values) in enumerate(p.options, start=1)],
            "featuredMedia": self.media[p.featured].node() if p.featured else None,
            "variantsCount": self._variants_count(p.number),
            "media": self._media_conn(p.media, variables["media"], None),
            "variants": self._variants_conn(p.number, variables["variants"], None, variables["variantMedia"]),
        }

    def _level_node(self, item: int, location: int) -> Dict[str, Any]:
        level = self.levels[(item, location)]
        return {"id": level_gid(level["number"], item), "updatedAt": level["updatedAt"],
                "quantities": [{"name": "available", "quantity": level["available"]}],
                "location": {"id": gid("Location", location)}}

    def _levels_conn(self, item: int, first: int, after: Optional[str]) -> Dict[str, Any]:
        start = _after(after)
        ordered = sorted(loc for (it, loc) in self.levels if it == item and loc > start)
        page = ordered[:first]
        return {"pageInfo": {"hasNextPage": len(ordered) > first,
                             "endCursor": _cursor("level", page[-1]) if page else None},
                "nodes": [self._level_node(item, loc) for loc in page]}

    # ── operations ────────────────────────────────────────────────────────

    def _count(self) -> Dict[str, Any]:
        if self.count_override is not None:
            return {"count": self.count_override[0], "precision": self.count_override[1]}
        return {"count": len(self.products), "precision": "EXACT"}

    def op_NahlaCatalogPreflight(self, variables):
        return {"shop": {"id": self.shop_gid, "myshopifyDomain": self.shop_domain, "currencyCode": self.currency},
                "currentAppInstallation": {"accessScopes": [{"handle": s} for s in self.scopes]},
                "productsCount": self._count()}

    def op_NahlaCatalogCount(self, variables):
        return {"shop": {"currencyCode": self.currency}, "productsCount": self._count()}

    def op_NahlaCatalogProducts(self, variables):
        start = _after(variables.get("after"))
        ordered = sorted(n for n in self.products if n > start)
        page = ordered[:variables["first"]]
        return {"products": {
            "pageInfo": {"hasNextPage": len(ordered) > variables["first"],
                         "endCursor": _cursor("product", page[-1]) if page else None},
            "nodes": [self._product_node(self.products[n], variables) for n in page]}}

    def op_NahlaCatalogProductVariants(self, variables):
        number = _num(variables["id"])
        if number not in self.products:
            return {"product": None}
        return {"product": {"id": variables["id"], "variantsCount": self._variants_count(number),
                            "variants": self._variants_conn(number, variables["first"], variables.get("after"),
                                                            variables["variantMedia"])}}

    def op_NahlaCatalogProductMedia(self, variables):
        number = _num(variables["id"])
        if number not in self.products:
            return {"product": None}
        return {"product": {"id": variables["id"], "media": self._media_conn(
            self.products[number].media, variables["first"], variables.get("after"))}}

    def op_NahlaCatalogVariantMedia(self, variables):
        number = _num(variables["id"])
        if number not in self.variants:
            return {"productVariant": None}
        return {"productVariant": {"id": variables["id"], "media": self._media_conn(
            self.variants[number].media, variables["first"], variables.get("after"))}}

    def _require_inventory(self):
        if "read_inventory" not in self.scopes:
            return httpx.Response(200, json={"errors": [{"message": "Access denied for inventoryLevels field.",
                                                         "extensions": {"code": "ACCESS_DENIED"}}]},
                                  headers={"X-Shopify-API-Version": API_VERSION})
        return None

    def op_NahlaCatalogLocations(self, variables):
        start = _after(variables.get("after"))
        ordered = sorted(n for n in self.locations if n > start)
        page = ordered[:variables["first"]]
        return {"locations": {"pageInfo": {"hasNextPage": len(ordered) > variables["first"],
                                           "endCursor": _cursor("location", page[-1]) if page else None},
                              "nodes": [self.locations[n] for n in page]}}

    def op_NahlaCatalogInventory(self, variables):
        nodes = []
        for item_gid in variables["ids"]:
            number = _num(item_gid)
            if number not in self.items:
                nodes.append(None)
                continue
            nodes.append({"__typename": "InventoryItem", "id": item_gid, "tracked": self.items[number]["tracked"],
                          "updatedAt": self.items[number]["updatedAt"],
                          "inventoryLevels": self._levels_conn(number, variables["first"], None)})
        return {"nodes": nodes}

    def op_NahlaCatalogInventoryLevels(self, variables):
        number = _num(variables["id"])
        if number not in self.items:
            return {"inventoryItem": None}
        return {"inventoryItem": {"id": variables["id"], "inventoryLevels": self._levels_conn(
            number, variables["first"], variables.get("after"))}}

    def op_NahlaCatalogConfirm(self, variables):
        nodes = []
        for value in variables["ids"]:
            number = _num(value)
            exists = ((value.startswith("gid://shopify/Product/") and number in self.products)
                      or (value.startswith("gid://shopify/ProductVariant/") and number in self.variants)
                      or (value.startswith("gid://shopify/Location/") and number in self.locations))
            nodes.append({"__typename": value.split("/")[3], "id": value} if exists else None)
        return {"nodes": nodes}

    # ── transport ─────────────────────────────────────────────────────────

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.scheme == "https" and request.url.host == self.shop_domain
        assert request.url.path == f"/admin/api/{API_VERSION}/graphql.json"
        assert not request.url.query
        token = request.headers.get("x-shopify-access-token")
        with self.lock:
            if token not in self.tokens:
                return httpx.Response(401, json={"errors": "[API] Invalid API key or access token"})
            body = json.loads(request.content)
            op = _OP_RE.search(body["query"]).group(1)
            variables = body.get("variables") or {}
            index = sum(1 for o, _ in self.calls if o == op)
            self.calls.append((op, variables))
            hook = self.hooks.get(op)
            if hook is not None:
                override = hook(self, variables, index)
                if override is not None:
                    return override
            if op in ("NahlaCatalogLocations", "NahlaCatalogInventory", "NahlaCatalogInventoryLevels"):
                denied = self._require_inventory()
                if denied is not None:
                    return denied
            data = getattr(self, f"op_{op}")(variables)
        headers = {} if self.version_header is None else {"X-Shopify-API-Version": self.version_header}
        return httpx.Response(200, json={"data": data, "extensions": {"cost": {
            "requestedQueryCost": 100, "actualQueryCost": 20,
            "throttleStatus": {"maximumAvailable": 2000.0, "currentlyAvailable": 1980.0, "restoreRate": 100.0}}}},
            headers=headers)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def ops(self, name: str) -> int:
        return sum(1 for o, _ in self.calls if o == name)


class SlowStream(httpx.AsyncByteStream):
    """A body that trickles: every chunk arrives well within any read timeout."""

    def __init__(self, chunks: int, delay: float):
        self.chunks, self.delay = chunks, delay

    async def __aiter__(self):
        for _ in range(self.chunks):
            await asyncio.sleep(self.delay)
            yield b" "

    async def aclose(self) -> None:
        return None
