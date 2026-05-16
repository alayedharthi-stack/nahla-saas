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
    KNOWN_SOURCES,
    SOURCE_MANUAL,
    SOURCE_UNKNOWN,
    assign_canonical_retailer_id,
    canonical_retailer_id,
    catalog_summary,
    dominant_source,
    effective_retailer_id,
    is_catalog_eligible,
    product_source,
    source_breakdown,
)
from core.database import get_db
from core.plan_entitlements import (
    EntitlementError,
    entitlement_http_error,
    get_entitlements,
    require_feature,
)
from core.tenant import resolve_tenant_id
from models import Product, Tenant, WhatsAppConnection
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
        resolve_best_effort,
        resolve_by_external_id,
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
            # Use the best-effort resolver here: the merchant is
            # testing whether their catalog renders correctly, and
            # filtering out OUT-OF-STOCK products would make the
            # test feel broken from the merchant's perspective.
            resolution = resolve_best_effort(
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


# ─────────────────────────────────────────────────────────────────────────────
# Product mapping endpoints (May 2026 #12)
# ─────────────────────────────────────────────────────────────────────────────
#
# The status endpoint only ships a 5-row sample of products. Operators
# need the full mapping picture when retailer_id coverage shows
# anomalies — these endpoints give that. ``GET .../catalog/products``
# returns a paged list; ``POST .../catalog/resync`` walks every row
# and writes a canonical ``meta_retailer_id`` wherever one is missing
# so the catalog send chain stops bailing out with ``no_retailer_id``.


def _product_diag_rows(
    db: Session, tenant_id: int, *, limit: int, offset: int,
) -> Dict[str, Any]:
    """Build the response shape for ``GET /merchant/catalog/products``.

    Shared between merchant + admin (admin endpoint just plumbs a
    different tenant_id). Returns:

      {
        "rows": [ {id, title, external_id, meta_retailer_id,
                   effective_retailer_id, publish_status}, ... ],
        "total": int,
        "limit": int, "offset": int,
        "coverage": {with_rid, missing_rid, published, unpublished, total},
      }

    ``publish_status`` is the three-state token operators read:

      * ``published``    — has ``meta_catalog_published_at``
                           (catalog send chain has fired at least once
                           successfully against this row).
      * ``ready``        — has a usable retailer id but the catalog
                           send chain has not been exercised yet.
      * ``needs_mapping``— missing both ``meta_retailer_id`` AND
                           ``external_id``. The resync endpoint will
                           assign a synthetic id so the row at least
                           reaches the legacy image+CTA fallback.
    """
    from models import Product as _Product  # noqa: PLC0415

    total = (
        db.query(_Product)
        .filter(_Product.tenant_id == tenant_id)
        .count()
    )

    rows = (
        db.query(_Product)
        .filter(_Product.tenant_id == tenant_id)
        .order_by(_Product.id)
        .limit(int(limit))
        .offset(int(offset))
        .all()
    )

    out_rows: List[Dict[str, Any]] = []
    for p in rows:
        eff = effective_retailer_id(p)
        published_at = getattr(p, "meta_catalog_published_at", None)
        if published_at:
            status = "published"
        elif eff:
            status = "ready"
        else:
            status = "needs_mapping"
        out_rows.append({
            "id":                    p.id,
            "title":                 p.title,
            "external_id":           getattr(p, "external_id", None),
            "meta_retailer_id":      getattr(p, "meta_retailer_id", None),
            "effective_retailer_id": eff or None,
            "publish_status":        status,
            "in_stock":              bool(getattr(p, "in_stock", True)),
            # Surface the product source so the dashboard table can
            # render a Salla / Manual / Unknown badge per row without
            # a second round-trip. Reads ``Product.source`` (column,
            # post-migration 0062) with JSONB + heuristic fallback —
            # see ``core.catalog.product_source`` for the contract.
            "source":                product_source(p),
        })

    # Cheap counts via SQL — same dual-column predicate as the audit
    # endpoint so the dashboard's "tedded" view matches the table view.
    try:
        with_rid = (
            db.query(_Product)
            .filter(_Product.tenant_id == tenant_id)
            .filter(
                (_Product.meta_retailer_id.isnot(None))
                | (_Product.external_id.isnot(None))
            )
            .count()
        )
        published = (
            db.query(_Product)
            .filter(_Product.tenant_id == tenant_id)
            .filter(_Product.meta_catalog_published_at.isnot(None))
            .count()
        )
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        with_rid, published = 0, 0

    return {
        "rows":   out_rows,
        "total":  int(total),
        "limit":  int(limit),
        "offset": int(offset),
        "coverage": {
            "with_rid":    int(with_rid),
            "missing_rid": int(total) - int(with_rid),
            "published":   int(published),
            "unpublished": int(total) - int(published),
            "total":       int(total),
        },
    }


def _run_catalog_resync(db: Session, tenant_id: int) -> Dict[str, Any]:
    """Backfill ``meta_retailer_id`` (and stamp
    ``meta_catalog_published_at``) for every product belonging to
    *tenant_id*. Returns a structured report:

      {
        "scanned":            int,    # rows visited
        "retailer_id_set":    int,    # rows that gained a retailer id
        "already_set":        int,    # rows that already had one
        "synthetic_assigned": int,    # rows that got nahla_p_<id>
        "published_stamped":  int,    # meta_catalog_published_at updated
        "errors":             int,
      }

    The operation is idempotent — running it twice in a row yields
    zeros for ``retailer_id_set`` and ``synthetic_assigned`` and the
    ``published_stamped`` count covers only rows whose stamp was
    older than the start of the run.
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    from models import Product as _Product  # noqa: PLC0415

    counters = {
        "scanned":            0,
        "retailer_id_set":    0,
        "already_set":        0,
        "synthetic_assigned": 0,
        "published_stamped":  0,
        "errors":             0,
    }
    now = datetime.now(timezone.utc)

    rows = (
        db.query(_Product)
        .filter(_Product.tenant_id == tenant_id)
        .order_by(_Product.id)
        .all()
    )
    counters["scanned"] = len(rows)

    for p in rows:
        try:
            previously_set = bool(
                (getattr(p, "meta_retailer_id", None) or "") and
                str(getattr(p, "meta_retailer_id", "") or "").strip()
            )
            if previously_set:
                counters["already_set"] += 1
            else:
                assigned = assign_canonical_retailer_id(p)
                if assigned:
                    counters["retailer_id_set"] += 1
                    new_val = str(p.meta_retailer_id or "")
                    if new_val.startswith("nahla_p_"):
                        counters["synthetic_assigned"] += 1

            # Stamp publish marker only when we actually have a
            # usable retailer id. The webhook send path is the
            # canonical place to mark success on real Meta sends,
            # but we mirror it here so the dashboard's "published"
            # counter starts useful before the first chat happens.
            if getattr(p, "meta_retailer_id", None) and getattr(
                p, "meta_catalog_published_at", None,
            ) is None:
                p.meta_catalog_published_at = now
                counters["published_stamped"] += 1
        except Exception as exc:  # noqa: BLE001
            counters["errors"] += 1
            logger.warning(
                "[CATALOG_RESYNC] tenant=%s product_id=%s failed=%r",
                tenant_id, getattr(p, "id", "?"), exc,
            )

    try:
        db.commit()
    except Exception as commit_exc:  # noqa: BLE001
        db.rollback()
        counters["errors"] += 1
        logger.warning(
            "[CATALOG_RESYNC] tenant=%s commit_failed=%r",
            tenant_id, commit_exc,
        )

    logger.info(
        "[CATALOG_RESYNC] tenant=%s scanned=%d set=%d "
        "synthetic=%d published=%d errors=%d",
        tenant_id,
        counters["scanned"], counters["retailer_id_set"],
        counters["synthetic_assigned"], counters["published_stamped"],
        counters["errors"],
    )
    return counters


# ── Merchant variants ─────────────────────────────────────────────────

@merchant_router.get("/products")
async def merchant_catalog_products(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _user: Dict[str, Any] = Depends(get_current_user),
):
    """List products with their retailer_id mapping state. Read-only;
    no plan gate (a merchant on Starter can inspect their coverage
    before upgrading — gating the READ would be hostile UX)."""
    tenant_id = resolve_tenant_id(request)
    return _product_diag_rows(db, tenant_id, limit=limit, offset=offset)


@merchant_router.post("/resync")
async def merchant_catalog_resync(
    request: Request,
    db: Session = Depends(get_db),
    _user: Dict[str, Any] = Depends(get_current_user),
):
    """Backfill ``meta_retailer_id`` across the merchant's products.

    Plan-gated: this is a write operation that prepares the catalog
    for live sends — same gate as the config / test-send endpoints.
    """
    tenant_id = resolve_tenant_id(request)
    _enforce_catalog_feature(db, tenant_id)
    report = _run_catalog_resync(db, tenant_id)
    audit(
        "merchant_catalog_resync",
        tenant_id=tenant_id,
        scanned=report["scanned"],
        retailer_id_set=report["retailer_id_set"],
    )
    return {"ok": True, "report": report}


# ── Admin variants ────────────────────────────────────────────────────

@admin_router.get("/products")
async def admin_catalog_products(
    tenant_id: int = Query(..., ge=1),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    return _product_diag_rows(db, tenant_id, limit=limit, offset=offset)


class _AdminResyncBody(BaseModel):
    tenant_id: int = Field(..., ge=1)


@admin_router.post("/resync")
async def admin_catalog_resync(
    body: _AdminResyncBody,
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    report = _run_catalog_resync(db, body.tenant_id)
    audit(
        "admin_catalog_resync",
        tenant_id=body.tenant_id,
        scanned=report["scanned"],
        retailer_id_set=report["retailer_id_set"],
    )
    return {"ok": True, "report": report}


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostics — catalog readiness + product source breakdown
# ─────────────────────────────────────────────────────────────────────────────
#
# This endpoint is the single source of truth for the "Catalog status"
# card the dashboard renders at the top of /catalog. It deliberately
# returns a flat, JSON-friendly dict (no Pydantic model — the shape is
# additive and we want to ship new keys without bumping versions) and
# reads EXACTLY the columns the catalog feature actually uses:
#
#   • meta_catalog_id  — Meta side of the integration (per WABA, on
#                        WhatsAppConnection)
#   • catalog_enabled  — per-WABA kill switch
#   • products + their source + their effective_retailer_id coverage
#
# It does NOT inspect Salla integrations, JWT scopes, plan tiers, or
# anything else outside the catalog feature itself. The merchant should
# see a complete picture of "where do my products come from, where will
# they appear, and what's missing" without having to cross-reference
# three other dashboards.


def _diagnostics_payload(db: Session, tenant_id: int) -> Dict[str, Any]:
    """Build the diagnostics payload for *tenant_id*.

    Three sections in the return value:

    ``catalog`` — Meta WhatsApp Catalog state:
        ``catalog_id_present`` (bool), ``catalog_id`` (str or empty),
        ``catalog_enabled`` (bool), ``whatsapp_connected`` (bool — does
        the tenant have a WhatsAppConnection row at all?).

    ``products`` — Nahla Product Catalog state:
        ``total`` (count), ``with_effective_retailer_id`` (count),
        ``without_effective_retailer_id`` (count),
        ``coverage_pct`` (0-100 int), ``source_breakdown`` (dict),
        ``dominant_source`` (one of KNOWN_SOURCES + "mixed").

    ``readiness`` — boolean rollup so the UI can render the big "green
    /amber/red" pill without re-computing the rules:
        ``catalog_ready`` — meta_catalog_id + catalog_enabled + at
        least one product has a retailer_id.

    The payload is intentionally tolerant of NULLs: a tenant with zero
    products + no WhatsApp connection still gets a well-formed response
    with every count = 0 and ``readiness.catalog_ready = false``.
    """
    conn = (
        db.query(WhatsAppConnection)
          .filter(WhatsAppConnection.tenant_id == tenant_id)
          .first()
    )
    catalog_id = (getattr(conn, "meta_catalog_id", None) or "").strip() if conn else ""
    catalog_enabled = bool(getattr(conn, "catalog_enabled", False)) if conn else False
    wa_connected = bool(
        conn
        and getattr(conn, "status", "") == "connected"
        and getattr(conn, "sending_enabled", False)
    )

    # Pull all products in one query — we already filter by tenant so the
    # cost is bounded by the merchant's catalog size, which is the same
    # bound the /products listing endpoint already pays.
    products = (
        db.query(Product)
          .filter(Product.tenant_id == tenant_id)
          .all()
    )
    total = len(products)
    with_rid = sum(1 for p in products if effective_retailer_id(p))
    without_rid = total - with_rid
    coverage_pct = int(round((with_rid / total) * 100)) if total else 0

    breakdown = source_breakdown(products)
    dom = dominant_source(breakdown)

    catalog_ready = (
        bool(catalog_id)
        and catalog_enabled
        and with_rid > 0
    )

    return {
        "catalog": {
            "catalog_id_present":  bool(catalog_id),
            "catalog_id":          catalog_id,
            "catalog_enabled":     catalog_enabled,
            "whatsapp_connected":  wa_connected,
        },
        "products": {
            "total":                          total,
            "with_effective_retailer_id":     with_rid,
            "without_effective_retailer_id":  without_rid,
            "coverage_pct":                   coverage_pct,
            "source_breakdown":               breakdown,
            "dominant_source":                dom,
        },
        "readiness": {
            "catalog_ready":  catalog_ready,
        },
    }


@merchant_router.get("/diagnostics")
async def merchant_catalog_diagnostics(
    request: Request,
    db: Session = Depends(get_db),
    _user: Dict[str, Any] = Depends(get_current_user),
):
    """Source-agnostic catalog diagnostics for the dashboard.

    Read-only, NOT plan-gated — a merchant on Starter should be able to
    see exactly where they stand before being asked to upgrade. The
    response shape is documented on ``_diagnostics_payload``.
    """
    tenant_id = resolve_tenant_id(request)
    return _diagnostics_payload(db, tenant_id)


@admin_router.get("/diagnostics")
async def admin_catalog_diagnostics(
    tenant_id: int = Query(..., ge=1),
    db: Session = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """Admin variant of the merchant diagnostics — same payload, but
    callable for any tenant. Used by support to triage "my products
    don't show up in WhatsApp" tickets without asking the merchant to
    re-share their dashboard."""
    return _diagnostics_payload(db, tenant_id)


# ─────────────────────────────────────────────────────────────────────────────
# Manual products — CRUD for tenants who don't have a synced store
# ─────────────────────────────────────────────────────────────────────────────
#
# Path 3 in the new architecture: a merchant with no Salla / Zid / Shopify
# can still build a Product Catalog inside Nahla by entering rows by
# hand. The same products are then usable everywhere the catalog feature
# already plugs in (WhatsApp catalog send, AI [PRODUCT:...] resolver,
# campaigns).
#
# Contract for manual products:
#   • ``source = "manual"`` is the marker that a sync run MUST NOT
#     overwrite this row even if its ``external_id`` (NULL by default
#     for manual rows) happens to clash with an upstream product. The
#     store_sync upsert at ``services/store_sync.py:_upsert_products``
#     honours this — see the explicit guard there.
#   • ``external_id`` is NULL by default. The merchant CAN provide one
#     if they're entering a row that mirrors a known platform id (e.g.
#     for hand-pre-population before connecting Salla) — same column
#     space, no extra plumbing.
#   • ``meta_retailer_id`` is optional. When NULL,
#     ``assign_canonical_retailer_id`` writes a synthetic
#     ``nahla_p_<id>`` after creation so the row is at least catalog-
#     dispatchable.
#   • Validation: title is required (and non-empty after trim); price
#     is a free-form string (merchant decides currency formatting —
#     same convention as platform-synced rows); image_url + product_url
#     live inside ``extra_metadata`` to match the Salla shape so the
#     resolver / sender don't need a manual-specific branch.


class _ManualProductIn(BaseModel):
    """Create payload for a manual product.

    All fields except ``title`` are optional. We deliberately keep the
    intake permissive — merchants typing on phones make a lot of typos
    and we'd rather accept and let them iterate than 422 them.
    """
    title:            str = Field(..., min_length=1, max_length=512)
    description:      Optional[str] = None
    price:            Optional[str] = Field(None, max_length=64)
    sku:              Optional[str] = Field(None, max_length=128)
    external_id:      Optional[str] = Field(None, max_length=128)
    meta_retailer_id: Optional[str] = Field(None, max_length=255)
    image_url:        Optional[str] = Field(None, max_length=2048)
    product_url:      Optional[str] = Field(None, max_length=2048)
    in_stock:         bool = True
    stock_quantity:   Optional[int] = Field(None, ge=0)


class _ManualProductPatch(BaseModel):
    """Patch payload — every field optional, ``None`` means "leave as is"."""
    title:            Optional[str] = Field(None, min_length=1, max_length=512)
    description:      Optional[str] = None
    price:            Optional[str] = Field(None, max_length=64)
    sku:              Optional[str] = Field(None, max_length=128)
    external_id:      Optional[str] = Field(None, max_length=128)
    meta_retailer_id: Optional[str] = Field(None, max_length=255)
    image_url:        Optional[str] = Field(None, max_length=2048)
    product_url:      Optional[str] = Field(None, max_length=2048)
    in_stock:         Optional[bool] = None
    stock_quantity:   Optional[int] = Field(None, ge=0)


def _serialise_manual_product(p: Product) -> Dict[str, Any]:
    """Render a product row in the shape the dashboard expects.

    Surfaces both top-level columns AND the JSONB fields the catalog
    UI cares about (image_url, product_url) — so callers don't need to
    know that those live in ``extra_metadata``.
    """
    meta = p.extra_metadata or {}
    return {
        "id":               int(p.id),
        "tenant_id":        int(p.tenant_id),
        "title":            p.title,
        "description":      p.description,
        "price":            p.price,
        "sku":              p.sku,
        "external_id":      p.external_id,
        "meta_retailer_id": p.meta_retailer_id,
        "effective_retailer_id": effective_retailer_id(p),
        "in_stock":         bool(p.in_stock),
        "stock_quantity":   p.stock_quantity,
        "source":           product_source(p),
        "image_url":        meta.get("image_url") or "",
        "product_url":      meta.get("product_url") or meta.get("url") or "",
    }


@merchant_router.post("/products/manual", status_code=201)
async def merchant_catalog_create_manual_product(
    payload: _ManualProductIn,
    request: Request,
    db: Session = Depends(get_db),
    _user: Dict[str, Any] = Depends(get_current_user),
):
    """Create a manual product row.

    Tenant isolation: tenant_id is resolved from the JWT, never from
    the body. NOT plan-gated — merchants on Starter need to be able to
    build their catalog before they upgrade to start sending.
    """
    tenant_id = resolve_tenant_id(request)
    # Build the same ``extra_metadata`` shape the Salla sync produces
    # so the resolver / sender don't need a manual-specific branch.
    meta_blob = {
        "source":        SOURCE_MANUAL,
        "image_url":     (payload.image_url or "").strip() or None,
        "product_url":   (payload.product_url or "").strip() or None,
    }
    p = Product(
        tenant_id        = tenant_id,
        title            = payload.title.strip(),
        description      = (payload.description or None),
        price            = (payload.price or None),
        sku              = (payload.sku or None),
        external_id      = (payload.external_id or None) or None,
        meta_retailer_id = (payload.meta_retailer_id or None) or None,
        in_stock         = bool(payload.in_stock),
        stock_quantity   = payload.stock_quantity,
        extra_metadata   = meta_blob,
        source           = SOURCE_MANUAL,
    )
    db.add(p)
    db.flush()
    # Backfill a synthetic retailer id so the row is dispatchable on day
    # one. Honours an explicit ``meta_retailer_id`` if the merchant
    # provided one — see ``assign_canonical_retailer_id`` docstring.
    try:
        assign_canonical_retailer_id(p)
    except Exception:  # noqa: BLE001
        pass
    db.commit()
    db.refresh(p)
    audit(
        "merchant_catalog_create_manual_product",
        tenant_id=tenant_id, product_id=int(p.id), title=p.title[:80],
    )
    return _serialise_manual_product(p)


@merchant_router.patch("/products/manual/{product_id}")
async def merchant_catalog_update_manual_product(
    product_id: int,
    payload: _ManualProductPatch,
    request: Request,
    db: Session = Depends(get_db),
    _user: Dict[str, Any] = Depends(get_current_user),
):
    """Update a manual product. Refuses to touch non-manual rows so a
    misclick can't accidentally edit a Salla-synced product (whose
    fields would get overwritten on the next sync anyway)."""
    tenant_id = resolve_tenant_id(request)
    p = (
        db.query(Product)
          .filter(Product.id == product_id, Product.tenant_id == tenant_id)
          .first()
    )
    if not p:
        raise HTTPException(status_code=404, detail="product_not_found")
    if product_source(p) != SOURCE_MANUAL:
        raise HTTPException(
            status_code=409,
            detail="product_not_manual_cannot_edit_via_manual_endpoint",
        )

    data = payload.model_dump(exclude_unset=True)
    # Top-level columns
    for col in (
        "title", "description", "price", "sku",
        "external_id", "meta_retailer_id",
        "in_stock", "stock_quantity",
    ):
        if col in data:
            setattr(p, col, data[col])
    # JSONB-only fields (image_url / product_url) — merge instead of
    # replacing the whole blob so we don't drop other metadata.
    if "image_url" in data or "product_url" in data:
        meta = dict(p.extra_metadata or {})
        if "image_url" in data:
            meta["image_url"] = (data["image_url"] or "").strip() or None
        if "product_url" in data:
            meta["product_url"] = (data["product_url"] or "").strip() or None
        # Keep the source marker pinned regardless of patch shape.
        meta["source"] = SOURCE_MANUAL
        p.extra_metadata = meta
    db.commit()
    db.refresh(p)
    audit(
        "merchant_catalog_update_manual_product",
        tenant_id=tenant_id, product_id=int(p.id), fields=sorted(data.keys()),
    )
    return _serialise_manual_product(p)


@merchant_router.delete("/products/manual/{product_id}")
async def merchant_catalog_delete_manual_product(
    product_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _user: Dict[str, Any] = Depends(get_current_user),
):
    """Delete a manual product. Refuses on synced rows for the same
    reason ``PATCH`` does — the sync would re-create them on the next
    run anyway, but the audit log + UX is cleaner with an explicit
    failure than a silent revert."""
    tenant_id = resolve_tenant_id(request)
    p = (
        db.query(Product)
          .filter(Product.id == product_id, Product.tenant_id == tenant_id)
          .first()
    )
    if not p:
        raise HTTPException(status_code=404, detail="product_not_found")
    if product_source(p) != SOURCE_MANUAL:
        raise HTTPException(
            status_code=409,
            detail="product_not_manual_cannot_delete_via_manual_endpoint",
        )
    db.delete(p)
    db.commit()
    audit(
        "merchant_catalog_delete_manual_product",
        tenant_id=tenant_id, product_id=int(product_id),
    )
    return {"deleted": True, "id": int(product_id)}


__all__ = ["merchant_router", "admin_router"]
