"""
core/checkout_shipping_policy.py
────────────────────────────────
Deterministic checkout shipping fee resolution — platform-wide.

Shipping fees must come from evidence (order_prep flags, KB policy, tenant
settings). Never invent a default amount (e.g. 29 SAR).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

_FEE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ريال|r\.?s\.?|sar)", re.I | re.UNICODE)
_FREE_MARKERS = ("مجاني", "مجانا", "free", "بدون شحن", "شحن مجاني")

# Generic commerce category buckets — not merchant-specific.
_CATEGORY_BUCKETS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "food",
        (
            "غذائي",
            "غذائية",
            "food",
            "مأكول",
            "مشروب",
            "عصير",
            "قهوة",
            "شاي",
        ),
    ),
    (
        "clothing",
        (
            "ملابس",
            "قميص",
            "فستان",
            "حذاء",
            "أحذية",
            "clothing",
            "apparel",
            "shirt",
            "shoes",
        ),
    ),
    (
        "accessories",
        (
            "مستحضر",
            "هدايا",
            "إكسسوار",
            "حقيبة",
            "perfume",
            "عطر",
            "cosmetic",
            "مكياج",
        ),
    ),
    (
        "electronics",
        (
            "إلكترون",
            "electronics",
            "جوال",
            "هاتف",
            "لابتوب",
            "سماعة",
        ),
    ),
)


@dataclass(frozen=True)
class CheckoutShippingResolution:
    shipping_fee_sar: Optional[float] = None
    free_shipping: bool = False
    source: str = "unknown"
    merchant_review_required: bool = False
    line_item_buckets: Tuple[str, ...] = ()

    def to_state_patch(self) -> Dict[str, Any]:
        patch: Dict[str, Any] = {"shipping_policy_source": self.source}
        if self.merchant_review_required:
            patch["shipping_policy_requires_review"] = True
            return patch
        if self.free_shipping:
            patch["free_shipping"] = True
            patch["shipping_fee"] = 0
            patch["shipping_cost"] = 0
            return patch
        if self.shipping_fee_sar is not None:
            patch["free_shipping"] = False
            patch["shipping_fee"] = float(self.shipping_fee_sar)
            patch["shipping_cost"] = float(self.shipping_fee_sar)
        return patch


def _line_items(order_prep: Dict[str, Any], brain_state: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    for container in (order_prep, brain_state or {}):
        raw = container.get("line_items") or container.get("cart_items") or []
        if isinstance(raw, list) and raw:
            return [dict(x) for x in raw if isinstance(x, dict)]
    return []


def classify_line_item_bucket(item: Dict[str, Any]) -> str:
    title = " ".join(
        str(item.get(k) or "")
        for k in ("product_name", "title", "name", "category", "product_category")
    ).lower()
    for bucket, keywords in _CATEGORY_BUCKETS:
        if any(kw in title for kw in keywords):
            return bucket
    return "general"


def _parse_kb_shipping_rules(body: str) -> Dict[str, CheckoutShippingResolution]:
    """Parse KB shipping_zones body into per-bucket resolutions."""
    rules: Dict[str, CheckoutShippingResolution] = {}
    for raw_line in str(body or "").splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        label, policy = line.split(":", 1)
        label_l = label.strip().lower()
        policy_l = policy.strip().lower()
        buckets: List[str] = []
        for bucket, keywords in _CATEGORY_BUCKETS:
            if bucket in label_l or any(kw in label_l for kw in keywords):
                buckets.append(bucket)
        if not buckets:
            buckets = ["general"]
        if any(marker in policy_l for marker in _FREE_MARKERS):
            resolution = CheckoutShippingResolution(
                free_shipping=True,
                shipping_fee_sar=0.0,
                source="kb_shipping_policy",
            )
        else:
            match = _FEE_RE.search(policy)
            if match:
                resolution = CheckoutShippingResolution(
                    shipping_fee_sar=float(match.group(1)),
                    free_shipping=False,
                    source="kb_shipping_policy",
                )
            else:
                continue
        for bucket in buckets:
            rules[bucket] = resolution
    return rules


def _resolution_from_prep(order_prep: Dict[str, Any]) -> Optional[CheckoutShippingResolution]:
    if order_prep.get("free_shipping"):
        return CheckoutShippingResolution(
            free_shipping=True,
            shipping_fee_sar=0.0,
            source="order_prep_free_shipping",
        )
    for key in ("shipping_fee", "shipping_cost"):
        raw = order_prep.get(key)
        if raw in (None, ""):
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val <= 0:
            return CheckoutShippingResolution(
                free_shipping=True,
                shipping_fee_sar=0.0,
                source="order_prep_shipping_fee",
            )
        return CheckoutShippingResolution(
            shipping_fee_sar=val,
            free_shipping=False,
            source="order_prep_shipping_fee",
        )
    return None


def resolve_checkout_shipping_policy(
    db: Any,
    *,
    tenant_id: int,
    order_prep: Dict[str, Any],
    brain_state: Optional[Dict[str, Any]] = None,
) -> CheckoutShippingResolution:
    prep = dict(order_prep or {})
    from_prep = _resolution_from_prep(prep)
    if from_prep is not None and prep.get("shipping_policy_source") != "llm_composed_summary":
        return from_prep

    items = _line_items(prep, brain_state)
    buckets = tuple(classify_line_item_bucket(it) for it in items) if items else ("general",)

    kb_body = ""
    if db is not None and tenant_id:
        try:
            from models import MerchantKnowledgeSection  # noqa: PLC0415

            row = (
                db.query(MerchantKnowledgeSection)
                .filter(
                    MerchantKnowledgeSection.tenant_id == int(tenant_id),
                    MerchantKnowledgeSection.kind == "shipping_zones",
                    MerchantKnowledgeSection.is_active.is_(True),
                )
                .order_by(MerchantKnowledgeSection.priority.desc())
                .first()
            )
            if row is not None:
                kb_body = str(getattr(row, "body", "") or "")
        except Exception:  # noqa: BLE001
            kb_body = ""

    rules = _parse_kb_shipping_rules(kb_body)
    if not rules:
        return CheckoutShippingResolution(
            source="unknown",
            merchant_review_required=bool(items),
            line_item_buckets=buckets,
        )

    per_item: List[CheckoutShippingResolution] = []
    for bucket in buckets:
        per_item.append(rules.get(bucket) or rules.get("general") or CheckoutShippingResolution(source="unknown"))

    if not per_item:
        return CheckoutShippingResolution(source="unknown", merchant_review_required=True, line_item_buckets=buckets)

    free_flags = {r.free_shipping for r in per_item}
    fees = {r.shipping_fee_sar for r in per_item if r.shipping_fee_sar is not None and not r.free_shipping}

    if len(free_flags) == 1 and True in free_flags:
        return CheckoutShippingResolution(
            free_shipping=True,
            shipping_fee_sar=0.0,
            source="kb_shipping_policy",
            line_item_buckets=buckets,
        )
    if len(fees) == 1 and not free_flags.intersection({True}):
        fee = next(iter(fees))
        return CheckoutShippingResolution(
            shipping_fee_sar=fee,
            free_shipping=False,
            source="kb_shipping_policy",
            line_item_buckets=buckets,
        )
    if len(fees) == 1 and True in free_flags:
        return CheckoutShippingResolution(
            shipping_fee_sar=next(iter(fees)),
            free_shipping=False,
            source="kb_shipping_policy",
            merchant_review_required=True,
            line_item_buckets=buckets,
        )

    return CheckoutShippingResolution(
        source="kb_shipping_policy",
        merchant_review_required=True,
        line_item_buckets=buckets,
    )


def reply_mentions_shipping_fee(reply: str) -> bool:
    text = str(reply or "")
    if not text.strip():
        return False
    if _FEE_RE.search(text) and any(tok in text for tok in ("شحن", "توصيل", "الشحن")):
        return True
    return bool(re.search(r"\b29\b", text) and "شحن" in text)


__all__ = [
    "CheckoutShippingResolution",
    "classify_line_item_bucket",
    "reply_mentions_shipping_fee",
    "resolve_checkout_shipping_policy",
]
