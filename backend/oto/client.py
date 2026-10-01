"""Small, explicit OTO v2 transport. No credentials or response bodies are logged."""
from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote

import httpx


class OtoApiError(RuntimeError):
    def __init__(self, operation: str, status_code: int | None = None):
        self.operation = operation
        self.status_code = status_code
        super().__init__(f"oto_{operation}_failed")


class OtoClient:
    """Use a merchant refresh token for shipment operations.

    Marketplace registration uses a distinct token and is intentionally not
    mixed into this client. A master token must never ship a merchant order.
    """

    def __init__(
        self,
        refresh_token: str,
        *,
        base_url: str = "https://api.tryoto.com/rest/v2",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not refresh_token:
            raise ValueError("oto_refresh_token_missing")
        if base_url not in {
            "https://api.tryoto.com/rest/v2",
            "https://staging-api.tryoto.com/rest/v2",
        }:
            raise ValueError("oto_base_url_invalid")
        self._refresh_token = refresh_token
        self._base_url = base_url
        self._transport = transport
        self._access_token: str | None = None

    async def _request(
        self, method: str, path: str, *,
        payload: Mapping[str, Any] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if authenticated:
            if not self._access_token:
                await self._refresh()
            headers["Authorization"] = f"Bearer {self._access_token}"
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(15.0),
            transport=self._transport,
        ) as client:
            try:
                response = await client.request(method, path, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                raise OtoApiError(path) from exc
        if response.status_code >= 400:
            raise OtoApiError(path, response.status_code)
        try:
            result = response.json()
        except ValueError as exc:
            raise OtoApiError(path, response.status_code) from exc
        if not isinstance(result, dict) or result.get("success") is False:
            raise OtoApiError(path, response.status_code)
        return result

    async def _refresh(self) -> None:
        result = await self._request(
            "POST", "/refreshToken",
            payload={"refresh_token": self._refresh_token},
            authenticated=False,
        )
        token = result.get("access_token") or result.get("accessToken")
        if not isinstance(token, str) or not token:
            raise OtoApiError("refreshToken")
        self._access_token = token

    async def check_oto_delivery_fee(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/checkOTODeliveryFee", payload=payload)

    async def list_pickup_locations(self) -> dict[str, Any]:
        return await self._request("GET", "/getPickupLocationList")

    async def create_pickup_location(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/createPickupLocation", payload=payload)

    async def create_order(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/createOrder", payload=payload)

    async def create_shipment(self, order_id: str, delivery_option_id: int) -> dict[str, Any]:
        return await self._request(
            "POST", "/createShipment",
            payload={"orderId": order_id, "deliveryOptionId": delivery_option_id},
        )

    async def order_status(self, order_id: str) -> dict[str, Any]:
        return await self._request("POST", "/orderStatus", payload={"orderId": order_id})

    async def print_awb(self, order_id: str) -> dict[str, Any]:
        if not order_id or "/" in order_id:
            raise ValueError("oto_order_id_invalid")
        return await self._request("GET", f"/print/{quote(order_id, safe='')}")

    async def cancel_shipment(self, order_id: str, shipment_id: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/cancelShipment",
            payload={"orderId": order_id, "shipmentId": shipment_id},
        )
