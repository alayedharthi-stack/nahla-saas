"""
StripeClient
────────────
Stripe payment gateway integration for SaaS subscription billing.
Handles: Customers, SetupIntents, Subscriptions, and Webhook signature verification.

All amounts are in the currency's smallest unit (e.g. halalas for SAR, cents for USD).
Uses the official `stripe` Python SDK.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import stripe as _stripe

logger = logging.getLogger("nahla.payment.stripe")


class StripeClient:
    def __init__(self, secret_key: str, webhook_secret: str = ""):
        self.secret_key = secret_key
        self.webhook_secret = webhook_secret
        _stripe.api_key = secret_key

    # ── Customers ─────────────────────────────────────────────────────────────

    def create_customer(
        self,
        email: str,
        name: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a Stripe Customer and return the full customer object."""
        customer = _stripe.Customer.create(
            email=email,
            name=name,
            metadata=metadata or {},
        )
        logger.info(f"[Stripe] Created customer {customer.id} for {email}")
        return dict(customer)

    def retrieve_customer(self, customer_id: str) -> Dict[str, Any]:
        return dict(_stripe.Customer.retrieve(customer_id))

    # ── SetupIntent (card collection without charging) ────────────────────────

    def create_setup_intent(
        self,
        customer_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Create a SetupIntent to securely collect a payment method via
        Stripe Elements without charging the customer yet.
        Returns the SetupIntent object including `client_secret`.
        """
        si = _stripe.SetupIntent.create(
            customer=customer_id,
            payment_method_types=["card"],
            usage="off_session",   # stored card, used for future recurring charges
            metadata=metadata or {},
        )
        logger.info(f"[Stripe] SetupIntent {si.id} created for customer {customer_id}")
        return {"setup_intent_id": si.id, "client_secret": si.client_secret}

    # ── Subscriptions ─────────────────────────────────────────────────────────

    def create_subscription(
        self,
        customer_id: str,
        price_id: str,
        trial_period_days: int = 14,
        metadata: Optional[Dict[str, Any]] = None,
        payment_method_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a Stripe Subscription with a trial period.
        Webhooks (invoice.paid / invoice.payment_failed) must be the source of truth.

        Returns the Subscription object.
        """
        params: Dict[str, Any] = {
            "customer": customer_id,
            "items": [{"price": price_id}],
            "trial_period_days": trial_period_days,
            "metadata": metadata or {},
            "payment_behavior": "default_incomplete",
            "expand": ["latest_invoice.payment_intent"],
        }
        if payment_method_id:
            params["default_payment_method"] = payment_method_id

        sub = _stripe.Subscription.create(**params)
        logger.info(
            f"[Stripe] Subscription {sub.id} created for customer {customer_id} "
            f"(price={price_id}, trial={trial_period_days}d, status={sub.status})"
        )
        return dict(sub)

    def retrieve_subscription(self, subscription_id: str) -> Dict[str, Any]:
        return dict(_stripe.Subscription.retrieve(subscription_id))

    def cancel_subscription(self, subscription_id: str) -> Dict[str, Any]:
        sub = _stripe.Subscription.cancel(subscription_id)
        logger.info(f"[Stripe] Subscription {subscription_id} cancelled")
        return dict(sub)

    # ── Webhook signature verification ────────────────────────────────────────

    def construct_webhook_event(self, payload: bytes, sig_header: str) -> Any:
        """
        Verify Stripe webhook signature and return the parsed Event object.
        Raises stripe.error.SignatureVerificationError on failure.
        """
        if not self.webhook_secret:
            raise ValueError("STRIPE_WEBHOOK_SECRET is not configured")
        return _stripe.Webhook.construct_event(
            payload, sig_header, self.webhook_secret
        )
