"""Meta WhatsApp Business App Coexistence — unit tests (no live Graph)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from core.webhook_guardian import _is_meta_graph_compatible, _subscribed_fields_for  # noqa: E402
from services.meta_coexistence import (  # noqa: E402
    COEXISTENCE_WEBHOOK_FIELDS,
    apply_smb_sync_results,
    coexistence_webhook_fields,
    initiate_smb_app_data,
    is_coexistence_mode,
    maybe_fail_sync_deadline,
    reject_coexistence_finish_event,
    smb_syncs_accepted,
    verify_coexistence_phone,
)
from services.whatsapp_connection_service import (  # noqa: E402
    register_phone_number,
    subscribe_phone_webhook,
)


def test_reject_migration_and_wrong_finish_events():
    assert reject_coexistence_finish_event("FINISH_OBO_MIGRATION")
    assert reject_coexistence_finish_event("FINISH")
    assert reject_coexistence_finish_event(None) is None
    assert reject_coexistence_finish_event("") is None
    assert reject_coexistence_finish_event("FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING") is None


def test_coexistence_webhook_fields_include_required():
    fields = coexistence_webhook_fields()
    for required in ("messages", "history", "smb_app_state_sync", "smb_message_echoes", "account_update"):
        assert required in fields
    assert fields == COEXISTENCE_WEBHOOK_FIELDS


def test_is_coexistence_mode_uses_metadata_not_connection_type():
    conn = SimpleNamespace(extra_metadata={"connection_mode": "coexistence"}, connection_type="embedded")
    assert is_coexistence_mode(conn) is True
    dialog = SimpleNamespace(extra_metadata={}, connection_type="coexistence")
    assert is_coexistence_mode(dialog) is False


def test_guardian_uses_coexistence_fields_only_for_embedded_mode():
    fields = _subscribed_fields_for("embedded", {"connection_mode": "coexistence"})
    assert "history" in fields
    default = _subscribed_fields_for("embedded", {})
    assert "history" not in default
    skipped = _subscribed_fields_for("coexistence", {"connection_mode": "coexistence"})
    assert "history" not in skipped


def test_meta_graph_still_skipped_for_360dialog_coexistence_type():
    assert _is_meta_graph_compatible(provider="meta", connection_type="embedded") is True
    assert _is_meta_graph_compatible(provider="dialog360", connection_type="coexistence") is False
    assert _is_meta_graph_compatible(provider="meta", connection_type="coexistence") is False


def test_smb_syncs_accepted_requires_both_with_request_ids():
    assert smb_syncs_accepted({"smb_sync": {
        "smb_app_state_sync": {"accepted": True, "request_id": "a"},
        "history": {"accepted": True, "request_id": "b"},
    }}) is True
    assert smb_syncs_accepted({"smb_sync": {
        "smb_app_state_sync": {"accepted": True, "request_id": "a"},
        "history": {"accepted": True},
    }}) is False
    assert smb_syncs_accepted({"smb_sync": {
        "smb_app_state_sync": {"accepted": True, "request_id": "a"},
        "history": {"accepted": False, "request_id": "b"},
    }}) is False


def test_deadline_failure_only_when_configuring():
    from datetime import datetime, timedelta, timezone
    past = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    conn = SimpleNamespace(
        status="configuring",
        sending_enabled=True,
        last_error=None,
        extra_metadata={"connection_mode": "coexistence", "smb_sync_deadline_at": past, "smb_sync": {}},
    )
    assert maybe_fail_sync_deadline(conn) is True
    assert conn.status == "failed"
    assert conn.sending_enabled is False

    connected = SimpleNamespace(
        status="connected",
        sending_enabled=True,
        last_error=None,
        extra_metadata={"connection_mode": "coexistence", "smb_sync_deadline_at": past, "smb_sync": {}},
    )
    assert maybe_fail_sync_deadline(connected) is False
    assert connected.status == "connected"


@patch("services.whatsapp_connection_service.httpx.post")
def test_subscribe_sends_coexistence_fields(mock_post):
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {"success": True}
    ok, err = subscribe_phone_webhook(
        "111",
        "token",
        9,
        subscribed_fields=coexistence_webhook_fields(),
    )
    assert ok is True
    assert err is None
    sent = mock_post.call_args.kwargs["json"]["subscribed_fields"]
    assert "smb_message_echoes" in sent
    assert "history" in sent
    url = mock_post.call_args.args[0]
    assert "/111/subscribed_apps" in url


@patch("services.whatsapp_connection_service.httpx.post")
def test_standard_subscribe_prefers_waba_then_phone(mock_post):
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {"success": True}
    ok, err = subscribe_phone_webhook(
        "pn-generic-cloud-1",
        "token",
        9,
        waba_id="waba-generic-cloud-1",
        prefer_waba=True,
    )
    assert ok is True
    assert err is None
    url = mock_post.call_args.args[0]
    assert "/waba-generic-cloud-1/subscribed_apps" in url
    assert mock_post.call_count == 1


@patch("services.whatsapp_connection_service.httpx.post")
def test_default_subscribe_stays_phone_first(mock_post):
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {"success": True}
    ok, err = subscribe_phone_webhook(
        "pn-generic-cloud-1",
        "token",
        9,
        waba_id="waba-generic-cloud-1",
    )
    assert ok is True
    assert err is None
    url = mock_post.call_args.args[0]
    assert "/pn-generic-cloud-1/subscribed_apps" in url


@patch("services.whatsapp_connection_service.httpx.post")
def test_standard_subscribe_does_not_fallback_on_waba_forbidden(mock_post):
    mock_post.return_value.status_code = 403
    mock_post.return_value.json.return_value = {"error": {"message": "Permissions error"}}
    ok, err = subscribe_phone_webhook(
        "pn-generic-cloud-1",
        "token",
        9,
        waba_id="waba-generic-cloud-1",
        prefer_waba=True,
    )
    assert ok is False
    assert err
    assert mock_post.call_count == 1
    url = mock_post.call_args.args[0]
    assert "/waba-generic-cloud-1/subscribed_apps" in url


@patch("services.whatsapp_connection_service.httpx.post")
def test_standard_subscribe_does_not_fallback_on_ambiguous_waba_unsupported(mock_post):
    mock_post.return_value.status_code = 400
    mock_post.return_value.json.return_value = {
        "error": {
            "message": (
                "Unsupported post request. Object with ID 'waba-generic-cloud-1' "
                "does not exist, cannot be loaded due to missing permissions, "
                "or does not support this operation."
            )
        }
    }
    ok, err = subscribe_phone_webhook(
        "pn-generic-cloud-1",
        "token",
        9,
        waba_id="waba-generic-cloud-1",
        prefer_waba=True,
    )
    assert ok is False
    assert err
    assert mock_post.call_count == 1
    url = mock_post.call_args.args[0]
    assert "/waba-generic-cloud-1/subscribed_apps" in url


@patch("services.whatsapp_connection_service.httpx.post")
def test_standard_subscribe_requires_waba_id_when_prefer_waba(mock_post):
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {"success": True}
    ok, err = subscribe_phone_webhook(
        "pn-generic-cloud-1",
        "token",
        9,
        waba_id=None,
        prefer_waba=True,
    )
    assert ok is False
    assert err
    assert mock_post.call_count == 0


@patch("services.whatsapp_connection_service.httpx.post")
def test_register_phone_number_still_callable_for_default_path(mock_post):
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {"success": True}
    ok, err = register_phone_number("111", "token", 9)
    assert ok is True
    assert err is None
    assert "/register" in mock_post.call_args.args[0]


def test_coexistence_mode_skips_register_gate():
    conn = SimpleNamespace(extra_metadata={"connection_mode": "coexistence"})
    skip_phone_register = False
    assert (skip_phone_register or is_coexistence_mode(conn)) is True
    default = SimpleNamespace(extra_metadata={})
    assert (skip_phone_register or is_coexistence_mode(default)) is False


@patch("services.meta_coexistence.httpx.post")
def test_smb_app_data_requires_request_id(mock_post):
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {"messaging_product": "whatsapp"}
    results = initiate_smb_app_data("111", "token", 9)
    assert mock_post.call_count == 2
    assert results["smb_app_state_sync"]["accepted"] is False
    assert results["history"]["accepted"] is False
    conn = SimpleNamespace(extra_metadata={"connection_mode": "coexistence"})
    apply_smb_sync_results(conn, results)
    assert smb_syncs_accepted(conn.extra_metadata) is False


@patch("services.meta_coexistence.httpx.post")
def test_smb_app_data_posts_both_sync_types(mock_post):
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {
        "request_id": "req-1",
        "messaging_product": "whatsapp",
    }
    results = initiate_smb_app_data("111", "token", 9)
    assert mock_post.call_count == 2
    types = [call.kwargs["json"]["sync_type"] for call in mock_post.call_args_list]
    assert types == ["smb_app_state_sync", "history"]
    conn = SimpleNamespace(extra_metadata={"connection_mode": "coexistence"})
    apply_smb_sync_results(conn, results)
    assert smb_syncs_accepted(conn.extra_metadata) is True
    for call in mock_post.call_args_list:
        assert "token" not in str(call.kwargs.get("json"))


@patch("services.meta_coexistence.httpx.get")
def test_ineligible_phone_requires_biz_app_and_cloud_api(mock_get):
    mock_get.return_value.json.return_value = {
        "id": "111",
        "is_on_biz_app": False,
        "platform_type": "CLOUD_API",
    }
    ok, data, err = verify_coexistence_phone("111", "token", 9)
    assert ok is False
    assert err
    assert data.get("is_on_biz_app") is False

    mock_get.return_value.json.return_value = {
        "id": "111",
        "is_on_biz_app": True,
        "platform_type": "CLOUD_API",
    }
    ok, _data, err = verify_coexistence_phone("111", "token", 9)
    assert ok is True
    assert err is None


def test_history_ingest_does_not_dispatch(monkeypatch):
    from routers.whatsapp_webhook import _ingest_coexistence_history  # noqa: PLC0415

    added = []

    class FakeQuery:
        def filter(self, *a, **k):
            return self

        def order_by(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def all(self):
            return []

        def first(self):
            return None

    db = SimpleNamespace(add=lambda row: added.append(row), query=lambda *a, **k: FakeQuery())
    convo = SimpleNamespace(id=3)
    monkeypatch.setattr(
        "routers.conversations._get_or_create_conversation",
        lambda *a, **k: convo,
    )
    dispatched = {"called": False}
    monkeypatch.setattr(
        "routers.whatsapp_webhook._dispatch_message",
        lambda *a, **k: dispatched.__setitem__("called", True),
    )
    conn = SimpleNamespace(
        tenant_id=4,
        phone_number_id="pnid",
        extra_metadata={"connection_mode": "coexistence"},
    )
    _ingest_coexistence_history(db, conn, {
        "history": [{
            "threads": [{
                "id": "966500000000",
                "messages": [{"id": "wamid.1", "from": "966500000000", "type": "text", "text": {"body": "hi"}}],
            }],
        }],
    })
    assert added
    assert dispatched["called"] is False
    assert added[0].event_type == "coexistence_history"
    assert added[0].extra_metadata["historical_only"] is True


def test_history_share_declined_is_not_connection_failure(monkeypatch):
    from routers.whatsapp_webhook import _ingest_coexistence_history  # noqa: PLC0415

    monkeypatch.setattr(
        "routers.conversations._get_or_create_conversation",
        lambda *a, **k: SimpleNamespace(id=1),
    )
    conn = SimpleNamespace(
        tenant_id=4,
        status="configuring",
        sending_enabled=False,
        phone_number_id="pnid",
        extra_metadata={"connection_mode": "coexistence"},
    )
    db = SimpleNamespace(add=lambda row: None, query=lambda *a, **k: None)
    _ingest_coexistence_history(db, conn, {
        "history": [{"errors": [{"code": 2593109, "message": "user declined"}]}],
    })
    assert conn.status == "configuring"
    assert conn.extra_metadata.get("history_share_declined") is True


def test_partner_removed_disconnects_without_forcing_mode(monkeypatch):
    import asyncio
    from routers.whatsapp_webhook import _handle_meta_coexistence_change  # noqa: PLC0415

    conn = SimpleNamespace(
        tenant_id=1,
        phone_number_id="pnid",
        whatsapp_business_account_id="waba",
        status="connected",
        sending_enabled=True,
        last_error=None,
        provider="meta",
        connection_type="embedded",
        extra_metadata={"connection_mode": "coexistence"},
    )

    class Q:
        def filter(self, *a, **k):
            return self

        def first(self):
            return conn

    db = SimpleNamespace(
        query=lambda *a: Q(),
        commit=lambda: None,
        close=lambda: None,
        rollback=lambda: None,
    )
    monkeypatch.setattr("routers.whatsapp_webhook.get_db", lambda: iter([db]))
    asyncio.run(_handle_meta_coexistence_change(
        field="account_update",
        value={"event": "PARTNER_REMOVED"},
        phone_number_id="pnid",
        waba_id="waba",
    ))
    assert conn.status == "disconnected"
    assert conn.sending_enabled is False
    assert conn.extra_metadata.get("failure_code") == "partner_removed"
    assert conn.extra_metadata.get("connection_mode") == "coexistence"


def test_partner_removed_ignores_non_embedded_rows(monkeypatch):
    import asyncio
    from routers.whatsapp_webhook import _handle_meta_coexistence_change  # noqa: PLC0415

    conn = SimpleNamespace(
        tenant_id=1,
        phone_number_id="pnid",
        whatsapp_business_account_id="waba",
        status="connected",
        sending_enabled=True,
        last_error=None,
        provider="meta",
        connection_type="direct",
        extra_metadata={},
    )

    class Q:
        def filter(self, *a, **k):
            return self

        def first(self):
            return conn

    db = SimpleNamespace(
        query=lambda *a: Q(),
        commit=lambda: None,
        close=lambda: None,
        rollback=lambda: None,
    )
    monkeypatch.setattr("routers.whatsapp_webhook.get_db", lambda: iter([db]))
    asyncio.run(_handle_meta_coexistence_change(
        field="account_update",
        value={"event": "PARTNER_REMOVED"},
        phone_number_id="pnid",
        waba_id="waba",
    ))
    assert conn.status == "connected"
    assert conn.sending_enabled is True


def test_partner_removed_ignores_embedded_without_coexistence_mode(monkeypatch):
    import asyncio
    from routers.whatsapp_webhook import _handle_meta_coexistence_change  # noqa: PLC0415

    conn = SimpleNamespace(
        tenant_id=1,
        phone_number_id="pnid",
        whatsapp_business_account_id="waba",
        status="connected",
        sending_enabled=True,
        last_error=None,
        provider="meta",
        connection_type="embedded",
        extra_metadata={},
    )

    class Q:
        def filter(self, *a, **k):
            return self

        def first(self):
            return conn

    db = SimpleNamespace(
        query=lambda *a: Q(),
        commit=lambda: None,
        close=lambda: None,
        rollback=lambda: None,
    )
    monkeypatch.setattr("routers.whatsapp_webhook.get_db", lambda: iter([db]))
    asyncio.run(_handle_meta_coexistence_change(
        field="account_update",
        value={"event": "PARTNER_REMOVED"},
        phone_number_id="pnid",
        waba_id="waba",
    ))
    assert conn.status == "connected"
    assert conn.sending_enabled is True


def test_partner_removed_ignores_invalid_provider(monkeypatch):
    import asyncio
    from routers.whatsapp_webhook import _handle_meta_coexistence_change  # noqa: PLC0415

    conn = SimpleNamespace(
        tenant_id=1,
        phone_number_id="pnid",
        whatsapp_business_account_id="waba",
        status="connected",
        sending_enabled=True,
        last_error=None,
        provider="dialog360",
        connection_type="embedded",
        extra_metadata={"connection_mode": "coexistence"},
    )

    class Q:
        def filter(self, *a, **k):
            return self

        def first(self):
            return conn

    db = SimpleNamespace(
        query=lambda *a: Q(),
        commit=lambda: None,
        close=lambda: None,
        rollback=lambda: None,
    )
    monkeypatch.setattr("routers.whatsapp_webhook.get_db", lambda: iter([db]))
    asyncio.run(_handle_meta_coexistence_change(
        field="account_update",
        value={"event": "PARTNER_REMOVED"},
        phone_number_id="pnid",
        waba_id="waba",
    ))
    assert conn.status == "connected"
    assert conn.sending_enabled is True


def test_legacy_smb_accepted_without_request_ids_is_not_complete():
    assert smb_syncs_accepted({"smb_sync": {
        "smb_app_state_sync": {"accepted": True},
        "history": {"accepted": True},
    }}) is False


def test_coexistence_paths_do_not_log_token_material():
    repo = Path(__file__).resolve().parents[1]
    targets = [
        repo / "backend" / "services" / "meta_coexistence.py",
        repo / "backend" / "routers" / "whatsapp_embedded.py",
        repo / "backend" / "services" / "whatsapp_connection_service.py",
        repo / "backend" / "core" / "webhook_guardian.py",
        repo / "backend" / "routers" / "whatsapp_webhook.py",
        repo / "backend" / "routers" / "whatsapp_connect.py",
    ]
    forbidden = (
        "token_tail",
        "token[-",
        "access_token[-",
        "ctx.token[-",
        "safe_token_tail",
    )
    for path in targets:
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, f"{path.name} contains forbidden token material {needle!r}"


def test_in_progress_phone_and_waba_claims_return_conflict():
    from sqlalchemy import JSON, create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.orm import sessionmaker

    from core.tenant_integrity import (
        TenantIntegrityError,
        assert_phone_id_not_claimed,
        assert_waba_id_not_claimed,
    )
    from models import Base, Tenant, WhatsAppConnection

    engine = create_engine("sqlite:///:memory:")
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig_type in saved:
        col.type = orig_type
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Tenant(id=21, name="Owner", is_active=True))
        db.add(Tenant(id=22, name="Claimant", is_active=True))
        db.commit()
        for status in ("authorizing", "configuring", "connected"):
            db.query(WhatsAppConnection).delete()
            db.commit()
            db.add(WhatsAppConnection(
                tenant_id=21,
                phone_number_id=f"PHONE-{status}",
                whatsapp_business_account_id=f"WABA-{status}",
                provider="meta",
                connection_type="embedded",
                status=status,
                extra_metadata={"connection_mode": "coexistence"},
            ))
            db.commit()
            try:
                assert_phone_id_not_claimed(db, f"PHONE-{status}", 22)
                raise AssertionError(f"phone claim should 409 for status={status}")
            except TenantIntegrityError:
                pass
            try:
                assert_waba_id_not_claimed(db, f"WABA-{status}", 22)
                raise AssertionError(f"waba claim should 409 for status={status}")
            except TenantIntegrityError:
                pass
            assert_phone_id_not_claimed(db, f"PHONE-{status}", 21)
            assert_waba_id_not_claimed(db, f"WABA-{status}", 21)
    finally:
        db.close()
        engine.dispose()
