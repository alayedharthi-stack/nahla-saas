"""
services/product_publication_status.py
──────────────────────────────────────
Separate local field readiness from actual Meta/WhatsApp publication.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from services.channel_specs import CHANNEL_WHATSAPP
from services.product_readiness import compute_for_channel


def _waba_linked_from_status(waba_link_status: Optional[Dict[str, Any]]) -> Optional[bool]:
    if waba_link_status is None:
        return None
    if waba_link_status.get("ok"):
        linked = waba_link_status.get("expected_catalog_linked")
        if linked is None:
            return None
        return bool(linked)
    return None


def build_product_publication_status(
    product: Any,
    *,
    waba_link_status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Publication states — never conflate local readiness with live catalog."""
    wa = compute_for_channel(product, CHANNEL_WHATSAPP)
    data_ready = bool(wa.ready) if wa is not None else False

    sync_status = str(getattr(product, "sync_status", None) or "").strip().lower()
    meta_synced = sync_status == "synced"

    waba_linked = _waba_linked_from_status(waba_link_status)
    if waba_linked is None:
        meta = getattr(product, "extra_metadata", None) or {}
        sync_meta = meta.get("sync_meta") if isinstance(meta, dict) else None
        if isinstance(sync_meta, dict) and "waba_catalog_linked" in sync_meta:
            cached = sync_meta.get("waba_catalog_linked")
            waba_linked = bool(cached) if cached is not None else None

    # Per-item WhatsApp storefront display cannot be read through the API.
    # ``visible_in_whatsapp`` stays False (never claimed); the evidence block
    # says what *is* proven so the UI can be honest instead of alarming.
    visible = False
    sync_meta_block = getattr(product, "extra_metadata", None) or {}
    sync_meta_block = sync_meta_block.get("sync_meta") if isinstance(sync_meta_block, dict) else None
    sync_meta_block = sync_meta_block if isinstance(sync_meta_block, dict) else {}
    retired = bool(sync_meta_block.get("channel_retired_at")) and not sync_meta_block.get("retire_pending")

    return {
        "data_ready_for_whatsapp": data_ready,
        "meta_catalog_synced": meta_synced,
        "waba_catalog_linked": waba_linked,
        "visible_in_whatsapp": visible,
        "visibility_evidence": {
            "provable_via_api": False,
            "content_verified_in_meta": bool(sync_meta_block.get("content_verified")) and meta_synced,
            "verified_at": sync_meta_block.get("verified_at"),
            "waba_catalog_linked": waba_linked,
            "channel_retired": retired,
            "retire_pending": bool(sync_meta_block.get("retire_pending")),
        },
    }


__all__ = ["build_product_publication_status"]
