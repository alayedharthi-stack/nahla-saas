"""
Platform-owned default assets for ``order_confirmation`` ONLY.

Do not import this module for other lifecycle service keys. Other templates
will get their own default header images in separate follow-up work.
"""
from __future__ import annotations

import os
from typing import Optional

# Relative to dashboard ``public/`` — served as ``/assets/order-updates/...``.
ORDER_CONFIRMATION_HEADER_ASSET_PATH = (
    "assets/order-updates/order-confirmation-header.jpg"
)
ORDER_CONFIRMATION_HEADER_ASSET_KEY = "order_confirmation_header_v1"


def order_confirmation_header_public_url() -> str:
    """
    Canonical HTTPS URL for the approved order-confirmation header image.

    Meta template review and runtime template sends must fetch this URL.
    Ops may override via ``NAHLA_ORDER_CONFIRMATION_HEADER_URL`` without redeploy.
    """
    override = (os.environ.get("NAHLA_ORDER_CONFIRMATION_HEADER_URL") or "").strip()
    if override:
        return override
    base = (
        os.environ.get("NAHLA_DASHBOARD_PUBLIC_URL")
        or os.environ.get("DASHBOARD_PUBLIC_URL")
        or "https://app.nahlah.ai"
    ).rstrip("/")
    return f"{base}/{ORDER_CONFIRMATION_HEADER_ASSET_PATH}"


def order_confirmation_image_header_component(
    *,
    header_image_url: Optional[str] = None,
) -> dict:
    """Meta IMAGE HEADER component for order_confirmation revisions."""
    return {
        "type": "HEADER",
        "format": "IMAGE",
        "example": {
            "header_handle": [],
            "header_url": header_image_url or order_confirmation_header_public_url(),
        },
    }


__all__ = [
    "ORDER_CONFIRMATION_HEADER_ASSET_KEY",
    "ORDER_CONFIRMATION_HEADER_ASSET_PATH",
    "order_confirmation_header_public_url",
    "order_confirmation_image_header_component",
]
