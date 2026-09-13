"""Platform-owned IMAGE header for the order-ready lifecycle template."""
from __future__ import annotations

import os
from typing import Optional

ORDER_READY_HEADER_ASSET_KEY = "order_ready_header_v1"
ORDER_READY_HEADER_DEFAULT_URL = (
    "https://raw.githubusercontent.com/alayedharthi-stack/nahla-saas/main/"
    "assets/order-updates/order-ready-header-v1.jpg"
)


def order_ready_header_public_url() -> str:
    override = (os.environ.get("NAHLA_ORDER_READY_HEADER_URL") or "").strip()
    return override or ORDER_READY_HEADER_DEFAULT_URL


def order_ready_image_header_component(
    *,
    header_image_url: Optional[str] = None,
    header_handle: Optional[str] = None,
) -> dict:
    example: dict = {
        "header_url": header_image_url or order_ready_header_public_url(),
    }
    if header_handle:
        example["header_handle"] = [header_handle]
    else:
        example["header_handle"] = []
    return {"type": "HEADER", "format": "IMAGE", "example": example}


__all__ = [
    "ORDER_READY_HEADER_ASSET_KEY",
    "ORDER_READY_HEADER_DEFAULT_URL",
    "order_ready_header_public_url",
    "order_ready_image_header_component",
]
