"""
Phase 2.8 — known customer identity for native catalog / active catalog checkout.

Operational only: prefill trusted customer name into order_prep and keep name
slots out of missing_fields when the customer is already known.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.brain.commerce.catalog_checkout_customer_identity")

_NAME_MISSING_SLOTS = frozenset({
    "name",
    "full_name",
    "customer_name",
    "customer_first_name",
    "customer_last_name",
})

_PHONE_MISSING_SLOTS = frozenset({
    "phone",
    "customer_phone",
    "customer_phone_number",
    "mobile",
})

_FORBIDDEN_NAME_PROMPT_RES = (
    re.compile(r"وش\s*اسم(?:ك|ك\s*الكامل)?", re.I | re.UNICODE),
    re.compile(r"ممكن\s+تذكر\s+اسم(?:ك)?", re.I | re.UNICODE),
    re.compile(r"اكتب\s+اسم(?:ك)?", re.I | re.UNICODE),
    re.compile(r"ما\s+اسم(?:ك)?", re.I | re.UNICODE),
)


@dataclass(frozen=True)
class CatalogCheckoutCustomerIdentity:
    prep_patch: Dict[str, Any] = field(default_factory=dict)
    known_facts: Dict[str, Any] = field(default_factory=dict)
    customer_name_known: bool = False


def split_operational_full_name(full_name: str) -> Tuple[str, str]:
    """Deterministic split: first token = first_name, remainder = last_name."""
    parts = [part.strip() for part in str(full_name or "").split() if part.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def looks_like_phone_name(text: str) -> bool:
    if not text:
        return False
    digits = text.lstrip("+").replace(" ", "").replace("-", "")
    return digits.isdigit() and len(digits) >= 7


def reply_contains_forbidden_catalog_name_question(text: str) -> bool:
    blob = str(text or "").strip()
    if not blob:
        return False
    return any(p.search(blob) for p in _FORBIDDEN_NAME_PROMPT_RES)


def _prep_str(prep: Dict[str, Any], key: str) -> str:
    return str(prep.get(key) or "").strip()


def _valid_customer_name(name: str) -> bool:
    text = str(name or "").strip()
    if not text or looks_like_phone_name(text):
        return False
    try:
        from core.customer_name_validator import validate_customer_name  # noqa: PLC0415

        return bool(validate_customer_name(text).valid)
    except Exception:  # noqa: BLE001
        return len(text) >= 2


def resolve_customer_row_by_phone(
    db: Any,
    tenant_id: int,
    phone: str,
    customer: Any = None,
) -> Any:
    """Resolve ``Customer`` by WhatsApp phone variants (+966 / 966 / local)."""
    return _load_customer_row(db, tenant_id, phone, customer=customer)


def _load_customer_row(
    db: Any,
    tenant_id: int,
    phone: str,
    customer: Any = None,
) -> Any:
    if customer is not None:
        return customer
    if db is None or not tenant_id or not phone:
        return None
    try:
        from models import Customer  # noqa: PLC0415
        from utils.phone_utils import normalize_to_e164  # noqa: PLC0415

        raw = str(phone or "").strip()
        e164 = normalize_to_e164(raw) or raw
        candidates = tuple({
            p for p in (
                raw,
                e164,
                e164.lstrip("+") if e164 else "",
                f"+{e164.lstrip('+')}" if e164 else "",
            ) if p
        })
        for candidate in candidates:
            row = (
                db.query(Customer)
                .filter(
                    Customer.tenant_id == int(tenant_id),
                    (Customer.normalized_phone == candidate) | (Customer.phone == candidate),
                )
                .first()
            )
            if row is not None:
                return row
        return None
    except Exception:  # noqa: BLE001
        logger.debug(
            "[CATALOG_CHECKOUT_IDENTITY] customer lookup failed tenant=%s",
            tenant_id,
            exc_info=True,
        )
        return None


def _resolve_operational_name(
    *,
    customer: Any,
    profile: Dict[str, Any],
) -> Tuple[str, str]:
    profile = dict(profile or {})
    if customer is not None:
        try:
            from core.customer_identity_resolver import (  # noqa: PLC0415
                can_use_name_for_operations,
                read_customer_identity,
            )

            if can_use_name_for_operations(customer):
                snap = read_customer_identity(customer)
                name = str(snap.customer_name or "").strip()
                if name:
                    return name, str(snap.customer_name_source or "customer_db").strip()
        except Exception:  # noqa: BLE001  # noqa: silent-ok — customer identity read must not block checkout
            pass
        for key in ("full_name", "name"):
            val = str(getattr(customer, key, "") or "").strip()
            if _valid_customer_name(val):
                return val, "customer_db"

    for key in ("customer_name", "name", "display_name", "full_name"):
        val = str(profile.get(key) or "").strip()
        if _valid_customer_name(val):
            return val, "profile"
    customer_block = profile.get("customer")
    if isinstance(customer_block, dict):
        for key in ("full_name", "name", "display_name", "customer_name"):
            val = str(customer_block.get(key) or "").strip()
            if _valid_customer_name(val):
                return val, "profile.customer"
    return "", ""


def resolve_catalog_checkout_customer_identity(
    *,
    db: Any = None,
    tenant_id: Optional[int] = None,
    phone: str = "",
    order_prep: Optional[Dict[str, Any]] = None,
    profile: Optional[Dict[str, Any]] = None,
    customer: Any = None,
) -> CatalogCheckoutCustomerIdentity:
    """Resolve trusted customer name for catalog checkout missing-field projection."""
    prep = dict(order_prep or {})
    profile_d = dict(profile or {})
    patch: Dict[str, Any] = {}
    known_facts: Dict[str, Any] = {}

    first = _prep_str(prep, "customer_first_name")
    last = _prep_str(prep, "customer_last_name")

    if phone:
        known_facts["phone_known"] = True
        if not _prep_str(prep, "customer_phone"):
            patch["customer_phone"] = str(phone).strip()

    customer_row = _load_customer_row(db, int(tenant_id or 0), phone, customer=customer)
    operational_name, name_source = _resolve_operational_name(
        customer=customer_row,
        profile=profile_d,
    )

    if operational_name and not (first and last):
        split_first, split_last = split_operational_full_name(operational_name)
        if split_first and not first and not looks_like_phone_name(split_first):
            patch["customer_first_name"] = split_first
            first = split_first
        if split_last and not last:
            patch["customer_last_name"] = split_last
            last = split_last

    full = " ".join(x for x in (first, last) if x).strip()
    name_known = False
    if first and last and not looks_like_phone_name(first):
        name_known = True
    elif full and _valid_customer_name(full):
        name_known = True

    if customer_row is not None and getattr(customer_row, "id", None):
        known_facts["customer_id"] = getattr(customer_row, "id")
        known_facts["customer_record_exists"] = True

    if name_known:
        known_facts["customer_name_known"] = True
        known_facts["customer_name"] = full or first
        if name_source:
            known_facts["customer_name_source"] = name_source

    return CatalogCheckoutCustomerIdentity(
        prep_patch=patch,
        known_facts=known_facts,
        customer_name_known=name_known,
    )


def merchant_customer_record_facts(
    identity: CatalogCheckoutCustomerIdentity,
) -> Dict[str, Any]:
    """Merchant-record identity for Brain facts — not personal familiarity."""
    facts = dict(identity.known_facts or {})
    if not (
        identity.customer_name_known
        or facts.get("customer_id")
        or facts.get("customer_record_exists")
    ):
        return {}
    record = {
        "registered": True,
        "personal_familiarity": False,
        "has_historical_orders": False,
        "historical_order_details_available": False,
        "customer_id": facts.get("customer_id"),
        "customer_name": facts.get("customer_name"),
        "customer_name_source": facts.get("customer_name_source"),
    }
    out: Dict[str, Any] = {
        "merchant_customer_record": record,
        "personal_familiarity": False,
    }
    if identity.customer_name_known:
        out["customer_name_known"] = True
        out["customer_name"] = facts.get("customer_name")
        if facts.get("customer_name_source"):
            out["customer_name_source"] = facts["customer_name_source"]
    if facts.get("customer_id"):
        out["customer_id"] = facts["customer_id"]
    return out


_IDENTITY_EVIDENCE_FACT_KEYS = (
    "merchant_customer_record",
    "customer_name_known",
    "customer_name",
    "customer_name_source",
    "customer_id",
    "personal_familiarity",
    "customer_history_facts",
)


def merchant_identity_evidence_slice(
    known_facts: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Slim merchant-record identity for social/identity compose. No commerce."""
    facts = dict(known_facts or {})
    out: Dict[str, Any] = {}
    for key in _IDENTITY_EVIDENCE_FACT_KEYS:
        val = facts.get(key)
        if val in (None, "", {}, []):
            continue
        out[key] = val
    return out


def merge_prep_with_customer_identity(
    order_prep: Dict[str, Any],
    identity: CatalogCheckoutCustomerIdentity,
) -> Dict[str, Any]:
    merged = dict(order_prep or {})
    for key, value in (identity.prep_patch or {}).items():
        if value not in (None, ""):
            merged[key] = value
    return merged


def filter_missing_for_known_catalog_customer(
    missing: List[str],
    *,
    known_facts: Optional[Dict[str, Any]] = None,
    phone: str = "",
) -> List[str]:
    facts = dict(known_facts or {})
    out = list(missing or [])
    if facts.get("customer_name_known"):
        out = [m for m in out if m not in _NAME_MISSING_SLOTS]
    phone_known = bool(facts.get("phone_known") or str(phone or "").strip())
    if phone_known:
        out = [m for m in out if m not in _PHONE_MISSING_SLOTS]
    return out


def enrich_catalog_checkout_prep_and_missing(
    order_prep: Dict[str, Any],
    missing: List[str],
    *,
    db: Any = None,
    tenant_id: Optional[int] = None,
    phone: str = "",
    profile: Optional[Dict[str, Any]] = None,
    customer: Any = None,
) -> Tuple[Dict[str, Any], List[str], Dict[str, Any]]:
    identity = resolve_catalog_checkout_customer_identity(
        db=db,
        tenant_id=tenant_id,
        phone=phone,
        order_prep=order_prep,
        profile=profile,
        customer=customer,
    )
    merged = merge_prep_with_customer_identity(order_prep, identity)
    filtered = filter_missing_for_known_catalog_customer(
        missing,
        known_facts=identity.known_facts,
        phone=phone,
    )
    return merged, filtered, identity.known_facts


def apply_catalog_customer_identity_to_contract(
    *,
    missing_fields: List[str],
    known_facts: Dict[str, Any],
    db: Any = None,
    tenant_id: Optional[int] = None,
    phone: str = "",
    order_prep: Optional[Dict[str, Any]] = None,
    profile: Optional[Dict[str, Any]] = None,
    customer: Any = None,
) -> Tuple[List[str], Dict[str, Any]]:
    merged_prep, filtered, identity_facts = enrich_catalog_checkout_prep_and_missing(
        dict(order_prep or {}),
        list(missing_fields or []),
        db=db,
        tenant_id=tenant_id,
        phone=phone,
        profile=profile,
        customer=customer,
    )
    facts = dict(known_facts or {})
    facts.update(identity_facts)
    if identity_facts.get("customer_name_known"):
        name = str(identity_facts.get("customer_name") or "").strip()
        if name:
            facts.setdefault("name", name)
    _ = merged_prep  # contract caller merges prep separately when needed
    return filtered, facts


def is_catalog_checkout_name_question_forbidden(
    *,
    known_facts: Optional[Dict[str, Any]] = None,
    ctx: Any = None,
    state: Any = None,
) -> bool:
    facts = dict(known_facts or {})
    if ctx is not None:
        contract = getattr(ctx, "commerce_turn_contract", None)
        if contract is not None:
            facts.update(dict(getattr(contract, "known_facts", None) or {}))
    if state is not None and not facts.get("customer_name_known"):
        prep = getattr(state, "order_prep", None)
        prep_d: Dict[str, Any] = {}
        if prep is not None and hasattr(prep, "to_dict"):
            try:
                prep_d = dict(prep.to_dict())
            except Exception:  # noqa: BLE001
                prep_d = {}
        elif isinstance(prep, dict):
            prep_d = dict(prep)
        first = str(prep_d.get("customer_first_name") or "").strip()
        last = str(prep_d.get("customer_last_name") or "").strip()
        full = " ".join(x for x in (first, last) if x).strip()
        if first and last:
            facts["customer_name_known"] = True
        elif full and _valid_customer_name(full):
            facts["customer_name_known"] = True
    return bool(facts.get("customer_name_known"))


def sanitize_forbidden_catalog_name_question(
    reply: str,
    *,
    known_facts: Optional[Dict[str, Any]] = None,
    ctx: Any = None,
    missing_fields: Optional[List[str]] = None,
) -> str:
    if not is_catalog_checkout_name_question_forbidden(known_facts=known_facts, ctx=ctx):
        return str(reply or "")
    if not reply_contains_forbidden_catalog_name_question(reply):
        return str(reply or "")
    missing = list(missing_fields or [])
    if ctx is not None:
        contract = getattr(ctx, "commerce_turn_contract", None)
        if contract is not None:
            missing = list(getattr(contract, "missing_fields", None) or missing)
    if any(m in {"city"} for m in missing):
        return "باقي نكمل بيانات التوصيل، المدينة والحي أو الموقع لو تكرمت."
    if any(m in {"delivery_address", "address", "address_line", "short_address_code"} for m in missing):
        return (
            "باقي نكمل بيانات التوصيل، شاركنا عنوان التوصيل: رابط Google Maps "
            "أو الرمز الوطني المختصر."
        )
    return "باقي نكمل بيانات التوصيل، المدينة والحي أو الموقع لو تكرمت."


__all__ = [
    "CatalogCheckoutCustomerIdentity",
    "apply_catalog_customer_identity_to_contract",
    "enrich_catalog_checkout_prep_and_missing",
    "filter_missing_for_known_catalog_customer",
    "is_catalog_checkout_name_question_forbidden",
    "merchant_customer_record_facts",
    "merchant_identity_evidence_slice",
    "merge_prep_with_customer_identity",
    "reply_contains_forbidden_catalog_name_question",
    "resolve_catalog_checkout_customer_identity",
    "resolve_customer_row_by_phone",
    "sanitize_forbidden_catalog_name_question",
    "split_operational_full_name",
]
