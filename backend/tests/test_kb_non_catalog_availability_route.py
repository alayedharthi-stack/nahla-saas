"""PR-KB3 — non-catalog availability KB route owner (platform-wide)."""
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
    retrieve_non_catalog_availability_kb_hit,
    try_non_catalog_availability_kb_decision,
)
from modules.ai.brain.commerce.solution_seeking import (  # noqa: E402
    classify_solution_seeking_commerce,
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
        priority: int = 10,
    ) -> None:
        self.id = section_id
        self.kind = kind
        self.title = title
        self.body = body
        self.priority = priority
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


def _bee_packages_kb() -> _StubKBSection:
    return _StubKBSection(
        section_id=501,
        kind="faq",
        title="طرود النحل",
        body=(
            "طرود النحل غير متوفرة حالياً. للحجز والتسجيل تواصل مع أبو هشام "
            "عند توفر الدفعة القادمة."
        ),
    )


def _ctx(
    message: str,
    *,
    db: Any = None,
    catalog_skus: Optional[List[dict]] = None,
    use_private_db: bool = False,
) -> SimpleNamespace:
    ns = SimpleNamespace(
        message=message,
        tenant_id=1,
        facts=SimpleNamespace(catalog_skus=catalog_skus or []),
        state=SimpleNamespace(
            current_product_focus={},
            last_recommended_products=[],
        ),
    )
    if use_private_db:
        ns._db = db
    else:
        ns.db = db
    return ns


def _sku(pid: int, title: str, *, checkout: bool = True) -> dict:
    from core.product_entity_resolution import family_key_from_title  # noqa: E402

    return {
        "id": pid,
        "title": title,
        "sku": f"SKU-{pid}",
        "external_id": f"ext-{pid}",
        "can_checkout": checkout,
        "in_stock": checkout,
        "years": [],
        "weights": [],
        "family_key": family_key_from_title(title),
    }


class TestKB3NonCatalogAvailabilityRoute:
    def test_bee_packages_negative_kb_hit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _install_kb_stubs(monkeypatch, [_bee_packages_kb()])
        hit = retrieve_non_catalog_availability_kb_hit(
            db,
            1,
            subject="طرود نحل",
            message="في عندك طرود نحل؟",
        )
        assert hit is not None
        assert hit.section_id == 501
        assert hit.availability_polarity == "negative"

    def test_route_decision_for_bee_packages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _install_kb_stubs(monkeypatch, [_bee_packages_kb()])
        decision = try_non_catalog_availability_kb_decision(
            _ctx(
                "في عندك طرود نحل؟",
                db=db,
                catalog_skus=[_sku(1, "عسل سمر الحجاز 2025")],
            ),
        )
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_KB_AVAILABILITY_FACTS
        assert decision.args.get("block_commerce_escalation") is True
        assert decision.args.get("availability_polarity") == "negative"
        assert decision.args.get("kb_section_ids") == [501]
        assert "طرود نحل" in str(decision.args.get("subject") or "")

    def test_route_blocks_catalog_escalation_not_search(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _install_kb_stubs(monkeypatch, [_bee_packages_kb()])
        decision = try_non_catalog_availability_kb_decision(
            _ctx("في عندك طرود نحل؟", db=db, catalog_skus=[_sku(2, "عسل طلح نجد")]),
        )
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_SEARCH_PRODUCTS
        assert decision.args.get("block_commerce_escalation") is True
        forbidden = list(decision.args.get("forbidden_claims") or [])
        assert "positive_availability" in forbidden
        assert "catalog_product_list" in forbidden

    def test_compose_goal_forbids_motawfir_and_catalog(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _install_kb_stubs(monkeypatch, [_bee_packages_kb()])
        decision = try_non_catalog_availability_kb_decision(
            _ctx("في عندك طرود نحل؟", db=db),
        )
        goal = str(decision.args.get("response_goal") or "")
        assert "kb_availability_facts" in goal
        assert "Do NOT send catalog" in goal
        assert "never claim متوفر" in goal

    def test_non_catalog_subject_with_kb_wins_over_catalog_token_overlap(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(
            monkeypatch,
            [
                _StubKBSection(
                    section_id=502,
                    kind="quick_update",
                    title="ملكات النحل",
                    body="ملكات النحل غير متوفرة — التسجيل عند أبو هشام.",
                ),
            ],
        )
        decision = try_non_catalog_availability_kb_decision(
            _ctx(
                "عندكم ملكات نحل؟",
                db=db,
                catalog_skus=[_sku(3, "عسل نحل جبلي")],
            ),
        )
        assert decision is not None
        assert decision.args.get("topic") == TOPIC_KB_AVAILABILITY_FACTS

    def test_catalog_product_availability_not_kb_route(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _install_kb_stubs(monkeypatch, [_bee_packages_kb()])
        decision = try_non_catalog_availability_kb_decision(
            _ctx(
                "هل عسل السمر متوفر؟",
                db=db,
                catalog_skus=[_sku(4, "عسل السمر 2025"), _sku(5, "عسل السمر 2024")],
            ),
        )
        assert decision is None

    def test_solution_seeking_not_kb_availability_route(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _install_kb_stubs(monkeypatch, [_bee_packages_kb()])
        message = "أبي عسل للمناعة"
        assert classify_solution_seeking_commerce(message) is not None
        decision = try_non_catalog_availability_kb_decision(
            _ctx(message, db=db, catalog_skus=[_sku(6, "عسل للمناعة")]),
        )
        assert decision is None

    def test_no_kb_hit_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _install_kb_stubs(monkeypatch, [])
        decision = try_non_catalog_availability_kb_decision(
            _ctx("في عندك طرود نحل؟", db=db),
        )
        assert decision is None

    def test_greeting_then_availability_routes_to_kb(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _install_kb_stubs(monkeypatch, [_bee_packages_kb()])
        message = "صباح الخير\nفي عندك طرود نحل ؟"
        decision = try_non_catalog_availability_kb_decision(
            _ctx(message, db=db, catalog_skus=[_sku(10, "عسل سمر الحجاز 2025")]),
        )
        assert decision is not None
        assert decision.args.get("topic") == TOPIC_KB_AVAILABILITY_FACTS
        assert decision.args.get("availability_polarity") == "negative"

    def test_space_before_question_mark_still_routes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _install_kb_stubs(monkeypatch, [_bee_packages_kb()])
        decision = try_non_catalog_availability_kb_decision(
            _ctx("في عندك طرود نحل ؟", db=db),
        )
        assert decision is not None
        assert decision.args.get("topic") == TOPIC_KB_AVAILABILITY_FACTS


class TestKB31DbContextFix:
    def test_private_db_attr_routes_kb_decision(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _install_kb_stubs(monkeypatch, [_bee_packages_kb()])
        decision = try_non_catalog_availability_kb_decision(
            _ctx(
                "في عندك طرود نحل؟",
                db=db,
                use_private_db=True,
                catalog_skus=[_sku(1, "عسل سمر الحجاز 2025")],
            ),
        )
        assert decision is not None
        assert decision.args.get("topic") == TOPIC_KB_AVAILABILITY_FACTS
        assert decision.args.get("availability_polarity") == "negative"

    def test_public_db_attr_still_routes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _install_kb_stubs(monkeypatch, [_bee_packages_kb()])
        decision = try_non_catalog_availability_kb_decision(
            _ctx("في عندك طرود نحل؟", db=db),
        )
        assert decision is not None
        assert decision.args.get("topic") == TOPIC_KB_AVAILABILITY_FACTS

    def test_greeting_with_private_db_routes_kb(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _install_kb_stubs(monkeypatch, [_bee_packages_kb()])
        message = "صباح الخير\nفي عندك طرود نحل ؟"
        decision = try_non_catalog_availability_kb_decision(
            _ctx(
                message,
                db=db,
                use_private_db=True,
                catalog_skus=[_sku(10, "عسل سمر الحجاز 2025")],
            ),
        )
        assert decision is not None
        assert decision.args.get("topic") == TOPIC_KB_AVAILABILITY_FACTS

    def test_no_kb_hit_with_private_db_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(monkeypatch, [])
        decision = try_non_catalog_availability_kb_decision(
            _ctx("في عندك طرود نحل؟", db=db, use_private_db=True),
        )
        assert decision is None

    def test_catalog_product_with_private_db_not_kb_route(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(monkeypatch, [_bee_packages_kb()])
        decision = try_non_catalog_availability_kb_decision(
            _ctx(
                "هل عسل السمر متوفر؟",
                db=db,
                use_private_db=True,
                catalog_skus=[_sku(4, "عسل السمر 2025"), _sku(5, "عسل السمر 2024")],
            ),
        )
        assert decision is None


class TestKB3GreetingAvailabilityRegression:
    def test_extract_subject_after_greeting(self) -> None:
        from modules.ai.brain.commerce.commerce_inquiry_boundary import (  # noqa: E402
            extract_inquiry_subject,
        )

        assert extract_inquiry_subject("صباح الخير\nفي عندك طرود نحل ؟") == "طرود نحل"
        assert extract_inquiry_subject("في عندك طرود نحل ؟") == "طرود نحل"
        assert extract_inquiry_subject("فيه عندك طرود نحل؟") == "طرود نحل"
        assert extract_inquiry_subject("صباح الخير\nفيه عندك طرود نحل؟") == "طرود نحل"


class TestKB3GuardIntegration:
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

    def test_kb_negative_not_rewritten_by_catalog_grounding_guard(self) -> None:
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

    def test_kb_negative_not_rewritten_by_availability_guard(self) -> None:
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

    def test_no_kb_hit_unknown_still_guarded_without_positive_claim(self) -> None:
        result = apply_product_availability_truth_guard(
            reply="متوفر طرود نحل بعدة خيارات.",
            availability_context={
                "platform_connected": True,
                "catalog_skus": [_sku(8, "عسل طلح")],
                "kb_signals": [],
                "product_links": [],
            },
            inbound_text="في عندك طرود نحل؟",
            chosen_path="llm",
            tenant_id=1,
        )
        assert result.replaced is True
        assert "متوفر" not in result.reply

    def test_options_variety_claim_rewritten_without_evidence(self) -> None:
        live_reply = (
            "حاضر، صباح النور! بالنسبة لطرود النحل، عندنا تشكيلة متنوعة. "
            "إذا تبي تفاصيل أكثر أو أي شيء معين، خبرني!"
        )
        result = apply_product_availability_truth_guard(
            reply=live_reply,
            availability_context={
                "platform_connected": True,
                "catalog_skus": [_sku(11, "عسل طلح")],
                "kb_signals": [],
                "product_links": [],
            },
            inbound_text="صباح الخير\nفي عندك طرود نحل ؟",
            chosen_path="llm",
            tenant_id=1,
        )
        assert result.replaced is True
        assert "تشكيلة متنوعة" not in result.reply
        assert "متوفر" not in result.reply
        assert "خيارات" not in result.reply


class TestKB1KB2Regression:
    @pytest.mark.parametrize(
        "message",
        [
            "في عندك طرود نحل؟",
            "عندكم طرود نحل؟",
        ],
    )
    def test_kb1_bare_availability_not_solution_seeking(self, message: str) -> None:
        assert classify_solution_seeking_commerce(message) is None

    def test_kb2_unknown_does_not_invent_motawfir(self) -> None:
        from modules.ai.brain.postprocess.product_availability_evidence import (  # noqa: E402
            EVIDENCE_UNKNOWN,
            evaluate_product_availability_evidence,
        )
        from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
            _UNKNOWN_REPLY_AR,
            build_operational_availability_conflict_reply,
        )

        ev = evaluate_product_availability_evidence(
            availability_context={
                "platform_connected": True,
                "catalog_skus": [_sku(9, "Honey Type A")],
                "kb_signals": [],
                "product_links": [],
            },
            inbound_text="في عندك طرود نحل؟",
        )
        assert ev.evidence_state == EVIDENCE_UNKNOWN
        reply = build_operational_availability_conflict_reply(
            ev,
            inbound_text="في عندك طرود نحل؟",
        )
        assert "متوفر" not in reply
        assert reply == _UNKNOWN_REPLY_AR
