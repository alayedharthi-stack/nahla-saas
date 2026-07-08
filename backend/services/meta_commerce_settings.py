"""
services/meta_commerce_settings.py
────────────────────────────────────
Read-only Meta Graph: WhatsApp commerce settings per phone number
(catalog visibility + cart) and WABA health signals.

GET /{phone_number_id}/whatsapp_commerce_settings only — no POST/PATCH, no DB writes.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

from core.config import META_GRAPH_API_VERSION
from services.meta_catalog_import import (
    _TOKEN_SOURCE_NONE,
    classify_meta_graph_error,
    _select_graph_token,
)
from services.meta_catalog_linking import _fetch_waba_product_catalogs

logger = logging.getLogger("nahla.meta_commerce_settings")

REQUEST_TIMEOUT: float = 45.0
_PHONE_PROFILE_FIELDS = "display_phone_number,verified_name"
_WABA_HEALTH_FIELDS = "business_verification_status,account_review_status"


def _base_payload(
    *,
    ok: bool,
    phone_number_id: Optional[str] = None,
    display_phone_number: Optional[str] = None,
    verified_name: Optional[str] = None,
    waba_id: Optional[str] = None,
    expected_catalog_id: Optional[str] = None,
    expected_catalog_linked: bool = False,
    token_source: str = _TOKEN_SOURCE_NONE,
    commerce_settings: Optional[Dict[str, Any]] = None,
    commerce_settings_found: bool = False,
    waba_health: Optional[Dict[str, Any]] = None,
    missing: Optional[List[str]] = None,
    http_status: Optional[int] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "ok": ok,
        "phone_number_id": phone_number_id,
        "display_phone_number": display_phone_number,
        "verified_name": verified_name,
        "waba_id": waba_id,
        "expected_catalog_id": expected_catalog_id,
        "expected_catalog_linked": expected_catalog_linked,
        "token_source": token_source,
        "commerce_settings": commerce_settings,
        "commerce_settings_found": commerce_settings_found,
        "waba_health": waba_health,
        "missing": missing or [],
        "http_status": http_status,
        "error": error,
    }


def _graph_get_json(
    path: str,
    token: str,
    *,
    params: Optional[Dict[str, str]] = None,
    client: httpx.Client,
) -> Tuple[Dict[str, Any], int, Optional[Dict[str, Any]]]:
    """GET graph.facebook.com/{path} — returns (body, http_status, error_body)."""
    url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{path}"
    query = {"access_token": token}
    if params:
        query.update(params)
    resp = client.get(url, params=query)
    status = resp.status_code
    try:
        body = resp.json()
    except Exception:
        body = {}
    if status >= 400 or (isinstance(body, dict) and "error" in body):
        err = body.get("error") if isinstance(body, dict) else {}
        return body if isinstance(body, dict) else {}, status, {
            "meta_code": (err or {}).get("code"),
            "meta_subcode": (err or {}).get("error_subcode"),
            "meta_type": (err or {}).get("type"),
            "meta_message": (err or {}).get("message"),
            "status": status,
        }
    return body if isinstance(body, dict) else {}, status, None


def _parse_commerce_settings(body: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], bool]:
    data = body.get("data")
    if not isinstance(data, list) or not data:
        return None, False
    row = data[0]
    if not isinstance(row, dict):
        return None, False
    settings_id = str(row.get("id") or "").strip() or None
    return {
        "id": settings_id,
        "is_catalog_visible": bool(row.get("is_catalog_visible")),
        "is_cart_enabled": bool(row.get("is_cart_enabled")),
    }, True


def _error_payload(
    *,
    graph_err: Dict[str, Any],
    http_status: int,
    stage: str,
    **fields: Any,
) -> Dict[str, Any]:
    token_source = str(fields.get("token_source") or _TOKEN_SOURCE_NONE)
    classified = classify_meta_graph_error(
        graph_err,
        http_status=http_status,
        token_source=token_source,
        stage=stage,
    )
    payload_fields = dict(fields)
    payload_fields.pop("token_source", None)
    base = _base_payload(
        ok=False,
        token_source=token_source,
        http_status=http_status,
        error=classified.get("result_code"),
        **payload_fields,
    )
    base["error_code"] = classified.get("meta_code")
    base["error_type"] = classified.get("meta_type")
    base["error_message"] = (graph_err.get("meta_message") or "")[:240] or None
    base["error_category"] = classified.get("permission_category")
    return base


def get_whatsapp_commerce_settings_status(db: Any, tenant_id: int) -> Dict[str, Any]:
    """Read-only commerce settings + WABA health for *tenant_id*."""
    from models import WhatsAppConnection  # noqa: PLC0415

    conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == int(tenant_id))
        .first()
    )
    if conn is None:
        return _base_payload(
            ok=False,
            missing=["connection"],
            error="connection_not_found",
        )

    phone_number_id = str(getattr(conn, "phone_number_id", "") or "").strip()
    waba_id = str(getattr(conn, "whatsapp_business_account_id", "") or "").strip()
    expected_catalog_id = str(getattr(conn, "meta_catalog_id", "") or "").strip()
    token_pick = _select_graph_token(conn) or {}
    token_source = str(token_pick.get("token_source") or _TOKEN_SOURCE_NONE)
    token = str(token_pick.get("token") or "").strip()

    common = {
        "phone_number_id": phone_number_id or None,
        "waba_id": waba_id or None,
        "expected_catalog_id": expected_catalog_id or None,
        "token_source": token_source,
    }

    missing: List[str] = []
    if not phone_number_id:
        missing.append("phone_number_id")
    if not waba_id:
        missing.append("waba_id")
    if not expected_catalog_id:
        missing.append("meta_catalog_id")
    if not token:
        missing.append("graph_token")

    if missing:
        primary = (
            "missing_phone_number_id" if "phone_number_id" in missing
            else "missing_waba_id" if "waba_id" in missing
            else "missing_catalog_id" if "meta_catalog_id" in missing
            else "missing_graph_token"
        )
        return _base_payload(
            ok=False,
            missing=missing,
            error=primary,
            **common,
        )

    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        commerce_body, commerce_status, commerce_err = _graph_get_json(
            f"{phone_number_id}/whatsapp_commerce_settings",
            token,
            client=client,
        )
        if commerce_err is not None:
            return _error_payload(
                graph_err=commerce_err,
                http_status=commerce_status,
                stage="whatsapp_commerce_settings",
                **common,
            )

        commerce_settings, commerce_found = _parse_commerce_settings(commerce_body)

        display_phone_number: Optional[str] = None
        verified_name: Optional[str] = None
        phone_body, _phone_status, phone_err = _graph_get_json(
            phone_number_id,
            token,
            params={"fields": _PHONE_PROFILE_FIELDS},
            client=client,
        )
        if phone_err is None:
            display_phone_number = str(phone_body.get("display_phone_number") or "").strip() or None
            verified_name = str(phone_body.get("verified_name") or "").strip() or None

        waba_health: Optional[Dict[str, Any]] = None
        waba_body, _waba_status, waba_err = _graph_get_json(
            waba_id,
            token,
            params={"fields": _WABA_HEALTH_FIELDS},
            client=client,
        )
        if waba_err is None:
            waba_health = {
                "business_verification_status": (
                    str(waba_body.get("business_verification_status") or "").strip() or None
                ),
                "account_review_status": (
                    str(waba_body.get("account_review_status") or "").strip() or None
                ),
            }

        linked_catalogs, link_status, link_err = _fetch_waba_product_catalogs(
            waba_id, token, client=client,
        )
        if link_err is not None:
            return _error_payload(
                graph_err=link_err,
                http_status=link_status,
                stage="waba_product_catalogs",
                display_phone_number=display_phone_number,
                verified_name=verified_name,
                waba_health=waba_health,
                commerce_settings=commerce_settings,
                commerce_settings_found=commerce_found,
                **common,
            )

        linked_ids = [c["id"] for c in linked_catalogs]
        expected_linked = expected_catalog_id in linked_ids

        return _base_payload(
            ok=True,
            display_phone_number=display_phone_number,
            verified_name=verified_name,
            expected_catalog_linked=expected_linked,
            commerce_settings=commerce_settings,
            commerce_settings_found=commerce_found,
            waba_health=waba_health,
            http_status=commerce_status,
            error=None,
            **common,
        )


__all__ = ["get_whatsapp_commerce_settings_status"]
