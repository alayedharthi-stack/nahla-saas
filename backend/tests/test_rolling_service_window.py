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


def _seed_service(db, *, tenant_id=TENANT_A, phone=PHONE_A, start=T0):
    db.add(
        WaConversationWindow(
            tenant_id=tenant_id,
            customer_phone=phone,
            window_start=start,
            category="service",
        )
    )
    db.commit()


def _inbound(db, *, tenant_id=TENANT_A, phone=PHONE_A, inbound_at, now=None):
    return _open_new_window(
        db,
        tenant_id,
        phone,
        "service",
        "inbound",
        now or inbound_at,
        inbound_at=inbound_at,
    )


class TestConservativeTwentyFourHourBound:
    def test_inbound_at_t0_is_open(self):
        db = _make_db()
        _inbound(db, inbound_at=T0)
        assert has_open_service_window(db, TENANT_A, PHONE_A, now=T0) is True

    def test_23_59_59_inside(self):
        db = _make_db()
        _seed_service(db, start=T0)
        now = T0 + timedelta(hours=23, minutes=59, seconds=59)
        assert has_open_service_window(db, TENANT_A, PHONE_A, now=now) is True

    def test_exact_24_00_00_outside(self):
        db = _make_db()
        _seed_service(db, start=T0)
        assert has_open_service_window(
            db, TENANT_A, PHONE_A, now=T0 + timedelta(hours=24)
        ) is False

    def test_greater_than_24h_outside(self):
        db = _make_db()
        _seed_service(db, start=T0)
        assert has_open_service_window(
            db, TENANT_A, PHONE_A, now=T0 + timedelta(hours=24, seconds=1)
        ) is False


class TestInboundRefreshesRollingWindow:
    def test_later_inbound_extends_window_without_new_billable(self):
        db = _make_db()
        _seed_service(db, start=T0)
        t_later = T0 + timedelta(hours=10)
        billed = _inbound(db, inbound_at=t_later, now=t_later)
        assert billed is False
        row = db.query(WaConversationWindow).one()
        assert row.window_start == t_later
        still_open_at = t_later + timedelta(hours=23, minutes=59, seconds=59)
        assert has_open_service_window(db, TENANT_A, PHONE_A, now=still_open_at) is True
        closed_at = t_later + timedelta(hours=24)
        assert has_open_service_window(db, TENANT_A, PHONE_A, now=closed_at) is False
        assert db.query(ConversationLog).count() == 0

    def test_inbound_after_expiry_opens_new_window(self):
        db = _make_db()
        _seed_service(db, start=T0)
        t_new = T0 + timedelta(hours=25)
        billed = _inbound(db, inbound_at=t_new, now=t_new)
        assert billed is True
        row = db.query(WaConversationWindow).one()
        assert row.window_start == t_new
        assert row.category == "service"
        assert has_open_service_window(db, TENANT_A, PHONE_A, now=t_new) is True
        assert db.query(ConversationLog).count() == 1


class TestOutboundDoesNotRefresh:
    def test_template_source_does_not_refresh_service_anchor(self):
        db = _make_db()
        _seed_service(db, start=T0)
        billed = _open_new_window(
            db, TENANT_A, PHONE_A, "marketing", "template", T0 + timedelta(hours=1)
        )
        assert billed is False
        row = db.query(WaConversationWindow).one()
        assert row.window_start == T0
        assert row.category == "service"

    def test_campaign_outbound_does_not_refresh(self):
        db = _make_db()
        _seed_service(db, start=T0)
        _open_new_window(
            db, TENANT_A, PHONE_A, "marketing", "campaign", T0 + timedelta(hours=2)
        )
        assert db.query(WaConversationWindow).one().window_start == T0

    def test_api_ai_outbound_does_not_refresh(self):
        db = _make_db()
        _seed_service(db, start=T0)
        _open_new_window(
            db, TENANT_A, PHONE_A, "service", "api", T0 + timedelta(hours=3)
        )
        row = db.query(WaConversationWindow).one()
        assert row.window_start == T0
        assert row.category == "service"


class TestConversationCreationDoesNotOpenWindow:
    def test_missing_window_is_closed(self):
        db = _make_db()
        assert has_open_service_window(db, TENANT_A, PHONE_A, now=T0) is False

    def test_runtime_window_row_only_written_from_wa_usage(self):
        src = (BACKEND_DIR / "core" / "wa_usage.py").read_text(encoding="utf-8")
        runtime_hits = []
        for rel in (
            "routers/conversations.py",
            "core/automation_engine.py",
            "modules/ai/brain/pipeline.py",
        ):
            text = (BACKEND_DIR / rel).read_text(encoding="utf-8")
            assert "WaConversationWindow(" not in text
            runtime_hits.append(rel)
        assert "WaConversationWindow(" in src
        assert runtime_hits


class TestFailClosed:
    def test_missing_state_closed(self):
        db = _make_db()
        opened, source = lifecycle_service_window_is_open(db, TENANT_A, PHONE_A)
        assert opened is False
        assert source == WINDOW_SOURCE_WA_USAGE

    def test_read_exception_closed(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        assert has_open_service_window(db, TENANT_A, PHONE_A, now=T0) is False

    def test_empty_phone_closed(self):
        db = _make_db()
        _seed_service(db)
        assert has_open_service_window(db, TENANT_A, "", now=T0) is False

    def test_lifecycle_wrapper_error_source(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        with patch(
            "core.wa_usage.has_open_service_window",
            side_effect=RuntimeError("boom"),
        ):
            opened, source = lifecycle_service_window_is_open(db, TENANT_A, PHONE_A)
        assert opened is False
        assert source == WINDOW_SOURCE_ERROR_FAIL_CLOSED


class TestMonotonicInbound:
    def test_replay_does_not_regress_timestamp(self):
        db = _make_db()
        _inbound(db, inbound_at=T0, now=T0)
        billed = _inbound(db, inbound_at=T0, now=T0 + timedelta(hours=2))
        assert billed is False
        assert db.query(WaConversationWindow).one().window_start == T0

    def test_older_out_of_order_inbound_does_not_regress(self):
        db = _make_db()
        _seed_service(db, start=T0)
        older = T0 - timedelta(hours=1)
        _inbound(db, inbound_at=older, now=T0 + timedelta(minutes=5))
        assert db.query(WaConversationWindow).one().window_start == T0

    def test_newer_inbound_wins(self):
        db = _make_db()
        _seed_service(db, start=T0)
        newer = T0 + timedelta(hours=4)
        _inbound(db, inbound_at=newer, now=newer)
        assert db.query(WaConversationWindow).one().window_start == newer


class TestTenantIsolation:
    def test_tenant_a_inbound_does_not_open_tenant_b(self):
        db = _make_db()
        _inbound(db, tenant_id=TENANT_A, phone=PHONE_A, inbound_at=T0, now=T0)
        assert has_open_service_window(db, TENANT_A, PHONE_A, now=T0) is True
        assert has_open_service_window(db, TENANT_B, PHONE_A, now=T0) is False
        assert has_open_service_window(db, TENANT_A, PHONE_B, now=T0) is False


class TestDualTransportSelection:
    def test_open_window_is_session_path(self):
        db = _make_db()
        _seed_service(db, start=datetime.utcnow())
        opened, source = lifecycle_service_window_is_open(db, TENANT_A, PHONE_A)
        assert opened is True
        assert source == WINDOW_SOURCE_WA_USAGE
        send_method = "session_message" if opened else "approved_template"
        assert send_method == "session_message"

    def test_closed_window_is_template_path(self):
        db = _make_db()
        _seed_service(db, start=datetime.utcnow() - timedelta(hours=25))
        opened, source = lifecycle_service_window_is_open(db, TENANT_A, PHONE_A)
        assert opened is False
        assert source == WINDOW_SOURCE_WA_USAGE
        send_method = "session_message" if opened else "approved_template"
        assert send_method == "approved_template"

    def test_error_window_is_fail_closed_template_path(self):
        with patch(
            "core.wa_usage.has_open_service_window",
            side_effect=RuntimeError("query failed"),
        ):
            opened, source = lifecycle_service_window_is_open(
                MagicMock(), TENANT_A, PHONE_A
            )
        assert opened is False
        assert source == WINDOW_SOURCE_ERROR_FAIL_CLOSED
        send_method = "session_message" if opened else "approved_template"
        assert send_method == "approved_template"


class TestCodInboundAndBrainZero:
    def test_webhook_records_inbound_before_cod_consume(self):
        src = (BACKEND_DIR / "routers" / "whatsapp_webhook.py").read_text(
            encoding="utf-8"
        )
        track_at = src.index("track_conversation(")
        assert "inbound_at=_wa_msg_ts" in src[track_at : track_at + 400]
        interactive = src.index("normalized_type == \"interactive\"")
        assert track_at < interactive
        consume_at = src.index("consume_owned_cod_button_inbound")
        assert track_at < consume_at

    def test_recognized_cod_button_stays_brain_zero(self):
        src = (BACKEND_DIR / "routers" / "whatsapp_webhook.py").read_text(
            encoding="utf-8"
        )
        interactive = src.index("normalized_type == \"interactive\"")
        brain_generic = src.index("button_reply (generic)")
        block = src[interactive:brain_generic]
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
