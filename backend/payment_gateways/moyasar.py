"""
MoyasarClient
─────────────
Moyasar payment gateway integration for Saudi Arabia.
API base: https://api.moyasar.com/v1
Auth: HTTP Basic auth — secret_key as username, empty password.

Amounts in halalas (1 SAR = 100 halalas).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("nahla.payment.moyasar")

MOYASAR_API_BASE = "https://api.moyasar.com/v1"
REQUEST_TIMEOUT = 20.0


class MoyasarClient:
    def __init__(self, secret_key: str, publishable_key: str = ""):
        self.secret_key = secret_key
        self.publishable_key = publishable_key

    def _auth(self):
        """Basic auth tuple: (secret_key, empty_password)."""
        return (self.secret_key, "")

    async def create_invoice(
        self,
        amount_sar: float,
        description: str,
        callback_url: str,
        success_url: str = "",
        error_url: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Create a hosted-page payment invoice.
        Returns Moyasar invoice dict including 'url' (the payment link).
        amount_sar: float in SAR (will be converted to halalas internally).
        """
        amount_halalas = int(round(amount_sar * 100))
        body: Dict[str, Any] = {
            "amount": amount_halalas,
            "currency": "SAR",
            "description": description,
            "callback_url": callback_url,
        }
        if success_url:
            body["success_url"] = success_url
        if error_url:
            body["error_url"] = error_url
        if metadata:
            body["metadata"] = metadata

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{MOYASAR_API_BASE}/invoices",
                auth=self._auth(),
                json=body,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_invoice(self, invoice_id: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(
                f"{MOYASAR_API_BASE}/invoices/{invoice_id}",
                auth=self._auth(),
            )
            resp.raise_for_status()
            return resp.json()

    async def get_payment(self, payment_id: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(
                f"{MOYASAR_API_BASE}/payments/{payment_id}",
                auth=self._auth(),
            )
            resp.raise_for_status()
            return resp.json()

    def verify_webhook_signature(
        self, body: bytes, signature: str, webhook_secret: str
    ) -> bool:
        """
        Verify Moyasar webhook HMAC-SHA256 signature.
        Moyasar signs the raw request body with the webhook secret.
        """
        if not signature or not webhook_secret:
            return False
        try:
            computed = hmac.new(
                webhook_secret.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(computed, signature.lower())
        except Exception as exc:
            logger.error(f"Signature verification error: {exc}")
            return False
