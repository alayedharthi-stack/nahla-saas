"""
core/meta_catalog_consent_config.py
───────────────────────────────────
Configuration and fail-closed availability for the catalog-only Meta consent
path (``catalog_management`` + ``business_management``; no WhatsApp scope).

Every name is catalog-specific; nothing here reads or changes the WhatsApp
Embedded Signup configuration. All values default to empty, so the path is
off unless an operator configures every item below **and** selects either a
verified catalog review environment (``core.review_environment``) or the
explicit primary-deployment opt-in. Selecting both modes fails closed:

  NAHLA_META_CATALOG_CONSENT_ENABLED   truthy (1/true/yes/on) to enable.
  NAHLA_META_CATALOG_CONSENT_PRIMARY_ENABLED
                                     separate truthy opt-in for primary only;
                                     NAHLA_CATALOG_REVIEW_ENV must be off.
  META_CATALOG_CONSENT_CONFIG_ID       dedicated Facebook Login for Business
                                       configuration that requests exactly
                                       catalog_management + business_management.
  META_CATALOG_CONSENT_REDIRECT_URI    exact https callback,
                                       ``https://<api host>/merchant/catalog/meta-consent/callback``
                                       (no port, query, fragment or credentials;
                                       primary accepts only the fixed URL below;
                                       review never accepts a production host).
  META_CATALOG_CONSENT_APPROVED_ASSETS server-side approvals,
                                       ``<tenant_id>:<catalog_id>:<business_id>``
                                       comma-separated; one entry per tenant and
                                       per catalog. A caller never chooses them.
  META_CATALOG_GRAPH_API_VERSION       optional catalog-only version override;
                                       unset preserves META_GRAPH_API_VERSION.
                                       Malformed explicit values fail closed.
  META_APP_ID / META_APP_SECRET        the Meta app the consent is issued for.
  WA_TOKEN_ENC_KEY                     dedicated Fernet key (no dev fallback).
  DASHBOARD_URL                        https dashboard; the callback
                                       returns to ``<DASHBOARD_URL>/catalog``.

Primary mode requires exactly
``https://api.nahlah.ai/merchant/catalog/meta-consent/callback`` and
``https://app.nahlah.ai``. It does not enable or bypass the review-environment
guard: review continues to require its own isolated database and marker.

Reasons are stable codes; no value of any variable is ever returned.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from core.meta_catalog_graph import CatalogGraphVersionError, catalog_graph_api_version

ENABLED_ENV = "NAHLA_META_CATALOG_CONSENT_ENABLED"
PRIMARY_ENABLED_ENV = "NAHLA_META_CATALOG_CONSENT_PRIMARY_ENABLED"
CONFIG_ID_ENV = "META_CATALOG_CONSENT_CONFIG_ID"
REDIRECT_URI_ENV = "META_CATALOG_CONSENT_REDIRECT_URI"
APPROVED_ASSETS_ENV = "META_CATALOG_CONSENT_APPROVED_ASSETS"
ENCRYPTION_KEY_ENV = "WA_TOKEN_ENC_KEY"

CALLBACK_PATH = "/merchant/catalog/meta-consent/callback"
DASHBOARD_RETURN_PATH = "/catalog"
PRIMARY_REDIRECT_URI = f"https://api.nahlah.ai{CALLBACK_PATH}"
PRIMARY_DASHBOARD_URL = "https://app.nahlah.ai"
REQUESTED_SCOPES: Tuple[str, ...] = ("catalog_management", "business_management")

_PRODUCTION_HOSTS = frozenset({"nahlah.ai", "www.nahlah.ai", "app.nahlah.ai", "api.nahlah.ai"})
_META_ID = re.compile(r"^[0-9]{5,32}$")

R_DISABLED = "disabled"
R_DEPLOYMENT_MODE_CONFLICT = "deployment_mode_conflict"
R_REVIEW_ENV_REQUIRED = "review_environment_required"
R_REVIEW_ENV_UNVERIFIED = "review_environment_unverified"
R_APP_CREDENTIALS_MISSING = "app_credentials_missing"
R_CONFIG_ID_MISSING = "config_id_missing"
R_GRAPH_VERSION_INVALID = "catalog_graph_version_invalid"
R_REDIRECT_URI_INVALID = "redirect_uri_invalid"
R_DASHBOARD_URL_INVALID = "dashboard_url_invalid"
R_ENCRYPTION_KEY_MISSING = "encryption_key_missing"
R_ENCRYPTION_KEY_INVALID = "encryption_key_invalid"
R_APPROVALS_INVALID = "approvals_invalid"
R_TENANT_NOT_APPROVED = "tenant_not_approved"


@dataclass(frozen=True)
class CatalogApproval:
    tenant_id: int
    catalog_id: str
    business_id: str


@dataclass(frozen=True)
class ConsentAvailability:
    available: bool
    reason: Optional[str] = None
    approval: Optional[CatalogApproval] = None
    redirect_uri: Optional[str] = None
    dashboard_return_url: Optional[str] = None


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return env if env is not None else os.environ


def consent_flag_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    return _truthy(_env(env).get(ENABLED_ENV))


def primary_consent_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Separate primary-deployment opt-in; never inferred from a host or stage."""
    return _truthy(_env(env).get(PRIMARY_ENABLED_ENV))


def _mixed_deployment_modes(env: Mapping[str, str]) -> bool:
    from core.review_environment import review_env_enabled  # noqa: PLC0415

    return primary_consent_enabled(env) and review_env_enabled(env)


def _https_host(raw: str) -> Optional[Tuple[str, Any]]:
    """(lowercase host, parsed) for a credential-free https URL on the default port."""
    value = str(raw or "").strip()
    if not value or any(ch.isspace() for ch in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.username or parts.password:
            return None
        port = parts.port
    except ValueError:
        return None
    if port not in (None, 443):
        return None
    host = (parts.hostname or "").strip().lower()
    if not host or "." not in host:
        return None
    return host, parts


def canonical_redirect_uri(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The exact configured callback, or None when it is not acceptable."""
    e = _env(env)
    raw = str(e.get(REDIRECT_URI_ENV) or "").strip()
    if _mixed_deployment_modes(e):
        return None
    if primary_consent_enabled(e):
        return raw if raw == PRIMARY_REDIRECT_URI else None
    checked = _https_host(raw)
    if checked is None:
        return None
    host, parts = checked
    if host.rstrip(".") in _PRODUCTION_HOSTS or parts.query or parts.fragment or parts.path != CALLBACK_PATH:
        return None
    canonical = f"https://{host}{CALLBACK_PATH}"
    # Byte-exact: the value sent to Meta and bound into state is the configured string.
    return canonical if raw == canonical else None


def dashboard_return_url(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Fixed ``https://<dashboard>/catalog`` return target, or None."""
    e = _env(env)
    raw = str(e.get("DASHBOARD_URL") or "").strip()
    if _mixed_deployment_modes(e):
        return None
    if primary_consent_enabled(e):
        return f"{PRIMARY_DASHBOARD_URL}{DASHBOARD_RETURN_PATH}" if raw == PRIMARY_DASHBOARD_URL else None
    raw = raw.rstrip("/")
    checked = _https_host(raw)
    if checked is None:
        return None
    host, parts = checked
    if host.rstrip(".") in _PRODUCTION_HOSTS or parts.query or parts.fragment or parts.path not in ("", "/"):
        return None
    return f"https://{host}{DASHBOARD_RETURN_PATH}"


def parse_approved_assets(raw: Optional[str]) -> Optional[Dict[int, CatalogApproval]]:
    """``tenant:catalog:business`` entries → {tenant_id: approval}; None when malformed.

    Ambiguity fails closed: a tenant or a catalog listed twice invalidates the
    whole value.
    """
    text_value = str(raw or "").strip()
    if not text_value:
        return {}
    out: Dict[int, CatalogApproval] = {}
    seen_catalogs: set = set()
    for entry in text_value.split(","):
        fields = [f.strip() for f in entry.strip().split(":")]
        if len(fields) != 3:
            return None
        tenant_raw, catalog_id, business_id = fields
        if not tenant_raw.isdigit() or int(tenant_raw) <= 0:
            return None
        if not _META_ID.match(catalog_id) or not _META_ID.match(business_id):
            return None
        tenant_id = int(tenant_raw)
        if tenant_id in out or catalog_id in seen_catalogs:
            return None
        seen_catalogs.add(catalog_id)
        out[tenant_id] = CatalogApproval(tenant_id, catalog_id, business_id)
    return out


def approval_for_tenant(tenant_id: int, env: Optional[Mapping[str, str]] = None) -> Optional[CatalogApproval]:
    approvals = parse_approved_assets(_env(env).get(APPROVED_ASSETS_ENV))
    if not approvals:
        return None
    try:
        return approvals.get(int(tenant_id))
    except (TypeError, ValueError):
        return None


def encryption_key_reason(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """None when the dedicated Fernet key is present and valid (never the JWT fallback)."""
    key = str(_env(env).get(ENCRYPTION_KEY_ENV) or "").strip()
    if not key:
        return R_ENCRYPTION_KEY_MISSING
    try:
        from cryptography.fernet import Fernet  # noqa: PLC0415

        Fernet(key.encode("utf-8"))
    except Exception:  # noqa: BLE001 — an invalid key is a stable code, never echoed
        return R_ENCRYPTION_KEY_INVALID
    return None


def meta_app_credentials(env: Optional[Mapping[str, str]] = None) -> Tuple[str, str]:
    e = _env(env)
    return str(e.get("META_APP_ID") or "").strip(), str(e.get("META_APP_SECRET") or "").strip()


def runtime_consent_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Revalidate configuration before using any stored authorization.

    Primary validation is static. Review also verifies its actual database
    marker: a flag alone is not proof of a verified boot or worker environment.
    Row/app/asset/expiry checks remain in the stored-authorization service.
    """
    return evaluate_consent_availability(env).available


def evaluate_consent_availability(
    env: Optional[Mapping[str, str]] = None,
    *,
    tenant_id: Optional[int] = None,
    with_database_marker: bool = True,
    marker_reader: Optional[Callable[[str], Any]] = None,
) -> ConsentAvailability:
    """Every precondition, in order; the first failure is the reason."""
    from core.review_environment import evaluate_review_environment, review_env_enabled  # noqa: PLC0415

    e = _env(env)
    if not consent_flag_enabled(e):
        return ConsentAvailability(False, R_DISABLED)
    if _mixed_deployment_modes(e):
        return ConsentAvailability(False, R_DEPLOYMENT_MODE_CONFLICT)
    if not primary_consent_enabled(e):
        if not review_env_enabled(e):
            return ConsentAvailability(False, R_REVIEW_ENV_REQUIRED)
        review = evaluate_review_environment(e, with_database_marker=with_database_marker, marker_reader=marker_reader)
        if not (review.enabled and review.ok):
            return ConsentAvailability(False, R_REVIEW_ENV_UNVERIFIED)
    app_id, app_secret = meta_app_credentials(e)
    if not app_id or not app_secret:
        return ConsentAvailability(False, R_APP_CREDENTIALS_MISSING)
    if not str(e.get(CONFIG_ID_ENV) or "").strip():
        return ConsentAvailability(False, R_CONFIG_ID_MISSING)
    try:
        catalog_graph_api_version(e)
    except CatalogGraphVersionError:
        return ConsentAvailability(False, R_GRAPH_VERSION_INVALID)
    redirect_uri = canonical_redirect_uri(e)
    if redirect_uri is None:
        return ConsentAvailability(False, R_REDIRECT_URI_INVALID)
    return_url = dashboard_return_url(e)
    if return_url is None:
        return ConsentAvailability(False, R_DASHBOARD_URL_INVALID)
    key_reason = encryption_key_reason(e)
    if key_reason:
        return ConsentAvailability(False, key_reason)
    approvals = parse_approved_assets(e.get(APPROVED_ASSETS_ENV))
    if approvals is None:
        return ConsentAvailability(False, R_APPROVALS_INVALID)
    approval = None
    if tenant_id is not None:
        approval = approvals.get(int(tenant_id))
        if approval is None:
            return ConsentAvailability(False, R_TENANT_NOT_APPROVED, redirect_uri=redirect_uri,
                                       dashboard_return_url=return_url)
    return ConsentAvailability(True, None, approval, redirect_uri, return_url)


__all__ = [
    "APPROVED_ASSETS_ENV",
    "CALLBACK_PATH",
    "CONFIG_ID_ENV",
    "CatalogApproval",
    "ConsentAvailability",
    "DASHBOARD_RETURN_PATH",
    "ENABLED_ENV",
    "PRIMARY_DASHBOARD_URL",
    "PRIMARY_ENABLED_ENV",
    "PRIMARY_REDIRECT_URI",
    "REDIRECT_URI_ENV",
    "REQUESTED_SCOPES",
    "approval_for_tenant",
    "canonical_redirect_uri",
    "consent_flag_enabled",
    "dashboard_return_url",
    "encryption_key_reason",
    "evaluate_consent_availability",
    "meta_app_credentials",
    "parse_approved_assets",
    "primary_consent_enabled",
    "runtime_consent_enabled",
]
