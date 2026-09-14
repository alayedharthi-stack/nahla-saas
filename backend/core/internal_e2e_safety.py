"""Fail-closed markers shared by persisted INTERNAL_E2E consumers.

These checks remain effective after the synchronous acceptance ContextVar has
ended. Metadata is intentionally secondary; source/direction are the primary
background eligibility boundary.
"""
from __future__ import annotations

from typing import Any


INTERNAL_E2E_ORDER_SOURCE = "internal_e2e"
INTERNAL_E2E_MESSAGE_DIRECTIONS = frozenset(
    {"internal_e2e_inbound", "internal_e2e_outbound", "internal_e2e_control"}
)
INTERNAL_E2E_IDENTITY_PREFIX = "internal_e2e:"


def is_internal_e2e_order(order: Any) -> bool:
    return str(getattr(order, "source", "") or "").strip().lower() == (
        INTERNAL_E2E_ORDER_SOURCE
    )


def is_internal_e2e_message_event(event: Any) -> bool:
    return str(getattr(event, "direction", "") or "").strip().lower() in (
        INTERNAL_E2E_MESSAGE_DIRECTIONS
    )


def assert_external_order_eligible(order: Any, *, operation: str) -> None:
    if is_internal_e2e_order(order):
        raise ValueError(f"internal_e2e_order_forbidden:{operation}")


def assert_external_order_reference_eligible(order_id: object, *, operation: str) -> None:
    if str(order_id or "").strip().lower().startswith(INTERNAL_E2E_IDENTITY_PREFIX):
        raise ValueError(f"internal_e2e_order_forbidden:{operation}")


__all__ = [
    "INTERNAL_E2E_IDENTITY_PREFIX",
    "INTERNAL_E2E_MESSAGE_DIRECTIONS",
    "INTERNAL_E2E_ORDER_SOURCE",
    "assert_external_order_eligible",
    "assert_external_order_reference_eligible",
    "is_internal_e2e_message_event",
    "is_internal_e2e_order",
]
