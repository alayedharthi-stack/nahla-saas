"""
modules/ai/security/tenant_isolation.py
───────────────────────────────────────
Hard boundary enforcement for every AI retrieval / tool / execution path.

Every piece of code that touches store data MUST go through this layer.

Contract
────────
* All tenant-scoped queries are scoped via ``TenantIsolationLayer.scope_query``
  which appends ``Model.tenant_id == ctx.tenant_id`` defensively, even when
  the caller already filtered.  Double-filtering is harmless; missing-filter
  is fatal.
* Any DB row that carries a ``tenant_id`` attribute is checked with
  ``assert_belongs`` before its data is returned to a caller in a different
  tenant context.
* Tool payloads coming from the LLM are sanitised by ``verify_payload`` so a
  hallucinated ``tenant_id`` field cannot redirect a tool call to another
  store.

The class is intentionally **stateless** so it can be used from sync code
(SQLAlchemy queries) and async code (CommerceToolRuntime) alike without
extra plumbing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional

logger = logging.getLogger("nahla.ai.security.isolation")


class TenantIsolationViolation(RuntimeError):
    """Raised whenever an AI code path tries to read / write data that does
    not belong to the active tenant.

    This is intentionally a hard error (not a warning).  Any caller is
    expected to either fix the contract or catch + reject the request.  We
    must never degrade silently when isolation is breached.
    """


@dataclass(frozen=True)
class TenantContext:
    """Immutable view of the merchant whose turn we are processing.

    The ``industry`` field is optional and only used by the learning
    layer to bucket anonymized signals; it never affects retrieval or
    permissions.  ``request_id`` is a free-form correlation id so logs
    from different layers can be joined for a single turn.
    """
    tenant_id: int
    customer_phone: str = ""
    customer_id: Optional[int] = None
    industry: str = ""
    request_id: str = ""
    metadata: dict = field(default_factory=dict)


# Models that are intentionally NOT tenant-scoped (platform-level data)
# and therefore must NOT be filtered by tenant_id.  Keep this list small.
_NON_TENANT_MODELS: frozenset = frozenset({
    "Tenant",
    "BillingPlan",
    "Developer",
    "App",
    "CrossMerchantSignal",
})


class TenantIsolationLayer:
    """Stateless guard — every method validates an explicit ``ctx``.

    The layer never holds a DB session or a request-scoped value, which
    makes it safe to call from any thread / task / sync context.
    """

    # ── Context construction ─────────────────────────────────────────────

    @staticmethod
    def make_context(
        tenant_id: Any,
        *,
        customer_phone: str = "",
        customer_id: Optional[int] = None,
        industry: str = "",
        request_id: str = "",
        metadata: Optional[dict] = None,
    ) -> TenantContext:
        """Build and validate a ``TenantContext``.

        Raises ``TenantIsolationViolation`` if ``tenant_id`` is missing,
        not coercible to a positive int, or zero/negative.
        """
        if tenant_id is None:
            raise TenantIsolationViolation("tenant_id is required to build TenantContext")
        try:
            tid = int(tenant_id)
        except (TypeError, ValueError):
            raise TenantIsolationViolation(
                f"tenant_id must be a positive int, got {tenant_id!r}"
            )
        if tid <= 0:
            raise TenantIsolationViolation(
                f"tenant_id must be a positive int, got {tid}"
            )

        cid: Optional[int] = None
        if customer_id is not None:
            try:
                cid_int = int(customer_id)
            except (TypeError, ValueError):
                raise TenantIsolationViolation(
                    f"customer_id must be int, got {customer_id!r}"
                )
            if cid_int <= 0:
                raise TenantIsolationViolation(
                    f"customer_id must be positive, got {cid_int}"
                )
            cid = cid_int

        return TenantContext(
            tenant_id=tid,
            customer_phone=str(customer_phone or ""),
            customer_id=cid,
            industry=str(industry or "").strip().lower(),
            request_id=str(request_id or ""),
            metadata=dict(metadata or {}),
        )

    @staticmethod
    def assert_active(ctx: Optional[TenantContext]) -> TenantContext:
        """Ensure a ``TenantContext`` was passed and looks valid."""
        if ctx is None:
            raise TenantIsolationViolation("active TenantContext is required")
        if not isinstance(ctx, TenantContext):
            raise TenantIsolationViolation(
                f"expected TenantContext, got {type(ctx).__name__}"
            )
        if ctx.tenant_id <= 0:
            raise TenantIsolationViolation(
                f"TenantContext has invalid tenant_id={ctx.tenant_id}"
            )
        return ctx

    # ── Query / record guards ────────────────────────────────────────────

    @staticmethod
    def scope_query(query: Any, model: Any, ctx: TenantContext) -> Any:
        """Append a defensive ``model.tenant_id == ctx.tenant_id`` filter.

        Returns the new query.  If the model is in ``_NON_TENANT_MODELS``
        the original query is returned unchanged — those models are
        platform data that intentionally has no per-tenant key.
        """
        TenantIsolationLayer.assert_active(ctx)
        model_name = getattr(model, "__name__", str(model))
        if model_name in _NON_TENANT_MODELS:
            return query
        if not hasattr(model, "tenant_id"):
            raise TenantIsolationViolation(
                f"model {model_name} has no tenant_id column — cannot scope"
            )
        return query.filter(model.tenant_id == ctx.tenant_id)

    @staticmethod
    def assert_belongs(record: Any, ctx: TenantContext) -> None:
        """Raise if ``record.tenant_id`` does not match ``ctx.tenant_id``.

        Records without a ``tenant_id`` attribute (platform tables) are
        accepted as-is so callers can pass them through uniformly.
        """
        TenantIsolationLayer.assert_active(ctx)
        if record is None:
            return
        record_tid = getattr(record, "tenant_id", None)
        if record_tid is None:
            return
        try:
            record_tid_int = int(record_tid)
        except (TypeError, ValueError):
            raise TenantIsolationViolation(
                f"record {type(record).__name__} carries non-int tenant_id={record_tid!r}"
            )
        if record_tid_int != ctx.tenant_id:
            raise TenantIsolationViolation(
                f"record {type(record).__name__} belongs to tenant={record_tid_int}, "
                f"active context is tenant={ctx.tenant_id}"
            )

    @staticmethod
    def filter_records(records: Iterable[Any], ctx: TenantContext) -> List[Any]:
        """Return only records whose ``tenant_id`` matches ``ctx``.

        Records without a ``tenant_id`` attribute are passed through.
        Mismatches are dropped silently and logged at warning level — this
        is a defensive last-line filter, the primary protection is
        ``scope_query`` upstream.
        """
        TenantIsolationLayer.assert_active(ctx)
        keep: List[Any] = []
        for r in records or []:
            try:
                TenantIsolationLayer.assert_belongs(r, ctx)
                keep.append(r)
            except TenantIsolationViolation as exc:
                logger.warning(
                    "[TenantIsolation] dropping cross-tenant record: %s", exc
                )
        return keep

    # ── Payload guards ───────────────────────────────────────────────────

    @staticmethod
    def verify_payload(payload: Any, ctx: TenantContext) -> dict:
        """Sanitise a tool / API payload before execution.

        Rules:
        * If ``payload`` is not a dict, return an empty dict.
        * If ``payload`` includes a ``tenant_id`` it must equal ``ctx.tenant_id``;
          a hallucinated mismatch is an isolation violation.
        * The returned dict always overrides ``tenant_id`` to the active
          context, so handlers cannot accidentally pick up an attacker
          value.
        """
        TenantIsolationLayer.assert_active(ctx)
        if not isinstance(payload, dict):
            return {"tenant_id": ctx.tenant_id}
        clean = dict(payload)
        if "tenant_id" in clean and clean["tenant_id"] is not None:
            try:
                supplied = int(clean["tenant_id"])
            except (TypeError, ValueError):
                raise TenantIsolationViolation(
                    f"payload tenant_id is not int: {clean['tenant_id']!r}"
                )
            if supplied != ctx.tenant_id:
                raise TenantIsolationViolation(
                    f"payload tenant_id={supplied} does not match active tenant={ctx.tenant_id}"
                )
        clean["tenant_id"] = ctx.tenant_id
        return clean

    # ── Convenience for cross-tenant aggregations ────────────────────────

    @staticmethod
    def is_cross_tenant_safe(model: Any) -> bool:
        """Return True when a model is intentionally cross-tenant.

        Used by the learning store to confirm at write-time that we are
        writing into a non-tenant-scoped table.
        """
        model_name = getattr(model, "__name__", str(model))
        return model_name in _NON_TENANT_MODELS
