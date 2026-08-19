"""Merchant first-contact email: once per tenant-scoped customer relationship."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (REPO_ROOT, BACKEND_DIR, REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import Base, Customer, Tenant, User  # noqa: E402
from services.merchant_first_contact import (  # noqa: E402
    EVENT_FIRST_CUSTOMER_CONTACT,
    STAMP_KEY,
    maybe_notify_first_customer,
    stamp_value,
    suppress_first_contact,
    try_claim_first_contact,
)
from services.email_service import _render  # noqa: E402

NOW = datetime.now(timezone.utc)
LONG_EMAIL = "gbpnshjxuhtlug7y@email.partners"


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    return sessionmaker(bind=engine)


@pytest.fixture
def db():
    Session = _make_db()
    s = Session()
    try:
        yield s
    finally:
        s.close()


def _tenant(db, name: str) -> Tenant:
    t = Tenant(name=name, is_active=True)
    db.add(t)
    db.flush()
    return t


def _merchant(db, tenant: Tenant, *, email: str, username: str) -> User:
    row = User(
        tenant_id=tenant.id,
        email=email,
        username=username,
        role="merchant",
        password_hash="x",
    )
    db.add(row)
    db.flush()
    return row


def _customer(
    db,
    tenant: Tenant,
    *,
    phone: str,
    channel="whatsapp_inbound",
    notified=None,
    first_seen=None,
):
    meta = None
    if notified is not None:
        stamp = notified.isoformat() if hasattr(notified, "isoformat") else str(notified)
        meta = {STAMP_KEY: stamp}
    row = Customer(
        tenant_id=tenant.id,
        phone=phone,
        normalized_phone=phone,
        name="أحمد سالم",
        acquisition_channel=channel,
        first_seen_at=first_seen if first_seen is not None else NOW,
        last_interaction_at=NOW,
        extra_metadata=meta,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_new_whatsapp_customer_sends_one_email(db):
    t = _tenant(db, "متجر تجريبي عام")
    _merchant(db, t, email="owner@example.com", username="نورة")
    cust = _customer(db, t, phone="+966542980511")
    with patch("services.email_service.enqueue_email") as enq:
        result = maybe_notify_first_customer(
            db=db,
            tenant_id=t.id,
            customer=cust,
            customer_phone="+966542980511",
            customer_name="أحمد سالم",
            message_preview="السلام عليكم",
        )
    assert result["send"] is True
    assert result["reason"] == EVENT_FIRST_CUSTOMER_CONTACT
    assert enq.call_count == 1
    kwargs = enq.call_args.kwargs
    assert kwargs["to"] == "owner@example.com"
    assert kwargs["template"] == "first_whatsapp_message"
    assert "عميل جديد" in kwargs["subject"]
    db.refresh(cust)
    assert stamp_value(cust) is not None


def test_second_message_does_not_email(db):
    t = _tenant(db, "متجر قميص قطني")
    _merchant(db, t, email="owner@example.com", username="نورة")
    cust = _customer(db, t, phone="+966500000002")
    with patch("services.email_service.enqueue_email") as enq:
        first = maybe_notify_first_customer(
            db=db, tenant_id=t.id, customer=cust, customer_phone="+966500000002",
        )
        second = maybe_notify_first_customer(
            db=db, tenant_id=t.id, customer=cust, customer_phone="+966500000002",
        )
    assert first["send"] is True
    assert second["send"] is False
    assert second["reason"] == "already_notified"
    assert enq.call_count == 1


def test_twenty_messages_still_one_email(db):
    t = _tenant(db, "متجر عطر ورد")
    _merchant(db, t, email="owner@example.com", username="نورة")
    cust = _customer(db, t, phone="+966500000003")
    with patch("services.email_service.enqueue_email") as enq:
        sent = [
            maybe_notify_first_customer(
                db=db, tenant_id=t.id, customer=cust, customer_phone="+966500000003",
            )["send"]
            for _ in range(20)
        ]
    assert sent.count(True) == 1
    assert sent[0] is True
    assert enq.call_count == 1


def test_webhook_retry_same_customer_no_duplicate(db):
    t = _tenant(db, "متجر حذاء رياضي")
    _merchant(db, t, email="owner@example.com", username="نورة")
    cust = _customer(db, t, phone="+966500000004")
    with patch("services.email_service.enqueue_email") as enq:
        a = try_claim_first_contact(db=db, tenant_id=t.id, customer=cust)
        b = try_claim_first_contact(db=db, tenant_id=t.id, customer=cust)
    assert a["send"] is True
    assert b["send"] is False
    assert enq.call_count == 0


def test_near_simultaneous_claims_only_one_wins(db):
    t = _tenant(db, "متجر إكسسوارات")
    cust = _customer(db, t, phone="+966500000005")
    first = try_claim_first_contact(db=db, tenant_id=t.id, customer=cust)
    stale = Customer(
        tenant_id=t.id,
        phone="+966500000005",
        normalized_phone="+966500000005",
        acquisition_channel="whatsapp_inbound",
        extra_metadata={},
    )
    stale.id = cust.id
    second = try_claim_first_contact(db=db, tenant_id=t.id, customer=stale)
    assert first["send"] is True
    assert second["send"] is False


def test_same_phone_other_tenant_gets_independent_email(db):
    phone = "+966542980511"
    a = _tenant(db, "متجر أ")
    b = _tenant(db, "متجر ب")
    _merchant(db, a, email="a@example.com", username="تاجر-أ")
    _merchant(db, b, email="b@example.com", username="تاجر-ب")
    ca = _customer(db, a, phone=phone)
    cb = _customer(db, b, phone=phone)
    with patch("services.email_service.enqueue_email") as enq:
        ra = maybe_notify_first_customer(db=db, tenant_id=a.id, customer=ca, customer_phone=phone)
        rb = maybe_notify_first_customer(db=db, tenant_id=b.id, customer=cb, customer_phone=phone)
    assert ra["send"] is True
    assert rb["send"] is True
    assert enq.call_count == 2
    assert {c.kwargs["to"] for c in enq.call_args_list} == {"a@example.com", "b@example.com"}


def test_new_conversation_same_customer_does_not_reemail(db):
    t = _tenant(db, "متجر هدايا")
    _merchant(db, t, email="owner@example.com", username="نورة")
    cust = _customer(db, t, phone="+966500000006")
    with patch("services.email_service.enqueue_email") as enq:
        maybe_notify_first_customer(db=db, tenant_id=t.id, customer=cust, customer_phone="+966500000006")
        cust.last_interaction_at = NOW + timedelta(days=2)
        db.commit()
        again = maybe_notify_first_customer(
            db=db, tenant_id=t.id, customer=cust, customer_phone="+966500000006",
        )
    assert again["send"] is False
    assert enq.call_count == 1


def test_archive_does_not_reset_first_contact(db):
    t = _tenant(db, "متجر ملابس")
    _merchant(db, t, email="owner@example.com", username="نورة")
    notified_at = NOW - timedelta(days=3)
    cust = _customer(db, t, phone="+966500000007", notified=notified_at)
    with patch("services.email_service.enqueue_email") as enq:
        result = maybe_notify_first_customer(
            db=db, tenant_id=t.id, customer=cust, customer_phone="+966500000007",
        )
    assert result["send"] is False
    assert result["reason"] == "already_notified"
    assert enq.call_count == 0
    db.refresh(cust)
    assert stamp_value(cust) == notified_at.isoformat()


def test_imported_customer_does_not_get_new_customer_email(db):
    t = _tenant(db, "متجر مستورد")
    _merchant(db, t, email="owner@example.com", username="نورة")
    cust = _customer(db, t, phone="+966500000008", channel="manual_import")
    with patch("services.email_service.enqueue_email") as enq:
        result = maybe_notify_first_customer(
            db=db, tenant_id=t.id, customer=cust, customer_phone="+966500000008",
        )
    assert result["send"] is False
    assert result["reason"] == "existing_relationship"
    assert enq.call_count == 0
    db.refresh(cust)
    assert stamp_value(cust) is not None
    again = maybe_notify_first_customer(
        db=db, tenant_id=t.id, customer=cust, customer_phone="+966500000008",
    )
    assert again["send"] is False


def test_salla_synced_customer_does_not_get_new_customer_email(db):
    t = _tenant(db, "متجر سلة")
    cust = _customer(db, t, phone="+966500000009", channel="salla_sync")
    result = try_claim_first_contact(db=db, tenant_id=t.id, customer=cust)
    assert result["send"] is False
    assert result["reason"] == "existing_relationship"


def test_silence_does_not_reemail(db):
    t = _tenant(db, "متجر صمت")
    _merchant(db, t, email="owner@example.com", username="نورة")
    cust = _customer(
        db, t, phone="+966500000010",
        notified=NOW - timedelta(hours=30),
    )
    cust.last_interaction_at = NOW - timedelta(hours=30)
    db.commit()
    with patch("services.email_service.enqueue_email") as enq:
        result = maybe_notify_first_customer(
            db=db, tenant_id=t.id, customer=cust, customer_phone="+966500000010",
        )
    assert result["send"] is False
    assert enq.call_count == 0


def test_preexisting_whatsapp_customer_is_not_new_now(db):
    t = _tenant(db, "متجر قديم")
    _merchant(db, t, email="owner@example.com", username="نورة")
    cust = _customer(
        db, t, phone="+966500000011",
        first_seen=NOW - timedelta(days=12),
    )
    with patch("services.email_service.enqueue_email") as enq:
        result = maybe_notify_first_customer(
            db=db, tenant_id=t.id, customer=cust, customer_phone="+966500000011",
        )
    assert result["send"] is False
    assert result["reason"] == "existing_relationship"
    assert enq.call_count == 0
    db.refresh(cust)
    assert stamp_value(cust) is not None


def test_history_suppress_then_live_inbound_does_not_email(db):
    t = _tenant(db, "متجر تاريخي")
    _merchant(db, t, email="owner@example.com", username="نورة")
    cust = _customer(db, t, phone="+966500000012")
    suppressed = suppress_first_contact(db=db, tenant_id=t.id, customer=cust)
    assert suppressed["send"] is False
    with patch("services.email_service.enqueue_email") as enq:
        live = maybe_notify_first_customer(
            db=db, tenant_id=t.id, customer=cust, customer_phone="+966500000012",
        )
    assert live["send"] is False
    assert live["reason"] == "already_notified"
    assert enq.call_count == 0


def test_notify_runs_before_unsubscribe_short_circuit():
    text = (BACKEND_DIR / "routers" / "whatsapp_webhook.py").read_text(encoding="utf-8")
    notify_at = text.index("maybe_notify_first_customer(")
    unsub_return_at = text.index("if _unsub_short_circuit:")
    assert notify_at < unsub_return_at
    assert "suppress_first_contact" in text
    assert text.count("maybe_notify_first_customer(") == 1


def test_history_gate_still_wraps_notify_in_webhook():
    text = (BACKEND_DIR / "routers" / "whatsapp_webhook.py").read_text(encoding="utf-8")
    notify_block = text[
        text.index("# First-contact email before unsubscribe return"):
        text.index("if _unsub_short_circuit:")
    ]
    assert "if not _hist_skip_live:" in notify_block
    assert "maybe_notify_first_customer" in notify_block


def test_history_inbound_does_not_track_conversation():
    text = (BACKEND_DIR / "routers" / "whatsapp_webhook.py").read_text(encoding="utf-8")
    after_unsub = text.split("if _unsub_short_circuit:", 1)[1]
    after_unsub = after_unsub.split("# Emit automation event", 1)[0]
    assert "track_conversation(" in after_unsub
    assert "if not _hist_skip_live:" in after_unsub.split("track_conversation(")[0]


def test_ai_outbound_does_not_send_first_contact_email():
    text = (BACKEND_DIR / "routers" / "whatsapp_webhook.py").read_text(encoding="utf-8")
    assert text.count("maybe_notify_first_customer(") == 1


def test_footer_is_compact():
    html = _render("first_whatsapp_message", {
        "merchant_name": "نورة عبدالله",
        "customer_name": "أحمد سالم",
        "customer_phone": "+966542980511",
        "message_preview": "السلام عليكم",
        "conversation_url": "https://app.nahlah.ai/conversations",
    })
    assert "نحلة AI · سجل تجاري 7050202485 · المملكة العربية السعودية" in html
    assert "support@nahlah.ai" in html
    assert "nahlah.ai" in html
    assert "المركز السعودي للأعمال" not in html
    assert "موثّق لدى وزارة التجارة" not in html
    assert "logo-moc.png" not in html
    assert "logo-sbc.png" not in html


def test_long_email_does_not_overflow_layout():
    html = _render("first_whatsapp_message", {
        "merchant_name": LONG_EMAIL,
        "customer_name": LONG_EMAIL,
        "customer_phone": "+966542980511",
        "message_preview": "مرحبا",
        "conversation_url": "https://app.nahlah.ai/conversations",
    })
    assert LONG_EMAIL in html
    assert "overflow-wrap:anywhere" in html
    assert "word-break:break-word" in html
    assert "table-layout:fixed" in html
    assert "عميل جديد بدأ محادثة عبر واتساب" in html
    assert "عرض المحادثة" in html
    assert "دخل تلقائيًا في حملة" not in html
    customer_cell = html[html.index("العميل"): html.index("الهاتف")]
    assert "font-weight:600" in customer_cell
    assert "font-size:15px" in customer_cell
