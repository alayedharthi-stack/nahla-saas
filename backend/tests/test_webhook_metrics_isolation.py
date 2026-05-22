"""
backend/tests/test_webhook_metrics_isolation.py
───────────────────────────────────────────────
P1 regression suite for the May 2026 silent-rollback bug.

What happened in production
───────────────────────────
``backend/core/wa_conn_write_metrics.py::record_row_flush`` did::

    _row_flush_count += 1

without declaring ``_row_flush_count`` ``global``. Python treats the
name as a function-local on the assignment, so the implicit read on
the RHS of ``+=`` raised ``UnboundLocalError: cannot access local
variable '_row_flush_count'`` **on every single invocation**.

That metric call is reached from two webhook paths:

  * ``_bg_stamp_run`` — runs on its own ``bg_db`` session. Failure
    rolls back ONLY the metrics UPDATE. Inbound pipeline safe.

  * ``_record_coexistence_event`` / ``_record_status_event`` — share
    the outer batch's ``db``. Failure propagated up into the batch
    handler's ``except``, which called ``db.rollback()`` — losing
    ``smb_message_echoes`` writes and any other pending state — and
    then returned 200 OK to 360dialog. The provider never retried.

The fix is twofold:

  1. Add the ``global`` declaration and wrap ``record_row_flush`` in
     a ``try/except`` so metrics CANNOT raise, ever.
  2. Wrap each per-change branch of the 360dialog batch loop in its
     own ``try/except + db.rollback()`` so one failed change cannot
     contaminate sibling persistence.

These tests lock both fixes.
"""
from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — pure metrics module
# ─────────────────────────────────────────────────────────────────────────────


def test_record_row_flush_does_not_raise_unboundlocalerror() -> None:
    """The original bug. ``+=`` without ``global`` raised
    ``UnboundLocalError`` on every call. Reload the module fresh so
    the module-level counters start from a known state."""
    sys.modules.pop("core.wa_conn_write_metrics", None)
    metrics = importlib.import_module("core.wa_conn_write_metrics")

    # Multiple calls (5 well above any one-bucket reset boundary)
    for i in range(5):
        # Must not raise ANY exception, period.
        metrics.record_row_flush(
            source="test",
            tenant_id=33,
            conn_id=99,
            flush_ms=12,
            approx_meta_json_bytes=4096,
        )

    # And the counter actually moved — proves the ``global`` is wired,
    # not just silently swallowed.
    assert metrics._row_flush_count >= 5  # noqa: SLF001
    assert metrics._meta_flush_count >= 5  # noqa: SLF001
    assert metrics._meta_bytes_sum >= 5 * 4096  # noqa: SLF001


def test_record_row_flush_swallows_arbitrary_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hard-isolation contract: even if a downstream helper raises,
    the metrics call must not bubble up. Without this guarantee, ANY
    future logging / accounting regression would once again become a
    silent-rollback P1.
    """
    sys.modules.pop("core.wa_conn_write_metrics", None)
    metrics = importlib.import_module("core.wa_conn_write_metrics")

    def _boom(*_a, **_kw) -> None:
        raise RuntimeError("simulated metrics fault")

    # Sabotage the bucket-advance helper to raise. The outer
    # try/except in ``record_row_flush`` must contain it.
    monkeypatch.setattr(metrics, "_advance_bucket_if_needed_locked", _boom)

    # MUST NOT RAISE.
    metrics.record_row_flush(
        source="test",
        tenant_id=33,
        conn_id=99,
        flush_ms=10,
    )


def test_record_row_flush_logs_warning_on_internal_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ops still need to *see* a metrics fault — silent containment
    must not become silent loss. A warning-level log is the contract."""
    sys.modules.pop("core.wa_conn_write_metrics", None)
    metrics = importlib.import_module("core.wa_conn_write_metrics")

    monkeypatch.setattr(
        metrics, "_advance_bucket_if_needed_locked",
        lambda: (_ for _ in ()).throw(RuntimeError("simulated")),
    )

    with caplog.at_level(logging.WARNING, logger="nahla.wa_conn_writes"):
        metrics.record_row_flush(
            source="webhook360_coex_event",
            tenant_id=33,
            conn_id=99,
            flush_ms=5,
        )

    assert any("suppressed" in rec.message for rec in caplog.records), (
        f"expected a 'suppressed' warning, got: "
        f"{[r.message for r in caplog.records]}"
    )


def test_advance_bucket_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twin contract for the inner helper: even if ``logger.info``
    raises (custom log handler in production has done so once), the
    counter rotation must complete without escaping the lock-held
    section.
    """
    sys.modules.pop("core.wa_conn_write_metrics", None)
    metrics = importlib.import_module("core.wa_conn_write_metrics")

    # Force the timestamp to differ from the in-memory bucket so the
    # logging branch fires.
    metrics._bucket_minute = -1  # noqa: SLF001

    class _BadLogger:
        def info(self, *a, **kw): raise RuntimeError("log handler fault")
        def warning(self, *a, **kw): pass

    monkeypatch.setattr(metrics, "logger", _BadLogger())
    # MUST NOT RAISE.
    metrics._advance_bucket_if_needed_locked()  # noqa: SLF001


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — module-level constants for the per-change observability path
# ─────────────────────────────────────────────────────────────────────────────


def test_batch_branch_isolated_constant_exists() -> None:
    """The new ``DROP_BATCH_BRANCH_ISOLATED`` symbol must exist so the
    webhook's per-change ``except`` blocks have something to log under.
    Catches a regression where the constant gets renamed but the call
    sites in ``whatsapp_webhook.py`` aren't updated.
    """
    from core import inbound_observability as obs

    assert hasattr(obs, "DROP_BATCH_BRANCH_ISOLATED")
    assert obs.DROP_BATCH_BRANCH_ISOLATED == "batch_branch_isolated"


def test_record_inbound_drop_accepts_batch_branch_isolated_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dashboard reads the literal ``mismatch_type``, so an
    unknown drop_kind silently passing through is fine — but it must
    NOT raise. We stub the SessionLocal so the test doesn't need a DB.
    """
    from core import inbound_observability as obs

    captured: list = []

    class _StubRow:
        def __init__(self, **kw):
            captured.append(kw)
        id = 1  # mimic the post-flush id

    class _StubSession:
        def add(self, row): self.row = row  # noqa: D401
        def commit(self): pass
        def rollback(self): pass
        def close(self): pass

    monkeypatch.setattr(obs, "_write_event", lambda **kw: 1)

    new_id = obs.record_inbound_drop(
        tenant_id=33,
        drop_kind=obs.DROP_BATCH_BRANCH_ISOLATED,
        inbound_preview="field=smb_message_echoes",
        detail="UnboundLocalError: cannot access local variable '_row_flush_count'",
        chosen_path="webhook360/coexistence",
    )
    assert new_id == 1


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — end-to-end: a metric failure during a coex event must NOT
# rollback prior writes on the same batch.
# ─────────────────────────────────────────────────────────────────────────────


class _StubConn:
    """Minimal stand-in for ``WhatsAppConnection``. We only touch
    ``id``, ``tenant_id``, and ``extra_metadata`` from the metric
    callers."""
    def __init__(self) -> None:
        self.id = 99
        self.tenant_id = 33
        self.extra_metadata = {}


def test_metrics_failure_does_not_propagate_from_record_coexistence_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: simulate the exact pre-fix failure (metrics raises)
    and assert the webhook helper still completes normally.

    We patch ``record_row_flush`` to raise the original
    ``UnboundLocalError`` to mimic the prod log line verbatim. With
    the fix in place, the outer ``try/except`` in
    ``_record_coexistence_event`` swallows it and the function
    returns without exception.
    """
    from routers import whatsapp_webhook as wh
    from sqlalchemy.orm import attributes as sa_attrs

    raised = {"hit": False}

    def _boom(**_kw) -> None:
        raised["hit"] = True
        raise UnboundLocalError(
            "cannot access local variable '_row_flush_count' "
            "where it is not associated with a value"
        )

    monkeypatch.setattr(wh, "record_row_flush", _boom)
    # ``_record_coexistence_event`` imports flag_modified inline from
    # sqlalchemy.orm.attributes. Our _StubConn isn't a real SA-mapped
    # instance so flag_modified would normally crash with
    # ``AttributeError: '_sa_instance_state'``. Patch it to a no-op
    # for the duration of this test — we're testing isolation, not
    # SQLA change tracking.
    monkeypatch.setattr(sa_attrs, "flag_modified", lambda *a, **kw: None)

    class _StubDB:
        def __init__(self) -> None:
            self.added: list = []
            self.flushed = 0

        def add(self, obj) -> None:
            self.added.append(obj)

        def flush(self) -> None:
            self.flushed += 1

    db = _StubDB()
    wa = _StubConn()

    # MUST NOT RAISE — pre-fix this propagated to the outer batch
    # ``except`` and triggered a full db.rollback().
    wh._record_coexistence_event(  # noqa: SLF001
        db, wa,
        event_type="coexistence",
        category="merchant_mobile_echo",
        value={"messages": [], "message_echoes": [{"to": "966500000000"}]},
    )

    # Sanity: the metric was reached (so the bug surface is exercised).
    assert raised["hit"] is True
    # And the persistence side ran to completion BEFORE the metric.
    assert db.flushed == 1
    assert db.added, "expected the wa_conn add() to happen pre-metric"
    # The coex metadata block was attached to the row — i.e. the write
    # the metric failure used to lose is now preserved.
    coex = (wa.extra_metadata or {}).get("coexistence") or {}
    assert coex.get("last_event"), (
        f"expected coex.last_event to be written, got: {wa.extra_metadata}"
    )
