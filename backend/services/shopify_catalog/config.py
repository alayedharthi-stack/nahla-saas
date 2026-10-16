"""
Flag and fixed limits of the dormant Shopify catalog import.

Everything defaults to off. The import is available only when the
connection foundation is available (``services.shopify_connection.config``)
**and** ``NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED`` is truthy. Off: every import
route answers 404 and nothing runs.

The Admin API version is pinned here, independently of the foundation's
``SHOPIFY_ADMIN_API_VERSION`` (whose default and behaviour are unchanged):
every catalog request names ``2026-10`` and every response must say it was
served by ``2026-10`` — a version fall-forward fails the run.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional

IMPORT_ENABLED_ENV = "NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED"

# Shop bound of the import. A run may start only for a shop listed here with
# a future deadline: ``{"<shop>.myshopify.com": {"expires_at": "YYYY-MM-DDTHH:MM:SSZ"}}``.
# While the install-first pilot switch is on the bound is mandatory (absent
# means no shop); otherwise an absent bound leaves the import unrestricted
# (unchanged behaviour; the import flag itself is off by default). Present
# but malformed, or any deadline beyond ``MAX_IMPORT_SHOP_VALIDITY``, admits
# no shop at all. The release gate pins the pilot to exactly one shop.
IMPORT_SHOPS_ENV = "NAHLA_SHOPIFY_CATALOG_IMPORT_SHOPS"
INSTALL_FIRST_ENV = "NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED"
MAX_IMPORT_SHOPS = 5
MAX_IMPORT_SHOP_VALIDITY = timedelta(days=7)
_DEADLINE_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

CATALOG_API_VERSION = "2026-10"

# Scopes. ``read_products`` is the only scope the foundation requests or
# accepts today. Per-location inventory needs ``read_inventory`` as well
# (InventoryLevel requires it; Location is readable with it). Not requested
# here — a later, approved reauthorization slice must add it.
SCOPE_PRODUCTS = "read_products"
SCOPE_INVENTORY = "read_inventory"

# Shopify limits pagination of a connection to 25,000 objects and counts are
# exact only up to 25,000. Larger catalogs are refused (bulk follow-up).
PAGINATION_CAP = 25_000
MAX_VARIANTS_PER_PRODUCT = 2_048
MAX_VARIANTS_TOTAL = 200_000
MAX_MEDIA_PER_PRODUCT = 250
MAX_MEDIA_PER_VARIANT = 250
MAX_LOCATIONS = 5_000
MAX_LEVELS_PER_ITEM = 5_000
MAX_LEVELS_TOTAL = 1_000_000
MAX_OPTIONS = 3
MAX_OPTION_VALUES = 2_048

# Page sizes chosen so every requested query cost stays well below the
# single-query maximum of 1,000 points.
PRODUCTS_PAGE = 5
NESTED_VARIANTS_PAGE = 16
NESTED_PRODUCT_MEDIA_PAGE = 4
NESTED_VARIANT_MEDIA_PAGE = 2
FOLLOWUP_VARIANTS_PAGE = 50
FOLLOWUP_MEDIA_PAGE = 50
INVENTORY_ITEMS_PER_QUERY = 20
NESTED_LEVELS_PAGE = 10
FOLLOWUP_LEVELS_PAGE = 100
LOCATIONS_PAGE = 100
CONFIRM_NODES_PER_QUERY = 100

# Bounded waiting and retries.
REQUEST_TIMEOUT_SECONDS = 30.0
# One whole attempt (connect, send, complete streamed body); httpx's read
# timeout alone restarts on every chunk.
REQUEST_TOTAL_SECONDS = 60.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_HTTP_RETRIES = 5
MAX_THROTTLE_RETRIES = 8
MAX_WAIT_SECONDS = 30.0
MAX_CREDENTIAL_ROTATIONS = 5
RUN_DEADLINE_SECONDS = 6 * 3600
LEASE_SECONDS = 120
COUNT_RECHECK_DELAY_SECONDS = 2.0


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return env if env is not None else os.environ


def import_flag_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    return str(_env(env).get(IMPORT_ENABLED_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def import_routes_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Both default-off flags: the connection foundation's and the import's."""
    from services.shopify_connection.config import flag_enabled  # noqa: PLC0415

    e = _env(env)
    return flag_enabled(e) and import_flag_enabled(e)


def _unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate_import_shop")
        out[key] = value
    return out


def import_shop_allowed(shop: str, env: Optional[Mapping[str, str]] = None,
                        now: Optional[datetime] = None) -> bool:
    """Whether an import run may start for *shop* (canonical domain). Fails closed."""
    from services.shopify_connection.shop_domain import canonical_shop_domain  # noqa: PLC0415

    e = _env(env)
    current = now or datetime.now(timezone.utc)
    raw = e.get(IMPORT_SHOPS_ENV)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        pilot = str(e.get(INSTALL_FIRST_ENV) or "").strip().lower() in ("1", "true", "yes", "on")
        return not pilot
    if not isinstance(raw, str) or len(raw) > 4096 or canonical_shop_domain(shop) != shop:
        return False
    try:
        entries = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(entries, dict) or not 0 < len(entries) <= MAX_IMPORT_SHOPS:
            return False
        deadlines = {}
        for domain, entry in entries.items():
            if (canonical_shop_domain(domain) != domain or not isinstance(entry, dict)
                    or set(entry) != {"expires_at"} or not isinstance(entry["expires_at"], str)
                    or not _DEADLINE_RE.fullmatch(entry["expires_at"])):
                return False
            deadline = datetime.fromisoformat(entry["expires_at"][:-1]).replace(tzinfo=timezone.utc)
            if deadline > current + MAX_IMPORT_SHOP_VALIDITY:
                return False
            deadlines[domain] = deadline
        return shop in deadlines and current < deadlines[shop]
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        return False
