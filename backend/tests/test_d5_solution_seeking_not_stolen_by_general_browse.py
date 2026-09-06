"""D5 — solution-seeking must not be stolen by general_store_browse.

Repair is ownership/yield from the existing intent on the turn.
Do not add customer-language regexes, product aliases, or voice branches.
Assert routing, ownership, provenance, and compose mode — not Arabic wording.
"""
from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

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
MSG_UNRESOLVED = "ما أدري أيهما أفضل للاستخدام، الاسم الأول ولا الاسم الثاني؟"
MSG_TYPES = "وش الانواع الي عندكم"
MSG_BESTSELLERS = "وريني الأكثر مبيعًا"
MSG_PRAISE = LIVE

COLLECTIONS = [
    {"group_id": "shoes", "group_name": "الأحذية", "browse_rank": 1},
    {"group_id": "shirts", "group_name": "القمصان", "browse_rank": 2},
]

_STORE = "https://shop.example"
_MAPS = "https://maps.example.com/showroom"


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
    intent_name: str | None = None,
    store_url: str = "",
    maps_url: str = "",
    source_type: str | None = None,
) -> BrainContext:
    intent = rules.match(msg)
    if intent is None:
        intent = Intent(
            name=intent_name or "general",
            confidence=0.5,
            raw_message=msg,
        )
    if intent_name:
        intent = Intent(name=intent_name, confidence=0.94, raw_message=msg)
    ctx = BrainContext(
        tenant_id=tenant_id,
        customer_phone="966500000001",
        conversation_id=9001,
        message=msg,
        intent=intent,
        state=MerchantConversationState(greeted=True, stage="discovery", turn=1),
        facts=_facts(store_url=store_url, maps_url=maps_url),
    )
    ctx._db = MagicMock()  # type: ignore[attr-defined]
    if source_type:
        ctx.raw_message = msg
        setattr(ctx, "source_type", source_type)
    return ctx


def _nav(ctx: BrainContext) -> Any:
    with patch(
        "modules.ai.brain.catalog.navigation._load_catalog_groups",
        return_value=COLLECTIONS,
    ), patch(
        "modules.ai.brain.catalog.navigation._try_native_catalog_entry_decision",
        return_value=None,
    ):
        return try_catalog_navigation_decision(ctx)


def _decide(ctx: BrainContext) -> Any:
    # Isolate D5 catalog yield from later pre-existing owners that fire on
    # long free-text (identity/collaboration word-count heuristic). Those
    # owners are out of this repair's two-file catalog scope.
    with patch(
        "modules.ai.brain.catalog.navigation._load_catalog_groups",
        return_value=COLLECTIONS,
    ), patch(
        "modules.ai.brain.catalog.navigation._try_native_catalog_entry_decision",
        return_value=None,
    ), patch(
        "modules.ai.brain.commerce.identity_collaboration_guard.try_identity_collaboration_decision",
        return_value=None,
    ):
        return DefaultDecisionEngine().decide(ctx)


def _ownership(ctx: BrainContext) -> dict[str, Any]:
    signals = evaluate_catalog_navigation_signals(ctx)
    nav = _nav(ctx)
    decision = _decide(ctx)
    return {
        "intent": ctx.intent.name,
        "signals": signals,
        "nav": nav,
        "decision": decision,
    }


class TestALiveTranscript:
    def test_live_transcript_keeps_solution_seeking_not_groups(self) -> None:
        ctx = _ctx(LIVE)
        assert ctx.intent.name == INTENT_SOLUTION_SEEKING_COMMERCE
        owned = _ownership(ctx)
        signals = owned["signals"]
        assert signals.evidence.get("general_store_browse") is True
        assert signals.catalog_browse_intent is False
        assert signals.exit_reason == "authoritative_solution_seeking"
        assert owned["nav"] is None
        decision = owned["decision"]
        assert decision.action != ACTION_CATALOG_NAVIGATE
        assert decision.args.get("chosen_path") != PATH_GROUPS
        assert decision.args.get("topic") != "purchase_channel_selection"
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "solution_seeking_commerce"


class TestBVoiceTextParity:
    def test_audio_envelope_and_plain_text_same_owner(self) -> None:
        text = _ownership(_ctx(LIVE))
        audio = _ownership(_ctx(LIVE, source_type="audio"))
        assert text["intent"] == audio["intent"] == INTENT_SOLUTION_SEEKING_COMMERCE
        assert (text["nav"] is None) and (audio["nav"] is None)
        assert text["decision"].action == audio["decision"].action
        assert text["decision"].args.get("topic") == audio["decision"].args.get("topic")


class TestCPureBrowse:
    def test_ish_indakum_retains_catalog_browse(self) -> None:
        ctx = _ctx(MSG_PURE_BROWSE)
        assert ctx.intent.name != INTENT_SOLUTION_SEEKING_COMMERCE
        owned = _ownership(ctx)
        assert owned["signals"].catalog_browse_intent is True
        assert owned["nav"] is not None
        assert owned["nav"].action == ACTION_CATALOG_NAVIGATE
        assert owned["nav"].args.get("chosen_path") == PATH_GROUPS


class TestDPurchase:
    def test_abi_atlub_retains_purchase_channel_selection(self) -> None:
        ctx = _ctx(MSG_PURCHASE, store_url=_STORE, maps_url=_MAPS)
        nav_stage = resolve_commerce_navigator(
            message=MSG_PURCHASE,
            intent_name=ctx.intent.name,
            store_url=_STORE,
            maps_url=_MAPS,
        ).stage
        assert nav_stage == "purchase_channel_selection"
        owned = _ownership(ctx)
        assert owned["nav"] is None
        decision = owned["decision"]
        assert decision.action != ACTION_CATALOG_NAVIGATE
        assert decision.args.get("topic") == "purchase_channel_selection"


class TestEExplicitComparison:
    def test_ayyuhuma_does_not_open_category_menu(self) -> None:
        ctx = _ctx(MSG_EXPLICIT_COMPARE)
        owned = _ownership(ctx)
        assert owned["signals"].advisory_or_comparison is True
        assert owned["nav"] is None
        assert owned["decision"].action != ACTION_CATALOG_NAVIGATE
        assert owned["decision"].args.get("chosen_path") != PATH_GROUPS


class TestFLiveGrammaticalShape:
    def test_generic_shape_keeps_solution_seeking(self) -> None:
        ctx = _ctx(MSG_SHAPE)
        assert ctx.intent.name == INTENT_SOLUTION_SEEKING_COMMERCE
        owned = _ownership(ctx)
        assert owned["nav"] is None
        assert owned["decision"].args.get("topic") == "solution_seeking_commerce"
        assert owned["decision"].args.get("chosen_path") != PATH_GROUPS


class TestGUnresolvedProducts:
    def test_unresolved_advisory_does_not_open_collections(self) -> None:
        ctx = _ctx(MSG_UNRESOLVED)
        owned = _ownership(ctx)
        assert owned["nav"] is None
        assert owned["decision"].action != ACTION_CATALOG_NAVIGATE
        assert owned["decision"].args.get("chosen_path") != PATH_GROUPS
        assert owned["decision"].action in {ACTION_LLM_REPLY}


class TestHExplicitCategory:
    def test_types_overview_keeps_existing_catalog_owner(self) -> None:
        ctx = _ctx(MSG_TYPES)
        owned = _ownership(ctx)
        assert owned["nav"] is not None
        assert owned["nav"].action == ACTION_CATALOG_NAVIGATE
        assert owned["nav"].args.get("chosen_path") == PATH_GROUPS


class TestIBestsellers:
    def test_top_products_discovery_owner_preserved(self) -> None:
        ctx = _ctx(MSG_BESTSELLERS)
        entry = resolve_discovery_entry(ctx)
        assert entry.matched is True
        assert entry.entry_type == TOP_PRODUCTS
        nav = _nav(ctx)
        assert nav is None or nav.args.get("chosen_path") != PATH_GROUPS


class TestJReportedPraise:
    def test_merchant_scope_in_praise_does_not_override_solution_seeking(self) -> None:
        ctx = _ctx(MSG_PRAISE, tenant_id=11)
        signals = evaluate_catalog_navigation_signals(ctx)
        assert signals.evidence.get("general_store_browse") is True
        assert signals.catalog_browse_intent is False
        assert _nav(ctx) is None
        decision = _decide(ctx)
        assert decision.args.get("topic") == "solution_seeking_commerce"


class TestKNoPurchaseInvention:
    def test_live_transcript_is_not_start_order(self) -> None:
        ctx = _ctx(LIVE, store_url=_STORE, maps_url=_MAPS)
        assert ctx.intent.name != "start_order"
        decision = _decide(ctx)
        assert decision.args.get("topic") != "purchase_channel_selection"
        assert resolve_commerce_navigator(
            message=LIVE,
            intent_name=ctx.intent.name,
            store_url=_STORE,
            maps_url=_MAPS,
        ).stage != "purchase_channel_selection"


class TestLMedicalSafety:
    def test_path_does_not_emit_treatment_claim(self) -> None:
        ctx = _ctx(LIVE)
        decision = _decide(ctx)
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
        guarded = apply_persona_compose_guards(
            "هذا يعالج المرض ويشفي",
            bundle,
        )
        assert guarded.passed is False
        assert guarded.failed_reason in {"medical_claim", "unsupported_cure_claim"}


class TestMTenantNeutrality:
    def test_generic_tenant_same_ownership(self) -> None:
        a = _ownership(_ctx(LIVE, tenant_id=11))
        b = _ownership(_ctx(LIVE, tenant_id=77))
        assert a["intent"] == b["intent"] == INTENT_SOLUTION_SEEKING_COMMERCE
        assert a["decision"].args.get("topic") == b["decision"].args.get("topic")
        assert a["nav"] is None and b["nav"] is None


class TestNNoVoiceBranch:
    def test_source_type_does_not_change_decision(self) -> None:
        plain = _decide(_ctx(LIVE))
        audio = _decide(_ctx(LIVE, source_type="audio"))
        assert plain.action == audio.action
        assert plain.args.get("topic") == audio.args.get("topic")
        src = open(
            os.path.join(_BACKEND, "modules", "ai", "brain", "catalog", "navigation.py"),
            encoding="utf-8",
        ).read()
        sig = open(
            os.path.join(_BACKEND, "modules", "ai", "brain", "catalog", "navigation_signals.py"),
            encoding="utf-8",
        ).read()
        combined = src + sig
        assert "source_type" not in combined
        assert "voice=true" not in combined
        assert "normalized_type" not in combined


class TestOModelOwnership:
    def test_collections_composer_not_invoked_on_live_turn(self) -> None:
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
                decision = _decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "solution_seeking_commerce"
        assert decision.args.get("expression_owner") != "catalog_navigate"
        compose_collections.assert_not_called()
        render_groups.assert_not_called()
        assert decision.action != ACTION_CATALOG_NAVIGATE


class TestPNonInterference:
    def test_production_files_have_no_new_language_rules(self) -> None:
        nav = open(
            os.path.join(_BACKEND, "modules", "ai", "brain", "catalog", "navigation.py"),
            encoding="utf-8",
        ).read()
        signals = open(
            os.path.join(_BACKEND, "modules", "ai", "brain", "catalog", "navigation_signals.py"),
            encoding="utf-8",
        ).read()
        combined = nav + signals
        assert "السدير" not in combined
        assert "الطالح" not in combined
        assert "tenant_id == 33" not in combined
        assert "tenant_id==33" not in combined
        assert "classify_solution_seeking_commerce" not in nav
        assert "classify_solution_seeking_commerce" not in signals
