"""
whatsapp_connections row churn — JSON size estimates + flush timing + per-minute rollups
+ in-memory stamping throttle.

Hot webhook paths call ``record_row_flush`` immediately after ``db.flush()`` so ops can
correlate PostgreSQL ``statement_timeout`` with oversized JSONB rewrites.

``should_stamp_now`` is the per-(conn_id, family) coalescer that prevents N webhook
deliveries from racing for the same row-level lock on ``whatsapp_connections`` — the
webhook receipt is purely informational and ~30s resolution is plenty for the dashboard.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger("nahla.wa_conn_writes")

_lock = threading.Lock()
_bucket_minute = int(time.time()) // 60
_row_flush_count = 0
_meta_flush_count = 0
_meta_bytes_sum = 0


def _advance_bucket_if_needed_locked() -> None:
    """Rotate the in-memory metrics bucket once a minute.

    Lock-held side-effect-only function — its caller already owns
    ``_lock``. Never raises: a logging failure must not propagate into
    the webhook batch's transaction (see ``record_row_flush`` for the
    P1 fix history).
    """
    global _bucket_minute, _row_flush_count, _meta_flush_count, _meta_bytes_sum
    try:
        m = int(time.time()) // 60
        if m != _bucket_minute:
            logger.info(
                "[WA_CONN_WRITE_METRICS] minute=%s row_flushes=%s meta_flushes=%s approx_meta_bytes_sum=%s",
                _bucket_minute,
                _row_flush_count,
                _meta_flush_count,
                _meta_bytes_sum,
            )
            _bucket_minute = m
            _row_flush_count = 0
            _meta_flush_count = 0
            _meta_bytes_sum = 0
    except Exception as exc:  # noqa: BLE001
        # Defensive — there's no exception path inside the body today,
        # but logging.info() can theoretically raise on a custom handler
        # and we promise the caller "metrics never throws".
        try:
            logger.warning(
                "[WA_CONN_WRITE_METRICS] bucket advance suppressed err=%s",
                exc,
            )
        except Exception:
            pass


def approx_json_bytes(obj: dict) -> int:
    try:
        return len(json.dumps(obj, ensure_ascii=False, default=str))
    except Exception:
        return -1


def record_row_flush(
    *,
    source: str,
    tenant_id: int,
    conn_id: Any,
    flush_ms: int,
    approx_meta_json_bytes: int | None = None,
) -> None:
    """Invoke right after ``db.flush()`` that touched ``whatsapp_connections``.

    Hard-isolation contract (P1 fix, May 2026):
      * MUST NEVER raise. Any internal error is caught and logged; metrics
        bookkeeping is best-effort by design and the inbound webhook batch
        must continue regardless.
      * Pre-fix bug: the in-place ``_row_flush_count += 1`` was missing
        ``global _row_flush_count`` (alongside the meta counters), so
        every call raised ``UnboundLocalError`` and bubbled up into the
        outer webhook ``except`` — which then did ``db.rollback()`` while
        still returning 200 OK to 360dialog. The provider never retried
        and ``smb_message_echoes`` writes (which use the SAME ``db``
        session as the metric call) were permanently lost.
    """
    # NOTE: the ``global`` declaration here is the actual fix. Removing
    # it brings back the silent-rollback bug. Treat as load-bearing.
    global _row_flush_count, _meta_flush_count, _meta_bytes_sum

    try:
        minute_idx = 0
        with _lock:
            _advance_bucket_if_needed_locked()
            _row_flush_count += 1
            minute_idx = _row_flush_count
            if approx_meta_json_bytes is not None:
                _meta_flush_count += 1
                if approx_meta_json_bytes >= 0:
                    _meta_bytes_sum += approx_meta_json_bytes

        meta_part = (
            f" approx_meta_json_bytes={approx_meta_json_bytes}"
            if approx_meta_json_bytes is not None
            else ""
        )
        logger.info(
            "[WA_CONN_ROW_FLUSH] source=%s tenant=%s conn_id=%s flush_ms=%s minute_row_idx=%s%s",
            source,
            tenant_id,
            conn_id,
            flush_ms,
            minute_idx,
            meta_part,
        )
    except Exception as exc:  # noqa: BLE001
        # Metrics MUST NOT poison the webhook transaction. Log at warning
        # level so ops still see it, but swallow everything else.
        logger.warning(
            "[WA_CONN_WRITE_METRICS] record_row_flush suppressed err=%s "
            "source=%s tenant=%s conn_id=%s",
            exc, source, tenant_id, conn_id,
        )


# ── Stamping throttle ───────────────────────────────────────────────────────
# Collapse N parallel webhook deliveries against the same WhatsApp connection
# into one UPDATE per (conn_id, family) per ``WA_STAMP_THROTTLE_SEC``. Without
# this, conn_id=3 (the high-traffic merchant) saw lock contention + statement
# timeouts on ``UPDATE whatsapp_connections SET last_webhook_received_at=...``.
WA_STAMP_THROTTLE_SEC = float(os.environ.get("NAHLA_WA_STAMP_THROTTLE_SEC", "20"))

_STAMP_LOCK = threading.Lock()
_LAST_STAMP_AT: dict[tuple[Any, str], float] = {}
_SKIP_COUNT = 0
_APPLY_COUNT = 0


def should_stamp_now(conn_id: Any, family: str) -> bool:
    """Return True iff a stamp for this (conn, family) is overdue.

    Updates the in-memory marker as a side effect when True is returned, so
    two callers racing on the same key cannot both decide "yes".
    """
    global _SKIP_COUNT, _APPLY_COUNT
    key = (conn_id, family)
    now_ts = time.monotonic()
    with _STAMP_LOCK:
        last = _LAST_STAMP_AT.get(key, 0.0)
        if now_ts - last < WA_STAMP_THROTTLE_SEC:
            _SKIP_COUNT += 1
            return False
        _LAST_STAMP_AT[key] = now_ts
        _APPLY_COUNT += 1
        return True


def reset_stamp_marker(conn_id: Any, family: str) -> None:
    """Clear the marker so a future delivery will retry immediately.

    Called by the webhook code when the SQL UPDATE itself fails (timeout,
    rollback) so we do not silently swallow stamping forever.
    """
    key = (conn_id, family)
    with _STAMP_LOCK:
        _LAST_STAMP_AT.pop(key, None)


def stamping_counters_snapshot() -> dict[str, int]:
    with _STAMP_LOCK:
        return {"applied": _APPLY_COUNT, "skipped": _SKIP_COUNT}


# ── Background fire-and-forget executor ─────────────────────────────────────
# Stamping the webhook receipt MUST NOT block the WhatsApp message pipeline.
# A single shared, daemon thread pool runs every UPDATE in its own connection
# so a row-level lock or `statement_timeout` cannot poison the main batch's
# transaction (which carries the message routing + state writes).
import concurrent.futures  # noqa: E402

_BG_MAX_WORKERS = max(2, int(os.environ.get("NAHLA_WA_STAMP_BG_WORKERS", "4")))
_BG_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None
_BG_LOCK = threading.Lock()


def _bg_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _BG_EXECUTOR
    with _BG_LOCK:
        if _BG_EXECUTOR is None:
            _BG_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                max_workers=_BG_MAX_WORKERS,
                thread_name_prefix="wa-stamp",
            )
            logger.info(
                "[WA_STAMP_BG] thread pool initialised workers=%s throttle_sec=%s",
                _BG_MAX_WORKERS, WA_STAMP_THROTTLE_SEC,
            )
        return _BG_EXECUTOR


def submit_stamp_background(fn, *args, **kwargs) -> None:
    """Run ``fn(*args, **kwargs)`` in the shared stamp pool. Errors are logged.

    Intentionally returns ``None`` — the caller treats stamping as best-effort
    and must continue regardless of outcome.
    """
    try:
        fut = _bg_executor().submit(fn, *args, **kwargs)
    except RuntimeError as exc:
        # Pool already shut down (e.g. process tearing down). Just log.
        logger.debug("[WA_STAMP_BG] pool unavailable, dropping stamp: %s", exc)
        return

    def _on_done(f: concurrent.futures.Future) -> None:
        try:
            f.result()
        except Exception as exc:
            logger.warning("[WA_STAMP_BG] background task failed: %s", exc)

    fut.add_done_callback(_on_done)
