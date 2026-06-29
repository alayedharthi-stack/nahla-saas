"""Nahla doctrine — operational deterministic, personality non-deterministic."""
from __future__ import annotations

import os
import re
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from modules.ai.brain.commerce_reply_humanizer import (  # noqa: E402
    apply_commerce_reply_humanizer,
    detect_product_category,
)
from modules.ai.brain.commerce_style_compose import resolve_style_bundle  # noqa: E402
from modules.ai.brain.intent_priority.types import GOAL_PRODUCT_AVAILABILITY  # noqa: E402
from modules.ai.brain.postprocess.product_availability_evidence import (  # noqa: E402
    EVIDENCE_VARIANT_OPTIONS,
)
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    build_operational_availability_conflict_reply,
)
from modules.ai.brain.types import INTENT_ASK_PRODUCT, INTENT_SOLUTION_SEEKING_COMMERCE  # noqa: E402
from unittest.mock import MagicMock

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)

_HONEY_DRY = "متوفر عسل طلح بعدة أحجام، أي حجم يناسبك؟"


class TestOperationalFactsPreserved:
    def test_guard_operational_reply_has_no_personality(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ev = MagicMock()
        ev.evidence_state = EVIDENCE_VARIANT_OPTIONS
        ev.evidence_ok_for_positive = True
        monkeypatch.setattr(
            "modules.ai.brain.postprocess.product_availability_truth_guard._product_label_for_reply",
            lambda *a, **k: "عسل طلح",
        )
        raw = build_operational_availability_conflict_reply(ev)
        assert raw == "متوفر عسل طلح بعدة خيارات."
        assert "أبشر" not in raw
        assert not _EMOJI_RE.search(raw)

    def test_humanizer_preserves_availability_fact(self) -> None:
        operational = "متوفر فساتين بعدة خيارات."
        out = apply_commerce_reply_humanizer(
            operational,
            inbound_text="هل عندكم فساتين؟",
            intent_name=INTENT_ASK_PRODUCT,
            primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
            chosen_path="llm",
            post_guard_rewrite=True,
            tenant_id=1,
            conversation_id=100,
            turn_id=3,
        ).reply
        assert "متوفر" in out
        assert "فساتين" in out
        assert "غير متوفر" not in out

    def test_humanizer_does_not_invent_price(self) -> None:
        operational = "متوفر جوالات بعدة خيارات."
        out = apply_commerce_reply_humanizer(
            operational,
            inbound_text="عندكم جوالات؟",
            intent_name=INTENT_ASK_PRODUCT,
            primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
            chosen_path="llm",
            post_guard_rewrite=True,
            tenant_id=2,
            conversation_id=200,
            turn_id=1,
        ).reply
        assert "ريال" not in out
        assert "SAR" not in out.upper()


class TestPersonalityNotDeterministic:
    @pytest.mark.parametrize(
        ("label", "inbound"),
        [
            ("عسل طلح", "هل عندكم عسل طلح؟"),
            ("فساتين", "هل عندكم فساتين؟"),
            ("جوالات", "عندكم جوالات؟"),
            ("أقلام", "عندكم أقلام؟"),
            ("الشاحن", "متوفر الشاحن؟"),
        ],
    )
    def test_different_conversations_vary_style_signature(
        self,
        label: str,
        inbound: str,
    ) -> None:
        category = detect_product_category(f"{label} {inbound}", product_title=label)
        sig_a = resolve_style_bundle(
            tenant_id=1,
            conversation_id=10,
            turn_id=1,
            intent_name=INTENT_ASK_PRODUCT,
            category=category,
        ).style_signature
        sig_b = resolve_style_bundle(
            tenant_id=1,
            conversation_id=99,
            turn_id=1,
            intent_name=INTENT_ASK_PRODUCT,
            category=category,
        ).style_signature
        assert sig_a != sig_b

    def test_same_category_different_replies_across_contexts(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        operational = "متوفر عسل طلح بعدة خيارات."
        replies = {
            apply_commerce_reply_humanizer(
                operational,
                inbound_text="هل عندكم عسل طلح؟",
                intent_name=INTENT_SOLUTION_SEEKING_COMMERCE,
                primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
                chosen_path="llm",
                post_guard_rewrite=True,
                tenant_id=7,
                conversation_id=conv,
                turn_id=turn,
                product_title="عسل طلح",
            ).reply
            for conv, turn in ((1, 1), (2, 1), (3, 5))
        }
        assert len(set(replies)) >= 2
        assert _HONEY_DRY not in replies
        assert all("متوفر" in r and "عسل طلح" in r for r in replies)

    def test_guard_alone_is_not_acceptance_example_template(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ev = MagicMock()
        ev.evidence_state = EVIDENCE_VARIANT_OPTIONS
        ev.evidence_ok_for_positive = True
        monkeypatch.setattr(
            "modules.ai.brain.postprocess.product_availability_truth_guard._product_label_for_reply",
            lambda *a, **k: "عسل طلح",
        )
        guard_only = build_operational_availability_conflict_reply(ev)
        assert guard_only != _HONEY_DRY
        assert "أبشر" not in guard_only
        assert "🍯" not in guard_only


class TestNoTenantOrPhraseHardcoding:
    def test_no_tenant_33_in_style_compose(self) -> None:
        import modules.ai.brain.commerce_style_compose as mod

        source = open(mod.__file__, encoding="utf-8").read()
        assert "tenant_id == 33" not in source
        assert "tenant_id=33" not in source

    def test_categories_use_general_pipeline(self) -> None:
        assert detect_product_category("فساتين", product_title="فساتين") == "dress"
        assert detect_product_category("جوالات", product_title="جوالات") == "mobile"
        assert detect_product_category("أقلام", product_title="أقلام") == "stationery"
        assert detect_product_category("شاحن", product_title="الشاحن") == "electronics"
