"""Merchant-owned Meta catalog bootstrap for App Review filming.

Flag-gated. Test App / staging only. Never share-and-bind. Never create
on a blocked Business. Never unlink or delete Meta assets.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import httpx
from sqlalchemy.orm.attributes import flag_modified

from core.catalog import assign_canonical_retailer_id, canonical_retailer_id, is_meta_export_eligible
from core.catalog_review_harness import (
    ERROR_BLOCKLIST_UNCONFIGURED,
    ERROR_BUSINESS_NAME_MISMATCH,
    ERROR_HARNESS_DISABLED,
    ERROR_NAHLAH_BM_BLOCKED,
    ERROR_OWNERSHIP_MISMATCH,
    ERROR_REAUTH_REQUIRED,
    ERROR_WRONG_APP_ID,
    HARNESS_META_KEY,
    REQUIRED_SCOPES,
    blocked_business_ids,
    business_name_matches,
    evaluate_harness_gate,
    harness_state,
    is_blocked_business_id,
    is_catalog_review_harness_enabled,
    missing_required_scopes,
    strip_secrets,
    test_app_id,
    test_app_secret,
)
from core.config import META_GRAPH_API_VERSION
from services.meta_catalog_import import (
    _TOKEN_SOURCE_MERCHANT_OAUTH,
    _select_graph_token,
    sanitize_token_pick,
)
from services.meta_catalog_linking import (
    fetch_waba_owner_business_id,
    link_waba_to_catalog,
)
from services.meta_catalog_push import push_one_meta_catalog_item

logger = logging.getLogger("nahla.meta_catalog_review_harness")

REQUEST_TIMEOUT = 45.0
_GRAPH = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_connection(db: Any, tenant_id: int) -> Any:
    from models import WhatsAppConnection  # noqa: PLC0415

    return (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == int(tenant_id))
        .first()
    )


def _merchant_bisu_token(conn: Any) -> Dict[str, Any]:
    pick = _select_graph_token(conn) or {}
    source = str(pick.get("token_source") or "")
    token = str(pick.get("token") or "").strip()
    if source != _TOKEN_SOURCE_MERCHANT_OAUTH or not token:
        return {
            "ok": False,
            "error": ERROR_REAUTH_REQUIRED,
            "token_pick": sanitize_token_pick(pick),
        }
    return {"ok": True, "token": token, "token_pick": sanitize_token_pick(pick)}


def _graph(
    method: str,
    path: str,
    token: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    url = f"{_GRAPH}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}"}

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
        sanitized = None
        if err:
            sanitized = {
                "code": (err or {}).get("code"),
                "subcode": (err or {}).get("error_subcode"),
                "type": (err or {}).get("type"),
            }
        return {
            "ok": resp.status_code < 400 and not err,
            "http_status": resp.status_code,
            "body": body if isinstance(body, dict) else {},
            "error": sanitized,
        }

    owns = client is None
    http = client or httpx.Client(timeout=REQUEST_TIMEOUT)
    try:
        return _run(http)
    finally:
        if owns:
            http.close()


def inspect_merchant_token(
    token: str,
    *,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """GET /debug_token. Never logs the input token."""
    app_id = test_app_id()
    app_secret = test_app_secret()
    out: Dict[str, Any] = {
        "ok": False,
        "app_id": None,
        "scopes": [],
        "is_valid": False,
        "error": None,
    }
    if not token or not app_id or not app_secret:
        out["error"] = ERROR_REAUTH_REQUIRED
        return out
    app_token = f"{app_id}|{app_secret}"
    url = f"{_GRAPH}/debug_token"
    owns = client is None
    http = client or httpx.Client(timeout=REQUEST_TIMEOUT)
    try:
        resp = http.get(
            url,
            params={"input_token": token, "access_token": app_token},
        )
        try:
            body = resp.json() if resp.content else {}
        except Exception:
            body = {}
        data = body.get("data") if isinstance(body, dict) else {}
        if not isinstance(data, dict):
            data = {}
        scopes = [str(s).strip() for s in (data.get("scopes") or []) if str(s).strip()]
        out["app_id"] = str(data.get("app_id") or "").strip() or None
        out["scopes"] = scopes
        out["is_valid"] = bool(data.get("is_valid"))
        out["ok"] = bool(out["is_valid"] and out["app_id"])
        if not out["ok"]:
            out["error"] = ERROR_REAUTH_REQUIRED
        return out
    finally:
        if owns:
            http.close()


def _persist_state(conn: Any, payload: Dict[str, Any]) -> None:
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    safe = strip_secrets(payload)
    safe["at"] = _now_iso()
    safe["disconnect_preserves_catalog"] = True
    meta[HARNESS_META_KEY] = safe
    conn.extra_metadata = meta
    if getattr(conn, "_sa_instance_state", None) is not None:
        flag_modified(conn, "extra_metadata")


def stamp_disconnect_preserves_assets(conn: Any) -> None:
    """Disconnect Nahlah authorization only. Never delete/unlink Meta assets."""
    state = harness_state(conn)
    state["disconnected_at"] = _now_iso()
    state["disconnect_preserves_catalog"] = True
    state["disconnect_unlinked_waba"] = False
    state["disconnect_deleted_catalog"] = False
    state["disconnect_deleted_product"] = False
    _persist_state(conn, state)


def _eligible_native_products(db: Any, tenant_id: int) -> List[Any]:
    from models import Product  # noqa: PLC0415

    rows = (
        db.query(Product)
        .filter(Product.tenant_id == int(tenant_id))
        .order_by(Product.id)
        .all()
    )
    out: List[Any] = []
    for row in rows:
        if int(getattr(row, "tenant_id", 0) or 0) != int(tenant_id):
            continue
        if not is_meta_export_eligible(row):
            continue
        if getattr(row, "id", None) is None:
            continue
        out.append(row)
    return out


def list_owned_product_catalogs(
    business_id: str,
    token: str,
    *,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    business_id = str(business_id or "").strip()
    out: Dict[str, Any] = {
        "ok": False,
        "catalogs": [],
        "error": None,
    }
    if not business_id or not token:
        out["error"] = "missing_business_or_token"
        return out
    resp = _graph(
        "GET",
        f"{business_id}/owned_product_catalogs",
        token,
        params={"fields": "id,name,business"},
        client=client,
    )
    if not resp.get("ok"):
        out["error"] = "owned_catalogs_unreadable"
        return out
    catalogs: List[Dict[str, Any]] = []
    for row in (resp.get("body") or {}).get("data") or []:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("id") or "").strip()
        if not cid:
            continue
        business = row.get("business") if isinstance(row.get("business"), dict) else {}
        catalogs.append({
            "id": cid,
            "name": str(row.get("name") or "").strip() or None,
            "business_id": str(business.get("id") or "").strip() or None,
        })
    out["ok"] = True
    out["catalogs"] = catalogs
    return out


def fetch_catalog_owner_business_id(
    catalog_id: str,
    token: str,
    *,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    catalog_id = str(catalog_id or "").strip()
    out: Dict[str, Any] = {
        "ok": False,
        "catalog_id": catalog_id or None,
        "business_id": None,
        "business_name": None,
        "error": None,
    }
    if not catalog_id or not token:
        out["error"] = "missing_catalog_or_token"
        return out
    resp = _graph(
        "GET",
        catalog_id,
        token,
        params={"fields": "id,name,business"},
        client=client,
    )
    if not resp.get("ok"):
        out["error"] = "catalog_owner_unreadable"
        return out
    body = resp.get("body") or {}
    business = body.get("business") if isinstance(body.get("business"), dict) else {}
    out["business_id"] = str(business.get("id") or "").strip() or None
    out["business_name"] = str(business.get("name") or "").strip() or None
    out["ok"] = bool(out["business_id"])
    if not out["ok"]:
        out["error"] = "catalog_owner_missing"
    return out


def create_owned_product_catalog(
    business_id: str,
    name: str,
    token: str,
    *,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    business_id = str(business_id or "").strip()
    name = str(name or "").strip()
    out: Dict[str, Any] = {
        "ok": False,
        "catalog_id": None,
        "error": None,
    }
    if not business_id or not name or not token:
        out["error"] = "missing_create_fields"
        return out
    resp = _graph(
        "POST",
        f"{business_id}/owned_product_catalogs",
        token,
        data={"name": name},
        client=client,
    )
    if not resp.get("ok"):
        out["error"] = "catalog_create_failed"
        return out
    body = resp.get("body") or {}
    out["catalog_id"] = str(body.get("id") or "").strip() or None
    out["ok"] = bool(out["catalog_id"])
    if not out["ok"]:
        out["error"] = "catalog_create_missing_id"
    return out


def _resolve_trusted_catalog(
    conn: Any,
    owned: List[Dict[str, Any]],
    owner_bm: str,
) -> Dict[str, Any]:
    """Reuse only a harness-stamped catalog. Never select by generic name."""
    state = harness_state(conn)
    owned_by_id = {c["id"]: c for c in owned if c.get("id")}
    stored = str(state.get("catalog_id") or "").strip()
    if stored and stored in owned_by_id:
        row = owned_by_id[stored]
        row_owner = str(row.get("business_id") or owner_bm or "").strip()
        if row_owner and row_owner != owner_bm:
            return {"ok": False, "error": ERROR_OWNERSHIP_MISMATCH, "catalog_id": None}
        return {"ok": True, "catalog_id": stored, "reused": True, "error": None}

    intended = str(state.get("intended_catalog_name") or "").strip()
    if intended:
        matches = [c for c in owned if str(c.get("name") or "") == intended]
        if len(matches) > 1:
            return {"ok": False, "error": "duplicate_catalog_ambiguous", "catalog_id": None}
        if len(matches) == 1:
            row = matches[0]
            row_owner = str(row.get("business_id") or owner_bm or "").strip()
            if row_owner and row_owner != owner_bm:
                return {"ok": False, "error": ERROR_OWNERSHIP_MISMATCH, "catalog_id": None}
            return {
                "ok": True,
                "catalog_id": row["id"],
                "reused": True,
                "error": None,
            }
    return {"ok": True, "catalog_id": None, "reused": False, "error": None}


def prove_mutation_preconditions(
    *,
    tenant_id: int,
    token_app_id: Optional[str],
    scopes: List[str],
    business_id: Optional[str],
    business_name: Optional[str],
) -> Dict[str, Any]:
    gate = evaluate_harness_gate(tenant_id=tenant_id)
    if not gate.get("ok"):
        return gate

    proof = {
        "ok": False,
        "environment": gate.get("environment"),
        "app_id": test_app_id(),
        "tenant_id": int(tenant_id),
        "business_owner": str(business_id or "").strip() or None,
        "business_name": str(business_name or "").strip() or None,
        "error": None,
    }
    if str(token_app_id or "").strip() != test_app_id():
        proof["error"] = ERROR_WRONG_APP_ID
        return proof
    if missing_required_scopes(scopes):
        proof["error"] = ERROR_REAUTH_REQUIRED
        return proof
    if not blocked_business_ids():
        proof["error"] = ERROR_BLOCKLIST_UNCONFIGURED
        return proof
    if is_blocked_business_id(business_id):
        proof["error"] = ERROR_NAHLAH_BM_BLOCKED
        return proof
    if not business_name_matches(business_name):
        proof["error"] = ERROR_BUSINESS_NAME_MISMATCH
        return proof
    proof["ok"] = True
    return proof


def run_catalog_review_harness(
    db: Any,
    tenant_id: int,
    *,
    confirm: bool = True,
    client: Optional[httpx.Client] = None,
    debug_token_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "ok": False,
        "skipped": False,
        "tenant_id": int(tenant_id),
        "error": None,
        "ui_status": "setting_up",
        "created_catalog": False,
        "catalog_reused": False,
        "bind_verified": False,
        "retailer_id": None,
        "product_synced": False,
        "graph_writes": 0,
    }
    gate = evaluate_harness_gate(tenant_id=tenant_id)
    if not gate.get("ok"):
        result["error"] = gate.get("error") or ERROR_HARNESS_DISABLED
        result["skipped"] = result["error"] == ERROR_HARNESS_DISABLED
        return result

    conn = _load_connection(db, tenant_id)
    if conn is None:
        result["error"] = "connection_not_found"
        return result

    token_info = _merchant_bisu_token(conn)
    if not token_info.get("ok"):
        result["error"] = ERROR_REAUTH_REQUIRED
        _persist_state(conn, {**result, "ui_status": "reauth_required", "error_code": ERROR_REAUTH_REQUIRED})
        db.commit()
        return result
    token = str(token_info["token"])

    inspect_fn = debug_token_fn or inspect_merchant_token
    debug = inspect_fn(token, client=client) if debug_token_fn is None else inspect_fn(token)
    scopes = list(debug.get("scopes") or [])
    token_app_id = debug.get("app_id")

    waba_id = str(getattr(conn, "whatsapp_business_account_id", "") or "").strip()
    if not waba_id:
        result["error"] = ERROR_REAUTH_REQUIRED
        _persist_state(conn, {**result, "ui_status": "reauth_required", "error_code": ERROR_REAUTH_REQUIRED})
        db.commit()
        return result

    owner = fetch_waba_owner_business_id(waba_id, token, client=client)
    owner_bm = str(owner.get("business_id") or "").strip()
    owner_name = str(owner.get("business_name") or "").strip() or None

    proof = prove_mutation_preconditions(
        tenant_id=int(tenant_id),
        token_app_id=str(token_app_id or "") if token_app_id else None,
        scopes=scopes,
        business_id=owner_bm or None,
        business_name=owner_name,
    )
    result["proof"] = {k: v for k, v in proof.items() if k != "token"}
    if not proof.get("ok"):
        result["error"] = proof.get("error")
        ui = "reauth_required" if result["error"] == ERROR_REAUTH_REQUIRED else "blocked"
        _persist_state(conn, {
            **result,
            "ui_status": ui,
            "error_code": result["error"],
        })
        db.commit()
        return result

    owned_resp = list_owned_product_catalogs(owner_bm, token, client=client)
    if not owned_resp.get("ok"):
        result["error"] = "owned_catalogs_unreadable"
        _persist_state(conn, {**result, "ui_status": "blocked", "error_code": result["error"]})
        db.commit()
        return result

    trusted = _resolve_trusted_catalog(conn, owned_resp.get("catalogs") or [], owner_bm)
    if trusted.get("error"):
        result["error"] = trusted.get("error")
        _persist_state(conn, {**result, "ui_status": "blocked", "error_code": result["error"]})
        db.commit()
        return result

    catalog_id = str(trusted.get("catalog_id") or "").strip()
    result["catalog_reused"] = bool(trusted.get("reused") and catalog_id)
    state = harness_state(conn)
    marker = str(state.get("marker") or uuid.uuid4().hex)
    intended = str(state.get("intended_catalog_name") or f"Nahlah Review Harness {marker[:8]}")

    if not catalog_id:
        if not confirm:
            result["ok"] = True
            result["dry_run"] = True
            return result
        _persist_state(conn, {
            **result,
            "marker": marker,
            "intended_catalog_name": intended,
            "ui_status": "setting_up",
            "error_code": None,
        })
        db.commit()
        created = create_owned_product_catalog(owner_bm, intended, token, client=client)
        result["graph_writes"] += 1
        if not created.get("ok"):
            result["error"] = created.get("error") or "catalog_create_failed"
            _persist_state(conn, {**result, "ui_status": "blocked", "error_code": result["error"]})
            db.commit()
            return result
        catalog_id = str(created.get("catalog_id") or "").strip()
        result["created_catalog"] = True

    catalog_owner = fetch_catalog_owner_business_id(catalog_id, token, client=client)
    catalog_bm = str(catalog_owner.get("business_id") or "").strip()
    if not catalog_bm or catalog_bm != owner_bm:
        result["error"] = ERROR_OWNERSHIP_MISMATCH
        _persist_state(conn, {
            **result,
            "ui_status": "blocked",
            "error_code": ERROR_OWNERSHIP_MISMATCH,
            "marker": marker,
            "intended_catalog_name": intended,
        })
        db.commit()
        return result
    if is_blocked_business_id(catalog_bm):
        result["error"] = ERROR_NAHLAH_BM_BLOCKED
        _persist_state(conn, {**result, "ui_status": "blocked", "error_code": result["error"]})
        db.commit()
        return result

    conn.meta_catalog_id = catalog_id
    conn.catalog_enabled = True

    link = link_waba_to_catalog(
        waba_id,
        catalog_id,
        token,
        confirm=confirm,
        client=client,
    )
    if confirm and not link.get("already_linked"):
        result["graph_writes"] += 1
    result["bind_verified"] = bool(link.get("ok"))
    if confirm and not result["bind_verified"]:
        result["error"] = "waba_catalog_link_unverified"
        _persist_state(conn, {
            **result,
            "marker": marker,
            "intended_catalog_name": intended,
            "catalog_id": catalog_id,
            "ui_status": "blocked",
            "error_code": result["error"],
        })
        db.commit()
        return result

    products = _eligible_native_products(db, tenant_id)
    if products:
        product = products[0]
        assign_canonical_retailer_id(product)
        retailer_id = canonical_retailer_id(product, fallback_to_synthetic=True)
        expected = f"nahla_p_{int(product.id)}"
        if retailer_id != expected and not str(getattr(product, "meta_retailer_id", "") or "").strip():
            retailer_id = expected
        result["retailer_id"] = retailer_id
        if confirm:
            pushed = push_one_meta_catalog_item(
                db, tenant_id, retailer_id, confirm=True, client=client,
            )
            result["product_synced"] = bool(pushed.get("ok"))
            if not result["product_synced"]:
                result["error"] = "product_sync_failed"
        else:
            result["product_synced"] = False

    if result["bind_verified"] and (result["product_synced"] or not products):
        result["ui_status"] = "connected_and_synced" if result["product_synced"] else "setting_up"
        result["ok"] = True
        result["error"] = None if result["ok"] else result.get("error")
    elif result["bind_verified"]:
        result["ui_status"] = "setting_up"
        result["ok"] = True
    else:
        result["ui_status"] = "setting_up"

    if result["bind_verified"] and result["product_synced"]:
        result["ui_status"] = "connected_and_synced"
        result["ok"] = True
        result["error"] = None

    persist = {
        **result,
        "marker": marker,
        "intended_catalog_name": intended,
        "catalog_id": catalog_id,
        "error_code": None if result["ok"] else result.get("error"),
        "required_scopes": sorted(REQUIRED_SCOPES),
        "token_pick": token_info.get("token_pick"),
    }
    persist.pop("proof", None)
    _persist_state(conn, persist)
    db.commit()
    logger.info(
        "[REVIEW_HARNESS] tenant=%s ok=%s ui_status=%s created=%s reused=%s bind=%s synced=%s error=%s",
        tenant_id,
        result.get("ok"),
        result.get("ui_status"),
        result.get("created_catalog"),
        result.get("catalog_reused"),
        result.get("bind_verified"),
        result.get("product_synced"),
        result.get("error"),
    )
    return result


def run_catalog_review_harness_background(tenant_id: int) -> None:
    from core.database import SessionLocal  # noqa: PLC0415

    db = SessionLocal()
    try:
        result = run_catalog_review_harness(db, int(tenant_id), confirm=True)
        logger.info(
            "[REVIEW_HARNESS] background tenant=%s ok=%s error=%s",
            tenant_id,
            result.get("ok"),
            result.get("error"),
        )
    except Exception:  # noqa: BLE001
        logger.exception("[REVIEW_HARNESS] background failed tenant=%s", tenant_id)
        db.rollback()
    finally:
        db.close()


def schedule_catalog_review_harness_best_effort(tenant_id: int) -> None:
    """Fire-and-forget. Must never fail WhatsApp connect. No-op under pytest."""
    if not is_catalog_review_harness_enabled():
        return
    if int(tenant_id) <= 0 or int(tenant_id) == 1:
        logger.warning("[REVIEW_HARNESS] schedule refused tenant=%s", tenant_id)
        return
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get(
        "NAHLA_FORCE_META_CATALOG_REVIEW_HARNESS"
    ):
        logger.info("[REVIEW_HARNESS] pytest skip background tenant=%s", tenant_id)
        return

    def _run() -> None:
        try:
            run_catalog_review_harness_background(int(tenant_id))
        except Exception:  # noqa: BLE001
            logger.exception("[REVIEW_HARNESS] thread failed tenant=%s", tenant_id)

    threading.Thread(
        target=_run,
        daemon=True,
        name=f"meta-catalog-review-harness-{tenant_id}",
    ).start()


def preserve_harness_metadata(existing: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Keep harness state across Embedded Signup metadata resets."""
    if not is_catalog_review_harness_enabled():
        return {}
    raw = (existing or {}).get(HARNESS_META_KEY)
    if isinstance(raw, dict) and raw:
        return {HARNESS_META_KEY: dict(raw)}
    return {}
