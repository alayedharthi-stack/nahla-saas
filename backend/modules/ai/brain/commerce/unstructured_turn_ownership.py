"""Unstructured NL ownership — Brain owns meaning; platform owns facts/execution.

Pre-Brain skip_brain customer-visible replies are allowed only when intent is
already explicit in a machine payload or a structural slot token that does
not require language interpretation.

Merchant/integration type must not change this boundary. Salla vs non-Salla
may differ in facts and capabilities, not in whether natural language reaches
Brain.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from modules.ai.brain.commerce.payment_execution_ownership import (
    is_structurally_explicit_inbound,
)

UNSTRUCTURED_REQUIRES_BRAIN_REASON = "unstructured_requires_brain_semantic_ownership"

_STRUCTURED_SLOT_TYPES = frozenset({
    "interactive",
    "button",
    "location",
    "location_pin",
    "whatsapp_location",
    "order",
    "catalog_order",
    "nfm_reply",
    "list_reply",
})

# National address short code as the entire inbound — machine token, not a sentence.
_NATIONAL_SHORT_CODE_RE = re.compile(r"^[A-Za-z]{4}\d{4}$")


def _meta(inbound_metadata: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    return inbound_metadata if isinstance(inbound_metadata, Mapping) else {}


def _normalized_type(
    inbound_metadata: Optional[Mapping[str, Any]] = None,
    *,
    normalized_type: Optional[str] = None,
) -> str:
    meta = _meta(inbound_metadata)
    return str(
        normalized_type
        or meta.get("normalized_type")
        or meta.get("inbound_normalized_type")
        or meta.get("source_type")
        or meta.get("type")
        or ""
    ).strip().lower()


def _has_location_pin(inbound_metadata: Optional[Mapping[str, Any]] = None) -> bool:
    meta = _meta(inbound_metadata)
    if meta.get("latitude") is not None and meta.get("longitude") is not None:
        return True
    loc = meta.get("location")
    if isinstance(loc, Mapping) and loc.get("latitude") is not None and loc.get("longitude") is not None:
        return True
    nested = meta.get("normalized_inbound")
    if isinstance(nested, Mapping):
        if nested.get("source_type") == "location":
            return True
        nloc = nested.get("location")
        if isinstance(nloc, Mapping) and nloc.get("latitude") is not None:
            return True
    return False


def _has_catalog_order_structure(
    inbound_metadata: Optional[Mapping[str, Any]] = None,
    *,
    message: str = "",
) -> bool:
    """True for native catalog-order payloads or the platform catalog-order dump."""
    meta = _meta(inbound_metadata)
    ntype = _normalized_type(inbound_metadata)
    if ntype in {"catalog_order", "order"}:
        if meta.get("product_items") or meta.get("order") or meta.get("catalog_order"):
            return True
    if meta.get("catalog_order_submitted"):
        return True
    text = str(message or "")
    # Platform-generated catalog-order dump (not customer phrasing).
    if "[طلب كتالوج من العميل]" in text:
        return bool(
            meta.get("product_items")
            or meta.get("order")
            or "رمز المنتج (SKU):" in text
        )
    return False


def inbound_is_machine_or_structural_slot(
    inbound_metadata: Optional[Mapping[str, Any]] = None,
    *,
    normalized_type: Optional[str] = None,
    message: str = "",
) -> bool:
    """True when intent is already explicit without language interpretation."""
    if is_structurally_explicit_inbound(
        inbound_metadata, normalized_type=normalized_type,
    ):
        return True
    ntype = _normalized_type(inbound_metadata, normalized_type=normalized_type)
    if ntype in _STRUCTURED_SLOT_TYPES:
        return True
    if _has_location_pin(inbound_metadata):
        return True
    if _has_catalog_order_structure(inbound_metadata, message=message or ""):
        return True
    text = str(message or "").strip()
    if text and _NATIONAL_SHORT_CODE_RE.match(text):
        return True
    return False


def unstructured_natural_language_requires_brain(
    inbound_metadata: Optional[Mapping[str, Any]] = None,
    *,
    normalized_type: Optional[str] = None,
    message: str = "",
) -> bool:
    """True for free-text (and media) turns that Brain must own semantically."""
    return not inbound_is_machine_or_structural_slot(
        inbound_metadata,
        normalized_type=normalized_type,
        message=message,
    )


def ofv2_may_own_prebrain(
    inbound_metadata: Optional[Mapping[str, Any]] = None,
    *,
    normalized_type: Optional[str] = None,
    message: str = "",
) -> bool:
    """OrderFlowV2 may skip Brain only for machine/structural slot payloads."""
    return inbound_is_machine_or_structural_slot(
        inbound_metadata,
        normalized_type=normalized_type,
        message=message,
    )


def prebrain_skip_brain_reply_allowed(
    inbound_metadata: Optional[Mapping[str, Any]] = None,
    *,
    normalized_type: Optional[str] = None,
    message: str = "",
) -> bool:
    """Customer-visible pre-Brain replies are allowed only for structural inbound."""
    return inbound_is_machine_or_structural_slot(
        inbound_metadata,
        normalized_type=normalized_type,
        message=message,
    )


__all__ = [
    "UNSTRUCTURED_REQUIRES_BRAIN_REASON",
    "inbound_is_machine_or_structural_slot",
    "ofv2_may_own_prebrain",
    "prebrain_skip_brain_reply_allowed",
    "unstructured_natural_language_requires_brain",
]
