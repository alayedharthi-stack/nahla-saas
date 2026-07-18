"""Tests for persona compose metadata merge into outbound message_events."""
from __future__ import annotations

from modules.ai.brain.persona.integration import merge_persona_compose_into_extra_metadata


class TestPersonaComposeMetadataMerge:
    def test_merge_preserves_catalog_fact_guard_diagnostics(self) -> None:
        event_meta = {
            "chosen_path": "fact_bound_persona_compose",
            "persona_compose": {
                "surface": "catalog_product_answer",
                "source": "persona_llm",
            },
            "question_kind": "price",
            "catalog_fact_products_len": 1,
            "catalog_fact_product_ids": [109, 121],
            "catalog_fact_price_values": [387],
            "catalog_fact_rebuild_source": "db_by_catalog_product_ids",
        }
        merged = merge_persona_compose_into_extra_metadata({}, event_meta)
        assert merged["catalog_fact_products_len"] == 1
        assert merged["catalog_fact_product_ids"] == [109, 121]
        assert merged["catalog_fact_price_values"] == [387]
        assert merged["catalog_fact_rebuild_source"] == "db_by_catalog_product_ids"

    def test_merge_preserves_conditional_coupon_constitutional_metadata(self) -> None:
        event_meta = {
            "chosen_path": "customer_conditional_coupon_compose",
            "compose_source": "persona_llm",
            "response_mode": "customer_conditional_coupon_answer",
            "llm_candidate_present": True,
            "final_text_transformed": True,
            "final_transform_reasons": ["commerce_reply_quality_guard"],
            "customer_conditional_coupon_compose_active": True,
            "facts_snapshot_id": "snap-merge-001",
        }
        merged = merge_persona_compose_into_extra_metadata({}, event_meta)
        assert merged["chosen_path"] == "customer_conditional_coupon_compose"
        assert merged["customer_conditional_coupon_compose_active"] is True
        assert merged["facts_snapshot_id"] == "snap-merge-001"
        assert merged["final_transform_reasons"] == ["commerce_reply_quality_guard"]

    def test_merge_preserves_general_llm_fallthrough_constitutional_metadata(self) -> None:
        event_meta = {
            "chosen_path": "customer_conditional_coupon_general_llm_fallthrough",
            "compose_source": "llm",
            "response_mode": "customer_conditional_coupon_general_llm",
            "llm_candidate_present": True,
            "final_text_transformed": True,
            "final_transform_reasons": ["customer_conditional_coupon_general_llm_evidence_guard"],
            "final_customer_text_source": "guard_rewrite",
            "customer_conditional_coupon_general_llm_fallthrough": True,
            "conditional_coupon_guard_failed_reason": "coupon_code_disclosure",
            "facts_snapshot_id": "snap-merge-general-llm",
        }
        merged = merge_persona_compose_into_extra_metadata({}, event_meta)
        assert merged["chosen_path"] == "customer_conditional_coupon_general_llm_fallthrough"
        assert merged["compose_source"] == "llm"
        assert merged["customer_conditional_coupon_general_llm_fallthrough"] is True
        assert merged["final_customer_text_source"] == "guard_rewrite"

    def test_merge_preserves_zero_len_diagnostics(self) -> None:
        event_meta = {
            "chosen_path": "fact_bound_persona_compose",
            "persona_compose": {"surface": "catalog_product_answer"},
            "catalog_fact_products_len": 0,
            "catalog_fact_product_ids": [109],
            "catalog_fact_price_values": [],
        }
        merged = merge_persona_compose_into_extra_metadata({}, event_meta)
        assert merged["catalog_fact_products_len"] == 0
        assert merged["catalog_fact_price_values"] == []
