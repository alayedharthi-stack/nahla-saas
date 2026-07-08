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
