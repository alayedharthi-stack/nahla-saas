"""
Read-only GraphQL Admin API client for the catalog import, pinned to 2026-10.

* Requests go only to ``https://<canonical shop>/admin/api/2026-10/graphql.json``
  (POST, JSON body, ``X-Shopify-Access-Token`` header). Redirects are never
  followed. The token never appears in a URL, a log line, an exception or a
  ``repr``.
* Every 200 response must carry exactly one ``X-Shopify-API-Version: 2026-10``
  header. Shopify serves an unsupported requested version with a newer one
  ("fall forward"); that is refused, never parsed.
* Any GraphQL ``errors`` entry refuses the whole response, even when ``data``
  is present: partial data is never accepted. ``THROTTLED`` is retried with a
  bounded wait; ``ACCESS_DENIED`` and ``MAX_COST_EXCEEDED`` end the run.
* Bounded everything: response size (streamed); each attempt as a whole —
  connect, send and the complete streamed body — by the smaller of a total
  per-request limit and the run's remaining budget (httpx's read timeout
  alone resets on every chunk, so a slow trickle could otherwise run
  forever); HTTP retries (429 with ``Retry-After``, 5xx, transport failures,
  timeouts); throttle retries; every single wait; and the run deadline,
  re-checked after every await.
* Fenced: the caller's async ``fence`` runs before **every** attempt
  (including each retry after a throttle or backoff wait) and again after a
  response arrives, before it is parsed. It re-checks the run lease, the
  connection's status / generation / shop identity and that the credential
  the token came from is still the stored one; when it raises (lease lost,
  disconnect, uninstall, quarantine, reinstall, rotation) the token is not
  used again and the response, if any, is discarded.
* Cost-aware pacing: the last reported ``throttleStatus`` (currently
  available points and restore rate) delays the next request until its
  requested cost is available.

Upstream bodies, messages and headers other than the version and
``Retry-After`` are never logged, stored or returned.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

import httpx

from services.shopify_catalog import errors as E
from services.shopify_catalog.config import (
    CATALOG_API_VERSION,
    MAX_HTTP_RETRIES,
    MAX_RESPONSE_BYTES,
    MAX_THROTTLE_RETRIES,
    MAX_WAIT_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    REQUEST_TOTAL_SECONDS,
)
from services.shopify_connection.shop_domain import canonical_shop_domain

logger = logging.getLogger("nahla.shopify_catalog")

Sleep = Callable[[float], Awaitable[None]]
Fence = Callable[[], Awaitable[None]]


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _retry_after(raw: Optional[str]) -> Optional[float]:
    """Seconds from a numeric ``Retry-After`` (HTTP dates are not trusted)."""
    if raw is None:
        return None
    text = raw.strip()
    if not text or len(text) > 12 or not all(ch.isdigit() or ch == "." for ch in text) or text.count(".") > 1:
        return None
    try:
        seconds = float(text)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) else None


class AdminGraphQL:
    """One instance per import run (it carries that run's throttle state)."""

    def __init__(
        self,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        sleep: Optional[Sleep] = None,
        monotonic: Optional[Callable[[], float]] = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        request_total: float = REQUEST_TOTAL_SECONDS,
        deadline: Optional[float] = None,
    ):
        self._transport = transport
        self._sleep = sleep or asyncio.sleep
        self._monotonic = monotonic or time.monotonic
        self._timeout = timeout
        self._request_total = request_total
        self.deadline = deadline
        self._available: Optional[float] = None
        self._restore: Optional[float] = None
        self.requests = 0
        self.waited_seconds = 0.0

    def __repr__(self) -> str:
        return f"AdminGraphQL(api_version={CATALOG_API_VERSION!r}, requests={self.requests})"

    __str__ = __repr__

    @staticmethod
    def endpoint(shop_domain: str) -> str:
        shop = canonical_shop_domain(shop_domain)
        if shop is None or shop != shop_domain:
            raise E.ImportFailure(E.R_INVALID_IDENTITY)
        return f"https://{shop}/admin/api/{CATALOG_API_VERSION}/graphql.json"

    async def _wait(self, seconds: float) -> None:
        seconds = min(max(seconds, 0.0), MAX_WAIT_SECONDS)
        if seconds <= 0:
            return
        if self.deadline is not None and self._monotonic() + seconds > self.deadline:
            raise E.ImportFailure(E.R_DEADLINE_EXCEEDED)
        self.waited_seconds += seconds
        await self._sleep(seconds)

    def _record_cost(self, body: Mapping[str, Any]) -> Optional[float]:
        """Remember the bucket; returns the requested cost when reported."""
        extensions = body.get("extensions") if isinstance(body.get("extensions"), dict) else {}
        cost = extensions.get("cost") if isinstance(extensions.get("cost"), dict) else {}
        status = cost.get("throttleStatus") if isinstance(cost.get("throttleStatus"), dict) else {}
        available, restore = _number(status.get("currentlyAvailable")), _number(status.get("restoreRate"))
        if available is not None and restore is not None and restore > 0:
            self._available, self._restore = available, restore
        return _number(cost.get("requestedQueryCost"))

    async def _pace(self, cost_hint: int) -> None:
        if self._available is None or self._restore is None or self._available >= cost_hint:
            return
        await self._wait((cost_hint - self._available) / self._restore)
        self._available = None   # refreshed by the next response

    def _check_deadline(self) -> None:
        if self.deadline is not None and self._monotonic() >= self.deadline:
            raise E.ImportFailure(E.R_DEADLINE_EXCEEDED)

    def _attempt_budget(self) -> float:
        """Seconds one whole attempt may take: the total per-request limit,
        cut to the run's remaining budget."""
        budget = self._request_total
        if self.deadline is not None:
            remaining = self.deadline - self._monotonic()
            if remaining <= 0:
                raise E.ImportFailure(E.R_DEADLINE_EXCEEDED)
            budget = min(budget, remaining)
        return budget

    async def _post(self, url: str, payload: bytes, access_token: str):
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": access_token,
        }
        async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout,
                                     follow_redirects=False) as client:
            async with client.stream("POST", url, content=payload, headers=headers) as resp:
                status = int(resp.status_code)
                versions = resp.headers.get_list("x-shopify-api-version")
                retry_after = resp.headers.get("retry-after")
                received = bytearray()
                if status == 200:
                    async for chunk in resp.aiter_bytes():
                        received.extend(chunk)
                        if len(received) > MAX_RESPONSE_BYTES:
                            raise E.ImportFailure(E.R_RESPONSE_TOO_LARGE)
                return status, versions, retry_after, bytes(received)

    async def execute(
        self,
        *,
        shop_domain: str,
        access_token: str,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
        cost_hint: int = 100,
        fence: Fence,
    ) -> Dict[str, Any]:
        """``data`` of a complete, error-free 2026-10 response, or ImportFailure.

        ``fence`` is awaited before every attempt and after every response;
        whatever it raises propagates unchanged and ends this call."""
        url = self.endpoint(shop_domain)
        payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        http_failures = 0
        throttles = 0
        while True:
            self._check_deadline()
            await self._pace(cost_hint)
            await fence()
            self._check_deadline()
            self.requests += 1
            try:
                status, versions, retry_after, raw = await asyncio.wait_for(
                    self._post(url, payload, access_token), timeout=self._attempt_budget())
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                self._check_deadline()
                http_failures += 1
                logger.warning("[SHOPIFY_CATALOG] transport failure kind=%s attempt=%s",
                               type(exc).__name__, http_failures)
                if http_failures > MAX_HTTP_RETRIES:
                    raise E.ImportFailure(E.R_UPSTREAM_UNAVAILABLE) from None
                await self._wait(2.0 ** http_failures)
                continue
            self._check_deadline()
            await fence()
            if status == 429 or status >= 500:
                http_failures += 1
                logger.warning("[SHOPIFY_CATALOG] upstream status=%s attempt=%s", status, http_failures)
                if http_failures > MAX_HTTP_RETRIES:
                    raise E.ImportFailure(E.R_UPSTREAM_UNAVAILABLE)
                delay = _retry_after(retry_after) if status == 429 else None
                await self._wait(delay if delay is not None else 2.0 ** http_failures)
                continue
            if status in (401, 403):
                raise E.ImportFailure(E.R_CREDENTIALS_REJECTED)
            if status in (402, 404, 423):
                raise E.ImportFailure(E.R_SHOP_UNAVAILABLE)
            if status != 200:
                raise E.ImportFailure(E.R_INVALID_RESPONSE)
            if versions != [CATALOG_API_VERSION]:
                raise E.ImportFailure(E.R_API_VERSION_MISMATCH)
            try:
                body = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, ValueError):
                raise E.ImportFailure(E.R_INVALID_RESPONSE) from None
            if not isinstance(body, dict):
                raise E.ImportFailure(E.R_INVALID_RESPONSE)
            requested = self._record_cost(body)
            errors = body.get("errors")
            if errors is not None:
                codes = _error_codes(errors)
                if codes == {"THROTTLED"}:
                    throttles += 1
                    if throttles > MAX_THROTTLE_RETRIES:
                        raise E.ImportFailure(E.R_THROTTLED)
                    need = requested if requested is not None else float(cost_hint)
                    if self._available is not None and self._restore:
                        delay = max((need - self._available) / self._restore, 1.0)
                    else:
                        delay = 2.0 ** throttles
                    self._available = None
                    await self._wait(delay)
                    continue
                if "ACCESS_DENIED" in codes:
                    raise E.ImportFailure(E.R_SCOPE_DENIED)
                if "MAX_COST_EXCEEDED" in codes:
                    raise E.ImportFailure(E.R_QUERY_COST_EXCEEDED)
                if "THROTTLED" in codes:
                    raise E.ImportFailure(E.R_THROTTLED)
                raise E.ImportFailure(E.R_GRAPHQL_ERROR)
            data = body.get("data")
            if not isinstance(data, dict):
                raise E.ImportFailure(E.R_INVALID_RESPONSE)
            return data


def _error_codes(errors: Any) -> set:
    """Codes of a GraphQL ``errors`` value; an unreadable one is ``UNKNOWN``."""
    if not isinstance(errors, list) or not errors:
        return {"UNKNOWN"}
    codes = set()
    for entry in errors:
        extensions = entry.get("extensions") if isinstance(entry, dict) else None
        code = extensions.get("code") if isinstance(extensions, dict) else None
        codes.add(code if isinstance(code, str) and code.isascii() and len(code) <= 64 else "UNKNOWN")
    return codes
