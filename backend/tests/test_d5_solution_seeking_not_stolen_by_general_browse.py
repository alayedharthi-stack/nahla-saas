"""D5 — solution-seeking must not be stolen by catalog browse or identity hygiene.

Catalog navigation and identity_collaboration must yield to the turn's
existing solution_seeking_commerce intent. Full-engine tests exercise
DefaultDecisionEngine.decide with real owner ordering.
Do not assert exact Arabic model wording.
"""
from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
_DATABASE = os.path.join(_REPO, "database")
for _p in (_REPO, _BACKEND, _DATABASE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.catalog.discovery_presenter import (  # noqa: E402
    DiscoveryPresentationComposer,
)
from modules.ai.brain.catalog.navigation import (  # noqa: E402
    PATH_GROUPS,
    try_catalog_navigation_decision,
)
from modules.ai.brain.catalog.navigation_signals import (  # noqa: E402
    evaluate_catalog_navigation_signals,
)
from modules.ai.brain.commerce.commerce_navigator import (  # noqa: E402
    resolve_commerce_navigator,
)
from modules.ai.brain.commerce.entity_extraction_guard import (  # noqa: E402
    is_identity_collaboration_without_purchase,
)
from modules.ai.brain.commerce.identity_collaboration_guard import (  # noqa: E402
    TOPIC_IDENTITY_COLLABORATION,
    compose_identity_collaboration_goal,
    try_identity_collaboration_decision,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_LLM_REPLY,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.discovery.entry import (  # noqa: E402
    TOP_PRODUCTS,
    resolve_discovery_entry,
)
from modules.ai.brain.execution.catalog_navigate import (  # noqa: E402
    CatalogNavigateHandler,
)
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.persona.compose_guards import (  # noqa: E402
    apply_persona_compose_guards,
)
from modules.ai.brain.persona.kb_product_answer import (  # noqa: E402
    build_kb_product_answer_facts_bundle,
)
from modules.ai.brain.persona_ownership import (  # noqa: E402
    build_brain_persona_ownership,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_SOLUTION_SEEKING_COMMERCE,
    Intent,
    MerchantConversationState,
)

LIVE = (
    "انا والله ما ادري ايش افضل شئ للتداوي السدير ولا الطالح "
    "لكن هو قال لي السدير عندكم كويس هو الرجل كده مدحل كده بالذات يعني"
)
MSG_PURE_BROWSE = "ايش عندكم؟"
MSG_PURCHASE = "أبي أطلب"
MSG_EXPLICIT_COMPARE = "أيهما أفضل، السدر أم الطلح؟"
MSG_SHAPE = (
    "ما أدري ايش أفضل شيء للمنتج، القميص ولا الحذاء، "
    "وقالوا إن القميص عندكم كويس"
)
MSG_TYPES = "وش الانواع الي عندكم"
MSG_BESTSELLERS = "وريني الأكثر مبيعًا"
# Existing identity/collaboration fixtures from test_identity_intro_product_label_guard.
MSG_TRUE_INTRO = "انا معلم في النحل وحبيت ادوم معاكم"
MSG_TRUE_COLLAB = "حاب اتعاون معكم"
MSG_LONG_NON_PRODUCT = (
    "اليوم كان يوم طويل وجلست مع العائلة في البيت وتكلمنا عن السفر "
    "والمدارس والطقس والزيارات"
)

COLLECTIONS = [
    {"group_id": "shoes", "group_name": "الأحذية", "browse_rank": 1},
    {"group_id": "shirts", "group_name": "القمصان", "browse_rank": 2},
]

_STORE = "https://shop.example"
_MAPS = "https://maps.example.com/showroom"
_MODEL_CANDIDATE = "نموذج مقارنة تجريبي بدون قائمة مجموعات."


class DummyDB:
    """Truthy DB stand-in so engine step 0a.52 runs. Queries fail closed."""

    def query(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("d5 full-engine fixture: no live db session")

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("d5 full-engine fixture: no live db session")


def _facts(*, store_url: str = "", maps_url: str = "") -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=24,
        in_stock_count=24,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="متجر تجريبي عام",
        store_url=store_url,
        maps_url=maps_url,
        top_products=[
            {
                "id": "501",
                "external_id": "sku-white-shoe",
                "title": "حذاء رياضي أبيض",
                "price": 249,
            },
        ],
    )


def _ctx(
    msg: str,
    *,
    tenant_id: int = 11,
    store_url: str = "",
    maps_url: str = "",
    source_type: str | None = None,
) -> BrainContext:
    intent = rules.match(msg)
    if intent is None:
        intent = Intent(name="general", confidence=0.5, raw_message=msg)
    ctx = BrainContext(
        tenant_id=tenant_id,
        customer_phone="966500000001",
        conversation_id=9001,
        message=msg,
        intent=intent,
        state=MerchantConversationState(greeted=True, stage="discovery", turn=2),
        facts=_facts(store_url=store_url, maps_url=maps_url),
        raw_message=msg,
    )
    ctx._db = DummyDB()  # type: ignore[attr-defined]
    if source_type:
        setattr(ctx, "source_type", source_type)
    return ctx


def _engine_decide(ctx: BrainContext) -> Any:
    """Full DefaultDecisionEngine.decide — no semantic-owner patches."""
    return DefaultDecisionEngine().decide(ctx)


class TestCatalogNavigationUnit:
    """Isolated catalog-navigator unit test. Not proof of final engine ownership."""

    def test_live_transcript_catalog_navigator_yields(self) -> None:
        ctx = _ctx(LIVE)
        assert ctx.intent.name == INTENT_SOLUTION_SEEKING_COMMERCE
        signals = evaluate_catalog_navigation_signals(ctx)
        assert signals.evidence.get("general_store_browse") is True
        assert signals.catalog_browse_intent is False
        assert signals.exit_reason == "authoritative_solution_seeking"
        with patch(
            "modules.ai.brain.catalog.navigation._load_catalog_groups",
            return_value=COLLECTIONS,
        ):
            nav = try_catalog_navigation_decision(ctx)
        assert nav is None


class TestALiveTranscriptFullEngine:
    def test_live_transcript_reaches_solution_seeking_through_real_owners(self) -> None:
        ctx = _ctx(LIVE)
        assert ctx.intent.name == INTENT_SOLUTION_SEEKING_COMMERCE
        assert is_identity_collaboration_without_purchase(LIVE) is True
        nav = try_catalog_navigation_decision(ctx)
        identity = try_identity_collaboration_decision(ctx, route="d5_full_engine")
        assert nav is None
        assert identity is None
        with patch(
            "modules.ai.brain.catalog.navigation.try_catalog_navigation_decision",
            wraps=try_catalog_navigation_decision,
        ) as nav_spy:
            with patch(
                "modules.ai.brain.commerce.identity_collaboration_guard.try_identity_collaboration_decision",
                wraps=try_identity_collaboration_decision,
            ) as identity_spy:
                with patch.object(
                    DiscoveryPresentationComposer,
                    "_compose_collections",
                    autospec=True,
                ) as compose_collections:
                    decision = _engine_decide(_ctx(LIVE))
        assert nav_spy.called
        assert identity_spy.called
        assert all(call is not None for call in (nav_spy.call_args, identity_spy.call_args))
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "solution_seeking_commerce"
        assert decision.args.get("topic") != TOPIC_IDENTITY_COLLABORATION
        assert decision.action != ACTION_CATALOG_NAVIGATE
        assert decision.args.get("chosen_path") != PATH_GROUPS
        assert decision.args.get("topic") != "purchase_channel_selection"
        compose_collections.assert_not_called()


class TestBVoiceTextParity:
    def test_audio_envelope_and_plain_text_same_final_owner(self) -> None:
        text_dec = _engine_decide(_ctx(LIVE))
        audio_dec = _engine_decide(_ctx(LIVE, source_type="audio"))
        assert text_dec.action == audio_dec.action
        assert text_dec.args.get("topic") == audio_dec.args.get("topic")
        assert text_dec.args.get("topic") == "solution_seeking_commerce"


class TestCTruePersonalIntroduction:
    def test_existing_intro_fixture_keeps_identity_collaboration(self) -> None:
        ctx = _ctx(MSG_TRUE_INTRO)
        assert ctx.intent.name != INTENT_SOLUTION_SEEKING_COMMERCE
        identity = try_identity_collaboration_decision(ctx)
        assert identity is not None
        assert identity.args.get("topic") == TOPIC_IDENTITY_COLLABORATION
        decision = _engine_decide(ctx)
        assert decision.args.get("topic") == TOPIC_IDENTITY_COLLABORATION


class TestDTrueCollaborationRequest:
    def test_existing_collaboration_fixture_keeps_identity_collaboration(self) -> None:
        ctx = _ctx(MSG_TRUE_COLLAB)
        assert ctx.intent.name != INTENT_SOLUTION_SEEKING_COMMERCE
        identity = try_identity_collaboration_decision(ctx)
        assert identity is not None
        assert identity.args.get("topic") == TOPIC_IDENTITY_COLLABORATION
        decision = _engine_decide(ctx)
        assert decision.args.get("topic") == TOPIC_IDENTITY_COLLABORATION


class TestELongNonProductSentence:
    def test_long_non_commerce_sentence_keeps_hygiene_identity_owner(self) -> None:
        ctx = _ctx(MSG_LONG_NON_PRODUCT)
        assert ctx.intent.name != INTENT_SOLUTION_SEEKING_COMMERCE
        assert is_identity_collaboration_without_purchase(MSG_LONG_NON_PRODUCT) is True
        identity = try_identity_collaboration_decision(ctx)
        assert identity is not None
        assert identity.args.get("topic") == TOPIC_IDENTITY_COLLABORATION
        decision = _engine_decide(ctx)
        # Earlier non-commerce owners may still win; hygiene itself must not yield.
        assert decision.args.get("topic") != "solution_seeking_commerce"
        assert decision.action != ACTION_CATALOG_NAVIGATE
        assert decision.args.get("chosen_path") != PATH_GROUPS
        assert decision.args.get("topic") != "purchase_channel_selection"


class TestFLongSolutionSeekingSentence:
    def test_ana_lead_solution_seeking_is_not_stolen_by_identity(self) -> None:
        ctx = _ctx(LIVE)
        assert ctx.intent.name == INTENT_SOLUTION_SEEKING_COMMERCE
        assert LIVE.startswith("انا")
        assert try_identity_collaboration_decision(ctx) is None
        decision = _engine_decide(ctx)
        assert decision.args.get("topic") == "solution_seeking_commerce"


class TestGPureCatalogBrowse:
    def test_ish_indakum_keeps_current_catalog_owner(self) -> None:
        ctx = _ctx(MSG_PURE_BROWSE)
        assert ctx.intent.name != INTENT_SOLUTION_SEEKING_COMMERCE
        decision = _engine_decide(ctx)
        assert decision.action == ACTION_CATALOG_NAVIGATE
        assert decision.args.get("topic") != TOPIC_IDENTITY_COLLABORATION
        assert decision.args.get("topic") != "solution_seeking_commerce"


class TestHPurchaseStart:
    def test_abi_atlub_keeps_purchase_channel_selection(self) -> None:
        ctx = _ctx(MSG_PURCHASE, store_url=_STORE, maps_url=_MAPS)
        assert resolve_commerce_navigator(
            message=MSG_PURCHASE,
            intent_name=ctx.intent.name,
            store_url=_STORE,
            maps_url=_MAPS,
        ).stage == "purchase_channel_selection"
        decision = _engine_decide(ctx)
        assert decision.action != ACTION_CATALOG_NAVIGATE
        assert decision.args.get("topic") == "purchase_channel_selection"


class TestIComparison:
    def test_ayyuhuma_is_not_identity_or_groups(self) -> None:
        ctx = _ctx(MSG_EXPLICIT_COMPARE)
        signals = evaluate_catalog_navigation_signals(ctx)
        assert signals.advisory_or_comparison is True
        assert try_catalog_navigation_decision(ctx) is None
        decision = _engine_decide(ctx)
        assert decision.args.get("topic") != TOPIC_IDENTITY_COLLABORATION
        assert decision.action != ACTION_CATALOG_NAVIGATE
        assert decision.args.get("chosen_path") != PATH_GROUPS


class TestJExplicitCatalogDiscoveryControls:
    def test_types_overview_fixture_is_not_identity(self) -> None:
        ctx = _ctx(MSG_TYPES)
        decision = _engine_decide(ctx)
        assert decision.args.get("topic") != TOPIC_IDENTITY_COLLABORATION
        assert decision.args.get("topic") != "solution_seeking_commerce"

    def test_bestsellers_fixture_keeps_discovery_or_catalog_owner(self) -> None:
        ctx = _ctx(MSG_BESTSELLERS)
        entry = resolve_discovery_entry(ctx)
        assert entry.matched is True
        assert entry.entry_type == TOP_PRODUCTS
        decision = _engine_decide(ctx)
        assert decision.args.get("topic") != TOPIC_IDENTITY_COLLABORATION
        assert decision.args.get("chosen_path") != PATH_GROUPS or decision.action == ACTION_CATALOG_NAVIGATE


class TestKUnresolvedEntities:
    def test_unresolved_advisory_is_generative_not_collections_or_identity(self) -> None:
        ctx = _ctx(LIVE)
        decision = _engine_decide(ctx)
        assert decision.action != ACTION_CATALOG_NAVIGATE
        assert decision.args.get("chosen_path") != PATH_GROUPS
        assert decision.args.get("topic") != TOPIC_IDENTITY_COLLABORATION
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "solution_seeking_commerce"


class TestLMedicalSafety:
    def test_live_path_does_not_emit_treatment_claim(self) -> None:
        decision = _engine_decide(_ctx(LIVE))
        blob = str(decision.args)
        assert "يعالج" not in blob
        assert "يشفي" not in blob
        assert "cure" not in blob.lower()
        bundle = build_kb_product_answer_facts_bundle(
            inbound_text=LIVE,
            question_kind="features",
            allowed_facts={
                "product_title": "منتج تجريبي عام",
                "kb_sections": [
                    {
                        "section_id": 1,
                        "title": "منتج تجريبي عام",
                        "body": "وصف عام بدون ادعاء علاج.",
                        "kind": "product_info",
                    }
                ],
            },
        )
        guarded = apply_persona_compose_guards("هذا يعالج المرض ويشفي", bundle)
        assert guarded.passed is False
        assert guarded.failed_reason in {"medical_claim", "unsupported_cure_claim"}


class TestMComposeOwnership:
    def test_stubbed_model_candidate_not_replaced_by_catalog_or_identity(self) -> None:
        ctx = _ctx(LIVE)
        with patch.object(
            DiscoveryPresentationComposer,
            "_compose_collections",
            autospec=True,
        ) as compose_collections:
            with patch.object(
                CatalogNavigateHandler,
                "_render_groups",
                autospec=True,
            ) as render_groups:
                decision = _engine_decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "solution_seeking_commerce"
        assert compose_identity_collaboration_goal() not in str(decision.args.get("response_goal") or "")
        compose_collections.assert_not_called()
        render_groups.assert_not_called()
        candidate = _MODEL_CANDIDATE
        final_body = candidate
        ownership = build_brain_persona_ownership(
            decision_action=decision.action,
            decision_args=decision.args,
            reply_state=None,
            chosen_path=str(decision.args.get("chosen_path") or ""),
            compose_source="llm",
            llm_candidate_present=True,
            final_customer_text_source="llm",
            final_text_transformed=False,
            compose_reply_candidate=candidate,
            final_reply=final_body,
        )
        assert ownership.expression_owner in {"llm_compose", "persona_llm"}
        assert ownership.compose_pass_count <= 1
        assert final_body == candidate
        assert PATH_GROUPS not in final_body
        assert "identity_collaboration" not in final_body


class TestNNoStateMutation:
    def test_live_transcript_does_not_start_checkout_or_purchase(self) -> None:
        ctx = _ctx(LIVE, store_url=_STORE, maps_url=_MAPS)
        before_stage = ctx.state.stage
        decision = _engine_decide(ctx)
        assert ctx.state.stage == before_stage
        assert getattr(ctx.state, "draft_order_id", None) in {None, ""}
        assert decision.args.get("topic") != "purchase_channel_selection"
        assert ctx.intent.name != "start_order"


class TestOTenantNeutrality:
    def test_generic_tenants_same_final_owner(self) -> None:
        a = _engine_decide(_ctx(LIVE, tenant_id=11))
        b = _engine_decide(_ctx(LIVE, tenant_id=77))
        assert a.args.get("topic") == b.args.get("topic") == "solution_seeking_commerce"
        src = open(
            os.path.join(_BACKEND, "modules", "ai", "brain", "commerce", "identity_collaboration_guard.py"),
            encoding="utf-8",
        ).read()
        assert "tenant_id == 33" not in src
        assert "tenant_id==33" not in src


class TestPNonInterference:
    def test_production_files_have_no_new_language_or_heuristic_rules(self) -> None:
        nav = open(
            os.path.join(_BACKEND, "modules", "ai", "brain", "catalog", "navigation.py"),
            encoding="utf-8",
        ).read()
        signals = open(
            os.path.join(_BACKEND, "modules", "ai", "brain", "catalog", "navigation_signals.py"),
            encoding="utf-8",
        ).read()
        identity = open(
            os.path.join(_BACKEND, "modules", "ai", "brain", "commerce", "identity_collaboration_guard.py"),
            encoding="utf-8",
        ).read()
        combined = nav + signals + identity
        assert "السدير" not in combined
        assert "الطالح" not in combined
        assert "tenant_id == 33" not in combined
        assert "len(words) >" not in identity.replace("len(words) > 8", "")
        assert "classify_solution_seeking_commerce(" not in identity
        assert "source_type" not in identity
        hygiene = open(
            os.path.join(_BACKEND, "modules", "ai", "brain", "commerce", "product_label_hygiene.py"),
            encoding="utf-8",
        ).read()
        assert "len(words) > 8" in hygiene
        assert "_IDENTITY_PRONOUN_LEAD_RE" in hygiene
