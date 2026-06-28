"""
Merchant operational policy resolver — KB instructions → action hints (shadow).

Read-only: does not execute actions, compose replies, or enforce decisions.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, FrozenSet, Optional, Sequence, Tuple

from .contracts import (
    ContactPolicyHint,
    EscalationPolicyHint,
    MerchantOperationalPolicyHint,
    ShowroomPolicyHint,
)
from .kb_operational_section_scanner import (
    SIGNAL_CONTACT_CONFIGURED_ONLY,
    SIGNAL_ESCALATE,
    SIGNAL_ESCALATION_LEVELS,
    SIGNAL_FORBID_BROWSE,
    SIGNAL_NAMED_STAFF,
    SIGNAL_SEND_CONTACT,
    SIGNAL_SEND_LOCATION,
    SIGNAL_SHOWROOM_CONDITION,
    SectionPolicyScan,
    aggregate_signals,
    detect_policy_conflicts,
    load_operational_kb_sections,
    scan_operational_sections,
)

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

ACTION_SEND_STORE_LOCATION = "send_store_location"
ACTION_SEND_CONFIGURED_CONTACT = "send_configured_contact"
ACTION_ESCALATE = "escalate"
ACTION_BROWSE_PRODUCTS = "browse_products"
ACTION_CATALOG_PROMISE = "catalog_promise"
ACTION_ASK_PRODUCT = "ask_product"
ACTION_LLM_COMPOSE = "llm_compose"

_DEFAULT_ALLOWED = frozenset({ACTION_LLM_COMPOSE})

_SHOWROOM_INPUT_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:اب(?:غ|غ)ى|اب(?:غ|غ)ي|أب(?:غ|غ)ى|أب(?:غ|غ)ي)\s*(?:اجي|أجي|اجيك|أجيك|ازور|أزور|زور)"
    r"|(?:^|\s)(?:ب(?:غ|غ)ى|ب(?:غ|غ)ي)\s*(?:اجي|أجي|اجيك|أجيك|ازور|أزور)"
    r"|(?:^|\s)(?:جا(?:ي|يك)(?:كم|ك|ين)?)"
    r"|(?:^|\s)(?:وصل(?:ت|نا|وا)?)"
    r"|(?:^|\s)(?:في\s*الطريق)"
    r"|(?:^|\s)(?:زيارة|زياره|زور(?:ني|نا)?)"
    r"|(?:^|\s)(?:عند(?:كم|ك)?|عند\s*(?:ال)?(?:معرض|فرع|محل|متجر|بواب(?:ة|ه)))"
    r"|(?:^|\s)(?:انا|أنا)\s*(?:في|ب(?:ـ)?)\s*(?:ال)?(?:طائف|رياض|جده|جدة|مكه|مكة|دمام|خبر|مدين(?:ه|ة))"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_CONTACT_REQUEST_INPUT_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:ارسل|أرسل|ارسلي|أرسلي)\s*(?:ال)?(?:ارقام|أرقام|رقم|الرقم|جوال|هاتف|تواصل)"
    r"|(?:^|\s)(?:ال)?(?:ارقام|أرقام)\s*(?:لاهنت|لو\s*س(?:مح|مح)ت|من\s*ف(?:ض|ض)لك)"
    r"|(?:^|\s)(?:رقم|ارقام|أرقام)\s*(?:ال)?(?:موظف|بائع|البائع|الموظف|خدمة\s*العملاء)"
    r"|(?:^|\s)(?:ممكن|ممكن\s*(?:ترسل|تعطيني|تذكر))\s*(?:رقم|جوال|هاتف|ارقام|أرقام)"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_BROWSE_INPUT_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:وش|ايش|إيش|what)\s*(?:ال)?(?:انواع|أنواع|types|options|choices|خيارات|منتجات|products)"
    r"|(?:^|\s)(?:ال)?(?:انواع|أنواع|خيارات|منتجات)\s*(?:المتوف(?:ر|رة|ره)|available|موجود(?:ه|ة)?)"
    r"|(?:^|\s)(?:عند(?:كم|ك)?|هل\s*(?:عند(?:كم|ك)?|متوفر))\s*(?:"
    r"(?:ال)?(?:انواع|أنواع|منتجات|خيارات)"
    r")"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_CONFIDENCE_REQUIRED_THRESHOLD = 0.7


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
    )
    return _WS_RE.sub(" ", t).strip()


def _detect_message_purpose(message: str) -> Optional[str]:
    norm = _norm(message)
    if not norm:
        return None
    if _SHOWROOM_INPUT_RE.search(norm):
        return "showroom_visit"
    if _CONTACT_REQUEST_INPUT_RE.search(norm):
        return "contact_request"
    if _BROWSE_INPUT_RE.search(norm):
        return "browse_discovery"
    return None


def _tenant_has_contact_config(db: Any, tenant_id: int, message: str) -> bool:
    if db is None or not tenant_id:
        return False
    try:
        from modules.operations.branch_contact_evidence import (  # noqa: PLC0415
            resolve_reception_contact,
            tenant_has_structured_branch_data,
        )

        if not tenant_has_structured_branch_data(db, int(tenant_id)):
            return False
        contact = resolve_reception_contact(db, int(tenant_id), message or "")
        return contact is not None and bool(str(contact.phone_e164 or "").strip())
    except Exception:
        return False


def _tenant_has_location_config(db: Any, tenant_id: int, message: str) -> bool:
    if db is None or not tenant_id:
        return False
    try:
        from modules.operations.branch_contact_evidence import (  # noqa: PLC0415
            lookup_structured_maps_url,
            tenant_has_structured_branch_data,
        )

        if not tenant_has_structured_branch_data(db, int(tenant_id)):
            return False
        maps_url, source, _branch_id = lookup_structured_maps_url(
            db, int(tenant_id), message or "",
        )
        return bool(maps_url and source != "none")
    except Exception:
        return False


def _relevant_scans(
    scans: Sequence[SectionPolicyScan],
    purpose: str,
) -> Tuple[SectionPolicyScan, ...]:
    if purpose == "showroom_visit":
        return tuple(
            s for s in scans
            if SIGNAL_SHOWROOM_CONDITION in s.signals
            or (
                SIGNAL_SEND_LOCATION in s.signals
                and SIGNAL_SEND_CONTACT in s.signals
            )
        )
    if purpose == "contact_request":
        return tuple(
            s for s in scans
            if SIGNAL_SEND_CONTACT in s.signals
        )
    return ()


def _build_showroom_hint(signals: FrozenSet[str]) -> ShowroomPolicyHint:
    return ShowroomPolicyHint(
        send_location_first=SIGNAL_SEND_LOCATION in signals,
        send_contact_after_location=(
            SIGNAL_SEND_LOCATION in signals and SIGNAL_SEND_CONTACT in signals
        ),
        escalate_after_contact=(
            SIGNAL_ESCALATE in signals and SIGNAL_SEND_CONTACT in signals
        ),
    )


def _build_contact_hint(
    signals: FrozenSet[str],
    *,
    source: str,
) -> ContactPolicyHint:
    configured_only = (
        SIGNAL_CONTACT_CONFIGURED_ONLY in signals
        or SIGNAL_NAMED_STAFF not in signals
    )
    return ContactPolicyHint(
        source=source,
        allow_named_staff=SIGNAL_NAMED_STAFF in signals and not configured_only,
        require_configured_only=configured_only,
    )


def _build_escalation_hint(signals: FrozenSet[str]) -> Optional[EscalationPolicyHint]:
    if SIGNAL_ESCALATE not in signals:
        return None
    return EscalationPolicyHint(
        use_configured_levels=SIGNAL_ESCALATION_LEVELS in signals,
        start_level="level_1" if SIGNAL_ESCALATION_LEVELS in signals else "",
    )


def _compute_confidence(
    *,
    purpose: Optional[str],
    relevant_scans: Sequence[SectionPolicyScan],
    signals: FrozenSet[str],
    conflict: bool,
) -> float:
    if conflict:
        return 0.2
    if not purpose or not relevant_scans:
        return 0.0
    score = 0.35
    if len(relevant_scans) == 1:
        score += 0.15
    if purpose == "showroom_visit":
        if SIGNAL_SHOWROOM_CONDITION in signals:
            score += 0.2
        if SIGNAL_SEND_LOCATION in signals and SIGNAL_SEND_CONTACT in signals:
            score += 0.2
        if SIGNAL_ESCALATE in signals:
            score += 0.1
    elif purpose == "contact_request":
        if SIGNAL_SEND_CONTACT in signals:
            score += 0.25
        if SIGNAL_CONTACT_CONFIGURED_ONLY in signals:
            score += 0.15
    return min(score, 1.0)


def resolve_merchant_operational_policy_hint(
    db: Any,
    tenant_id: int,
    message: str,
    *,
    sections: Optional[Sequence[Any]] = None,
    settings: Optional[Any] = None,
    has_contact_config: Optional[bool] = None,
    has_location_config: Optional[bool] = None,
) -> MerchantOperationalPolicyHint:
    """
    Resolve operational policy hints from KB + customer message.

    Test hooks ``sections``, ``settings``, ``has_contact_config``, and
    ``has_location_config`` avoid DB in unit tests.
    """
    if sections is None:
        loaded_sections, loaded_settings = load_operational_kb_sections(db, int(tenant_id or 0))
        sections = loaded_sections
        if settings is None:
            settings = loaded_settings

    scans = scan_operational_sections(sections or (), settings=settings)
    purpose = _detect_message_purpose(message or "")
    conflict = detect_policy_conflicts(scans)

    if purpose is None:
        return MerchantOperationalPolicyHint(
            allowed_actions=(ACTION_LLM_COMPOSE,),
            confidence=0.0,
            evidence=(),
            source_sections=tuple(s.section_ref for s in scans[:3]),
        )

    relevant = _relevant_scans(scans, purpose)
    signals = aggregate_signals(relevant) if relevant else frozenset()
    source_sections = tuple(s.section_ref for s in relevant)
    evidence = tuple(sorted(signals))[:8]

    confidence = _compute_confidence(
        purpose=purpose,
        relevant_scans=relevant,
        signals=signals,
        conflict=conflict,
    )

    allowed: set[str] = set(_DEFAULT_ALLOWED)
    forbidden: set[str] = set()
    showroom_hint: Optional[ShowroomPolicyHint] = None
    contact_hint: Optional[ContactPolicyHint] = None
    escalation_hint: Optional[EscalationPolicyHint] = None
    missing_config_reason: Optional[str] = None
    required_action: Optional[str] = None

    if purpose == "showroom_visit" and relevant and not conflict:
        showroom_hint = _build_showroom_hint(signals)
        escalation_hint = _build_escalation_hint(signals)
        contact_hint = _build_contact_hint(signals, source="showroom_policy")
        if SIGNAL_SEND_LOCATION in signals:
            allowed.add(ACTION_SEND_STORE_LOCATION)
        if SIGNAL_SEND_CONTACT in signals:
            allowed.add(ACTION_SEND_CONFIGURED_CONTACT)
        if SIGNAL_ESCALATE in signals:
            allowed.add(ACTION_ESCALATE)
        if SIGNAL_FORBID_BROWSE in signals or (
            SIGNAL_SHOWROOM_CONDITION in signals
            and SIGNAL_SEND_LOCATION in signals
        ):
            forbidden.update({
                ACTION_BROWSE_PRODUCTS,
                ACTION_CATALOG_PROMISE,
                ACTION_ASK_PRODUCT,
            })

    elif purpose == "contact_request" and relevant and not conflict:
        contact_hint = _build_contact_hint(signals, source="contact_request_policy")
        allowed.add(ACTION_SEND_CONFIGURED_CONTACT)

    elif purpose == "browse_discovery":
        allowed.add(ACTION_BROWSE_PRODUCTS)
        allowed.add(ACTION_CATALOG_PROMISE)
        return MerchantOperationalPolicyHint(
            response_purpose=purpose,
            allowed_actions=tuple(sorted(allowed)),
            forbidden_actions=(),
            confidence=0.0,
            evidence=evidence,
            source_sections=source_sections,
            conflict=conflict,
        )

    if conflict:
        return MerchantOperationalPolicyHint(
            response_purpose=purpose,
            allowed_actions=(ACTION_LLM_COMPOSE,),
            forbidden_actions=tuple(sorted(forbidden)),
            contact_policy_hint=contact_hint,
            showroom_policy_hint=showroom_hint,
            escalation_policy_hint=escalation_hint,
            confidence=confidence,
            evidence=evidence,
            source_sections=source_sections,
            conflict=True,
        )

    needs_contact = ACTION_SEND_CONFIGURED_CONTACT in allowed
    needs_location = ACTION_SEND_STORE_LOCATION in allowed

    if has_contact_config is None:
        has_contact_config = _tenant_has_contact_config(db, int(tenant_id or 0), message or "")
    if has_location_config is None:
        has_location_config = _tenant_has_location_config(db, int(tenant_id or 0), message or "")

    if needs_contact and not has_contact_config:
        missing_config_reason = "contact_requested_but_missing_config"
        allowed = {ACTION_LLM_COMPOSE}
        required_action = None
        confidence = min(confidence, 0.4)
    elif needs_location and not has_location_config and purpose == "showroom_visit":
        missing_config_reason = "location_requested_but_missing_config"
        if ACTION_SEND_CONFIGURED_CONTACT in allowed and has_contact_config:
            allowed.discard(ACTION_SEND_STORE_LOCATION)
        elif not has_contact_config:
            missing_config_reason = "contact_requested_but_missing_config"
            allowed = {ACTION_LLM_COMPOSE}
            required_action = None
            confidence = min(confidence, 0.4)

    if (
        confidence >= _CONFIDENCE_REQUIRED_THRESHOLD
        and not missing_config_reason
        and purpose == "showroom_visit"
        and showroom_hint is not None
        and showroom_hint.send_location_first
    ):
        required_action = ACTION_SEND_STORE_LOCATION
    elif (
        confidence >= _CONFIDENCE_REQUIRED_THRESHOLD
        and not missing_config_reason
        and purpose == "contact_request"
        and contact_hint is not None
        and contact_hint.require_configured_only
    ):
        required_action = ACTION_SEND_CONFIGURED_CONTACT

    if confidence < _CONFIDENCE_REQUIRED_THRESHOLD:
        required_action = None

    return MerchantOperationalPolicyHint(
        response_purpose=purpose,
        required_action=required_action,
        allowed_actions=tuple(sorted(allowed)),
        forbidden_actions=tuple(sorted(forbidden)),
        escalation_policy_hint=escalation_hint,
        contact_policy_hint=contact_hint,
        showroom_policy_hint=showroom_hint,
        confidence=confidence,
        evidence=evidence,
        source_sections=source_sections,
        conflict=conflict,
        missing_config_reason=missing_config_reason,
    )
