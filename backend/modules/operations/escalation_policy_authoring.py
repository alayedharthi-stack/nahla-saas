"""Admin-side escalation authoring — NL instruction → structured draft.

Natural language is for merchant authoring only. Customer runtime must
load the confirmed structured policy; it must not re-parse this text.

This compiler is tenant-roster entity linking. It never invents contacts,
phone numbers, branches, or permissions.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from modules.operations.contact_visibility import (
    BOTH,
    CUSTOMER_VISIBLE,
    HANDOFF_CONVERSATION,
    INTERNAL_ONLY,
    NOTIFY_OR_HANDOFF,
    SHARE_CUSTOMER_CONTACT,
    WHATSAPP_CTA,
    default_action_for_visibility,
    normalize_action,
    normalize_visibility,
)

CONDITION_SEQUENCE = "sequence"
CONDITION_ARRIVAL = "arrival"
CONDITION_NO_RESPONSE = "no_response"
CONDITION_COMPLAINT = "complaint_urgent"

VALID_CONDITIONS = frozenset({
    CONDITION_SEQUENCE,
    CONDITION_ARRIVAL,
    CONDITION_NO_RESPONSE,
    CONDITION_COMPLAINT,
})

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")
_CLAUSE_SPLIT_RE = re.compile(r"[\n\r]+|(?=\b(?:إذا|اذا)\b)")
_NAME_AFTER_VERB_RE = re.compile(
    r"(?:اعطه|أعطه|اعطي|أعطي|أعطها|كلم|كلمي|صعد|صعّد|تواصل مع|حوله|حوّله)\s+"
    r"(?:رقم\s+|مع\s+)?([^\n،,.]+)",
    re.UNICODE,
)

# Authoring-time stopwords only. Not used on the customer runtime path.
_STOP = frozenset({
    "رقم", "الواتساب", "واتساب", "اذا", "إذا", "ثم", "او", "أو", "و",
    "العميل", "البائع", "خدمة", "العملاء", "الادارة", "الإدارة", "الموظف",
    "التالي", "البديل", "المعرض", "للمعرض", "رد", "لم", "ما", "احد", "أحد",
    "شكوى", "عاجلة", "عاجل", "اولا", "أولا", "بعدها", "بعدين", "ليه",
    "مع", "في", "من", "على", "إلى", "الى", "له", "لها", "هذا", "هذه",
})

_NEXT_TOKENS = ("التالي", "البديل", "اخر", "آخر", "موظف اخر", "موظف آخر")


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
        .replace("\u0629", "\u0647")
    )
    return _WS_RE.sub(" ", t).strip()


@dataclass(frozen=True)
class AuthoringContact:
    id: int
    display_name: str
    role: str
    phone_e164: str
    whatsapp_e164: str
    branch_id: int
    branch_name: str
    customer_visibility: str
    is_active: bool = True


@dataclass
class DraftStep:
    order: int
    contact_id: int
    display_name: str
    role: str
    branch_id: int
    branch_name: str
    customer_visibility: str
    permitted_action: str
    trigger_condition: str
    live_phone_e164: str = ""
    customer_share_allowed: bool = False

    def preview_action_label(self) -> str:
        labels = {
            SHARE_CUSTOMER_CONTACT: "مشاركة الرقم",
            WHATSAPP_CTA: "إرسال واتساب",
            NOTIFY_OR_HANDOFF: "تنبيه الموظف",
            HANDOFF_CONVERSATION: "تحويل المحادثة",
        }
        return labels.get(self.permitted_action, self.permitted_action)


@dataclass
class UnresolvedReference:
    token: str
    reason: str
    clause: str = ""


@dataclass
class PolicyDraft:
    instruction_text: str
    branch_id: Optional[int]
    steps: List[DraftStep] = field(default_factory=list)
    unresolved: List[UnresolvedReference] = field(default_factory=list)
    ambiguities: List[str] = field(default_factory=list)
    confirmation_required: bool = True
    invented_contacts: bool = False
    invented_numbers: bool = False
    can_confirm: bool = False
    change_summary: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        for step in payload.get("steps") or []:
            step["preview_action_label"] = DraftStep(
                order=int(step.get("order") or 0),
                contact_id=int(step.get("contact_id") or 0),
                display_name=str(step.get("display_name") or ""),
                role=str(step.get("role") or ""),
                branch_id=int(step.get("branch_id") or 0),
                branch_name=str(step.get("branch_name") or ""),
                customer_visibility=str(step.get("customer_visibility") or ""),
                permitted_action=str(step.get("permitted_action") or ""),
                trigger_condition=str(step.get("trigger_condition") or ""),
            ).preview_action_label()
        return payload


def contact_from_row(row: Any, *, branch_name: str = "") -> AuthoringContact:
    vis = normalize_visibility(getattr(row, "customer_visibility", INTERNAL_ONLY))
    return AuthoringContact(
        id=int(row.id),
        display_name=str(row.display_name or "").strip(),
        role=str(row.role or "").strip(),
        phone_e164=str(row.phone_e164 or "").strip(),
        whatsapp_e164=str(getattr(row, "whatsapp_e164", "") or "").strip(),
        branch_id=int(row.branch_id),
        branch_name=branch_name or str(getattr(getattr(row, "branch", None), "name", "") or ""),
        customer_visibility=vis,
        is_active=bool(getattr(row, "is_active", True)),
    )


def load_tenant_contacts(db: Any, tenant_id: int) -> Tuple[AuthoringContact, ...]:
    if db is None or not tenant_id:
        return ()
    from models import BranchContact, MerchantBranch  # noqa: PLC0415

    rows = (
        db.query(BranchContact, MerchantBranch)
        .join(MerchantBranch, MerchantBranch.id == BranchContact.branch_id)
        .filter(
            MerchantBranch.tenant_id == int(tenant_id),
            BranchContact.is_active.is_(True),
            MerchantBranch.is_active.is_(True),
        )
        .order_by(
            MerchantBranch.sort_order.asc(),
            BranchContact.sort_order.asc(),
            BranchContact.id.asc(),
        )
        .all()
    )
    out: List[AuthoringContact] = []
    for contact, branch in rows:
        out.append(contact_from_row(contact, branch_name=str(branch.name or "")))
    return tuple(out)


def _condition_for_clause(clause_norm: str) -> str:
    if any(tok in clause_norm for tok in ("وصول", "وصلت", "معرض", "فرع")):
        return CONDITION_ARRIVAL
    if any(tok in clause_norm for tok in ("ما رد", "لم يرد", "ما ردوا", "ما جاوب")):
        return CONDITION_NO_RESPONSE
    if any(tok in clause_norm for tok in ("شكوى", "عاجل", "الاداره", "الادارة")):
        return CONDITION_COMPLAINT
    return CONDITION_SEQUENCE


def _longest_matches(
    clause_norm: str,
    contacts: Sequence[AuthoringContact],
    used_ids: set[int],
) -> List[AuthoringContact]:
    ranked: List[Tuple[int, int, AuthoringContact]] = []
    for contact in contacts:
        if contact.id in used_ids:
            continue
        name_n = _norm(contact.display_name)
        role_n = _norm(contact.role)
        best_pos = -1
        best_len = 0
        for token in (name_n, role_n):
            if not token or len(token) < 2:
                continue
            pos = clause_norm.find(token)
            if pos < 0:
                continue
            if len(token) > best_len:
                best_len = len(token)
                best_pos = pos
        if best_pos >= 0:
            ranked.append((best_pos, -best_len, contact))
    ranked.sort()
    return [item[2] for item in ranked]


def _person_like_tokens(clause: str) -> List[str]:
    found: List[str] = []
    for match in _NAME_AFTER_VERB_RE.finditer(clause or ""):
        raw = (match.group(1) or "").strip()
        parts = [p for p in re.split(r"\s+", raw) if p]
        buf: List[str] = []
        for part in parts[:3]:
            n = _norm(part)
            if not n or n in _STOP or len(n) < 2:
                if buf:
                    break
                continue
            buf.append(part.strip("؟?.,،"))
        if buf:
            found.append(" ".join(buf))
    return found


def _next_same_role(
    contacts: Sequence[AuthoringContact],
    used_ids: set[int],
    role: str,
) -> Optional[AuthoringContact]:
    role_n = _norm(role)
    if not role_n:
        return None
    for contact in contacts:
        if contact.id in used_ids:
            continue
        if _norm(contact.role) == role_n:
            return contact
    return None


def _step_from_contact(
    contact: AuthoringContact,
    *,
    order: int,
    condition: str,
    action_override: str = "",
) -> DraftStep:
    vis = normalize_visibility(contact.customer_visibility)
    action = normalize_action(action_override) if action_override else default_action_for_visibility(vis)
    share = vis in {CUSTOMER_VISIBLE, BOTH} and action in {
        SHARE_CUSTOMER_CONTACT, WHATSAPP_CTA,
    }
    return DraftStep(
        order=order,
        contact_id=contact.id,
        display_name=contact.display_name,
        role=contact.role,
        branch_id=contact.branch_id,
        branch_name=contact.branch_name,
        customer_visibility=vis,
        permitted_action=action,
        trigger_condition=condition if condition in VALID_CONDITIONS else CONDITION_SEQUENCE,
        live_phone_e164=contact.phone_e164 if share else "",
        customer_share_allowed=share,
    )


def compile_instruction(
    instruction_text: str,
    contacts: Sequence[AuthoringContact],
    *,
    branch_id: Optional[int] = None,
    resolutions: Optional[Dict[str, int]] = None,
    existing_steps: Optional[Sequence[DraftStep]] = None,
) -> PolicyDraft:
    text = (instruction_text or "").strip()
    roster = [c for c in contacts if c.is_active]
    draft = PolicyDraft(instruction_text=text, branch_id=branch_id)
    if not text:
        draft.ambiguities.append("empty_instruction")
        return draft
    if not roster:
        draft.ambiguities.append("no_contacts")
        return draft

    by_id = {c.id: c for c in roster}
    used: set[int] = set()
    last_role = ""
    clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(text) if c and c.strip()]
    if not clauses:
        clauses = [text]

    resolution_map = {
        _norm(k): int(v)
        for k, v in (resolutions or {}).items()
        if str(k).strip() and int(v or 0) in by_id
    }

    for clause in clauses:
        clause_n = _norm(clause)
        condition = _condition_for_clause(clause_n)
        matched = _longest_matches(clause_n, roster, used)
        wants_next = any(tok in clause_n for tok in _NEXT_TOKENS)
        if wants_next and last_role:
            nxt = _next_same_role(roster, used, last_role)
            if nxt is not None and nxt not in matched:
                matched.append(nxt)
        if not matched:
            for token in _person_like_tokens(clause):
                token_n = _norm(token)
                resolved_id = resolution_map.get(token_n)
                if resolved_id and resolved_id not in used:
                    matched.append(by_id[resolved_id])
                    continue
                known = any(
                    token_n == _norm(c.display_name) or token_n == _norm(c.role)
                    for c in roster
                )
                if not known:
                    draft.unresolved.append(
                        UnresolvedReference(
                            token=token,
                            reason="unknown_person",
                            clause=clause,
                        )
                    )

        for contact in matched:
            if contact.id in used:
                continue
            used.add(contact.id)
            last_role = contact.role
            draft.steps.append(
                _step_from_contact(
                    contact,
                    order=len(draft.steps) + 1,
                    condition=condition,
                )
            )

    if not draft.steps and not draft.unresolved:
        draft.ambiguities.append("no_contact_references")

    if existing_steps:
        old_ids = [int(s.contact_id) for s in existing_steps]
        new_ids = [s.contact_id for s in draft.steps]
        if old_ids != new_ids:
            draft.change_summary.append("escalation_sequence_changed")

    draft.can_confirm = bool(draft.steps) and not draft.unresolved
    draft.confirmation_required = True
    draft.invented_contacts = False
    draft.invented_numbers = False
    return draft


def steps_from_existing_rows(
    rows: Sequence[Any],
    contacts: Sequence[AuthoringContact],
) -> List[DraftStep]:
    by_id = {c.id: c for c in contacts}
    steps: List[DraftStep] = []
    for idx, row in enumerate(rows, start=1):
        cid = int(getattr(row, "contact_id", 0) or 0)
        contact = by_id.get(cid)
        if contact is None:
            continue
        steps.append(
            _step_from_contact(
                contact,
                order=idx,
                condition=str(getattr(row, "trigger_condition", "") or CONDITION_SEQUENCE),
                action_override=str(getattr(row, "permitted_action", "") or ""),
            )
        )
    return steps


def apply_confirmed_draft(
    db: Any,
    *,
    tenant_id: int,
    branch_id: int,
    draft: PolicyDraft,
) -> Dict[str, Any]:
    """Persist confirmed steps. Replaces the branch ladder; does not invent rows."""
    from models import BranchContact, BranchEscalationStep, MerchantBranch  # noqa: PLC0415

    branch = (
        db.query(MerchantBranch)
        .filter(
            MerchantBranch.id == int(branch_id),
            MerchantBranch.tenant_id == int(tenant_id),
        )
        .first()
    )
    if branch is None:
        raise ValueError("branch_not_found")
    if not draft.can_confirm:
        raise ValueError("draft_not_confirmable")

    contact_ids = [s.contact_id for s in draft.steps]
    owned = {
        int(r.id)
        for r in (
            db.query(BranchContact)
            .join(MerchantBranch, MerchantBranch.id == BranchContact.branch_id)
            .filter(
                MerchantBranch.tenant_id == int(tenant_id),
                BranchContact.id.in_(contact_ids),
            )
            .all()
        )
    }
    if set(contact_ids) - owned:
        raise ValueError("contact_not_in_tenant")

    db.query(BranchEscalationStep).filter(
        BranchEscalationStep.branch_id == int(branch_id),
    ).delete(synchronize_session=False)

    persisted = []
    for step in draft.steps:
        contact = (
            db.query(BranchContact)
            .filter(BranchContact.id == int(step.contact_id))
            .first()
        )
        if contact is None:
            continue
        row = BranchEscalationStep(
            branch_id=int(branch_id),
            escalation_level=int(step.order),
            display_name=contact.display_name,
            role=contact.role,
            phone_e164=str(contact.phone_e164 or ""),
            contact_id=int(contact.id),
            permitted_action=step.permitted_action,
            trigger_condition=step.trigger_condition,
            is_active=True,
            sort_order=int(step.order) - 1,
        )
        db.add(row)
        persisted.append(step)

    branch.escalation_instruction_text = draft.instruction_text
    branch.escalation_policy_json = {
        "compiled_at": datetime.now(timezone.utc).isoformat(),
        "instruction_text": draft.instruction_text,
        "steps": [
            {
                "order": s.order,
                "contact_id": s.contact_id,
                "permitted_action": s.permitted_action,
                "trigger_condition": s.trigger_condition,
                "display_name": s.display_name,
                "role": s.role,
                "customer_visibility": s.customer_visibility,
            }
            for s in persisted
        ],
        "source": "merchant_confirmed_authoring",
    }
    branch.updated_at = datetime.now(timezone.utc)
    db.flush()
    return {
        "branch_id": int(branch.id),
        "instruction_text": draft.instruction_text,
        "steps": [asdict(s) for s in persisted],
    }


def apply_structured_sequence(
    db: Any,
    *,
    tenant_id: int,
    branch_id: int,
    steps: Sequence[Dict[str, Any]],
    instruction_text: str = "",
) -> PolicyDraft:
    contacts = load_tenant_contacts(db, tenant_id)
    by_id = {c.id: c for c in contacts}
    draft = PolicyDraft(
        instruction_text=(instruction_text or "").strip(),
        branch_id=branch_id,
    )
    for idx, raw in enumerate(steps, start=1):
        cid = int(raw.get("contact_id") or 0)
        contact = by_id.get(cid)
        if contact is None:
            draft.unresolved.append(
                UnresolvedReference(token=str(cid), reason="unknown_contact_id"),
            )
            continue
        if branch_id and contact.branch_id != int(branch_id):
            # Same tenant is allowed; sequence is stored on the selected branch
            # but the contact record remains the source of truth.
            pass
        condition = str(raw.get("trigger_condition") or CONDITION_SEQUENCE)
        action = str(raw.get("permitted_action") or "")
        draft.steps.append(
            _step_from_contact(
                contact,
                order=idx,
                condition=condition,
                action_override=action,
            )
        )
    draft.can_confirm = bool(draft.steps) and not draft.unresolved
    return draft
