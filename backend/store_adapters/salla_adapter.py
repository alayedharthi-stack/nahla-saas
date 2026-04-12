"""
SallaAdapter
────────────
Implements BaseStoreAdapter for the Salla e-commerce platform.
API base: https://api.salla.dev/admin/v2
Auth: Bearer token (OAuth2 access token from Salla App)
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from store_integration.models import (
    NormalizedOffer,
    NormalizedOrder,
    NormalizedProduct,
    NormalizedVariant,
    OrderInput,
    OrderItem,
    ShippingOption,
)
from store_integration.registry import register_adapter
from store_adapters.base_adapter import BaseStoreAdapter

logger = logging.getLogger("nahla.adapter.salla")

SALLA_API_BASE = "https://api.salla.dev/admin/v2"
REQUEST_TIMEOUT = 20.0


@register_adapter("salla")
class SallaAdapter(BaseStoreAdapter):
    platform = "salla"

    def __init__(self, api_key: str, store_id: str = "", refresh_token: str = "", tenant_id: int = 0):
        self.api_key = api_key
        self.store_id = store_id
        self._refresh_token = refresh_token
        self._tenant_id = tenant_id

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _refresh_access_token(self) -> bool:
        """Use refresh_token to get a new access_token from Salla."""
        if not self._refresh_token or not self._tenant_id:
            return False
        client_id = os.environ.get("SALLA_CLIENT_ID", "")
        client_secret = os.environ.get("SALLA_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            return False
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://accounts.salla.sa/oauth2/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "refresh_token": self._refresh_token,
                    },
                    headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
                )
                if resp.status_code != 200:
                    logger.error("Salla token refresh failed: %s %s", resp.status_code, resp.text[:200])
                    return False
                data = resp.json()
                new_access = data.get("access_token", "")
                new_refresh = data.get("refresh_token", self._refresh_token)
                if not new_access:
                    return False
                self.api_key = new_access
                self._refresh_token = new_refresh
                self._persist_refreshed_tokens(new_access, new_refresh)
                logger.info("Salla token refreshed | tenant=%s", self._tenant_id)
                return True
        except Exception as exc:
            logger.error("Salla token refresh error: %s", exc)
            return False

    def _persist_refreshed_tokens(self, access_token: str, refresh_token: str) -> None:
        """Save refreshed tokens back to the Integration row."""
        try:
            from database.session import SessionLocal
            from database.models import Integration
            db = SessionLocal()
            try:
                intg = db.query(Integration).filter(
                    Integration.tenant_id == self._tenant_id,
                    Integration.provider == "salla",
                ).first()
                if intg:
                    cfg = dict(intg.config or {})
                    cfg["api_key"] = access_token
                    cfg["refresh_token"] = refresh_token
                    intg.config = cfg
                    db.commit()
            finally:
                db.close()
        except Exception as exc:
            logger.warning("Failed to persist refreshed tokens: %s", exc)

    async def _get(self, path: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(
                f"{SALLA_API_BASE}{path}",
                headers=self._headers(),
                params=params or {},
            )
            if resp.status_code == 401 and await self._refresh_access_token():
                resp = await client.get(
                    f"{SALLA_API_BASE}{path}",
                    headers=self._headers(),
                    params=params or {},
                )
            resp.raise_for_status()
            return resp.json()

    async def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{SALLA_API_BASE}{path}",
                headers=self._headers(),
                json=body,
            )
            resp.raise_for_status()
            return resp.json()

    def _log_error(self, method: str, exc: Exception) -> None:
        logger.error(f"SallaAdapter.{method} failed: {exc}", exc_info=True)

    # ── Products ───────────────────────────────────────────────────────────────

    async def get_products(self) -> List[NormalizedProduct]:
        try:
            data = await self._get("/products", {"per_page": 50})
            return [self._normalize_product(p) for p in data.get("data", [])]
        except httpx.HTTPStatusError as exc:
            self._log_error("get_products", exc)
            logger.error(f"Salla get_products HTTP error {exc.response.status_code}: {exc.response.text[:200]}")
            raise
        except Exception as exc:
            self._log_error("get_products", exc)
            raise

    async def get_product(self, product_id: str) -> Optional[NormalizedProduct]:
        try:
            data = await self._get(f"/products/{product_id}")
            raw = data.get("data")
            return self._normalize_product(raw) if raw else None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            self._log_error("get_product", exc)
            raise
        except Exception as exc:
            self._log_error("get_product", exc)
            raise

    async def get_product_variants(self, product_id: str) -> List[NormalizedVariant]:
        product = await self.get_product(product_id)
        return product.variants if product else []

    def _normalize_product(self, raw: Dict[str, Any]) -> NormalizedProduct:
        price_block = raw.get("price") or {}
        price_amount = price_block.get("amount") if isinstance(price_block, dict) else raw.get("price")
        try:
            price_f = float(price_amount) if price_amount is not None else None
        except (TypeError, ValueError):
            price_f = None

        variants = [
            self._normalize_variant(v)
            for v in (raw.get("variants") or [])
        ]

        return NormalizedProduct(
            id=str(raw.get("id", "")),
            title=raw.get("name") or raw.get("title") or "",
            price=price_f,
            currency=(price_block.get("currency") if isinstance(price_block, dict) else "SAR") or "SAR",
            sku=raw.get("sku") or "",
            in_stock=(raw.get("quantity", 1) or 0) > 0,
            stock_quantity=raw.get("quantity"),
            description=(raw.get("description") or "")[:300],
            image_url=raw.get("main_image") or raw.get("thumbnail"),
            product_url=raw.get("url"),
            tags=raw.get("tags") or [],
            variants=variants,
        )

    def _normalize_variant(self, raw: Dict[str, Any]) -> NormalizedVariant:
        price_block = raw.get("price") or {}
        price_amount = price_block.get("amount") if isinstance(price_block, dict) else raw.get("price")
        try:
            price_f = float(price_amount) if price_amount is not None else None
        except (TypeError, ValueError):
            price_f = None
        return NormalizedVariant(
            id=str(raw.get("id", "")),
            title=raw.get("name") or str(raw.get("id", "")),
            price=price_f,
            sku=raw.get("sku"),
            in_stock=raw.get("available", True),
            stock_quantity=raw.get("quantity"),
        )

    # ── Orders ─────────────────────────────────────────────────────────────────

    async def create_order(self, order_input: OrderInput) -> NormalizedOrder:
        body = self._build_order_body(order_input, draft=False)
        try:
            data = await self._post("/orders", body)
            return self._normalize_order(data.get("data", data), order_input)
        except Exception as exc:
            self._log_error("create_order", exc)
            raise

    async def create_draft_order(self, order_input: OrderInput) -> NormalizedOrder:
        body = self._build_order_body(order_input, draft=True)
        try:
            data = await self._post("/orders", body)
            return self._normalize_order(data.get("data", data), order_input)
        except Exception as exc:
            self._log_error("create_draft_order", exc)
            raise

    def _build_order_body(self, order_input: OrderInput, draft: bool) -> Dict[str, Any]:
        items = []
        for item in order_input.items:
            entry: Dict[str, Any] = {
                "product_id": int(item.product_id),
                "quantity": item.quantity,
            }
            if item.variant_id:
                entry["variants"] = [{"id": int(item.variant_id)}]
            items.append(entry)

        body: Dict[str, Any] = {
            "source": "api",
            "items": items,
            "customer": {
                "name": order_input.customer_name,
                "mobile": order_input.customer_phone,
            },
            "payment_method": "cod" if order_input.payment_method in ("cod", "cash_on_delivery") else "online",
        }
        if order_input.customer_email:
            body["customer"]["email"] = order_input.customer_email
        if any([order_input.city, order_input.address, order_input.street,
                order_input.building_number, order_input.district, order_input.postal_code]):
            body["address"] = {
                "city":            order_input.city,
                "street":          order_input.street or order_input.address,
                "building_number": order_input.building_number,
                "district":        order_input.district,
                "postal_code":     order_input.postal_code,
            }
        if order_input.notes:
            body["notes"] = order_input.notes
        if draft:
            body["status"] = "draft"
        return body

    async def get_order(self, order_id: str) -> Optional[NormalizedOrder]:
        try:
            data = await self._get(f"/orders/{order_id}")
            raw = data.get("data")
            return self._normalize_order(raw, None) if raw else None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            self._log_error("get_order", exc)
            raise
        except Exception as exc:
            self._log_error("get_order", exc)
            raise

    async def get_orders(self, limit: int = 50) -> List[NormalizedOrder]:
        try:
            data = await self._get("/orders", {"per_page": min(limit, 50)})
            return [self._normalize_order(o, None) for o in data.get("data", [])]
        except Exception as exc:
            self._log_error("get_orders", exc)
            return []

    async def get_customer_orders(self, customer_phone: str) -> List[NormalizedOrder]:
        try:
            data = await self._get("/orders", {"mobile": customer_phone, "per_page": 10})
            return [self._normalize_order(o, None) for o in data.get("data", [])]
        except Exception as exc:
            self._log_error("get_customer_orders", exc)
            return []

    def _normalize_order(self, raw: Dict[str, Any], order_input: Optional[OrderInput]) -> NormalizedOrder:
        amounts = raw.get("amounts") or {}
        total_block = amounts.get("total") or {}
        if isinstance(total_block, dict):
            total = float(total_block.get("amount", 0) or 0)
            currency = total_block.get("currency", "SAR")
        else:
            total = float(total_block or 0)
            currency = "SAR"

        payment_link = raw.get("payment_url") or raw.get("checkout_url")

        items = []
        for li in (raw.get("items") or raw.get("line_items") or []):
            price_val = li.get("price")
            unit_price = None
            if isinstance(price_val, dict):
                unit_price = float(price_val.get("amount", 0) or 0)
            items.append(OrderItem(
                product_id=str(li.get("product_id") or li.get("id", "")),
                product_title=li.get("name") or li.get("product_name") or "",
                variant_id=str(li.get("variant_id")) if li.get("variant_id") else None,
                quantity=li.get("quantity", 1),
                unit_price=unit_price,
            ))

        customer = raw.get("customer") or {}
        cname = customer.get("name") or (order_input.customer_name if order_input else "")
        cphone = customer.get("mobile") or (order_input.customer_phone if order_input else "")

        return NormalizedOrder(
            id=str(raw.get("id") or raw.get("reference_id", "")),
            status=str(raw.get("status", "pending")),
            total=total,
            currency=currency,
            payment_link=payment_link,
            customer_name=cname,
            customer_phone=cphone,
            items=items,
            created_at=str(raw.get("created_at", "")),
        )

    # ── Payment ────────────────────────────────────────────────────────────────

    async def generate_payment_link(self, order_id: str, amount: float) -> Optional[str]:
        try:
            data = await self._get(f"/orders/{order_id}")
            raw = data.get("data", {})
            return raw.get("payment_url") or raw.get("checkout_url")
        except Exception as exc:
            self._log_error("generate_payment_link", exc)
            return None

    # ── Shipping ───────────────────────────────────────────────────────────────

    async def get_shipping_options(self, city: str = "") -> List[ShippingOption]:
        try:
            params = {"city": city} if city else {}
            data = await self._get("/shipping/zones", params)
            options = []
            for zone in (data.get("data") or []):
                costs = zone.get("costs") or zone.get("prices") or [{}]
                cost_entry = costs[0] if costs else {}
                options.append(ShippingOption(
                    name=zone.get("name") or zone.get("courier_name") or "شحن",
                    cost=float(cost_entry.get("amount", 0) or 0),
                    currency=cost_entry.get("currency", "SAR"),
                    estimated_days=str(zone.get("min_days", "")) or None,
                    zone=zone.get("name"),
                    courier=zone.get("courier_name"),
                ))
            return options
        except Exception as exc:
            self._log_error("get_shipping_options", exc)
            return []

    # ── Customers ──────────────────────────────────────────────────────────────

    async def get_customers(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Fetch customers from Salla and return raw dicts."""
        try:
            data = await self._get("/customers", {"per_page": min(limit, 50)})
            return data.get("data") or []
        except Exception as exc:
            self._log_error("get_customers", exc)
            return []

    # ── Offers / Coupons ──────────────────────────────────────────────────────

    async def get_coupons(self) -> List[Dict[str, Any]]:
        """Return raw coupon dicts from Salla (used by sync_coupons)."""
        try:
            data = await self._get("/coupons", {"per_page": 50})
            return data.get("data") or []
        except Exception as exc:
            self._log_error("get_coupons", exc)
            return []

    async def create_coupon(
        self,
        code: str,
        discount_type: str = "percentage",
        discount_value: int = 10,
        expiry_days: int = 3,
    ) -> Optional[Dict[str, Any]]:
        """Create a coupon in Salla. Returns the created coupon data or None."""
        from datetime import timedelta
        expiry = (datetime.now(timezone.utc) + timedelta(days=expiry_days)).strftime("%Y-%m-%d")
        salla_type = "PERCENT" if discount_type == "percentage" else "FIXED"
        payload = {
            "code": code,
            "type": salla_type,
            "percent_off": discount_value if salla_type == "PERCENT" else 0,
            "amount_off": discount_value if salla_type == "FIXED" else 0,
            "status": "active",
            "expiry_date": expiry,
        }
        try:
            data = await self._post("/coupons", payload)
            logger.info("Salla coupon created: %s | tenant=%s", code, self._tenant_id)
            return data.get("data", data)
        except Exception as exc:
            self._log_error("create_coupon", exc)
            return None

    async def get_active_offers(self) -> List[NormalizedOffer]:
        try:
            data = await self._get("/coupons", {"status": "active", "per_page": 20})
            return [self._normalize_coupon(c) for c in (data.get("data") or [])]
        except Exception as exc:
            self._log_error("get_active_offers", exc)
            return []

    async def validate_coupon(self, code: str) -> Optional[NormalizedOffer]:
        try:
            data = await self._get("/coupons", {"code": code})
            results = data.get("data") or []
            for c in results:
                if c.get("code") == code:
                    offer = self._normalize_coupon(c)
                    return offer if offer.valid else None
            return None
        except Exception as exc:
            self._log_error("validate_coupon", exc)
            return None

    def _normalize_coupon(self, raw: Dict[str, Any]) -> NormalizedOffer:
        coupon_type = "percentage" if raw.get("percent") else "fixed"
        value = float(raw.get("percent") or raw.get("amount") or 0)
        expires_raw = raw.get("expire_date")
        valid = raw.get("status", "active") == "active"
        if expires_raw:
            try:
                exp = datetime.fromisoformat(str(expires_raw).replace("Z", "+00:00"))
                if exp < datetime.now(timezone.utc).replace(tzinfo=exp.tzinfo):
                    valid = False
            except Exception:
                pass
        return NormalizedOffer(
            code=raw.get("code"),
            type=coupon_type,
            value=value,
            min_order=float(raw.get("minimum_order_amount") or 0) or None,
            expires_at=str(expires_raw) if expires_raw else None,
            description=raw.get("description"),
            valid=valid,
        )
