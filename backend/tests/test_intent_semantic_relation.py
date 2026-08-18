"""Canonical intent semantic-relation registry — TAX-01 through TAX-08.

Asserts structured ownership, not customer wording.
"""
from __future__ import annotations

import inspect
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_BACKEND, os.path.join(_REPO, "database"), _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.intent.semantic_relation import (  # noqa: E402
    IntentSemanticDomain,
    IntentSemanticRegistryError,
    IntentSemanticRelation,
    get_intent_semantic_relation,
    is_direct_broader_relation,
    validate_intent_semantic_registry,
)
from modules.ai.brain.types import (  # noqa: E402
    INTENT_ASK_LOCATION,
    INTENT_ASK_OWNER_CONTACT,
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_PLATFORM_INQUIRY,
    INTENT_PRODUCT_VISUAL_REQUEST,
    INTENT_SOCIAL,
    INTENT_START_ORDER,
)


class TestTax01SelfParentRejected:
    def test_validator_rejects_self_parent(self) -> None:
        registry = {
            INTENT_ASK_PRODUCT: IntentSemanticRelation(
                domain=IntentSemanticDomain.PRODUCT_INQUIRY,
                broader_label=INTENT_ASK_PRODUCT,
            ),
        }
        try:
            validate_intent_semantic_registry(registry)
        except IntentSemanticRegistryError as exc:
            assert "own broader_label" in str(exc)
        else:
            raise AssertionError("self-parent registry must fail")


class TestTax02CyclesRejected:
    def test_validator_rejects_direct_parent_cycle(self) -> None:
        registry = {
            INTENT_ASK_PRODUCT: IntentSemanticRelation(
                domain=IntentSemanticDomain.PRODUCT_INQUIRY,
                broader_label=INTENT_ASK_PRICE,
            ),
            INTENT_ASK_PRICE: IntentSemanticRelation(
                domain=IntentSemanticDomain.PRODUCT_INQUIRY,
                broader_label=INTENT_ASK_PRODUCT,
            ),
        }
        try:
            validate_intent_semantic_registry(registry)
        except IntentSemanticRegistryError as exc:
            assert "cycle" in str(exc)
        else:
            raise AssertionError("cyclic registry must fail")


class TestTax03ParentDomainMismatchRejected:
    def test_validator_rejects_cross_domain_parent(self) -> None:
        mismatched = IntentSemanticRelation(
            domain=IntentSemanticDomain.PRODUCT_INQUIRY,
            broader_label=INTENT_ASK_PRODUCT,
        )
        object.__setattr__(mismatched, "domain", type("D", (), {"value": "store_info"})())
        registry = {
            INTENT_ASK_PRODUCT: IntentSemanticRelation(
                domain=IntentSemanticDomain.PRODUCT_INQUIRY,
            ),
            INTENT_PRODUCT_VISUAL_REQUEST: mismatched,
        }
        try:
            validate_intent_semantic_registry(registry)
        except IntentSemanticRegistryError as exc:
            assert "does not match" in str(exc)
        else:
            raise AssertionError("cross-domain parent must fail")

    def test_validator_rejects_dangling_parent(self) -> None:
        registry = {
            INTENT_PRODUCT_VISUAL_REQUEST: IntentSemanticRelation(
                domain=IntentSemanticDomain.PRODUCT_INQUIRY,
                broader_label=INTENT_ASK_LOCATION,
            ),
        }
        try:
            validate_intent_semantic_registry(registry)
        except IntentSemanticRegistryError as exc:
            assert "not registered" in str(exc)
        else:
            raise AssertionError("dangling broader_label must fail")


class TestTax04UnknownRelationNotInferred:
    def test_unregistered_intent_is_unknown(self) -> None:
        assert get_intent_semantic_relation(INTENT_ASK_LOCATION) is None
        assert get_intent_semantic_relation(INTENT_PLATFORM_INQUIRY) is None
        assert get_intent_semantic_relation(INTENT_SOCIAL) is None
        assert get_intent_semantic_relation("") is None
        assert is_direct_broader_relation(INTENT_ASK_LOCATION, INTENT_ASK_PRODUCT) is False
        assert is_direct_broader_relation("not_an_intent", INTENT_ASK_PRODUCT) is False


class TestTax05SameDomainIsInsufficient:
    def test_ask_price_is_not_parent_of_product_visual(self) -> None:
        visual = get_intent_semantic_relation(INTENT_PRODUCT_VISUAL_REQUEST)
        price = get_intent_semantic_relation(INTENT_ASK_PRICE)
        assert visual is not None and price is not None
        assert visual.domain == price.domain == IntentSemanticDomain.PRODUCT_INQUIRY
        assert is_direct_broader_relation(
            INTENT_PRODUCT_VISUAL_REQUEST, INTENT_ASK_PRICE
        ) is False
        assert is_direct_broader_relation(
            INTENT_ASK_PRICE, INTENT_PRODUCT_VISUAL_REQUEST
        ) is False


class TestTax06CanonicalProductVisualParent:
    def test_product_visual_is_direct_child_of_ask_product(self) -> None:
        relation = get_intent_semantic_relation(INTENT_PRODUCT_VISUAL_REQUEST)
        assert relation is not None
        assert relation.domain is IntentSemanticDomain.PRODUCT_INQUIRY
        assert relation.broader_label == INTENT_ASK_PRODUCT
        assert is_direct_broader_relation(
            INTENT_PRODUCT_VISUAL_REQUEST, INTENT_ASK_PRODUCT
        ) is True

    def test_ask_product_and_ask_price_have_no_parent(self) -> None:
        product = get_intent_semantic_relation(INTENT_ASK_PRODUCT)
        price = get_intent_semantic_relation(INTENT_ASK_PRICE)
        assert product is not None and price is not None
        assert product.domain is IntentSemanticDomain.PRODUCT_INQUIRY
        assert price.domain is IntentSemanticDomain.PRODUCT_INQUIRY
        assert product.broader_label is None
        assert price.broader_label is None


class TestTax07OutOfVocabDoesNotInferCompatibility:
    def test_unrelated_labels_are_not_compatible(self) -> None:
        for specific in (
            INTENT_ASK_LOCATION,
            INTENT_PLATFORM_INQUIRY,
            INTENT_SOCIAL,
            INTENT_START_ORDER,
            INTENT_ASK_OWNER_CONTACT,
        ):
            assert is_direct_broader_relation(specific, INTENT_ASK_PRODUCT) is False


class TestTax08NoExecutionOrLanguageOwnership:
    def test_registry_has_no_language_regex_or_action_mapping(self) -> None:
        from modules.ai.brain.intent import semantic_relation as registry_mod

        src = inspect.getsource(registry_mod)
        assert "import re" not in src
        assert "re.compile" not in src
        assert "ACTION_SEARCH_PRODUCTS" not in src
        assert "ACTION_" not in src
        assert "LAYER2_INTENT_HINT_VOCABULARY" not in src
        assert "LAYER2_PRODUCT_DOMAIN_HINTS" not in src
        assert "slot_extractor" not in src
        assert "_resolve_layer2_rule_precedence" not in src
        assert "tenant_id" not in src
        assert "customer_phone" not in src

    def test_registry_does_not_import_classifier_or_decision(self) -> None:
        from modules.ai.brain.intent import semantic_relation as registry_mod

        src = inspect.getsource(registry_mod)
        assert "from .classifier" not in src
        assert "decision.engine" not in src
        assert "decision.actions" not in src


class TestRegistryClassifierConsumptionBoundary:
    def test_classifier_consumes_direct_broader_relation_only(self) -> None:
        from modules.ai.brain.intent import classifier as classifier_mod

        src = inspect.getsource(classifier_mod)
        assert "from .semantic_relation import is_direct_broader_relation" in src
        assert "get_intent_semantic_relation" not in src
        assert "LAYER2_PRODUCT_DOMAIN_HINTS" not in src
        assert "LAYER2_INTENT_HINT_VOCABULARY" not in inspect.getsource(
            classifier_mod.layer2_is_compatible_broader_evidence
        )

    def test_slot_extractor_prompt_unchanged_by_this_module(self) -> None:
        from modules.ai.brain.intent import slot_extractor as slot_mod
        from modules.ai.brain.intent import semantic_relation as registry_mod

        assert "_SYSTEM" not in inspect.getsource(registry_mod)
        assert "product_visual_request" not in slot_mod._SYSTEM
