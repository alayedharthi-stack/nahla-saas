"""Typed tool evidence and final output contracts for Commerce Agent V2."""
from __future__ import annotations

from typing import Any, Literal

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
_HTTP_URL_ADAPTER = TypeAdapter(HttpUrl)


def _validated_http_url(value: str) -> str:
    """Validate URLs without emitting an unsupported ``format: uri`` schema keyword."""
    return str(_HTTP_URL_ADAPTER.validate_python(value))


class EvidenceRecord(BaseModel):
    """One immutable, source-addressable fact bundle returned by a tool."""

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1, max_length=160)
    source: EvidenceSource
    source_id: str = Field(min_length=1, max_length=128)
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


class FactClaim(BaseModel):
    """A commercial assertion in ``text`` tied to one exact tool evidence row."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "product_name",
        "description",
        "price",
        "sale_price",
        "stock",
        "product_url",
        "image_url",
        "merchant_knowledge",
        "product_knowledge",
    ]
    value: str = Field(min_length=1, max_length=4000)
    evidence_ref: str = Field(min_length=1, max_length=160)


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
