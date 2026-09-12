"""Platform-owned IMAGE header for the COD confirmation template."""
from __future__ import annotations

import os
from typing import Optional

COD_CONFIRMATION_HEADER_ASSET_KEY = "cod_confirmation_header_v1"
COD_CONFIRMATION_HEADER_DEFAULT_URL = (
    "https://raw.githubusercontent.com/alayedharthi-stack/nahla-saas/main/"
    "assets/order-updates/cod-confirmation-header-v1.jpg"
)


def cod_confirmation_header_public_url() -> str:
    override = (os.environ.get("NAHLA_COD_CONFIRMATION_HEADER_URL") or "").strip()
    return override or COD_CONFIRMATION_HEADER_DEFAULT_URL


def cod_confirmation_image_header_component(
    *,
    header_image_url: Optional[str] = None,
    header_handle: Optional[str] = None,
) -> dict:
    example: dict = {"header_url": header_image_url or cod_confirmation_header_public_url()}
    if header_handle:
        example["header_handle"] = [header_handle]
    else:
        example["header_handle"] = []
    return {"type": "HEADER", "format": "IMAGE", "example": example}


__all__ = [
    "COD_CONFIRMATION_HEADER_ASSET_KEY",
    "COD_CONFIRMATION_HEADER_DEFAULT_URL",
    "cod_confirmation_header_public_url",
    "cod_confirmation_image_header_component",
]
