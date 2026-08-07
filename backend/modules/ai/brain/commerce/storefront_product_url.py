"""
Storefront completion URL SoT — product page first, fail closed.

Used by checkout_route_owner when the customer selects online storefront.
Does not invent URLs. Does not teach the LLM. Platform owns the link truth.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger("nahla.brain.storefront_product_url")

# WhatsApp interactive CTA display_text max length.
WA_CTA_LABEL_MAX = 20

# Customer-facing CTA labels (operational labels, not conversational prose).
CTA_LABEL_PRODUCT = "فتح المنتج"  # 10 chars
CTA_LABEL_STORE = "فتح المتجر"  # 10 chars

_PLATFORM_HOST_SUFFIXES = (
    "nahlah.ai",
    "nahla.ai",
    "nahlah.com",
    "nahla.com",
)

_PLATFORM_PATH_MARKERS = (
    "/register",
    "/signup",
    "/login",
    "/auth",
)


@dataclass(frozen=True)
class StorefrontLinkResolution:
    """Resolved storefront completion link (or honest fail-closed)."""

    found: bool
    url: str = ""
    source: str = "none"
    reason: str = ""
    cta_label: str = CTA_LABEL_STORE
    has_product_focus: bool = False
    product_id: str = ""

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "found": self.found,
            "url_len": len(self.url or ""),
            "source": self.source,
            "reason": self.reason,
            "cta_label_len": len(self.cta_label or ""),
            "has_product_focus": self.has_product_focus,
            "product_id": self.product_id or "",
        }


def _normalise_http_url(url: str) -> str:
    s = str(url or "").strip().rstrip("/")
    if not s:
        return ""
    low = s.lower()
    if "<" in s or "magicmock" in low or "mock" in low:
        return ""
    if not low.startswith(("http://", "https://")):
        s = "https://" + s.lstrip("/")
    if not re.match(r"^https?://[^\s<]+", s, re.IGNORECASE):
        return ""
    return s


def is_platform_non_merchant_url(url: str) -> bool:
    """True when URL is a Nahla platform page, not a merchant storefront/PDP."""
    raw = _normalise_http_url(url)
    if not raw:
        return False
    try:
        parsed = urlparse(raw)
    except Exception:  # noqa: BLE001
        return False
    host = (parsed.hostname or "").lower().lstrip(".")
    if not host:
        return False
    path = (parsed.path or "").lower()
    is_platform_host = any(
        host == suffix or host.endswith("." + suffix)
        for suffix in _PLATFORM_HOST_SUFFIXES
    )
    if not is_platform_host:
        return False
    # Any Nahla platform host is non-merchant for storefront SoT.
    # Explicit path markers keep intent clear in logs/tests.
    if any(marker in path for marker in _PLATFORM_PATH_MARKERS):
        return True
    return True


def is_trusted_merchant_http_url(url: str) -> bool:
    """Valid http(s) URL that is not a Nahla platform non-storefront page."""
    raw = _normalise_http_url(url)
    if not raw:
        return False
    if is_platform_non_merchant_url(raw):
        return False
    return True


def truncate_wa_cta_label(label: str, *, fallback: str = CTA_LABEL_STORE) -> str:
    """Ensure WhatsApp CTA display_text ≤ 20 characters."""
    text = str(label or "").strip() or fallback
    if len(text) <= WA_CTA_LABEL_MAX:
        return text
    trimmed = text[:WA_CTA_LABEL_MAX].rstrip()
    return trimmed or fallback[:WA_CTA_LABEL_MAX]


def extract_product_url_from_focus(focus: Optional[Dict[str, Any]]) -> str:
    if not isinstance(focus, dict) or not focus:
        return ""
    raw = (
        focus.get("product_url")
        or focus.get("url")
        or (focus.get("extra_metadata") or {}).get("product_url")
        or (focus.get("extra_metadata") or {}).get("url")
        or ""
    )
    return _normalise_http_url(str(raw or "").strip())


def product_focus_identity(focus: Optional[Dict[str, Any]]) -> str:
    if not isinstance(focus, dict) or not focus:
        return ""
    for key in ("id", "product_id", "external_id"):
        val = focus.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def load_product_focus_from_brain_state(brain_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    bs = brain_state if isinstance(brain_state, dict) else {}
    focus = bs.get("current_product_focus")
    return dict(focus) if isinstance(focus, dict) else {}


def lookup_catalog_product_url(
    db: Any,
    tenant_id: int,
    *,
    product_id: str = "",
    external_id: str = "",
) -> str:
    """Read product_url from tenant-scoped catalog Product.extra_metadata."""
    if db is None or not tenant_id:
        return ""
    try:
        from models import Product  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return ""

    row = None
    try:
        tid = int(tenant_id)
        if product_id and str(product_id).isdigit():
            row = (
                db.query(Product)
                .filter(
                    Product.tenant_id == tid,
                    Product.id == int(product_id),
                )
                .first()
            )
        if row is None and external_id:
            row = (
                db.query(Product)
                .filter(
                    Product.tenant_id == tid,
                    Product.external_id == str(external_id),
                )
                .first()
            )
        if row is None and product_id and not str(product_id).isdigit():
            row = (
                db.query(Product)
                .filter(
                    Product.tenant_id == tid,
                    Product.external_id == str(product_id),
                )
                .first()
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "storefront_product_url.catalog_lookup_failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return ""

    if row is None:
        return ""
    meta = getattr(row, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    return _normalise_http_url(
        str(meta.get("product_url") or meta.get("url") or "").strip()
    )


def allow_store_homepage_fallback_enabled(
    db: Any = None,
    tenant_id: int = 0,
) -> bool:
    """Commerce Completion Policy default: false. Opt-in via store_settings only."""
    if db is None or not tenant_id:
        return False
    try:
        from core.tenant import DEFAULT_STORE, get_or_create_settings, merge_defaults  # noqa: PLC0415

        settings = get_or_create_settings(db, int(tenant_id))
        store_cfg = merge_defaults(settings.store_settings, DEFAULT_STORE)
        completion = store_cfg.get("commerce_completion_policy") or {}
        if not isinstance(completion, dict):
            completion = {}
        return bool(completion.get("allow_store_homepage_fallback", False))
    except Exception:  # noqa: BLE001
        return False


def resolve_storefront_completion_link(
    db: Any,
    *,
    tenant_id: int,
    brain_state: Optional[Dict[str, Any]] = None,
    store_url: str = "",
    store_url_source: str = "",
    allow_store_homepage_fallback: Optional[bool] = None,
) -> StorefrontLinkResolution:
    """
    Resolve the URL for storefront completion delivery.

    Priority when Product Focus exists:
      1) trusted catalog/focus product_url (PDP)
      2) fail closed unless allow_store_homepage_fallback + trusted store_url
    Without Product Focus:
      trusted merchant store_url only (never platform register).
    """
    tid = int(tenant_id or 0)
    focus = load_product_focus_from_brain_state(brain_state)
    has_focus = bool(focus)
    pid = product_focus_identity(focus)

    if allow_store_homepage_fallback is None:
        allow_home = allow_store_homepage_fallback_enabled(db, tid)
    else:
        allow_home = bool(allow_store_homepage_fallback)

    trusted_store = ""
    if is_trusted_merchant_http_url(store_url):
        trusted_store = _normalise_http_url(store_url)
    elif store_url and is_platform_non_merchant_url(store_url):
        logger.info(
            "[STOREFRONT_URL] rejected_platform_store_url tenant=%s source=%s url_len=%d",
            tid,
            store_url_source or "caps.store_url",
            len(str(store_url)),
        )

    if has_focus:
        # 1) Focus dict
        focus_url = extract_product_url_from_focus(focus)
        if focus_url and is_trusted_merchant_http_url(focus_url):
            resolution = StorefrontLinkResolution(
                found=True,
                url=focus_url,
                source="product_focus.product_url",
                reason="trusted_product_focus_url",
                cta_label=truncate_wa_cta_label(CTA_LABEL_PRODUCT),
                has_product_focus=True,
                product_id=pid,
            )
            logger.info(
                "[STOREFRONT_URL] tenant=%s %s",
                tid,
                resolution.to_log_dict(),
            )
            return resolution
        if focus_url and is_platform_non_merchant_url(focus_url):
            logger.info(
                "[STOREFRONT_URL] rejected_platform_product_url tenant=%s url_len=%d",
                tid,
                len(focus_url),
            )

        # 2) Catalog projection by id / external_id
        catalog_url = lookup_catalog_product_url(
            db,
            tid,
            product_id=str(focus.get("id") or focus.get("product_id") or ""),
            external_id=str(focus.get("external_id") or ""),
        )
        if catalog_url and is_trusted_merchant_http_url(catalog_url):
            resolution = StorefrontLinkResolution(
                found=True,
                url=catalog_url,
                source="catalog.product_url",
                reason="trusted_catalog_product_url",
                cta_label=truncate_wa_cta_label(CTA_LABEL_PRODUCT),
                has_product_focus=True,
                product_id=pid,
            )
            logger.info(
                "[STOREFRONT_URL] tenant=%s %s",
                tid,
                resolution.to_log_dict(),
            )
            return resolution

        # 3) Fail closed (optional explicit homepage fallback)
        if allow_home and trusted_store:
            resolution = StorefrontLinkResolution(
                found=True,
                url=trusted_store,
                source="store_url_homepage_fallback",
                reason="allow_store_homepage_fallback",
                cta_label=truncate_wa_cta_label(CTA_LABEL_STORE),
                has_product_focus=True,
                product_id=pid,
            )
            logger.info(
                "[STOREFRONT_URL] tenant=%s %s",
                tid,
                resolution.to_log_dict(),
            )
            return resolution

        resolution = StorefrontLinkResolution(
            found=False,
            url="",
            source="none",
            reason="product_focus_missing_product_url",
            cta_label=truncate_wa_cta_label(CTA_LABEL_PRODUCT),
            has_product_focus=True,
            product_id=pid,
        )
        logger.info(
            "[STOREFRONT_URL] tenant=%s %s",
            tid,
            resolution.to_log_dict(),
        )
        return resolution

    # No Product Focus — store-level link only when trusted merchant store URL exists.
    if trusted_store:
        resolution = StorefrontLinkResolution(
            found=True,
            url=trusted_store,
            source=store_url_source or "store_url",
            reason="no_product_focus_store_url",
            cta_label=truncate_wa_cta_label(CTA_LABEL_STORE),
            has_product_focus=False,
        )
        logger.info(
            "[STOREFRONT_URL] tenant=%s %s",
            tid,
            resolution.to_log_dict(),
        )
        return resolution

    resolution = StorefrontLinkResolution(
        found=False,
        url="",
        source="none",
        reason="no_product_focus_no_trusted_store_url",
        cta_label=truncate_wa_cta_label(CTA_LABEL_STORE),
        has_product_focus=False,
    )
    logger.info(
        "[STOREFRONT_URL] tenant=%s %s",
        tid,
        resolution.to_log_dict(),
    )
    return resolution


__all__ = [
    "CTA_LABEL_PRODUCT",
    "CTA_LABEL_STORE",
    "StorefrontLinkResolution",
    "WA_CTA_LABEL_MAX",
    "allow_store_homepage_fallback_enabled",
    "extract_product_url_from_focus",
    "is_platform_non_merchant_url",
    "is_trusted_merchant_http_url",
    "load_product_focus_from_brain_state",
    "lookup_catalog_product_url",
    "product_focus_identity",
    "resolve_storefront_completion_link",
    "truncate_wa_cta_label",
]
