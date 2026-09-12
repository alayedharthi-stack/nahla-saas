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
EvidenceSource = Literal["catalog_product", "merchant_knowledge", "product_knowledge"]
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
    evidence_ref: str


class CatalogSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolStatus
    products: list[ProductSnapshot] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    failure_reason: str | None = None


class ProductDetailsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolStatus
    product: ProductSnapshot | None = None
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    failure_reason: str | None = None


class KnowledgeSectionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: int = Field(gt=0)
    kind: str
    title: str = ""
    body: str
    linked_product_ids: list[int] = Field(default_factory=list)
    evidence_ref: str


class KnowledgeSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolStatus
    sections: list[KnowledgeSectionSnapshot] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    failure_reason: str | None = None


def _validate_canonical_fact_value(kind: FactKind, value: CanonicalFactValue) -> None:
    if kind in {"price", "sale_price", "regular_price"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{kind} must be a JSON number")
        return
    if kind == "availability":
        if not isinstance(value, bool):
            raise ValueError("availability must be boolean")
        return
    if kind == "stock_quantity":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("stock_quantity must be a non-negative integer")
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
    text_span: str = Field(
        min_length=1,
        max_length=2000,
        description="Exact excerpt from CommerceReply.text that expresses this fact naturally.",
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

    kind: Literal["open_product"]
    label: str = Field(min_length=1, max_length=120)
    url: str
    evidence_ref: str

    _validate_url = field_validator("url")(_validated_http_url)


class CommerceReply(BaseModel):
    """The only accepted final output from the V2 agent."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=6000)
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
