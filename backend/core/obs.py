"""
core/obs.py
───────────
Canonical structured-event logger for Nahla's order / customer / coupon
pipeline. Emits a single JSON-like line per event so Railway logs can be
grepped by event name.

USAGE
─────
    from core.obs import log_event, EVENTS

    log_event(
        EVENTS.ORDER_WEBHOOK_PERSISTED,
        tenant_id=tenant_id,
        store_id=store_id,
        webhook_event_id=ev.id,
        event_type="order.created",
    )

    # For failures — pass err as an Exception; it gets formatted.
    log_event(
        EVENTS.COUPON_AUTOGEN_FAILED,
        tenant_id=tenant_id,
        segment="vip",
        code=code,
        err=exc,
    )

Rules:
  • Only canonical event names declared on the EVENTS class may be used.
    Keep them grep-able across the codebase forever.
  • Never call `logger.debug` on a business failure. Use log_event with an
    event ending in `.failed` or `.error`.
  • Never swallow exceptions: if you catch, you MUST log_event before re-
    raising or returning early.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional

_LOGGER = logging.getLogger("nahla.obs")


class EVENTS:
    """Canonical event names. Add new events here — do not invent them inline."""

    # ── Webhook ingress ───────────────────────────────────────────────────────
    WEBHOOK_RECEIVED           = "webhook.received"
    WEBHOOK_SIGNATURE_INVALID  = "webhook.signature_invalid"
    WEBHOOK_INVALID_JSON       = "webhook.invalid_json"
    WEBHOOK_PERSISTED          = "webhook.persisted"
    WEBHOOK_PERSIST_FAILED     = "webhook.persist_failed"

    # ── Dispatcher ────────────────────────────────────────────────────────────
    EVENT_CLAIMED              = "dispatcher.event_claimed"
    EVENT_DISPATCHED           = "dispatcher.event_dispatched"
    EVENT_FAILED               = "dispatcher.event_failed"
    EVENT_DEAD_LETTER          = "dispatcher.event_dead_letter"
    EVENT_REPLAYED             = "dispatcher.event_replayed"
    DISPATCHER_LOOP_ERROR      = "dispatcher.loop_error"
    DISPATCHER_TENANT_UNRESOLVED = "dispatcher.tenant_unresolved"

    # ── Orders ────────────────────────────────────────────────────────────────
    ORDER_UPSERT_SUCCESS       = "order.upsert_success"
    ORDER_UPSERT_CONFLICT      = "order.upsert_conflict"
    ORDER_UPSERT_ERROR         = "order.upsert_error"

    # ── Customer ──────────────────────────────────────────────────────────────
    CUSTOMER_UPSERT_MATCHED_BY_SALLA_ID = "customer.upsert.matched_by_salla_id"
    CUSTOMER_UPSERT_MATCHED_BY_PHONE    = "customer.upsert.matched_by_phone"
    CUSTOMER_UPSERT_MATCHED_BY_NAME     = "customer.upsert.matched_by_name"
    CUSTOMER_UPSERT_INSERTED            = "customer.upsert.inserted"
    CUSTOMER_UPSERT_FAILED              = "customer.upsert.failed"
    CUSTOMER_CLASSIFICATION_CHANGED     = "customer.classification.changed"
    CUSTOMER_CLASSIFICATION_RECOMPUTED  = "customer.classification.recomputed"
    CUSTOMER_CLASSIFICATION_ERROR       = "customer.classification.error"

    # ── Coupons ───────────────────────────────────────────────────────────────
    COUPON_AUTOGEN_TRIGGERED   = "coupon.autogen.triggered"
    COUPON_AUTOGEN_CREATED     = "coupon.autogen.created"
    COUPON_AUTOGEN_COLLISION   = "coupon.autogen.collision"
    COUPON_AUTOGEN_FAILED      = "coupon.autogen.failed"
    COUPON_AUTOGEN_ROLLED_BACK = "coupon.autogen.rolled_back"
    COUPON_POOL_EXHAUSTED      = "coupon.pool.exhausted"


def _coerce(value: Any) -> Any:
    """Coerce value into something JSON-safe; Exceptions become str repr."""
    if value is None:
        return None
    if isinstance(value, BaseException):
        return f"{type(value).__name__}: {value}"
    if isinstance(value, (str, int, float, bool)):
        return value
    try:
        json.dumps(value, default=str)
        return value
    except Exception:
        return str(value)


def log_event(
    name: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """
    Emit a single structured log line with event=<name> + user fields.

    Known fields (use consistently — they make log searches easy):
      tenant_id, store_id, customer_id, order_id, coupon_id, segment,
      external_id, external_event_id, webhook_event_id, provider,
      event_type, attempts, elapsed_ms, status, err
    """
    err = fields.pop("err", None)
    safe: Dict[str, Any] = {"event": name}
    for k, v in fields.items():
        coerced = _coerce(v)
        if coerced is not None:
            safe[k] = coerced

    if err is not None:
        safe["error"] = _coerce(err)
        # For errors, always log with exc_info so we preserve the stack.
        # The caller may have already re-raised so this may not always have
        # live traceback, but if it does it will be captured.
        level = logging.ERROR if level < logging.ERROR else level
        _LOGGER.log(level, json.dumps(safe, default=str), exc_info=isinstance(err, BaseException))
        return

    _LOGGER.log(level, json.dumps(safe, default=str))


@contextmanager
def timed_event(name: str, **fields: Any):
    """
    Context manager that logs a `name` + `_ok`/`_error` pair with elapsed_ms.

        with timed_event(EVENTS.ORDER_UPSERT_SUCCESS, tenant_id=1, external_id='abc'):
            ... # body

    If the body raises, a matching `<name>.error` event is emitted with err=.
    The exception is re-raised.
    """
    started = time.monotonic()
    try:
        yield
    except BaseException as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        log_event(
            name + ".error",
            level=logging.ERROR,
            elapsed_ms=elapsed_ms,
            err=exc,
            **fields,
        )
        raise
    else:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        log_event(name, elapsed_ms=elapsed_ms, **fields)
