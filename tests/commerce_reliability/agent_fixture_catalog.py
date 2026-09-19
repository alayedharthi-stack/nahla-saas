"""Generic, merchant-agnostic fixture catalogue for the agent loop tests.

Three merchants with different, overlapping-in-shape catalogues across rotating
categories (clothing, footwear, perfume, accessories) so nothing in the loop or
the tools can be specific to one merchant or one product family. Two of them
stock the *same* category differently, which is what lets a test compare two
branches under one identical question and one identical tool query and show
that the observation, not the wording, changed the next decision. Tenant ids
are bound at build time by the test; the registry itself never learns a scope.
"""
from __future__ import annotations

from typing import Dict, Tuple

from core.commerce_runtime import agent_tools as at

TENANT_A_PRODUCTS: Tuple[at.FixtureProduct, ...] = (
    at.FixtureProduct(ref="product:blue_cotton_shirt", name="قميص قطني أزرق", price="149.00", currency="SAR",
                      in_stock=True, tags=("shirt", "cotton", "clothing")),
    at.FixtureProduct(ref="product:white_sneaker", name="حذاء رياضي أبيض", price="299.00", currency="SAR",
                      in_stock=False, tags=("shoe", "sneaker", "footwear", "حذاء")),
    at.FixtureProduct(ref="product:rose_perfume_100", name="عطر ورد 100ml", price="420.00", currency="SAR",
                      in_stock=True, tags=("perfume", "rose")),
)
TENANT_B_PRODUCTS: Tuple[at.FixtureProduct, ...] = (
    at.FixtureProduct(ref="product:leather_belt", name="حزام جلد بني", price="99.00", currency="SAR",
                      in_stock=True, tags=("belt", "accessory")),
)
# A second merchant in the same category, stocked differently: the branch
# comparison uses the identical question and the identical query against this.
TENANT_C_PRODUCTS: Tuple[at.FixtureProduct, ...] = (
    at.FixtureProduct(ref="product:black_sneaker", name="حذاء رياضي أسود", price="279.00", currency="SAR",
                      in_stock=True, tags=("shoe", "sneaker", "footwear", "حذاء")),
    at.FixtureProduct(ref="product:linen_shirt", name="قميص كتان بيج", price="189.00", currency="SAR",
                      in_stock=True, tags=("shirt", "linen", "clothing")),
)
TENANT_A_KNOWLEDGE: Tuple[at.FixtureKnowledge, ...] = (
    at.FixtureKnowledge(ref="kb:shipping", topic="shipping", text="الشحن خلال ٣ أيام عمل داخل المدن الرئيسية."),
    at.FixtureKnowledge(ref="kb:returns", topic="returns", text="الاستبدال خلال ١٤ يومًا مع الفاتورة."),
)
TENANT_B_KNOWLEDGE: Tuple[at.FixtureKnowledge, ...] = (
    at.FixtureKnowledge(ref="kb:hours", topic="hours", text="المتجر يعمل من ٩ صباحًا حتى ٩ مساءً."),
)
TENANT_C_KNOWLEDGE: Tuple[at.FixtureKnowledge, ...] = (
    at.FixtureKnowledge(ref="kb:shipping", topic="shipping", text="التوصيل خلال ٥ أيام عمل لجميع المناطق."),
)


def build_catalog(tenant_a: int = 1, tenant_b: int = 2, tenant_c: int = 3) -> at.FixtureCatalog:
    products: Dict[int, Tuple[at.FixtureProduct, ...]] = {
        tenant_a: TENANT_A_PRODUCTS, tenant_b: TENANT_B_PRODUCTS, tenant_c: TENANT_C_PRODUCTS}
    knowledge: Dict[int, Tuple[at.FixtureKnowledge, ...]] = {
        tenant_a: TENANT_A_KNOWLEDGE, tenant_b: TENANT_B_KNOWLEDGE, tenant_c: TENANT_C_KNOWLEDGE}
    return at.FixtureCatalog(products=products, knowledge=knowledge)


def build_registry(tenant_a: int = 1, tenant_b: int = 2, tenant_c: int = 3) -> at.ToolRegistry:
    return at.ToolRegistry(at.build_fixture_tools(build_catalog(tenant_a, tenant_b, tenant_c)))


__all__ = ["TENANT_A_KNOWLEDGE", "TENANT_A_PRODUCTS", "TENANT_B_KNOWLEDGE", "TENANT_B_PRODUCTS",
           "TENANT_C_KNOWLEDGE", "TENANT_C_PRODUCTS", "build_catalog", "build_registry"]
