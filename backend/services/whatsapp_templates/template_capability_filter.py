"""
template_capability_filter.py
─────────────────────────────
Capability-aware Nahla library filtering and merchant-mode grouping.

Read-path only — does not affect automation sends or template approval.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from core.merchant_capabilities import (
    MerchantCapabilities,
    MerchantMode,
    OrderChannelPreference,
    resolve_merchant_mode,
)
from services.whatsapp_templates.template_filter_metadata import (
    TemplateFilterMeta,
    filter_meta_to_dict,
    resolve_template_filter_meta,
    template_passes_capabilities,
)

GROUP_LABELS_AR: Dict[str, str] = {
    "external_store": "المتجر الإلكتروني",
    "whatsapp":       "الطلب عبر واتساب",
}


def _channel_allowed_for_group(
    meta: TemplateFilterMeta,
    group_channel: str,
    merchant_mode: MerchantMode,
) -> bool:
    if meta.order_channel == "any":
        return True
    if meta.order_channel == group_channel:
        return True
    # Strict single-mode merchants: hide cross-channel templates
    if merchant_mode == "whatsapp_only":
        return meta.order_channel == "whatsapp"
    if merchant_mode == "external_store":
        return meta.order_channel == "external_store"
    return meta.order_channel in {group_channel, "any"}


def _groups_for_mode(merchant_mode: MerchantMode) -> List[str]:
    if merchant_mode == "hybrid":
        return ["external_store", "whatsapp"]
    if merchant_mode == "whatsapp_only":
        return ["whatsapp"]
    return ["external_store"]


def _sort_groups(
    groups: List[Dict[str, Any]],
    default_order_channel: OrderChannelPreference,
) -> List[Dict[str, Any]]:
    if default_order_channel == "whatsapp":
        order = ["whatsapp", "external_store"]
    elif default_order_channel == "external_store":
        order = ["external_store", "whatsapp"]
    else:
        order = ["external_store", "whatsapp"]
    rank = {ch: i for i, ch in enumerate(order)}
    return sorted(groups, key=lambda g: rank.get(g.get("channel", ""), 99))


def filter_and_group_library_templates(
    templates: List[Dict[str, Any]],
    caps: MerchantCapabilities,
    *,
    default_order_channel: OrderChannelPreference = "adaptive",
    include_debug: bool = False,
) -> Dict[str, Any]:
    """
    Filter templates by capabilities and group by order channel.

    Returns a response envelope suitable for ``GET /templates/nahla-library``.
    """
    merchant_mode = resolve_merchant_mode(caps)
    group_channels = _groups_for_mode(merchant_mode)

    buckets: Dict[str, List[Dict[str, Any]]] = {ch: [] for ch in group_channels}
    seen_flat: Dict[str, Dict[str, Any]] = {}

    for tpl in templates:
        meta = resolve_template_filter_meta(tpl)
        if not template_passes_capabilities(meta, caps):
            if include_debug:
                pass  # hidden templates omitted from merchant view
            continue

        preview = dict(tpl)
        preview["filter_meta"] = filter_meta_to_dict(meta)

        placed = False
        for ch in group_channels:
            if not _channel_allowed_for_group(meta, ch, merchant_mode):
                continue
            buckets[ch].append(preview)
            placed = True

        if placed and preview.get("key") not in seen_flat:
            seen_flat[str(preview.get("key"))] = preview

    groups: List[Dict[str, Any]] = []
    for ch in group_channels:
        items = buckets.get(ch) or []
        if not items:
            continue
        groups.append({
            "channel":    ch,
            "label_ar":   GROUP_LABELS_AR.get(ch, ch),
            "templates":  items,
            "total":      len(items),
        })

    groups = _sort_groups(groups, default_order_channel)
    flat = list(seen_flat.values())

    result: Dict[str, Any] = {
        "capability_aware":      True,
        "merchant_mode":         merchant_mode,
        "default_order_channel": default_order_channel,
        "capabilities":          caps.to_dict(),
        "groups":                groups,
        "templates":             flat,
        "total":                 len(flat),
    }
    return result


__all__ = [
    "GROUP_LABELS_AR",
    "filter_and_group_library_templates",
]
