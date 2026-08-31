"""
services/meta_catalog_reconnect.py
──────────────────────────────────
After WhatsApp (re)connection: run new-merchant catalog onboarding
(ensure_waba_catalog_for_tenant), then bind + optional product sync.

Does not delete catalogs. Does not move Business Portfolio ownership.
Cross-Business stamps are fail-closed as catalog_business_mismatch
(legacy repair), not auto-shared or replaced.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy.orm.attributes import flag_modified

from core.catalog import is_whatsapp_channel_publish_eligible
from services.meta_catalog_access import select_catalog_graph_token
from services.meta_catalog_import import _select_graph_token
from services.meta_catalog_linking import (
    fetch_waba_owner_business_id,
    get_waba_catalog_link_status,
    link_waba_to_catalog,
)

logger = logging.getLogger("nahla.meta_catalog_reconnect")

_BIND_META_KEY = "meta_catalog_bind"
_RECONNECT_EXECUTOR: ThreadPoolExecutor | None = None
_RECONNECT_COALESCER = None
RECONNECT_QUEUE_MAX = 32


def catalog_config_changes_require_reconcile(changes: Optional[Dict[str, Any]]) -> bool:
    """True when a catalog settings write should re-bind + re-sync."""
    if not isinstance(changes, dict) or not changes:
        return False
    enabled = changes.get("catalog_enabled")
    if isinstance(enabled, dict) and enabled.get("after") is True:
        return True
    catalog_id = changes.get("meta_catalog_id")
    if isinstance(catalog_id, dict) and str(catalog_id.get("after") or "").strip():
        return True
    return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _persist_bind_result(conn: Any, payload: Dict[str, Any]) -> None:
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    safe = {
        k: v for k, v in payload.items()
        if k not in {"token", "access_token", "authorization"}
    }
    meta[_BIND_META_KEY] = safe
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


def _eligible_product_ids(db: Any, tenant_id: int) -> List[int]:
    from models import Product  # noqa: PLC0415

    rows = (
        db.query(Product)
        .filter(Product.tenant_id == int(tenant_id))
        .all()
    )
    out: List[int] = []
    for row in rows:
        if int(getattr(row, "tenant_id", 0) or 0) != int(tenant_id):
            continue
        if not is_whatsapp_channel_publish_eligible(row):
            continue
        pid = getattr(row, "id", None)
        if pid is None:
            continue
        out.append(int(pid))
    return out


def bind_current_waba_to_merchant_catalog(
    db: Any,
    tenant_id: int,
    *,
    confirm: bool = False,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """Share (if needed) + POST-link current WABA to conn.meta_catalog_id."""
    result: Dict[str, Any] = {
        "ok": False,
        "skipped": False,
        "tenant_id": int(tenant_id),
        "catalog_id": None,
        "waba_id": None,
        "catalog_reused": False,
        "already_linked": False,
        "shared": False,
        "error": None,
        "link_status": None,
        "catalog_token_source": None,
    }
    conn = _load_connection(db, tenant_id)
    if conn is None:
        result["error"] = "connection_not_found"
        return result

    catalog_id = str(getattr(conn, "meta_catalog_id", "") or "").strip()
    waba_id = str(getattr(conn, "whatsapp_business_account_id", "") or "").strip()
    result["catalog_id"] = catalog_id or None
    result["waba_id"] = waba_id or None

    if not bool(getattr(conn, "catalog_enabled", False)):
        result["ok"] = True
        result["skipped"] = True
        result["error"] = "catalog_disabled"
        _persist_bind_result(conn, {**result, "at": _now_iso()})
        db.commit()
        return result

    if not catalog_id:
        result["error"] = "missing_catalog_id"
        _persist_bind_result(conn, {**result, "at": _now_iso()})
        db.commit()
        return result
    result["catalog_reused"] = True

    if not waba_id:
        result["error"] = "missing_waba_id"
        _persist_bind_result(conn, {**result, "at": _now_iso()})
        db.commit()
        return result

    catalog_pick = select_catalog_graph_token(conn, catalog_id, client=client)
    result["catalog_token_source"] = catalog_pick.get("token_source")
    catalog_token = str(catalog_pick.get("token") or "").strip()
    if not catalog_token:
        result["error"] = str(catalog_pick.get("error") or "catalog_not_readable")
        result["probes"] = catalog_pick.get("probes")
        _persist_bind_result(conn, {**result, "at": _now_iso()})
        db.commit()
        return result

    merchant_pick = _select_graph_token(conn) or {}
    merchant_token = str(merchant_pick.get("token") or "").strip()
    if not merchant_token:
        result["error"] = "missing_graph_token"
        _persist_bind_result(conn, {**result, "at": _now_iso()})
        db.commit()
        return result

    owner = fetch_waba_owner_business_id(waba_id, merchant_token, client=client)
    owner_bm = str(owner.get("business_id") or "").strip()
    catalog_bm = str((catalog_pick.get("catalog") or {}).get("business_id") or "").strip()
    result["waba_owner_business_id"] = owner_bm or None
    result["catalog_business_id"] = catalog_bm or None

    if catalog_bm and owner_bm and catalog_bm != owner_bm:
        from services.meta_catalog_onboarding import auto_catalog_onboarding_enabled  # noqa: PLC0415

        if auto_catalog_onboarding_enabled():
            result["error"] = "catalog_business_mismatch"
            result["legacy_repair"] = True
            result["ok"] = False
            _persist_bind_result(conn, {**result, "at": _now_iso()})
            db.commit()
            return result

    link = link_waba_to_catalog(
        waba_id,
        catalog_id,
        merchant_token,
        confirm=confirm,
        client=client,
    )
    result["already_linked"] = bool(link.get("already_linked"))
    result["link_status"] = link.get("link_status")
    result["linked_catalog_ids"] = link.get("linked_catalog_ids")
    result["link"] = {
        "action": link.get("action"),
        "error": link.get("error"),
        "dry_run": link.get("dry_run"),
    }
    if confirm:
        result["ok"] = bool(link.get("ok"))
        if not result["ok"]:
            result["error"] = str(link.get("error") or "waba_catalog_link_failed")
    else:
        result["ok"] = True
        result["dry_run"] = True

    _persist_bind_result(conn, {**result, "at": _now_iso()})
    db.commit()
    return result


def reconcile_meta_catalog_after_whatsapp_change(
    db: Any,
    tenant_id: int,
    *,
    confirm: bool = True,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """Bind current WABA to the reusable catalog, then upsert eligible items."""
    from services.native_meta_sync_orchestrator import attempt_native_meta_sync  # noqa: PLC0415

    out: Dict[str, Any] = {
        "ok": False,
        "tenant_id": int(tenant_id),
        "bind": None,
        "product_ids": [],
        "synced": 0,
        "failed": 0,
        "skipped": 0,
        "error": None,
    }
    from services.meta_catalog_onboarding import (  # noqa: PLC0415
        auto_catalog_onboarding_enabled,
        ensure_waba_catalog_for_tenant,
    )

    if auto_catalog_onboarding_enabled():
        ensure = ensure_waba_catalog_for_tenant(
            db, tenant_id, confirm=confirm, client=client,
        )
        out["ensure"] = {
            "ok": ensure.get("ok"),
            "action": ensure.get("action"),
            "error": ensure.get("error"),
            "catalog_id": ensure.get("catalog_id"),
            "created": ensure.get("created"),
            "legacy_repair": ensure.get("legacy_repair"),
            "waba_catalog_linked": ensure.get("waba_catalog_linked"),
        }
        if not ensure.get("ok"):
            out["error"] = ensure.get("error")
            return out
    else:
        out["ensure"] = {
            "skipped": True,
            "reason": "auto_catalog_onboarding_disabled",
        }

    bind = bind_current_waba_to_merchant_catalog(
        db, tenant_id, confirm=confirm, client=client,
    )
    out["bind"] = {
        "ok": bind.get("ok"),
        "skipped": bind.get("skipped"),
        "error": bind.get("error"),
        "catalog_id": bind.get("catalog_id"),
        "waba_id": bind.get("waba_id"),
        "catalog_reused": bind.get("catalog_reused"),
        "already_linked": bind.get("already_linked"),
        "catalog_token_source": bind.get("catalog_token_source"),
        "legacy_repair": bind.get("legacy_repair"),
    }
    if bind.get("skipped") and bind.get("error") == "catalog_disabled":
        out["ok"] = True
        out["skipped"] = 1
        return out
    if not bind.get("ok"):
        out["error"] = bind.get("error")
        return out

    product_ids = _eligible_product_ids(db, tenant_id)
    out["product_ids"] = product_ids
    if not product_ids:
        out["ok"] = True
        return out

    if not confirm:
        out["ok"] = True
        out["dry_run"] = True
        return out

    from services.whatsapp_catalog_sync import whatsapp_catalog_auto_sync_enabled  # noqa: PLC0415

    if not whatsapp_catalog_auto_sync_enabled():
        out["ok"] = True
        out["skipped"] = len(product_ids)
        out["error"] = "auto_sync_disabled"
        return out

    for pid in product_ids:
        try:
            sync = attempt_native_meta_sync(
                db,
                int(tenant_id),
                int(pid),
                client=client,
                allow_synced_retry=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "[META_CATALOG_RECONNECT] product sync failed tenant=%s product=%s",
                tenant_id,
                pid,
            )
            out["failed"] += 1
            continue
        if sync.get("ok"):
            out["synced"] += 1
        elif sync.get("skipped"):
            out["skipped"] += 1
        else:
            out["failed"] += 1

    out["ok"] = out["failed"] == 0
    if not out["ok"] and out["error"] is None:
        out["error"] = "product_sync_partial_failure"
    return out


def run_meta_catalog_reconnect_background(tenant_id: int) -> None:
    from core.database import SessionLocal  # noqa: PLC0415

    db = SessionLocal()
    try:
        result = reconcile_meta_catalog_after_whatsapp_change(db, int(tenant_id), confirm=True)
        logger.info(
            "[META_CATALOG_RECONNECT] tenant=%s ok=%s bind_error=%s synced=%s failed=%s",
            tenant_id,
            result.get("ok"),
            (result.get("bind") or {}).get("error"),
            result.get("synced"),
            result.get("failed"),
        )
    except Exception:  # noqa: BLE001
        logger.exception("[META_CATALOG_RECONNECT] background failed tenant=%s", tenant_id)
        db.rollback()
    finally:
        db.close()


def get_meta_catalog_reconnect_executor() -> ThreadPoolExecutor:
    global _RECONNECT_EXECUTOR
    if _RECONNECT_EXECUTOR is None:
        _RECONNECT_EXECUTOR = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="meta-catalog-reconnect",
        )
    return _RECONNECT_EXECUTOR


def _reconnect_coalescer():
    global _RECONNECT_COALESCER
    if _RECONNECT_COALESCER is None:
        from services.whatsapp_catalog_sync import TenantWorkCoalescer  # noqa: PLC0415

        _RECONNECT_COALESCER = TenantWorkCoalescer(
            max_queued=RECONNECT_QUEUE_MAX,
            max_overflow=RECONNECT_QUEUE_MAX,
            get_executor=get_meta_catalog_reconnect_executor,
            runner=run_meta_catalog_reconnect_background,
        )
    return _RECONNECT_COALESCER


def reset_meta_catalog_reconnect_scheduler_for_tests() -> None:
    global _RECONNECT_COALESCER
    if _RECONNECT_COALESCER is not None:
        _RECONNECT_COALESCER.reset_for_tests()
    _RECONNECT_COALESCER = None


def schedule_meta_catalog_reconnect_best_effort(tenant_id: int) -> None:
    """Fire-and-forget. Must never fail WhatsApp connect. No-op under pytest."""
    if tenant_id <= 0:
        return
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get(
        "NAHLA_FORCE_META_CATALOG_RECONNECT"
    ):
        logger.info(
            "[META_CATALOG_RECONNECT] pytest skip background tenant=%s", tenant_id,
        )
        return

    _reconnect_coalescer().submit(int(tenant_id))


def current_waba_link_snapshot(db: Any, tenant_id: int) -> Dict[str, Any]:
    return get_waba_catalog_link_status(db, tenant_id)


__all__ = [
    "bind_current_waba_to_merchant_catalog",
    "catalog_config_changes_require_reconcile",
    "current_waba_link_snapshot",
    "reconcile_meta_catalog_after_whatsapp_change",
    "run_meta_catalog_reconnect_background",
    "schedule_meta_catalog_reconnect_best_effort",
    "get_meta_catalog_reconnect_executor",
    "reset_meta_catalog_reconnect_scheduler_for_tests",
]
