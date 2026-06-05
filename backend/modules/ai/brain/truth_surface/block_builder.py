"""
truth_surface/block_builder.py
──────────────────────────────
Build ``operational_facts_block`` from UTS v1 manifest (shadow egress).
"""
from __future__ import annotations

from typing import List

from .contract import (
    EffectiveFact,
    EffectiveFactStatus,
    FactDomain,
    OperationalFactsBlock,
)

_DOMAIN_HEADINGS = {
    FactDomain.CATALOG: "#### كتالوج المنتجات",
    FactDomain.KNOWLEDGE: "#### معرفة المتجر (KB)",
    FactDomain.ORDER: "#### حالة الطلب / الدفع",
    FactDomain.POLICY: "#### سياسات وشحن",
    FactDomain.PLATFORM: "#### منصّة نحلة",
    FactDomain.GOAL: "#### توصيات حسب الهدف",
    FactDomain.STORE: "#### معلومات المتجر",
}


def build_operational_facts_block(facts: List[EffectiveFact]) -> OperationalFactsBlock:
    active = [f for f in facts if f.status == EffectiveFactStatus.ACTIVE]
    if not active:
        return OperationalFactsBlock(
            text=(
                "### حقائق تشغيلية موحّدة (UTS v1 — shadow)\n"
                "لا توجد حقائق تشغيلية active في manifest هذه الجولة."
            ),
            fact_count=len(facts),
            active_fact_count=0,
        )

    lines = [
        "### حقائق تشغيلية موحّدة (UTS v1 — shadow)",
        "المصدر الوحيد المقترح للحقائق التشغيلية — لا يُحقَن في prompt إلا عند enforce.",
        "",
    ]

    by_domain: dict[FactDomain, List[EffectiveFact]] = {}
    for f in active:
        by_domain.setdefault(f.fact_domain, []).append(f)

    for domain in FactDomain:
        group = by_domain.get(domain)
        if not group:
            continue
        lines.append(_DOMAIN_HEADINGS[domain])
        for f in group:
            preview = f.value.replace("\n", " ").strip()
            if len(preview) > 240:
                preview = preview[:240] + "…"
            lines.append(
                f"- `{f.fact_key}` | {f.kind.value} | {preview}"
            )
        lines.append("")

    text = "\n".join(lines).strip()
    return OperationalFactsBlock(
        text=text,
        fact_count=len(facts),
        active_fact_count=len(active),
    )


__all__ = ["build_operational_facts_block"]
