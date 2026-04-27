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
from datetime import datetime, timedelta, timezone
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


class SallaTokenRevokedException(Exception):
    """Raised when Salla returns invalid_grant — token permanently revoked."""


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
        """Use refresh_token to get a new access_token from Salla.

        Returns True on success, False on transient failure.
        Raises SallaTokenRevokedException when Salla returns invalid_grant
        (token permanently revoked — re-auth required by merchant).
        """
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
                    resp_text = resp.text[:300]
                    logger.error("Salla token refresh failed: %s %s", resp.status_code, resp_text)
                    # invalid_grant = token permanently revoked — stop retrying
                    if resp.status_code == 400 and "invalid_grant" in resp_text:
                        logger.error(
                            "[Salla] INVALID_GRANT — token revoked permanently | tenant=%s. "
                            "Marking needs_reauth and disabling sync.",
                            self._tenant_id,
                        )
                        self._mark_needs_reauth("invalid_grant")
                        raise SallaTokenRevokedException(
                            f"Salla refresh_token revoked for tenant={self._tenant_id} (invalid_grant)"
                        )
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
        except SallaTokenRevokedException:
            raise  # re-raise so callers can handle it
        except Exception as exc:
            logger.error("Salla token refresh error: %s", exc)
            return False

    def _mark_needs_reauth(self, reason: str = "unknown") -> None:
        """Remove the revoked refresh_token and stop future refresh attempts.

        Keeps the integration ENABLED so the existing access_token (api_key) can
        still be used for API calls.  Consistent with the scheduler's handling of
        invalid_grant: only the refresh_token rotation is stopped, not the whole
        integration.  The merchant can enter a fresh Account token from
        Salla Partners → API credentials whenever they want to rotate it.
        """
        try:
            from database.session import SessionLocal  # noqa: PLC0415
            from database.models import Integration as _Integration  # noqa: PLC0415
            _db = SessionLocal()
            try:
                intg = _db.query(_Integration).filter(
                    _Integration.tenant_id == self._tenant_id,
                    _Integration.provider == "salla",
                ).first()
                if intg:
                    cfg = dict(intg.config or {})
                    # Remove the revoked token so we stop retrying
                    cfg.pop("refresh_token", None)
                    cfg["no_auto_refresh"]        = True
                    cfg["no_auto_refresh_reason"] = reason
                    cfg["no_auto_refresh_at"]     = datetime.now(timezone.utc).isoformat()
                    # Clear any stale reauth flags so the UI doesn't show a blocker
                    cfg.pop("needs_reauth", None)
                    cfg.pop("needs_reauth_at", None)
                    cfg.pop("needs_reauth_reason", None)
                    intg.config  = cfg
                    intg.enabled = True   # keep active — api_key may still work
                    _db.commit()
                    logger.warning(
                        "[Salla] refresh_token revoked — removed, integration kept active | "
                        "tenant=%s reason=%s",
                        self._tenant_id, reason,
                    )
            finally:
                _db.close()
        except Exception as exc:
            logger.warning("[Salla] Failed to persist no_auto_refresh: %s", exc)

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
        url = f"{SALLA_API_BASE}{path}"
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(url, headers=self._headers(), params=params or {})
            logger.info(
                "[Salla API] GET %s → %d | tenant=%s store=%s",
                path, resp.status_code, self._tenant_id, self.store_id,
            )
            if resp.status_code == 401:
                logger.warning(
                    "[Salla API] 401 on %s | tenant=%s — attempting token refresh | response=%s",
                    path, self._tenant_id, resp.text[:200],
                )
                if await self._refresh_access_token():
                    resp = await client.get(url, headers=self._headers(), params=params or {})
                    logger.info("[Salla API] RETRY GET %s → %d | tenant=%s", path, resp.status_code, self._tenant_id)
            if resp.status_code >= 400:
                logger.error(
                    "[Salla API] ERROR GET %s → %d | tenant=%s body=%s",
                    path, resp.status_code, self._tenant_id, resp.text[:300],
                )
            resp.raise_for_status()
            return resp.json()

    async def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        import json as _json
        url = f"{SALLA_API_BASE}{path}"
        # Always emit the request payload for /orders at ERROR level so it
        # appears alongside any failure in Railway log filters that hide INFO.
        _payload_str = _json.dumps(body, ensure_ascii=False)
        if path == "/orders":
            # Unmissable pre-flight log — this MUST appear on every order
            # creation attempt regardless of which path triggered it. If
            # we ever see Salla return 422 without this line, it means a
            # different process is posting orders (impossible via this
            # adapter) or the deployment is stale.
            try:
                _items_brief = [
                    {
                        "identifier": p.get("identifier"),
                        "quantity": p.get("quantity"),
                        "options": p.get("options"),
                    }
                    for p in (body.get("products") or [])
                ]
            except Exception:
                _items_brief = []
            logger.error(
                "[SallaAdapter] ABOUT_TO_POST_ORDER | tenant=%s products=%s",
                self._tenant_id,
                _items_brief,
            )
            logger.error(
                "[SallaAdapter] POST /orders REQUEST | tenant=%s payload=%s",
                self._tenant_id,
                _payload_str,
            )
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(url, headers=self._headers(), json=body)
            logger.info("[Salla API] POST %s → %d | tenant=%s", path, resp.status_code, self._tenant_id)
            if resp.status_code == 401:
                logger.warning("[Salla API] 401 on POST %s — attempting refresh", path)
                if await self._refresh_access_token():
                    resp = await client.post(url, headers=self._headers(), json=body)
                    logger.info("[Salla API] RETRY POST %s → %d", path, resp.status_code)
            if resp.status_code >= 400:
                # Emit the FULL response body — DO NOT truncate. Salla's 422
                # validation messages are nested under `error.fields` and we
                # need the entire structure to know which field was rejected.
                _raw_text = resp.text or ""
                _parsed: Optional[Dict[str, Any]] = None
                try:
                    _parsed_obj = resp.json()
                    if isinstance(_parsed_obj, dict):
                        _parsed = _parsed_obj
                except Exception:
                    _parsed = None

                logger.error(
                    "[SallaAdapter] POST %s FAILED | tenant=%s status=%d response=%s",
                    path, self._tenant_id, resp.status_code, _raw_text,
                )
                logger.error(
                    "[SallaAdapter] POST %s FAILED | tenant=%s request_payload=%s",
                    path, self._tenant_id, _payload_str,
                )
                # Best-effort field-level breakdown to make root cause obvious.
                if _parsed is not None:
                    _err = _parsed.get("error") if isinstance(_parsed.get("error"), dict) else None
                    _msg = (_parsed.get("message")
                            or (_err.get("message") if _err else "")
                            or "")
                    _fields = (_err or {}).get("fields") if _err else None
                    if _fields is None:
                        _fields = _parsed.get("errors") or _parsed.get("fields")
                    logger.error(
                        "[SallaAdapter] POST %s FAILED | tenant=%s status=%d "
                        "salla_message=%r salla_fields=%s",
                        path, self._tenant_id, resp.status_code,
                        _msg, _json.dumps(_fields, ensure_ascii=False) if _fields is not None else "<none>",
                    )
            resp.raise_for_status()
            return resp.json()

    async def _delete(self, path: str) -> bool:
        """DELETE helper. Returns True on 2xx, False otherwise (never raises)."""
        url = f"{SALLA_API_BASE}{path}"
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                resp = await client.delete(url, headers=self._headers())
                if resp.status_code == 401 and await self._refresh_access_token():
                    resp = await client.delete(url, headers=self._headers())
                logger.info(
                    "[Salla API] DELETE %s → %d | tenant=%s", path, resp.status_code, self._tenant_id,
                )
                return 200 <= resp.status_code < 300 or resp.status_code == 404
        except Exception as exc:
            self._log_error("_delete", exc)
            return False

    def _log_error(self, method: str, exc: Exception) -> None:
        logger.error(f"SallaAdapter.{method} failed: {exc}", exc_info=True)

    # ── Pagination helper ────────────────────────────────────────────────────

    async def _get_all_pages(
        self,
        path: str,
        per_page: int = 50,
        extra_params: Optional[Dict[str, Any]] = None,
        label: str = "",
    ) -> List[Dict[str, Any]]:
        """Fetch ALL pages from a paginated Salla endpoint until data is exhausted.

        No hard page limit — continues until:
          1. API returns an empty page, OR
          2. Current page >= total pages reported by API, OR
          3. A single page returns fewer items than per_page (last page).
        """
        tag = label or path.strip("/")
        all_items: List[Dict[str, Any]] = []
        page = 1
        total_pages_hint = None

        while True:
            params: Dict[str, Any] = {"per_page": per_page, "page": page}
            if extra_params:
                params.update(extra_params)

            try:
                data = await self._get(path, params)
            except SallaTokenRevokedException:
                raise  # propagate — callers must handle this as a hard stop
            except Exception as exc:
                logger.error(
                    "[Salla:%s] tenant=%s page %d FAILED — stopping pagination: %s",
                    tag, self._tenant_id, page, exc,
                )
                break

            items = data.get("data") or []
            all_items.extend(items)

            pagination = data.get("pagination") or data.get("meta") or {}
            total_pages_hint = pagination.get(
                "totalPages",
                pagination.get("last_page", pagination.get("total_pages", None)),
            )
            total_items_hint = pagination.get(
                "total", pagination.get("count", None),
            )

            logger.info(
                "[Salla:%s] tenant=%s page %d → %d items (cumulative=%d%s)",
                tag, self._tenant_id, page, len(items), len(all_items),
                f", total_pages={total_pages_hint}" if total_pages_hint else "",
            )

            if not items:
                break
            if total_pages_hint and page >= total_pages_hint:
                break
            if len(items) < per_page:
                break

            page += 1

        logger.info(
            "[Salla:%s] tenant=%s pagination complete — %d total items across %d pages",
            tag, self._tenant_id, len(all_items), page,
        )
        return all_items

    # ── Products ───────────────────────────────────────────────────────────────

    async def get_products(self, updated_since: Optional[str] = None) -> List[NormalizedProduct]:
        try:
            extra: Optional[Dict[str, Any]] = None
            if updated_since:
                extra = {"updated_at_min": updated_since}
            raw_list = await self._get_all_pages("/products", label="products", extra_params=extra)
            return [self._normalize_product(p) for p in raw_list]
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
            raw = data.get("data") or {}
            if not raw:
                return None

            # Salla's product detail endpoint occasionally returns the
            # product without its `options` array (depends on store
            # config / API version). Hit the dedicated `/products/{id}
            # /options` endpoint to ensure we always know the option
            # groups before creating the order — Salla rejects the order
            # with a 422 ("خيارات المنتج مطلوبة") if a required option
            # value is missing, so this fetch is critical for stores
            # that use sizes/colors/variants.
            if not raw.get("options"):
                try:
                    opt_data = await self._get(f"/products/{product_id}/options")
                    fallback_opts = opt_data.get("data") or []
                    if isinstance(fallback_opts, list) and fallback_opts:
                        raw["options"] = fallback_opts
                        logger.info(
                            "[SallaAdapter] product options fetched via /products/%s/options | count=%d",
                            product_id, len(fallback_opts),
                        )
                except httpx.HTTPStatusError as opt_exc:
                    # Endpoint not available on every Salla plan/scope.
                    # Log and continue — order flow will surface this
                    # later as required-options-missing if applicable.
                    logger.info(
                        "[SallaAdapter] /products/%s/options unavailable (%s) — "
                        "using detail-endpoint options only",
                        product_id, opt_exc.response.status_code,
                    )
                except Exception as opt_exc:
                    logger.info(
                        "[SallaAdapter] product options fallback fetch failed | "
                        "product=%s err=%s",
                        product_id, opt_exc,
                    )

            normalized = self._normalize_product(raw)
            logger.info(
                "[SallaAdapter] get_product | id=%s title=%r option_groups=%d "
                "has_required_options=%s",
                product_id,
                normalized.title,
                len(normalized.options or []),
                normalized.has_required_options,
            )
            return normalized
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

        options, has_required = self._normalize_options(raw.get("options") or [])

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
            options=options,
            has_required_options=has_required,
        )

    def _normalize_options(
        self, raw_options: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], bool]:
        """Convert a Salla `options` array into a stable, JSON-friendly shape.

        Salla returns each option group as
            {id, name, type, required, values: [{id, name, ...}, ...]}.
        We keep the same structure (so the adapter can refer back to ids
        when posting an order) and surface `has_required_options` so the
        Brain knows whether to ask the customer before creating the order.
        """
        out: List[Dict[str, Any]] = []
        has_required = False
        for opt in raw_options or []:
            if not isinstance(opt, dict):
                continue
            opt_id = opt.get("id")
            opt_name = (opt.get("name") or "").strip()
            opt_type = (opt.get("type") or "select")
            # Salla's product API is inconsistent about the `required`
            # flag — some payloads omit it entirely, others use
            # `is_required`. When neither is present we default to
            # `True` because sending a value that Salla considers
            # optional is harmless, but skipping a required option
            # triggers a 422 ("خيارات المنتج مطلوبة"). Erring on the
            # side of asking the customer is the safer trade-off.
            if "required" in opt:
                opt_required = bool(opt.get("required"))
            elif "is_required" in opt:
                opt_required = bool(opt.get("is_required"))
            else:
                opt_required = True
            values_raw = opt.get("values") or []
            values_out: List[Dict[str, Any]] = []
            for val in values_raw:
                if not isinstance(val, dict):
                    continue
                values_out.append({
                    "id": val.get("id"),
                    "name": (val.get("name") or "").strip(),
                    "price": val.get("price"),
                    "image_url": val.get("image_url") or val.get("image"),
                })
            if not opt_name:
                continue
            out.append({
                "id": opt_id,
                "name": opt_name,
                "type": opt_type,
                "required": opt_required,
                "values": values_out,
            })
            if opt_required and values_out:
                has_required = True
        return out, has_required

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

    async def _assert_required_options_present(self, order_input: OrderInput) -> None:
        """Last-line defence against Salla 422 (`خيارات المنتج مطلوبة`).

        For every order item that arrives WITHOUT any options selected,
        fetch the product from Salla and check whether it has any
        required option groups. If yes, abort the order — do NOT call
        POST /orders. Raises ``ValueError("required_product_options_missing")``.

        We only network-hit Salla when the caller forgot to supply
        options, so happy-path orders pay no extra cost.
        """
        for item in order_input.items or []:
            if item.options:
                continue
            pid = str(item.product_id or "").strip()
            if not pid:
                continue
            try:
                product = await self.get_product(pid)
            except Exception as exc:
                logger.warning(
                    "[SallaAdapter] options pre-flight: get_product failed | "
                    "product=%s err=%s — proceeding anyway",
                    pid, exc,
                )
                continue
            if not product:
                continue
            required_groups = [
                g for g in (product.options or [])
                if g.get("required") and g.get("values")
            ]
            if required_groups:
                logger.error(
                    "[SallaAdapter] BLOCKING create_order: product has required options but none supplied | "
                    "tenant=%s product=%s required_groups=%s",
                    self._tenant_id, pid,
                    [g.get("name") for g in required_groups],
                )
                raise ValueError("required_product_options_missing")

    async def create_order(self, order_input: OrderInput) -> NormalizedOrder:
        await self._assert_required_options_present(order_input)
        shipping_company_id = order_input.shipping_company_id
        if not shipping_company_id:
            shipping_company_id = await self._get_default_shipping_company_id(order_input.city)
        body = self._build_order_body(order_input, draft=False, shipping_company_id=shipping_company_id)
        try:
            data = await self._post("/orders", body)
            return self._normalize_order(data.get("data", data), order_input)
        except Exception as exc:
            self._log_error("create_order", exc)
            raise

    async def create_draft_order(self, order_input: OrderInput) -> NormalizedOrder:
        await self._assert_required_options_present(order_input)
        # ── Shipping resolution ───────────────────────────────────────────────────
        # Auto-resolve the default shipping company if not already cached.
        # We never ask the customer for shipping; we just pick Salla's first zone.
        shipping_company_id = order_input.shipping_company_id
        if not shipping_company_id:
            logger.info(
                "[ORDER FLOW] resolving shipping method | tenant=%s city=%r",
                self._tenant_id, order_input.city,
            )
            shipping_company_id = await self._get_default_shipping_company_id(order_input.city)
            if shipping_company_id:
                logger.info(
                    "[ORDER FLOW] selected default shipping method | company_id=%s tenant=%s",
                    shipping_company_id, self._tenant_id,
                )
            else:
                logger.info(
                    "[ORDER FLOW] shipping method unavailable, proceeding without | tenant=%s city=%r",
                    self._tenant_id, order_input.city,
                )
        else:
            logger.info(
                "[ORDER FLOW] using cached shipping method | company_id=%s tenant=%s",
                shipping_company_id, self._tenant_id,
            )

        body = self._build_order_body(order_input, draft=True, shipping_company_id=shipping_company_id)
        try:
            data = await self._post("/orders", body)
            order = self._normalize_order(data.get("data", data), order_input)

            # ── Payment URL fallback ──────────────────────────────────────────────
            # Salla does not always embed the payment URL in the create response.
            # If it is missing, make one extra GET /orders/{id} call to fetch it.
            if not order.payment_link and order.id:
                logger.info(
                    "[ORDER FLOW] payment url absent in create response, fetching separately "
                    "| order_id=%s tenant=%s",
                    order.id, self._tenant_id,
                )
                try:
                    fetched_url = await self.generate_payment_link(order.id, order.total)
                    if fetched_url:
                        order.payment_link = fetched_url
                        logger.info(
                            "[ORDER FLOW] payment url fetched via GET /orders | "
                            "order_id=%s url=%s tenant=%s",
                            order.id, fetched_url, self._tenant_id,
                        )
                except Exception as _fetch_exc:
                    logger.warning(
                        "[ORDER FLOW] payment url fetch failed (non-blocking) | "
                        "order_id=%s err=%s tenant=%s",
                        order.id, _fetch_exc, self._tenant_id,
                    )

            return order
        except Exception as exc:
            self._log_error("create_draft_order", exc)
            raise

    async def _get_default_shipping_company_id(self, city: str = "") -> Optional[int]:
        """Return the Salla zone/company ID of the first available shipping option.

        Tries with the customer's city first, falls back to no filter.
        Returns None if no zones are configured or the API call fails.
        """
        for attempt_city in ([city, ""] if city else [""]):
            try:
                params: Dict[str, str] = {}
                if attempt_city:
                    params["city"] = attempt_city
                data = await self._get("/shipping/zones", params)
                zones = data.get("data") or []
                if zones:
                    zone_id = zones[0].get("id")
                    if zone_id is not None:
                        return int(zone_id)
            except Exception as exc:
                logger.warning(
                    "[SallaAdapter] _get_default_shipping_company_id failed | "
                    "city=%r attempt=%r err=%s",
                    city, attempt_city, exc,
                )
        return None

    @staticmethod
    def _normalize_mobile(phone: str) -> str:
        """Normalise to E.164 (+966XXXXXXXXX) — Salla Admin API v2 requires this format.

        Salla's 422 response confirms:
          "رقم الهاتف يجب ان يبدأ بـ + متبوعا برقم الدولة"
          (Phone number must start with + followed by country code)

        WhatsApp gives us either +966XXXXXXXXX, 966XXXXXXXXX, or 0XXXXXXXXX.
        All three must be normalised to +966XXXXXXXXX.

        Examples:
          +966555906901  →  +966555906901  (already correct)
           966555906901  →  +966555906901
           0555906901    →  +966555906901  (Saudi local → E.164)
        """
        raw = (phone or "").strip().replace(" ", "").replace("-", "")
        if raw.startswith("+"):
            return raw                          # already E.164
        if raw.startswith("966") and len(raw) >= 12:
            return f"+{raw}"                   # 966XXXXXXXXX → +966XXXXXXXXX
        if raw.startswith("0") and len(raw) == 10:
            return f"+966{raw[1:]}"            # 0XXXXXXXXX  → +966XXXXXXXXX
        # Fallback: prepend + if it looks like digits
        if raw.isdigit():
            return f"+{raw}"
        return raw

    def _build_order_body(
        self,
        order_input: OrderInput,
        draft: bool,
        shipping_company_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        # ── Products (Salla Admin API v2 requires identifier + identifier_type) ──
        products = []
        for item in order_input.items:
            entry: Dict[str, Any] = {
                "identifier_type": "id",
                "identifier": str(int(item.product_id)),
                "quantity": item.quantity,
            }
            if item.variant_id:
                try:
                    entry["variant_id"] = int(item.variant_id)
                except (TypeError, ValueError):
                    entry["variant_id"] = item.variant_id
            # Salla expects {"id": option_id, "value": value_id} per option
            # group. We accept either explicit value_id (preferred) or fall
            # back to value_name when the merchant uses free-text options.
            if item.options:
                opts_payload: List[Dict[str, Any]] = []
                for sel in item.options:
                    if not isinstance(sel, dict):
                        continue
                    _oid = sel.get("option_id") if "option_id" in sel else sel.get("id")
                    _vid = sel.get("value_id") if "value_id" in sel else sel.get("value")
                    if _oid is None:
                        continue
                    if _vid is not None:
                        opts_payload.append({"id": _oid, "value": _vid})
                    elif sel.get("value_name"):
                        opts_payload.append({"id": _oid, "value": sel["value_name"]})
                if opts_payload:
                    entry["options"] = opts_payload
                logger.info(
                    "[SallaAdapter] item options built | product=%s raw=%s payload=%s",
                    item.product_id, item.options, opts_payload,
                )
            else:
                logger.info(
                    "[SallaAdapter] item options EMPTY | product=%s",
                    item.product_id,
                )
            products.append(entry)

        # ── Phone — Salla requires E.164 (+966XXXXXXXXX) ────────────────────────
        mobile = self._normalize_mobile(order_input.customer_phone)
        logger.info(
            "[SallaAdapter] phone normalization | raw=%r normalized=%r tenant=%s",
            order_input.customer_phone, mobile, self._tenant_id,
        )

        # ── Customer name ────────────────────────────────────────────────────────
        _first = (order_input.customer_first_name or "").strip()
        _last  = (order_input.customer_last_name  or "").strip()
        if not _first:
            _parts = (order_input.customer_name or "").strip().split()
            _first = _parts[0] if _parts else ""
            if not _last:
                _last = " ".join(_parts[1:]) if len(_parts) > 1 else ""

        # ── Payment — Salla Admin API v2:
        #   `payment.accepted_methods` is REQUIRED by Salla (422 otherwise:
        #   "حقل وسائل الدفع المتاحة مطلوب"). The slugs must be a subset of
        #   the methods the merchant has enabled in Salla. The only slug
        #   that is guaranteed to be enabled on every store is `cod` (cash
        #   on delivery), so that is the safe default. Operators who want
        #   online payment can override via env (comma-separated):
        #     SALLA_DEFAULT_PAYMENT_METHODS=mada,cod,credit_card
        import os as _os
        _methods_env = (_os.environ.get("SALLA_DEFAULT_PAYMENT_METHODS") or "").strip()
        if _methods_env:
            _accepted_methods = [m.strip() for m in _methods_env.split(",") if m.strip()]
        else:
            _accepted_methods = ["cod"]
        payment_block: Dict[str, Any] = {
            "status": "pending_payment",
            "accepted_methods": _accepted_methods,
        }

        body: Dict[str, Any] = {
            "products": products,
            "customer": {
                "first_name": _first or (order_input.customer_name or "عميل"),
                "last_name":  _last,
                "mobile":     mobile,
            },
            "payment": payment_block,
        }
        if order_input.customer_email:
            body["customer"]["email"] = order_input.customer_email

        # ── Shipping ─────────────────────────────────────────────────────────────
        # Pass the resolved shipping company/zone ID to Salla.
        # If no ID was resolved, omit the block and let Salla use store defaults.
        _sid = shipping_company_id or order_input.shipping_company_id
        if _sid:
            body["shipping"] = {"company_id": _sid}

        # Build address block — include city and short address code whenever available.
        # ── Address ──────────────────────────────────────────────────────────────
        # Saudi customers typically supply a national short address code (TAPA7401)
        # with a city. Salla rejects the bare alphanumeric code as a street value
        # ("street must be a readable address"), so when no real street is
        # available we synthesise a human-readable fallback such as
        # "الطائف - الرمز الوطني TAPA7401" or "العنوان عبر الرمز الوطني: TAPA7401".
        # If a Google Maps URL was provided, it gets a sensible textual fallback
        # too. The raw code itself is still preserved in the order note for the
        # merchant to see.
        street_val = (order_input.street or order_input.address or "").strip()
        _short_code_clean = (order_input.short_address_code or "").strip().upper()
        _maps_url_clean = (order_input.google_maps_url or "").strip()

        if not street_val and _short_code_clean:
            if order_input.city:
                street_val = f"{order_input.city.strip()} - الرمز الوطني {_short_code_clean}"
            else:
                street_val = f"العنوان عبر الرمز الوطني {_short_code_clean}"
        elif not street_val and _maps_url_clean:
            if order_input.city:
                street_val = f"{order_input.city.strip()} - الموقع عبر خرائط Google"
            else:
                street_val = "الموقع عبر خرائط Google"

        if order_input.city or street_val:
            addr: Dict[str, Any] = {}
            if order_input.city:
                addr["city"] = order_input.city
            if street_val:
                addr["street"] = street_val
            if order_input.building_number:
                addr["building_number"] = order_input.building_number
            if order_input.district:
                addr["district"] = order_input.district
            if order_input.postal_code:
                addr["zip_code"] = order_input.postal_code
            if order_input.additional_number:
                addr["additional_number"] = order_input.additional_number
            # Salla expects a country on shipping address; default to Saudi Arabia.
            addr.setdefault("country", "Saudi Arabia")
            body["address"] = addr

        # ── Notes (human-readable) ───────────────────────────────────────────────
        notes_parts = []
        if order_input.notes:
            notes_parts.append(order_input.notes)
        if order_input.short_address_code and order_input.short_address_code not in (order_input.notes or ""):
            notes_parts.append(f"العنوان الوطني: {order_input.short_address_code}")
        if order_input.google_maps_url and order_input.google_maps_url not in (order_input.notes or ""):
            notes_parts.append(f"خريطة: {order_input.google_maps_url}")
        if notes_parts:
            body["note"] = " | ".join(notes_parts)   # Salla uses "note" (singular)

        if draft:
            body["status"] = "under_review"   # Salla draft-equivalent status

        logger.info(
            "[SallaAdapter] _build_order_body | product_id=%s city=%s short_code=%s "
            "mobile=%s has_street=%s",
            products[0]["identifier"] if products else "?",
            order_input.city or "",
            order_input.short_address_code or "",
            mobile,
            bool(street_val),
        )
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

    async def get_orders(self, updated_since: Optional[str] = None) -> List[NormalizedOrder]:
        extra: Optional[Dict[str, Any]] = None
        if updated_since:
            date_only = str(updated_since).split("T", 1)[0]
            extra = {"from_date": date_only}
        try:
            raw_list = await self._get_all_pages("/orders", label="orders", extra_params=extra)
            return [self._normalize_order(o, None) for o in raw_list]
        except httpx.HTTPStatusError as exc:
            self._log_error("get_orders", exc)
            logger.error(
                "Salla get_orders HTTP error %s: %s",
                exc.response.status_code,
                exc.response.text[:300],
            )
            raise
        except Exception as exc:
            self._log_error("get_orders", exc)
            raise

    async def get_customer_orders(self, customer_phone: str) -> List[NormalizedOrder]:
        try:
            data = await self._get("/orders", {"mobile": customer_phone, "per_page": 10})
            return [self._normalize_order(o, None) for o in data.get("data", [])]
        except Exception as exc:
            self._log_error("get_customer_orders", exc)
            return []

    # ── Abandoned carts ────────────────────────────────────────────────────────
    #
    # Salla's `/orders` endpoint NEVER returns abandoned carts — those live
    # behind the dedicated Merchant API endpoint:
    #
    #     GET https://api.salla.dev/admin/v2/carts/abandoned
    #
    # docs:  https://docs.salla.dev/api-5394138 (List Abandoned Carts)
    # scope: ``carts.read``
    #
    # CRITICAL: an earlier version of this adapter called ``/carts``
    # (without the ``/abandoned`` suffix). That path silently returns a
    # 404 / empty body on Salla, which is then swallowed by the
    # ``except`` blocks below — the symptom on the merchant's screen is
    # that "Salla shows N abandoned carts" while Nahla's dashboard sits
    # on zero forever. The fix below restores the documented path.
    #
    # The Salla response shape is:
    #   { "status": 200, "success": true,
    #     "data": [ { "id": ..., "total": {amount,currency},
    #                 "checkout_url": "...", "customer": {...},
    #                 "items": [...], "created_at": {date,timezone,...} } ],
    #     "pagination": {count,total,perPage,currentPage,totalPages,links} }
    #
    # ``_get_all_pages`` already extracts the ``data`` array and walks
    # pagination — we just need the right URL.
    async def get_abandoned_carts(self) -> List[Dict[str, Any]]:
        """Fetch all abandoned carts from Salla.

        Returns the raw cart dicts (not normalized into NormalizedOrder) so
        the sync layer can preserve cart-specific fields like ``checkout_url``
        and ``items`` exactly as Salla returns them. Never raises — returns
        an empty list on any error so the orders sync pipeline keeps moving.
        """
        try:
            return await self._get_all_pages(
                "/carts/abandoned", label="abandoned_carts",
            )
        except SallaTokenRevokedException:
            raise
        except httpx.HTTPStatusError as exc:
            self._log_error("get_abandoned_carts", exc)
            logger.error(
                "Salla get_abandoned_carts HTTP %s: %s",
                exc.response.status_code, exc.response.text[:300],
            )
            return []
        except Exception as exc:
            self._log_error("get_abandoned_carts", exc)
            return []

    def _normalize_order(self, raw: Dict[str, Any], order_input: Optional[OrderInput]) -> NormalizedOrder:
        amounts = raw.get("amounts") or {}

        # Salla returns `amounts.total` either as `{"amount": 100, "currency": "SAR"}`
        # or as a flat number depending on endpoint. Some endpoints (notably the
        # listing endpoint) put the grand total at `raw["total"]` directly. Fall
        # through to every plausible shape so we never silently store 0.0 for a
        # real order.
        total = 0.0
        currency = "SAR"
        for candidate in (
            amounts.get("total"),
            amounts.get("sub_total"),
            raw.get("total"),
            raw.get("amount"),
            raw.get("price"),
        ):
            if candidate is None:
                continue
            if isinstance(candidate, dict):
                amt = candidate.get("amount") or candidate.get("value") or 0
                cur = candidate.get("currency")
                try:
                    parsed = float(amt or 0)
                except (TypeError, ValueError):
                    parsed = 0.0
                if parsed > 0:
                    total = parsed
                    if cur:
                        currency = str(cur)
                    break
            else:
                try:
                    parsed = float(candidate or 0)
                except (TypeError, ValueError):
                    parsed = 0.0
                if parsed > 0:
                    total = parsed
                    break

        # Salla returns the payment URL under several possible keys depending on
        # endpoint version. Check all known shapes before falling back to None.
        _urls = raw.get("urls") or {}
        payment_link = (
            raw.get("payment_url")
            or raw.get("checkout_url")
            or _urls.get("payment")
            or _urls.get("checkout")
            or _urls.get("pay")
        )

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
        cname = str(customer.get("name") or (order_input.customer_name if order_input else "") or "")
        cphone = str(customer.get("mobile") or (order_input.customer_phone if order_input else "") or "")

        # CRITICAL: Salla returns `status` as a dict like
        #   {"id": 566146469, "name": "بإنتظار المراجعة", "slug": "under_review",
        #    "customized": {...}}
        # `str(dict)` produced a Python repr (e.g. "{'id': 566146469, ...}") which
        # poisoned every downstream consumer (dashboard, customer classifier,
        # automations). Always extract the canonical slug; fall back to name then
        # to the literal string so unrecognized shapes are still searchable.
        status_raw = raw.get("status")
        if isinstance(status_raw, dict):
            status_str = str(
                status_raw.get("slug")
                or status_raw.get("name")
                or status_raw.get("code")
                or "pending"
            ).strip()
        elif status_raw is None:
            status_str = "pending"
        else:
            status_str = str(status_raw).strip() or "pending"

        # Salla sends `created_at` as either a plain ISO string or
        # `{"date": "2026-04-15 12:00:00.000000", "timezone_type": 3,
        #   "timezone": "Asia/Riyadh"}`. Preserve the inner date string when nested.
        created_raw = raw.get("created_at") or raw.get("date") or ""
        if isinstance(created_raw, dict):
            created_str = str(created_raw.get("date") or "")
        else:
            created_str = str(created_raw or "")

        # Salla returns BOTH `id` (internal numeric primary key) and
        # `reference_id` (the human-visible order number the merchant sees
        # in their Salla dashboard, e.g. 1585297702). We want to keep
        # using `id` for stable upserts but also expose `reference_id`
        # to the dashboard so merchants see the same number Salla shows.
        internal_id = str(raw.get("id") or raw.get("reference_id", "")).strip()
        reference   = str(raw.get("reference_id") or raw.get("id", "")).strip()

        return NormalizedOrder(
            id=internal_id,
            reference_id=reference or internal_id,
            status=status_str,
            total=total,
            currency=currency,
            payment_link=payment_link,
            customer_name=cname,
            customer_phone=cphone,
            items=items,
            created_at=created_str,
            source="salla",
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
                zone_id = zone.get("id")
                options.append(ShippingOption(
                    name=zone.get("name") or zone.get("courier_name") or "شحن",
                    cost=float(cost_entry.get("amount", 0) or 0),
                    currency=cost_entry.get("currency", "SAR"),
                    estimated_days=str(zone.get("min_days", "")) or None,
                    zone=zone.get("name"),
                    courier=zone.get("courier_name"),
                    company_id=int(zone_id) if zone_id is not None else None,
                ))
            return options
        except Exception as exc:
            self._log_error("get_shipping_options", exc)
            return []

    # ── Customers ──────────────────────────────────────────────────────────────

    async def get_customers(self, updated_since: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch all customers from Salla across all pages until exhaustion."""
        try:
            extra: Optional[Dict[str, Any]] = None
            if updated_since:
                extra = {"updated_at_min": updated_since}
            return await self._get_all_pages("/customers", label="customers", extra_params=extra)
        except Exception as exc:
            self._log_error("get_customers", exc)
            return []

    # ── Offers / Coupons ──────────────────────────────────────────────────────

    async def get_coupons(self) -> List[Dict[str, Any]]:
        """Return raw coupon dicts from Salla across all pages until exhaustion."""
        try:
            return await self._get_all_pages("/coupons", label="coupons")
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
        """Create a coupon in Salla. Returns the created coupon data or None.

        Salla Admin API v2 (verified live, April 2026) expects:
          type   = "percentage" | "fixed"   (lowercase)
          amount = numeric discount value   (single field; no percent_off/amount_off)
        The previous uppercase/split-field shape is rejected with
        422 alert.invalid_fields{type, amount}.
        """
        start_dt = datetime.now(timezone.utc)
        expiry_dt = start_dt + timedelta(days=expiry_days)
        start  = start_dt.strftime("%Y-%m-%d")
        expiry = expiry_dt.strftime("%Y-%m-%d")

        salla_type = "percentage" if discount_type in ("percentage", "PERCENT", "percent") else "fixed"

        payload = {
            "code":                   code,
            "type":                   salla_type,
            "amount":                 int(discount_value),
            "start_date":             start,
            "expiry_date":            expiry,
            "free_shipping":          False,
            "exclude_sale_products":  False,
        }
        try:
            data = await self._post("/coupons", payload)
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                data["data"].setdefault("expires_at", expiry_dt.isoformat())
                data["data"].setdefault("expiry_date", expiry)
            elif isinstance(data, dict):
                data.setdefault("expires_at", expiry_dt.isoformat())
            logger.info("Salla coupon created: %s | tenant=%s", code, self._tenant_id)
            return data.get("data", data)
        except httpx.HTTPStatusError as exc:
            self._log_error("create_coupon", exc)
            logger.error(
                "Salla create_coupon HTTP %s: %s",
                exc.response.status_code, exc.response.text[:500],
            )
            return None
        except Exception as exc:
            self._log_error("create_coupon", exc)
            return None

    async def delete_coupon_by_code(self, code: str) -> bool:
        """
        Delete a Salla coupon by its code. Used for compensation when we
        created a coupon in Salla but the local DB insert then failed — we
        must remove the orphan to keep the two sides in sync.

        Returns True if Salla confirms deletion (or the coupon is already
        gone), False on any other failure. Never raises.
        """
        if not code:
            return False
        try:
            data = await self._get("/coupons", {"code": code, "per_page": 1})
            rows = data.get("data") or [] if isinstance(data, dict) else []
            if not rows:
                return True
            target = rows[0]
            coupon_id = target.get("id") if isinstance(target, dict) else None
            if not coupon_id:
                return False
            return await self._delete(f"/coupons/{coupon_id}")
        except Exception as exc:
            self._log_error("delete_coupon_by_code", exc)
            return False

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
