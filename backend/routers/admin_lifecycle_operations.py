"""Platform-admin lifecycle diagnostics and idempotent recovery actions."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from core.audit import audit
from core.auth import require_admin, require_not_support_impersonation
from core.commerce_lifecycle.operations import (
    build_lifecycle_preflight,
    build_order_recovery_preflight,
    retry_post_cod_final_confirmation,
)
from core.database import get_db


router = APIRouter(prefix="/admin/operations", tags=["admin-operations"])


def _no_store(payload: Dict[str, Any]) -> JSONResponse:
    return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})


@router.get("/commerce-lifecycle/preflight")
def commerce_lifecycle_preflight(
    db: Session = Depends(get_db),
    admin: Dict[str, Any] = Depends(require_admin),
    _not_impersonating: Dict[str, Any] = Depends(require_not_support_impersonation),
):
    result = build_lifecycle_preflight(db)
    audit(
        "admin_commerce_lifecycle_preflight",
        admin_sub=admin.get("sub"),
        schema_ready=result["schema_ready"],
    )
    return _no_store(result)


@router.get("/commerce-lifecycle/orders/{order_id}/preflight")
def commerce_lifecycle_order_preflight(
    order_id: int,
    db: Session = Depends(get_db),
    admin: Dict[str, Any] = Depends(require_admin),
    _not_impersonating: Dict[str, Any] = Depends(require_not_support_impersonation),
):
    result = build_order_recovery_preflight(db, order_id=order_id)
    audit(
        "admin_commerce_lifecycle_order_preflight",
        admin_sub=admin.get("sub"),
        order_id=order_id,
        recovery_eligible=result.get("recovery_eligible", False),
    )
    return _no_store(result)


@router.post("/orders/{order_id}/retry-final-confirmation")
async def retry_final_confirmation(
    order_id: int,
    db: Session = Depends(get_db),
    admin: Dict[str, Any] = Depends(require_admin),
    _not_impersonating: Dict[str, Any] = Depends(require_not_support_impersonation),
):
    result = await retry_post_cod_final_confirmation(db, order_id=order_id)
    audit(
        "admin_retry_post_cod_final_confirmation",
        admin_sub=admin.get("sub"),
        order_id=order_id,
        tenant_id=(result.get("eligibility") or {}).get("tenant_id"),
        outcome=result.get("outcome"),
        ledger_id=result.get("ledger_id"),
    )
    return _no_store(result)


__all__ = ["router"]
