"""
services/meta_catalog_linking.py
────────────────────────────────
WABA ↔ Meta Commerce Catalog association.

Read path (unchanged):
  GET /{WABA_ID}/product_catalogs — status only, no writes.

Write path (opt-in, confirm=True):
  POST /{catalog_id}/agencies — share a reusable catalog with the
  tenant WABA owner Business (ownership stays on the catalog BM).
  POST /{waba_id}/product_catalogs — bind the current WABA to an
  existing catalog. Never creates a catalog. Never deletes one.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from core.config import META_GRAPH_API_VERSION
from services.meta_catalog_import import (
    GRAPH_RESULT_META_HTTP_ERROR,
    GRAPH_RESULT_TOKEN_INVALID,
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
        "expected_catalog_linked": None,
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
    meta_type = str(graph_err.get("meta_type") or "")
    base = {
        "meta_code": meta_code,
        "meta_type": graph_err.get("meta_type"),
    }

    if meta_code in (190, 102) or (
        meta_type == "OAuthException" and "invalid" in msg_lower and "token" in msg_lower
    ):
        return {
            **base,
            "result_code": GRAPH_RESULT_TOKEN_INVALID,
            "permission_category": "invalid_token",
        }

    if meta_code in (4, 17, 32) or http_status >= 500:
        return {
            **base,
            "result_code": GRAPH_RESULT_META_HTTP_ERROR,
            "permission_category": "meta_http_error",
        }

    if "unexpected graph response shape" in msg_lower:
        return {
            **base,
            "result_code": GRAPH_RESULT_META_HTTP_ERROR,
            "permission_category": "meta_http_error",
        }

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
    elif permission_hint:
        code = WABA_ERROR_INACCESSIBLE
    else:
        return {
            **base,
            "result_code": GRAPH_RESULT_META_HTTP_ERROR,
            "permission_category": "meta_http_error",
        }
    return {
        **base,
        "result_code": code,
        "permission_category": code,
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
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        logger.warning(
            "catalog_exists_probe_transport_error catalog_id=%s error=%s",
            catalog_id,
            type(exc).__name__,
        )
        return None
    except httpx.HTTPError as exc:
        logger.warning(
            "catalog_exists_probe_http_error catalog_id=%s error=%s",
            catalog_id,
            type(exc).__name__,
        )
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
                "expected_catalog_linked": None,
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

    extra = getattr(conn, "extra_metadata", None) or {}
    ensure_meta = extra.get("meta_catalog_ensure") if isinstance(extra, dict) else {}
    onboarding_error = None
    legacy = False
    if isinstance(ensure_meta, dict):
        onboarding_error = str(ensure_meta.get("error") or "").strip() or None
        legacy = bool(ensure_meta.get("legacy_repair"))

    payload = {
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
        "legacy_repair": legacy,
        "onboarding_error": onboarding_error,
    }
    if not expected_linked and onboarding_error == "catalog_business_mismatch":
        payload["error"] = "catalog_business_mismatch"
        payload["legacy_repair"] = True
    return payload


def _auth_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {(token or '').strip()}"}


def _graph_json(
    method: str,
    path: str,
    token: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{path}"
    headers = _auth_headers(token)

    def _run(http: httpx.Client) -> Dict[str, Any]:
        resp = http.request(
            method,
            url,
            params=params,
            data=None if json_body is not None else data,
            json=json_body,
            headers=headers,
        )
        try:
            body = resp.json() if resp.content else {}
        except Exception:
            body = {}
        err = body.get("error") if isinstance(body, dict) else None
        return {
            "ok": resp.status_code < 400 and not err,
            "http_status": resp.status_code,
            "body": body if isinstance(body, dict) else {},
            "error": {
                "code": (err or {}).get("code"),
                "subcode": (err or {}).get("error_subcode"),
                "type": (err or {}).get("type"),
                "message": str((err or {}).get("message") or "")[:240],
                "user_title": str((err or {}).get("error_user_title") or "")[:120] or None,
                "user_msg": str((err or {}).get("error_user_msg") or "")[:240] or None,
            } if err else None,
        }

    owns = client is None
    http = client or httpx.Client(timeout=REQUEST_TIMEOUT)
    try:
        return _run(http)
    finally:
        if owns:
            http.close()


def fetch_waba_owner_business_id(
    waba_id: str,
    token: str,
    *,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """GET /{waba_id}?fields=owner_business_info,on_behalf_of_business_info."""
    waba_id = str(waba_id or "").strip()
    out: Dict[str, Any] = {
        "ok": False,
        "waba_id": waba_id or None,
        "business_id": None,
        "business_name": None,
        "error": None,
    }
    if not waba_id or not (token or "").strip():
        out["error"] = "missing_waba_or_token"
        return out
    resp = _graph_json(
        "GET",
        waba_id,
        token,
        params={"fields": "id,name,owner_business_info,on_behalf_of_business_info"},
        client=client,
    )
    if not resp.get("ok"):
        out["error"] = "waba_owner_unreadable"
        out["http_status"] = resp.get("http_status")
        out["graph_error"] = resp.get("error")
        return out
    body = resp.get("body") or {}
    owner = body.get("owner_business_info") if isinstance(body.get("owner_business_info"), dict) else {}
    behalf = (
        body.get("on_behalf_of_business_info")
        if isinstance(body.get("on_behalf_of_business_info"), dict) else {}
    )
    business_id = str(owner.get("id") or behalf.get("id") or "").strip()
    out["ok"] = bool(business_id)
    out["business_id"] = business_id or None
    out["business_name"] = str(owner.get("name") or behalf.get("name") or "").strip() or None
    if not business_id:
        out["error"] = "waba_owner_business_missing"
    return out


def list_catalog_agency_business_ids(
    catalog_id: str,
    token: str,
    *,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """GET /{catalog_id}/agencies — which businesses already have catalog access."""
    catalog_id = str(catalog_id or "").strip()
    out: Dict[str, Any] = {
        "ok": False,
        "catalog_id": catalog_id or None,
        "business_ids": [],
        "error": None,
    }
    if not catalog_id or not (token or "").strip():
        out["error"] = "missing_catalog_or_token"
        return out
    resp = _graph_json(
        "GET",
        f"{catalog_id}/agencies",
        token,
        params={"fields": "id,name"},
        client=client,
    )
    if not resp.get("ok"):
        out["error"] = "agencies_unreadable"
        out["http_status"] = resp.get("http_status")
        out["graph_error"] = resp.get("error")
        return out
    rows = (resp.get("body") or {}).get("data") or []
    ids: List[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        bid = str(row.get("id") or "").strip()
        if bid:
            ids.append(bid)
    out["ok"] = True
    out["business_ids"] = ids
    return out


def share_catalog_with_business(
    catalog_id: str,
    business_id: str,
    token: str,
    *,
    confirm: bool = False,
    allowed_business_id: Optional[str] = None,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """Share a reusable catalog with a tenant WABA owner BM. Ownership stays put."""
    catalog_id = str(catalog_id or "").strip()
    business_id = str(business_id or "").strip()
    allowed = str(allowed_business_id or "").strip()
    if allowed and business_id and business_id != allowed:
        return {
            "ok": False,
            "action": "share",
            "dry_run": not confirm,
            "catalog_id": catalog_id,
            "business_id": business_id,
            "already_shared": False,
            "error": "business_id_not_waba_owner",
            "meta": None,
        }
    listed = list_catalog_agency_business_ids(catalog_id, token, client=client)
    already = business_id in (listed.get("business_ids") or [])
    result: Dict[str, Any] = {
        "ok": already,
        "action": "already_shared" if already else "share",
        "dry_run": not confirm,
        "catalog_id": catalog_id,
        "business_id": business_id,
        "already_shared": already,
        "error": None,
        "meta": None,
    }
    if not catalog_id or not business_id:
        result["error"] = "missing_catalog_or_business"
        result["ok"] = False
        return result
    if already:
        result["ok"] = True
        return result
    if not confirm:
        return result
    resp = _graph_json(
        "POST",
        f"{catalog_id}/agencies",
        token,
        json_body={"business": business_id, "permitted_tasks": ["MANAGE"]},
        client=client,
    )
    result["meta"] = {
        "http_status": resp.get("http_status"),
        "error": resp.get("error"),
    }
    result["ok"] = bool(resp.get("ok"))
    if not result["ok"]:
        result["error"] = "catalog_share_failed"
    return result


def link_waba_to_catalog(
    waba_id: str,
    catalog_id: str,
    token: str,
    *,
    confirm: bool = False,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """POST /{waba_id}/product_catalogs — bind current WABA to an existing catalog."""
    waba_id = str(waba_id or "").strip()
    catalog_id = str(catalog_id or "").strip()
    linked, http_status, graph_err = _fetch_waba_product_catalogs(
        waba_id, token, client=client,
    )
    linked_ids = [c["id"] for c in linked]
    already = catalog_id in linked_ids
    result: Dict[str, Any] = {
        "ok": already and graph_err is None,
        "action": "already_linked" if already else "link",
        "dry_run": not confirm,
        "waba_id": waba_id,
        "catalog_id": catalog_id,
        "already_linked": already,
        "linked_catalog_ids": linked_ids,
        "error": None,
        "meta": None,
    }
    if graph_err is not None and not already:
        result["error"] = "waba_catalogs_unreadable"
        result["meta"] = {"http_status": http_status, "error": graph_err}
        return result
    if already:
        result["ok"] = True
        result["link_status"] = LINK_STATUS_LINKED
        return result
    if not confirm:
        return result
    resp = _graph_json(
        "POST",
        f"{waba_id}/product_catalogs",
        token,
        data={"catalog_id": catalog_id},
        client=client,
    )
    result["meta"] = {
        "http_status": resp.get("http_status"),
        "error": resp.get("error"),
    }
    if not resp.get("ok"):
        graph_err = resp.get("error") or {}
        if int(graph_err.get("subcode") or 0) == 2388100:
            result["error"] = "catalog_manage_permission_required"
        else:
            result["error"] = "waba_catalog_link_failed"
        result["ok"] = False
        return result
    linked_after, _, err_after = _fetch_waba_product_catalogs(
        waba_id, token, client=client,
    )
    after_ids = [c["id"] for c in linked_after]
    result["linked_catalog_ids"] = after_ids
    result["already_linked"] = catalog_id in after_ids
    result["ok"] = catalog_id in after_ids and err_after is None
    result["link_status"] = LINK_STATUS_LINKED if result["ok"] else LINK_STATUS_NOT_LINKED
    if not result["ok"]:
        result["error"] = "waba_catalog_link_unverified"
    return result


__all__ = [
    "get_waba_catalog_link_status",
    "fetch_waba_owner_business_id",
    "list_catalog_agency_business_ids",
    "share_catalog_with_business",
    "link_waba_to_catalog",
    "WABA_ERROR_INACCESSIBLE",
    "WABA_ERROR_NOT_FOUND",
]
