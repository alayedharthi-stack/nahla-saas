"""
modules/ai/security
───────────────────
Foundational safety layer for the merchant AI brain.

This package centralises three concerns that every higher layer
(SalesContextSnapshot, CommerceToolRuntime, MerchantBrain pipeline,
orchestrator adapter, memory updater) must respect:

1. ``tenant_isolation`` — ``TenantContext`` + ``TenantIsolationLayer``
   force ``tenant_id`` onto every retrieval / tool execution / state
   write path and raise ``TenantIsolationViolation`` on the first
   contract breach instead of silently leaking data across stores.

2. ``trace_schema`` — ``TraceEvent`` defines the only shape allowed to
   leave the per-tenant boundary.  ``validate_anonymized`` rejects any
   payload that still carries raw customer / store data (phone,
   message text, product titles, prices, raw tenant_id, …).

3. ``cross_merchant_store`` — ``CrossMerchantLearningStore`` is the
   single writer that may persist anonymized signals into the
   ``cross_merchant_signals`` table; nothing else in the codebase is
   allowed to write there.

By construction the merchant-specific tier never crosses tenants —
it stays inside existing tenant-scoped tables (``ConversationTrace``,
``CustomerProfile``, …).  Only the ``global`` and ``vertical`` tiers
are written to the cross-merchant store.
"""
from .tenant_isolation import (  # noqa: F401
    TenantContext,
    TenantIsolationLayer,
    TenantIsolationViolation,
)
from .trace_schema import (  # noqa: F401
    FORBIDDEN_TRACE_KEYS,
    LearningTier,
    MODEL_PATH_MAX_LENGTH,
    ModelPathTooLongError,
    OutcomeKind,
    TraceEvent,
    UIMode,
    anonymize_tenant,
    industry_of,
    sanitize_extra,
    validate_anonymized,
    value_bucket,
)
from .cross_merchant_store import CrossMerchantLearningStore  # noqa: F401
