"""Tenant/support-bound navigation for INTERNAL_E2E transcripts."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.auth import require_admin
from core.database import get_db
from database.models import (
    Base,
    Conversation,
    MessageEvent,
    Order,
    OrderShipment,
    Tenant,
)
from modules.ai.commerce_agent_v2.internal_e2e_identity import internal_e2e_metadata
from routers.conversations import router


SUPPORT_USER = {
    "role": "support_impersonation",
    "impersonation": True,
    "tenant_id": 1,
    "actor_user_id": 700,
}


@pytest.fixture()
def api() -> tuple[TestClient, FastAPI, Any, dict[str, int]]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    saved = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, JSONB):
                saved.append((column, column.type))
                column.type = JSON()
    Base.metadata.create_all(engine)
    for column, original in saved:
        column.type = original
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    db.add_all(
        [
            Tenant(id=1, name="Tenant 1", is_active=True),
            Tenant(id=2, name="Tenant 2", is_active=True),
        ]
    )
    db.flush()

    conversations: dict[str, Conversation] = {}
    for alias in "ABC":
        identity = f"internal_e2e:t1:customer:{alias.lower()}"
        conversation = Conversation(
            tenant_id=1,
            customer_id=None,
            external_id=identity,
            status="active",
            extra_metadata={
                **internal_e2e_metadata(1, alias),
                "fixture_version": "commerce_v2_internal_e2e_fixture_v1",
            },
        )
        db.add(conversation)
        db.flush()
        conversations[alias] = conversation
        db.add_all(
            [
                MessageEvent(
                    tenant_id=1,
                    conversation_id=conversation.id,
                    direction="internal_e2e_inbound",
                    body=f"inbound {alias}",
                    event_type="internal_e2e_customer_turn",
                    extra_metadata={
                        **internal_e2e_metadata(1, alias),
                        "internal_message_id": f"internal:{alias}:in",
                    },
                ),
                MessageEvent(
                    tenant_id=1,
                    conversation_id=conversation.id,
                    direction="internal_e2e_outbound",
                    body=f"outbound {alias}",
                    event_type="internal_e2e_commerce_reply",
                    extra_metadata={
                        **internal_e2e_metadata(1, alias),
                        "internal_message_id": f"internal:{alias}:out",
                        **(
                            {
                                "artifact": {
                                    "presentation_bundle": {
                                        "schema_version": "commerce_v2_presentation_bundle_v1",
                                        "channel": "internal_e2e",
                                        "dispatchable": False,
                                        "actions": [
                                            {"kind": "text", "payload": {"text": "outbound A"}},
                                            {
                                                "kind": "product",
                                                "payload": {
                                                    "id": 501,
                                                    "external_id": "synthetic-product-a",
                                                    "title": "Synthetic Product A",
                                                    "price": "19.00",
                                                    "currency": "SAR",
                                                    "file_url": "https://example.invalid/product-a.png",
                                                    "product_url": "https://example.invalid/product-a",
                                                },
                                            },
                                        ],
                                    }
                                }
                            }
                            if alias == "A"
                            else {}
                        ),
                    },
                ),
            ]
        )

    foreign = Conversation(
        tenant_id=2,
        customer_id=None,
        external_id="internal_e2e:t2:customer:a",
        status="active",
        extra_metadata=internal_e2e_metadata(2, "A"),
    )
    ordinary = Conversation(
        tenant_id=1,
        customer_id=None,
        external_id="ordinary-no-customer-row",
        status="active",
        extra_metadata={},
    )
    db.add_all([foreign, ordinary])
    db.flush()

    order = Order(
        tenant_id=1,
        customer_id=None,
        external_id="internal_e2e:t1:customer:c:order:001",
        external_order_number="IE2E-C-001",
        status="draft",
        total="249.00",
        customer_name="INTERNAL E2E CUSTOMER C",
        customer_info={"phone": "internal_e2e:t1:customer:c"},
        line_items=[{"name": "Synthetic item", "quantity": 2, "price": "124.50"}],
        source="internal_e2e",
        extra_metadata=internal_e2e_metadata(1, "C"),
    )
    db.add(order)
    db.flush()
    db.add(
        OrderShipment(
            tenant_id=1,
            order_id=order.id,
            provider="internal_e2e_carrier",
            status="in_transit",
            tracking_number="IE2E-TRACK-C-001",
            recipient_name="INTERNAL E2E CUSTOMER C",
            recipient_phone="internal_e2e:t1:customer:c",
            extra_metadata={
                **internal_e2e_metadata(1, "C"),
                "tracking_url": "https://example.invalid/internal-e2e/track/c/001",
            },
        )
    )
    db.commit()

    ids = {
        **{alias: int(row.id) for alias, row in conversations.items()},
        "foreign": int(foreign.id),
        "ordinary": int(ordinary.id),
    }
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_admin] = lambda: dict(SUPPORT_USER)
    client = TestClient(app)
    yield client, app, db, ids
    db.close()
    engine.dispose()


def test_synthetic_transcript_is_opened_only_by_canonical_conversation_id(
    api: tuple[TestClient, FastAPI, Any, dict[str, int]],
) -> None:
    client, _app, _db, ids = api
    response = client.get(f"/conversations/internal-e2e/{ids['A']}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["conversation"] == {
        "id": str(ids["A"]),
        "customer": "INTERNAL_E2E · Customer A",
        "phone": "",
        "lastMsg": "outbound A",
        "time": payload["conversation"]["time"],
        "isAI": True,
        "status": "active",
        "unread": 0,
        "lastMsgType": "ai",
        "customerId": None,
        "synthetic": True,
        "syntheticAlias": "A",
        "syntheticConversationId": ids["A"],
        "syntheticIdentifier": "INTERNAL_E2E · Customer A",
        "channelLabel": "INTERNAL_E2E",
        "readOnly": True,
    }
    assert [row["direction"] for row in payload["messages"]] == ["in", "out"]
    assert all(row["wamid"] is None for row in payload["messages"])
    presentations = payload["messages"][1]["responseBundle"]["presentations"]
    assert [item["kind"] for item in presentations] == ["text", "product"]
    assert presentations[1]["product"] == {
        "id": "501",
        "retailer_id": "synthetic-product-a",
        "name": "Synthetic Product A",
        "image_url": "https://example.invalid/product-a.png",
        "price": "19.00",
        "currency": "SAR",
        "availability": None,
        "url": "https://example.invalid/product-a",
    }
    assert "internal_e2e:t1:customer" not in response.text


@pytest.mark.parametrize("role", ["admin", "merchant", "merchant_admin"])
def test_plain_admin_or_merchant_session_cannot_inspect_synthetic_transcript(
    api: tuple[TestClient, FastAPI, Any, dict[str, int]],
    role: str,
) -> None:
    client, app, _db, ids = api
    app.dependency_overrides[require_admin] = lambda: {
        "role": role,
        "tenant_id": 1,
    }
    assert client.get(f"/conversations/internal-e2e/{ids['A']}").status_code == 403


def test_cross_tenant_and_unmarked_conversation_ids_fail_closed(
    api: tuple[TestClient, FastAPI, Any, dict[str, int]],
) -> None:
    client, _app, _db, ids = api
    assert client.get(f"/conversations/internal-e2e/{ids['foreign']}").status_code == 404
    assert client.get(f"/conversations/internal-e2e/{ids['ordinary']}").status_code == 404
    assert client.get("/conversations/internal-e2e/999999").status_code == 404


def test_lifecycle_or_missing_bearer_cannot_authorize_navigation(
    api: tuple[TestClient, FastAPI, Any, dict[str, int]],
) -> None:
    client, app, _db, ids = api
    app.dependency_overrides.pop(require_admin)
    assert client.get(f"/conversations/internal-e2e/{ids['A']}").status_code == 401
    response = client.get(
        f"/conversations/internal-e2e/{ids['A']}",
        headers={"Authorization": "Bearer lifecycle-m2m-only"},
    )
    assert response.status_code == 401


def test_customer_orders_are_isolated_by_synthetic_conversation_identity(
    api: tuple[TestClient, FastAPI, Any, dict[str, int]],
) -> None:
    client, _app, _db, ids = api
    for alias in ("A", "B"):
        response = client.get(
            f"/conversations/internal-e2e/{ids[alias]}/customer-orders"
        )
        assert response.status_code == 200
        assert response.json() == {"orders": [], "count": 0, "readOnly": True}

    response = client.get(
        f"/conversations/internal-e2e/{ids['C']}/customer-orders"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["readOnly"] is True
    assert payload["count"] == 1
    assert payload["orders"][0]["reference"] == "IE2E-C-001"
    assert payload["orders"][0]["shipment"]["trackingNumber"] == "IE2E-TRACK-C-001"
    assert "mutation" not in response.text.lower()


def test_message_marker_mismatch_blocks_entire_synthetic_page(
    api: tuple[TestClient, FastAPI, Any, dict[str, int]],
) -> None:
    client, _app, db, ids = api
    row = (
        db.query(MessageEvent)
        .filter(MessageEvent.conversation_id == ids["A"])
        .order_by(MessageEvent.id.asc())
        .first()
    )
    row.extra_metadata = internal_e2e_metadata(1, "B")
    db.commit()

    response = client.get(f"/conversations/internal-e2e/{ids['A']}")
    assert response.status_code == 409
    assert response.json()["detail"] == "synthetic_conversation_message_identity_invalid"
