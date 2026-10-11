"""
Shopify authorization-code grant primitives (standalone app).

Callback query verification (Shopify OAuth query HMAC — **not** the webhook
HMAC): remove ``hmac``, sort the remaining parameters by name, join
``name=value`` with ``&``, HMAC-SHA256 with the client secret, **hex** digest,
constant-time compare. The webhook signature is base64 over the raw body and
lives in ``webhooks.py``.

Fail-closed parsing: a malformed query, a duplicated parameter, an array
parameter, an undecodable value, a value outside a conservative character
set (where escaping conventions could differ), a missing required parameter,
a missing secret or a timestamp outside the window all refuse before any
state is looked up.

Outbound calls go only to ``https://<canonical shop>/admin/...``; redirects are
never followed; credentials travel in the JSON body or the
``X-Shopify-Access-Token`` header, never in a URL. Upstream bodies are never
logged or reflected (they can carry tokens).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Mapping, Optional, Tuple
from urllib.parse import parse_qsl, urlencode

import httpx

from services.shopify_connection.config import ALLOWED_GRANTED_SCOPES, REQUESTED_SCOPES
from services.shopify_connection.shop_domain import canonical_shop_domain

logger = logging.getLogger("nahla.shopify_connection")

CALLBACK_REQUIRED = ("code", "hmac", "shop", "state", "timestamp")
_MAX_QUERY_LENGTH = 4096
_MAX_FIELDS = 16
_KEY_RE = re.compile(r"[a-z_]{1,32}")
# Values whose escaping is unambiguous across Shopify's documented and library
# conventions. Anything else is refused rather than guessed.
_VALUE_RE = re.compile(r"[A-Za-z0-9._\-+/=]{0,512}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
TIMESTAMP_MAX_AGE_SECONDS = 600
TIMESTAMP_MAX_SKEW_SECONDS = 60

_HTTP_TIMEOUT = 10.0
_MAX_RESPONSE_BYTES = 65536
_TOKEN_RE = re.compile(r"[\x21-\x7e]{1,512}")
_SHOP_GID_RE = re.compile(r"gid://shopify/Shop/([1-9][0-9]{0,19})")
SHOP_IDENTITY_QUERY = "query NahlaShopIdentity { shop { id myshopifyDomain } }"


class OAuthQueryError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ── Callback query ────────────────────────────────────────────────────────────

def parse_callback_query(raw_query: str) -> Dict[str, str]:
    """Strictly parse the raw callback query. Raises OAuthQueryError."""
    if not isinstance(raw_query, str) or not raw_query or len(raw_query) > _MAX_QUERY_LENGTH:
        raise OAuthQueryError("query_invalid")
    try:
        pairs = parse_qsl(
            raw_query, keep_blank_values=True, strict_parsing=True,
            errors="strict", max_num_fields=_MAX_FIELDS,
        )
    except (ValueError, UnicodeDecodeError):
        raise OAuthQueryError("query_invalid") from None
    params: Dict[str, str] = {}
    for key, value in pairs:
        if not _KEY_RE.fullmatch(key):
            raise OAuthQueryError("parameter_invalid")
        if key in params:
            raise OAuthQueryError("parameter_duplicated")
        if not _VALUE_RE.fullmatch(value):
            raise OAuthQueryError("parameter_invalid")
        params[key] = value
    for key in CALLBACK_REQUIRED:
        if not params.get(key):
            raise OAuthQueryError("parameter_missing")
    return params


def query_hmac_message(params: Mapping[str, str]) -> str:
    """The signed message: every parameter except ``hmac``, sorted by name."""
    return "&".join(f"{k}={params[k]}" for k in sorted(params) if k != "hmac")


def compute_query_hmac(params: Mapping[str, str], secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), query_hmac_message(params).encode("utf-8"), hashlib.sha256).hexdigest()


def verify_query_hmac(params: Mapping[str, str], secret: str) -> None:
    """Raises OAuthQueryError unless the hex HMAC matches. A missing secret refuses."""
    if not secret:
        raise OAuthQueryError("secret_missing")
    received = str(params.get("hmac") or "")
    if not _HEX64_RE.fullmatch(received):
        raise OAuthQueryError("hmac_invalid")
    if not hmac.compare_digest(compute_query_hmac(params, secret), received):
        raise OAuthQueryError("hmac_invalid")


def verify_timestamp(params: Mapping[str, str], *, now: Optional[float] = None) -> int:
    raw = str(params.get("timestamp") or "")
    if not re.fullmatch(r"[1-9][0-9]{8,10}", raw):
        raise OAuthQueryError("timestamp_invalid")
    stamp = int(raw)
    current = int(now if now is not None else time.time())
    if stamp > current + TIMESTAMP_MAX_SKEW_SECONDS or current - stamp > TIMESTAMP_MAX_AGE_SECONDS:
        raise OAuthQueryError("timestamp_out_of_window")
    return stamp


@dataclass(frozen=True)
class VerifiedCallback:
    shop_domain: str
    state: str = field(repr=False)
    code: str = field(repr=False)


def verify_callback(raw_query: str, *, secret: str, now: Optional[float] = None) -> VerifiedCallback:
    """Parse, then HMAC, timestamp and shop format — before any state lookup."""
    params = parse_callback_query(raw_query)
    verify_query_hmac(params, secret)
    verify_timestamp(params, now=now)
    shop = canonical_shop_domain(params["shop"])
    if shop is None or shop != params["shop"]:
        raise OAuthQueryError("shop_invalid")
    return VerifiedCallback(shop_domain=shop, state=params["state"], code=params["code"])


def build_authorize_url(*, shop_domain: str, client_id: str, redirect_uri: str, state: str) -> str:
    shop = canonical_shop_domain(shop_domain)
    if shop is None or shop != shop_domain:
        raise OAuthQueryError("shop_invalid")
    query = urlencode({
        "client_id": client_id,
        "scope": ",".join(REQUESTED_SCOPES),
        "redirect_uri": redirect_uri,
        "state": state,
    })
    # No grant_options[]=per-user: an offline (shop) token is requested.
    return f"https://{shop}/admin/oauth/authorize?{query}"


# ── Admin API client ──────────────────────────────────────────────────────────

class ShopifyApiError(Exception):
    """``kind`` is ``permanent`` (re-authorization required), ``rejected``
    (credentials refused), ``transient`` (retry later) or ``invalid_response``.
    ``str()`` is the code only — never an upstream body."""

    def __init__(self, kind: str, code: str):
        super().__init__(code)
        self.kind = kind
        self.code = code


@dataclass(frozen=True)
class TokenGrant:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in: int
    refresh_token_expires_in: int
    scopes: FrozenSet[str]


@dataclass(frozen=True)
class ShopIdentity:
    shop_gid: str
    shop_domain: str


def parse_scopes(raw: Any) -> FrozenSet[str]:
    if not isinstance(raw, str):
        return frozenset()
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def validate_scopes(scopes: FrozenSet[str]) -> None:
    """Required ⊆ granted ⊆ allowed (read-only catalog); anything else refuses."""
    if not set(REQUESTED_SCOPES) <= scopes:
        raise ShopifyApiError("invalid_response", "scope_missing")
    if not scopes <= ALLOWED_GRANTED_SCOPES:
        raise ShopifyApiError("invalid_response", "scope_excess")


def _positive_int(value: Any, *, upper: int) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value <= upper else None


def parse_token_grant(body: Mapping[str, Any]) -> TokenGrant:
    """Validate an expiring offline token response. Raises ShopifyApiError."""
    if body.get("associated_user") is not None or body.get("associated_user_scope") is not None:
        raise ShopifyApiError("invalid_response", "online_token_refused")
    access = body.get("access_token")
    refresh = body.get("refresh_token")
    if not isinstance(access, str) or not _TOKEN_RE.fullmatch(access):
        raise ShopifyApiError("invalid_response", "access_token_invalid")
    if not isinstance(refresh, str) or not _TOKEN_RE.fullmatch(refresh) or refresh == access:
        raise ShopifyApiError("invalid_response", "expiring_token_missing")
    expires_in = _positive_int(body.get("expires_in"), upper=30 * 86400)
    refresh_expires_in = _positive_int(body.get("refresh_token_expires_in"), upper=400 * 86400)
    if expires_in is None or refresh_expires_in is None:
        raise ShopifyApiError("invalid_response", "expiring_token_missing")
    scopes = parse_scopes(body.get("scope"))
    validate_scopes(scopes)
    return TokenGrant(access, refresh, expires_in, refresh_expires_in, scopes)


class ShopifyApi:
    """Minimal Admin API client. ``transport`` is injectable (tests use a mock)."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        api_version: str,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout: float = _HTTP_TIMEOUT,
    ):
        self._client_id = client_id
        self.__client_secret = client_secret
        self._api_version = api_version
        self._transport = transport
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"ShopifyApi(api_version={self._api_version!r}, <credentials withheld>)"

    __str__ = __repr__

    @staticmethod
    def _admin_url(shop_domain: str, path: str) -> str:
        shop = canonical_shop_domain(shop_domain)
        if shop is None or shop != shop_domain:
            raise ShopifyApiError("invalid_response", "shop_invalid")
        return f"https://{shop}/admin/{path}"

    async def _post(self, url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        base_headers = {"Accept": "application/json", "Content-Type": "application/json"}
        base_headers.update(headers)
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout, follow_redirects=False,
            ) as client:
                resp = await client.post(url, content=json.dumps(payload).encode("utf-8"), headers=base_headers)
        except httpx.HTTPError as exc:
            logger.warning("[SHOPIFY_CONNECTION] transport failure kind=%s", type(exc).__name__)
            raise ShopifyApiError("transient", "transport_error") from None
        if len(resp.content or b"") > _MAX_RESPONSE_BYTES:
            raise ShopifyApiError("invalid_response", "response_too_large")
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = {}
        return int(resp.status_code), body if isinstance(body, dict) else {}

    @staticmethod
    def _classify_token_failure(status: int) -> ShopifyApiError:
        if status in (400, 401, 403):
            return ShopifyApiError("permanent", "grant_rejected")
        if status == 429 or status >= 500:
            return ShopifyApiError("transient", "upstream_unavailable")
        return ShopifyApiError("invalid_response", "unexpected_status")

    async def exchange_code(self, *, shop_domain: str, code: str) -> TokenGrant:
        status, body = await self._post(
            self._admin_url(shop_domain, "oauth/access_token"),
            {
                "client_id": self._client_id,
                "client_secret": self.__client_secret,
                "code": code,
                "expiring": 1,
            },
            {},
        )
        if status != 200:
            raise self._classify_token_failure(status)
        return parse_token_grant(body)

    async def refresh(self, *, shop_domain: str, refresh_token: str) -> TokenGrant:
        status, body = await self._post(
            self._admin_url(shop_domain, "oauth/access_token"),
            {
                "client_id": self._client_id,
                "client_secret": self.__client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            {},
        )
        if status != 200:
            raise self._classify_token_failure(status)
        return parse_token_grant(body)

    async def shop_identity(self, *, shop_domain: str, access_token: str) -> ShopIdentity:
        """Authenticated GraphQL identity check: the token's shop id and domain."""
        status, body = await self._post(
            self._admin_url(shop_domain, f"api/{self._api_version}/graphql.json"),
            {"query": SHOP_IDENTITY_QUERY},
            {"X-Shopify-Access-Token": access_token},
        )
        if status in (401, 403):
            raise ShopifyApiError("rejected", "credentials_rejected")
        if status == 429 or status >= 500:
            raise ShopifyApiError("transient", "upstream_unavailable")
        if status != 200 or body.get("errors"):
            raise ShopifyApiError("invalid_response", "identity_unverified")
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        shop = data.get("shop") if isinstance(data.get("shop"), dict) else {}
        gid = shop.get("id")
        domain = canonical_shop_domain(shop.get("myshopifyDomain"))
        if not isinstance(gid, str) or not _SHOP_GID_RE.fullmatch(gid) or domain is None:
            raise ShopifyApiError("invalid_response", "identity_unverified")
        return ShopIdentity(shop_gid=gid, shop_domain=domain)


def numeric_shop_id(shop_gid: str) -> Optional[int]:
    match = _SHOP_GID_RE.fullmatch(str(shop_gid or ""))
    return int(match.group(1)) if match else None
