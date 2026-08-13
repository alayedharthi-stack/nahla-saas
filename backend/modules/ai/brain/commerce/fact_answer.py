"""
Semantic fact-answer ownership.

Platform owns which truth surface may answer an authoritative factual
question. The LLM owns wording. This is not a phrase blacklist: concept
family + question shape select a fact_kind; evidence status decides
KNOWN vs UNKNOWN.

Catalog must not become a universal fallback for merchant operational truth.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from modules.ai.brain.types import (
    INTENT_ASK_COD,
    INTENT_ASK_LOCATION,
    INTENT_ASK_PAYMENT_INFO,
    INTENT_ASK_SHIPPING,
    INTENT_ASK_WORKING_HOURS,
    INTENT_TALK_HUMAN,
    INTENT_TRACK_ORDER,
)

STATUS_KNOWN_VALUE = "KNOWN_VALUE"
STATUS_KNOWN_EMPTY = "KNOWN_EMPTY"
STATUS_UNKNOWN = "UNKNOWN"

DOMAIN_PROFILE = "merchant_profile"
DOMAIN_CAPABILITIES = "merchant_capabilities"
DOMAIN_POLICY = "merchant_policy"
DOMAIN_CATALOG = "catalog"
DOMAIN_PRODUCT = "product"

KIND_WORKING_HOURS = "working_hours"
KIND_OPEN_NOW = "open_now"
KIND_BRANCH_EXISTENCE = "branch_existence"
KIND_LOCATION = "location"
KIND_CERTIFICATION = "certification"
KIND_SHIPPING_FEE = "shipping_fee"
KIND_SHIPPING_ETA = "shipping_eta"
KIND_SHIPPING_COMPANIES = "shipping_companies"
KIND_PAYMENT_METHODS = "payment_methods"
KIND_CASH_ON_DELIVERY = "cash_on_delivery"
KIND_WARRANTY = "warranty"
KIND_RETURN_POLICY = "return_policy"
KIND_GIFT_RECOMMENDATION = "gift_recommendation"
KIND_SHIPPING_COVERAGE = "shipping_coverage"

_DIA = r"[\u064B-\u065F\u0640]"

_POLICY_KINDS = frozenset({
    KIND_WARRANTY,
    KIND_RETURN_POLICY,
    "refund_policy",
    "exchange_policy",
    "shipping_policy",
    "terms_policy",
    "privacy_policy",
    "store_story",
})

# Generic merchant facts that must NOT yield to post-order shipping even when
# a paid order exists in state (hours, warranty, certification, payments).
_TRANSACTIONAL_YIELD_KINDS = frozenset({
    KIND_LOCATION,
    KIND_BRANCH_EXISTENCE,
    KIND_SHIPPING_COMPANIES,
    KIND_SHIPPING_ETA,
    KIND_SHIPPING_FEE,
    KIND_SHIPPING_COVERAGE,
    "shipping_policy",
})


def _norm(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(_DIA, "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    t = re.sub(r"[؟?!.,،؛:]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _history_blob(history: Optional[Sequence[Any]]) -> str:
    parts: List[str] = []
    for item in list(history or [])[-6:]:
        if isinstance(item, dict):
            parts.append(str(item.get("content") or item.get("body") or item.get("text") or ""))
        else:
            parts.append(str(item or ""))
    return _norm(" ".join(parts))


@dataclass(frozen=True)
class FactAnswerRequest:
    domain: str
    fact_kind: str
    catalog_allowed: bool = False
    reason: str = ""


@dataclass
class FactAnswerContract:
    domain: str
    fact_kind: str
    status: str
    claimable_values: List[Any] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    forbidden_inferences: List[str] = field(default_factory=list)
    catalog_allowed: bool = False
    subject: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domain": self.domain,
            "fact_kind": self.fact_kind,
            "status": self.status,
            "claimable_values": list(self.claimable_values),
            "evidence_refs": list(self.evidence_refs),
            "forbidden_inferences": list(self.forbidden_inferences),
            "catalog_allowed": self.catalog_allowed,
            "subject": self.subject,
        }


_COMPANY_CONCEPT = re.compile(
    r"(شرك[ةه]|شركات|ناقل|carrier|courier)",
    re.UNICODE | re.IGNORECASE,
)
_SHIP_CONCEPT = re.compile(
    r"(شحن|توصيل|توصل|تشحن|يشحن|ماسكه|delivery|shipping)",
    re.UNICODE | re.IGNORECASE,
)
_FEE_SHAPE = re.compile(
    r"(تكلف[ةه]|رسوم|بكم|سعر\s*(?:ال)?شحن|shipping\s*(?:fee|cost|price)|how\s*much.{0,12}ship)",
    re.UNICODE | re.IGNORECASE,
)
_ETA_SHAPE = re.compile(
    r"(يستغرق|مده\s*(?:ال)?شحن|كم\s*ياخذ|كم\s*ياخد|كم\s*يوم.{0,16}شحن|eta|how\s*long.{0,12}ship)",
    re.UNICODE | re.IGNORECASE,
)
_WHO_SHAPE = re.compile(r"(اي|مين|who|which)", re.UNICODE | re.IGNORECASE)
_CERT_CONCEPT = re.compile(
    r"(معتمد|اعتماد|شهاد[ةه]|هيئ[ةه]|ساسو|saso|\bce\b|organic|certif|موافق[ةه]\s*رسمي)",
    re.UNICODE | re.IGNORECASE,
)
_HOURS_CONCEPT = re.compile(
    r"(دوام|ساعات\s*(?:ال)?(?:عمل|دعم)|اوقات\s*(?:ال)?عمل|working\s*hours|open\s*now)",
    re.UNICODE | re.IGNORECASE,
)
_OPEN_NOW_CONCEPT = re.compile(
    r"(شغال|مفتوح|فاتح|مسكر|مقفل|are\s*you\s*open|is\s*(?:the\s*)?store\s*open)",
    re.UNICODE | re.IGNORECASE,
)
_BRANCH_CONCEPT = re.compile(
    r"(فرع|فروع|عنوان(?:كم|ك|نا)?|موقع(?:كم|ك)?\s*(?:ال)?(?:فرع|محل)|branch|location)",
    re.UNICODE | re.IGNORECASE,
)
_WARRANTY_CONCEPT = re.compile(r"(ضمان|warranty)", re.UNICODE | re.IGNORECASE)
_RETURN_CONCEPT = re.compile(
    r"(ارجاع|استرجاع|استرداد|استبدال|return|refund|exchange)",
    re.UNICODE | re.IGNORECASE,
)
_PRODUCT_BOUND_POLICY = re.compile(
    r"(هذا\s*(?:ال)?منتج|هالمنتج|عليه\s*ضمان|ضمان\s*(?:على|لهذا)|this\s*product|product\s*warranty)",
    re.UNICODE | re.IGNORECASE,
)
_PAYMENT_CONCEPT = re.compile(
    r"(دفع|ادفع|اسدد|طرق\s*(?:ال)?دفع|وسائل\s*(?:ال)?دفع|payment)",
    re.UNICODE | re.IGNORECASE,
)
_USE_AT_ORDER_SHAPE = re.compile(
    r"(بطلب|اطلب|الطلب).{0,24}(استخدم|ادفع)|(استخدم|ادفع).{0,24}(بطلب|اطلب|الطلب)|"
    r"اذا\s*بطلب|what\s+can\s+i\s+use",
    re.UNICODE | re.IGNORECASE,
)
_COD_CONCEPT = re.compile(
    r"(دفع\s*عند\s*الاستلام|وقت\s*الاستلام|\bcod\b|cash\s*on\s*delivery)",
    re.UNICODE | re.IGNORECASE,
)
_GIFT_CONCEPT = re.compile(r"(هديه|gift)", re.UNICODE | re.IGNORECASE)
_QTY_OR_SKU = re.compile(
    r"(\d+\s*(?:حبه|قطع[ةه]|كيلو|مل|ml|kg)|sku|فستان|تنوره|عطر|حذاء|قميص)",
    re.UNICODE | re.IGNORECASE,
)
_EXISTENCE_SHAPE = re.compile(
    r"(عندكم|عندك|فيه|هل|do\s*you\s*have|is\s*there)",
    re.UNICODE | re.IGNORECASE,
)


def classify_fact_answer(
    message: str,
    *,
    intent_name: str = "",
    history: Optional[Sequence[Any]] = None,
) -> Optional[FactAnswerRequest]:
    """Return the authoritative fact request for this turn, or None."""
    text = str(message or "").strip()
    if not text:
        return None
    norm = _norm(text)
    intent = str(intent_name or "").strip()
    hist = _history_blob(history)
    # TRACK_ORDER / TALK_HUMAN stay transactional. PAY_NOW must not skip
    # payment-method *discovery* ("وش عندكم طريقة أدفع فيها؟") — that is
    # MERCHANT_CAPABILITIES.payment_methods, not checkout continuation.
    if intent in {INTENT_TRACK_ORDER, INTENT_TALK_HUMAN}:
        return None

    pack_b_pay = False
    pack_b_ship = False
    try:
        from .merchant_capability_faq import (  # noqa: PLC0415
            is_merchant_payment_methods_question,
            is_merchant_shipping_companies_question,
        )

        pack_b_pay = is_merchant_payment_methods_question(text)
        pack_b_ship = is_merchant_shipping_companies_question(text)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — local shapes remain authoritative
        pack_b_pay = False
        pack_b_ship = False

    if _CERT_CONCEPT.search(norm):
        return FactAnswerRequest(
            domain=DOMAIN_PRODUCT,
            fact_kind=KIND_CERTIFICATION,
            catalog_allowed=False,
            reason="certification_fact_kind",
        )

    if _COD_CONCEPT.search(norm) or intent == INTENT_ASK_COD:
        return FactAnswerRequest(
            domain=DOMAIN_CAPABILITIES,
            fact_kind=KIND_CASH_ON_DELIVERY,
            catalog_allowed=False,
            reason="cod_capability",
        )

    if _FEE_SHAPE.search(norm) and _SHIP_CONCEPT.search(norm):
        return FactAnswerRequest(
            domain=DOMAIN_POLICY,
            fact_kind=KIND_SHIPPING_FEE,
            catalog_allowed=False,
            reason="shipping_fee_shape",
        )
    if _ETA_SHAPE.search(norm) and (
        _SHIP_CONCEPT.search(norm) or _SHIP_CONCEPT.search(hist)
    ):
        return FactAnswerRequest(
            domain=DOMAIN_POLICY,
            fact_kind=KIND_SHIPPING_ETA,
            catalog_allowed=False,
            reason="shipping_eta_shape",
        )
    if (
        (
            pack_b_ship
            or (
                _COMPANY_CONCEPT.search(norm)
                and (_SHIP_CONCEPT.search(norm) or _WHO_SHAPE.search(norm))
            )
            or (
                _WHO_SHAPE.search(norm)
                and _SHIP_CONCEPT.search(norm)
            )
        )
        and not _FEE_SHAPE.search(norm)
        and not _ETA_SHAPE.search(norm)
    ):
        return FactAnswerRequest(
            domain=DOMAIN_CAPABILITIES,
            fact_kind=KIND_SHIPPING_COMPANIES,
            catalog_allowed=False,
            reason="shipping_companies_shape",
        )

    payment_list_shape = bool(
        re.search(r"(طرق|وسائل|خيارات|طريق[ةه])\s*(?:ال)?دفع", norm)
        or re.search(r"how\s*(?:can|do)\s*i\s*pay|payment\s*methods?", norm)
    )
    if (
        pack_b_pay
        or payment_list_shape
        or (_USE_AT_ORDER_SHAPE.search(norm) and not _QTY_OR_SKU.search(norm))
        or (
            _PAYMENT_CONCEPT.search(norm)
            and _EXISTENCE_SHAPE.search(norm)
            and not _SHIP_CONCEPT.search(norm)
            and not _QTY_OR_SKU.search(norm)
        )
    ):
        return FactAnswerRequest(
            domain=DOMAIN_CAPABILITIES,
            fact_kind=KIND_PAYMENT_METHODS,
            catalog_allowed=False,
            reason="payment_capability_shape",
        )

    if _WARRANTY_CONCEPT.search(norm) and not _PRODUCT_BOUND_POLICY.search(norm):
        return FactAnswerRequest(
            domain=DOMAIN_POLICY,
            fact_kind=KIND_WARRANTY,
            catalog_allowed=False,
            reason="warranty_policy_shape",
        )
    if _RETURN_CONCEPT.search(norm) and (
        _EXISTENCE_SHAPE.search(norm)
        or re.search(r"(سياس[ةه]|شروط|كيف)", norm)
    ):
        return FactAnswerRequest(
            domain=DOMAIN_POLICY,
            fact_kind=KIND_RETURN_POLICY,
            catalog_allowed=False,
            reason="return_policy_shape",
        )

    if intent == INTENT_ASK_WORKING_HOURS or _HOURS_CONCEPT.search(norm) or _OPEN_NOW_CONCEPT.search(norm):
        kind = KIND_OPEN_NOW if _OPEN_NOW_CONCEPT.search(norm) else KIND_WORKING_HOURS
        return FactAnswerRequest(
            domain=DOMAIN_PROFILE,
            fact_kind=kind,
            catalog_allowed=False,
            reason="hours_or_open_now",
        )

    branch_place_followup = bool(
        _BRANCH_CONCEPT.search(hist)
        and re.search(
            r"^(?:طيب|حسنا|ok|okay)?\s*(?:في|ب)\s*\S{3,}\s*$",
            norm,
        )
    )
    if intent == INTENT_ASK_LOCATION or _BRANCH_CONCEPT.search(norm) or branch_place_followup:
        kind = (
            KIND_BRANCH_EXISTENCE
            if (_EXISTENCE_SHAPE.search(norm) or branch_place_followup)
            else KIND_LOCATION
        )
        return FactAnswerRequest(
            domain=DOMAIN_PROFILE,
            fact_kind=kind,
            catalog_allowed=False,
            reason="branch_or_location",
        )

    if _GIFT_CONCEPT.search(norm) and not _QTY_OR_SKU.search(norm):
        return FactAnswerRequest(
            domain=DOMAIN_CATALOG,
            fact_kind=KIND_GIFT_RECOMMENDATION,
            catalog_allowed=True,
            reason="gift_recommendation_catalog_grounded",
        )

    if intent == INTENT_ASK_SHIPPING:
        return FactAnswerRequest(
            domain=DOMAIN_POLICY,
            fact_kind=KIND_SHIPPING_COVERAGE,
            catalog_allowed=False,
            reason="shipping_intent_coverage_unknown_unless_evidenced",
        )

    try:
        from .merchant_policy_intents import classify_merchant_policy_topic  # noqa: PLC0415

        topic = classify_merchant_policy_topic(text)
        if topic:
            return FactAnswerRequest(
                domain=DOMAIN_POLICY,
                fact_kind=str(topic),
                catalog_allowed=False,
                reason="pack_a3_policy_topic",
            )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional pack classifier
        pass

    return None


def fact_answer_owns_non_catalog_turn(
    message: str,
    *,
    intent_name: str = "",
    history: Optional[Sequence[Any]] = None,
) -> bool:
    """True when this turn already has a non-catalog FactAnswer owner.

    Coarse SOCIAL/greeting labels must not skip capability/profile fact
    load or strip the per-turn answer_contract from compose.
    """
    req = classify_fact_answer(
        message, intent_name=intent_name, history=history,
    )
    return bool(req is not None and not req.catalog_allowed)


def fact_answer_yields_to_transactional(
    message: str,
    *,
    intent_name: str = "",
    state: Any = None,
    fact_kind: str = "",
) -> bool:
    """Customer-specific order/shipment truth outranks generic merchant facts.

    Certification, hours, warranty, return-policy, and payment facts stay
    fact-answer owned even when a paid order exists. Only shipping/origin/
    carrier/location fact-kinds yield, and only when the inbound is
    order-referential (طلبي / شحنتي / …).
    """
    if fact_kind and fact_kind not in _TRANSACTIONAL_YIELD_KINDS:
        return False
    try:
        from .order_tracking_intent_guard import (  # noqa: PLC0415
            is_order_actual_shipping_question,
        )

        if not is_order_actual_shipping_question(message):
            return False
    except Exception:  # noqa: BLE001  # noqa: silent-ok — yield probe must not block fact-answer
        return False
    # ``state`` / ``intent_name`` are accepted so the engine can pass the
    # same context the later ASK_SHIPPING owner uses; message semantics
    # decide the yield so generic facts remain owned during post-order.
    _ = (intent_name, state)
    return True


def catalog_must_yield_to_fact_owner(
    *,
    intent_name: str = "",
    message: str = "",
    history: Optional[Sequence[Any]] = None,
) -> bool:
    """True when CatalogNavigator must not own this turn."""
    req = classify_fact_answer(message, intent_name=intent_name, history=history)
    if req is not None and not req.catalog_allowed:
        return True
    name = str(intent_name or "").strip()
    if name in {
        INTENT_ASK_WORKING_HOURS,
        INTENT_ASK_LOCATION,
        INTENT_ASK_SHIPPING,
        INTENT_ASK_PAYMENT_INFO,
        INTENT_ASK_COD,
    }:
        return True
    try:
        from .merchant_profile_intents import (  # noqa: PLC0415
            should_yield_catalog_for_merchant_profile,
        )

        if should_yield_catalog_for_merchant_profile(
            intent_name=name, message=message,
        ):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass
    try:
        from .merchant_policy_intents import (  # noqa: PLC0415
            should_yield_catalog_for_merchant_policy,
        )

        if not should_yield_catalog_for_merchant_policy(
            intent_name=name, message=message,
        ):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass
    try:
        from .merchant_capability_faq import (  # noqa: PLC0415
            should_yield_catalog_navigator_for_capability,
        )

        if should_yield_catalog_navigator_for_capability(
            intent_name=name, message=message,
        ):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass
    return False


def _projection_map(facts: Any, merchant_context: Any, key: str) -> Dict[str, Any]:
    for source in (
        getattr(facts, key, None),
        (merchant_context or {}).get(key) if isinstance(merchant_context, dict) else None,
        ((merchant_context or {}).get("trusted_context_projection") or {}).get(key)
        if isinstance(merchant_context, dict)
        else None,
    ):
        if isinstance(source, dict) and source:
            return dict(source)
    proj = getattr(facts, "trusted_context_projection", None)
    if isinstance(proj, dict) and isinstance(proj.get(key), dict):
        return dict(proj.get(key) or {})
    return {}


def _field_status(payload: Dict[str, Any], key: str) -> tuple[str, Any]:
    status = str(payload.get(f"{key}.status") or payload.get("status") or "").upper()
    value = payload.get(key)
    if status in {STATUS_KNOWN_VALUE, STATUS_KNOWN_EMPTY, STATUS_UNKNOWN}:
        if status == STATUS_KNOWN_VALUE and value in (None, "", [], {}):
            return STATUS_UNKNOWN, None
        return status, value
    if value in (None, "", [], {}):
        return STATUS_UNKNOWN, None
    return STATUS_KNOWN_VALUE, value


def _asked_branch_place(norm_msg: str) -> str:
    """Named place on a branch-existence question, if the turn supplied one."""
    m = re.search(r"(?:فرع|فروع).{0,20}(?:في|ب)\s*(\S{2,})", norm_msg)
    if m:
        return str(m.group(1) or "").strip()
    # Standalone preposition + place ("طيب في لندن"). Require a word
    # boundary so the trailing ب in طيب is not read as "in".
    m = re.search(r"(?:^|\s)(?:في|ب)\s+(\S{3,})", norm_msg)
    if m:
        return str(m.group(1) or "").strip()
    return ""


def _capability_list(caps: Dict[str, Any], *paths: str) -> tuple[str, List[Any]]:
    cursor: Any = caps
    for part in paths:
        if not isinstance(cursor, dict):
            return STATUS_UNKNOWN, []
        cursor = cursor.get(part)
    status = ""
    values: List[Any] = []
    if isinstance(cursor, dict):
        status = str(
            cursor.get("status")
            or cursor.get("companies_status")
            or ""
        ).lower()
        raw = cursor.get("methods") or cursor.get("companies") or cursor.get("value") or []
        if isinstance(raw, list):
            values = list(raw)
    elif isinstance(cursor, list):
        values = list(cursor)
        status = "known" if values else "empty"
    if status in {"known", "empty"} or values:
        return (STATUS_KNOWN_VALUE if values else STATUS_KNOWN_EMPTY), values
    return STATUS_UNKNOWN, []


def build_fact_answer_contract(
    request: FactAnswerRequest,
    *,
    facts: Any = None,
    merchant_context: Any = None,
    message: str = "",
) -> FactAnswerContract:
    """Bind the current question to claimable evidence only."""
    profile = _projection_map(facts, merchant_context, "merchant_profile")
    caps = _projection_map(facts, merchant_context, "merchant_capabilities")
    if not caps and facts is not None:
        caps = dict(getattr(facts, "merchant_capabilities", None) or {})
    policy = _projection_map(facts, merchant_context, "merchant_policy")

    forbidden_base = [
        "world_knowledge_fill",
        "neighboring_domain_substitution",
        "catalog_as_operational_evidence",
    ]
    contract = FactAnswerContract(
        domain=request.domain,
        fact_kind=request.fact_kind,
        status=STATUS_UNKNOWN,
        catalog_allowed=request.catalog_allowed,
        subject=_norm(message)[:80],
        forbidden_inferences=list(forbidden_base),
    )

    if request.fact_kind in {KIND_WORKING_HOURS, KIND_OPEN_NOW}:
        hours_status, hours_val = _field_status(profile, "working_hours")
        support = str(getattr(facts, "support_hours", "") or "").strip() if facts else ""
        if hours_status == STATUS_KNOWN_VALUE and hours_val not in (None, "", [], {}):
            contract.status = STATUS_KNOWN_VALUE
            contract.claimable_values = [hours_val]
            contract.evidence_refs = ["merchant_profile.working_hours"]
        elif support:
            contract.status = STATUS_KNOWN_VALUE
            contract.claimable_values = [support]
            contract.evidence_refs = ["commerce_facts.support_hours"]
        else:
            contract.status = STATUS_UNKNOWN
            contract.evidence_refs = ["merchant_profile.working_hours"]
        contract.forbidden_inferences.extend([
            "invent_city_hours",
            "infer_open_now_from_catalog",
            "treat_support_hours_as_branch_hours_when_absent",
        ])
        return contract

    if request.fact_kind in {KIND_BRANCH_EXISTENCE, KIND_LOCATION}:
        loc_status, loc_val = _field_status(profile, "location")
        branch_status, branch_val = _field_status(profile, "default_branch")
        maps = ""
        if facts is not None:
            maps = str(getattr(facts, "maps_url", "") or "").strip()
        known = []
        refs = []
        if loc_status == STATUS_KNOWN_VALUE and loc_val:
            known.append(loc_val)
            refs.append("merchant_profile.location")
        if branch_status == STATUS_KNOWN_VALUE and branch_val:
            known.append(branch_val)
            refs.append("merchant_profile.default_branch")
        if request.fact_kind == KIND_LOCATION and maps:
            known.append(maps)
            refs.append("commerce_facts.maps_url")
        if request.fact_kind == KIND_BRANCH_EXISTENCE:
            asked = _asked_branch_place(_norm(message))
            evidence_blob = _norm(" ".join(str(x) for x in known if x))
            if asked and (not evidence_blob or asked not in evidence_blob):
                known = []
                refs = refs or ["merchant_profile.location"]
        contract.evidence_refs = refs or ["merchant_profile.location"]
        contract.claimable_values = known
        contract.status = STATUS_KNOWN_VALUE if known else STATUS_UNKNOWN
        contract.forbidden_inferences.extend([
            "imply_branch_network",
            "invent_city_branch",
            "maps_url_proves_named_city",
            "branch_network_exists",
            "branch_address_exists",
            "branch_selectable",
            "offer_to_send_branch_address_without_evidence",
        ])
        return contract

    if request.fact_kind == KIND_CERTIFICATION:
        contract.status = STATUS_UNKNOWN
        contract.evidence_refs = ["product.certification", "merchant_knowledge.compliance"]
        contract.forbidden_inferences.extend([
            "product_existence_implies_certification",
            "regulatory_world_knowledge",
        ])
        return contract

    if request.fact_kind == KIND_SHIPPING_COMPANIES:
        status, values = _capability_list(caps, "shipping")
        if not values:
            status, values = _capability_list(caps, "shipping", "companies")
        names: List[str] = []
        for item in values:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("code") or "").strip()
            else:
                name = str(item or "").strip()
            if name:
                names.append(name)
        if not names and facts is not None:
            for item in list(getattr(facts, "shipping_methods", None) or []):
                name = str(item or "").strip()
                if name:
                    names.append(name)
        contract.claimable_values = names
        contract.evidence_refs = ["merchant_capabilities.shipping.companies"]
        contract.status = STATUS_KNOWN_VALUE if names else (
            STATUS_KNOWN_EMPTY if status == STATUS_KNOWN_EMPTY else STATUS_UNKNOWN
        )
        contract.forbidden_inferences.extend([
            "carrier_implies_fee",
            "carrier_implies_eta",
            "carrier_implies_everywhere",
        ])
        return contract

    if request.fact_kind in {KIND_PAYMENT_METHODS, KIND_CASH_ON_DELIVERY}:
        status, values = _capability_list(caps, "payments")
        methods: List[str] = []
        for item in values:
            if isinstance(item, dict):
                code = str(item.get("code") or item.get("label") or "").strip()
            else:
                code = str(item or "").strip()
            if code:
                methods.append(code)
        if not methods and facts is not None:
            methods = [str(x).strip() for x in (getattr(facts, "payment_methods", None) or []) if str(x).strip()]
        contract.claimable_values = methods
        contract.evidence_refs = ["merchant_capabilities.payments.methods"]
        contract.status = STATUS_KNOWN_VALUE if methods else (
            STATUS_KNOWN_EMPTY if status == STATUS_KNOWN_EMPTY else STATUS_UNKNOWN
        )
        if request.fact_kind == KIND_CASH_ON_DELIVERY:
            enabled = any(
                str(m).lower() in {"cod", "cash_on_delivery", "cash-on-delivery"}
                for m in methods
            )
            contract.claimable_values = ["cod"] if enabled else []
            contract.status = STATUS_KNOWN_VALUE if enabled else (
                STATUS_KNOWN_EMPTY if methods else STATUS_UNKNOWN
            )
        return contract

    if request.fact_kind in {KIND_SHIPPING_FEE, KIND_SHIPPING_ETA, KIND_SHIPPING_COVERAGE}:
        row = policy.get("shipping_policy") if isinstance(policy.get("shipping_policy"), dict) else {}
        policy_status = str((row or {}).get("status") or "UNKNOWN")
        contract.evidence_refs = ["merchant_policy.shipping_policy"]
        contract.status = STATUS_UNKNOWN
        contract.forbidden_inferences.extend([
            "carrier_implies_fee",
            "carrier_implies_eta",
            "checkout_implies_fee_display",
            "city_variation_without_evidence",
        ])
        if policy_status == "KNOWN_PRESENT" and request.fact_kind == "shipping_policy":
            contract.status = STATUS_KNOWN_VALUE
        return contract

    if request.fact_kind in _POLICY_KINDS:
        row = policy.get(request.fact_kind) if isinstance(policy.get(request.fact_kind), dict) else {}
        status = str((row or {}).get("status") or getattr(facts, f"policy_{request.fact_kind}_status", "") or "UNKNOWN")
        doc_ref = (row or {}).get("doc_ref") or getattr(facts, f"policy_{request.fact_kind}_doc_ref", None)
        contract.status = STATUS_KNOWN_VALUE if status == "KNOWN_PRESENT" else STATUS_UNKNOWN
        if doc_ref:
            contract.evidence_refs = [str(doc_ref)]
            contract.claimable_values = [str(doc_ref)]
        else:
            contract.evidence_refs = [f"merchant_policy.{request.fact_kind}"]
        return contract

    if request.fact_kind == KIND_GIFT_RECOMMENDATION:
        titles: List[str] = []
        if facts is not None:
            for item in list(getattr(facts, "top_products", None) or []):
                if isinstance(item, dict):
                    title = str(item.get("title") or "").strip()
                    if title:
                        titles.append(title)
        contract.claimable_values = titles
        contract.evidence_refs = ["commerce_facts.top_products"]
        contract.status = STATUS_KNOWN_VALUE if titles else STATUS_UNKNOWN
        contract.catalog_allowed = True
        contract.forbidden_inferences.extend([
            "invent_catalog_category",
            "recommend_absent_product_family",
        ])
        return contract

    return contract


def _unknown_goal(fact_kind: str) -> str:
    base = (
        f"answer_contract status=UNKNOWN for {fact_kind}. "
        "The customer asked an authoritative factual question. "
        "You may reason and speak naturally, but you must NOT invent "
        "the missing fact from world knowledge, neighboring facts, or "
        "catalog emptiness. Say you do not have confirmed information. "
        "Do not construct hours, branches, certifications, fees, ETAs, "
        "or payment methods that are not in claimable_values."
    )
    if fact_kind == KIND_BRANCH_EXISTENCE:
        return (
            base
            + " Forbidden inferences: branch_network_exists, "
            "branch_address_exists, branch_selectable. Do not imply "
            "that branches can be selected or that an address can be sent."
        )
    return base


def _known_goal(fact_kind: str) -> str:
    return (
        f"answer_contract status=KNOWN_VALUE for {fact_kind}. "
        "Use ONLY claimable_values from known_facts.answer_contract. "
        "Persona owns wording. Do not add neighboring-domain claims "
        "(carrier ≠ fee/ETA; maps ≠ branch network; catalog ≠ store hours)."
    )


def build_fact_answer_decision(
    *,
    message: str,
    intent_name: str = "",
    facts: Any = None,
    merchant_context: Any = None,
    history: Optional[Sequence[Any]] = None,
    state: Any = None,
) -> Optional[Any]:
    """Decision for an authoritative fact question, or None if catalog/other owners apply."""
    req = classify_fact_answer(
        message, intent_name=intent_name, history=history,
    )
    if req is None:
        return None
    if fact_answer_yields_to_transactional(
        message,
        intent_name=intent_name,
        state=state,
        fact_kind=req.fact_kind,
    ):
        return None
    if req.fact_kind == KIND_GIFT_RECOMMENDATION and req.catalog_allowed:
        # Still own compose so recommendations stay inside catalog evidence.
        pass
    elif req.catalog_allowed:
        return None

    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    contract = build_fact_answer_contract(
        req, facts=facts, merchant_context=merchant_context, message=message,
    )

    if req.domain == DOMAIN_POLICY and req.fact_kind in _POLICY_KINDS:
        from .merchant_policy_intents import build_merchant_policy_decision  # noqa: PLC0415

        dec = build_merchant_policy_decision(
            message=message,
            facts=facts,
            merchant_context=merchant_context,
            question_kind=req.fact_kind,
        )
        if dec is not None:
            args = dict(dec.args or {})
            args["answer_contract"] = contract.to_dict()
            args["block_catalog_navigation"] = True
            return Decision(
                action=dec.action,
                args=args,
                reason=f"fact_answer policy kind={req.fact_kind} status={contract.status}",
                confidence=getattr(dec, "confidence", 0.9) or 0.9,
            )

    topic_map = {
        KIND_WORKING_HOURS: "working_hours",
        KIND_OPEN_NOW: "working_hours",
        KIND_BRANCH_EXISTENCE: "location_delivery",
        KIND_LOCATION: "location_delivery",
        KIND_CERTIFICATION: "product_certification",
        KIND_SHIPPING_FEE: "shipping_fee",
        KIND_SHIPPING_ETA: "shipping_eta",
        KIND_SHIPPING_COVERAGE: "shipping_inquiry",
        KIND_SHIPPING_COMPANIES: "shipping_inquiry",
        KIND_PAYMENT_METHODS: "merchant_payment_methods",
        KIND_CASH_ON_DELIVERY: "cash_on_delivery",
        KIND_GIFT_RECOMMENDATION: "catalog_gift_recommendation",
    }
    topic = topic_map.get(req.fact_kind, req.fact_kind)
    goal = (
        _known_goal(req.fact_kind)
        if contract.status == STATUS_KNOWN_VALUE
        else _unknown_goal(req.fact_kind)
    )
    if req.fact_kind == KIND_GIFT_RECOMMENDATION:
        goal = (
            "Gift recommendation: reason creatively only over claimable_values "
            "(real catalog titles). Do not invent categories or product families "
            "absent from those values. If none, ask a natural clarifying question "
            "without naming unsupported categories."
        )
    args: Dict[str, Any] = {
        "topic": topic,
        "question_kind": req.fact_kind,
        "fact_domain": req.domain,
        "answer_contract": contract.to_dict(),
        "response_goal": goal,
        "block_catalog_navigation": not req.catalog_allowed,
    }
    if req.fact_kind in {KIND_SHIPPING_COMPANIES, KIND_PAYMENT_METHODS, KIND_CASH_ON_DELIVERY}:
        args["capability_surface"] = "salla_merchant_enabled"
        if req.fact_kind == KIND_SHIPPING_COMPANIES:
            args["topic"] = "shipping_inquiry"
            args["topic_hint"] = "shipping_companies"
            args["question_kind"] = "shipping_companies"
    if req.fact_kind in {KIND_SHIPPING_FEE, KIND_SHIPPING_ETA, KIND_SHIPPING_COVERAGE}:
        args.setdefault("topic_hint", "shipping")
    if req.fact_kind in {KIND_WORKING_HOURS, KIND_OPEN_NOW, KIND_BRANCH_EXISTENCE, KIND_LOCATION}:
        args["profile_surface"] = "merchant_profile"
    return Decision(
        action=ACTION_LLM_REPLY,
        args=args,
        reason=f"fact_answer kind={req.fact_kind} status={contract.status}",
        confidence=0.93,
    )


__all__ = [
    "FactAnswerContract",
    "FactAnswerRequest",
    "STATUS_KNOWN_EMPTY",
    "STATUS_KNOWN_VALUE",
    "STATUS_UNKNOWN",
    "build_fact_answer_contract",
    "build_fact_answer_decision",
    "catalog_must_yield_to_fact_owner",
    "classify_fact_answer",
    "fact_answer_owns_non_catalog_turn",
    "fact_answer_yields_to_transactional",
]
