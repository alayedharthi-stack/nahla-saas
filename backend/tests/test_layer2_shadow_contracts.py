"""Focused unit tests for Layer 2 pure shadow contracts (Gate 0)."""
from __future__ import annotations

import ast
import importlib
import inspect
import json
import pkgutil
import re
from pathlib import Path
from typing import Iterable

import pytest

from modules.ai.brain.truth_surface.contract import (
    TrustedContextSnapshot,
    TrustedDomain,
)
from modules.ai.brain.truth_surface.layer2 import (
    AmbiguityState,
    DecisionPlanShadow,
    IntentEvidence,
    LAYER2_CONTRACT_STATUS,
    ProposedActionKind,
    build_decision_plan_shadow,
    build_intent_evidence,
    get_domain_definition,
    list_domain_definitions,
    registered_domain_ids,
)

_FORBIDDEN_IMPORT_PREFIXES = (
    "core.commerce_lifecycle",
    "modules.ai.brain.truth_surface.trusted_context",
    "routers.whatsapp_webhook",
    "modules.ai.brain.pipeline",
    "modules.ai.brain.compose",
    "sqlalchemy",
    "httpx",
    "requests",
    "aiohttp",
)
_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")
_COUPON_CODE = "SAVE20"
_PHONE = "966500000099"
_ARABIC_TEXT = "عندكم عرض؟"
_PROMO_CONDITION = "min_cart_total=250|SAR|weekend-only"
_EXCEPTION_TEXT = "RuntimeError: database connection refused"


def _sample_intent() -> IntentEvidence:
    return IntentEvidence(
        confidence=0.9,
        entities=({"entity_kind": "coupon_code"},),
        required_domains=("coupons", "customer", "capabilities"),
        evidence_refs=("trigger:coupon_intent", "trigger:always_base"),
        ambiguity_state=AmbiguityState.CLEAR,
        trigger_ids=("always_base", "coupon_intent"),
        source_turn_ref="turn-ref-1",
    )


def _all_layer2_module_names() -> list[str]:
    import modules.ai.brain.truth_surface.layer2 as layer2_pkg

    return [
        module_info.name
        for module_info in pkgutil.walk_packages(layer2_pkg.__path__, layer2_pkg.__name__ + ".")
    ]


def _assert_import_allowed(module_name: str) -> None:
    for forbidden in _FORBIDDEN_IMPORT_PREFIXES:
        if module_name == forbidden or module_name.startswith(forbidden + "."):
            pytest.fail(f"forbidden import: {module_name}")


def _assert_rejected_and_not_serialized(factory, *forbidden_values: str) -> None:
    with pytest.raises(ValueError):
        factory()
    for value in forbidden_values:
        assert value not in json.dumps({})


def test_intent_evidence_round_trip_serialization() -> None:
    original = _sample_intent()
    restored = IntentEvidence.from_dict(original.to_dict())
    assert restored == original


def test_intent_evidence_rejects_invalid_confidence() -> None:
    with pytest.raises(ValueError, match="confidence"):
        IntentEvidence(confidence=1.5)
    with pytest.raises(ValueError, match="confidence"):
        IntentEvidence(confidence=-0.1)


def test_intent_evidence_tolerates_unknown_optional_fields() -> None:
    payload = _sample_intent().to_dict()
    payload["future_field"] = "ignored"
    restored = IntentEvidence.from_dict(payload)
    assert restored.confidence == 0.9
    assert restored.required_domains == ("coupons", "customer", "capabilities")


def test_intent_evidence_rejects_entity_extra_keys_and_values() -> None:
    _assert_rejected_and_not_serialized(
        lambda: IntentEvidence(
            confidence=0.5,
            entities=({"entity_kind": "coupon_code", "code": _COUPON_CODE},),
        ),
        _COUPON_CODE,
    )
    _assert_rejected_and_not_serialized(
        lambda: IntentEvidence(
            confidence=0.5,
            entities=({"entity_kind": "coupon_code", "value": _COUPON_CODE},),
        ),
        _COUPON_CODE,
    )


def test_intent_evidence_rejects_phone_like_source_turn_ref() -> None:
    _assert_rejected_and_not_serialized(
        lambda: IntentEvidence(confidence=0.5, source_turn_ref=_PHONE),
        _PHONE,
    )


def test_intent_evidence_rejects_arabic_customer_text_in_evidence_refs() -> None:
    _assert_rejected_and_not_serialized(
        lambda: IntentEvidence(
            confidence=0.5,
            evidence_refs=(f"trigger:{_ARABIC_TEXT}",),
        ),
        _ARABIC_TEXT,
    )


def test_decision_plan_shadow_round_trip_serialization() -> None:
    original = DecisionPlanShadow(
        proposed_action=ProposedActionKind.ANSWER_FROM_FACTS,
        required_facts=("domain:coupons",),
        loaded_coverage=("coupons", "customer", "capabilities"),
        reason_codes=("facts_available",),
        snapshot_ref="snap-1",
    )
    restored = DecisionPlanShadow.from_dict(original.to_dict())
    assert restored == original


def test_decision_plan_shadow_shadow_only_cannot_be_false() -> None:
    with pytest.raises(ValueError, match="shadow_only"):
        DecisionPlanShadow(
            proposed_action=ProposedActionKind.NO_OP_SHADOW,
            shadow_only=False,
        )
    payload = DecisionPlanShadow(
        proposed_action=ProposedActionKind.NO_OP_SHADOW,
    ).to_dict()
    payload["shadow_only"] = False
    with pytest.raises(ValueError, match="shadow_only"):
        DecisionPlanShadow.from_dict(payload)


def test_decision_plan_shadow_has_no_enforce_or_execute_api() -> None:
    plan = DecisionPlanShadow(proposed_action=ProposedActionKind.NO_OP_SHADOW)
    for forbidden in ("execute", "enforce", "dispatch", "apply", "run"):
        assert not hasattr(plan, forbidden)
        assert forbidden not in DecisionPlanShadow.__dict__


def test_decision_plan_shadow_rejects_raw_promotion_condition_in_constraints() -> None:
    _assert_rejected_and_not_serialized(
        lambda: DecisionPlanShadow(
            proposed_action=ProposedActionKind.NO_OP_SHADOW,
            constraints=(_PROMO_CONDITION,),
        ),
        _PROMO_CONDITION,
    )


def test_decision_plan_shadow_rejects_arbitrary_exception_text_in_reason_codes() -> None:
    _assert_rejected_and_not_serialized(
        lambda: DecisionPlanShadow(
            proposed_action=ProposedActionKind.NO_OP_SHADOW,
            reason_codes=(_EXCEPTION_TEXT,),
        ),
        _EXCEPTION_TEXT,
    )


def test_decision_plan_shadow_metadata_never_contains_rejected_customer_content() -> None:
    plan = DecisionPlanShadow(
        proposed_action=ProposedActionKind.DEFER_UNAVAILABLE,
        reason_codes=("snapshot_missing",),
    )
    meta = plan.to_metadata()
    blob = json.dumps(meta, ensure_ascii=False)
    for forbidden in (_COUPON_CODE, _PHONE, _ARABIC_TEXT, _PROMO_CONDITION, _EXCEPTION_TEXT):
        assert forbidden not in blob


def test_missing_coverage_represented_as_shadow_metadata_only() -> None:
    evidence = build_intent_evidence(message="coupon please")
    snapshot = TrustedContextSnapshot(
        tenant_id=1,
        customer_phone="966500000099",
        loaded_domains=["customer", "capabilities"],
        facts=[],
    )
    plan = build_decision_plan_shadow(evidence=evidence, snapshot=snapshot)
    assert plan.proposed_action == ProposedActionKind.CLARIFY_MISSING
    assert plan.missing_facts
    assert "coupons" in plan.missing_facts[0]
    assert plan.shadow_only is True
    meta = plan.to_metadata()
    assert meta["proposed_action"] == ProposedActionKind.CLARIFY_MISSING.value
    assert "message_text" not in meta
    assert "facts" not in meta
    assert _PHONE not in json.dumps(meta)


def test_domain_registry_metadata_validates_and_has_no_callable_loaders() -> None:
    definitions = list_domain_definitions()
    assert len(definitions) == 9
    for definition in definitions:
        metadata = definition.to_metadata()
        assert metadata["schema_version"] == "1"
        assert isinstance(metadata["loader_id"], str)
        assert not callable(definition.loader_id)
        assert definition.read_only is True
    assert TrustedDomain.COUPONS.value in registered_domain_ids()
    coupon = get_domain_definition(TrustedDomain.COUPONS)
    assert coupon.privacy_classification.value == "secret_never_log"


def test_schema_version_required_on_contracts() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        IntentEvidence.from_dict({"confidence": 0.5, "schema_version": "2"})
    with pytest.raises(ValueError, match="schema_version"):
        DecisionPlanShadow.from_dict(
            {"proposed_action": "no_op_shadow", "schema_version": "9"},
        )


def test_backward_compatible_optional_field_addition() -> None:
    payload = DecisionPlanShadow(
        proposed_action=ProposedActionKind.DEFER_UNAVAILABLE,
        reason_codes=("snapshot_missing",),
    ).to_dict()
    payload["future_optional_flag"] = True
    restored = DecisionPlanShadow.from_dict(payload)
    assert restored.proposed_action == ProposedActionKind.DEFER_UNAVAILABLE


def test_all_layer2_modules_have_no_forbidden_imports() -> None:
    module_names = _all_layer2_module_names()
    assert "modules.ai.brain.truth_surface.layer2._serialization" in module_names
    assert "modules.ai.brain.truth_surface.layer2.builders" in module_names
    for module_name in module_names:
        module = importlib.import_module(module_name)
        source = inspect.getsource(module)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    _assert_import_allowed(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                _assert_import_allowed(node.module)


def test_contract_modules_contain_no_customer_facing_arabic_prose_constants() -> None:
    contract_modules = [
        name
        for name in _all_layer2_module_names()
        if name.endswith(
            (
                "intent_evidence",
                "decision_plan_shadow",
                "domain_registry",
                "__init__",
                "_privacy",
                "_serialization",
            ),
        )
    ]
    for module_name in contract_modules:
        path = importlib.import_module(module_name).__file__
        assert path is not None
        text = Path(path).read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip().startswith("#"):
                continue
            if "re.compile" in line or "\\u" in line:
                continue
            assert not _ARABIC_RE.search(line), f"Arabic prose constant in {module_name}: {line!r}"


def test_layer2_status_is_proposed_shadow_contract() -> None:
    assert LAYER2_CONTRACT_STATUS == "PROPOSED / SHADOW CONTRACT"


def test_build_intent_evidence_is_pure_without_runtime_side_effects() -> None:
    evidence = build_intent_evidence(message="discount code?")
    assert evidence.shadow_only is True
    assert "coupons" in evidence.required_domains
    assert all(item.keys() == {"entity_kind"} for item in evidence.entities)
    assert _COUPON_CODE not in json.dumps(evidence.to_dict())


def test_build_decision_plan_shadow_defers_without_snapshot() -> None:
    evidence = _sample_intent()
    plan = build_decision_plan_shadow(evidence=evidence, snapshot=None)
    assert plan.proposed_action == ProposedActionKind.DEFER_UNAVAILABLE
    assert plan.missing_facts == plan.required_facts
