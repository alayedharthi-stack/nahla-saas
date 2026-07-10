"""
services/meta_catalog_linking.py
────────────────────────────────
Read-only Meta Graph check: is the tenant's configured catalog linked
to their WhatsApp Business Account (WABA)?

GET /{WABA_ID}/product_catalogs only — no POST, no DB writes.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from core.config import META_GRAPH_API_VERSION
from services.meta_catalog_import import (
    _TOKEN_SOURCE_NONE,
    _select_graph_token,
)

logger = logging.getLogger("nahla.meta_catalog_linking")

REQUEST_TIMEOUT: float = 45.0
_GRAPH_FIELDS = "id,name"

WABA_ERROR_NOT_FOUND = "waba_not_found"
WABA_ERROR_INACCESSIBLE = "waba_inaccessible"

LINK_STATUS_LINKED = "linked"
LINK_STATUS_MISMATCH = "mismatch"
LINK_STATUS_NOT_LINKED = "not_linked"
LINK_STATUS_UNKNOWN = "unknown"


def _missing_payload(
    *,
    missing: List[str],
    error: str,
    waba_id: Optional[str] = None,
    expected_catalog_id: Optional[str] = None,
    token_source: str = _TOKEN_SOURCE_NONE,
) -> Dict[str, Any]:
    return {
        "ok": False,
        "connected": False,
        "waba_id": waba_id,
        "expected_catalog_id": expected_catalog_id,
        "linked_catalogs": [],
        "linked_catalog_ids": [],
        "expected_catalog_linked": False,
        "token_source": token_source,
        "http_status": None,
        "missing": missing,
        "error": error,
        "link_status": LINK_STATUS_UNKNOWN,
        "catalog_exists": None,
    }


def _classify_waba_product_catalogs_error(
    graph_err: Dict[str, Any],
    *,
    http_status: int,
) -> Dict[str, Any]:
    """Classify Graph errors from GET /{waba_id}/product_catalogs.

  Never maps to ``catalog_not_found`` — that code is reserved for a
  missing Commerce catalog object, not an unreadable WABA ID.
    """
    msg_lower = str(graph_err.get("meta_message") or "").lower()
    meta_code = graph_err.get("meta_code")
    permission_hint = (
        "missing permissions" in msg_lower
        or http_status in (401, 403)
        or meta_code in (10, 200, 294)
    )
    not_exist_hint = (
        "does not exist" in msg_lower
        or "cannot be loaded" in msg_lower
        or http_status == 404
    )
    if not_exist_hint and not permission_hint:
        code = WABA_ERROR_NOT_FOUND
    else:
        code = WABA_ERROR_INACCESSIBLE
    return {
        "result_code": code,
        "permission_category": code,
        "meta_code": meta_code,
        "meta_type": graph_err.get("meta_type"),
    }


def _probe_catalog_exists(
    catalog_id: str,
    token: str,
    *,
    client: httpx.Client,
) -> Optional[bool]:
    """Lightweight read-only check that the configured catalog object exists."""
    url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{catalog_id}"
    try:
        resp = client.get(url, params={"fields": "id", "access_token": token})
    except Exception:  # noqa: BLE001
        return None
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    return None


def _fetch_waba_product_catalogs(
    waba_id: str,
    token: str,
    *,
    client: Optional[httpx.Client] = None,
) -> tuple[List[Dict[str, Any]], int, Optional[Dict[str, Any]]]:
    """GET /{waba_id}/product_catalogs — returns (catalogs, http_status, error_body)."""
    url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{waba_id}/product_catalogs"
    params = {"fields": _GRAPH_FIELDS, "access_token": token}
    owns_client = client is None
    http = client or httpx.Client(timeout=REQUEST_TIMEOUT)
    try:
        resp = http.get(url, params=params)
        status = resp.status_code
        try:
            body = resp.json()
        except Exception:
            body = {}
        if status >= 400 or "error" in body:
            err = body.get("error") if isinstance(body, dict) else {}
            return [], status, {
                "meta_code": (err or {}).get("code"),
                "meta_subcode": (err or {}).get("error_subcode"),
                "meta_type": (err or {}).get("type"),
                "meta_message": (err or {}).get("message"),
                "status": status,
            }
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            return [], status, {
                "meta_message": "unexpected Graph response shape",
                "status": status,
            }
        catalogs: List[Dict[str, Any]] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            cid = str(row.get("id") or "").strip()
            if not cid:
                continue
            catalogs.append({
                "id": cid,
                "name": str(row.get("name") or "").strip() or None,
            })
        return catalogs, status, None
    finally:
        if owns_client:
            http.close()


def get_waba_catalog_link_status(db: Any, tenant_id: int) -> Dict[str, Any]:
    """Read-only WABA ↔ catalog link status for *tenant_id*."""
    from models import WhatsAppConnection  # noqa: PLC0415

    conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == int(tenant_id))
        .first()
    )
    if conn is None:
        return _missing_payload(missing=["connection"], error="connection_not_found")

    waba_id = str(getattr(conn, "whatsapp_business_account_id", "") or "").strip()
    expected_catalog_id = str(getattr(conn, "meta_catalog_id", "") or "").strip()
    token_pick = _select_graph_token(conn) or {}
    token_source = str(token_pick.get("token_source") or _TOKEN_SOURCE_NONE)
    token = str(token_pick.get("token") or "").strip()

    missing: List[str] = []
    if not waba_id:
        missing.append("waba_id")
    if not expected_catalog_id:
        missing.append("meta_catalog_id")
    if not token:
        missing.append("graph_token")

    if missing:
        primary = (
            "missing_waba_id" if "waba_id" in missing
            else "missing_catalog_id" if "meta_catalog_id" in missing
            else "missing_graph_token"
        )
        return _missing_payload(
            missing=missing,
            error=primary,
            waba_id=waba_id or None,
            expected_catalog_id=expected_catalog_id or None,
            token_source=token_source,
        )

    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        linked_catalogs, http_status, graph_err = _fetch_waba_product_catalogs(
            waba_id, token, client=client,
        )
        if graph_err is not None:
            classified = _classify_waba_product_catalogs_error(
                graph_err,
                http_status=http_status,
            )
            catalog_exists = (
                _probe_catalog_exists(expected_catalog_id, token, client=client)
                if expected_catalog_id
                else None
            )
            return {
                "ok": False,
                "connected": False,
                "waba_id": waba_id,
                "expected_catalog_id": expected_catalog_id,
                "linked_catalogs": [],
                "linked_catalog_ids": [],
                "expected_catalog_linked": False,
                "token_source": token_source,
                "http_status": http_status,
                "missing": [],
                "error": classified.get("result_code"),
                "error_code": classified.get("meta_code"),
                "error_type": classified.get("meta_type"),
                "error_message": (graph_err.get("meta_message") or "")[:240] or None,
                "error_category": classified.get("permission_category"),
                "link_status": LINK_STATUS_UNKNOWN,
                "catalog_exists": catalog_exists,
            }

    linked_catalog_ids = [c["id"] for c in linked_catalogs]
    expected_linked = expected_catalog_id in linked_catalog_ids
    any_linked = bool(linked_catalog_ids)
    if expected_linked:
        link_status = LINK_STATUS_LINKED
    elif any_linked:
        link_status = LINK_STATUS_MISMATCH
    else:
        link_status = LINK_STATUS_NOT_LINKED

    return {
        "ok": True,
        "connected": any_linked,
        "waba_id": waba_id,
        "expected_catalog_id": expected_catalog_id,
        "linked_catalogs": linked_catalogs,
        "linked_catalog_ids": linked_catalog_ids,
        "expected_catalog_linked": expected_linked,
        "token_source": token_source,
        "http_status": http_status,
        "missing": [],
        "error": None,
        "link_status": link_status,
        "catalog_exists": True if expected_linked else None,
    }


__all__ = [
    "get_waba_catalog_link_status",
    "WABA_ERROR_INACCESSIBLE",
    "WABA_ERROR_NOT_FOUND",
]
