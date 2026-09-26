"""An accepted order template appears in the merchant inbox with its image."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "backend", ROOT / "database"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from core.order_update_timeline import persist_accepted_lifecycle_send  # noqa: E402
from models import Customer, MessageEvent  # noqa: E402


def _db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    saved = []
    for model in (Customer, MessageEvent):
        table = model.__table__
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
        table.create(engine)
    for col, original in saved:
        col.type = original
    return sessionmaker(bind=engine)()


def test_accepted_template_image_and_amount_are_persisted_immediately():
    db = _db()
    definition = SimpleNamespace(
        name="generic_order_confirmation_ar",
        service_key="order_confirmation",
        language="ar",
        category="UTILITY",
        components=[
            {"type": "HEADER", "format": "IMAGE"},
            {"type": "BODY", "text": "Order {{1}} total {{2}}"},
        ],
    )
    wire = {
        "type": "template",
        "template": {
            "name": definition.name,
            "language": {"code": "ar"},
            "components": [
                {"type": "header", "parameters": [
                    {"type": "image", "image": {"link": "https://cdn.example.org/order.jpg"}},
                ]},
                {"type": "body", "parameters": [
                    {"type": "text", "text": "ORD-8801"},
                    {"type": "text", "text": "249.00"},
                ]},
            ],
        },
    }
    with patch("core.message_presentation._template_definition", return_value=definition), patch(
        "routers.conversations._get_or_create_conversation",
        return_value=SimpleNamespace(id=42),
    ):
        assert persist_accepted_lifecycle_send(
            db,
            tenant_id=20,
            order_id=501,
            phone="+966500111222",
            customer_name="أحمد سالم",
            service_key="order_confirmation",
            template_name=definition.name,
            send_method="approved_template",
            provider_message_id="wamid.order.1",
            wire_payload=wire,
        )

    # Separate read proves that the row was committed, rather than only staged.
    db.expire_all()
    rows = db.query(MessageEvent).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.tenant_id == 20
    assert row.conversation_id == 42
    assert row.direction == "outbound"
    assert row.body == "Order ORD-8801 total 249.00"
    meta = row.extra_metadata
    assert meta["wa_message_id"] == "wamid.order.1"
    assert meta["order_id"] == 501
    assert meta["provider_send"]["status"] == "sent"
    item = meta["response_bundle"]["presentations"][0]
    assert item["kind"] == "order_lifecycle"
    assert item["media"]["url"] == "https://cdn.example.org/order.jpg"
    assert item["body"] == row.body


def test_missing_wire_evidence_does_not_invent_an_inbox_message():
    db = _db()
    assert not persist_accepted_lifecycle_send(
        db,
        tenant_id=20,
        order_id=501,
        phone="+966500111222",
        customer_name="أحمد سالم",
        service_key="order_confirmation",
        template_name="generic_order_confirmation_ar",
        send_method="approved_template",
        provider_message_id="wamid.order.1",
        wire_payload=None,
    )
    assert db.query(MessageEvent).count() == 0
