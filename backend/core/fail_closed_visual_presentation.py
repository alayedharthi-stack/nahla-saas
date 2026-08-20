"""
core/fail_closed_visual_presentation.py
──────────────────────────────────────
Family 3 R2-B — visual/title fallback after Meta membership fail-closed.

AD-F3-R2B-1
    After canonical Meta membership fails closed, fallback presentation
    must stay bound to the same structured Product / ProductVariant
    identity. If that referent has no safe local visual/title, fail
    closed. Do not invent another SKU, do not title-FTS substitute,
    and do not use customer wording as a production routing rule.

This module is the presentation owner for that path. It does not
authorize native Meta catalog send (R2-A / MetaCatalogMembership)
and does not select products for unstructured visual asks (AI-D02).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from core.native_catalog_capability import (
    REASON_CATALOG_ID_MISMATCH,
    REASON_CATALOG_ID_MISSING,
    REASON_META_CATALOG_UNPUBLISHED,
    REASON_META_CATALOG_UNVERIFIED,
    REASON_SKU_ONLY_RETAILER_ID,
    REASON_SYNTHETIC_RETAILER_ID,
    REASON_VARIANT_MAPPING_MISSING,
)

MEMBERSHIP_FAIL_CLOSED_REASONS = frozenset({
    REASON_META_CATALOG_UNVERIFIED,
    REASON_META_CATALOG_UNPUBLISHED,
    REASON_CATALOG_ID_MISMATCH,
    REASON_CATALOG_ID_MISSING,
    REASON_VARIANT_MAPPING_MISSING,
    REASON_SYNTHETIC_RETAILER_ID,
    REASON_SKU_ONLY_RETAILER_ID,
})

SOURCE_STRUCTURED_IDENTITY = "structured_product_identity"
REASON_BOUND = "bound_structured_identity"
REASON_NO_SAFE_VISUAL = "no_safe_bound_visual"
REASON_UNBOUND = "no_structured_identity"
REASON_TENANT_MISS = "structured_identity_not_in_tenant"
REASON_VARIANT_MISS = "structured_variant_not_in_tenant"
_VARIANT_ID_KEYS = (
    "canonical_variant_id",
    "picked_variant_id",
    "selected_variant_id",
    "variant_id",
)


@dataclass(frozen=True)
class StructuredVisualBind:
    """Outcome of binding a visual/title fallback to a canonical referent."""

    canonical_present: bool
    product_id: Optional[int] = None
    variant_id: Optional[int] = None
    external_id: str = ""
    title: str = ""
    image_url: str = ""
    product_url: str = ""
    in_stock: bool = True
    reason: str = REASON_UNBOUND
    source: str = SOURCE_STRUCTURED_IDENTITY

    @property
    def has_safe_visual(self) -> bool:
        return bool(self.image_url or self.product_url)

    @property
    def allow_presentation(self) -> bool:
        return self.canonical_present and self.has_safe_visual


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_structured_product_id(
    *sources: Any,
) -> Tuple[Optional[int], str]:
    """Return ``(product_id, external_id)`` from structured fields only.

    Title / name / inbound text are not identity. First structured hit wins.
    """
    for source in sources:
        if source is None:
            continue
        rows: Sequence[Any]
        if isinstance(source, Mapping):
            rows = (source,)
        elif isinstance(source, (list, tuple)):
            rows = source
        else:
            rows = ()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            pid = _optional_int(row.get("id") if row.get("id") not in (None, "") else row.get("product_id"))
            ext = str(row.get("external_id") or "").strip()
            if pid is not None or ext:
                return pid, ext
    return None, ""


def extract_structured_variant_id(*sources: Any) -> Optional[int]:
    """Return a bound variant id from explicit variant fields only.

    Parent ``id`` / ``product_id`` are not variant identity.
    """
    for source in sources:
        if source is None:
            continue
        rows: Sequence[Any]
        if isinstance(source, Mapping):
            rows = (source,)
        elif isinstance(source, (list, tuple)):
            rows = source
        else:
            rows = ()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            for key in _VARIANT_ID_KEYS:
                vid = _optional_int(row.get(key))
                if vid is not None:
                    return vid
    return None


def is_membership_fail_closed(decision: Any) -> bool:
    """True when native Meta send was denied by canonical membership/capability."""
    if decision is None:
        return False
    diagnostics = getattr(decision, "diagnostics", None)
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    if diagnostics.get("membership_fail_closed") is True:
        return True
    if diagnostics.get("native_catalog_available") is False:
        return True
    reason = str(getattr(decision, "reason", "") or "").strip()
    return reason in MEMBERSHIP_FAIL_CLOSED_REASONS


def stamp_membership_fail_closed(
    audit: Optional[Dict[str, Any]],
    decision: Any,
) -> None:
    """Record fail-closed identity on the caller-owned delivery audit."""
    if not isinstance(audit, dict) or not is_membership_fail_closed(decision):
        return
    diagnostics = getattr(decision, "diagnostics", None)
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    audit["membership_fail_closed"] = True
    canonical = diagnostics.get("canonical_product_id")
    if canonical is not None and canonical != "":
        audit["canonical_product_id"] = canonical
    variant = diagnostics.get("canonical_variant_id")
    if variant is not None and variant != "":
        audit["canonical_variant_id"] = variant


def should_block_title_query_substitution(
    *,
    membership_fail_closed: bool = False,
    canonical_product_id: Optional[int] = None,
    canonical_external_id: str = "",
    canonical_variant_id: Optional[int] = None,
) -> bool:
    """Title-FTS / inbound-text rescue must not run when a referent is already known."""
    if membership_fail_closed:
        return True
    if canonical_product_id is not None:
        return True
    if canonical_variant_id is not None:
        return True
    return bool(str(canonical_external_id or "").strip())


def resolved_sku_matches_canonical(
    *,
    canonical_product_id: Optional[int],
    resolved_product_id: Optional[int],
    canonical_variant_id: Optional[int] = None,
    resolved_variant_id: Optional[int] = None,
) -> bool:
    if canonical_product_id is None or resolved_product_id is None:
        return False
    if int(canonical_product_id) != int(resolved_product_id):
        return False
    if canonical_variant_id is None:
        return True
    if resolved_variant_id is None:
        return False
    return int(canonical_variant_id) == int(resolved_variant_id)


def bind_from_local_facts(
    *,
    product_id: Optional[int] = None,
    variant_id: Optional[int] = None,
    external_id: str = "",
    title: str = "",
    image_url: str = "",
    product_url: str = "",
    in_stock: bool = True,
) -> StructuredVisualBind:
    """Bind using already-loaded tenant-scoped facts — no title search."""
    ext = str(external_id or "").strip()
    if product_id is None and not ext:
        return StructuredVisualBind(canonical_present=False, reason=REASON_UNBOUND)
    image = str(image_url or "").strip()
    url = str(product_url or "").strip()
    safe = bool(image or url)
    return StructuredVisualBind(
        canonical_present=True,
        product_id=product_id,
        variant_id=variant_id,
        external_id=ext,
        title=str(title or "").strip(),
        image_url=image,
        product_url=url,
        in_stock=bool(in_stock),
        reason=REASON_BOUND if safe else REASON_NO_SAFE_VISUAL,
        source=SOURCE_STRUCTURED_IDENTITY,
    )


def load_bound_variant_facts(
    db: Any,
    *,
    tenant_id: int,
    product_id: int,
    variant_id: int,
) -> Optional[Dict[str, Any]]:
    """Tenant-scoped exact variant belonging to *product_id*. Never a sibling SKU."""
    if db is None or not tenant_id:
        return None
    try:
        from models import ProductVariant  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        try:
            from database.models import ProductVariant  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return None
    try:
        row = (
            db.query(ProductVariant)
            .filter(
                ProductVariant.tenant_id == int(tenant_id),
                ProductVariant.id == int(variant_id),
                ProductVariant.product_id == int(product_id),
            )
            .first()
        )
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    summary = str(getattr(row, "option_summary", "") or "").strip()
    return {
        "variant_id": int(getattr(row, "id")),
        "image_url": str(getattr(row, "image_url", "") or "").strip(),
        "option_summary": summary,
        "in_stock": bool(getattr(row, "in_stock", True)),
        "is_default": bool(getattr(row, "is_default", False)),
    }


def bind_structured_visual_referent(
    db: Any,
    tenant_id: int,
    *,
    brain_state: Optional[Mapping[str, Any]] = None,
    attachments: Optional[Iterable[Any]] = None,
    audit: Optional[Mapping[str, Any]] = None,
) -> StructuredVisualBind:
    """Resolve the canonical referent by structured id only (tenant-scoped).

    Does not call title FTS, relaxed substring match, or inbound-text rescue.
    """
    bs = brain_state if isinstance(brain_state, Mapping) else {}
    focus = bs.get("current_product_focus") if isinstance(bs.get("current_product_focus"), Mapping) else {}
    audit_map = audit if isinstance(audit, Mapping) else {}
    pid, ext = extract_structured_product_id(
        {"id": audit_map.get("canonical_product_id")} if audit_map.get("canonical_product_id") not in (None, "") else None,
        focus,
        list(attachments or []),
    )
    variant_id = extract_structured_variant_id(
        audit_map,
        focus,
        list(attachments or []),
    )
    if pid is None and not ext:
        return StructuredVisualBind(canonical_present=False, reason=REASON_UNBOUND)

    if db is None or not tenant_id:
        return bind_from_local_facts(
            product_id=pid,
            variant_id=variant_id,
            external_id=ext,
            title=str(focus.get("title") or "").strip(),
            image_url=str(
                focus.get("variant_image_url")
                or (focus.get("image_url") if variant_id is None else "")
                or focus.get("image")
                or ""
            ).strip(),
            product_url=str(focus.get("product_url") or "").strip(),
        )

    try:
        from services.product_resolver import (  # noqa: PLC0415
            resolve_by_external_id,
            resolve_by_product_id,
        )
    except Exception:  # noqa: BLE001
        return StructuredVisualBind(
            canonical_present=True,
            product_id=pid,
            variant_id=variant_id,
            external_id=ext,
            reason=REASON_TENANT_MISS,
            source=SOURCE_STRUCTURED_IDENTITY,
        )

    resolved = None
    if pid is not None:
        resolved = resolve_by_product_id(db, int(tenant_id), pid)
    if resolved is None and ext:
        resolved = resolve_by_external_id(db, int(tenant_id), ext)
        if resolved is not None and pid is not None and int(resolved.id) != int(pid):
            # External id must not silently retarget a different SKU.
            resolved = None

    if resolved is None:
        return StructuredVisualBind(
            canonical_present=True,
            product_id=pid,
            variant_id=variant_id,
            external_id=ext,
            reason=REASON_TENANT_MISS,
            source=SOURCE_STRUCTURED_IDENTITY,
        )

    parent_title = str(resolved.title or "")
    parent_image = str(resolved.image_url or "")
    parent_url = str(resolved.product_url or "")
    if variant_id is None:
        return bind_from_local_facts(
            product_id=int(resolved.id),
            external_id=str(resolved.external_id or ext),
            title=parent_title,
            image_url=parent_image,
            product_url=parent_url,
            in_stock=bool(resolved.in_stock),
        )

    variant = load_bound_variant_facts(
        db,
        tenant_id=int(tenant_id),
        product_id=int(resolved.id),
        variant_id=int(variant_id),
    )
    if variant is None:
        return StructuredVisualBind(
            canonical_present=True,
            product_id=int(resolved.id),
            variant_id=int(variant_id),
            external_id=str(resolved.external_id or ext),
            reason=REASON_VARIANT_MISS,
            source=SOURCE_STRUCTURED_IDENTITY,
        )
    summary = str(variant.get("option_summary") or "").strip()
    title = f"{parent_title} — {summary}" if summary and parent_title else (summary or parent_title)
    if variant.get("is_default"):
        image = str(variant.get("image_url") or parent_image or "").strip()
    else:
        # Non-default variants must not inherit a parent photo (often another color/size).
        image = str(variant.get("image_url") or "").strip()
    return bind_from_local_facts(
        product_id=int(resolved.id),
        variant_id=int(variant["variant_id"]),
        external_id=str(resolved.external_id or ext),
        title=title,
        image_url=image,
        product_url=parent_url,
        in_stock=bool(variant.get("in_stock", True)),
    )


__all__ = [
    "MEMBERSHIP_FAIL_CLOSED_REASONS",
    "REASON_BOUND",
    "REASON_NO_SAFE_VISUAL",
    "REASON_TENANT_MISS",
    "REASON_UNBOUND",
    "REASON_VARIANT_MISS",
    "SOURCE_STRUCTURED_IDENTITY",
    "StructuredVisualBind",
    "bind_from_local_facts",
    "bind_structured_visual_referent",
    "extract_structured_product_id",
    "extract_structured_variant_id",
    "is_membership_fail_closed",
    "load_bound_variant_facts",
    "resolved_sku_matches_canonical",
    "should_block_title_query_substitution",
    "stamp_membership_fail_closed",
]
