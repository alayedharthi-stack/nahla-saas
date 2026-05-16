"""
routers/catalog.py
──────────────────
Production endpoints for WhatsApp Catalog settings (May 2026 #11).

Promotes the catalog config / send-product / status surfaces that
previously lived under ``/admin/debug`` into proper merchant- and
admin-facing APIs:

  Merchant (JWT, own tenant only)
    GET    /merchant/catalog/status
    PATCH  /merchant/catalog/config
    POST   /merchant/catalog/test-send

  Admin (require_admin, target any tenant)
    GET    /admin/catalog/status?tenant_id=…
    GET    /admin/catalog/audit
    PATCH  /admin/catalog/config
    POST   /admin/catalog/test-send

Why this exists
───────────────
The catalog configuration was previously a manual DB edit + a
``/admin/debug/catalog-config`` POST — neither path is usable for
self-serve. With this router every merchant can wire up their Meta
Commerce Manager catalog from the dashboard, run an end-to-end test
send, and see a structured diagnosis when something fails. The
admin variant adds a cross-tenant audit so support can spot
misconfigured catalogs in bulk.

Design contract
───────────────
* All endpoints reuse the SAME helpers as the WhatsApp webhook —
  ``core.catalog`` for eligibility / retailer-id resolution and
  ``whatsapp_webhook._try_send_catalog_product`` /
  ``_send_media_message`` / ``_send_cta_url`` for the actual sender
  chain. The test-send path is bit-for-bit identical to a real
  brain reply — no parallel implementation to drift.
* Plan gating uses ``meta_catalog_sync`` from PlanFeatures
  (Growth+). Merchants on Starter see a 403 with the standard
  upgrade payload.
* Tenant isolation: merchant endpoints derive tenant_id ONLY from
  the JWT claim (``resolve_tenant_id``) — a request body field is
  never trusted. Admin endpoints take an explicit ``tenant_id``
  param and require ``require_admin``.
* No social / stance / AI tone changes — this router is product
  catalog configuration UX only.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.audit import audit
from core.auth import get_current_user, require_admin
from core.catalog import (
    catalog_summary,
    effective_retailer_id,
    is_catalog_eligible,
)
from core.database import get_db
from core.plan_entitlements import (
    EntitlementError,
    entitlement_http_error,
    get_entitlements,
    require_feature,
)
from core.tenant import resolve_tenant_id
from models import Tenant, WhatsAppConnection
from modules.observability.delivery_mode import (
    compute_final_delivery_mode,
    new_delivery_audit,
)

logger = logging.getLogger("nahla.catalog")

# Two routers — one mounted under /merchant, one under /admin. Both
# live in this file because the underlying helpers are shared.
merchant_router = APIRouter(prefix="/merchant/catalog", tags=["Merchant Catalog"])
admin_router    = APIRouter(prefix="/admin/catalog",    tags=["Admin Catalog"])

_FEATURE_KEY = "meta_catalog_sync"


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic bodies — SHARED between merchant and admin endpoints
# ─────────────────────────────────────────────────────────────────────────────

class CatalogConfigPatch(BaseModel):
    """Body for PATCH .../catalog/config.

    All fields are optional — only the supplied ones are written.
    Pass ``meta_catalog_id=""`` to CLEAR the binding (sets the column
    to NULL).
    """

    meta_catalog_id: Optional[str] = Field(
        default=None,
        description=(
            "Meta Commerce Manager catalog id (numeric string). The "
            "value Meta exposes in Commerce Manager → Settings → "
            "Catalog ID. Required when toggling catalog_enabled to "
            "true with no previously-set id."
        ),
    )
    catalog_enabled: Optional[bool] = Field(
        default=None,
        description=(
            "Per-connection kill switch. Must be true AND "
            "meta_catalog_id must be set for the webhook to attempt "
            "catalog product sends."
        ),
    )


class CatalogTestSendBody(BaseModel):
    """Body for POST .../catalog/test-send."""

    to: str = Field(
        ...,
        description=(
            "Recipient MSISDN in E.164 without the leading + "
            "(e.g. 9665XXXXXXXX)."
        ),
    )
    product_id: Optional[int] = Field(
        default=None,
        description="Resolve by Nahla Product.id (preferred when known).",
    )
    product_title: Optional[str] = Field(
        default=None,
        description=(
            "Resolve by fuzzy title match. Required when "
            "product_id is absent."
        ),
    )
    mode: str = Field(
        default="auto",
        description=(
            "Send strategy: auto | catalog | image | cta. "
            "Unknown values fall back to auto."
        ),
    )


class AdminCatalogConfigPatch(CatalogConfigPatch):
    """Admin variant that carries the target tenant_id."""

    tenant_id: int = Field(..., ge=1)


class AdminCatalogTestSendBody(CatalogTestSendBody):
    """Admin variant that carries the target tenant_id."""

    tenant_id: int = Field(..., ge=1)


# ─────────────────────────────────────────────────────────────────────────────
# Shared core helpers
# ─────────────────────────────────────────────────────────────────────────────

def _status_payload(db: Session, tenant_id: int, *, sample: int = 5) -> Dict[str, Any]:
    """Build the same status snapshot the merchant + admin endpoints
    return. Reused inside the audit endpoint per-tenant.

    Shape:
      {
        "tenant_id":      int,
        "connection":     {found, phone_id_tail, status, catalog_enabled, meta_catalog_id},
        "eligibility":    {ok, reason},
        "products_sample":[{id, title, external_id, meta_retailer_id, effective_retailer_id}],
        "coverage":       {with_retailer_id, without_retailer_id, sample_size},
        "advice":         str,
      }
    """
    conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == tenant_id)
        .first()
    )
    summary = catalog_summary(conn)
    phone_number_id = str(getattr(conn, "phone_number_id", "") or "") if conn else ""
    conn_block: Dict[str, Any] = {
        "found":            conn is not None,
        "phone_id_tail":    phone_number_id[-4:] if phone_number_id else None,
        "status":           getattr(conn, "status", None) if conn else None,
        "catalog_enabled":  summary["catalog_enabled"],
        "meta_catalog_id":  summary["meta_catalog_id"],
    }

    elig = is_catalog_eligible(conn, products=None)
    eligibility_block = {"ok": elig.ok, "reason": elig.reason}

    products_sample: List[Dict[str, Any]] = []
    coverage = {"with_retailer_id": 0, "without_retailer_id": 0, "sample_size": 0}
    try:
        from models import Product as _Product  # noqa: PLC0415

        rows = (
            db.query(_Product)
            .filter(_Product.tenant_id == tenant_id)
            .order_by(_Product.id)
            .limit(int(sample))
            .all()
        )
        coverage["sample_size"] = len(rows)
        for p in rows:
            rid = effective_retailer_id(p)
            products_sample.append({
                "id":                    p.id,
                "title":                 p.title,
                "external_id":           getattr(p, "external_id", None),
                "meta_retailer_id":      getattr(p, "meta_retailer_id", None),
                "effective_retailer_id": rid or None,
            })
            if rid:
                coverage["with_retailer_id"] += 1
            else:
                coverage["without_retailer_id"] += 1
    except Exception as p_exc:  # noqa: BLE001
        # Swallow — product probe is a nice-to-have, eligibility is
        # the primary signal.
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(
            "[catalog/status] product sample failed for tenant=%s: %s",
            tenant_id, p_exc,
        )

    advice = _status_advice(
        elig_reason=elig.reason,
        connection_found=conn is not None,
        coverage=coverage,
    )

    return {
        "tenant_id":       tenant_id,
        "connection":      conn_block,
        "eligibility":     eligibility_block,
        "products_sample": products_sample,
        "coverage":        coverage,
        "advice":          advice,
    }


def _status_advice(
    *,
    elig_reason: str,
    connection_found: bool,
    coverage: Dict[str, int],
) -> str:
    """Translate the structured diagnostic into one actionable
    sentence. Kept short and merchant-facing — the admin debug
    helper has the long-form version for support escalations.
    """
    if not connection_found:
        return (
            "لم يتم ربط واتساب بعد. أكمل الربط من صفحة «واتساب» "
            "قبل تفعيل الكتالوج."
        )
    if elig_reason == "catalog_disabled":
        return (
            "الكتالوج معطّل. فعّل الزر بعد التأكد من إدخال "
            "Catalog ID من Meta Commerce Manager."
        )
    if elig_reason == "catalog_id_missing":
        return (
            "Catalog ID فارغ. انسخه من Meta Commerce Manager "
            "(Settings → Catalog ID) وألصقه في الحقل بالأعلى."
        )
    if (
        coverage.get("without_retailer_id", 0) > 0
        and coverage.get("with_retailer_id", 0) == 0
    ):
        return (
            "لا يوجد منتج في العيّنة يحمل retailer_id صالحاً. "
            "أعد مزامنة المنتجات من المتجر (سلة/زد/شوبيفاي) "
            "ثم جرّب الإرسال."
        )
    if elig_reason == "ok":
        return (
            "الربط جاهز. استخدم «إرسال تجريبي» للتأكد أن واتساب يعرض "
            "كرت المنتج بشكل صحيح."
        )
    return f"الحالة: {elig_reason}."


def _apply_config_changes(
    conn: WhatsAppConnection,
    patch: CatalogConfigPatch,
) -> Dict[str, Any]:
    """Mutate *conn* in place to reflect the patch. Returns a
    dict ``{field: {before, after}}`` listing the actual diffs so
    the caller can audit them. Does NOT commit.

    Validation:
      * ``catalog_enabled=True`` requires that the resulting
        connection has a non-empty ``meta_catalog_id`` (either in
        the patch or already on the row). Raises HTTPException(400)
        otherwise — exposing a button that toggles to "enabled" but
        leaves the binding empty would silently break catalog sends
        and confuse the merchant.
    """
    before = {
        "meta_catalog_id":  (conn.meta_catalog_id or None),
        "catalog_enabled":  bool(conn.catalog_enabled),
    }
    after = dict(before)

    if patch.meta_catalog_id is not None:
        new_val = patch.meta_catalog_id.strip() or None
        after["meta_catalog_id"] = new_val

    if patch.catalog_enabled is not None:
        after["catalog_enabled"] = bool(patch.catalog_enabled)

    # Validation: enabling requires an id.
    if after["catalog_enabled"] and not after["meta_catalog_id"]:
        raise HTTPException(
            status_code=400,
            detail={
                "error":         "catalog_id_required",
                "message_ar":    (
                    "لا يمكن تفعيل الكتالوج بدون إدخال Catalog ID "
                    "من Meta Commerce Manager."
                ),
                "missing_field": "meta_catalog_id",
            },
        )

    changes: Dict[str, Any] = {}
    if after["meta_catalog_id"] != before["meta_catalog_id"]:
        conn.meta_catalog_id = after["meta_catalog_id"]
        changes["meta_catalog_id"] = {
            "before": before["meta_catalog_id"],
            "after":  after["meta_catalog_id"],
        }
    if after["catalog_enabled"] != before["catalog_enabled"]:
        conn.catalog_enabled = after["catalog_enabled"]
        changes["catalog_enabled"] = {
            "before": before["catalog_enabled"],
            "after":  after["catalog_enabled"],
        }
    return changes


async def _run_test_send(
    db: Session,
    tenant_id: int,
    body: CatalogTestSendBody,
) -> Dict[str, Any]:
    """Exercise the full send chain (catalog → image+CTA → CTA).

    The implementation mirrors ``admin_debug.admin_debug_send_product``
    but lives here so the production endpoints don't import a debug
    module. Returns the same structured audit shape.
    """
    from routers.whatsapp_webhook import (  # noqa: PLC0415
        _send_cta_url, _send_media_message, _try_send_catalog_product,
    )
    from services.product_resolver import (  # noqa: PLC0415
        format_product_card_caption,
        resolve_by_external_id,
        resolve_by_query,
    )

    requested_mode = (body.mode or "auto").strip().lower()
    if requested_mode not in ("auto", "catalog", "image", "cta"):
        requested_mode = "auto"

    to = (body.to or "").strip().lstrip("+")
    if not to.isdigit() or len(to) < 8:
        raise HTTPException(status_code=400, detail="invalid recipient phone")
    to_masked = f"{to[:4]}***{to[-3:]}" if len(to) >= 7 else f"***{to[-2:]}"

    # ── Resolve product ────────────────────────────────────────
    resolution = None
    if body.product_id is not None:
        try:
            from models import Product as _Product  # noqa: PLC0415
            product_row = (
                db.query(_Product)
                .filter(_Product.id == body.product_id,
                        _Product.tenant_id == tenant_id)
                .first()
            )
            if product_row is not None:
                resolution = resolve_by_external_id(
                    db, tenant_id, product_row.external_id or "",
                )
        except Exception:
            resolution = None
    if resolution is None and body.product_title:
        try:
            resolution = resolve_by_query(
                db, tenant_id, body.product_title.strip(),
            )
        except Exception:
            resolution = None
    if resolution is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "could not resolve product — pass product_id or a "
                "more specific product_title."
            ),
        )

    # ── Resolve connection ─────────────────────────────────────
    conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == tenant_id)
        .first()
    )
    if conn is None:
        raise HTTPException(
            status_code=404,
            detail="WhatsAppConnection not found for this tenant.",
        )
    phone_id = str(getattr(conn, "phone_number_id", "") or "").strip()
    if not phone_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "WhatsAppConnection missing phone_number_id. Complete "
                "the WhatsApp onboarding before testing catalog sends."
            ),
        )

    attachment: Dict[str, Any] = {
        "kind":         "product_card",
        "id":           resolution.id,
        "title":        resolution.title,
        "media_type":   "image",
        "file_url":     resolution.image_url or "",
        "caption":      format_product_card_caption(resolution),
        "product_url":  resolution.product_url or "",
        "price":        resolution.price,
        "in_stock":     resolution.in_stock,
        "external_id":  resolution.external_id,
        "confidence":   resolution.confidence,
    }
    retailer_id = effective_retailer_id(attachment)
    elig = is_catalog_eligible(conn, products=[attachment])

    audit_doc = new_delivery_audit()
    audit_doc["text_sent"] = True
    catalog_block: Dict[str, Any] = {
        "eligible":  elig.ok,
        "reason":    elig.reason,
        "attempted": False,
        "succeeded": False,
        "raw_error": None,
    }
    image_cta_block: Dict[str, Any] = {
        "attempted": False, "image_ok": False, "cta_ok": False,
    }
    cta_only_block: Dict[str, Any] = {"attempted": False, "ok": False}

    if requested_mode in ("auto", "catalog"):
        catalog_block["attempted"] = True
        try:
            catalog_block["succeeded"] = await _try_send_catalog_product(
                db=db, connection=conn,
                tenant_id=tenant_id,
                phone_id=phone_id, to=to,
                attachment=attachment,
            )
            if catalog_block["succeeded"]:
                audit_doc["catalog_card_sent_count"] = 1
        except Exception as exc:  # noqa: BLE001
            catalog_block["raw_error"] = repr(exc)
            catalog_block["succeeded"] = False

    if (
        requested_mode == "image"
        or (requested_mode == "auto" and not catalog_block["succeeded"])
    ):
        if attachment.get("file_url"):
            image_cta_block["attempted"] = True
            try:
                image_cta_block["image_ok"] = await _send_media_message(
                    phone_id=phone_id, to=to,
                    media_type="image",
                    media_url=attachment["file_url"],
                    filename=None,
                    caption=attachment.get("caption"),
                    _tenant_id=tenant_id, _db=db,
                )
                if image_cta_block["image_ok"]:
                    audit_doc["legacy_media_sent_count"] = 1
            except Exception as exc:  # noqa: BLE001
                image_cta_block["raw_error"] = repr(exc)
            if image_cta_block["image_ok"] and attachment.get("product_url"):
                try:
                    image_cta_block["cta_ok"] = await _send_cta_url(
                        phone_id=phone_id, to=to,
                        body_text="اضغط زر «عرض المنتج» للمتابعة.",
                        btn_label="عرض المنتج",
                        btn_url=attachment["product_url"],
                        _tenant_id=tenant_id, _db=db,
                    )
                    if image_cta_block["cta_ok"]:
                        audit_doc["cta_url_sent_count"] = (
                            int(audit_doc.get("cta_url_sent_count", 0)) + 1
                        )
                except Exception as exc:  # noqa: BLE001
                    image_cta_block["raw_error"] = repr(exc)

    if (
        requested_mode == "cta"
        or (
            requested_mode == "auto"
            and not catalog_block["succeeded"]
            and not image_cta_block.get("image_ok")
        )
    ):
        if attachment.get("product_url"):
            cta_only_block["attempted"] = True
            try:
                cta_only_block["ok"] = await _send_cta_url(
                    phone_id=phone_id, to=to,
                    body_text=attachment.get("title") or "عرض المنتج",
                    btn_label="عرض المنتج",
                    btn_url=attachment["product_url"],
                    _tenant_id=tenant_id, _db=db,
                )
                if cta_only_block["ok"]:
                    audit_doc["cta_url_sent_count"] = (
                        int(audit_doc.get("cta_url_sent_count", 0)) + 1
                    )
            except Exception as exc:  # noqa: BLE001
                cta_only_block["raw_error"] = repr(exc)

    final_mode = compute_final_delivery_mode(audit_doc)

    return {
        "ok": (
            catalog_block["succeeded"]
            or image_cta_block.get("image_ok")
            or cta_only_block.get("ok")
        ),
        "tenant_id":      tenant_id,
        "to_masked":      to_masked,
        "mode_requested": requested_mode,
        "product": {
            "id":          resolution.id,
            "title":       resolution.title,
            "external_id": resolution.external_id,
            "retailer_id": retailer_id or None,
            "image_url":   bool(resolution.image_url),
            "product_url": bool(resolution.product_url),
        },
        "catalog":      catalog_block,
        "image_cta":    image_cta_block,
        "cta_only":     cta_only_block,
        "final_mode":   final_mode,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Merchant endpoints
# ─────────────────────────────────────────────────────────────────────────────

def _enforce_catalog_feature(db: Session, tenant_id: int) -> None:
    """Plan-gate the catalog feature for merchant routes. Admins
    bypass this — they manage configuration on behalf of merchants
    regardless of plan."""
    try:
        ent = get_entitlements(db, tenant_id)
        require_feature(ent, _FEATURE_KEY)
    except EntitlementError as exc:
        entitlement_http_error(exc)


@merchant_router.get("/status")
async def merchant_catalog_status(
    request: Request,
    db: Session = Depends(get_db),
    _user: Dict[str, Any] = Depends(get_current_user),
):
    """Return the current catalog configuration + eligibility for
    the calling merchant. No plan gate on READ so the merchant can
    see upgrade hints even when the feature is locked.
    """
    tenant_id = resolve_tenant_id(request)
    return _status_payload(db, tenant_id)


@merchant_router.patch("/config")
async def merchant_catalog_patch(
    body: CatalogConfigPatch,
    request: Request,
    db: Session = Depends(get_db),
    _user: Dict[str, Any] = Depends(get_current_user),
):
    """Update ``meta_catalog_id`` / ``catalog_enabled`` for the
    calling merchant. Tenant-id derived ONLY from the JWT — request
    body fields are never trusted for tenant scoping.
    """
    tenant_id = resolve_tenant_id(request)
    _enforce_catalog_feature(db, tenant_id)

    conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == tenant_id)
        .first()
    )
    if conn is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "لم يتم ربط واتساب بعد. أكمل الربط أولاً ثم أعد "
                "ضبط الكتالوج."
            ),
        )

    changes = _apply_config_changes(conn, body)
    if changes:
        db.commit()
        db.refresh(conn)

    audit(
        "merchant_catalog_config",
        tenant_id=tenant_id,
        changes=changes,
    )
    logger.info(
        "[CATALOG_CONFIG] surface=merchant tenant=%s changes=%s",
        tenant_id, changes,
    )

    return {
        "ok":              True,
        "applied_changes": changes,
        "status":          _status_payload(db, tenant_id),
    }


@merchant_router.post("/test-send")
async def merchant_catalog_test_send(
    body: CatalogTestSendBody,
    request: Request,
    db: Session = Depends(get_db),
    _user: Dict[str, Any] = Depends(get_current_user),
):
    """Send a single product to a test recipient through the full
    dispatch chain. Plan-gated."""
    tenant_id = resolve_tenant_id(request)
    _enforce_catalog_feature(db, tenant_id)

    result = await _run_test_send(db, tenant_id, body)
    audit(
        "merchant_catalog_test_send",
        tenant_id=tenant_id,
        product_id=result["product"]["id"],
        final_mode=result["final_mode"],
    )
    logger.info(
        "[CATALOG_TEST_SEND] surface=merchant tenant=%s product=%s mode=%s",
        tenant_id, result["product"]["id"], result["final_mode"],
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Admin endpoints — require_admin, cross-tenant
# ─────────────────────────────────────────────────────────────────────────────

@admin_router.get("/status")
async def admin_catalog_status(
    tenant_id: int = Query(..., ge=1, description="Tenant to inspect."),
    sample: int = Query(5, ge=1, le=25),
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Same shape as merchant /status but targeted at any tenant."""
    return _status_payload(db, tenant_id, sample=sample)


@admin_router.get("/audit")
async def admin_catalog_audit(
    limit: int = Query(200, ge=1, le=1000),
    only_connected: bool = Query(
        default=True,
        description="When true, exclude tenants with no WhatsAppConnection row.",
    ),
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Cross-tenant audit listing.

    For every tenant (optionally only those with a WhatsApp
    connection) returns a compact row with catalog config, the
    eligibility verdict, and product retailer-id coverage. Powers
    the admin dashboard table for support to spot misconfigured
    catalogs in bulk.
    """
    from models import Product as _Product  # noqa: PLC0415

    rows = (
        db.query(Tenant, WhatsAppConnection)
        .outerjoin(
            WhatsAppConnection,
            WhatsAppConnection.tenant_id == Tenant.id,
        )
        .order_by(Tenant.id)
        .limit(int(limit))
        .all()
    )

    out: List[Dict[str, Any]] = []
    for tenant, conn in rows:
        if only_connected and conn is None:
            continue
        summary = catalog_summary(conn)
        elig = is_catalog_eligible(conn, products=None)

        # Cheap coverage approximation: count products with a
        # non-null external_id (meta_retailer_id COALESCE external_id
        # is the resolution rule so external_id presence is a strong
        # proxy without a per-row Python loop).
        try:
            total = (
                db.query(_Product)
                .filter(_Product.tenant_id == tenant.id)
                .count()
            )
            with_rid = (
                db.query(_Product)
                .filter(_Product.tenant_id == tenant.id)
                .filter(
                    (_Product.meta_retailer_id.isnot(None))
                    | (_Product.external_id.isnot(None))
                )
                .count()
            )
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            total, with_rid = 0, 0

        out.append({
            "tenant_id":              tenant.id,
            "merchant_name":          getattr(tenant, "merchant_name", None)
                                      or getattr(tenant, "store_name", None)
                                      or getattr(tenant, "name", None),
            "whatsapp_connected":     conn is not None and (
                getattr(conn, "status", "") == "connected"
            ),
            "catalog_enabled":        bool(summary["catalog_enabled"]),
            "meta_catalog_id_set":    bool(summary["meta_catalog_id"]),
            "eligibility_ok":         bool(elig.ok),
            "eligibility_reason":     elig.reason,
            "products_total":         int(total),
            "products_with_rid":      int(with_rid),
            "products_with_rid_pct":  round(
                (with_rid / total * 100.0) if total else 0.0, 1,
            ),
        })

    return {"rows": out, "count": len(out)}


@admin_router.patch("/config")
async def admin_catalog_patch(
    body: AdminCatalogConfigPatch,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Admin write — sets catalog config for the target tenant.
    Bypasses the merchant plan gate so support can prepare
    configuration during onboarding regardless of plan state."""
    conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == body.tenant_id)
        .first()
    )
    if conn is None:
        raise HTTPException(
            status_code=404,
            detail=f"WhatsAppConnection not found for tenant_id={body.tenant_id}",
        )

    changes = _apply_config_changes(conn, body)
    if changes:
        db.commit()
        db.refresh(conn)

    audit(
        "admin_catalog_config",
        tenant_id=body.tenant_id,
        changes=changes,
    )
    logger.info(
        "[CATALOG_CONFIG] surface=admin tenant=%s changes=%s",
        body.tenant_id, changes,
    )

    return {
        "ok":              True,
        "applied_changes": changes,
        "status":          _status_payload(db, body.tenant_id),
    }


@admin_router.post("/test-send")
async def admin_catalog_test_send(
    body: AdminCatalogTestSendBody,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Admin test send — same chain as merchant but cross-tenant."""
    result = await _run_test_send(db, body.tenant_id, body)
    audit(
        "admin_catalog_test_send",
        tenant_id=body.tenant_id,
        product_id=result["product"]["id"],
        final_mode=result["final_mode"],
    )
    logger.info(
        "[CATALOG_TEST_SEND] surface=admin tenant=%s product=%s mode=%s",
        body.tenant_id, result["product"]["id"], result["final_mode"],
    )
    return result


__all__ = ["merchant_router", "admin_router"]
