"""Automatic WABA ↔ catalog binding for new-merchant onboarding.

Path A (this module) — Embedded Signup / WhatsApp reconnect:
  1. Discover WABA owner Business.
  2. Use the catalog already linked to that WABA, if any.
  3. Else reuse a single owned catalog in the same Business wallet.
  4. Else create one catalog on that Business and link it.
  5. Never pick by catalog name. Never delete. Never attach another
     tenant's catalog. Never create a replacement to bypass ownership
     or permission failures.

Path B (legacy repair) is NOT implemented here. Cross-Business
catalogs (tenant 1 / 33 style) return ``catalog_business_mismatch``
and require a one-time ownership fix.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.orm.attributes import flag_modified

from core.plan_entitlements import EntitlementLookupUnavailable, get_entitlements
from services.meta_catalog_access import probe_catalog_readable
from services.meta_catalog_import import _select_graph_token
from services.meta_catalog_linking import (
    LINK_STATUS_LINKED,
    _fetch_waba_product_catalogs,
    _graph_json,
    fetch_waba_owner_business_id,
    link_waba_to_catalog,
)

logger = logging.getLogger("nahla.meta_catalog_onboarding")

_AUTO_ONBOARD_ENV = "NAHLA_AUTO_CATALOG_ONBOARDING"
_ONBOARD_LOCK_KEY = 904221

ENSURE_META_KEY = "meta_catalog_ensure"
ENSURED_CATALOG_KEY = "nahla_ensured_catalog_id"

ERROR_CATALOG_BUSINESS_MISMATCH = "catalog_business_mismatch"
ERROR_AMBIGUOUS_WABA_CATALOGS = "ambiguous_waba_catalogs"
ERROR_AMBIGUOUS_OWNED_CATALOGS = "ambiguous_owned_catalogs"
ERROR_CATALOG_MANAGE_PERMISSION = "catalog_manage_permission_required"
ERROR_OWNED_CATALOGS_UNREADABLE = "owned_catalogs_unreadable"
ERROR_CATALOG_CREATE_FAILED = "catalog_create_failed"
ERROR_ONBOARDING_DISABLED = "auto_catalog_onboarding_disabled"
ERROR_CATALOG_CLAIMED_OTHER_TENANT = "catalog_claimed_other_tenant"
ERROR_ONBOARDING_LOCK_FAILED = "onboarding_lock_failed"


class OnboardingLockError(RuntimeError):
    """PostgreSQL advisory lock could not be acquired. Fail closed."""


def auto_catalog_onboarding_enabled() -> bool:
    """Independent of product auto-sync. Default OFF until catalog_management is approved."""
    return os.environ.get(_AUTO_ONBOARD_ENV, "").strip().lower() in ("1", "true", "yes")


def _acquire_tenant_onboard_lock(db: Any, tenant_id: int) -> None:
    """Serialize create/link for one tenant. SQLite/unit callers skip; Postgres failures raise."""
    from sqlalchemy import text  # noqa: PLC0415

    bind = db.get_bind() if hasattr(db, "get_bind") else None
    dialect = str(getattr(getattr(bind, "dialect", None), "name", "") or "")
    if dialect != "postgresql":
        return
    try:
        db.execute(
            text("SELECT pg_advisory_xact_lock(:k, :t)"),
            {"k": _ONBOARD_LOCK_KEY, "t": int(tenant_id)},
        )
    except Exception as exc:
        logger.error(
            "[META_CATALOG_ONBOARD] advisory lock failed tenant=%s",
            tenant_id,
            exc_info=True,
        )
        raise OnboardingLockError(ERROR_ONBOARDING_LOCK_FAILED) from exc


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ids_of(rows: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for row in rows:
        cid = str((row or {}).get("id") or "").strip()
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def _persist_ensure(conn: Any, payload: Dict[str, Any]) -> None:
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    safe = {
        k: v for k, v in payload.items()
        if k not in {"token", "access_token", "authorization"}
    }
    meta[ENSURE_META_KEY] = safe
    if payload.get("catalog_id") and payload.get("created"):
        meta[ENSURED_CATALOG_KEY] = str(payload["catalog_id"])
    if payload.get("waba_catalog_linked") is True:
        meta["waba_catalog_linked"] = True
    elif payload.get("waba_catalog_linked") is False:
        meta["waba_catalog_linked"] = False
    conn.extra_metadata = meta
    if getattr(conn, "_sa_instance_state", None) is not None:
        flag_modified(conn, "extra_metadata")


def _load_connection(db: Any, tenant_id: int) -> Any:
    from models import WhatsAppConnection  # noqa: PLC0415

    return (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == int(tenant_id))
        .first()
    )


def _tenant_catalog_name(db: Any, tenant_id: int) -> str:
    from models import Tenant  # noqa: PLC0415

    row = db.query(Tenant).filter(Tenant.id == int(tenant_id)).first()
    name = str(getattr(row, "name", None) or "").strip()
    if name:
        return name[:80]
    return f"Nahla catalog {int(tenant_id)}"


def _entitled_for_catalog(db: Any, tenant_id: int) -> bool:
    try:
        ent = get_entitlements(db, int(tenant_id), strict_lookup=True)
        return bool(ent.has_feature("meta_catalog_sync"))
    except EntitlementLookupUnavailable:
        return False
    except Exception:  # noqa: silent-ok — entitlement read must not fail WhatsApp onboarding
        logger.warning(
            "[META_CATALOG_ONBOARD] entitlement check failed tenant=%s",
            tenant_id,
            exc_info=True,
        )
        return False


def _list_owned_catalog_ids(
    business_id: str,
    token: str,
    *,
    client: Optional[httpx.Client] = None,
) -> Tuple[List[str], Optional[str], bool]:
    """Return (ids, error, has_more_pages). Never matches by name."""
    resp = _graph_json(
        "GET",
        f"{business_id}/owned_product_catalogs",
        token,
        params={"fields": "id", "limit": 100},
        client=client,
    )
    if not resp.get("ok"):
        graph_err = resp.get("error") or {}
        if int(graph_err.get("subcode") or 0) == 2388100:
            return [], ERROR_CATALOG_MANAGE_PERMISSION, False
        return [], ERROR_OWNED_CATALOGS_UNREADABLE, False
    body = resp.get("body") or {}
    rows = body.get("data") or []
    ids = _ids_of(rows if isinstance(rows, list) else [])
    paging = body.get("paging") if isinstance(body.get("paging"), dict) else {}
    has_more = bool((paging.get("next") or "").strip())
    return ids, None, has_more


def _create_owned_catalog(
    business_id: str,
    token: str,
    name: str,
    *,
    client: Optional[httpx.Client] = None,
) -> Tuple[str, Optional[str]]:
    resp = _graph_json(
        "POST",
        f"{business_id}/owned_product_catalogs",
        token,
        data={"name": name},
        client=client,
    )
    if not resp.get("ok"):
        graph_err = resp.get("error") or {}
        if int(graph_err.get("subcode") or 0) == 2388100:
            return "", ERROR_CATALOG_MANAGE_PERMISSION
        return "", ERROR_CATALOG_CREATE_FAILED
    cid = str((resp.get("body") or {}).get("id") or "").strip()
    if not cid:
        return "", ERROR_CATALOG_CREATE_FAILED
    return cid, None


def _stamp_catalog_id(conn: Any, catalog_id: str) -> None:
    current = str(getattr(conn, "meta_catalog_id", "") or "").strip()
    if current == catalog_id:
        return
    conn.meta_catalog_id = catalog_id


def _enable_catalog_if_entitled(db: Any, conn: Any, tenant_id: int) -> None:
    if bool(getattr(conn, "catalog_enabled", False)):
        return
    if not _entitled_for_catalog(db, tenant_id):
        return
    conn.catalog_enabled = True


def _catalog_claimed_by_other_tenant(db: Any, tenant_id: int, catalog_id: str) -> bool:
    cid = str(catalog_id or "").strip()
    if not cid:
        return False
    from models import WhatsAppConnection  # noqa: PLC0415

    rows = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.meta_catalog_id == cid)
        .all()
    )
    if not isinstance(rows, (list, tuple)):
        return False
    for row in rows:
        other = int(getattr(row, "tenant_id", 0) or 0)
        if other and other != int(tenant_id):
            return True
    return False


def _catalog_management_granted(
    token: str,
    *,
    client: Optional[httpx.Client] = None,
) -> Optional[bool]:
    """True when the token is proven to include catalog_management.

    False = proven missing. None = unproven (treat as fail-closed).
    """
    if not str(token or "").strip():
        return None
    resp = _graph_json(
        "GET",
        "me/permissions",
        token,
        params={},
        client=client,
    )
    if not resp.get("ok"):
        return None
    body = resp.get("body") or {}
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return None
    granted = False
    saw = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("permission") or "") != "catalog_management":
            continue
        saw = True
        granted = str(row.get("status") or "").lower() == "granted"
    if not saw:
        return False
    return granted


def _prove_readable_then_stamp(
    conn: Any,
    db: Any,
    tenant_id: int,
    token: str,
    catalog_id: str,
    result: Dict[str, Any],
    *,
    owner_bm: str = "",
    client: Optional[httpx.Client] = None,
) -> bool:
    """Stamp meta_catalog_id only after Graph proves the catalog is readable."""
    if _catalog_claimed_by_other_tenant(db, tenant_id, catalog_id):
        result["error"] = ERROR_CATALOG_CLAIMED_OTHER_TENANT
        result["ok"] = False
        result["waba_catalog_linked"] = False
        return False
    probe = probe_catalog_readable(token, catalog_id, client=client)
    result["catalog_read_ok"] = bool(probe.get("ok"))
    catalog_bm = str(probe.get("business_id") or "").strip()
    if catalog_bm:
        result["catalog_business_id"] = catalog_bm
    if not probe.get("ok"):
        result["error"] = "catalog_not_readable"
        result["ok"] = False
        result["waba_catalog_linked"] = False
        return False
    owner = str(owner_bm or result.get("waba_owner_business_id") or "").strip()
    if catalog_bm and owner and catalog_bm != owner:
        result["error"] = ERROR_CATALOG_BUSINESS_MISMATCH
        result["legacy_repair"] = True
        result["ok"] = False
        result["waba_catalog_linked"] = False
        return False
    _stamp_catalog_id(conn, catalog_id)
    _enable_catalog_if_entitled(db, conn, tenant_id)
    return True


def ensure_waba_catalog_for_tenant(
    db: Any,
    tenant_id: int,
    *,
    confirm: bool = False,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """Discover / reuse / create+link a catalog in the WABA owner Business.

    Idempotent. ``confirm=False`` is dry-run (no Graph POST, no DB stamp
    of a new catalog id).
    """
    result: Dict[str, Any] = {
        "ok": False,
        "tenant_id": int(tenant_id),
        "dry_run": not confirm,
        "path": "onboarding",
        "action": None,
        "catalog_id": None,
        "waba_id": None,
        "waba_owner_business_id": None,
        "catalog_business_id": None,
        "created": False,
        "already_linked": False,
        "waba_catalog_linked": False,
        "legacy_repair": False,
        "error": None,
        "link_status": None,
    }
    if not auto_catalog_onboarding_enabled():
        result["skipped"] = True
        result["error"] = ERROR_ONBOARDING_DISABLED
        return result

    conn = _load_connection(db, tenant_id)
    if conn is None:
        result["error"] = "connection_not_found"
        return result

    try:
        _acquire_tenant_onboard_lock(db, tenant_id)
    except OnboardingLockError:
        result["error"] = ERROR_ONBOARDING_LOCK_FAILED
        return result

    conn = _load_connection(db, tenant_id)
    if conn is None:
        result["error"] = "connection_not_found"
        return result

    waba_id = str(getattr(conn, "whatsapp_business_account_id", "") or "").strip()
    stamped = str(getattr(conn, "meta_catalog_id", "") or "").strip()
    result["waba_id"] = waba_id or None
    result["catalog_id"] = stamped or None
    pick = _select_graph_token(conn) or {}
    token = str(pick.get("token") or "").strip()
    if not waba_id:
        result["error"] = "missing_waba_id"
        _persist_ensure(conn, {**result, "at": _now_iso()})
        db.commit()
        return result
    if not token:
        result["error"] = "missing_graph_token"
        _persist_ensure(conn, {**result, "at": _now_iso()})
        db.commit()
        return result

    owner = fetch_waba_owner_business_id(waba_id, token, client=client)
    owner_bm = str(owner.get("business_id") or "").strip()
    result["waba_owner_business_id"] = owner_bm or None
    if not owner_bm:
        result["error"] = str(owner.get("error") or "waba_owner_business_missing")
        _persist_ensure(conn, {**result, "at": _now_iso()})
        db.commit()
        return result

    linked, http_status, graph_err = _fetch_waba_product_catalogs(
        waba_id, token, client=client,
    )
    result["http_status"] = http_status
    if graph_err is not None:
        result["error"] = "waba_catalogs_unreadable"
        result["graph_error"] = {
            "code": graph_err.get("meta_code") or graph_err.get("code"),
            "message": str(graph_err.get("meta_message") or graph_err.get("message") or "")[:240],
        }
        _persist_ensure(conn, {**result, "at": _now_iso()})
        db.commit()
        return result

    linked_ids = _ids_of(linked)
    result["linked_catalog_ids"] = linked_ids

    chosen = ""
    if len(linked_ids) > 1:
        result["error"] = ERROR_AMBIGUOUS_WABA_CATALOGS
        result["waba_catalog_linked"] = False
        _persist_ensure(conn, {**result, "at": _now_iso()})
        db.commit()
        return result
    if len(linked_ids) == 1:
        chosen = linked_ids[0]
        result["action"] = "reuse_linked"
        if stamped and stamped != chosen:
            result["error"] = ERROR_AMBIGUOUS_WABA_CATALOGS
            result["waba_catalog_linked"] = False
            _persist_ensure(conn, {**result, "at": _now_iso()})
            db.commit()
            return result

    if chosen:
        result["catalog_id"] = chosen
        if confirm:
            if not _prove_readable_then_stamp(
                conn, db, tenant_id, token, chosen, result,
                owner_bm=owner_bm, client=client,
            ):
                _persist_ensure(conn, {**result, "at": _now_iso()})
                db.commit()
                return result
        else:
            probe = probe_catalog_readable(token, chosen, client=client)
            catalog_bm = str(probe.get("business_id") or "").strip()
            if catalog_bm:
                result["catalog_business_id"] = catalog_bm
            if catalog_bm and owner_bm and catalog_bm != owner_bm:
                result["error"] = ERROR_CATALOG_BUSINESS_MISMATCH
                result["legacy_repair"] = True
                result["waba_catalog_linked"] = False
                _persist_ensure(conn, {**result, "at": _now_iso()})
                db.commit()
                return result
            if _catalog_claimed_by_other_tenant(db, tenant_id, chosen):
                result["error"] = ERROR_CATALOG_CLAIMED_OTHER_TENANT
                result["waba_catalog_linked"] = False
                _persist_ensure(conn, {**result, "at": _now_iso()})
                db.commit()
                return result
        result["already_linked"] = True
        result["waba_catalog_linked"] = True
        result["link_status"] = LINK_STATUS_LINKED
        result["ok"] = True
        _persist_ensure(conn, {**result, "at": _now_iso()})
        db.commit()
        return result

    # No catalog is linked to this WABA.
    if stamped:
        probe = probe_catalog_readable(token, stamped, client=client)
        catalog_bm = str(probe.get("business_id") or "").strip()
        result["catalog_business_id"] = catalog_bm or None
        if not probe.get("ok"):
            result["error"] = "catalog_not_readable"
            result["waba_catalog_linked"] = False
            _persist_ensure(conn, {**result, "at": _now_iso()})
            db.commit()
            return result
        if catalog_bm and catalog_bm != owner_bm:
            result["error"] = ERROR_CATALOG_BUSINESS_MISMATCH
            result["legacy_repair"] = True
            result["waba_catalog_linked"] = False
            _persist_ensure(conn, {**result, "at": _now_iso()})
            db.commit()
            return result
        result["action"] = "link_stamped"
        result["catalog_id"] = stamped
        if not confirm:
            result["ok"] = True
            _persist_ensure(conn, {**result, "at": _now_iso()})
            db.commit()
            return result
        link = link_waba_to_catalog(
            waba_id, stamped, token, confirm=True, client=client,
        )
        result["link"] = {
            "action": link.get("action"),
            "error": link.get("error"),
            "meta": link.get("meta"),
        }
        if not link.get("ok"):
            result["error"] = str(link.get("error") or "waba_catalog_link_failed")
            result["waba_catalog_linked"] = False
            _persist_ensure(conn, {**result, "at": _now_iso()})
            db.commit()
            return result
        result["ok"] = True
        result["already_linked"] = bool(link.get("already_linked"))
        result["waba_catalog_linked"] = True
        result["link_status"] = LINK_STATUS_LINKED
        _enable_catalog_if_entitled(db, conn, tenant_id)
        _persist_ensure(conn, {**result, "at": _now_iso()})
        db.commit()
        return result

    owned_ids, owned_err, has_more = _list_owned_catalog_ids(
        owner_bm, token, client=client,
    )
    if owned_err:
        result["error"] = owned_err
        result["waba_catalog_linked"] = False
        _persist_ensure(conn, {**result, "at": _now_iso()})
        db.commit()
        return result

    meta = dict(getattr(conn, "extra_metadata", None) or {})
    previously_ensured = str(meta.get(ENSURED_CATALOG_KEY) or "").strip()
    if previously_ensured and previously_ensured in owned_ids:
        chosen = previously_ensured
        result["action"] = "reuse_ensured"
    elif len(owned_ids) == 1 and not has_more:
        chosen = owned_ids[0]
        result["action"] = "reuse_owned"
    elif owned_ids or has_more:
        result["error"] = ERROR_AMBIGUOUS_OWNED_CATALOGS
        result["owned_catalog_count"] = len(owned_ids)
        result["waba_catalog_linked"] = False
        _persist_ensure(conn, {**result, "at": _now_iso()})
        db.commit()
        return result

    if chosen:
        if _catalog_claimed_by_other_tenant(db, tenant_id, chosen):
            result["error"] = ERROR_CATALOG_CLAIMED_OTHER_TENANT
            result["ok"] = False
            result["waba_catalog_linked"] = False
            _persist_ensure(conn, {**result, "at": _now_iso()})
            db.commit()
            return result
        result["catalog_id"] = chosen
        result["action"] = result.get("action") or "reuse_owned"
        if not confirm:
            result["ok"] = True
            _persist_ensure(conn, {**result, "at": _now_iso()})
            db.commit()
            return result
        link = link_waba_to_catalog(
            waba_id, chosen, token, confirm=True, client=client,
        )
        result["link"] = {
            "action": link.get("action"),
            "error": link.get("error"),
            "meta": link.get("meta"),
        }
        if not link.get("ok"):
            result["error"] = str(link.get("error") or "waba_catalog_link_failed")
            result["waba_catalog_linked"] = False
            _persist_ensure(conn, {**result, "at": _now_iso()})
            db.commit()
            return result
        if not _prove_readable_then_stamp(
            conn, db, tenant_id, token, chosen, result,
            owner_bm=owner_bm, client=client,
        ):
            _persist_ensure(conn, {**result, "at": _now_iso()})
            db.commit()
            return result
        result["ok"] = True
        result["waba_catalog_linked"] = True
        result["link_status"] = LINK_STATUS_LINKED
        _persist_ensure(conn, {**result, "at": _now_iso()})
        db.commit()
        return result

    granted = _catalog_management_granted(token, client=client)
    if granted is not True:
        result["error"] = (
            ERROR_CATALOG_MANAGE_PERMISSION
            if granted is False
            else ERROR_OWNED_CATALOGS_UNREADABLE
        )
        result["waba_catalog_linked"] = False
        _persist_ensure(conn, {**result, "at": _now_iso()})
        db.commit()
        return result

    result["action"] = "create_and_link"
    if not confirm:
        result["ok"] = True
        _persist_ensure(conn, {**result, "at": _now_iso()})
        db.commit()
        return result

    created_id, create_err = _create_owned_catalog(
        owner_bm, token, _tenant_catalog_name(db, tenant_id), client=client,
    )
    if create_err or not created_id:
        result["error"] = create_err or ERROR_CATALOG_CREATE_FAILED
        result["waba_catalog_linked"] = False
        _persist_ensure(conn, {**result, "at": _now_iso()})
        db.commit()
        return result
    result["created"] = True
    result["catalog_id"] = created_id
    link = link_waba_to_catalog(
        waba_id, created_id, token, confirm=True, client=client,
    )
    result["link"] = {
        "action": link.get("action"),
        "error": link.get("error"),
        "meta": link.get("meta"),
    }
    if not link.get("ok"):
        result["error"] = str(link.get("error") or "waba_catalog_link_failed")
        result["waba_catalog_linked"] = False
        _persist_ensure(conn, {**result, "at": _now_iso()})
        db.commit()
        return result
    if not _prove_readable_then_stamp(
        conn, db, tenant_id, token, created_id, result,
        owner_bm=owner_bm, client=client,
    ):
        _persist_ensure(conn, {**result, "at": _now_iso()})
        db.commit()
        return result
    result["ok"] = True
    result["waba_catalog_linked"] = True
    result["link_status"] = LINK_STATUS_LINKED
    _persist_ensure(conn, {**result, "at": _now_iso()})
    db.commit()
    return result


__all__ = [
    "ENSURE_META_KEY",
    "ERROR_AMBIGUOUS_OWNED_CATALOGS",
    "ERROR_AMBIGUOUS_WABA_CATALOGS",
    "ERROR_CATALOG_BUSINESS_MISMATCH",
    "ERROR_CATALOG_MANAGE_PERMISSION",
    "ERROR_CATALOG_CLAIMED_OTHER_TENANT",
    "ERROR_OWNED_CATALOGS_UNREADABLE",
    "ERROR_ONBOARDING_LOCK_FAILED",
    "ERROR_ONBOARDING_DISABLED",
    "OnboardingLockError",
    "auto_catalog_onboarding_enabled",
    "ensure_waba_catalog_for_tenant",
]
