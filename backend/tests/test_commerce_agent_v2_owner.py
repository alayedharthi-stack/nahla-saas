from __future__ import annotations

from pathlib import Path

from modules.ai.commerce_agent_v2.delivery import build_commerce_delivery_plan
from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.output import (
    CanonicalEvidenceFact,
    CommerceReply,
    EvidenceRecord,
    MediaReference,
    ProductReference,
    UIAction,
)


def test_outbound_gate_requires_every_explicit_tenant_gate(monkeypatch) -> None:
    import modules.ai.commerce_agent_v2.ownership as ownership

    monkeypatch.setattr(ownership, "COMMERCE_AGENT_V2_ENABLED", True)
    monkeypatch.setattr(ownership, "COMMERCE_AGENT_V2_SHADOW_ONLY", False)
    monkeypatch.setattr(ownership, "COMMERCE_AGENT_V2_KILL_SWITCH", False)
    monkeypatch.setattr(ownership, "COMMERCE_AGENT_V2_TENANT_IDS", {7, 8})
    monkeypatch.setattr(ownership, "COMMERCE_AGENT_V2_OUTBOUND_TENANT_IDS", {7})

    assert ownership.outbound_enabled_for_tenant(7)
    assert not ownership.outbound_enabled_for_tenant(8)
    monkeypatch.setattr(ownership, "COMMERCE_AGENT_V2_SHADOW_ONLY", True)
    assert not ownership.outbound_enabled_for_tenant(7)
    monkeypatch.setattr(ownership, "COMMERCE_AGENT_V2_SHADOW_ONLY", False)
    monkeypatch.setattr(ownership, "COMMERCE_AGENT_V2_KILL_SWITCH", True)
    assert not ownership.outbound_enabled_for_tenant(7)


def test_structured_delivery_uses_only_grounded_fields_without_legacy_markers() -> None:
    evidence = EvidenceRecord(
        ref="catalog:product:17",
        source="catalog_product",
        source_id="17",
        fields={
            "product_id": 17,
            "external_id": "SKU-17",
            "title": "منتج موثق",
            "price": "25",
            "currency": "SAR",
            "image_url": "https://example.test/product.jpg",
            "product_url": "https://example.test/product",
        },
        facts=[
            CanonicalEvidenceFact(
                kind="image_url",
                value="https://example.test/product.jpg",
                subject_product_id=17,
            ),
            CanonicalEvidenceFact(
                kind="product_url",
                value="https://example.test/product",
                subject_product_id=17,
            ),
        ],
    )
    reply = CommerceReply(
        text="هذا المنتج الموثق.",
        evidence_refs=[evidence.ref],
        product_refs=[ProductReference(product_id=17, evidence_ref=evidence.ref)],
        media_refs=[
            MediaReference(url="https://example.test/product.jpg", evidence_ref=evidence.ref)
        ],
        ui_actions=[
            UIAction(
                kind="open_product",
                label="عرض المنتج",
                url="https://example.test/product",
                evidence_ref=evidence.ref,
            )
        ],
    )

    plan = build_commerce_delivery_plan(reply, {evidence.ref: evidence})

    assert [action.kind for action in plan] == ["text", "product", "ui_action"]
    serialized = repr(plan)
    assert "[PRODUCT:" not in serialized
    assert "[MEDIA_KEY:" not in serialized
    assert "[CALL:" not in serialized


def test_missing_presentation_evidence_fails_closed_to_grounded_text() -> None:
    reply = CommerceReply(
        text="لا تتوفر لدي صورة موثوقة.",
        product_refs=[ProductReference(product_id=17, evidence_ref="catalog:product:17")],
    )

    plan = build_commerce_delivery_plan(reply, {})

    assert plan[0].kind == "text"
    assert plan[1].kind == "unsupported"
    assert plan[1].payload["reason"] == "missing_grounded_product_evidence"


def test_customer_name_is_not_exposed_through_inherited_v1_history() -> None:
    context = CommerceAgentContext(
        tenant_id=1,
        conversation_id=2,
        customer_id=3,
        normalized_customer_phone="0500000000",
        connection_id="4",
        inbound_trace_id="trace-test",
    )
    context._verified_customer_name = "أحمد سالم"

    redacted = context.redact_unexposed_customer_identity(
        "إيه، اسمك أحمد سالم. والمنتج قميص قطني."
    )

    assert "أحمد سالم" not in redacted
    assert "[redacted-customer-name]" in redacted
    assert "قميص قطني" in redacted


def test_v2_owner_precedes_legacy_owners_and_returns_without_v1_fallback() -> None:
    source = (Path(__file__).parents[1] / "routers" / "whatsapp_webhook.py").read_text(
        encoding="utf-8"
    )
    owner_gate = source.index("if not _skip and _v2_outbound_enabled(int(tenant_id)):")
    order_flow = source.index("# ── OrderFlowV2 deterministic checkout owner")
    brain = source.index("_turn_eval = await evaluate_live_merchant_brain_turn")

    assert owner_gate < order_flow < brain
    owner_block = source[owner_gate:order_flow]
    assert "silent_v1_fallback=false" in owner_block
    assert "return" in owner_block
    assert "evaluate_live_merchant_brain_turn" not in owner_block
    assert "try_handle_order_flow_v2" not in owner_block
