"""Observability + structured-fact precedence for merchant knowledge.

Does not own customer semantics. Retrieval remains tenant-scoped at query
time. Structured catalog/location/contact/order/payment facts win when a
knowledge section would otherwise compete.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence


@dataclass
class KnowledgeObservability:
    knowledge_query_run: bool = False
    tenant_id: int = 0
    candidate_count: int = 0
    selected_knowledge_ids: List[int] = field(default_factory=list)
    source_section: str = ""
    structured_conflicts: List[str] = field(default_factory=list)
    model_visible_knowledge_ids: List[int] = field(default_factory=list)
    knowledge_query_failed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "knowledge_query_run": self.knowledge_query_run,
            "tenant_id": int(self.tenant_id or 0),
            "candidate_count": int(self.candidate_count or 0),
            "selected_knowledge_ids": list(self.selected_knowledge_ids),
            "source_section": self.source_section,
            "structured_conflicts": list(self.structured_conflicts),
            "model_visible_knowledge_ids": list(self.model_visible_knowledge_ids),
            "knowledge_query_failed": bool(self.knowledge_query_failed),
        }


_CATALOG_OPERATIONAL_KINDS = frozenset({
    "product_price", "product_inventory", "inventory",
})
_BRANCH_OPERATIONAL_KINDS = frozenset({
    "branch", "branches", "store_location", "location",
})
_CONTACT_OPERATIONAL_KINDS = frozenset({
    "contact", "staff_contact", "escalation",
})
_PROMOTION_OPERATIONAL_KINDS = frozenset({
    "coupon", "offer", "promotion", "discount",
})
_PAYMENT_OPERATIONAL_KINDS = frozenset({
    "payment_method", "bank_transfer", "cod",
})
_ORDER_OPERATIONAL_KINDS = frozenset({
    "order", "orders", "order_status",
})


def overlay_kinds_held_by_structured_truth(
    *,
    has_catalog: bool = False,
    has_branches: bool = False,
    has_contacts: bool = False,
    has_promotions: bool = False,
    has_payments: bool = False,
    has_orders: bool = False,
) -> FrozenSet[str]:
    """Overlay kinds that must not compete with structured operational records.

    Product usage/recipe/benefit prose is explanation and stays available.
    Price/inventory, branch location, contacts, payments, promotions, and
    order prose are held back when the matching structured owner exists.
    """
    held: set[str] = set()
    if has_catalog:
        held.update(_CATALOG_OPERATIONAL_KINDS)
    if has_branches:
        held.update(_BRANCH_OPERATIONAL_KINDS)
    if has_contacts:
        held.update(_CONTACT_OPERATIONAL_KINDS)
    if has_promotions:
        held.update(_PROMOTION_OPERATIONAL_KINDS)
    if has_payments:
        held.update(_PAYMENT_OPERATIONAL_KINDS)
    if has_orders:
        held.update(_ORDER_OPERATIONAL_KINDS)
    return frozenset(held)


def structured_conflicts_for_kinds(
    retrieved_kinds: Sequence[str],
    *,
    has_catalog: bool = False,
    has_branches: bool = False,
    has_contacts: bool = False,
    has_promotions: bool = False,
    has_payments: bool = False,
    has_orders: bool = False,
) -> List[str]:
    """Record precedence: KB may explain, structured records win."""
    conflicts: List[str] = []
    kinds = {str(k or "").strip().lower() for k in retrieved_kinds}
    if has_catalog and kinds.intersection(_CATALOG_OPERATIONAL_KINDS):
        conflicts.append("catalog_structured_wins")
    if has_branches and kinds.intersection(_BRANCH_OPERATIONAL_KINDS):
        conflicts.append("branches_structured_wins")
    if has_contacts and kinds.intersection(_CONTACT_OPERATIONAL_KINDS):
        conflicts.append("contacts_structured_wins")
    if has_promotions and kinds.intersection(_PROMOTION_OPERATIONAL_KINDS):
        conflicts.append("promotions_structured_wins")
    if has_payments and kinds.intersection(_PAYMENT_OPERATIONAL_KINDS):
        conflicts.append("payments_structured_wins")
    if has_orders and kinds.intersection(_ORDER_OPERATIONAL_KINDS):
        conflicts.append("orders_structured_wins")
    return conflicts


def merge_knowledge_observability(
    *,
    tenant_id: int,
    overlay_ids: Optional[Iterable[int]] = None,
    retrieval_ids: Optional[Iterable[int]] = None,
    candidate_count: int = 0,
    retrieved_kinds: Optional[Sequence[str]] = None,
    has_catalog: bool = False,
    has_branches: bool = False,
    has_contacts: bool = False,
    has_promotions: bool = False,
    has_payments: bool = False,
    has_orders: bool = False,
    knowledge_query_failed: bool = False,
) -> KnowledgeObservability:
    overlay = [int(i) for i in (overlay_ids or []) if i]
    retrieved = [int(i) for i in (retrieval_ids or []) if i]
    visible = list(dict.fromkeys([*overlay, *retrieved]))
    source = "none"
    if overlay and retrieved:
        source = "overlay+retrieval"
    elif overlay:
        source = "overlay"
    elif retrieved:
        source = "retrieval"
    return KnowledgeObservability(
        knowledge_query_run=True,
        tenant_id=int(tenant_id or 0),
        candidate_count=int(candidate_count or 0),
        selected_knowledge_ids=visible,
        source_section=source,
        structured_conflicts=structured_conflicts_for_kinds(
            retrieved_kinds or (),
            has_catalog=has_catalog,
            has_branches=has_branches,
            has_contacts=has_contacts,
            has_promotions=has_promotions,
            has_payments=has_payments,
            has_orders=has_orders,
        ),
        model_visible_knowledge_ids=visible,
        knowledge_query_failed=bool(knowledge_query_failed),
    )


__all__ = [
    "KnowledgeObservability",
    "merge_knowledge_observability",
    "overlay_kinds_held_by_structured_truth",
    "structured_conflicts_for_kinds",
]
