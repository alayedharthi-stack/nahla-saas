"""
Shared paginated-sync helpers.

Platform-specific sync modules (salla/sync/*.py, zid/sync/*.py) call
these helpers instead of duplicating the pagination loop and SyncLog
writing logic.
"""

import sys
import os
from typing import Any, Callable, Dict, List, Optional

import httpx

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from database.models import SyncLog
from database.session import SessionLocal


def write_sync_log(
    tenant_id:     int,
    resource_type: str,
    external_id:   Optional[str],
    status:        str,
    message:       str = "",
) -> None:
    """Append a SyncLog entry for any sync or webhook event."""
    db = SessionLocal()
    try:
        log = SyncLog(
            tenant_id     = tenant_id,
            resource_type = resource_type,
            external_id   = external_id,
            status        = status,
            message       = message,
        )
        db.add(log)
        db.commit()
    finally:
        db.close()


async def paginated_fetch(
    url:          str,
    headers:      Dict[str, str],
    page_param:   str = "page",
    size_param:   str = "per_page",
    page_size:    int = 50,
    items_key:    str = "data",
    total_pages_path: List[str] = ["pagination", "total_pages"],
) -> List[Dict[str, Any]]:
    """
    Generic async paginated GET fetcher.

    Iterates all pages of a REST endpoint and returns a flat list of items.

    Args:
        url:               Full endpoint URL (no query string needed).
        headers:           HTTP headers (Authorization, Accept, …).
        page_param:        Query param name for the page number (default 'page').
        size_param:        Query param name for page size (default 'per_page').
        page_size:         How many items per page to request.
        items_key:         Key in the JSON response that holds the list of items.
        total_pages_path:  List of keys to drill into to find total_pages.
                           E.g. ['pagination', 'total_pages'] → body['pagination']['total_pages']
                           or   ['meta', 'last_page'] → body['meta']['last_page']

    Returns a flat list of all item dicts across all pages.
    Raises httpx.HTTPError on non-2xx response.
    """
    all_items: List[Dict[str, Any]] = []
    page = 1

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(
                url,
                headers=headers,
                params={page_param: page, size_param: page_size},
            )
            resp.raise_for_status()
            body = resp.json()

            # Extract items list (may be nested or at root)
            items = body
            for key in [items_key]:
                if isinstance(items, dict):
                    items = items.get(key, items)

            if isinstance(items, list):
                all_items.extend(items)
            elif isinstance(items, dict):
                # Some APIs wrap data: {"data": [...]}
                inner = items.get("data", [])
                all_items.extend(inner)
                items = inner  # for empty-page check below

            if not items:
                break

            # Resolve total_pages via the configured path
            total_pages = body
            for key in total_pages_path:
                if isinstance(total_pages, dict):
                    total_pages = total_pages.get(key, 1)
                else:
                    total_pages = 1
                    break

            if not isinstance(total_pages, int):
                total_pages = 1

            if page >= total_pages:
                break
            page += 1

    return all_items
