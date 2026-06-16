"""Direct admin / L3 structured escalation — Operations Center."""
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

    def filter(self, *args: Any, **kwargs: Any) -> "_Q":
        return self

    def order_by(self, *args: Any, **kwargs: Any) -> "_Q":
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


def _tenant33_db(*, include_admin: bool = True) -> _DB:
    steps = [
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
    ]
    if include_admin:
        steps.append(
            _Row(
                id=104, branch_id=1, escalation_level=3,
                display_name="أبو هشام", role="admin",
                phone_e164="966533333333", sort_order=0, is_active=True,
            )
        )
    return _DB(
        branches=[
            _Row(
                id=1,
                tenant_id=33,
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
        steps=steps,
    )


@pytest.fixture(autouse=True)
def _structured_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_STRUCTURED_BRANCH_CONTACTS", "1")


class TestDirectAdminRoutesToL3:
    def test_abi_alidara_selects_level_3(self) -> None:
        from modules.operations.branch_escalation_evidence import (
            resolve_direct_admin_from_structured_chain,
        )

        resolution = resolve_direct_admin_from_structured_chain(
            _tenant33_db(), 33, "أبي الإدارة",
        )
        assert resolution is not None
        assert resolution.found is True
        assert len(resolution.entries) == 1
        assert resolution.entries[0].lookup_name == "أبو هشام"
        assert resolution.escalation_level == 3

    def test_abi_almasool_selects_admin_level(self) -> None:
        from modules.operations.structured_admin_contact_policy import (
            evaluate_structured_admin_contact_policy,
        )

        decision = evaluate_structured_admin_contact_policy(
            _tenant33_db(), tenant_id=33, message="أبي المسؤول",
        )
        assert decision is not None
        assert decision.deliver_contact is True
        assert decision.escalation_level == 3
        assert len(decision.call_targets) == 1

    def test_abu_hasham_name_match_without_hardcode(self) -> None:
        from modules.operations.branch_escalation_evidence import (
            resolve_direct_admin_from_structured_chain,
        )

        resolution = resolve_direct_admin_from_structured_chain(
            _tenant33_db(), 33, "أبو هشام",
        )
        assert resolution is not None
        assert resolution.found is True
        assert resolution.entries[0].lookup_name == "أبو هشام"


class TestOwnerRoutesOnlyIfConfigured:
    def test_aklm_almalik_sends_admin_when_configured(self) -> None:
        from modules.operations.structured_admin_contact_policy import (
            evaluate_structured_admin_contact_policy,
        )

        decision = evaluate_structured_admin_contact_policy(
            _tenant33_db(include_admin=True),
            tenant_id=33,
            message="أكلم المالك",
        )
        assert decision is not None
        assert decision.deliver_contact is True
        assert decision.contact_count == 1

    def test_aklm_almalik_no_phone_when_admin_missing(self) -> None:
        from modules.operations.structured_admin_contact_policy import (
            MSG_STRUCTURED_ADMIN_NOT_CONFIGURED,
            evaluate_structured_admin_contact_policy,
        )

        decision = evaluate_structured_admin_contact_policy(
            _tenant33_db(include_admin=False),
            tenant_id=33,
            message="أكلم المالك",
        )
        assert decision is not None
        assert decision.deliver_contact is False
        assert decision.call_targets == ()
        assert MSG_STRUCTURED_ADMIN_NOT_CONFIGURED in decision.reply_text


class TestSequentialEscalationUnchanged:
    def test_no_response_still_advances_l2_then_l3(self) -> None:
        from modules.operations.branch_escalation_evidence import (
            load_structured_escalation_chain,
            resolve_next_structured_escalation,
        )

        db = _tenant33_db()
        chain = load_structured_escalation_chain(db, 33)
        sent_l1 = [{"name": "أمين", "phone": "966511111111", "turn": 1}]
        nxt = resolve_next_structured_escalation(chain, sent_l1)
        assert nxt is not None
        assert nxt.lookup_name == "هشام"

        sent_l2a = sent_l1 + [{"name": "هشام", "phone": "966522222222", "turn": 2}]
        nxt2 = resolve_next_structured_escalation(chain, sent_l2a)
        assert nxt2 is not None
        assert nxt2.lookup_name == "هيثم"

        sent_l2b = sent_l2a + [{"name": "هيثم", "phone": "966522222223", "turn": 3}]
        nxt3 = resolve_next_structured_escalation(chain, sent_l2b)
        assert nxt3 is not None
        assert nxt3.lookup_name == "أبو هشام"


class TestKBCutover:
    def test_structured_admin_does_not_read_kb(self) -> None:
        from modules.operations.structured_admin_contact_policy import (
            evaluate_structured_admin_contact_policy,
        )

        db = _tenant33_db()
        with patch(
            "modules.ai.brain.commerce.staff_contact_fallback_v0.load_staff_chain_sections",
        ) as mock_kb:
            decision = evaluate_structured_admin_contact_policy(
                db, tenant_id=33, message="أبي الإدارة",
            )
            assert decision is not None
            assert decision.deliver_contact is True
            mock_kb.assert_not_called()


class TestFlagOffLegacyBehavior:
    def test_flag_off_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_STRUCTURED_BRANCH_CONTACTS", "0")
        from modules.operations.branch_escalation_evidence import (
            resolve_direct_admin_from_structured_chain,
        )
        from modules.operations.structured_admin_contact_policy import (
            evaluate_structured_admin_contact_policy,
        )

        db = _tenant33_db()
        assert resolve_direct_admin_from_structured_chain(db, 33, "أبي الإدارة") is None
        assert evaluate_structured_admin_contact_policy(
            db, tenant_id=33, message="أبي الإدارة",
        ) is None
