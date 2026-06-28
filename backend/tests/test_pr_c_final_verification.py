"""PR-C final verification — structured smoke, KB cutover, media routing."""
from __future__ import annotations

import os
import sys
from typing import Any, List, Optional, Sequence, Type
from unittest.mock import patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


class _Row:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _Q:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def filter(self, *a: Any, **k: Any) -> "_Q":
        return self

    def order_by(self, *a: Any, **k: Any) -> "_Q":
        return self

    def first(self) -> Optional[Any]:
        return self._rows[0] if self._rows else None

    def all(self) -> List[Any]:
        return list(self._rows)


class _DB:
    def __init__(self, **tables: List[Any]) -> None:
        self.branches = tables.get("branches", [])
        self.contacts = tables.get("contacts", [])
        self.steps = tables.get("steps", [])
        self.keywords = tables.get("keywords", [])

    def query(self, model: Type[Any]) -> _Q:
        name = getattr(model, "__name__", str(model))
        mapping = {
            "MerchantBranch": self.branches,
            "BranchContact": self.contacts,
            "BranchEscalationStep": self.steps,
            "BranchArrivalKeyword": self.keywords,
        }
        return _Q(mapping.get(name, []))


def _smoke_db() -> _DB:
    """Operations Center only — no KB sections."""
    return _DB(
        branches=[
            _Row(
                id=1,
                tenant_id=10,
                name="المعرض",
                city="",
                district="",
                address="",
                maps_url="https://maps.google.com/?q=showroom",
                sort_order=0,
                is_active=True,
                location_response_mode="location_plus_reception",
                arrival_response_mode="reception_only",
                location_instructions_text="",
            ),
        ],
        contacts=[
            _Row(
                id=11,
                branch_id=1,
                display_name="أمين",
                role="reception",
                phone_e164="966511111111",
                whatsapp_e164="",
                sort_order=0,
                is_active=True,
                is_default_reception=True,
            ),
        ],
        steps=[
            _Row(
                id=101, branch_id=1, escalation_level=1,
                display_name="أمين", role="showroom",
                phone_e164="966511111111", sort_order=0, is_active=True,
            ),
            _Row(
                id=102, branch_id=1, escalation_level=2,
                display_name="هشام", role="customer_service",
                phone_e164="966522222222", sort_order=0, is_active=True,
            ),
            _Row(
                id=103, branch_id=1, escalation_level=2,
                display_name="هيثم", role="customer_service",
                phone_e164="966522222223", sort_order=1, is_active=True,
            ),
            _Row(
                id=104, branch_id=1, escalation_level=3,
                display_name="أبو هشام", role="admin",
                phone_e164="966533333333", sort_order=0, is_active=True,
            ),
        ],
        keywords=[
            _Row(id=1, branch_id=1, phrase="وصلت", trigger_type="arrival_confirmed", sort_order=0, is_active=True),
            _Row(id=2, branch_id=1, phrase="عند البوابة", trigger_type="arrival_confirmed", sort_order=1, is_active=True),
            _Row(id=3, branch_id=1, phrase="في الحوش", trigger_type="arrival_confirmed", sort_order=2, is_active=True),
            _Row(id=4, branch_id=1, phrase="ما لقيت أحد", trigger_type="no_response", sort_order=3, is_active=True),
            _Row(id=5, branch_id=1, phrase="ما يرد", trigger_type="no_response", sort_order=4, is_active=True),
            _Row(id=6, branch_id=1, phrase="وين موقعكم", trigger_type="location_request", sort_order=5, is_active=True),
        ],
    )


@pytest.fixture(autouse=True)
def _structured_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_STRUCTURED_BRANCH_CONTACTS", "1")


# ── 1. Structured branch smoke ───────────────────────────────────────────────


def test_smoke_location_plus_reception() -> None:
    from modules.ai.brain.commerce.branch_trigger_router import (
        MSG_PICKUP_PREFERENCE_ASK,
        evaluate_branch_trigger_routing,
    )

    db = _smoke_db()
    d = evaluate_branch_trigger_routing(db, tenant_id=10, message="وين موقعكم")
    assert d is not None
    assert d.trigger_type == "location_request"
    assert d.maps_url
    assert d.deliver_reception_after_maps is False
    assert MSG_PICKUP_PREFERENCE_ASK not in d.reply_text


def test_smoke_arrival_soft_no_escalation() -> None:
    from modules.ai.brain.commerce.branch_trigger_router import evaluate_branch_trigger_routing

    db = _smoke_db()
    d = evaluate_branch_trigger_routing(db, tenant_id=10, message="أنا في الطريق")
    assert d is not None
    assert d.trigger_type == "arrival_soft"
    assert d.deliver_contact is False
    assert d.persist_contact is False


def test_smoke_arrival_confirmed_at_gate() -> None:
    from modules.ai.brain.commerce.branch_trigger_router import evaluate_branch_trigger_routing

    db = _smoke_db()
    d = evaluate_branch_trigger_routing(db, tenant_id=10, message="عند البوابة")
    assert d is not None
    assert d.trigger_type == "arrival_confirmed"
    assert d.deliver_contact is True


def test_smoke_no_staff_advances_escalation() -> None:
    from modules.ai.brain.commerce.branch_trigger_router import evaluate_branch_trigger_routing

    db = _smoke_db()
    with patch(
        "modules.ai.brain.commerce.branch_trigger_router._load_contacts_sent",
        return_value=[{"name": "أمين", "phone": "966511111111", "turn": 1}],
    ):
        d = evaluate_branch_trigger_routing(
            db, tenant_id=10, message="ما لقيت أحد", customer_phone="966500000001",
        )
    assert d is not None
    assert d.trigger_type == "no_response"
    assert d.deliver_contact is True
    assert d.reason == "no_response_escalation_advance"
    assert getattr(d.call_target, "name", "") == "هشام" or "هشام" in str(d.call_target)


def test_smoke_multi_contact_level2_chain() -> None:
    from modules.operations.branch_escalation_evidence import (
        load_structured_escalation_chain,
        resolve_next_structured_escalation,
    )

    db = _smoke_db()
    chain = load_structured_escalation_chain(db, 10)
    assert len(chain) == 4
    sent_l1 = [{"name": "أمين", "phone": "966511111111", "turn": 1}]
    nxt = resolve_next_structured_escalation(chain, sent_l1)
    assert nxt is not None
    assert nxt.lookup_name == "هشام"
    sent_l2a = sent_l1 + [{"name": "هشام", "phone": "966522222222", "turn": 2}]
    nxt2 = resolve_next_structured_escalation(chain, sent_l2a)
    assert nxt2 is not None
    assert nxt2.lookup_name == "هيثم"


# ── 2. KB cutover ────────────────────────────────────────────────────────────


def test_kb_cutover_arrival_skips_kb_compile() -> None:
    from modules.ai.brain.commerce.arrival_contact_delivery_policy import (
        resolve_arrival_contact_evidence,
    )

    db = _smoke_db()
    with patch(
        "modules.ai.brain.commerce.arrival_contact_policy.resolve_arrival_contact_policy",
    ) as mock_policy:
        ev = resolve_arrival_contact_evidence(db, 10, message="وصلت")
        assert ev is not None
        assert ev.compile_reason == "structured_branch_reception"
        mock_policy.assert_not_called()


def test_kb_cutover_staff_registry_skips_kb() -> None:
    from modules.ai.brain.commerce.staff_contact_evidence import load_staff_contact_registry

    db = _smoke_db()
    with patch(
        "modules.ai.brain.commerce.staff_contact_fallback_v0.load_staff_chain_sections",
    ) as mock_sections:
        reg = load_staff_contact_registry(db, 10)
        assert reg is not None
        assert reg.records
        assert reg.records[0].lookup_name == "أمين"
        mock_sections.assert_not_called()


def test_kb_cutover_escalation_skips_kb_tiers() -> None:
    from modules.ai.brain.commerce.staff_contact_escalation_chain import (
        resolve_next_escalation_contact,
    )

    db = _smoke_db()
    kb_chain = []
    sent = [{"name": "أمين", "phone": "966511111111", "turn": 1}]
    with patch(
        "modules.ai.brain.commerce.staff_contact_escalation_chain.resolve_next_tiered_contact",
    ) as mock_kb:
        nxt = resolve_next_escalation_contact(
            db, 10, kb_chain, sent, message="ما يرد",
        )
        assert nxt is not None
        assert nxt.lookup_name == "هشام"
        mock_kb.assert_not_called()


def test_kb_cutover_maps_from_structured_branch() -> None:
    from modules.ai.postprocess import safety_nets as sn

    db = _smoke_db()
    url, source = sn._lookup_tenant_maps_url(db, 10)
    assert url == "https://maps.google.com/?q=showroom"
    assert source == "structured_branch"


# ── 3. Media routing regression ──────────────────────────────────────────────


def test_media_pdf_ocr_does_not_trigger_location() -> None:
    from modules.ai.media.routing_guard import resolve_pre_brain_customer_message
    from modules.ai.brain.commerce.branch_trigger_router import evaluate_branch_trigger_routing

    brain = "نص الملف المستخرج: موقع المعرض وين موقعكم"
    msg = resolve_pre_brain_customer_message(
        brain_text=brain,
        inbound_metadata={"source_type": "document", "caption": ""},
    )
    assert msg == ""
    db = _smoke_db()
    d = evaluate_branch_trigger_routing(db, tenant_id=10, message=msg)
    assert d is None


def test_media_pdf_caption_with_location_intent_triggers() -> None:
    from modules.ai.media.routing_guard import resolve_pre_brain_customer_message
    from modules.ai.brain.commerce.branch_trigger_router import evaluate_branch_trigger_routing

    msg = resolve_pre_brain_customer_message(
        brain_text="نص OCR طويل",
        inbound_metadata={"source_type": "document", "caption": "وين موقعكم"},
    )
    assert msg == "وين موقعكم"
    db = _smoke_db()
    d = evaluate_branch_trigger_routing(db, tenant_id=10, message=msg)
    assert d is not None
    assert d.trigger_type == "location_request"


def test_media_image_without_caption_no_location() -> None:
    from modules.ai.media.routing_guard import resolve_pre_brain_customer_message
    from modules.ai.brain.commerce.location_link_policy import evaluate_location_link_policy

    msg = resolve_pre_brain_customer_message(
        brain_text="إيصال دفع 500 ريال موقع",
        inbound_metadata={"source_type": "image", "caption": ""},
    )
    assert msg == ""
    db = _smoke_db()
    assert evaluate_location_link_policy(db, tenant_id=10, message=msg) is None
    from modules.ai.brain.commerce.branch_trigger_router import evaluate_branch_trigger_routing
    assert evaluate_branch_trigger_routing(db, tenant_id=10, message=msg) is None
