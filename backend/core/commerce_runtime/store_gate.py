"""Whether a merchant's own AI settings admit one recipient — read, never written.

The pilot narrows itself with an operator-configured recipient allowlist. Once
the runtime is the platform's default AI path that list stops being the right
authority: the question is no longer "is this one of the owner's test numbers"
but "does this store's own AI setting admit this customer" — the same question
``core.ai_disabled_gate`` already answers for the legacy path, and it must be
answered the same way by both, or the two runtimes disagree about who may be
spoken to.

So the rule is not restated here. ``store_ai_mode_allows`` is imported and
called; this module only supplies the settings it decides from, and it supplies
them **without writing**. ``is_ai_allowed_by_store_mode`` reaches them through
``get_or_create_settings``, which inserts a defaulted row for a tenant that has
none. That is right on the webhook path, where the turn is being handled
anyway; it is wrong at the acceptance boundary, where the guard runs before a
webhook request is acknowledged and is contractually a read. A tenant with no
saved settings is therefore read as the platform's defaults directly — the same
values the created row would have carried, so the two callers still answer
identically.

The third outcome is the point of the module. "This store does not admit this
recipient" is a fact about the merchant's configuration; "the settings could not
be read" is a fact about us, and answering the first for the second would hand a
store's traffic to whichever runtime happens to fail more quietly.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any

logger = logging.getLogger("nahla.commerce_runtime.store_gate")

# Closed statuses. Exactly one is reported per read.
GATE_ALLOWED = "allowed"
GATE_REFUSED = "refused"
GATE_UNAVAILABLE = "unavailable"


@dataclasses.dataclass(frozen=True)
class StoreGateRead:
    """What the store's own AI setting says about one recipient."""

    status: str
    reason: str = ""      # the gate's own word when refused; the error name when unavailable
    mode: str = ""        # off | test | on, when it could be resolved

    @property
    def allowed(self) -> bool:
        return self.status == GATE_ALLOWED

    @property
    def decided(self) -> bool:
        """Whether this is an answer about the store rather than about us."""
        return self.status in {GATE_ALLOWED, GATE_REFUSED}


def read_store_gate(db: Any, *, tenant_id: Any, customer_phone: Any) -> StoreGateRead:
    """The store's AI decision for ``customer_phone``, without creating a row.

    ``customer_phone`` is passed through **exactly as the caller received it**.
    The test-mode allowlist is matched with the platform's own
    ``normalize_whatsapp_phone_for_ai_allowlist``, not with the runtime's
    recipient normalisation, and pre-normalising here would answer a different
    question from the one the legacy path asks about the same message.
    """
    if db is None:
        return StoreGateRead(status=GATE_UNAVAILABLE, reason="no_session")
    try:
        tenant = int(tenant_id)
    except (TypeError, ValueError):
        return StoreGateRead(status=GATE_UNAVAILABLE, reason="tenant_unreadable")

    try:
        from database.models import TenantSettings  # noqa: PLC0415

        row = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == tenant)
            .first()
        )
        stored = getattr(row, "ai_settings", None) if row is not None else None
    except Exception as exc:  # noqa: BLE001 - an unreadable setting is not a permission
        logger.error(
            "[COMMERCE_RUNTIME_STORE_GATE] settings unreadable tenant=%s error=%s — "
            "unavailable, not 'refused'", tenant, type(exc).__name__)
        return StoreGateRead(status=GATE_UNAVAILABLE, reason=type(exc).__name__)

    try:
        from core.ai_disabled_gate import store_ai_mode_allows  # noqa: PLC0415

        decision = store_ai_mode_allows(stored, str(customer_phone or ""))
    except Exception as exc:  # noqa: BLE001 - a rule that cannot be applied decides nothing
        logger.error(
            "[COMMERCE_RUNTIME_STORE_GATE] decision failed tenant=%s error=%s",
            tenant, type(exc).__name__)
        return StoreGateRead(status=GATE_UNAVAILABLE, reason=type(exc).__name__)

    if decision.allowed:
        return StoreGateRead(status=GATE_ALLOWED, mode=str(decision.mode or ""))
    return StoreGateRead(status=GATE_REFUSED, reason=str(decision.reason or ""),
                         mode=str(decision.mode or ""))


__all__ = [
    "GATE_ALLOWED", "GATE_REFUSED", "GATE_UNAVAILABLE",
    "StoreGateRead", "read_store_gate",
]
