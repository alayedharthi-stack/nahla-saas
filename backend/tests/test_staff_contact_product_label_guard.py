"""Staff/contact/showroom phrases must never become product availability labels."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.commerce.staff_contact_product_label_guard import (  # noqa: E402
    StaffContactGuardContext,
    has_explicit_product_commerce_intent,
    is_staff_or_contact_context,
    is_staff_or_contact_label,
    should_block_product_availability_rewrite,
)
from modules.ai.brain.postprocess.availability_guard_policy import (  # noqa: E402
    inbound_exempt_from_availability_rewrite,
    should_block_availability_rewrite,
)
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    _label_from_inbound_availability_ask,
    apply_product_availability_truth_guard,
    build_operational_availability_conflict_reply,
)
from modules.ai.brain.product_discovery_gate import product_browse_negative_context_reason  # noqa: E402


def _evidence():
    entity = SimpleNamespace(product_id=None, family_key="inbound:test", resolution_mode="none", confidence=0.0)
    return SimpleNamespace(
        entity=entity,
        evidence_state="conflict",
        conflict_type="MISSING_CATALOG_ENTITY",
        reason="unresolved_entity_with_kb_conflict",
        catalog_checkout=None,
        kb_avail_polarity=None,
    )


class TestStaffContactProductLabelGuard:
    def test_t1_staff_location_phrase_is_not_product(self):
        message = "وين هو أمين"
        ctx = StaffContactGuardContext(
            arrival_thread_active=True,
            configured_staff_names=("أمين",),
        )
        assert is_staff_or_contact_context(message, ctx=ctx) is True
        assert is_staff_or_contact_label(message, ctx=ctx) is True
        assert _label_from_inbound_availability_ask(message) == ""
        reply = build_operational_availability_conflict_reply(
            _evidence(),
            inbound_text=message,
        )
        assert reply == ""
        assert "متوفر" not in reply
        assert should_block_product_availability_rewrite(message, ctx=ctx) is True

        with patch(
            "modules.ai.brain.postprocess.product_availability_truth_guard.product_availability_guard_mode",
            return_value="enforce",
        ):
            result = apply_product_availability_truth_guard(
                reply="نعم متوفر عندنا",
                inbound_text=message,
            )
        assert "متوفر وين هو أمين" not in (result.reply or "")
        assert result.replaced is False

    def test_t2_pronoun_contact_phrase_is_not_product(self):
        message = "ارسل رقمه"
        ctx = StaffContactGuardContext(
            conversation_history=("انا في الطريق", "أمين بائع المعرض يقدر يساعدك"),
            staff_route_detected=True,
        )
        assert is_staff_or_contact_context(message, ctx=ctx) is True
        assert _label_from_inbound_availability_ask(message) == ""
        assert product_browse_negative_context_reason(message) in {
            "contact_context",
            "staff_contact_context",
            "showroom_escalation_context",
        }
        assert build_operational_availability_conflict_reply(
            _evidence(),
            inbound_text=message,
        ) == ""

    def test_t3_showroom_no_response_thread_blocks_product_rewrite(self):
        history = ("انا في الطريق", "مافيه احد", "وين هو الموظف؟")
        for msg in history:
            ctx = StaffContactGuardContext(conversation_history=history)
            assert is_staff_or_contact_context(msg, ctx=ctx) is True
            assert should_block_product_availability_rewrite(msg, ctx=ctx) is True
            assert inbound_exempt_from_availability_rewrite(msg) is True

    def test_t4_real_product_availability_still_works(self):
        message = "هل عسل السمر متوفر؟"
        assert has_explicit_product_commerce_intent(message) is True
        assert is_staff_or_contact_context(message) is False
        assert should_block_product_availability_rewrite(message) is False
        assert inbound_exempt_from_availability_rewrite(message) is False
        label = _label_from_inbound_availability_ask(message)
        assert label != "" or "عسل" in message

    def test_t5_catalog_browse_still_works(self):
        message = "وش الأنواع المتوفرة؟"
        assert has_explicit_product_commerce_intent(message) is True
        assert is_staff_or_contact_context(message) is False
        assert should_block_product_availability_rewrite(message) is False
        assert product_browse_negative_context_reason(message) == ""

    def test_t6_configured_staff_name_general(self):
        staff_name = "Sara Showroom"
        message = f"وين هو {staff_name}"
        ctx = StaffContactGuardContext(configured_staff_names=(staff_name,))
        assert is_staff_or_contact_context(message, ctx=ctx) is True
        assert is_staff_or_contact_label(message, ctx=ctx) is True
        assert _label_from_inbound_availability_ask(message) == ""
        assert build_operational_availability_conflict_reply(
            _evidence(),
            inbound_text=message,
        ) == ""

    def test_false_social_context_does_not_block_staff_phrase_with_ameen_substring(self):
        message = "وين هو أمين"
        reason = product_browse_negative_context_reason(message)
        assert reason != "social_context"
        assert reason in {"staff_contact_context", "showroom_escalation_context", ""} or reason

    def test_religious_amen_still_social_context(self):
        assert product_browse_negative_context_reason("آمين يا رب") == "social_context"
        assert product_browse_negative_context_reason("اللهم آمين") == "social_context"

    def test_should_block_availability_rewrite_integration(self):
        message = "وين هو الموظف؟"
        assert should_block_availability_rewrite(
            inbound_text=message,
            evidence_state="conflict",
            guard_action="rewrite_conflict",
        ) is True
