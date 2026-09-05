"""Salla webhook persist identity — replay vs later distinct transitions."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.salla_webhook_identity import build_salla_webhook_external_event_id  # noqa: E402


def _payload(event: str, order_id: int, status: str, payment: str, updated_at: str, **extra):
    body = {
        "event": event,
        "merchant": "22825873",
        "data": {
            "id": order_id,
            "status": {"slug": status, "name": status},
            "payment": {"method": "cod", "status": payment},
            "updated_at": updated_at,
        },
    }
    body.update(extra)
    return body


class TestSallaWebhookIdentity:
    def test_exact_replay_same_id(self):
        payload = _payload(
            "order.status.updated",
            566146469,
            "shipped",
            "paid",
            "2026-09-05T10:00:00Z",
        )
        first = build_salla_webhook_external_event_id(
            event_type="order.status.updated", parsed_payload=payload
        )
        second = build_salla_webhook_external_event_id(
            event_type="order.status.updated", parsed_payload=payload
        )
        assert first == second
        assert first is not None

    def test_later_status_transition_is_distinct(self):
        shipped = _payload(
            "order.status.updated",
            566146469,
            "shipped",
            "paid",
            "2026-09-05T10:00:00Z",
        )
        delivered = _payload(
            "order.status.updated",
            566146469,
            "delivered",
            "paid",
            "2026-09-05T18:00:00Z",
        )
        a = build_salla_webhook_external_event_id(
            event_type="order.status.updated", parsed_payload=shipped
        )
        b = build_salla_webhook_external_event_id(
            event_type="order.status.updated", parsed_payload=delivered
        )
        assert a != b

    def test_later_payment_transition_is_distinct(self):
        pending = _payload(
            "order.payment.updated",
            566146469,
            "under_review",
            "pending",
            "2026-09-05T10:00:00Z",
        )
        paid = _payload(
            "order.payment.updated",
            566146469,
            "under_review",
            "paid",
            "2026-09-05T10:05:00Z",
        )
        a = build_salla_webhook_external_event_id(
            event_type="order.payment.updated", parsed_payload=pending
        )
        b = build_salla_webhook_external_event_id(
            event_type="order.payment.updated", parsed_payload=paid
        )
        assert a != b

    def test_provider_event_id_wins_and_namespaces_oauth(self):
        payload = _payload(
            "order.created",
            1,
            "new",
            "pending",
            "2026-09-05T10:00:00Z",
            event_id="salla-delivery-99",
        )
        comm = build_salla_webhook_external_event_id(
            event_type="order.created", parsed_payload=payload, provider_prefix="salla"
        )
        oauth = build_salla_webhook_external_event_id(
            event_type="order.created", parsed_payload=payload, provider_prefix="salla_oauth"
        )
        assert comm == "salla:order.created:salla-delivery-99"
        assert oauth == "salla_oauth:order.created:salla-delivery-99"
        assert comm != oauth
