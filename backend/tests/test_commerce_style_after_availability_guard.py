"""Pipeline-style tests: operational availability facts must pass style layer."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from modules.ai.brain.commerce_reply_humanizer import (  # noqa: E402
    apply_commerce_reply_humanizer,
    detect_product_category,
    is_operational_availability_fact,
)
from modules.ai.brain.commerce_style_compose import resolve_style_bundle  # noqa: E402
from modules.ai.brain.final_reply_source import resolve_final_source  # noqa: E402
from modules.ai.brain.intent_priority.types import GOAL_PRODUCT_AVAILABILITY  # noqa: E402
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

_BANNED_DRY_HONEY = "متوفر عسل طلح بعدة خيارات."


def _style_final(
    operational: str,
    *,
    inbound: str,
    label: str,
    conversation_id: int,
    turn_id: int = 1,
) -> str:
    return apply_commerce_reply_humanizer(
        operational,
        inbound_text=inbound,
        intent_name=INTENT_ASK_PRODUCT,
        primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
        chosen_path="llm",
        post_guard_rewrite=True,
        tenant_id=7,
        conversation_id=conversation_id,
        turn_id=turn_id,
        product_title=label,
    ).reply


class TestOperationalFactDetection:
    def test_guard_alone_is_operational_fact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.postprocess.product_availability_truth_guard._product_label_for_reply",
            lambda *a, **k: "عسل طلح",
        )
        raw = build_operational_availability_conflict_reply(MagicMock())
        assert raw == _BANNED_DRY_HONEY
        assert is_operational_availability_fact(raw)

    def test_styled_reply_is_not_operational_fact(self) -> None:
        styled = _style_final(
            _BANNED_DRY_HONEY,
            inbound="هل عندكم عسل طلح؟",
            label="عسل طلح",
            conversation_id=11,
        )
        assert not is_operational_availability_fact(styled)


class TestStyleLayerAfterGuard:
    @pytest.mark.parametrize(
        ("label", "inbound", "operational"),
        [
            ("عسل طلح", "هل عندكم عسل طلح؟", "متوفر عسل طلح بعدة خيارات."),
            ("عسل سدر", "عندكم سدر؟", "متوفر عسل سدر."),
            ("فساتين", "هل عندكم فساتين؟", "متوفر فساتين بعدة خيارات."),
            ("الشاحن", "متوفر الشاحن؟", "متوفر الشاحن."),
            ("أقلام", "عندكم أقلام؟", "متوفر أقلام بعدة خيارات."),
        ],
    )
    def test_final_reply_is_not_dry_operational(
        self,
        label: str,
        inbound: str,
        operational: str,
    ) -> None:
        final = _style_final(
            operational,
            inbound=inbound,
            label=label,
            conversation_id=hash(label) % 1000 + 50,
        )
        assert final.strip() != operational.strip()
        assert is_operational_availability_fact(operational)
        assert not is_operational_availability_fact(final)
        assert "متوفر" in final or "متوفر" in operational
        assert label.split()[-1] in final or label in final
        assert "؟" in final or "?" in final
        assert _EMOJI_RE.search(final) is None or len(_EMOJI_RE.findall(final)) <= 2

    def test_banned_dry_honey_not_final(self) -> None:
        final = _style_final(
            _BANNED_DRY_HONEY,
            inbound="هل عندكم عسل طلح؟",
            label="عسل طلح",
            conversation_id=33,
        )
        assert final.strip() != _BANNED_DRY_HONEY

    def test_style_signatures_vary_by_conversation(self) -> None:
        operational = "متوفر فساتين بعدة خيارات."
        sigs = {
            apply_commerce_reply_humanizer(
                operational,
                inbound_text="هل عندكم فساتين؟",
                intent_name=INTENT_SOLUTION_SEEKING_COMMERCE,
                primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
                chosen_path="llm",
                post_guard_rewrite=True,
                tenant_id=1,
                conversation_id=conv,
                turn_id=1,
                product_title="فساتين",
            ).style_signature
            for conv in (10, 20, 30)
        }
        assert len({s for s in sigs if s}) >= 2

    def test_webhook_guard_overwrite_gets_restyled(self) -> None:
        """Simulate non-brain webhook guard replacing styled text with dry fact."""
        dry_after_webhook_guard = _BANNED_DRY_HONEY
        recovered = apply_commerce_reply_humanizer(
            dry_after_webhook_guard,
            inbound_text="هل عندكم عسل طلح؟",
            intent_name=INTENT_ASK_PRODUCT,
            primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
            chosen_path="catalog_miss_llm_fallback",
            post_guard_rewrite=True,
            tenant_id=7,
            conversation_id=88,
            product_title="عسل طلح",
        )
        assert recovered.replaced
        assert recovered.reply.strip() != _BANNED_DRY_HONEY
        assert "؟" in recovered.reply

    def test_operational_fact_auto_detect_without_post_guard_flag(self) -> None:
        out = apply_commerce_reply_humanizer(
            "متوفر جوالات.",
            inbound_text="عندكم جوالات؟",
            intent_name=INTENT_ASK_PRODUCT,
            primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
            chosen_path="catalog_miss_llm_fallback",
            post_guard_rewrite=False,
            tenant_id=3,
            conversation_id=44,
            product_title="جوالات",
        )
        assert out.operational_fact_detected
        assert out.style_layer_applied
        assert out.reply.strip() != "متوفر جوالات."


class TestCategoryEmojiPolicy:
    def test_honey_may_use_food_emoji_not_for_stationery(self) -> None:
        honey = _style_final(
            _BANNED_DRY_HONEY,
            inbound="هل عندكم عسل طلح؟",
            label="عسل طلح",
            conversation_id=101,
        )
        pens = _style_final(
            "متوفر أقلام بعدة خيارات.",
            inbound="عندكم أقلام؟",
            label="أقلام",
            conversation_id=102,
        )
        honey_cat = detect_product_category(honey, product_title="عسل طلح")
        pens_cat = detect_product_category(pens, product_title="أقلام")
        assert honey_cat in {"honey", "food"}
        assert pens_cat == "stationery"
        if "🍯" in honey:
            assert honey_cat in {"honey", "food", "dates", "coffee"}
        assert "🍯" not in pens

    def test_no_sonnet_in_style_compose(self) -> None:
        import modules.ai.brain.commerce_style_compose as mod

        source = open(mod.__file__, encoding="utf-8").read()
        assert "sonnet" not in source.lower()
        assert "haiku" not in source.lower()


class TestWebhookAvailabilityGuardContract:
    def test_brain_path_skips_duplicate_availability_guard(self) -> None:
        webhook_src = (
            Path(_backend) / "routers" / "whatsapp_webhook.py"
        ).read_text(encoding="utf-8")
        marker = "if reply and not _brain_handoff and not _brain_active:"
        assert marker in webhook_src
        block_start = webhook_src.index(marker)
        block = webhook_src[block_start : block_start + 400]
        assert "apply_product_availability_truth_guard" in block

    def test_non_brain_path_runs_humanizer_after_guard(self) -> None:
        webhook_src = (
            Path(_backend) / "routers" / "whatsapp_webhook.py"
        ).read_text(encoding="utf-8")
        marker = "if _pavg_replaced or is_operational_availability_fact(reply or \"\"):"
        assert marker in webhook_src
        block_start = webhook_src.index(marker)
        block = webhook_src[block_start : block_start + 2000]
        assert "apply_commerce_reply_humanizer" in block

    def test_brain_path_safety_net_styles_operational_fact(self) -> None:
        webhook_src = (
            Path(_backend) / "routers" / "whatsapp_webhook.py"
        ).read_text(encoding="utf-8")
        marker = "if reply and _brain_active and not _brain_handoff:"
        assert marker in webhook_src
        block_start = webhook_src.index(marker)
        block = webhook_src[block_start : block_start + 1200]
        assert "is_operational_availability_fact" in block
        assert "apply_commerce_reply_humanizer" in block


class TestFinalReplySource:
    def test_style_layer_after_availability_guard(self) -> None:
        source = resolve_final_source(
            chosen_path="llm",
            guard_replaced={"product_availability_truth_guard": True},
            humanizer_changed=True,
        )
        assert source == "style_layer_after_availability_guard"

    def test_operational_guard_without_humanizer_is_not_style_layer(self) -> None:
        source = resolve_final_source(
            chosen_path="llm",
            guard_replaced={"product_availability_truth_guard": True},
            humanizer_changed=False,
        )
        assert source == "availability_guard_operational"
