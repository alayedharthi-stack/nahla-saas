"""
HyperPayClient
──────────────
HyperPay (OPP) payment gateway integration for Saudi local payment methods.
Supports: MADA, Apple Pay, STC Pay, Visa/Mastercard (manual billing flow).

API docs: https://developer.hyperpay.com/reference
Auth: Bearer token (access token) in Authorization header.
Sandbox: https://eu-test.oppwa.com
Live:    https://oppwa.com

Flow:
  1. POST /v1/checkouts  → get checkoutId
  2. Redirect merchant to HyperPay hosted payment page (or embed widget)
  3. Webhook / redirect confirms payment
  4. GET /v1/checkouts/{checkoutId}/payment → verify status
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("nahla.payment.hyperpay")

HYPERPAY_TEST_BASE = "https://eu-test.oppwa.com"
HYPERPAY_LIVE_BASE = "https://oppwa.com"
REQUEST_TIMEOUT = 20.0

# Result codes that represent a successful payment
SUCCESSFUL_RESULT_CODES = {
    "000.000.000",  # Transaction succeeded
    "000.100.110",  # Request successfully processed (3DSecure)
    "000.100.111",  # Request successfully processed (recurring)
    "000.100.112",  # Request successfully processed (moto)
    "000.200.100",  # Successfully created checkout
    "000.200.101",  # Successfully updated checkout
}


class HyperPayClient:
    def __init__(
        self,
        access_token: str,
        entity_id: str,
        webhook_secret: str = "",
        live_mode: bool = False,
    ):
        self.access_token = access_token
        self.entity_id = entity_id
        self.webhook_secret = webhook_secret
        self.base_url = HYPERPAY_LIVE_BASE if live_mode else HYPERPAY_TEST_BASE

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    # ── Checkout session ──────────────────────────────────────────────────────

    async def create_checkout(
        self,
        amount: float,
        currency: str = "SAR",
        payment_type: str = "DB",           # DB = debit (direct charge)
        brand: str = "MADA",                 # MADA | APPLEPAY | STC_PAY | VISA | MASTER
        merchant_transaction_id: str = "",
        description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Create a HyperPay checkout session.
        Returns dict with 'id' (checkoutId) and 'result' (status code + description).
        The frontend uses checkoutId to render the HyperPay embedded widget or
        redirect to the hosted payment page.
        """
        data: Dict[str, Any] = {
            "entityId": self.entity_id,
            "amount": f"{amount:.2f}",
            "currency": currency,
            "paymentType": payment_type,
            "descriptor": description[:127] if description else "Nahla SaaS Subscription",
        }
        if merchant_transaction_id:
            data["merchantTransactionId"] = merchant_transaction_id
        if metadata:
            for key, value in metadata.items():
                data[f"customParameters[{key}]"] = str(value)

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{self.base_url}/v1/checkouts",
                headers=self._headers(),
                data=data,
            )
            resp.raise_for_status()
            result = resp.json()

        checkout_id = result.get("id", "")
        result_code = result.get("result", {}).get("code", "")
        logger.info(
            f"[HyperPay] Checkout created: id={checkout_id} "
            f"brand={brand} amount={amount} {currency} result_code={result_code}"
        )
        return result

    async def get_payment_status(self, checkout_id: str) -> Dict[str, Any]:
        """
        Query the status of a payment by checkoutId.
        Use this to verify payment after redirect or webhook.
        """
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(
                f"{self.base_url}/v1/checkouts/{checkout_id}/payment",
                headers=self._headers(),
                params={"entityId": self.entity_id},
            )
            resp.raise_for_status()
            return resp.json()

    def is_payment_successful(self, payment_result: Dict[str, Any]) -> bool:
        """
        Determine if a HyperPay payment result represents a successful payment.
        Checks the result code against the known success set.
        """
        code = payment_result.get("result", {}).get("code", "")
        return code in SUCCESSFUL_RESULT_CODES

    # ── Webhook signature verification ────────────────────────────────────────

    def verify_webhook_signature(
        self, payload: bytes, iv: str, signature: str
    ) -> bool:
        """
        Verify HyperPay webhook HMAC-SHA256 signature.
        HyperPay signs: iv + payload using the webhook secret.
        Header names: X-Initialization-Vector, X-Authentication-Tag
        """
        if not self.webhook_secret:
            return False
        try:
            message = iv.encode("utf-8") + payload
            computed = hmac.new(
                self.webhook_secret.encode("utf-8"),
                message,
                hashlib.sha256,
            ).hexdigest()
            return hmac.compare_digest(computed.lower(), signature.lower())
        except Exception as exc:
            logger.error(f"[HyperPay] Signature verification error: {exc}")
            return False

    def build_payment_page_url(self, checkout_id: str, brand: str = "MADA") -> str:
        """
        Build the HyperPay hosted payment page URL.
        Merchants are redirected here to complete payment.
        """
        return f"{self.base_url}/v1/paymentWidgets.js?checkoutId={checkout_id}"
