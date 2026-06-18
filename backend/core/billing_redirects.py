"""Moyasar checkout redirect URL builders for subscription billing."""

from __future__ import annotations

DEFAULT_PAYMENT_RESULT_BASE = "https://app.nahlah.ai/billing/payment-result"


def build_moyasar_checkout_redirects(
    success_url: str | None,
    error_url: str | None,
    subscription_id: int,
    *,
    default_base: str = DEFAULT_PAYMENT_RESULT_BASE,
) -> tuple[str, str]:
    """Return (success_redirect, error_redirect) query strings for Moyasar."""
    base_success = (success_url or "").rstrip("/") or default_base
    base_error = (error_url or "").rstrip("/") or default_base
    return (
        f"{base_success}?status=paid&sub_id={subscription_id}",
        f"{base_error}?status=failed&sub_id={subscription_id}",
    )
