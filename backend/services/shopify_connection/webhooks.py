"""
Shopify webhook delivery verification (``app/uninstalled``).

What the signature proves — and what it does not
────────────────────────────────────────────────
``X-Shopify-Hmac-Sha256`` is the **base64** HMAC-SHA256 of the **raw body**
with the app's client secret. It authenticates the body bytes only. The
``X-Shopify-Shop-Domain``, ``X-Shopify-Topic``, ``X-Shopify-Webhook-Id``,
``X-Shopify-Event-Id`` and ``X-Shopify-Triggered-At`` headers are **not**
covered: anyone holding one old signed body can resend it with any header
values. Therefore:

  * the shop identity is read from the signed body (``id`` and
    ``myshopify_domain``) and the unsigned shop header must merely agree;
  * the delivery id is used for dedupe only — never as proof of anything;
  * no timestamp header ever decides retention, revival or transfer of
    access (``lifecycle.record_uninstall`` quarantines; only an authenticated
    Shopify API probe with the current-generation credential resolves it).

A missing secret refuses (never "dev mode accept").
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Optional

from services.shopify_connection.shop_domain import canonical_shop_domain

TOPIC_APP_UNINSTALLED = "app/uninstalled"
MAX_BODY_BYTES = 65536


class WebhookRejected(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def verify_webhook_hmac(raw_body: bytes, header_value: Optional[str], secret: str) -> None:
    """Raises WebhookRejected unless the base64 HMAC of *raw_body* matches."""
    if not secret:
        raise WebhookRejected("secret_missing")
    if not isinstance(raw_body, (bytes, bytearray)) or len(raw_body) > MAX_BODY_BYTES:
        raise WebhookRejected("body_invalid")
    if not header_value or not isinstance(header_value, str) or len(header_value) > 128:
        raise WebhookRejected("hmac_invalid")
    try:
        received = base64.b64decode(header_value.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise WebhookRejected("hmac_invalid") from None
    expected = hmac.new(secret.encode("utf-8"), bytes(raw_body), hashlib.sha256).digest()
    if len(received) != len(expected) or not hmac.compare_digest(expected, received):
        raise WebhookRejected("hmac_invalid")


@dataclass(frozen=True)
class SignedShopIdentity:
    """Shop identity taken from the signed body (never from headers)."""

    shop_id: int
    shop_domain: str

    @property
    def shop_gid(self) -> str:
        return f"gid://shopify/Shop/{self.shop_id}"


def parse_uninstall_body(raw_body: bytes) -> SignedShopIdentity:
    """The signed ``app/uninstalled`` payload's shop id and myshopify domain."""
    try:
        payload: Any = json.loads(bytes(raw_body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise WebhookRejected("body_invalid") from None
    if not isinstance(payload, dict):
        raise WebhookRejected("body_invalid")
    shop_id = payload.get("id")
    if isinstance(shop_id, bool) or not isinstance(shop_id, int) or not 0 < shop_id < 10 ** 20:
        raise WebhookRejected("body_identity_invalid")
    raw_domain = payload.get("myshopify_domain")
    domain = canonical_shop_domain(raw_domain)
    if domain is None or domain != raw_domain:
        raise WebhookRejected("body_identity_invalid")
    return SignedShopIdentity(shop_id=shop_id, shop_domain=domain)


def body_digest(raw_body: bytes) -> str:
    return hashlib.sha256(bytes(raw_body)).hexdigest()
