"""KB unavailable availability compose guidance — principle-based, no templates."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.commerce.non_catalog_availability_kb_route import (  # noqa: E402
    TOPIC_KB_AVAILABILITY_FACTS,
    apply_kb_availability_reply_polish,
    compose_kb_availability_facts_goal,
    strip_kb_unavailable_shopping_emojis,
    try_non_catalog_availability_kb_decision,
)
from modules.ai.brain.commerce_reply_humanizer import (  # noqa: E402
    should_apply_commerce_humanizer,
)
from modules.ai.brain.intent_priority.types import GOAL_PRODUCT_AVAILABILITY  # noqa: E402
from modules.ai.brain.types import INTENT_ASK_PRODUCT  # noqa: E402
from modules.ai.postprocess.marketing_emoji_policy import (  # noqa: E402
    MarketingEmojiContext,
    PURPOSE_NONE,
    resolve_message_purpose,
)


class _StubKBSection:
    def __init__(
        self,
        *,
        section_id: int,
        kind: str = "faq",
        title: str = "",
        body: str = "",
    ) -> None:
        self.id = section_id
        self.kind = kind
        self.title = title
        self.body = body
        self.priority = 10
        self.updated_at = None
        self.is_active = True
        self.deleted_at = None
        self.tenant_id = 1


class _Col:
    def __init__(self, name: str) -> None:
        self.name = name

    def in_(self, values: Any) -> "_Col":
        return self

    def asc(self) -> "_Col":
        return self

    def desc(self) -> "_Col":
        return self


class _QueryStub:
    def __init__(self, sections: List[_StubKBSection]) -> None:
        self._sections = sections

    def filter(self, *args: Any, **kwargs: Any) -> "_QueryStub":
        return self

    def order_by(self, *args: Any, **kwargs: Any) -> "_QueryStub":
        return self

    def limit(self, n: int) -> "_QueryStub":
        return self

    def all(self) -> List[_StubKBSection]:
        return list(self._sections)


class _StubDB:
    def __init__(self, sections: Optional[List[_StubKBSection]] = None) -> None:
        self._sections = sections or []

    def query(self, model: Any) -> _QueryStub:
        return _QueryStub(self._sections)


def _install_kb_stubs(monkeypatch: pytest.MonkeyPatch, sections: List[_StubKBSection]) -> _StubDB:
    import types as _types

    models_stub = _types.ModuleType("models")

    class _MksStub:
        tenant_id = _Col("tenant_id")
        kind = _Col("kind")
        priority = _Col("priority")
        updated_at = _Col("updated_at")

    models_stub.MerchantKnowledgeSection = _MksStub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "models", models_stub)

    knowledge_stub = _types.ModuleType("core.knowledge")

    def _apply_ai_visible_kb_query_filters(q: Any) -> Any:
        return q

    knowledge_stub.apply_ai_visible_kb_query_filters = _apply_ai_visible_kb_query_filters  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.knowledge", knowledge_stub)

    return _StubDB(sections)


def _ctx(message: str, *, db: Any) -> SimpleNamespace:
    return SimpleNamespace(
        message=message,
        tenant_id=1,
        facts=SimpleNamespace(catalog_skus=[{"id": 1, "title": "عسل سمر 2025"}]),
        state=SimpleNamespace(current_product_focus={}, last_recommended_products=[]),
        db=db,
    )


def _negative_kb_with_next_step() -> _StubKBSection:
    return _StubKBSection(
        section_id=501,
        title="طرود النحل",
        body=(
            "طرود النحل غير متوفرة حالياً. للحجز والتسجيل تواصل مع أبو هشام "
            "عند توفر الدفعة القادمة."
        ),
    )



def _positive_kb() -> _StubKBSection:
    return _StubKBSection(
        section_id=503,
        title="زيارات المنحل",
        body="زيارات المنحل متوفرة يوم الجمعة — للحجز تواصل مع reception.",
    )


class TestUnavailableKBComposeGuidance:
    def test_greeting_unavailable_goal_forbids_shopping_emoji_and_next_step(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(monkeypatch, [_negative_kb_with_next_step()])
        decision = try_non_catalog_availability_kb_decision(
            _ctx("صباح الخير\nفي عندك طرود نحل؟", db=db),
        )
        assert decision is not None
        goal = str(decision.args.get("response_goal") or "")
        assert "UNAVAILABLE KB compose principles" in goal
        assert "acknowledge the greeting" in goal
        assert "shopping/cart/catalog emojis" in goal
        assert "follow-up/next step" in goal
        assert "quote contact names" in goal
        assert "shopping_cart_emoji" in str(decision.args.get("forbidden_claims") or [])

        polished = apply_kb_availability_reply_polish(
            "صباح النور! 🛒\nللأسف، ما عندنا طرود نحل حاليًا",
            topic=TOPIC_KB_AVAILABILITY_FACTS,
            availability_polarity="negative",
        )
        assert "🛒" not in polished
        assert "طرود نحل" in polished

    def test_unavailable_without_next_step_goal_does_not_invent_action(self) -> None:
        goal = compose_kb_availability_facts_goal({
            "availability_polarity": "negative",
            "allowed_facts": {
                "kb_section_title": "خدمة معينة",
                "kb_section_body": "هذه الخدمة غير متوفرة حالياً.",
            },
            "forbidden_claims": ["invented_contact_or_next_step"],
        })
        assert "do not invent registration, waitlist, or contact actions" in goal
        assert "follow-up/next step — mention" not in goal

    def test_unavailable_with_contact_in_kb_goal_allows_kb_contact_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        goal = compose_kb_availability_facts_goal({
            "availability_polarity": "negative",
            "allowed_facts": {
                "kb_section_title": "طرود النحل",
                "kb_section_body": (
                    "طرود النحل غير متوفرة. للتسجيل تواصل مع أبو هشام."
                ),
            },
            "forbidden_claims": ["invented_contact_or_next_step"],
        })
        assert "do not invent" in goal.lower()
        assert "quote contact names" in goal
        assert "invented_contact_or_next_step" in goal

    def test_positive_kb_goal_not_tightened_by_unavailable_guidance(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(monkeypatch, [_positive_kb()])
        decision = try_non_catalog_availability_kb_decision(
            _ctx("عندكم زيارات المنحل؟", db=db),
        )
        assert decision is not None
        assert decision.args.get("availability_polarity") == "positive"
        goal = str(decision.args.get("response_goal") or "")
        assert "UNAVAILABLE KB compose principles" not in goal
        assert "KB confirms availability" in goal

    def test_no_kb_hit_stays_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _install_kb_stubs(monkeypatch, [])
        decision = try_non_catalog_availability_kb_decision(
            _ctx("صباح الخير\nفي عندك طرود نحل؟", db=db),
        )
        assert decision is None


class TestUnavailableKBEmojiPolicy:
    def test_strip_removes_cart_and_checkmark(self) -> None:
        raw = "صباح النور! 🛒\nغير متوفر ✅"
        cleaned = strip_kb_unavailable_shopping_emojis(raw)
        assert "🛒" not in cleaned
        assert "✅" not in cleaned

    def test_humanizer_skipped_for_kb_availability_path(self) -> None:
        assert should_apply_commerce_humanizer(
            reply="غير متوفر حالياً",
            inbound_text="هل متوفر؟",
            intent_name=INTENT_ASK_PRODUCT,
            primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
            chosen_path="kb_availability_facts",
        ) is False

    def test_marketing_emoji_purpose_none_for_kb_negative(self) -> None:
        ctx = MarketingEmojiContext(
            chosen_path="kb_availability_facts",
            decision_args={"availability_polarity": "negative"},
            intent_name=INTENT_ASK_PRODUCT,
        )
        assert resolve_message_purpose(ctx, "غير متوفر حالياً") == PURPOSE_NONE
