"""Rolling 24h service window — last customer inbound is the only truth."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.commerce_lifecycle.window import (  # noqa: E402
    WINDOW_SOURCE_ERROR_FAIL_CLOSED,
    WINDOW_SOURCE_WA_USAGE,
    lifecycle_service_window_is_open,
)
from core.wa_usage import (  # noqa: E402
    _open_new_window,
    has_open_service_window,
    record_customer_inbound_window,
)
from models import ConversationLog, WaConversationWindow  # noqa: E402

T0 = datetime(2026, 9, 5, 12, 0, 0)
PHONE_A = "+966500111222"
PHONE_B = "+966500999888"
TENANT_A = 20
TENANT_B = 21


def _make_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    saved = []
    for model in (WaConversationWindow, ConversationLog):
        table = model.__table__
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
        table.create(engine, checkfirst=True)
    for col, orig in saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session()


def _seed_inbound(db, *, tenant_id=TENANT_A, phone=PHONE_A, inbound_at=T0, window_start=None):
    db.add(
        WaConversationWindow(
            tenant_id=tenant_id,
            customer_phone=phone,
            window_start=window_start if window_start is not None else inbound_at,
            last_customer_inbound_at=inbound_at,
            category="service",
        )
    )
    db.commit()


def _record(db, *, tenant_id=TENANT_A, phone=PHONE_A, inbound_at, now=None):
    return record_customer_inbound_window(
        db,
        tenant_id,
        phone,
        inbound_at,
        now=now or inbound_at or T0,
        commit=True,
    )


class TestConservativeTwentyFourHourBound:
    def test_inbound_at_t0_is_open(self):
        db = _make_db()
        _record(db, inbound_at=T0, now=T0)
        assert has_open_service_window(db, TENANT_A, PHONE_A, now=T0) is True

    def test_less_than_24h_open(self):
        db = _make_db()
        _seed_inbound(db, inbound_at=T0)
        now = T0 + timedelta(hours=23, minutes=59, seconds=59)
        assert has_open_service_window(db, TENANT_A, PHONE_A, now=now) is True

    def test_exact_24h_closed(self):
        db = _make_db()
        _seed_inbound(db, inbound_at=T0)
        assert has_open_service_window(
            db, TENANT_A, PHONE_A, now=T0 + timedelta(hours=24)
        ) is False

    def test_greater_than_24h_closed(self):
        db = _make_db()
        _seed_inbound(db, inbound_at=T0)
        assert has_open_service_window(
            db, TENANT_A, PHONE_A, now=T0 + timedelta(hours=24, seconds=1)
        ) is False


class TestOutboundNeverMutatesServiceTruth:
    def test_no_prior_inbound_api_outbound_stays_closed(self):
        db = _make_db()
        _open_new_window(db, TENANT_A, PHONE_A, "service", "api", T0)
        row = db.query(WaConversationWindow).one()
        assert row.last_customer_inbound_at is None
        assert has_open_service_window(db, TENANT_A, PHONE_A, now=T0) is False

    def test_expired_inbound_plus_api_outbound_stays_closed(self):
        db = _make_db()
        _seed_inbound(db, inbound_at=T0, window_start=T0)
        later = T0 + timedelta(hours=25)
        _open_new_window(db, TENANT_A, PHONE_A, "service", "api", later)
        row = db.query(WaConversationWindow).one()
        assert row.last_customer_inbound_at == T0
        assert has_open_service_window(db, TENANT_A, PHONE_A, now=later) is False

    def test_expired_inbound_plus_template_preserves_inbound_truth(self):
        db = _make_db()
        _seed_inbound(db, inbound_at=T0, window_start=T0)
        later = T0 + timedelta(hours=25)
        _open_new_window(db, TENANT_A, PHONE_A, "marketing", "template", later)
        row = db.query(WaConversationWindow).one()
        assert row.last_customer_inbound_at == T0
        assert row.window_start == later
        assert has_open_service_window(db, TENANT_A, PHONE_A, now=later) is False

    def test_expired_inbound_plus_campaign_stays_closed(self):
        db = _make_db()
        _seed_inbound(db, inbound_at=T0, window_start=T0)
        later = T0 + timedelta(hours=25)
        _open_new_window(db, TENANT_A, PHONE_A, "marketing", "campaign", later)
        row = db.query(WaConversationWindow).one()
        assert row.last_customer_inbound_at == T0
        assert has_open_service_window(db, TENANT_A, PHONE_A, now=later) is False

    def test_active_inbound_plus_outbound_keeps_original_inbound(self):
        db = _make_db()
        _seed_inbound(db, inbound_at=T0, window_start=T0)
        _open_new_window(
            db, TENANT_A, PHONE_A, "service", "api", T0 + timedelta(hours=1)
        )
        row = db.query(WaConversationWindow).one()
        assert row.last_customer_inbound_at == T0
        assert has_open_service_window(
            db, TENANT_A, PHONE_A, now=T0 + timedelta(hours=1)
        ) is True


class TestProviderTimestampContract:
    def test_valid_timestamp_monotonic_update(self):
        db = _make_db()
        assert _record(db, inbound_at=T0, now=T0) is True
        newer = T0 + timedelta(hours=3)
        assert _record(db, inbound_at=newer, now=newer) is True
        assert db.query(WaConversationWindow).one().last_customer_inbound_at == newer

    def test_missing_timestamp_does_not_open(self):
        db = _make_db()
        assert _record(db, inbound_at=None, now=T0) is False
        assert db.query(WaConversationWindow).count() == 0
        assert has_open_service_window(db, TENANT_A, PHONE_A, now=T0) is False

    def test_malformed_timestamp_does_not_extend(self):
        db = _make_db()
        _seed_inbound(db, inbound_at=T0)
        assert record_customer_inbound_window(
            db, TENANT_A, PHONE_A, "not-a-timestamp", now=T0 + timedelta(hours=1), commit=True
        ) is False
        assert db.query(WaConversationWindow).one().last_customer_inbound_at == T0

    def test_older_delayed_timestamp_does_not_regress(self):
        db = _make_db()
        _seed_inbound(db, inbound_at=T0)
        older = T0 - timedelta(hours=2)
        _record(db, inbound_at=older, now=T0 + timedelta(minutes=5))
        assert db.query(WaConversationWindow).one().last_customer_inbound_at == T0

    def test_future_timestamp_capped_at_receipt(self):
        db = _make_db()
        future = T0 + timedelta(hours=10)
        _record(db, inbound_at=future, now=T0)
        row = db.query(WaConversationWindow).one()
        assert row.last_customer_inbound_at == T0
        assert has_open_service_window(
            db, TENANT_A, PHONE_A, now=T0 + timedelta(hours=23, minutes=59, seconds=59)
        ) is True
        assert has_open_service_window(
            db, TENANT_A, PHONE_A, now=T0 + timedelta(hours=24)
        ) is False


class TestFailClosedProvenance:
    def test_missing_state_is_normal_closed(self):
        db = _make_db()
        opened, source = lifecycle_service_window_is_open(db, TENANT_A, PHONE_A)
        assert opened is False
        assert source == WINDOW_SOURCE_WA_USAGE

    def test_injected_db_failure_is_error_source(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        opened, source = lifecycle_service_window_is_open(db, TENANT_A, PHONE_A)
        assert opened is False
        assert source == WINDOW_SOURCE_ERROR_FAIL_CLOSED

    def test_empty_phone_closed(self):
        db = _make_db()
        _seed_inbound(db)
        assert has_open_service_window(db, TENANT_A, "", now=T0) is False


class TestTenantIsolation:
    def test_tenant_a_inbound_does_not_open_tenant_b(self):
        db = _make_db()
        _record(db, tenant_id=TENANT_A, phone=PHONE_A, inbound_at=T0, now=T0)
        assert has_open_service_window(db, TENANT_A, PHONE_A, now=T0) is True
        assert has_open_service_window(db, TENANT_B, PHONE_A, now=T0) is False
        assert has_open_service_window(db, TENANT_A, PHONE_B, now=T0) is False


class TestDualTransportSelection:
    def test_open_window_is_session_path(self):
        db = _make_db()
        now = datetime.utcnow()
        _seed_inbound(db, inbound_at=now, window_start=now)
        opened, source = lifecycle_service_window_is_open(db, TENANT_A, PHONE_A)
        assert opened is True
        assert source == WINDOW_SOURCE_WA_USAGE
        assert ("session_message" if opened else "approved_template") == "session_message"

    def test_closed_window_is_template_path(self):
        db = _make_db()
        _seed_inbound(
            db,
            inbound_at=datetime.utcnow() - timedelta(hours=25),
        )
        opened, source = lifecycle_service_window_is_open(db, TENANT_A, PHONE_A)
        assert opened is False
        assert source == WINDOW_SOURCE_WA_USAGE
        assert ("session_message" if opened else "approved_template") == "approved_template"

    def test_error_window_is_fail_closed_template_path(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("query failed")
        opened, source = lifecycle_service_window_is_open(db, TENANT_A, PHONE_A)
        assert opened is False
        assert source == WINDOW_SOURCE_ERROR_FAIL_CLOSED
        assert ("session_message" if opened else "approved_template") == "approved_template"


class TestWebhookInboundBeforeShortCircuits:
    def _webhook_src(self) -> str:
        return (BACKEND_DIR / "routers" / "whatsapp_webhook.py").read_text(encoding="utf-8")

    def test_record_runs_after_dedup_before_unsubscribe_cod_normalizer_brain(self):
        src = self._webhook_src()
        record_at = src.index("record_customer_inbound_window(")
        assert "inbound_at=_wa_msg_ts" in src[record_at : record_at + 500]
        assert src.index("normalized_sender = normalize_phone") < record_at
        assert src.index("IdempotencyGuard.is_duplicate") < record_at
        assert record_at < src.index("is_unsubscribe_request(")
        assert record_at < src.index("UNSUB_CONFIRM_BUTTON_ID")
        assert record_at < src.index("UNSUB_CANCEL_BUTTON_ID")
        assert record_at < src.index("consume_owned_cod_button_inbound")
        assert record_at < src.index("normalize_whatsapp_inbound(")
        assert record_at < src.index("button_reply (generic)")
        assert record_at < src.index("_handle_merchant_message(")

    def test_unsubscribe_keyword_and_buttons_are_after_record(self):
        src = self._webhook_src()
        record_at = src.index("record_customer_inbound_window(")
        unsub_kw = src.index("is_unsubscribe_request(_inbound_text)")
        unsub_confirm = src.index("_btn_id == UNSUB_CONFIRM_BUTTON_ID")
        unsub_cancel = src.index("_btn_id == UNSUB_CANCEL_BUTTON_ID")
        assert record_at < unsub_kw < src.index("EVENT_UNSUB_SHORT_CIRCUIT")
        assert record_at < unsub_confirm
        assert record_at < unsub_cancel

    def test_cod_confirm_cancel_refresh_then_brain_zero(self):
        src = self._webhook_src()
        record_at = src.index("record_customer_inbound_window(")
        interactive = src.index("normalized_type == \"interactive\"")
        brain_generic = src.index("button_reply (generic)")
        block = src[interactive:brain_generic]
        assert record_at < interactive
        assert "is_owned_cod_button_payload(btn_id)" in block
        assert "consume_owned_cod_button_inbound" in block
        owned_at = block.index("if is_owned_cod_button_payload(btn_id)")
        assert block.find("return", owned_at) > 0
        assert "classify_cod_reply(btn_txt)" not in block
        button_rescue = src.index("msg_type == \"button\"")
        merchant_rescue = src.index("_handle_merchant_message", button_rescue)
        rescue = src[button_rescue:merchant_rescue]
        assert "is_owned_cod_button_payload(_btn_payload)" in rescue
        assert "consume_owned_cod_button_inbound" in rescue
        assert "classify_cod_reply(_wa_text)" not in rescue


class TestZeroAi:
    def test_window_modules_do_not_call_model(self):
        forbidden = ("MerchantBrain", "openai", "anthropic")
        for rel in (
            "core/wa_usage.py",
            "core/commerce_lifecycle/window.py",
            "core/commerce_lifecycle/dispatch.py",
        ):
            text = (BACKEND_DIR / rel).read_text(encoding="utf-8")
            for token in forbidden:
                assert token not in text, f"{rel} contains {token}"
