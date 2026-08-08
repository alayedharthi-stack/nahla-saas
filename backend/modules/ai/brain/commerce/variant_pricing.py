"""
brain/commerce/variant_pricing.py
──────────────────────────────────
Generic variant-aware pricing — product, variant, unit, and price stay
bound together at runtime.

Root principle: a price NEVER floats without the variant that supplied it.
Quantity / budget math uses the variant whose unit matches the customer's
requested basis, or asks for clarification instead of guessing.

Telemetry (grep targets):
  [PRICE_RESOLUTION_TRACE]
  [VARIANT_RESOLUTION_TRACE]
  [QUANTITY_CALCULATION_TRACE]
"""
from __future__ import annotations

import logging
import math
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("nahla.brain.variant_pricing")

# ── Unit taxonomy (generic — not category-specific) ─────────────────────

class UnitKind(str, Enum):
    WEIGHT = "weight"
    VOLUME = "volume"
    SIZE = "size"
    PACK = "pack"
    SUBSCRIPTION = "subscription"
    COUNT = "count"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class UnitSpec:
    """Normalized sellable unit attached to one variant."""
    kind: UnitKind
    magnitude: Optional[float]
    base_unit: str
    display_label: str
    normalized_key: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "magnitude": self.magnitude,
            "base_unit": self.base_unit,
            "display_label": self.display_label,
            "normalized_key": self.normalized_key,
        }


@dataclass(frozen=True)
class VariantBinding:
    """Price permanently bound to one catalog variant row."""
    product_id: Optional[str]
    variant_id: Optional[str]
    variant_label: str
    unit: UnitSpec
    price: float
    currency: str = "SAR"
    source: str = "catalog_variant"
    source_section_id: Optional[str] = None
    in_stock: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "product_id": self.product_id,
            "variant_id": self.variant_id,
            "variant_label": self.variant_label,
            "unit": self.unit.to_dict(),
            "price": self.price,
            "currency": self.currency,
            "source": self.source,
            "source_section_id": self.source_section_id,
            "in_stock": self.in_stock,
        }


@dataclass
class VariantResolutionOutcome:
    status: str  # resolved | ambiguous | missing | not_applicable
    variant: Optional[VariantBinding] = None
    candidates: List[VariantBinding] = field(default_factory=list)
    reason: str = ""
    calculation_basis: str = ""

    def to_trace(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "variant_id": getattr(self.variant, "variant_id", None),
            "variant_label": getattr(self.variant, "variant_label", None),
            "unit": (self.variant.unit.to_dict() if self.variant else None),
            "reason": self.reason,
            "calculation_basis": self.calculation_basis,
            "candidate_count": len(self.candidates),
        }


@dataclass
class PriceResolutionOutcome:
    status: str  # resolved | needs_variant | unavailable
    variant: Optional[VariantBinding] = None
    reason: str = ""
    calculation_basis: str = ""

    def to_trace(self) -> Dict[str, Any]:
        v = self.variant
        return {
            "status": self.status,
            "product_id": getattr(v, "product_id", None) if v else None,
            "variant_id": getattr(v, "variant_id", None) if v else None,
            "variant_label": getattr(v, "variant_label", None) if v else None,
            "unit": (v.unit.to_dict() if v else None),
            "price": getattr(v, "price", None) if v else None,
            "calculation_basis": self.calculation_basis,
            "source": getattr(v, "source", None) if v else None,
            "source_section_id": getattr(v, "source_section_id", None) if v else None,
            "reason": self.reason,
        }


@dataclass
class QuantityCalculationOutcome:
    status: str  # resolved | needs_clarification | unit_mismatch | no_price
    variant: Optional[VariantBinding] = None
    budget: Optional[float] = None
    quantity: Optional[float] = None
    quantity_label: str = ""
    calculation_basis: str = ""
    reason: str = ""

    def to_trace(self) -> Dict[str, Any]:
        v = self.variant
        return {
            "status": self.status,
            "product_id": getattr(v, "product_id", None) if v else None,
            "variant_id": getattr(v, "variant_id", None) if v else None,
            "variant_label": getattr(v, "variant_label", None) if v else None,
            "unit": (v.unit.to_dict() if v else None),
            "price": getattr(v, "price", None) if v else None,
            "budget": self.budget,
            "quantity": self.quantity,
            "quantity_label": self.quantity_label,
            "calculation_basis": self.calculation_basis,
            "reason": self.reason,
        }


# ── Normalization helpers ───────────────────────────────────────────────

_DIA = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_ZW = re.compile(r"[\u200B-\u200F\u2028-\u202F\u2060-\u206F]")


def normalize_text(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _ZW.sub("", s)
    s = _DIA.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return re.sub(r"\s+", " ", s).strip().lower()


_WEIGHT_RE = re.compile(
    r"(?P<num>\d+(?:[.,]\d+)?)\s*"
    r"(?P<u>g|gram|grams|جر|جرام|جرامات|kg|kilo|kilos|kilogram|"
    r"كilo|كيلo|كيلو|كيلograms?|lb|lbs|oz)\b",
    re.I | re.UNICODE,
)
_VOLUME_RE = re.compile(
    r"(?P<num>\d+(?:[.,]\d+)?)\s*(?P<u>ml|milliliter|l|liter|litre|لتر)\b",
    re.I,
)
_PACK_RE = re.compile(
    r"(?:pack\s*of|x)\s*(?P<num>\d+)|(?P<num2>\d+)\s*(?:pack|packs|حزمه|حزم|عبوه|عبوات)\b",
    re.I | re.UNICODE,
)
_SUBSCRIPTION_RE = re.compile(
    r"(monthly|annual|yearly|شهري|شهريه|سنوي|سنويه|subscription)\b",
    re.I | re.UNICODE,
)
_SIZE_TOKENS = {
    "xs": "xs", "extra small": "xs", "صغير جدا": "xs",
    "s": "s", "small": "s", "صغير": "s", "صغيره": "s", "صغيرة": "s",
    "m": "m", "medium": "m", "وسط": "m", "متوسط": "m",
    "l": "l", "large": "l", "كبير": "l", "كبيره": "l", "كبيرة": "l",
    "xl": "xl", "xxl": "xxl",
}

_BUDGET_RE = re.compile(
    r"(?P<budget>\d+(?:[.,]\d+)?)\s*(?:ريال|riyal|riyals|ر\.?\s*س|sar|sr)\b",
    re.I | re.UNICODE,
)
_QTY_QUESTION_RE = re.compile(
    r"(?:كم\s+(?:كilo|كيلo|كيلو|kg|وحده|وحدات|حبه|حبات|piece|pieces|pack|packs|"
    r"عبوه|عبوات|unit|units)\b|"
    r"how\s+many\s+(?:kg|kilo|kilos|units?|packs?)\b|how\s+many\b|"
    r"how\s+much\s+can\s+i\s+(?:get|buy))",
    re.I | re.UNICODE,
)
_PRICE_QUESTION_RE = re.compile(
    r"(?:كم\s+سعر|كم\s+السعر|بكم|سعر|price|how\s+much)",
    re.I | re.UNICODE,
)
_UNIT_ONLY_QUESTION_RE = re.compile(
    r"^(?:كم|how\s+many)\b",
    re.I | re.UNICODE,
)


def _to_float(raw: str) -> Optional[float]:
    try:
        return float(str(raw).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _weight_to_kg(num: float, unit: str) -> Tuple[float, str]:
    u = unit.lower()
    if u in {"g", "gram", "grams", "جر", "جرام", "جرامات"}:
        return num / 1000.0, "kg"
    if u in {"kg", "kilo", "kilos", "kilogram", "كilo", "كيلo", "كيلo", "كيلو"}:
        return num, "kg"
    if u in {"lb", "lbs"}:
        return num * 0.453592, "kg"
    if u == "oz":
        return num * 0.0283495, "kg"
    return num, "kg"


def parse_unit_from_text(text: str) -> Optional[UnitSpec]:
    """Extract the most specific unit mention from free text."""
    norm = normalize_text(text or "")
    if not norm:
        return None

    m = _WEIGHT_RE.search(norm)
    if m:
        num = _to_float(m.group("num")) or 0.0
        kg, base = _weight_to_kg(num, m.group("u"))
        label = m.group(0).strip()
        return UnitSpec(
            kind=UnitKind.WEIGHT,
            magnitude=kg,
            base_unit=base,
            display_label=label,
            normalized_key=f"weight:{kg:.6f}kg",
        )

    m = _VOLUME_RE.search(norm)
    if m:
        num = _to_float(m.group("num")) or 0.0
        u = m.group("u").lower()
        liters = num / 1000.0 if u == "ml" else num
        return UnitSpec(
            kind=UnitKind.VOLUME,
            magnitude=liters,
            base_unit="l",
            display_label=m.group(0).strip(),
            normalized_key=f"volume:{liters:.6f}l",
        )

    m = _PACK_RE.search(norm)
    if m:
        num = float(m.group("num") or m.group("num2") or 1)
        return UnitSpec(
            kind=UnitKind.PACK,
            magnitude=num,
            base_unit="pack",
            display_label=f"{int(num)} pack",
            normalized_key=f"pack:{int(num)}",
        )

    if _SUBSCRIPTION_RE.search(norm):
        period = "month" if re.search(r"month|شهر", norm) else "year"
        return UnitSpec(
            kind=UnitKind.SUBSCRIPTION,
            magnitude=1.0,
            base_unit=period,
            display_label=period,
            normalized_key=f"subscription:{period}",
        )

    for tok, code in _SIZE_TOKENS.items():
        if re.search(rf"\b{re.escape(tok)}\b", norm):
            return UnitSpec(
                kind=UnitKind.SIZE,
                magnitude=None,
                base_unit=code,
                display_label=tok,
                normalized_key=f"size:{code}",
            )

    if re.search(r"\b(?:1\s*)?(?:kg|kilo|كilo|كيلo|كيلو)\b", norm):
        return UnitSpec(
            kind=UnitKind.WEIGHT,
            magnitude=1.0,
            base_unit="kg",
            display_label="1kg",
            normalized_key="weight:1.000000kg",
        )

    return None


def parse_budget_amount(text: str) -> Optional[float]:
    m = _BUDGET_RE.search(normalize_text(text or ""))
    if not m:
        return None
    return _to_float(m.group("budget"))


def is_budget_quantity_question(text: str) -> bool:
    norm = normalize_text(text or "")
    if not norm:
        return False
    if parse_budget_amount(text) is not None and (
        _QTY_QUESTION_RE.search(norm) or _UNIT_ONLY_QUESTION_RE.search(norm)
    ):
        return True
    return bool(_QTY_QUESTION_RE.search(norm) and parse_budget_amount(text) is not None)


def is_variant_price_question(text: str) -> bool:
    norm = normalize_text(text or "")
    if not norm:
        return False
    if is_budget_quantity_question(text):
        return True
    if _PRICE_QUESTION_RE.search(norm):
        return True
    if parse_unit_from_text(text) and re.search(r"كم|سعر|price", norm):
        return True
    return False


def _unit_from_variant_label(label: str) -> UnitSpec:
    parsed = parse_unit_from_text(label or "")
    if parsed:
        return parsed
    return UnitSpec(
        kind=UnitKind.UNKNOWN,
        magnitude=None,
        base_unit="unit",
        display_label=(label or "unit").strip() or "unit",
        normalized_key=f"unknown:{normalize_text(label or 'unit')}",
    )


def _parse_price_value(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", s.replace(",", ""))
    if not m:
        return None
    return _to_float(m.group(1))


def bindings_from_catalog_product(product: Dict[str, Any]) -> List[VariantBinding]:
    """Build bound variants from a CatalogContextBuilder._format dict."""
    if not product:
        return []

    product_id = str(product.get("id") or product.get("product_id") or "").strip() or None
    parent_price = _parse_price_value(product.get("sale_price") or product.get("price"))
    variants = list(product.get("variants") or [])
    out: List[VariantBinding] = []

    for v in variants:
        if not isinstance(v, dict):
            continue
        if v.get("is_default"):
            continue
        if not v.get("in_stock", True):
            continue
        price = _parse_price_value(v.get("price") or v.get("sale_price"))
        if price is None:
            continue
        label = (
            str(v.get("option_summary") or "").strip()
            or str(v.get("sku") or "").strip()
            or str(v.get("name") or "").strip()
            or f"variant-{v.get('id') or len(out) + 1}"
        )
        unit = _unit_from_variant_label(label)
        out.append(
            VariantBinding(
                product_id=product_id,
                variant_id=str(v.get("id") or v.get("salla_variant_id") or "").strip() or None,
                variant_label=label,
                unit=unit,
                price=price,
                currency=str(v.get("currency") or "SAR"),
                source="catalog_variant",
            )
        )

    if not out and parent_price is not None:
        title = str(product.get("title") or "product").strip()
        unit = _unit_from_variant_label(title)
        out.append(
            VariantBinding(
                product_id=product_id,
                variant_id=str(product.get("default_variant_id") or "").strip() or None,
                variant_label=title,
                unit=unit,
                price=parent_price,
                source="catalog_product_parent",
            )
        )

    return out


def binding_from_state(selected_variant: Optional[Dict[str, Any]]) -> Optional[VariantBinding]:
    if not isinstance(selected_variant, dict) or not selected_variant:
        return None
    price = _parse_price_value(selected_variant.get("price"))
    if price is None:
        return None
    unit_raw = selected_variant.get("unit")
    if isinstance(unit_raw, dict) and unit_raw.get("normalized_key"):
        unit = UnitSpec(
            kind=UnitKind(str(unit_raw.get("kind") or UnitKind.UNKNOWN.value)),
            magnitude=unit_raw.get("magnitude"),
            base_unit=str(unit_raw.get("base_unit") or "unit"),
            display_label=str(unit_raw.get("display_label") or "unit"),
            normalized_key=str(unit_raw.get("normalized_key")),
        )
    else:
        unit = _unit_from_variant_label(
            str(selected_variant.get("variant_label") or selected_variant.get("label") or "")
        )
    return VariantBinding(
        product_id=str(selected_variant.get("product_id") or "").strip() or None,
        variant_id=str(selected_variant.get("variant_id") or "").strip() or None,
        variant_label=str(selected_variant.get("variant_label") or selected_variant.get("label") or ""),
        unit=unit,
        price=price,
        currency=str(selected_variant.get("currency") or "SAR"),
        source=str(selected_variant.get("source") or "state_bound"),
        source_section_id=selected_variant.get("source_section_id"),
    )


def _units_compatible(requested: UnitSpec, candidate: UnitSpec) -> bool:
    if requested.kind != candidate.kind:
        return False
    if requested.kind in {UnitKind.WEIGHT, UnitKind.VOLUME, UnitKind.PACK, UnitKind.COUNT}:
        if requested.magnitude is None or candidate.magnitude is None:
            return requested.normalized_key == candidate.normalized_key
        return abs((requested.magnitude or 0) - (candidate.magnitude or 0)) < 1e-6
    return requested.normalized_key == candidate.normalized_key


def _log_variant_resolution(*, tenant_id: Any, outcome: VariantResolutionOutcome, **extra: Any) -> None:
    logger.info(
        "[VARIANT_RESOLUTION_TRACE] tenant=%s status=%s variant_id=%s "
        "variant_label=%r unit=%s reason=%s calculation_basis=%s extra=%s",
        tenant_id,
        outcome.status,
        getattr(outcome.variant, "variant_id", None),
        getattr(outcome.variant, "variant_label", None),
        (outcome.variant.unit.to_dict() if outcome.variant else None),
        outcome.reason,
        outcome.calculation_basis,
        extra,
    )


def _log_price_resolution(*, tenant_id: Any, outcome: PriceResolutionOutcome, **extra: Any) -> None:
    trace = outcome.to_trace()
    logger.info(
        "[PRICE_RESOLUTION_TRACE] tenant=%s %s extra=%s",
        tenant_id,
        " ".join(f"{k}={v!r}" for k, v in trace.items() if v is not None),
        extra,
    )


def _log_quantity_calculation(*, tenant_id: Any, outcome: QuantityCalculationOutcome, **extra: Any) -> None:
    trace = outcome.to_trace()
    logger.info(
        "[QUANTITY_CALCULATION_TRACE] tenant=%s %s extra=%s",
        tenant_id,
        " ".join(f"{k}={v!r}" for k, v in trace.items() if v is not None),
        extra,
    )


def resolve_variant(
    message: str,
    *,
    variants: Sequence[VariantBinding],
    bound: Optional[VariantBinding] = None,
    requested_unit: Optional[UnitSpec] = None,
    tenant_id: Any = None,
) -> VariantResolutionOutcome:
    """Pick the catalog variant that matches the customer's unit intent."""
    if not variants:
        outcome = VariantResolutionOutcome(status="missing", reason="no_variants_in_catalog")
        _log_variant_resolution(tenant_id=tenant_id, outcome=outcome)
        return outcome

    unit = requested_unit or parse_unit_from_text(message or "")

    if unit:
        matches = [v for v in variants if _units_compatible(unit, v.unit)]
        if len(matches) == 1:
            outcome = VariantResolutionOutcome(
                status="resolved",
                variant=matches[0],
                reason="unit_match",
                calculation_basis=f"explicit_unit:{unit.normalized_key}",
            )
            _log_variant_resolution(tenant_id=tenant_id, outcome=outcome)
            return outcome
        if len(matches) > 1:
            outcome = VariantResolutionOutcome(
                status="ambiguous",
                candidates=list(matches),
                reason="multiple_variants_same_unit",
                calculation_basis=f"explicit_unit:{unit.normalized_key}",
            )
            _log_variant_resolution(tenant_id=tenant_id, outcome=outcome)
            return outcome

    if bound and bound.variant_id:
        for v in variants:
            if v.variant_id and v.variant_id == bound.variant_id:
                outcome = VariantResolutionOutcome(
                    status="resolved",
                    variant=v,
                    reason="state_bound_variant",
                    calculation_basis="prior_selected_variant",
                )
                _log_variant_resolution(tenant_id=tenant_id, outcome=outcome)
                return outcome

    norm = normalize_text(message or "")
    label_matches: List[VariantBinding] = []
    for v in variants:
        vl = normalize_text(v.variant_label)
        if vl and vl in norm:
            label_matches.append(v)
    if len(label_matches) == 1:
        outcome = VariantResolutionOutcome(
            status="resolved",
            variant=label_matches[0],
            reason="label_token_match",
            calculation_basis="message_label_match",
        )
        _log_variant_resolution(tenant_id=tenant_id, outcome=outcome)
        return outcome

    sellable = [v for v in variants if v.in_stock and v.price]
    if len(sellable) == 1:
        outcome = VariantResolutionOutcome(
            status="resolved",
            variant=sellable[0],
            reason="single_sellable_variant",
            calculation_basis="catalog_singleton",
        )
        _log_variant_resolution(tenant_id=tenant_id, outcome=outcome)
        return outcome

    if len(sellable) > 1:
        outcome = VariantResolutionOutcome(
            status="ambiguous",
            candidates=list(sellable),
            reason="variant_not_specified",
            calculation_basis="needs_variant_clarification",
        )
        _log_variant_resolution(tenant_id=tenant_id, outcome=outcome)
        return outcome

    outcome = VariantResolutionOutcome(status="missing", reason="no_in_stock_variant")
    _log_variant_resolution(tenant_id=tenant_id, outcome=outcome)
    return outcome


def resolve_price(
    *,
    variant: VariantBinding,
    tenant_id: Any = None,
    calculation_basis: str = "",
) -> PriceResolutionOutcome:
    if variant.price <= 0:
        outcome = PriceResolutionOutcome(
            status="unavailable",
            variant=variant,
            reason="non_positive_price",
            calculation_basis=calculation_basis,
        )
    else:
        outcome = PriceResolutionOutcome(
            status="resolved",
            variant=variant,
            reason="variant_bound_price",
            calculation_basis=calculation_basis or "variant_price_binding",
        )
    _log_price_resolution(tenant_id=tenant_id, outcome=outcome)
    return outcome


def _find_variant_for_target_unit(
    variants: Sequence[VariantBinding],
    target: UnitSpec,
) -> Optional[VariantBinding]:
    exact = [v for v in variants if _units_compatible(target, v.unit)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return sorted(exact, key=lambda x: x.price)[0]

    if target.kind == UnitKind.WEIGHT and target.magnitude:
        weight_variants = [
            v for v in variants
            if v.unit.kind == UnitKind.WEIGHT and v.unit.magnitude and v.price
        ]
        if not weight_variants:
            return None
        return min(
            weight_variants,
            key=lambda v: abs((v.unit.magnitude or 0) - (target.magnitude or 0)),
        )

    return None


def calculate_quantity_for_budget(
    budget: float,
    *,
    variants: Sequence[VariantBinding],
    target_unit: Optional[UnitSpec] = None,
    bound_variant: Optional[VariantBinding] = None,
    tenant_id: Any = None,
) -> QuantityCalculationOutcome:
    """Budget math — always anchored to the variant that owns the unit basis."""
    if budget <= 0:
        outcome = QuantityCalculationOutcome(
            status="needs_clarification",
            budget=budget,
            reason="invalid_budget",
            calculation_basis="budget_validation",
        )
        _log_quantity_calculation(tenant_id=tenant_id, outcome=outcome)
        return outcome

    basis: Optional[VariantBinding] = None
    calc_basis = ""

    if target_unit:
        basis = _find_variant_for_target_unit(variants, target_unit)
        calc_basis = f"target_unit:{target_unit.normalized_key}"
        if basis is None:
            outcome = QuantityCalculationOutcome(
                status="needs_clarification",
                budget=budget,
                reason="no_variant_for_requested_unit",
                calculation_basis=calc_basis,
            )
            _log_quantity_calculation(tenant_id=tenant_id, outcome=outcome)
            return outcome
    elif bound_variant:
        basis = bound_variant
        calc_basis = "prior_bound_variant"
    else:
        resolved = resolve_variant(
            "",
            variants=variants,
            bound=bound_variant,
            tenant_id=tenant_id,
        )
        if resolved.status != "resolved" or not resolved.variant:
            outcome = QuantityCalculationOutcome(
                status="needs_clarification",
                budget=budget,
                reason=resolved.reason or "variant_ambiguous",
                calculation_basis="budget_without_unit",
            )
            _log_quantity_calculation(tenant_id=tenant_id, outcome=outcome)
            return outcome
        basis = resolved.variant
        calc_basis = resolved.calculation_basis

    if not basis or basis.price <= 0:
        outcome = QuantityCalculationOutcome(
            status="no_price",
            variant=basis,
            budget=budget,
            reason="missing_variant_price",
            calculation_basis=calc_basis,
        )
        _log_quantity_calculation(tenant_id=tenant_id, outcome=outcome)
        return outcome

    if target_unit and not _units_compatible(target_unit, basis.unit):
        outcome = QuantityCalculationOutcome(
            status="unit_mismatch",
            variant=basis,
            budget=budget,
            reason="price_unit_mismatch",
            calculation_basis=calc_basis,
        )
        _log_quantity_calculation(tenant_id=tenant_id, outcome=outcome)
        return outcome

    if basis.unit.kind == UnitKind.WEIGHT and target_unit and basis.unit.magnitude:
        packs = budget / basis.price
        total_weight = packs * (basis.unit.magnitude or 0)
        label = f"{_format_qty(total_weight)} {basis.unit.base_unit}"
        outcome = QuantityCalculationOutcome(
            status="resolved",
            variant=basis,
            budget=budget,
            quantity=total_weight,
            quantity_label=label,
            calculation_basis=f"{calc_basis};budget/{basis.price}",
        )
        _log_quantity_calculation(tenant_id=tenant_id, outcome=outcome)
        return outcome

    qty = budget / basis.price
    unit_label = basis.unit.display_label or basis.variant_label
    outcome = QuantityCalculationOutcome(
        status="resolved",
        variant=basis,
        budget=budget,
        quantity=qty,
        quantity_label=f"{_format_qty(qty)} × {unit_label}",
        calculation_basis=f"{calc_basis};budget/{basis.price}",
    )
    _log_quantity_calculation(tenant_id=tenant_id, outcome=outcome)
    return outcome


def _format_qty(value: float) -> str:
    if math.isclose(value, round(value), rel_tol=0, abs_tol=0.05):
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _format_price(value: float, currency: str = "SAR") -> str:
    sym = {"SAR": "ريال"}.get(currency.upper(), currency)
    return f"{_format_qty(value)} {sym}"


def _clarify_variant_question(variants: Sequence[VariantBinding]) -> str:
    lines = ["أي خيار/حجم تقصد؟"]
    for idx, v in enumerate(variants[:6], 1):
        lines.append(f"{idx}. {v.variant_label} — {_format_price(v.price, v.currency)}")
    lines.append("(اكتب رقم الخيار أو اسمه)")
    return "\n".join(lines)


def compose_price_reply(variant: VariantBinding) -> str:
    unit = variant.unit.display_label
    if unit and unit.lower() not in variant.variant_label.lower():
        return f"{variant.variant_label} ({unit}): {_format_price(variant.price, variant.currency)}"
    return f"{variant.variant_label}: {_format_price(variant.price, variant.currency)}"


def compose_quantity_reply(outcome: QuantityCalculationOutcome) -> str:
    v = outcome.variant
    if not v:
        return "حدّد الخيار/الحجم المطلوب عشان أحسب لك الكمية بدقة."
    return (
        f"بـ {_format_price(outcome.budget or 0, v.currency)} "
        f"تقدر تأخذ تقريباً {outcome.quantity_label} "
        f"(حسب {v.variant_label} — {_format_price(v.price, v.currency)})."
    )


def evaluate_variant_pricing_turn(
    message: str,
    *,
    product: Dict[str, Any],
    selected_variant: Optional[Dict[str, Any]] = None,
    tenant_id: Any = None,
) -> Optional[Dict[str, Any]]:
    """
    Deterministic variant-pricing evaluation for one inbound turn.

    Returns a dict suitable for Decision.args when handled, else None.
    Keys: action_kind (reply|clarify), reply_text, question, variant_binding,
    price_trace, variant_trace, quantity_trace, root_cause_class.
    """
    if not is_variant_price_question(message):
        return None

    variants = bindings_from_catalog_product(product)
    bound = binding_from_state(selected_variant)
    requested_unit = parse_unit_from_text(message or "")
    budget = parse_budget_amount(message or "")

    if is_budget_quantity_question(message) and budget is not None:
        qty_outcome = calculate_quantity_for_budget(
            budget,
            variants=variants,
            target_unit=requested_unit,
            bound_variant=bound,
            tenant_id=tenant_id,
        )
        if qty_outcome.status == "resolved" and qty_outcome.variant:
            price_outcome = resolve_price(
                variant=qty_outcome.variant,
                tenant_id=tenant_id,
                calculation_basis=qty_outcome.calculation_basis,
            )
            return {
                "action_kind": "reply",
                "reply_text": compose_quantity_reply(qty_outcome),
                "variant_binding": qty_outcome.variant.to_dict(),
                "price_trace": price_outcome.to_trace(),
                "variant_trace": {
                    "status": "resolved",
                    "variant_id": qty_outcome.variant.variant_id,
                    "calculation_basis": qty_outcome.calculation_basis,
                },
                "quantity_trace": qty_outcome.to_trace(),
                "root_cause_class": None,
            }
        rc = "A" if qty_outcome.reason in {"variant_ambiguous", "no_variant_for_requested_unit"} else "C"
        if qty_outcome.status == "unit_mismatch":
            rc = "C"
        question = (
            _clarify_variant_question(variants)
            if variants
            else "حدّد الحجم أو الخيار المطلوب عشان أحسب الكمية على أساس السعر الصحيح."
        )
        return {
            "action_kind": "clarify",
            "question": question,
            "quantity_trace": qty_outcome.to_trace(),
            "root_cause_class": rc,
        }

    var_outcome = resolve_variant(
        message,
        variants=variants,
        bound=bound,
        requested_unit=requested_unit,
        tenant_id=tenant_id,
    )
    if var_outcome.status == "ambiguous":
        return {
            "action_kind": "clarify",
            "question": _clarify_variant_question(var_outcome.candidates or variants),
            "variant_trace": var_outcome.to_trace(),
            "root_cause_class": "A",
        }
    if var_outcome.status != "resolved" or not var_outcome.variant:
        return None

    price_outcome = resolve_price(
        variant=var_outcome.variant,
        tenant_id=tenant_id,
        calculation_basis=var_outcome.calculation_basis,
    )
    if price_outcome.status != "resolved":
        return None

    return {
        "action_kind": "reply",
        "reply_text": compose_price_reply(var_outcome.variant),
        "variant_binding": var_outcome.variant.to_dict(),
        "price_trace": price_outcome.to_trace(),
        "variant_trace": var_outcome.to_trace(),
        "root_cause_class": None,
    }


def _catalog_fact_products_from_hydrated(product: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Project live catalog/variant prices into the grounding fact channel.

    Parent/list price alone is insufficient when sellable variants define the
    price SoT — include the hydrated product row (with variants[]) so
    product_claim_grounding_evidence can ground 40/44/38-style amounts.
    """
    if not isinstance(product, dict) or not product:
        return []
    row = dict(product)
    # Never treat a stale focus snapshot as the sole truth without variants when
    # the hydrated row already carries live variant prices.
    return [row]


def try_variant_pricing_decision(ctx: Any) -> Optional[Any]:
    """Hook for product_discovery_gate — return Decision or None."""
    from ..decision.actions import (  # noqa: PLC0415
        ACTION_VARIANT_PRICING,
    )
    from ..decision.engine import Decision  # noqa: PLC0415

    focus = getattr(getattr(ctx, "state", None), "current_product_focus", None) or {}
    if not focus:
        return None

    product = _hydrate_catalog_product(ctx, focus)
    if not product:
        return None

    selected = getattr(getattr(ctx, "state", None), "selected_variant", None)
    evaluated = evaluate_variant_pricing_turn(
        getattr(ctx, "message", "") or "",
        product=product,
        selected_variant=selected if isinstance(selected, dict) else None,
        tenant_id=getattr(ctx, "tenant_id", None),
    )
    if not evaluated:
        return None

    catalog_facts = _catalog_fact_products_from_hydrated(product)

    if evaluated.get("action_kind") == "clarify":
        # Keep chosen_path=variant_pricing (via ACTION_VARIANT_PRICING) so the
        # grounding guard allowlists deterministic catalog prices instead of
        # treating a rule-path clarify as ungrounded prose.
        question = str(evaluated.get("question") or "").strip()
        args = dict(evaluated)
        args["reply_text"] = question
        args["question"] = question
        args["catalog_fact_products"] = catalog_facts
        return Decision(
            action=ACTION_VARIANT_PRICING,
            args=args,
            reason="variant_pricing — ambiguous variant",
            confidence=0.93,
        )

    args = dict(evaluated)
    args["catalog_fact_products"] = catalog_facts
    return Decision(
        action=ACTION_VARIANT_PRICING,
        args=args,
        reason="variant-aware deterministic pricing",
        confidence=0.94,
    )


def _hydrate_catalog_product(ctx: Any, focus: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if list(focus.get("variants") or []):
        return dict(focus)
    db = getattr(ctx, "_db", None)
    tenant_id = getattr(ctx, "tenant_id", None)
    pid = focus.get("id") or focus.get("product_id")
    if db is None or tenant_id is None or not pid:
        return dict(focus) if focus else None
    try:
        from models import Product  # noqa: PLC0415
        from core.store_knowledge import CatalogContextBuilder  # noqa: PLC0415

        row = (
            db.query(Product)
            .filter(Product.tenant_id == int(tenant_id), Product.id == int(pid))
            .first()
        )
        if row is None:
            return dict(focus)
        return CatalogContextBuilder(db, int(tenant_id))._format(row)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[VARIANT_PRICING] catalog hydrate failed: %s", exc)
        return dict(focus)
