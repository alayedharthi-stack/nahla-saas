"""Subject-aware availability grounding for Commerce Agent V2 outputs."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.guardrails import (
    grounded_output_guardrail,
    validate_grounded_reply,
)
from modules.ai.commerce_agent_v2.output import (
    CanonicalEvidenceFact,
    CommerceReply,
    EvidenceRecord,
    FactClaim,
)


PRODUCT_ID = 501
ORDER_ID = 9001
PRODUCT_REF = "catalog:product:501"
SHIPMENT_REF = "order:shipment:9001"


def _context() -> CommerceAgentContext:
    return CommerceAgentContext(
        tenant_id=1,
        tenant_name="متجر تجريبي عام",
        conversation_id=10,
        customer_id=20,
        normalized_customer_phone="+966500000001",
        connection_id="30",
        inbound_trace_id="availability-scope-test",
    )


def _product_context(*, availability: bool) -> CommerceAgentContext:
    context = _context()
    context.register_evidence(
        [
            EvidenceRecord(
                ref=PRODUCT_REF,
                source="catalog_product",
                source_id=str(PRODUCT_ID),
                facts=[
                    CanonicalEvidenceFact(
                        kind="product_name",
                        value="حذاء رياضي أبيض",
                        subject_product_id=PRODUCT_ID,
                    ),
                    CanonicalEvidenceFact(
                        kind="availability",
                        value=availability,
                        subject_product_id=PRODUCT_ID,
                    ),
                    CanonicalEvidenceFact(
                        kind="stock_quantity",
                        value=5,
                        subject_product_id=PRODUCT_ID,
                    ),
                ],
            )
        ]
    )
    return context


def _shipment_context() -> CommerceAgentContext:
    context = _context()
    context.register_evidence(
        [
            EvidenceRecord(
                ref=SHIPMENT_REF,
                source="order_shipment",
                source_id=str(ORDER_ID),
                facts=[
                    CanonicalEvidenceFact(
                        kind="shipment_status",
                        value="in_transit",
                        subject_order_id=ORDER_ID,
                    ),
                    CanonicalEvidenceFact(
                        kind="shipment_status_label",
                        value="في الطريق",
                        subject_order_id=ORDER_ID,
                    ),
                    CanonicalEvidenceFact(
                        kind="carrier",
                        value="شركة شحن تجريبية",
                        subject_order_id=ORDER_ID,
                    ),
                    CanonicalEvidenceFact(
                        kind="tracking_number",
                        value="TEST-TRACKING-VALUE",
                        subject_order_id=ORDER_ID,
                    ),
                ],
            )
        ]
    )
    return context


def _grounded_availability_reply(text: str, *, availability: bool) -> CommerceReply:
    return CommerceReply(
        text=text,
        evidence_refs=[PRODUCT_REF],
        fact_claims=[
            FactClaim(
                kind="availability",
                value=availability,
                evidence_ref=PRODUCT_REF,
                subject_product_id=PRODUCT_ID,
                text_span=text,
            )
        ],
    )


def _shipment_reply(clause: str) -> CommerceReply:
    status_span = "في الطريق"
    return CommerceReply(
        text=f"{clause}. حالة الشحنة: {status_span}.",
        evidence_refs=[SHIPMENT_REF],
        fact_claims=[
            FactClaim(
                kind="shipment_status",
                value="in_transit",
                evidence_ref=SHIPMENT_REF,
                subject_order_id=ORDER_ID,
                text_span=status_span,
            )
        ],
    )


@pytest.mark.parametrize(
    ("text", "availability"),
    [
        ("المنتج متوفر", True),
        ("المنتج غير متوفر", False),
        ("المنتج متاح للطلب", True),
        ("المنتج في المخزون", True),
        ("نفد مخزون المنتج", False),
        ("The product is available", True),
        ("The product is in stock", True),
        ("The product is out of stock", False),
    ],
)
def test_grounded_product_availability_expressions_pass(
    text: str,
    availability: bool,
) -> None:
    context = _product_context(availability=availability)
    assert validate_grounded_reply(
        context,
        _grounded_availability_reply(text, availability=availability),
    ) == []


@pytest.mark.parametrize(
    "text",
    [
        "المنتج متوفر",
        "هذا المنتج موجود",
        "متاح للطلب",
        "في المخزون",
        "باقي 5 حبات",
        "غير متوفر",
        "نفد المنتج",
        "The product is available",
        "The product is in stock",
        "The product is out of stock",
    ],
)
def test_ungrounded_product_availability_expressions_fail(text: str) -> None:
    errors = validate_grounded_reply(_context(), CommerceReply(text=text))
    expected = (
        "stock_quantity_in_text_without_verified_claim"
        if "5" in text
        else "availability_in_text_without_verified_claim"
    )
    assert expected in errors


@pytest.mark.parametrize(
    "clause",
    [
        "معلومات الشحنة متوفرة",
        "رقم التتبع متاح",
        "الناقل موجود",
        "بيانات التتبع غير متوفرة",
        "تفاصيل الطلب متاحة",
        "معلومات الطلب موجودة",
        "البيانات المتاحة للطلب هي تفاصيل الشحنة",
        "معلومات الشحن متوفرة الحين",
        "رقم التتبع موجود عندنا",
        "Shipment information is available",
        "The tracking number is available",
        "The carrier is available",
        "Order details are available",
    ],
)
def test_order_and_shipment_language_is_not_product_stock(clause: str) -> None:
    assert validate_grounded_reply(_shipment_context(), _shipment_reply(clause)) == []


def test_grounded_merchant_information_availability_is_not_product_stock() -> None:
    context = _context()
    ref = "merchant:knowledge:gift-wrap"
    body = "خدمة التغليف متاحة خلال ساعات العمل"
    context.register_evidence(
        [
            EvidenceRecord(
                ref=ref,
                source="merchant_knowledge",
                source_id="gift-wrap",
                facts=[CanonicalEvidenceFact(kind="merchant_knowledge", value=body)],
                fields={"title": "خدمة التغليف", "body": body},
            )
        ]
    )
    reply = CommerceReply(
        text=body,
        evidence_refs=[ref],
        fact_claims=[
            FactClaim(
                kind="merchant_knowledge",
                value=body,
                evidence_ref=ref,
                text_span=body,
            )
        ],
    )
    assert validate_grounded_reply(context, reply) == []


def test_mixed_clauses_validate_product_and_tracking_independently() -> None:
    context = _product_context(availability=True)
    shipment = _shipment_context().evidence[SHIPMENT_REF]
    context.register_evidence([shipment])
    text = "المنتج متوفر، لكن رقم التتبع غير متاح. حالة الشحنة: في الطريق."
    reply = CommerceReply(
        text=text,
        evidence_refs=[PRODUCT_REF, SHIPMENT_REF],
        fact_claims=[
            FactClaim(
                kind="availability",
                value=True,
                evidence_ref=PRODUCT_REF,
                subject_product_id=PRODUCT_ID,
                text_span="المنتج متوفر",
            ),
            FactClaim(
                kind="shipment_status",
                value="in_transit",
                evidence_ref=SHIPMENT_REF,
                subject_order_id=ORDER_ID,
                text_span="في الطريق",
            ),
        ],
    )
    assert validate_grounded_reply(context, reply) == []

    missing_product_claim = reply.model_copy(
        update={"fact_claims": reply.fact_claims[1:]}
    )
    assert "availability_in_text_without_verified_claim" in validate_grounded_reply(
        context,
        missing_product_claim,
    )


def test_mixed_subjects_without_punctuation_bind_each_mention_independently() -> None:
    context = _product_context(availability=True)
    context.register_evidence([_shipment_context().evidence[SHIPMENT_REF]])
    text = "المنتج متوفر ورقم التتبع متاح وحالة الشحنة في الطريق"
    reply = CommerceReply(
        text=text,
        evidence_refs=[PRODUCT_REF, SHIPMENT_REF],
        fact_claims=[
            FactClaim(
                kind="availability",
                value=True,
                evidence_ref=PRODUCT_REF,
                subject_product_id=PRODUCT_ID,
                text_span="المنتج متوفر",
            ),
            FactClaim(
                kind="shipment_status",
                value="in_transit",
                evidence_ref=SHIPMENT_REF,
                subject_order_id=ORDER_ID,
                text_span="في الطريق",
            ),
        ],
    )
    assert validate_grounded_reply(context, reply) == []

    ungrounded_product = reply.model_copy(update={"fact_claims": reply.fact_claims[1:]})
    assert "availability_in_text_without_verified_claim" in validate_grounded_reply(
        context,
        ungrounded_product,
    )


def test_quantity_bound_product_availability_remains_strict() -> None:
    context = _product_context(availability=True)
    reply = CommerceReply(
        text="المنتج متوفر، وباقي 5 حبات.",
        evidence_refs=[PRODUCT_REF],
        fact_claims=[
            FactClaim(
                kind="availability",
                value=True,
                evidence_ref=PRODUCT_REF,
                subject_product_id=PRODUCT_ID,
                text_span="المنتج متوفر",
            ),
            FactClaim(
                kind="stock_quantity",
                value=5,
                evidence_ref=PRODUCT_REF,
                subject_product_id=PRODUCT_ID,
                text_span="5 حبات",
            ),
        ],
    )
    assert validate_grounded_reply(context, reply) == []

    wrong_subject = reply.model_copy(
        update={
            "fact_claims": [
                claim.model_copy(update={"subject_product_id": PRODUCT_ID + 1})
                for claim in reply.fact_claims
            ]
        }
    )
    errors = validate_grounded_reply(context, wrong_subject)
    assert "claim_not_in_evidence:availability" in errors
    assert "claim_not_in_evidence:stock_quantity" in errors
    assert "availability_in_text_without_verified_claim" in errors


@pytest.mark.asyncio
async def test_rejected_availability_output_persists_only_safe_semantic_metadata() -> None:
    context = _product_context(availability=True)
    rejected_text = "المنتج غير متوفر"
    reply = CommerceReply(text=rejected_text, evidence_refs=[PRODUCT_REF])

    result = await grounded_output_guardrail.guardrail_function(
        SimpleNamespace(context=context),
        None,
        reply,
    )

    assert result.tripwire_triggered is True
    diagnostic = result.output_info["rejected_output_diagnostic"]
    lexical = diagnostic["lexical_commercial_diagnostics"]
    assert len(lexical) == 1
    assert lexical[0] == {
        "detector_type": "product_availability_semantic_scope",
        "matched_normalized_lexeme": "غير متوفر",
        "semantic_scope": "PRODUCT",
        "strong_stock_expression": False,
        "referenced_evidence_source_types": ["catalog_product"],
        "referenced_claim_kinds": [],
        "verified_claim_kinds": [],
        "product_subject_present": True,
        "order_subject_present": False,
        "clause_sha256": lexical[0]["clause_sha256"],
        "guardrail_code": "availability_in_text_without_verified_claim",
    }
    assert len(lexical[0]["clause_sha256"]) == 64
    persisted_shape = json.dumps(diagnostic, ensure_ascii=False)
    assert rejected_text not in persisted_shape
    assert "+966500000001" not in persisted_shape
    assert "TEST-TRACKING-VALUE" not in persisted_shape
