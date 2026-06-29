"""KB unavailable availability compose guidance — KB truth only, no emoji policy."""
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
    compose_kb_availability_facts_goal,
    try_non_catalog_availability_kb_decision,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY, ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.postprocess.catalog_product_grounding_guard import (  # noqa: E402
    apply_catalog_product_grounding_guard,
)
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    apply_product_availability_truth_guard,
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


def _ctx(
    message: str,
    *,
    db: Any,
    catalog_skus: Optional[List[dict]] = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        message=message,
        tenant_id=1,
        facts=SimpleNamespace(
            catalog_skus=catalog_skus or [{"id": 1, "title": "عسل سمر 2025"}],
        ),
        state=SimpleNamespace(current_product_focus={}, last_recommended_products=[]),
        db=db,
    )


def _sku(pid: int, title: str) -> dict:
    return {
        "id": pid,
        "title": title,
        "sku": f"SKU-{pid}",
        "can_checkout": True,
        "in_stock": True,
    }


def _negative_kb_with_next_step() -> _StubKBSection:
    return _StubKBSection(
        section_id=501,
        title="طرود النحل",
        body=(
            "طرود النحل غير متوفرة حالياً. للحجز والتسجيل تواصل مع أبو هشام "
            "عند توفر الدفعة القادمة."
        ),
    )


class TestKBUnavailableTruthCompose:
    def test_greeting_negative_goal_states_unavailable_not_uncertainty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(monkeypatch, [_negative_kb_with_next_step()])
        decision = try_non_catalog_availability_kb_decision(
            _ctx("صباح الخير\nفي عندك طرود نحل؟", db=db),
        )
        assert decision is not None
        assert decision.args.get("availability_polarity") == "negative"
        goal = str(decision.args.get("response_goal") or "")
        assert "UNAVAILABLE KB compose principles" in goal
        assert "acknowledge the greeting" in goal
        assert "NOT currently available" in goal
        assert "availability uncertainty" in goal
        assert "ما نقدر نؤكد التوفر" in goal
        forbidden = list(decision.args.get("forbidden_claims") or [])
        assert "availability_uncertainty_when_kb_negative" in forbidden
        assert "invented_contact_or_next_step" in forbidden

    def test_negative_with_next_step_goal_from_kb_only(self) -> None:
        goal = compose_kb_availability_facts_goal({
            "availability_polarity": "negative",
            "allowed_facts": {
                "kb_section_title": "طرود النحل",
                "kb_section_body": (
                    "طرود النحل غير متوفرة. للتسجيل تواصل مع أبو هشام."
                ),
            },
        })
        assert "follow-up/next step" in goal
        assert "quote contact names or actions only from KB" in goal
        assert "do not invent" in goal.lower()

    def test_negative_without_next_step_does_not_invent_action(self) -> None:
        goal = compose_kb_availability_facts_goal({
            "availability_polarity": "negative",
            "allowed_facts": {
                "kb_section_title": "خدمة معينة",
                "kb_section_body": "هذه الخدمة غير متوفرة حالياً.",
            },
        })
        assert "do not invent registration, waitlist, or contact actions" in goal
        assert "follow-up/next step — mention" not in goal

    def test_no_kb_hit_returns_none_not_negative_compose(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(monkeypatch, [])
        decision = try_non_catalog_availability_kb_decision(
            _ctx("صباح الخير\nفي عندك طرود نحل؟", db=db),
        )
        assert decision is None

    def test_kb_negative_routes_llm_not_catalog_search(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(monkeypatch, [_negative_kb_with_next_step()])
        decision = try_non_catalog_availability_kb_decision(
            _ctx("في عندك طرود نحل؟", db=db, catalog_skus=[_sku(2, "عسل طلح")]),
        )
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_SEARCH_PRODUCTS
        assert decision.args.get("topic") == TOPIC_KB_AVAILABILITY_FACTS
        assert decision.args.get("block_commerce_escalation") is True


class TestKBNegativeGuardTruth:
    def setup_method(self) -> None:
        self._prev_avail = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        self._prev_cat = os.environ.get("NAHLA_CATALOG_PRODUCT_GROUNDING_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"
        os.environ["NAHLA_CATALOG_PRODUCT_GROUNDING_GUARD_MODE"] = "enforce"

    def teardown_method(self) -> None:
        for key, prev in (
            ("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", self._prev_avail),
            ("NAHLA_CATALOG_PRODUCT_GROUNDING_GUARD_MODE", self._prev_cat),
        ):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev

    def test_kb_negative_not_rewritten_to_unknown(self) -> None:
        reply = "طرود النحل غير متوفرة حالياً — التسجيل عند أبو هشام."
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context={
                "platform_connected": True,
                "catalog_skus": [_sku(7, "عسل طلح")],
                "kb_signals": [],
                "product_links": [],
            },
            inbound_text="في عندك طرود نحل؟",
            chosen_path="kb_availability_facts",
            tenant_id=1,
        )
        assert result.replaced is False
        assert "غير متوفرة" in result.reply
        assert "ما نقدر نؤكد" not in result.reply

    def test_kb_negative_not_catalog_grounded(self) -> None:
        reply = (
            "طرود النحل غير متوفرة حالياً. للحجز تواصل مع أبو هشام عند توفر الدفعة."
        )
        result = apply_catalog_product_grounding_guard(
            reply=reply,
            inbound_text="في عندك طرود نحل؟",
            chosen_path="kb_availability_facts",
            tenant_id=1,
        )
        assert result.replaced is False
        assert "غير متوفرة" in result.reply
