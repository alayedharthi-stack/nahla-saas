"""
Configuration and fail-closed availability of the Shopify connection.

Everything defaults to off. The connection is available only when **all** of
these hold (the first failure is the reason; no value is ever returned):

  NAHLA_SHOPIFY_CONNECTION_ENABLED  truthy (1/true/yes/on). Off: every route
                                    answers 404 and nothing runs.
  SHOPIFY_CLIENT_ID                 the app's client id.
  SHOPIFY_CLIENT_SECRET             the app's client secret (signs the OAuth
                                    callback query and webhook bodies).
  SHOPIFY_OAUTH_REDIRECT_URI        exact ``https://<api host>/merchant/integrations/shopify/callback``
                                    (no port, query, fragment or credentials);
                                    must equal the URL registered with Shopify.
  DASHBOARD_URL                     https dashboard origin; the callback only
                                    ever returns to ``<origin>/integrations/shopify/complete``.
  SHOPIFY_TOKEN_ENC_KEY             dedicated 32-byte url-safe base64 AES key.
                                    There is no fallback (not JWT_SECRET, not
                                    WA_TOKEN_ENC_KEY, not TOTP_ENC_KEY) and a
                                    key equal to one of those is refused.
  SHOPIFY_ADMIN_API_VERSION         optional ``YYYY-MM``; default below.

Scopes are fixed here, not configurable: read-only catalog access only.
"""
from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple
from urllib.parse import urlsplit

ENABLED_ENV = "NAHLA_SHOPIFY_CONNECTION_ENABLED"
CLIENT_ID_ENV = "SHOPIFY_CLIENT_ID"
CLIENT_SECRET_ENV = "SHOPIFY_CLIENT_SECRET"
REDIRECT_URI_ENV = "SHOPIFY_OAUTH_REDIRECT_URI"
DASHBOARD_URL_ENV = "DASHBOARD_URL"
ENCRYPTION_KEY_ENV = "SHOPIFY_TOKEN_ENC_KEY"
API_VERSION_ENV = "SHOPIFY_ADMIN_API_VERSION"
_FOREIGN_KEY_ENVS = ("WA_TOKEN_ENC_KEY", "TOTP_ENC_KEY", "JWT_SECRET")

ROUTE_PREFIX = "/merchant/integrations/shopify"
CALLBACK_PATH = f"{ROUTE_PREFIX}/callback"
WEBHOOK_UNINSTALL_PATH = "/webhooks/shopify/app-uninstalled"
DASHBOARD_COMPLETE_PATH = "/integrations/shopify/complete"

# Least privilege: read-only catalog. No customer, order, payment, inventory
# write, fulfillment or marketing scope is requested or accepted.
REQUESTED_SCOPES: Tuple[str, ...] = ("read_products",)
ALLOWED_GRANTED_SCOPES = frozenset(REQUESTED_SCOPES)

DEFAULT_API_VERSION = "2026-07"
_API_VERSION_RE = re.compile(r"20[2-9][0-9]-(01|04|07|10)")
# A DNS host name only (no IP literal, no brackets, no trailing dot).
_HOST_RE = re.compile(r"(?=.{4,253}\Z)[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)+")

R_DISABLED = "disabled"
R_CLIENT_CREDENTIALS_MISSING = "client_credentials_missing"
R_REDIRECT_URI_INVALID = "redirect_uri_invalid"
R_DASHBOARD_URL_INVALID = "dashboard_url_invalid"
R_ENCRYPTION_KEY_MISSING = "encryption_key_missing"
R_ENCRYPTION_KEY_INVALID = "encryption_key_invalid"
R_ENCRYPTION_KEY_REUSED = "encryption_key_reused"
R_API_VERSION_INVALID = "api_version_invalid"


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return env if env is not None else os.environ


def _get(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name) or "").strip()


def flag_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    return _get(_env(env), ENABLED_ENV).lower() in ("1", "true", "yes", "on")


def _https_host(raw: str) -> Optional[Tuple[str, object]]:
    if not raw or any(ch.isspace() for ch in raw) or not raw.isascii():
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
        host = (parts.hostname or "").lower()
    except ValueError:  # malformed IPv6 literal, bad port, …
        return None
    if parts.scheme != "https" or parts.username or parts.password or port is not None:
        return None
    if not _HOST_RE.fullmatch(host) or ".." in host:
        return None
    return host, parts


def canonical_redirect_uri(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The configured callback when it is exactly ``https://<host>/merchant/integrations/shopify/callback``."""
    raw = _get(_env(env), REDIRECT_URI_ENV)
    checked = _https_host(raw)
    if checked is None:
        return None
    host, parts = checked
    if parts.query or parts.fragment or parts.path != CALLBACK_PATH:
        return None
    canonical = f"https://{host}{CALLBACK_PATH}"
    # Byte-exact: Shopify requires the redirect_uri to match the registered one.
    return canonical if raw == canonical else None


def dashboard_complete_url(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The only return target: ``https://<dashboard host>/integrations/shopify/complete``."""
    raw = _get(_env(env), DASHBOARD_URL_ENV).rstrip("/")
    checked = _https_host(raw)
    if checked is None:
        return None
    host, parts = checked
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        return None
    return f"https://{host}{DASHBOARD_COMPLETE_PATH}"


def parse_encryption_key(raw: str) -> Optional[bytes]:
    """32 raw bytes from url-safe base64 (padding optional), else None."""
    value = str(raw or "").strip()
    if not value or not re.fullmatch(r"[A-Za-z0-9_\-]{43}=?", value):
        return None
    try:
        key = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        return None
    return key if len(key) == 32 else None


def _decoded_variants(raw: str) -> set:
    """Every byte string *raw* decodes to as url-safe or standard base64
    (padding optional), plus its own bytes — used only to detect key reuse."""
    value = str(raw or "").strip()
    out = {value.encode("utf-8")} if value else set()
    padded = value + "=" * (-len(value) % 4)
    for decode in (base64.urlsafe_b64decode, base64.standard_b64decode):
        try:
            out.add(decode(padded.encode("ascii")))
        except (ValueError, TypeError, UnicodeEncodeError):
            continue
    return out


def encryption_key_reason(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    e = _env(env)
    raw = _get(e, ENCRYPTION_KEY_ENV)
    if not raw:
        return R_ENCRYPTION_KEY_MISSING
    key = parse_encryption_key(raw)
    if key is None:
        return R_ENCRYPTION_KEY_INVALID
    for name in _FOREIGN_KEY_ENVS:
        foreign = _get(e, name)
        if foreign and (foreign == raw or key in _decoded_variants(foreign)):
            return R_ENCRYPTION_KEY_REUSED
    return None


def api_version(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    raw = _get(_env(env), API_VERSION_ENV)
    if not raw:
        return DEFAULT_API_VERSION
    return raw if _API_VERSION_RE.fullmatch(raw) else None


@dataclass(frozen=True)
class ShopifyConfig:
    """A complete, validated configuration. The secret and key never print."""

    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    dashboard_complete_url: str
    encryption_key: bytes = field(repr=False)
    api_version: str


@dataclass(frozen=True)
class Availability:
    available: bool
    reason: Optional[str] = None
    config: Optional[ShopifyConfig] = field(default=None, repr=False)


def evaluate_availability(env: Optional[Mapping[str, str]] = None) -> Availability:
    """Every precondition, in order; the first failure is the reason."""
    e = _env(env)
    if not flag_enabled(e):
        return Availability(False, R_DISABLED)
    client_id, client_secret = _get(e, CLIENT_ID_ENV), _get(e, CLIENT_SECRET_ENV)
    if not client_id or not client_secret:
        return Availability(False, R_CLIENT_CREDENTIALS_MISSING)
    redirect_uri = canonical_redirect_uri(e)
    if redirect_uri is None:
        return Availability(False, R_REDIRECT_URI_INVALID)
    complete_url = dashboard_complete_url(e)
    if complete_url is None:
        return Availability(False, R_DASHBOARD_URL_INVALID)
    key_reason = encryption_key_reason(e)
    if key_reason:
        return Availability(False, key_reason)
    version = api_version(e)
    if version is None:
        return Availability(False, R_API_VERSION_INVALID)
    return Availability(
        True,
        None,
        ShopifyConfig(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            dashboard_complete_url=complete_url,
            encryption_key=parse_encryption_key(_get(e, ENCRYPTION_KEY_ENV)),
            api_version=version,
        ),
    )


def webhook_secret(env: Optional[Mapping[str, str]] = None) -> str:
    """Webhook bodies are signed with the app's client secret ('' when missing)."""
    return _get(_env(env), CLIENT_SECRET_ENV)
