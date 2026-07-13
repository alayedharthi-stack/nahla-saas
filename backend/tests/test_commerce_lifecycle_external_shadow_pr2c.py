"""
PR 2C — external-store lifecycle normalization + shadow producer tests.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.commerce_lifecycle.external_shadow_flags import (  # noqa: E402
    commerce_lifecycle_external_shadow_enabled,
)
from core.commerce_lifecycle.external_shadow_producer import (  # noqa: E402
    build_order_lifecycle_evidence,
    record_external_order_transition_shadow,
)
from core.commerce_lifecycle.intents import BusinessIntent  # noqa: E402
from core.commerce_lifecycle.ledger import ShadowLedgerOutcome  # noqa: E402
from models import CommerceLifecycleNotificationLedger  # noqa: E402
from store_adapters.salla_lifecycle import normalize_salla_lifecycle_business_intent  # noqa: E402
from store_integration.lifecycle_normalization import (  # noqa: E402
    build_transition_identity,
    normalize_external_lifecycle_intent,
    resolve_lifecycle_intent_normalizer,
)
from store_integration.registry import register_adapter  # noqa: E402


def _make_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    saved = []
    table = CommerceLifecycleNotificationLedger.__table__
    for col in table.columns:
        if isinstance(col.type, JSONB):
            saved.append((col, col.type))
            col.type = JSON()
    table.create(engine)
    for col, orig in saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _order_row(**kwargs):
    defaults = dict(
        id=42,
        external_id="ord-99",
        external_order_number="1001",
        status="shipped",
        checkout_url="https://shop.example/checkout",
        customer_name="أحمد",
        customer_info={"phone": "+966500000000"},
        extra_metadata={"tracking_number": "TRK1", "payment_method": "cod"},
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestSallaNormalization:
    def test_known_transition_maps_to_shipment_available(self):
        intent = normalize_salla_lifecycle_business_intent(
            "under_review", "shipped", {"payment_method": "cod"}
        )
        assert intent == BusinessIntent.SHIPMENT_AVAILABLE

    def test_new_confirmed_order_maps_order_confirmed(self):
        intent = normalize_salla_lifecycle_business_intent(
            None, "under_review", {"payment_method": "cod"}
        )
        assert intent == BusinessIntent.ORDER_CONFIRMED

    def test_first_observation_shipped_returns_none(self):
        assert normalize_salla_lifecycle_business_intent(None, "shipped", {}) is None

    def test_first_observation_delivered_returns_none(self):
        assert normalize_salla_lifecycle_business_intent(None, "delivered", {}) is None

    def test_repeated_status_returns_none(self):
        assert normalize_salla_lifecycle_business_intent("shipped", "shipped", {}) is None

    def test_unknown_status_returns_none(self):
        assert normalize_salla_lifecycle_business_intent("foo", "bar", {}) is None

    def test_same_status_returns_none(self):
        assert normalize_salla_lifecycle_business_intent("shipped", "shipped", {}) is None

    def test_provider_mapping_lives_in_adapter_not_core_branching(self):
        src = importlib.import_module("store_integration.lifecycle_normalization").__file__
        tree = ast.parse(Path(src).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "salla":
                pytest.fail("core lifecycle_normalization must not branch on provider slug")

    def test_generic_adapter_can_declare_mappings(self):
        @register_adapter("fake_pr2c")
        class _FakeAdapter:
            platform = "fake_pr2c"

            @staticmethod
            def normalize_lifecycle_business_intent(prev, curr, _order):
                if curr == "packed":
                    return BusinessIntent.ORDER_CONFIRMED
                return None

        normalizer = resolve_lifecycle_intent_normalizer("fake_pr2c")
        assert normalizer is not None
        assert normalizer(None, "packed", {}) == BusinessIntent.ORDER_CONFIRMED


class TestTransitionIdentity:
    def test_same_webhook_retry_same_identity(self):
        payload = {"event_id": "evt:pipe|id", "updated_at": "2026-07-13T10:00:00Z"}
        a = build_transition_identity(
            provider="salla",
            external_order_id="ord-1",
            raw_previous_status="under_review",
            raw_current_status="shipped",
            raw_payload=payload,
        )
        b = build_transition_identity(
            provider="salla",
            external_order_id="ord-1",
            raw_previous_status="under_review",
            raw_current_status="shipped",
            raw_payload=payload,
        )
        assert a == b

    def test_different_transition_different_identity(self):
        payload = {"event_id": "evt-1", "updated_at": "2026-07-13T10:00:00Z"}
        a = build_transition_identity(
            provider="salla",
            external_order_id="ord-1",
            raw_previous_status="under_review",
            raw_current_status="shipped",
            raw_payload=payload,
        )
        b = build_transition_identity(
            provider="salla",
            external_order_id="ord-1",
            raw_previous_status="shipped",
            raw_current_status="delivered",
            raw_payload=payload,
        )
        assert a != b

    def test_delimiter_provider_ids_do_not_collide(self):
        payload_a = {"updated_at": "2026-07-13T10:00:00Z"}
        payload_b = {"updated_at": "2026-07-13T10:00:00Z"}
        id_a = build_transition_identity(
            provider="salla",
            external_order_id="1|2",
            raw_previous_status="a",
            raw_current_status="b",
            raw_payload=payload_a,
        )
        id_b = build_transition_identity(
            provider="salla",
            external_order_id="12",
            raw_previous_status="a",
            raw_current_status="b",
            raw_payload=payload_b,
        )
        assert id_a != id_b

    def test_missing_provider_event_id_uses_deterministic_fallback(self):
        source_id, _version = build_transition_identity(
            provider="salla",
            external_order_id="ord-77",
            raw_previous_status="pending",
            raw_current_status="shipped",
            raw_payload={"updated_at": "2026-07-13T11:00:00Z"},
        )
        assert source_id.startswith("ext:")

    def test_no_random_identity_for_same_factual_event(self):
        kwargs = dict(
            provider="salla",
            external_order_id="ord-5",
            raw_previous_status="pending",
            raw_current_status="delivered",
            raw_payload={"updated_at": "2026-07-13T12:00:00Z"},
        )
        first = build_transition_identity(**kwargs)
        second = build_transition_identity(**kwargs)
        assert first == second


class TestShadowFlag:
    def test_flag_defaults_false(self, monkeypatch):
        monkeypatch.delenv("COMMERCE_LIFECYCLE_EXTERNAL_SHADOW_ENABLED", raising=False)
        assert commerce_lifecycle_external_shadow_enabled() is False

    @patch.dict(os.environ, {"COMMERCE_LIFECYCLE_EXTERNAL_SHADOW_ENABLED": "false"}, clear=False)
    def test_flag_false_no_ledger_row(self):
        db, _ = _make_db()
        order = _order_row()
        result = record_external_order_transition_shadow(
            db,
            tenant_id=1,
            order=order,
            provider="salla",
            raw_previous_status="under_review",
            raw_current_status="shipped",
            normalized_order={"external_id": "ord-99", "status": "shipped"},
            raw_payload={"event_id": "evt-1", "updated_at": "t1"},
        )
        assert result is None
        assert db.query(CommerceLifecycleNotificationLedger).count() == 0

    @patch.dict(os.environ, {"COMMERCE_LIFECYCLE_EXTERNAL_SHADOW_ENABLED": "false"}, clear=False)
    @patch("core.commerce_lifecycle.external_shadow_producer.resolve_merchant_capabilities")
    @patch("core.commerce_lifecycle.external_shadow_producer.normalize_external_lifecycle_intent")
    def test_flag_false_skips_capability_and_normalization(
        self, mock_normalize, mock_caps
    ):
        db, _ = _make_db()
        record_external_order_transition_shadow(
            db,
            tenant_id=1,
            order=_order_row(),
            provider="salla",
            raw_previous_status="under_review",
            raw_current_status="shipped",
            normalized_order={"status": "shipped"},
        )
        mock_normalize.assert_not_called()
        mock_caps.assert_not_called()

    @patch.dict(os.environ, {"COMMERCE_LIFECYCLE_EXTERNAL_SHADOW_ENABLED": "0"}, clear=False)
    def test_flag_zero_is_false(self):
        assert commerce_lifecycle_external_shadow_enabled() is False

    @patch.dict(os.environ, {"COMMERCE_LIFECYCLE_EXTERNAL_SHADOW_ENABLED": "on"}, clear=False)
    def test_flag_on_is_true(self):
        assert commerce_lifecycle_external_shadow_enabled() is True

    @patch.dict(os.environ, {"COMMERCE_LIFECYCLE_EXTERNAL_SHADOW_ENABLED": "true"}, clear=False)
    @patch("core.commerce_lifecycle.external_shadow_producer.resolve_merchant_capabilities")
    def test_flag_true_writes_one_row(self, mock_caps):
        mock_caps.return_value = SimpleNamespace(
            to_dict=lambda: {
                "has_external_store": True,
                "supports_external_checkout": True,
                "supports_external_coupons": False,
                "supports_whatsapp_orders": True,
                "supports_nahla_orders": False,
                "supports_bank_transfer": False,
                "supports_cod": True,
                "has_whatsapp_catalog": False,
                "has_external_tracking": True,
                "has_nahla_tracking": False,
                "has_payment_link": True,
            }
        )
        db, _ = _make_db()
        order = _order_row(
            status="shipped",
            extra_metadata={
                "tracking_number": "TRK1",
                "payment_method": "cod",
            },
        )
        raw = {
            "event_id": "evt-ledger-1",
            "updated_at": "2026-07-13T10:00:00Z",
            "shipping": {"tracking_link": "https://carrier.example/track/1"},
        }
        ledger_id = record_external_order_transition_shadow(
            db,
            tenant_id=1,
            order=order,
            provider="salla",
            raw_previous_status="under_review",
            raw_current_status="shipped",
            normalized_order={"external_id": "ord-99", "status": "shipped"},
            raw_payload=raw,
        )
        assert ledger_id is not None
        assert db.query(CommerceLifecycleNotificationLedger).count() == 1

    @patch.dict(os.environ, {"COMMERCE_LIFECYCLE_EXTERNAL_SHADOW_ENABLED": "true"}, clear=False)
    @patch("core.commerce_lifecycle.external_shadow_producer.resolve_merchant_capabilities")
    def test_duplicate_retry_still_one_row(self, mock_caps):
        mock_caps.return_value = SimpleNamespace(
            to_dict=lambda: {
                "has_external_store": True,
                "supports_external_checkout": True,
                "supports_external_coupons": False,
                "supports_whatsapp_orders": True,
                "supports_nahla_orders": False,
                "supports_bank_transfer": False,
                "supports_cod": True,
                "has_whatsapp_catalog": False,
                "has_external_tracking": True,
                "has_nahla_tracking": False,
                "has_payment_link": True,
            }
        )
        db, _ = _make_db()
        order = _order_row(status="shipped")
        kwargs = dict(
            db=db,
            tenant_id=1,
            order=order,
            provider="salla",
            raw_previous_status="under_review",
            raw_current_status="shipped",
            normalized_order={"external_id": "ord-99", "status": "shipped"},
            raw_payload={
                "event_id": "evt-dup",
                "updated_at": "2026-07-13T10:00:00Z",
                "shipping": {"tracking_link": "https://carrier.example/track/1"},
            },
        )
        record_external_order_transition_shadow(**kwargs)
        record_external_order_transition_shadow(**kwargs)
        assert db.query(CommerceLifecycleNotificationLedger).count() == 1


class TestTruthfulness:
    @patch.dict(os.environ, {"COMMERCE_LIFECYCLE_EXTERNAL_SHADOW_ENABLED": "true"}, clear=False)
    @patch("core.commerce_lifecycle.external_shadow_producer.resolve_merchant_capabilities")
    def test_shipment_without_tracking_evidence_blocked(self, mock_caps):
        mock_caps.return_value = SimpleNamespace(
            to_dict=lambda: {
                "has_external_store": True,
                "supports_external_checkout": True,
                "supports_external_coupons": False,
                "supports_whatsapp_orders": True,
                "supports_nahla_orders": False,
                "supports_bank_transfer": False,
                "supports_cod": True,
                "has_whatsapp_catalog": False,
                "has_external_tracking": True,
                "has_nahla_tracking": False,
                "has_payment_link": True,
            }
        )
        db, _ = _make_db()
        order = _order_row(status="shipped", extra_metadata={"payment_method": "cod"})
        record_external_order_transition_shadow(
            db,
            tenant_id=1,
            order=order,
            provider="salla",
            raw_previous_status="under_review",
            raw_current_status="shipped",
            normalized_order={"external_id": "ord-99", "status": "shipped"},
            raw_payload={"event_id": "evt-no-track", "updated_at": "t1"},
        )
        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert row.outcome == ShadowLedgerOutcome.SHADOW_BLOCKED.value

    def test_checkout_url_does_not_populate_payment_url(self):
        order = _order_row(checkout_url="https://shop.example/checkout")
        evidence = build_order_lifecycle_evidence(
            order=order,
            normalized_order={"checkout_url": "https://shop.example/checkout"},
            raw_payload={},
            source_event_id="evt-1",
            transition_version="v1",
        )
        assert evidence.checkout_url
        assert evidence.payment_url is None

    def test_delivered_requires_delivered_at_evidence(self):
        order = _order_row(status="delivered", extra_metadata={})
        evidence = build_order_lifecycle_evidence(
            order=order,
            normalized_order={"status": "delivered"},
            raw_payload={},
            source_event_id="evt-2",
            transition_version="v2",
        )
        assert evidence.delivered_at is None

    @patch.dict(os.environ, {"COMMERCE_LIFECYCLE_EXTERNAL_SHADOW_ENABLED": "true"}, clear=False)
    @patch("core.commerce_lifecycle.external_shadow_producer.resolve_merchant_capabilities")
    def test_json_contains_field_names_only(self, mock_caps):
        mock_caps.return_value = SimpleNamespace(
            to_dict=lambda: {
                "has_external_store": True,
                "supports_external_checkout": True,
                "supports_external_coupons": False,
                "supports_whatsapp_orders": True,
                "supports_nahla_orders": False,
                "supports_bank_transfer": False,
                "supports_cod": True,
                "has_whatsapp_catalog": False,
                "has_external_tracking": True,
                "has_nahla_tracking": False,
                "has_payment_link": True,
            }
        )
        db, _ = _make_db()
        order = _order_row(status="shipped")
        record_external_order_transition_shadow(
            db,
            tenant_id=1,
            order=order,
            provider="salla",
            raw_previous_status="under_review",
            raw_current_status="shipped",
            normalized_order={"external_id": "ord-99", "status": "shipped"},
            raw_payload={
                "event_id": "evt-json",
                "updated_at": "t1",
                "shipping": {"tracking_link": "https://carrier.example/track/1"},
            },
        )
        row = db.query(CommerceLifecycleNotificationLedger).one()
        for field in row.evidence_present_json:
            assert field in {
                "order_number",
                "checkout_url",
                "payment_url",
                "tracking_url",
                "tracking_number",
                "carrier",
                "delivered_at",
                "payment_method",
                "review_url",
                "coupon_code",
                "customer_phone",
                "customer_name",
                "status",
                "source_event_id",
                "transition_version",
            }
        blob = json.dumps(row.evidence_present_json)
        assert "https://" not in blob
        assert "+966" not in blob


class TestIsolation:
    def test_no_ai_imports_in_pr2c_modules(self):
        modules = [
            "core.commerce_lifecycle.external_shadow_producer",
            "store_integration.lifecycle_normalization",
            "store_adapters.salla_lifecycle",
        ]
        forbidden = (
            "backend.modules.ai",
            "automation_engine",
            "delivery_policy",
            "service_template_resolver",
        )
        for mod_name in modules:
            mod = importlib.import_module(mod_name)
            src = Path(mod.__file__).read_text(encoding="utf-8")
            for token in forbidden:
                assert token not in src, f"{mod_name} must not import {token}"

    def test_normalize_external_via_adapter(self):
        intent, reason = normalize_external_lifecycle_intent(
            provider="salla",
            raw_previous_status="under_review",
            raw_current_status="shipped",
            normalized_order={"payment_method": "cod"},
        )
        assert intent == BusinessIntent.SHIPMENT_AVAILABLE
        assert reason == "adapter_mapped"


class TestStoreSyncHook:
    @patch.dict(os.environ, {"COMMERCE_LIFECYCLE_EXTERNAL_SHADOW_ENABLED": "false"}, clear=False)
    def test_shadow_failure_does_not_fail_ingestion_helper(self):
        from services.store_sync import _record_external_lifecycle_shadow_best_effort

        sync = SimpleNamespace(db=MagicMock(), tenant_id=1)
        order = _order_row()
        with patch(
            "core.commerce_lifecycle.external_shadow_producer.record_external_order_transition_shadow",
            side_effect=RuntimeError("boom"),
        ):
            _record_external_lifecycle_shadow_best_effort(
                sync,
                order=order,
                provider="salla",
                raw_previous_status="pending",
                raw_current_status="shipped",
                normalized_order={"status": "shipped"},
            )

    @patch.dict(os.environ, {"COMMERCE_LIFECYCLE_EXTERNAL_SHADOW_ENABLED": "true"}, clear=False)
    @patch("core.commerce_lifecycle.external_shadow_producer.reserve_shadow_decision")
    def test_shadow_error_does_not_propagate_from_helper(self, mock_reserve):
        mock_reserve.side_effect = RuntimeError("ledger down")
        from services.store_sync import _record_external_lifecycle_shadow_best_effort

        sync = SimpleNamespace(db=MagicMock(), tenant_id=1)
        _record_external_lifecycle_shadow_best_effort(
            sync,
            order=_order_row(),
            provider="salla",
            raw_previous_status="pending",
            raw_current_status="shipped",
            normalized_order={"status": "shipped"},
        )
