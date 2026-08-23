"""
services/meta_catalog_access.py
──────────────────────────────
Catalog-capable Graph token selection.

``_select_graph_token`` prefers the merchant WhatsApp OAuth token whenever
it looks like a Graph token. That is correct for WABA edges, but wrong
for Commerce Catalog objects when:

  * the reusable catalog lives on the platform Business Portfolio, and
  * the merchant WhatsApp token has no ``catalog_management`` / catalog
    visibility (common after Cloud API reconnect).

This module probes GET /{catalog_id} and returns the first token that can
actually read the catalog. Failures are explicit — never silent success.
Tokens are never logged.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from core.config import META_GRAPH_API_VERSION, WA_TOKEN
from services.meta_catalog_import import (
    _TOKEN_SOURCE_MERCHANT_OAUTH,
    _TOKEN_SOURCE_NONE,
    _TOKEN_SOURCE_PLATFORM_SYSTEM,
    _select_graph_token,
)

logger = logging.getLogger("nahla.meta_catalog_access")

REQUEST_TIMEOUT: float = 45.0
ERROR_CATALOG_NOT_READABLE = "catalog_not_readable"
ERROR_CATALOG_ID_MISSING = "catalog_id_missing"
ERROR_NO_GRAPH_TOKEN = "missing_graph_token"


def _auth_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {(token or '').strip()}"}


def probe_catalog_readable(
    token: str,
    catalog_id: str,
    *,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """GET /{catalog_id}?fields=id,name,product_count,business — no writes."""
    catalog_id = str(catalog_id or "").strip()
    result: Dict[str, Any] = {
        "ok": False,
        "catalog_id": catalog_id or None,
        "http_status": None,
        "name": None,
        "product_count": None,
        "business_id": None,
        "error": None,
        "error_code": None,
        "error_subcode": None,
    }
    if not catalog_id:
        result["error"] = ERROR_CATALOG_ID_MISSING
        return result
    if not (token or "").strip():
        result["error"] = ERROR_NO_GRAPH_TOKEN
        return result

    url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{catalog_id}"
    params = {"fields": "id,name,product_count,business{id,name}"}
    headers = _auth_headers(token)

    def _run(http: httpx.Client) -> Dict[str, Any]:
        resp = http.get(url, params=params, headers=headers)
        result["http_status"] = resp.status_code
        try:
            body = resp.json() if resp.content else {}
        except Exception:
            body = {}
        err = body.get("error") if isinstance(body, dict) else None
        if resp.status_code >= 400 or err:
            result["error"] = "meta_http_error"
            if isinstance(err, dict):
                result["error_code"] = err.get("code")
                result["error_subcode"] = err.get("error_subcode")
            return result
        result["ok"] = True
        result["name"] = str(body.get("name") or "").strip() or None
        result["product_count"] = body.get("product_count")
        business = body.get("business") if isinstance(body.get("business"), dict) else {}
        result["business_id"] = str(business.get("id") or "").strip() or None
        return result

    try:
        if client is not None:
            return _run(client)
        with httpx.Client(timeout=REQUEST_TIMEOUT) as owned:
            return _run(owned)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        result["error"] = f"transport:{type(exc).__name__}"
        return result


def catalog_token_candidates(conn: Any) -> List[Dict[str, Any]]:
    """Ordered unique Graph tokens to try against a catalog object."""
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def _add(token: str, token_source: str) -> None:
        raw = str(token or "").strip()
        if not raw or raw in seen:
            return
        seen.add(raw)
        out.append({"token": raw, "token_source": token_source})

    pick = _select_graph_token(conn) or {}
    _add(str(pick.get("token") or ""), str(pick.get("token_source") or _TOKEN_SOURCE_NONE))
    _add(str(WA_TOKEN or "").strip(), _TOKEN_SOURCE_PLATFORM_SYSTEM)
    return out


def select_catalog_graph_token(
    conn: Any,
    catalog_id: str,
    *,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """Return a token that can GET the catalog, or a structured failure."""
    catalog_id = str(catalog_id or "").strip()
    probes: List[Dict[str, Any]] = []
    if not catalog_id:
        return {
            "token": None,
            "token_source": _TOKEN_SOURCE_NONE,
            "catalog_readable": False,
            "error": ERROR_CATALOG_ID_MISSING,
            "probes": probes,
            "catalog": None,
        }

    candidates = catalog_token_candidates(conn)
    if not candidates:
        return {
            "token": None,
            "token_source": _TOKEN_SOURCE_NONE,
            "catalog_readable": False,
            "error": ERROR_NO_GRAPH_TOKEN,
            "probes": probes,
            "catalog": None,
        }

    for cand in candidates:
        probe = probe_catalog_readable(cand["token"], catalog_id, client=client)
        probes.append({
            "token_source": cand["token_source"],
            "ok": bool(probe.get("ok")),
            "http_status": probe.get("http_status"),
            "error": probe.get("error"),
            "error_code": probe.get("error_code"),
            "error_subcode": probe.get("error_subcode"),
            "business_id": probe.get("business_id"),
            "product_count": probe.get("product_count"),
        })
        if probe.get("ok"):
            logger.info(
                "[META_CATALOG_ACCESS] catalog_readable catalog=%s token_source=%s product_count=%s",
                catalog_id,
                cand["token_source"],
                probe.get("product_count"),
            )
            return {
                "token": cand["token"],
                "token_source": cand["token_source"],
                "catalog_readable": True,
                "error": None,
                "probes": probes,
                "catalog": {
                    "id": catalog_id,
                    "name": probe.get("name"),
                    "product_count": probe.get("product_count"),
                    "business_id": probe.get("business_id"),
                },
            }

    logger.warning(
        "[META_CATALOG_ACCESS] catalog_not_readable catalog=%s probes=%s",
        catalog_id,
        [
            {
                "token_source": p.get("token_source"),
                "http_status": p.get("http_status"),
                "error_code": p.get("error_code"),
            }
            for p in probes
        ],
    )
    return {
        "token": None,
        "token_source": _TOKEN_SOURCE_NONE,
        "catalog_readable": False,
        "error": ERROR_CATALOG_NOT_READABLE,
        "probes": probes,
        "catalog": None,
    }


__all__ = [
    "ERROR_CATALOG_ID_MISSING",
    "ERROR_CATALOG_NOT_READABLE",
    "ERROR_NO_GRAPH_TOKEN",
    "catalog_token_candidates",
    "probe_catalog_readable",
    "select_catalog_graph_token",
]
