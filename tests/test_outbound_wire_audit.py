"""Actual provider identity and payload evidence, isolated from real egress."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from sqlalchemy import Column, Integer, String, JSON, create_engine
from sqlalchemy.orm import declarative_base, Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.outbound_wire_audit import (
    bind_wire_audit, reset_wire_audit, record_wire_attempt,
    observe_wire_payload, set_wire_expression, wire_row_id, wire_transcript_text,
)

Base = declarative_base()


class AuditRow(Base):
    __tablename__ = "audit_rows"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer)
    direction = Column(String)
    body = Column(String)
    extra_metadata = Column(JSON)


@pytest.fixture
def db(monkeypatch):
    import models
    monkeypatch.setattr(models, "MessageEvent", AuditRow)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            AuditRow(id=1, tenant_id=1, direction="outbound", body="Shirt XL",
                     extra_metadata={"provider_send": {"status": "sent"}}),
            AuditRow(id=2, tenant_id=1, direction="outbound", body="newer turn",
                     extra_metadata={"provider_send": {"status": "queued"}}),
            AuditRow(id=3, tenant_id=2, direction="outbound", body="other tenant", extra_metadata={}),
        ])
        session.commit()
        yield session
    engine.dispose()


PHONE = "966500000001"
META = {"actual_model": "provider-model", "requested_model": "requested-model",
        "final_expression_owner": "llm", "final_customer_text_source": "llm",
        "final_text_transformed": False, "final_transform_reasons": []}


def payload(body, kind="text"):
    if kind == "text":
        return {"to": PHONE, "type": "text", "text": {"body": body}}
    return {"to": PHONE, "type": "interactive", "interactive": {
        "type": "cta_url", "body": {"text": body},
        "action": {"name": "cta_url", "parameters": {"url": "https://example.test/shirt"}},
    }}


def test_sent_row_is_updated_by_id_and_newer_turn_is_untouched(db):
    token = bind_wire_audit(db, 1, 1, PHONE, "Shirt XL https://example.test/shirt", META)
    try:
        p = payload("Shirt XL", "interactive")
        observe_wire_payload(1, p, "whatsapp_payload_assembly:cta_url")
        record_wire_attempt(tenant_id=1, payload=p, operation="send_message", classification="ok", wamid="w1")
        row = db.get(AuditRow, 1)
        assert row.body == "Shirt XL"
        assert row.extra_metadata["final_expression_owner"] == "whatsapp_payload_assembly:cta_url"
        assert row.extra_metadata["wire_attempts"][0]["text_fields"] == {"interactive.body.text": "Shirt XL"}
        assert db.get(AuditRow, 2).body == "newer turn"
        assert "wire_attempts" not in db.get(AuditRow, 2).extra_metadata
    finally:
        reset_wire_audit(token)


def test_split_attempts_and_native_fallback_have_separate_provenance(db):
    token = bind_wire_audit(db, 1, 1, PHONE, "Shirt XL", META)
    try:
        record_wire_attempt(tenant_id=1, payload=payload("Shirt XL", "interactive"),
                            operation="catalog", classification="provider_error_field")
        set_wire_expression(1, PHONE, "fallback evidence", "native_catalog_failure_fallback", source="deterministic")
        record_wire_attempt(tenant_id=1, payload=payload("fallback evidence"), operation="fallback", classification="ok", wamid="w2")
        record_wire_attempt(tenant_id=1, payload=payload("second part", "interactive"), operation="split", classification="ok", wamid="w3")
        attempts = db.get(AuditRow, 1).extra_metadata["wire_attempts"]
        assert [a["sequence"] for a in attempts] == [1, 2, 3]
        assert attempts[0]["provenance"]["actual_model"] == "provider-model"
        assert attempts[1]["provenance"]["actual_model"] is None
        assert attempts[1]["provenance"]["final_customer_text_source"] == "deterministic"
        assert attempts[1]["provenance"]["final_expression_owner"] == "native_catalog_failure_fallback"
        assert attempts[2]["text_fields"]["interactive.body.text"] == "second part"
        assert db.get(AuditRow, 1).body == "fallback evidence\nsecond part"
    finally:
        reset_wire_audit(token)


@pytest.mark.parametrize("caption", ["جاكيت", "حذاء رياضي أبيض"])
def test_successful_text_survives_followup_card_and_failed_retry(db, caption):
    text = "model response preceding the product card"
    token = bind_wire_audit(db, 1, 1, PHONE, text, META)
    try:
        record_wire_attempt(tenant_id=1, payload=payload(text), operation="send_message",
                            classification="ok", wamid="text-accepted")
        record_wire_attempt(tenant_id=1, payload=payload(caption, "interactive"), operation="card",
                            classification="ok", wamid="card-accepted")
        record_wire_attempt(tenant_id=1, payload=payload("unsent attempt"), operation="retry",
                            classification="exception")
        assert db.get(AuditRow, 1).body == text + "\n" + caption
    finally:
        reset_wire_audit(token)


def test_wire_transcript_uses_provider_identity_for_duplicate_audit_records():
    accepted = {"classification": "ok", "wamid": "first-send",
                "text_fields": {"text.body": "sent text"}}
    assert wire_transcript_text([
        {"classification": "exception", "text_fields": {"text.body": "unsent text"}},
        accepted, dict(accepted),
        {**accepted, "wamid": "distinct-send"},
        {"classification": "ok", "wamid": "image-send",
         "text_fields": {"image.caption": "قميص قطني أزرق"}},
    ]) == "sent text\nsent text\nقميص قطني أزرق"
    assert wire_transcript_text([]) is None
    assert wire_transcript_text(None) is None
    assert wire_transcript_text([{"classification": "exception"}]) == ""


def test_tenant_recipient_and_child_task_are_excluded(db):
    async def run():
        token = bind_wire_audit(db, 1, 1, PHONE, "Shirt XL", META)
        try:
            assert wire_row_id(2, PHONE) is None
            assert wire_row_id(1, "966500000002") is None
            async def child():
                assert wire_row_id(1, PHONE) is None
            await asyncio.create_task(child())
            assert wire_row_id(1, PHONE) == 1
        finally:
            reset_wire_audit(token)
        assert wire_row_id(1, PHONE) is None
    asyncio.run(run())


def test_actual_http_boundary_records_scrubbed_payload_and_exception(db):
    from services.whatsapp_platform import service
    async def run():
        token = bind_wire_audit(db, 1, 1, PHONE, "Shirt [DEBUG] XL", META)
        conn = SimpleNamespace(phone_number_id="P", id=1, connection_type="direct")
        ctx = SimpleNamespace(source="test")
        response = MagicMock(status_code=200, text="ok")
        response.json.return_value = {"messages": [{"id": "wire-id"}]}
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=SimpleNamespace(post=AsyncMock(return_value=response)))
        client.__aexit__ = AsyncMock(return_value=False)
        try:
            with patch.object(service, "_provider_url", return_value="https://example.test/messages"), \
                 patch.object(service, "_provider_headers", return_value={}), \
                 patch.object(service, "wa_provider", return_value="meta"), \
                 patch.object(service.httpx, "AsyncClient", return_value=client):
                p = service._scrub_outbound_payload(payload("Shirt [DEBUG] XL"))
                observe_wire_payload(1, p, "provider_marker_scrub")
                result = await service.provider_post_with_context(conn, ctx, tenant_id=1,
                    operation="send_message", path="P/messages", json=p)
                assert result["_nahla_wamid"] == "wire-id"
                client.__aenter__.return_value.post.side_effect = RuntimeError("test transport failure")
                with pytest.raises(RuntimeError):
                    await service.provider_post_with_context(conn, ctx, tenant_id=1,
                        operation="send_message_retry", path="P/messages", json=p)
            attempts = db.get(AuditRow, 1).extra_metadata["wire_attempts"]
            assert len(attempts) == 2
            assert "[DEBUG]" not in attempts[0]["text_fields"]["text.body"]
            assert attempts[0]["wamid"] == "wire-id"
            assert attempts[1]["classification"] == "exception"
        finally:
            reset_wire_audit(token)
    asyncio.run(run())


@pytest.mark.parametrize("reported", ["actual-revision", None])
def test_model_identity_comes_from_response_only(reported):
    from modules.ai.orchestrator.providers import openai_compatible_provider as provider
    response = MagicMock()
    response.json.return_value = {"model": reported, "choices": [{"message": {"content": "Shirt XL"}}]}
    client = MagicMock()
    client.__enter__.return_value.post.return_value = response
    with patch.object(provider, "_API_KEY", "test-only"), \
         patch.object(provider, "resolve_model_for_provider", return_value="requested-model"), \
         patch.object(provider, "emit_llm_cost_audit"), \
         patch.object(provider, "record_ai_usage_from_openai_compatible"), \
         patch("httpx.Client", return_value=client):
        result = provider.OpenAICompatibleProvider().call("test", "test")
    assert result["requested_model"] == "requested-model"
    assert result["attempted_model"] == "requested-model"
    assert result["actual_model"] == reported
    assert result["model_identity_source"] == ("provider_response" if reported else "unknown")


def test_noop_is_not_a_text_change_but_full_deletion_is():
    from core.outbound_text_policy import OutboundTextTracker
    tracker = OutboundTextTracker()
    tracker.record_mutation(layer="facts", op="noop", before="XL", after="XL", text_written=False)
    tracker.record_mutation(layer="scrub", op="strip", before="XL", after="", text_written=False)
    assert tracker.postprocess_mutations[0].text_changed is False
    assert tracker.postprocess_mutations[1].text_changed is True


@pytest.mark.parametrize("saved_id", [321, None])
def test_webhook_binds_persisted_id_and_resets_on_exit(saved_id):
    from backend.tests.test_trusted_context_shadow_wireup import (
        _merchant_handler_convo, _merchant_handler_db, _merchant_handler_patch_ctx,
    )
    from routers.whatsapp_webhook import _handle_merchant_message
    captured = []

    async def send(**kwargs):
        captured.append(wire_row_id(1, PHONE))
        return True

    async def run():
        with _merchant_handler_patch_ctx(convo=_merchant_handler_convo(),
                whatsapp_send_mock=send) as (brain, _state):
            brain.return_value.process = AsyncMock(return_value={
                "reply": "The blue shirt is available in XL.", "buttons": [],
                "handoff": False, "chosen_path": "llm", "compose_source": "llm",
                "llm_candidate_present": True, "final_text_transformed": False,
                "final_transform_reasons": [],
            })
            with patch("routers.whatsapp_webhook.StateManager.save_message", return_value=saved_id):
                await _handle_merchant_message(phone_id="P", to=PHONE, text="Show the shirt size",
                    tenant_id=1, db=_merchant_handler_db())
        assert wire_row_id(1, PHONE) is None
    asyncio.run(run())
    assert captured == [saved_id]


def test_retry_status_updates_exact_sent_row_and_missing_id_never_guesses(db):
    from core.outbound_send_status import stamp_outbound_send_status
    kwargs = dict(tenant_id=1, recipient=PHONE, classification="ok",
                  response_body={"messages": [{"id": "retry-id"}]},
                  wamid="retry-id", operation="send_message_retry")
    token = bind_wire_audit(db, 1, 1, PHONE, "Shirt XL", META)
    try:
        assert stamp_outbound_send_status(db, **kwargs) == 1
        assert db.get(AuditRow, 1).extra_metadata["provider_send"]["wamid"] == "retry-id"
        assert db.get(AuditRow, 2).extra_metadata["provider_send"]["status"] == "queued"
    finally:
        reset_wire_audit(token)
    token = bind_wire_audit(db, None, 1, PHONE, "Shirt XL", META)
    try:
        assert stamp_outbound_send_status(db, **kwargs) is None
        assert not db.in_nested_transaction()
        record_wire_attempt(tenant_id=1, payload=payload("Shirt XL"),
                            operation="send_message", classification="ok", wamid="w1")
        assert "wire_attempts" not in db.get(AuditRow, 2).extra_metadata
    finally:
        reset_wire_audit(token)


def test_late_sync_preserves_wire_body_and_provenance(db):
    from core.outbound_send_status import sync_outbound_body_to_final
    token = bind_wire_audit(db, 1, 1, PHONE, "Shirt [DEBUG] XL", META)
    try:
        observe_wire_payload(1, payload("Shirt XL"), "provider_marker_scrub")
        record_wire_attempt(tenant_id=1, payload=payload("Shirt XL"),
                            operation="send_message", classification="ok", wamid="w1")
        assert sync_outbound_body_to_final(db, tenant_id=1, recipient=PHONE,
            final_body="Shirt [DEBUG] XL", reason="late_sync",
            provenance_metadata=META) == 1
        row = db.get(AuditRow, 1)
        assert row.body == "Shirt XL"
        assert row.extra_metadata["final_expression_owner"] == "provider_marker_scrub"
        assert row.extra_metadata["final_text_transformed"] is True
    finally:
        reset_wire_audit(token)


def test_native_catalog_with_deferred_empty_body_keeps_persisted_binding():
    from backend.tests.test_trusted_context_shadow_wireup import (
        _merchant_handler_convo, _merchant_handler_db, _merchant_handler_patch_ctx,
    )
    from routers.whatsapp_webhook import _handle_merchant_message
    captured = []

    async def native_send(**kwargs):
        captured.append(wire_row_id(1, PHONE))
        return SimpleNamespace(success=True)

    async def run():
        with _merchant_handler_patch_ctx(convo=_merchant_handler_convo()) as (brain, _state):
            brain.return_value.process = AsyncMock(return_value={
                "reply": "Explore the clothing catalog", "buttons": [], "handoff": False,
                "native_catalog_entry": {"thumbnail_product_retailer_id": "shirt-xl"},
                "chosen_path": "llm", "compose_source": "llm", "llm_candidate_present": True,
            })
            with patch("core.native_catalog_fallback.defer_native_catalog_customer_reply", return_value=""), \
                 patch("routers.whatsapp_webhook.StateManager.save_message", return_value=321), \
                 patch("routers.whatsapp_webhook._try_send_native_catalog_entry", side_effect=native_send):
                await _handle_merchant_message(phone_id="P", to=PHONE, text="Show the catalog",
                    tenant_id=1, db=_merchant_handler_db())
        assert wire_row_id(1, PHONE) is None
    asyncio.run(run())
    assert captured == [321]
