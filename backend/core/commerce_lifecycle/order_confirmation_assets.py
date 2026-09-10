"""
Platform-owned default assets for ``order_confirmation`` ONLY.

Do not import this module for other lifecycle service keys. Other templates
will get their own default header images in separate follow-up work.
"""
from __future__ import annotations

import os
from typing import Optional

ORDER_CONFIRMATION_HEADER_ASSET_KEY = "order_confirmation_header_v1"

# Verified production asset: HTTP 200, image/jpeg (~83 KiB).
ORDER_CONFIRMATION_HEADER_R2_DEFAULT_URL = (
    "https://pub-6c51fa068bbe49fa98f4444e88aeb093.r2.dev/"
    "platform/order-updates/order-confirmation-header-v1.jpg"
)

# Legacy dashboard-relative path — not valid for Meta fetch (SPA HTML fallback).
ORDER_CONFIRMATION_HEADER_ASSET_PATH = (
    "assets/order-updates/order-confirmation-header.jpg"
)


def order_confirmation_header_public_url() -> str:
    """
    Canonical HTTPS URL for the approved order-confirmation header image.

    Defaults to the platform R2 asset. Ops may override via
    ``NAHLA_ORDER_CONFIRMATION_HEADER_URL`` without redeploy.
    """
    override = (os.environ.get("NAHLA_ORDER_CONFIRMATION_HEADER_URL") or "").strip()
    if override:
        return override
    return ORDER_CONFIRMATION_HEADER_R2_DEFAULT_URL


def order_confirmation_image_header_component(
    *,
    header_image_url: Optional[str] = None,
    header_handle: Optional[str] = None,
) -> dict:
    """Meta IMAGE HEADER component for order_confirmation (Nahla storage / preview)."""
    example: dict = {}
    if header_handle:
        example["header_handle"] = [header_handle]
    url = header_image_url or order_confirmation_header_public_url()
    # Nahla preview + upload source only — stripped before Meta submit.
    example["header_url"] = url
    if not header_handle:
        example.setdefault("header_handle", [])
    return {
        "type": "HEADER",
        "format": "IMAGE",
        "example": example,
    }


__all__ = [
    "ORDER_CONFIRMATION_HEADER_ASSET_KEY",
    "ORDER_CONFIRMATION_HEADER_ASSET_PATH",
    "ORDER_CONFIRMATION_HEADER_R2_DEFAULT_URL",
    "order_confirmation_header_public_url",
    "order_confirmation_image_header_component",
]
