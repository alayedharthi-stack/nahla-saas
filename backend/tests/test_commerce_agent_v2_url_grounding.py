"""Contract tests for fail-closed Commerce V2 URL grounding."""
from __future__ import annotations

import pytest

from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.guardrails import validate_grounded_reply
from modules.ai.commerce_agent_v2.output import (
    CanonicalEvidenceFact,
    CommerceReply,
    EvidenceRecord,
    FactClaim,
    MediaReference,
    UIAction,
)
from modules.ai.commerce_agent_v2.url_grounding import (
    canonical_http_url,
    canonical_http_url_equal,
)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://example.com/x", "https://example.com/x"),
        (
            "https://demostore.salla.sa/dev/فستان/p398551325",
            "https://demostore.salla.sa/dev/%D9%81%D8%B3%D8%AA%D8%A7%D9%86/p398551325",
        ),
        ("https://example.com/%d9%81", "https://example.com/%D9%81"),
        ("https://example.com", "https://example.com/"),
        ("https://EXAMPLE.com:443/x", "https://example.com/x"),
        ("https://bücher.example/x", "https://xn--bcher-kva.example/x"),
        ("https://faß.de/x", "https://xn--fa-hia.de/x"),
    ],
)
def test_canonical_http_url_equal_accepts_only_safe_representation_changes(
    left: str,
    right: str,
) -> None:
    assert canonical_http_url_equal(left, right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://example.com/x", "https://other.example/x"),
        ("http://example.com/x", "https://example.com/x"),
        ("https://example.com:444/x", "https://example.com/x"),
        ("https://example.com/a", "https://example.com/b"),
        ("https://example.com/path", "https://example.com/path/"),
        ("https://example.com/x?a=1", "https://example.com/x?a=2"),
        ("https://example.com/x?a=1", "https://example.com/x"),
        ("https://example.com/x?a=1", "https://example.com/x?a=1&b=2"),
        ("https://example.com/a%2Fb", "https://example.com/a/b"),
        ("https://example.com/x#one", "https://example.com/x#two"),
        ("https://faß.de/x", "https://fass.de/x"),
    ],
)
def test_canonical_http_url_equal_preserves_resource_semantics(
    left: str,
    right: str,
) -> None:
    assert not canonical_http_url_equal(left, right)


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/%",
        "https://example.com/%2",
        "https://example.com/%GG",
        " https://example.com/x",
        "https://example.com/x ",
        "ftp://example.com/x",
    ],
)
def test_canonical_http_url_rejects_malformed_or_non_http_values(value: str) -> None:
    assert canonical_http_url(value) is None


def _context_with_evidence(record: EvidenceRecord) -> CommerceAgentContext:
    context = CommerceAgentContext(
        tenant_id=1,
        conversation_id=1,
        customer_id=1,
        normalized_customer_phone="+966500000001",
        connection_id="1",
        inbound_trace_id="url-grounding-test",
    )
    context.register_evidence([record])
    return context


def test_product_url_action_only_accepts_canonical_equivalence_with_strict_binding() -> None:
    ref = "catalog:product:17"
    raw_url = "https://demostore.salla.sa/dev/فستان/p398551325"
    context = _context_with_evidence(
        EvidenceRecord(
            ref=ref,
            source="catalog_product",
            source_id="17",
            facts=[
                CanonicalEvidenceFact(
                    kind="product_url",
                    value=raw_url,
                    subject_product_id=17,
                )
            ],
        )
    )
    reply = CommerceReply(
        text="افتح صفحة المنتج من الزر.",
        evidence_refs=[ref],
        fact_claims=[
            FactClaim(
                kind="product_url",
                value=raw_url,
                evidence_ref=ref,
                subject_product_id=17,
                text_span=None,
            )
        ],
        ui_actions=[
            UIAction(kind="open_product", label="عرض المنتج", url=raw_url, evidence_ref=ref)
        ],
    )
    assert validate_grounded_reply(context, reply) == []


def test_product_url_action_accepts_direct_subject_bound_evidence_without_duplicate_claim() -> None:
    ref = "catalog:product:17"
    evidence_url = "https://demostore.salla.sa/dev/فستان/p398551325"
    rendered_url = "https://demostore.salla.sa/dev/%D9%81%D8%B3%D8%AA%D8%A7%D9%86/p398551325"
    context = _context_with_evidence(
        EvidenceRecord(
            ref=ref,
            source="catalog_product",
            source_id="17",
            facts=[
                CanonicalEvidenceFact(
                    kind="product_url",
                    value=evidence_url,
                    subject_product_id=17,
                )
            ],
        )
    )
    reply = CommerceReply(
        text="افتح صفحة المنتج من الزر.",
        evidence_refs=[ref],
        ui_actions=[
            UIAction(kind="open_product", label="عرض المنتج", url=rendered_url, evidence_ref=ref)
        ],
    )

    assert validate_grounded_reply(context, reply) == []


def test_product_url_action_rejects_direct_evidence_with_wrong_subject_binding() -> None:
    ref = "catalog:product:17"
    url = "https://demostore.salla.sa/products/17"
    context = _context_with_evidence(
        EvidenceRecord(
            ref=ref,
            source="catalog_product",
            source_id="17",
            facts=[
                CanonicalEvidenceFact(
                    kind="product_url",
                    value=url,
                    subject_product_id=18,
                )
            ],
        )
    )
    reply = CommerceReply(
        text="افتح صفحة المنتج من الزر.",
        evidence_refs=[ref],
        ui_actions=[UIAction(kind="open_product", label="عرض المنتج", url=url, evidence_ref=ref)],
    )

    assert "action_url_not_in_evidence" in validate_grounded_reply(context, reply)


def test_image_url_media_accepts_canonical_equivalence() -> None:
    ref = "catalog:product:17"
    raw_url = "https://cdn.example.com/صور/فستان.jpg"
    context = _context_with_evidence(
        EvidenceRecord(
            ref=ref,
            source="catalog_product",
            source_id="17",
            facts=[
                CanonicalEvidenceFact(
                    kind="image_url",
                    value=raw_url,
                    subject_product_id=17,
                )
            ],
        )
    )
    reply = CommerceReply(
        text="هذه صورة المنتج.",
        evidence_refs=[ref],
        fact_claims=[
            FactClaim(
                kind="image_url",
                value=raw_url,
                evidence_ref=ref,
                subject_product_id=17,
                text_span=None,
            )
        ],
        media_refs=[MediaReference(url=raw_url, evidence_ref=ref)],
    )
    assert validate_grounded_reply(context, reply) == []


def test_image_media_accepts_direct_subject_bound_catalog_evidence() -> None:
    ref = "catalog:product:17"
    image_url = "https://cdn.example.com/images/product-17.jpg"
    context = _context_with_evidence(
        EvidenceRecord(
            ref=ref,
            source="catalog_product",
            source_id="17",
            facts=[
                CanonicalEvidenceFact(
                    kind="image_url",
                    value=image_url,
                    subject_product_id=17,
                )
            ],
        )
    )
    reply = CommerceReply(
        text="هذه صورة المنتج.",
        evidence_refs=[ref],
        media_refs=[MediaReference(url=image_url, evidence_ref=ref)],
    )

    assert validate_grounded_reply(context, reply) == []


def test_image_media_rejects_mismatched_catalog_subject() -> None:
    ref = "catalog:product:17"
    image_url = "https://cdn.example.com/images/product-18.jpg"
    context = _context_with_evidence(
        EvidenceRecord(
            ref=ref,
            source="catalog_product",
            source_id="17",
            facts=[
                CanonicalEvidenceFact(
                    kind="image_url",
                    value=image_url,
                    subject_product_id=18,
                )
            ],
        )
    )
    reply = CommerceReply(
        text="هذه صورة المنتج.",
        evidence_refs=[ref],
        media_refs=[MediaReference(url=image_url, evidence_ref=ref)],
    )

    assert "media_url_not_in_evidence" in validate_grounded_reply(context, reply)


def test_tracking_url_action_accepts_canonical_equivalence() -> None:
    ref = "order:shipment:701"
    raw_url = "https://carrier.example.com/تتبع/ABC123"
    context = _context_with_evidence(
        EvidenceRecord(
            ref=ref,
            source="order_shipment",
            source_id="701",
            facts=[
                CanonicalEvidenceFact(
                    kind="tracking_url",
                    value=raw_url,
                    subject_order_id=701,
                )
            ],
        )
    )
    reply = CommerceReply(
        text="تتبع الشحنة من الزر.",
        evidence_refs=[ref],
        fact_claims=[
            FactClaim(
                kind="tracking_url",
                value=raw_url,
                evidence_ref=ref,
                subject_order_id=701,
                text_span=None,
            )
        ],
        ui_actions=[
            UIAction(kind="track_shipment", label="تتبع الشحنة", url=raw_url, evidence_ref=ref)
        ],
    )
    assert validate_grounded_reply(context, reply) == []


def test_url_rendered_in_text_uses_the_same_canonical_contract() -> None:
    ref = "catalog:product:17"
    raw_url = "https://demostore.salla.sa/dev/فستان/p398551325"
    rendered_url = (
        "https://demostore.salla.sa/dev/"
        "%D9%81%D8%B3%D8%AA%D8%A7%D9%86/p398551325"
    )
    context = _context_with_evidence(
        EvidenceRecord(
            ref=ref,
            source="catalog_product",
            source_id="17",
            facts=[
                CanonicalEvidenceFact(
                    kind="product_url",
                    value=raw_url,
                    subject_product_id=17,
                )
            ],
        )
    )
    reply = CommerceReply(
        text=f"رابط المنتج: {rendered_url}",
        evidence_refs=[ref],
        fact_claims=[
            FactClaim(
                kind="product_url",
                value=raw_url,
                evidence_ref=ref,
                subject_product_id=17,
                text_span=rendered_url,
            )
        ],
    )
    assert validate_grounded_reply(context, reply) == []


@pytest.mark.parametrize(
    ("claim_url", "claim_ref", "subject_product_id", "expected_error"),
    [
        (
            "https://shop.example.com/product/17",
            "catalog:product:99",
            17,
            "unknown_evidence_refs:catalog:product:99",
        ),
        (
            "https://shop.example.com/product/17",
            "catalog:product:17",
            99,
            "claim_not_in_evidence:product_url",
        ),
        (
            "https://shop.example.com/invented",
            "catalog:product:17",
            17,
            "claim_not_in_evidence:product_url",
        ),
    ],
)
def test_product_url_does_not_bypass_evidence_or_subject_binding(
    claim_url: str,
    claim_ref: str,
    subject_product_id: int,
    expected_error: str,
) -> None:
    ref = "catalog:product:17"
    evidence_url = "https://shop.example.com/product/17"
    context = _context_with_evidence(
        EvidenceRecord(
            ref=ref,
            source="catalog_product",
            source_id="17",
            facts=[
                CanonicalEvidenceFact(
                    kind="product_url",
                    value=evidence_url,
                    subject_product_id=17,
                )
            ],
        )
    )
    reply = CommerceReply(
        text="افتح المنتج من الزر.",
        evidence_refs=[claim_ref],
        fact_claims=[
            FactClaim(
                kind="product_url",
                value=claim_url,
                evidence_ref=claim_ref,
                subject_product_id=subject_product_id,
                text_span=None,
            )
        ],
        ui_actions=[
            UIAction(
                kind="open_product",
                label="عرض المنتج",
                url=claim_url,
                evidence_ref=claim_ref,
            )
        ],
    )
    assert expected_error in validate_grounded_reply(context, reply)


def test_tracking_url_rejects_wrong_order_subject_and_product_url_kind() -> None:
    ref = "order:shipment:701"
    product_url = "https://shop.example.com/product/17"
    context = _context_with_evidence(
        EvidenceRecord(
            ref=ref,
            source="order_shipment",
            source_id="701",
            facts=[
                CanonicalEvidenceFact(
                    kind="product_url",
                    value=product_url,
                    subject_product_id=17,
                )
            ],
        )
    )
    reply = CommerceReply(
        text="تتبع الشحنة من الزر.",
        evidence_refs=[ref],
        fact_claims=[
            FactClaim(
                kind="tracking_url",
                value=product_url,
                evidence_ref=ref,
                subject_order_id=702,
                text_span=None,
            )
        ],
        ui_actions=[
            UIAction(
                kind="track_shipment",
                label="تتبع الشحنة",
                url=product_url,
                evidence_ref=ref,
            )
        ],
    )
    errors = validate_grounded_reply(context, reply)
    assert "claim_not_in_evidence:tracking_url" in errors
    assert "action_url_not_in_evidence" in errors


def test_tracking_url_rejects_wrong_order_subject_even_for_same_url() -> None:
    ref = "order:shipment:701"
    url = "https://carrier.example.com/track/ABC123"
    context = _context_with_evidence(
        EvidenceRecord(
            ref=ref,
            source="order_shipment",
            source_id="701",
            facts=[
                CanonicalEvidenceFact(
                    kind="tracking_url",
                    value=url,
                    subject_order_id=701,
                )
            ],
        )
    )
    reply = CommerceReply(
        text="تتبع الشحنة من الزر.",
        evidence_refs=[ref],
        fact_claims=[
            FactClaim(
                kind="tracking_url",
                value=url,
                evidence_ref=ref,
                subject_order_id=702,
                text_span=None,
            )
        ],
        ui_actions=[
            UIAction(kind="track_shipment", label="تتبع الشحنة", url=url, evidence_ref=ref)
        ],
    )
    errors = validate_grounded_reply(context, reply)
    assert "claim_not_in_evidence:tracking_url" in errors
    assert "action_url_not_in_evidence" in errors


def test_image_url_does_not_accept_different_canonical_resource() -> None:
    ref = "catalog:product:17"
    evidence_url = "https://cdn.example.com/images/product-17.jpg"
    changed_url = "https://cdn.example.com/images/product-18.jpg"
    context = _context_with_evidence(
        EvidenceRecord(
            ref=ref,
            source="catalog_product",
            source_id="17",
            facts=[
                CanonicalEvidenceFact(
                    kind="image_url",
                    value=evidence_url,
                    subject_product_id=17,
                )
            ],
        )
    )
    reply = CommerceReply(
        text="هذه صورة المنتج.",
        evidence_refs=[ref],
        fact_claims=[
            FactClaim(
                kind="image_url",
                value=evidence_url,
                evidence_ref=ref,
                subject_product_id=17,
                text_span=None,
            )
        ],
        media_refs=[MediaReference(url=changed_url, evidence_ref=ref)],
    )
    errors = validate_grounded_reply(context, reply)
    assert "claim_span_not_equivalent:image_url" in errors
    assert "media_url_not_in_evidence" in errors


def test_tracking_url_does_not_accept_changed_query_value() -> None:
    ref = "order:shipment:701"
    evidence_url = "https://carrier.example.com/track?id=ABC123"
    changed_url = "https://carrier.example.com/track?id=OTHER"
    context = _context_with_evidence(
        EvidenceRecord(
            ref=ref,
            source="order_shipment",
            source_id="701",
            facts=[
                CanonicalEvidenceFact(
                    kind="tracking_url",
                    value=evidence_url,
                    subject_order_id=701,
                )
            ],
        )
    )
    reply = CommerceReply(
        text="تتبع الشحنة من الزر.",
        evidence_refs=[ref],
        fact_claims=[
            FactClaim(
                kind="tracking_url",
                value=evidence_url,
                evidence_ref=ref,
                subject_order_id=701,
                text_span=None,
            )
        ],
        ui_actions=[
            UIAction(
                kind="track_shipment",
                label="تتبع الشحنة",
                url=changed_url,
                evidence_ref=ref,
            )
        ],
    )
    errors = validate_grounded_reply(context, reply)
    assert "claim_span_not_equivalent:tracking_url" in errors
    assert "action_url_not_in_evidence" in errors
