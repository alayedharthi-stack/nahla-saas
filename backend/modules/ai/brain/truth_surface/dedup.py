"""
truth_surface/dedup.py
──────────────────────
UTS v1 conservative dedup — no full resolver, no superseding.
"""
from __future__ import annotations

from collections import defaultdict
from typing import List, Tuple

from .contract import EffectiveFact, EffectiveFactStatus, TruthSurface


def _replace(
    fact: EffectiveFact,
    *,
    status: EffectiveFactStatus,
    reason: str,
) -> EffectiveFact:
    return EffectiveFact(
        fact_key=fact.fact_key,
        fact_domain=fact.fact_domain,
        value=fact.value,
        source_surface=fact.source_surface,
        source=fact.source,
        confidence=fact.confidence,
        status=status,
        reason=reason,
        path=fact.path,
        kind=fact.kind,
    )


def dedup_uts_v1_facts(facts: List[EffectiveFact]) -> Tuple[List[EffectiveFact], int]:
    """Conservative dedup. DEDUPED/CONFLICT facts remain in output for audit."""
    if not facts:
        return [], 0

    deduped_count = 0
    working: List[EffectiveFact] = list(facts)

    catalog_ids: set[str] = set()
    for f in working:
        if (
            f.status == EffectiveFactStatus.ACTIVE
            and f.source_surface == TruthSurface.MERCHANT_CONTEXT_PRODUCTS
            and f.fact_key.startswith("catalog:")
        ):
            parts = f.fact_key.split(":")
            if len(parts) >= 2:
                catalog_ids.add(parts[1])

    pass1: List[EffectiveFact] = []
    for f in working:
        if f.status != EffectiveFactStatus.ACTIVE:
            pass1.append(f)
            continue
        if f.source_surface in {
            TruthSurface.SELECTED_PRODUCT,
            TruthSurface.LAST_RECOMMENDED_PRODUCTS,
        }:
            parts = f.fact_key.split(":")
            if len(parts) >= 2 and parts[1] in catalog_ids:
                pass1.append(
                    _replace(
                        f,
                        status=EffectiveFactStatus.DEDUPED,
                        reason="duplicate_of_merchant_context.products",
                    )
                )
                deduped_count += 1
                continue
        pass1.append(f)
    working = pass1

    has_policies = any(
        f.status == EffectiveFactStatus.ACTIVE
        and f.source_surface == TruthSurface.MERCHANT_CONTEXT_POLICIES
        for f in working
    )
    if has_policies:
        pass2: List[EffectiveFact] = []
        for f in working:
            if (
                f.status == EffectiveFactStatus.ACTIVE
                and f.source_surface == TruthSurface.KNOWN_FACTS
                and f.fact_key.startswith("store:shipping")
            ):
                pass2.append(
                    _replace(
                        f,
                        status=EffectiveFactStatus.DEDUPED,
                        reason="duplicate_of_merchant_context.policies",
                    )
                )
                deduped_count += 1
            else:
                pass2.append(f)
        working = pass2

    by_key: dict[str, List[EffectiveFact]] = defaultdict(list)
    for f in working:
        if f.status == EffectiveFactStatus.ACTIVE:
            by_key[f.fact_key].append(f)

    status_overrides: dict[int, tuple[EffectiveFactStatus, str]] = {}
    for key, group in by_key.items():
        if len(group) <= 1:
            continue
        values = {g.value for g in group}
        if len(values) == 1:
            for dup in group[1:]:
                status_overrides[id(dup)] = (
                    EffectiveFactStatus.DEDUPED,
                    f"duplicate_same_value:{group[0].source_surface.value}",
                )
                deduped_count += 1
        else:
            surfaces = ", ".join(g.source_surface.value for g in group)
            for g in group:
                status_overrides[id(g)] = (
                    EffectiveFactStatus.CONFLICT,
                    f"conflicting_values_across:{surfaces}",
                )

    final: List[EffectiveFact] = []
    for f in working:
        override = status_overrides.get(id(f))
        if override:
            final.append(_replace(f, status=override[0], reason=override[1]))
        else:
            final.append(f)

    return final, deduped_count


__all__ = ["dedup_uts_v1_facts"]
