"""services/google_merchant_protocol_v1.py
──────────────────────────────────────────
Pure Google Merchant API v1 protocol layer (no transport).

Scope
─────
* Request shapes for ``accounts.productInputs`` insert / patch / delete and
  ``accounts.products`` get / list, built from an explicit
  :class:`MerchantApiBinding` and the accepted offline mapper payload.
* Structured read-back parsing (``productStatus`` only), payload
  fingerprints and deterministic retry classification / scheduling.
* No HTTP client, socket, credential, OAuth, DB session, flag or clock:
  every time value is injected by the caller. :class:`MerchantTransport`
  is a typing Protocol only; this module ships no implementation of it.

Truth rules
───────────
* A binding is a caller-supplied claim. It is usable only when its tenant,
  language and feed label match the target, its data source input is
  ``API`` and its evidence is fresh. Nothing here verifies it with Google.
* HTTP 2xx is never publication success. Only a parsed read-back whose
  identity, data source and attribute projection match is confirmation,
  and Google's ``productStatus`` is reported separately.
* 429 may be resent after its scheduled delay. A write that ends in 5xx,
  timeout or an unexpected status is uncertain: it needs a read-back
  before anything may be resent.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Tuple
from urllib.parse import urlencode

from services.google_merchant_products_v1 import (
    GoogleMerchantTarget,
    product_input_encoded_id,
)

API_HOST = "https://merchantapi.googleapis.com"
API_VERSION_PATH = "/products/v1"

DATA_SOURCE_INPUT_API = "API"

# Top-level ProductAttributes fields the accepted mapper emits. Insert and
# patch refuse any other key, so a mask can never name an unverified field.
SUPPORTED_PRODUCT_ATTRIBUTES = frozenset({
    "title", "description", "link", "imageLink", "additionalImageLinks",
    "price", "salePrice", "availability", "brand", "gtins", "mpn",
    "itemGroupId", "color", "size", "material", "pattern",
})
# The ONLY top-level ProductInput fields an insert may carry. Anything else
# (``legacyLocal``, ``customAttributes``, ``name``, ``versionNumber``, ...)
# is refused: some of them change the Google identity of the offer.
INSERT_BODY_FIELDS = frozenset({"offerId", "contentLanguage", "feedLabel", "productAttributes"})
# updateMask path prefix for top-level ProductAttributes fields. Re-verify
# against the productInputs.patch reference before any live slice.
UPDATE_MASK_ATTRIBUTE_PREFIX = "productAttributes."

LIST_PAGE_SIZE_MAX = 1000

OP_INSERT = "insert"
OP_PATCH = "patch"
OP_DELETE = "delete"
OP_GET = "get"
OP_LIST = "list"
WRITE_OPS = frozenset({OP_INSERT, OP_PATCH, OP_DELETE})

KNOWN_SEVERITIES = frozenset({"NOT_IMPACTED", "DEMOTED", "DISAPPROVED"})
SEVERITY_UNKNOWN = "UNKNOWN"
KNOWN_REPORTING_CONTEXTS = frozenset({
    "SHOPPING_ADS", "DISCOVERY_ADS", "DEMAND_GEN_ADS", "DEMAND_GEN_ADS_DISCOVER_SURFACE",
    "VIDEO_ADS", "DISPLAY_ADS", "LOCAL_INVENTORY_ADS", "VEHICLE_INVENTORY_ADS",
    "FREE_LISTINGS", "FREE_LOCAL_LISTINGS", "FREE_LOCAL_VEHICLE_LISTINGS",
    "YOUTUBE_AFFILIATE", "YOUTUBE_SHOPPING", "CLOUD_RETAIL", "LOCAL_CLOUD_RETAIL",
    "PRODUCT_REVIEWS", "MERCHANT_REVIEWS", "YOUTUBE_CHECKOUT",
})

PUBLICATION_APPROVED = "approved"
PUBLICATION_PENDING = "pending"
PUBLICATION_DISAPPROVED = "disapproved"
PUBLICATION_UNKNOWN = "unknown"

READBACK_IDENTITY_MATCHES = "matches"
READBACK_FOREIGN_SOURCE = "foreign_source"
READBACK_IDENTITY_MISMATCH = "identity_mismatch"

# Response classes.
RESP_ACCEPTED = "accepted"
RESP_NOT_FOUND = "not_found"
RESP_RATE_LIMITED = "rate_limited"
RESP_TRANSIENT_READ = "transient_read"
RESP_UNCERTAIN_WRITE = "uncertain_write_requires_readback"
RESP_PERMANENT_AUTH = "permanent_auth"
RESP_PERMANENT_PERMISSION = "permanent_permission"
RESP_PERMANENT_VALIDATION = "permanent_validation"
RESP_PERMANENT_UNEXPECTED = "permanent_unexpected"

# Always used with ``fullmatch``: ``$`` would also accept a trailing newline.
_DIGITS_RE = re.compile(r"[1-9][0-9]{0,19}")
_CONTENT_LANGUAGE_RE = re.compile(r"[a-z]{2}")
_FEED_LABEL_RE = re.compile(r"[A-Z0-9_-]{1,20}")
_RETRY_AFTER_SECONDS_RE = re.compile(r"[0-9]{1,9}")
_COUNTRY_RE = re.compile(r"[A-Z]{2}")
MALFORMED_SUFFIX = "_malformed"


class InvalidBindingError(ValueError):
    """A binding value is malformed (not merely unusable)."""


class ProtocolShapeError(ValueError):
    """A request cannot be built without violating the API shape."""


# ─────────────────────────────────────────────────────────────────────────────
# Binding
# ─────────────────────────────────────────────────────────────────────────────


def _is_epoch(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class MerchantApiBinding:
    """Caller-supplied tenant → account → data source binding. No defaults."""

    tenant_id: int
    account_id: str
    data_source_id: str
    data_source_input: str
    content_language: str
    feed_label: str
    observed_at: int

    def __post_init__(self) -> None:
        if isinstance(self.tenant_id, bool) or not isinstance(self.tenant_id, int) or self.tenant_id <= 0:
            raise InvalidBindingError("tenant_id_invalid")
        for name in ("account_id", "data_source_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _DIGITS_RE.fullmatch(value):
                raise InvalidBindingError(f"{name}_invalid")
        if not isinstance(self.data_source_input, str):
            raise InvalidBindingError("data_source_input_invalid")
        if not isinstance(self.content_language, str) or not _CONTENT_LANGUAGE_RE.fullmatch(self.content_language):
            raise InvalidBindingError("content_language_invalid")
        if not isinstance(self.feed_label, str) or not _FEED_LABEL_RE.fullmatch(self.feed_label):
            raise InvalidBindingError("feed_label_invalid")
        if not _is_epoch(self.observed_at):
            raise InvalidBindingError("observed_at_invalid")

    @property
    def account_name(self) -> str:
        return f"accounts/{self.account_id}"

    @property
    def data_source_name(self) -> str:
        return f"accounts/{self.account_id}/dataSources/{self.data_source_id}"


def freshness_issue(observed_at: Any, now: int, max_age_s: int) -> Optional[str]:
    """``"future"`` / ``"stale"`` / ``"invalid"`` / None for an evidence timestamp."""
    if not _is_epoch(observed_at) or not _is_epoch(now) or not _is_epoch(max_age_s):
        return "invalid"
    if observed_at > now:
        return "future"
    if now - observed_at > max_age_s:
        return "stale"
    return None


def validate_binding(
    binding: Optional[MerchantApiBinding],
    target: GoogleMerchantTarget,
    *,
    now: int,
    max_age_s: int,
) -> Tuple[str, ...]:
    """Reasons the binding is unusable for *target*; empty tuple means usable."""
    if not isinstance(binding, MerchantApiBinding):
        return ("binding_missing",)
    reasons: List[str] = []
    if binding.tenant_id != target.tenant_id:
        reasons.append("binding_tenant_mismatch")
    if (binding.content_language, binding.feed_label) != (target.content_language, target.feed_label):
        reasons.append("binding_language_or_label_mismatch")
    if binding.data_source_input != DATA_SOURCE_INPUT_API:
        reasons.append("data_source_not_api")
    stale = freshness_issue(binding.observed_at, now, max_age_s)
    if stale:
        reasons.append(f"binding_evidence_{stale}")
    return tuple(reasons)


# ─────────────────────────────────────────────────────────────────────────────
# Requests
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MerchantRequest:
    op: str
    method: str
    path: str
    query: Tuple[Tuple[str, str], ...]
    body: Optional[Dict[str, Any]]

    @property
    def is_write(self) -> bool:
        return self.op in WRITE_OPS

    @property
    def url(self) -> str:
        base = f"{API_HOST}{self.path}"
        return f"{base}?{urlencode(self.query)}" if self.query else base


def _resource_id(language: str, label: str, offer_id: str) -> str:
    return product_input_encoded_id(language, label, offer_id)


def _check_attributes(attrs: Mapping[str, Any]) -> None:
    unknown = sorted(set(attrs) - SUPPORTED_PRODUCT_ATTRIBUTES)
    if unknown:
        raise ProtocolShapeError("attribute_not_in_supported_schema:" + ",".join(unknown))


def write_target(binding: MerchantApiBinding, op: str, offer_id: str) -> Tuple[str, str]:
    """``(method, path)`` every write for *offer_id* under *binding* must use."""
    if op == OP_INSERT:
        return "POST", f"{API_VERSION_PATH}/{binding.account_name}/productInputs:insert"
    rid = _resource_id(binding.content_language, binding.feed_label, offer_id)
    method = {OP_PATCH: "PATCH", OP_DELETE: "DELETE"}.get(op)
    if method is None:
        raise ProtocolShapeError("not_a_write_op")
    return method, f"{API_VERSION_PATH}/{binding.account_name}/productInputs/{rid}"


def insert_request(binding: MerchantApiBinding, product_input: Mapping[str, Any], *, offer_id: str) -> MerchantRequest:
    """The payload identity must equal the decided *offer_id* exactly."""
    if not isinstance(offer_id, str) or not offer_id:
        raise ProtocolShapeError("offer_id_missing")
    if not isinstance(product_input, Mapping) or set(product_input) != INSERT_BODY_FIELDS:
        raise ProtocolShapeError("product_input_field_not_allowed")
    if product_input.get("offerId") != offer_id:
        raise ProtocolShapeError("offer_identity_mismatch")
    if (product_input.get("contentLanguage"), product_input.get("feedLabel")) != (
        binding.content_language, binding.feed_label,
    ):
        raise ProtocolShapeError("product_input_target_mismatch")
    attrs = product_input.get("productAttributes")
    if not isinstance(attrs, Mapping):
        raise ProtocolShapeError("product_attributes_missing")
    _check_attributes(attrs)
    body = {
        "offerId": offer_id,
        "contentLanguage": binding.content_language,
        "feedLabel": binding.feed_label,
        "productAttributes": json.loads(canonical_json(attrs)),
    }
    method, path = write_target(binding, OP_INSERT, offer_id)
    return MerchantRequest(OP_INSERT, method, path, (("dataSource", binding.data_source_name),), body)


def attribute_changes(
    desired: Mapping[str, Any], previous: Mapping[str, Any],
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """``(changed_or_added, removed)`` top-level keys, each sorted."""
    changed = sorted(k for k in desired if k not in previous or canonical_json(desired[k]) != canonical_json(previous[k]))
    removed = sorted(k for k in previous if k not in desired)
    return tuple(changed), tuple(removed)


def patch_request(
    binding: MerchantApiBinding,
    offer_id: str,
    desired: Mapping[str, Any],
    previous: Mapping[str, Any],
) -> MerchantRequest:
    """Patch only changed fields; removed fields are masked and absent (= deleted)."""
    _check_attributes(desired)
    _check_attributes(previous)
    changed, removed = attribute_changes(desired, previous)
    if not changed and not removed:
        raise ProtocolShapeError("patch_without_changes")
    mask = ",".join(UPDATE_MASK_ATTRIBUTE_PREFIX + k for k in sorted(changed + removed))
    body = {"productAttributes": json.loads(canonical_json({k: desired[k] for k in changed}))}
    method, path = write_target(binding, OP_PATCH, offer_id)
    return MerchantRequest(
        OP_PATCH, method, path, (("dataSource", binding.data_source_name), ("updateMask", mask)), body,
    )


def delete_request(binding: MerchantApiBinding, offer_id: str) -> MerchantRequest:
    method, path = write_target(binding, OP_DELETE, offer_id)
    return MerchantRequest(OP_DELETE, method, path, (("dataSource", binding.data_source_name),), None)


def get_product_request(binding: MerchantApiBinding, offer_id: str) -> MerchantRequest:
    rid = _resource_id(binding.content_language, binding.feed_label, offer_id)
    return MerchantRequest(OP_GET, "GET", f"{API_VERSION_PATH}/{binding.account_name}/products/{rid}", (), None)


def list_products_request(
    binding: MerchantApiBinding, page_size: int, page_token: Optional[str] = None,
) -> MerchantRequest:
    """One list page. A page never proves an offer is absent."""
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= LIST_PAGE_SIZE_MAX:
        raise ProtocolShapeError("page_size_out_of_range")
    query: List[Tuple[str, str]] = [("pageSize", str(page_size))]
    if page_token:
        query.append(("pageToken", page_token))
    return MerchantRequest(OP_LIST, "GET", f"{API_VERSION_PATH}/{binding.account_name}/products", tuple(query), None)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_snapshot(request: MerchantRequest) -> str:
    """Immutable canonical text of a request (op, method, path, query, body)."""
    return canonical_json([request.op, request.method, request.path, [list(q) for q in request.query], request.body])


def request_from_snapshot(snapshot: str) -> MerchantRequest:
    """A fresh request rebuilt from :func:`request_snapshot` text (no shared objects)."""
    op, method, path, query, body = json.loads(snapshot)
    return MerchantRequest(op, method, path, tuple(tuple(q) for q in query), body)


def payload_fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Read-back
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DestinationStatus:
    reporting_context: str
    approved_countries: Tuple[str, ...]
    pending_countries: Tuple[str, ...]
    disapproved_countries: Tuple[str, ...]
    known_context: bool


@dataclass(frozen=True)
class ItemIssue:
    code: str
    severity: str
    raw_severity: str
    attribute: str
    reporting_context: str
    resolution: str
    applicable_countries: Tuple[str, ...]


@dataclass(frozen=True)
class ProductReadback:
    name: str
    base64_encoded_name: Optional[str]
    offer_id: str
    content_language: str
    feed_label: str
    data_source: str
    product_attributes: Optional[Dict[str, Any]]
    destinations: Tuple[DestinationStatus, ...]
    issues: Tuple[ItemIssue, ...]
    flags: Tuple[str, ...]
    publication_state: str

    @property
    def malformed(self) -> bool:
        return any(f.endswith(MALFORMED_SUFFIX) for f in self.flags)


def _opt_str(item: Mapping[str, Any], key: str, flags: List[str], flag: str) -> str:
    """Absent → ""; a present non-string is malformed (flagged), never coerced."""
    value = item.get(key)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    flags.append(flag)
    return ""


def _members(value: Any, flags: List[str], flag: str) -> List[Any]:
    """Absent → []; any non-array container is malformed (flagged)."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        flags.append(flag)
        return []
    return list(value)


def _countries(value: Any, flags: List[str]) -> Tuple[str, ...]:
    out: List[str] = []
    for member in _members(value, flags, "countries_malformed"):
        if isinstance(member, str) and _COUNTRY_RE.fullmatch(member):
            out.append(member)
        else:
            flags.append("countries_malformed")
    return tuple(out)


def _publication_state(
    destinations: Tuple[DestinationStatus, ...], issues: Tuple[ItemIssue, ...], flags: List[str],
) -> str:
    if any(f.endswith(MALFORMED_SUFFIX) for f in flags):
        return PUBLICATION_UNKNOWN
    if any(d.disapproved_countries for d in destinations) or any(i.severity == "DISAPPROVED" for i in issues):
        return PUBLICATION_DISAPPROVED
    if flags or not destinations:
        return PUBLICATION_UNKNOWN
    if any(d.pending_countries for d in destinations):
        return PUBLICATION_PENDING
    if all(d.approved_countries for d in destinations):
        return PUBLICATION_APPROVED
    return PUBLICATION_UNKNOWN


def _destination(item: Any, flags: List[str]) -> Optional[DestinationStatus]:
    if not isinstance(item, Mapping):
        flags.append("destination_malformed")
        return None
    context = item.get("reportingContext")
    if not isinstance(context, str) or not context:
        flags.append("destination_malformed")
        return None
    known = context in KNOWN_REPORTING_CONTEXTS
    if not known:
        flags.append("unknown_reporting_context")
    return DestinationStatus(
        context,
        _countries(item.get("approvedCountries"), flags),
        _countries(item.get("pendingCountries"), flags),
        _countries(item.get("disapprovedCountries"), flags),
        known,
    )


def _issue(item: Any, flags: List[str]) -> Optional[ItemIssue]:
    if not isinstance(item, Mapping):
        flags.append("issue_malformed")
        return None
    code = item.get("code")
    raw_severity = item.get("severity")
    if not isinstance(code, str) or not code or not isinstance(raw_severity, (str, type(None))):
        flags.append("issue_malformed")
        return None
    raw_severity = raw_severity or ""
    severity = raw_severity if raw_severity in KNOWN_SEVERITIES else SEVERITY_UNKNOWN
    if severity == SEVERITY_UNKNOWN:
        flags.append("unknown_issue_severity")
    context = _opt_str(item, "reportingContext", flags, "issue_malformed")
    if context and context not in KNOWN_REPORTING_CONTEXTS:
        flags.append("unknown_reporting_context")
    return ItemIssue(
        code, severity, raw_severity,
        _opt_str(item, "attribute", flags, "issue_malformed"), context,
        _opt_str(item, "resolution", flags, "issue_malformed"),
        _countries(item.get("applicableCountries"), flags),
    )


def parse_product_readback(raw: Any) -> ProductReadback:
    """Parse an ``accounts.products`` resource. Never raises on odd shapes.

    Every container and member is validated; anything malformed is flagged
    (``*_malformed``) and makes the publication state ``unknown`` — nothing
    is silently discarded into a more favourable state.
    """
    flags: List[str] = []
    if not isinstance(raw, Mapping):
        flags.append("readback_malformed")
        raw = {}
    status = raw.get("productStatus")
    if status is None:
        flags.append("obsolete_status_field" if "productStatuses" in raw else "status_field_missing")
        status = {}
    elif not isinstance(status, Mapping):
        flags.append("status_malformed")
        status = {}

    destinations = [
        d for d in (_destination(item, flags) for item in _members(
            status.get("destinationStatuses"), flags, "destinations_malformed",
        )) if d is not None
    ]
    issues = [
        i for i in (_issue(item, flags) for item in _members(
            status.get("itemLevelIssues"), flags, "issues_malformed",
        )) if i is not None
    ]

    attrs = raw.get("productAttributes")
    if attrs is not None and not isinstance(attrs, Mapping):
        flags.append("product_attributes_malformed")
    encoded = raw.get("base64EncodedName")
    if encoded is not None and not isinstance(encoded, str):
        flags.append("identity_malformed")
        encoded = ""
    identity = {key: _opt_str(raw, key, flags, "identity_malformed")
                for key in ("name", "offerId", "contentLanguage", "feedLabel", "dataSource")}
    flags = sorted(set(flags))
    return ProductReadback(
        name=identity["name"],
        base64_encoded_name=encoded,
        offer_id=identity["offerId"],
        content_language=identity["contentLanguage"],
        feed_label=identity["feedLabel"],
        data_source=identity["dataSource"],
        product_attributes=dict(attrs) if isinstance(attrs, Mapping) else None,
        destinations=tuple(destinations),
        issues=tuple(issues),
        flags=tuple(flags),
        publication_state=_publication_state(tuple(destinations), tuple(issues), flags),
    )


def readback_identity_state(readback: ProductReadback, binding: MerchantApiBinding, offer_id: str) -> str:
    """Does the read-back describe exactly this offer in exactly this API source?

    Official forms: ``name`` is the raw ``accounts/{account}/products/
    {contentLanguage}~{feedLabel}~{offerId}`` (any characters, compared
    exactly); ``base64EncodedName``, when present, must be the same name
    with the last segment unpadded-base64url encoded. Case-sensitive.
    """
    lang, label = binding.content_language, binding.feed_label
    raw_name = f"{binding.account_name}/products/{lang}~{label}~{offer_id}"
    encoded_name = f"{binding.account_name}/products/{_resource_id(lang, label, offer_id)}"
    if (
        readback.name != raw_name
        or (readback.base64_encoded_name is not None and readback.base64_encoded_name != encoded_name)
        or (readback.offer_id, readback.content_language, readback.feed_label) != (offer_id, lang, label)
    ):
        return READBACK_IDENTITY_MISMATCH
    if readback.data_source != binding.data_source_name:
        return READBACK_FOREIGN_SOURCE
    return READBACK_IDENTITY_MATCHES


def attributes_match(
    desired: Mapping[str, Any], readback: Optional[Mapping[str, Any]], removed: Iterable[str] = (),
) -> bool:
    """True only when every desired field reads back identically and removed fields are gone."""
    if not isinstance(readback, Mapping):
        return False
    for key, value in desired.items():
        if key not in readback or canonical_json(readback[key]) != canonical_json(value):
            return False
    return not any(key in readback for key in removed)


# ─────────────────────────────────────────────────────────────────────────────
# Transport contract and retry classification
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TransportResponse:
    """``status`` None means no HTTP response (``error`` says why)."""

    status: Optional[int]
    headers: Mapping[str, str] = field(default_factory=dict)
    body: Optional[Mapping[str, Any]] = None
    error: Optional[str] = None


class MerchantTransport(Protocol):
    """Type contract only. This slice ships no implementation that does I/O."""

    def send(self, request: MerchantRequest) -> TransportResponse: ...


@dataclass(frozen=True)
class Classification:
    kind: str
    resend_allowed: bool
    requires_readback: bool
    retry_after_s: Optional[int] = None


def parse_retry_after(value: Any, now: int) -> Optional[int]:
    """Delta-seconds or HTTP-date → seconds from *now*; None when unusable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if _RETRY_AFTER_SECONDS_RE.fullmatch(text):
        return int(text)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None or when.tzinfo is None:
        return None
    return max(0, int(when.timestamp()) - now)


def _header(headers: Mapping[str, str], name: str) -> Any:
    for key, value in (headers or {}).items():
        if isinstance(key, str) and key.lower() == name.lower():
            return value
    return None


def classify_response(response: TransportResponse, *, write: bool, now: int) -> Classification:
    status = response.status
    if isinstance(status, int) and not isinstance(status, bool):
        if 200 <= status < 300:
            return Classification(RESP_ACCEPTED, False, write)
        if status == 429:
            return Classification(RESP_RATE_LIMITED, True, False, parse_retry_after(_header(response.headers, "Retry-After"), now))
        if status == 401:
            return Classification(RESP_PERMANENT_AUTH, False, False)
        if status == 403:
            return Classification(RESP_PERMANENT_PERMISSION, False, False)
        if status == 404 and not write:
            return Classification(RESP_NOT_FOUND, False, False)
        if 400 <= status < 500 and status != 408:
            return Classification(RESP_PERMANENT_VALIDATION, False, False)
        if not (status == 408 or 500 <= status < 600):
            if write:
                return Classification(RESP_UNCERTAIN_WRITE, False, True)
            return Classification(RESP_PERMANENT_UNEXPECTED, False, False)
    # 408 / 5xx / no response (timeout, reset): a write may have landed.
    if write:
        return Classification(RESP_UNCERTAIN_WRITE, False, True)
    return Classification(RESP_TRANSIENT_READ, True, False)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    base_delay_s: int
    max_delay_s: int

    def __post_init__(self) -> None:
        for name in ("max_attempts", "base_delay_s", "max_delay_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name}_invalid")


def next_retry_at(policy: RetryPolicy, attempt: int, now: int, retry_after_s: Optional[int]) -> Optional[int]:
    """Deterministic time of attempt ``attempt + 1``; None means give up.

    Exponential ``base * 2**(attempt-1)`` capped at ``max_delay_s``, never
    earlier than Retry-After; a Retry-After beyond the cap gives up.
    """
    if attempt >= policy.max_attempts:
        return None
    delay = min(policy.base_delay_s * 2 ** (attempt - 1), policy.max_delay_s)
    if retry_after_s is not None:
        if retry_after_s > policy.max_delay_s:
            return None
        delay = max(delay, retry_after_s)
    return now + delay


__all__ = [
    "Classification",
    "DATA_SOURCE_INPUT_API",
    "INSERT_BODY_FIELDS",
    "InvalidBindingError",
    "MerchantApiBinding",
    "MerchantRequest",
    "MerchantTransport",
    "ProductReadback",
    "ProtocolShapeError",
    "RetryPolicy",
    "SUPPORTED_PRODUCT_ATTRIBUTES",
    "TransportResponse",
    "attribute_changes",
    "attributes_match",
    "canonical_json",
    "classify_response",
    "delete_request",
    "freshness_issue",
    "get_product_request",
    "insert_request",
    "list_products_request",
    "next_retry_at",
    "parse_product_readback",
    "parse_retry_after",
    "patch_request",
    "payload_fingerprint",
    "readback_identity_state",
    "request_from_snapshot",
    "request_snapshot",
    "validate_binding",
    "write_target",
]
