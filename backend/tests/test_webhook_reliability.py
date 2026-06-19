"""
backend/tests/test_webhook_reliability.py
─────────────────────────────────────────
Regression suite for the May 2026 #37 reliability work:

  1. AI Quality dashboard noise filter — ``reaction`` / ``revoke``
     / ``ephemeral`` / ``system`` inbound types must NOT open
     ``ai_quality_events`` rows. Production observed dozens of
     such rows per day from emoji reactions alone, drowning out
     the signals merchants actually need to triage.

  2. Coexistence echo timestamp guard — the merchant's outbound
     mobile-app echoes (``smb_message_echoes``) used to call
     ``upsert_customer_identity(source="whatsapp_inbound")`` via
     ``_get_or_create_conversation``. That triggered an implicit
     ``UPDATE customers SET last_interaction_at=…`` per echo
     which (a) wrongly bumped customer activity timestamps for
     merchant-side messages and (b) could hit the 5s
     ``statement_timeout`` and crash the whole coexistence batch.
     The ``whatsapp_outbound_echo`` source is the dedicated
     channel that finds-or-creates the customer row WITHOUT
     touching the timestamp.

  3. Best-effort flush — when the per-echo flush DOES hit a
     ``QueryCanceled`` / ``OperationalError``, the ingest must
     swallow it and return without raising; the webhook 200-ack
     stays intact and the next batch keeps flowing. We assert
     the wrapper logs ``[LAST_INTERACTION] flush=timeout_or_op_error``
     and exits cleanly rather than propagating.

The tests stay hermetic — no real DB, no HTTP — by stubbing
SessionLocal / SQLAlchemy as the existing
``test_smb_echo_media_ingest.py`` already does for echoes.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Noise filter — reaction / revoke / ephemeral / system
# ─────────────────────────────────────────────────────────────────────────────


def test_is_noise_inbound_type_recognises_reaction_and_revoke() -> None:
    """The four protocol-noise types must report True; everything
    else (including the empty string) must report False."""
    from core.inbound_observability import is_noise_inbound_type

    assert is_noise_inbound_type("reaction") is True
    assert is_noise_inbound_type("revoke") is True
    assert is_noise_inbound_type("ephemeral") is True
    assert is_noise_inbound_type("system") is True

    # Case-insensitivity safeguards the call site against the
    # normalizer accidentally returning "Reaction" instead of
    # "reaction" for a future provider variant.
    assert is_noise_inbound_type("REACTION") is True
    assert is_noise_inbound_type(" Reaction ") is True

    # Sticker / location / contacts / unsupported are NOT noise —
    # merchants may want to act on them, so the dashboard still
    # opens a row.
    assert is_noise_inbound_type("sticker") is False
    assert is_noise_inbound_type("location") is False
    assert is_noise_inbound_type("contacts") is False
    assert is_noise_inbound_type("unsupported") is False
    assert is_noise_inbound_type("text") is False
    assert is_noise_inbound_type("") is False
    assert is_noise_inbound_type(None) is False


def test_record_inbound_drop_skips_db_write_for_reaction(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``record_inbound_drop`` with ``drop_kind=DROP_UNSUPPORTED_TYPE``
    AND ``normalized_type='reaction'`` must:
      * NOT call the underlying ``_write_event`` writer (no DB row).
      * Emit a structured ``[INBOUND_NOISE_FILTER]`` INFO log so
        operators still have an audit trail in the application log.
      * Return ``None`` so the call site can branch on "row written?"
        without surfacing the filter to upstream code."""
    from core import inbound_observability as obs

    write_mock = MagicMock(return_value=999)
    monkeypatch.setattr(obs, "_write_event", write_mock)

    caplog.set_level(logging.INFO, logger="nahla.inbound_observability")

    result = obs.record_inbound_drop(
        tenant_id=33,
        drop_kind=obs.DROP_UNSUPPORTED_TYPE,
        customer_phone="966500000001",
        chosen_path="normalized_type=reaction",
        detail="msg_type='reaction' normalized_type='reaction'",
        normalized_type="reaction",
    )

    assert result is None, (
        "noise-filtered drops must return None so callers don't "
        "treat them as DB rows"
    )
    write_mock.assert_not_called()

    # The audit trail still exists in the log.
    msgs = [r.getMessage() for r in caplog.records]
    assert any("[INBOUND_NOISE_FILTER]" in m for m in msgs), (
        f"expected an [INBOUND_NOISE_FILTER] line in log; got {msgs!r}"
    )
    assert any("normalized_type=reaction" in m for m in msgs)


def test_record_inbound_drop_writes_row_for_sticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sticker`` is unsupported but NOT noise — the dashboard must
    still surface it so merchants decide whether to add support.
    """
    from core import inbound_observability as obs

    write_mock = MagicMock(return_value=42)
    monkeypatch.setattr(obs, "_write_event", write_mock)

    result = obs.record_inbound_drop(
        tenant_id=33,
        drop_kind=obs.DROP_UNSUPPORTED_TYPE,
        customer_phone="966500000001",
        chosen_path="normalized_type=sticker",
        detail="msg_type='sticker' normalized_type='sticker'",
        normalized_type="sticker",
    )

    assert result == 42
    write_mock.assert_called_once()


def test_record_inbound_drop_writes_row_when_normalized_type_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward-compat: existing call sites that don't pass the new
    ``normalized_type`` kwarg must keep behaving exactly as before
    (one row per drop)."""
    from core import inbound_observability as obs

    write_mock = MagicMock(return_value=7)
    monkeypatch.setattr(obs, "_write_event", write_mock)

    result = obs.record_inbound_drop(
        tenant_id=33,
        drop_kind=obs.DROP_PRE_BRAIN_HANDOFF,
        customer_phone="966500000001",
    )

    assert result == 7
    write_mock.assert_called_once()


def test_noise_filter_only_applies_to_unsupported_drop_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A future bug where some other drop_kind happens to receive a
    ``normalized_type='reaction'`` (e.g. an empty-text drop carrying
    a reaction payload) must STILL be persisted — the filter is
    deliberately scoped to ``DROP_UNSUPPORTED_TYPE`` so unrelated
    drops don't go silent."""
    from core import inbound_observability as obs

    write_mock = MagicMock(return_value=5)
    monkeypatch.setattr(obs, "_write_event", write_mock)

    result = obs.record_inbound_drop(
        tenant_id=33,
        drop_kind=obs.DROP_DISPATCHER_EXCEPTION,
        normalized_type="reaction",
    )

    assert result == 5
    write_mock.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# 2. Echo source skips last_interaction_at update
# ─────────────────────────────────────────────────────────────────────────────


class _StubCustomer:
    """Tracks every attribute write so the test can assert that
    ``last_interaction_at`` was NOT touched on echo updates."""
    def __init__(self) -> None:
        self.id = 1
        self.tenant_id = 33
        self.phone = "966500000001"
        self.normalized_phone = "966500000001"
        self.name = "خالد"
        self.email = None
        self.extra_metadata: Dict[str, Any] = {}
        self.salla_customer_id = None
        self.acquisition_channel = "whatsapp_inbound"
        self.first_seen_at = None
        self.last_interaction_at = None
        # Tracks which attributes were assigned post-construction.
        self._writes: List[str] = []

    def __setattr__(self, key: str, value: Any) -> None:
        if "_writes" in self.__dict__ and not key.startswith("_"):
            self._writes.append(key)
        object.__setattr__(self, key, value)


class _StubDB:
    def __init__(self, customer: _StubCustomer) -> None:
        self._customer = customer
        self.added: List[Any] = []
        self.flushed = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        self.flushed += 1

    def query(self, *_a, **_k) -> "_StubDB":
        return self


def test_outbound_echo_source_does_not_update_last_interaction_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``upsert_customer_identity(source='whatsapp_outbound_echo')``
    must FIND the existing customer and update phone / metadata as
    needed, but MUST NOT assign ``last_interaction_at`` NOR adopt a
    name from merchant-mobile echo text.
    """
    from datetime import datetime, timezone

    from services.customer_intelligence import CustomerIntelligenceService

    customer = _StubCustomer()
    db = _StubDB(customer)
    svc = CustomerIntelligenceService(db, tenant_id=33)

    # Stub identity resolution to return our customer.
    monkeypatch.setattr(
        svc, "_find_customer_by_external_id", lambda _eid: None,
    )
    monkeypatch.setattr(svc, "find_customer_by_phone", lambda _e164: customer)
    monkeypatch.setattr(svc, "_query_customers", lambda: [])
    monkeypatch.setattr(svc, "ensure_profile", lambda _c, **_kw: None)

    pre_writes = list(customer._writes)
    pre_name = customer.name
    svc.upsert_customer_identity(
        phone="966500000001",
        name="ايه وقف النحلة شغلتنا",
        source="whatsapp_outbound_echo",
        seen_at=datetime.now(timezone.utc),
    )

    # The function may legitimately update phone/email/metadata,
    # but ``last_interaction_at`` is the single attribute we MUST
    # NOT see in the writes list, and echo text must never become
    # the canonical customer name.
    new_writes = customer._writes[len(pre_writes):]
    assert "last_interaction_at" not in new_writes, (
        "echo source unexpectedly bumped last_interaction_at "
        f"(writes seen: {new_writes!r})"
    )
    assert customer.name == pre_name, (
        "echo source must not adopt merchant message text as Customer.name"
    )


def test_inbound_source_still_updates_last_interaction_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: real customer-driven inbound (the default
    ``whatsapp_inbound`` source) MUST still bump
    ``last_interaction_at``. We don't want the new echo carve-out
    to accidentally suppress the timestamp on actual customer
    messages."""
    from datetime import datetime, timezone

    from services.customer_intelligence import CustomerIntelligenceService

    customer = _StubCustomer()
    db = _StubDB(customer)
    svc = CustomerIntelligenceService(db, tenant_id=33)
    monkeypatch.setattr(
        svc, "_find_customer_by_external_id", lambda _eid: None,
    )
    monkeypatch.setattr(svc, "find_customer_by_phone", lambda _e164: customer)
    monkeypatch.setattr(svc, "_query_customers", lambda: [])
    monkeypatch.setattr(svc, "ensure_profile", lambda _c, **_kw: None)

    pre_writes = list(customer._writes)
    svc.upsert_customer_identity(
        phone="966500000001",
        name="خالد",
        source="whatsapp_inbound",
        seen_at=datetime.now(timezone.utc),
    )

    new_writes = customer._writes[len(pre_writes):]
    assert "last_interaction_at" in new_writes, (
        "inbound source must still update last_interaction_at "
        f"(writes seen: {new_writes!r})"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Best-effort echo flush survives statement_timeout
# ─────────────────────────────────────────────────────────────────────────────


class _TimeoutDB:
    """SA-shaped stub whose ``flush()`` raises QueryCanceled the first
    time and succeeds afterwards — mirrors what production sees when
    the customers table is briefly under contention.
    """
    def __init__(self) -> None:
        self.added: List[Any] = []
        self.flush_calls = 0
        self.rollback_calls = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        self.flush_calls += 1
        from sqlalchemy.exc import OperationalError
        # mimic psycopg2.errors.QueryCanceled wrapper shape
        raise OperationalError(
            "UPDATE customers SET last_interaction_at=…",
            {}, Exception("canceling statement due to statement timeout"),
        )

    def rollback(self) -> None:
        self.rollback_calls += 1

    def query(self, *_a, **_k):
        return self


def test_echo_flush_timeout_does_not_crash_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Production trace: an ``OperationalError`` on the per-echo
    flush used to propagate up and kill the entire coexistence
    batch (``[Webhook360] smb_message_echoes branch failed``).
    The new wrapper must:
      * Roll back the session so it's reusable for the next batch.
      * Log a structured ``[LAST_INTERACTION] flush=timeout_or_op_error``
        warning so the operator dashboard can chart timeout rate.
      * Return without raising.
    """
    from routers import whatsapp_webhook as wh

    convo = MagicMock()
    convo.id = 12345
    convo.customer_id = 7
    convo.status = "active"

    monkeypatch.setattr(
        "routers.conversations._get_or_create_conversation",
        lambda *_a, **_k: convo,
    )

    db = _TimeoutDB()
    wa = MagicMock()
    wa.tenant_id = 33

    value = {
        "metadata": {"phone_number_id": "PID_360"},
        "message_echoes": [{
            "to": "966500000001",
            "id": "wamid.echo.text",
            "type": "text",
            "text": {"body": "أهلا"},
        }],
    }

    caplog.set_level(logging.INFO, logger="nahla-backend")

    # MUST NOT raise — the whole point of the wrapper is that the
    # outer 360dialog handler keeps its 200-ack envelope.
    _run(wh._ingest_smb_message_echoes(db, wa, value))

    assert db.flush_calls == 1
    assert db.rollback_calls == 1, (
        "the wrapper must rollback so the session is reusable for "
        "the next coexistence batch"
    )
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "[LAST_INTERACTION] flush=timeout_or_op_error" in m for m in msgs
    ), f"missing timeout telemetry — got: {msgs!r}"


def test_echo_flush_emits_per_echo_last_interaction_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each echo must emit one ``[LAST_INTERACTION] tenant=… …
    last_interaction_at_skipped=True`` line so the operator dashboard
    can later chart "how many merchant echoes did we ingest" /
    "did any echo wrongly touch customer.last_interaction_at?".
    """
    from routers import whatsapp_webhook as wh

    convo = MagicMock()
    convo.id = 12345
    convo.customer_id = 7
    convo.status = "active"

    monkeypatch.setattr(
        "routers.conversations._get_or_create_conversation",
        lambda *_a, **_k: convo,
    )

    class _OkDB:
        def __init__(self) -> None:
            self.added: List[Any] = []
            self.flushed = 0

        def add(self, obj: Any) -> None:
            self.added.append(obj)

        def flush(self) -> None:
            self.flushed += 1

    db = _OkDB()
    wa = MagicMock()
    wa.tenant_id = 33

    value = {
        "metadata": {"phone_number_id": "PID_360"},
        "message_echoes": [
            {"to": "966500000001", "id": "e1", "type": "text",
             "text": {"body": "one"}},
            {"to": "966500000002", "id": "e2", "type": "text",
             "text": {"body": "two"}},
        ],
    }

    caplog.set_level(logging.INFO, logger="nahla-backend")
    _run(wh._ingest_smb_message_echoes(db, wa, value))

    msgs = [r.getMessage() for r in caplog.records]
    skip_lines = [
        m for m in msgs
        if "[LAST_INTERACTION]" in m
        and "last_interaction_at_skipped=True" in m
    ]
    assert len(skip_lines) == 2, (
        f"expected one skip telemetry line per echo (got {len(skip_lines)}): "
        f"{msgs!r}"
    )
    # Successful flush also emits an [LAST_INTERACTION] flush=ok line.
    assert any(
        "[LAST_INTERACTION] flush=ok" in m for m in msgs
    ), f"missing flush=ok telemetry — got: {msgs!r}"
