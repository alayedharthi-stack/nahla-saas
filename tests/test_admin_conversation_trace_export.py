"""Authenticated export isolation and evidence semantics; no external egress."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Column, DateTime, Integer, JSON, String, create_engine
from sqlalchemy.orm import Session, declarative_base
from sqlalchemy.pool import StaticPool

Base = declarative_base()


class ExportConversation(Base):
    __tablename__ = "export_conversations"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer)
    extra_metadata = Column(JSON)


class ExportMessage(Base):
    __tablename__ = "export_messages"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer)
    conversation_id = Column(Integer)
    created_at = Column(DateTime)
    direction = Column(String)
    event_type = Column(String)
    body = Column(String)
    extra_metadata = Column(JSON)


@pytest.fixture
def export_client(monkeypatch):
    import models
    from core import auth
    from core.database import get_db
    from routers.admin_debug import router

    original_auth = auth.get_current_user
    actor = {"role": "platform_admin", "sub": "synthetic-admin"}
    monkeypatch.setattr(auth, "get_current_user", lambda *a, **kw: actor)
    monkeypatch.setattr(models, "Conversation", ExportConversation)
    monkeypatch.setattr(models, "MessageEvent", ExportMessage)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([
            ExportConversation(id=10, tenant_id=2, extra_metadata={"brain_state": {
                "stage": "exploring", "turn": 7, "last_question_asked": "Which product?",
                "credential": "must-not-export", "order_prep": {"delivery_address": "private-address"},
            }}),
            ExportConversation(id=11, tenant_id=2, extra_metadata={}),
            ExportConversation(id=12, tenant_id=3, extra_metadata={}),
        ])
        for i, (tenant, conversation, body) in enumerate([
            (2, 10, "Show the shirt"), (2, 10, "Shirt details"), (2, 10, "Perfume details"),
            (2, 11, "other conversation"), (3, 10, "wrong tenant row"),
        ], start=1):
            db.add(ExportMessage(id=i, tenant_id=tenant, conversation_id=conversation,
                created_at=datetime(2026, 1, 1) + timedelta(seconds=i), direction="outbound",
                event_type="whatsapp_message", body=body, extra_metadata={
                    "requested_model": "requested-model", "credentials": {"token": "must-not-export"},
                    "final_expression_owner": "llm", "wa_message_id": f"synthetic-{i}",
                }))
        db.commit()
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as client:
            yield client, db, actor, original_auth
    engine.dispose()


PATH = "/admin/debug/conversation-trace-export?tenant_id=2&conversation_id=10"


def test_export_is_scoped_chronological_and_does_not_guess_model(export_client):
    client, db, _, _ = export_client
    before = deepcopy(db.get(ExportConversation, 10).extra_metadata)
    response = client.get(PATH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert [row["message_event_id"] for row in data["messages"]] == [1, 2, 3]
    assert all(row["provenance"]["actual_model"] is None for row in data["messages"])
    assert data["current_state"]["is_historical_turn_snapshot"] is False
    assert data["limitations"]["raw_model_requests_included"] is False
    assert "must-not-export" not in response.text
    assert "private-address" not in response.text
    assert db.get(ExportConversation, 10).extra_metadata == before
    assert not db.dirty and not db.new


def test_limit_reports_older_rows_omitted(export_client):
    client, *_ = export_client
    data = client.get(PATH + "&limit=2").json()
    assert data["has_more_messages"] is True
    assert [row["message_event_id"] for row in data["messages"]] == [2, 3]


@pytest.mark.parametrize("query", ["tenant_id=3&conversation_id=10", "tenant_id=2&conversation_id=999"])
def test_missing_or_wrong_tenant_conversation_is_not_exported(export_client, query):
    client, *_ = export_client
    assert client.get("/admin/debug/conversation-trace-export?" + query).status_code == 404


def test_merchant_role_is_denied_by_admin_dependency(export_client):
    client, _, actor, _ = export_client
    actor["role"] = "merchant_admin"
    assert client.get(PATH).status_code == 403


def test_unauthenticated_request_is_denied(export_client, monkeypatch):
    from core import auth
    client, _, _, original_auth = export_client
    monkeypatch.setattr(auth, "get_current_user", original_auth)
    assert client.get(PATH).status_code == 401


def test_support_session_cannot_export_another_tenant(export_client, monkeypatch):
    from core import auth
    client, _, actor, _ = export_client
    actor.update(role="support_impersonation", impersonation=True, tenant_id=3, actor_user_id=42)
    monkeypatch.setattr(auth, "_actor_is_still_platform_admin", lambda _: True)
    assert client.get(PATH).status_code == 403


@pytest.mark.parametrize("limit", [0, 201])
def test_message_limit_is_bounded(export_client, limit):
    client, *_ = export_client
    assert client.get(PATH + f"&limit={limit}").status_code == 422
