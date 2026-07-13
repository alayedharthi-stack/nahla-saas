"""Focused unit tests for Layer 2 pure shadow contracts (Gate 0)."""
from __future__ import annotations

import ast
import importlib
import inspect
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

_LAYER2_PACKAGE = Path(__file__).resolve().parents[1] / "modules" / "ai" / "brain" / "truth_surface" / "layer2"
_CONTRACT_MODULES = (
    "modules.ai.brain.truth_surface.layer2.intent_evidence",
    "modules.ai.brain.truth_surface.layer2.decision_plan_shadow",
    "modules.ai.brain.truth_surface.layer2.domain_registry",
    "modules.ai.brain.truth_surface.layer2.__init__",
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


def _sample_intent() -> IntentEvidence:
    return IntentEvidence(
        confidence=0.9,
        entities=({"entity_kind": "coupon_code"},),
        required_domains=("coupons", "customer", "capabilities"),
        evidence_refs=("trigger:coupon_intent",),
        ambiguity_state=AmbiguityState.CLEAR,
        trigger_ids=("always_base", "coupon_intent"),
        source_turn_ref="turn-ref-1",
    )


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


def test_layer2_contract_modules_have_no_forbidden_imports() -> None:
    for module_name in _CONTRACT_MODULES:
        source = inspect.getsource(importlib.import_module(module_name))
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    _assert_import_allowed(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                _assert_import_allowed(node.module)


def _assert_import_allowed(module_name: str) -> None:
    for forbidden in _FORBIDDEN_IMPORT_PREFIXES:
        if module_name == forbidden or module_name.startswith(forbidden + "."):
            pytest.fail(f"forbidden import: {module_name}")


def test_builders_module_has_no_db_or_network_imports() -> None:
  builders = importlib.import_module("modules.ai.brain.truth_surface.layer2.builders")
  source = inspect.getsource(builders)
  tree = ast.parse(source)
  for node in ast.walk(tree):
      if isinstance(node, ast.Import):
          for alias in node.names:
              _assert_import_allowed(alias.name)
      elif isinstance(node, ast.ImportFrom) and node.module:
          _assert_import_allowed(node.module)


def test_contract_modules_contain_no_customer_facing_arabic_prose_constants() -> None:
    for module_name in _CONTRACT_MODULES:
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
    assert all("entity_kind" in item for item in evidence.entities)


def test_build_decision_plan_shadow_defers_without_snapshot() -> None:
    evidence = _sample_intent()
    plan = build_decision_plan_shadow(evidence=evidence, snapshot=None)
    assert plan.proposed_action == ProposedActionKind.DEFER_UNAVAILABLE
    assert plan.missing_facts == plan.required_facts
