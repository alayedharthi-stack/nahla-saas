"""Unit tests for redacted model payload attestation telemetry."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.truth_surface.contract import (  # noqa: E402
    TrustedContextSnapshot,
    TrustedDomain,
    TrustedFact,
    TruthSource,
)
from modules.ai.brain.truth_surface.model_payload_attestation import (  # noqa: E402
    assert_attestation_redacted,
    build_model_payload_attestation,
    candidate_ids_and_order_from_sources,
    facts_loaded_from_snapshot,
    facts_reaching_brain_from_projection,
    facts_reaching_compose_from_known_facts,
    selected_product_and_variant_ids,
    slim_compose_payload_fingerprint,
)
from modules.ai.brain.truth_surface.trusted_context_brain_consumption_gate import (  # noqa: E402
    safe_trusted_context_brain_projection_trace_metadata,
)
from modules.ai.brain.truth_surface.trusted_context_brain_projection import (  # noqa: E402
    project_trusted_context_brain_facts,
)

_PHONE = "966500000099"
_TENANT = 9001
_CONV = 42


def _catalog_snapshot() -> TrustedContextSnapshot:
    facts = [
        TrustedFact(
            domain=TrustedDomain.CATALOG,
            key="product_id",
            value=501,
            source=TruthSource.PRODUCTS_TABLE,
        ),
        TrustedFact(
            domain=TrustedDomain.CATALOG,
            key="title",
            value="حذاء رياضي أبيض",
            source=TruthSource.PRODUCTS_TABLE,
        ),
        TrustedFact(
            domain=TrustedDomain.CATALOG,
            key="price",
            value="199.00",
            source=TruthSource.PRODUCTS_TABLE,
        ),
        TrustedFact(
            domain=TrustedDomain.CATALOG,
            key="available",
            value=True,
            source=TruthSource.PRODUCTS_TABLE,
        ),
        TrustedFact(
            domain=TrustedDomain.CATALOG,
            key="product_candidates",
            value=[
                {"ref": 1, "product_id": 711, "title": "قميص قطني أزرق", "price": "80"},
                {"ref": 2, "product_id": 712, "title": "عطر ورد 100ml", "price": "149"},
            ],
            source=TruthSource.ORDER_PREPARATION_STATE,
        ),
        TrustedFact(
            domain=TrustedDomain.ORDER,
            key="external_id",
            value="RRRD1234",
            source=TruthSource.ORDER_PREPARATION_STATE,
        ),
    ]
    snap = TrustedContextSnapshot(
        tenant_id=_TENANT,
        customer_phone=_PHONE,
        conversation_id=_CONV,
        facts=facts,
        loaded_domains=["catalog", "order"],
    )
    snap.ensure_snapshot_id()
    return snap


def _projection(snapshot: TrustedContextSnapshot) -> dict:
    return project_trusted_context_brain_facts(
        snapshot=snapshot,
        tenant_id=_TENANT,
        customer_phone=_PHONE,
        conversation_id=_CONV,
    )


def test_facts_loaded_redacted_counts_only() -> None:
    snap = _catalog_snapshot()
    loaded = facts_loaded_from_snapshot(snap)
    assert loaded["present"] is True
    assert loaded["loaded_domains"] == ["catalog", "order"]
    assert loaded["domain_fact_counts"]["catalog"] == 5
    assert loaded["domain_fact_counts"]["order"] == 1
    assert loaded["facts_snapshot_id"] == snap.snapshot_id
    assert "title" not in loaded
    assert_attestation_redacted({"facts_loaded": loaded})


def test_facts_reaching_brain_ids_only() -> None:
    projection = _projection(_catalog_snapshot())
    brain = facts_reaching_brain_from_projection(projection)
    assert brain["present"] is True
    assert brain["product_id"] == 501
    assert brain["candidate_count"] == 2
    assert "product_identity" not in brain
    assert "title" not in brain
    assert_attestation_redacted({"facts_reaching_brain": brain})


def test_candidate_ids_and_order_strips_prose() -> None:
    projection = _projection(_catalog_snapshot())
    rows = candidate_ids_and_order_from_sources(projection=projection)
    assert rows == [
        {"ref": 1, "product_id": 711},
        {"ref": 2, "product_id": 712},
    ]
    assert all("title" not in row for row in rows)
    assert all("price" not in row for row in rows)


def test_selected_product_and_variant_ids_only() -> None:
    selected = selected_product_and_variant_ids(
        {"product_id": 501, "title": "حذاء رياضي أبيض", "price": "199.00"},
    )
    assert selected == {"present": True, "product_id": 501}


def test_facts_reaching_compose_known_facts_keys_only() -> None:
    projection = _projection(_catalog_snapshot())
    known_facts = {
        "store_name": "متجر تجريبي عام",
        "trusted_context_projection": projection,
    }
    compose = facts_reaching_compose_from_known_facts(known_facts)
    assert compose["trusted_context_projection_present"] is True
    assert compose["projection_product_id"] == 501
    assert "store_name" not in compose
    assert "trusted_context_projection" not in compose
    assert_attestation_redacted({"facts_reaching_compose": compose})


def test_build_model_payload_attestation_unified_shape() -> None:
    snap = _catalog_snapshot()
    projection = _projection(snap)
    attestation = build_model_payload_attestation(
        stage="reply_state",
        snapshot=snap,
        brain_projection=projection,
        known_facts={"trusted_context_projection": projection},
        selected_product={"product_id": 501, "title": "حذاء رياضي أبيض"},
        history=[{"body": "هل الحذاء متوفر؟", "direction": "in"}],
        recent_turns=["customer: هل الحذاء متوفر؟"],
        decision_action="llm_reply",
        result_data={"chosen_path": "catalog_product"},
    )
    assert attestation["stage"] == "reply_state"
    assert set(attestation.keys()) >= {
        "facts_loaded",
        "facts_reaching_brain",
        "facts_reaching_compose",
        "candidate_ids_and_order",
        "selected_product_and_variant",
        "history_window",
        "tool_results_used",
        "model_and_route",
    }
    assert attestation["facts_loaded"]["fact_count"] == 6
    assert attestation["candidate_ids_and_order"][0]["product_id"] == 711
    assert attestation["selected_product_and_variant"]["product_id"] == 501
    assert attestation["history_window"]["history_message_count"] == 1
    assert attestation["tool_results_used"]["chosen_path"] == "catalog_product"
    assert_attestation_redacted(attestation)


def test_slim_compose_fingerprint_projection_ids_only() -> None:
    projection = _projection(_catalog_snapshot())
    slim = {
        "stage": "browsing",
        "selected_product": {"product_id": 501, "title": "حذاء رياضي أبيض"},
        "known_facts": {"trusted_context_projection": projection},
    }
    fingerprint = slim_compose_payload_fingerprint(slim)
    assert fingerprint["projection_product_id"] == 501
    assert fingerprint["projection_candidate_count"] == 2
    assert fingerprint["selected_product"]["product_id"] == 501
    assert "title" not in fingerprint
    assert_attestation_redacted({"slim_compose_fingerprint": fingerprint})


def test_safe_brain_projection_trace_metadata_uses_unified_brain_facts() -> None:
    projection = _projection(_catalog_snapshot())
    meta = safe_trusted_context_brain_projection_trace_metadata(projection)
    assert meta["status"] == "ok"
    assert meta["product_id"] == 501
    assert meta["product_candidate_count"] == 2
    assert meta["facts_reaching_brain"]["present"] is True
    assert "title" not in meta
    assert_attestation_redacted(meta)


def test_assert_attestation_redacted_rejects_forbidden_keys() -> None:
    with pytest.raises(ValueError, match="forbidden keys"):
        assert_attestation_redacted({"title": "حذاء رياضي أبيض"})
