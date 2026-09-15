"""Translate grounded CommerceReply fields into structured delivery actions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from modules.ai.commerce_agent_v2.output import CommerceReply, EvidenceRecord
from modules.ai.commerce_agent_v2.url_grounding import canonical_http_url_equal


DeliveryKind = Literal["text", "product", "image", "ui_action", "unsupported"]


@dataclass(frozen=True)
class CommerceDeliveryAction:
    kind: DeliveryKind
    payload: dict[str, Any]


def build_commerce_delivery_plan(
    reply: CommerceReply,
    evidence: dict[str, EvidenceRecord],
) -> list[CommerceDeliveryAction]:
    """Build a direct structured plan without converting fields to legacy markers."""
    actions = [CommerceDeliveryAction("text", {"text": reply.text})]
    presented_images: set[str] = set()

    for product_ref in reply.product_refs:
        record = evidence.get(product_ref.evidence_ref)
        fields = dict(record.fields) if record and record.source == "catalog_product" else {}
        if int(fields.get("product_id") or 0) != product_ref.product_id:
            actions.append(
                CommerceDeliveryAction(
                    "unsupported",
                    {"capability": "product_card", "reason": "missing_grounded_product_evidence"},
                )
            )
            continue
        image_url = str(fields.get("image_url") or "").strip()
        if image_url:
            presented_images.add(image_url)
        actions.append(
            CommerceDeliveryAction(
                "product",
                {
                    "id": product_ref.product_id,
                    "external_id": fields.get("external_id"),
                    "title": str(fields.get("title") or ""),
                    "caption": str(fields.get("title") or ""),
                    "price": fields.get("price"),
                    "currency": fields.get("currency"),
                    "file_url": image_url,
                    "product_url": str(fields.get("product_url") or "").strip(),
                    "evidence_ref": product_ref.evidence_ref,
                    "kind": "product_card",
                    "dispatch_source": "commerce_agent_v2",
                },
            )
        )

    for media_ref in reply.media_refs:
        record = evidence.get(media_ref.evidence_ref)
        supported = bool(
            record
            and any(
                fact.kind == "image_url" and str(fact.value) == str(media_ref.url)
                for fact in record.facts
            )
        )
        if not supported or media_ref.url in presented_images:
            if not supported:
                actions.append(
                    CommerceDeliveryAction(
                        "unsupported",
                        {"capability": "image", "reason": "missing_grounded_media_evidence"},
                    )
                )
            continue
        presented_images.add(media_ref.url)
        actions.append(
            CommerceDeliveryAction(
                "image",
                {"url": media_ref.url, "evidence_ref": media_ref.evidence_ref},
            )
        )

    for ui_action in reply.ui_actions:
        record = evidence.get(ui_action.evidence_ref)
        expected_kind = "product_url" if ui_action.kind == "open_product" else "tracking_url"
        expected_source = "catalog_product" if ui_action.kind == "open_product" else "order_shipment"
        supported = bool(
            record
            and record.source == expected_source
            and any(
                fact.kind == expected_kind
                and canonical_http_url_equal(fact.value, ui_action.url)
                and (
                    fact.subject_product_id
                    if ui_action.kind == "open_product"
                    else fact.subject_order_id
                )
                is not None
                and record.source_id
                == str(
                    fact.subject_product_id
                    if ui_action.kind == "open_product"
                    else fact.subject_order_id
                )
                for fact in record.facts
            )
        )
        actions.append(
            CommerceDeliveryAction(
                "ui_action" if supported else "unsupported",
                (
                    {
                        "kind": ui_action.kind,
                        "label": ui_action.label,
                        "url": ui_action.url,
                        "evidence_ref": ui_action.evidence_ref,
                    }
                    if supported
                    else {"capability": ui_action.kind, "reason": "missing_grounded_ui_evidence"}
                ),
            )
        )
    return actions


__all__ = ["CommerceDeliveryAction", "build_commerce_delivery_plan"]
