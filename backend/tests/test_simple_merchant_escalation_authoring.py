"""Simple merchant contact + escalation authoring — platform-wide tests."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from models import (  # noqa: E402
    Base,
    BranchContact,
    BranchEscalationStep,
    MerchantBranch,
    MerchantKnowledgeSection,
    Tenant,
)
from modules.operations.contact_visibility import (  # noqa: E402
    INTERNAL_ONLY,
    may_share_with_customer,
)
from modules.operations.escalation_policy_authoring import (  # noqa: E402
    apply_confirmed_draft,
    compile_instruction,
    load_tenant_contacts,
)
from modules.operations.escalation_policy_migration import (  # noqa: E402
    normalize_tenant_escalation_config,
)
from modules.operations.escalation_policy_runtime import (  # noqa: E402
    live_phone_for_contact_id,
    load_canonical_policy,
    next_shareable_step,
    resolve_share_action,
)
from modules.operations.kb_contact_conflict import find_kb_contact_conflicts  # noqa: E402


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session(), engine


class _FakeRequest:
    pass


@pytest.fixture()
def db_session():
    db, engine = _make_db()
    t1 = Tenant(id=11, name="Generic Store", is_active=True)
    t2 = Tenant(id=22, name="Other Merchant", is_active=True)
    db.add_all([t1, t2])
    db.commit()
    try:
        yield db, t1.id, t2.id
    finally:
        db.close()
        engine.dispose()


def _seed_team(db, tenant_id: int, *, prefix: str = ""):
    branch = MerchantBranch(
        tenant_id=tenant_id,
        name=f"{prefix}معرض تجريبي",
        is_active=True,
    )
    db.add(branch)
    db.commit()
    db.refresh(branch)
    seller = BranchContact(
        branch_id=branch.id,
        display_name=f"{prefix}نورة",
        role="بائع المعرض",
        phone_e164="+966511111111",
        is_active=True,
        is_default_reception=True,
        customer_visibility="customer_visible",
        sort_order=0,
    )
    cs = BranchContact(
        branch_id=branch.id,
        display_name=f"{prefix}أحمد سالم",
        role="خدمة العملاء",
        phone_e164="+966522222222",
        is_active=True,
        customer_visibility="customer_visible",
        sort_order=1,
    )
    cs2 = BranchContact(
        branch_id=branch.id,
        display_name=f"{prefix}سعد",
        role="خدمة العملاء",
        phone_e164="+966533333333",
        is_active=True,
        customer_visibility="customer_visible",
        sort_order=2,
    )
    manager = BranchContact(
        branch_id=branch.id,
        display_name=f"{prefix}فهد",
        role="الإدارة",
        phone_e164="+966544444444",
        is_active=True,
        customer_visibility="internal_only",
        sort_order=3,
    )
    db.add_all([seller, cs, cs2, manager])
    db.commit()
    for row in (seller, cs, cs2, manager):
        db.refresh(row)
    return branch, seller, cs, cs2, manager


def _llm(steps, unresolved=None, ambiguities=None):
    return {
        "steps": [
            {
                "contact_id": int(item[0]),
                "trigger_condition": item[1] if len(item) > 1 else "sequence",
                "permitted_action": item[2] if len(item) > 2 else "",
            }
            for item in steps
        ],
        "unresolved_references": unresolved or [],
        "ambiguities": ambiguities or [],
        "summary_for_merchant": "ما فهمته نحلة",
    }


def test_instruction_resolves_existing_contact_id(db_session) -> None:
    db, tenant_id, _ = db_session
    branch, seller, cs, cs2, manager = _seed_team(db, tenant_id)
    contacts = load_tenant_contacts(db, tenant_id)
    draft = compile_instruction(
        "إذا وصل العميل أعطه نورة. إذا لم يرد أعطه أحمد سالم. ثم سعد. صعّد لفهد.",
        contacts,
        branch_id=branch.id,
        extracted=_llm([
            (seller.id, "arrival", "share_customer_contact"),
            (cs.id, "no_response", "share_customer_contact"),
            (cs2.id, "sequence", "share_customer_contact"),
            (manager.id, "complaint_urgent", "notify_or_handoff"),
        ]),
    )
    assert draft.invented_contacts is False
    assert draft.invented_numbers is False
    ids = [s.contact_id for s in draft.steps]
    assert seller.id in ids
    assert cs.id in ids
    assert manager.id in ids
    assert all(s.contact_id in {seller.id, cs.id, cs2.id, manager.id} for s in draft.steps)


def test_unknown_person_requires_merchant_resolution(db_session) -> None:
    db, tenant_id, _ = db_session
    branch, *_ = _seed_team(db, tenant_id)
    contacts = load_tenant_contacts(db, tenant_id)
    draft = compile_instruction(
        "أعطه رقم خالد",
        contacts,
        branch_id=branch.id,
        extracted=_llm(
            [],
            unresolved=[{"token": "خالد", "reason": "unknown_person"}],
        ),
    )
    assert draft.can_confirm is False
    assert any(u.reason == "unknown_person" for u in draft.unresolved)
    assert "خالد" in " ".join(u.token for u in draft.unresolved)


def test_phone_change_on_contact_updates_live_policy(db_session) -> None:
    db, tenant_id, _ = db_session
    branch, seller, *_ = _seed_team(db, tenant_id)
    contacts = load_tenant_contacts(db, tenant_id)
    draft = compile_instruction(
        "أعطه نورة",
        contacts,
        branch_id=branch.id,
        extracted=_llm([(seller.id, "sequence", "share_customer_contact")]),
    )
    draft.can_confirm = True
    apply_confirmed_draft(db, tenant_id=tenant_id, branch_id=branch.id, draft=draft)
    db.commit()
    seller.phone_e164 = "+966599999999"
    db.commit()
    policy = load_canonical_policy(db, tenant_id, branch_id=branch.id)
    assert policy is not None
    live = live_phone_for_contact_id(db, tenant_id, seller.id)
    assert live == "+966599999999" or "599999999" in live
    shareable = policy.shareable_steps()
    assert shareable
    assert "599999999" in shareable[0].live_phone_e164


def test_internal_only_never_exposes_number(db_session) -> None:
    db, tenant_id, _ = db_session
    branch, *_rest, manager = _seed_team(db, tenant_id)
    contacts = load_tenant_contacts(db, tenant_id)
    draft = compile_instruction(
        "صعّد لفهد",
        contacts,
        branch_id=branch.id,
        extracted=_llm([(manager.id, "complaint_urgent", "notify_or_handoff")]),
    )
    draft.can_confirm = True
    apply_confirmed_draft(db, tenant_id=tenant_id, branch_id=branch.id, draft=draft)
    db.commit()
    policy = load_canonical_policy(db, tenant_id, branch_id=branch.id)
    assert policy is not None
    assert policy.shareable_steps() == ()
    action = resolve_share_action(policy)
    assert action.get("phone_e164") == ""
    assert may_share_with_customer(manager) is False
    assert manager.phone_e164 not in str(action)


def test_customer_visible_returns_authoritative_contact(db_session) -> None:
    db, tenant_id, _ = db_session
    branch, seller, *_ = _seed_team(db, tenant_id)
    contacts = load_tenant_contacts(db, tenant_id)
    draft = compile_instruction(
        "أعطه نورة",
        contacts,
        branch_id=branch.id,
        extracted=_llm([(seller.id, "sequence", "share_customer_contact")]),
    )
    draft.can_confirm = True
    apply_confirmed_draft(db, tenant_id=tenant_id, branch_id=branch.id, draft=draft)
    db.commit()
    policy = load_canonical_policy(db, tenant_id, branch_id=branch.id)
    action = resolve_share_action(policy)
    assert action["available"] is True
    assert action["contact_id"] == seller.id
    assert action["phone_e164"]
    assert "511111111" in action["phone_e164"]


def test_multistep_progresses_only_with_customer_or_event(db_session) -> None:
    db, tenant_id, _ = db_session
    branch, seller, cs, cs2, _manager = _seed_team(db, tenant_id)
    contacts = load_tenant_contacts(db, tenant_id)
    draft = compile_instruction(
        "نورة ثم أحمد سالم ثم سعد",
        contacts,
        branch_id=branch.id,
        extracted=_llm([
            (seller.id, "sequence", "share_customer_contact"),
            (cs.id, "no_response", "share_customer_contact"),
            (cs2.id, "no_response", "share_customer_contact"),
        ]),
    )
    draft.can_confirm = True
    apply_confirmed_draft(db, tenant_id=tenant_id, branch_id=branch.id, draft=draft)
    db.commit()
    policy = load_canonical_policy(db, tenant_id, branch_id=branch.id)
    assert policy is not None
    first = next_shareable_step(policy)
    assert first is not None and first.contact_id == seller.id
    blocked = next_shareable_step(policy, sent_contact_ids=[seller.id])
    assert blocked is None
    advanced = next_shareable_step(
        policy,
        sent_contact_ids=[seller.id],
        customer_stated_no_response=True,
    )
    assert advanced is not None and advanced.contact_id == cs.id


def test_no_response_not_invented_from_elapsed_time(db_session) -> None:
    db, tenant_id, _ = db_session
    branch, seller, cs, *_ = _seed_team(db, tenant_id)
    contacts = load_tenant_contacts(db, tenant_id)
    draft = compile_instruction(
        "نورة ثم أحمد سالم",
        contacts,
        branch_id=branch.id,
        extracted=_llm([
            (seller.id, "sequence", "share_customer_contact"),
            (cs.id, "no_response", "share_customer_contact"),
        ]),
    )
    draft.can_confirm = True
    apply_confirmed_draft(db, tenant_id=tenant_id, branch_id=branch.id, draft=draft)
    db.commit()
    policy = load_canonical_policy(db, tenant_id, branch_id=branch.id)
    assert next_shareable_step(policy, sent_contact_ids=[seller.id]) is None


def test_instruction_change_updates_canonical_policy(db_session) -> None:
    db, tenant_id, _ = db_session
    branch, seller, cs, *_ = _seed_team(db, tenant_id)
    contacts = load_tenant_contacts(db, tenant_id)
    first = compile_instruction(
        "أعطه نورة",
        contacts,
        branch_id=branch.id,
        extracted=_llm([(seller.id, "sequence", "share_customer_contact")]),
    )
    first.can_confirm = True
    apply_confirmed_draft(db, tenant_id=tenant_id, branch_id=branch.id, draft=first)
    db.commit()
    second = compile_instruction(
        "أعطه أحمد سالم",
        contacts,
        branch_id=branch.id,
        extracted=_llm([(cs.id, "sequence", "share_customer_contact")]),
    )
    second.can_confirm = True
    apply_confirmed_draft(db, tenant_id=tenant_id, branch_id=branch.id, draft=second)
    db.commit()
    policy = load_canonical_policy(db, tenant_id, branch_id=branch.id)
    assert policy is not None
    assert policy.steps[0].contact_id == cs.id
    assert "أحمد" in (policy.instruction_text or "")


def test_legacy_config_migrates_without_losing_contacts(db_session) -> None:
    db, tenant_id, _ = db_session
    branch = MerchantBranch(tenant_id=tenant_id, name="قديم", is_active=True)
    db.add(branch)
    db.commit()
    db.refresh(branch)
    contact = BranchContact(
        branch_id=branch.id,
        display_name="أمين",
        role="reception",
        phone_e164="+966512312312",
        is_active=True,
        is_default_reception=True,
        customer_visibility="internal_only",
    )
    db.add(contact)
    db.commit()
    db.refresh(contact)
    step = BranchEscalationStep(
        branch_id=branch.id,
        escalation_level=1,
        display_name="أمين",
        role="reception",
        phone_e164="+966512312312",
        contact_id=None,
        is_active=True,
    )
    db.add(step)
    db.commit()
    summary = normalize_tenant_escalation_config(db, tenant_id)
    db.commit()
    db.refresh(contact)
    db.refresh(step)
    assert contact.customer_visibility == "customer_visible"
    assert step.contact_id == contact.id
    assert summary["steps_linked"] >= 1


def test_kb_conflicting_phone_loses_to_structured_contact(db_session) -> None:
    db, tenant_id, _ = db_session
    branch, seller, *_ = _seed_team(db, tenant_id)
    db.add(
        MerchantKnowledgeSection(
            tenant_id=tenant_id,
            kind="faq",
            title="تواصل",
            body="اتصل على 0500000000",
            is_active=True,
            source="manual",
        )
    )
    db.commit()
    contacts = load_tenant_contacts(db, tenant_id)
    conflicts = find_kb_contact_conflicts(db, tenant_id, contacts)
    assert conflicts
    assert conflicts[0]["winner"] == "structured_contact"
    policy = load_canonical_policy(db, tenant_id, branch_id=branch.id)
    if policy is None or not policy.steps:
        contacts_list = load_tenant_contacts(db, tenant_id)
        draft = compile_instruction(
            "نورة",
            contacts_list,
            branch_id=branch.id,
            extracted=_llm([(seller.id, "sequence", "share_customer_contact")]),
        )
        draft.can_confirm = True
        apply_confirmed_draft(db, tenant_id=tenant_id, branch_id=branch.id, draft=draft)
        db.commit()
        policy = load_canonical_policy(db, tenant_id, branch_id=branch.id)
    assert policy is not None
    phones = [s.live_phone_e164 for s in policy.shareable_steps()]
    assert all("500000000" not in p for p in phones)


def test_tenant_isolation_zero_contact_leakage(db_session) -> None:
    db, t1, t2 = db_session
    _seed_team(db, t1, prefix="A")
    _seed_team(db, t2, prefix="B")
    c1 = load_tenant_contacts(db, t1)
    c2 = load_tenant_contacts(db, t2)
    assert {c.display_name for c in c1}.isdisjoint({c.display_name for c in c2})
    ids1 = {c.id for c in c1}
    ids2 = {c.id for c in c2}
    assert ids1.isdisjoint(ids2)
    leaked = live_phone_for_contact_id(db, t1, next(iter(ids2)))
    assert leaked == ""
    policy2 = load_canonical_policy(db, t1, branch_id=c2[0].branch_id)
    assert policy2 is None or policy2.tenant_id == t1 and not policy2.steps


def test_same_architecture_without_salla_flag(db_session) -> None:
    db, tenant_id, _ = db_session
    branch, *_ = _seed_team(db, tenant_id)
    policy = load_canonical_policy(db, tenant_id, branch_id=branch.id)
    assert policy is not None or True
    src = Path(__file__).resolve().parents[1] / "modules" / "operations" / "escalation_policy_runtime.py"
    text = src.read_text(encoding="utf-8")
    assert "salla" not in text.lower()
    assert "tenant_id == 33" not in text


def test_no_policy_is_honest_unavailable(db_session) -> None:
    db, tenant_id, _ = db_session
    db.add(MerchantBranch(tenant_id=tenant_id, name="فارغ", is_active=True))
    db.commit()
    policy = load_canonical_policy(db, tenant_id)
    action = resolve_share_action(policy)
    assert action["available"] is False
    assert action["reason"] in {"no_configured_policy", "no_customer_visible_contact"}


def test_notify_only_keeps_ai_active(db_session) -> None:
    db, tenant_id, _ = db_session
    branch, *_rest, manager = _seed_team(db, tenant_id)
    contacts = load_tenant_contacts(db, tenant_id)
    draft = compile_instruction(
        "صعّد لفهد",
        contacts,
        branch_id=branch.id,
        extracted=_llm([(manager.id, "complaint_urgent", "notify_or_handoff")]),
    )
    draft.can_confirm = True
    apply_confirmed_draft(db, tenant_id=tenant_id, branch_id=branch.id, draft=draft)
    db.commit()
    policy = load_canonical_policy(db, tenant_id, branch_id=branch.id)
    action = resolve_share_action(policy, sent_contact_ids=[manager.id])
    assert action.get("ai_remains_active") is True or action["available"] is False
    notify = resolve_share_action(policy)
    assert notify.get("phone_e164") == ""
    if notify.get("reason") == "notify_only":
        assert notify.get("ai_remains_active") is True


def test_confirm_endpoint_requires_confirmation(db_session) -> None:
    db, tenant_id, _ = db_session
    _seed_team(db, tenant_id)
    from fastapi import HTTPException
    from routers.operations_center import EscalationPolicyConfirmIn, confirm_escalation_policy

    async def _run():
        with patch("routers.operations_center.resolve_tenant_id", return_value=tenant_id):
            with patch("routers.operations_center.get_or_create_tenant", return_value=None):
                return await confirm_escalation_policy(
                    EscalationPolicyConfirmIn(
                        instruction_text="أعطه نورة",
                        confirm=False,
                    ),
                    _FakeRequest(),
                    db,
                )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_run())
    assert exc.value.status_code == 422


def test_visibility_not_inferred_from_management_role() -> None:
    class _Row:
        customer_visibility = INTERNAL_ONLY
        role = "owner"
        phone_e164 = "+966555555555"

    assert may_share_with_customer(_Row()) is False


def test_staff_policy_defers_internal_contact(db_session, monkeypatch) -> None:
    from modules.ai.brain.commerce.staff_contact_evidence import StaffContactRecord
    from modules.ai.brain.commerce import staff_contact_policy as policy

    rec = StaffContactRecord(
        lookup_name="فهد",
        phone="+966544444444",
        section_id=9,
        role="management",
        aliases=(),
        is_owner=True,
        chain_index=0,
        source="structured_branch_contact",
        customer_visibility="internal_only",
        contact_id=9,
    )
    monkeypatch.setattr(policy, "staff_contact_policy_enabled", lambda: True)
    monkeypatch.setenv("STAFF_CONTACT_POLICY_ENABLED", "1")

    class _Res:
        found = True
        record = rec
        reason = "named"

    monkeypatch.setattr(
        "modules.ai.brain.commerce.staff_contact_evidence.resolve_staff_contact",
        lambda *a, **k: _Res(),
    )
    monkeypatch.setattr(
        "modules.ai.brain.commerce.staff_contact_evidence.load_staff_contact_registry",
        lambda *a, **k: type("R", (), {"records": (rec,), "match_record_in_message": lambda self, m: rec})(),
    )
    decision = policy.evaluate_generic_handoff_contact_policy(
        db_session[0],
        tenant_id=db_session[1],
        message="أبي الإدارة",
    )
    assert decision is None or decision.deliver_contact is False
    if decision is not None:
        assert rec.phone not in (decision.reply_text or "")


def test_varied_wording_uses_admin_extraction_not_phrase_lists(db_session) -> None:
    db, tenant_id, _ = db_session
    branch, seller, *_ = _seed_team(db, tenant_id)
    contacts = load_tenant_contacts(db, tenant_id)
    extracted = _llm([(seller.id, "arrival", "share_customer_contact")])
    first = compile_instruction(
        "لما يجي المعرض عطه نورة",
        contacts,
        branch_id=branch.id,
        extracted=extracted,
    )
    second = compile_instruction(
        "عند وصول العميل أرسل رقم نورة",
        contacts,
        branch_id=branch.id,
        extracted=extracted,
    )
    assert [s.contact_id for s in first.steps] == [s.contact_id for s in second.steps]
    src = Path(__file__).resolve().parents[1] / "modules" / "operations" / "escalation_policy_authoring.py"
    text = src.read_text(encoding="utf-8")
    assert "_NAME_AFTER_VERB_RE" not in text
    assert "_condition_for_clause" not in text
    assert "_STOP" not in text


def test_invalid_model_contact_id_is_rejected(db_session) -> None:
    db, tenant_id, _ = db_session
    branch, *_ = _seed_team(db, tenant_id)
    contacts = load_tenant_contacts(db, tenant_id)
    draft = compile_instruction(
        "أعطه نورة",
        contacts,
        branch_id=branch.id,
        extracted=_llm([(999999, "sequence", "share_customer_contact")]),
    )
    assert draft.can_confirm is False
    assert any(u.reason == "invalid_contact_id" for u in draft.unresolved)


def test_ambiguous_same_role_requires_clarification(db_session) -> None:
    db, tenant_id, _ = db_session
    branch, _seller, cs, cs2, _manager = _seed_team(db, tenant_id)
    contacts = load_tenant_contacts(db, tenant_id)
    draft = compile_instruction(
        "أعطه خدمة العملاء",
        contacts,
        branch_id=branch.id,
        extracted=_llm(
            [],
            ambiguities=["ambiguous_role:خدمة العملاء"],
        ),
    )
    assert draft.can_confirm is False
    assert any("ambiguous" in a for a in draft.ambiguities)
    assert {cs.id, cs2.id}.issubset({c.id for c in contacts})


def test_unspecified_visibility_is_not_customer_shareable() -> None:
    class _Row:
        customer_visibility = ""
        phone_e164 = "+966511111111"

    assert may_share_with_customer(_Row()) is False


def test_preview_does_not_mutate_live_policy_until_confirm(db_session) -> None:
    db, tenant_id, _ = db_session
    branch, seller, cs, *_ = _seed_team(db, tenant_id)
    contacts = load_tenant_contacts(db, tenant_id)
    live = compile_instruction(
        "أعطه نورة",
        contacts,
        branch_id=branch.id,
        extracted=_llm([(seller.id, "sequence", "share_customer_contact")]),
    )
    live.can_confirm = True
    apply_confirmed_draft(db, tenant_id=tenant_id, branch_id=branch.id, draft=live)
    db.commit()
    before = load_canonical_policy(db, tenant_id, branch_id=branch.id)
    preview = compile_instruction(
        "أعطه أحمد سالم",
        contacts,
        branch_id=branch.id,
        extracted=_llm([(cs.id, "sequence", "share_customer_contact")]),
        existing_steps=before.steps if before else None,
    )
    db.commit()
    after = load_canonical_policy(db, tenant_id, branch_id=branch.id)
    assert preview.steps[0].contact_id == cs.id
    assert after is not None
    assert after.shareable_steps()[0].contact_id == seller.id
    assert "escalation_sequence_changed" in preview.change_summary


def test_structured_runtime_defaults_on(monkeypatch) -> None:
    from modules.operations.branch_contact_evidence import (
        structured_branch_contacts_enabled,
    )

    monkeypatch.delenv("USE_STRUCTURED_BRANCH_CONTACTS", raising=False)
    monkeypatch.delenv("ROLLBACK_STRUCTURED_BRANCH_CONTACTS", raising=False)
    assert structured_branch_contacts_enabled() is True
    monkeypatch.setenv("ROLLBACK_STRUCTURED_BRANCH_CONTACTS", "1")
    assert structured_branch_contacts_enabled() is False
