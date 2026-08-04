"""
core/checkout_shipping_policy.py
────────────────────────────────
Deterministic checkout shipping fee resolution — platform-wide.

Shipping fees must come from evidence (order_prep flags, KB policy, tenant
settings). Never invent a default amount (e.g. 29 SAR).

KB kind contract (BQ-2): readers accept both ``shipping_zones`` (canonical)
and legacy ``shipping`` sections at read time. Writers may use either kind.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

_FEE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ريال|r\.?s\.?|sar)", re.I | re.UNICODE)
_ETA_RE = re.compile(
    r"(\d+(?:\s*[-–—]\s*\d+)?\s*(?:أ?يام?(?:\s+عمل)?|يوم(?:\s+عمل)?|ساع(?:ة|ات)|hours?|days?))"
    r"|(?:خلال\s+\d+(?:\s*[-–—]\s*\d+)?\s*(?:أ?يام?|يوم))",
    re.I | re.UNICODE,
)
_FREE_MARKERS = ("مجاني", "مجانا", "free", "بدون شحن", "شحن مجاني")

# Canonical read contract — ``shipping`` is a legacy alias of ``shipping_zones``.
SHIPPING_KB_KINDS: Tuple[str, ...] = ("shipping_zones", "shipping")
_PENDING_SHIPPING_CITY_KEY = "pending_shipping_city_inquiry"

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


@dataclass(frozen=True)
class CityShippingResolution:
    city: str = ""
    shipping_fee_sar: Optional[float] = None
    free_shipping: bool = False
    eta: str = ""
    source: str = "unknown"
    known: bool = False
    city_not_covered: bool = False


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


def _fetch_shipping_kb_body(db: Any, tenant_id: int) -> str:
    if db is None or not tenant_id:
        return ""
    try:
        from models import MerchantKnowledgeSection  # noqa: PLC0415

        rows = (
            db.query(MerchantKnowledgeSection)
            .filter(
                MerchantKnowledgeSection.tenant_id == int(tenant_id),
                MerchantKnowledgeSection.kind.in_(SHIPPING_KB_KINDS),
                MerchantKnowledgeSection.is_active.is_(True),
            )
            .order_by(MerchantKnowledgeSection.priority.desc())
            .all()
        )
        parts = [str(getattr(row, "body", "") or "").strip() for row in rows]
        return "\n".join(part for part in parts if part)
    except Exception:  # noqa: BLE001
        return ""


def _detect_city_in_text(text: str) -> str:
    try:
        from modules.ai.brain.intent.ordering_extractor import (  # noqa: PLC0415
            _SAUDI_CITIES,
            _detect_city,
            _normalize_arabic,
        )

        detected = str(_detect_city(text or "") or "").strip()
        if detected:
            return detected
        norm_seg = _normalize_arabic(text or "")
        if not norm_seg:
            return ""
        canonical = sorted(set(_SAUDI_CITIES.values()), key=len, reverse=True)
        for city in canonical:
            city_norm = _normalize_arabic(city)
            if city_norm and city_norm in norm_seg:
                return city
            bare = city_norm.removeprefix("ال").strip()
            if bare and bare in norm_seg:
                return city
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _kb_segments(body: str) -> List[str]:
    segments: List[str] = []
    for raw_line in str(body or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for piece in re.split(r"[.;]\s*", line):
            piece = piece.strip()
            if piece:
                segments.append(piece)
    return segments


def _parse_segment_city_policy(segment: str) -> Optional[CityShippingResolution]:
    city = _detect_city_in_text(segment)
    if not city:
        return None
    policy_l = segment.lower()
    eta_match = _ETA_RE.search(segment)
    eta = eta_match.group(0).strip() if eta_match else ""
    if any(marker in policy_l for marker in _FREE_MARKERS):
        return CityShippingResolution(
            city=city,
            free_shipping=True,
            shipping_fee_sar=0.0,
            eta=eta,
            source="kb_shipping_policy",
            known=True,
        )
    fee_match = _FEE_RE.search(segment)
    if fee_match:
        return CityShippingResolution(
            city=city,
            shipping_fee_sar=float(fee_match.group(1)),
            free_shipping=False,
            eta=eta,
            source="kb_shipping_policy",
            known=True,
        )
    return CityShippingResolution(
        city=city,
        eta=eta,
        source="kb_shipping_policy",
        known=True,
        city_not_covered=True,
    )


def _parse_kb_city_shipping_rules(body: str) -> Dict[str, CityShippingResolution]:
    rules: Dict[str, CityShippingResolution] = {}
    for segment in _kb_segments(body):
        parsed = _parse_segment_city_policy(segment)
        if parsed is not None and parsed.city:
            rules[parsed.city] = parsed
    return rules


def _parse_kb_shipping_rules(body: str) -> Dict[str, CheckoutShippingResolution]:
    """Parse KB shipping body into per-bucket resolutions (checkout cart)."""
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


def _effective_city(
    *,
    city: str = "",
    message: str = "",
    brain_state: Optional[Dict[str, Any]] = None,
    order_prep: Optional[Dict[str, Any]] = None,
) -> str:
    for candidate in (
        str(city or "").strip(),
        str((order_prep or {}).get("city") or "").strip(),
        str((brain_state or {}).get("order_prep", {}).get("city") or "").strip(),
        _detect_city_in_text(message or ""),
    ):
        if candidate:
            return candidate
    session = dict((brain_state or {}).get("commerce_session") or {})
    pending = session.get(_PENDING_SHIPPING_CITY_KEY)
    if isinstance(pending, dict) and pending.get("needs_city"):
        detected = _detect_city_in_text(message or "")
        if detected:
            return detected
    return ""


def resolve_city_shipping_policy(
    db: Any,
    *,
    tenant_id: int,
    city: str,
) -> CityShippingResolution:
    normalized_city = str(city or "").strip()
    if not normalized_city:
        return CityShippingResolution(source="unknown")

    kb_body = _fetch_shipping_kb_body(db, tenant_id)
    rules = _parse_kb_city_shipping_rules(kb_body)
    if normalized_city in rules:
        return rules[normalized_city]

    # Match canonical spellings (e.g. «جده» → «جدة»).
    for rule_city, resolution in rules.items():
        if rule_city == normalized_city or _detect_city_in_text(normalized_city) == rule_city:
            return resolution

    return CityShippingResolution(
        city=normalized_city,
        source="kb_shipping_policy",
        known=True,
        city_not_covered=True,
    )


def build_shipping_knowledge_facts(
    db: Any,
    *,
    tenant_id: int,
    city: str = "",
    message: str = "",
    brain_state: Optional[Dict[str, Any]] = None,
    order_prep: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Structured trusted shipping facts for LLM compose (ask_shipping path)."""
    effective_city = _effective_city(
        city=city,
        message=message,
        brain_state=brain_state,
        order_prep=order_prep,
    )
    if not effective_city:
        return {"need_city": True, "source": "kb"}

    resolution = resolve_city_shipping_policy(
        db,
        tenant_id=int(tenant_id or 0),
        city=effective_city,
    )
    if resolution.city_not_covered:
        return {
            "city": effective_city,
            "need_city": False,
            "city_not_in_policy": True,
            "source": "kb",
        }
    if resolution.free_shipping:
        return {
            "city": effective_city,
            "fee_sar": 0.0,
            "free_shipping": True,
            "eta": resolution.eta,
            "source": "kb",
            "need_city": False,
        }
    if resolution.shipping_fee_sar is not None:
        return {
            "city": effective_city,
            "fee_sar": float(resolution.shipping_fee_sar),
            "free_shipping": False,
            "eta": resolution.eta,
            "source": "kb",
            "need_city": False,
        }
    return {
        "city": effective_city,
        "need_city": False,
        "city_not_in_policy": True,
        "source": "kb",
    }


def pin_pending_shipping_city(state: Any, *, source: str = "ask_shipping") -> None:
    if state is None:
        return
    session = dict(getattr(state, "commerce_session", None) or {})
    session[_PENDING_SHIPPING_CITY_KEY] = {
        "needs_city": True,
        "source": str(source or "ask_shipping"),
    }
    state.commerce_session = session


def clear_pending_shipping_city(state: Any) -> None:
    if state is None:
        return
    session = dict(getattr(state, "commerce_session", None) or {})
    session.pop(_PENDING_SHIPPING_CITY_KEY, None)
    state.commerce_session = session


def get_pending_shipping_city(state: Any) -> Optional[Dict[str, Any]]:
    session = dict(getattr(state, "commerce_session", None) or {})
    pending = session.get(_PENDING_SHIPPING_CITY_KEY)
    return dict(pending) if isinstance(pending, dict) else None


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

    kb_body = _fetch_shipping_kb_body(db, int(tenant_id or 0))
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


def resolve_verified_shipping_fee(
    db: Any,
    *,
    tenant_id: int,
    order_prep: Optional[Dict[str, Any]] = None,
    brain_state: Optional[Dict[str, Any]] = None,
    message: str = "",
) -> Tuple[Optional[float], CheckoutShippingResolution]:
    """Best-effort verified shipping fee for truth guards."""
    prep = dict(order_prep or {})
    city = _effective_city(
        city=str(prep.get("city") or ""),
        message=message,
        brain_state=brain_state,
        order_prep=prep,
    )
    if city and db is not None and tenant_id:
        city_res = resolve_city_shipping_policy(db, tenant_id=int(tenant_id), city=city)
        if city_res.known and not city_res.city_not_covered:
            if city_res.free_shipping:
                return 0.0, CheckoutShippingResolution(
                    free_shipping=True,
                    shipping_fee_sar=0.0,
                    source="kb_shipping_policy",
                )
            if city_res.shipping_fee_sar is not None:
                return float(city_res.shipping_fee_sar), CheckoutShippingResolution(
                    shipping_fee_sar=float(city_res.shipping_fee_sar),
                    free_shipping=False,
                    source="kb_shipping_policy",
                )

    resolution = resolve_checkout_shipping_policy(
        db,
        tenant_id=int(tenant_id or 0),
        order_prep=prep,
        brain_state=brain_state,
    )
    if resolution.free_shipping:
        return 0.0, resolution
    if resolution.shipping_fee_sar is not None and not resolution.merchant_review_required:
        return float(resolution.shipping_fee_sar), resolution
    return None, resolution


def reply_mentions_shipping_fee(reply: str) -> bool:
    text = str(reply or "")
    if not text.strip():
        return False
    if _FEE_RE.search(text) and any(tok in text for tok in ("شحن", "توصيل", "الشحن")):
        return True
    return bool(re.search(r"\b29\b", text) and "شحن" in text)


__all__ = [
    "SHIPPING_KB_KINDS",
    "CheckoutShippingResolution",
    "CityShippingResolution",
    "build_shipping_knowledge_facts",
    "classify_line_item_bucket",
    "clear_pending_shipping_city",
    "get_pending_shipping_city",
    "pin_pending_shipping_city",
    "reply_mentions_shipping_fee",
    "resolve_checkout_shipping_policy",
    "resolve_city_shipping_policy",
    "resolve_verified_shipping_fee",
]
