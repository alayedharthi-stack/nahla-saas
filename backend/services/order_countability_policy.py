"""Canonical deterministic policy for whether an order is countable.

This module intentionally only classifies supplied order state.  It does not
query, count, write, or depend on tenant/customer identity.
"""
from __future__ import annotations

import ast
from typing import Any

from sqlalchemy import and_, false, func
from sqlalchemy.sql.elements import ColumnElement

from models import Order


COUNTABLE_ORDER_STATUSES = frozenset(
    {
        "paid",
        "confirmed",
        "processing",
        "shipped",
        "out_for_delivery",
        "delivered",
        "completed",
    }
)
EXCLUDED_ORDER_STATUSES = frozenset(
    {
        "cancelled",
        "canceled",
        "failed",
        "payment_failed",
        "refunded",
        "voided",
        "abandoned",
        "draft",
    }
)

# ``Order.status`` historically received the Python repr of a provider status
# dict.  PostgreSQL ``substring(value FROM pattern)`` returns capture group 1,
# so these anchored patterns recover the same supported top-level fields as
# ``order_status_key`` while keeping the parsing literals parameter-bound.
_LEGACY_STATUS_FIELD_PATTERNS = (
    r"^\s*\{.*['\"]slug['\"]\s*:\s*['\"]([^'\"]*)['\"].*\}\s*$",
    r"^\s*\{.*['\"]name['\"]\s*:\s*['\"]([^'\"]*)['\"].*\}\s*$",
    r"^\s*\{.*['\"]code['\"]\s*:\s*['\"]([^'\"]*)['\"].*\}\s*$",
)


def order_status_key(order_or_status: Any) -> str:
    """Return the normalized status used by the countability policy.

    Legacy rows may contain a Python representation of a provider status
    mapping.  Keep the existing read-time recovery behavior until those rows
    are repaired independently.
    """
    if isinstance(order_or_status, Order):
        text = str(getattr(order_or_status, "status", "") or "").strip()
    else:
        text = str(order_or_status or "").strip()

    if text.startswith("{"):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, dict):
                text = str(
                    parsed.get("slug")
                    or parsed.get("name")
                    or parsed.get("code")
                    or text
                )
        except (ValueError, SyntaxError):
            pass
    return text.strip().lower()


def is_countable_order(order_or_status: Any) -> bool:
    """Classify an order object or raw status without performing I/O."""
    status = order_status_key(order_or_status)
    if status in EXCLUDED_ORDER_STATUSES:
        return False
    if isinstance(order_or_status, Order) and bool(
        getattr(order_or_status, "is_abandoned", False)
    ):
        return False
    if status in COUNTABLE_ORDER_STATUSES:
        return True
    return status not in EXCLUDED_ORDER_STATUSES and bool(status)


def order_status_key_sql(status: ColumnElement[Any]) -> ColumnElement[str]:
    """Build PostgreSQL status normalization matching supported legacy reprs.

    A supported legacy repr is a complete braced mapping containing a quoted
    top-level provider field.  The expressions retain the Python precedence:
    a nonblank ``slug`` wins, then ``name``, then ``code``; malformed strings
    or absent fields fall back to the lower-trimmed raw column value.
    """
    raw_status_key = func.lower(func.trim(status))
    recovered_fields = (
        func.lower(
            func.trim(
                func.nullif(func.substring(status, pattern), "")
            )
        )
        for pattern in _LEGACY_STATUS_FIELD_PATTERNS
    )
    return func.coalesce(*recovered_fields, raw_status_key)


def countable_order_sql_predicate(
    status: ColumnElement[Any],
    is_abandoned: ColumnElement[Any],
) -> ColumnElement[bool]:
    """Build the parameterized SQLAlchemy predicate matching stored-order policy.

    The predicate is deliberately composable: callers supply the actual
    ``orders.status`` and ``orders.is_abandoned`` expressions, then attach the
    returned clause to their own query.  It performs no database I/O and adds
    no tenant/customer relationship condition.

    PostgreSQL's bound parameters carry the fixed policy statuses and legacy
    parsing patterns; no status value is interpolated into SQL text.
    ``coalesce(..., false)`` matches the Python treatment of a null/false
    ``is_abandoned`` value.
    """
    status_key = order_status_key_sql(status)
    return and_(
        func.length(status_key) > 0,
        ~status_key.in_(tuple(sorted(EXCLUDED_ORDER_STATUSES))),
        func.coalesce(is_abandoned, false()).is_(False),
    )
