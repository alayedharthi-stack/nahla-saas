"""
services/product_publication_status.py
──────────────────────────────────────
Separate local field readiness from actual Meta/WhatsApp publication.
"""
from __future__ import annotations

from typing import Any, Dict

from services.channel_specs import CHANNEL_WHATSAPP
from services.product_readiness import compute_for_channel


def build_product_publication_status(product: Any) -> Dict[str, Any]:
    """Publication states — never conflate local readiness with live catalog."""
    wa = compute_for_channel(product, CHANNEL_WHATSAPP)
    data_ready = bool(wa.ready) if wa is not None else False

    sync_status = str(getattr(product, "sync_status", None) or "").strip().lower()
    meta_synced = sync_status == "synced"

    return {
        "data_ready_for_whatsapp": data_ready,
        "meta_catalog_synced": meta_synced,
        # Confirmed WABA↔catalog link requires Graph read — out of PR1 scope.
        "waba_catalog_linked": None,
        "visible_in_whatsapp": False,
    }


__all__ = ["build_product_publication_status"]
