"""Regression tests for evidence-free clarification questions."""
from modules.ai.commerce_agent_v2.guardrails import (
    _contains_evidence_free_factual_assertion,
)


def test_bare_product_noun_in_clarification_question_is_not_an_assertion() -> None:
    assert not _contains_evidence_free_factual_assertion(
        "أي منتج تقصد؟ اذكر اسمه أو ترتيبه."
    )


def test_store_possession_assertion_remains_blocked_inside_question() -> None:
    assert _contains_evidence_free_factual_assertion(
        "عندنا منتجات كثيرة، أي واحد تقصد؟"
    )


def test_product_statement_without_question_remains_blocked() -> None:
    assert _contains_evidence_free_factual_assertion("هذا هو المنتج.")
