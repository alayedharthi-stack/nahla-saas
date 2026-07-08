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
    classify_meta_graph_error,
    _select_graph_token,
)

logger = logging.getLogger("nahla.meta_catalog_linking")

REQUEST_TIMEOUT: float = 45.0
_GRAPH_FIELDS = "id,name"


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
    }


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

    linked_catalogs, http_status, graph_err = _fetch_waba_product_catalogs(waba_id, token)
    if graph_err is not None:
        classified = classify_meta_graph_error(
            graph_err,
            http_status=http_status,
            token_source=token_source,
            stage="waba_product_catalogs",
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
        }

    linked_catalog_ids = [c["id"] for c in linked_catalogs]
    expected_linked = expected_catalog_id in linked_catalog_ids
    any_linked = bool(linked_catalog_ids)

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
    }


__all__ = ["get_waba_catalog_link_status"]
