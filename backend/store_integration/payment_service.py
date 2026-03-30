"""
PaymentService
──────────────
Generates real payment links through the store adapter or Moyasar gateway.
Priority:
  1. Moyasar (if configured and enabled for this tenant)
  2. Store adapter payment link (Salla, etc.)
  3. Placeholder URL (when nothing is configured)
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from store_integration.registry import get_adapter

logger = logging.getLogger("nahla.store_integration.payment")


def _get_moyasar_settings(tenant_id: int) -> dict:
    """Read Moyasar settings from TenantSettings.extra_metadata['moyasar']."""
    try:
        from database.session import SessionLocal
        from database.models import TenantSettings
        db = SessionLocal()
        try:
            settings = db.query(TenantSettings).filter(
                TenantSettings.tenant_id == tenant_id
            ).first()
            if settings and settings.extra_metadata:
                return settings.extra_metadata.get("moyasar", {})
        finally:
            db.close()
    except Exception as exc:
        logger.warning(f"[PaymentService] Could not load Moyasar settings: {exc}")
    return {}


async def generate_payment_link(
    tenant_id: int,
    order_id: str,
    amount: float,
    description: str = "",
) -> str:
    """
    Generate a payment link for an order.
    Returns a real Moyasar link, store adapter link, or placeholder — in that priority.
    """
    # ── Priority 1: Moyasar ────────────────────────────────────────────────────
    moyasar_cfg = _get_moyasar_settings(tenant_id)
    if moyasar_cfg.get("enabled") and moyasar_cfg.get("secret_key") and amount > 0:
        try:
            from payment_gateways.moyasar import MoyasarClient
            client = MoyasarClient(
                secret_key=moyasar_cfg["secret_key"],
                publishable_key=moyasar_cfg.get("publishable_key", ""),
            )
            invoice = await client.create_invoice(
                amount_sar=amount,
                description=description or f"طلب #{order_id}",
                callback_url=moyasar_cfg.get("callback_url") or "https://api.nahla.ai/payments/webhook/moyasar",
                success_url=moyasar_cfg.get("success_url", ""),
                error_url=moyasar_cfg.get("error_url", ""),
                metadata={"order_id": order_id, "tenant_id": str(tenant_id)},
            )
            link = invoice.get("url", "")
            if link:
                logger.info(
                    f"[PaymentService] Moyasar payment link generated for "
                    f"tenant={tenant_id} order={order_id}"
                )
                return link
        except Exception as exc:
            logger.error(f"[PaymentService] Moyasar link generation failed: {exc}")

    # ── Priority 2: Store adapter (Salla, etc.) ────────────────────────────────
    adapter = get_adapter(tenant_id)
    if adapter:
        try:
            link = await adapter.generate_payment_link(order_id, amount)
            if link:
                logger.info(
                    f"[PaymentService] Store adapter ({adapter.platform}) payment link "
                    f"for tenant={tenant_id} order={order_id}"
                )
                return link
        except Exception as exc:
            logger.error(f"[PaymentService] Adapter payment link failed: {exc}")

    # ── Priority 3: Placeholder ────────────────────────────────────────────────
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    placeholder = f"https://pay.nahla.ai/checkout/{tenant_id}-{ts}"
    logger.warning(
        f"[PaymentService] No gateway configured for tenant={tenant_id} — "
        f"returning placeholder payment link"
    )
    return placeholder


def has_real_payment(tenant_id: int) -> bool:
    """Returns True if this tenant has any real payment gateway configured."""
    cfg = _get_moyasar_settings(tenant_id)
    if cfg.get("enabled") and cfg.get("secret_key"):
        return True
    return get_adapter(tenant_id) is not None
