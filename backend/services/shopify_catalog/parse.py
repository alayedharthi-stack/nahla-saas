"""
Strict validation of Admin API 2026-10 catalog nodes into normalised records.

Every function either returns a plain, JSON-serialisable record built only
from validated values, or raises ``ImportFailure``. Nothing is guessed:

* every field the import queries must be **present** in the response:
  an omitted key is ``invalid_response`` even when the field is nullable,
  while an explicit ``null`` stays a legitimate value (a valid-JSON partial
  payload can therefore never clear a stored value);
* a wrong type, an over-long string or an enum value the pinned version
  does not define is ``invalid_response``;
* a malformed GID, a ``legacyResourceId`` that disagrees with its GID, or a
  parent link that does not match is ``invalid_identity``;
* the parameterised InventoryLevel GID
  ``gid://shopify/InventoryLevel/<n>?inventory_item_id=<m>`` is accepted only
  in exactly that canonical form (one parameter, no encoding, no fragment),
  and ``<m>`` must be the parent InventoryItem's own id;
* prices are exact decimal strings (never floats);
* an image is recorded only when Shopify returns an ``https`` URL on
  ``cdn.shopify.com`` (at most a numeric ``v`` cache parameter). A missing,
  still-processing or unsafe image stays absent — never fabricated.

All regular expressions use ``fullmatch`` (Python's ``$`` would accept a
trailing newline).
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from services.shopify_catalog import errors as E
from services.shopify_catalog.config import MAX_OPTION_VALUES, MAX_OPTIONS
from services.shopify_catalog.models import COMBINED_LISTING_ROLES, INVENTORY_POLICIES, PRODUCT_STATUSES

_NUM = r"[1-9][0-9]{0,19}"
_U64_MAX = 2 ** 64 - 1
GID_TYPES = ("Product", "ProductVariant", "InventoryItem", "Location", "MediaImage", "Video",
             "ExternalVideo", "Model3d", "Shop")
_GID_RE = {t: re.compile(rf"gid://shopify/{t}/({_NUM})") for t in GID_TYPES}
_LEVEL_RE = re.compile(rf"gid://shopify/InventoryLevel/({_NUM})\?inventory_item_id=({_NUM})")
_MONEY_RE = re.compile(r"(0|[1-9][0-9]{0,14})(\.[0-9]{1,6})?")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_CURSOR_MAX = 1024
_IMAGE_HOST = "cdn.shopify.com"
_IMAGE_QUERY_RE = re.compile(r"(v=[0-9]{1,20})?")
MEDIA_TYPES = ("IMAGE", "VIDEO", "EXTERNAL_VIDEO", "MODEL_3D")
_MEDIA_GID_TYPE = {"IMAGE": "MediaImage", "VIDEO": "Video", "EXTERNAL_VIDEO": "ExternalVideo",
                   "MODEL_3D": "Model3d"}


def _invalid() -> E.ImportFailure:
    return E.ImportFailure(E.R_INVALID_RESPONSE)


def _identity() -> E.ImportFailure:
    return E.ImportFailure(E.R_INVALID_IDENTITY)


class Strict(dict):
    """A response object whose ``get`` requires the key to be present
    (presence is checked separately from nullability)."""

    def get(self, key, default=None):  # noqa: D401 — same signature, stricter contract
        if key not in self:
            raise _invalid()
        return dict.__getitem__(self, key)


def obj(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise _invalid()
    return value if isinstance(value, Strict) else Strict(value)


def lst(value: Any, *, limit: int) -> List[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise _invalid()
    return value


def boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise _invalid()
    return value


def integer(value: Any, *, lo: int = -(2 ** 31), hi: int = 2 ** 31 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise _invalid()
    return value


def text(value: Any, *, limit: int, required: bool = True) -> Optional[str]:
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise _invalid()
    if required and not value.strip():
        raise _invalid()
    return value


def optional_text(value: Any, *, limit: int) -> Optional[str]:
    """Shopify returns '' for an unset optional string; both mean absent."""
    if value is None or value == "":
        return None
    return text(value, limit=limit)


def enum(value: Any, allowed) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _invalid()
    return value


def timestamp(value: Any, *, required: bool = True) -> Optional[str]:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not 20 <= len(value) <= 40:
        raise _invalid()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _invalid() from None
    if parsed.tzinfo is None:
        raise _invalid()
    return parsed.isoformat()


def money(value: Any, *, required: bool = True) -> Optional[str]:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _MONEY_RE.fullmatch(value):
        raise _invalid()
    return str(Decimal(value))


def currency(value: Any) -> str:
    if not isinstance(value, str) or not _CURRENCY_RE.fullmatch(value):
        raise _invalid()
    return value


def gid(value: Any, kind: str) -> str:
    """The canonical GID of *kind*, else ``invalid_identity``."""
    if not isinstance(value, str):
        raise _identity()
    match = _GID_RE[kind].fullmatch(value)
    if not match or int(match.group(1)) > _U64_MAX:
        raise _identity()
    return value


def gid_number(value: str) -> str:
    return value.rsplit("/", 1)[-1]


def legacy_matches(node: Mapping[str, Any], gid_value: str) -> None:
    legacy = node.get("legacyResourceId")
    if not isinstance(legacy, str) or legacy != gid_number(gid_value):
        raise _identity()


def level_gid(value: Any, *, item_gid: str) -> str:
    """The parameterised InventoryLevel GID, bound to *item_gid*."""
    if not isinstance(value, str):
        raise _identity()
    match = _LEVEL_RE.fullmatch(value)
    if not match or int(match.group(1)) > _U64_MAX or int(match.group(2)) > _U64_MAX:
        raise _identity()
    if match.group(2) != gid_number(item_gid):
        raise _identity()   # the level names a different inventory item than its parent
    return value


def page_info(value: Any) -> Tuple[bool, Optional[str]]:
    info = obj(value)
    has_next = boolean(info.get("hasNextPage"))
    cursor = info.get("endCursor")
    if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > _CURSOR_MAX):
        raise _invalid()
    return has_next, cursor


def connection(value: Any, *, limit: int) -> Tuple[List[Any], bool, Optional[str]]:
    conn = obj(value)
    nodes = lst(conn.get("nodes"), limit=limit)
    has_next, cursor = page_info(conn.get("pageInfo"))
    return nodes, has_next, cursor


def count(value: Any) -> Tuple[int, str]:
    node = obj(value)
    number = integer(node.get("count"), lo=0, hi=2 ** 31 - 1)
    precision = enum(node.get("precision"), ("EXACT", "AT_LEAST"))
    return number, precision


def safe_image_url(value: Any) -> Optional[str]:
    """An https ``cdn.shopify.com`` URL, else None (never a fabricated one)."""
    if not isinstance(value, str) or not value or len(value) > 2048 or not value.isascii():
        return None
    if any(ch.isspace() or ord(ch) < 0x20 for ch in value) or "\\" in value or "%" in value:
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme != "https" or parts.netloc != _IMAGE_HOST or parts.username or parts.password
            or port is not None or parts.fragment or not parts.path.startswith("/")
            or not _IMAGE_QUERY_RE.fullmatch(parts.query)):
        return None
    return value


def media(node: Any) -> Dict[str, Any]:
    """``{"gid", "type", "url"?, "alt"?}``; non-image media and images still
    processing carry no URL."""
    n = obj(node)
    kind = enum(n.get("mediaContentType"), MEDIA_TYPES)
    media_gid = gid(n.get("id"), _MEDIA_GID_TYPE[kind])
    record: Dict[str, Any] = {"gid": media_gid, "type": kind, "url": None, "alt": None}
    if kind == "IMAGE":
        image = n.get("image")
        if image is not None:
            image = obj(image)
            url = safe_image_url(image.get("url"))
            if url is not None:
                record["url"] = url
                record["alt"] = optional_text(image.get("altText"), limit=512)
    return record


def options(value: Any) -> List[Dict[str, Any]]:
    out = []
    for entry in lst(value, limit=MAX_OPTIONS):
        e = obj(entry)
        values = [text(obj(v).get("name"), limit=255) for v in lst(e.get("optionValues"), limit=MAX_OPTION_VALUES)]
        out.append({
            "name": text(e.get("name"), limit=255),
            "position": integer(e.get("position"), lo=1, hi=MAX_OPTIONS),
            "values": values,
        })
    return out


def product(node: Any) -> Dict[str, Any]:
    n = obj(node)
    product_gid = gid(n.get("id"), "Product")
    legacy_matches(n, product_gid)
    featured = n.get("featuredMedia")
    variants_total, precision = count(n.get("variantsCount"))
    role = n.get("combinedListingRole")
    return {
        "gid": product_gid,
        "title": text(n.get("title"), limit=255),
        "handle": text(n.get("handle"), limit=255),
        "status": enum(n.get("status"), PRODUCT_STATUSES),
        "vendor": optional_text(n.get("vendor"), limit=255),
        "product_type": optional_text(n.get("productType"), limit=255),
        "source_created_at": timestamp(n.get("createdAt")),
        "source_updated_at": timestamp(n.get("updatedAt")),
        "online_store_published_at": timestamp(n.get("publishedAt"), required=False),
        "is_gift_card": boolean(n.get("isGiftCard")),
        "requires_selling_plan": boolean(n.get("requiresSellingPlan")),
        "combined_listing_role": None if role is None else enum(role, COMBINED_LISTING_ROLES),
        "has_only_default_variant": boolean(n.get("hasOnlyDefaultVariant")),
        "options": options(n.get("options")),
        "featured_media": None if featured is None else media(featured),
        "variants_count": variants_total,
        "variants_count_precision": precision,
    }


def variant(node: Any) -> Dict[str, Any]:
    n = obj(node)
    variant_gid = gid(n.get("id"), "ProductVariant")
    legacy_matches(n, variant_gid)
    item = obj(n.get("inventoryItem"))
    selected = []
    for entry in lst(n.get("selectedOptions"), limit=MAX_OPTIONS):
        e = obj(entry)
        selected.append({"name": text(e.get("name"), limit=255), "value": text(e.get("value"), limit=255)})
    return {
        "gid": variant_gid,
        "title": text(n.get("title"), limit=255),
        "sku": optional_text(n.get("sku"), limit=255),
        "barcode": optional_text(n.get("barcode"), limit=255),
        "position": integer(n.get("position"), lo=1, hi=100_000),
        "price": money(n.get("price")),
        "compare_at_price": money(n.get("compareAtPrice"), required=False),
        "available_for_sale": boolean(n.get("availableForSale")),
        "inventory_policy": enum(n.get("inventoryPolicy"), INVENTORY_POLICIES),
        "requires_components": boolean(n.get("requiresComponents")),
        "source_created_at": timestamp(n.get("createdAt")),
        "source_updated_at": timestamp(n.get("updatedAt")),
        "selected_options": selected,
        "inventory_item_gid": gid(item.get("id"), "InventoryItem"),
        "tracking": "tracked" if boolean(item.get("tracked")) else "untracked",
        "item_updated_at": timestamp(item.get("updatedAt")),
    }


def shop(node: Any) -> Dict[str, str]:
    n = obj(node)
    return {
        "gid": gid(n.get("id"), "Shop"),
        "domain": text(n.get("myshopifyDomain"), limit=255),
        "currency": currency(n.get("currencyCode")),
    }


def access_scopes(node: Any) -> frozenset:
    n = obj(node)
    handles = set()
    for entry in lst(n.get("accessScopes"), limit=256):
        handles.add(text(obj(entry).get("handle"), limit=64))
    return frozenset(handles)


def location(node: Any) -> Dict[str, Any]:
    n = obj(node)
    return {
        "gid": gid(n.get("id"), "Location"),
        "name": text(n.get("name"), limit=255),
        "is_active": boolean(n.get("isActive")),
        "fulfills_online_orders": boolean(n.get("fulfillsOnlineOrders")),
    }


def inventory_level(node: Any, *, item_gid: str) -> Dict[str, Any]:
    n = obj(node)
    quantities = lst(n.get("quantities"), limit=1)
    if len(quantities) != 1:
        raise _invalid()
    quantity = obj(quantities[0])
    if quantity.get("name") != "available":
        raise _invalid()
    return {
        "gid": level_gid(n.get("id"), item_gid=item_gid),
        "item_gid": item_gid,
        "location_gid": gid(obj(n.get("location")).get("id"), "Location"),
        "available": integer(quantity.get("quantity")),
        "source_updated_at": timestamp(n.get("updatedAt")),
    }


def resolve_images(product_media: List[Dict[str, Any]], featured: Optional[Dict[str, Any]]):
    """(url, alt) of the product's own image: the featured media when it is a
    usable image, else the first usable image of its media; else absent."""
    if featured is not None and featured.get("url"):
        return featured["url"], featured.get("alt")
    for item in product_media:
        if item.get("url"):
            return item["url"], item.get("alt")
    return None, None


def variant_image(variant_media: List[Dict[str, Any]], product_image: Tuple[Optional[str], Optional[str]]):
    """(url, alt, source): the variant's own first image, else the product's
    image (``source='product'``), else nothing."""
    for item in variant_media:
        if item.get("url"):
            return item["url"], item.get("alt"), "variant"
    url, alt = product_image
    if url:
        return url, alt, "product"
    return None, None, None
