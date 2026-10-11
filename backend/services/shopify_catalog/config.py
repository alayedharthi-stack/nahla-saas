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

import os
from typing import Mapping, Optional

IMPORT_ENABLED_ENV = "NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED"

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
