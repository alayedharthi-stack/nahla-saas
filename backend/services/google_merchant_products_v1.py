"""services/google_merchant_products_v1.py
──────────────────────────────────────────
Pure, offline Google Merchant API v1 ``ProductInput`` mapper (dry-run only).

Scope
─────
* Maps one tenant's local catalog rows (``Product`` + ``ProductVariant``)
  to the camelCase JSON body of Merchant API v1
  ``accounts.productInputs.insert`` and reports, per offer, every issue
  that can be proven offline.
* No transport, no credentials, no DB session, no network, no image
  fetch. Merchant API writes only into API data sources; nothing here
  touches an existing scheduled/file data source.
* Passing every offline check is not a Google approval. Google computes
  ``productStatus`` asynchronously after a real insert, which is outside
  this module.

Field names follow ``google.shopping.merchant.products.v1``:
``offerId`` / ``contentLanguage`` / ``feedLabel`` / ``productAttributes``,
``Price{amountMicros, currencyCode}`` and ``gtins`` as a list. int64
values (``amountMicros``) are serialized as decimal strings, as the
proto3 JSON mapping requires.

Truth rules
───────────
* Identity comes from the source store. Salla variants use the shared
  deterministic ``{external_id}-{salla_variant_id}``; synthetic
  ``nahla_*`` ids are never offered as Google identities.
* Prices are exact ``Decimal`` → micros. Floats are refused, nothing is
  rounded, and currency is read only from the same row as the price —
  never defaulted and never borrowed from another row. An effective price
  that neither the regular price nor a proven sale explains is blocking
  and no price is emitted.
* Missing brand / GTIN / MPN / description stay missing and are
  reported. GTIN / MPN are emitted only when stored source metadata
  already carries a value that passes validation.
* Any blocking issue makes the offer ``blocked``; only an offer with no
  blocking issue is ``passes_offline_checks``.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from core.catalog import (
    CATALOG_STATUS_ARCHIVED,
    CATALOG_STATUS_MERCHANT_HIDDEN,
    CATALOG_STATUS_REMOVED_FROM_META,
    META_EXISTING_SOURCES,
    OWNERSHIP_ARCHIVED_OR_DISCONNECTED,
    catalog_status_of,
    is_hidden_at_source,
    is_synthetic_retailer_id,
    normalize_source,
)
from core.catalog_image import coerce_image_url
from services.salla_variant_catalog_identity import (
    deterministic_variant_retailer_id,
    is_salla_source,
)

SEVERITY_BLOCKING = "blocking"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

OFFER_STATUS_BLOCKED = "blocked"
OFFER_STATUS_PASSES_OFFLINE_CHECKS = "passes_offline_checks"

AVAILABILITY_IN_STOCK = "IN_STOCK"
AVAILABILITY_OUT_OF_STOCK = "OUT_OF_STOCK"

LIFECYCLE_ACTIVE = "active"
LIFECYCLE_UNKNOWN = "unknown"

IMAGE_DIMENSIONS_UNKNOWN = "unknown"

# Limits from the Google product data specification. Used only to raise
# issues; re-verify against the live specification before any publishing
# slice relies on them.
SPEC_LIMITS: Mapping[str, int] = {
    "offer_id": 50,
    "title": 150,
    "description": 5000,
    "additional_image_links": 10,
    "gtins": 10,
    "mpn": 70,
    "brand": 70,
    "feed_label": 20,
}
INT64_MAX = 2**63 - 1

_CONTENT_LANGUAGE_RE = re.compile(r"^[a-z]{2}$")
_FEED_LABEL_RE = re.compile(r"^[A-Z0-9_-]{1,20}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_PRICE_TEXT_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_GTIN_RE = re.compile(r"^(?:[0-9]{8}|[0-9]{12,14})$")
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
# RFC 3986 unreserved characters minus "~" (the resource-key separator).
# Any other character in an offer id means only the encoded name is given.
_PLAIN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_INT64_DIGITS = 19
_SYNTHETIC_VARIANT_ID_PREFIX = "nahla_v_"

# GS1 prefixes (on the GTIN-13 view) Google does not accept: restricted
# circulation (2, 02, 04) and coupons (05, 98, 99).
_RESTRICTED_GTIN13_PREFIXES = ("2", "02", "04", "05", "98", "99")

_EXCLUDED_CATALOG_STATUSES = frozenset({
    CATALOG_STATUS_ARCHIVED,
    CATALOG_STATUS_MERCHANT_HIDDEN,
})
_RETIRED_VARIANT_STATUSES = frozenset({"retired", "deleted", "archived", "hidden"})
_RETIRED_VARIANT_STAMP_KEYS = ("retired_at", "deleted_at", "archived_at")

# Source option keys that already ARE Google attribute names. Merchant-
# language option names are not translated here; they are reported.
_DIRECT_VARIANT_ATTRIBUTES = ("color", "size", "material", "pattern")

_GTIN_METADATA_KEYS = ("gtin", "gtins", "barcode")
_MPN_METADATA_KEYS = ("mpn",)
_VARIANT_LINK_KEYS = ("product_url", "url")

# Exclusion reasons (rows deliberately not mapped to an offer).
EXCLUDED_META_OWNED_SOURCE = "meta_owned_source"
EXCLUDED_SOURCE_DISCONNECTED = "source_disconnected"
EXCLUDED_MERCHANT_HIDDEN = "merchant_hidden"
EXCLUDED_CATALOG_ARCHIVED = "catalog_archived"
EXCLUDED_HIDDEN_AT_SOURCE = "hidden_at_source"
EXCLUDED_SOURCE_LIFECYCLE_NOT_ACTIVE = "source_lifecycle_not_active"
EXCLUDED_VARIANT_RETIRED = "variant_retired_at_source"
EXCLUDED_SUPERSEDED_DEFAULT_VARIANT = "superseded_default_variant"
EXCLUDED_NO_STABLE_IDENTITY = "no_stable_source_identity"
EXCLUDED_VARIANT_ROWS_MISSING = "variant_rows_missing"


class InvalidTargetError(ValueError):
    """The Merchant target (language / feed label / tenant) is invalid."""


class CrossTenantRowError(ValueError):
    """A row handed to the mapper does not belong to the target tenant."""


@dataclass(frozen=True)
class GoogleMerchantTarget:
    """Tenant-scoped Merchant API target. No defaults: every value is explicit."""

    tenant_id: int
    content_language: str
    feed_label: str

    def __post_init__(self) -> None:
        if isinstance(self.tenant_id, bool) or not isinstance(self.tenant_id, int) or self.tenant_id <= 0:
            raise InvalidTargetError("tenant_id_invalid")
        if not isinstance(self.content_language, str) or not _CONTENT_LANGUAGE_RE.match(self.content_language):
            raise InvalidTargetError("content_language_invalid")
        if not isinstance(self.feed_label, str) or not _FEED_LABEL_RE.match(self.feed_label):
            raise InvalidTargetError("feed_label_invalid")


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str
    attribute: Optional[str] = None
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"code": self.code, "severity": self.severity}
        if self.attribute:
            out["attribute"] = self.attribute
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass
class OfferPreview:
    offer_id: str
    local_product_id: Any
    local_variant_id: Any
    product_input: Dict[str, Any]
    lifecycle: str
    issues: List[Issue] = field(default_factory=list)
    provenance: Dict[str, str] = field(default_factory=dict)
    # Tenant of the GoogleMerchantTarget that produced this offer. None means
    # the offer did not come from map_product and has no tenant provenance.
    tenant_id: Optional[int] = None

    @property
    def blocking(self) -> bool:
        return any(i.severity == SEVERITY_BLOCKING for i in self.issues)

    @property
    def status(self) -> str:
        return OFFER_STATUS_BLOCKED if self.blocking else OFFER_STATUS_PASSES_OFFLINE_CHECKS

    def to_dict(self) -> Dict[str, Any]:
        target_language = self.product_input.get("contentLanguage", "")
        target_label = self.product_input.get("feedLabel", "")
        return {
            "offer_id": self.offer_id,
            "status": self.status,
            "lifecycle": self.lifecycle,
            "product_input_id": product_input_plain_id(target_language, target_label, self.offer_id),
            "product_input_id_encoded": product_input_encoded_id(target_language, target_label, self.offer_id),
            "local_product_id": self.local_product_id,
            "local_variant_id": self.local_variant_id,
            "image_dimensions": IMAGE_DIMENSIONS_UNKNOWN,
            "product_input": self.product_input,
            "issues": [i.to_dict() for i in self.issues],
            "provenance": dict(self.provenance),
        }


@dataclass
class Exclusion:
    local_product_id: Any
    local_variant_id: Any
    reason: str
    offer_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "local_product_id": self.local_product_id,
            "local_variant_id": self.local_variant_id,
            "offer_id": self.offer_id,
            "reason": self.reason,
        }


@dataclass
class MappedProduct:
    offers: List[OfferPreview] = field(default_factory=list)
    exclusions: List[Exclusion] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Resource ids
# ─────────────────────────────────────────────────────────────────────────────


def _resource_key(content_language: str, feed_label: str, offer_id: str) -> str:
    return f"{content_language}~{feed_label}~{offer_id}"


def product_input_plain_id(content_language: str, feed_label: str, offer_id: str) -> Optional[str]:
    """``contentLanguage~feedLabel~offerId`` only when every part is plain-safe.

    Conservative: any character outside RFC 3986 "unreserved" (and any
    ``~``) means None; the encoded name is the canonical id. The offer id
    itself is never escaped or altered.
    """
    for part in (content_language, feed_label, offer_id):
        if not _PLAIN_ID_RE.match(part or ""):
            return None
    return _resource_key(content_language, feed_label, offer_id)


def product_input_encoded_id(content_language: str, feed_label: str, offer_id: str) -> str:
    """Unpadded base64url of ``contentLanguage~feedLabel~offerId`` (always safe)."""
    raw = _resource_key(content_language, feed_label, offer_id).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# ─────────────────────────────────────────────────────────────────────────────
# Price
# ─────────────────────────────────────────────────────────────────────────────


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def parse_price_amount(value: Any) -> Tuple[Optional[Decimal], Optional[str]]:
    """Strict Decimal price parse. Returns ``(amount, error_code)``.

    Accepts int, Decimal, plain digit strings (``"129.99"``) and Salla
    ``{"amount": ...}`` objects. Refuses floats, booleans, signs, grouping
    separators, exponents and currency text. A parsed amount that is not
    strictly positive is returned with ``price_not_positive``.
    """
    if isinstance(value, bool):
        return None, "price_unparseable"
    if isinstance(value, float):
        return None, "price_float_rejected"
    if isinstance(value, Mapping):
        return parse_price_amount(value.get("amount"))
    if isinstance(value, int):
        if value.bit_length() > 128:
            return None, "price_out_of_range"
        amount = Decimal(value)
    elif isinstance(value, Decimal):
        if not value.is_finite():
            return None, "price_unparseable"
        amount = value
    elif isinstance(value, str):
        text = value.strip()
        if not _PRICE_TEXT_RE.match(text):
            return None, "price_unparseable"
        try:
            amount = Decimal(text)
        except InvalidOperation:
            return None, "price_unparseable"
    else:
        return None, "price_unparseable"
    if amount <= 0:
        return amount, "price_not_positive"
    return amount, None


def micros_or_error(amount: Decimal) -> Tuple[Optional[int], Optional[str]]:
    """Exact int64 micros for *amount*, else ``(None, error_code)``.

    Pure integer arithmetic on the Decimal sign/digits/exponent tuple: no
    decimal context is used, so the ambient precision can never round a
    value. Any non-zero digit below one micro is ``price_precision``;
    anything beyond int64 is ``price_out_of_range``. Never builds a huge
    integer from an extreme exponent or digit count.
    """
    if not isinstance(amount, Decimal) or not amount.is_finite():
        return None, "price_unparseable"
    sign, digit_tuple, exponent = amount.as_tuple()
    digits = list(digit_tuple)
    shift = exponent + 6
    if shift < 0:
        cut = -shift
        if cut >= len(digits):
            if any(digits):
                return None, "price_precision"
            return 0, None
        if any(digits[-cut:]):
            return None, "price_precision"
        digits = digits[:-cut]
        shift = 0
    while len(digits) > 1 and digits[0] == 0:
        digits.pop(0)
    if not any(digits):
        return 0, None
    if len(digits) + shift > _INT64_DIGITS:
        return None, "price_out_of_range"
    micros = int("".join(str(d) for d in digits)) * 10**shift
    if micros > INT64_MAX:
        return None, "price_out_of_range"
    return (-micros if sign else micros), None


def to_micros(amount: Decimal) -> Optional[int]:
    """Exact int64 micros for *amount*, or None when not exactly representable."""
    return micros_or_error(amount)[0]


def _price_json(micros: int, currency: Optional[str]) -> Dict[str, str]:
    out = {"amountMicros": str(micros)}
    if currency:
        out["currencyCode"] = currency
    return out


@dataclass
class _PriceSource:
    """Price fields of ONE row (variant or product). Currency comes from here only."""

    origin: str
    price: Any
    regular_price: Any
    sale_price: Any
    currency: Any


def _resolve_prices(src: _PriceSource, issues: List[Issue], provenance: Dict[str, str]) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {}

    currency: Optional[str] = None
    if not _present(src.currency):
        issues.append(Issue("currency_missing", SEVERITY_BLOCKING, "price"))
    else:
        text = str(src.currency).strip()
        if _CURRENCY_RE.match(text):
            currency = text
            provenance["currencyCode"] = f"{src.origin}.currency"
        else:
            issues.append(Issue("currency_invalid", SEVERITY_BLOCKING, "price"))

    # ``price`` is the row's current/effective price; ``regular_price`` the
    # explicit list price. Salla reports 0 for an unset regular price.
    regular_raw, regular_field = src.price, "price"
    if _present(src.regular_price):
        explicit, explicit_err = parse_price_amount(src.regular_price)
        if not (explicit_err == "price_not_positive" and explicit == 0):
            regular_raw, regular_field = src.regular_price, "regular_price"
    if not _present(regular_raw):
        issues.append(Issue("price_missing", SEVERITY_BLOCKING, "price"))
        return attrs
    regular, err = parse_price_amount(regular_raw)
    if err:
        issues.append(Issue(err, SEVERITY_BLOCKING, "price"))
        return attrs
    regular_micros, micros_err = micros_or_error(regular)
    if micros_err:
        issues.append(Issue(micros_err, SEVERITY_BLOCKING, "price"))
        return attrs

    # Sale candidate: a positive sale price strictly below an explicit regular.
    sale: Optional[Decimal] = None
    sale_issue: Optional[Issue] = None
    if _present(src.sale_price):
        candidate, sale_err = parse_price_amount(src.sale_price)
        if sale_err == "price_not_positive" and candidate == 0:
            pass  # Salla reports 0 when no sale is running.
        elif sale_err:
            sale_issue = Issue("sale_price_invalid", SEVERITY_WARNING, "sale_price", sale_err)
        elif regular_field != "regular_price":
            if candidate != regular:
                # Only one of price/sale_price can be what the customer pays.
                issues.append(Issue("price_conflict_unresolved", SEVERITY_BLOCKING, "price"))
                return attrs
            sale_issue = Issue("sale_price_ambiguous", SEVERITY_WARNING, "sale_price")
        elif candidate >= regular:
            sale_issue = Issue("sale_price_not_below_regular", SEVERITY_WARNING, "sale_price")
        else:
            sale = candidate

    # The effective price must be explained by the regular price or by the
    # proven sale; anything else is an unresolved conflict and fails closed
    # (no price is emitted, so no misleading offer can look ready).
    if regular_field == "regular_price" and _present(src.price):
        current, current_err = parse_price_amount(src.price)
        if current_err is None and current == regular:
            if sale is not None:
                sale_issue = Issue("sale_price_not_effective", SEVERITY_WARNING, "sale_price")
                sale = None
        elif not (current_err is None and sale is not None and current == sale):
            if sale_issue is not None:
                issues.append(sale_issue)
            issues.append(Issue("price_conflict_unresolved", SEVERITY_BLOCKING, "price"))
            return attrs

    if sale_issue is not None:
        issues.append(sale_issue)
    attrs["price"] = _price_json(regular_micros, currency)
    provenance["price"] = f"{src.origin}.{regular_field}"
    if sale is None:
        return attrs
    sale_micros, sale_micros_err = micros_or_error(sale)
    if sale_micros_err:
        issues.append(Issue(f"sale_{sale_micros_err}", SEVERITY_BLOCKING, "sale_price"))
        return attrs
    attrs["salePrice"] = _price_json(sale_micros, currency)
    provenance["salePrice"] = f"{src.origin}.sale_price"
    return attrs


# ─────────────────────────────────────────────────────────────────────────────
# Identifiers
# ─────────────────────────────────────────────────────────────────────────────


def _gtin_check_digit_ok(digits: str) -> bool:
    body, check = digits[:-1], int(digits[-1])
    total = 0
    for i, ch in enumerate(reversed(body)):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    return (10 - total % 10) % 10 == check


def validate_gtin(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(gtin, None)`` for a valid GTIN-8/12/13/14, else ``(None, error)``."""
    if isinstance(value, bool) or value is None:
        return None, "gtin_invalid"
    text = str(value).strip()
    if not _GTIN_RE.match(text):
        return None, "gtin_invalid"
    if not text.strip("0"):
        return None, "gtin_invalid"
    if not _gtin_check_digit_ok(text):
        return None, "gtin_invalid"
    gtin13 = text.zfill(14)[1:]
    if len(text) != 8 and gtin13.startswith(_RESTRICTED_GTIN13_PREFIXES):
        return None, "gtin_restricted_range"
    return text, None


def _meta(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    meta = row.get("extra_metadata") if isinstance(row, dict) else getattr(row, "extra_metadata", None)
    return meta if isinstance(meta, dict) else {}


def _collect_identifiers(row: Any, origin: str, issues: List[Issue], provenance: Dict[str, str]) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {}
    meta = _meta(row)

    gtins: List[str] = []
    gtin_sources: List[str] = []
    for key in _GTIN_METADATA_KEYS:
        raw = meta.get(key)
        if not _present(raw):
            continue
        candidates = raw if isinstance(raw, (list, tuple)) else [raw]
        for candidate in candidates:
            gtin, err = validate_gtin(candidate)
            if err:
                issues.append(Issue(err, SEVERITY_WARNING, "gtins", f"{origin}.{key}"))
                continue
            if gtin not in gtins:
                gtins.append(gtin)
                if key not in gtin_sources:
                    gtin_sources.append(key)
            if key == "barcode":
                issues.append(Issue("gtin_from_barcode_field", SEVERITY_INFO, "gtins"))
    if len(gtins) > SPEC_LIMITS["gtins"]:
        issues.append(Issue("gtins_truncated", SEVERITY_WARNING, "gtins"))
        gtins = gtins[: SPEC_LIMITS["gtins"]]
    if gtins:
        attrs["gtins"] = gtins
        provenance["gtins"] = ",".join(f"{origin}.{k}" for k in gtin_sources)

    for key in _MPN_METADATA_KEYS:
        raw = meta.get(key)
        if not _present(raw) or isinstance(raw, (dict, list, tuple, bool)):
            continue
        mpn = str(raw).strip()
        if len(mpn) > SPEC_LIMITS["mpn"]:
            issues.append(Issue("mpn_too_long", SEVERITY_WARNING, "mpn"))
            continue
        attrs["mpn"] = mpn
        provenance["mpn"] = f"{origin}.{key}"
        break

    if "gtins" not in attrs and "mpn" not in attrs:
        issues.append(Issue("identifiers_not_available_in_source", SEVERITY_WARNING, "gtins"))
    return attrs


def _brand(product: Any, issues: List[Issue], provenance: Dict[str, str]) -> Dict[str, Any]:
    raw = _meta(product).get("brand")
    name = ""
    if isinstance(raw, str):
        name = raw.strip()
    elif isinstance(raw, Mapping) and isinstance(raw.get("name"), str):
        name = raw["name"].strip()
    if not name:
        issues.append(Issue("brand_missing", SEVERITY_WARNING, "brand"))
        return {}
    if len(name) > SPEC_LIMITS["brand"]:
        issues.append(Issue("brand_too_long", SEVERITY_WARNING, "brand"))
        return {}
    provenance["brand"] = "product.extra_metadata.brand"
    return {"brand": name}


# ─────────────────────────────────────────────────────────────────────────────
# Links and images
# ─────────────────────────────────────────────────────────────────────────────


def _url_issue(url: str) -> Optional[str]:
    """``"invalid"`` / ``"not_https"`` / None for a stored URL. Never raises.

    Invalid: control characters, any unescaped whitespace, unparseable
    authority (e.g. ``https://[bad``), invalid port, non-http(s) scheme or
    missing host. Non-ASCII path characters are left to Google.
    """
    if not isinstance(url, str) or not url:
        return "invalid"
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return "invalid"
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 — raises ValueError for a non-numeric / out-of-range port
    except ValueError:
        return "invalid"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "invalid"
    if parts.scheme != "https":
        return "not_https"
    return None


def _link(product: Any, variant: Any, genuine: bool, issues: List[Issue], provenance: Dict[str, str]) -> Dict[str, Any]:
    url, origin = "", ""
    if genuine:
        vmeta = _meta(variant)
        for key in _VARIANT_LINK_KEYS:
            if isinstance(vmeta.get(key), str) and vmeta[key].strip():
                url, origin = vmeta[key].strip(), f"variant.extra_metadata.{key}"
                break
    if not url:
        pmeta = _meta(product)
        for key in _VARIANT_LINK_KEYS:
            if isinstance(pmeta.get(key), str) and pmeta[key].strip():
                url, origin = pmeta[key].strip(), f"product.extra_metadata.{key}"
                break
        if url and genuine:
            issues.append(Issue("variant_landing_page_not_variant_specific", SEVERITY_WARNING, "link"))
    if not url:
        issues.append(Issue("link_missing", SEVERITY_BLOCKING, "link"))
        return {}
    problem = _url_issue(url)
    if problem == "invalid":
        issues.append(Issue("link_invalid", SEVERITY_BLOCKING, "link"))
        return {}
    if problem == "not_https":
        issues.append(Issue("link_not_https", SEVERITY_WARNING, "link"))
    provenance["link"] = origin
    return {"link": url}


def _images(product: Any, variant: Any, genuine: bool, issues: List[Issue], provenance: Dict[str, str]) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {}
    pmeta = _meta(product)
    primary, origin = "", ""
    if genuine and variant is not None:
        primary = coerce_image_url(getattr(variant, "image_url", None))
        origin = "variant.image_url" if primary else ""
    if not primary:
        primary = coerce_image_url(pmeta.get("image_url"))
        origin = "product.extra_metadata.image_url" if primary else ""
        if primary and genuine:
            issues.append(Issue("variant_image_inherited", SEVERITY_WARNING, "image_link"))
    if not primary:
        issues.append(Issue("image_missing", SEVERITY_BLOCKING, "image_link"))
        return attrs
    problem = _url_issue(primary)
    if problem == "invalid":
        issues.append(Issue("image_link_invalid", SEVERITY_BLOCKING, "image_link"))
        return attrs
    if problem == "not_https":
        issues.append(Issue("image_link_not_https", SEVERITY_WARNING, "image_link"))
    attrs["imageLink"] = primary
    provenance["imageLink"] = origin
    issues.append(Issue("image_dimensions_unknown", SEVERITY_INFO, "image_link"))

    extra_raw = pmeta.get("additional_images")
    if isinstance(extra_raw, (list, tuple)):
        extra: List[str] = []
        for item in extra_raw:
            url = coerce_image_url(item)
            if not url or url == primary or url in extra:
                continue
            if _url_issue(url) == "invalid":
                issues.append(Issue("additional_image_link_invalid", SEVERITY_BLOCKING, "additional_image_links"))
                continue
            extra.append(url)
        if len(extra) > SPEC_LIMITS["additional_image_links"]:
            issues.append(Issue("additional_images_truncated", SEVERITY_WARNING, "additional_image_links"))
            extra = extra[: SPEC_LIMITS["additional_image_links"]]
        if extra:
            attrs["additionalImageLinks"] = extra
            provenance["additionalImageLinks"] = "product.extra_metadata.additional_images"
    return attrs


# ─────────────────────────────────────────────────────────────────────────────
# Text
# ─────────────────────────────────────────────────────────────────────────────


def _text_attrs(product: Any, issues: List[Issue], provenance: Dict[str, str]) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {}
    title = str(getattr(product, "title", "") or "").strip()
    if not title:
        issues.append(Issue("title_missing", SEVERITY_BLOCKING, "title"))
    else:
        if len(title) > SPEC_LIMITS["title"]:
            issues.append(Issue("title_too_long", SEVERITY_BLOCKING, "title"))
        attrs["title"] = title
        provenance["title"] = "product.title"

    description = str(getattr(product, "description", "") or "").strip()
    if not description:
        issues.append(Issue("description_missing", SEVERITY_BLOCKING, "description"))
    else:
        if len(description) > SPEC_LIMITS["description"]:
            issues.append(Issue("description_too_long", SEVERITY_BLOCKING, "description"))
        if _HTML_TAG_RE.search(description):
            issues.append(Issue("description_contains_html", SEVERITY_WARNING, "description"))
        attrs["description"] = description
        provenance["description"] = "product.description"
    return attrs


def _variant_attributes(variant: Any, issues: List[Issue], provenance: Dict[str, str]) -> Dict[str, Any]:
    """Real variant attributes; independent of how many siblings are live."""
    attrs: Dict[str, Any] = {}
    options = getattr(variant, "options", None) if variant is not None else None
    unmapped = 0
    if isinstance(options, Mapping):
        for key, value in options.items():
            if key == "option_value_ids" or isinstance(value, (list, tuple, dict)) or not _present(value):
                continue
            name = str(key).strip().lower()
            if name in _DIRECT_VARIANT_ATTRIBUTES and name not in attrs:
                attrs[name] = str(value).strip()
                provenance[name] = f"variant.options.{key}"
            else:
                unmapped += 1
    if not attrs:
        issues.append(Issue(
            "variant_attributes_unmapped", SEVERITY_WARNING, "item_group_id",
            f"unmapped_option_keys={unmapped}",
        ))
    return attrs


# ─────────────────────────────────────────────────────────────────────────────
# Lifecycle / stock
# ─────────────────────────────────────────────────────────────────────────────


def _product_exclusion(product: Any) -> Optional[str]:
    if normalize_source(getattr(product, "source", None)) in META_EXISTING_SOURCES:
        return EXCLUDED_META_OWNED_SOURCE
    if str(getattr(product, "ownership_mode", "") or "").strip().lower() == OWNERSHIP_ARCHIVED_OR_DISCONNECTED:
        return EXCLUDED_SOURCE_DISCONNECTED
    if getattr(product, "merchant_hidden_at", None):
        return EXCLUDED_MERCHANT_HIDDEN
    status = catalog_status_of(product)
    if status == CATALOG_STATUS_MERCHANT_HIDDEN:
        return EXCLUDED_MERCHANT_HIDDEN
    if status in _EXCLUDED_CATALOG_STATUSES or getattr(product, "archived_at", None):
        return EXCLUDED_CATALOG_ARCHIVED
    if is_hidden_at_source(product):
        return EXCLUDED_HIDDEN_AT_SOURCE
    lifecycle = str(_meta(product).get("status") or "").strip().lower()
    if lifecycle and lifecycle != LIFECYCLE_ACTIVE:
        return EXCLUDED_SOURCE_LIFECYCLE_NOT_ACTIVE
    return None


def _variant_explicitly_retired(variant: Any) -> bool:
    meta = _meta(variant)
    status = str(meta.get("status") or "").strip().lower()
    if status in _RETIRED_VARIANT_STATUSES:
        return True
    return any(meta.get(key) for key in _RETIRED_VARIANT_STAMP_KEYS)


def _in_stock(row: Any) -> bool:
    qty = getattr(row, "stock_quantity", None)
    if isinstance(qty, int) and not isinstance(qty, bool) and qty <= 0:
        return False
    flag = getattr(row, "in_stock", None)
    return True if flag is None else bool(flag)


# ─────────────────────────────────────────────────────────────────────────────
# Identity
# ─────────────────────────────────────────────────────────────────────────────


def _is_synthetic(identity: str) -> bool:
    return is_synthetic_retailer_id(identity) or identity.startswith(_SYNTHETIC_VARIANT_ID_PREFIX)


def _offer_identity(product: Any, variant: Any, genuine: bool) -> Tuple[str, str]:
    """Return ``(offer_id, provenance)``; empty offer id means no stable identity."""
    external_id = str(getattr(product, "external_id", "") or "").strip()
    if is_salla_source(product):
        if genuine:
            svid = str(getattr(variant, "salla_variant_id", "") or "").strip()
            offer = deterministic_variant_retailer_id(external_id, svid)
            return offer, "product.external_id-variant.salla_variant_id"
        return external_id, "product.external_id"
    if genuine:
        rid = str(getattr(variant, "retailer_id", "") or "").strip()
        if rid and not _is_synthetic(rid):
            return rid, "variant.retailer_id"
        return "", ""
    if external_id and not _is_synthetic(external_id):
        return external_id, "product.external_id"
    rid = str(getattr(variant, "retailer_id", "") or "").strip() if variant is not None else ""
    if rid and not _is_synthetic(rid):
        return rid, "variant.retailer_id"
    return "", ""


def _identity_issues(offer_id: str, variant: Any, origin: str, issues: List[Issue]) -> None:
    if offer_id != " ".join(offer_id.split()):
        issues.append(Issue("offer_id_whitespace_changes_identity", SEVERITY_BLOCKING, "offer_id"))
    if len(offer_id) > SPEC_LIMITS["offer_id"]:
        issues.append(Issue("offer_id_too_long", SEVERITY_BLOCKING, "offer_id"))
    stored = str(getattr(variant, "retailer_id", "") or "").strip() if variant is not None else ""
    if stored and stored != offer_id:
        issues.append(Issue("local_retailer_id_differs", SEVERITY_WARNING, "offer_id"))
    if origin == "variant.retailer_id":
        issues.append(Issue("identity_from_local_retailer_id", SEVERITY_INFO, "offer_id"))
    # Offer ids are not compared with any existing Merchant Center source:
    # a matching id can transfer ownership, a changed id can duplicate goods.
    issues.append(Issue("live_source_identity_unverified", SEVERITY_INFO, "offer_id"))


# ─────────────────────────────────────────────────────────────────────────────
# Public mapper
# ─────────────────────────────────────────────────────────────────────────────


def _row_tenant(row: Any) -> Any:
    return getattr(row, "tenant_id", None)


def _assert_tenant_scope(target: GoogleMerchantTarget, product: Any, variants: Sequence[Any]) -> None:
    if _row_tenant(product) != target.tenant_id:
        raise CrossTenantRowError("product_tenant_mismatch")
    product_id = getattr(product, "id", None)
    for variant in variants:
        if _row_tenant(variant) != target.tenant_id:
            raise CrossTenantRowError("variant_tenant_mismatch")
        if getattr(variant, "product_id", None) != product_id:
            raise CrossTenantRowError("variant_parent_mismatch")


def _build_offer(
    target: GoogleMerchantTarget,
    product: Any,
    variant: Any,
    *,
    genuine: bool,
    offer_id: str,
    identity_origin: str,
) -> OfferPreview:
    issues: List[Issue] = []
    provenance: Dict[str, str] = {"offerId": identity_origin}
    attrs: Dict[str, Any] = {}

    _identity_issues(offer_id, variant, identity_origin, issues)
    attrs.update(_text_attrs(product, issues, provenance))
    attrs.update(_link(product, variant, genuine, issues, provenance))
    attrs.update(_images(product, variant, genuine, issues, provenance))

    price_row = variant if genuine else product
    origin = "variant" if genuine else "product"
    price_meta = _meta(price_row)
    attrs.update(_resolve_prices(
        _PriceSource(
            origin=origin,
            price=getattr(price_row, "price", None),
            regular_price=price_meta.get("regular_price"),
            sale_price=price_meta.get("sale_price"),
            currency=getattr(price_row, "currency", None) if genuine else price_meta.get("currency"),
        ),
        issues,
        provenance,
    ))

    lifecycle = LIFECYCLE_ACTIVE
    stock_row = variant if genuine else product
    available = _in_stock(stock_row)
    if genuine and available and not _in_stock(product):
        issues.append(Issue("stock_parent_variant_conflict", SEVERITY_WARNING, "availability"))
        available = False
    if genuine and not available:
        # A variant missing from the latest source payload is soft-pruned to
        # in_stock=False, which is indistinguishable from real zero stock.
        lifecycle = LIFECYCLE_UNKNOWN
        issues.append(Issue("variant_lifecycle_unknown", SEVERITY_WARNING, "availability"))
    attrs["availability"] = AVAILABILITY_IN_STOCK if available else AVAILABILITY_OUT_OF_STOCK
    provenance["availability"] = f"{origin}.in_stock/stock_quantity"

    attrs.update(_brand(product, issues, provenance))
    attrs.update(_collect_identifiers(price_row, origin, issues, provenance))

    if genuine:
        # Family identity is the parent; it stays stable when siblings retire.
        group_id = str(getattr(product, "external_id", "") or "").strip()
        if group_id:
            attrs["itemGroupId"] = group_id
            provenance["itemGroupId"] = "product.external_id"
        else:
            issues.append(Issue("item_group_id_unavailable", SEVERITY_WARNING, "item_group_id"))
        attrs.update(_variant_attributes(variant, issues, provenance))

    if catalog_status_of(product) == CATALOG_STATUS_REMOVED_FROM_META:
        issues.append(Issue("meta_channel_status_ignored", SEVERITY_INFO))

    return OfferPreview(
        offer_id=offer_id,
        local_product_id=getattr(product, "id", None),
        local_variant_id=getattr(variant, "id", None) if variant is not None else None,
        product_input={
            "offerId": offer_id,
            "contentLanguage": target.content_language,
            "feedLabel": target.feed_label,
            "productAttributes": attrs,
        },
        lifecycle=lifecycle,
        issues=issues,
        provenance=provenance,
        tenant_id=target.tenant_id,
    )


def map_product(target: GoogleMerchantTarget, product: Any, variants: Sequence[Any]) -> MappedProduct:
    """Map one product and its variant rows to Merchant API v1 offer previews.

    Raises :class:`CrossTenantRowError` when any row is outside
    ``target.tenant_id`` (fail closed). Never performs I/O.
    """
    if not isinstance(target, GoogleMerchantTarget):
        raise InvalidTargetError("target_invalid")
    variants = list(variants or [])
    _assert_tenant_scope(target, product, variants)

    result = MappedProduct()
    product_id = getattr(product, "id", None)

    excluded = _product_exclusion(product)
    if excluded:
        result.exclusions.append(Exclusion(product_id, None, excluded))
        return result

    defaults = [v for v in variants if bool(getattr(v, "is_default", False))]
    genuine_rows = [v for v in variants if not bool(getattr(v, "is_default", False))]

    if genuine_rows:
        for row in defaults:
            result.exclusions.append(Exclusion(product_id, getattr(row, "id", None), EXCLUDED_SUPERSEDED_DEFAULT_VARIANT))
        live: List[Any] = []
        for row in genuine_rows:
            if _variant_explicitly_retired(row):
                result.exclusions.append(Exclusion(product_id, getattr(row, "id", None), EXCLUDED_VARIANT_RETIRED))
            else:
                live.append(row)
        for row in live:
            offer_id, origin = _offer_identity(product, row, genuine=True)
            if not offer_id:
                result.exclusions.append(Exclusion(product_id, getattr(row, "id", None), EXCLUDED_NO_STABLE_IDENTITY))
                continue
            result.offers.append(_build_offer(
                target, product, row,
                genuine=True, offer_id=offer_id, identity_origin=origin,
            ))
        mark_duplicate_identities(result.offers)
        return result

    if is_salla_source(product) and (
        bool(getattr(product, "has_variants", False)) or bool(_meta(product).get("has_required_options"))
    ):
        # A Salla parent with options is not a sellable identity on its own.
        result.exclusions.append(Exclusion(product_id, None, EXCLUDED_VARIANT_ROWS_MISSING))
        return result

    default_row = defaults[0] if defaults else None
    for row in defaults[1:]:
        result.exclusions.append(Exclusion(product_id, getattr(row, "id", None), EXCLUDED_SUPERSEDED_DEFAULT_VARIANT))
    offer_id, origin = _offer_identity(product, default_row, genuine=False)
    if not offer_id:
        result.exclusions.append(Exclusion(
            product_id, getattr(default_row, "id", None) if default_row is not None else None,
            EXCLUDED_NO_STABLE_IDENTITY,
        ))
        return result
    result.offers.append(_build_offer(
        target, product, default_row,
        genuine=False, offer_id=offer_id, identity_origin=origin,
    ))
    return result


def _identity_key(offer: OfferPreview) -> Tuple[str, str, str]:
    body = offer.product_input
    return (
        str(body.get("contentLanguage", "")),
        str(body.get("feedLabel", "")),
        # Conservative: ids differing only by letter case are treated as one
        # identity, so a case-only difference can never silently overwrite.
        offer.offer_id.casefold(),
    )


def mark_duplicate_identities(offers: Sequence[OfferPreview]) -> int:
    """Block EVERY offer whose final identity collides with another one.

    Run over one product (inside :func:`map_product`) and over a complete
    tenant preview. Idempotent. Returns the number of colliding offers.
    """
    counts: Dict[Tuple[str, str, str], int] = {}
    for offer in offers:
        key = _identity_key(offer)
        counts[key] = counts.get(key, 0) + 1
    colliding = 0
    for offer in offers:
        total = counts[_identity_key(offer)]
        if total < 2:
            continue
        colliding += 1
        if not any(i.code == "duplicate_offer_identity" for i in offer.issues):
            offer.issues.append(Issue(
                "duplicate_offer_identity", SEVERITY_BLOCKING, "offer_id", f"offers_sharing_identity={total}",
            ))
    return colliding


__all__ = [
    "CrossTenantRowError",
    "Exclusion",
    "GoogleMerchantTarget",
    "InvalidTargetError",
    "Issue",
    "MappedProduct",
    "OFFER_STATUS_BLOCKED",
    "OFFER_STATUS_PASSES_OFFLINE_CHECKS",
    "OfferPreview",
    "SPEC_LIMITS",
    "map_product",
    "mark_duplicate_identities",
    "micros_or_error",
    "parse_price_amount",
    "product_input_encoded_id",
    "product_input_plain_id",
    "to_micros",
    "validate_gtin",
]
