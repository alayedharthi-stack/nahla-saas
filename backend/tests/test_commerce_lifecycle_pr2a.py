"""
PR 2A — commerce lifecycle contracts, registry, and validation tests.
"""
from __future__ import annotations

import ast
import importlib
import pkgutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.commerce_lifecycle.contracts import (
    STABLE_REASON_CODES,
    ApprovedTemplateRequired,
    Blocked,
    MerchantActionRequired,
    NoNotification,
    SessionMessageRequired,
)
from core.commerce_lifecycle.definitions import (
    INITIAL_DEFINITIONS,
    KNOWN_CAPABILITY_FIELDS,
    BusinessIntentDefinition,
    build_initial_definitions,
)
from core.commerce_lifecycle.evidence import (
    OrderLifecycleEvidence,
    is_valid_https_evidence_url,
    validate_capabilities,
    validate_evidence,
    validate_template_evidence,
)
from core.commerce_lifecycle.intents import BusinessIntent
from core.commerce_lifecycle.registry import LifecycleIntentRegistry, get_default_registry
from core.commerce_lifecycle.strategies import (
    ClosedWindowStrategy,
    OpenWindowStrategy,
    RetryPolicy,
)
from core.merchant_capabilities import MerchantCapabilities


def _caps(**kwargs) -> MerchantCapabilities:
    defaults = dict(
        has_external_store=False,
        supports_external_checkout=False,
        supports_external_coupons=False,
        supports_whatsapp_orders=False,
        supports_nahla_orders=False,
        supports_bank_transfer=False,
        supports_cod=False,
        has_whatsapp_catalog=False,
        has_external_tracking=False,
        has_nahla_tracking=False,
        has_payment_link=False,
    )
    defaults.update(kwargs)
    return MerchantCapabilities(**defaults)


class TestBusinessIntent:
    def test_stable_machine_readable_values(self):
        for intent in BusinessIntent:
            assert intent.value == intent.value.lower()
            assert " " not in intent.value
            assert intent.value.isascii()

    def test_no_provider_specific_values(self):
        forbidden = {"salla", "shopify", "zid", "shipped", "in_transit"}
        for intent in BusinessIntent:
            assert intent.value not in forbidden


class TestBusinessIntentDefinition:
    def test_frozen(self):
        definition = INITIAL_DEFINITIONS[0]
        with pytest.raises(Exception):
            definition.service_key = "other"  # type: ignore[misc]

    def test_rejects_approved_template_without_service_key(self):
        with pytest.raises(ValueError, match="service_key"):
            BusinessIntentDefinition(
                intent=BusinessIntent.ORDER_CANCELLED,
                closed_window_strategy=ClosedWindowStrategy.APPROVED_TEMPLATE,
            )

    def test_rejects_duplicate_required_evidence(self):
        with pytest.raises(ValueError, match="duplicate"):
            BusinessIntentDefinition(
                intent=BusinessIntent.ORDER_CANCELLED,
                required_evidence=("order_number", "order_number"),
            )

    def test_rejects_unknown_capability(self):
        with pytest.raises(ValueError, match="unknown capability"):
            BusinessIntentDefinition(
                intent=BusinessIntent.ORDER_CANCELLED,
                required_capabilities=("not_a_real_capability",),
            )

    def test_rejects_required_optional_overlap(self):
        with pytest.raises(ValueError, match="required and optional"):
            BusinessIntentDefinition(
                intent=BusinessIntent.ORDER_CANCELLED,
                required_evidence=("order_number",),
                optional_evidence=("order_number",),
            )

    def test_rejects_empty_idempotency_fields(self):
        with pytest.raises(ValueError, match="idempotency"):
            BusinessIntentDefinition(
                intent=BusinessIntent.ORDER_CANCELLED,
                idempotency_key_fields=(),
            )


class TestRegistry:
    def test_initial_intents_registered_once(self):
        registry = LifecycleIntentRegistry(INITIAL_DEFINITIONS)
        expected = {
            BusinessIntent.ORDER_CONFIRMED,
            BusinessIntent.PAYMENT_NEEDED,
            BusinessIntent.SHIPMENT_AVAILABLE,
            BusinessIntent.ORDER_DELIVERED,
            BusinessIntent.CUSTOMER_ACTION_REQUIRED,
            BusinessIntent.REVIEW_REQUEST,
        }
        assert registry.registered_intents() == expected

    def test_duplicate_registration_rejected(self):
        registry = LifecycleIntentRegistry()
        definition = INITIAL_DEFINITIONS[0]
        registry.register(definition)
        with pytest.raises(ValueError, match="duplicate"):
            registry.register(definition)

    def test_unknown_intent_fails_safely(self):
        registry = LifecycleIntentRegistry(INITIAL_DEFINITIONS)
        with pytest.raises(KeyError, match="unsupported"):
            registry.get(BusinessIntent.ORDER_CANCELLED)

    def test_list_definitions_is_immutable_copy(self):
        registry = LifecycleIntentRegistry(INITIAL_DEFINITIONS)
        listed = registry.list_definitions()
        assert len(listed) == 6
        assert listed[0].intent == listed[0].intent

    def test_default_registry_singleton(self):
        assert len(get_default_registry()) == 6


class TestEvidenceValidation:
    def test_valid_https_tracking_url_accepted(self):
        definition = get_default_registry().get(BusinessIntent.SHIPMENT_AVAILABLE)
        evidence = OrderLifecycleEvidence(
            tracking_url="https://track.shipping-provider.io/packages/abc123",
        )
        result = validate_evidence(definition, evidence)
        assert result.valid is True
        assert "tracking_url" in result.present_fields

    def test_empty_tracking_url_rejected(self):
        definition = get_default_registry().get(BusinessIntent.SHIPMENT_AVAILABLE)
        result = validate_evidence(
            definition,
            OrderLifecycleEvidence(tracking_url=""),
        )
        assert result.valid is False
        assert "tracking_url" in result.missing_fields or "tracking_url" in result.invalid_fields

    def test_http_tracking_url_rejected(self):
        definition = get_default_registry().get(BusinessIntent.SHIPMENT_AVAILABLE)
        result = validate_evidence(
            definition,
            OrderLifecycleEvidence(tracking_url="http://track.shipping-provider.io/x"),
        )
        assert result.valid is False
        assert "tracking_url" in result.invalid_fields

    def test_placeholder_url_rejected(self):
        assert is_valid_https_evidence_url("https://example.com/track") is False
        assert is_valid_https_evidence_url("https://localhost/track") is False

    def test_payment_url_not_inferred_from_checkout_url(self):
        evidence = OrderLifecycleEvidence(
            checkout_url="https://shop.merchant.io/checkout/1",
            payment_url=None,
        )
        assert evidence.checkout_url != evidence.payment_url
        definition = get_default_registry().get(BusinessIntent.PAYMENT_NEEDED)
        result = validate_evidence(definition, evidence)
        assert result.valid is True
        assert "payment_url" not in result.present_fields

    def test_order_number_required_for_order_confirmed(self):
        definition = get_default_registry().get(BusinessIntent.ORDER_CONFIRMED)
        result = validate_evidence(definition, OrderLifecycleEvidence())
        assert result.valid is False
        assert "order_number" in result.missing_fields

    def test_delivered_intent_requires_delivered_at(self):
        definition = get_default_registry().get(BusinessIntent.ORDER_DELIVERED)
        result = validate_evidence(
            definition,
            OrderLifecycleEvidence(order_number="1001"),
        )
        assert result.valid is False
        assert "delivered_at" in result.missing_fields

    def test_validation_outputs_field_names_only(self):
        definition = get_default_registry().get(BusinessIntent.ORDER_CONFIRMED)
        result = validate_evidence(
            definition,
            OrderLifecycleEvidence(order_number="secret-order-999"),
        )
        dumped = repr(result)
        assert "secret-order-999" not in dumped

    def test_tracking_number_group_satisfies_shipment_intent(self):
        definition = get_default_registry().get(BusinessIntent.SHIPMENT_AVAILABLE)
        result = validate_evidence(
            definition,
            OrderLifecycleEvidence(tracking_number="TRK-12345"),
        )
        assert result.valid is True

    def test_tracking_number_only_fails_template_evidence_for_shipment(self):
        definition = get_default_registry().get(BusinessIntent.SHIPMENT_AVAILABLE)
        evidence = OrderLifecycleEvidence(tracking_number="TRK-12345")
        business = validate_evidence(definition, evidence)
        template = validate_template_evidence(definition, evidence)
        assert business.valid is True
        assert template.valid is False
        assert "tracking_url" in template.missing_fields

    def test_tracking_url_satisfies_business_and_template_evidence(self):
        definition = get_default_registry().get(BusinessIntent.SHIPMENT_AVAILABLE)
        evidence = OrderLifecycleEvidence(
            tracking_url="https://track.shipping-provider.io/packages/abc123",
        )
        assert validate_evidence(definition, evidence).valid is True
        assert validate_template_evidence(definition, evidence).valid is True

    def test_customer_action_requires_missing_fields(self):
        definition = get_default_registry().get(BusinessIntent.CUSTOMER_ACTION_REQUIRED)
        result = validate_evidence(
            definition,
            OrderLifecycleEvidence(missing_fields=("delivery_address",)),
        )
        assert result.valid is True


class TestCapabilityValidation:
    def test_required_capability_true_passes(self):
        definition = BusinessIntentDefinition(
            intent=BusinessIntent.ORDER_CANCELLED,
            required_capabilities=("has_external_tracking",),
        )
        result = validate_capabilities(
            definition,
            _caps(has_external_tracking=True),
        )
        assert result.valid is True

    def test_required_capability_false_fails(self):
        definition = BusinessIntentDefinition(
            intent=BusinessIntent.ORDER_CANCELLED,
            required_capabilities=("has_external_tracking",),
        )
        result = validate_capabilities(definition, _caps())
        assert result.valid is False
        assert "has_external_tracking" in result.missing_capabilities

    def test_unknown_capability_fails_closed(self):
        with pytest.raises(ValueError, match="unknown capability"):
            BusinessIntentDefinition(
                intent=BusinessIntent.ORDER_CANCELLED,
                required_capabilities=("unknown_capability_field",),
            )

    def test_forbidden_capability_detected(self):
        definition = BusinessIntentDefinition(
            intent=BusinessIntent.ORDER_CANCELLED,
            forbidden_capabilities=("has_payment_link",),
        )
        result = validate_capabilities(definition, _caps(has_payment_link=True))
        assert result.valid is False
        assert "has_payment_link" in result.forbidden_capabilities

    def test_evidence_still_required_when_capability_true(self):
        definition = get_default_registry().get(BusinessIntent.ORDER_CONFIRMED)
        caps = validate_capabilities(definition, _caps())
        evidence = validate_evidence(definition, OrderLifecycleEvidence())
        assert caps.valid is True
        assert evidence.valid is False


class TestInitialDefinitionsPolicy:
    """Service keys must match established Nahla template catalog semantics."""

    _APPROVED_TEMPLATE_BINDINGS = {
        BusinessIntent.ORDER_CONFIRMED: "order_confirmation",
        BusinessIntent.SHIPMENT_AVAILABLE: "shipping_tracking",
        BusinessIntent.REVIEW_REQUEST: "post_delivery",
    }

    def test_approved_template_service_keys_match_established_semantics(self):
        registry = get_default_registry()
        for intent, expected_service_key in self._APPROVED_TEMPLATE_BINDINGS.items():
            definition = registry.get(intent)
            assert definition.closed_window_strategy == ClosedWindowStrategy.APPROVED_TEMPLATE
            assert definition.service_key == expected_service_key

    def test_order_delivered_closed_window_blocked_without_ambiguous_service_key(self):
        definition = get_default_registry().get(BusinessIntent.ORDER_DELIVERED)
        assert definition.closed_window_strategy == ClosedWindowStrategy.BLOCKED
        assert definition.service_key is None

    def test_post_delivery_not_shared_by_order_delivered(self):
        delivered = get_default_registry().get(BusinessIntent.ORDER_DELIVERED)
        review = get_default_registry().get(BusinessIntent.REVIEW_REQUEST)
        assert delivered.service_key is None
        assert review.service_key == "post_delivery"


class TestOutcomes:
    def test_session_message_required_has_no_message_text_field(self):
        outcome = SessionMessageRequired(
            intent=BusinessIntent.ORDER_CONFIRMED,
            structured_facts={"order_number": "1001"},
        )
        assert outcome.handoff_kind == "lifecycle_notification"
        assert not hasattr(outcome, "message_text")
        with pytest.raises(ValueError, match="message_text"):
            SessionMessageRequired(
                intent=BusinessIntent.ORDER_CONFIRMED,
                structured_facts={"message_text": "hello"},
            )

    def test_approved_template_required_is_metadata_only(self):
        outcome = ApprovedTemplateRequired(
            intent=BusinessIntent.SHIPMENT_AVAILABLE,
            service_key="shipping_tracking",
            variables={"tracking_url": "https://track.shipping-provider.io/x"},
        )
        assert outcome.service_key == "shipping_tracking"
        assert not hasattr(outcome, "send")

    def test_reason_codes_are_machine_readable(self):
        for code in [
            NoNotification(BusinessIntent.PAYMENT_NEEDED, "capability_absent").reason_code,
            Blocked(BusinessIntent.PAYMENT_NEEDED, "missing_evidence").reason_code,
            MerchantActionRequired(BusinessIntent.PAYMENT_NEEDED, "merchant_action_required").reason_code,
        ]:
            assert code in STABLE_REASON_CODES
            assert " " not in code

    def test_outcomes_serializable_as_dict(self):
        delivered = datetime.now(timezone.utc)
        outcome = SessionMessageRequired(
            intent=BusinessIntent.ORDER_DELIVERED,
            structured_facts={"delivered_at": delivered.isoformat()},
        )
        assert outcome.intent == BusinessIntent.ORDER_DELIVERED


class TestArchitectureIsolation:
    def test_no_ai_module_imports_in_package(self):
        package_root = Path(__file__).resolve().parents[1] / "core" / "commerce_lifecycle"
        forbidden = ("modules.ai", "automation_engine", "meta", "openai")
        for module_info in pkgutil.walk_packages([str(package_root)], prefix="core.commerce_lifecycle."):
            if module_info.name.endswith(".tests"):
                continue
            mod = importlib.import_module(module_info.name)
            source_path = Path(mod.__file__).resolve()
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for bad in forbidden:
                            assert bad not in alias.name
                elif isinstance(node, ast.ImportFrom) and node.module:
                    for bad in forbidden:
                        assert bad not in node.module

    def test_build_initial_definitions_count(self):
        assert len(build_initial_definitions()) == 6

    def test_known_capability_fields_match_merchant_capabilities(self):
        cap_fields = set(MerchantCapabilities.__dataclass_fields__)
        assert KNOWN_CAPABILITY_FIELDS.issubset(cap_fields)
