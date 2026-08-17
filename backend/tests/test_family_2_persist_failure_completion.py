"""Family 2 addendum — persist-failure customer-turn completion.

Integration coverage above persist_checkout_location_patch. Asserts owners
and structured facts, not assistant wording.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_BACKEND, os.path.join(_REPO, "database"), _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.customer_shipping_address_writer import (  # noqa: E402
    persist_customer_shipping_address_if_confirmed,
)
from core.order_flow import persist_checkout_location_outcome  # noqa: E402
from core.order_shipping_snapshot import shipping_snapshot_confirmed  # noqa: E402
from models import Base, Customer, CustomerAddress, Tenant  # noqa: E402
from modules.ai.brain.execution.orders import _merge_message_details  # noqa: E402
from modules.ai.brain.types import OrderPreparationState  # noqa: E402
from core.wa_address_ingestion import resolve_address_state_patch  # noqa: E402
from modules.ai.brain.commerce.unstructured_turn_ownership import (  # noqa: E402
    ofv2_may_own_prebrain,
)
from modules.ai.media.customer_turn_completion import (  # noqa: E402
    AUDITED_CUSTOMER_ORIGIN_EARLY_RETURNS,
    COMPLETION_BRAIN,
    COMPLETION_ORPHAN,
    COMPLETION_STRUCTURED_AND_CONTINUATION,
    customer_authored_location_continue_text,
    resolve_checkout_location_persist_turn,
    should_continue_checkout_location_persist_failure,
)
from modules.ai.order_flow_v2.ingest import apply_inbound_slots  # noqa: E402

GENERIC_MAPS = "https://maps.google.com/?q=24.7136,46.6753"
GENERIC_CITY = "الرياض"
GENERIC_DISTRICT = "الورود"

BRAIN_NATURAL_REPLY = "brain_natural_reply"
STRUCTURED_ACTION_PLUS_REPLY = "structured_action_and_natural_continuation"


def _maps_patch() -> dict:
    return {
        "google_maps_url": GENERIC_MAPS,
        "delivery_address_status": "accepted",
        "delivery_address_type": "maps_url",
    }


class TestPersistFailureLocationCompletion:
    def test_valid_maps_persist_false_does_not_ack_or_orphan(self) -> None:
        plan = resolve_checkout_location_persist_turn(
            persist_ok=False,
            persist_reason="apply_state_patch_false",
            inbound_type="location",
            inbound_metadata={"location": {"latitude": 24.7136, "longitude": 46.6753}},
            inbound_text="",
            state_patch=_maps_patch(),
        )
        assert plan["emit_success_ack"] is False
        assert plan["call_brain"] is True
        assert plan["emit_success_ack"] is not plan["call_brain"]
        assert plan["location_received"] is True
        assert plan["location_persisted"] is False
        assert plan["location_saved"] is False
        assert plan["persistence_failure_reason"] == "apply_state_patch_false"
        assert plan["completion_class"] == COMPLETION_BRAIN
        assert plan["completion_class"] == BRAIN_NATURAL_REPLY
        assert plan["completion_class"] != COMPLETION_ORPHAN
        assert should_continue_checkout_location_persist_failure(
            plan["inbound_metadata"]
        ) is True
        ctc = (plan["inbound_metadata"] or {}).get("customer_turn_completion") or {}
        assert ctc["semantic_owner"] == "brain"
        assert ctc["state_persisted"] is False
        assert plan["brain_text"] == ""
        meta = plan["inbound_metadata"] or {}
        assert meta.get("checkout_location_ingest_blocked") is True
        assert meta.get("location_saved") is False
        assert "location" not in meta
        assert meta.get("state_patch") is None
        assert not meta.get("google_maps_url")
        assert meta.get("inbound_normalized_type") == "text"


class TestPersistFailureAddressTextCompletion:
    def test_address_text_persist_false_hands_off_to_brain(self) -> None:
        plan = resolve_checkout_location_persist_turn(
            persist_ok=False,
            persist_reason="empty_patch",
            inbound_type="address_text",
            inbound_metadata={},
            inbound_text=GENERIC_MAPS,
            state_patch=_maps_patch(),
        )
        assert plan["emit_success_ack"] is False
        assert plan["call_brain"] is True
        assert plan["location_saved"] is False
        assert plan["location_persisted"] is False
        assert plan["location_received"] is True
        assert plan["completion_class"] == BRAIN_NATURAL_REPLY
        assert plan["brain_text"] == ""
        assert plan["brain_text"] != GENERIC_MAPS
        meta = plan["inbound_metadata"] or {}
        assert meta.get("checkout_location_ingest_blocked") is True
        assert meta.get("location_saved") is False
        assert meta.get("state_patch") is None
        assert not meta.get("google_maps_url")


class TestPersistSuccessControl:
    def test_persist_true_keeps_structured_ack_without_brain(self) -> None:
        plan = resolve_checkout_location_persist_turn(
            persist_ok=True,
            persist_reason="persisted",
            inbound_type="location",
            inbound_metadata={"location": {"latitude": 24.7, "longitude": 46.6}},
            inbound_text="",
            state_patch=_maps_patch(),
        )
        assert plan["emit_success_ack"] is True
        assert plan["call_brain"] is False
        assert plan["emit_success_ack"] is not plan["call_brain"]
        assert plan["location_persisted"] is True
        assert plan["location_saved"] is True
        assert plan["completion_class"] == COMPLETION_STRUCTURED_AND_CONTINUATION
        assert plan["completion_class"] == STRUCTURED_ACTION_PLUS_REPLY
        meta = plan["inbound_metadata"] or {}
        assert meta.get("location_saved") is True
        assert meta.get("checkout_location_ingest_blocked") is not True
        assert (meta.get("location") or {}).get("latitude") == 24.7


class TestPersistFailureDoesNotIngestAsAccepted:
    def test_location_failure_is_not_ofv2_structural_location(self) -> None:
        plan = resolve_checkout_location_persist_turn(
            persist_ok=False,
            persist_reason="apply_state_patch_false",
            inbound_type="location",
            inbound_metadata={
                "normalized_type": "location",
                "type": "location",
                "location": {"latitude": 24.7136, "longitude": 46.6753},
                "text": GENERIC_MAPS,
            },
            inbound_text="",
            state_patch=_maps_patch(),
        )
        meta = plan["inbound_metadata"] or {}
        assert ofv2_may_own_prebrain(
            meta,
            normalized_type=str(meta.get("inbound_normalized_type") or ""),
            message=plan["brain_text"],
        ) is False
        assert apply_inbound_slots(
            message=plan["brain_text"],
            inbound_normalized_type=str(meta.get("inbound_normalized_type") or "text"),
            inbound_metadata=meta,
        ) == {}
        assert resolve_address_state_patch(
            inbound_normalized_type=str(meta.get("inbound_normalized_type") or "text"),
            inbound_metadata=meta,
            inbound_text=plan["brain_text"],
        ) is None

    def test_blocked_continue_text_does_not_restore_maps_url(self) -> None:
        plan = resolve_checkout_location_persist_turn(
            persist_ok=False,
            persist_reason="apply_state_patch_false",
            inbound_type="address_text",
            inbound_metadata={"text": GENERIC_MAPS},
            inbound_text=GENERIC_MAPS,
            state_patch=_maps_patch(),
        )
        restored = customer_authored_location_continue_text(
            plan["inbound_metadata"],
            GENERIC_MAPS,
        )
        assert restored == ""
        assert restored != GENERIC_MAPS


class TestPersistOutcomeReason:
    def test_empty_patch_is_not_saved(self) -> None:
        ok, reason = persist_checkout_location_outcome(
            MagicMock(),
            tenant_id=1,
            phone="966511111111",
            state_patch={},
        )
        assert ok is False
        assert reason == "empty_patch"

    def test_apply_false_reason_is_observable(self) -> None:
        import core.order_flow as order_flow

        with patch.object(order_flow, "apply_state_patch", return_value=False):
            ok, reason = order_flow.persist_checkout_location_outcome(
                MagicMock(),
                tenant_id=1,
                phone="966511111111",
                state_patch=_maps_patch(),
            )
        assert ok is False
        assert reason == "apply_state_patch_false"


class TestWebhookCompletionOwners:
    def test_location_persist_failure_calls_brain_not_silent_return(self) -> None:
        src = open(
            os.path.join(_BACKEND, "routers", "whatsapp_webhook.py"),
            encoding="utf-8",
        ).read()
        loc_idx = src.find("location ack skipped persist_failed")
        assert loc_idx > 0
        loc_chunk = src[loc_idx: loc_idx + 900]
        assert "_handle_merchant_message(" in loc_chunk
        assert "brain_text" in loc_chunk
        assert "resolve_checkout_location_persist_turn" in src

    def test_address_text_persist_failure_does_not_return_before_brain(self) -> None:
        src = open(
            os.path.join(_BACKEND, "routers", "whatsapp_webhook.py"),
            encoding="utf-8",
        ).read()
        addr_idx = src.find("address ack skipped persist_failed")
        assert addr_idx > 0
        addr_chunk = src[addr_idx: addr_idx + 700]
        assert "normalized_inbound.metadata" in addr_chunk
        assert "brain_text" in addr_chunk
        assert "\n                    return\n" not in addr_chunk.split("else:")[0]

    def test_audited_persist_failure_paths_are_brain_owned(self) -> None:
        rows = {
            row["path"]: row
            for row in AUDITED_CUSTOMER_ORIGIN_EARLY_RETURNS
        }
        loc = rows["whatsapp_webhook.location_persist_failure"]
        addr = rows["whatsapp_webhook.address_text_persist_failure"]
        assert loc["before"] == COMPLETION_ORPHAN
        assert loc["after"] == COMPLETION_BRAIN
        assert addr["after"] == COMPLETION_BRAIN
        assert loc["repaired"] is True
        assert addr["repaired"] is True


class TestCorrectionLifecycleEvidence:
    """Evidence only — city/district live on order_prep until confirmed write."""

    def test_current_turn_city_correction_stays_on_order_prep(self) -> None:
        prep = OrderPreparationState(city=GENERIC_CITY, district="العليا")
        _merge_message_details(
            prep,
            {"city": "جدة", "district": GENERIC_DISTRICT},
            "جدة الحي الورود",
        )
        assert prep.city == "جدة"
        assert prep.district == GENERIC_DISTRICT

    def test_customer_address_updates_only_when_shipping_confirmed(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        saved: list = []
        for table in Base.metadata.sorted_tables:
            for col in table.columns:
                if isinstance(col.type, JSONB):
                    saved.append((col, col.type))
                    col.type = JSON()
        Base.metadata.create_all(engine)
        for col, orig in saved:
            col.type = orig
        Session = sessionmaker(bind=engine)
        db = Session()
        tenant = Tenant(name="متجر تجريبي عام", is_active=True)
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        customer = Customer(
            tenant_id=tenant.id,
            phone="+966511111111",
            normalized_phone="966511111111",
            name="أحمد سالم",
        )
        db.add(customer)
        db.commit()
        db.refresh(customer)

        city_only = {"city": "جدة", "district": GENERIC_DISTRICT}
        confirmed, _reason = shipping_snapshot_confirmed(
            city_only,
            order_prep=city_only,
        )
        assert confirmed is False
        persisted, row = persist_customer_shipping_address_if_confirmed(
            db,
            tenant_id=tenant.id,
            customer_id=customer.id,
            order_id=None,
            snapshot=city_only,
            order_prep=city_only,
        )
        assert persisted is False
        assert row is None
        assert db.query(CustomerAddress).filter_by(customer_id=customer.id).count() == 0

        accepted = {
            "city": "جدة",
            "district": GENERIC_DISTRICT,
            "google_maps_url": GENERIC_MAPS,
            "accepted_delivery_address": True,
        }
        confirmed, reason = shipping_snapshot_confirmed(
            accepted,
            order_prep={
                "google_maps_url": GENERIC_MAPS,
                "delivery_address_status": "accepted",
            },
        )
        assert confirmed is True
        persisted, row = persist_customer_shipping_address_if_confirmed(
            db,
            tenant_id=tenant.id,
            customer_id=customer.id,
            order_id=None,
            snapshot=accepted,
            order_prep={
                "google_maps_url": GENERIC_MAPS,
                "delivery_address_status": "accepted",
            },
        )
        assert persisted is True
        assert row is not None
        assert row.city == "جدة"
        assert reason in {"google_maps_url", "whatsapp_location", "short_address_code"}
