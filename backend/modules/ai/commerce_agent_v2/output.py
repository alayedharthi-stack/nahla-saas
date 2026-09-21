"""Typed tool evidence and final output contracts for Commerce Agent V2."""
from __future__ import annotations

from typing import Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    field_validator,
    model_validator,
)


ToolStatus = Literal["ok", "not_found", "no_evidence", "denied", "error"]
EvidenceSource = Literal[
    "catalog_product",
    "merchant_knowledge",
    "product_knowledge",
    "order_summary",
    "order_details",
    "order_shipment",
    "promotion_coupon",
    "promotion_offer",
]
FactKind = Literal[
    "product_name",
    "description",
    "price",
    "currency",
    "sale_price",
    "regular_price",
    "availability",
    "stock_quantity",
    "product_url",
    "image_url",
    "merchant_knowledge",
    "product_knowledge",
    "order_reference",
    "order_status",
    "order_status_label",
    "order_total",
    "order_currency",
    "order_item_name",
    "order_item_quantity",
    "shipment_status",
    "shipment_status_label",
    "carrier",
    "tracking_number",
    "tracking_url",
]
CanonicalFactValue: TypeAlias = str | float | int | bool
_HTTP_URL_ADAPTER = TypeAdapter(HttpUrl)


def _validated_http_url(value: str) -> str:
    """Validate URLs without emitting an unsupported ``format: uri`` schema keyword."""
    return str(_HTTP_URL_ADAPTER.validate_python(value))


class CanonicalEvidenceFact(BaseModel):
    """One typed fact inside an evidence record.

    Monetary values are JSON numbers, availability is boolean, quantity is an
    integer, and all other values are canonical source strings.  Product-bound
    facts carry the trusted catalog product id so a claim cannot be validated
    against a similarly shaped fact for another product.
    """

    model_config = ConfigDict(extra="forbid")

    kind: FactKind
    value: CanonicalFactValue
    subject_product_id: int | None = Field(default=None, gt=0)
    subject_order_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _validate_kind_value(self) -> "CanonicalEvidenceFact":
        _validate_canonical_fact_value(self.kind, self.value)
        return self


class EvidenceRecord(BaseModel):
    """One immutable, source-addressable fact bundle returned by a tool."""

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1, max_length=160)
    source: EvidenceSource
    source_id: str = Field(min_length=1, max_length=128)
    facts: list[CanonicalEvidenceFact] = Field(default_factory=list)
    fields: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, str] = Field(default_factory=dict)


class ProductSnapshot(BaseModel):
    """Customer-safe catalog projection sourced from CatalogContextBuilder."""

    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(gt=0)
    external_id: str | None = None
    title: str
    description: str = ""
    price: str | None = None
    sale_price: str | None = None
    regular_price: str | None = None
    currency: str | None = None
    in_stock: bool | None = None
    stock_quantity: int | None = None
    image_url: str = ""
    product_url: str = ""
    orderable: bool = False
    # Option values (colour, size, …) of the variants that can be bought now,
    # from the catalog's own variant rows; empty when the product has none.
    variant_options: dict[str, list[str]] = Field(default_factory=dict)
    variants_in_stock: int | None = None
    variants_total: int | None = None
    evidence_ref: str


class KnowledgeSectionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: int = Field(gt=0)
    kind: str
    title: str = ""
    body: str
    linked_product_ids: list[int] = Field(default_factory=list)
    evidence_ref: str


class PromotionSnapshot(BaseModel):
    """Customer-safe projection of one currently valid, shareable promotion.

    Sourced from ``promotion_truth.resolve_shareable_promotions``: a coupon
    the merchant may hand out on this channel, or an offer's terms. An offer
    never carries a code, and no code is ever invented. Whether the customer
    in the conversation qualifies is not determined here, and the projection
    says so.
    """

    model_config = ConfigDict(extra="forbid")

    promotion_id: int = Field(gt=0)
    record_kind: Literal["coupon", "offer"]
    code: str = ""
    name: str = ""
    description: str = ""
    discount_type: str = ""
    discount_value: str = ""
    discount: str = ""                        # the one reading the record supports: "5%" or "20 SAR"; "" when unreadable
    expires_at: str = ""
    coupon_level: str = ""
    conditions: dict[str, Any] = Field(default_factory=dict)
    bound_to_this_customer: bool = False      # a personal code issued to this conversation's customer
    eligibility_determined: bool = False
    eligibility_note: str = ""
    evidence_ref: str


class PromotionListResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolStatus
    promotions: list[PromotionSnapshot] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    query_outcome: str = ""
    partial: bool = False                     # a source could not be read; the list may be incomplete
    failure_reason: str | None = None


class CatalogSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolStatus
    products: list[ProductSnapshot] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    # Merchant-authored knowledge retrieved for these products in the same
    # deterministic step. It may be empty, and it never carries a commercial fact.
    knowledge_sections: list[KnowledgeSectionSnapshot] = Field(default_factory=list)
    failure_reason: str | None = None


class ProductDetailsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolStatus
    product: ProductSnapshot | None = None
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    knowledge_sections: list[KnowledgeSectionSnapshot] = Field(default_factory=list)
    failure_reason: str | None = None


class KnowledgeSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolStatus
    sections: list[KnowledgeSectionSnapshot] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    failure_reason: str | None = None


class OrderSummarySnapshot(BaseModel):
    """Customer-safe projection of one authorized local order."""

    model_config = ConfigDict(extra="forbid")

    order_id: int = Field(gt=0)
    order_reference: str | None = None
    status: str
    status_label: str
    evidence_ref: str


class OrderResolveResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolStatus
    order: OrderSummarySnapshot | None = None
    selection_reason: str | None = None
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    failure_reason: str | None = None


class OrderLineItemSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    quantity: int | None = Field(default=None, ge=0)


class OrderDetailsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: int = Field(gt=0)
    order_reference: str | None = None
    total: int | float | None = None
    currency: str | None = None
    line_items: list[OrderLineItemSnapshot] = Field(default_factory=list)
    evidence_ref: str


class OrderDetailsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolStatus
    order: OrderDetailsSnapshot | None = None
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    failure_reason: str | None = None


class OrderShipmentSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: int = Field(gt=0)
    order_reference: str | None = None
    shipment_status: str | None = None
    shipment_status_label: str | None = None
    carrier: str | None = None
    tracking_number: str | None = None
    tracking_url: str | None = None
    evidence_ref: str

    @field_validator("tracking_url")
    @classmethod
    def _validate_optional_tracking_url(cls, value: str | None) -> str | None:
        return _validated_http_url(value) if value is not None else None


class OrderShipmentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolStatus
    shipment: OrderShipmentSnapshot | None = None
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    failure_reason: str | None = None


def _validate_canonical_fact_value(kind: FactKind, value: CanonicalFactValue) -> None:
    if kind in {"price", "sale_price", "regular_price", "order_total"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{kind} must be a JSON number")
        return
    if kind == "availability":
        if not isinstance(value, bool):
            raise ValueError("availability must be boolean")
        return
    if kind in {"stock_quantity", "order_item_quantity"}:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{kind} must be a non-negative integer")
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{kind} must be a non-empty string")


class FactClaim(BaseModel):
    """A canonical commercial fact plus its natural-language span in ``text``."""

    model_config = ConfigDict(extra="forbid")

    kind: FactKind
    value: CanonicalFactValue = Field(
        description=(
            "Canonical typed value: price fields are JSON numbers, availability "
            "is boolean, stock_quantity is an integer, and knowledge values copy "
            "the canonical source fact exactly."
        )
    )
    evidence_ref: str = Field(min_length=1, max_length=160)
    subject_product_id: int | None = Field(
        default=None,
        gt=0,
        description="Trusted product_id for every product-bound fact; null only for merchant knowledge.",
    )
    subject_order_id: int | None = Field(
        default=None,
        gt=0,
        description="Trusted local order_id for every order/shipment-bound fact.",
    )
    text_span: str | None = Field(
        default=None,
        max_length=2000,
        description=(
            "Exact excerpt from CommerceReply.text that expresses this fact naturally; "
            "null only when a URL or image is rendered solely by ui_actions or media_refs."
        ),
    )

    @model_validator(mode="after")
    def _validate_kind_value(self) -> "FactClaim":
        _validate_canonical_fact_value(self.kind, self.value)
        return self


class ProductReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(gt=0)
    evidence_ref: str


class MediaReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    media_type: Literal["image"] = "image"
    evidence_ref: str

    _validate_url = field_validator("url")(_validated_http_url)


class UIAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["open_product", "track_shipment"]
    label: str = Field(min_length=1, max_length=120)
    url: str
    evidence_ref: str

    _validate_url = field_validator("url")(_validated_http_url)


class CommerceReply(BaseModel):
    """The only accepted final output from the V2 agent."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=6000)
    response_mode: Literal["grounded", "social"] = Field(
        default="grounded",
        description=(
            "Use social only for a short, genuinely non-commercial social reply "
            "that contains no store, product, price, availability, order, shipment, "
            "location, recommendation, URL, or other factual claim. A greeting joined "
            "to any commercial or factual request remains grounded."
        ),
    )
    evidence_refs: list[str] = Field(default_factory=list)
    fact_claims: list[FactClaim] = Field(default_factory=list)
    product_refs: list[ProductReference] = Field(default_factory=list)
    media_refs: list[MediaReference] = Field(default_factory=list)
    ui_actions: list[UIAction] = Field(default_factory=list)
    safe_fallback_reason: str | None = Field(default=None, max_length=240)

    @model_validator(mode="after")
    def _deduplicate_references(self) -> "CommerceReply":
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("evidence_refs must be unique")
        return self
