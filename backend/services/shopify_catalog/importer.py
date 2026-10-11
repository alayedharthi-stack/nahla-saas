"""
One import run, end to end:

  preflight → [locations] → products (+ nested pages) → [inventory levels]
  → product count reconciliation → Shopify confirmation of every absence
  → single-transaction publish

Credentials come only from the foundation (``lifecycle.active_access_token``):
the access token lives in a local variable of this object for the duration of
the run, is sent only in the ``X-Shopify-Access-Token`` header, and is dropped
whenever the stored credential changes. The credential version the token
belongs to is read before and after acquiring it, so a token is only ever
paired with the version it came from; every request is fenced against that
version, the run lease and the connection's status / generation / shop.

Outcomes are fixed codes. A run that does not reach a complete, confirmed
snapshot publishes nothing: the previously committed mirror stays exactly as
it was, and the run is recorded as ``failed`` or ``cancelled`` with its reason.
A worker that lost its lease writes nothing at all.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional, Tuple

from services.shopify_catalog import errors as E
from services.shopify_catalog import parse, queries, store
from services.shopify_catalog.config import (
    MAX_CREDENTIAL_ROTATIONS,
    PAGINATION_CAP,
    RUN_DEADLINE_SECONDS,
    SCOPE_INVENTORY,
    SCOPE_PRODUCTS,
)
from services.shopify_catalog.graphql import AdminGraphQL
from services.shopify_catalog.models import (
    INVENTORY_PER_LOCATION,
    INVENTORY_SCOPE_MISSING,
    KIND_LOCATION,
    KIND_PRODUCT,
    KIND_VARIANT,
    RUN_CANCELLED,
    RUN_FAILED,
)
from services.shopify_catalog.traversal import CatalogTraversal

logger = logging.getLogger("nahla.shopify_catalog")

OUT_COMMITTED = "committed"
OUT_LEASE_LOST = "lease_lost"


class CatalogImporter:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        handle: store.RunHandle,
        api: Any,
        cipher: Any,
        client: Optional[AdminGraphQL] = None,
        sleep=None,
        monotonic: Optional[Callable[[], float]] = None,
        deadline_seconds: float = RUN_DEADLINE_SECONDS,
    ):
        self._sessions = session_factory
        self.handle = handle
        self._api = api
        self._cipher = cipher
        self._sleep = sleep or asyncio.sleep
        self._monotonic = monotonic or time.monotonic
        self.client = client or AdminGraphQL(sleep=self._sleep, monotonic=self._monotonic)
        if self.client.deadline is None:
            self.client.deadline = self._monotonic() + deadline_seconds
        self._token: Optional[Tuple[str, int]] = None
        self._stored_scopes: frozenset = frozenset()
        self.pages = 0
        self.rotations = 0

    def __repr__(self) -> str:
        return f"CatalogImporter(run_id={self.handle.run_id!r})"

    __str__ = __repr__

    # ── credentials and fences ───────────────────────────────────────────────

    def _db(self):
        return self._sessions()

    async def _acquire_token(self) -> Tuple[str, int]:
        from services.shopify_connection import lifecycle  # noqa: PLC0415

        if self._token is not None:
            return self._token
        for _ in range(3):
            with self._db() as db:
                before, scopes = store.credential_snapshot(db, self.handle)
            with self._db() as db:
                try:
                    token, generation = await lifecycle.active_access_token(
                        db, tenant_id=self.handle.tenant_id, connection_id=self.handle.connection_id,
                        api=self._api, cipher=self._cipher)
                except lifecycle.ConnectionRefused:
                    token = None
                    generation = None
            with self._db() as db:
                after, scopes = store.credential_snapshot(db, self.handle)   # classifies a lost connection
            if token is None:
                raise E.ImportFailure(E.R_CREDENTIAL_UNAVAILABLE)
            if int(generation) != self.handle.generation:
                token = None
                raise E.ImportFailure(E.R_CONNECTION_CHANGED)
            if before == after:
                self._token = (token, before)
                self._stored_scopes = scopes
                return self._token
            token = None   # rotated while acquiring: the pairing is uncertain, acquire again
        raise E.ImportFailure(E.R_CREDENTIAL_CHURN)

    def _drop_token(self) -> None:
        self._token = None

    async def _fence(self, credential_version: int) -> None:
        try:
            with self._db() as db:
                store.heartbeat(db, self.handle, credential_version=credential_version)
        except E.CredentialRotated:
            self._drop_token()
            raise

    async def _query(self, document: str, variables: Dict[str, Any], cost: int) -> Tuple[Dict[str, Any], int]:
        token, version = await self._acquire_token()

        async def fence() -> None:
            await self._fence(version)

        try:
            data = await self.client.execute(shop_domain=self.handle.shop_domain, access_token=token,
                                             query=document, variables=variables, cost_hint=cost, fence=fence)
        finally:
            token = None
        self.pages += 1
        return data, version

    async def _stage(self, records, credential_version: int) -> None:
        try:
            with self._db() as db:
                store.stage(db, self.handle, records, credential_version=credential_version, pages=self.pages)
        except E.CredentialRotated:
            self._drop_token()
            raise

    def _rotated(self) -> None:
        self._drop_token()
        self.rotations += 1
        if self.rotations > MAX_CREDENTIAL_ROTATIONS:
            raise E.ImportFailure(E.R_CREDENTIAL_CHURN)

    async def _preflight(self) -> Tuple[Dict[str, str], bool]:
        """Shop identity, currency and grant evidence, recorded under the same
        credential version they were read with. A rotation anywhere in
        between discards the whole reading and starts it again."""
        while True:
            try:
                data, version = await self._query(queries.PREFLIGHT, {}, 10)
                shop = parse.shop(data.get("shop"))
                if shop["gid"] != self.handle.shop_gid or shop["domain"] != self.handle.shop_domain:
                    raise E.ImportFailure(E.R_IDENTITY_MISMATCH)
                granted = parse.access_scopes(data.get("currentAppInstallation"))
                if SCOPE_PRODUCTS not in granted or SCOPE_PRODUCTS not in self._stored_scopes:
                    raise E.ImportFailure(E.R_SCOPE_MISSING)
                # Inventory per location only when Shopify itself reports the
                # scope AND the stored grant carries it; otherwise it is
                # explicitly not read (and reported as not read).
                per_location = SCOPE_INVENTORY in granted and SCOPE_INVENTORY in self._stored_scopes
                expected, precision = parse.count(data.get("productsCount"))
                if precision != "EXACT" or expected > PAGINATION_CAP:
                    raise E.ImportFailure(E.R_CATALOG_TOO_LARGE)
                mode = INVENTORY_PER_LOCATION if per_location else INVENTORY_SCOPE_MISSING
                with self._db() as db:
                    store.record_preflight(db, self.handle, currency=shop["currency"], inventory_mode=mode,
                                           expected_products=expected, credential_version=version)
                return shop, per_location
            except E.CredentialRotated:
                self._rotated()

    # ── the run ──────────────────────────────────────────────────────────────

    async def _execute(self) -> Dict[str, Any]:
        with self._db() as db:
            store.heartbeat(db, self.handle)
        shop, per_location = await self._preflight()
        traversal = CatalogTraversal(query=self._query, stage=self._stage, sleep=self._sleep,
                                     currency=shop["currency"])
        if per_location:
            await traversal.locations()
        await traversal.products()
        if per_location:
            await traversal.inventory()
        await traversal.verify_product_count()
        with self._db() as db:
            absent = store.absent_identities(db, self.handle, include_locations=per_location)
        pending = set().union(*absent.values()) if absent else set()
        await traversal.confirm_absent(pending)
        confirmed = {kind: set(gids) for kind, gids in absent.items()}
        with self._db() as db:
            result = store.publish(db, self.handle, confirmed_absent=confirmed, pages=self.pages)
        if traversal.snapshot.images_absent:
            logger.info("[SHOPIFY_CATALOG] run=%s images absent=%s", self.handle.run_id,
                        traversal.snapshot.images_absent)
        return result

    async def run(self) -> str:
        """``committed`` | ``failed`` | ``cancelled`` | ``lease_lost``."""
        try:
            result = await self._execute()
        except E.LeaseLost:
            logger.warning("[SHOPIFY_CATALOG] run=%s lease lost; nothing written", self.handle.run_id)
            return OUT_LEASE_LOST
        except E.ImportFailure as exc:
            return self._close(exc.reason)
        except E.CredentialRotated:
            return self._close(E.R_CREDENTIAL_CHURN)
        except Exception as exc:  # noqa: BLE001 — closed with a fixed code; only the type is logged
            logger.error("[SHOPIFY_CATALOG] run=%s failed kind=%s", self.handle.run_id, type(exc).__name__)
            return self._close(E.R_INTERNAL_ERROR)
        finally:
            self._drop_token()
        logger.info("[SHOPIFY_CATALOG] run=%s committed products=%s variants=%s", self.handle.run_id,
                    result["products_count"], result["variants_count"])
        return OUT_COMMITTED

    def _close(self, reason: str) -> str:
        status = RUN_CANCELLED if reason in E.CANCEL_REASONS else RUN_FAILED
        try:
            with self._db() as db:
                closed = store.finish(db, self.handle, status=status, reason=reason, pages=self.pages)
        except Exception as exc:  # noqa: BLE001 — the lease expires and the next start closes the run
            logger.error("[SHOPIFY_CATALOG] run=%s close failed kind=%s", self.handle.run_id, type(exc).__name__)
            return status
        logger.warning("[SHOPIFY_CATALOG] run=%s %s reason=%s", self.handle.run_id,
                       status if closed else "already closed", reason)
        return status if closed else OUT_LEASE_LOST


async def run_import_job(handle: store.RunHandle) -> str:
    """Background entry used by the router: production session factory and
    the foundation's configured API client and cipher. Re-checks the gate."""
    from core.database import SessionLocal  # noqa: PLC0415

    from services.shopify_catalog.config import import_routes_enabled  # noqa: PLC0415
    from services.shopify_connection import crypto  # noqa: PLC0415
    from services.shopify_connection.config import evaluate_availability  # noqa: PLC0415
    from services.shopify_connection.oauth import ShopifyApi  # noqa: PLC0415

    availability = evaluate_availability()
    importer_session = SessionLocal
    if not import_routes_enabled() or not availability.available:
        with importer_session() as db:
            store.finish(db, handle, status=RUN_FAILED, reason=E.R_CREDENTIAL_UNAVAILABLE)
        return RUN_FAILED
    config = availability.config
    api = ShopifyApi(client_id=config.client_id, client_secret=config.client_secret, api_version=config.api_version)
    importer = CatalogImporter(session_factory=importer_session, handle=handle, api=api,
                               cipher=crypto.TokenCipher(config.encryption_key))
    return await importer.run()
