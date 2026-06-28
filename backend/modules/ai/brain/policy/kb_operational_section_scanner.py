"""
Read-only scan of merchant KB sections for operational policy signals.

Does not copy section bodies into hints — only short evidence tokens and
section refs.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from services.knowledge_section_kinds import BEHAVIORAL_KINDS

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

OPERATIONAL_SCAN_KINDS: FrozenSet[str] = frozenset({
    "custom",
    "quick_update",
    "branches",
    "escalation_rules",
    "faq",
}) | BEHAVIORAL_KINDS

# Policy condition: customer visit / showroom / arrival context in KB prose.
_SHOWROOM_CONDITION_RE = re.compile(
    r"(?:"
    r"زيارة\s*(?:ال)?(?:معرض|فرع|محل|متجر)?"
    r"|(?:^|\s)يريد\s*(?:زيارة|الزيارة)"
    r"|(?:^|\s)عند\s*زيارة"
    r"|(?:^|\s)اذا\s*(?:قال|طلب)\s*العميل"
    r"|(?:^|\s)إذا\s*(?:قال|طلب)\s*العميل"
    r"|(?:^|\s)عند\s*الوصول"
    r"|(?:^|\s)في\s*الطريق"
    r"|(?:^|\s)وصل(?:ت|نا|وا)?"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_LOCATION_ACTION_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:ارسل|أرسل|ارسلي|أرسلي)\s*(?:ال)?(?:موقع|الموقع|موقع\s*ال(?:فرع|معرض|محل|متجر))"
    r"|(?:^|\s)(?:ارسل|أرسل)\s*(?:ال)?(?:فرع|معرض|محل|متجر|خريطة|رابط\s*الموقع)"
    r"|(?:^|\s)(?:موقع|الموقع|خريطة|maps|google\s*maps)"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_CONTACT_ACTION_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:ارسل|أرسل|ارسلي|أرسلي)(?:\s*(?:رقم|ال)?)?"
    r"|(?:^|\s)(?:اعط|أعط|اعطي|أعطي)(?:\s*(?:رقم|ال)?)?"
    r"|(?:^|\s)رقم\s*(?:ال)?(?:بائع|موظف|مندوب|البائع|الموظف)"
    r"|(?:^|\s)(?:بائع\s*المعرض|البائع|الموظف|المندوب)"
    r"|(?:^|\s)(?:بيانات\s*التواصل|جهة\s*التواصل|رقم\s*ال(?:بائع|موظف))"
    r"|(?:^|\s)(?:خدمة\s*العملاء|رقم\s*خدمة)"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_CONFIGURED_ONLY_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:الم(?:ه|ه)ي(?:أ|ا)ة\s*فقط|الم(?:ه|ه)ي(?:أ|ا)\s*فقط)"
    r"|(?:^|\s)(?:جهة\s*التواصل\s*الم(?:ه|ه)ي(?:أ|ا)ة)"
    r"|(?:^|\s)(?:configured\s*contact|configured\s*only)"
    r"|(?:^|\s)(?:لا\s*(?:ت(?:ذكر|ذكر)|(?:ت(?:ستخدم|ستخدم)))\s*(?:اسم|رقم)\s*(?:موظف|بائع))"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_ESCALATE_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:ص(?:ع|ّ)د|ص(?:ع|ّ)دي|التصعيد)"
    r"|(?:^|\s)(?:المستوى\s*(?:ال)?(?:اول|أول|1|ثاني|ثان|2|ثالث|3))"
    r"|(?:^|\s)(?:escalat)"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_FORBID_BROWSE_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:لا\s*(?:ت(?:عرض|ذكر)|(?:ت(?:ستخدم|ستخدم)))\s*(?:ال)?(?:كتالوج|منتج|انواع|أنواع))"
    r"|(?:^|\s)(?:بدون\s*(?:كتالوج|منتج|browse|catalog))"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_NAMED_STAFF_RE = re.compile(
    r"(?:"
    r"(?:^|\s)(?:اذكر|أذكر|قل\s*اسم|اسم\s*(?:ال)?(?:موظف|بائع))"
    r"|(?:^|\s)(?:named\s*staff|staff\s*name)"
    r")",
    re.IGNORECASE | re.UNICODE,
)

_METADATA_BOOL_TRUE = frozenset({"1", "true", "yes", "on"})

SIGNAL_SHOWROOM_CONDITION = "showroom_condition"
SIGNAL_SEND_LOCATION = "send_location"
SIGNAL_SEND_CONTACT = "send_contact"
SIGNAL_CONTACT_CONFIGURED_ONLY = "contact_configured_only"
SIGNAL_ESCALATE = "escalate"
SIGNAL_ESCALATION_LEVELS = "escalation_levels"
SIGNAL_FORBID_BROWSE = "forbid_browse_on_visit"
SIGNAL_CONTACT_FREEFORM = "contact_freeform"
SIGNAL_NAMED_STAFF = "named_staff_allowed"


@dataclass(frozen=True)
class SectionPolicyScan:
    section_ref: str
    kind: str
    signals: FrozenSet[str]


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


def _metadata_dict(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def _metadata_bool_true(value: Any) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    return str(value).strip().lower() in _METADATA_BOOL_TRUE


def _section_fields(section: Any) -> tuple[str, str, str, Dict[str, Any], Optional[int]]:
    if isinstance(section, dict):
        kind = str(section.get("kind") or "").strip().lower()
        title = str(section.get("title") or "").strip()
        body = str(section.get("body") or "").strip()
        meta = _metadata_dict(section.get("metadata") or section.get("metadata_json"))
        sid = section.get("id")
    else:
        kind = str(getattr(section, "kind", "") or "").strip().lower()
        title = str(getattr(section, "title", "") or "").strip()
        body = str(getattr(section, "body", "") or "").strip()
        meta = _metadata_dict(getattr(section, "metadata_json", None) or getattr(section, "metadata", None))
        sid = getattr(section, "id", None)
    try:
        section_id = int(sid) if sid is not None else None
    except (TypeError, ValueError):
        section_id = None
    return kind, title, body, meta, section_id


def _section_ref(kind: str, section_id: Optional[int]) -> str:
    slug = kind or "section"
    if section_id is not None:
        return f"{slug}:{section_id}"
    return slug


def _scan_metadata_signals(meta: Dict[str, Any]) -> FrozenSet[str]:
    signals: set[str] = set()
    if not meta:
        return frozenset()
    if _metadata_bool_true(meta.get("arrival_contact")):
        signals.add(SIGNAL_SHOWROOM_CONDITION)
        signals.add(SIGNAL_SEND_CONTACT)
    if _metadata_bool_true(meta.get("arriving_customer_contact_policy")):
        signals.add(SIGNAL_SHOWROOM_CONDITION)
        signals.add(SIGNAL_SEND_CONTACT)
    intent = str(meta.get("intent") or "").strip().lower()
    artifact = str(meta.get("artifact_target") or "").strip().lower()
    if intent == "ask_location_or_arrival_help" and artifact == "maps_link_or_staff_contact":
        signals.add(SIGNAL_SHOWROOM_CONDITION)
        signals.add(SIGNAL_SEND_LOCATION)
        signals.add(SIGNAL_SEND_CONTACT)
    if _metadata_bool_true(meta.get("require_configured_contact")):
        signals.add(SIGNAL_CONTACT_CONFIGURED_ONLY)
    if _metadata_bool_true(meta.get("forbid_browse_on_visit")):
        signals.add(SIGNAL_FORBID_BROWSE)
    return frozenset(signals)


def _scan_text_signals(text: str) -> FrozenSet[str]:
    norm = _norm(text)
    if not norm:
        return frozenset()
    signals: set[str] = set()
    if _SHOWROOM_CONDITION_RE.search(norm):
        signals.add(SIGNAL_SHOWROOM_CONDITION)
    if _LOCATION_ACTION_RE.search(norm):
        signals.add(SIGNAL_SEND_LOCATION)
    if _CONTACT_ACTION_RE.search(norm):
        signals.add(SIGNAL_SEND_CONTACT)
    if _CONFIGURED_ONLY_RE.search(norm):
        signals.add(SIGNAL_CONTACT_CONFIGURED_ONLY)
    if _ESCALATE_RE.search(norm):
        signals.add(SIGNAL_ESCALATE)
        if re.search(r"مستوى", norm):
            signals.add(SIGNAL_ESCALATION_LEVELS)
    if _FORBID_BROWSE_RE.search(norm):
        signals.add(SIGNAL_FORBID_BROWSE)
    if _NAMED_STAFF_RE.search(norm):
        signals.add(SIGNAL_NAMED_STAFF)
    elif SIGNAL_SEND_CONTACT in signals and SIGNAL_CONTACT_CONFIGURED_ONLY not in signals:
        signals.add(SIGNAL_CONTACT_FREEFORM)
    return frozenset(signals)


def scan_operational_sections(
    sections: Sequence[Any],
    *,
    settings: Optional[Mapping[str, Any]] = None,
) -> Tuple[SectionPolicyScan, ...]:
    """Scan KB sections and legacy settings for operational policy signals."""
    results: List[SectionPolicyScan] = []

    for section in sections or ():
        kind, title, body, meta, section_id = _section_fields(section)
        if kind and kind not in OPERATIONAL_SCAN_KINDS:
            continue
        combined = f"{title}\n{body}".strip()
        signals = set(_scan_metadata_signals(meta))
        signals.update(_scan_text_signals(combined))
        if not signals:
            continue
        results.append(
            SectionPolicyScan(
                section_ref=_section_ref(kind, section_id),
                kind=kind or "section",
                signals=frozenset(signals),
            )
        )

    legacy_text = _legacy_settings_text(settings)
    if legacy_text:
        legacy_signals = _scan_text_signals(legacy_text)
        if legacy_signals:
            results.append(
                SectionPolicyScan(
                    section_ref="legacy:escalation_rules",
                    kind="escalation_rules",
                    signals=legacy_signals,
                )
            )

    return tuple(results)


def _legacy_settings_text(settings: Optional[Mapping[str, Any]]) -> str:
    if not settings:
        return ""
    direct = str(settings.get("escalation_rules") or "").strip()
    if direct:
        return direct
    ai = settings.get("ai_settings")
    if isinstance(ai, dict):
        return str(ai.get("escalation_rules") or "").strip()
    return ""


def load_operational_kb_sections(
    db: Any,
    tenant_id: int,
) -> tuple[Sequence[Any], Optional[Mapping[str, Any]]]:
    """Load AI-visible KB sections + legacy escalation settings for a tenant."""
    sections: Sequence[Any] = ()
    settings: Optional[Mapping[str, Any]] = None
    if db is None or not tenant_id:
        return sections, settings

    try:
        from models import MerchantKnowledgeSection  # noqa: PLC0415
        from core.knowledge import apply_ai_visible_kb_query_filters  # noqa: PLC0415

        sections = (
            apply_ai_visible_kb_query_filters(
                db.query(MerchantKnowledgeSection)
            )
            .filter(MerchantKnowledgeSection.tenant_id == int(tenant_id))
            .order_by(
                MerchantKnowledgeSection.priority.asc(),
                MerchantKnowledgeSection.updated_at.desc(),
            )
            .limit(120)
            .all()
        )
    except Exception:
        sections = ()

    try:
        from models import TenantSettings  # noqa: PLC0415

        ts = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == int(tenant_id))
            .first()
        )
        if ts is not None:
            ai = dict(getattr(ts, "ai_settings", None) or {})
            meta = dict(getattr(ts, "extra_metadata", None) or {})
            settings = {
                "escalation_rules": (
                    ai.get("escalation_rules")
                    or meta.get("escalation_rules")
                    or ""
                ),
                "ai_settings": ai,
            }
    except Exception:
        settings = None

    return sections, settings


def aggregate_signals(scans: Iterable[SectionPolicyScan]) -> FrozenSet[str]:
    merged: set[str] = set()
    for scan in scans:
        merged.update(scan.signals)
    return frozenset(merged)


def detect_policy_conflicts(scans: Sequence[SectionPolicyScan]) -> bool:
    """True when KB sections disagree on contact delivery policy."""
    configured_only = False
    freeform = False
    named_staff = False
    for scan in scans:
        if SIGNAL_CONTACT_CONFIGURED_ONLY in scan.signals:
            configured_only = True
        if SIGNAL_CONTACT_FREEFORM in scan.signals:
            freeform = True
        if SIGNAL_NAMED_STAFF in scan.signals:
            named_staff = True
    if configured_only and (freeform or named_staff):
        return True
    if freeform and named_staff and not configured_only:
        return True
    return False
