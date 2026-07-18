"""PostgreSQL evidence fixture: injected rows are explicitly NOT_REAL_CHANNEL."""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.operators.real_channel_acceptance_session import (  # noqa: E402
    classify_inbound_candidate,
)
from scripts.operators.real_channel_conversational_acceptance_contract import (  # noqa: E402
    EVIDENCE_CHANNEL_ACTUAL_PROVIDER,
    EVIDENCE_CHANNEL_DIRECT_SIGNED_WEBHOOK,
    hmac_identifier,
)


def _pg_url() -> str:
    return (os.environ.get("A1_PG_TEST_DATABASE_URL") or "").strip()


@pytest.mark.skipif(not _pg_url(), reason="A1 PostgreSQL integration URL unavailable")
def test_pg_injected_live_webhook_shaped_row_cannot_pass_actual_channel() -> None:
    """Even a valid-looking persisted row remains an ingress probe candidate."""
    engine = create_engine(_pg_url(), pool_pre_ping=True)
    tenant_name = f"NOT_REAL_CHANNEL-{uuid.uuid4().hex}"
    phone = "15550001111"
    provider_id = "wamid.HBgLVALIDSHAPED123456789"
    tenant_id: int | None = None
    event_id: int | None = None
    try:
        with engine.begin() as conn:
            tenant_id = int(
                conn.execute(
                    text("INSERT INTO tenants(name,is_active) VALUES(:name,true) RETURNING id"),
                    {"name": tenant_name},
                ).scalar_one()
            )
            event_id = int(
                conn.execute(
                    text(
                        'INSERT INTO message_events(tenant_id,direction,body,event_type,created_at,metadata) '
                        "VALUES(:tenant_id,'inbound','NOT_REAL_CHANNEL','pg_fixture',NOW(),CAST(:metadata AS jsonb)) "
                        "RETURNING id"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "metadata": json.dumps(
                            {
                                "message_origin": "live_webhook",
                                "historical_import": False,
                                "wa_message_id": provider_id,
                                "phone": phone,
                                "acceptance_fixture": "NOT_REAL_CHANNEL",
                            }
                        ),
                    },
                ).scalar_one()
            )
        with engine.connect() as conn:
            row = dict(
                conn.execute(
                    text(
                        "SELECT id,direction,created_at,metadata "
                        "FROM message_events WHERE id=:event_id"
                    ),
                    {"event_id": event_id},
                ).mappings().one()
            )
        key = "pg-integration-evidence-key"
        result = classify_inbound_candidate(
            row,
            event_cursor=event_id - 1,
            started_at_utc=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
            expected_phone_hmac=hmac_identifier(phone, key=key),
            hmac_key=key,
        )
        assert result["eligible_provider_candidate"] is False
        assert "inbound_origin_rejected" in result["blockers"]
        assert result["evidence_channel"] == EVIDENCE_CHANNEL_DIRECT_SIGNED_WEBHOOK
        assert result["evidence_channel"] != EVIDENCE_CHANNEL_ACTUAL_PROVIDER
    finally:
        if tenant_id is not None:
            with engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM message_events WHERE tenant_id=:tenant_id"),
                    {"tenant_id": tenant_id},
                )
                conn.execute(
                    text("DELETE FROM tenants WHERE id=:tenant_id"),
                    {"tenant_id": tenant_id},
                )
        engine.dispose()
