"""Tests for goal-based commerce P0 (platform modules — no merchant SKU hardcoding)."""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.goal.bundle_composition import compose_regimen_bundle
from modules.ai.brain.commerce.goal.goal_reasoning import detect_customer_goal
from modules.ai.brain.commerce.goal.goal_retrieval import GoalKBEntry
from modules.ai.brain.commerce.goal.goal_schema import GoalKBMetadata
from modules.ai.brain.commerce.goal.goal_taxonomy import (
    GOAL_TAXONOMY,
    GoalTag,
    normalize_goal_tag,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    INTENT_NEED_BASED_PRODUCT_ADVICE,
    Intent,
    MerchantConversationState,
)


class _FakeProduct:
    def __init__(self, pid: int, title: str, *, active: bool = True):
        self.id = pid
        self.title = title
        self.external_id = f"ext-{pid}"
        self.sku = ""
        self.price = 100.0
        self.is_active = active


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def limit(self, n):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, products):
        self._products = products

    def query(self, model):
        return _FakeQuery(self._products)


def test_goal_taxonomy_closed():
    assert normalize_goal_tag("fertility_vitality") == GoalTag.FERTILITY_VITALITY.value
    assert normalize_goal_tag("unknown_goal_xyz") is None
    assert len(GOAL_TAXONOMY) >= 8


def test_detect_fertility_goal_no_products():
    m = detect_customer_goal("أبي شيء للخصوبة")
    assert m is not None
    assert m.goal == GoalTag.FERTILITY_VITALITY.value


def test_platform_modules_contain_no_merchant_skus():
    """Invariant: platform code must not hardcode merchant product names."""
    import modules.ai.brain.commerce.goal.goal_reasoning as gr
    import modules.ai.brain.commerce.goal.goal_taxonomy as gt

    forbidden = ("عسل طلح", "غذاء ملكات", "حبوب لقاح", "royal jelly", "bee venom")
    for mod in (gr, gt):
        src = open(mod.__file__, encoding="utf-8").read().lower()
        for token in forbidden:
            assert token.lower() not in src, f"{mod.__name__} contains forbidden {token!r}"


def test_compose_bundle_resolves_and_excludes_unresolved():
    meta = GoalKBMetadata.from_metadata_json(
        {
            "goal_tags": ["energy_daily"],
            "products": [
                {"ref": "عسل طلح", "role": "primary"},
                {"ref": "منتج غير موجود", "role": "complement"},
            ],
            "usage_guidance": ["ملعقة صباحًا"],
            "soft_claims": ["قد يناسب"],
            "compliance": ["ليس علاجًا"],
        }
    )
    entry = GoalKBEntry(
        section_id=1,
        title="طاقة",
        body="",
        metadata=meta,
    )
    db = _FakeDB([_FakeProduct(10, "عسل طلح بلدي")])
    bundle = compose_regimen_bundle(db, 33, GoalTag.ENERGY_DAILY.value, entry)
    assert bundle.resolved_count == 1
    assert len(bundle.unresolved_refs) == 1
    assert bundle.items[0].title


def test_decision_engine_uses_goal_bundle_when_present():
    bundle_dict = {
        "goal": GoalTag.FERTILITY_VITALITY.value,
        "section_id": 1,
        "title": "خصوبة",
        "items": [{"title": "Product A", "resolved": True, "role": "primary"}],
        "resolved_count": 1,
        "unresolved_refs": [],
    }

    class _Bundle:
        goal = GoalTag.FERTILITY_VITALITY.value
        title = "خصوبة"
        resolved_count = 1
        unresolved_refs = []

        def to_dict(self):
            return bundle_dict

    ctx = BrainContext(
        tenant_id=33,
        customer_phone="966500000001",
        message="أبي شيء للخصوبة",
        intent=Intent(
            name=INTENT_NEED_BASED_PRODUCT_ADVICE,
            confidence=0.94,
            raw_message="أبي شيء للخصوبة",
        ),
        state=MerchantConversationState(),
        facts=CommerceFacts(has_products=True, orderable=True),
        goal_regimen_bundle=_Bundle(),
    )
    d = DefaultDecisionEngine().decide(ctx)
    assert d.action == ACTION_LLM_REPLY
    assert (d.args or {}).get("topic") == "goal_based_commerce"
    assert (d.args or {}).get("goal") == GoalTag.FERTILITY_VITALITY.value


def test_schema_rejects_missing_products():
    assert GoalKBMetadata.from_metadata_json({"goal_tags": ["energy_daily"]}) is None
