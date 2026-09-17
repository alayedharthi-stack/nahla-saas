"""Fused merchant descriptions must not enter Commerce V2 evidence.

Phase 2.7A run 1 (production, synthetic Tenant 1, turn A3 "أبغى تفاصيل أول
منتج عندكم") halted on ``output_guardrail_tripwire:claim_span_not_equivalent:
description``: Salla returned product 23's size chart flattened without any
separators, the model rendered it faithfully with spaces, and the grounding
guardrail — correctly — could not match the rendered numbers against the
fused source. The customer received only the safe fallback. These tests pin
both halves: the guardrail behaviour that makes such text uncitable, and the
catalog evidence gate that keeps it out of the model's evidence.
"""
from __future__ import annotations

from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.guardrails import validate_grounded_reply
from modules.ai.commerce_agent_v2.output import (
    CanonicalEvidenceFact,
    CommerceReply,
    EvidenceRecord,
    FactClaim,
)
from modules.ai.commerce_agent_v2.tools.catalog import _product_evidence

DESCRIPTION_WITHHELD_FUSED_NUMERIC_TEXT = "fused_numeric_text"

# Exact stored description of Tenant 1 product 23 (raw Salla payload, no
# HTML, 90 characters): header cells and numeric cells fused end to end.
FUSED_SIZE_CHART = (
    "المقاس36نصف محيط الخصر19.5الطول125الكم11.53820.512511.54021.5125124222.5125124423.512512.5"
)
# Products 21 and 24 from the same catalog (referenced by Customer B's seed history).
FUSED_SIZE_CHART_2 = "المقاس36الطول130الكم28.53813028.5401302942130294413029.5"
FUSED_SIZE_CHART_3 = "المقاس36نصف محيط الخصر12.5الطول1003813.51004014.51004215.51004416.5100"


def _row(description: str) -> dict[str, object]:
    return {
        "id": 23,
        "external_id": "398551325",
        "title": "فستان",
        "description": description,
        "price": 289.0,
        "sale_price": None,
        "regular_price": None,
        "currency": "SAR",
        "in_stock": True,
        "stock_qty": 6,
        "image_url": "https://cdn.example.test/dress.jpg",
        "product_url": "https://store.example.test/dress/p398551325",
        "orderable": True,
    }


def _context() -> CommerceAgentContext:
    return CommerceAgentContext(
        tenant_id=1,
        conversation_id=10308,
        customer_id=None,
        normalized_customer_phone="internal-e2e-a",
        connection_id="internal_e2e",
        inbound_trace_id="trace-a3",
    )


def test_faithful_rendering_of_fused_size_chart_cannot_pass_the_guardrail() -> None:
    """Documents why the text is uncitable: the guardrail is right to reject it."""
    ref = "catalog:product:23"
    context = _context()
    context.authorize_products([23])
    context.register_evidence(
        [
            EvidenceRecord(
                ref=ref,
                source="catalog_product",
                source_id="23",
                facts=[
                    CanonicalEvidenceFact(kind="product_name", value="فستان", subject_product_id=23),
                    CanonicalEvidenceFact(
                        kind="description", value=FUSED_SIZE_CHART, subject_product_id=23
                    ),
                ],
                fields={"title": "فستان", "description": FUSED_SIZE_CHART},
            )
        ]
    )
    rendered = "المقاس 36: نصف محيط الخصر 19.5، الطول 125، الكم 11.5"
    reply = CommerceReply(
        text=f"أول منتج هو فستان. {rendered}.",
        evidence_refs=[ref],
        fact_claims=[
            FactClaim(
                kind="product_name",
                value="فستان",
                evidence_ref=ref,
                subject_product_id=23,
                text_span="فستان",
            ),
            FactClaim(
                kind="description",
                value=FUSED_SIZE_CHART,
                evidence_ref=ref,
                subject_product_id=23,
                text_span=rendered,
            ),
        ],
    )
    assert "claim_span_not_equivalent:description" in validate_grounded_reply(context, reply)


def test_fused_size_chart_is_withheld_from_catalog_evidence() -> None:
    snapshot, record = _product_evidence(_row(FUSED_SIZE_CHART))

    assert snapshot.description == ""
    assert record.fields["description"] == ""
    assert [fact.kind for fact in record.facts] == [
        "product_name",
        "price",
        "currency",
        "availability",
        "stock_quantity",
        "image_url",
        "product_url",
    ]
    assert record.provenance["description_withheld"] == DESCRIPTION_WITHHELD_FUSED_NUMERIC_TEXT
    # Every verifiable fact is untouched.
    assert snapshot.title == "فستان"
    assert snapshot.price == "289.0"
    assert snapshot.in_stock is True
    assert snapshot.stock_quantity == 6


def test_other_synthetic_catalog_size_charts_are_withheld_too() -> None:
    from modules.ai.commerce_agent_v2.tools.catalog import citable_description

    for text in (FUSED_SIZE_CHART_2, FUSED_SIZE_CHART_3):
        assert citable_description(text) == ("", DESCRIPTION_WITHHELD_FUSED_NUMERIC_TEXT)


def test_ordinary_descriptions_are_kept_verbatim() -> None:
    from modules.ai.commerce_agent_v2.tools.catalog import citable_description

    kept = (
        "",
        "فستان صيفي بأكمام قصيرة، خامة قطن 100%",
        "المقاس 36، الطول 130، الكم 28.5",  # separated numbers stay verifiable
        "المقاس36 الطول130 الكم28.5",  # letter-digit fusion alone is still verifiable
        "يدعم بلوتوث 5.0.1 وشحن سريع",  # dotted identifier in spaced prose
        "Bluetooth 5.0.1 wireless speaker, 10W",
        "1,2,3 مقاسات متاحة",  # no Arabic letter fused to a digit
    )
    for text in kept:
        assert citable_description(text) == (text, None), text
    snapshot, record = _product_evidence(_row("المقاس 36، الطول 130، الكم 28.5"))
    assert snapshot.description == "المقاس 36، الطول 130، الكم 28.5"
    assert any(fact.kind == "description" for fact in record.facts)
    assert "description_withheld" not in record.provenance


def test_grounded_reply_on_the_remaining_facts_passes_and_description_claims_do_not() -> None:
    """After the gate the A3-style turn can be answered from verifiable facts."""
    snapshot, record = _product_evidence(_row(FUSED_SIZE_CHART))
    context = _context()
    context.authorize_products([snapshot.product_id])
    context.register_evidence([record])
    ref = record.ref

    grounded = CommerceReply(
        text="أول منتج عندنا هو فستان، سعره 289 ريال، ومتوفر.",
        evidence_refs=[ref],
        fact_claims=[
            FactClaim(
                kind="product_name",
                value="فستان",
                evidence_ref=ref,
                subject_product_id=23,
                text_span="فستان",
            ),
            FactClaim(
                kind="price",
                value=289,
                evidence_ref=ref,
                subject_product_id=23,
                text_span="سعره 289 ريال",
            ),
            FactClaim(
                kind="currency",
                value="ريال",
                evidence_ref=ref,
                subject_product_id=23,
                text_span="289 ريال",
            ),
            FactClaim(
                kind="availability",
                value=True,
                evidence_ref=ref,
                subject_product_id=23,
                text_span="متوفر",
            ),
        ],
    )
    assert validate_grounded_reply(context, grounded) == []

    # The withheld text is not evidence any more, so a description claim built
    # on it is rejected as not-in-evidence rather than silently accepted.
    with_description = grounded.model_copy(
        update={
            "text": grounded.text + " المقاس 36: نصف محيط الخصر 19.5.",
            "fact_claims": [
                *grounded.fact_claims,
                FactClaim(
                    kind="description",
                    value=FUSED_SIZE_CHART,
                    evidence_ref=ref,
                    subject_product_id=23,
                    text_span="المقاس 36: نصف محيط الخصر 19.5",
                ),
            ],
        }
    )
    assert "claim_not_in_evidence:description" in validate_grounded_reply(
        context, with_description
    )
